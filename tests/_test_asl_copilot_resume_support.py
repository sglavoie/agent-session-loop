# mypy: ignore-errors
# ruff: noqa
"""Shared fakes and helpers for the test_asl_copilot_resume test groups.

The fakes are imported from :mod:`tests.test_asl_copilot_acp` rather than
copied. Two hand-written copies of one protocol drift, and the failure that
finds the drift is a test that passes against a fake no agent resembles.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    conversations,
    copilot_acp,
    guidance_delivery,
    harness_copilot,
    harness_process,
    harnesses,
)
from tests.support import wait_until
from tests.test_asl_copilot_acp import (
    FAKE_ACP_FIXTURE,
    PROMPT,
    SESSION_ID,
    WAIT_SECONDS,
    _FakeProcess,
    build_session,
    line,
    update,
)

#: The session a restore is answered with when the test wants a contradiction.
OTHER_SESSION_ID = "acp-session-somewhere-else"

#: What the fixture replays and what it says this turn did. Kept apart on
#: purpose: every assertion about the log is an assertion that one of these
#: texts is present and the other is not.
REPLAYED_PROMPT = "the prompt of the turn that came before"
REPLAYED_ANSWER = "what the earlier turn answered\n"
LIVE_ANSWER = "working\n"

_UNSET = object()


class _RenderingProcess(_FakeProcess):
    """A fake process that keeps what each fed chunk rendered into the log.

    ``feed`` returns the text the turn's log receives, and the plain fake
    discards it. Every replay assertion here is about that return value being
    empty, so it has to be kept.
    """

    def __init__(self, responder: Any = None) -> None:
        super().__init__(responder)
        self.rendered: list[str] = []

    def handle(self, text: str) -> None:
        message = json.loads(text)
        self.messages.append(message)
        if self.responder is None:
            return
        for chunk in self.responder(self, message):
            assert self.session is not None
            self.rendered.append(self.session.feed(chunk))

    def log(self) -> str:
        return "".join(self.rendered)


class _SurvivingProcess(_RenderingProcess):
    """An agent that outlives its own stdin.

    Closing stdin is how this client asks an ACP agent to shut down, and the
    grace period exists because a wedged one does not always take the hint.
    That is the only state where a cancellation is attempted against a process
    that is still running through a pipe that is already gone.
    """

    def on_stdin_closed(self) -> None:
        return


def resume_responder(
    *,
    agent_capabilities: Any = _UNSET,
    load_error: dict[str, Any] | None = None,
    load_result: Any = _UNSET,
    replay: tuple[str, ...] = (),
    updates: tuple[str, ...] = (),
    stop_reason: str | None = copilot_acp.STOP_REASON_END_TURN,
    hold_prompt: bool = False,
):
    """An agent that can restore a session, with one seam per refusal.

    ``hold_prompt`` never answers ``session/prompt``, which is how a test gets
    to a submitted-but-unfinished turn without a timer: the answer arrives only
    when the test cancels, and the cancellation is what produces it.
    """
    if agent_capabilities is _UNSET:
        agent_capabilities = {copilot_acp.AGENT_CAPABILITY_LOAD_SESSION: True}
    #: Remembered so the cancellation can answer the prompt it interrupted.
    live: dict[str, Any] = {"prompt_id": None}

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
                            "protocolVersion": copilot_acp.PROTOCOL_VERSION,
                            "agentCapabilities": agent_capabilities,
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
                        "result": {"sessionId": SESSION_ID},
                    }
                )
            ]
        if method == "session/load":
            # The transcript first, then the answer, which is the ordering the
            # protocol specifies and the one the client has to survive.
            chunks = list(replay)
            if load_error is not None:
                chunks.append(
                    line({"jsonrpc": "2.0", "id": request_id, "error": load_error})
                )
                return chunks
            result = {} if load_result is _UNSET else load_result
            chunks.append(line({"jsonrpc": "2.0", "id": request_id, "result": result}))
            return chunks
        if method == "session/prompt":
            live["prompt_id"] = request_id
            if hold_prompt:
                return list(updates)
            return [*updates, _prompt_answer(request_id, stop_reason)]
        if method == "session/cancel":
            if not hold_prompt:
                return []
            # The acknowledgment the protocol actually defines: the pending
            # prompt comes back cancelled.
            return [
                _prompt_answer(live["prompt_id"], copilot_acp.STOP_REASON_CANCELLED)
            ]
        return []

    return respond


def _prompt_answer(request_id: object, stop_reason: str | None) -> str:
    result: dict[str, Any] = {}
    if stop_reason is not None:
        result["stopReason"] = stop_reason
    return line({"jsonrpc": "2.0", "id": request_id, "result": result})


def replayed_transcript() -> tuple[str, ...]:
    """One earlier turn, streamed back the way ``session/load`` streams it."""
    return (
        update(
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": REPLAYED_PROMPT},
            }
        ),
        update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "old-tool-1",
                "title": "an earlier command",
                "status": "completed",
            }
        ),
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": REPLAYED_ANSWER},
            }
        ),
    )


def live_activity() -> tuple[str, ...]:
    return (
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": LIVE_ANSWER},
            }
        ),
    )


def run_resume(
    process: _FakeProcess,
    *,
    resume: str | None = SESSION_ID,
    **overrides: Any,
) -> copilot_acp.CopilotAcpSession:
    session = build_session(process, resume_conversation_id=resume, **overrides)
    session.start()
    session.shutdown()
    return session


def _guidance_request() -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id="22222222-2222-4222-8222-222222222222",
        seq=1,
        text="prefer the smaller change",
        turn=guidance_delivery.TurnIdentity(
            run_id="run-1", session_id="6", turn_index=1, harness="copilot"
        ),
    )
