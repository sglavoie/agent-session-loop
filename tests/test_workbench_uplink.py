"""The service's own half of the link.

``tests/test_workbench_connector.py`` covers the Mac end and
``tests/test_workbench_fullstack_*.py`` runs both ends over a link it can break.
This module is about the object in between: what an upload does to the store,
what rides back down in its acknowledgement, and how a read or a control
settles when nothing ever carries it.

Every clock here is an argument, so each test asserts about a decision rather
than racing one. :class:`WaitingReadTests` is the exception and says why.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from asl_workbench.connector_payloads import heartbeat_payload
from asl_workbench.observation import ObservationPolicy
from asl_workbench.operations import (
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    OPERATION_ACCEPTED,
    OPERATION_DISPATCHED,
    OPERATION_PENDING,
)
from asl_workbench.store_workbench import WorkbenchStore
from asl_workbench.transport import (
    REQUEST_DETAIL,
    REQUEST_MUTATE,
    detail_envelope,
    observation_envelope,
    operation_envelope,
    receipt_envelope,
)
from asl_workbench.uplink import Uplink, UplinkRefusal

ASL_TEST_PORTABLE = True

NOW = 1_700_000_000.0
CONNECTOR = "mac-studio"
TARGET = "pi.example.ts.net"
#: Plan and run keys are sixteen hex characters on the wire, so the fixtures
#: are real keys rather than readable names: a test that had to loosen the
#: transport's own field check to run would be testing something else.
PLAN_KEY = "a1b2c3d4e5f60718"
RUN_KEY = "0f1e2d3c4b5a6978"


def operation_record(operation_id: str, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "operation_id": operation_id,
        "command_id": f"cmd-{operation_id}",
        "method": METHOD_STOP_AFTER_CURRENT,
        "status": OPERATION_PENDING,
        "plan_key": PLAN_KEY,
        "run_key": RUN_KEY,
        "run_id": "20260619T140000Z-alpha",
        "requested_at": NOW - 60,
        "deliver_by": NOW + 60,
    }
    record.update(overrides)
    return record


def submission(operation_id: str) -> dict[str, Any]:
    return {
        "request": REQUEST_MUTATE,
        "method": METHOD_STOP_AFTER_CURRENT,
        "operation_id": operation_id,
        "target": {"plan_key": PLAN_KEY, "run_key": RUN_KEY},
    }


def heartbeat(*, at: float = NOW, connector: str = CONNECTOR) -> dict[str, Any]:
    return observation_envelope(
        payload=heartbeat_payload(observed_at=at, bridge_alive=True),
        connector_id=connector,
        target_id=TARGET,
        sent_at=at,
    )


def answer(request_id: str, *, at: float = NOW, content: str = "{}") -> dict[str, Any]:
    return detail_envelope(
        payload={
            "detail": "state",
            "target": {"plan_key": PLAN_KEY},
            "observed_at": at,
            "complete": "complete",
            "content": content,
        },
        connector_id=CONNECTOR,
        target_id=TARGET,
        sent_at=at,
        request_id=request_id,
    )


class UplinkCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = WorkbenchStore.open(
            Path(temporary.name) / "workbench.db",
            policy=ObservationPolicy(),
            created_at=NOW,
        )
        self.addCleanup(self.store.close)
        self.now = NOW
        # Durations run on their own clock. Tests that are not about a wall
        # step move both together; the ones that are move only one.
        self.monotonic = 0.0
        self.uplink = self.build()

    def build(self, **kwargs: Any) -> Uplink:
        defaults: dict[str, Any] = {
            "clock": lambda: self.now,
            "monotonic": lambda: self.monotonic,
            "connector_id": CONNECTOR,
            "target_id": TARGET,
            "read_wait_seconds": 0.0,
        }
        defaults.update(kwargs)
        return Uplink(self.store, **defaults)

    def refusal(self, frame: Any, *, subject: str | None = None) -> UplinkRefusal:
        with self.assertRaises(UplinkRefusal) as raised:
            self.uplink.ingest(frame, now=self.now, subject=subject)
        return raised.exception


class UploadTests(UplinkCase):
    """What an upload is allowed to be, and what it does when it is."""

    def test_a_heartbeat_is_retained_and_brings_the_link_up(self) -> None:
        self.assertFalse(self.uplink.linked)
        carried = self.uplink.ingest(heartbeat(), now=NOW, subject=CONNECTOR)
        self.assertIsNone(carried, "nothing was being held, so nothing rides back")
        self.assertTrue(self.uplink.linked)
        self.assertEqual(self.uplink.last_upload_at, NOW)
        statuses = self.store.connector_statuses(now=NOW)
        self.assertEqual([row.connector_id for row in statuses], [CONNECTOR])

    def test_an_upload_for_another_connector_is_refused(self) -> None:
        """Two ways to be the wrong connector, and both are the same refusal.

        The credential's subject and the envelope's sender are checked
        separately because they fail separately: a credential issued to one Mac
        may not upload as another, and a frame from a Mac this service was
        never paired with is not attributable at all. Everything the store
        holds is keyed by connector id, so a frame accepted under the wrong one
        is a row nobody can account for.
        """
        by_subject = self.refusal(heartbeat(), subject="someone-else")
        self.assertEqual(by_subject.status, 403)
        self.assertEqual(by_subject.reason, "wrong-connector")

        unpaired = self.refusal(heartbeat(connector="other-mac"))
        self.assertEqual(unpaired.status, 403)
        self.assertEqual(unpaired.reason, "wrong-connector")
        self.assertEqual(unpaired.details["connector_id"], CONNECTOR)

    def test_a_frame_addressed_elsewhere_is_refused(self) -> None:
        elsewhere = observation_envelope(
            payload=heartbeat_payload(observed_at=NOW, bridge_alive=True),
            connector_id=CONNECTOR,
            target_id="someone-elses-pi.example.ts.net",
            sent_at=NOW,
        )
        refused = self.refusal(elsewhere)
        self.assertEqual(refused.status, 400)

    def test_a_downward_kind_may_not_be_uploaded(self) -> None:
        """A request travels down, so a request arriving up is refused.

        Not a pedantic check: the response body of an upload is where the
        service puts requests, and an end that could also *send* them is an end
        that could ask this service to perform a read on its behalf.
        """
        refused = self.refusal(
            {
                "kind": "request",
                "capability": "detail",
                "connector_id": CONNECTOR,
                "target_id": TARGET,
                "sent_at": NOW,
                "payload": {"request": REQUEST_DETAIL, "detail": "state"},
            }
        )
        self.assertEqual(refused.status, 400)

    def test_a_report_for_an_unknown_operation_is_dropped_not_refused(self) -> None:
        """The journal records what this service handed down, and only that.

        Dropped rather than refused because the connector has nothing to fix
        and a retry would not help it: inserting a row from an incoming frame
        would be a command nobody here can account for.
        """
        report = operation_envelope(
            payload={
                "operation_id": "op-never-submitted",
                "status": OPERATION_ACCEPTED,
                "observed_at": NOW,
            },
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW,
        )
        self.assertIsNone(self.uplink.ingest(report, now=NOW, subject=CONNECTOR))
        self.assertIsNone(self.store.operation("op-never-submitted"))

    def test_a_report_settles_the_operation_this_service_submitted(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.store.advance_operation(
            "op-1", OPERATION_DISPATCHED, now=NOW, dispatched_at=NOW
        )
        report = operation_envelope(
            payload={
                "operation_id": "op-1",
                "status": OPERATION_ACCEPTED,
                "observed_at": NOW + 5,
                "settled_at": NOW + 5,
            },
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW + 5,
        )
        self.uplink.ingest(report, now=NOW + 5, subject=CONNECTOR)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_ACCEPTED)
        self.assertEqual(row["settled_at"], NOW + 5)


class CarriageTests(UplinkCase):
    """What rides back down, and what happens when nothing collects it."""

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
        self.assertEqual(
            stale["status"],
            OPERATION_PENDING,
            "a command that never left was never dispatched",
        )

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


class VolatileBodyTests(UplinkCase):
    """Reply prose is memory-only and redeemable only after dispatch."""

    def _dispatch(self, operation_id: str, text: str) -> tuple[str, str]:
        reply_ref, action_token, created = self.uplink.stage_body(
            operation_id, text, now=NOW
        )
        self.assertTrue(created)
        self.store.record_operation(
            operation_record(
                operation_id,
                method=METHOD_QUESTION_REPLY,
                question_id="question-1",
            ),
            connector_id=CONNECTOR,
            received_at=NOW,
        )
        self.uplink.hand_down(
            {
                "request": REQUEST_MUTATE,
                "method": METHOD_QUESTION_REPLY,
                "operation_id": operation_id,
                "target": {
                    "plan_key": PLAN_KEY,
                    "run_key": RUN_KEY,
                    "question_id": "question-1",
                },
                "reply_ref": reply_ref,
                "action_token": action_token,
                "bridge_token": "fresh-view",
            },
            now=NOW,
        )
        self.uplink.ingest(heartbeat(at=NOW + 1), now=NOW + 1)
        return reply_ref, action_token

    def test_a_dispatched_body_is_redeemed_by_the_paired_connector(self) -> None:
        reply_ref, action_token = self._dispatch("reply-1", "Use approach A.")
        self.assertEqual(
            "Use approach A.",
            self.uplink.redeem_body(
                "reply-1",
                reply_ref,
                action_token,
                connector_id=CONNECTOR,
                now=NOW + 2,
            ),
        )
        with self.assertRaises(UplinkRefusal) as raised:
            self.uplink.redeem_body(
                "reply-1",
                reply_ref,
                "wrong-token",
                connector_id=CONNECTOR,
                now=NOW + 2,
            )
        self.assertEqual("invalid-action-token", raised.exception.reason)

    def test_a_settled_command_forgets_its_reply(self) -> None:
        reply_ref, action_token = self._dispatch("reply-2", "Continue.")
        self.uplink.ingest(
            operation_envelope(
                payload={
                    "operation_id": "reply-2",
                    "status": OPERATION_ACCEPTED,
                    "observed_at": NOW + 3,
                    "settled_at": NOW + 3,
                },
                connector_id=CONNECTOR,
                target_id=TARGET,
                sent_at=NOW + 3,
            ),
            now=NOW + 3,
            subject=CONNECTOR,
        )
        with self.assertRaises(UplinkRefusal):
            self.uplink.redeem_body(
                "reply-2",
                reply_ref,
                action_token,
                connector_id=CONNECTOR,
                now=NOW + 4,
            )


class ReadTests(UplinkCase):
    """A browser asks the Pi, the Pi asks the Mac, and one thread waits."""

    def fetch(self, **kwargs: Any) -> Any:
        defaults: dict[str, Any] = {"detail": "state", "plan_key": PLAN_KEY}
        defaults.update(kwargs)
        return self.uplink.fetch(now=self.now, **defaults)

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


class WaitingReadTests(UplinkCase):
    """The one behaviour that cannot be asserted on a frozen clock.

    Everywhere else in this file the clock is an argument, because a test
    should assert about a decision rather than race one. A request thread
    blocking until an upload carries its question out is not a decision, it is
    two threads, so these two tests run on the real clock and on a real wait --
    and the reader is a daemon with a bounded join, so a regression that never
    releases it fails the test instead of hanging the suite.
    """

    def setUp(self) -> None:
        super().setUp()
        self.uplink = self.build(
            clock=time.time, monotonic=time.monotonic, read_wait_seconds=10.0
        )
        self.started = time.time()
        self.uplink.ingest(heartbeat(at=self.started), now=self.started)

    def answered(self) -> list[Any]:
        """Start a reader, hand its question out, and return what it got.

        The upload that carries the question is the *test's* second heartbeat:
        the Pi has nothing to send until the Mac speaks to it, which is the
        whole shape of an outbound-only link.
        """
        answers: list[Any] = []

        def ask() -> None:
            answers.append(
                self.uplink.fetch(detail="state", plan_key=PLAN_KEY, now=time.time())
            )

        reader = threading.Thread(target=ask, name="reader", daemon=True)
        reader.start()
        self.addCleanup(reader.join, 15.0)
        limit = time.time() + 10.0
        while self.uplink.carried == 0 and time.time() < limit:
            time.sleep(0.01)
        self.assertEqual(self.uplink.carried, 1, "the read was queued for the link")
        now = time.time()
        self.carried = self.uplink.ingest(heartbeat(at=now), now=now)
        assert self.carried is not None
        self.assertEqual(self.carried["payload"]["request"], REQUEST_DETAIL)
        self.settle(str(self.carried["request_id"]))
        reader.join(15.0)
        self.assertFalse(reader.is_alive(), "the answer released the waiting thread")
        self.assertEqual(len(answers), 1)
        return answers

    def settle(self, request_id: str) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def test_a_detail_answer_settles_the_waiting_read(self) -> None:
        def settle(request_id: str) -> None:
            now = time.time()
            self.uplink.ingest(
                answer(request_id, at=now, content='{"ok": true}'), now=now
            )

        self.settle = settle  # type: ignore[method-assign]
        answers = self.answered()
        self.assertEqual(answers[0].outcome, "answered")
        self.assertEqual(answers[0].content, '{"ok": true}')
        self.assertEqual(answers[0].request_id, self.carried["request_id"])

    def test_a_receipt_refusing_the_read_settles_it_too(self) -> None:
        """A declined read is an answered request, and it arrives the same way.

        The alternative -- leaving a refused read to time out -- would tell the
        operator the Mac was slow when the Mac had already said no. The outcome
        is `unavailable` rather than `refused`, because on a read a receipt is
        the connector declining to look rather than the plan saying no; that
        naming is the read client's and it is asserted here so this path cannot
        start reporting a refusal as an answer.
        """

        def settle(request_id: str) -> None:
            now = time.time()
            self.uplink.ingest(
                receipt_envelope(
                    payload={
                        "outcome": "refused",
                        "observed_at": now,
                        "detail_code": "no-result",
                    },
                    capability="detail",
                    connector_id=CONNECTOR,
                    target_id=TARGET,
                    sent_at=now,
                    request_id=request_id,
                ),
                now=now,
            )

        self.settle = settle  # type: ignore[method-assign]
        answers = self.answered()
        self.assertEqual(answers[0].outcome, "unavailable")
        self.assertEqual(answers[0].detail_code, "no-result")
        self.assertEqual(answers[0].note, "the connector refused the read")


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
