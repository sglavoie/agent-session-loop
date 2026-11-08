"""CarriageTests: carriage."""

from __future__ import annotations

from .workbench_uplink_support import (
    CONNECTOR,
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_DEADLINE_PASSED,
    DETAIL_NO_RESULT,
    NOW,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_UNCONFIRMED,
    PLAN_KEY,
    TARGET,
    Any,
    CarriageTestsBase,
    UplinkRefusal,
    heartbeat,
    mock,
    operation_envelope,
    operation_record,
    submission,
)


class CarriageTests(CarriageTestsBase):
    def test_an_undelivered_copy_cannot_expire_a_concurrently_dispatched_operation(
        self,
    ) -> None:
        self.store.record_operation(
            operation_record("race-departure"),
            connector_id=CONNECTOR,
            received_at=NOW,
        )
        self.uplink.hand_down(submission("race-departure"), now=NOW)
        original = self.store.operation

        def read_then_dispatch(identifier: str) -> Any:
            row = original(identifier)
            self.store.advance_operation(identifier, OPERATION_DISPATCHED, now=NOW + 1)
            return row

        with mock.patch.object(self.store, "operation", side_effect=read_then_dispatch):
            self.assertIsNone(
                self.uplink.ingest(heartbeat(at=NOW + 1000), now=NOW + 1000)
            )
        self.assertEqual(
            self.store.operation("race-departure")["status"], OPERATION_DISPATCHED
        )

    def test_a_submission_leaves_on_the_next_upload_and_is_dispatched_then(
        self,
    ) -> None:
        """`pending` means admitted and still here; `dispatched` means gone.

        The row moves when the frame is handed to an acknowledgement rather
        than when it was queued, because a submission that never found a
        carrier is one the operator may still cancel.
        """
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.assertEqual(self.uplink.carried, 1)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_PENDING)

        carried = self.uplink.ingest(heartbeat(at=NOW + 1), now=NOW + 1)
        assert carried is not None
        self.assertEqual(carried["kind"], "request")
        self.assertEqual(carried["payload"]["operation_id"], "op-1")
        self.assertEqual(carried["request_id"], "submit-op-1")
        self.assertEqual(self.uplink.carried, 0)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_DISPATCHED)
        self.assertEqual(row["dispatched_at"], NOW + 1)

    def test_one_acknowledgement_carries_one_frame(self) -> None:
        for index in (1, 2):
            self.store.record_operation(
                operation_record(f"op-{index}"),
                connector_id=CONNECTOR,
                received_at=NOW,
            )
            self.uplink.hand_down(submission(f"op-{index}"), now=NOW)
        first = self.uplink.ingest(heartbeat(at=NOW + 1), now=NOW + 1)
        assert first is not None
        self.assertEqual(first["payload"]["operation_id"], "op-1")
        self.assertEqual(self.uplink.carried, 1)
        second = self.uplink.ingest(heartbeat(at=NOW + 2), now=NOW + 2)
        assert second is not None
        self.assertEqual(second["payload"]["operation_id"], "op-2")

    def test_an_expired_frame_is_discarded_rather_than_carried(self) -> None:
        """A request past its deadline would be refused at the far end.

        Sending it anyway would spend the one acknowledgement the next live
        request needed, so it is dropped here and the next frame is handed out
        in its place.
        """
        self.store.record_operation(
            operation_record("stale"), connector_id=CONNECTOR, received_at=NOW
        )
        self.store.record_operation(
            operation_record("fresh"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.hand_down(submission("stale"), now=NOW)
        later = NOW + 10_000
        self.uplink.hand_down(submission("fresh"), now=later)
        carried = self.uplink.ingest(heartbeat(at=later), now=later)
        assert carried is not None
        self.assertEqual(carried["payload"]["operation_id"], "fresh")
        self.assertEqual(self.uplink.carried, 0)
        stale = self.store.operation("stale")
        assert stale is not None
        # A command that never left was never dispatched, and will never be:
        # left `pending`, it would hold the plan's one outstanding mutation
        # for good.
        self.assertEqual(stale["status"], OPERATION_EXPIRED)
        self.assertEqual(stale["detail_code"], "deadline-passed")
        self.assertNotIn("dispatched_at", stale)

    def test_the_carry_queue_is_bounded(self) -> None:
        """A bound, because the alternative is growing for a Mac that is gone."""
        self.uplink = self.build(max_carried=2)
        for index in range(2):
            self.uplink.hand_down(submission(f"op-{index}"), now=NOW)
        with self.assertRaises(UplinkRefusal) as raised:
            self.uplink.hand_down(submission("op-over"), now=NOW)
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.reason, "link-congested")
        self.assertEqual(raised.exception.details["carried"], 2)

    def test_a_submission_the_full_queue_refuses_does_not_hold_its_plan(
        self,
    ) -> None:
        self.uplink = self.build(max_carried=1)
        self.uplink.hand_down(submission("op-0"), now=NOW)
        self.store.record_operation(
            operation_record("op-over"), connector_id=CONNECTOR, received_at=NOW
        )
        with self.assertRaises(UplinkRefusal):
            self.uplink.hand_down(submission("op-over"), now=NOW)
        row = self.store.operation("op-over")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_EXPIRED)
        self.assertIsNone(self.store.outstanding_operation(PLAN_KEY))

    def test_silence_drops_the_link_and_what_it_was_holding(self) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.now, self.monotonic = NOW + 10, 10.0
        self.uplink.sweep(self.now)
        self.assertTrue(self.uplink.linked, "ten seconds is a quiet Mac, not no Mac")
        self.now, self.monotonic = NOW + 1_000, 1_000.0
        self.uplink.sweep(self.now)
        self.assertFalse(self.uplink.linked)
        self.assertEqual(self.uplink.carried, 0)

    def test_a_fresh_upload_during_silence_check_keeps_the_link_up(self) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        self.monotonic = 1_000.0
        uploaded = False

        def clock() -> float:
            nonlocal uploaded
            if not uploaded:
                uploaded = True
                self.uplink.ingest(heartbeat(at=NOW + 1_000), now=NOW + 1_000)
            return self.monotonic

        self.uplink.monotonic = clock
        self.uplink.sweep(NOW + 1_000)
        self.assertTrue(uploaded)
        self.assertTrue(self.uplink.linked)
        self.assertEqual(self.uplink.last_upload_at, NOW + 1_000)

    def test_a_submission_dropped_with_the_link_does_not_hold_its_plan(
        self,
    ) -> None:
        self.uplink.ingest(heartbeat(), now=NOW)
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.now, self.monotonic = NOW + 1_000, 1_000.0
        self.uplink.sweep(self.now)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_EXPIRED)
        self.assertIsNone(self.store.outstanding_operation(PLAN_KEY))

    def test_a_restart_expires_what_its_carry_queue_was_holding(self) -> None:
        """The queue is memory; the journal row would hold its plan for good."""
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.uplink = self.build()
        self.uplink.recover(NOW + 1)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_EXPIRED)
        self.assertEqual(row["detail_code"], DETAIL_CONNECTOR_RESTARTED)
        self.assertIsNone(self.store.outstanding_operation(PLAN_KEY))

    def test_an_uncarried_pending_row_expires_only_once_its_window_closes(
        self,
    ) -> None:
        """Between journal write and hand-down a live submission is not queued."""
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.sweep(NOW + 59)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_PENDING)
        self.uplink.sweep(NOW + 60)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_EXPIRED)
        self.assertEqual(row["detail_code"], DETAIL_DEADLINE_PASSED)

    def test_a_carried_pending_row_is_left_for_the_carry_queue(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.uplink.sweep(NOW + 100)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_PENDING)
        self.assertEqual(self.uplink.carried, 1)

    def test_a_frame_whose_row_was_settled_without_it_is_not_carried(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.store.advance_operation("op-1", OPERATION_EXPIRED, now=NOW + 1)
        self.assertIsNone(self.uplink.ingest(heartbeat(at=NOW + 2), now=NOW + 2))
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_EXPIRED)

    def test_a_dispatch_whose_acknowledgement_was_lost_becomes_unconfirmed(
        self,
    ) -> None:
        """Nobody can say whether it ran, which is not the same as holding forever."""
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.assertIsNotNone(self.uplink.ingest(heartbeat(at=NOW + 1), now=NOW + 1))
        grace = self.uplink.command_lifetime_seconds
        self.uplink.sweep(NOW + 60 + grace - 1)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_DISPATCHED)
        self.uplink.sweep(NOW + 60 + grace)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_UNCONFIRMED)
        self.assertEqual(row["detail_code"], DETAIL_NO_RESULT)

    def test_a_late_report_the_sweep_has_concluded_against_is_dropped(
        self,
    ) -> None:
        """Refused, it would be resent on every upload; a later fact still lands."""
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.store.advance_operation(
            "op-1", OPERATION_DISPATCHED, now=NOW, dispatched_at=NOW
        )
        self.store.advance_operation("op-1", OPERATION_UNCONFIRMED, now=NOW + 400)

        def report(status: str, at: float) -> dict[str, Any]:
            return operation_envelope(
                payload={"operation_id": "op-1", "status": status, "observed_at": at},
                connector_id=CONNECTOR,
                target_id=TARGET,
                sent_at=at,
            )

        self.uplink.ingest(report(OPERATION_ACCEPTED, NOW + 401), now=NOW + 401)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_UNCONFIRMED)
        self.uplink.ingest(report(OPERATION_CONSUMED, NOW + 402), now=NOW + 402)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_CONSUMED)

    def test_a_wall_clock_step_is_not_silence(self) -> None:
        """A Pi has no RTC: its wall clock jumps when NTP first answers.

        Silence is how long the link has actually been quiet, so a forward
        step of the wall clock with no time elapsing keeps the link up and
        keeps what it is holding.
        """
        self.uplink.ingest(heartbeat(), now=NOW)
        self.uplink.hand_down(submission("op-1"), now=NOW)
        self.now, self.monotonic = NOW + 86_400, 1.0
        self.uplink.sweep(self.now)
        self.assertTrue(self.uplink.linked)
        self.assertEqual(self.uplink.carried, 1)
        self.assertEqual(self.uplink.last_upload_at, NOW)

    def test_read_ids_stay_distinct_across_a_restart_in_the_same_second(
        self,
    ) -> None:
        first = self.build()._next_read_id()
        second = self.build()._next_read_id()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(f"read-{int(NOW)}."))

    def test_a_sweep_before_any_upload_decides_nothing(self) -> None:
        """A service that has never been uploaded to has no link to declare down.

        The runtime sweeps on a timer from the moment it starts serving, which
        is before the connector's first frame; treating "never" as silence
        would mean a fresh service declared its link dead before it had one.
        """
        self.uplink.sweep(NOW + 100_000)
        self.assertFalse(self.uplink.linked)
        self.assertIsNone(self.uplink.last_upload_at)
