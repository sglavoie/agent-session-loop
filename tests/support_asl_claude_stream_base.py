"""Shared fixtures and helpers for :mod:`tests.test_asl_claude_stream`."""

# ruff: noqa: F401
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_session_loop import (
    claude_stream,
    guidance_delivery,
    harness_claude,
    harness_copilot,
    harness_transport,
    harnesses,
)

GUIDANCE_ID = "11111111-1111-4111-8111-111111111111"

HOOK_WAIT_SECONDS = 30


def request_for(
    *,
    guidance_id: str = GUIDANCE_ID,
    seq: int = 1,
    text: str = "prefer the smaller change",
) -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id=guidance_id,
        seq=seq,
        text=text,
        turn=guidance_delivery.TurnIdentity(
            run_id="run-1",
            session_id="7",
            turn_index=2,
            harness="claude",
        ),
    )


class _Stdin(io.StringIO):
    """A stdin pipe that can be made to fail the way a dead CLI's does."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False
        self.closed_by_asl = False
        #: Set by the fake process that owns this pipe.
        self.process: Any = None
        #: Stands in for the output-processing thread reaching the session
        #: while a write is still in flight.
        self.on_flush: Any = None

    def write(self, text: str) -> int:  # type: ignore[override]
        if self.broken:
            raise BrokenPipeError("the harness is gone")
        return super().write(text)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("the harness is gone")
        super().flush()
        if self.on_flush is not None:
            self.on_flush()

    def close(self) -> None:
        self.closed_by_asl = True
        # Left open so a test can still read what was written to it.

    def messages(self) -> list[dict[str, Any]]:
        return [
            json.loads(line) for line in self.getvalue().splitlines() if line.strip()
        ]


class _Process(SimpleNamespace):
    """The CLI as the session sees it: a stdin pipe and a liveness answer."""

    def __init__(self, stdin: _Stdin) -> None:
        super().__init__(stdin=stdin)
        self.returncode: int | None = None
        # So a test holding only the pipe can still make the CLI die.
        stdin.process = self

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, code: int = 0) -> None:
        self.returncode = code


def session_with(
    *,
    prompt: str = "do the work",
    spool: claude_stream.ClaudeGuidanceSpool | None = None,
    session_id: str | None = None,
    interrupt_timeout: float = claude_stream.INTERRUPT_TIMEOUT_SECONDS,
) -> tuple[claude_stream.ClaudeStreamSession, _Stdin]:
    stdin = _Stdin()
    session = claude_stream.ClaudeStreamSession(
        process=_Process(stdin),
        prompt=prompt,
        spool=spool,
        session_id=session_id,
        interrupt_timeout=interrupt_timeout,
    )
    return session, stdin


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
