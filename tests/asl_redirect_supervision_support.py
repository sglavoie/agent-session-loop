from __future__ import annotations

import argparse
import itertools
import unittest
from types import ModuleType

from agent_session_loop import (
    conversation_resume,
    conversations,
    events,
    overseer_decisions,
    pause_policy,
    redirect_cli,
    redirect_recovery,
    supervisor_guide,
)
from tests.support import _load_script_module

PLAN_DIR = "/tmp/plan"

PARSED = {"plan_dir": PLAN_DIR, "argv": ["asl", "run", PLAN_DIR, "--json"]}


def _counter_clock():
    """Monotonic-ish clock advancing one unit per call, so no test sleeps."""
    ticks = itertools.count()
    return lambda: float(next(ticks))


def _status_payload(pending, *, lock=None):
    return {
        "ok": True,
        "counts": {"pending": pending, "complete": 0},
        "next_pending_id": None if pending == 0 else "1",
        "lock": lock or {"present": False, "active": False, "stale": False},
        "sessions": [],
        "plan_dir": PLAN_DIR,
    }


def _active_lock():
    return {
        "present": True,
        "active": True,
        "stale": False,
        "plan_dir": PLAN_DIR,
        "reason": "active",
        "created_at": "2026-09-10T12:00:00+00:00",
    }


class _StatusScript:
    """Return scripted status payloads in order, repeating the last one."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, parsed):
        index = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return self.statuses[index]


class _RunScript:
    """Return scripted run payloads and record the chunk indices requested."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.chunks = []

    def __call__(self, parsed, chunk_index):
        self.chunks.append(chunk_index)
        return self.payloads[min(len(self.chunks) - 1, len(self.payloads) - 1)]


def _never_run(parsed, chunk_index):  # pragma: no cover - guard for attach paths
    raise AssertionError("run_invoker must not be called")


def _pause(reason, *, message=None, details=None, policy=None):
    """A paused run payload shaped the way the runner writes one."""
    payload = {
        "ok": False,
        "paused": True,
        "reason": reason,
        "pause_reason": reason,
        "pause_message": message or f"paused: {reason}",
        "pause_policy": policy or pause_policy.classify_pause(reason),
    }
    if details is not None:
        payload["pause_details"] = details
    return payload


def _transport_pause():
    return _pause(
        "transport-failed",
        message="connection reset by peer",
        details={
            "failure_classification": {
                "reason": "transport-failed",
                "retryable": True,
                "fingerprint": "transport-failed:connection-reset",
                "retry_at": None,
            }
        },
    )


def _conversation_recovery_pause(
    *, selection_state="incompatible", mismatched_fields=("root",)
):
    """The pause ``turn_preparation`` writes when no conversation may be resumed."""
    return _pause(
        events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        message="the recorded conversation cannot be resumed",
        details={
            "session": "1",
            "conversation_recovery": {
                "disposition": conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
                "conversation": "conv-1",
                "selection_state": selection_state,
                "mismatched_fields": list(mismatched_fields),
            },
        },
    )


def _successor_blocked_pause(reason="worker-still-alive"):
    """The pause ``turn_dispositions`` writes when a predecessor may still be alive."""
    return _pause(
        events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
        message="the redirected turn could not be confirmed stopped",
        details={
            "session": "1",
            "redirect_continuation": {"reason": reason, "report": None},
        },
    )


class _SupervisionCase(unittest.TestCase):
    """Shared loader and driver for the supervision loop under a fake clock."""

    autopilot: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.autopilot = _load_script_module(
            "asl_autopilot_redirect_supervision",
            "skills",
            "asl",
            "scripts",
            "asl_autopilot.py",
        )

    def _supervise(self, status_invoker, run_invoker, *, sleeps=None, **kwargs):
        return self.autopilot.supervise_run(
            dict(PARSED),
            status_invoker=status_invoker,
            run_invoker=run_invoker,
            clock=_counter_clock(),
            sleep=(lambda seconds: None) if sleeps is None else sleeps.append,
            heartbeat=lambda *_: None,
            **kwargs,
        )


# Explicit so the underscore-prefixed helpers survive ``import *``.
__all__ = [name for name in globals() if not name.startswith("__")]
