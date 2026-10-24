"""The typed coordination boundary, exercised without the run loop.

The ``run_coordination_*`` modules own the three places a run is raced from outside: a
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

import threading
import unittest

from agent_session_loop import (
    clock,
    events,
    mailbox_constants,
    mailbox_models,
    mailbox_vocabulary,
    run_coordination_wait_models,
    run_coordination_wait_state,
)
from agent_session_loop import run_coordination_constants as wait_budgets
from tests import support_asl_run_coordination as fixtures

from .support import assert_no_runner_import


class CoordinationBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any wait it coordinates."""

    def test_run_coordination_does_not_import_the_runner(self) -> None:
        for module in fixtures.COORDINATION_MODULES:
            with self.subTest(module=module.__name__):
                assert_no_runner_import(self, module)


class WaitStateStoreTests(fixtures.CoordinationTestCase):
    """The wait slot is compare-and-set, so a late writer cannot resurrect it."""

    def test_begin_persists_the_wait_and_announces_it_before_polling(self) -> None:
        store = self.store()
        waiting = fixtures.waiting_payload("question-1")

        persisted = store.begin(waiting, pending_outbox=2)

        self.assertEqual(persisted["question_id"], "question-1")
        self.assertEqual(self.payload["waiting_for_reply"], waiting)
        (event,) = self.persistence.payloads(events.MAILBOX_EVENT)
        self.assertEqual(event["action"], events.MAILBOX_ACTION_WAIT_STARTED)
        self.assertEqual(event["pending_outbox"], 2)
        self.assertEqual(event["run_id"], fixtures.RUN_ID)
        self.assertEqual(len(self.persistence.states), 1)

    def test_begin_copies_the_record_so_the_caller_cannot_mutate_the_slot(self) -> None:
        store = self.store()
        waiting = fixtures.waiting_payload("question-1")

        store.begin(waiting, pending_outbox=0)
        waiting["question_id"] = "hijacked"

        self.assertEqual(self.payload["waiting_for_reply"]["question_id"], "question-1")

    def test_current_returns_a_copy_rather_than_the_live_slot(self) -> None:
        store = self.store()
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=0)

        snapshot = store.current()
        assert snapshot is not None
        snapshot["question_id"] = "hijacked"

        self.assertEqual(self.payload["waiting_for_reply"]["question_id"], "question-1")

    def test_lease_activity_for_another_question_is_dropped(self) -> None:
        store = self.store()
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=0)
        writes = len(self.persistence.states)

        store.record_lease_activity(question_id="question-2", updated_at="2026-01-01Z")

        self.assertIsNone(
            self.payload["waiting_for_reply"].get("last_lease_activity_at")
        )
        self.assertEqual(len(self.persistence.states), writes)

    def test_repeating_a_lease_timestamp_writes_no_state(self) -> None:
        store = self.store()
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=0)
        store.record_lease_activity(question_id="question-1", updated_at="2026-01-01Z")
        writes = len(self.persistence.states)

        store.record_lease_activity(question_id="question-1", updated_at="2026-01-01Z")

        self.assertEqual(len(self.persistence.states), writes)

    def test_a_rebase_after_the_wait_cleared_does_not_resurrect_it(self) -> None:
        store = self.store()
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=0)
        store.clear(
            run_coordination_wait_models.MailboxWaitResult(
                reason=wait_budgets.WAIT_REASON_TIMEOUT, pending_outbox=0
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
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=0)
        store.clear(None)

        store.record_lease_activity(question_id="question-1", updated_at="2026-01-01Z")

        self.assertNotIn("waiting_for_reply", self.payload)

    def test_clearing_without_a_result_still_removes_the_slot(self) -> None:
        store = self.store()
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=0)

        reason = store.clear(None)

        self.assertEqual(reason, wait_budgets.WAIT_REASON_ERROR)
        self.assertNotIn("waiting_for_reply", self.payload)
        cleared = self.persistence.payloads(events.MAILBOX_EVENT)[-1]
        self.assertEqual(cleared["action"], events.MAILBOX_ACTION_WAIT_CLEARED)
        self.assertEqual(cleared["reason"], wait_budgets.WAIT_REASON_ERROR)
        self.assertNotIn("pending_outbox", cleared)

    def test_clearing_on_a_resolution_publishes_its_closure_metadata(self) -> None:
        store = self.store()
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=1)
        resolution = mailbox_models.MailboxMessage(
            id="22222222-3333-4444-5555-666666666666",
            seq=7,
            kind=mailbox_vocabulary.KIND_RESOLUTION,
            created_at=clock.isoformat(clock.utc_now()),
            action=mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
        )

        reason = store.clear(
            run_coordination_wait_models.MailboxWaitResult(
                reason=wait_budgets.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution=resolution,
            )
        )

        self.assertEqual(reason, wait_budgets.WAIT_REASON_RESOLUTION)
        cleared = self.persistence.payloads(events.MAILBOX_EVENT)[-1]
        self.assertEqual(
            cleared["resolution_action"], mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
        )
        self.assertEqual(cleared["resolution_message_id"], resolution.id)
        self.assertEqual(cleared["resolution_seq"], 7)

    def test_partial_resolution_metadata_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            run_coordination_wait_state.clear_waiting_for_reply(
                self.event_path,
                self.payload,
                reason=wait_budgets.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution_action=mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                persistence=self.persistence,
            )

    def test_preparation_failure_records_an_event_and_no_state(self) -> None:
        store = self.store()

        store.record_preparation_failed(
            session_id=fixtures.SESSION_ID,
            turn_index=1,
            reason=mailbox_constants.LEASE_MISSING,
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
        store.begin(fixtures.waiting_payload("question-1"), pending_outbox=0)
        release = threading.Barrier(9)
        failures: list[BaseException] = []

        def probe(index: int) -> None:
            try:
                release.wait(timeout=5.0)
                store.record_lease_activity(
                    question_id="question-1", updated_at=f"2026-01-0{index + 1}Z"
                )
            except BaseException as error:
                failures.append(error)

        def finish() -> None:
            try:
                release.wait(timeout=5.0)
                store.clear(
                    run_coordination_wait_models.MailboxWaitResult(
                        reason=wait_budgets.WAIT_REASON_TIMEOUT,
                        pending_outbox=0,
                    )
                )
            except BaseException as error:
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


if __name__ == "__main__":
    unittest.main()
