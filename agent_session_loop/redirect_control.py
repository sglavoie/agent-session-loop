"""The urgent controller: one live turn's route from correction to dead worker.

:mod:`agent_session_loop.redirect_store` makes a correction durable and
:mod:`agent_session_loop.redirect_admission` decides whether a live run may take
it. Neither of them stops anything. This module is what turns an accepted
correction into a worker that has actually stopped, inside a bound the operator
was promised, and it exists as its own module because every other route to that
outcome runs through something that can be blocked.

The turn's own wait loop is one such route, and it is the cooperative one: it
notices the armed :class:`~agent_session_loop.harnesses.AbortSignal`, names the
cancellation window, kills the group, sweeps the descendants, and produces the
``aborted`` result the run loop accounts for. All of that is worth having and
none of it is guaranteed. It runs only once the transport has been opened, so a
handshake wedged inside ``open_transport`` never reaches it; and it samples the
process table before it kills, on a five-second bound that is longer than this
whole escalation window. A correction whose delivery depends on a healthy
harness is a correction that fails exactly when it is needed.

So the escalation here is a separate, dedicated path with four properties:

**It is armed before acceptance is reported.** An operator is told "accepted"
by the journal record, and by then the deadline that makes acceptance mean
something is already running. :meth:`UrgentController.arm` is deliberately
cheap and revocable — a timer and a background sample, nothing anyone can
observe — so it can precede the append without committing to anything, and
:meth:`UrgentController.abandon` stands it down if the append is refused.
Revocable also means repeatable: a refusal costs the *request* its escalation,
never the turn its urgent channel, so an arming that was stood down can be
armed again for the next correction aimed at the same live worker.

**It initiates native cancellation immediately.** The moment acceptance is
durable, the harness is told to stop over its own protocol and the abort signal
is armed. Both are fire-and-forget: the protocol call goes on a daemon thread
nothing joins, because ``interrupt`` on a wedged transport is precisely the call
that can block forever.

**It force-stops at the deadline regardless.** The watchdog runs on its own
thread against a monotonic clock, takes no lock but its own leaf lock, and calls
:func:`~agent_session_loop.process_tree.force_stop_process_group`, which spawns
nothing. There is no ``ps``, no journal read, no transport call and no shared
lock between the deadline passing and the ``killpg``. The watchdog cannot queue
behind live guidance delivery, a protocol request, or a process-table probe,
because it never asks any of them for anything.

**It reports whether the worker actually stopped.** Killing is not stopping. A
descendant that left the process group survives a ``killpg``, and a signalled
process stays visible until its parent reaps it, so continuation is gated on
:class:`TerminationReport` rather than on the kill having been issued. Evidence
is collected while the worker is still alive — the only moment an escapee is
reachable by a descendant walk — and identity-checked cleanup and confirmation
both run after the kill, so neither is on the deadline path.

The gate is :meth:`UrgentController.require_successor_allowed`, and it refuses
for an uncollected predecessor as readily as for a live one, and for a report
that never arrived as readily as for one that says no. Cleanup and confirmation
run per pid against a real process table after the kill, so an escalation that
is merely slow, one that is wedged, and one whose thread died all present the
same way — as an acceptance with nothing published behind it — and none of them
is evidence that anything stopped. Two workers on one conversation, one of them
a process nobody is waiting on any more, is the failure this whole subsystem
would otherwise introduce.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from . import process_tree, redirect_admission, redirect_store

#: The escalation deadline, measured from arming. Derived from the one number
#: the producer's own bounded wait is derived from, rather than set beside it:
#: they are the two halves of a single promise — "this correction reaches a
#: stopped worker within two seconds, or you are told it did not" — and two
#: independently maintained constants could drift into a window where the
#: producer gives up before the runner escalates.
ESCALATION_WINDOW_SECONDS = redirect_admission.URGENT_WINDOW_SECONDS

#: How often the watchdog re-reads the clock while waiting out the window. The
#: wait is interruptible, so this is not the latency of a disarm; it is the
#: upper bound on how late the kill lands after the deadline.
WATCHDOG_POLL_SECONDS = 0.02

#: How long the deadline may wait for an arming that has not been confirmed or
#: abandoned yet. Reached only when the acceptance append is itself stalled, and
#: bounded because a watchdog that waits indefinitely is not a watchdog.
DECISION_GRACE_SECONDS = 0.5

#: How long escalation waits for the background evidence sample, *after* the
#: kill has already been issued. A sample that has not arrived by now is
#: reported as unavailable, which blocks continuation rather than delaying it.
EVIDENCE_WAIT_SECONDS = 2.0

#: How long escalation waits for the turn's owner to report the worker reaped
#: and its transport released. Nothing is retried when this elapses: the
#: confirmation simply records that the predecessor was not collected.
COLLECTION_WAIT_SECONDS = 5.0

#: The reason recorded on the abort signal, and the one a turn's own accounting
#: reads back to tell a redirect from a stop, an abort, or a timeout.
REASON_REDIRECT = "redirect"

Phase = Literal["idle", "armed", "confirmed", "abandoned", "escalated", "released"]
PHASE_IDLE: Phase = "idle"
PHASE_ARMED: Phase = "armed"
PHASE_CONFIRMED: Phase = "confirmed"
PHASE_ABANDONED: Phase = "abandoned"
PHASE_ESCALATED: Phase = "escalated"
PHASE_RELEASED: Phase = "released"

#: Why an armed watchdog reached its deadline and stopped nothing.
SKIP_ABANDONED = "acceptance-abandoned"
SKIP_UNDECIDED = "acceptance-undecided"
SKIP_TURN_ENDED = "turn-already-ended"
SKIP_DISARMED = "disarmed"

#: Why a successor was refused. The first two are read off a report that exists
#: and says the predecessor is still around; the last two are the *absence* of a
#: report, which is a refusal in its own right once an acceptance is durable --
#: an escalation that has not published cannot have proved anything.
BLOCK_UNCOLLECTED = "predecessor-uncollected"
BLOCK_UNCONFIRMED = "termination-unconfirmed"
BLOCK_UNREPORTED = "termination-unreported"
BLOCK_FAILED = "termination-failed"


class CancellableTurn(Protocol):
    """The part of a live turn's cancellation seam the controller touches.

    Structurally :class:`~agent_session_loop.harnesses.AbortSignal`, named as a
    protocol so this module does not import the harness layer for one type. The
    three members are chosen for what they promise about blocking: ``arm``
    returns as soon as the request is recorded and takes only that signal's own
    leaf lock, and ``completed`` is set by the boundary that owns the process,
    after the process is reaped — which is what makes it usable as the "the
    predecessor has been collected" fact rather than merely "a kill was sent".
    """

    def arm(self, reason: str, *, grace_seconds: float = 0.0) -> None: ...

    @property
    def completed(self) -> bool: ...

    def wait_completed(self, timeout: float | None = None) -> bool: ...


class InterruptibleTransport(Protocol):
    """The one call the controller makes into a harness's own protocol.

    Structurally :class:`~agent_session_loop.harnesses.HarnessTransport`. It is
    never awaited: an app-server that records its turn as *interrupted* rather
    than as one that stopped answering is worth asking for, and worth nothing at
    all if asking can delay the kill.
    """

    def interrupt(self) -> bool: ...


@dataclass(frozen=True)
class OwnedWorker:
    """The process group this controller is permitted to force-stop.

    Ownership is stated rather than discovered. A controller that looked a pgid
    up at the deadline would be reading the process table on the one path that
    must not read it, and would be aiming at whatever that number means by then;
    stating it at spawn time binds the kill to the process this run started.
    """

    pid: int
    pgid: int

    @classmethod
    def session_leader(cls, pid: int) -> OwnedWorker:
        """The worker of a turn spawned with ``start_new_session=True``.

        Such a child is its own process group leader, so its pgid is its pid.
        Every harness turn spawns that way; this constructor names the reason
        rather than leaving the equality looking like a coincidence.
        """
        return cls(pid=pid, pgid=pid)

    def to_payload(self) -> dict[str, Any]:
        return {"pid": self.pid, "pgid": self.pgid}


@dataclass(frozen=True)
class TerminationTools:
    """The four process operations escalation performs, injectable as one.

    Grouped rather than passed separately because their *order* is the contract
    — collect while alive, kill without probing, clean up after, confirm last —
    and a test that could substitute one of them alone would be able to verify
    an ordering the production path does not have.
    """

    collect_evidence: Callable[..., dict[str, Any]] = (
        process_tree.collect_descendant_evidence
    )
    force_stop: Callable[..., dict[str, Any]] = process_tree.force_stop_process_group
    terminate_escapees: Callable[..., dict[str, Any]] = (
        process_tree.terminate_escaped_descendants
    )
    confirm: Callable[..., dict[str, Any]] = process_tree.confirm_termination


@dataclass(frozen=True)
class TerminationReport:
    """What escalation did, and whether a successor may now run.

    ``forced`` distinguishes a worker this controller killed from one that
    honored the native interrupt and ended inside the window; ``collected``
    reports whether the turn's owner ever said the process was reaped. Both are
    separate from ``complete``, because "we killed it", "someone waited on it"
    and "nothing of it is still running" are three different claims and only the
    third one licenses a successor.
    """

    operation_id: str | None
    worker: OwnedWorker
    reason: str
    deadline: float
    escalated_at: float
    forced: bool
    collected: bool
    skipped: str | None = None
    group_stop: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    cleanup: Mapping[str, Any] = field(default_factory=dict)
    confirmation: Mapping[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Whether the worker and every tracked descendant is confirmed gone."""
        return bool(self.confirmation.get("complete")) and self.collected

    @property
    def permits_continuation(self) -> bool:
        """Whether a successor turn may start. Never true on a bare kill."""
        return self.complete

    @property
    def blocking_reason(self) -> str | None:
        """Why continuation is blocked, or ``None`` when it is not.

        An uncollected predecessor is reported as such even when every probe
        says its processes are gone, because a caller that has not waited on the
        child has not finished with it: its pipes, its transport and its pid are
        all still the previous turn's.
        """
        if self.complete:
            return None
        if not self.collected:
            return BLOCK_UNCOLLECTED
        reason = self.confirmation.get("incomplete_reason")
        return str(reason) if reason else BLOCK_UNCONFIRMED

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "worker": self.worker.to_payload(),
            "reason": self.reason,
            "deadline": round(self.deadline, 3),
            "escalated_at": round(self.escalated_at, 3),
            "escalated_after_seconds": round(self.escalated_at - self.deadline, 3),
            "forced": self.forced,
            "collected": self.collected,
            "skipped": self.skipped,
            "complete": self.complete,
            "permits_continuation": self.permits_continuation,
            "blocking_reason": self.blocking_reason,
            "group_stop": dict(self.group_stop),
            "evidence": _evidence_summary(self.evidence),
            "cleanup": dict(self.cleanup),
            "confirmation": dict(self.confirmation),
        }


def _evidence_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Project the evidence without its command strings.

    A descendant's command line is a worker's argv, which can carry a prompt
    path, a model name, or an operator's own shell. The projection keeps the
    counts and the pids a reader needs and leaves the text in the sample.
    """
    entries = evidence.get("entries") or ()
    return {
        "root_pid": evidence.get("root_pid"),
        "root_pgid": evidence.get("root_pgid"),
        "collected": bool(evidence.get("collected")),
        "root_absent": bool(evidence.get("root_absent")),
        "descendant_count": len(tuple(entries)),
        "escaped_pids": list(evidence.get("escaped_pids") or ()),
        "disabled": evidence.get("disabled"),
        "sample_error": evidence.get("sample_error"),
    }


class SuccessorBlockedError(Exception):
    """Raised when a successor turn would run beside a live or unreaped worker.

    Carries the report rather than only a reason, because the caller that has to
    decide between waiting, pausing and asking an operator needs to know which
    of the three blocking facts it is looking at.
    """

    def __init__(self, reason: str, report: TerminationReport | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.report = report

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "report": self.report.to_payload() if self.report is not None else None,
        }


class UrgentController:
    """One turn's escalation clock, owned by whoever owns that turn's process.

    Constructed per turn, armed at most once per accepted correction, and
    released when the turn ends. Every injectable seam exists so the escalation
    can be driven without real time and without real processes: ``monotonic`` is
    the clock the deadline is measured on, ``tools`` are the four process
    operations, and ``spawn`` is how the watchdog and the fire-and-forget
    protocol call reach a thread.
    """

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

    # -- state ---------------------------------------------------------------

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

    # -- arming --------------------------------------------------------------

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

    # -- acceptance ----------------------------------------------------------

    def accept(
        self,
        plan_dir: str | Path,
        operation_id: str,
        *,
        target: redirect_store.RedirectTarget,
        state_dir: str | Path | None = None,
        reason: str = REASON_REDIRECT,
    ) -> redirect_store.RedirectRequest:
        """Arm, record acceptance, and start cancelling — in that order.

        The order is the contract. Arming first means the escalation is running
        before the journal says "accepted", so there is no window in which a
        producer has been told its correction landed while nothing is counting
        down. Recording before cancelling means a turn is never interrupted for
        a correction the store refused — the refusals it can still raise here
        are expiry and a target that moved, and both mean the correction is not
        this turn's to act on.

        Raises :class:`~agent_session_loop.redirect_store.RedirectStoreError`
        exactly as the store does, with the watchdog already stood down.
        """
        self.arm(operation_id=operation_id, reason=reason)
        try:
            request = redirect_admission.accept_redirect(
                plan_dir, operation_id, target=target, state_dir=state_dir
            )
        except redirect_store.RedirectStoreError:
            self.abandon(SKIP_ABANDONED)
            raise
        self.confirm(operation_id=request.operation_id)
        return request

    # -- continuation gate ---------------------------------------------------

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
        return report

    # -- internals -----------------------------------------------------------

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
