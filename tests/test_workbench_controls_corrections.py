"""CorrectionTests coverage."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    intervention,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_vocab,
    workbench_controls,
)
from asl_workbench import controls
from tests.workbench_controls_support import (
    DEAD_PID,
    EDITED_FEEDBACK,
    FEEDBACK,
    OPERATION_ID,
    RECOVERY_PAUSE_REASON,
    RUN_ID,
    SESSION_ID,
    UNRESOLVABLE_PLAN_DIR,
    ControlsTestCase,
)


class CorrectionTests(ControlsTestCase):
    """What one correction establishes, and what it refuses to guess."""

    def test_a_correction_reaches_the_journal_and_the_receipt_is_read_back(
        self,
    ) -> None:
        self.live_run()
        self.accept_when_pending()

        submission = self.submit(wait_seconds_raw=5.0)

        self.assertEqual(OPERATION_ID, submission.operation_id)
        self.assertTrue(submission.persisted)
        self.assertFalse(submission.replayed)
        self.assertIsNotNone(submission.operation)
        payload = submission.operation_payload()
        assert payload is not None
        self.assertEqual(OPERATION_ID, payload["operation_id"])
        recorded = tuple(item.operation_id for item in self.journal().requests)
        self.assertEqual((OPERATION_ID,), recorded)

    def test_a_receipt_carries_a_digest_and_never_the_words(self) -> None:
        self.live_run()
        self.accept_when_pending()

        submission = self.submit(wait_seconds_raw=5.0)

        payload = submission.operation_payload()
        assert payload is not None
        self.assertEqual(len(FEEDBACK.encode("utf-8")), payload["feedback_bytes"])
        self.assertNotIn(FEEDBACK, repr(payload))

    def test_a_retry_replays_the_durable_record_and_appends_nothing(self) -> None:
        self.live_run()
        self.accept_when_pending()
        first = self.submit(wait_seconds_raw=5.0)

        second = self.submit()

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        # A replay submitted nothing, so it has no outcome of its own; the
        # durable operation carries the truth instead.
        self.assertIsNone(second.outcome)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(1, len(self.journal().requests))

    def test_a_replay_is_judged_before_the_freshness_proof_is_checked(self) -> None:
        """The correction that landed is exactly what made the proof stale, so
        asking the journal first is the only thing that makes a retry possible.
        """
        self.live_run()
        self.accept_when_pending()
        stale = self.action_token()
        self.submit(wait_seconds_raw=5.0, action_token=stale)
        self.write_state(paused=True, pause_reason="verify-failed")

        replay = self.submit(
            authorization=controls.AUTHORIZATION_STRICT, action_token=stale
        )

        self.assertTrue(replay.replayed)
        self.assertEqual(1, len(self.journal().requests))

    def test_an_edited_retry_is_a_conflict_rather_than_a_second_correction(
        self,
    ) -> None:
        """An operator whose response was lost still has an editable field.
        Reporting their edited words as a successful replay of the words the
        worker actually got is the one falsehood this path exists to avoid.
        """
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds_raw=5.0)

        with self.assertRaises(workbench_controls.RedirectConflict) as caught:
            self.submit(text=EDITED_FEEDBACK)

        self.assertEqual(
            redirect_store_vocab.CONFLICT_FEEDBACK, caught.exception.conflict
        )
        self.assertEqual(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT, caught.exception.reason
        )
        self.assertEqual(controls.SCOPE_CONFLICT, caught.exception.scope)
        # Nothing was written and the recorded request is untouched, which is
        # why the refusal still carries the receipt for the one on file.
        self.assertEqual(1, len(self.journal().requests))
        self.assertTrue(caught.exception.submission.replayed)

    def test_a_retry_naming_a_different_turn_is_the_same_conflict(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds_raw=5.0)

        with self.assertRaises(workbench_controls.RedirectConflict) as caught:
            self.submit(
                expected_target_raw={
                    "run_id": RUN_ID,
                    "session_id": SESSION_ID,
                    "turn_index": 7,
                    "harness": "claude",
                }
            )

        self.assertEqual(
            redirect_store_vocab.CONFLICT_TARGET, caught.exception.conflict
        )

    def test_a_turn_that_moved_is_a_refusal_the_receipt_reports(self) -> None:
        """The race a composer actually loses: the turn ended while they typed.

        Admission answers rather than raising, because the answer is about the
        run rather than about the request -- and both surfaces render the same
        receipt out of it, which is how a client learns the turn it lost.
        """
        self.live_run()

        submission = self.submit(
            expected_target_raw={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 4,
                "harness": "claude",
            }
        )

        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, submission.reason)
        self.assertFalse(submission.persisted)
        self.assertEqual((), self.journal().requests)

    def test_a_field_the_journal_cannot_store_is_the_clients_error(self) -> None:
        """One exception covers "this is not a correction" and "this journal
        cannot be written", so the refusal is split on the journal's own reason:
        a cap a client chose past is request-shaped and not worth retrying, and
        each surface turns that into a 400 rather than a server fault.
        """
        self.live_run()
        oversized = "o" * (redirect_store_vocab.REDIRECT_MAX_OPERATION_ID_CHARS + 1)

        with self.assertRaises(workbench_controls.RedirectSubmitFailed) as caught:
            self.submit(operation_id=oversized)

        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_OPERATION_ID, caught.exception.reason
        )
        self.assertEqual(controls.SOURCE_JOURNAL, caught.exception.source)
        self.assertTrue(caught.exception.request_shaped)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.submitted_message, caught.exception.message)
        self.assertEqual((), self.journal().requests)

    def test_an_urgent_channel_nobody_is_listening_on_persists_nothing(self) -> None:
        """A lock naming no endpoint: the correction has nowhere to be accepted,
        so it is refused rather than left in the journal for a reader to find.
        """
        self.write_state()
        self.write_lock()

        submission = self.submit()

        self.assertEqual(
            redirect_admission.REFUSE_URGENT_CHANNEL_UNAVAILABLE, submission.reason
        )
        self.assertFalse(submission.persisted)
        self.assertFalse(submission.accepted)

    def test_a_refusal_that_persisted_nothing_names_no_operation(self) -> None:
        """Echoing the client's own id for a correction that never reached the
        journal would invite it to ask after one that does not exist.
        """
        self.live_run()

        submission = self.submit(
            expected_target_raw={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 4,
                "harness": "claude",
            }
        )

        self.assertIsNone(submission.operation_id)
        self.assertIsNone(submission.operation_payload())
        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), view.operations)

    def test_whitespace_is_refused_by_the_shared_service_too(self) -> None:
        self.live_run()

        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            self.submit(text="  \n ")

        self.assertEqual(controls.REASON_EMPTY_TEXT, caught.exception.reason)
        self.assertEqual((), self.journal().requests)

    def test_a_correction_nothing_answered_is_reported_unfinished(self) -> None:
        """Persisted, unanswered, and the run that would have taken it is gone:
        one of the seven boundaries, and none of them is resolved by guessing.
        """
        self.live_run()
        self.submit()
        self.write_lock(pid=DEAD_PID, redirect_endpoint=None)

        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)

        payload = view.operations[0]
        self.assertEqual(redirect_recovery.STATUS_UNANSWERED, payload.status)
        self.assertEqual(
            redirect_cli.STAGE_UNACCEPTED,
            redirect_cli.operation_payload(payload, live_target=view.live_target)[
                "stage"
            ],
        )

    def test_a_correction_delivered_without_a_receipt_is_uncertain(self) -> None:
        """Handed to an attempt that never accounted for it. Whether a model
        read the words is the question the acknowledgment protocol exists
        because nothing cheaper answers, so this declines to answer it.
        """
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds_raw=5.0)
        redirect_store_carry.carry_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.deliver_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=DEAD_PID)

        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)

        payload = view.operation_payloads()[0]
        self.assertEqual(redirect_recovery.STATUS_UNCERTAIN, payload["status"])
        self.assertEqual(redirect_cli.STAGE_UNCERTAIN, payload["stage"])

    def test_an_unfinished_correction_is_what_a_fresh_start_would_inherit(self) -> None:
        """An operator authorizing a relaunch sees what it carries here, rather
        than discovering it in the successor's first prompt.
        """
        self.live_run()
        self.submit()
        self.write_state(paused=True, pause_reason=RECOVERY_PAUSE_REASON)
        self.write_lock(pid=DEAD_PID, redirect_endpoint=None)

        view = workbench_controls.recovery_view(self.plan_dir, state_dir=self.state_dir)

        self.assertTrue(view.decision.required)
        self.assertEqual(
            (OPERATION_ID,), tuple(item.operation_id for item in view.retained)
        )
        self.assertTrue(view.one_use)
        self.assertEqual(redirect_cli.FRESH_CONVERSATION_NOTE, view.note)

    def test_a_recovery_read_is_only_a_read(self) -> None:
        """Performing it here would persist a one-use authorization in the one
        place it must never live: a request that can be replayed.
        """
        self.live_run(paused=True, pause_reason=RECOVERY_PAUSE_REASON)
        before = self.journal().requests

        workbench_controls.recovery_view(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(before, self.journal().requests)
        self.assertEqual((), self.queued().consumed_ids)

    def test_a_status_read_hands_out_the_proof_a_correction_must_carry_back(
        self,
    ) -> None:
        """The client most in need of this read is one whose proof is stale, so
        the read is unauthorized and mints the fresh one.
        """
        self.live_run()

        view = workbench_controls.status_view(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(self.action_token(), intervention.action_token(view.facts))

    def test_a_status_read_filtered_to_an_absent_id_reports_nothing(self) -> None:
        self.live_run()
        self.submit()

        view = workbench_controls.status_view(
            self.plan_dir, state_dir=self.state_dir, operation_ids=("wbc-op-absent",)
        )

        self.assertEqual((), view.operations)
        self.assertFalse(view.truncated)

    def test_a_plan_that_cannot_be_read_refuses_rather_than_reporting_nothing(
        self,
    ) -> None:
        """Answering as though a plan held no corrections is the one wrong
        answer a client cannot detect.
        """
        with self.assertRaises(workbench_controls.ControlRefused) as caught:
            workbench_controls.status_view(
                UNRESOLVABLE_PLAN_DIR, state_dir=self.state_dir
            )

        self.assertEqual(intervention.REASON_INVALID_PLAN, caught.exception.reason)


if __name__ == "__main__":
    unittest.main()
