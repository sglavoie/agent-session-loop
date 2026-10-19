"""Shared fixtures for test_workbench_remote_reads."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import events, plans_files, state, workbench_remote_reads
from asl_workbench import catalog, remote_reads
from asl_workbench import connector_protocols as portable_protocols
from asl_workbench import connector_runtime as portable_runtime
from asl_workbench import connector_support as portable_support
from asl_workbench.transport import (
    CAPABILITY_CATALOG,
    CAPABILITY_DETAIL,
    CAPABILITY_OBSERVE,
    KIND_DETAIL,
    KIND_RECEIPT,
    encode_frame,
    request_envelope,
)
from tests.support import _write_state_payload, write_plan

NOW = 1789574400.0


CONNECTOR_ID = "mac-studio"


TARGET_ID = "pi-1"


PLAN_KEY = "0123456789abcdef"


OTHER_KEY = "fedcba9876543210"


RUN_KEY = "abcdefabcdef0123"


def iso(offset: float) -> str:
    """An ASL timestamp ``offset`` seconds from :data:`NOW`."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(NOW + offset, tz=UTC).isoformat()


class LinkChannel:
    """A channel that can hand a request back on an acknowledgement.

    The whole downward direction is this return value: there is no inbound
    socket, so a question reaches this process only by riding the response to a
    frame the connector was sending anyway.
    """

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.inbound: list[bytes] = []
        self.failing = False

    def send(self, frame: bytes) -> bytes | None:
        if self.failing:
            raise portable_support.ChannelError("the link is down")
        self.frames.append(json.loads(frame.decode("utf-8")))
        return self.inbound.pop(0) if self.inbound else None

    def carry(self, envelope: Any) -> None:
        """Queue one request for the next acknowledgement to carry."""
        self.inbound.append(encode_frame(envelope))

    def kinds(self) -> list[str]:
        return [frame["kind"] for frame in self.frames]

    def lanes(self) -> list[str]:
        """What each frame was, with observations named by their view."""
        return [
            frame["payload"]["view"]
            if frame["kind"] == "observation"
            else frame["kind"]
            for frame in self.frames
        ]

    def answers(self) -> list[dict[str, Any]]:
        return [
            frame
            for frame in self.frames
            if frame["kind"] in (KIND_DETAIL, KIND_RECEIPT)
        ]


class StubSource:
    """An observation source with a fixed cycle, and its commits counted."""

    def __init__(
        self, cycle: portable_protocols.ObservationCycle | None = None
    ) -> None:
        self.cycle = cycle
        self.committed: list[portable_protocols.ObservationCycle] = []

    def observe(self, now: float) -> portable_protocols.ObservationCycle:
        if self.cycle is None:
            return portable_protocols.ObservationCycle(observed_at=now)
        return self.cycle

    def commit(self, cycle: portable_protocols.ObservationCycle) -> None:
        self.committed.append(cycle)


class StubResponder:
    """A responder whose answer is handed to it, and which records its asks."""

    def __init__(
        self, payload: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.payload = payload
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def answer(self, request: Any, *, now: float) -> dict[str, Any]:
        self.requests.append(dict(request))
        if self.error is not None:
            raise self.error
        body = dict(self.payload) if self.payload is not None else detail_body()
        body["observed_at"] = now
        return body


def inline(attempt: Any) -> None:
    """Run one worker pass on the calling thread."""
    attempt()
    return None


class HeldWorker:
    """A worker that keeps passes pending until released."""

    def __init__(self) -> None:
        self.pending: list[Any] = []

    def __call__(self, attempt: Any) -> None:
        self.pending.append(attempt)
        return None

    def release(self) -> int:
        released = len(self.pending)
        for attempt in self.pending:
            attempt()
        self.pending.clear()
        return released


def detail_body(**overrides: Any) -> dict[str, Any]:
    """One valid :data:`DETAIL_SHAPE` payload carrying a little material."""
    payload: dict[str, Any] = {
        "detail": "logs",
        "target": {"plan_key": PLAN_KEY},
        "observed_at": NOW,
        "complete": "complete",
        "content": "the log text",
        "content_bytes": len("the log text"),
        "encoding": "utf-8",
    }
    payload.update(overrides)
    return payload


def rows_body(rows: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "detail": "events",
        "target": {"plan_key": PLAN_KEY},
        "observed_at": NOW,
        "complete": "complete",
        "entries": rows,
        "entry_count": len(rows),
    }
    payload.update(overrides)
    return payload


def detail_frame(request_id: str, **overrides: Any) -> dict[str, Any]:
    """One upward detail envelope answering ``request_id``."""
    from asl_workbench.transport import detail_envelope

    return detail_envelope(
        payload=detail_body(**overrides),
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
    )


def receipt_frame(
    request_id: str, outcome: str, detail_code: str | None = None
) -> dict[str, Any]:
    """One upward receipt declining ``request_id``."""
    from asl_workbench.transport import receipt_envelope

    payload: dict[str, Any] = {"outcome": outcome, "observed_at": NOW}
    if detail_code is not None:
        payload["detail_code"] = detail_code
    return receipt_envelope(
        payload=payload,
        capability=CAPABILITY_DETAIL,
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
    )


def mutation_request(request_id: str = "op-req") -> dict[str, Any]:
    """A downward ``operation.submit``: the traffic this link never accepts."""
    return request_envelope(
        payload={
            "request": "operation.submit",
            "operation_id": "op-1",
            "method": "plan.abort",
            "target": {"plan_key": PLAN_KEY},
        },
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
        deadline=NOW + 30.0,
    )


def make_client(**overrides: Any) -> remote_reads.DetailReadClient:
    """A client already paired with a connector that offers detail reads."""
    fields: dict[str, Any] = {
        "connector_id": CONNECTOR_ID,
        "target_id": TARGET_ID,
    }
    fields.update(overrides)
    client = remote_reads.DetailReadClient(**fields)
    client.link_up((CAPABILITY_OBSERVE, CAPABILITY_CATALOG, CAPABILITY_DETAIL), now=NOW)
    return client


def make_connector(**overrides: Any) -> portable_runtime.ObservationConnector:
    """A connector wired for the correlated lane, with inline workers."""
    fields: dict[str, Any] = {
        "connector_id": CONNECTOR_ID,
        "target_id": TARGET_ID,
        "source": StubSource(),
        "channel": LinkChannel(),
        # Long enough that the heartbeat is not the lane under test, except in
        # the tests that ask for it by name.
        "heartbeat_interval": 60.0,
        "observation_interval": 3600.0,
        "responder": StubResponder(),
        "run_observation": inline,
        "run_answer": inline,
    }
    fields.update(overrides)
    return portable_runtime.ObservationConnector(**fields)


class ResponderTestCase(unittest.TestCase):
    """A temporary ASL corpus, read the way the wire reads it."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state_root = self.root / "state"
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.plan = self.make_plan(
            "paused-plan",
            ("complete", "pending"),
            {
                "run_id": "run-paused",
                "run_started_at": iso(-7200),
                "last_activity_at": iso(-1800),
                "harness": "claude",
                "current_session": "2",
                "sessions": {"1": {"status": "complete"}, "2": {"status": "pending"}},
                "paused": True,
                "pause_reason": "blocked",
            },
        )
        self.plan_key = catalog.plan_key(str(self.plan))
        self.responder = workbench_remote_reads.AslDetailResponder(
            state_dir=self.state_root,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )

    def make_plan(
        self, name: str, statuses: tuple[str, ...], payload: dict[str, Any]
    ) -> Path:
        plan_dir = self.plans_root / name
        write_plan(plan_dir, statuses)
        (plan_dir / "manifest.md").write_text("# A plan\n", encoding="utf-8")
        canonical = plans_files.canonicalize_path(plan_dir)
        _write_state_payload(
            canonical, self.state_root, dict(payload, plan_dir=str(canonical))
        )
        return canonical

    def write_events(
        self, plan_dir: Path, count: int, *, payload_size: int = 0
    ) -> None:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        path = plan_state / events.EVENTS_FILE_NAME
        with path.open("a", encoding="utf-8") as handle:
            for index in range(count):
                record: dict[str, Any] = {
                    "created_at": iso(-3600 + index),
                    "name": events.TURN_FINISHED_EVENT,
                    "version": 2,
                    "payload": {"run_id": "run-paused", "session_id": "1"},
                }
                if payload_size:
                    record["payload"]["note"] = "x" * payload_size
                handle.write(json.dumps(record) + "\n")

    def ask(self, detail: str, **overrides: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "request": "detail.read",
            "detail": detail,
            "target": {"archive": "list"}
            if detail == "archive"
            else {"plan_key": self.plan_key},
        }
        if detail == "logs":
            # A log read selects a stream or an artifact by name; asking for
            # "the logs" is an incomplete request rather than an empty one.
            request["stream"] = "stdout"
        request.update(overrides)
        return self.responder.answer(request, now=NOW)
