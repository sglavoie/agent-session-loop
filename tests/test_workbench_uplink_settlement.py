"""Read settlement must publish its answer before a waiter can abandon it."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any
from unittest import mock

from asl_workbench.remote_reads import ReadAnswer
from tests.test_workbench_uplink import NOW, PLAN_KEY, UplinkCase, answer, heartbeat

ASL_TEST_PORTABLE = True


class _ObservedCondition(threading.Condition):
    """Let the test distinguish a blocked reader from an unscheduled reader."""

    def __init__(self, reader_stopped: threading.Event) -> None:
        super().__init__()
        self.reader_stopped = reader_stopped

    def __enter__(self) -> bool:
        if self.acquire(False):
            return True
        if threading.current_thread().name == "abandon-read":
            self.reader_stopped.set()
        return self.acquire()


class ReadSettlementTests(UplinkCase):
    def test_simultaneous_uploads_do_not_reconnect_over_a_new_read(self) -> None:
        linking = threading.Event()
        release = threading.Event()
        second_stopped = threading.Event()
        self.uplink._ready = _ObservedCondition(second_stopped)
        link_up = self.uplink.reads.link_up
        errors: list[BaseException] = []

        def hold_first_link(capabilities: Iterable[str], *, now: float) -> None:
            if threading.current_thread().name == "first-upload":
                linking.set()
                if not release.wait(5.0):
                    raise AssertionError("test did not release the first upload")
            link_up(capabilities, now=now)

        def upload(*, queue_read: bool) -> None:
            try:
                self.uplink.ingest(heartbeat(), now=NOW)
                if queue_read:
                    self.uplink.reads.read(
                        request_id="new", detail="state", plan_key=PLAN_KEY, now=NOW
                    )
            except BaseException as error:
                errors.append(error)
            finally:
                if queue_read:
                    second_stopped.set()

        first = threading.Thread(
            target=lambda: upload(queue_read=False), name="first-upload", daemon=True
        )
        second = threading.Thread(
            target=lambda: upload(queue_read=True), name="abandon-read", daemon=True
        )
        with mock.patch.object(
            self.uplink.reads, "link_up", side_effect=hold_first_link
        ):
            first.start()
            try:
                self.assertTrue(linking.wait(5.0))
                second.start()
                self.assertTrue(second_stopped.wait(5.0))
            finally:
                release.set()
                first.join(5.0)
                if second.ident is not None:
                    second.join(5.0)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.uplink.reads.pending(), ("new",))
        self.assertEqual(self.uplink.reads.generation, 1)

    def assert_settlement_survives_abandon(
        self, settle: Callable[[], Any], *, outcome: str
    ) -> None:
        removed = threading.Event()
        release = threading.Event()
        reader_stopped = threading.Event()
        self.uplink._ready = _ObservedCondition(reader_stopped)
        self.uplink.ingest(heartbeat(), now=NOW)
        self.uplink.reads.read(
            request_id="reading", detail="state", plan_key=PLAN_KEY, now=NOW
        )
        keep = self.uplink._keep
        results: list[ReadAnswer] = []
        errors: list[BaseException] = []

        def hold_publication(answers: Iterable[ReadAnswer]) -> None:
            answers = tuple(answers)
            if any(item.request_id == "reading" for item in answers):
                removed.set()
                if not release.wait(5.0):
                    raise AssertionError("test did not release the settled answer")
            keep(answers)

        def publish() -> None:
            try:
                settle()
            except BaseException as error:
                errors.append(error)

        def abandon() -> None:
            try:
                results.append(self.uplink._abandon("reading"))
            except BaseException as error:
                errors.append(error)
            finally:
                reader_stopped.set()

        producer = threading.Thread(target=publish, daemon=True)
        reader = threading.Thread(target=abandon, name="abandon-read", daemon=True)
        with mock.patch.object(self.uplink, "_keep", side_effect=hold_publication):
            producer.start()
            try:
                self.assertTrue(removed.wait(5.0), "settlement did not remove the read")
                reader.start()
                # Either abandonment finishes (the old bug), or it blocks on
                # publication. No sleep or scheduling assumption picks a winner.
                self.assertTrue(reader_stopped.wait(5.0))
            finally:
                release.set()
                producer.join(5.0)
                if reader.ident is not None:
                    reader.join(5.0)
        self.assertFalse(producer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([item.outcome for item in results], [outcome])
        self.assertEqual(self.uplink.reads.pending(), ())
        self.assertEqual(self.uplink._settled, {}, "no uncollected answer is leaked")

    def test_an_upload_publishes_before_abandonment_can_observe_settlement(
        self,
    ) -> None:
        self.assert_settlement_survives_abandon(
            lambda: self.uplink.ingest(answer("reading"), now=NOW + 1),
            outcome="answered",
        )

    def test_expiry_publishes_before_abandonment_can_observe_settlement(self) -> None:
        self.assert_settlement_survives_abandon(
            lambda: self.uplink.sweep(NOW + 120), outcome="timeout"
        )

    def test_disconnect_publishes_before_abandonment_can_observe_settlement(
        self,
    ) -> None:
        def disconnect() -> None:
            self.monotonic = self.uplink.silence_seconds + 1
            self.uplink.sweep(NOW + 1)

        self.assert_settlement_survives_abandon(disconnect, outcome="offline")

    def test_another_abandonment_publishes_the_other_reads_it_expires(self) -> None:
        def abandon_other() -> None:
            self.uplink.reads.read(
                request_id="other", detail="state", plan_key=PLAN_KEY, now=NOW
            )
            self.now = NOW + 120
            self.uplink._abandon("other")

        self.assert_settlement_survives_abandon(abandon_other, outcome="timeout")
