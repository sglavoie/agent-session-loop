"""Mailbox error, message, counts, lease, and wait-completion records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mailbox_constants import MAILBOX_LEASE_VERSION, MAILBOX_SCHEMA_VERSION
from .mailbox_vocabulary import (
    KIND_SUPERVISION_END,
    DeliveryMode,
    GuidanceDeliveryChannel,
    GuidanceDeliveryReason,
    GuidanceDeliveryState,
    MessageKind,
    ResolutionAction,
    SupervisionOutcome,
    WaitEndReason,
    WaiterOrigin,
)


class MailboxError(Exception):
    """A stable mailbox operation failure suitable for CLI translation."""

    def __init__(
        self,
        reason: str,
        detail: str | None = None,
        *,
        path: Path | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.path = path
        message = reason if detail is None else f"{reason}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class MailboxMessage:
    """One validated mailbox record."""

    seq: int
    id: str
    kind: MessageKind
    created_at: str
    text: str | None = None
    to: str | None = None
    upto: int | None = None
    run_id: str | None = None
    session_id: str | None = None
    turn_index: int | None = None
    waiter_origin: WaiterOrigin | None = None
    action: ResolutionAction | None = None
    note: str | None = None
    delivery_mode: DeliveryMode | None = None
    wait_id: str | None = None
    wait_end_reason: WaitEndReason | None = None
    closure_id: str | None = None
    supervision_outcome: SupervisionOutcome | None = None
    supervision_id: str | None = None
    # ``guidance-delivery`` fields. ``delivery_reason`` is deliberately not
    # spelled ``reason``: that on-disk key already belongs to ``wait-end``.
    delivery_state: GuidanceDeliveryState | None = None
    delivery_reason: GuidanceDeliveryReason | None = None
    channel: GuidanceDeliveryChannel | None = None
    harness: str | None = None
    version: int = MAILBOX_SCHEMA_VERSION

    def to_record(self) -> dict[str, Any]:
        """Return the append-only JSON representation (without ``seq``)."""
        payload: dict[str, Any] = {
            "created_at": self.created_at,
            "id": self.id,
            "kind": self.kind,
            "version": self.version,
        }
        if self.text is not None:
            payload["text"] = self.text
        if self.to is not None:
            payload["to"] = self.to
        if self.upto is not None:
            payload["upto"] = self.upto
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.turn_index is not None:
            payload["turn_index"] = self.turn_index
        if self.waiter_origin is not None:
            payload["waiter_origin"] = self.waiter_origin
        if self.action is not None:
            payload["action"] = self.action
        if self.note is not None:
            payload["note"] = self.note
        if self.delivery_mode is not None:
            payload["delivery_mode"] = self.delivery_mode
        if self.wait_id is not None:
            payload["wait_id"] = self.wait_id
        if self.wait_end_reason is not None:
            payload["reason"] = self.wait_end_reason
        if self.closure_id is not None:
            payload["closure_id"] = self.closure_id
        if self.delivery_state is not None:
            payload["delivery_state"] = self.delivery_state
        if self.delivery_reason is not None:
            payload["delivery_reason"] = self.delivery_reason
        if self.channel is not None:
            payload["channel"] = self.channel
        if self.harness is not None:
            payload["harness"] = self.harness
        if self.kind == KIND_SUPERVISION_END:
            if self.supervision_outcome is not None:
                payload["outcome"] = self.supervision_outcome
            if self.supervision_id is not None:
                payload["supervision_id"] = self.supervision_id
        return payload

    def to_payload(self) -> dict[str, Any]:
        payload = self.to_record()
        payload["seq"] = self.seq
        return payload


@dataclass(frozen=True)
class RejectedMailboxRecord:
    """One physical mailbox line that failed validation."""

    seq: int
    reason: str
    raw: str
    detail: str | None = None

    @property
    def line_number(self) -> int:
        """Compatibility name for other repository JSONL rejection records."""
        return self.seq

    def to_payload(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "raw": self.raw,
            "reason": self.reason,
            "seq": self.seq,
        }


@dataclass(frozen=True)
class MailboxCounts:
    """Summary counts derived from one immutable mailbox snapshot."""

    guidance: int
    questions: int
    replies: int
    resolutions: int
    notices: int
    acknowledgements: int
    wait_starts: int
    wait_ends: int
    receipts: int
    supervision_ends: int
    guidance_deliveries: int
    pending_guidance: int
    deliverable_guidance: int
    unanswered_questions: int
    rejected: int

    def to_payload(self) -> dict[str, int]:
        payload = {
            "acknowledgements": self.acknowledgements,
            "guidance": self.guidance,
            "pending_guidance": self.pending_guidance,
            "questions": self.questions,
            "rejected": self.rejected,
            "replies": self.replies,
            "resolutions": self.resolutions,
            "notices": self.notices,
            "unanswered_questions": self.unanswered_questions,
        }
        # Preserve the exact legacy summary shape for logs without v3
        # lifecycle activity while exposing additive counts as soon as they
        # are meaningful.
        if self.wait_starts or self.wait_ends or self.receipts or self.supervision_ends:
            payload.update(
                wait_starts=self.wait_starts,
                wait_ends=self.wait_ends,
                receipts=self.receipts,
                supervision_ends=self.supervision_ends,
            )
        # The delivery ledger is reported on the same terms: additive, and only
        # once a mailbox actually carries one.
        if self.guidance_deliveries:
            payload.update(
                guidance_deliveries=self.guidance_deliveries,
                deliverable_guidance=self.deliverable_guidance,
            )
        return payload


@dataclass(frozen=True)
class MailboxLease:
    """One validated supervisor lease record."""

    supervisor_id: str
    updated_at: str
    run_id: str | None = None
    projection_seq: int | None = None
    unanswered_questions: int | None = None
    latest_unanswered_question_id: str | None = None
    latest_unanswered_question_created_at: str | None = None
    version: int = MAILBOX_LEASE_VERSION

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "supervisor_id": self.supervisor_id,
            "updated_at": self.updated_at,
            "version": self.version,
        }
        if self.version >= 2 and self.run_id is not None:
            payload["run_id"] = self.run_id
            payload["projection_seq"] = self.projection_seq
            payload["unanswered_questions"] = self.unanswered_questions
            if self.latest_unanswered_question_id is not None:
                payload["latest_unanswered_question_id"] = (
                    self.latest_unanswered_question_id
                )
                payload["latest_unanswered_question_created_at"] = (
                    self.latest_unanswered_question_created_at
                )
        return payload


@dataclass(frozen=True)
class MailboxLeaseView:
    """Best-effort lease state; malformed records are represented, not raised."""

    lease_path: Path
    lease: MailboxLease | None
    fresh: bool
    reason: str | None = None

    @property
    def supervisor_id(self) -> str | None:
        return self.lease.supervisor_id if self.lease is not None else None

    @property
    def updated_at(self) -> str | None:
        return self.lease.updated_at if self.lease is not None else None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fresh": self.fresh,
            "lease_path": str(self.lease_path),
            "reason": self.reason,
            "supervisor_id": self.supervisor_id,
            "updated_at": self.updated_at,
            "version": (self.lease.version if self.lease is not None else None),
        }
        if self.lease is not None:
            payload.update(
                {
                    "latest_unanswered_question_created_at": (
                        self.lease.latest_unanswered_question_created_at
                    ),
                    "latest_unanswered_question_id": (
                        self.lease.latest_unanswered_question_id
                    ),
                    "projection_seq": self.lease.projection_seq,
                    "run_id": self.lease.run_id,
                    "unanswered_questions": self.lease.unanswered_questions,
                }
            )
        return payload


@dataclass(frozen=True)
class WaitCompletion:
    """Atomic result of ending a v3 wait or consuming its closure."""

    question: MailboxMessage
    wait_start: MailboxMessage
    wait_end: MailboxMessage
    closure: MailboxMessage | None = None
    receipt: MailboxMessage | None = None

    @property
    def consumed(self) -> bool:
        return self.receipt is not None
