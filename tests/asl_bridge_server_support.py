"""Shared fixtures for the bridge server test modules.

Split from tests/test_asl_bridge_server.py."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from agent_session_loop import (
    agent_mux_projection,
    bridge_projection,
    bridge_protocol,
    bridge_session,
    events,
    mailbox_appenders,
    mailbox_cursors,
    state,
)

from .support import (
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
)

QUESTION_BODY = "Should we drop the v1 column before the backfill lands?"
SECOND_QUESTION_BODY = "Which staging database should the rehearsal target?"
REPLY_TEXT = "Yes, drop it after the backfill verifies clean."
RUN_ID = "20260619T150000Z-1"

# Generous enough that a loaded machine cannot fail a handshake between the
# test and a worker thread, short enough that a real deadlock still fails.
WAIT = 5.0


class _Corpus:
    """A plans root plus its ASL state root, wired the way discovery expects."""

    def __init__(self, tmp_path: Path) -> None:
        self.plans_root = tmp_path / "plans"
        self.plans_root.mkdir()
        self.state_root = tmp_path / "state"

    def snapshot(self) -> dict[str, Any]:
        return bridge_projection.build_runs_snapshot(
            state_dir=self.state_root,
            plan_roots=(self.plans_root,),
            include_default_dirs=False,
        )

    def session(self, **kwargs: Any) -> bridge_session.BridgeSession:
        return bridge_session.BridgeSession(
            snapshot_builder=self.snapshot, state_dir=self.state_root, **kwargs
        )

    def write_state(self, plan_dir: Path, payload: dict[str, Any]) -> Path:
        return _write_state_payload(
            plan_dir, self.state_root, {"plan_dir": str(plan_dir), **payload}
        )

    def waiting_plan(self, name: str = "plan-waiting") -> tuple[Path, str]:
        """A live run holding an open mailbox question, the reply-ready shape."""
        plan_dir = _make_workbench_plan(self.plans_root, name)
        _write_lock_file(
            plan_dir,
            self.state_root,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        question = mailbox_appenders.append_question(
            plan_dir,
            QUESTION_BODY,
            state_dir=self.state_root,
            run_id=RUN_ID,
            session_id="2",
            turn_index=3,
        )
        self.write_state(
            plan_dir,
            {
                "ok": None,
                "paused": False,
                "run_id": RUN_ID,
                "harness": "codex",
                "run_started_at": "2026-06-19T15:00:00+00:00",
                "active_turn": {
                    "session_id": "2",
                    "turn_index": 3,
                    "started_at": "2026-06-19T15:00:00+00:00",
                    "last_activity_at": "2026-06-19T15:04:00+00:00",
                },
                "waiting_for_reply": {
                    "question_id": question.id,
                    "hard_deadline": "2026-06-19T17:00:00+00:00",
                },
            },
        )
        return plan_dir, question.id

    def two_question_plan(
        self, name: str = "plan-two-questions"
    ) -> tuple[Path, str, str]:
        """A live run holding two open questions, the first one primary.

        The runner state names only the first question, so the second is
        reachable solely as an explicitly selected id.
        """
        plan_dir, primary_id = self.waiting_plan(name)
        second = mailbox_appenders.append_question(
            plan_dir,
            SECOND_QUESTION_BODY,
            state_dir=self.state_root,
            run_id=RUN_ID,
            session_id="2",
            turn_index=3,
        )
        return plan_dir, primary_id, second.id

    def running_plan(self, name: str = "plan-running") -> Path:
        """A live run with no open question: control commands only."""
        plan_dir = _make_workbench_plan(self.plans_root, name)
        _write_lock_file(
            plan_dir,
            self.state_root,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        self.write_state(
            plan_dir,
            {
                "ok": None,
                "paused": False,
                "run_id": RUN_ID,
                "harness": "codex",
                "run_started_at": "2026-06-19T15:00:00+00:00",
                "active_turn": {
                    "session_id": "2",
                    "turn_index": 3,
                    "started_at": "2026-06-19T15:00:00+00:00",
                    "last_activity_at": "2026-06-19T15:04:00+00:00",
                },
            },
        )
        return plan_dir

    def input_wait_plan(self, name: str = "plan-input-wait") -> tuple[Path, str]:
        """A harness turn blocked in ``asl ask --wait``."""
        plan_dir = self.running_plan(name)
        question = mailbox_appenders.append_question(
            plan_dir,
            QUESTION_BODY,
            state_dir=self.state_root,
            run_id=RUN_ID,
            session_id="2",
            turn_index=3,
        )
        path = state.plan_state_dir(plan_dir, self.state_root) / events.STATE_FILE_NAME
        payload = json.loads(path.read_text())
        payload["active_turn"]["input_wait_active"] = True
        self.write_state(plan_dir, payload)
        return plan_dir, question.id

    def paused_plan(self, name: str = "plan-paused") -> Path:
        """A paused run whose question is the harness's own last message."""
        plan_dir = _make_workbench_plan(self.plans_root, name)
        artifact_dir = state.plan_state_dir(plan_dir, self.state_root) / "runs"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        last_message = artifact_dir / "last-message.txt"
        last_message.write_text(QUESTION_BODY, encoding="utf-8")
        self.write_state(
            plan_dir,
            {
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "pause_reason": "agent-question",
                "pause_message": "Awaiting confirmation on the v1 column",
                "run_id": "20260619T140000Z-1",
                "harness": "claude",
                "run_started_at": "2026-06-19T14:00:00+00:00",
                "turns": [
                    {
                        "session_id": "1",
                        "turn_index": 1,
                        "last_message_path": str(last_message),
                        "harness": "claude",
                        "started_at": "2026-06-19T14:00:00+00:00",
                    }
                ],
            },
        )
        return plan_dir

    def row_for(self, plan_dir: Path) -> dict[str, Any]:
        for row in self.snapshot()["rows"]:
            if row["plan_dir"] == str(plan_dir):
                return row
        raise AssertionError(f"no snapshot row for {plan_dir}")

    def mailbox_lines(self, plan_dir: Path) -> list[dict[str, Any]]:
        path = mailbox_cursors.mailbox_path_for_plan(plan_dir, self.state_root)
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def record_command_observed(self, plan_dir: Path, command_id: str) -> None:
        """Write the runner's own delivery receipt for one queued command."""
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        record = {
            "name": events.RUN_CONTROL_EVENT,
            "payload": {
                "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                "command_id": command_id,
            },
        }
        with (plan_state / events.EVENTS_FILE_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def record_reply_provided(
        self,
        plan_dir: Path,
        *,
        run_id: str,
        origin: str = "stdin",
        question_id: str | None = None,
    ) -> None:
        """Write the record a run appends when it reads an operator's reply.

        Shaped the way ``run_loop`` writes it: the run names itself and the way
        the text reached it, and the text itself is nowhere in the record.
        """
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        source: dict[str, Any] = {
            "run_id": run_id,
            "session_id": "1",
            "turn_index": 2,
            "reply_length": len(REPLY_TEXT),
            "origin": origin,
        }
        if question_id is not None:
            source["question_id"] = question_id
        record = {
            "created_at": "2026-06-19T15:30:00+00:00",
            "name": events.REPLY_PROVIDED_EVENT,
            "payload": {
                "run_id": run_id,
                "turn_index": 2,
                "session_id": "1",
                "reply_length": len(REPLY_TEXT),
                "source": source,
            },
            "version": 1,
        }
        with (plan_state / events.EVENTS_FILE_NAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def _request(request_id: str, method: str, **params: Any) -> bytes:
    payload: dict[str, Any] = {
        "type": "request",
        "protocol": bridge_protocol.PROTOCOL_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(payload).encode("utf-8")


def _handshake_request(digest: str, request_id: str = "hs-1") -> bytes:
    """The frame a protocol-2 client must send before any mutation is accepted."""
    return _request(
        request_id,
        bridge_protocol.METHOD_HANDSHAKE,
        contract_set_id=agent_mux_projection.CONTRACT_SET_ID,
        snapshot_digest=digest,
    )


class _LineWriter:
    """A protocol sink a scripted client can wait on one response at a time.

    ``serve_stdio`` blocks its caller, so a client that must react to what the
    server said — as a protocol-2 client must, because the handshake names a
    digest — needs the frames as they land rather than after the loop returns.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self.lines: list[str] = []
        self._partial = ""

    def write(self, text: str) -> int:
        with self._arrived:
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                self.lines.append(line)
            self._arrived.notify_all()
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        with self._lock:
            return "".join(f"{line}\n" for line in self.lines) + self._partial

    def wait_for(self, needle: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self._arrived:
            while True:
                for line in self.lines:
                    if needle in line:
                        return line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no frame matched {needle!r}: {self.lines}")
                self._arrived.wait(remaining)


def _handshake(session: bridge_session.BridgeSession) -> dict[str, Any]:
    """Open the mutation gate on a session under test, and prove it opened."""
    digest = session.runs_snapshot()["digest"]
    response = session.handle_frame(_handshake_request(digest))
    assert response is not None and response["ok"], response
    assert response["result"]["mutations_enabled"], response
    return response["result"]


class _RecordingServer:
    """A stand-in for the Workbench HTTP server that never binds a socket."""

    instances: list[_RecordingServer] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.token = "test-token"
        self.bound_host = "127.0.0.1"
        self.bound_port = 54321
        self.served = 0
        self.poll_intervals: list[float] = []
        self.shutdowns = 0
        self.closes = 0
        _RecordingServer.instances.append(self)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.served += 1
        self.poll_intervals.append(poll_interval)

    def shutdown(self) -> None:
        self.shutdowns += 1

    def server_close(self) -> None:
        self.closes += 1
