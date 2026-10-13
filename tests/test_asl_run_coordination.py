"""The typed coordination boundary, exercised without the run loop.

``run_coordination`` owns the three places a run is raced from outside: a
supervisor answering through the mailbox, an operator queueing control
commands from another process, and a terminal audit that can be cancelled
mid-flight. These tests drive it through collaborators defined here rather
than through ``runner``, which is the point of the boundary.

Every race the module claims to survive is covered as a *deterministic*
property rather than by hoping a sleep lands in the right window:

* a reply committed in the instant a wait times out is honoured, because
  ``mailbox_waits.finish_wait`` re-checks under the mailbox lock;
* a lease probe or deadline rebase whose wait has already been cleared is
  dropped instead of resurrecting the wait;
* a supervisor lost, recovered, and lost again across a suspend moves the
  cursor's ratchets in one direction only;
* a duplicate control command leaves exactly one receipt;
* an abort with no turn in flight degrades instead of vanishing;
* a cancelled terminal audit still closes its finalization phase.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    control_queue,
    events,
    harnesses,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
    qna,
    run_control,
    run_coordination,
    supervisor_audit,
)

from .support import assert_no_runner_import

RUN_ID = "run-coordination-1"
SESSION_ID = "3"


class RecordingPersistence:
    """A :class:`run_coordination.CoordinationPersistence` that keeps its calls.

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
    """A :class:`run_coordination.WaitNotifier` that records both lifecycle calls."""

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
    """A :class:`run_coordination.FinalizationPhase` that records open and close."""

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


def settings(**overrides: float) -> run_coordination.MailboxWaitSettings:
    values: dict[str, float] = {
        "base_seconds": 300.0,
        "lease_extension_seconds": 60.0,
        "unsupervised_ceiling_seconds": 1800.0,
        "poll_seconds": 2.0,
        "io_grace_seconds": 10.0,
    }
    values.update(overrides)
    return run_coordination.MailboxWaitSettings(**values)


def waiting_payload(question_id: str, *, turn_index: int = 1) -> dict[str, Any]:
    started_at = clock.utc_now()
    return run_coordination.waiting_for_reply_payload(
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

    def store(self) -> run_coordination.WaitStateStore:
        return run_coordination.WaitStateStore(
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


class CoordinationBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any wait it coordinates."""

    def test_run_coordination_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_coordination)


class WaitStateStoreTests(CoordinationTestCase):
    """The wait slot is compare-and-set, so a late writer cannot resurrect it."""

    def test_begin_persists_the_wait_and_announces_it_before_polling(self) -> None:
        store = self.store()
        waiting = waiting_payload("question-1")

        persisted = store.begin(waiting, pending_outbox=2)

        self.assertEqual(persisted["question_id"], "question-1")
        self.assertEqual(self.payload["waiting_for_reply"], waiting)
        (event,) = self.persistence.payloads(events.MAILBOX_EVENT)
        self.assertEqual(event["action"], events.MAILBOX_ACTION_WAIT_STARTED)
        self.assertEqual(event["pending_outbox"], 2)
        self.assertEqual(event["run_id"], RUN_ID)
        self.assertEqual(len(self.persistence.states), 1)

    def test_begin_copies_the_record_so_the_caller_cannot_mutate_the_slot(self) -> None:
        store = self.store()
        waiting = waiting_payload("question-1")

        store.begin(waiting, pending_outbox=0)
        waiting["question_id"] = "hijacked"

        self.assertEqual(self.payload["waiting_for_reply"]["question_id"], "question-1")

    def test_current_returns_a_copy_rather_than_the_live_slot(self) -> None:
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=0)

        snapshot = store.current()
        assert snapshot is not None
        snapshot["question_id"] = "hijacked"

        self.assertEqual(self.payload["waiting_for_reply"]["question_id"], "question-1")

    def test_lease_activity_for_another_question_is_dropped(self) -> None:
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=0)
        writes = len(self.persistence.states)

        store.record_lease_activity(question_id="question-2", updated_at="2026-01-01Z")

        self.assertIsNone(
            self.payload["waiting_for_reply"].get("last_lease_activity_at")
        )
        self.assertEqual(len(self.persistence.states), writes)

    def test_repeating_a_lease_timestamp_writes_no_state(self) -> None:
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=0)
        store.record_lease_activity(question_id="question-1", updated_at="2026-01-01Z")
        writes = len(self.persistence.states)

        store.record_lease_activity(question_id="question-1", updated_at="2026-01-01Z")

        self.assertEqual(len(self.persistence.states), writes)

    def test_a_rebase_after_the_wait_cleared_does_not_resurrect_it(self) -> None:
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=0)
        store.clear(
            run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_TIMEOUT, pending_outbox=0
            )
        )
        moment = clock.utc_now()

        store.rebase_deadlines(
            question_id="question-1",
            base_deadline=moment,
            unsupervised_deadline_at=moment,
        )

        self.assertNotIn("waiting_for_reply", self.payload)

    def test_a_lease_probe_after_the_wait_cleared_does_not_resurrect_it(self) -> None:
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=0)
        store.clear(None)

        store.record_lease_activity(question_id="question-1", updated_at="2026-01-01Z")

        self.assertNotIn("waiting_for_reply", self.payload)

    def test_clearing_without_a_result_still_removes_the_slot(self) -> None:
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=0)

        reason = store.clear(None)

        self.assertEqual(reason, run_coordination.WAIT_REASON_ERROR)
        self.assertNotIn("waiting_for_reply", self.payload)
        cleared = self.persistence.payloads(events.MAILBOX_EVENT)[-1]
        self.assertEqual(cleared["action"], events.MAILBOX_ACTION_WAIT_CLEARED)
        self.assertEqual(cleared["reason"], run_coordination.WAIT_REASON_ERROR)
        self.assertNotIn("pending_outbox", cleared)

    def test_clearing_on_a_resolution_publishes_its_closure_metadata(self) -> None:
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=1)
        resolution = mailbox_models.MailboxMessage(
            id="22222222-3333-4444-5555-666666666666",
            seq=7,
            kind=mailbox_vocabulary.KIND_RESOLUTION,
            created_at=clock.isoformat(clock.utc_now()),
            action=mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
        )

        reason = store.clear(
            run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution=resolution,
            )
        )

        self.assertEqual(reason, run_coordination.WAIT_REASON_RESOLUTION)
        cleared = self.persistence.payloads(events.MAILBOX_EVENT)[-1]
        self.assertEqual(
            cleared["resolution_action"], mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
        )
        self.assertEqual(cleared["resolution_message_id"], resolution.id)
        self.assertEqual(cleared["resolution_seq"], 7)

    def test_partial_resolution_metadata_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            run_coordination.clear_waiting_for_reply(
                self.event_path,
                self.payload,
                reason=run_coordination.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution_action=mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                persistence=self.persistence,
            )

    def test_preparation_failure_records_an_event_and_no_state(self) -> None:
        store = self.store()

        store.record_preparation_failed(
            session_id=SESSION_ID, turn_index=1, reason=mailbox_constants.LEASE_MISSING
        )

        (event,) = self.persistence.payloads(events.MAILBOX_EVENT)
        self.assertEqual(event["action"], events.MAILBOX_ACTION_WAIT_PREPARATION_FAILED)
        self.assertEqual(event["reason"], mailbox_constants.LEASE_MISSING)
        self.assertEqual(self.persistence.states, [])
        self.assertNotIn("waiting_for_reply", self.payload)

    def test_concurrent_lease_probes_never_outlive_the_clear(self) -> None:
        """Deterministic because every mutation is a CAS under the run lock.

        Eight probe threads and one clearing thread are released together. The
        interleaving is genuinely arbitrary, but the outcome is not: once the
        clear commits, no probe can put the slot back.
        """
        store = self.store()
        store.begin(waiting_payload("question-1"), pending_outbox=0)
        release = threading.Barrier(9)
        failures: list[BaseException] = []

        def probe(index: int) -> None:
            try:
                release.wait(timeout=5.0)
                store.record_lease_activity(
                    question_id="question-1", updated_at=f"2026-01-0{index + 1}Z"
                )
            except BaseException as error:  # noqa: BLE001 - reported, not swallowed
                failures.append(error)

        def finish() -> None:
            try:
                release.wait(timeout=5.0)
                store.clear(
                    run_coordination.MailboxWaitResult(
                        reason=run_coordination.WAIT_REASON_TIMEOUT, pending_outbox=0
                    )
                )
            except BaseException as error:  # noqa: BLE001 - reported, not swallowed
                failures.append(error)

        threads = [threading.Thread(target=probe, args=(index,)) for index in range(8)]
        threads.append(threading.Thread(target=finish))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(failures, [])
        self.assertNotIn("waiting_for_reply", self.payload)
        cleared = [
            payload
            for payload in self.persistence.payloads(events.MAILBOX_EVENT)
            if payload["action"] == events.MAILBOX_ACTION_WAIT_CLEARED
        ]
        self.assertEqual(len(cleared), 1)


class ReplyRaceTests(CoordinationTestCase):
    """A closure committed as the wait gives up wins, and is consumed once."""

    def timed_out(self, **overrides: Any) -> run_coordination.MailboxWaitResult:
        return run_coordination.MailboxWaitResult.unclosed(
            run_coordination.WAIT_REASON_TIMEOUT,
            pending_outbox=overrides.pop("pending_outbox", 1),
            evidence=overrides.pop(
                "evidence",
                run_coordination.LeaseEvidence.observed(
                    lease_view(fresh=False, updated_at="2026-01-01T00:00:00+00:00"),
                    unsupervised_seconds=42.0,
                ),
            ),
        )

    def finish(
        self,
        question: mailbox_models.MailboxMessage,
        wait_start: mailbox_models.MailboxMessage,
        result: run_coordination.MailboxWaitResult,
    ) -> run_coordination.MailboxWaitResult:
        return run_coordination.finish_mailbox_reply_wait_lifecycle(
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir_arg,
            question_id=question.id,
            wait_id=wait_start.id,
            result=result,
        )

    def test_a_reply_landing_at_the_timeout_overrides_the_timeout(self) -> None:
        question, wait_start = self.open_wait()
        # The poll loop already decided it had timed out; the reply commits
        # before ``finish_wait`` takes the mailbox lock.
        reply = mailbox_appenders.append_reply(
            self.plan_dir, question.id, "here you go", state_dir=self.state_dir_arg
        )

        result = self.finish(question, wait_start, self.timed_out())

        self.assertEqual(result.reason, run_coordination.WAIT_REASON_REPLY)
        self.assertEqual(result.reply, reply)
        self.assertIsNone(result.resolution)
        self.assertEqual(result.closure_id, reply.id)

    def test_the_overridden_timeout_keeps_the_evidence_the_loop_gathered(self) -> None:
        question, wait_start = self.open_wait()
        mailbox_appenders.append_reply(
            self.plan_dir, question.id, "here you go", state_dir=self.state_dir_arg
        )
        timed_out = self.timed_out()

        result = self.finish(question, wait_start, timed_out)

        self.assertEqual(result.lease_updated_at, timed_out.lease_updated_at)
        self.assertEqual(result.unsupervised_seconds, 42.0)
        self.assertEqual(result.pending_outbox, timed_out.pending_outbox)

    def test_a_resolution_landing_at_the_timeout_overrides_the_timeout(self) -> None:
        question, wait_start = self.open_wait()
        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
            expected_run_id=RUN_ID,
            state_dir=self.state_dir_arg,
        )

        result = self.finish(question, wait_start, self.timed_out())

        self.assertEqual(result.reason, run_coordination.WAIT_REASON_RESOLUTION)
        self.assertEqual(result.resolution, resolution)
        self.assertIsNone(result.reply)

    def test_a_closure_replacing_a_reply_result_clears_the_other_field(self) -> None:
        question, wait_start = self.open_wait()
        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            expected_run_id=RUN_ID,
            state_dir=self.state_dir_arg,
        )
        stale_reply = mailbox_models.MailboxMessage(
            id="33333333-4444-5555-6666-777777777777",
            seq=1,
            kind=mailbox_vocabulary.KIND_REPLY,
            created_at=clock.isoformat(clock.utc_now()),
        )
        arrived = run_coordination.MailboxWaitResult(
            reason=run_coordination.WAIT_REASON_REPLY,
            pending_outbox=0,
            reply=stale_reply,
        )

        result = self.finish(question, wait_start, arrived)

        self.assertIsNone(result.reply)
        self.assertEqual(result.resolution, resolution)

    def test_a_wait_that_really_timed_out_keeps_its_timeout(self) -> None:
        question, wait_start = self.open_wait()

        result = self.finish(question, wait_start, self.timed_out())

        self.assertEqual(result.reason, run_coordination.WAIT_REASON_TIMEOUT)
        self.assertIsNone(result.closure)

    def test_the_same_wait_cannot_be_finished_twice(self) -> None:
        question, wait_start = self.open_wait()
        mailbox_appenders.append_reply(
            self.plan_dir, question.id, "here you go", state_dir=self.state_dir_arg
        )
        self.finish(question, wait_start, self.timed_out())

        with self.assertRaises(mailbox_models.MailboxError):
            self.finish(question, wait_start, self.timed_out())

    def test_a_reply_after_a_clean_timeout_is_rejected(self) -> None:
        question, wait_start = self.open_wait()
        self.finish(question, wait_start, self.timed_out())

        with self.assertRaises(mailbox_models.MailboxError) as caught:
            mailbox_appenders.append_reply(
                self.plan_dir, question.id, "too late", state_dir=self.state_dir_arg
            )

        self.assertEqual(
            caught.exception.reason, mailbox_constants.REJECT_QUESTION_NOT_WAITING
        )


class MailboxWaitCoordinatorTests(CoordinationTestCase):
    """Announce, poll, commit, clear -- in that order, whatever the poll does."""

    def prepared(
        self,
        question: mailbox_models.MailboxMessage,
        wait_start: mailbox_models.MailboxMessage,
    ) -> run_coordination.PreparedMailboxWait:
        return run_coordination.PreparedMailboxWait(
            context=qna_context(
                self.plan_dir, self.state_dir, last_message=question.text or ""
            ),
            question=question,
            wait_start=wait_start,
            waiting_for_reply=waiting_payload(question.id),
            pending_outbox=1,
            settings=settings(),
            started_monotonic=100.0,
        )

    def coordinator(
        self,
        prepared: run_coordination.PreparedMailboxWait,
        *,
        wait_for_reply,
        notifier: RecordingNotifier | None = None,
    ) -> run_coordination.MailboxWaitCoordinator:
        return run_coordination.MailboxWaitCoordinator(
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir_arg,
            prepared=prepared,
            run_id=RUN_ID,
            store=self.store(),
            persistence=self.persistence,
            notifier=notifier,
            wait_for_reply=wait_for_reply,
        )

    def test_a_reply_racing_the_timeout_is_honoured_end_to_end(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        notifier = RecordingNotifier()

        def poll(**_kwargs: Any) -> run_coordination.MailboxWaitResult:
            # The supervisor answers in the window between the loop's last
            # unlocked read and the locked commit below.
            mailbox_appenders.append_reply(
                self.plan_dir, question.id, "answered", state_dir=self.state_dir_arg
            )
            return run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_TIMEOUT, pending_outbox=1
            )

        result = self.coordinator(
            prepared, wait_for_reply=poll, notifier=notifier
        ).run()

        self.assertEqual(result.reason, run_coordination.WAIT_REASON_REPLY)
        self.assertNotIn("waiting_for_reply", self.payload)
        self.assertEqual(notifier.started, [(question.id, question.text)])
        self.assertEqual(
            notifier.ended,
            [(question.id, run_coordination.WAIT_REASON_REPLY, result.closure_id)],
        )
        self.assertEqual(
            [
                payload["action"]
                for payload in self.persistence.payloads(events.MAILBOX_EVENT)
            ],
            [
                events.MAILBOX_ACTION_WAIT_STARTED,
                events.MAILBOX_ACTION_WAIT_CLEARED,
            ],
        )

    def test_the_wait_is_announced_before_the_poll_can_block(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        notifier = RecordingNotifier()
        observed: list[tuple[bool, int]] = []

        def poll(**_kwargs: Any) -> run_coordination.MailboxWaitResult:
            observed.append(
                ("waiting_for_reply" in self.payload, len(notifier.started))
            )
            return run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_TIMEOUT, pending_outbox=0
            )

        self.coordinator(prepared, wait_for_reply=poll, notifier=notifier).run()

        self.assertEqual(observed, [(True, 1)])

    def test_a_poll_that_raises_still_clears_and_announces_the_wait(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        notifier = RecordingNotifier()

        def poll(**_kwargs: Any) -> run_coordination.MailboxWaitResult:
            raise RuntimeError("poll exploded")

        coordinator = self.coordinator(prepared, wait_for_reply=poll, notifier=notifier)
        with self.assertRaises(RuntimeError):
            coordinator.run()

        self.assertNotIn("waiting_for_reply", self.payload)
        self.assertEqual(
            notifier.ended, [(question.id, run_coordination.WAIT_REASON_ERROR, None)]
        )
        cleared = self.persistence.payloads(events.MAILBOX_EVENT)[-1]
        self.assertEqual(cleared["action"], events.MAILBOX_ACTION_WAIT_CLEARED)
        self.assertEqual(cleared["reason"], run_coordination.WAIT_REASON_ERROR)

    def test_the_injected_poll_receives_the_prepared_wait_verbatim(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)
        seen: dict[str, Any] = {}

        def poll(**kwargs: Any) -> run_coordination.MailboxWaitResult:
            seen.update(kwargs)
            return run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_TIMEOUT, pending_outbox=0
            )

        self.coordinator(prepared, wait_for_reply=poll).run()

        self.assertEqual(seen["question_id"], question.id)
        self.assertEqual(seen["run_id"], RUN_ID)
        self.assertEqual(seen["settings"], prepared.settings)
        self.assertEqual(seen["started_monotonic"], 100.0)
        self.assertIs(seen["payload"], self.payload)

    def test_a_wait_without_a_notifier_still_runs(self) -> None:
        question, wait_start = self.open_wait()
        prepared = self.prepared(question, wait_start)

        result = self.coordinator(
            prepared,
            wait_for_reply=lambda **_kwargs: run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_TIMEOUT, pending_outbox=0
            ),
        ).run()

        self.assertEqual(result.reason, run_coordination.WAIT_REASON_TIMEOUT)
        self.assertNotIn("waiting_for_reply", self.payload)


class ReplyStatePreserverTests(CoordinationTestCase):
    """An unconsumed reply attempt is rolled back; a consumed one is not."""

    def setUp(self) -> None:
        super().setUp()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.state_path.write_text('{"before": true}\n', encoding="utf-8")
        self.event_path.write_text('{"event": "before"}\n', encoding="utf-8")

    def preserver(
        self, *, enabled: bool = True
    ) -> run_coordination.ReplyStatePreserver:
        return run_coordination.ReplyStatePreserver(
            state_path=self.state_path, event_path=self.event_path, enabled=enabled
        )

    def test_an_unconsumed_attempt_is_rolled_back(self) -> None:
        preserver = self.preserver()
        self.state_path.write_text('{"during": true}\n', encoding="utf-8")

        preserver.restore_if_unconsumed()

        self.assertFalse(preserver.consumed)
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), '{"before": true}\n'
        )

    def test_a_consumed_attempt_is_left_alone(self) -> None:
        preserver = self.preserver()
        self.state_path.write_text('{"during": true}\n', encoding="utf-8")
        preserver.mark_consumed()

        preserver.restore_if_unconsumed()

        self.assertTrue(preserver.consumed)
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), '{"during": true}\n'
        )

    def test_a_file_that_did_not_exist_is_removed_again(self) -> None:
        missing = Path(self.tmp.name) / "absent.json"
        preserver = run_coordination.ReplyStatePreserver(
            state_path=missing, event_path=self.event_path, enabled=True
        )
        missing.write_text("created\n", encoding="utf-8")

        preserver.restore_if_unconsumed()

        self.assertFalse(missing.exists())

    def test_a_disabled_preserver_captures_and_restores_nothing(self) -> None:
        preserver = self.preserver(enabled=False)
        self.state_path.write_text('{"during": true}\n', encoding="utf-8")

        preserver.restore_if_unconsumed()

        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), '{"during": true}\n'
        )


class SupervisionCursorTests(unittest.TestCase):
    """Loss, recovery, and suspend move the cursor's ratchets one way only."""

    def cursor(self, clocks: FakeClock, **overrides: float):
        return run_coordination.SupervisionCursor(
            settings=settings(**overrides),
            started=clocks.monotonic(),
            monotonic=clocks.monotonic,
            wall_clock=clocks.wall,
        )

    def test_a_new_cursor_measures_from_its_own_start(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)

        self.assertEqual(cursor.active_deadline, clocks.monotonic_value + 300.0)
        self.assertEqual(
            cursor.unsupervised_deadline(), clocks.monotonic_value + 1800.0
        )
        self.assertEqual(cursor.unsupervised_elapsed(), 0.0)

    def test_a_fresh_lease_extends_the_deadline_and_rolls_the_ceiling(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(300.0)

        verdict = cursor.observe_lease(
            lease_view(fresh=True, updated_at="2026-01-01T00:00:00+00:00"),
            clocks.monotonic(),
        )

        self.assertEqual(verdict, "fresh")
        self.assertEqual(cursor.active_deadline, clocks.monotonic_value + 60.0)
        self.assertEqual(
            cursor.unsupervised_deadline(), clocks.monotonic_value + 1800.0
        )
        self.assertEqual(cursor.unsupervised_elapsed(), 0.0)

    def test_a_stale_lease_outside_any_grace_ends_the_wait(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(300.0)

        verdict = cursor.observe_lease(
            lease_view(fresh=False, reason=mailbox_constants.LEASE_EXPIRED),
            clocks.monotonic(),
        )

        self.assertEqual(verdict, "lost")
        self.assertEqual(cursor.active_deadline, clocks.monotonic_value)

    def test_recovery_after_a_stale_probe_restores_the_ratchet(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(300.0)
        cursor.observe_lease(lease_view(fresh=False), clocks.monotonic())
        clocks.advance(30.0)

        verdict = cursor.observe_lease(
            lease_view(fresh=True, updated_at="2026-01-01T00:05:30+00:00"),
            clocks.monotonic(),
        )

        self.assertEqual(verdict, "fresh")
        self.assertEqual(cursor.last_fresh_lease, clocks.monotonic_value)
        self.assertEqual(
            cursor.evidence().lease_updated_at, "2026-01-01T00:05:30+00:00"
        )

    def test_an_undisturbed_clock_detects_no_suspend(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(120.0)

        self.assertIsNone(cursor.observe_clock())
        self.assertEqual(cursor.suspend_skew_seconds, 0.0)
        self.assertIsNone(cursor.lease_grace_until)

    def test_wall_clock_running_ahead_is_read_as_a_suspend(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)
        ceiling_before = cursor.unsupervised_deadline()
        clocks.advance(2.0, wall_extra=600.0)

        rebased = cursor.observe_clock()

        assert rebased is not None
        base_deadline, unsupervised_deadline_at = rebased
        self.assertAlmostEqual(cursor.suspend_skew_seconds, 600.0, places=6)
        self.assertEqual(
            cursor.suspend_lease_grace_seconds,
            run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS,
        )
        self.assertEqual(cursor.last_fresh_lease, clocks.monotonic_value)
        # The ceiling restarts at the wake instant: nobody could prove liveness
        # while the machine slept.
        self.assertGreater(cursor.unsupervised_deadline(), ceiling_before)
        self.assertEqual(
            base_deadline,
            clocks.wall_value
            + timedelta(seconds=cursor.active_deadline - clocks.monotonic_value),
        )
        self.assertEqual(
            unsupervised_deadline_at,
            clocks.wall_value + timedelta(seconds=1800.0),
        )

    def test_a_stale_lease_inside_the_post_suspend_grace_proves_nothing(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(2.0, wall_extra=600.0)
        cursor.observe_clock()
        active_before = cursor.active_deadline
        ceiling_before = cursor.unsupervised_deadline()
        clocks.advance(10.0)

        verdict = cursor.observe_lease(lease_view(fresh=False), clocks.monotonic())

        self.assertEqual(verdict, "grace")
        self.assertEqual(cursor.active_deadline, active_before)
        self.assertEqual(cursor.unsupervised_deadline(), ceiling_before)

    def test_a_stale_lease_after_the_grace_expires_ends_the_wait(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)
        clocks.advance(2.0, wall_extra=600.0)
        cursor.observe_clock()
        clocks.advance(
            run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS + 1.0
        )

        verdict = cursor.observe_lease(lease_view(fresh=False), clocks.monotonic())

        self.assertEqual(verdict, "lost")

    def test_evidence_distinguishes_no_probe_from_an_empty_lease(self) -> None:
        clocks = FakeClock()
        cursor = self.cursor(clocks)

        unprobed = cursor.evidence()
        cursor.observe_lease(lease_view(fresh=False), clocks.monotonic())
        probed = cursor.evidence()

        self.assertFalse(unprobed.lease_observed)
        self.assertEqual(unprobed.to_payload(), {"unsupervised_seconds": 0.0})
        self.assertTrue(probed.lease_observed)
        self.assertIn("lease_supervisor_id", probed.to_payload())
        self.assertIsNone(probed.to_payload()["lease_supervisor_id"])

    def test_a_zero_suspend_is_absence_rather_than_a_measurement(self) -> None:
        evidence = run_coordination.LeaseEvidence.observed(
            lease_view(fresh=True, updated_at="2026-01-01T00:00:00+00:00")
        )

        self.assertIsNone(evidence.suspend_skew_seconds)
        self.assertIsNone(evidence.suspend_lease_grace_seconds)
        self.assertNotIn("suspend_skew_seconds", evidence.to_payload())


class MailboxWaitSettingsTests(unittest.TestCase):
    """Environment overrides are a narrow seam that can only fail safe."""

    def test_the_defaults_are_the_production_budgets(self) -> None:
        resolved = run_coordination.mailbox_reply_wait_settings({})

        self.assertEqual(
            resolved.base_seconds, run_coordination.MAILBOX_REPLY_WAIT_BASE_SECONDS
        )
        self.assertEqual(
            resolved.unsupervised_ceiling_seconds,
            run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS,
        )

    def test_a_nonsense_override_falls_back_rather_than_raising(self) -> None:
        resolved = run_coordination.mailbox_reply_wait_settings(
            {
                run_coordination.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "not-a-number",
                run_coordination.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "-1",
                run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "inf",
            }
        )

        self.assertEqual(
            resolved.base_seconds, run_coordination.MAILBOX_REPLY_WAIT_BASE_SECONDS
        )
        self.assertEqual(
            resolved.poll_seconds, run_coordination.MAILBOX_REPLY_WAIT_POLL_SECONDS
        )
        self.assertEqual(
            resolved.io_grace_seconds,
            run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS,
        )

    def test_a_base_beyond_the_ceiling_is_clamped_not_raised(self) -> None:
        resolved = run_coordination.mailbox_reply_wait_settings(
            {
                run_coordination.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "10000",
                run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "60",
            }
        )

        self.assertEqual(resolved.base_seconds, 60.0)
        self.assertEqual(resolved.unsupervised_ceiling_seconds, 60.0)


class ControlDrainTestCase(CoordinationTestCase):
    """Shared control-plane fixture: a state object, its slot, and a drain seam."""

    def setUp(self) -> None:
        super().setUp()
        self.control_state = run_control.RunControlState(current_harness="codex")
        self.control_payload = run_coordination.initial_control_payload()
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
        return run_coordination.drain_external_control_queue(**kwargs)

    def queue(self, command: str, **kwargs: Any) -> control_queue.ControlCommand:
        return control_queue.append_command(
            self.plan_dir, command, state_dir=self.state_dir_arg, **kwargs
        )


class ControlDrainTests(ControlDrainTestCase):
    """Every accepted command leaves exactly one receipt, before its transition."""

    def test_the_receipt_precedes_the_transition_it_causes(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        self.drain()

        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
        )
        self.assertTrue(self.control_payload["stop_after_current"])
        self.assertTrue(self.payload["control"]["stop_after_current"])

    def test_the_receipt_carries_the_command_identity_asl_control_tails(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        self.drain()

        receipt = self.persistence.payloads(events.RUN_CONTROL_EVENT)[0]
        self.assertEqual(receipt["command_id"], "cmd-1")
        self.assertEqual(
            receipt["queue_command"], control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT
        )
        self.assertEqual(receipt["session"], SESSION_ID)
        self.assertEqual(receipt["turn_index"], 1)

    def test_a_duplicate_id_in_one_queue_file_is_accepted_once(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        result = self.drain()

        self.assertEqual([command.id for command in result.accepted], ["cmd-1"])
        self.assertEqual(
            [rejected.reason for rejected in result.rejected],
            [control_queue.REJECT_DUPLICATE_ID],
        )
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT).count(
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
            ),
            1,
        )

    def test_a_replayed_id_after_the_drain_leaves_no_second_receipt(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        self.drain()
        before = len(self.persistence.events)
        # An external producer rewrites the queue with the same command.
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        result = self.drain()

        self.assertEqual(result.accepted, ())
        self.assertEqual(len(self.persistence.events), before)

    def test_an_already_applied_request_still_leaves_a_receipt(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        self.drain()
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-2")

        self.drain()

        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
            ],
        )

    def test_a_harness_selection_names_both_harnesses(self) -> None:
        self.queue(
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            command_id="cmd-1",
            harness="claude",
        )

        self.drain(context={"harness": "codex"})

        selected = self.persistence.payloads(events.RUN_CONTROL_EVENT)[-1]
        self.assertEqual(selected["action"], events.RUN_CONTROL_ACTION_HARNESS_SELECTED)
        self.assertEqual(selected["current_harness"], "codex")
        self.assertEqual(selected["selected_harness"], "claude")

    def test_an_unreadable_queue_degrades_to_an_empty_drain(self) -> None:
        with mock.patch.object(
            run_coordination.control_queue, "drain_queue", side_effect=OSError("nope")
        ):
            result = self.drain()

        self.assertEqual(result.accepted, ())
        self.assertEqual(self.persistence.events, [])

    def test_a_wakeup_is_cleared_before_the_read(self) -> None:
        wakeup = threading.Event()
        wakeup.set()
        observed: list[bool] = []
        real_drain = control_queue.drain_queue

        def watching_drain(*args: Any, **kwargs: Any) -> control_queue.DrainResult:
            observed.append(wakeup.is_set())
            return real_drain(*args, **kwargs)

        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        with mock.patch.object(
            run_coordination.control_queue, "drain_queue", watching_drain
        ):
            self.drain(wakeup=wakeup)

        self.assertEqual(observed, [False])

    def test_two_concurrent_drains_accept_the_command_once(self) -> None:
        """Deterministic because the queue file is read-and-truncated under flock.

        Whichever drain wins the lock takes the command with it; the other
        finds an empty queue. Either interleaving leaves one receipt.
        """
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        release = threading.Barrier(2)
        failures: list[BaseException] = []

        def drain_once() -> None:
            try:
                release.wait(timeout=5.0)
                self.drain()
            except BaseException as error:  # noqa: BLE001 - reported, not swallowed
                failures.append(error)

        threads = [threading.Thread(target=drain_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(failures, [])
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
        )


class AbortCancellationTests(ControlDrainTestCase):
    """An abort either arms a kill or degrades; it never disappears."""

    def test_an_abort_with_a_turn_in_flight_arms_the_kill_and_warns_the_worker(
        self,
    ) -> None:
        abort = harnesses.AbortSignal()
        self.queue(
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            command_id="cmd-1",
            grace_seconds=30,
            reason="rethinking the plan",
        )

        self.drain(abort=abort, mailbox_enabled=True)

        self.assertTrue(abort.armed)
        self.assertEqual(abort.reason, "rethinking the plan")
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
            ],
        )
        self.assertIsNotNone(self.control_payload["abort_deadline"])
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir_arg)
        guidance = [
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_GUIDANCE
        ]
        self.assertEqual(len(guidance), 1)
        self.assertIn("rethinking the plan", guidance[0].text or "")

    def test_an_abort_with_no_turn_in_flight_degrades_to_stop_after_current(
        self,
    ) -> None:
        self.queue(
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            command_id="cmd-1",
            reason="never mind",
        )

        self.drain(abort=None)

        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
        )
        self.assertTrue(self.control_state.stop_after_current)
        self.assertFalse(self.control_payload["abort_turn_requested"])
        self.assertIsNone(self.control_payload["abort_deadline"])
        degraded = self.persistence.payloads(events.RUN_CONTROL_EVENT)[2]
        self.assertIn("no turn in flight", degraded["message"])

    def test_a_finalization_abort_arms_immediately_and_warns_nobody(self) -> None:
        abort = harnesses.AbortSignal()
        self.queue(
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            command_id="cmd-1",
            grace_seconds=120,
            reason="stop auditing",
        )

        self.drain(abort=abort, mailbox_enabled=True, finalization=True)

        self.assertTrue(abort.armed)
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED,
            ],
        )
        honored = self.persistence.payloads(events.RUN_CONTROL_EVENT)[-1]
        self.assertIn("cancelling the terminal audit", honored["message"])
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir_arg)
        self.assertEqual(
            [
                message
                for message in view.messages
                if message.kind == mailbox_vocabulary.KIND_GUIDANCE
            ],
            [],
        )

    def test_an_abort_carrying_no_reason_still_reads_as_an_explanation(self) -> None:
        abort = harnesses.AbortSignal()
        action = run_control.RunControlState().request_abort_turn()

        run_coordination.arm_requested_abort(
            action,
            abort=abort,
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir_arg,
            control_payload=self.control_payload,
            mailbox_enabled=False,
        )

        self.assertEqual(abort.reason, run_coordination.ABORT_TURN_DEFAULT_REASON)

    def test_a_failed_guidance_append_does_not_lose_the_armed_abort(self) -> None:
        abort = harnesses.AbortSignal()
        action = run_control.RunControlState().request_abort_turn(reason="stop")

        with mock.patch.object(
            run_coordination.mailbox_appenders,
            "append_guidance",
            side_effect=OSError("mailbox gone"),
        ):
            run_coordination.arm_requested_abort(
                action,
                abort=abort,
                plan_dir=self.plan_dir,
                state_dir_arg=self.state_dir_arg,
                control_payload=self.control_payload,
                mailbox_enabled=True,
            )

        self.assertTrue(abort.armed)
        self.assertEqual(abort.reason, "stop")


class HarnessNameGuardTests(unittest.TestCase):
    """The restated literals must keep agreeing with the runtime membership."""

    def test_the_restated_literals_cover_every_known_harness(self) -> None:
        self.assertEqual(
            {name for name in harnesses.HARNESS_NAMES},
            {"codex", "claude", "copilot"},
        )

    def test_every_known_harness_narrows_to_itself(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                self.assertEqual(run_coordination.harness_name(name), name)

    def test_an_unknown_name_degrades_to_no_harness(self) -> None:
        self.assertIsNone(run_coordination.harness_name("gpt-9"))
        self.assertIsNone(run_coordination.harness_name(None))
        self.assertIsNone(run_coordination.harness_name(""))


class ControlPayloadTests(unittest.TestCase):
    """The ``control`` slot and its event can never disagree."""

    def test_a_consumed_abort_clears_the_pending_fields(self) -> None:
        control_payload = run_coordination.initial_control_payload()
        state = run_control.RunControlState()
        requested = state.request_abort_turn(grace_seconds=30, reason="stop")
        run_coordination.apply_run_control_action(control_payload, requested)
        control_payload["abort_deadline"] = "2026-01-01T00:00:00+00:00"

        consumed = state.consume_abort_turn()
        run_coordination.apply_run_control_action(control_payload, consumed)

        self.assertFalse(control_payload["abort_turn_requested"])
        self.assertIsNone(control_payload["abort_grace_seconds"])
        self.assertIsNone(control_payload["abort_reason"])
        self.assertIsNone(control_payload["abort_deadline"])
        self.assertEqual(
            control_payload["last_action"],
            events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
        )

    def test_a_pending_abort_keeps_the_grace_it_was_requested_with(self) -> None:
        control_payload = run_coordination.initial_control_payload()
        state = run_control.RunControlState()

        requested = state.request_abort_turn(grace_seconds=30, reason="stop")
        run_coordination.apply_run_control_action(control_payload, requested)

        self.assertTrue(control_payload["abort_turn_requested"])
        self.assertEqual(control_payload["abort_grace_seconds"], 30)
        self.assertEqual(control_payload["abort_reason"], "stop")

    def test_every_action_name_has_an_event_action_and_a_message(self) -> None:
        self.assertEqual(
            set(run_coordination.RUN_CONTROL_EVENT_ACTIONS),
            set(run_coordination.RUN_CONTROL_EVENT_MESSAGES),
        )


class TerminalAssuranceTests(CoordinationTestCase):
    """The finalization phase closes exactly once, whatever the audit does."""

    def setUp(self) -> None:
        super().setUp()
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        self.payload.update(
            {
                "ok": True,
                "run_dir": str(self.run_dir),
                "assurance": {
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "state": "pending",
                    "eligible": True,
                },
            }
        )
        self.audit_plan = supervisor_audit.AuditPlan(
            config=supervisor_audit.AuditConfig(),
            role=None,
            state="pending",
            reason=None,
        )
        self.context = run_coordination.TerminalAuditContext(
            plan=self.audit_plan,
            run_dir=self.run_dir,
            state_dir=self.state_dir,
            plan_dir=self.plan_dir,
            workspace_root=Path(self.tmp.name),
            ledger_state_dir=self.state_dir,
        )
        self.phase = RecordingPhase()

    def coordinator(self, **overrides: Any):
        kwargs: dict[str, Any] = {
            "payload": self.payload,
            "context": self.context,
            "persistence": self.persistence,
            "phase": self.phase,
            "state_snapshot": dict,
        }
        kwargs.update(overrides)
        return run_coordination.TerminalAssuranceCoordinator(**kwargs)

    def summary(self, state: str) -> supervisor_audit.AssuranceSummary:
        return supervisor_audit.AssuranceSummary(
            run_id=RUN_ID, state=state, eligible=True, reason=None, role=None
        )

    def test_a_completed_audit_publishes_its_verdict_and_closes_the_phase(self) -> None:
        with mock.patch.object(
            run_coordination.supervisor_audit,
            "audit_terminal_run",
            return_value=self.summary("all-clear"),
        ):
            outcome = self.coordinator().run()

        self.assertFalse(outcome.cancelled)
        self.assertTrue(outcome.audited)
        self.assertTrue(outcome.reported)
        self.assertEqual(outcome.state, "all-clear")
        self.assertFalse(outcome.degraded)
        self.assertEqual(self.phase.calls, ["start", "finish"])
        self.assertEqual(self.payload["assurance"]["state"], "all-clear")
        self.assertIn("assurance_report", self.payload)

    def test_a_cancelled_audit_closes_the_phase_as_cancelled(self) -> None:
        with mock.patch.object(
            run_coordination.supervisor_audit,
            "audit_terminal_run",
            side_effect=supervisor_audit.AuditCancelled("aborted"),
        ):
            outcome = self.coordinator().run()

        self.assertTrue(outcome.cancelled)
        self.assertEqual(self.phase.calls, ["start", "finish-cancelled"])
        self.assertEqual(self.payload["assurance"]["state"], "unavailable")
        self.assertTrue(outcome.degraded)

    def test_a_keyboard_interrupt_is_a_cancelled_audit_rather_than_a_crash(
        self,
    ) -> None:
        with mock.patch.object(
            run_coordination.supervisor_audit,
            "audit_terminal_run",
            side_effect=KeyboardInterrupt,
        ):
            outcome = self.coordinator().run()

        self.assertTrue(outcome.cancelled)
        self.assertEqual(self.phase.calls, ["start", "finish-cancelled"])
        self.assertEqual(
            self.payload["assurance"]["reason"], supervisor_audit.REASON_INTERRUPTED
        )

    def test_the_cancelled_run_keeps_the_outcome_it_had_already_earned(self) -> None:
        with mock.patch.object(
            run_coordination.supervisor_audit,
            "audit_terminal_run",
            side_effect=supervisor_audit.AuditCancelled("aborted"),
        ):
            self.coordinator().run()

        self.assertTrue(self.payload["ok"])

    def test_an_unexpected_failure_still_closes_the_phase(self) -> None:
        with (
            mock.patch.object(
                run_coordination.supervisor_audit,
                "audit_terminal_run",
                side_effect=RuntimeError("auditor exploded"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.coordinator().run()

        self.assertEqual(self.phase.calls, ["start", "finish"])

    def test_an_armed_abort_reaches_the_audit(self) -> None:
        abort = harnesses.AbortSignal()
        abort.arm("stop auditing")
        with mock.patch.object(
            run_coordination.supervisor_audit,
            "audit_terminal_run",
            return_value=self.summary("all-clear"),
        ) as audit:
            self.coordinator(abort=abort).run()

        self.assertIs(audit.call_args.kwargs["abort"], abort)

    def test_the_audit_reads_a_snapshot_taken_under_the_run_lock(self) -> None:
        taken: list[dict[str, Any]] = []

        def snapshot(payload: dict[str, Any]) -> dict[str, Any]:
            self.assertTrue(self.persistence.lock.acquire(blocking=False))
            self.persistence.lock.release()
            copied = dict(payload)
            taken.append(copied)
            return copied

        with mock.patch.object(
            run_coordination.supervisor_audit,
            "audit_terminal_run",
            return_value=self.summary("all-clear"),
        ) as audit:
            self.coordinator(state_snapshot=snapshot).run()

        self.assertEqual(len(taken), 1)
        self.assertIs(audit.call_args.kwargs["state_payload"], taken[0])

    def test_an_ineligible_run_is_not_audited_but_the_phase_still_closes(self) -> None:
        self.payload["assurance"] = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "state": "pending",
            "eligible": False,
        }

        with mock.patch.object(
            run_coordination.supervisor_audit, "audit_terminal_run"
        ) as audit:
            outcome = self.coordinator().run()

        audit.assert_not_called()
        self.assertFalse(outcome.audited)
        self.assertEqual(self.phase.calls, ["start", "finish"])

    def test_an_already_graded_run_is_not_audited_again(self) -> None:
        self.payload["assurance"] = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "state": "all-clear",
            "eligible": True,
        }

        with mock.patch.object(
            run_coordination.supervisor_audit, "audit_terminal_run"
        ) as audit:
            outcome = self.coordinator().run()

        audit.assert_not_called()
        self.assertFalse(outcome.audited)
        self.assertEqual(outcome.state, "all-clear")


class DegradedAssuranceTests(CoordinationTestCase):
    """A run whose audit could not speak says so, without failing the run."""

    def test_an_unavailable_verdict_is_recorded_as_a_degraded_success(self) -> None:
        self.payload.update(
            {
                "ok": True,
                "assurance": {
                    "state": "unavailable",
                    "reason": supervisor_audit.REASON_INTERRUPTED,
                    "artifacts": [{"locator": "run/audit/report.json"}],
                    "issue_ids": ["issue-1"],
                },
            }
        )

        run_coordination.record_degraded_assurance(
            self.payload, persistence=self.persistence
        )

        degraded = self.payload["assurance_degraded"]
        self.assertEqual(degraded["assurance_state"], "unavailable")
        self.assertFalse(degraded["blocking"])
        self.assertTrue(degraded["run_ok"])
        self.assertEqual(degraded["evidence"], ["run/audit/report.json"])
        self.assertEqual(degraded["issue_ids"], ["issue-1"])

    def test_a_settled_decision_not_to_audit_is_not_a_degradation(self) -> None:
        self.payload["assurance"] = {"state": "skipped"}
        self.payload["assurance_degraded"] = {"stale": True}

        run_coordination.record_degraded_assurance(
            self.payload, persistence=self.persistence
        )

        self.assertNotIn("assurance_degraded", self.payload)
        self.assertEqual(self.persistence.states, [])


class QuestionTextTests(CoordinationTestCase):
    """A question body is bounded and byte-stable across a retried preparation."""

    def test_a_short_message_is_the_question_verbatim(self) -> None:
        context = qna_context(self.plan_dir, self.state_dir, last_message="short")

        self.assertEqual(run_coordination.mailbox_question_text(context), "short")

    def test_an_oversized_message_is_truncated_deterministically(self) -> None:
        body = "x" * (run_coordination.MAILBOX_QUESTION_TEXT_MAX_CHARS + 500)
        context = qna_context(self.plan_dir, self.state_dir, last_message=body)

        first = run_coordination.mailbox_question_text(context)
        second = run_coordination.mailbox_question_text(context)

        self.assertEqual(first, second)
        self.assertTrue(
            first.startswith("x" * run_coordination.MAILBOX_QUESTION_TEXT_MAX_CHARS)
        )
        self.assertIn("question truncated", first)
        self.assertIn(str(context.last_message_path), first)

    def test_the_question_id_is_stable_across_retries_of_one_turn(self) -> None:
        first = run_coordination.mailbox_question_id(
            plan_dir=self.plan_dir, run_id=RUN_ID, session_id=SESSION_ID, turn_index=1
        )
        second = run_coordination.mailbox_question_id(
            plan_dir=self.plan_dir, run_id=RUN_ID, session_id=SESSION_ID, turn_index=1
        )
        other_turn = run_coordination.mailbox_question_id(
            plan_dir=self.plan_dir, run_id=RUN_ID, session_id=SESSION_ID, turn_index=2
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, other_turn)


if __name__ == "__main__":
    unittest.main()
