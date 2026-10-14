"""Shared fixtures for the redirect continuation test modules.

Split from tests/test_asl_redirect_continuation.py."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    redirect_admission,
    redirect_continuation,
    redirect_control,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
)

RUN_ID = "run-redirect-1"
SESSION_ID = "1"
SESSION_LABEL = "Session 1"
FEEDBACK = "the parser you picked cannot see the second column"

# Nothing here signals anything: every controller in this module is built with
# stand-in process tools, so the pid is only an identity the report carries.
WORKER_PID = 424242

# A plan image the caller did not pass, which is not the same as one it passed
# as ``None`` -- an uninspectable plan is a case the branch has to handle.
UNCHANGED = object()


def target(turn_index: int = 1, *, harness: str | None = "codex", run_id: str = RUN_ID):
    return redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id=SESSION_ID,
        turn_index=turn_index,
        harness=harness,
    )


def termination_report(
    *, collected: bool = True, complete: bool = True
) -> redirect_control.TerminationReport:
    """A report shaped like the escalation's, with its verdict dialled."""
    return redirect_control.TerminationReport(
        operation_id="op-1",
        worker=redirect_control.OwnedWorker.session_leader(WORKER_PID),
        reason=redirect_control.REASON_REDIRECT,
        deadline=1000.0,
        escalated_at=1002.0,
        forced=True,
        collected=collected,
        confirmation={"complete": complete, "incomplete_reason": None},
    )


class FakeController:
    """Structurally an :class:`redirect_control.UrgentController`.

    Journal-truthful on purpose: ``accept`` delegates to the real admission
    append, because the half of the serialization these tests are about is
    whether the *record* exists, and a fake that only remembered the call in
    memory could not tell an accepted correction from a refused one.
    """

    def __init__(
        self,
        *,
        report: redirect_control.TerminationReport | None = None,
        blocked: Exception | None = None,
    ) -> None:
        self.accepted: list[str] = []
        self.released = 0
        self.waits: list[float | None] = []
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self._report = report
        self._blocked = blocked

    def accept(
        self,
        plan_dir: str | Path,
        operation_id: str,
        *,
        target: redirect_store_models.RedirectTarget,
        state_dir: str | Path | None = None,
    ) -> redirect_store_models.RedirectRequest:
        self.entered.set()
        self.gate.wait(10.0)
        self.accepted.append(operation_id)
        return redirect_admission.accept_redirect(
            plan_dir, operation_id, target=target, state_dir=state_dir
        )

    def release(self) -> None:
        self.released += 1

    def wait_escalated(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return True

    def require_successor_allowed(self) -> redirect_control.TerminationReport | None:
        if self._blocked is not None:
            raise self._blocked
        return self._report

    def to_payload(self) -> dict[str, Any]:
        return {"phase": "confirmed", "operation_id": "op-1"}


class JournalTestCase(unittest.TestCase):
    """A plan directory with a redirect journal and nothing else live."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.state_dir = self.tmp_dir / "state"
        self.plan_dir.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        operation_id: str = "op-1",
        *,
        turn_index: int = 1,
        feedback: str = FEEDBACK,
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(turn_index),
            feedback=feedback,
            state_dir=self.state_dir,
            operation_id=operation_id,
        )

    def journal(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def gate(
        self,
        *,
        controller: FakeController | None = None,
        wakeup: threading.Event | None = None,
        bind_wait_seconds: float = 0.05,
        turn_index: int = 1,
    ) -> tuple[redirect_continuation.TurnRedirect, FakeController]:
        built = controller or FakeController(report=termination_report())
        turn_redirect = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(turn_index),
            state_dir=self.state_dir,
            wakeup=wakeup,
            build_controller=lambda _worker: built,
            poll_seconds=0.01,
            bind_wait_seconds=bind_wait_seconds,
            report_wait_seconds=0.5,
        )
        self.addCleanup(turn_redirect.close)
        return turn_redirect, built
