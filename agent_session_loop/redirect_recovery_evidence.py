"""Status vocabulary and owner/ledger evidence for redirect recovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import conversations, state, state_locks

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
#: A runner undertook to act on it and no worker was ever launched holding it.
#: Covers a request never carried and one carried into a prompt that never ran,
#: because they owe the operator the same thing.
STATUS_ACCEPTED: OperationStatus = "accepted"
#: An attempt launched a worker with it; no receipt. Kept in the vocabulary and
#: deliberately never produced: that shape is reported as :data:`STATUS_UNCERTAIN`,
#: since knowing a process was started is not knowing a model read anything.
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
        lock = state_locks.inspect_lock_info(plan_dir, state_dir)
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
