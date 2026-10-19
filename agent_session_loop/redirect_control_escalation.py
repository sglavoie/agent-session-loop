"""Urgent cancellation, escalation evidence, and successor gating."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import redirect_restart
from .redirect_control_models import (
    BLOCK_FAILED,
    BLOCK_UNCONFIRMED,
    BLOCK_UNREPORTED,
    COLLECTION_WAIT_SECONDS,
    DECISION_GRACE_SECONDS,
    ESCALATION_WINDOW_SECONDS,
    EVIDENCE_WAIT_SECONDS,
    PHASE_ABANDONED,
    PHASE_CONFIRMED,
    PHASE_ESCALATED,
    PHASE_IDLE,
    REASON_REDIRECT,
    SKIP_ABANDONED,
    SKIP_DISARMED,
    SKIP_TURN_ENDED,
    SKIP_UNDECIDED,
    WATCHDOG_POLL_SECONDS,
    CancellableTurn,
    InterruptibleTransport,
    OwnedWorker,
    Phase,
    SuccessorBlockedError,
    TerminationReport,
    TerminationTools,
)


class UrgentEscalation:
    """Shared turn state and the watchdog that proves predecessor termination."""

    def __init__(
        self,
        *,
        worker: OwnedWorker,
        cancellation: CancellableTurn | None = None,
        transport: InterruptibleTransport | None = None,
        window_seconds: float = ESCALATION_WINDOW_SECONDS,
        poll_seconds: float = WATCHDOG_POLL_SECONDS,
        decision_grace_seconds: float = DECISION_GRACE_SECONDS,
        evidence_wait_seconds: float = EVIDENCE_WAIT_SECONDS,
        collection_wait_seconds: float = COLLECTION_WAIT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        tools: TerminationTools | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._recovery_directory: Path | None = None
        self._worker = worker
        self._cancellation = cancellation
        self._transport = transport
        self._window = max(0.0, float(window_seconds))
        self._poll = max(0.0, float(poll_seconds))
        self._decision_grace = max(0.0, float(decision_grace_seconds))
        self._evidence_wait = max(0.0, float(evidence_wait_seconds))
        self._collection_wait = max(0.0, float(collection_wait_seconds))
        self._monotonic = monotonic
        self._tools = tools or TerminationTools()
        self._environ = environ

        # A leaf lock and nothing else. Every field below is read and written
        # only under it, and no call made while holding it can block: the kill
        # spawns nothing, and the protocol interrupt is dispatched to a thread.
        self._lock = threading.Lock()
        self._phase: Phase = PHASE_IDLE
        self._operation_id: str | None = None
        self._reason: str = REASON_REDIRECT
        self._deadline: float = 0.0
        self._skipped: str | None = None
        self._report: TerminationReport | None = None
        # Set by ``confirm`` and never cleared: once an acceptance is in the
        # journal this turn owes a successor proof that its worker stopped, and
        # no later transition -- a rearming, a release, a failed escalation --
        # takes that debt back. The successor gate reads it rather than the
        # phase because the phase is about what the watchdog is doing now.
        self._acceptance_durable = False
        # What the escalation raised, if it raised. Separates "the report is
        # not here yet" from "the run that would have written it is gone".
        self._failure: str | None = None

        self._decided = threading.Event()
        # ``_wake`` ends the watchdog's wait early; ``_stand_down`` is the
        # subset of those wakeups that mean "and stop nothing". Two events
        # rather than one because a released turn has to wake the watchdog
        # *and* have it finish a report.
        self._wake = threading.Event()
        self._stand_down = threading.Event()
        self._turn_over = threading.Event()
        self._finished = threading.Event()
        self._evidence_ready = threading.Event()
        self._evidence: dict[str, Any] = {}
        self._threads: list[threading.Thread] = []
        # Bumped when a late acceptance restarts an escalation the watchdog had
        # already given up on, so the superseded run cannot publish ``finished``
        # over the top of the one that replaced it.
        self._escalation_epoch = 0

    def set_recovery_directory(self, directory: Path) -> None:
        """Persist the ownership obligation before any acceptance is possible."""
        redirect_restart.write_evidence(directory, self._worker.to_payload(), {})
        self._recovery_directory = directory

    @property
    def worker(self) -> OwnedWorker:
        return self._worker

    @property
    def phase(self) -> Phase:
        with self._lock:
            return self._phase

    @property
    def deadline(self) -> float:
        """The monotonic reading the force-stop is due at, or ``0.0`` if unarmed."""
        with self._lock:
            return self._deadline

    @property
    def operation_id(self) -> str | None:
        with self._lock:
            return self._operation_id

    @property
    def termination(self) -> TerminationReport | None:
        """The escalation's report, or ``None`` if it has not finished one."""
        with self._lock:
            return self._report

    def to_payload(self) -> dict[str, Any]:
        with self._lock:
            report = self._report
            payload: dict[str, Any] = {
                "worker": self._worker.to_payload(),
                "phase": self._phase,
                "operation_id": self._operation_id,
                "reason": self._reason,
                "window_seconds": self._window,
                "deadline": round(self._deadline, 3),
                "skipped": self._skipped,
                "acceptance_durable": self._acceptance_durable,
                "failure": self._failure,
            }
        payload["termination"] = report.to_payload() if report is not None else None
        return payload

    def confirm_stopped(self) -> TerminationReport | None:
        """Re-check the stored report against the machine as it is now.

        The first confirmation runs microseconds after the kill, when a reaped
        child and an unreaped one look different and a caller may not yet have
        waited on either. Re-checking is how a blocked continuation clears once
        the turn's owner has finished with the process, and it re-runs the
        identity checks rather than trusting the earlier verdict.
        """
        with self._lock:
            report = self._report
        if report is None:
            return None
        confirmation = self._tools.confirm(
            pgid=report.worker.pgid, evidence=report.evidence
        )
        updated = TerminationReport(
            operation_id=report.operation_id,
            worker=report.worker,
            reason=report.reason,
            deadline=report.deadline,
            escalated_at=report.escalated_at,
            forced=report.forced,
            collected=report.collected or self._turn_collected(0.0),
            skipped=report.skipped,
            group_stop=report.group_stop,
            evidence=report.evidence,
            cleanup=report.cleanup,
            confirmation=confirmation,
        )
        with self._lock:
            self._report = updated
        return updated

    def require_successor_allowed(self) -> TerminationReport | None:
        """Raise unless no predecessor of this turn is still around.

        Returns ``None`` only when nothing was ever accepted against this turn,
        which is the ordinary case: a turn that was never redirected leaves no
        predecessor for the next one to collide with, and a controller that was
        armed and then refused never interrupted anything either.

        Once an acceptance is durable the absence of a report is a refusal, not
        a pass. The report is published after the escalation has cleaned up
        escaped descendants and confirmed them gone, and both of those run per
        pid against a real process table, so "not here yet" is the shape a slow
        cleanup takes as readily as a wedged one -- while a watchdog that died
        on the way leaves exactly the same nothing behind. None of the three is
        a proof that the worker stopped, so none of them may admit a successor:
        the caller pauses and an operator looks, which is recoverable, where
        two workers on one conversation is not.

        This does not wait for the report. The turn's own teardown already
        spent a bounded wait on it; a second one here would push the same
        unbounded cleanup into the disposition that has to decide.
        """
        report = self.confirm_stopped()
        if report is None:
            with self._lock:
                promised = self._acceptance_durable
                failure = self._failure
            if not promised:
                return None
            raise SuccessorBlockedError(
                BLOCK_FAILED if failure is not None else BLOCK_UNREPORTED, None
            )
        if not report.permits_continuation:
            raise SuccessorBlockedError(
                report.blocking_reason or BLOCK_UNCONFIRMED, report
            )
        if self._recovery_directory is not None:
            try:
                redirect_restart.mark_stopped(self._recovery_directory)
            except OSError as error:
                raise SuccessorBlockedError(BLOCK_UNREPORTED, report) from error
        return report

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _initiate_cancellation(self, reason: str, grace_seconds: float) -> None:
        """Ask the turn to stop, twice, without waiting for either answer.

        The abort signal is armed with the escalation's own remaining grace, so
        the turn's cooperative path aims at the same deadline this watchdog
        does. When both fire, the second ``killpg`` finds an empty group; when
        the cooperative path is blocked or has not started, the watchdog is the
        one that lands.
        """
        cancellation = self._cancellation
        if cancellation is not None:
            cancellation.arm(reason, grace_seconds=grace_seconds)
        transport = self._transport
        if transport is None:
            return

        def send() -> None:
            try:
                transport.interrupt()
            except Exception:  # noqa: BLE001 - a refused interrupt is not a failure
                return

        self._spawn(send, "asl-redirect-interrupt")

    def _collect_evidence(self) -> None:
        """Sample the worker's descendants while the worker is still alive."""
        try:
            evidence = self._tools.collect_evidence(
                self._worker.pid, environ=self._environ
            )
        except Exception as exc:  # noqa: BLE001 - unavailable evidence blocks
            evidence = {
                "root_pid": self._worker.pid,
                "entries": [],
                "escaped_pids": [],
                "collected": False,
                "sample_error": str(exc),
            }
        if self._recovery_directory is not None:
            try:
                redirect_restart.write_evidence(
                    self._recovery_directory, self._worker.to_payload(), evidence
                )
            except OSError as error:
                evidence = {**evidence, "collected": False, "sample_error": str(error)}
        self._evidence = evidence
        self._evidence_ready.set()

    def _watch(self, epoch: int) -> None:
        """Wait out the window on a monotonic clock, then escalate once.

        ``epoch`` is the arming this watchdog belongs to, passed in rather than
        read here: a rearming clears the very events this loop is waiting on, so
        a watchdog that read the current epoch after waking would adopt the
        arming that replaced it.
        """
        while not self._wake.is_set():
            remaining = self.deadline - self._monotonic()
            if remaining <= 0.0:
                break
            self._wake.wait(min(remaining, self._poll) if self._poll else remaining)
        if self._stand_down.is_set():
            self._mark_finished(epoch)
            return
        self._escalate_once(epoch)

    def _escalate_once(self, epoch: int) -> None:
        """Run one escalation to completion, finished either way.

        A raising escalation is recorded rather than only re-raised. It runs on
        a daemon thread nobody joins, so the exception itself reaches only the
        interpreter's excepthook, while the fact a caller needs -- that the
        report is absent because the run producing it died -- would otherwise be
        indistinguishable from a cleanup still in progress.
        """
        try:
            self._escalate(epoch)
        except BaseException as exc:
            with self._lock:
                if epoch == self._escalation_epoch:
                    self._failure = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._mark_finished(epoch)

    def _mark_finished(self, epoch: int) -> None:
        """Publish completion unless a later escalation has superseded this one.

        A watchdog that gave up undecided and a late acceptance that restarts
        it — or one stood down by a refusal and a rearming that replaces it —
        race by construction: the first can still be unwinding when the second
        clears the flag. Without the epoch the stale ``set`` lands after that
        clear and a caller waiting on the restarted escalation is told it
        finished before it began. The check and the ``set`` share one hold of
        the lock, which is the same hold the bump and the clear share, so
        there is no ordering in which the stale watchdog can slip between them.
        """
        with self._lock:
            if epoch != self._escalation_epoch:
                return
            self._finished.set()

    def _escalate(self, epoch: int) -> None:
        """The deadline path. Everything slow happens after the kill."""
        if not self._decided.is_set():
            # Only reachable when the acceptance append is itself stalled. The
            # producer has not been told anything yet, so the safe direction is
            # to leave the turn alone rather than kill for an unmade promise.
            self._decided.wait(self._decision_grace)
        # Read outside the lock: ``completed`` takes the abort signal's own leaf
        # lock, and the deadline path acquires nothing it does not own.
        cancellation = self._cancellation
        already_done = cancellation is not None and cancellation.completed
        with self._lock:
            if epoch != self._escalation_epoch:
                # A rearming replaced this watchdog while it was waiting out a
                # deadline that is no longer anyone's. It must record nothing:
                # the ``skipped`` it would write is the *previous* arming's
                # verdict, and the live arming reads that field to decide
                # whether its own acceptance arrived overdue.
                return
            phase = self._phase
            skipped: str | None = None
            if phase != PHASE_CONFIRMED:
                skipped = self._skipped or (
                    SKIP_ABANDONED if phase == PHASE_ABANDONED else SKIP_UNDECIDED
                )
            elif self._turn_over.is_set() or already_done:
                skipped = SKIP_TURN_ENDED
            if skipped is not None:
                self._skipped = skipped
            else:
                self._phase = PHASE_ESCALATED
            operation_id = self._operation_id
            reason = self._reason
            deadline = self._deadline

        if skipped in (SKIP_ABANDONED, SKIP_UNDECIDED, SKIP_DISARMED):
            return

        group_stop: dict[str, Any] = {}
        if skipped is None:
            # The kill, and nothing between it and the deadline: no sample, no
            # journal read, no lock held across it.
            group_stop = self._tools.force_stop(self._worker.pgid)
        escalated_at = self._monotonic()

        self._evidence_ready.wait(self._evidence_wait)
        evidence = dict(self._evidence)
        cleanup = self._tools.terminate_escapees(evidence)
        collected = self._turn_collected(self._collection_wait)
        confirmation = self._tools.confirm(pgid=self._worker.pgid, evidence=evidence)

        report = TerminationReport(
            operation_id=operation_id,
            worker=self._worker,
            reason=reason,
            deadline=deadline,
            escalated_at=escalated_at,
            forced=skipped is None,
            collected=collected,
            skipped=skipped,
            group_stop=group_stop,
            evidence=evidence,
            cleanup=cleanup,
            confirmation=confirmation,
        )
        with self._lock:
            self._report = report

    def _turn_collected(self, timeout: float) -> bool:
        """Whether the turn's owner has reported the worker reaped.

        A controller with no cancellation seam has no owner to hear this from,
        and says so by reporting the worker uncollected: an unproven collection
        blocks a successor exactly as a failed one does.
        """
        cancellation = self._cancellation
        if cancellation is None:
            return False
        if cancellation.completed:
            return True
        return cancellation.wait_completed(timeout)
