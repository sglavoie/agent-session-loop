"""Shared fixtures and helpers for :mod:`tests.test_asl_redirect_recovery`."""

# ruff: noqa: F401
from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import (
    clock,
    conversations,
    plans_snapshot,
    redirect_ack,
    redirect_ack_attempt,
    redirect_ack_carry,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
    run_loop_carried_corrections,
    run_loop_control_redirects,
    state,
)

from .support import write_plan
from .support_asl_turn_dispositions import FakeRunContext

SESSION_ID = "1"

DEAD_RUN = "run-crashed"

NEW_RUN = "run-successor"

FIRST = "stop rewriting the fixture and read the contract"

SECOND = "the second column is the one that matters"


def target(
    turn_index: int = 1,
    *,
    run_id: str = DEAD_RUN,
    session_id: str = SESSION_ID,
    harness: str | None = "claude",
) -> redirect_store_models.RedirectTarget:
    return redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )


class RecoveryTestCase(unittest.TestCase):
    """A plan with its own state root and a real journal file behind it."""

    # Above every pid ceiling this runs on, so `is_pid_running` is false for it
    # without spawning and reaping anything -- which is what keeps this module
    # in the portable lane. The repo's other stale-lock tests use the same one.
    DEAD_PID = 2**30
    # A process that is certainly alive and certainly not this one. `os.kill(1, 0)`
    # answers either cleanly or with a permission error, and `is_pid_running`
    # reads both as running, so this needs no privileges and spawns nothing.
    LIVE_FOREIGN_PID = 1

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plan_dir = self.root / "plan"
        self.state_dir = self.root / "state"
        write_plan(self.plan_dir, ("pending",))

    # -- journal construction ------------------------------------------------

    def submit(
        self,
        feedback: str = FIRST,
        *,
        turn_index: int = 1,
        session_id: str = SESSION_ID,
        run_id: str = DEAD_RUN,
        **kwargs: Any,
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(turn_index, run_id=run_id, session_id=session_id),
            feedback=feedback,
            state_dir=self.state_dir,
            **kwargs,
        )

    def accept(
        self, request: redirect_store_models.RedirectRequest
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=request.target,
            state_dir=self.state_dir,
        )

    def read(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def journal_path(self) -> Path:
        return redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        )

    def journal_lines(self) -> list[dict[str, Any]]:
        path = self.journal_path()
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # -- lock construction ---------------------------------------------------

    def write_lock(
        self,
        *,
        pid: int | None = None,
        hostname: str | None = None,
        run_id: str = DEAD_RUN,
    ) -> Path:
        """Publish a lock by hand.

        Written rather than acquired because every interesting case is a lock
        the acquiring API would refuse to produce: a pid that is not running, a
        pid belonging to another machine's process table.
        """
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid() if pid is None else pid,
                    "plan_dir": str(self.plan_dir.resolve()),
                    "created_at": clock.utc_iso_now(),
                    "hostname": socket.gethostname() if hostname is None else hostname,
                    "run_id": run_id,
                    "redirect_endpoint": str(plan_state / "redirect-wakeup.fifo"),
                }
            ),
            encoding="utf-8",
        )
        return lock_path

    # -- convenience ---------------------------------------------------------

    def reconcile(
        self,
        *,
        run_id: str = NEW_RUN,
        pending: tuple[str, ...] = (SESSION_ID,),
        owner_pid: int | None = None,
    ) -> redirect_recovery.ReconciliationReport:
        return redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id=run_id,
            pending_session_ids=pending,
            state_dir=self.state_dir,
            owner_pid=owner_pid,
        )

    def inspect(self) -> redirect_recovery.RecoveryReport:
        return redirect_recovery.inspect_plan_recovery(
            self.plan_dir, state_dir=self.state_dir
        )

    def status_of(self, operation_id: str) -> str:
        report = self.inspect()
        for item in report.operations:
            if item.operation_id == operation_id:
                return item.status
        raise AssertionError(f"{operation_id} is not in the report")
