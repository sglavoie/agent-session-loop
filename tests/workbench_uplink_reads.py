"""ReadTests: reads."""

from __future__ import annotations

from .workbench_uplink_support import (
    NOW,
    PLAN_KEY,
    Any,
    ReadTestsBase,
    heartbeat,
    mock,
    validate_envelope,
)


class ReadTests(ReadTestsBase):
    def test_a_carried_read_gives_the_connector_the_same_deadline_as_the_client(
        self,
    ) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        for timeout in (5.0, 90.0):
            with self.subTest(timeout=timeout):
                request_id = f"queued-{timeout}"
                ticket = self.uplink.reads.read(
                    request_id=request_id,
                    detail="state",
                    plan_key=PLAN_KEY,
                    now=NOW,
                    timeout=timeout,
                )
                assert ticket.envelope is not None
                self.uplink._offer(ticket.envelope)
                departed_at = NOW + timeout - 1.0
                carried = self.uplink.ingest(heartbeat(at=departed_at), now=departed_at)
                assert carried is not None
                validate_envelope(carried)
                self.assertEqual(carried["sent_at"], departed_at)
                self.assertEqual(carried["deadline"], departed_at + timeout)
                # A response past the original deadline is still timely at
                # both ends, and reaching the new deadline expires the read.
                self.assertEqual(self.uplink.reads.expire(departed_at + 1.5), ())
                expired = self.uplink.reads.expire(float(carried["deadline"]))
                self.assertEqual([item.request_id for item in expired], [request_id])

    def test_a_read_cancelled_after_dequeue_does_not_spend_the_next_upload(
        self,
    ) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        for request_id in ("cancelled", "live"):
            ticket = self.uplink.reads.read(
                request_id=request_id, detail="state", plan_key=PLAN_KEY, now=NOW
            )
            assert ticket.envelope is not None
            self.uplink._offer(ticket.envelope)
        dequeue = self.uplink._next_carried

        def dequeue_then_cancel() -> Any:
            envelope = dequeue()
            if envelope is not None and envelope["request_id"] == "cancelled":
                self.uplink.reads.cancel("cancelled", now=NOW + 1.0)
            return envelope

        with mock.patch.object(
            self.uplink, "_next_carried", side_effect=dequeue_then_cancel
        ):
            carried = self.uplink.ingest(heartbeat(at=NOW + 1.0), now=NOW + 1.0)
        assert carried is not None
        self.assertEqual(carried["request_id"], "live")
        self.assertEqual(self.uplink.carried, 0)

    def test_a_read_whose_queue_deadline_passed_is_not_revived_at_departure(
        self,
    ) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        ticket = self.uplink.reads.read(
            request_id="stale", detail="state", plan_key=PLAN_KEY, now=NOW, timeout=5.0
        )
        assert ticket.envelope is not None
        self.uplink._offer(ticket.envelope)
        self.assertIsNone(self.uplink.ingest(heartbeat(at=NOW + 5.0), now=NOW + 5.0))
        expired = self.uplink.reads.expire(NOW + 5.0)
        self.assertEqual([item.request_id for item in expired], ["stale"])

    def test_a_read_with_no_link_is_offline_and_costs_no_wait(self) -> None:
        """`offline`, `timeout` and `refused` stay three different answers.

        The operator acts on which one it was: an offline link is a Mac to go
        and look at, a timeout is a read to try again.
        """
        answered = self.fetch()
        self.assertEqual(answered.outcome, "offline")
        self.assertEqual(self.uplink.carried, 0, "nothing was queued for a dead link")

    def test_a_read_nobody_carries_times_out_rather_than_waiting_forever(
        self,
    ) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        answered = self.fetch()
        self.assertIn(answered.outcome, ("timeout", "cancelled"))
        self.assertEqual(
            self.uplink.carried,
            0,
            "an abandoned read is taken back out of the queue, not left to be sent",
        )

    def test_a_wall_clock_step_back_does_not_stretch_a_read_wait(self) -> None:
        """The wait budget is a duration, so it ignores the wall clock.

        The wall clock is stepped back an hour on every reading while the
        monotonic one advances a second at a time: a budget measured on wall
        time would keep this request thread for as long as the clock jumped.
        """
        readings = iter(float(second) for second in range(1_000))

        def wall() -> float:
            self.now -= 3_600.0
            return self.now

        self.uplink = self.build(
            clock=wall,
            monotonic=lambda: next(readings),
            read_wait_seconds=3.0,
        )
        self.uplink.ingest(heartbeat(), now=NOW)
        answered = self.fetch()
        self.assertIn(answered.outcome, ("timeout", "cancelled"))

    def test_every_read_asks_under_its_own_correlation_id(self) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        first = self.fetch()
        second = self.fetch()
        self.assertNotEqual(first.request_id, second.request_id)
