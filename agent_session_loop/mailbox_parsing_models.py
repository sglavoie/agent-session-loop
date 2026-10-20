"""Shared state and result types for the mailbox record parser."""

from __future__ import annotations

from dataclasses import dataclass, field

from .mailbox_models import MailboxMessage
from .mailbox_vocabulary import (
    DeliveryMode,
    GuidanceDeliveryChannel,
    GuidanceDeliveryReason,
    GuidanceDeliveryState,
    ResolutionAction,
    SupervisionOutcome,
    WaitEndReason,
    WaiterOrigin,
)


class RecordRejected(Exception):
    """Raised by a record validator to refuse the line being parsed."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class ParseState:
    """Accepted-record indexes the stateful validators consult."""

    seen_ids: set[str] = field(default_factory=set)
    questions: dict[str, MailboxMessage] = field(default_factory=dict)
    closures: dict[str, MailboxMessage] = field(default_factory=dict)
    wait_starts: dict[str, MailboxMessage] = field(default_factory=dict)
    active_waits: dict[str, MailboxMessage] = field(default_factory=dict)
    receipts: dict[str, MailboxMessage] = field(default_factory=dict)
    guidance_records: dict[str, MailboxMessage] = field(default_factory=dict)
    terminal_deliveries: set[tuple[str, str | None, str | None, int | None]] = field(
        default_factory=set
    )


@dataclass
class RecordFields:
    """Kind-specific fields collected while validating one record."""

    text: str | None = None
    target: str | None = None
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
    delivery_state: GuidanceDeliveryState | None = None
    delivery_reason: GuidanceDeliveryReason | None = None
    channel: GuidanceDeliveryChannel | None = None
    harness: str | None = None
