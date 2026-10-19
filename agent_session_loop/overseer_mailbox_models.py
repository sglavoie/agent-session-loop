"""Mailbox operation vocabulary, posting outcomes, and fleet poll records."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from . import mailbox_constants, overseer_decisions, overseer_dispatch

Operation = overseer_decisions.Operation

#: The operations this module implements. A strict subset of the operational
#: scope: the other three are somebody else's adapters.
MAIL_OPERATIONS: tuple[Operation, ...] = ("reply", "guidance")

State = Literal["queued", "accepted", "delivered", "consumed", "rejected"]

#: What became of one posting, ordered from least to most carried. ``queued``
#: and ``accepted`` are outstanding; ``delivered`` and ``consumed`` are the two
#: kinds of arrival evidence the mailbox actually records.
STATES: tuple[State, ...] = (
    "queued",
    "accepted",
    "delivered",
    "consumed",
    "rejected",
)

Refusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "question_unknown",
    "question_closed",
    "question_stale",
    "mailbox_full",
    "mailbox_unreadable",
    "record_rejected",
]

#: Why a posting did not happen. Closed, and deliberately separate from the
#: dispatcher's refusals: these are what an adapter reports about the mailbox,
#: not what the single door decided about permission.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "question_unknown",
    "question_closed",
    "question_stale",
    "mailbox_full",
    "mailbox_unreadable",
    "record_rejected",
)

#: The ledger observation stream this module's rotation cursor lives in. Its own
#: stream rather than the watcher's, so polling and enrolling rotate
#: independently and neither starves the other.
MAILBOX_STREAM = "mailbox"

#: The UUID5 namespace posting identities are derived in. Fixed, because an id
#: that changed between builds would make every pre-upgrade row unreconcilable.
_ID_NAMESPACE = uuid.UUID("6f4a1d38-5f2e-5a63-9a2d-2b0f0f1a7c11")

#: How the mailbox's own rejection words map onto :data:`REFUSALS`. A reason
#: absent from here is ``record_rejected``; ``duplicate-id`` and
#: ``mailbox-write-error`` are not refusals at all and are handled separately.
_REFUSAL_FOR_REASON: dict[str, Refusal] = {
    mailbox_constants.REJECT_QUESTION_NOT_FOUND: "question_unknown",
    mailbox_constants.REJECT_QUESTION_ALREADY_ANSWERED: "question_closed",
    mailbox_constants.REJECT_QUESTION_ALREADY_RESOLVED: "question_closed",
    mailbox_constants.REJECT_QUESTION_NOT_OPEN: "question_stale",
    mailbox_constants.REJECT_QUESTION_NOT_WAITING: "question_stale",
    mailbox_constants.ERROR_MAILBOX_FULL: "mailbox_full",
    mailbox_constants.ERROR_MAILBOX_READ: "mailbox_unreadable",
}


class PostingUncertain(Exception):
    """A posting whose outcome this module refuses to guess.

    Raised only when the mailbox could neither be written nor re-read, so
    "it landed" and "it did not" are both unsupported. The dispatcher turns
    this into ``outcome_uncertain`` and leaves the ledger row attempted, which
    is what :func:`inspect_posting` is later able to settle.
    """

    def __init__(self, detail: str, *, message_id: str = "") -> None:
        self.detail = detail
        self.message_id = message_id
        super().__init__(detail)


@dataclass(frozen=True)
class Posting:
    """One mailbox operation, and how far it has actually got.

    ``mailbox_state`` is the mailbox's own vocabulary for the same fact --
    ``queued``, ``deferred``, ``delivered-live``, ``accepted``, ``consumed`` --
    kept next to :attr:`state` rather than replaced by it, because the
    projection loses the difference between guidance nobody tried to deliver
    and guidance a delivery attempt deferred.
    """

    operation: Operation
    plan_dir: str
    incident_id: int | None
    target: str
    message_id: str
    state: State
    mailbox_state: str = ""
    refusal: Refusal | None = None
    detail: str = ""
    seq: int = 0
    created_at: str = ""

    @property
    def landed(self) -> bool:
        """Whether a record for this posting is in the mailbox."""
        return self.state != "rejected"

    @property
    def outstanding(self) -> bool:
        """Whether the worker has not yet been shown to have received it."""
        return self.state in ("queued", "accepted")

    @property
    def arrived(self) -> bool:
        """Whether the mailbox holds evidence the worker received it."""
        return self.state in ("delivered", "consumed")


@dataclass(frozen=True)
class PlanPoll:
    """One non-blocking look at one plan's mailbox.

    ``read`` false means the mailbox could not be read this pass. It is kept
    apart from an empty ``postings`` because a plan with nothing outstanding
    and a plan nobody could look at are opposite facts.
    """

    plan_dir: str
    read: bool
    postings: tuple[Posting, ...] = ()
    last_seq: int = 0
    detail: str = ""

    @property
    def outstanding(self) -> tuple[Posting, ...]:
        """Postings still waiting on the worker."""
        return tuple(posting for posting in self.postings if posting.outstanding)

    @property
    def arrived(self) -> tuple[Posting, ...]:
        """Postings the mailbox says reached the worker."""
        return tuple(posting for posting in self.postings if posting.arrived)


@dataclass(frozen=True)
class FleetPoll:
    """One pass over many plans, and the ones it did not reach."""

    polls: tuple[PlanPoll, ...] = ()
    deferred: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether every plan offered was actually polled."""
        return not self.deferred

    @property
    def outstanding(self) -> tuple[Posting, ...]:
        """Every posting still waiting, across every plan polled."""
        return tuple(posting for poll in self.polls for posting in poll.outstanding)


Authority = Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None]
