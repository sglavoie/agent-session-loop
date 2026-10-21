"""Admission: what a producer is told, and what it is never told falsely.

The store's tests prove a correction cannot be lost. These prove the second
half — that "accepted" is only ever said when a runner recorded acceptance,
that every refusal that could have been persisted was refused *before* it was,
and that a submission the urgent channel could not carry is closed in the
journal rather than left for some later runner to act on.

The endpoints are real FIFOs served by the production reader, because "is the
urgent channel actually being served" is the fact admission turns on and a fake
would answer it by assumption.
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    clock,
    control_signal,
    redirect_admission,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
)
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "run-20260910-000000"


class FrozenClock:
    """A monotonic source the wait loop advances by asking for the time.

    Driving the clock from ``sleep`` rather than from real time is what keeps
    these tests deterministic: a two-second bound tested against a real clock is
    a two-second test and a flake on a loaded machine.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class AdmissionTestCase(unittest.TestCase):
    """A plan whose live run, active turn, and urgent endpoint are set per test."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.clock = FrozenClock()

    def write_state(
        self,
        *,
        run_id: str | None = RUN_ID,
        active_turn: dict | None = None,
        paused: bool = False,
        finished: bool = False,
        session_kind: str = "implementation",
        turn_index: int = 1,
    ) -> None:
        if active_turn is None and run_id is not None:
            active_turn = {
                "run_id": run_id,
                "session_id": "session-9",
                "turn_index": turn_index,
                "session_kind": session_kind,
                "harness": "claude",
            }
        payload: dict = {
            "version": 3,
            "plan_dir": str(self.plan_dir),
            "paused": paused,
        }
        if run_id is not None:
            payload["run_id"] = run_id
        if active_turn is not None:
            payload["active_turn"] = active_turn
        if finished:
            payload["run_finished_at"] = clock.utc_iso_now()
        _write_state_payload(self.plan_dir, self.state_dir, payload)

    def write_lock(self, **kwargs) -> None:
        params = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }
        params.update(kwargs)
        _write_lock_file(self.plan_dir, self.state_dir, **params)

    def serve_urgent_endpoint(self) -> Path:
        endpoint, self.wakeup = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        return endpoint

    def make_live_run(self, **state_kwargs) -> Path:
        """A run that is live, has an active turn, and serves urgent redirects."""
        self.write_state(**state_kwargs)
        endpoint = self.serve_urgent_endpoint()
        self.write_lock(redirect_endpoint=endpoint)
        return endpoint

    def submit(self, **kwargs) -> redirect_admission.SubmissionOutcome:
        params = {
            "feedback": "stop and re-read the session contract",
            "state_dir": self.state_dir,
            "monotonic": self.clock.monotonic,
            "sleep": self.clock.sleep,
        }
        params.update(kwargs)
        feedback = params.pop("feedback")
        return redirect_admission.submit_redirect(self.plan_dir, feedback, **params)

    def journal(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def assertNothingPersisted(self) -> None:
        self.assertEqual((), self.journal().requests)


__all__ = [
    "os",
    "socket",
    "tempfile",
    "unittest",
    "Path",
    "clock",
    "control_signal",
    "redirect_admission",
    "redirect_store_journal",
    "redirect_store_models",
    "redirect_store_requests",
    "redirect_store_vocab",
    "_write_lock_file",
    "_write_state_payload",
    "live_redirect_endpoint",
    "write_plan",
    "RUN_ID",
    "FrozenClock",
    "AdmissionTestCase",
]
