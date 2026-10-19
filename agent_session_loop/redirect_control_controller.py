"""Urgent-redirect acceptance and phase transitions for a live turn."""

from __future__ import annotations

from pathlib import Path

from . import redirect_admission, redirect_store_models
from .redirect_control_escalation import UrgentEscalation
from .redirect_control_models import (
    PHASE_ABANDONED,
    PHASE_ARMED,
    PHASE_CONFIRMED,
    PHASE_IDLE,
    PHASE_RELEASED,
    REASON_REDIRECT,
    SKIP_ABANDONED,
    SKIP_DISARMED,
    SKIP_UNDECIDED,
)


class UrgentController(UrgentEscalation):
    """One turn's escalation clock, owned by whoever owns that turn's process.

    Constructed per turn, armed at most once per accepted correction, and
    released when the turn ends. Every injectable seam exists so the escalation
    can be driven without real time and without real processes: ``monotonic`` is
    the clock the deadline is measured on, ``tools`` are the four process
    operations, and ``spawn`` is how the watchdog and the fire-and-forget
    protocol call reach a thread.
    """

    def arm(
        self, *, operation_id: str | None = None, reason: str = REASON_REDIRECT
    ) -> float:
        """Start the escalation clock and the background evidence sample.

        Returns the monotonic deadline. Nothing observable outside this process
        has happened yet — no signal, no protocol call, no journal record — which
        is what makes it safe to run *before* acceptance is appended and still
        honest to call the window "two seconds from acceptance": the deadline is
        set slightly early, never late.

        Arming twice for one turn is refused rather than silently re-based. A
        second correction against a turn already escalating does not get a fresh
        two seconds; it rides the deadline already running, because the operator
        who sent the first one was promised that one.

        An *abandoned* arming is the one state that does re-base, and it is the
        exact complement of that rule: nothing was accepted, so there is no
        promised deadline to preserve and no acceptance left counting down. The
        alternative — a controller that stays abandoned for the rest of the turn
        — is worse than a missed deadline, because the journal would still
        accept the next correction against this turn while nothing at all was
        armed behind it. Rearming keeps the first arming's descendant sample:
        it was taken of this same worker while it was alive, which is the only
        moment it can be taken, and discarding a sample already in hand to start
        another could leave the escalation with no evidence at its deadline.
        """
        with self._lock:
            if self._phase not in (PHASE_IDLE, PHASE_ABANDONED):
                return self._deadline
            rearming = self._phase == PHASE_ABANDONED
            self._phase = PHASE_ARMED
            self._operation_id = operation_id
            self._reason = reason or REASON_REDIRECT
            self._deadline = self._monotonic() + self._window
            deadline = self._deadline
            if rearming:
                self._skipped = None
                self._failure = None
                # Bumped and cleared under one hold of the lock, which is what
                # stops the stood-down watchdog publishing over this arming:
                # it re-reads the epoch under the same lock before it sets
                # ``_finished`` or records anything.
                self._escalation_epoch += 1
                self._decided.clear()
                self._finished.clear()
                self._stand_down.clear()
                self._wake.clear()
            epoch = self._escalation_epoch
        if not rearming:
            self._spawn(self._collect_evidence, "asl-redirect-evidence")
        self._spawn(lambda: self._watch(epoch), "asl-redirect-watchdog")
        return deadline

    def confirm(self, *, operation_id: str | None = None) -> None:
        """Report the acceptance durable, and initiate native cancellation.

        This is the only transition that makes the watchdog's deadline binding.
        The two cancellation initiations it sends — the harness's own protocol
        interrupt and the abort signal the turn's wait loop reads — are both
        fire-and-forget, and both are strictly additional to the forced stop
        rather than a substitute for it.

        An acceptance that arrives after the watchdog has already given up
        waiting for it escalates immediately rather than not at all. The window
        is not extended by the delay — the deadline it missed stays the one on
        the report — but a durable acceptance always gets a forced stop, because
        the alternative is an operator told "accepted" by a journal record that
        no longer has anything counting down behind it.
        """
        with self._lock:
            if self._phase != PHASE_ARMED:
                return
            self._phase = PHASE_CONFIRMED
            self._acceptance_durable = True
            if operation_id is not None:
                self._operation_id = operation_id
            reason = self._reason
            remaining = max(0.0, self._deadline - self._monotonic())
            overdue = self._skipped == SKIP_UNDECIDED
            epoch = self._escalation_epoch
            if overdue:
                self._skipped = None
                self._escalation_epoch += 1
                epoch = self._escalation_epoch
                self._finished.clear()
        self._decided.set()
        self._initiate_cancellation(reason, remaining)
        if overdue:
            self._spawn(lambda: self._escalate_once(epoch), "asl-redirect-escalation")

    def abandon(self, reason: str = SKIP_ABANDONED) -> None:
        """Stand the watchdog down: the acceptance was refused or withdrawn.

        Safe only before :meth:`confirm`, and that asymmetry is deliberate. An
        arming nobody was told about can be taken back; an acceptance in the
        journal has already promised an operator that their correction reached a
        live turn, and no later refusal may quietly leave that turn running.

        Taking one arming back is not retiring the controller. The worker is
        still alive and the turn still owns it, so :meth:`arm` may start a fresh
        window here for the next correction; only :meth:`release` ends the
        controller with its turn.
        """
        with self._lock:
            if self._phase != PHASE_ARMED:
                return
            self._phase = PHASE_ABANDONED
            self._skipped = reason
        self._decided.set()
        self._stand_down.set()
        self._wake.set()

    def release(self) -> None:
        """End the controller with the turn it governs.

        The two phases part ways here, and the asymmetry is the same one
        :meth:`abandon` rests on. A watchdog that was armed but never confirmed
        promised nobody anything, so it is stood down outright — the pid it
        holds belongs to a finished turn and may be reused by anything. A
        *confirmed* one is woken instead of silenced: its deadline stops
        mattering the moment the turn is over, but the report does not, because
        a successor still has to be told whether anything of that turn is left
        running. It records ``turn-already-ended``, kills nothing, and confirms.

        An abandoned arming is stood down here too, and that is what separates
        the two ways an arming can end: :meth:`abandon` leaves the controller
        rearmable because its worker is still alive, while this leaves nothing
        rearmable at all, because the pid the next ``arm`` would aim at no
        longer belongs to this turn.
        """
        self._turn_over.set()
        with self._lock:
            if self._phase in (PHASE_IDLE, PHASE_ARMED, PHASE_ABANDONED):
                self._phase = PHASE_RELEASED
                if self._skipped is None:
                    self._skipped = SKIP_DISARMED
                self._decided.set()
                self._stand_down.set()
        self._wake.set()

    def wait_escalated(self, timeout: float | None = None) -> bool:
        """Wait for the watchdog to finish, reporting whether it did."""
        return self._finished.wait(timeout)

    def join(self, timeout: float | None = None) -> None:
        """Join the controller's own threads, for a caller tearing a turn down."""
        for thread in tuple(self._threads):
            thread.join(timeout)

    def accept(
        self,
        plan_dir: str | Path,
        operation_id: str,
        *,
        target: redirect_store_models.RedirectTarget,
        state_dir: str | Path | None = None,
        reason: str = REASON_REDIRECT,
    ) -> redirect_store_models.RedirectRequest:
        """Arm, record acceptance, and start cancelling — in that order.

        The order is the contract. Arming first means the escalation is running
        before the journal says "accepted", so there is no window in which a
        producer has been told its correction landed while nothing is counting
        down. Recording before cancelling means a turn is never interrupted for
        a correction the store refused — the refusals it can still raise here
        are expiry and a target that moved, and both mean the correction is not
        this turn's to act on.

        Raises :class:`~agent_session_loop.redirect_store_models.RedirectStoreError`
        exactly as the store does, with the watchdog already stood down.
        """
        self.arm(operation_id=operation_id, reason=reason)
        try:
            request = redirect_admission.accept_redirect(
                plan_dir, operation_id, target=target, state_dir=state_dir
            )
        except redirect_store_models.RedirectStoreError:
            self.abandon(SKIP_ABANDONED)
            raise
        self.confirm(operation_id=request.operation_id)
        return request
