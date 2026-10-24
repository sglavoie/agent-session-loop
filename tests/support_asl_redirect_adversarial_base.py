"""Shared fixtures and helpers for :mod:`tests.test_asl_redirect_adversarial`."""

# ruff: noqa: F401
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    clock,
    continuation_guards,
    conversation_resume,
    conversations,
    durability,
    events,
    plans_payloads,
    plans_session_ids,
    process_tree,
    redirect_admission,
    redirect_continuation,
    redirect_control,
    redirect_recovery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
)
from tests.support_base import ManualClock as _Clock

from .support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)
from .support_asl_continuation_guards import registered_guard
from .test_asl_conversation_resume import ContinuationTestBase

PATIENCE = 10.0

RUN_ID = "run-adversarial-1"

SESSION_ID = "7"

FEEDBACK = "the column you are parsing is the wrong one; re-read the fixture"


def target(
    turn_index: int = 1,
    *,
    run_id: str = RUN_ID,
    session_id: str = SESSION_ID,
    harness: str | None = "codex",
) -> redirect_store_models.RedirectTarget:
    return redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )


class _Cancellation:
    """Structurally :class:`harnesses.AbortSignal`, with its waits observable."""

    def __init__(self, *, completed: bool = False) -> None:
        self.armed: list[tuple[str, float]] = []
        self._done = threading.Event()
        if completed:
            self._done.set()

    def arm(self, reason: str, *, grace_seconds: float = 0.0) -> None:
        self.armed.append((reason, grace_seconds))

    @property
    def completed(self) -> bool:
        return self._done.is_set()

    def wait_completed(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def collect(self) -> None:
        self._done.set()


class _Tools:
    """Recording stand-ins for the four process operations."""

    def __init__(self, *, confirmation: dict | None = None) -> None:
        self._lock = threading.Lock()
        self.force_stopped: list[int] = []
        self._confirmation = confirmation or {
            "pgid": 4242,
            "group": process_tree.GROUP_STOPPED,
            "remaining_pids": [],
            "pid_reused": [],
            "unverified_pids": [],
            "ownership_unknown": False,
            "complete": True,
            "incomplete_reason": None,
        }

    def collect_evidence(self, root_pid: int, **kwargs) -> dict:
        return {
            "root_pid": root_pid,
            "root_pgid": root_pid,
            "entries": [],
            "escaped_pids": [],
            "collected": True,
            "disabled": None,
            "sample_error": None,
        }

    def force_stop(self, pgid: int, **kwargs) -> dict:
        with self._lock:
            self.force_stopped.append(pgid)
        return {
            "pgid": pgid,
            "signal": "SIGKILL",
            "signalled": True,
            "already_gone": False,
            "refused": None,
            "error": None,
        }

    def terminate_escapees(self, evidence, **kwargs) -> dict:
        return {
            "policy": "identity_checked_sigkill",
            "candidate_pids": [],
            "killed": [],
        }

    def confirm(self, *, pgid: int, evidence, **kwargs) -> dict:
        return dict(self._confirmation)

    def bundle(self) -> redirect_control.TerminationTools:
        return redirect_control.TerminationTools(
            collect_evidence=self.collect_evidence,
            force_stop=self.force_stop,
            terminate_escapees=self.terminate_escapees,
            confirm=self.confirm,
        )


class JournalTestCase(unittest.TestCase):
    """A plan directory with a real redirect journal on a real filesystem."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))

    def submit(
        self,
        operation_id: str | None = "op-1",
        *,
        turn_index: int = 1,
        feedback: str = FEEDBACK,
        ttl_seconds: int | None = None,
        now=None,
        session_id: str = SESSION_ID,
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(turn_index, session_id=session_id),
            feedback=feedback,
            state_dir=self.state_dir,
            operation_id=operation_id,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def journal(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def raw_records(self) -> list[dict]:
        path = redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        )
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class _StubController:
    """A controller that records nothing and stops nothing."""

    def __init__(self) -> None:
        self.released = 0

    def accept(self, plan_dir, operation_id, *, target, state_dir=None):
        return redirect_admission.accept_redirect(
            plan_dir, operation_id, target=target, state_dir=state_dir
        )

    def release(self) -> None:
        self.released += 1

    def wait_escalated(self, timeout: float | None = None) -> bool:
        return True

    def require_successor_allowed(self):
        return None

    def to_payload(self) -> dict:
        return {"phase": "idle", "operation_id": None}


_WEDGED_WORKER_SOURCE = """
import sys, time
chunk = "x" * 65536
while True:
    sys.stdout.write(chunk)
    sys.stdout.flush()
    time.sleep(0.001)
"""


def _accepted(
    operation_id: str,
    *,
    turn_index: int = 1,
    feedback_bytes: int = 12,
    digest: str = "d",
) -> redirect_continuation.AcceptedRedirect:
    return redirect_continuation.AcceptedRedirect(
        operation_id=operation_id,
        target=target(turn_index),
        accepted_at=clock.utc_iso_now(),
        feedback_digest=digest * 64,
        feedback_bytes=feedback_bytes,
    )
