"""Reading a remote submission and holding it in the bounded command queue."""

from __future__ import annotations

import unittest
from typing import Any

from asl_workbench import remote_actions
from asl_workbench.operations import (
    DETAIL_BRIDGE_UNAVAILABLE,
    DETAIL_DEADLINE_PASSED,
    METHOD_ABORT,
    METHOD_LAUNCH,
    METHOD_PLAN_ARCHIVE,
    METHOD_QUESTION_REPLY,
    METHOD_REPLY,
    METHOD_SELECT_HARNESS,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_UNLOCK,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_PENDING,
    OPERATION_REJECTED,
)
from asl_workbench.transport import (
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNAUTHORIZED,
    ERROR_WRONG_TARGET,
    RECEIPT_DETAIL_CODES,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
)
from tests.support_workbench_remote_actions import (
    ACTION_TOKEN,
    NOW,
    PLAN_KEY,
    REPLY_REF,
    RUN_ID,
    RUN_KEY,
    make_request,
    submit_body,
)

ASL_TEST_PORTABLE = True

# --------------------------------------------------------------------------
# Reading a submission
# --------------------------------------------------------------------------


class SubmissionTests(unittest.TestCase):
    """What this connector will agree to hold, decided before it holds it."""

    def parse(self, **overrides: Any) -> remote_actions.MutationRequest:
        return remote_actions.parse_submission(
            submit_body(**overrides), now=NOW, deadline=NOW + 30.0
        )

    def refusal(self, **overrides: Any) -> remote_actions.RemoteActionError:
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            self.parse(**overrides)
        return caught.exception

    def test_a_submission_becomes_a_command_with_a_bounded_window(self) -> None:
        request = self.parse()

        self.assertEqual("op-1", request.operation_id)
        self.assertEqual(METHOD_STOP_AFTER_CURRENT, request.method)
        self.assertEqual(PLAN_KEY, request.plan_key)
        self.assertEqual(NOW + 30.0, request.deliver_by)
        self.assertEqual(RUN_ID, request.run_id)
        # The operation id doubles as the command id, which is what lets a
        # restart tell a delivered mutation from a browser's own.
        self.assertEqual(request.operation_id, request.command_id)

    def test_the_connectors_ceiling_beats_a_generous_service(self) -> None:
        """A service may ask for an hour; it gets the window this end honours."""
        far = remote_actions.parse_submission(
            submit_body(), now=NOW, deadline=NOW + 3600.0
        )

        self.assertEqual(
            NOW + remote_actions.MAX_DELIVERY_WINDOW_SECONDS, far.deliver_by
        )

    def test_a_tighter_service_deadline_wins_over_the_ceiling(self) -> None:
        near = remote_actions.parse_submission(
            submit_body(), now=NOW, deadline=NOW + 5.0
        )

        self.assertEqual(NOW + 5.0, near.deliver_by)

    def test_a_window_that_closed_in_flight_is_expired_not_refused(self) -> None:
        """``expired`` is the word that means *it certainly did not run*."""
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            remote_actions.parse_submission(submit_body(), now=NOW, deadline=NOW - 1.0)

        self.assertEqual(RECEIPT_EXPIRED, caught.exception.outcome)
        self.assertEqual(ERROR_DEADLINE, caught.exception.detail_code)

    def test_a_method_this_connector_will_not_deliver_is_refused(self) -> None:
        error = self.refusal(method="detail.read")

        self.assertEqual(RECEIPT_REFUSED, error.outcome)
        self.assertEqual(ERROR_FORBIDDEN_METHOD, error.detail_code)

    def test_a_reply_without_a_way_to_fetch_its_prose_is_refused(self) -> None:
        """Carrying it out would answer the question with an empty string."""
        error = self.refusal(method=METHOD_QUESTION_REPLY, reply_ref=REPLY_REF)

        self.assertEqual(ERROR_FORBIDDEN_METHOD, error.detail_code)

    def test_a_reply_reference_without_a_token_is_refused_too(self) -> None:
        error = self.refusal(method=METHOD_REPLY, action_token=ACTION_TOKEN)

        self.assertEqual(ERROR_FORBIDDEN_METHOD, error.detail_code)

    def test_acting_on_a_live_run_needs_the_proof_asl_issued(self) -> None:
        payload = submit_body(method=METHOD_ABORT)
        payload.pop("bridge_token")

        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            remote_actions.parse_submission(payload, now=NOW, deadline=NOW + 30.0)

        self.assertEqual(ERROR_UNAUTHORIZED, caught.exception.detail_code)

    def test_the_methods_not_decided_against_a_live_run_need_no_proof(self) -> None:
        """No run yet, a run already gone, a plan not running, or a *future* turn.

        Selecting a harness belongs here rather than with the other two controls
        because ASL gives it no action to validate a proof against: demanding
        one would refuse every submission instead of checking anything.
        """
        for method in (
            METHOD_LAUNCH,
            METHOD_UNLOCK,
            METHOD_PLAN_ARCHIVE,
            METHOD_SELECT_HARNESS,
        ):
            with self.subTest(method=method):
                payload = submit_body(method=method)
                payload.pop("bridge_token")
                request = remote_actions.parse_submission(
                    payload, now=NOW, deadline=NOW + 30.0
                )
                self.assertEqual(method, request.method)

    def test_a_submission_without_a_plan_key_names_no_target(self) -> None:
        error = self.refusal(target={"run_key": RUN_KEY})

        self.assertEqual(ERROR_WRONG_TARGET, error.detail_code)

    def test_every_refusal_word_is_one_a_receipt_may_carry(self) -> None:
        """The receipt vocabulary is narrower than the error vocabulary.

        ``invalid-field`` and ``missing-field`` are transport errors and are
        *not* receipt detail codes, so a refusal reaching for one would raise
        inside the exception rather than reach the far end.
        """
        cases: list[dict[str, Any]] = [
            {"method": "detail.read"},
            {"operation_id": "not a valid id"},
            {"target": {"run_key": RUN_KEY}},
            {"method": METHOD_QUESTION_REPLY},
            {"harness": ""},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(remote_actions.RemoteActionError) as caught:
                    self.parse(**overrides)
                self.assertIn(caught.exception.detail_code, RECEIPT_DETAIL_CODES)

    def test_a_request_from_the_other_lane_is_not_a_submission(self) -> None:
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            remote_actions.parse_submission(
                {"request": "detail.read", "detail": "logs"}, now=NOW
            )

        self.assertEqual(ERROR_FORBIDDEN_METHOD, caught.exception.detail_code)

    def test_a_result_may_only_speak_words_the_transport_knows(self) -> None:
        with self.assertRaises(ValueError):
            remote_actions.MutationResult(status=OPERATION_PENDING)
        with self.assertRaises(ValueError):
            remote_actions.MutationResult(
                status=OPERATION_REJECTED, detail_code="made-up"
            )

    def test_a_queue_only_status_is_folded_onto_one_that_may_be_reported(
        self,
    ) -> None:
        self.assertEqual(
            OPERATION_DISPATCHED, remote_actions.reportable_status(OPERATION_PENDING)
        )
        self.assertEqual(
            OPERATION_REJECTED, remote_actions.reportable_status("cancelled")
        )
        self.assertEqual(
            OPERATION_CONSUMED, remote_actions.reportable_status(OPERATION_CONSUMED)
        )


# --------------------------------------------------------------------------
# Holding it
# --------------------------------------------------------------------------


class QueueTests(unittest.TestCase):
    """Bounded, cancellable up to the attempt, and forgetful on every exit."""

    def setUp(self) -> None:
        self.queue = remote_actions.MutationQueue()

    def fill(self, count: int) -> list[remote_actions.MutationRequest]:
        held = []
        for index in range(count):
            request = make_request(operation_id=f"op-{index}")
            self.assertTrue(self.queue.accept(request, now=NOW))
            held.append(request)
        return held

    def test_the_queue_is_bounded_while_the_service_can_still_react(self) -> None:
        self.fill(remote_actions.MAX_PENDING_MUTATIONS)

        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            self.queue.accept(make_request(operation_id="op-extra"), now=NOW)

        self.assertEqual(RECEIPT_REFUSED, caught.exception.outcome)
        self.assertEqual(DETAIL_BRIDGE_UNAVAILABLE, caught.exception.detail_code)

    def test_a_resubmission_of_a_command_in_hand_is_dropped_silently(self) -> None:
        """The answer the service waits for is the outcome, and it is coming."""
        self.fill(1)
        duplicate = make_request(operation_id="op-0", reply_ref=REPLY_REF)

        self.assertFalse(self.queue.accept(duplicate, now=NOW))
        self.assertEqual(1, self.queue.pending)
        # Dropped *and* disarmed: the copy that lost cannot redeem anything.
        self.assertIsNone(duplicate.reply_ref)
        self.assertEqual("", duplicate.action_token)

    def test_a_command_whose_window_closed_before_it_was_queued_is_expired(
        self,
    ) -> None:
        with self.assertRaises(remote_actions.RemoteActionError) as caught:
            self.queue.accept(make_request(deliver_by=NOW - 1.0), now=NOW)

        self.assertEqual(RECEIPT_EXPIRED, caught.exception.outcome)
        self.assertEqual(DETAIL_DEADLINE_PASSED, caught.exception.detail_code)

    def test_cancellation_reaches_a_waiting_command(self) -> None:
        (request,) = self.fill(1)

        cancelled = self.queue.cancel("op-0")

        self.assertIs(request, cancelled)
        self.assertEqual(0, self.queue.pending)

    def test_cancellation_does_not_reach_one_being_carried_out(self) -> None:
        """The attempt is running; its result is the only honest answer left."""
        self.fill(1)
        self.assertIsNotNone(self.queue.next_attempt(NOW))

        self.assertIsNone(self.queue.cancel("op-0"))
        self.assertEqual(1, self.queue.attempting)

    def test_a_command_is_handed_out_once_and_only_once(self) -> None:
        self.fill(2)

        first = self.queue.next_attempt(NOW)
        second = self.queue.next_attempt(NOW)
        third = self.queue.next_attempt(NOW)

        assert first is not None and second is not None
        self.assertEqual(["op-0", "op-1"], [first.operation_id, second.operation_id])
        self.assertIsNone(third)

    def test_expiry_reaches_only_what_is_still_waiting(self) -> None:
        self.fill(2)
        self.queue.next_attempt(NOW)

        expired = self.queue.expire(NOW + 31.0)

        self.assertEqual(["op-1"], [request.operation_id for request in expired])
        self.assertEqual(1, self.queue.attempting)

    def test_abandoning_drops_what_waits_and_leaves_what_runs(self) -> None:
        self.fill(3)
        self.queue.next_attempt(NOW)

        abandoned = self.queue.abandon()

        self.assertEqual(
            ["op-1", "op-2"], [request.operation_id for request in abandoned]
        )
        self.assertEqual(0, self.queue.pending)
        self.assertEqual(1, self.queue.attempting)

    def test_every_exit_takes_the_body_reference_and_both_tokens(self) -> None:
        exits = {
            "cancel": lambda queue: queue.cancel("op-0"),
            "expire": lambda queue: queue.expire(NOW + 31.0),
            "abandon": lambda queue: queue.abandon(),
            "settle": lambda queue: queue.settle("op-0"),
            "clear": lambda queue: queue.clear(),
        }
        for name, leave in exits.items():
            with self.subTest(exit=name):
                queue = remote_actions.MutationQueue()
                request = make_request(
                    operation_id="op-0",
                    method=METHOD_QUESTION_REPLY,
                    reply_ref=REPLY_REF,
                    action_token=ACTION_TOKEN,
                )
                queue.accept(request, now=NOW)

                leave(queue)

                self.assertIsNone(request.reply_ref)
                self.assertEqual("", request.action_token)
                self.assertEqual("", request.bridge_token)


# --------------------------------------------------------------------------
# The lane
# --------------------------------------------------------------------------
