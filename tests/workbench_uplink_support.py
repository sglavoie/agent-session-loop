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
from unittest import mock

from asl_workbench.connector_payloads import heartbeat_payload
from asl_workbench.observation import ObservationPolicy
from asl_workbench.operations import (
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_DEADLINE_PASSED,
    DETAIL_NO_RESULT,
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
)
from asl_workbench.store_workbench import WorkbenchStore
from asl_workbench.transport import (
    REQUEST_DETAIL,
    REQUEST_MUTATE,
    detail_envelope,
    observation_envelope,
    operation_envelope,
    receipt_envelope,
    validate_envelope,
)
from asl_workbench.uplink import Uplink, UplinkRefusal

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


class UploadTestsBase(UplinkCase):
    """What an upload is allowed to be, and what it does when it is."""


class CarriageTestsBase(UplinkCase):
    """What rides back down, and what happens when nothing collects it."""


class VolatileBodyTestsBase(UplinkCase):
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


class ReadTestsBase(UplinkCase):
    """A browser asks the Pi, the Pi asks the Mac, and one thread waits."""

    def fetch(self, **kwargs: Any) -> Any:
        defaults: dict[str, Any] = {"detail": "state", "plan_key": PLAN_KEY}
        defaults.update(kwargs)
        return self.uplink.fetch(now=self.now, **defaults)


class WaitingReadTestsBase(UplinkCase):
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


__all__ = [
    "Any",
    "CONNECTOR",
    "CarriageTestsBase",
    "DETAIL_CONNECTOR_RESTARTED",
    "DETAIL_DEADLINE_PASSED",
    "DETAIL_NO_RESULT",
    "METHOD_QUESTION_REPLY",
    "METHOD_STOP_AFTER_CURRENT",
    "NOW",
    "OPERATION_ACCEPTED",
    "OPERATION_CONSUMED",
    "OPERATION_DISPATCHED",
    "OPERATION_EXPIRED",
    "OPERATION_PENDING",
    "OPERATION_REJECTED",
    "OPERATION_UNCONFIRMED",
    "ObservationPolicy",
    "PLAN_KEY",
    "Path",
    "REQUEST_DETAIL",
    "REQUEST_MUTATE",
    "RUN_KEY",
    "ReadTestsBase",
    "TARGET",
    "TemporaryDirectory",
    "Uplink",
    "UplinkCase",
    "UplinkRefusal",
    "UploadTestsBase",
    "VolatileBodyTestsBase",
    "WaitingReadTestsBase",
    "WorkbenchStore",
    "answer",
    "detail_envelope",
    "heartbeat",
    "heartbeat_payload",
    "mock",
    "observation_envelope",
    "operation_envelope",
    "operation_record",
    "receipt_envelope",
    "submission",
    "threading",
    "time",
    "unittest",
    "validate_envelope",
]
