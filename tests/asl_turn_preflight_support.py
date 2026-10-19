"""Asl turn preflight support."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    external_write_evidence,
    plans_models,
    plans_payloads,
    plans_session_scope,
    plans_snapshot,
    turn_preflight,
    turn_preparation,
)

from .support import write_plan

RUN_ID = "run-preflight-1"


SESSION_ID = "1"


#: Capture entry points owned here, including startup restoration evidence.
ENTRY_POINTS = (
    "turn_preflight.retained_scope_restored",
    "turn_preflight.resolve_retained_scope_repair",
    "turn_preflight.build_active_turn",
    "turn_preflight.capture_turn_baselines",
)


#: Capture and comparison calls that belonged to the loop and now belong here.
#: ``_latest_workspace_scope_repair_requirement`` is deliberately absent: the
#: readiness gate reads it once before any turn exists, and that reader is not
#: this boundary's to take.
MOVED_CALLS = frozenset(
    {
        "handoff_guards._capture_workspace_write_guard_snapshot",
        "handoff_guards._capture_legacy_implementation_snapshot",
        "review_guard._capture_review_repo_fingerprint",
        "review_guard._review_guard_unavailable_detail",
        "workspace_evidence._workspace_snapshot_unavailable",
        "workspace_evidence._workspace_unavailable_message",
        "workspace_evidence._workspace_write_guard_evidence",
        "workspace_evidence._workspace_scope_repair_comparison",
        "workspace_evidence._workspace_scope_repair_is_fresh",
        "workspace_evidence._session_expansion_evidence",
        "external_write_evidence._capture_external_write_digests",
        "external_write_evidence._control_plane_digest_roots",
        "repository_evidence.compare_workspace_snapshots",
    }
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real -- the boundary has one durable write and a fake that did
    not hold a lock would hide a missing hold rather than expose it -- while
    events are kept in order so a test can assert on what was recorded without
    reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))


class FakeVerificationContext:
    """The verification context, reduced to the members preflight reads."""

    def __init__(self, *, workspace: Any = None) -> None:
        self.workspace = workspace
        self.root = Path("/workspace")
        self.registry_digest = "digest"


class FakeRunContext:
    """A :class:`turn_preflight.RunContext` built by hand rather than by a run.

    Only the members the three preflight steps reach are populated, which is
    what the Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.run_dir = run_dir
        self.event_path = event_path
        self.persistence = RecordingPersistence()
        self.verification_context = FakeVerificationContext()
        self.runner_work_finished_at: Any = None
        self.final_snapshots: list[plans_models.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None:
        self.final_snapshots.append(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


def fake_workspace(*aliases: str) -> Any:
    """A resolved workspace reduced to the repositories preflight enumerates."""
    return SimpleNamespace(
        repositories=tuple(
            SimpleNamespace(alias=alias, repository=SimpleNamespace(path=alias))
            for alias in aliases
        )
    )


class PreflightTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments a step needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = self.tmp_dir / "repo"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)

    def snapshot(
        self, statuses: tuple[str, ...] = ("pending",)
    ) -> plans_models.PlanSnapshot:
        write_plan(self.plan_dir, statuses)
        return plans_snapshot.inspect_plan(self.plan_dir)

    def classification(
        self,
        *,
        review: bool = False,
        repair: bool = False,
    ) -> turn_preparation.SessionClassification:
        return turn_preparation.SessionClassification(
            is_review_session=review,
            is_repair_session=repair,
            session_kind=(
                plans_payloads.SESSION_KIND_REVIEW
                if review
                else plans_payloads.SESSION_KIND_IMPLEMENTATION
            ),
        )

    def turn_prompt(self, **overrides: Any) -> turn_preparation.TurnPrompt:
        values: dict[str, Any] = {
            "prompt": "the words the harness receives",
            "prompt_kind": "session",
            "persisted_prompt": "the words the state file keeps",
            "reply_source": None,
            "resolution_source": None,
            "paused": False,
        }
        values.update(overrides)
        return turn_preparation.TurnPrompt(**values)

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.run_dir / "turn-1",
            "stdout": self.run_dir / "turn-1" / "stdout.log",
            "stderr": self.run_dir / "turn-1" / "stderr.log",
            "last_message": self.run_dir / "turn-1" / "last-message.txt",
            "result": self.run_dir / "turn-1" / "result.json",
        }

    def baselines(
        self,
        snapshot: plans_models.PlanSnapshot,
        *,
        classification: turn_preparation.SessionClassification,
        workspace: Any = None,
        session_scope: plans_session_scope.SessionScope | None = None,
        review_base: Any = None,
        retained_scope_repair: dict[str, Any] | None = None,
        regenerated_write_scope: dict[str, Any] | None = None,
        authorized_external_roots: tuple[Path, ...] = (),
    ) -> turn_preflight.TurnBaselines:
        return turn_preflight.capture_turn_baselines(
            self.ctx,
            snapshot=snapshot,
            state_dir_arg=None,
            cwd=self.cwd,
            classification=classification,
            workspace=workspace,
            session_scope=session_scope,
            review_base=review_base,
            retained_scope_repair=retained_scope_repair,
            regenerated_write_scope=regenerated_write_scope,
            session_id=SESSION_ID,
            session_label="Session 1",
            session_details={"session_id": SESSION_ID},
            review_guard_excludes=(".venv",),
            review_guard_include_ignored=False,
            active_turn={"turn_index": 1},
            live_turn=mock.Mock(),
            paths=self.paths(),
            authorized_external_roots=authorized_external_roots,
        )

    def patch_digests(self, digests: dict[str, dict[str, Any]] | None = None) -> Any:
        """Stand in for the control-plane digest sweep, which walks real trees."""
        captured = {"plan": {"digest": "before"}} if digests is None else digests
        patcher = mock.patch.object(
            external_write_evidence,
            "_capture_external_write_digests",
            return_value=captured,
        )
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked
