"""What an interrupted redirect means once the process holding it is gone.

Every earlier module in this subsystem assumes a live runner. Submission waits
on one, acceptance is a statement by one, escalation is a deadline inside one,
and delivery is a prompt one builds. A crash removes that process at an
arbitrary instant, and the instant it removes it decides what the operator is
owed. Five boundaries matter, and the journal distinguishes all five because
nothing in it is ever rewritten:

- before acceptance: a ``submitted`` line and nothing after it. Nobody promised
  the operator anything except that the correction was written down;
- after acceptance, before delivery: ``accepted`` with no ``continued``. A
  process undertook to stop a worker and hand this correction to its successor,
  and then stopped existing. Nobody else has taken that on;
- after delivery, before receipt: ``continued`` with no ``acknowledged``. Some
  attempt took delivery. Whether a model read it is exactly the question the
  acknowledgment protocol exists because nothing cheaper answers;
- after receipt: ``acknowledged``. Settled in the only sense that counts;
- the torn tail: a line that was being written when the machine stopped. Replay
  refuses it and the next append terminates it, but a projection built in
  between has to say that one record may be missing rather than presenting a
  short history as a whole one.

Two rules shape everything here.

**Reading never starts anything.** :func:`inspect_plan_recovery` opens the
journal and the ledger read-only, classifies the lock without reclaiming it, and
touches no FIFO, no process, and no plan file. An operator asking what happened
to a correction must not be the reason a worker starts, and a status command
that spawned one would make "check before acting" the dangerous option.

**Ambiguity is reported, never resolved by guessing.** The one case this module
refuses to decide is delivery without receipt. Re-handing the correction to a
new worker would tell it the operator's first correction is new; dropping it
would discard a correction that may never have been read. Both are worse than
saying so, so those ids come back as :data:`STATUS_UNCERTAIN` and nothing here
acts on them -- not by default and not under a flag. The operator who looks and
decides it was never read says so by submitting the correction again, in words
chosen knowing what happened.

The mutating half, :func:`reconcile_startup`, runs under the new run's own lock
and follows the shape ``state.migrate_plan_state`` established for relocation:
no mutation until the lock evidence agrees. A plan another live process owns is
refused outright rather than reconciled optimistically, because a second
reconciler settling requests the first is still answering is how a correction
gets two contradictory outcomes.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import clock, conversations, redirect_store, state

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

OperationStatus = Literal[
    "submitted",
    "unanswered",
    "accepted",
    "delivered",
    "acknowledged",
    "rejected",
    "expired",
    "uncertain",
]

#: Persisted, unanswered, and a live run still owns the plan: the ordinary
#: in-flight case, not a recovery concern.
STATUS_SUBMITTED: OperationStatus = "submitted"
#: Persisted and unanswered, but the run that could have answered it is gone.
STATUS_UNANSWERED: OperationStatus = "unanswered"
#: A runner undertook to act on it and did not record delivery.
STATUS_ACCEPTED: OperationStatus = "accepted"
#: An attempt took delivery; no worker has receipted it.
STATUS_DELIVERED: OperationStatus = "delivered"
#: A worker running the carrying attempt receipted it.
STATUS_ACKNOWLEDGED: OperationStatus = "acknowledged"
#: Settled without being acted on.
STATUS_REJECTED: OperationStatus = "rejected"
#: Its acceptance window closed.
STATUS_EXPIRED: OperationStatus = "expired"
#: The durable evidence does not determine an answer. Reported, never guessed.
STATUS_UNCERTAIN: OperationStatus = "uncertain"

OPERATION_STATUSES: tuple[OperationStatus, ...] = (
    STATUS_SUBMITTED,
    STATUS_UNANSWERED,
    STATUS_ACCEPTED,
    STATUS_DELIVERED,
    STATUS_ACKNOWLEDGED,
    STATUS_REJECTED,
    STATUS_EXPIRED,
    STATUS_UNCERTAIN,
)

#: Statuses naming a correction nothing is going to act on unless something
#: picks it up. Kept as its own set because "unfinished" is the question a
#: recovering run asks, and it is not the same as "not terminal".
UNFINISHED_STATUSES: frozenset[OperationStatus] = frozenset(
    {STATUS_UNANSWERED, STATUS_ACCEPTED}
)

OwnerLiveness = Literal["live", "gone", "unknown"]

#: A run holds this plan's lock and the lock reads as active.
OWNER_LIVE: OwnerLiveness = "live"
#: No lock, or a lock this host has proven dead.
OWNER_GONE: OwnerLiveness = "gone"
#: A lock whose owner this host cannot evaluate -- another machine's.
OWNER_UNKNOWN: OwnerLiveness = "unknown"

#: Reasons :func:`reconcile_startup` declines to touch the journal. Stable
#: strings: the run loop puts them in an event and the workbench reads them.
BLOCKED_FOREIGN_OWNER = "foreign-lock-owner"
BLOCKED_LIVE_OWNER = "live-lock-owner"
BLOCKED_JOURNAL_UNREADABLE = "redirect-journal-unreadable"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerEvidence:
    """Who, if anyone, is still answerable for this plan's redirects.

    Derived from the lock alone. No signal is sent, no process table is read,
    and no endpoint is opened: this object exists to be safe to build from a
    read-only command, and every cheaper-looking probe has a side effect on the
    thing it probes.
    """

    liveness: OwnerLiveness
    detail: str
    pid: int | None = None
    run_id: str | None = None
    redirect_endpoint: str | None = None

    @property
    def answerable(self) -> bool:
        """Whether some process may still act on a correction in this journal.

        ``unknown`` counts as answerable. A lock belonging to another host may
        name a perfectly healthy run, and treating what we cannot evaluate as
        dead is how two runners end up reconciling the same journal.
        """
        return self.liveness in (OWNER_LIVE, OWNER_UNKNOWN)

    def to_payload(self) -> dict[str, Any]:
        return {
            "liveness": self.liveness,
            "detail": self.detail,
            "pid": self.pid,
            "run_id": self.run_id,
            "redirect_endpoint": self.redirect_endpoint,
        }


def inspect_owner(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> OwnerEvidence:
    """Classify the plan's current redirect owner without changing anything.

    ``state.inspect_lock_info`` rather than ``read_lock_info``: the reclaiming
    read deletes a lock this host has proven dead, which is right for a run
    about to write and wrong for a question about what happened.
    """
    try:
        lock = state.inspect_lock_info(plan_dir, state_dir)
    except (OSError, ValueError) as error:
        # An unreadable lock is not an absent one. Failing closed here keeps a
        # transient stat error from being read as "nobody owns this plan".
        return OwnerEvidence(liveness=OWNER_UNKNOWN, detail=f"lock-unreadable: {error}")
    if lock is None:
        return OwnerEvidence(liveness=OWNER_GONE, detail="no-lock")
    if lock.foreign_host:
        return OwnerEvidence(
            liveness=OWNER_UNKNOWN,
            detail="foreign-host",
            pid=lock.pid,
            run_id=_lock_run_id(lock),
            redirect_endpoint=lock.redirect_endpoint,
        )
    return OwnerEvidence(
        liveness=OWNER_LIVE if lock.active else OWNER_GONE,
        detail=lock.reason,
        pid=lock.pid,
        run_id=_lock_run_id(lock),
        redirect_endpoint=lock.redirect_endpoint,
    )


def _lock_run_id(lock: state.LockInfo) -> str | None:
    raw = lock.raw.get("run_id")
    return raw if isinstance(raw, str) and raw else None


@dataclass(frozen=True)
class ConversationEvidence:
    """What the conversation ledger can still say about this plan.

    Redirect recovery needs one fact from session 1's ledger and no more:
    whether a conversation can be selected at all. An untrustworthy ledger --
    one with a line nothing could attribute -- already forces
    ``conversation-recovery-required`` before a turn runs, so a correction this
    module would otherwise hand to that turn has nowhere to land yet. Reporting
    it here is what keeps the two recoveries from contradicting each other.
    """

    readable: bool
    trustworthy: bool
    records: int = 0
    unreadable_lines: int = 0
    detail: str | None = None

    @property
    def usable(self) -> bool:
        return self.readable and self.trustworthy

    def to_payload(self) -> dict[str, Any]:
        return {
            "readable": self.readable,
            "trustworthy": self.trustworthy,
            "records": self.records,
            "unreadable_lines": self.unreadable_lines,
            "detail": self.detail,
        }


def inspect_conversations(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> ConversationEvidence:
    """Read the ledger's health, never its contents, and never write to it."""
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    try:
        ledger = conversations.read_conversations(plan_state)
    except (conversations.ConversationError, OSError) as error:
        return ConversationEvidence(
            readable=False, trustworthy=False, detail=str(error)
        )
    return ConversationEvidence(
        readable=True,
        trustworthy=ledger.trustworthy,
        records=len(ledger.records),
        unreadable_lines=len(ledger.rejected),
    )


# ---------------------------------------------------------------------------
# Status projection
# ---------------------------------------------------------------------------


def operation_status(
    request: redirect_store.RedirectRequest,
    *,
    owner: OwnerEvidence,
    now: datetime | None = None,
) -> OperationStatus:
    """Project one request's status from durable evidence alone.

    A pure function of the request, the lock classification, and the clock. It
    reads nothing, so the same inputs always give the same answer and a caller
    can hold several of these side by side without the earlier ones going stale
    underneath it.

    The distinction this exists for is ``submitted`` against ``unanswered``.
    They are the same journal state; what separates them is whether any process
    is still in a position to answer. An operator looking at ``submitted`` is
    waiting; an operator looking at ``unanswered`` is looking at a correction
    that will never be answered unless something picks it up.
    """
    moment = clock.utc_now() if now is None else now
    if request.state == redirect_store.STATE_REJECTED:
        return STATUS_REJECTED
    if request.state == redirect_store.STATE_EXPIRED:
        return STATUS_EXPIRED
    if request.accepted:
        if request.acknowledged:
            return STATUS_ACKNOWLEDGED
        if request.carried:
            # Delivered and unreceipted. Not a state to be resolved by looking
            # harder at this journal: what is unknown is what a worker read,
            # and the journal records only what was handed to it.
            return STATUS_UNCERTAIN
        return STATUS_ACCEPTED
    if request.is_expired_at(moment):
        # Past its window but not yet settled as such. Reported as expired
        # because that is what the next writer will record, and reporting it as
        # open would invite an operator to wait for an answer already refused.
        return STATUS_EXPIRED
    return STATUS_SUBMITTED if owner.answerable else STATUS_UNANSWERED


@dataclass(frozen=True)
class OperationOutcome:
    """One request as recovery understands it: identity, status, and evidence."""

    operation_id: str
    status: OperationStatus
    target: redirect_store.RedirectTarget
    journal_state: str
    feedback_digest: str
    feedback_bytes: int
    created_at: str
    expires_at: str
    continuation: redirect_store.RedirectTarget | None = None
    continuation_recovered: bool = False
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "target": self.target.to_payload(),
            "journal_state": self.journal_state,
            # Digest and byte count, exactly as the journal reports them. The
            # operator's words stay behind ``redirect_store.read_feedback``.
            "feedback_digest": self.feedback_digest,
            "feedback_bytes": self.feedback_bytes,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "continuation": (
                self.continuation.to_payload()
                if self.continuation is not None
                else None
            ),
            "continuation_recovered": self.continuation_recovered,
            "reason": self.reason,
        }


def _outcome(
    request: redirect_store.RedirectRequest,
    status: OperationStatus,
) -> OperationOutcome:
    return OperationOutcome(
        operation_id=request.operation_id,
        status=status,
        target=request.target,
        journal_state=request.state,
        feedback_digest=request.feedback_digest,
        feedback_bytes=request.feedback_bytes,
        created_at=request.created_at,
        expires_at=request.expires_at,
        continuation=request.continuation,
        continuation_recovered=request.continuation_recovered,
        reason=request.reason,
    )


@dataclass(frozen=True)
class RecoveryReport:
    """Everything a read of this plan's redirect state can establish.

    ``complete`` is the honesty flag. A torn tail or a line replay could not
    attribute means some record is not in this answer, and a caller that treats
    a partial projection as exhaustive is the caller that decides a correction
    never existed.
    """

    plan_dir: str
    journal_path: str
    owner: OwnerEvidence
    conversations: ConversationEvidence
    operations: tuple[OperationOutcome, ...] = ()
    torn_tail: bool = False
    unreadable_lines: int = 0
    checked_at: str = ""

    @property
    def complete(self) -> bool:
        return not self.torn_tail and not self.unreadable_lines

    def with_status(self, *statuses: OperationStatus) -> tuple[OperationOutcome, ...]:
        wanted = frozenset(statuses)
        return tuple(item for item in self.operations if item.status in wanted)

    def operation_ids(self, *statuses: OperationStatus) -> tuple[str, ...]:
        return tuple(item.operation_id for item in self.with_status(*statuses))

    @property
    def unfinished(self) -> tuple[OperationOutcome, ...]:
        """Corrections nothing is answering, oldest first."""
        return self.with_status(*sorted(UNFINISHED_STATUSES))

    @property
    def uncertain(self) -> tuple[OperationOutcome, ...]:
        """Corrections delivered to an attempt that never receipted them."""
        return self.with_status(STATUS_UNCERTAIN)

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": self.plan_dir,
            "journal_path": self.journal_path,
            "owner": self.owner.to_payload(),
            "conversations": self.conversations.to_payload(),
            "operations": [item.to_payload() for item in self.operations],
            "torn_tail": self.torn_tail,
            "unreadable_lines": self.unreadable_lines,
            "complete": self.complete,
            "checked_at": self.checked_at,
        }


def inspect_plan_recovery(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> RecoveryReport:
    """Report this plan's redirect state without writing or starting anything.

    The read every operator-facing surface should use. It appends no journal
    line, settles nothing, opens no redirect endpoint, sends no wakeup, and
    never reclaims the lock it classifies -- so asking the question twice gives
    the same answer, and asking it at all changes nothing about the run it is
    asked about.
    """
    moment = clock.utc_now() if now is None else now
    owner = inspect_owner(plan_dir, state_dir=state_dir)
    ledger = inspect_conversations(plan_dir, state_dir=state_dir)
    view = redirect_store.read_journal(plan_dir, state_dir=state_dir)
    return RecoveryReport(
        plan_dir=str(plan_dir),
        journal_path=str(view.journal_path),
        owner=owner,
        conversations=ledger,
        operations=tuple(
            _outcome(request, operation_status(request, owner=owner, now=moment))
            for request in view.requests
        ),
        torn_tail=view.torn_tail,
        unreadable_lines=len(view.rejected),
        checked_at=clock.isoformat(moment),
    )


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrandedCorrection:
    """One unfinished correction and the session a future attempt could carry it into.

    The session travels with the id rather than beside it in a parallel list:
    the two are read together every time, and a report that could disagree with
    itself about which session an id belongs to would deliver a correction to
    the wrong worker.
    """

    operation_id: str
    session_id: str

    def to_payload(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "session_id": self.session_id}


@dataclass(frozen=True)
class ReconciliationReport:
    """What a starting run did, or declined to do, about a crash's leftovers."""

    plan_dir: str
    run_id: str
    reconciled: bool
    blocked_reason: str | None = None
    expired: tuple[str, ...] = ()
    settled: tuple[str, ...] = ()
    stranded: tuple[StrandedCorrection, ...] = ()
    recoverable: tuple[StrandedCorrection, ...] = ()
    uncertain: tuple[StrandedCorrection, ...] = ()
    torn_tail: bool = False
    unreadable_lines: int = 0
    owner: OwnerEvidence | None = None
    conversations: ConversationEvidence | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    def recoverable_for(self, session_id: str) -> tuple[str, ...]:
        """The ids a new attempt of ``session_id`` may carry, oldest first."""
        return tuple(
            item.operation_id
            for item in self.recoverable
            if item.session_id == session_id
        )

    def uncertain_for(self, session_id: str) -> tuple[str, ...]:
        """The ids delivered to an attempt of ``session_id`` that nobody receipted."""
        return tuple(
            item.operation_id
            for item in self.uncertain
            if item.session_id == session_id
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": self.plan_dir,
            "run_id": self.run_id,
            "reconciled": self.reconciled,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "expired": list(self.expired),
            "settled": list(self.settled),
            "stranded": [item.to_payload() for item in self.stranded],
            "recoverable": [item.to_payload() for item in self.recoverable],
            "uncertain": [item.to_payload() for item in self.uncertain],
            "torn_tail": self.torn_tail,
            "unreadable_lines": self.unreadable_lines,
            "owner": None if self.owner is None else self.owner.to_payload(),
            "conversations": (
                None if self.conversations is None else self.conversations.to_payload()
            ),
        }


def _blocked(
    plan_dir: str | Path,
    run_id: str,
    reason: str,
    owner: OwnerEvidence | None = None,
) -> ReconciliationReport:
    return ReconciliationReport(
        plan_dir=str(plan_dir),
        run_id=run_id,
        reconciled=False,
        blocked_reason=reason,
        owner=owner,
    )


def reconcile_startup(
    plan_dir: str | Path,
    *,
    run_id: str,
    pending_session_ids: Sequence[str] = (),
    state_dir: str | Path | None = None,
    owner_pid: int | None = None,
    now: datetime | None = None,
) -> ReconciliationReport:
    """Settle what a crash stranded, and say what this run may pick up.

    Called once by a starting run, under that run's own lock and before the
    first turn is selected. Three things happen and nothing else does:

    - every request past its window is expired in the journal, so the refusal
      survives this process too;
    - every *unanswered* request of a dead run whose session this plan is not
      going to run again is settled ``rejected``/``run-ended``. That reason is
      written exactly here: it means the process that owed the operator an
      answer ended without giving one, which is a different fact from the
      deadline passing and deserves a different word. One the dead run had
      already *accepted* is reported as ``stranded`` instead, because its answer
      is written and first terminal wins: the journal has no transition for
      "accepted, and then nobody was left to deliver it", and inventing one
      would overwrite the runner's own decision after the fact;
    - everything still unfinished whose session *is* pending is reported as
      recoverable, and every delivered-but-unreceipted request is reported as
      uncertain. Neither is acted on here. Recovery happens when the session is
      actually selected, because only then does a continuation attempt exist to
      carry a correction into.

    ``pending_session_ids`` is supplied by the caller rather than read from the
    plan: this module has no business deciding what a plan is going to run, and
    the run loop already holds that snapshot. An empty sequence therefore means
    "no session remains", which settles every stranded correction -- correct for
    a finished plan and the reason the caller must pass the real list.

    The refusals come first and write nothing. A plan owned by a live process
    other than this one, or by a host we cannot evaluate, is left entirely
    alone: a second reconciler settling what the first is still answering is how
    one correction acquires two outcomes.
    """
    moment = clock.utc_now() if now is None else now
    owner = inspect_owner(plan_dir, state_dir=state_dir)
    if owner.liveness == OWNER_UNKNOWN:
        return _blocked(plan_dir, run_id, BLOCKED_FOREIGN_OWNER, owner)
    if owner.liveness == OWNER_LIVE and owner.pid != _resolved_pid(owner_pid):
        return _blocked(plan_dir, run_id, BLOCKED_LIVE_OWNER, owner)

    try:
        expired = redirect_store.expire_due_requests(
            plan_dir, state_dir=state_dir, now=moment
        )
    except redirect_store.RedirectStoreError:
        # An unreadable or unwritable journal stops reconciliation rather than
        # being worked around. Every answer below is a claim about what the
        # journal says, and a journal that cannot be read supports none of them.
        return _blocked(plan_dir, run_id, BLOCKED_JOURNAL_UNREADABLE, owner)

    ledger = inspect_conversations(plan_dir, state_dir=state_dir)
    view = redirect_store.read_journal(plan_dir, state_dir=state_dir)
    live_sessions = frozenset(pending_session_ids)

    settled: list[str] = []
    stranded: list[StrandedCorrection] = []
    recoverable: list[StrandedCorrection] = []
    for request in view.strandable_by(run_id):
        where = StrandedCorrection(
            operation_id=request.operation_id,
            session_id=request.target.session_id,
        )
        if request.target.session_id in live_sessions:
            recoverable.append(where)
            continue
        if request.terminal:
            # Accepted, and the session it was accepted for will not run again.
            # Settling it would be a second terminal record over the first.
            stranded.append(where)
            continue
        try:
            redirect_store.reject_request(
                plan_dir,
                request.operation_id,
                reason=redirect_store.REASON_RUN_ENDED,
                state_dir=state_dir,
                now=moment,
            )
        except redirect_store.RedirectStoreError:
            # Settled by someone else between the read and here, or a journal
            # that just filled. Either way the request is not this run's to
            # report as settled, and nothing is lost by saying less.
            stranded.append(where)
            continue
        settled.append(request.operation_id)

    uncertain = tuple(
        StrandedCorrection(
            operation_id=request.operation_id,
            session_id=request.target.session_id,
        )
        for request in view.requests
        if request.target.run_id != run_id
        and request.accepted
        and request.carried
        and not request.acknowledged
    )
    return ReconciliationReport(
        plan_dir=str(plan_dir),
        run_id=run_id,
        reconciled=True,
        expired=tuple(request.operation_id for request in expired),
        settled=tuple(settled),
        stranded=tuple(stranded),
        recoverable=tuple(recoverable),
        uncertain=uncertain,
        torn_tail=view.torn_tail,
        unreadable_lines=len(view.rejected),
        owner=owner,
        conversations=ledger,
    )


def _resolved_pid(owner_pid: int | None) -> int:
    return os.getpid() if owner_pid is None else owner_pid


# ---------------------------------------------------------------------------
# Handing recovered corrections to a continuation
# ---------------------------------------------------------------------------


def recoverable_operation_ids(
    report: ReconciliationReport,
    session_id: str,
) -> tuple[str, ...]:
    """The ids an attempt of ``session_id`` may carry across the crash.

    Gated on the conversation ledger being usable. An untrustworthy ledger means
    no conversation can be selected yet, so the turn this would deliver into is
    not going to exist; handing corrections to it would mark them delivered to
    a prompt that was never built. Reporting nothing leaves them recoverable for
    the run that starts after the ledger is repaired, which is the outcome that
    loses nothing.

    The uncertain ids are never here, and there is no flag that adds them. Their
    delivery is already written, so the journal would refuse a second one; and
    the question they raise -- did a model read this before the machine stopped
    -- is not one the journal answers. An operator who decides it did not says
    so by submitting the correction again, which produces a new request with its
    own acceptance and its own words, chosen knowing what happened.
    """
    if report.blocked or not report.reconciled:
        return ()
    if report.conversations is not None and not report.conversations.usable:
        return ()
    return report.recoverable_for(session_id)
