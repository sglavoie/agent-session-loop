"""Read-only status projection for redirect recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_vocab,
)
from .redirect_recovery_evidence import (
    STATUS_ACCEPTED,
    STATUS_ACKNOWLEDGED,
    STATUS_EXPIRED,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_UNANSWERED,
    STATUS_UNCERTAIN,
    UNFINISHED_STATUSES,
    ConversationEvidence,
    OperationStatus,
    OwnerEvidence,
    inspect_conversations,
    inspect_owner,
)

# ---------------------------------------------------------------------------
# Status projection
# ---------------------------------------------------------------------------


def operation_status(
    request: redirect_store_models.RedirectRequest,
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
    if request.state == redirect_store_vocab.STATE_REJECTED:
        return STATUS_REJECTED
    if request.state == redirect_store_vocab.STATE_EXPIRED:
        return STATUS_EXPIRED
    if request.accepted:
        if request.acknowledged:
            return STATUS_ACKNOWLEDGED
        if request.delivered:
            # Launched and unreceipted. Not a state to be resolved by looking
            # harder at this journal: what is unknown is what a worker read,
            # and the journal records only what was handed to it. A version-1
            # carry arrives here too, and for the stronger reason: its writer
            # recorded the carry only after the launch, so the journal cannot
            # even say whether the prompt went out, let alone what was read.
            return STATUS_UNCERTAIN
        if request.launch_ambiguous:
            # The carrying attempt declared the prompt about to go out and never
            # said what happened -- or wrote at a version that had no way to
            # declare it. The unknown here is one step earlier than above: not
            # what a worker read, but whether one was ever handed anything. It
            # lands in the same bucket because the only safe answers are the
            # same two, and both belong to an operator.
            return STATUS_UNCERTAIN
        # A carry with no launch reads as ``accepted`` alongside one that was
        # never carried at all, and the collapse is deliberate. Both say a
        # runner undertook to act and no process ever ran holding the words, so
        # both are owed to the next attempt; keeping them apart here would put a
        # correction nothing saw into the one bucket recovery refuses to touch.
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
    target: redirect_store_models.RedirectTarget
    journal_state: str
    feedback_digest: str
    feedback_bytes: int
    created_at: str
    expires_at: str
    continuation: redirect_store_models.RedirectTarget | None = None
    continuation_recovered: bool = False
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "target": self.target.to_payload(),
            "journal_state": self.journal_state,
            # Digest and byte count, exactly as the journal reports them. The
            # operator's words stay behind ``redirect_store_journal.read_feedback``.
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
    request: redirect_store_models.RedirectRequest,
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
        """Corrections whose fate the journal cannot settle, oldest first.

        Delivered to an attempt that never receipted them, or bound into a
        prompt an attempt declared it was about to send and never accounted for.
        """
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
    view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
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
