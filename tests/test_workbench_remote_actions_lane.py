"""The mutation lane of a connector: carrying, body redemption and lost responses."""

from __future__ import annotations

import json
import unittest

from asl_workbench import remote_actions
from asl_workbench.operations import (
    DETAIL_BRIDGE_UNAVAILABLE,
    DETAIL_DEADLINE_PASSED,
    DETAIL_NO_RESULT,
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_EXPIRED,
    OPERATION_UNCONFIRMED,
)
from asl_workbench.transport import (
    CAPABILITY_DETAIL,
    CAPABILITY_MUTATE,
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNSUPPORTED_CAPABILITY,
    KIND_OPERATION,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    VIEW_HEARTBEAT,
    request_envelope,
)
from tests.support_workbench_remote_actions import (
    ACTION_TOKEN,
    BRIDGE_TOKEN,
    CONNECTOR_ID,
    NOW,
    REPLY_TEXT,
    TARGET_ID,
    HeldWorker,
    LinkChannel,
    SettlingPerformer,
    StubBodySource,
    StubPerformer,
    StubResponder,
    detail_request,
    drive,
    make_connector,
    reply_body,
    submission,
)


class LaneTests(unittest.TestCase):
    """Mutation delivery beside the reads, never inside them and never above."""

    def test_a_performer_makes_the_connector_stop_being_observation_only(
        self,
    ) -> None:
        connector = make_connector()

        self.assertIn(CAPABILITY_MUTATE, connector.capabilities)
        self.assertFalse(connector.observation_only)
        self.assertFalse(connector.hello()["observation_only"])

    def test_without_a_performer_the_connector_declares_observation_only(
        self,
    ) -> None:
        connector = make_connector(performer=None, body_source=None)

        self.assertNotIn(CAPABILITY_MUTATE, connector.capabilities)
        self.assertTrue(connector.observation_only)
        self.assertIn(CAPABILITY_DETAIL, connector.capabilities)

    def test_a_performer_without_a_responder_offers_mutate_and_not_detail(
        self,
    ) -> None:
        """Derived from what was wired, so neither implies the other."""
        connector = make_connector(responder=None)

        self.assertIn(CAPABILITY_MUTATE, connector.capabilities)
        self.assertNotIn(CAPABILITY_DETAIL, connector.capabilities)

    def test_a_carried_submission_is_carried_out_and_reported(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(KIND_OPERATION, report["kind"])
        self.assertEqual(CAPABILITY_MUTATE, report["capability"])
        self.assertEqual("op-1", report["payload"]["operation_id"])
        self.assertEqual(OPERATION_ACCEPTED, report["payload"]["status"])
        self.assertEqual(METHOD_STOP_AFTER_CURRENT, report["payload"]["method"])
        self.assertEqual(1, len(performer.calls))

    def test_an_accepted_mutation_is_reported_again_when_asl_consumes_it(self) -> None:
        channel = LinkChannel()
        performer = SettlingPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1"))

        drive(connector, 6)
        drive(connector, 6, start=NOW + 2.0)

        reports = channel.operations()
        self.assertEqual(
            [OPERATION_ACCEPTED, OPERATION_CONSUMED],
            [report["payload"]["status"] for report in reports],
        )
        self.assertEqual(["op-1"], performer.reconciliations)
        self.assertEqual(0, connector.pending_mutations())

    def test_the_report_is_uncorrelated_because_the_receipt_is_the_outcome(
        self,
    ) -> None:
        """An operation frame answers by ``operation_id``, not ``request_id``."""
        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertIsNone(report.get("request_id"))
        self.assertEqual([], channel.receipts())

    def test_a_submission_to_a_connector_that_does_not_act_is_refused(self) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=None, body_source=None)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (receipt,) = channel.receipts()
        self.assertEqual(RECEIPT_REFUSED, receipt["payload"]["outcome"])
        self.assertEqual(
            ERROR_UNSUPPORTED_CAPABILITY, receipt["payload"]["detail_code"]
        )

    def test_a_refused_submission_names_the_capability_it_belongs_to(self) -> None:
        """So the far end can route the refusal rather than guess at it."""
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=None, body_source=None)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (receipt,) = channel.receipts()
        self.assertEqual(CAPABILITY_MUTATE, receipt["capability"])

    def test_a_submission_that_arrives_past_its_deadline_is_never_journalled(
        self,
    ) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1", deadline=NOW + 0.001))

        drive(connector, 6, start=NOW + 10.0)

        (receipt,) = channel.receipts()
        self.assertEqual(RECEIPT_EXPIRED, receipt["payload"]["outcome"])
        self.assertEqual(ERROR_DEADLINE, receipt["payload"]["detail_code"])
        self.assertEqual([], performer.calls)

    def test_a_refused_submission_is_refused_before_it_is_queued(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1", method=METHOD_QUESTION_REPLY))

        drive(connector, 6)

        (receipt,) = channel.receipts()
        self.assertEqual(ERROR_FORBIDDEN_METHOD, receipt["payload"]["detail_code"])
        self.assertEqual([], performer.calls)
        self.assertEqual(0, connector.pending_mutations())

    def test_a_resent_submission_is_carried_out_once(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, performer=performer, run_mutation=worker
        )
        envelope = submission("m1")
        channel.carry(envelope)
        channel.carry(envelope)

        drive(connector, 6)
        worker.release()
        drive(connector, 4)

        self.assertEqual(1, len(performer.calls))
        self.assertEqual(1, len(channel.operations()))

    def test_mutations_are_carried_out_one_at_a_time(self) -> None:
        """Two actions at once are two decisions against state neither saw."""
        channel = LinkChannel()
        performer = StubPerformer()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, performer=performer, run_mutation=worker
        )
        channel.carry(submission("m1", operation_id="op-1"))
        channel.carry(submission("m2", operation_id="op-2"))

        drive(connector, 8)

        self.assertEqual(1, len(worker.pending))
        worker.release()
        drive(connector, 8)
        self.assertEqual(1, len(worker.pending))

    def test_a_read_and_a_mutation_do_not_wait_on_each_other(self) -> None:
        channel = LinkChannel()
        responder = StubResponder()
        reads = HeldWorker()
        connector = make_connector(
            channel=channel,
            responder=responder,
            run_answer=reads,
        )
        channel.carry(detail_request("r1"))
        channel.carry(submission("m1"))

        drive(connector, 8)

        # The read is stuck on its worker; the mutation went out anyway.
        self.assertEqual(1, len(reads.pending))
        self.assertEqual(1, len(channel.operations()))

    def test_the_heartbeat_outranks_a_finished_mutation(self) -> None:
        """A connector the receiver has written off cannot report anything."""
        channel = LinkChannel()
        connector = make_connector(channel=channel, heartbeat_interval=0.001)
        channel.carry(submission("m1"))

        lanes = drive(connector, 3)

        self.assertEqual(VIEW_HEARTBEAT, lanes[0])

    def test_a_waiting_command_whose_window_closes_is_reported_expired(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, performer=performer, run_mutation=worker
        )
        channel.carry(submission("m1", operation_id="op-1"))
        channel.carry(submission("m2", operation_id="op-2"))
        drive(connector, 6)

        drive(connector, 4, start=NOW + 31.0)

        reported = {
            frame["payload"]["operation_id"]: frame["payload"]
            for frame in channel.operations()
        }
        self.assertEqual(OPERATION_EXPIRED, reported["op-2"]["status"])
        self.assertEqual(DETAIL_DEADLINE_PASSED, reported["op-2"]["detail_code"])
        # ``op-1`` was handed to the worker before the window closed, so it is
        # past the point where "it did not run" could still be true.
        self.assertNotIn("op-1", reported)

    def test_stopping_drops_every_command_still_in_hand(self) -> None:
        channel = LinkChannel()
        worker = HeldWorker()
        connector = make_connector(channel=channel, run_mutation=worker)
        channel.carry(submission("m1"))
        drive(connector, 2)

        connector.stop()

        self.assertEqual(0, connector.pending_mutations())
        self.assertIsNone(connector.step(NOW + 1.0))


# --------------------------------------------------------------------------
# The body
# --------------------------------------------------------------------------


class BodyTests(unittest.TestCase):
    """Prose is fetched at the moment of use, and nowhere else, ever."""

    def test_the_body_is_redeemed_inside_the_attempt_and_handed_straight_on(
        self,
    ) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        source = StubBodySource()
        connector = make_connector(
            channel=channel, performer=performer, body_source=source
        )
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        self.assertEqual(["op-1"], source.fetches)
        (request, body) = performer.calls[0]
        self.assertEqual(REPLY_TEXT, body)
        self.assertEqual(METHOD_QUESTION_REPLY, request.method)

    def test_a_command_that_never_ran_never_redeemed_anything(self) -> None:
        """The window closes while it waits; the prose stayed on the Pi."""
        channel = LinkChannel()
        source = StubBodySource()
        worker = HeldWorker()
        connector = make_connector(
            channel=channel, body_source=source, run_mutation=worker
        )
        channel.carry(
            request_envelope(
                payload=reply_body(operation_id="op-2"),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m2",
                deadline=NOW + 30.0,
            )
        )
        channel.carry(submission("m1", operation_id="op-1"))
        drive(connector, 6)

        drive(connector, 4, start=NOW + 31.0)

        self.assertEqual([], source.fetches)

    def test_a_refused_redemption_settles_expired_rather_than_retrying(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        source = StubBodySource(
            error=remote_actions.BodyUnavailable(
                "the token was spent", detail_code="action-token-refused"
            )
        )
        connector = make_connector(
            channel=channel, performer=performer, body_source=source
        )
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_EXPIRED, report["payload"]["status"])
        self.assertEqual("action-token-refused", report["payload"]["detail_code"])
        self.assertEqual([], performer.calls)
        assert connector.mutation_fault is not None
        self.assertIn("token was spent", connector.mutation_fault)

    def test_a_body_bearing_command_with_no_redemption_route_is_expired(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer()
        connector = make_connector(
            channel=channel, performer=performer, body_source=None
        )
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_EXPIRED, report["payload"]["status"])
        self.assertEqual(DETAIL_BRIDGE_UNAVAILABLE, report["payload"]["detail_code"])
        self.assertEqual([], performer.calls)

    def test_no_frame_the_lane_produces_carries_prose(self) -> None:
        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.carry(
            request_envelope(
                payload=reply_body(),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 6)

        rendered = json.dumps(channel.frames)
        self.assertNotIn(REPLY_TEXT, rendered)
        self.assertNotIn(ACTION_TOKEN, rendered)
        self.assertNotIn(BRIDGE_TOKEN, rendered)


# --------------------------------------------------------------------------
# Losing the answer
# --------------------------------------------------------------------------


class LostResponseTests(unittest.TestCase):
    """The two ways an attempt can end without anybody being able to say what."""

    def test_a_performer_that_raises_is_unconfirmed_rather_than_failed(self) -> None:
        """It may have acted before it raised, and nobody can say which."""
        channel = LinkChannel()
        performer = StubPerformer(error=RuntimeError("the runner vanished"))
        connector = make_connector(channel=channel, performer=performer)
        channel.carry(submission("m1"))

        drive(connector, 6)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_UNCONFIRMED, report["payload"]["status"])
        self.assertEqual(DETAIL_NO_RESULT, report["payload"]["detail_code"])
        assert connector.mutation_fault is not None
        self.assertIn("RuntimeError", connector.mutation_fault)

    def test_a_dropped_link_abandons_what_waits_and_says_so(self) -> None:
        channel = LinkChannel()
        worker = HeldWorker()
        connector = make_connector(channel=channel, run_mutation=worker)
        channel.carry(submission("m1", operation_id="op-1"))
        channel.carry(submission("m2", operation_id="op-2"))
        drive(connector, 4)
        channel.failing = True

        drive(connector, 2, start=NOW + 1.0)
        channel.failing = False
        drive(connector, 8, start=NOW + 120.0)

        reported = {
            frame["payload"]["operation_id"]: frame["payload"]
            for frame in channel.operations()
        }
        self.assertEqual(OPERATION_EXPIRED, reported["op-2"]["status"])
        self.assertEqual(DETAIL_BRIDGE_UNAVAILABLE, reported["op-2"]["detail_code"])

    def test_a_report_of_what_was_carried_out_survives_the_link_dropping(
        self,
    ) -> None:
        """It is the only record this end has of what ASL actually did."""
        channel = LinkChannel()
        connector = make_connector(channel=channel)
        channel.carry(submission("m1"))
        drive(connector, 4)
        channel.failing = True
        drive(connector, 2, start=NOW + 1.0)

        channel.failing = False
        drive(connector, 8, start=NOW + 120.0)

        (report,) = channel.operations()
        self.assertEqual(OPERATION_ACCEPTED, report["payload"]["status"])


# --------------------------------------------------------------------------
# Restarting
# --------------------------------------------------------------------------
