"""Locked, non-destructive mailbox storage for supervised ASL runs.

The mailbox is a per-plan JSONL log. Supervisors append ``guidance``, ``reply``,
and ``resolution`` records; workers append ``question``, ``notice``, and
``ack`` records. Reads never truncate the log, and the physical one-based line
number is the durable sequence number. Invalid lines therefore consume
sequence numbers without preventing later valid records from being read.

The adjacent lease is deliberately separate from the append-only log. It is a
small, fixed-size record renewed by the supervisor so readers can distinguish
an actively supervised run from stale mailbox files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
from typing import Any, Literal
import uuid

from . import durability, state


MAILBOX_FILE_NAME = "mailbox.jsonl"
MAILBOX_LEASE_FILE_NAME = "mailbox-lease.json"
MAILBOX_LEGACY_SCHEMA_VERSION = 1
MAILBOX_PREVIOUS_SCHEMA_VERSION = 2
MAILBOX_SCHEMA_VERSION = 3
MAILBOX_SUPPORTED_SCHEMA_VERSIONS = (
    MAILBOX_LEGACY_SCHEMA_VERSION,
    MAILBOX_PREVIOUS_SCHEMA_VERSION,
    MAILBOX_SCHEMA_VERSION,
)
MAILBOX_LEASE_VERSION = 2
MAILBOX_LEASE_SUPPORTED_VERSIONS = (1, MAILBOX_LEASE_VERSION)
MAILBOX_MAX_BYTES = 4 * 1024 * 1024
# The smallest compact record produced with normal generated metadata is an
# acknowledgement with a one-digit ``upto`` value, a canonical UUID, and a
# generated UTC timestamp with six fractional-second digits. Once less than this
# remains, no ordinary mailbox append can succeed.
MAILBOX_MIN_APPEND_BYTES = 128
# The smallest compact reply produced with normal generated metadata uses a
# one-character body plus canonical UUIDs for both its id and target.
MAILBOX_MIN_REPLY_BYTES = 176
# The smallest compact resolution uses ``pause``, a one-character run id, and
# canonical UUIDs for both its id and target.
MAILBOX_MIN_RESOLUTION_BYTES = 200
# Headroom below the cap that only question-closing records may consume.
# Guidance, questions, notices, and acknowledgements stop at the reduced cap
# so they cannot strand a waiting run by consuming its closure capacity.
MAILBOX_CLOSURE_RESERVE_BYTES = 512 * 1024
# Lifecycle records must remain appendable after ordinary messages and even a
# maximal closure have exhausted their respective tiers.  The three nested
# limits are intentionally conservative: a closure cannot consume the bytes
# reserved for ending its wait and recording consumption, and a wait-end cannot
# consume the final receipt reserve.
MAILBOX_WAIT_END_RESERVE_BYTES = 192 * 1024
MAILBOX_RECEIPT_RESERVE_BYTES = 96 * 1024
# Backward-compatible names for callers that compute the old reply/question
# reserve. Both aliases intentionally name the shared closure reserve.
MAILBOX_REPLY_RESERVE_BYTES = MAILBOX_CLOSURE_RESERVE_BYTES
MAILBOX_QUESTION_RESERVE_BYTES = MAILBOX_CLOSURE_RESERVE_BYTES
# Questions record the run that owns them. A runner id is
# ``<timestamp>-<pid>`` (~30 characters); the bound only keeps a hostile or
# corrupt value from consuming the mailbox capacity budget.
MAILBOX_MAX_RUN_ID_CHARS = 128
MAILBOX_MAX_SESSION_ID_CHARS = 128
# Mailbox sequence and turn fields are operational counters, not arbitrary
# precision values. A signed 64-bit ceiling keeps every accepted value safely
# serializable even when Python's integer-string conversion guard is enabled.
MAILBOX_MAX_INTEGER = (1 << 63) - 1
MAILBOX_LEASE_BYTES = 512
MAILBOX_LEASE_FRESH_SECONDS = 90.0
MAILBOX_CHANGE_TOKEN_UNAVAILABLE = object()

MessageKind = Literal[
    "guidance",
    "question",
    "reply",
    "resolution",
    "notice",
    "ack",
    "wait-start",
    "wait-end",
    "receipt",
]
ResolutionAction = Literal["continue", "pause"]
WaiterOrigin = Literal["worker", "runner"]
DeliveryMode = Literal["async", "wait"]
WaitEndReason = Literal["timeout", "closure", "error"]
KIND_GUIDANCE: MessageKind = "guidance"
KIND_QUESTION: MessageKind = "question"
KIND_REPLY: MessageKind = "reply"
KIND_RESOLUTION: MessageKind = "resolution"
KIND_NOTICE: MessageKind = "notice"
KIND_ACK: MessageKind = "ack"
KIND_WAIT_START: MessageKind = "wait-start"
KIND_WAIT_END: MessageKind = "wait-end"
KIND_RECEIPT: MessageKind = "receipt"
MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_NOTICE,
    KIND_ACK,
    KIND_WAIT_START,
    KIND_WAIT_END,
    KIND_RECEIPT,
)
V2_MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_NOTICE,
    KIND_ACK,
)
LEGACY_MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_ACK,
)
CLOSURE_KINDS: tuple[MessageKind, ...] = (KIND_REPLY, KIND_RESOLUTION)
LIFECYCLE_KINDS: tuple[MessageKind, ...] = (
    KIND_WAIT_START,
    KIND_WAIT_END,
    KIND_RECEIPT,
)
DELIVERY_MODE_ASYNC: DeliveryMode = "async"
DELIVERY_MODE_WAIT: DeliveryMode = "wait"
DELIVERY_MODES: tuple[DeliveryMode, ...] = (
    DELIVERY_MODE_ASYNC,
    DELIVERY_MODE_WAIT,
)
WAIT_END_REASON_TIMEOUT: WaitEndReason = "timeout"
WAIT_END_REASON_CLOSURE: WaitEndReason = "closure"
WAIT_END_REASON_ERROR: WaitEndReason = "error"
WAIT_END_REASONS: tuple[WaitEndReason, ...] = (
    WAIT_END_REASON_TIMEOUT,
    WAIT_END_REASON_CLOSURE,
    WAIT_END_REASON_ERROR,
)
RESOLUTION_ACTION_CONTINUE: ResolutionAction = "continue"
RESOLUTION_ACTION_PAUSE: ResolutionAction = "pause"
RESOLUTION_ACTIONS: tuple[ResolutionAction, ...] = (
    RESOLUTION_ACTION_CONTINUE,
    RESOLUTION_ACTION_PAUSE,
)
WAITER_ORIGIN_WORKER: WaiterOrigin = "worker"
WAITER_ORIGIN_RUNNER: WaiterOrigin = "runner"
WAITER_ORIGINS: tuple[WaiterOrigin, ...] = (
    WAITER_ORIGIN_WORKER,
    WAITER_ORIGIN_RUNNER,
)

# Stable record-rejection reasons. These are intentionally data, not prose:
# later CLI and status surfaces can branch on them without parsing exceptions.
REJECT_EMPTY_LINE = "empty-line"
REJECT_INVALID_JSON = "invalid-json"
REJECT_NOT_OBJECT = "not-object"
REJECT_UNSUPPORTED_VERSION = "unsupported-version"
REJECT_UNKNOWN_KIND = "unknown-kind"
REJECT_INVALID_ID = "invalid-id"
REJECT_DUPLICATE_ID = "duplicate-id"
REJECT_INVALID_CREATED_AT = "invalid-created-at"
REJECT_MISSING_TEXT = "missing-text"
REJECT_INVALID_TARGET = "invalid-target"
REJECT_QUESTION_NOT_FOUND = "question-not-found"
REJECT_QUESTION_NOT_OPEN = "question-not-open"
REJECT_QUESTION_ALREADY_ANSWERED = "question-already-answered"
REJECT_QUESTION_ALREADY_RESOLVED = "question-already-resolved"
REJECT_INVALID_RUN_ID = "invalid-run-id"
REJECT_INVALID_SESSION_ID = "invalid-session-id"
REJECT_INVALID_TURN_INDEX = "invalid-turn-index"
REJECT_INVALID_WAITER_ORIGIN = "invalid-waiter-origin"
REJECT_INVALID_ACTION = "invalid-action"
REJECT_INVALID_NOTE = "invalid-note"
REJECT_INVALID_ACKNOWLEDGEMENT = "invalid-acknowledgement"
REJECT_INVALID_DELIVERY_MODE = "invalid-delivery-mode"
REJECT_QUESTION_NOT_WAITING = "question-not-waiting"
REJECT_QUESTION_ALREADY_WAITING = "question-already-waiting"
REJECT_INVALID_WAIT_ID = "invalid-wait-id"
REJECT_INVALID_WAIT_END_REASON = "invalid-wait-end-reason"
REJECT_INVALID_CLOSURE_ID = "invalid-closure-id"
REJECT_CLOSURE_NOT_FOUND = "closure-not-found"
REJECT_CLOSURE_ALREADY_CONSUMED = "closure-already-consumed"

# Stable operation errors. Validation failures use the corresponding rejection
# reason where possible so callers receive the same reason for live input and
# persisted corruption.
ERROR_MAILBOX_FULL = "mailbox-full"
ERROR_MAILBOX_READ = "mailbox-read-error"
ERROR_MAILBOX_WRITE = "mailbox-write-error"
ERROR_LEASE_READ = "lease-read-error"
ERROR_LEASE_WRITE = "lease-write-error"

LEASE_MISSING = "missing"
LEASE_INVALID_SIZE = "invalid-size"
LEASE_EMPTY = "empty"
LEASE_INVALID_JSON = "invalid-json"
LEASE_NOT_OBJECT = "not-object"
LEASE_UNSUPPORTED_VERSION = "unsupported-version"
LEASE_INVALID_SUPERVISOR_ID = "invalid-supervisor-id"
LEASE_INVALID_UPDATED_AT = "invalid-updated-at"
LEASE_INVALID_RUN_ID = "invalid-run-id"
LEASE_INVALID_PROJECTION_SEQ = "invalid-projection-seq"
LEASE_INVALID_UNANSWERED_QUESTIONS = "invalid-unanswered-questions"
LEASE_INVALID_LATEST_QUESTION_ID = "invalid-latest-question-id"
LEASE_INVALID_LATEST_QUESTION_CREATED_AT = "invalid-latest-question-created-at"
LEASE_INCOMPLETE_QUESTION_PROJECTION = "incomplete-question-projection"
LEASE_FUTURE_UPDATED_AT = "future-updated-at"
LEASE_EXPIRED = "expired"


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
    pending_guidance: int
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
        if self.wait_starts or self.wait_ends or self.receipts:
            payload.update(
                wait_starts=self.wait_starts,
                wait_ends=self.wait_ends,
                receipts=self.receipts,
            )
        return payload


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
                message.version < MAILBOX_SCHEMA_VERSION
                or message.delivery_mode != DELIVERY_MODE_WAIT
                or self.active_wait_for(message.id) is not None
            )
        )

    @property
    def counts(self) -> MailboxCounts:
        return MailboxCounts(
            guidance=sum(
                message.kind == KIND_GUIDANCE for message in self.messages
            ),
            questions=sum(
                message.kind == KIND_QUESTION for message in self.messages
            ),
            replies=sum(message.kind == KIND_REPLY for message in self.messages),
            resolutions=sum(
                message.kind == KIND_RESOLUTION for message in self.messages
            ),
            notices=sum(message.kind == KIND_NOTICE for message in self.messages),
            acknowledgements=sum(
                message.kind == KIND_ACK for message in self.messages
            ),
            wait_starts=sum(
                message.kind == KIND_WAIT_START for message in self.messages
            ),
            wait_ends=sum(
                message.kind == KIND_WAIT_END for message in self.messages
            ),
            receipts=sum(
                message.kind == KIND_RECEIPT for message in self.messages
            ),
            pending_guidance=len(self.pending_guidance),
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
        return (
            self.size_bytes + MAILBOX_MIN_RESOLUTION_BYTES
            <= MAILBOX_MAX_BYTES
        )

    @property
    def accepts_questions(self) -> bool:
        # Questions share the reduced non-closure cap with guidance, notices,
        # and acknowledgements. Mirror ``full``'s exhaustion probe so the
        # status key agrees with the append-site refusal boundary.
        return (
            self.size_bytes + MAILBOX_MIN_APPEND_BYTES
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
            if message.seq > since
            and (allowed is None or message.kind in allowed)
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
                payload[
                    "latest_unanswered_question_id"
                ] = self.latest_unanswered_question_id
                payload[
                    "latest_unanswered_question_created_at"
                ] = self.latest_unanswered_question_created_at
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
            "version": (
                self.lease.version if self.lease is not None else None
            ),
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


def mailbox_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the canonical mailbox log path for ``plan_dir``."""
    return state.plan_state_dir(plan_dir, state_dir) / MAILBOX_FILE_NAME


def lease_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the canonical supervisor lease path for ``plan_dir``."""
    return state.plan_state_dir(plan_dir, state_dir) / MAILBOX_LEASE_FILE_NAME


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="microseconds")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or value == "":
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _valid_run_id(value: object) -> bool:
    """Return whether ``value`` is a bounded, nonblank run identifier."""
    return (
        isinstance(value, str)
        and value.strip() != ""
        and len(value) <= MAILBOX_MAX_RUN_ID_CHARS
    )


def _valid_session_id(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > MAILBOX_MAX_SESSION_ID_CHARS
    ):
        return False
    components = value.split(".")
    return bool(components) and all(
        component.isascii()
        and component.isdigit()
        and component[0] != "0"
        for component in components
    )


def _valid_turn_index(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 < value <= MAILBOX_MAX_INTEGER
    )


def _parse_timestamp(value: object, *, require_utc: bool = False) -> datetime | None:
    if not isinstance(value, str) or value == "":
        return None
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    if require_utc and parsed.utcoffset() != timedelta(0):
        return None
    return parsed


def _validate_sequence(value: object, *, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAILBOX_MAX_INTEGER
    ):
        raise ValueError(
            f"{field_name} must be a non-negative integer no greater than "
            f"{MAILBOX_MAX_INTEGER}"
        )
    return value


def _physical_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    return lines


def _reject(
    rejected: list[RejectedMailboxRecord],
    *,
    seq: int,
    reason: str,
    raw: str,
    detail: str | None = None,
) -> None:
    rejected.append(
        RejectedMailboxRecord(
            seq=seq,
            reason=reason,
            raw=raw,
            detail=detail,
        )
    )


def _parse_mailbox_bytes(data: bytes, mailbox_path: Path) -> MailboxView:
    messages: list[MailboxMessage] = []
    rejected: list[RejectedMailboxRecord] = []
    seen_ids: set[str] = set()
    questions: dict[str, MailboxMessage] = {}
    closures: dict[str, MailboxMessage] = {}
    wait_starts: dict[str, MailboxMessage] = {}
    active_waits: dict[str, MailboxMessage] = {}
    receipts: dict[str, MailboxMessage] = {}
    physical_lines = _physical_lines(data)

    for seq, raw_bytes in enumerate(physical_lines, start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        if raw.strip() == "":
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_EMPTY_LINE,
                raw=raw,
            )
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_JSON,
                raw=raw,
                detail=str(error),
            )
            continue
        if not isinstance(payload, dict):
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_NOT_OBJECT,
                raw=raw,
            )
            continue

        # Versionless records predate the v2 writer and therefore retain the
        # legacy v1 interpretation even after the current write version moves.
        version = payload.get("version", MAILBOX_LEGACY_SCHEMA_VERSION)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version not in MAILBOX_SUPPORTED_SCHEMA_VERSIONS
        ):
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNSUPPORTED_VERSION,
                raw=raw,
                detail=str(version),
            )
            continue

        kind = payload.get("kind")
        allowed_kinds = (
            LEGACY_MESSAGE_KINDS
            if version == MAILBOX_LEGACY_SCHEMA_VERSION
            else V2_MESSAGE_KINDS
            if version == MAILBOX_PREVIOUS_SCHEMA_VERSION
            else MESSAGE_KINDS
        )
        if kind not in allowed_kinds:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNKNOWN_KIND,
                raw=raw,
                detail=str(kind),
            )
            continue

        message_id = payload.get("id")
        if not _is_uuid(message_id):
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_ID,
                raw=raw,
            )
            continue
        assert isinstance(message_id, str)
        if message_id in seen_ids:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_DUPLICATE_ID,
                raw=raw,
                detail=message_id,
            )
            continue

        created_at = payload.get("created_at")
        if _parse_timestamp(created_at) is None:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_CREATED_AT,
                raw=raw,
            )
            continue
        assert isinstance(created_at, str)

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

        if kind in (KIND_GUIDANCE, KIND_QUESTION, KIND_REPLY, KIND_NOTICE):
            text_value = payload.get("text")
            if not isinstance(text_value, str) or text_value.strip() == "":
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_MISSING_TEXT,
                    raw=raw,
                )
                continue
            text = text_value

        if kind == KIND_QUESTION:
            if version == MAILBOX_LEGACY_SCHEMA_VERSION:
                # v1 accepted ownership before it was part of the schema and
                # deliberately degraded malformed owners to unattributed.
                if _valid_run_id(payload.get("run_id")):
                    run_id = payload["run_id"]
            else:
                if "run_id" in payload:
                    if not _valid_run_id(payload.get("run_id")):
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_RUN_ID,
                            raw=raw,
                        )
                        continue
                    run_id = payload["run_id"]
                if "session_id" in payload:
                    if not _valid_session_id(payload.get("session_id")):
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_SESSION_ID,
                            raw=raw,
                        )
                        continue
                    session_id = payload["session_id"]
                if "turn_index" in payload:
                    if not _valid_turn_index(payload.get("turn_index")):
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_TURN_INDEX,
                            raw=raw,
                        )
                        continue
                    turn_index = payload["turn_index"]
                if "waiter_origin" in payload:
                    waiter_value = payload.get("waiter_origin")
                    if waiter_value not in WAITER_ORIGINS:
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_WAITER_ORIGIN,
                            raw=raw,
                            detail=str(waiter_value),
                        )
                        continue
                    waiter_origin = waiter_value
                if version >= MAILBOX_SCHEMA_VERSION:
                    delivery_value = payload.get("delivery_mode")
                    if delivery_value not in DELIVERY_MODES:
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_DELIVERY_MODE,
                            raw=raw,
                            detail=str(delivery_value),
                        )
                        continue
                    delivery_mode = delivery_value

        if kind == KIND_NOTICE:
            run_value = payload.get("run_id")
            session_value = payload.get("session_id")
            turn_value = payload.get("turn_index")
            if not _valid_run_id(run_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_RUN_ID,
                    raw=raw,
                )
                continue
            if not _valid_session_id(session_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_SESSION_ID,
                    raw=raw,
                )
                continue
            if not _valid_turn_index(turn_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_TURN_INDEX,
                    raw=raw,
                )
                continue
            run_id = run_value
            session_id = session_value
            turn_index = turn_value

        if kind in CLOSURE_KINDS:
            target_value = payload.get("to")
            if not _is_uuid(target_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_TARGET,
                    raw=raw,
                )
                continue
            assert isinstance(target_value, str)
            target = target_value
            if target not in questions:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_FOUND,
                    raw=raw,
                    detail=target,
                )
                continue
            prior_closure = closures.get(target)
            if prior_closure is not None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=(
                        REJECT_QUESTION_ALREADY_ANSWERED
                        if prior_closure.kind == KIND_REPLY
                        else REJECT_QUESTION_ALREADY_RESOLVED
                    ),
                    raw=raw,
                    detail=target,
                )
                continue
            question = questions[target]
            if (
                question.version >= MAILBOX_SCHEMA_VERSION
                and question.delivery_mode == DELIVERY_MODE_WAIT
                and target not in active_waits
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_WAITING,
                    raw=raw,
                    detail=target,
                )
                continue

        if kind == KIND_RESOLUTION:
            run_value = payload.get("run_id")
            if not _valid_run_id(run_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_RUN_ID,
                    raw=raw,
                )
                continue
            assert target is not None
            if questions[target].run_id != run_value:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_OPEN,
                    raw=raw,
                    detail=target,
                )
                continue
            action_value = payload.get("action")
            if action_value not in RESOLUTION_ACTIONS:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_ACTION,
                    raw=raw,
                    detail=str(action_value),
                )
                continue
            if "note" in payload and not isinstance(payload.get("note"), str):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_NOTE,
                    raw=raw,
                )
                continue
            run_id = run_value
            action = action_value
            note = payload.get("note")

        if kind == KIND_ACK:
            upto_value = payload.get("upto")
            if (
                isinstance(upto_value, bool)
                or not isinstance(upto_value, int)
                or upto_value < 0
                or upto_value >= seq
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_ACKNOWLEDGEMENT,
                    raw=raw,
                    detail=str(upto_value),
                )
                continue
            upto = upto_value

        if kind in LIFECYCLE_KINDS:
            target_value = payload.get("to")
            if not _is_uuid(target_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_TARGET,
                    raw=raw,
                )
                continue
            assert isinstance(target_value, str)
            target = target_value
            question = questions.get(target)
            if question is None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_FOUND,
                    raw=raw,
                    detail=target,
                )
                continue
        if kind == KIND_WAIT_START:
            assert target is not None
            if target in closures:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_OPEN,
                    raw=raw,
                    detail=target,
                )
                continue
            if target in active_waits:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_ALREADY_WAITING,
                    raw=raw,
                    detail=target,
                )
                continue

        if kind == KIND_WAIT_END:
            assert target is not None
            wait_value = payload.get("wait_id")
            active_wait = active_waits.get(target)
            if (
                not _is_uuid(wait_value)
                or active_wait is None
                or active_wait.id != wait_value
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_WAIT_ID,
                    raw=raw,
                    detail=str(wait_value),
                )
                continue
            wait_id = wait_value

        if kind == KIND_RECEIPT:
            assert target is not None
            wait_value = payload.get("wait_id")
            active_wait = active_waits.get(target)
            question = questions[target]
            strict = (
                question.version >= MAILBOX_SCHEMA_VERSION
                and question.delivery_mode == DELIVERY_MODE_WAIT
            )
            if wait_value is not None:
                if (
                    not _is_uuid(wait_value)
                    or active_wait is None
                    or active_wait.id != wait_value
                ):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_WAIT_ID,
                        raw=raw,
                        detail=str(wait_value),
                    )
                    continue
                wait_id = wait_value
            elif strict or active_wait is not None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=(
                        REJECT_QUESTION_NOT_WAITING
                        if strict
                        else REJECT_INVALID_WAIT_ID
                    ),
                    raw=raw,
                    detail=target,
                )
                continue

        if kind == KIND_WAIT_END:
            reason_value = payload.get("reason")
            if reason_value not in WAIT_END_REASONS:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_WAIT_END_REASON,
                    raw=raw,
                    detail=str(reason_value),
                )
                continue
            wait_end_reason = reason_value
            closure_value = payload.get("closure_id")
            closure = closures.get(target)
            if reason_value == WAIT_END_REASON_CLOSURE:
                if (
                    not _is_uuid(closure_value)
                    or closure is None
                    or closure.id != closure_value
                    or closure_value not in receipts
                ):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_CLOSURE_ID,
                        raw=raw,
                        detail=str(closure_value),
                    )
                    continue
                closure_id = closure_value
            elif closure_value is not None or closure is not None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_CLOSURE_ID,
                    raw=raw,
                    detail=str(closure_value),
                )
                continue

        if kind == KIND_RECEIPT:
            closure_value = payload.get("closure_id")
            closure = closures.get(target)
            if (
                not _is_uuid(closure_value)
                or closure is None
                or closure.id != closure_value
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_CLOSURE_ID,
                    raw=raw,
                    detail=str(closure_value),
                )
                continue
            if closure_value in receipts:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_CLOSURE_ALREADY_CONSUMED,
                    raw=raw,
                    detail=closure_value,
                )
                continue
            closure_id = closure_value

        message = MailboxMessage(
            seq=seq,
            id=message_id,
            kind=kind,
            created_at=created_at,
            text=text,
            to=target,
            upto=upto,
            run_id=run_id,
            session_id=session_id,
            turn_index=turn_index,
            waiter_origin=waiter_origin,
            action=action,
            note=note,
            delivery_mode=delivery_mode,
            wait_id=wait_id,
            wait_end_reason=wait_end_reason,
            closure_id=closure_id,
            version=version,
        )
        messages.append(message)
        seen_ids.add(message_id)
        if kind == KIND_QUESTION:
            questions[message_id] = message
        elif kind in CLOSURE_KINDS and target is not None:
            closures[target] = message
        elif kind == KIND_WAIT_START and target is not None:
            wait_starts[message_id] = message
            active_waits[target] = message
        elif kind == KIND_RECEIPT and closure_id is not None:
            receipts[closure_id] = message
        elif kind == KIND_WAIT_END and target is not None:
            active_waits.pop(target, None)

    return MailboxView(
        mailbox_path=mailbox_path,
        messages=tuple(messages),
        rejected=tuple(rejected),
        last_seq=len(physical_lines),
        size_bytes=len(data),
    )


def _read_locked(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _message_from_record(record: dict[str, Any], seq: int) -> MailboxMessage:
    return MailboxMessage(
        seq=seq,
        id=record["id"],
        kind=record["kind"],
        created_at=record["created_at"],
        text=record.get("text"),
        to=record.get("to"),
        upto=record.get("upto"),
        run_id=record.get("run_id"),
        session_id=record.get("session_id"),
        turn_index=record.get("turn_index"),
        waiter_origin=record.get("waiter_origin"),
        action=record.get("action"),
        note=record.get("note"),
        delivery_mode=record.get("delivery_mode"),
        wait_id=record.get("wait_id"),
        wait_end_reason=record.get("reason"),
        closure_id=record.get("closure_id"),
        version=record["version"],
    )


def _encoded_records(records: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
        for record in records
    )


def _write_locked_records(
    fd: int,
    *,
    existing: bytes,
    records: list[dict[str, Any]],
    effective_max_bytes: int,
    mailbox_path: Path,
) -> tuple[MailboxMessage, ...]:
    """Append a prevalidated batch as one lock-scoped lifecycle transaction."""
    separator = b"\n" if existing and not existing.endswith(b"\n") else b""
    encoded = separator + _encoded_records(records)
    original_size = os.fstat(fd).st_size
    if original_size + len(encoded) > effective_max_bytes:
        raise MailboxError(
            ERROR_MAILBOX_FULL,
            f"append would exceed {effective_max_bytes} bytes",
            path=mailbox_path,
        )
    try:
        written = os.write(fd, encoded)
        if written != len(encoded):
            os.ftruncate(fd, original_size)
            raise OSError(
                f"short mailbox append: wrote {written} of {len(encoded)} bytes"
            )
        # Mailbox records carry durable decisions. Flush the completed
        # transaction before releasing its lock so a sudden shutdown cannot
        # acknowledge a reply, resolution, or receipt that never reached disk.
        durability.fullsync(fd)
    except OSError:
        try:
            os.ftruncate(fd, original_size)
        except OSError:
            pass
        raise
    first_seq = len(_physical_lines(existing)) + 1
    return tuple(
        _message_from_record(record, first_seq + offset)
        for offset, record in enumerate(records)
    )


def mailbox_change_token(mailbox_path: str | Path) -> object:
    """Return cheap append-log metadata used to avoid unchanged full reads."""
    try:
        stat_result = Path(mailbox_path).stat()
    except FileNotFoundError:
        return None
    except OSError:
        # The metadata probe is only an optimization. Callers must perform a
        # full read so the mailbox API preserves its established error behavior.
        return MAILBOX_CHANGE_TOKEN_UNAVAILABLE
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def read_mailbox(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> MailboxView:
    """Read and validate the complete mailbox under a shared lock."""
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        fd = os.open(mailbox_path, os.O_RDONLY)
    except FileNotFoundError:
        return MailboxView(mailbox_path=mailbox_path)
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_READ,
            str(error),
            path=mailbox_path,
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        data = _read_locked(fd)
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_READ,
            str(error),
            path=mailbox_path,
        ) from error
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
    return _parse_mailbox_bytes(data, mailbox_path)


def read_messages_since(
    plan_dir: str | Path,
    since: int,
    *,
    state_dir: str | Path | None = None,
    kinds: tuple[MessageKind, ...] | None = None,
) -> tuple[MailboxMessage, ...]:
    """Perform a stateless read and return accepted records after ``since``."""
    return read_mailbox(plan_dir, state_dir=state_dir).messages_since(
        since,
        kinds=kinds,
    )


def pending_guidance(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> tuple[MailboxMessage, ...]:
    return read_mailbox(plan_dir, state_dir=state_dir).pending_guidance


def unanswered_questions(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> tuple[MailboxMessage, ...]:
    return read_mailbox(plan_dir, state_dir=state_dir).unanswered_questions


def find_reply(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
) -> MailboxMessage | None:
    return read_mailbox(plan_dir, state_dir=state_dir).reply_for(question_id)


def find_resolution(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
) -> MailboxMessage | None:
    return read_mailbox(plan_dir, state_dir=state_dir).resolution_for(question_id)


def find_closure(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
) -> MailboxMessage | None:
    return read_mailbox(plan_dir, state_dir=state_dir).closure_for(question_id)


def _normalize_append_record(
    *,
    kind: MessageKind,
    text: str | None,
    target: str | None,
    upto: int | None,
    message_id: str | None,
    created_at: str | None,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    waiter_origin: WaiterOrigin | None = None,
    action: ResolutionAction | str | None = None,
    note: str | None = None,
    delivery_mode: DeliveryMode | str | None = None,
    wait_id: str | None = None,
    wait_end_reason: WaitEndReason | str | None = None,
    closure_id: str | None = None,
) -> dict[str, Any]:
    if kind not in MESSAGE_KINDS:
        raise MailboxError(REJECT_UNKNOWN_KIND, str(kind))
    resolved_id = _new_id() if message_id is None else message_id
    if not _is_uuid(resolved_id):
        raise MailboxError(REJECT_INVALID_ID)
    resolved_created_at = _utc_now_iso() if created_at is None else created_at
    if _parse_timestamp(resolved_created_at) is None:
        raise MailboxError(REJECT_INVALID_CREATED_AT)

    record: dict[str, Any] = {
        "created_at": resolved_created_at,
        "id": resolved_id,
        "kind": kind,
        "version": MAILBOX_SCHEMA_VERSION,
    }
    if kind in (KIND_GUIDANCE, KIND_QUESTION, KIND_REPLY, KIND_NOTICE):
        if not isinstance(text, str) or text.strip() == "":
            raise MailboxError(REJECT_MISSING_TEXT)
        record["text"] = text

    if kind == KIND_QUESTION:
        if delivery_mode not in DELIVERY_MODES:
            raise MailboxError(REJECT_INVALID_DELIVERY_MODE, str(delivery_mode))
        record["delivery_mode"] = delivery_mode
        if run_id is not None:
            if not _valid_run_id(run_id):
                raise MailboxError(REJECT_INVALID_RUN_ID)
            record["run_id"] = run_id
        if session_id is not None:
            if not _valid_session_id(session_id):
                raise MailboxError(REJECT_INVALID_SESSION_ID)
            record["session_id"] = session_id
        if turn_index is not None:
            if not _valid_turn_index(turn_index):
                raise MailboxError(REJECT_INVALID_TURN_INDEX)
            record["turn_index"] = turn_index
        if waiter_origin is not None:
            if waiter_origin not in WAITER_ORIGINS:
                raise MailboxError(
                    REJECT_INVALID_WAITER_ORIGIN,
                    str(waiter_origin),
                )
            record["waiter_origin"] = waiter_origin

    if kind == KIND_NOTICE:
        if not _valid_run_id(run_id):
            raise MailboxError(REJECT_INVALID_RUN_ID)
        if not _valid_session_id(session_id):
            raise MailboxError(REJECT_INVALID_SESSION_ID)
        if not _valid_turn_index(turn_index):
            raise MailboxError(REJECT_INVALID_TURN_INDEX)
        record["run_id"] = run_id
        record["session_id"] = session_id
        record["turn_index"] = turn_index

    if kind in CLOSURE_KINDS:
        if not _is_uuid(target):
            raise MailboxError(REJECT_INVALID_TARGET)
        record["to"] = target

    if kind in LIFECYCLE_KINDS:
        if not _is_uuid(target):
            raise MailboxError(REJECT_INVALID_TARGET)
        record["to"] = target

    if kind == KIND_WAIT_END:
        if not _is_uuid(wait_id):
            raise MailboxError(REJECT_INVALID_WAIT_ID)
        record["wait_id"] = wait_id

    if kind == KIND_RECEIPT and wait_id is not None:
        if not _is_uuid(wait_id):
            raise MailboxError(REJECT_INVALID_WAIT_ID)
        record["wait_id"] = wait_id

    if kind == KIND_WAIT_END:
        if wait_end_reason not in WAIT_END_REASONS:
            raise MailboxError(
                REJECT_INVALID_WAIT_END_REASON, str(wait_end_reason)
            )
        record["reason"] = wait_end_reason
        if wait_end_reason == WAIT_END_REASON_CLOSURE:
            if not _is_uuid(closure_id):
                raise MailboxError(REJECT_INVALID_CLOSURE_ID)
            record["closure_id"] = closure_id
        elif closure_id is not None:
            raise MailboxError(REJECT_INVALID_CLOSURE_ID)

    if kind == KIND_RECEIPT:
        if not _is_uuid(closure_id):
            raise MailboxError(REJECT_INVALID_CLOSURE_ID)
        record["closure_id"] = closure_id

    if kind == KIND_RESOLUTION:
        if not _valid_run_id(run_id):
            raise MailboxError(REJECT_INVALID_RUN_ID)
        if action not in RESOLUTION_ACTIONS:
            raise MailboxError(REJECT_INVALID_ACTION, str(action))
        if note is not None and not isinstance(note, str):
            raise MailboxError(REJECT_INVALID_NOTE)
        record["run_id"] = run_id
        record["action"] = action
        if note is not None:
            record["note"] = note

    if kind == KIND_ACK:
        try:
            record["upto"] = _validate_sequence(
                upto,
                field_name="upto",
            )
        except ValueError as error:
            raise MailboxError(
                REJECT_INVALID_ACKNOWLEDGEMENT,
                str(error),
            ) from error
    return record


def _append_record(
    plan_dir: str | Path,
    *,
    kind: MessageKind,
    state_dir: str | Path | None,
    text: str | None = None,
    target: str | None = None,
    upto: int | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    waiter_origin: WaiterOrigin | None = None,
    action: ResolutionAction | str | None = None,
    note: str | None = None,
    delivery_mode: DeliveryMode | str | None = None,
    wait_id: str | None = None,
    wait_end_reason: WaitEndReason | str | None = None,
    closure_id: str | None = None,
) -> MailboxMessage:
    record = _normalize_append_record(
        kind=kind,
        text=text,
        target=target,
        upto=upto,
        message_id=message_id,
        created_at=created_at,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        waiter_origin=waiter_origin,
        action=action,
        note=note,
        delivery_mode=delivery_mode,
        wait_id=wait_id,
        wait_end_reason=wait_end_reason,
        closure_id=closure_id,
    )
    encoded_record = (
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        mailbox_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            mailbox_path,
            os.O_RDWR | os.O_CREAT | os.O_APPEND,
            0o600,
        )
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE,
            str(error),
            path=mailbox_path,
        ) from error

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        existing = _read_locked(fd)
        current_view = _parse_mailbox_bytes(existing, mailbox_path)
        if any(message.id == record["id"] for message in current_view.messages):
            raise MailboxError(
                REJECT_DUPLICATE_ID,
                str(record["id"]),
                path=mailbox_path,
            )

        seq = current_view.last_seq + 1
        strict_wait_closure = False
        if kind == KIND_ACK:
            ack_upto = record["upto"]
            if ack_upto >= seq:
                raise MailboxError(
                    REJECT_INVALID_ACKNOWLEDGEMENT,
                    f"upto {ack_upto} must be less than append sequence {seq}",
                    path=mailbox_path,
                )
        elif kind in CLOSURE_KINDS:
            question_id = record["to"]
            question = next(
                (
                    message
                    for message in current_view.messages
                    if message.kind == KIND_QUESTION
                    and message.id == question_id
                ),
                None,
            )
            if question is None:
                raise MailboxError(
                    REJECT_QUESTION_NOT_FOUND,
                    question_id,
                    path=mailbox_path,
                )
            closure = current_view.closure_for(question_id)
            if closure is not None:
                raise MailboxError(
                    (
                        REJECT_QUESTION_ALREADY_ANSWERED
                        if closure.kind == KIND_REPLY
                        else REJECT_QUESTION_ALREADY_RESOLVED
                    ),
                    question_id,
                    path=mailbox_path,
                )
            if (
                kind == KIND_RESOLUTION
                and question.run_id != record["run_id"]
            ):
                raise MailboxError(
                    REJECT_QUESTION_NOT_OPEN,
                    question_id,
                    path=mailbox_path,
                )
            if (
                question.version >= MAILBOX_SCHEMA_VERSION
                and question.delivery_mode == DELIVERY_MODE_WAIT
                and current_view.active_wait_for(question_id) is None
            ):
                raise MailboxError(
                    REJECT_QUESTION_NOT_WAITING,
                    question_id,
                    path=mailbox_path,
                )
            strict_wait_closure = (
                question.version >= MAILBOX_SCHEMA_VERSION
                and question.delivery_mode == DELIVERY_MODE_WAIT
            )
            if not strict_wait_closure:
                strict_wait_closure = any(
                    candidate.kind == KIND_QUESTION
                    and current_view.active_wait_for(candidate.id) is not None
                    for candidate in current_view.messages
                )

        if kind == KIND_WAIT_START:
            question_id = record["to"]
            question = next(
                (
                    message
                    for message in current_view.messages
                    if message.kind == KIND_QUESTION
                    and message.id == question_id
                ),
                None,
            )
            if question is None:
                raise MailboxError(
                    REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
                )
            if current_view.closure_for(question_id) is not None:
                raise MailboxError(
                    REJECT_QUESTION_NOT_OPEN, question_id, path=mailbox_path
                )
            if current_view.active_wait_for(question_id) is not None:
                raise MailboxError(
                    REJECT_QUESTION_ALREADY_WAITING,
                    question_id,
                    path=mailbox_path,
                )

        if kind == KIND_WAIT_END:
            question_id = record["to"]
            active_wait = current_view.active_wait_for(question_id)
            if active_wait is None or active_wait.id != record["wait_id"]:
                raise MailboxError(
                    REJECT_QUESTION_NOT_WAITING,
                    question_id,
                    path=mailbox_path,
                )

        if kind == KIND_RECEIPT:
            question_id = record["to"]
            question = next(
                (
                    message
                    for message in current_view.messages
                    if message.kind == KIND_QUESTION
                    and message.id == question_id
                ),
                None,
            )
            if question is None:
                raise MailboxError(
                    REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
                )
            closure = current_view.closure_for(question_id)
            if closure is None or closure.id != record["closure_id"]:
                raise MailboxError(
                    REJECT_CLOSURE_NOT_FOUND,
                    record["closure_id"],
                    path=mailbox_path,
                )
            if current_view.receipt_for_closure(closure.id) is not None:
                raise MailboxError(
                    REJECT_CLOSURE_ALREADY_CONSUMED,
                    closure.id,
                    path=mailbox_path,
                )
            active_wait = current_view.active_wait_for(question_id)
            strict = (
                question.version >= MAILBOX_SCHEMA_VERSION
                and question.delivery_mode == DELIVERY_MODE_WAIT
            )
            if active_wait is not None and record.get("wait_id") is None:
                raise MailboxError(
                    (
                        REJECT_QUESTION_NOT_WAITING
                        if strict
                        else REJECT_INVALID_WAIT_ID
                    ),
                    question_id,
                    path=mailbox_path,
                )
            if strict and (
                active_wait is None
                or active_wait.id != record.get("wait_id")
            ):
                raise MailboxError(
                    REJECT_QUESTION_NOT_WAITING,
                    question_id,
                    path=mailbox_path,
                )
            if record.get("wait_id") is not None and (
                active_wait is None
                or active_wait.id != record["wait_id"]
            ):
                raise MailboxError(
                    REJECT_INVALID_WAIT_ID,
                    record["wait_id"],
                    path=mailbox_path,
                )

        separator = b"\n" if existing and not existing.endswith(b"\n") else b""
        append_bytes = separator + encoded_record
        original_size = os.fstat(fd).st_size
        if kind == KIND_RECEIPT:
            effective_max_bytes = MAILBOX_MAX_BYTES
        elif kind == KIND_WAIT_END:
            effective_max_bytes = (
                MAILBOX_MAX_BYTES - MAILBOX_RECEIPT_RESERVE_BYTES
            )
        elif kind in CLOSURE_KINDS and strict_wait_closure:
            effective_max_bytes = (
                MAILBOX_MAX_BYTES
                - MAILBOX_WAIT_END_RESERVE_BYTES
                - MAILBOX_RECEIPT_RESERVE_BYTES
            )
        elif kind in CLOSURE_KINDS:
            effective_max_bytes = MAILBOX_MAX_BYTES
        else:
            effective_max_bytes = (
                MAILBOX_MAX_BYTES - MAILBOX_CLOSURE_RESERVE_BYTES
            )
        if original_size + len(append_bytes) > effective_max_bytes:
            raise MailboxError(
                ERROR_MAILBOX_FULL,
                f"append would exceed {effective_max_bytes} bytes",
                path=mailbox_path,
            )
        try:
            written = os.write(fd, append_bytes)
            if written != len(append_bytes):
                os.ftruncate(fd, original_size)
                raise OSError(
                    f"short mailbox append: wrote {written} of "
                    f"{len(append_bytes)} bytes"
                )
            durability.fullsync(fd)
        except OSError:
            # A regular-file write should be all-or-error, but restoring the
            # original length also fails safe on an observable short write.
            try:
                os.ftruncate(fd, original_size)
            except OSError:
                pass
            raise
    except MailboxError:
        raise
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE,
            str(error),
            path=mailbox_path,
        ) from error
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    return MailboxMessage(
        seq=seq,
        id=record["id"],
        kind=kind,
        created_at=record["created_at"],
        text=record.get("text"),
        to=record.get("to"),
        upto=record.get("upto"),
        run_id=record.get("run_id"),
        session_id=record.get("session_id"),
        turn_index=record.get("turn_index"),
        waiter_origin=record.get("waiter_origin"),
        action=record.get("action"),
        note=record.get("note"),
        delivery_mode=record.get("delivery_mode"),
        wait_id=record.get("wait_id"),
        wait_end_reason=record.get("reason"),
        closure_id=record.get("closure_id"),
        version=record["version"],
    )


def append_guidance(
    plan_dir: str | Path,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    return _append_record(
        plan_dir,
        kind=KIND_GUIDANCE,
        state_dir=state_dir,
        text=text,
        message_id=message_id,
        created_at=created_at,
    )


def append_question(
    plan_dir: str | Path,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    waiter_origin: WaiterOrigin | None = None,
    delivery_mode: DeliveryMode | str = DELIVERY_MODE_ASYNC,
) -> MailboxMessage:
    return _append_record(
        plan_dir,
        kind=KIND_QUESTION,
        state_dir=state_dir,
        text=text,
        message_id=message_id,
        created_at=created_at,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        waiter_origin=waiter_origin,
        delivery_mode=delivery_mode,
    )


def append_wait_question(
    plan_dir: str | Path,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    wait_id: str | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    waiter_origin: WaiterOrigin | None = None,
) -> tuple[MailboxMessage, MailboxMessage]:
    """Atomically create a wait-delivered question and its first active wait."""
    question_record = _normalize_append_record(
        kind=KIND_QUESTION,
        text=text,
        target=None,
        upto=None,
        message_id=message_id,
        created_at=created_at,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        waiter_origin=waiter_origin,
        delivery_mode=DELIVERY_MODE_WAIT,
    )
    wait_record = _normalize_append_record(
        kind=KIND_WAIT_START,
        text=None,
        target=question_record["id"],
        upto=None,
        message_id=wait_id,
        created_at=created_at,
    )
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        mailbox_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            mailbox_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600
        )
    except OSError as error:
        raise MailboxError(ERROR_MAILBOX_WRITE, str(error), path=mailbox_path) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        existing = _read_locked(fd)
        view = _parse_mailbox_bytes(existing, mailbox_path)
        existing_ids = {message.id for message in view.messages}
        for record in (question_record, wait_record):
            if record["id"] in existing_ids:
                raise MailboxError(
                    REJECT_DUPLICATE_ID, record["id"], path=mailbox_path
                )
            existing_ids.add(record["id"])
        messages = _write_locked_records(
            fd,
            existing=existing,
            records=[question_record, wait_record],
            effective_max_bytes=(
                MAILBOX_MAX_BYTES - MAILBOX_CLOSURE_RESERVE_BYTES
            ),
            mailbox_path=mailbox_path,
        )
    except MailboxError:
        raise
    except OSError as error:
        raise MailboxError(ERROR_MAILBOX_WRITE, str(error), path=mailbox_path) from error
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
    return messages[0], messages[1]


def append_wait_start(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Start a resumed wait for an open v3 wait-delivered question."""
    return _append_record(
        plan_dir,
        kind=KIND_WAIT_START,
        state_dir=state_dir,
        target=question_id,
        message_id=message_id,
        created_at=created_at,
    )


def append_reply(
    plan_dir: str | Path,
    question_id: str,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Atomically append the first reply to a valid question."""
    return _append_record(
        plan_dir,
        kind=KIND_REPLY,
        state_dir=state_dir,
        text=text,
        target=question_id,
        message_id=message_id,
        created_at=created_at,
    )


def append_resolution(
    plan_dir: str | Path,
    question_id: str,
    action: ResolutionAction | str,
    *,
    expected_run_id: str,
    note: str | None = None,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Atomically append the first answerless closure for an owned question."""
    return _append_record(
        plan_dir,
        kind=KIND_RESOLUTION,
        state_dir=state_dir,
        target=question_id,
        run_id=expected_run_id,
        action=action,
        note=note,
        message_id=message_id,
        created_at=created_at,
    )


def append_receipt(
    plan_dir: str | Path,
    question_id: str,
    closure_id: str,
    *,
    wait_id: str | None = None,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Record that an ask/runner consumer actually observed one closure."""
    return _append_record(
        plan_dir,
        kind=KIND_RECEIPT,
        state_dir=state_dir,
        target=question_id,
        wait_id=wait_id,
        closure_id=closure_id,
        message_id=message_id,
        created_at=created_at,
    )


def finish_wait(
    plan_dir: str | Path,
    question_id: str,
    wait_id: str,
    *,
    reason: WaitEndReason | str,
    state_dir: str | Path | None = None,
    created_at: str | None = None,
) -> WaitCompletion:
    """Atomically end a wait, giving any committed closure priority.

    The mailbox lock is the serialization point for timeout-versus-closure
    races.  If a closure won first, this transaction records its receipt and a
    closure wait-end.  Otherwise it records the requested timeout/error end;
    any later strict wait-mode closure is rejected as ``question-not-waiting``.
    """
    if reason not in (WAIT_END_REASON_TIMEOUT, WAIT_END_REASON_ERROR):
        raise MailboxError(REJECT_INVALID_WAIT_END_REASON, str(reason))
    if not _is_uuid(question_id):
        raise MailboxError(REJECT_INVALID_TARGET)
    if not _is_uuid(wait_id):
        raise MailboxError(REJECT_INVALID_WAIT_ID)
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        fd = os.open(mailbox_path, os.O_RDWR | os.O_APPEND)
    except FileNotFoundError as error:
        raise MailboxError(
            REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
        ) from error
    except OSError as error:
        raise MailboxError(ERROR_MAILBOX_WRITE, str(error), path=mailbox_path) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = _read_locked(fd)
        view = _parse_mailbox_bytes(existing, mailbox_path)
        question = next(
            (
                message
                for message in view.messages
                if message.kind == KIND_QUESTION and message.id == question_id
            ),
            None,
        )
        if question is None:
            raise MailboxError(
                REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
            )
        wait_start = view.active_wait_for(question_id)
        if wait_start is None or wait_start.id != wait_id:
            raise MailboxError(
                REJECT_QUESTION_NOT_WAITING, question_id, path=mailbox_path
            )
        closure = view.closure_for(question_id)
        records: list[dict[str, Any]] = []
        receipt: MailboxMessage | None = None
        if closure is not None:
            existing_receipt = view.receipt_for_closure(closure.id)
            if existing_receipt is None:
                records.append(
                    _normalize_append_record(
                        kind=KIND_RECEIPT,
                        text=None,
                        target=question_id,
                        upto=None,
                        message_id=None,
                        created_at=created_at,
                        wait_id=wait_id,
                        closure_id=closure.id,
                    )
                )
            else:
                receipt = existing_receipt
            end_reason: WaitEndReason = WAIT_END_REASON_CLOSURE
        else:
            end_reason = reason
        records.append(
            _normalize_append_record(
                kind=KIND_WAIT_END,
                text=None,
                target=question_id,
                upto=None,
                message_id=None,
                created_at=created_at,
                wait_id=wait_id,
                wait_end_reason=end_reason,
                closure_id=closure.id if closure is not None else None,
            )
        )
        written = _write_locked_records(
            fd,
            existing=existing,
            records=records,
            effective_max_bytes=(
                MAILBOX_MAX_BYTES
                if closure is not None
                else MAILBOX_MAX_BYTES - MAILBOX_RECEIPT_RESERVE_BYTES
            ),
            mailbox_path=mailbox_path,
        )
        if closure is not None and receipt is None:
            receipt = written[0]
        wait_end = written[-1]
    except MailboxError:
        raise
    except OSError as error:
        raise MailboxError(ERROR_MAILBOX_WRITE, str(error), path=mailbox_path) from error
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
    return WaitCompletion(
        question=question,
        wait_start=wait_start,
        wait_end=wait_end,
        closure=closure,
        receipt=receipt,
    )


def consume_wait_closure(
    plan_dir: str | Path,
    question_id: str,
    wait_id: str,
    *,
    state_dir: str | Path | None = None,
) -> WaitCompletion | None:
    """Consume a closure if present without ending an otherwise active wait."""
    view = read_mailbox(plan_dir, state_dir=state_dir)
    if view.closure_for(question_id) is None:
        return None
    return finish_wait(
        plan_dir,
        question_id,
        wait_id,
        reason=WAIT_END_REASON_ERROR,
        state_dir=state_dir,
    )


def append_notice(
    plan_dir: str | Path,
    text: str,
    *,
    run_id: str,
    session_id: str,
    turn_index: int,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Append a nonblocking worker notice with exact turn ownership."""
    return _append_record(
        plan_dir,
        kind=KIND_NOTICE,
        state_dir=state_dir,
        text=text,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        message_id=message_id,
        created_at=created_at,
    )


def append_acknowledgement(
    plan_dir: str | Path,
    upto: int,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Acknowledge worker-visible records through physical sequence ``upto``."""
    return _append_record(
        plan_dir,
        kind=KIND_ACK,
        state_dir=state_dir,
        upto=upto,
        message_id=message_id,
        created_at=created_at,
    )


# Short alias for call sites that already use the on-disk kind name.
append_ack = append_acknowledgement


def _normalize_now(now: datetime | None) -> datetime:
    resolved = _utc_now() if now is None else now
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return resolved.astimezone(timezone.utc)


def _validate_lease_age(max_age_seconds: float) -> float:
    try:
        resolved = float(max_age_seconds)
    except (TypeError, ValueError) as error:
        raise ValueError("max_age_seconds must be a non-negative number") from error
    if not math.isfinite(resolved) or resolved < 0:
        raise ValueError("max_age_seconds must be a non-negative number")
    return resolved


def _lease_bytes(lease: MailboxLease) -> bytes:
    encoded = (
        json.dumps(
            lease.to_payload(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAILBOX_LEASE_BYTES:
        raise MailboxError(
            ERROR_LEASE_WRITE,
            f"lease payload exceeds {MAILBOX_LEASE_BYTES} bytes",
        )
    return encoded.ljust(MAILBOX_LEASE_BYTES, b" ")


def write_lease(
    plan_dir: str | Path,
    supervisor_id: str,
    *,
    state_dir: str | Path | None = None,
    updated_at: str | None = None,
    run_id: str | None = None,
    projection_seq: int | None = None,
    unanswered_questions: int | None = None,
    latest_unanswered_question_id: str | None = None,
    latest_unanswered_question_created_at: str | None = None,
) -> MailboxLease:
    """Create or renew the fixed-size supervisor lease under an exclusive lock."""
    if not _is_uuid(supervisor_id):
        raise MailboxError(LEASE_INVALID_SUPERVISOR_ID)
    resolved_updated_at = _utc_now_iso() if updated_at is None else updated_at
    if _parse_timestamp(resolved_updated_at, require_utc=True) is None:
        raise MailboxError(LEASE_INVALID_UPDATED_AT)
    projection_values = (
        projection_seq,
        unanswered_questions,
        latest_unanswered_question_id,
        latest_unanswered_question_created_at,
    )
    if run_id is None:
        if any(value is not None for value in projection_values):
            raise MailboxError(LEASE_INCOMPLETE_QUESTION_PROJECTION)
    else:
        if not isinstance(run_id, str) or run_id.strip() == "":
            raise MailboxError(LEASE_INVALID_RUN_ID)
        if (
            isinstance(projection_seq, bool)
            or not isinstance(projection_seq, int)
            or projection_seq < 0
        ):
            raise MailboxError(LEASE_INVALID_PROJECTION_SEQ)
        if (
            isinstance(unanswered_questions, bool)
            or not isinstance(unanswered_questions, int)
            or unanswered_questions < 0
        ):
            raise MailboxError(LEASE_INVALID_UNANSWERED_QUESTIONS)
        if unanswered_questions == 0:
            if (
                latest_unanswered_question_id is not None
                or latest_unanswered_question_created_at is not None
            ):
                raise MailboxError(LEASE_INCOMPLETE_QUESTION_PROJECTION)
        else:
            assert isinstance(projection_seq, int)
            if projection_seq == 0:
                raise MailboxError(LEASE_INVALID_PROJECTION_SEQ)
            if not _is_uuid(latest_unanswered_question_id):
                raise MailboxError(LEASE_INVALID_LATEST_QUESTION_ID)
            if (
                _parse_timestamp(
                    latest_unanswered_question_created_at,
                    require_utc=True,
                )
                is None
            ):
                raise MailboxError(
                    LEASE_INVALID_LATEST_QUESTION_CREATED_AT
                )
    lease = MailboxLease(
        supervisor_id=supervisor_id,
        updated_at=resolved_updated_at,
        run_id=run_id,
        projection_seq=projection_seq,
        unanswered_questions=unanswered_questions,
        latest_unanswered_question_id=latest_unanswered_question_id,
        latest_unanswered_question_created_at=(
            latest_unanswered_question_created_at
        ),
    )
    data = _lease_bytes(lease)
    lease_path = lease_path_for_plan(plan_dir, state_dir)
    try:
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_WRITE,
            str(error),
            path=lease_path,
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        os.lseek(fd, 0, os.SEEK_SET)
        total = 0
        while total < len(data):
            written = os.write(fd, data[total:])
            if written <= 0:
                raise OSError("short lease write")
            total += written
        os.ftruncate(fd, MAILBOX_LEASE_BYTES)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_WRITE,
            str(error),
            path=lease_path,
        ) from error
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
    return lease


def _invalid_lease_view(
    lease_path: Path,
    reason: str,
    *,
    lease: MailboxLease | None = None,
) -> MailboxLeaseView:
    return MailboxLeaseView(
        lease_path=lease_path,
        lease=lease,
        fresh=False,
        reason=reason,
    )


def read_lease(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    max_age_seconds: float = MAILBOX_LEASE_FRESH_SECONDS,
    now: datetime | None = None,
) -> MailboxLeaseView:
    """Read a supervisor lease; corruption, expiry, and future times are stale."""
    max_age = _validate_lease_age(max_age_seconds)
    resolved_now = _normalize_now(now)
    lease_path = lease_path_for_plan(plan_dir, state_dir)
    try:
        fd = os.open(lease_path, os.O_RDONLY)
    except FileNotFoundError:
        return _invalid_lease_view(lease_path, LEASE_MISSING)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_READ,
            str(error),
            path=lease_path,
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        data = _read_locked(fd)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_READ,
            str(error),
            path=lease_path,
        ) from error
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    if len(data) != MAILBOX_LEASE_BYTES:
        return _invalid_lease_view(lease_path, LEASE_INVALID_SIZE)
    stripped = data.rstrip(b" \t\r\n\0")
    if not stripped:
        return _invalid_lease_view(lease_path, LEASE_EMPTY)
    try:
        payload = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _invalid_lease_view(lease_path, LEASE_INVALID_JSON)
    if not isinstance(payload, dict):
        return _invalid_lease_view(lease_path, LEASE_NOT_OBJECT)
    version = payload.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in MAILBOX_LEASE_SUPPORTED_VERSIONS
    ):
        return _invalid_lease_view(lease_path, LEASE_UNSUPPORTED_VERSION)
    supervisor_id = payload.get("supervisor_id")
    if not _is_uuid(supervisor_id):
        return _invalid_lease_view(lease_path, LEASE_INVALID_SUPERVISOR_ID)
    updated_at = payload.get("updated_at")
    updated = _parse_timestamp(updated_at, require_utc=True)
    if updated is None:
        return _invalid_lease_view(lease_path, LEASE_INVALID_UPDATED_AT)
    assert isinstance(supervisor_id, str)
    assert isinstance(updated_at, str)
    run_id: str | None = None
    projection_seq: int | None = None
    unanswered_questions: int | None = None
    latest_unanswered_question_id: str | None = None
    latest_unanswered_question_created_at: str | None = None
    if version >= 2 and "run_id" in payload:
        run_id_value = payload.get("run_id")
        if not isinstance(run_id_value, str) or run_id_value.strip() == "":
            return _invalid_lease_view(lease_path, LEASE_INVALID_RUN_ID)
        projection_seq_value = payload.get("projection_seq")
        if (
            isinstance(projection_seq_value, bool)
            or not isinstance(projection_seq_value, int)
            or projection_seq_value < 0
        ):
            return _invalid_lease_view(
                lease_path, LEASE_INVALID_PROJECTION_SEQ
            )
        unanswered_value = payload.get("unanswered_questions")
        if (
            isinstance(unanswered_value, bool)
            or not isinstance(unanswered_value, int)
            or unanswered_value < 0
        ):
            return _invalid_lease_view(
                lease_path, LEASE_INVALID_UNANSWERED_QUESTIONS
            )
        latest_id_value = payload.get("latest_unanswered_question_id")
        latest_created_value = payload.get(
            "latest_unanswered_question_created_at"
        )
        if unanswered_value == 0:
            if latest_id_value is not None or latest_created_value is not None:
                return _invalid_lease_view(
                    lease_path, LEASE_INCOMPLETE_QUESTION_PROJECTION
                )
        else:
            if projection_seq_value == 0:
                return _invalid_lease_view(
                    lease_path, LEASE_INVALID_PROJECTION_SEQ
                )
            if not _is_uuid(latest_id_value):
                return _invalid_lease_view(
                    lease_path, LEASE_INVALID_LATEST_QUESTION_ID
                )
            if (
                _parse_timestamp(latest_created_value, require_utc=True)
                is None
            ):
                return _invalid_lease_view(
                    lease_path,
                    LEASE_INVALID_LATEST_QUESTION_CREATED_AT,
                )
            assert isinstance(latest_id_value, str)
            assert isinstance(latest_created_value, str)
            latest_unanswered_question_id = latest_id_value
            latest_unanswered_question_created_at = latest_created_value
        run_id = run_id_value
        projection_seq = projection_seq_value
        unanswered_questions = unanswered_value
    elif version >= 2 and any(
        field in payload
        for field in (
            "projection_seq",
            "unanswered_questions",
            "latest_unanswered_question_id",
            "latest_unanswered_question_created_at",
        )
    ):
        return _invalid_lease_view(
            lease_path, LEASE_INCOMPLETE_QUESTION_PROJECTION
        )
    lease = MailboxLease(
        supervisor_id=supervisor_id,
        updated_at=updated_at,
        run_id=run_id,
        projection_seq=projection_seq,
        unanswered_questions=unanswered_questions,
        latest_unanswered_question_id=latest_unanswered_question_id,
        latest_unanswered_question_created_at=(
            latest_unanswered_question_created_at
        ),
        version=version,
    )
    updated_utc = updated.astimezone(timezone.utc)
    if updated_utc > resolved_now:
        return _invalid_lease_view(
            lease_path,
            LEASE_FUTURE_UPDATED_AT,
            lease=lease,
        )
    age_seconds = (resolved_now - updated_utc).total_seconds()
    if age_seconds > max_age:
        return _invalid_lease_view(
            lease_path,
            LEASE_EXPIRED,
            lease=lease,
        )
    return MailboxLeaseView(
        lease_path=lease_path,
        lease=lease,
        fresh=True,
    )


def lease_is_fresh(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    max_age_seconds: float = MAILBOX_LEASE_FRESH_SECONDS,
    now: datetime | None = None,
) -> bool:
    return read_lease(
        plan_dir,
        state_dir=state_dir,
        max_age_seconds=max_age_seconds,
        now=now,
    ).fresh
