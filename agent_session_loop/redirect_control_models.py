"""Urgent-redirect phases, worker identity, termination reports, and tool seams."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from . import process_tree, redirect_admission

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
