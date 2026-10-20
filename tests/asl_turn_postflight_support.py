"""Shared fixtures for the ``turn_postflight`` test modules.

Holds the stand-in run context, the guard recorder, the guard-name constants and
the :class:`PostflightTestCase` base class used by
``tests/test_asl_turn_postflight*.py``. Not collected as a test module.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    continuation_guards,
    events,
    handoff_guards,
    harnesses,
    plans_models,
    plans_snapshot,
    turn_postflight,
    workspace_evidence,
    write_guards,
    write_guards_legacy,
    write_guards_protected,
)
from agent_session_loop.run_errors import RunError

from .support import write_plan

RUN_ID = "run-postflight-1"
SESSION_ID = "1"

#: The one entry point the run loop is allowed to reach here. A second would be
#: a guard the loop kept sequencing itself.
ENTRY_POINTS = ("turn_postflight.enforce_post_turn_guards",)

#: The guards this boundary sequences, in the order it must run them, named as
#: ``module._name`` so a recorder can report the order it observed.
WORKSPACE_WRITE = "write_guards._enforce_workspace_write_guard"
LEGACY_WRITE = "write_guards_legacy._enforce_legacy_implementation_write_guard"
REVIEW_MUTATION = "handoff_guards._enforce_review_mutation_guard"
PROTECTED_PATH = "write_guards_protected._enforce_protected_path_guard"
PLAN_ARTIFACT = "handoff_guards._enforce_plan_artifact_guard"
DISPOSITION = "handoff_guards._terminating_result_disposition"
DISARM = "disarm_exception_guard"

#: Sequencing calls that belonged to the loop and now belong here. The two write
#: guards, the protected-path guard and the change-path projection are
#: deliberately absent: the runner's ``except BaseException`` handler re-runs
#: them from the armed crash-path context, which is a different call site with a
#: different before-image. The plan artifact guard is absent for the same kind of
#: reason -- the status-guard path below the loop's result branches has its own
#: call to it.
MOVED_CALLS = frozenset({REVIEW_MUTATION, DISPOSITION, "_workspace_review_bases"})


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real -- this boundary rewrites the turn's result artifact under
    it, and a fake that did not hold a lock would hide a missing hold rather
    than expose it -- while events are kept in order so a test can assert on
    what was recorded without reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))


class FakeRunContext:
    """A :class:`turn_postflight.RunContext` built by hand rather than by a run.

    Only the members the five guards reach are populated, which is what the
    Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {"run_id": RUN_ID}
        self.persistence = RecordingPersistence()
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.guard_controller = mock.Mock()
        self.session_attempts = continuation_guards.SessionAttempts()
        self.pauses: list[dict[str, Any]] = []

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class GuardRecorder:
    """Every guard in the sequence, replaced by a recorder of order and calls.

    Each guard's verdict is a value the test sets, so what the sequence does
    with a refusal is asserted rather than simulated: the recorder never touches
    the filesystem and never spawns a process, which is what keeps this module
    portable.
    """

    def __init__(self) -> None:
        self.order: list[str] = []
        self.calls: dict[str, dict[str, Any]] = {}
        self.verdicts: dict[str, bool] = {}
        self.disposition: str | None = "interrupted"
        self.review_bases: list[dict[str, Any]] | RunError = [{"alias": "app"}]
        self.changed_paths: tuple[str, ...] | None = ("app:src/main.py",)

    def _guard(self, name: str) -> Any:
        def recorder(*args: Any, **kwargs: Any) -> bool:
            self.order.append(name)
            self.calls[name] = kwargs
            return self.verdicts.get(name, False)

        return recorder

    def _disposition(self, *args: Any, **kwargs: Any) -> str | None:
        self.order.append(DISPOSITION)
        return self.disposition

    def _bases(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if isinstance(self.review_bases, RunError):
            raise self.review_bases
        return self.review_bases

    def disarm(self) -> None:
        self.order.append(DISARM)

    @contextlib.contextmanager
    def patched(self) -> Any:
        targets = (
            (write_guards, "_enforce_workspace_write_guard", WORKSPACE_WRITE),
            (
                write_guards_legacy,
                "_enforce_legacy_implementation_write_guard",
                LEGACY_WRITE,
            ),
            (handoff_guards, "_enforce_review_mutation_guard", REVIEW_MUTATION),
            (write_guards_protected, "_enforce_protected_path_guard", PROTECTED_PATH),
            (handoff_guards, "_enforce_plan_artifact_guard", PLAN_ARTIFACT),
        )
        with contextlib.ExitStack() as stack:
            for module, attribute, name in targets:
                stack.enter_context(
                    mock.patch.object(module, attribute, self._guard(name))
                )
            stack.enter_context(
                mock.patch.object(
                    handoff_guards,
                    "_terminating_result_disposition",
                    self._disposition,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    workspace_evidence, "_workspace_review_bases", self._bases
                )
            )
            stack.enter_context(
                mock.patch.object(
                    write_guards_protected,
                    "_protected_path_changed_paths",
                    lambda evidence: self.changed_paths,
                )
            )
            yield self


def fake_workspace(*aliases: str) -> Any:
    """A resolved workspace reduced to what the sequence hands its guards."""
    return mock.Mock(name="workspace", repositories=tuple(aliases))


class PostflightTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments the sequence needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = self.tmp_dir / "repo"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)
        write_plan(self.plan_dir, ("pending",))
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        self.recorder = GuardRecorder()

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.artifact_dir,
            "stdout": self.artifact_dir / "stdout.log",
            "stderr": self.artifact_dir / "stderr.log",
            "last_message": self.artifact_dir / "last-message.txt",
            "result": self.artifact_dir / "result.json",
        }

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        values: dict[str, Any] = {
            "argv": ["claude"],
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        values.update(overrides)
        return harnesses.HarnessResult(**values)

    def enforce(
        self,
        *,
        workspace: Any = None,
        session_scope: Any = None,
        workspace_snapshot_before: Any = None,
        legacy_snapshot_before: Any = None,
        review_fingerprint_before: dict[str, Any] | None = None,
        is_review_session: bool = False,
        turn_payload: dict[str, Any] | None = None,
        after: plans_models.PlanSnapshot | None = None,
    ) -> turn_postflight.PostTurnGuards:
        with self.recorder.patched():
            return turn_postflight.enforce_post_turn_guards(
                self.ctx,
                before=self.before,
                after=after,
                result=self.result(),
                turn_payload={} if turn_payload is None else turn_payload,
                workspace=workspace,
                session_scope=session_scope,
                workspace_snapshot_before=workspace_snapshot_before,
                legacy_snapshot_before=legacy_snapshot_before,
                review_fingerprint_before=review_fingerprint_before,
                external_digests_before={"plan": {"digest": "before"}},
                turn_started_ns=1,
                plan_artifacts_before={"session-2.md": "digest"},
                plan_artifacts_written_by_runner=(),
                is_review_session=is_review_session,
                cwd=self.cwd,
                session_id=SESSION_ID,
                session_label="Session 1",
                session_details={"session_id": SESSION_ID},
                review_guard_excludes=(".venv",),
                review_guard_include_ignored=False,
                paths=self.paths(),
                state_dir_arg=None,
                disarm_exception_guard=self.recorder.disarm,
            )

    def workspace_turn(self, **overrides: Any) -> turn_postflight.PostTurnGuards:
        """A turn in the registered-workspace mode, the common case."""
        arguments: dict[str, Any] = {
            "workspace": fake_workspace("app"),
            "session_scope": mock.Mock(name="scope"),
            "workspace_snapshot_before": {"app": {"head": "abc"}},
        }
        arguments.update(overrides)
        return self.enforce(**arguments)

    def legacy_turn(self, **overrides: Any) -> turn_postflight.PostTurnGuards:
        """A turn in the legacy single-repository implementation mode."""
        arguments: dict[str, Any] = {"legacy_snapshot_before": {".": {"head": "abc"}}}
        arguments.update(overrides)
        return self.enforce(**arguments)

    def legacy_review_turn(self, **overrides: Any) -> turn_postflight.PostTurnGuards:
        """A legacy review turn, whose only comparison is its fingerprint."""
        arguments: dict[str, Any] = {
            "review_fingerprint_before": {".": {"head": "abc"}},
            "is_review_session": True,
        }
        arguments.update(overrides)
        return self.enforce(**arguments)
