"""Doubles, payload fixtures and the performer base for the remote-action tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import (
    control_queue,
    intervention,
    plans_files,
    state,
    workbench_launch,
    workbench_operations,
    workbench_registry_models,
    workbench_remote_actions,
)
from asl_workbench import catalog, remote_actions
from asl_workbench import connector_protocols as portable_protocols
from asl_workbench import connector_runtime as portable_runtime
from asl_workbench import connector_support as portable_support
from asl_workbench.operations import (
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
)
from asl_workbench.transport import (
    KIND_OPERATION,
    KIND_RECEIPT,
    encode_frame,
    request_envelope,
)
from tests.support import (
    _write_live_lock_file,
    _write_state_payload,
    write_plan,
)
from tests.support_workbench_remote_reads import inline, iso

#: A fixed moment. Every window in this module is relative to it, so nothing
#: here depends on how long the suite takes to run.

NOW = 1789574400.0

CONNECTOR_ID = "mac-studio"
TARGET_ID = "pi-1"

#: Digest-shaped keys: the transport refuses anything else, so a fixture using
#: ``"plan-1"`` would fail validation rather than the assertion it was written
#: for.
PLAN_KEY = "0123456789abcdef"
RUN_KEY = "abcdefabcdef0123"

RUN_ID = "run-alpha"
SESSION_ID = "1"
BRIDGE_TOKEN = "freshness-token-1"
REPLY_REF = "reply-ref-1"
ACTION_TOKEN = "redeem-token-1"
REPLY_TEXT = "carry on, but check the lock first"


# -- Doubles ----------------------------------------------------------------


class LinkChannel:
    """A channel that can hand a submission back on an acknowledgement.

    The whole downward direction is this return value: there is no inbound
    socket, so a command reaches this process only by riding the response to a
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
        """Queue one submission for the next acknowledgement to carry."""
        self.inbound.append(encode_frame(envelope))

    def operations(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame["kind"] == KIND_OPERATION]

    def receipts(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame["kind"] == KIND_RECEIPT]


class StubSource:
    """An observation source with an empty cycle, and its commits counted."""

    def __init__(self) -> None:
        self.committed: list[portable_protocols.ObservationCycle] = []

    def observe(self, now: float) -> portable_protocols.ObservationCycle:
        return portable_protocols.ObservationCycle(observed_at=now)

    def commit(self, cycle: portable_protocols.ObservationCycle) -> None:
        self.committed.append(cycle)


class StubResponder:
    """A detail responder, present only so the read lane exists beside the other."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def answer(self, request: Any, *, now: float) -> dict[str, Any]:
        self.requests.append(dict(request))
        return {
            "detail": "logs",
            "target": {"plan_key": PLAN_KEY},
            "observed_at": now,
            "complete": "complete",
            "content": "the log text",
            "content_bytes": len("the log text"),
            "encoding": "utf-8",
        }


class StubPerformer:
    """A performer whose answer is handed to it, and which records its asks.

    Records the body it was given as well as the request, because "the prose
    reached the action and nothing else" is the claim several tests here make.
    """

    def __init__(
        self,
        result: remote_actions.MutationResult | None = None,
        error: Exception | None = None,
        settlements: list[dict[str, Any]] | None = None,
    ) -> None:
        self.result = result or remote_actions.MutationResult(
            status=OPERATION_ACCEPTED, run_id=RUN_ID
        )
        self.error = error
        self.settlements = settlements or []
        self.calls: list[tuple[remote_actions.MutationRequest, str | None]] = []
        self.recovered = 0

    def perform(
        self, request: remote_actions.MutationRequest, *, body: str | None, now: float
    ) -> remote_actions.MutationResult:
        self.calls.append((request, body))
        if self.error is not None:
            raise self.error
        return self.result

    def recover(self, *, now: float) -> list[dict[str, Any]]:
        self.recovered += 1
        return list(self.settlements)


class StubBodySource:
    """A redemption that counts its fetches and can refuse."""

    def __init__(self, text: str = REPLY_TEXT, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.fetches: list[str] = []

    def fetch(self, request: remote_actions.MutationRequest, *, now: float) -> str:
        self.fetches.append(request.operation_id)
        if self.error is not None:
            raise self.error
        return self.text


class SettlingPerformer(StubPerformer):
    """An accepted command whose later evidence reaches consumed."""

    def __init__(self) -> None:
        super().__init__()
        self.reconciliations: list[str] = []

    def reconcile(
        self, request: remote_actions.MutationRequest, *, now: float
    ) -> remote_actions.MutationResult:
        self.reconciliations.append(request.operation_id)
        return remote_actions.MutationResult(status=OPERATION_CONSUMED)


# -- Payload fixtures -------------------------------------------------------


def submit_body(**overrides: Any) -> dict[str, Any]:
    """One valid ``operation.submit`` payload, defaulting to a run control."""
    payload: dict[str, Any] = {
        "request": "operation.submit",
        "operation_id": "op-1",
        "method": METHOD_STOP_AFTER_CURRENT,
        "target": {"plan_key": PLAN_KEY, "run_key": RUN_KEY, "run_id": RUN_ID},
        "bridge_token": BRIDGE_TOKEN,
    }
    payload.update(overrides)
    return payload


def reply_body(**overrides: Any) -> dict[str, Any]:
    """A submission whose method needs prose fetched out of band."""
    return submit_body(
        method=METHOD_QUESTION_REPLY,
        reply_ref=REPLY_REF,
        action_token=ACTION_TOKEN,
        **overrides,
    )


def submission(
    request_id: str = "m1", deadline: float = NOW + 30.0, **overrides: Any
) -> dict[str, Any]:
    """One downward request carrying a command for this connector."""
    return request_envelope(
        payload=submit_body(**overrides),
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
        deadline=deadline,
    )


def detail_request(request_id: str = "r1") -> dict[str, Any]:
    """A downward ``detail.read``: the lane the mutation lane runs beside."""
    return request_envelope(
        payload={
            "request": "detail.read",
            "detail": "logs",
            "stream": "stdout",
            "target": {"plan_key": PLAN_KEY},
        },
        connector_id=CONNECTOR_ID,
        target_id=TARGET_ID,
        sent_at=NOW,
        request_id=request_id,
        deadline=NOW + 30.0,
    )


def make_request(**overrides: Any) -> remote_actions.MutationRequest:
    """One parsed command, without going through an envelope to get it."""
    fields: dict[str, Any] = {
        "operation_id": "op-1",
        "method": METHOD_STOP_AFTER_CURRENT,
        "plan_key": PLAN_KEY,
        "requested_at": NOW,
        "deliver_by": NOW + 30.0,
        "run_id": RUN_ID,
        "bridge_token": BRIDGE_TOKEN,
    }
    fields.update(overrides)
    return remote_actions.MutationRequest(**fields)


def make_connector(**overrides: Any) -> portable_runtime.ObservationConnector:
    """A connector wired for the mutation lane, with inline workers."""
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
        "performer": StubPerformer(),
        "body_source": StubBodySource(),
        "run_observation": inline,
        "run_answer": inline,
        "run_mutation": inline,
    }
    fields.update(overrides)
    return portable_runtime.ObservationConnector(**fields)


def drive(connector: Any, steps: int, *, start: float = NOW) -> list[str]:
    """Step the connector ``steps`` times just after ``start``."""
    lanes: list[str] = []
    for index in range(steps):
        lane = connector.step(start + 0.01 * (index + 1))
        if lane is not None:
            lanes.append(lane)
    return lanes


# --------------------------------------------------------------------------
# Reading a submission
# --------------------------------------------------------------------------


class PerformerTestCase(unittest.TestCase):
    """A plan on disk, acted on the way the wire acts on it."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.registry_path = self.root / "workbench.json"
        self.plan_dir = self.make_plan("plan-alpha")
        self.plan_key = catalog.plan_key(str(self.plan_dir))
        self.performer = workbench_remote_actions.AslMutationPerformer(
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
            registry_path=self.registry_path,
        )

    def make_plan(self, name: str, statuses: tuple[str, ...] = ("pending",)) -> Path:
        plan_dir = self.plans_root / name
        write_plan(plan_dir, statuses)
        return plans_files.canonicalize_path(plan_dir)

    def write_state(self, target: Path | None = None, **overrides: Any) -> None:
        target = target or self.plan_dir
        payload: dict[str, Any] = {
            "version": 3,
            "plan_dir": str(target),
            "harness": "claude",
            "paused": False,
            "run_id": RUN_ID,
            "run_started_at": iso(-3600),
            "last_activity_at": iso(-60),
            "active_turn": {
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "session_kind": "implementation",
                "harness": "claude",
            },
        }
        payload.update(overrides)
        _write_state_payload(target, self.state_dir, payload)

    def live_run(self, **overrides: Any) -> None:
        """A run that is live and holds a turn, so strict controls authorize."""
        self.write_state(**overrides)
        _write_live_lock_file(self.plan_dir, self.state_dir)

    def make_paused(self) -> None:
        """A plan stopped on a question, which is what a reply is about."""
        runs = state.plan_state_dir(self.plan_dir, self.state_dir) / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        last_message = runs / "last-message.txt"
        last_message.write_text("which branch should this land on?", encoding="utf-8")
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.plan_dir),
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "run_id": RUN_ID,
                "harness": "claude",
                "turns": [
                    {
                        "session_id": SESSION_ID,
                        "turn_index": 1,
                        "last_message_path": str(last_message),
                        "harness": "claude",
                    }
                ],
            },
        )

    def launch_result(self, *, reply: bool = False) -> workbench_launch.LaunchResult:
        """What a spawn would have returned, without a spawn happening."""
        return workbench_launch.LaunchResult(
            pid=4242,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.plan_dir,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=len(REPLY_TEXT) if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def token(self) -> str:
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        return intervention.action_token(facts)

    def request(self, **overrides: Any) -> remote_actions.MutationRequest:
        fields: dict[str, Any] = {
            "operation_id": "op-1",
            "method": METHOD_STOP_AFTER_CURRENT,
            "plan_key": self.plan_key,
            "requested_at": NOW,
            "deliver_by": NOW + 30.0,
            "run_id": RUN_ID,
        }
        fields.update(overrides)
        return remote_actions.MutationRequest(**fields)

    def perform(
        self, body: str | None = None, **overrides: Any
    ) -> remote_actions.MutationResult:
        return self.performer.perform(self.request(**overrides), body=body, now=NOW)

    def rows(self, plan_dir: Path | None = None) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            plan_dir or self.plan_dir, state_dir=self.state_dir
        )

    def row(self, operation_id: str = "op-1") -> dict[str, Any]:
        for record in reversed(self.rows()):
            if record.get("operation_id") == operation_id:
                return record
        raise AssertionError(f"no journal row for {operation_id}")

    def queued(self) -> list[str]:
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        return [entry.command for entry in drained.accepted]
