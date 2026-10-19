"""Shared fixtures for test_asl_run_coordination."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import (
    clock,
    control_queue,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
    qna,
    run_control,
    run_coordination_control,
    run_coordination_control_drain,
    run_coordination_mailbox_poll,
    run_coordination_mailbox_question,
    run_coordination_mailbox_wait,
    run_coordination_reply_state,
    run_coordination_seams,
    run_coordination_terminal,
    run_coordination_wait_models,
    run_coordination_wait_record,
    run_coordination_wait_state,
)
from agent_session_loop import run_coordination_constants as wait_budgets

COORDINATION_MODULES = (
    wait_budgets,
    run_coordination_control,
    run_coordination_control_drain,
    run_coordination_mailbox_poll,
    run_coordination_mailbox_question,
    run_coordination_mailbox_wait,
    run_coordination_reply_state,
    run_coordination_seams,
    run_coordination_terminal,
    run_coordination_wait_models,
    run_coordination_wait_record,
    run_coordination_wait_state,
)


RUN_ID = "run-coordination-1"


SESSION_ID = "3"


class RecordingPersistence:
    """A :class:`run_coordination_seams.CoordinationPersistence` that keeps its calls.

    Events are kept in order and state writes are kept as snapshots, so a test
    can assert on sequence without reading files back. The lock is real: the
    concurrency tests below take it from several threads at once.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))

    def actions(self, name: str) -> list[str]:
        """The ``action`` field of every event appended under ``name``."""
        return [
            payload.get("action") for event, payload in self.events if event == name
        ]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.events if event == name]


class RecordingNotifier:
    """A :class:`WaitNotifier` that records both lifecycle calls."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str | None]] = []
        self.ended: list[tuple[str, str, str | None]] = []

    def wait_started(self, *, question_id: str, question_text: str | None) -> None:
        self.started.append((question_id, question_text))

    def wait_ended(
        self, *, question_id: str, wait_reason: str, closure_id: str | None
    ) -> None:
        self.ended.append((question_id, wait_reason, closure_id))


class RecordingPhase:
    """A :class:`FinalizationPhase` that records open and close."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def finish(self, *, cancelled: bool = False) -> None:
        self.calls.append("finish-cancelled" if cancelled else "finish")


class FakeClock:
    """A monotonic and wall clock a test steps by hand.

    The bounded wait paces itself on monotonic time and persists wall-clock
    deadlines, and suspend detection is exactly the disagreement between the
    two, so both have to be drivable independently.
    """

    def __init__(self) -> None:
        self.monotonic_value = 1000.0
        self.wall_value = clock.utc_now()

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self):
        return self.wall_value

    def advance(self, seconds: float, *, wall_extra: float = 0.0) -> None:
        """Move both clocks, optionally letting wall time run ahead (a suspend)."""
        self.monotonic_value += seconds
        self.wall_value += timedelta(seconds=seconds + wall_extra)


def lease_view(
    *, fresh: bool, updated_at: str | None = None, reason: str | None = None
):
    """Build the lease view a probe would return, without writing a lease file."""
    lease = None
    if updated_at is not None:
        lease = mailbox_models.MailboxLease(
            supervisor_id="11111111-2222-3333-4444-555555555555",
            updated_at=updated_at,
        )
    return mailbox_models.MailboxLeaseView(
        lease_path=Path("lease.json"),
        lease=lease,
        fresh=fresh,
        reason=reason,
    )


def settings(**overrides: float) -> run_coordination_wait_models.MailboxWaitSettings:
    values: dict[str, float] = {
        "base_seconds": 300.0,
        "lease_extension_seconds": 60.0,
        "unsupervised_ceiling_seconds": 1800.0,
        "poll_seconds": 2.0,
        "io_grace_seconds": 10.0,
    }
    values.update(overrides)
    return run_coordination_wait_models.MailboxWaitSettings(**values)


def waiting_payload(question_id: str, *, turn_index: int = 1) -> dict[str, Any]:
    started_at = clock.utc_now()
    return run_coordination_wait_record.waiting_for_reply_payload(
        question_id=question_id,
        session_id=SESSION_ID,
        turn_index=turn_index,
        started_at=started_at,
        base_deadline=started_at + timedelta(seconds=300),
        unsupervised_deadline_at=started_at + timedelta(seconds=1800),
        unsupervised_ceiling_seconds=1800.0,
        last_lease_activity_at=None,
    )


def qna_context(
    plan_dir: Path, state_dir: Path, *, last_message: str
) -> qna.QnAContext:
    """A minimal paused-turn context; only its message fields are read here."""
    return qna.QnAContext(
        plan_dir=plan_dir,
        state_dir=state_dir,
        state_path=state_dir / "state.json",
        run_id=RUN_ID,
        run_dir=state_dir / "run",
        pause_reason="needs-input",
        pause_message="blocked",
        pause_details={},
        harness="codex",
        model=None,
        model_source=None,
        effort=None,
        effort_source=None,
        session_id=SESSION_ID,
        next_pending_id=SESSION_ID,
        turn_index=1,
        artifact_dir=None,
        stdout_path=None,
        stderr_path=None,
        last_message_path=state_dir / "last-message.txt",
        result_path=None,
        last_message=last_message,
    )


class CoordinationTestCase(unittest.TestCase):
    """Shared temporary plan, state directory, and recording persistence."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.plan_dir = root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = root / "state"
        self.state_dir_arg = str(self.state_dir)
        self.event_path = root / "events.jsonl"
        self.persistence = RecordingPersistence()
        self.payload: dict[str, Any] = {"run_id": RUN_ID}

    def store(self) -> run_coordination_wait_state.WaitStateStore:
        return run_coordination_wait_state.WaitStateStore(
            event_path=self.event_path,
            payload=self.payload,
            persistence=self.persistence,
        )

    def open_wait(
        self, text: str = "what next?", *, turn_index: int = 1
    ) -> tuple[mailbox_models.MailboxMessage, mailbox_models.MailboxMessage]:
        """Append a wait-delivered runner question and return it with its wait."""
        return mailbox_waits.append_wait_question(
            self.plan_dir,
            text,
            state_dir=self.state_dir_arg,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=turn_index,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
        )


class ControlDrainTestCase(CoordinationTestCase):
    """Shared control-plane fixture: a state object, its slot, and a drain seam."""

    def setUp(self) -> None:
        super().setUp()
        self.control_state = run_control.RunControlState(current_harness="codex")
        self.control_payload = run_coordination_control.initial_control_payload()
        self.seen_ids: set[str] = set()
        self.run_control_lock = threading.RLock()

    def drain(self, **overrides: Any) -> control_queue.DrainResult:
        kwargs: dict[str, Any] = {
            "plan_dir": self.plan_dir,
            "state_dir_arg": self.state_dir_arg,
            "control_state": self.control_state,
            "seen_ids": self.seen_ids,
            "payload": self.payload,
            "event_path": self.event_path,
            "control_payload": self.control_payload,
            "context": {"session": SESSION_ID, "turn_index": 1},
            "persistence": self.persistence,
            "run_control_lock": self.run_control_lock,
        }
        kwargs.update(overrides)
        return run_coordination_control_drain.drain_external_control_queue(**kwargs)

    def queue(self, command: str, **kwargs: Any) -> control_queue.ControlCommand:
        return control_queue.append_command(
            self.plan_dir, command, state_dir=self.state_dir_arg, **kwargs
        )
