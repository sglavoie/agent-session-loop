"""Test workbench remote reads lanes."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from asl_workbench import catalog
from asl_workbench import connector_protocols as portable_protocols
from asl_workbench import connector_support as portable_support
from asl_workbench.transport import (
    CAPABILITY_DETAIL,
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNSUPPORTED_CAPABILITY,
    KIND_DETAIL,
    KIND_RECEIPT,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    VIEW_HEARTBEAT,
    VIEW_SNAPSHOT,
)
from tests import support_workbench_remote_reads as fixtures

ASL_TEST_PORTABLE = True


class LaneTests(unittest.TestCase):
    """Detail traffic beside the cycle, never inside it and never above it."""

    def read_request(self, request_id: str = "r1", **overrides: Any) -> Any:
        client = fixtures.make_client()
        fields: dict[str, Any] = {
            "request_id": request_id,
            "detail": "logs",
            "plan_key": fixtures.PLAN_KEY,
        }
        fields.update(overrides)
        ticket = client.read(now=fixtures.NOW, **fields)
        assert ticket.envelope is not None
        return ticket.envelope

    def drive(
        self,
        connector: Any,
        channel: fixtures.LinkChannel,
        steps: int,
    ) -> list[str]:
        lanes: list[str] = []
        for index in range(steps):
            lane = connector.step(fixtures.NOW + 0.1 * (index + 1))
            if lane is not None:
                lanes.append(lane)
        del channel
        return lanes

    # -- what the connector says it can do -------------------------------
    def test_a_responder_makes_the_connector_declare_detail(self) -> None:
        connector = fixtures.make_connector()
        self.assertIn(CAPABILITY_DETAIL, connector.capabilities)
        self.assertIn(CAPABILITY_DETAIL, connector.hello()["capabilities"])

    def test_without_a_responder_detail_is_not_declared(self) -> None:
        connector = fixtures.make_connector(responder=None)
        self.assertNotIn(CAPABILITY_DETAIL, connector.capabilities)
        self.assertEqual(
            tuple(connector.capabilities), portable_support.OBSERVATION_CAPABILITIES
        )

    # -- a round trip ----------------------------------------------------
    def test_a_carried_request_is_answered_on_a_later_frame(self) -> None:
        channel = fixtures.LinkChannel()
        responder = fixtures.StubResponder()
        connector = fixtures.make_connector(channel=channel, responder=responder)
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        answers = channel.answers()
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["kind"], KIND_DETAIL)
        self.assertEqual(answers[0]["request_id"], "r1")
        self.assertEqual(answers[0]["payload"]["content"], "the log text")
        self.assertEqual(len(responder.requests), 1)
        self.assertEqual(responder.requests[0]["detail"], "logs")

    def test_a_resent_request_is_answered_once(self) -> None:
        channel = fixtures.LinkChannel()
        responder = fixtures.StubResponder()
        connector = fixtures.make_connector(channel=channel, responder=responder)
        envelope = self.read_request("r1")
        channel.carry(envelope)
        channel.carry(envelope)
        self.drive(connector, channel, 8)
        self.assertEqual(len(channel.answers()), 1)
        self.assertEqual(len(responder.requests), 1)

    def test_completion_after_collection_preserves_both_read_answers(self) -> None:
        channel = fixtures.LinkChannel()
        worker = fixtures.HeldWorker()
        connector = fixtures.make_connector(channel=channel, run_answer=worker)
        channel.carry(self.read_request("r1"))
        channel.carry(self.read_request("r2"))
        self.drive(connector, channel, 8)

        with mock.patch.object(
            connector, "_expire_reads", side_effect=lambda now: worker.release()
        ):
            connector.step(fixtures.NOW + 1.0)
        worker.release()
        for index in range(4):
            connector.step(fixtures.NOW + 2.0 + 0.1 * index)
        worker.release()
        for index in range(4):
            connector.step(fixtures.NOW + 3.0 + 0.1 * index)

        self.assertEqual(
            ["r1", "r2"], [answer["request_id"] for answer in channel.answers()]
        )

    def test_a_request_for_another_connector_is_not_taken_in(self) -> None:
        from asl_workbench.transport import request_envelope as compose

        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel)
        channel.carry(
            compose(
                payload={
                    "request": "detail.read",
                    "detail": "logs",
                    "target": {"plan_key": fixtures.PLAN_KEY},
                },
                connector_id="another-mac",
                target_id=fixtures.TARGET_ID,
                sent_at=fixtures.NOW,
                request_id="r1",
                deadline=fixtures.NOW + 30.0,
            )
        )
        self.drive(connector, channel, 4)
        self.assertEqual(channel.answers(), [])
        self.assertEqual(
            connector.request_fault, "request addressed to another connector"
        )

    def test_an_undecodable_request_is_recorded_and_does_not_degrade_the_cycle(
        self,
    ) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel)
        channel.inbound.append(b"{not json")
        self.drive(connector, channel, 3)
        assert connector.request_fault is not None
        self.assertIn("undecodable request", connector.request_fault)
        self.assertIsNone(connector.degraded_reason)

    # -- separation from mutation ----------------------------------------
    def test_a_mutation_is_refused_on_arrival_and_never_reaches_the_responder(
        self,
    ) -> None:
        channel = fixtures.LinkChannel()
        responder = fixtures.StubResponder()
        connector = fixtures.make_connector(channel=channel, responder=responder)
        channel.carry(fixtures.mutation_request("op-req"))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["outcome"], RECEIPT_REFUSED)
        self.assertEqual(answer["payload"]["detail_code"], ERROR_UNSUPPORTED_CAPABILITY)
        self.assertEqual(responder.requests, [])

    def test_an_offered_capability_with_the_wrong_method_is_refused(self) -> None:
        from asl_workbench.transport import request_envelope as compose

        channel = fixtures.LinkChannel()
        responder = fixtures.StubResponder()
        connector = fixtures.make_connector(channel=channel, responder=responder)
        # ``observation.refresh`` maps to a capability this connector does
        # offer, but the correlated lane performs detail reads and nothing else.
        channel.carry(
            compose(
                payload={"request": "observation.refresh"},
                connector_id=fixtures.CONNECTOR_ID,
                target_id=fixtures.TARGET_ID,
                sent_at=fixtures.NOW,
                request_id="r1",
                deadline=fixtures.NOW + 30.0,
            )
        )
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["payload"]["detail_code"], ERROR_FORBIDDEN_METHOD)
        self.assertEqual(responder.requests, [])

    def test_a_connector_without_a_responder_refuses_rather_than_ignores(self) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel, responder=None)
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["detail_code"], ERROR_UNSUPPORTED_CAPABILITY)

    # -- separation from the heartbeat -----------------------------------
    def test_the_heartbeat_goes_first_however_many_answers_are_waiting(self) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel, heartbeat_interval=1.0)
        for index in range(3):
            channel.carry(self.read_request(f"r{index}"))
        lanes = self.drive(connector, channel, 12)
        self.assertEqual(lanes[0], VIEW_HEARTBEAT)
        # Every heartbeat that came due was sent before the answers waiting
        # beside it, and the reads still got out.
        self.assertIn(KIND_DETAIL, lanes)

    def test_a_read_that_never_finishes_does_not_stop_the_heartbeat(self) -> None:
        worker = fixtures.HeldWorker()
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(
            channel=channel, heartbeat_interval=1.0, run_answer=worker
        )
        channel.carry(self.read_request("r1"))
        beats = 0
        for index in range(30):
            if connector.step(fixtures.NOW + 0.5 * index) == VIEW_HEARTBEAT:
                beats += 1
        self.assertGreaterEqual(beats, 5)
        self.assertTrue(connector.answering)
        self.assertEqual(channel.answers(), [])
        self.assertEqual(worker.release(), 1)

    def test_the_carrier_tightens_while_a_service_is_reading(self) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel, heartbeat_interval=60.0)
        # Nothing has been asked: one beat a minute, whatever else goes out.
        idle = [connector.step(fixtures.NOW + index) for index in range(11)]
        self.assertEqual(idle.count(VIEW_HEARTBEAT), 1)
        # The beat that comes due next carries a request back with it, and from
        # then on the carrier runs at the reading cadence rather than the idle
        # one -- including the very next beat, which was scheduled before
        # anybody was asking.
        channel.carry(self.read_request("r1"))
        self.assertEqual(connector.step(fixtures.NOW + 60.0), VIEW_HEARTBEAT)
        active = [connector.step(fixtures.NOW + 60.0 + index) for index in range(1, 11)]
        self.assertGreaterEqual(active.count(VIEW_HEARTBEAT), 5)

    # -- separation from the cycle ---------------------------------------
    def test_an_answer_is_not_buried_behind_a_corpus_being_paged_out(self) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(
            channel=channel,
            source=fixtures.StubSource(self.big_cycle()),
            observation_interval=3600.0,
            heartbeat_interval=600.0,
        )
        channel.carry(self.read_request("r1"))
        lanes = self.drive(connector, channel, 12)
        self.assertIn(KIND_DETAIL, lanes)
        # It did not wait for the whole catalog to drain first.
        self.assertLess(lanes.index(KIND_DETAIL), len(lanes) - 1)

    def test_a_reader_asking_continuously_does_not_starve_the_corpus(self) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(
            channel=channel,
            source=fixtures.StubSource(self.big_cycle()),
            observation_interval=3600.0,
            heartbeat_interval=600.0,
        )
        for index in range(6):
            channel.carry(self.read_request(f"r{index}"))
        lanes = self.drive(connector, channel, 20)
        self.assertIn(KIND_DETAIL, lanes)
        self.assertIn(VIEW_SNAPSHOT, lanes)
        # The two lanes take turns while both have something to send, so the
        # corpus is not left until the reader stops asking. Six frames in,
        # neither lane has had them all.
        opening = lanes[:6]
        self.assertGreaterEqual(opening.count(KIND_DETAIL), 2)
        self.assertGreaterEqual(
            len([lane for lane in opening if lane not in (KIND_DETAIL, KIND_RECEIPT)]),
            2,
        )

    def big_cycle(self) -> portable_protocols.ObservationCycle:
        """A cycle with a snapshot and several pages waiting behind it."""
        records = [
            catalog.record_from_summary(self.summary(f"/tmp/plans/plan-{index}"))
            for index in range(4)
        ]
        built = catalog.build_catalog(records)
        return portable_protocols.ObservationCycle(
            observed_at=fixtures.NOW,
            catalog_pages=tuple(
                built.page(offset=index, limit=1) for index in range(4)
            ),
        )

    def summary(self, plan_dir: str) -> dict[str, Any]:
        payload: dict[str, Any] = {name: None for name in catalog.PRIVATE_FIELDS}
        payload.update({name: None for name in catalog.CACHE_SAFE_FIELDS})
        payload.update(
            {
                "plan_dir": plan_dir,
                "exists": True,
                "primary_state": "ready",
                "recommended_action": "run",
                "urgency_rank": 80,
                "urgency_group": "ready",
                "activity_label": "Ready to run",
                "counts": {"pending": 1, "complete": 0, "empty": 0, "unknown": 0},
                "sources": ["state"],
                "repositories": [],
                "repository_count": 0,
                "warnings": [],
            }
        )
        return payload

    # -- bounds ----------------------------------------------------------
    def test_more_reads_than_the_connector_will_hold_are_refused_not_dropped(
        self,
    ) -> None:
        worker = fixtures.HeldWorker()
        channel = fixtures.LinkChannel()
        # A frequent carrier, because a request only arrives on the
        # acknowledgement of a frame going out: a silent connector is one
        # nobody can queue work on.
        connector = fixtures.make_connector(
            channel=channel, run_answer=worker, heartbeat_interval=0.25
        )
        for index in range(portable_support.MAX_PENDING_REQUESTS + 3):
            channel.carry(self.read_request(f"r{index}"))
        self.drive(connector, channel, 40)
        refusals = [
            frame for frame in channel.answers() if frame["kind"] == KIND_RECEIPT
        ]
        self.assertGreaterEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["payload"]["outcome"], RECEIPT_REFUSED)

    def test_a_responder_that_raises_costs_one_read_and_not_the_deadline(self) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(
            channel=channel,
            responder=fixtures.StubResponder(error=RuntimeError("disk gone")),
        )
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["outcome"], RECEIPT_REFUSED)
        self.assertEqual(answer["payload"]["detail_code"], "bridge-unavailable")
        assert connector.answer_fault is not None
        self.assertIn("RuntimeError", connector.answer_fault)

    def test_an_unsendable_answer_does_not_make_a_healthy_mac_look_degraded(
        self,
    ) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(
            channel=channel,
            responder=fixtures.StubResponder(payload={"detail": "logs"}),
        )
        channel.carry(self.read_request("r1"))
        self.drive(connector, channel, 6)
        self.assertEqual(channel.answers(), [])
        assert connector.answer_fault is not None
        self.assertIn("unsendable", connector.answer_fault)
        self.assertIsNone(connector.degraded_reason)

    # -- expiry, disconnect, restart -------------------------------------
    def test_a_request_past_its_deadline_is_expired_without_being_performed(
        self,
    ) -> None:
        channel = fixtures.LinkChannel()
        responder = fixtures.StubResponder()
        connector = fixtures.make_connector(channel=channel, responder=responder)
        channel.carry(self.read_request("r1", timeout=0.05))
        self.drive(connector, channel, 6)
        (answer,) = channel.answers()
        self.assertEqual(answer["kind"], KIND_RECEIPT)
        self.assertEqual(answer["payload"]["outcome"], RECEIPT_EXPIRED)
        self.assertEqual(answer["payload"]["detail_code"], ERROR_DEADLINE)
        self.assertEqual(responder.requests, [])

    def test_an_answer_whose_deadline_passed_while_queued_is_forgotten(self) -> None:
        worker = fixtures.HeldWorker()
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel, run_answer=worker)
        channel.carry(self.read_request("r1", timeout=20.0))
        connector.step(fixtures.NOW)
        connector.step(fixtures.NOW + 1.0)
        self.assertEqual(worker.release(), 1)
        # The answer is finished and waiting, and then the deadline passes.
        connector.step(fixtures.NOW + 40.0)
        self.assertEqual(channel.answers(), [])
        self.assertEqual(connector.pending_reads(), 0)

    def held_answer(self, channel: fixtures.LinkChannel, **overrides: Any) -> Any:
        """A connector with one finished answer waiting for the channel.

        The heartbeat is due on every step, so it outranks the answer and the
        answer stays queued -- which is the state these two tests are about.
        """
        connector = fixtures.make_connector(
            channel=channel, heartbeat_interval=0.5, **overrides
        )
        channel.carry(self.read_request("r1"))
        for index in range(3):
            connector.step(fixtures.NOW + 1.0 * index)
        self.assertEqual(connector.pending_reads(), 1)
        return connector

    def test_a_dropped_link_clears_the_answers_it_was_holding(self) -> None:
        channel = fixtures.LinkChannel()
        connector = self.held_answer(channel)
        channel.failing = True
        connector.step(fixtures.NOW + 3.0)
        self.assertEqual(connector.pending_reads(), 0)
        self.assertEqual(connector.state, portable_support.STATE_BACKOFF)
        self.assertEqual(channel.answers(), [])

    def test_a_stopped_connector_holds_no_material(self) -> None:
        channel = fixtures.LinkChannel()
        connector = self.held_answer(channel)
        connector.stop()
        self.assertEqual(connector.pending_reads(), 0)
        self.assertEqual(connector.pending(), 0)
        self.assertEqual(connector.state, portable_support.STATE_STOPPED)
        self.assertIsNone(connector.step(fixtures.NOW + 10.0))
        self.assertEqual(channel.answers(), [])

    def test_a_read_in_flight_when_the_connector_stops_is_let_go(self) -> None:
        worker = fixtures.HeldWorker()
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel, run_answer=worker)
        channel.carry(self.read_request("r1"))
        connector.step(fixtures.NOW)
        connector.step(fixtures.NOW + 0.1)
        self.assertTrue(connector.answering)
        connector.stop()
        self.assertFalse(connector.answering)
        self.assertEqual(connector.pending_reads(), 0)
        # The worker finishing afterwards answers nobody.
        worker.release()
        self.assertIsNone(connector.step(fixtures.NOW + 1.0))
        self.assertEqual(channel.answers(), [])


if __name__ == "__main__":
    unittest.main()
