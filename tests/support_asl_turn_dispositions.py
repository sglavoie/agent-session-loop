"""Shared fixtures for test_asl_turn_dispositions."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    harnesses,
    mailbox_models,
    mailbox_vocabulary,
    plans_models,
    plans_payloads,
    plans_snapshot,
)

from .support import write_plan

RUN_ID = "run-dispositions-1"


SESSION_ID = "1"


SESSION_LABEL = "Session 1"


STATE_DIR_ARG = "/tmp/asl-state-does-not-exist"


#: The entry points the run loop is allowed to reach here: one per terminating
#: branch. The loop keeps the ``if terminating_disposition == ...`` dispatch
#: itself, so a ninth name would be a branch that was never dispatched.
ENTRY_POINTS = (
    "turn_dispositions.pause_interrupted_run",
    "turn_dispositions.pause_timed_out_turn",
    "turn_dispositions.record_aborted_turn",
    "turn_dispositions.settle_malformed_plan",
    "turn_dispositions.settle_restore_refused",
    "turn_dispositions.settle_runner_pause",
    "turn_dispositions.settle_nonzero_exit",
    "turn_dispositions.settle_redirected_turn",
)


#: Branch work that belonged to the loop and now belongs here. Each of these is
#: checked as the *callee* of a call, so the runner's surviving module-level
#: aliases -- which are assignments, not calls -- are deliberately unaffected.
MOVED_CALLS = frozenset(
    {
        "run_termination._honor_abort_turn",
        "handoff_correction._correct_malformed_handoff",
        "handoff_correction._reopen_uninspectable_handoff",
        "run_coordination_mailbox_wait.MailboxWaitCoordinator",
        "model_availability.suppress_until",
        "_worker_pause_resolution_for_turn",
        "_runner_pause_can_wait_for_mailbox_reply",
        "_runner_pause_reason",
        "_record_mailbox_reply_wait_preparation_failed",
        "classify_harness_failure",
    }
)


#: The mailbox wait seams the loop still resolves and passes in as callbacks.
#: Session 37 moved the loop out of the runner, so they are now read at the
#: module that owns them; in-tree callers patch them there to assert a wait was
#: or was not entered, which only works while the loop resolves them at the
#: call site rather than binding them once at import.
INJECTED_SEAMS = (
    "run_coordination_mailbox_wait.prepare_mailbox_reply_wait",
    "run_coordination_mailbox_wait.wait_for_mailbox_reply",
    "run_coordination_mailbox_wait.finish_mailbox_reply_wait_lifecycle",
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real -- the Spark fallback commits a route change, an artifact
    and an event under one hold, and a fake that did not lock would hide a
    missing hold rather than expose it -- while events and state writes are
    kept in order so a test can assert on what was recorded without reading
    files back.
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


class FakeRunContext:
    """A :class:`turn_dispositions.RunContext` built by hand, not by a run.

    Only the members these branches reach are populated, which is what the
    Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.event_path = event_path
        self.payload: dict[str, Any] = {
            "run_id": RUN_ID,
            "spark": {"baseline": "sonnet"},
        }
        self.persistence = RecordingPersistence()
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.control_payload: dict[str, Any] = {}
        self.runner_work_finished_at: Any = None
        self.idle_baseline_pinned = False
        self.final_snapshots: list[plans_models.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []
        # Present and None, exactly as `run_loop.RunContext` declares it: the
        # crash-recovery branches read it before anything sets it, and absent
        # here that read would raise instead of taking the untouched path.
        self.redirect_recovery_report: Any = None

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


def fake_message(**overrides: Any) -> mailbox_models.MailboxMessage:
    """One mailbox record, with only the fields these branches read set."""
    values: dict[str, Any] = {
        "seq": 7,
        "id": "msg-1",
        "kind": mailbox_vocabulary.KIND_REPLY,
        "created_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return mailbox_models.MailboxMessage(**values)


class DispositionTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments a branch needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(event_path=self.event_path)
        write_plan(self.plan_dir, ("pending",))
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        self.after = plans_snapshot.inspect_plan(self.plan_dir)
        self.session_details: dict[str, Any] = {"session_id": SESSION_ID}

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.artifact_dir,
            "result": self.artifact_dir / "result.json",
        }

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        values: dict[str, Any] = {
            "argv": ["claude"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
        }
        values.update(overrides)
        return harnesses.HarnessResult(**values)
