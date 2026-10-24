# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401
"""The copilot ACP transport: the protocol client and the adapter seam.

Two levels, because the two failure families live in different places. The
session tests drive :class:`copilot_acp.CopilotAcpSession` against an in-process
fake whose every answer is scripted, so capability negotiation, framing, and the
exact answer to a permission request are asserted without a race. The adapter
tests spawn ``tests/fixtures/fake_copilot_acp.py`` for real, so process
ownership, the access restrictions crossing the wire, and the truthfulness of
the result are asserted against an actual pipe.

The invariant that gets the most attention here is the one the transport was
introduced against: moving a worker turn off ``-p`` must not widen what it may
do. The client advertises no filesystem and no terminal capability, it never
selects a standing permission option, and it only ever allows what the launch
argv already granted.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    copilot_acp,
    guidance_delivery,
    harness_adapter,
    harness_copilot,
    harness_process,
    harnesses,
)
from tests.support import REPO_ROOT, wait_until

FAKE_ACP_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_copilot_acp.py"

#: Every bounded wait in this module. Long enough that a loaded machine does not
#: fail a correct implementation, short enough that a wrong one fails rather
#: than hangs.
WAIT_SECONDS = 20.0

PROMPT = "do the work for session five"
SESSION_ID = "acp-session-1"

WORKER_GRANTED = copilot_acp.PermissionPolicy.for_turn(
    is_audit=False, unsafe_bypass=True
)
WORKER_UNGRANTED = copilot_acp.PermissionPolicy.for_turn(
    is_audit=False, unsafe_bypass=False
)
AUDIT = copilot_acp.PermissionPolicy.for_turn(is_audit=True, unsafe_bypass=True)


def line(payload: dict[str, Any]) -> str:
    return json.dumps(payload) + "\n"


def permission_request(options: list[dict[str, Any]] | None = None) -> str:
    if options is None:
        options = [
            {"optionId": "aa", "kind": "allow_always"},
            {"optionId": "ao", "kind": "allow_once"},
            {"optionId": "ro", "kind": "reject_once"},
            {"optionId": "ra", "kind": "reject_always"},
        ]
    return line(
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "method": "session/request_permission",
            "params": {
                "sessionId": SESSION_ID,
                "toolCall": {"toolCallId": "t1", "title": "rm -rf /"},
                "options": options,
            },
        }
    )


def update(payload: dict[str, Any], *, session_id: str = SESSION_ID) -> str:
    return line(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }
    )


class _FakeStdin:
    """The write end of a fake agent, answering on the caller's thread."""

    def __init__(self, process: _FakeProcess) -> None:
        self._process = process
        self.closed = False

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("write to a closed pipe")
        self._process.handle(text)
        return len(text)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("flush of a closed pipe")

    def close(self) -> None:
        self.closed = True
        self._process.on_stdin_closed()


class _FakeProcess:
    """A process-shaped object whose answers a test writes by hand.

    ``responder`` is called with each decoded message and returns the raw text
    to feed back, which is what lets a test split one response across reads,
    answer out of order, or never answer at all.
    """

    def __init__(self, responder: Any = None) -> None:
        self.stdin = _FakeStdin(self)
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.messages: list[dict[str, Any]] = []
        self.responder = responder
        self.session: copilot_acp.CopilotAcpSession | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def on_stdin_closed(self) -> None:
        if self.returncode is None:
            self.returncode = 0

    def handle(self, text: str) -> None:
        message = json.loads(text)
        self.messages.append(message)
        if self.responder is None:
            return
        for chunk in self.responder(self, message):
            assert self.session is not None
            self.session.feed(chunk)

    def methods(self) -> list[str]:
        return [
            str(message.get("method"))
            for message in self.messages
            if "method" in message
        ]

    def sent(self, method: str) -> dict[str, Any]:
        for message in self.messages:
            if message.get("method") == method:
                return message
        raise AssertionError(f"no {method} message was sent")

    def answer_to(self, request_id: object) -> dict[str, Any]:
        for message in self.messages:
            if message.get("id") == request_id and "method" not in message:
                return message
        raise AssertionError(f"no answer to request {request_id!r}")


def scripted_responder(
    *,
    protocol_version: int = copilot_acp.PROTOCOL_VERSION,
    config_options: list[dict[str, Any]] | None = None,
    session_id: str = SESSION_ID,
    stop_reason: str | None = copilot_acp.STOP_REASON_END_TURN,
    prompt_error: dict[str, Any] | None = None,
    updates: list[str] | None = None,
):
    """The happy-path agent, with one seam per failure a test wants."""

    if config_options is None:
        config_options = [
            {"id": "model", "currentValue": "fake-model-1"},
            {"id": "reasoning_effort", "currentValue": "low"},
        ]

    def respond(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            return [
                line(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": protocol_version,
                            "agentInfo": {"name": "FakeCopilot", "version": "0.0.1"},
                        },
                    }
                )
            ]
        if method == "session/new":
            return [
                line(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "sessionId": session_id,
                            "configOptions": config_options,
                        },
                    }
                )
            ]
        if method == "session/prompt":
            chunks = list(updates or [])
            if prompt_error is not None:
                chunks.append(
                    line({"jsonrpc": "2.0", "id": request_id, "error": prompt_error})
                )
                return chunks
            result: dict[str, Any] = {}
            if stop_reason is not None:
                result["stopReason"] = stop_reason
            chunks.append(line({"jsonrpc": "2.0", "id": request_id, "result": result}))
            return chunks
        return []

    return respond


def build_session(
    process: _FakeProcess,
    *,
    permissions: copilot_acp.PermissionPolicy = WORKER_GRANTED,
    **overrides: Any,
) -> copilot_acp.CopilotAcpSession:
    fields: dict[str, Any] = {
        "process": process,
        "prompt": PROMPT,
        "cwd": "/work",
        "permissions": permissions,
        "request_timeout": WAIT_SECONDS,
        "shutdown_grace": WAIT_SECONDS,
    }
    fields.update(overrides)
    session = copilot_acp.CopilotAcpSession(**fields)
    process.session = session
    return session


def run_session(
    process: _FakeProcess, **overrides: Any
) -> copilot_acp.CopilotAcpSession:
    """Drive one whole turn against a scripted agent and return the session."""
    session = build_session(process, **overrides)
    session.start()
    session.shutdown()
    return session


def _guidance_request() -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id="11111111-1111-4111-8111-111111111111",
        seq=1,
        text="prefer the smaller change",
        turn=guidance_delivery.TurnIdentity(
            run_id="run-1", session_id="5", turn_index=1, harness="copilot"
        ),
    )


__all__ = [name for name in globals() if not name.startswith("__")]
