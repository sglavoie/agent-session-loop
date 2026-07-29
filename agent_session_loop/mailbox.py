"""Locked, non-destructive mailbox storage for supervised ASL runs.

The mailbox is a per-plan JSONL log. Supervisors append ``guidance`` and
``reply`` records; workers append ``question`` and ``ack`` records. Reads never
truncate the log, and the physical one-based line number is the durable
sequence number. Invalid lines therefore consume sequence numbers without
preventing later valid records from being read.

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

from . import state


MAILBOX_FILE_NAME = "mailbox.jsonl"
MAILBOX_LEASE_FILE_NAME = "mailbox-lease.json"
MAILBOX_SCHEMA_VERSION = 1
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
# Headroom below the cap that only replies may consume. Guidance, questions,
# and acknowledgements stop at the reduced cap so they cannot strand a waiting
# run by consuming the capacity its reply needs.
MAILBOX_REPLY_RESERVE_BYTES = 512 * 1024
# Backward-compatible name for callers that compute the reduced question cap.
MAILBOX_QUESTION_RESERVE_BYTES = MAILBOX_REPLY_RESERVE_BYTES
# Questions record the run that owns them. A runner id is
# ``<timestamp>-<pid>`` (~30 characters); the bound only keeps a hostile or
# corrupt value from consuming the mailbox capacity budget.
MAILBOX_MAX_RUN_ID_CHARS = 128
MAILBOX_LEASE_BYTES = 512
MAILBOX_LEASE_FRESH_SECONDS = 90.0
MAILBOX_CHANGE_TOKEN_UNAVAILABLE = object()

MessageKind = Literal["guidance", "question", "reply", "ack"]
KIND_GUIDANCE: MessageKind = "guidance"
KIND_QUESTION: MessageKind = "question"
KIND_REPLY: MessageKind = "reply"
KIND_ACK: MessageKind = "ack"
MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_ACK,
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
REJECT_QUESTION_ALREADY_ANSWERED = "question-already-answered"
REJECT_INVALID_ACKNOWLEDGEMENT = "invalid-acknowledgement"

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
    acknowledgements: int
    pending_guidance: int
    unanswered_questions: int
    rejected: int

    def to_payload(self) -> dict[str, int]:
        return {
            "acknowledgements": self.acknowledgements,
            "guidance": self.guidance,
            "pending_guidance": self.pending_guidance,
            "questions": self.questions,
            "rejected": self.rejected,
            "replies": self.replies,
            "unanswered_questions": self.unanswered_questions,
        }


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
        answered = {
            message.to
            for message in self.messages
            if message.kind == KIND_REPLY and message.to is not None
        }
        return tuple(
            message
            for message in self.messages
            if message.kind == KIND_QUESTION and message.id not in answered
        )

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
            acknowledgements=sum(
                message.kind == KIND_ACK for message in self.messages
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
    def accepts_questions(self) -> bool:
        # Questions share the reduced non-reply cap with guidance and
        # acknowledgements. Mirror ``full``'s exhaustion probe so the status
        # key agrees with the append-site refusal boundary.
        return (
            self.size_bytes + MAILBOX_MIN_APPEND_BYTES
            <= MAILBOX_MAX_BYTES - MAILBOX_REPLY_RESERVE_BYTES
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

    def to_payload(self) -> dict[str, Any]:
        return {
            "accepts_questions": self.accepts_questions,
            "accepts_replies": self.accepts_replies,
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
    """Accept a run identifier, treating anything malformed as unattributed."""
    return (
        isinstance(value, str)
        and value.strip() != ""
        and len(value) <= MAILBOX_MAX_RUN_ID_CHARS
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
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
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
    questions: set[str] = set()
    answered_questions: set[str] = set()
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
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
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

        version = payload.get("version", MAILBOX_SCHEMA_VERSION)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != MAILBOX_SCHEMA_VERSION
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
        if kind not in MESSAGE_KINDS:
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
        # A malformed or absent owner degrades the question to unattributed
        # rather than rejecting the line: history written before run
        # attribution must keep parsing.
        run_id: str | None = (
            payload.get("run_id")
            if kind == KIND_QUESTION and _valid_run_id(payload.get("run_id"))
            else None
        )
        if kind in (KIND_GUIDANCE, KIND_QUESTION, KIND_REPLY):
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

        if kind == KIND_REPLY:
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
            if target in answered_questions:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_ALREADY_ANSWERED,
                    raw=raw,
                    detail=target,
                )
                continue

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

        message = MailboxMessage(
            seq=seq,
            id=message_id,
            kind=kind,
            created_at=created_at,
            text=text,
            to=target,
            upto=upto,
            run_id=run_id,
            version=version,
        )
        messages.append(message)
        seen_ids.add(message_id)
        if kind == KIND_QUESTION:
            questions.add(message_id)
        elif kind == KIND_REPLY and target is not None:
            answered_questions.add(target)

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


def _normalize_append_record(
    *,
    kind: MessageKind,
    text: str | None,
    target: str | None,
    upto: int | None,
    message_id: str | None,
    created_at: str | None,
    run_id: str | None = None,
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
    if kind in (KIND_GUIDANCE, KIND_QUESTION, KIND_REPLY):
        if not isinstance(text, str) or text.strip() == "":
            raise MailboxError(REJECT_MISSING_TEXT)
        record["text"] = text
    if kind == KIND_QUESTION and _valid_run_id(run_id):
        record["run_id"] = run_id
    if kind == KIND_REPLY:
        if not _is_uuid(target):
            raise MailboxError(REJECT_INVALID_TARGET)
        record["to"] = target
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
) -> MailboxMessage:
    record = _normalize_append_record(
        kind=kind,
        text=text,
        target=target,
        upto=upto,
        message_id=message_id,
        created_at=created_at,
        run_id=run_id,
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
        if kind == KIND_ACK:
            ack_upto = record["upto"]
            if ack_upto >= seq:
                raise MailboxError(
                    REJECT_INVALID_ACKNOWLEDGEMENT,
                    f"upto {ack_upto} must be less than append sequence {seq}",
                    path=mailbox_path,
                )
        elif kind == KIND_REPLY:
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
            if current_view.reply_for(question_id) is not None:
                raise MailboxError(
                    REJECT_QUESTION_ALREADY_ANSWERED,
                    question_id,
                    path=mailbox_path,
                )

        separator = b"\n" if existing and not existing.endswith(b"\n") else b""
        append_bytes = separator + encoded_record
        original_size = os.fstat(fd).st_size
        effective_max_bytes = (
            MAILBOX_MAX_BYTES
            if kind == KIND_REPLY
            else MAILBOX_MAX_BYTES - MAILBOX_REPLY_RESERVE_BYTES
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
) -> MailboxMessage:
    return _append_record(
        plan_dir,
        kind=KIND_QUESTION,
        state_dir=state_dir,
        text=text,
        message_id=message_id,
        created_at=created_at,
        run_id=run_id,
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
