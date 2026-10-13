"""Result and projection records passed between the mailbox command modules."""

from __future__ import annotations

from dataclasses import dataclass

from . import (
    mailbox_models,
    mailbox_snapshot,
)


@dataclass(frozen=True)
class LeaseSupervision:
    degraded: bool = False
    renewal_failures: int = 0
    renewal_recoveries: int = 0

    def to_payload(self) -> dict[str, bool | int]:
        return {
            "degraded": self.degraded,
            "renewal_failures": self.renewal_failures,
            "renewal_recoveries": self.renewal_recoveries,
        }


@dataclass(frozen=True)
class OutboxWaitResult:
    questions: tuple[mailbox_models.MailboxMessage, ...]
    notices: tuple[mailbox_models.MailboxMessage, ...]
    supervision_ends: tuple[mailbox_models.MailboxMessage, ...]
    include: str
    supervisor_id: str
    next_since: int
    timed_out: bool
    lease_supervision: LeaseSupervision
    mailbox_view: mailbox_snapshot.MailboxView | None = None
    lost_reason: str | None = None


@dataclass(frozen=True)
class MailboxReplyProjection:
    """Authoritative pre-reply evidence for one supervisor question."""

    run_id: str
    projection_seq: int
    question_id: str
    lease_predates_question: bool = False


@dataclass(frozen=True)
class MailboxQuestionProjection:
    """Body-free actionable question set surfaced by one outbox wait."""

    run_id: str
    projection_seq: int
    open_questions: tuple[mailbox_models.MailboxMessage, ...]
    latest_question: mailbox_models.MailboxMessage
