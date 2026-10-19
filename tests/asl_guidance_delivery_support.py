# ruff: noqa: F401
"""Live guidance delivery: the endpoint, the harness seam, and the poller.

Everything here runs in-process. The harness tests drive the real
``HarnessAdapter.run`` boundary with a fake process object rather than a fake
executable, so the module spawns nothing and belongs in the portable shard
while still exercising the code that owns the live process.
"""

from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    guidance_delivery,
    harness_execution,
    harness_process,
    harnesses,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    run_control,
    run_coordination_control,
    run_coordination_mailbox_poll,
    run_persistence,
)
from tests.support import read_jsonl

#: Every bounded wait in this module. Long enough that a loaded machine does
#: not fail a correct implementation, short enough that a wrong one fails
#: rather than hangs.
WAIT_SECONDS = 10.0


def turn_identity(**overrides: Any) -> guidance_delivery.TurnIdentity:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "session_id": "7",
        "turn_index": 2,
        "harness": "codex",
    }
    fields.update(overrides)
    return guidance_delivery.TurnIdentity(**fields)


def request_for(
    turn: guidance_delivery.TurnIdentity,
    *,
    guidance_id: str = "11111111-1111-4111-8111-111111111111",
    seq: int = 1,
    text: str = "prefer the smaller change",
) -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id=guidance_id,
        seq=seq,
        text=text,
        turn=turn,
    )


def recording_transport(
    calls: list[guidance_delivery.GuidanceRequest],
) -> guidance_delivery.GuidanceTransport:
    """A transport that always succeeds and remembers what it was handed."""

    def transport(
        request: guidance_delivery.GuidanceRequest,
    ) -> guidance_delivery.GuidanceOutcome:
        calls.append(request)
        return guidance_delivery.DELIVERED

    return transport


__all__ = [
    "Any",
    "Path",
    "WAIT_SECONDS",
    "events",
    "guidance_delivery",
    "harness_execution",
    "harness_process",
    "harnesses",
    "io",
    "mailbox",
    "mailbox_appenders",
    "mailbox_constants",
    "mailbox_cursors",
    "mailbox_models",
    "mailbox_snapshot",
    "mailbox_vocabulary",
    "mock",
    "read_jsonl",
    "recording_transport",
    "request_for",
    "run_control",
    "run_coordination_control",
    "run_coordination_mailbox_poll",
    "run_persistence",
    "tempfile",
    "threading",
    "time",
    "turn_identity",
    "unittest",
]
