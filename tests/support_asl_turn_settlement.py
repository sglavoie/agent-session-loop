"""Shared fixtures for test_asl_turn_settlement."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_settlement,
    continuation_guards,
    events,
    handoff_guards,
    plans_models,
    plans_payloads,
    plans_snapshot,
    plans_status_tables,
    readiness_gate,
    review_cycle,
    run_coordination_control_drain,
    run_inputs,
    session_verification,
    turn_execution_commit,
    turn_settlement,
)

from .support import write_plan

#: Captured at import, before any test patches the recorder over it.
_REAL_PLAN_ARTIFACT_GUARD = handoff_guards._enforce_plan_artifact_guard


RUN_ID = "run-settlement-1"


SESSION_ID = "1"


SESSION_LABEL = "Session 1"


#: The one entry point the run loop is allowed to reach here. The settlement is
#: a single sequence whose order is the guarantee, so a second name would be a
#: seam through which that order could be reassembled differently.
ENTRY_POINTS = ("turn_settlement.settle_turn",)


#: Settlement work that belonged to the loop and now belongs here. Each is
#: checked as the *callee* of a call, so the runner's surviving module-level
#: aliases -- assignments, not calls -- are deliberately unaffected.
MOVED_CALLS = frozenset(
    {
        "handoff_guards._enforce_plan_artifact_guard",
        "handoff_guards._append_progress_entry",
        "session_verification._enforce_session_verification",
        "session_verification._enforce_review_verdict",
        "plans_status_tables.sync_derived_status_ledgers",
        "readiness_gate._refresh_active_readiness_state",
        "commit_settlement._registered_session_commit_targets",
        "commit_settlement._dirty_commit_targets",
        "commit_settlement._commit_target_is_clean",
        "commit_settlement._run_commit_group",
        "turn_executor.commit",
        "review_cycle._advance_review_cycle",
        "review_cycle._set_review_resume",
        "review_cycle._terminal_review_reason",
    }
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real, because several of the writes this boundary makes are
    meant to land under one hold and a fake that did not lock would hide a
    missing hold rather than expose it. Events and state writes are kept in
    order so a test asserts on what was recorded without reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]


class FakeRunContext:
    """A :class:`turn_settlement.RunContext` built by hand, not by a run.

    Only the members the settlement reaches are populated, which is what the
    Protocol declares; reaching anything else is an ``AttributeError`` here
    rather than a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {
            "run_id": RUN_ID,
            "completed_sessions": [],
        }
        self.persistence = RecordingPersistence()
        self.progress = object()
        self.guard_controller = object()
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.session_attempts = continuation_guards.SessionAttempts()
        self.verification_context = argparse.Namespace(workspace=None)
        self.commit_generation = object()
        self.control_payload: dict[str, Any] = {}
        self.seen_external_command_ids: set[str] = set()
        self.completed_count = 0
        self.review_appended = True
        self.final_snapshots: list[plans_models.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None:
        self.final_snapshots.append(snapshot)
        with self.persistence.lock:
            self.payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class FakeTurnExecutor:
    """The publication seam, reduced to what the settlement asks of it."""

    def __init__(self, *, published: plans_models.PlanSnapshot, record: dict[str, Any]):
        self.published = published
        self.record = record
        self.calls: list[dict[str, Any]] = []
        self.rejection: turn_execution_commit.TurnCommitRejected | None = None

    def commit(self, **kwargs: Any) -> turn_execution_commit.TurnCommit:
        self.calls.append(kwargs)
        if self.rejection is not None:
            raise self.rejection
        return turn_execution_commit.TurnCommit(
            published=self.published, record=self.record
        )


class SettlementTestCase(unittest.TestCase):
    """A real plan on disk, a fake context, and every collaborator recorded.

    Each collaborator is patched at its owning module rather than through the
    runner's aliases, because that is where this boundary reads it; a patch on
    the alias would pass while the code under test called the real thing.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME

        write_plan(self.plan_dir, ("pending", "pending"))
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        write_plan(self.plan_dir, ("complete", "pending"))
        self.after = plans_snapshot.inspect_plan(self.plan_dir)

        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)
        self.turn_payload: dict[str, Any] = {"turn": 1}
        self.session_details: dict[str, Any] = {"session_id": SESSION_ID}
        self.paths = {
            "artifact_dir": self.artifact_dir,
            "result": self.artifact_dir / "result.json",
        }
        self.published = self.after
        self.turn_record: dict[str, Any] = {"turn": 1, "accepted": True}
        self.turn_executor = FakeTurnExecutor(
            published=self.published, record=self.turn_record
        )
        self.run_controls = argparse.Namespace(
            state=argparse.Namespace(stop_after_current=False)
        )
        self.soft_failures: list[dict[str, Any]] = []
        self.trace: list[str] = []
        self._install_collaborators()

    # -- collaborators ---------------------------------------------------

    def patch(self, owner: Any, name: str, value: Any) -> None:
        patcher = mock.patch.object(owner, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorder(self, label: str, result: Any) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            self.trace.append(label)
            return result

        return _call

    def _install_collaborators(self) -> None:
        self.patch(
            handoff_guards,
            "_enforce_plan_artifact_guard",
            self.recorder("artifact-guard", False),
        )
        self.patch(
            session_verification,
            "_enforce_session_verification",
            self.recorder("verification", False),
        )
        self.patch(
            run_coordination_control_drain,
            "drain_external_control_queue",
            self.recorder("drain", None),
        )
        self.patch(
            session_verification,
            "_enforce_review_verdict",
            self.recorder("verdict", ("pass", None)),
        )
        self.patch(
            handoff_guards, "_append_progress_entry", self.recorder("progress", True)
        )
        self.patch(
            plans_status_tables,
            "sync_derived_status_ledgers",
            self.recorder("ledgers", []),
        )
        self.patch(
            readiness_gate,
            "_refresh_active_readiness_state",
            self.recorder("readiness", None),
        )
        self.patch(
            commit_settlement, "_dirty_commit_targets", self.recorder("targets", [])
        )
        self.patch(
            commit_settlement,
            "_registered_session_commit_targets",
            self.recorder("registered-targets", []),
        )
        self.patch(
            commit_settlement,
            "_commit_target_is_clean",
            self.recorder("target-clean", False),
        )
        self.patch(
            commit_settlement,
            "_run_commit_group",
            self.recorder("commit-group", (None, None)),
        )
        self.patch(
            review_cycle,
            "_advance_review_cycle",
            self.recorder("advance", ("complete", self.after)),
        )
        self.patch(
            review_cycle, "_set_review_resume", self.recorder("review-resume", None)
        )
        self.patch(
            review_cycle,
            "_terminal_review_reason",
            self.recorder("terminal-reason", "review-complete"),
        )
        self.patch(
            run_inputs, "_max_repair_group_attempts", self.recorder("max-attempts", 2)
        )

        publish = self.turn_executor.commit

        def _commit(**kwargs: Any) -> turn_execution_commit.TurnCommit:
            self.trace.append("turn-commit")
            return publish(**kwargs)

        self.turn_executor.commit = _commit  # type: ignore[method-assign]

    def soft_fail_commit(self, **kwargs: Any) -> None:
        self.trace.append("soft-fail")
        self.soft_failures.append(kwargs)

    # -- the call under test ---------------------------------------------

    def settle(self, **overrides: Any) -> turn_settlement.TurnSettlement:
        kwargs: dict[str, Any] = {
            "args": argparse.Namespace(state_dir=None),
            "cwd": self.tmp_dir,
            "before": self.before,
            "after": self.after,
            "snapshot": self.before,
            "session_id": SESSION_ID,
            "session_label": SESSION_LABEL,
            "session_details": self.session_details,
            "turn_index": 1,
            "total_sessions": 2,
            "turn_payload": self.turn_payload,
            "paths": self.paths,
            "plan_artifacts_before": {},
            "plan_artifacts_written_by_runner": (),
            "is_review_session": False,
            "is_repair_session": False,
            "review_session_version": None,
            "allow_not_loop_ready": False,
            "commit_mode": None,
            "max_review_fix_cycles": 1,
            "review_requested": False,
            "turn_executor": self.turn_executor,
            "live_turn_payload": {},
            "run_controls": self.run_controls,
            "run_control_lock": threading.RLock(),
            "wakeup": None,
            "soft_fail_commit": self.soft_fail_commit,
        }
        kwargs.update(overrides)
        return turn_settlement.settle_turn(self.ctx, **kwargs)
