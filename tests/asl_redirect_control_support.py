"""Shared fixtures for test_asl_redirect_control."""

from __future__ import annotations

import threading

from agent_session_loop import (
    process_tree,
    redirect_control,
)
from tests.support_base import ManualClock as _Clock

# Real-process cases wait on other threads; every wait is bounded so a broken
# controller fails the assertion rather than hanging the suite.
PATIENCE = 10.0


class _FakeCancellation:
    """Structurally :class:`harnesses.AbortSignal`, with the waits observable."""

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
        """What a turn's owner calls once the process is reaped."""
        self._done.set()


class _Tools:
    """Recording stand-ins for the four process operations, order preserved."""

    def __init__(
        self,
        *,
        evidence: dict | None = None,
        confirmation: dict | None = None,
        evidence_gate: threading.Event | None = None,
        cleanup_gate: threading.Event | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.order: list[str] = []
        self.force_stopped: list[int] = []
        self.confirm_calls: list[int] = []
        # Set the moment the escaped-descendant cleanup begins, which is the
        # last point before the report exists: a test that holds ``cleanup_gate``
        # can wait on this and know the escalation is unfinished rather than
        # merely slow to start.
        self.cleanup_entered = threading.Event()
        self._lock = threading.Lock()
        self._evidence = (
            evidence
            if evidence is not None
            else {
                "root_pid": 4242,
                "root_pgid": 4242,
                "entries": [],
                "escaped_pids": [],
                "collected": True,
                "disabled": None,
                "sample_error": None,
            }
        )
        self._confirmation = (
            confirmation
            if confirmation is not None
            else {
                "pgid": 4242,
                "group": process_tree.GROUP_STOPPED,
                "remaining_pids": [],
                "pid_reused": [],
                "unverified_pids": [],
                "ownership_unknown": False,
                "complete": True,
                "incomplete_reason": None,
            }
        )
        self._evidence_gate = evidence_gate
        self._cleanup_gate = cleanup_gate
        self._cleanup_error = cleanup_error

    def _note(self, name: str) -> None:
        with self._lock:
            self.order.append(name)

    def collect_evidence(self, root_pid: int, **kwargs) -> dict:
        self._note("collect")
        if self._evidence_gate is not None:
            self._evidence_gate.wait(PATIENCE)
        self._note("collected")
        return dict(self._evidence)

    def force_stop(self, pgid: int, **kwargs) -> dict:
        self._note("force_stop")
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
        self._note("cleanup")
        self.cleanup_entered.set()
        if self._cleanup_gate is not None:
            self._cleanup_gate.wait(PATIENCE)
        if self._cleanup_error is not None:
            raise self._cleanup_error
        return {
            "policy": "identity_checked_sigkill",
            "candidate_pids": [],
            "killed": [],
        }

    def confirm(self, *, pgid: int, evidence, **kwargs) -> dict:
        self._note("confirm")
        with self._lock:
            self.confirm_calls.append(pgid)
        return dict(self._confirmation)

    def bundle(self) -> redirect_control.TerminationTools:
        return redirect_control.TerminationTools(
            collect_evidence=self.collect_evidence,
            force_stop=self.force_stop,
            terminate_escapees=self.terminate_escapees,
            confirm=self.confirm,
        )


def _controller(
    *,
    clock: _Clock,
    tools: _Tools,
    cancellation=None,
    transport=None,
    window_seconds: float = 2.0,
    **kwargs,
) -> redirect_control.UrgentController:
    controller = redirect_control.UrgentController(
        worker=redirect_control.OwnedWorker.session_leader(4242),
        cancellation=cancellation,
        transport=transport,
        window_seconds=window_seconds,
        poll_seconds=0.005,
        decision_grace_seconds=kwargs.pop("decision_grace_seconds", 0.05),
        evidence_wait_seconds=kwargs.pop("evidence_wait_seconds", 1.0),
        collection_wait_seconds=kwargs.pop("collection_wait_seconds", 0.05),
        monotonic=clock,
        tools=tools.bundle(),
        environ={},
        **kwargs,
    )
    return controller


# A worker that puts a grandchild in its *own* session, which is exactly what a
# harness shell that daemonizes a helper does -- and exactly what a group kill
# aimed at the worker cannot reach.
_WORKER_SOURCE = """
import subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    start_new_session=True,
)
sys.stdout.write("%d\\n" % child.pid)
sys.stdout.flush()
time.sleep(120)
"""
