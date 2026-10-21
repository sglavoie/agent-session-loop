"""Admission cases: submission outcomes, undeliverable channels, runner side."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_redirect_admission_support import *


class SubmissionOutcomeTests(AdmissionTestCase):
    """What a producer learns once the correction is durable."""

    def test_an_unanswered_submission_is_uncertain_and_stays_open(self) -> None:
        """Settling here would race an acceptance already in flight."""
        self.make_live_run()

        outcome = self.submit()

        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, outcome.outcome)
        self.assertTrue(outcome.persisted)
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request(outcome.operation_id).state,
        )

    def test_the_wait_is_bounded_by_the_monotonic_clock(self) -> None:
        self.make_live_run()

        outcome = self.submit(wait_seconds=2.0)

        self.assertGreaterEqual(outcome.waited_seconds, 2.0)
        self.assertLess(outcome.waited_seconds, 2.5)
        self.assertEqual(
            redirect_admission.ACCEPTANCE_POLL_INTERVAL_SECONDS, max(self.clock.slept)
        )

    def test_a_zero_wait_reports_uncertain_immediately(self) -> None:
        self.make_live_run()

        outcome = self.submit(wait_seconds=0.0)

        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, outcome.outcome)
        self.assertEqual([], self.clock.slept)

    def test_a_runner_that_accepts_during_the_wait_is_reported_as_accepted(
        self,
    ) -> None:
        self.make_live_run()
        target = redirect_admission.inspect_target(
            self.plan_dir, state_dir=self.state_dir
        ).target
        accepted: list[str] = []

        def accept_on_second_poll(seconds: float) -> None:
            self.clock.slept.append(seconds)
            self.clock.now += seconds
            if len(self.clock.slept) == 2 and not accepted:
                pending = redirect_admission.pending_redirects(
                    self.plan_dir, target=target, state_dir=self.state_dir
                )
                accepted.append(pending[0].operation_id)
                redirect_admission.accept_redirect(
                    self.plan_dir,
                    pending[0].operation_id,
                    target=target,
                    state_dir=self.state_dir,
                )

        outcome = self.submit(sleep=accept_on_second_poll)

        self.assertEqual(redirect_admission.OUTCOME_ACCEPTED, outcome.outcome)
        self.assertTrue(outcome.accepted)
        self.assertEqual(accepted[0], outcome.operation_id)
        self.assertLess(outcome.waited_seconds, 2.0)

    def test_a_runner_that_settles_during_the_wait_is_reported_as_rejected(
        self,
    ) -> None:
        self.make_live_run()

        def settle(seconds: float) -> None:
            self.clock.slept.append(seconds)
            self.clock.now += seconds
            for request in self.journal().pending():
                redirect_store_requests.reject_request(
                    self.plan_dir,
                    request.operation_id,
                    reason=redirect_store_vocab.REASON_RUN_ENDED,
                    state_dir=self.state_dir,
                )

        outcome = self.submit(sleep=settle)

        self.assertEqual(redirect_admission.OUTCOME_REJECTED, outcome.outcome)
        self.assertEqual(redirect_store_vocab.REASON_RUN_ENDED, outcome.reason)

    def test_the_urgent_channel_receives_the_wakeup(self) -> None:
        self.make_live_run()

        outcome = self.submit(wait_seconds=0.0)

        self.assertTrue(outcome.delivery.delivered)
        self.assertEqual(control_signal.REASON_DELIVERED, outcome.delivery.reason)
        self.assertTrue(self.wakeup.wait(timeout=5.0))

    def test_a_wakeup_that_lands_is_still_not_an_acceptance(self) -> None:
        """A delivered byte proves a reader existed, not that it took the request."""
        self.make_live_run()

        outcome = self.submit()

        self.assertTrue(outcome.delivery.delivered)
        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, outcome.outcome)

    def test_an_uncertain_outcome_still_names_the_operation(self) -> None:
        """Without an id the operator has no way to find out what happened."""
        self.make_live_run()

        outcome = self.submit()

        self.assertIsNotNone(outcome.operation_id)
        self.assertIn(outcome.operation_id, str(self.journal().to_payload()))

    def test_a_retried_submission_does_not_become_a_second_correction(self) -> None:
        self.make_live_run()

        first = self.submit(operation_id="op-1")
        second = self.submit(operation_id="op-1")

        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(1, len(self.journal().requests))

    def test_the_outcome_payload_carries_no_feedback_text(self) -> None:
        self.make_live_run()
        secret = "never put this in a projection"

        outcome = self.submit(feedback=secret)

        self.assertNotIn(secret, str(outcome.to_payload()))


class UndeliverableChannelTests(AdmissionTestCase):
    """A submission the urgent channel could not carry is closed, not left open."""

    def setUp(self) -> None:
        super().setUp()
        self.write_state()
        # A lock naming an endpoint that no process is reading: the exact state
        # a runner leaves behind when it dies without tearing its FIFO down.
        abandoned = self.state_dir / "abandoned-redirect.fifo"
        abandoned.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(abandoned, 0o600)
        self.write_lock(redirect_endpoint=abandoned)

    def test_a_dead_endpoint_yields_unavailable_rather_than_accepted(self) -> None:
        outcome = self.submit()

        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, outcome.outcome)
        self.assertFalse(outcome.delivery.delivered)
        self.assertEqual(control_signal.REASON_ENDPOINT_CLOSED, outcome.delivery.reason)

    def test_the_undeliverable_request_is_durably_settled(self) -> None:
        """So no later runner can decide the correction landed after all."""
        outcome = self.submit()

        settled = self.journal().request(outcome.operation_id)
        self.assertEqual(redirect_store_vocab.STATE_REJECTED, settled.state)
        self.assertEqual(
            redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE, settled.reason
        )

    def test_a_settled_undeliverable_request_cannot_later_be_accepted(self) -> None:
        outcome = self.submit()

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_admission.accept_redirect(
                self.plan_dir,
                outcome.operation_id,
                target=redirect_store_models.RedirectTarget(RUN_ID, "session-9", 1),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_ALREADY_SETTLED, caught.exception.reason
        )

    def test_the_feedback_is_still_recoverable_after_the_refusal(self) -> None:
        """Refused is not lost: the operator's words are still in the journal."""
        outcome = self.submit(feedback="the correction that did not land")

        self.assertEqual(
            "the correction that did not land",
            redirect_store_journal.read_feedback(
                self.plan_dir, outcome.operation_id, state_dir=self.state_dir
            ),
        )

    def test_an_unwritable_settlement_leaves_the_request_open_not_accepted(
        self,
    ) -> None:
        original = redirect_store_requests.reject_request

        def failing(*args, **kwargs):
            raise redirect_store_models.RedirectStoreError(
                redirect_store_vocab.ERROR_JOURNAL_WRITE, "device is gone"
            )

        redirect_store_requests.reject_request = failing
        self.addCleanup(setattr, redirect_store_requests, "reject_request", original)

        outcome = self.submit()

        redirect_store_requests.reject_request = original
        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, outcome.outcome)
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED,
            self.journal().request(outcome.operation_id).state,
        )


class RunnerSideTests(AdmissionTestCase):
    """The read and accept path the live runner uses."""

    def test_pending_requests_are_scoped_to_the_turn_that_owns_them(self) -> None:
        self.make_live_run()
        this_turn = redirect_store_models.RedirectTarget(RUN_ID, "session-9", 1)
        next_turn = redirect_store_models.RedirectTarget(RUN_ID, "session-9", 2)
        mine = self.submit(wait_seconds=0.0).operation_id
        redirect_store_requests.submit_request(
            self.plan_dir,
            target=next_turn,
            feedback="for the next turn",
            state_dir=self.state_dir,
        )

        pending = redirect_admission.pending_redirects(
            self.plan_dir, target=this_turn, state_dir=self.state_dir
        )

        self.assertEqual((mine,), tuple(r.operation_id for r in pending))

    def test_accepting_settles_the_request_and_empties_the_queue(self) -> None:
        self.make_live_run()
        target = redirect_store_models.RedirectTarget(RUN_ID, "session-9", 1)
        outcome = self.submit(wait_seconds=0.0)

        accepted = redirect_admission.accept_redirect(
            self.plan_dir, outcome.operation_id, target=target, state_dir=self.state_dir
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(
            (),
            redirect_admission.pending_redirects(
                self.plan_dir, target=target, state_dir=self.state_dir
            ),
        )

    def test_a_runner_cannot_accept_a_request_aimed_at_another_turn(self) -> None:
        self.make_live_run()
        outcome = self.submit(wait_seconds=0.0)

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_admission.accept_redirect(
                self.plan_dir,
                outcome.operation_id,
                target=redirect_store_models.RedirectTarget(RUN_ID, "session-9", 2),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_TARGET_MISMATCH, caught.exception.reason
        )


if __name__ == "__main__":
    unittest.main()
