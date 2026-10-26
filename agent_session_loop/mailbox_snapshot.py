"""The parsed, queryable projection of a mailbox log."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mailbox_constants import (
    MAILBOX_CLOSURE_RESERVE_BYTES,
    MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES,
    MAILBOX_MAX_BYTES,
    MAILBOX_MIN_APPEND_BYTES,
    MAILBOX_MIN_GUIDANCE_DELIVERY_BYTES,
    MAILBOX_MIN_REPLY_BYTES,
    MAILBOX_MIN_RESOLUTION_BYTES,
    MAILBOX_RECEIPT_RESERVE_BYTES,
    MAILBOX_WAIT_END_RESERVE_BYTES,
    MAILBOX_WAIT_SCHEMA_VERSION,
)
from .mailbox_models import MailboxCounts, MailboxMessage, RejectedMailboxRecord
from .mailbox_validation import _validate_sequence
from .mailbox_vocabulary import (
    CLOSURE_KINDS,
    DELIVERY_MODE_WAIT,
    GUIDANCE_DELIVERED_STATES,
    GUIDANCE_STATE_DEFERRED,
    GUIDANCE_STATE_DELIVERED_BY_READ,
    GUIDANCE_STATE_QUEUED,
    KIND_ACK,
    KIND_GUIDANCE,
    KIND_GUIDANCE_DELIVERY,
    KIND_NOTICE,
    KIND_QUESTION,
    KIND_RECEIPT,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_SUPERVISION_END,
    KIND_WAIT_END,
    KIND_WAIT_START,
    MESSAGE_KINDS,
    WAITER_ORIGIN_WORKER,
    GuidanceDeliveryState,
    MessageKind,
)


@dataclass(frozen=True)
class MailboxView:
    """A non-destructive, internally consistent mailbox snapshot."""

    mailbox_path: Path
    messages: tuple[MailboxMessage, ...] = ()
    rejected: tuple[RejectedMailboxRecord, ...] = ()
    last_seq: int = 0
    size_bytes: int = 0

    @property
    def max_acknowledged_seq(self) -> int:
        return max(
            (
                message.upto
                for message in self.messages
                if message.kind == KIND_ACK and message.upto is not None
            ),
            default=0,
        )

    @property
    def pending_guidance(self) -> tuple[MailboxMessage, ...]:
        acknowledged = self.max_acknowledged_seq
        return tuple(
            message
            for message in self.messages
            if message.kind == KIND_GUIDANCE and message.seq > acknowledged
        )

    def guidance_deliveries(self, guidance_id: str) -> tuple[MailboxMessage, ...]:
        """Return every accepted delivery attempt for one guidance record."""
        return tuple(
            message
            for message in self.messages
            if message.kind == KIND_GUIDANCE_DELIVERY and message.to == guidance_id
        )

    def latest_guidance_delivery(self, guidance_id: str) -> MailboxMessage | None:
        """Return the highest-sequence delivery attempt, successful or not."""
        for message in reversed(self.messages):
            if message.kind == KIND_GUIDANCE_DELIVERY and message.to == guidance_id:
                return message
        return None

    def successful_guidance_delivery(self, guidance_id: str) -> MailboxMessage | None:
        """Return the first attempt that actually reached the worker."""
        for message in self.messages:
            if (
                message.kind == KIND_GUIDANCE_DELIVERY
                and message.to == guidance_id
                and message.delivery_state in GUIDANCE_DELIVERED_STATES
            ):
                return message
        return None

    def deferred_guidance_deliveries(
        self,
        guidance_id: str,
    ) -> tuple[MailboxMessage, ...]:
        """Return the attempts that ended without delivering anything.

        A deferred attempt is a durable record of "not yet", never of "no": the
        guidance it names stays in ``deliverable_guidance``.
        """
        return tuple(
            message
            for message in self.guidance_deliveries(guidance_id)
            if message.delivery_state == GUIDANCE_STATE_DEFERRED
        )

    def guidance_delivery_state(self, guidance_id: str) -> GuidanceDeliveryState:
        """Return one guidance record's current delivery state.

        Legacy cumulative acknowledgement is honoured as read evidence even
        though it predates the ledger, so a mailbox whose worker only ever ran
        ``asl inbox read`` reports ``delivered-by-read`` rather than pretending
        nothing was ever delivered.
        """
        delivered = self.successful_guidance_delivery(guidance_id)
        if delivered is not None:
            assert delivered.delivery_state is not None
            return delivered.delivery_state
        guidance = next(
            (
                message
                for message in self.messages
                if message.kind == KIND_GUIDANCE and message.id == guidance_id
            ),
            None,
        )
        if guidance is not None and guidance.seq <= self.max_acknowledged_seq:
            return GUIDANCE_STATE_DELIVERED_BY_READ
        if self.deferred_guidance_deliveries(guidance_id):
            return GUIDANCE_STATE_DEFERRED
        return GUIDANCE_STATE_QUEUED

    @property
    def deliverable_guidance(self) -> tuple[MailboxMessage, ...]:
        """Return guidance automatic delivery should still try to deliver.

        Successful live, next-turn, or explicit-read delivery suppresses further
        attempts, and so does the legacy acknowledgement sequence. Everything
        else — never attempted, or attempted and deferred — remains deliverable.
        """
        acknowledged = self.max_acknowledged_seq
        delivered = {
            message.to
            for message in self.messages
            if message.kind == KIND_GUIDANCE_DELIVERY
            and message.to is not None
            and message.delivery_state in GUIDANCE_DELIVERED_STATES
        }
        return tuple(
            message
            for message in self.messages
            if message.kind == KIND_GUIDANCE
            and message.seq > acknowledged
            and message.id not in delivered
        )

    @property
    def unanswered_questions(self) -> tuple[MailboxMessage, ...]:
        closed = {
            message.to
            for message in self.messages
            if message.kind in CLOSURE_KINDS and message.to is not None
        }
        return tuple(
            message
            for message in self.messages
            if message.kind == KIND_QUESTION and message.id not in closed
        )

    def active_wait_for(self, question_id: str) -> MailboxMessage | None:
        """Return the latest valid wait-start not paired with a wait-end."""
        ended = {
            message.wait_id
            for message in self.messages
            if message.kind == KIND_WAIT_END and message.wait_id is not None
        }
        for message in reversed(self.messages):
            if (
                message.kind == KIND_WAIT_START
                and message.to == question_id
                and message.id not in ended
            ):
                return message
        return None

    def active_worker_wait(
        self,
        *,
        run_id: str,
        session_id: str | None = None,
        turn_index: int | None = None,
    ) -> MailboxMessage | None:
        """Return the newest input wait owned by the selected worker turn."""
        for question in reversed(self.unanswered_questions):
            if question.run_id != run_id:
                continue
            if question.waiter_origin != WAITER_ORIGIN_WORKER:
                continue
            if session_id is not None and question.session_id != session_id:
                continue
            if turn_index is not None and question.turn_index != turn_index:
                continue
            wait = self.active_wait_for(question.id)
            if wait is not None:
                return wait
        return None

    @property
    def latest_supervision_end(self) -> MailboxMessage | None:
        """Return the highest-sequence supervision-end record, if any."""
        for message in reversed(self.messages):
            if message.kind == KIND_SUPERVISION_END:
                return message
        return None

    def receipt_for_closure(self, closure_id: str) -> MailboxMessage | None:
        for message in self.messages:
            if message.kind == KIND_RECEIPT and message.closure_id == closure_id:
                return message
        return None

    def open_questions(
        self,
        run_id: str | None,
    ) -> tuple[MailboxMessage, ...]:
        """Return unanswered questions ``run_id`` can still consume answers for.

        The mailbox outlives every run, so an unanswered question whose owning
        run has ended is history, not a live block: nothing is waiting on it and
        no reply can reach a worker. Questions predating run attribution carry no
        ``run_id`` and are therefore never open.
        """
        if not isinstance(run_id, str) or not run_id:
            return ()
        return tuple(
            message
            for message in self.unanswered_questions
            if message.run_id == run_id
            and (
                message.version < MAILBOX_WAIT_SCHEMA_VERSION
                or message.delivery_mode != DELIVERY_MODE_WAIT
                or self.active_wait_for(message.id) is not None
            )
        )

    @property
    def counts(self) -> MailboxCounts:
        return MailboxCounts(
            guidance=sum(message.kind == KIND_GUIDANCE for message in self.messages),
            questions=sum(message.kind == KIND_QUESTION for message in self.messages),
            replies=sum(message.kind == KIND_REPLY for message in self.messages),
            resolutions=sum(
                message.kind == KIND_RESOLUTION for message in self.messages
            ),
            notices=sum(message.kind == KIND_NOTICE for message in self.messages),
            acknowledgements=sum(message.kind == KIND_ACK for message in self.messages),
            wait_starts=sum(
                message.kind == KIND_WAIT_START for message in self.messages
            ),
            wait_ends=sum(message.kind == KIND_WAIT_END for message in self.messages),
            receipts=sum(message.kind == KIND_RECEIPT for message in self.messages),
            supervision_ends=sum(
                message.kind == KIND_SUPERVISION_END for message in self.messages
            ),
            guidance_deliveries=sum(
                message.kind == KIND_GUIDANCE_DELIVERY for message in self.messages
            ),
            pending_guidance=len(self.pending_guidance),
            deliverable_guidance=len(self.deliverable_guidance),
            unanswered_questions=len(self.unanswered_questions),
            rejected=len(self.rejected),
        )

    @property
    def full(self) -> bool:
        return self.size_bytes + MAILBOX_MIN_APPEND_BYTES > MAILBOX_MAX_BYTES

    @property
    def accepts_replies(self) -> bool:
        return self.size_bytes + MAILBOX_MIN_REPLY_BYTES <= MAILBOX_MAX_BYTES

    @property
    def accepts_resolutions(self) -> bool:
        return self.size_bytes + MAILBOX_MIN_RESOLUTION_BYTES <= MAILBOX_MAX_BYTES

    @property
    def accepts_wait_closures(self) -> bool:
        # A closure for a question with a live wait stops below both lifecycle
        # reserves (see ``_append_record``), so once even the smallest one no
        # longer fits, no answer can close the wait. Gating the wait on this
        # rather than on ``full`` also keeps the wait-end it then writes inside
        # its own cap, which sits a whole wait-end reserve above this one.
        return (
            self.size_bytes + MAILBOX_MIN_REPLY_BYTES
            <= MAILBOX_MAX_BYTES
            - MAILBOX_WAIT_END_RESERVE_BYTES
            - MAILBOX_RECEIPT_RESERVE_BYTES
        )

    @property
    def accepts_questions(self) -> bool:
        # Questions share the reduced non-closure cap with guidance, notices,
        # and acknowledgements. Mirror ``full``'s exhaustion probe so the
        # status key agrees with the append-site refusal boundary.
        return (
            self.size_bytes + MAILBOX_MIN_APPEND_BYTES
            <= MAILBOX_MAX_BYTES
            - MAILBOX_CLOSURE_RESERVE_BYTES
            - MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES
        )

    @property
    def accepts_guidance_deliveries(self) -> bool:
        # Delivery records outlive the ordinary tier by exactly the delivery
        # reserve, so a mailbox saturated by guidance can still say what became
        # of it.
        return (
            self.size_bytes + MAILBOX_MIN_GUIDANCE_DELIVERY_BYTES
            <= MAILBOX_MAX_BYTES - MAILBOX_CLOSURE_RESERVE_BYTES
        )

    def messages_since(
        self,
        since: int,
        *,
        kinds: tuple[MessageKind, ...] | None = None,
    ) -> tuple[MailboxMessage, ...]:
        """Return accepted records after ``since`` without storing a cursor."""
        _validate_sequence(since, field_name="since")
        allowed = set(kinds) if kinds is not None else None
        if allowed is not None and not allowed.issubset(MESSAGE_KINDS):
            raise ValueError("kinds contains an unsupported mailbox kind")
        return tuple(
            message
            for message in self.messages
            if message.seq > since and (allowed is None or message.kind in allowed)
        )

    def reply_for(self, question_id: str) -> MailboxMessage | None:
        """Return the first valid reply targeting ``question_id``."""
        for message in self.messages:
            if message.kind == KIND_REPLY and message.to == question_id:
                return message
        return None

    def resolution_for(self, question_id: str) -> MailboxMessage | None:
        """Return the first valid resolution targeting ``question_id``."""
        for message in self.messages:
            if message.kind == KIND_RESOLUTION and message.to == question_id:
                return message
        return None

    def closure_for(self, question_id: str) -> MailboxMessage | None:
        """Return the first valid reply or resolution for ``question_id``."""
        for message in self.messages:
            if message.kind in CLOSURE_KINDS and message.to == question_id:
                return message
        return None

    def delivery_state_for(self, question_id: str) -> str:
        closure = self.closure_for(question_id)
        if closure is None:
            return "waiting" if self.active_wait_for(question_id) else "open"
        return (
            "consumed"
            if self.receipt_for_closure(closure.id) is not None
            else "accepted"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "accepts_guidance_deliveries": self.accepts_guidance_deliveries,
            "accepts_questions": self.accepts_questions,
            "accepts_replies": self.accepts_replies,
            "accepts_resolutions": self.accepts_resolutions,
            "counts": self.counts.to_payload(),
            "full": self.full,
            "last_seq": self.last_seq,
            "mailbox_path": str(self.mailbox_path),
            "max_acknowledged_seq": self.max_acknowledged_seq,
            "messages": [message.to_payload() for message in self.messages],
            "rejected": [record.to_payload() for record in self.rejected],
            "size_bytes": self.size_bytes,
        }
