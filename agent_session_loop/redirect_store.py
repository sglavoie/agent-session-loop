"""Durable append-only journal for urgent redirect requests.

A redirect carries an operator's correction into a turn that is already
running. The correction is the whole point, so the record of it has to survive
everything the control queue does not: ``control-queue.jsonl`` is read with
``ftruncate`` and deduplicated in the draining process's memory, which makes it
a fine transport for "drain now" and an impossible authority for "this
correction was submitted and has not been answered yet". This journal is that
authority. Run state, events, Workbench, and bridge receipts are projections of
it.

Four properties are structural rather than incidental:

- **Nothing is destroyed.** Every state change is another line. The physical
  one-based line number is the sequence number, so an invalid line consumes a
  sequence and never prevents a later valid one from being read, and replay
  after a crash is a re-read rather than a reconstruction.
- **A request is settled once.** ``submitted`` is the only non-terminal state;
  ``accepted``, ``rejected``, and ``expired`` are terminal, and the reducer
  keeps the first terminal record it sees. A second acceptance appended by a
  racing writer is rejected as a line, not applied as a transition, which is
  what stops two turns from being redirected by one request.
- **The feedback text never leaves this module by accident.** No parsed object
  carries it: :class:`RedirectRequest` holds a byte count and a digest, and
  rejected lines are reported without their raw bytes because a malformed line
  can still contain the operator's words. Reading the text at all requires
  calling :func:`read_feedback` and saying which operation you want.
- **A submission cannot consume the capacity that settles it.** The journal is
  bounded, and submissions stop one tier below the cap so a full journal can
  still record the acceptance, rejection, or expiry of what is already in it.

Concurrency is coordinated through an exclusive ``fcntl.flock`` held across
read, decide, and append, so the reducer's view is the one the append is
validated against. Completed appends are flushed with
:func:`agent_session_loop.durability.fullsync` before the lock is released: a
submission a producer was told is durable must be on the device, and a short
write is rolled back to the pre-append size rather than left as a torn tail.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from . import clock, durability, literals, state

REDIRECT_JOURNAL_FILE_NAME = "redirect-journal.jsonl"
REDIRECT_JOURNAL_VERSION = 1
REDIRECT_JOURNAL_SUPPORTED_VERSIONS: tuple[int, ...] = (REDIRECT_JOURNAL_VERSION,)

#: Total journal size. A redirect journal holds one run's corrections, so this
#: is generous rather than tight; the bound exists to keep a runaway producer
#: from filling the state directory, not to ration ordinary use.
REDIRECT_JOURNAL_MAX_BYTES = 4 * 1024 * 1024
#: Headroom below the cap that only terminal records may consume. A submission
#: that used the last byte would leave a request nothing could settle, and an
#: unsettleable request is exactly the "falsely reports acceptance" failure
#: this journal exists to prevent.
REDIRECT_SETTLEMENT_RESERVE_BYTES = 512 * 1024
#: The largest correction one request may carry. Feedback is a message to a
#: worker, not an artifact: anything larger belongs in a file the worker reads.
REDIRECT_MAX_FEEDBACK_BYTES = 64 * 1024
REDIRECT_MAX_OPERATION_ID_CHARS = 64
REDIRECT_MAX_RUN_ID_CHARS = 128
REDIRECT_MAX_SESSION_ID_CHARS = 128
REDIRECT_MAX_HARNESS_CHARS = 64
#: Turn indexes are operational counters. A signed 64-bit ceiling keeps every
#: accepted value serializable even under Python's integer-string conversion
#: guard.
REDIRECT_MAX_TURN_INDEX = (1 << 63) - 1

#: How long a submitted request stays acceptable. The urgent wakeup reaches a
#: live runner in milliseconds, so this is a bound on how long a *missed*
#: wakeup may leave a request live — long enough for the runner's ordinary poll
#: to notice, short enough that a correction cannot surface in a turn the
#: operator has long stopped thinking about.
REDIRECT_DEFAULT_TTL_SECONDS = 120
REDIRECT_MIN_TTL_SECONDS = 1
REDIRECT_MAX_TTL_SECONDS = 3600

RecordKind = Literal["submitted", "accepted", "rejected", "expired"]
KIND_SUBMITTED: RecordKind = "submitted"
KIND_ACCEPTED: RecordKind = "accepted"
KIND_REJECTED: RecordKind = "rejected"
KIND_EXPIRED: RecordKind = "expired"
RECORD_KINDS: tuple[RecordKind, ...] = (
    KIND_SUBMITTED,
    KIND_ACCEPTED,
    KIND_REJECTED,
    KIND_EXPIRED,
)

RequestState = Literal["submitted", "accepted", "rejected", "expired"]
STATE_SUBMITTED: RequestState = "submitted"
STATE_ACCEPTED: RequestState = "accepted"
STATE_REJECTED: RequestState = "rejected"
STATE_EXPIRED: RequestState = "expired"
REQUEST_STATES: tuple[RequestState, ...] = (
    STATE_SUBMITTED,
    STATE_ACCEPTED,
    STATE_REJECTED,
    STATE_EXPIRED,
)
#: The states a request never leaves. ``submitted`` is deliberately the only
#: one missing: everything else is an answer, and answers are not revised.
TERMINAL_STATES: tuple[RequestState, ...] = (
    STATE_ACCEPTED,
    STATE_REJECTED,
    STATE_EXPIRED,
)

SettlementReason = Literal[
    "expired",
    "stale-target",
    "terminal-audit",
    "urgent-channel-unavailable",
    "run-ended",
    "superseded",
    "operator-cancelled",
]
REASON_EXPIRED: SettlementReason = "expired"
REASON_STALE_TARGET: SettlementReason = "stale-target"
REASON_TERMINAL_AUDIT: SettlementReason = "terminal-audit"
REASON_URGENT_CHANNEL_UNAVAILABLE: SettlementReason = "urgent-channel-unavailable"
REASON_RUN_ENDED: SettlementReason = "run-ended"
REASON_SUPERSEDED: SettlementReason = "superseded"
REASON_OPERATOR_CANCELLED: SettlementReason = "operator-cancelled"
SETTLEMENT_REASONS: tuple[SettlementReason, ...] = (
    REASON_EXPIRED,
    REASON_STALE_TARGET,
    REASON_TERMINAL_AUDIT,
    REASON_URGENT_CHANNEL_UNAVAILABLE,
    REASON_RUN_ENDED,
    REASON_SUPERSEDED,
    REASON_OPERATOR_CANCELLED,
)

# Failures raised to a caller. Stable strings so the CLI, the Workbench, and
# the bridge all say the same thing about the same refusal.
ERROR_JOURNAL_READ = "redirect-journal-read"
ERROR_JOURNAL_WRITE = "redirect-journal-write"
ERROR_JOURNAL_FULL = "redirect-journal-full"
ERROR_INVALID_TARGET = "invalid-redirect-target"
ERROR_INVALID_FEEDBACK = "invalid-redirect-feedback"
ERROR_INVALID_TTL = "invalid-redirect-ttl"
ERROR_INVALID_OPERATION_ID = "invalid-redirect-operation-id"
ERROR_INVALID_REASON = "invalid-redirect-reason"
ERROR_UNKNOWN_REQUEST = "unknown-redirect-request"
ERROR_ALREADY_SETTLED = "redirect-already-settled"
ERROR_TARGET_MISMATCH = "redirect-target-mismatch"
ERROR_REQUEST_EXPIRED = "redirect-request-expired"

# Why one physical line was not applied. A rejected line is reported without
# its bytes: a malformed submission still carries the operator's words.
REJECT_EMPTY_LINE = "empty-line"
REJECT_INVALID_JSON = "invalid-json"
REJECT_NOT_OBJECT = "not-object"
REJECT_UNSUPPORTED_VERSION = "unsupported-version"
REJECT_UNKNOWN_KIND = "unknown-kind"
REJECT_MISSING_ID = "missing-id"
REJECT_DUPLICATE_ID = "duplicate-id"
REJECT_UNKNOWN_REQUEST = "unknown-request"
REJECT_ALREADY_SETTLED = "already-settled"
REJECT_INVALID_TARGET = "invalid-target"
REJECT_INVALID_FEEDBACK = "invalid-feedback"
REJECT_INVALID_TIMESTAMP = "invalid-timestamp"
REJECT_INVALID_REASON = "invalid-reason"


class RedirectStoreError(Exception):
    """A refused journal operation carrying a stable, surface-agnostic reason."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        path: Path | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.path = path
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "path": str(self.path) if self.path is not None else None,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RedirectTarget:
    """The exact turn a correction is bound to.

    Identity is the ``(run_id, session_id, turn_index)`` triple and nothing
    else. ``harness`` is recorded because an operator reading the journal wants
    to know what was interrupted, but it is deliberately excluded from
    :meth:`identifies_same_turn`: the run loop may re-select a future harness
    between submission and acceptance, and that changes what runs the turn
    rather than which turn it is.
    """

    run_id: str
    session_id: str
    turn_index: int
    harness: str | None = None

    def identifies_same_turn(self, other: RedirectTarget) -> bool:
        return (
            self.run_id == other.run_id
            and self.session_id == other.session_id
            and self.turn_index == other.turn_index
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "harness": self.harness,
        }


@dataclass(frozen=True)
class RedirectRequest:
    """One request as the journal's replay currently understands it.

    Carries no feedback text by construction. ``feedback_digest`` is what lets
    a projection, a receipt, or an operator prove *which* correction this is
    without any surface but :func:`read_feedback` ever holding the words.
    """

    operation_id: str
    target: RedirectTarget
    state: RequestState
    created_at: str
    expires_at: str
    feedback_bytes: int
    feedback_digest: str
    submitted_seq: int
    settled_at: str | None = None
    settled_seq: int | None = None
    reason: SettlementReason | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def accepted(self) -> bool:
        return self.state == STATE_ACCEPTED

    def is_expired_at(self, moment: datetime) -> bool:
        """Whether this request's acceptance window has closed by ``moment``.

        An unparsable ``expires_at`` reads as expired. The alternative is to
        treat a timestamp we cannot evaluate as open-ended, which would let a
        corrupt record redirect a turn indefinitely.
        """
        deadline = clock.parse_datetime(self.expires_at)
        if deadline is None:
            return True
        return moment >= deadline

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "state": self.state,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "feedback_bytes": self.feedback_bytes,
            "feedback_digest": self.feedback_digest,
            "submitted_seq": self.submitted_seq,
            "settled_at": self.settled_at,
            "settled_seq": self.settled_seq,
            "reason": self.reason,
            **self.target.to_payload(),
        }


@dataclass(frozen=True)
class RejectedRecord:
    """One physical line replay could not apply.

    Unlike the mailbox's equivalent this keeps no copy of the raw line. A line
    rejected for a malformed target can still hold a complete, valid feedback
    body, and a projection that echoed it would put the operator's words in
    every surface this module exists to keep them out of.
    """

    seq: int
    reason: str
    detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"seq": self.seq, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class RedirectJournalView:
    """The replayed journal: every request, plus what could not be replayed."""

    journal_path: Path
    requests: tuple[RedirectRequest, ...] = ()
    rejected: tuple[RejectedRecord, ...] = field(default=(), repr=False)
    last_seq: int = 0
    size_bytes: int = 0

    def request(self, operation_id: str) -> RedirectRequest | None:
        for request in self.requests:
            if request.operation_id == operation_id:
                return request
        return None

    def pending(self) -> tuple[RedirectRequest, ...]:
        """Requests still awaiting an answer, oldest first."""
        return tuple(
            request for request in self.requests if request.state == STATE_SUBMITTED
        )

    def pending_for(self, target: RedirectTarget) -> tuple[RedirectRequest, ...]:
        return tuple(
            request
            for request in self.pending()
            if request.target.identifies_same_turn(target)
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "journal_path": str(self.journal_path),
            "requests": [request.to_payload() for request in self.requests],
            "rejected": [record.to_payload() for record in self.rejected],
            "last_seq": self.last_seq,
            "size_bytes": self.size_bytes,
        }


def journal_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the canonical redirect-journal path for ``plan_dir``."""
    return state.plan_state_dir(plan_dir, state_dir) / REDIRECT_JOURNAL_FILE_NAME


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require_bounded_text(
    value: object,
    *,
    field_name: str,
    max_chars: int,
    reason: str,
) -> str:
    if not isinstance(value, str) or not value:
        raise RedirectStoreError(reason, f"{field_name} must be a non-empty string")
    if len(value) > max_chars:
        raise RedirectStoreError(reason, f"{field_name} exceeds {max_chars} characters")
    return value


def validate_target(target: RedirectTarget) -> RedirectTarget:
    """Return ``target`` if every field is within bounds, else raise."""
    _require_bounded_text(
        target.run_id,
        field_name="run_id",
        max_chars=REDIRECT_MAX_RUN_ID_CHARS,
        reason=ERROR_INVALID_TARGET,
    )
    _require_bounded_text(
        target.session_id,
        field_name="session_id",
        max_chars=REDIRECT_MAX_SESSION_ID_CHARS,
        reason=ERROR_INVALID_TARGET,
    )
    if (
        isinstance(target.turn_index, bool)
        or not isinstance(target.turn_index, int)
        or target.turn_index < 1
        or target.turn_index > REDIRECT_MAX_TURN_INDEX
    ):
        raise RedirectStoreError(
            ERROR_INVALID_TARGET, "turn_index must be a positive integer"
        )
    if target.harness is not None:
        _require_bounded_text(
            target.harness,
            field_name="harness",
            max_chars=REDIRECT_MAX_HARNESS_CHARS,
            reason=ERROR_INVALID_TARGET,
        )
    return target


def validate_feedback(feedback: str) -> str:
    """Return ``feedback`` if it is a correction this journal can carry."""
    if not isinstance(feedback, str) or not feedback.strip():
        raise RedirectStoreError(
            ERROR_INVALID_FEEDBACK, "redirect feedback must be non-empty text"
        )
    size = len(feedback.encode("utf-8"))
    if size > REDIRECT_MAX_FEEDBACK_BYTES:
        raise RedirectStoreError(
            ERROR_INVALID_FEEDBACK,
            f"redirect feedback exceeds {REDIRECT_MAX_FEEDBACK_BYTES} bytes",
            details={
                "feedback_bytes": size,
                "max_feedback_bytes": REDIRECT_MAX_FEEDBACK_BYTES,
            },
        )
    return feedback


def _validate_ttl(ttl_seconds: int | None) -> int:
    if ttl_seconds is None:
        return REDIRECT_DEFAULT_TTL_SECONDS
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not REDIRECT_MIN_TTL_SECONDS <= ttl_seconds <= REDIRECT_MAX_TTL_SECONDS
    ):
        # Out of range is refused rather than clamped: silently shortening a
        # stated window would retire a correction earlier than asked.
        raise RedirectStoreError(
            ERROR_INVALID_TTL,
            f"ttl_seconds must be between {REDIRECT_MIN_TTL_SECONDS} and "
            f"{REDIRECT_MAX_TTL_SECONDS}",
        )
    return ttl_seconds


def feedback_digest(feedback: str) -> str:
    """Return the short digest that names a correction without carrying it."""
    return hashlib.sha256(feedback.encode("utf-8")).hexdigest()[:32]


def new_operation_id() -> str:
    """Mint a durable operation identity for one redirect."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _physical_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    return lines


def _parse_target(payload: Mapping[str, Any]) -> RedirectTarget | None:
    run_id = payload.get("run_id")
    session_id = payload.get("session_id")
    turn_index = payload.get("turn_index")
    harness = payload.get("harness")
    if harness is not None and not isinstance(harness, str):
        return None
    if not isinstance(run_id, str) or not isinstance(session_id, str):
        return None
    if isinstance(turn_index, bool) or not isinstance(turn_index, int):
        return None
    candidate = RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness or None,
    )
    try:
        return validate_target(candidate)
    except RedirectStoreError:
        return None


class _Replay:
    """Fold the journal's lines into requests and the lines that failed.

    A class rather than a closure-heavy function because every branch needs the
    same three pieces of mutable state, and a reducer whose rejection path can
    silently skip updating one of them is how a duplicate acceptance gets
    applied twice.
    """

    def __init__(self) -> None:
        self.order: list[str] = []
        self.requests: dict[str, RedirectRequest] = {}
        self.feedback: dict[str, str] = {}
        self.rejected: list[RejectedRecord] = []
        self.last_seq = 0
        self.size_bytes = 0

    def reject(self, seq: int, reason: str, detail: str | None = None) -> None:
        self.rejected.append(RejectedRecord(seq=seq, reason=reason, detail=detail))

    def apply(self, seq: int, payload: Mapping[str, Any]) -> None:
        version = payload.get("version")
        if version not in REDIRECT_JOURNAL_SUPPORTED_VERSIONS:
            self.reject(seq, REJECT_UNSUPPORTED_VERSION, str(version))
            return
        kind = payload.get("kind")
        if kind not in RECORD_KINDS:
            self.reject(seq, REJECT_UNKNOWN_KIND, str(kind))
            return
        operation_id = payload.get("id")
        if not isinstance(operation_id, str) or not operation_id:
            self.reject(seq, REJECT_MISSING_ID)
            return
        created_at = payload.get("created_at")
        if clock.parse_datetime(created_at) is None:
            self.reject(seq, REJECT_INVALID_TIMESTAMP, operation_id)
            return
        if kind == KIND_SUBMITTED:
            self._apply_submitted(seq, payload, operation_id, str(created_at))
            return
        self._apply_settlement(seq, payload, operation_id, str(created_at), kind)

    def _apply_submitted(
        self,
        seq: int,
        payload: Mapping[str, Any],
        operation_id: str,
        created_at: str,
    ) -> None:
        if operation_id in self.requests:
            self.reject(seq, REJECT_DUPLICATE_ID, operation_id)
            return
        target = _parse_target(payload)
        if target is None:
            self.reject(seq, REJECT_INVALID_TARGET, operation_id)
            return
        expires_at = payload.get("expires_at")
        if clock.parse_datetime(expires_at) is None:
            self.reject(seq, REJECT_INVALID_TIMESTAMP, operation_id)
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            self.reject(seq, REJECT_INVALID_FEEDBACK, operation_id)
            return
        self.order.append(operation_id)
        self.feedback[operation_id] = text
        self.requests[operation_id] = RedirectRequest(
            operation_id=operation_id,
            target=target,
            state=STATE_SUBMITTED,
            created_at=created_at,
            expires_at=str(expires_at),
            feedback_bytes=len(text.encode("utf-8")),
            feedback_digest=feedback_digest(text),
            submitted_seq=seq,
        )

    def _apply_settlement(
        self,
        seq: int,
        payload: Mapping[str, Any],
        operation_id: str,
        created_at: str,
        kind: str,
    ) -> None:
        existing = self.requests.get(operation_id)
        if existing is None:
            self.reject(seq, REJECT_UNKNOWN_REQUEST, operation_id)
            return
        if existing.terminal:
            # First terminal record wins. A racing second acceptance is a line
            # that was not applied, never a request that changed its answer.
            self.reject(seq, REJECT_ALREADY_SETTLED, operation_id)
            return
        reason: SettlementReason | None = None
        if kind == KIND_REJECTED:
            try:
                reason = literals.as_literal(
                    payload.get("reason"),
                    SETTLEMENT_REASONS,
                    "reason",
                )
            except ValueError:
                self.reject(seq, REJECT_INVALID_REASON, operation_id)
                return
        elif kind == KIND_EXPIRED:
            reason = REASON_EXPIRED
        self.requests[operation_id] = RedirectRequest(
            operation_id=existing.operation_id,
            target=existing.target,
            state=literals.as_literal(kind, REQUEST_STATES, "kind"),
            created_at=existing.created_at,
            expires_at=existing.expires_at,
            feedback_bytes=existing.feedback_bytes,
            feedback_digest=existing.feedback_digest,
            submitted_seq=existing.submitted_seq,
            settled_at=created_at,
            settled_seq=seq,
            reason=reason,
        )


def _replay(data: bytes) -> _Replay:
    replay = _Replay()
    lines = _physical_lines(data)
    replay.last_seq = len(lines)
    replay.size_bytes = len(data)
    for seq, raw_bytes in enumerate(lines, start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        if not raw.strip():
            replay.reject(seq, REJECT_EMPTY_LINE)
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            # The message names a position, never the bytes at it.
            replay.reject(seq, REJECT_INVALID_JSON, error.msg)
            continue
        if not isinstance(payload, dict):
            replay.reject(seq, REJECT_NOT_OBJECT)
            continue
        replay.apply(seq, payload)
    return replay


def _view(replay: _Replay, journal_path: Path) -> RedirectJournalView:
    return RedirectJournalView(
        journal_path=journal_path,
        requests=tuple(replay.requests[key] for key in replay.order),
        rejected=tuple(replay.rejected),
        last_seq=replay.last_seq,
        size_bytes=replay.size_bytes,
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


def read_journal(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> RedirectJournalView:
    """Replay the whole journal under a shared lock."""
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    return _view(_replay(_read_shared(journal_path)), journal_path)


def read_feedback(
    plan_dir: str | Path,
    operation_id: str,
    *,
    state_dir: str | Path | None = None,
) -> str | None:
    """Return one request's correction text, or ``None`` if there is none.

    The only way the operator's words leave this module. Named per operation on
    purpose: a caller has to say which correction it is about to render, so a
    projection cannot pick the text up by walking a view.
    """
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    return _replay(_read_shared(journal_path)).feedback.get(operation_id)


def _read_shared(journal_path: Path) -> bytes:
    try:
        fd = os.open(journal_path, os.O_RDONLY)
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_READ, str(error), path=journal_path
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        return _read_locked(fd)
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_READ, str(error), path=journal_path
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _locked_journal(journal_path: Path) -> Iterator[tuple[int, _Replay]]:
    """Hold the journal exclusively and yield its current replay.

    Read, decide, and append all happen inside this block. Deciding outside it
    would validate against a view another producer has already invalidated,
    which is the whole failure mode idempotent submission has to survive.
    """
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(journal_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_WRITE, str(error), path=journal_path
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        yield fd, _replay(_read_locked(fd))
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_WRITE, str(error), path=journal_path
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _append_locked(
    fd: int,
    replay: _Replay,
    record: Mapping[str, Any],
    *,
    journal_path: Path,
    effective_max_bytes: int,
) -> int:
    """Append one record durably, returning its sequence number.

    The sequence is ``replay.last_seq + 1`` rather than a re-count of the file:
    the exclusive lock is still held, so the replay the caller decided against
    is still the file's contents, and a torn last line is terminated below
    rather than merged — either way this record lands on the next physical
    line.

    A short write is rolled back to the pre-append size. A torn tail would be
    replayed as a rejected line forever, and a producer told its submission
    failed must not find that submission accepted later.
    """
    existing_size = os.fstat(fd).st_size
    encoded = json.dumps(dict(record), separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    if existing_size and not _ends_with_newline(fd, existing_size):
        # An unterminated tail from a crashed writer: close its line so this
        # record is its own, rather than appending onto a half-written one.
        encoded = b"\n" + encoded
    encoded += b"\n"
    if existing_size + len(encoded) > effective_max_bytes:
        raise RedirectStoreError(
            ERROR_JOURNAL_FULL,
            f"append would exceed {effective_max_bytes} bytes",
            path=journal_path,
            details={"size_bytes": existing_size, "max_bytes": effective_max_bytes},
        )
    try:
        written = os.write(fd, encoded)
        if written != len(encoded):
            os.ftruncate(fd, existing_size)
            raise OSError(
                f"short redirect-journal append: wrote {written} of "
                f"{len(encoded)} bytes"
            )
        # A correction a producer has been told is durable must be on the
        # device before the lock that serialized it is released.
        durability.fullsync(fd)
    except OSError as error:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, existing_size)
        raise RedirectStoreError(
            ERROR_JOURNAL_WRITE, str(error), path=journal_path
        ) from error
    replay.last_seq += 1
    replay.size_bytes = existing_size + len(encoded)
    return replay.last_seq


def _ends_with_newline(fd: int, size: int) -> bool:
    os.lseek(fd, size - 1, os.SEEK_SET)
    return os.read(fd, 1) == b"\n"


def _record(kind: RecordKind, operation_id: str, created_at: str) -> dict[str, Any]:
    return {
        "version": REDIRECT_JOURNAL_VERSION,
        "kind": kind,
        "id": operation_id,
        "created_at": created_at,
    }


def _moment(now: datetime | None) -> datetime:
    return clock.utc_now() if now is None else now.astimezone(UTC)


def submit_request(
    plan_dir: str | Path,
    *,
    target: RedirectTarget,
    feedback: str,
    state_dir: str | Path | None = None,
    operation_id: str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> RedirectRequest:
    """Persist one correction against ``target`` and return its request.

    Idempotent in the only sense a retry needs: re-submitting an operation id
    that already names an identical request returns the existing record and
    appends nothing, so a producer that never learned the outcome of its first
    attempt can safely repeat it. Reusing an id for *different* content is a
    producer bug and is refused, because the alternative is silently replacing
    a correction that may already have been accepted.
    """
    validate_target(target)
    validate_feedback(feedback)
    ttl = _validate_ttl(ttl_seconds)
    if operation_id is not None:
        _require_bounded_text(
            operation_id,
            field_name="operation_id",
            max_chars=REDIRECT_MAX_OPERATION_ID_CHARS,
            reason=ERROR_INVALID_OPERATION_ID,
        )
    resolved_id = operation_id or new_operation_id()
    created = _moment(now)
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    digest = feedback_digest(feedback)

    with _locked_journal(journal_path) as (fd, replay):
        existing = replay.requests.get(resolved_id)
        if existing is not None:
            if (
                existing.feedback_digest != digest
                or not existing.target.identifies_same_turn(target)
            ):
                raise RedirectStoreError(
                    ERROR_INVALID_OPERATION_ID,
                    "operation id already names a different redirect request",
                    path=journal_path,
                    details={"operation_id": resolved_id},
                )
            return existing
        record = {
            **_record(KIND_SUBMITTED, resolved_id, clock.isoformat(created)),
            **target.to_payload(),
            "expires_at": clock.isoformat(created + timedelta(seconds=ttl)),
            "text": feedback,
        }
        seq = _append_locked(
            fd,
            replay,
            record,
            journal_path=journal_path,
            # A submission stops one tier below the cap so the settlement that
            # answers it always has room.
            effective_max_bytes=(
                REDIRECT_JOURNAL_MAX_BYTES - REDIRECT_SETTLEMENT_RESERVE_BYTES
            ),
        )
        return RedirectRequest(
            operation_id=resolved_id,
            target=target,
            state=STATE_SUBMITTED,
            created_at=str(record["created_at"]),
            expires_at=str(record["expires_at"]),
            feedback_bytes=len(feedback.encode("utf-8")),
            feedback_digest=digest,
            submitted_seq=seq,
        )


def _settle_locked(
    fd: int,
    replay: _Replay,
    *,
    journal_path: Path,
    operation_id: str,
    kind: RecordKind,
    reason: SettlementReason | None,
    moment: datetime,
) -> RedirectRequest:
    existing = replay.requests.get(operation_id)
    if existing is None:
        raise RedirectStoreError(
            ERROR_UNKNOWN_REQUEST,
            f"no redirect request named {operation_id!r}",
            path=journal_path,
            details={"operation_id": operation_id},
        )
    if existing.terminal:
        raise RedirectStoreError(
            ERROR_ALREADY_SETTLED,
            f"redirect request {operation_id!r} is already {existing.state}",
            path=journal_path,
            details={"operation_id": operation_id, "state": existing.state},
        )
    record = _record(kind, operation_id, clock.isoformat(moment))
    if reason is not None and kind == KIND_REJECTED:
        record["reason"] = reason
    seq = _append_locked(
        fd,
        replay,
        record,
        journal_path=journal_path,
        # Terminal records may use the reserve: settling what is already in
        # the journal is what keeps a full journal from stranding a request.
        effective_max_bytes=REDIRECT_JOURNAL_MAX_BYTES,
    )
    settled = RedirectRequest(
        operation_id=existing.operation_id,
        target=existing.target,
        state=literals.as_literal(kind, REQUEST_STATES, "kind"),
        created_at=existing.created_at,
        expires_at=existing.expires_at,
        feedback_bytes=existing.feedback_bytes,
        feedback_digest=existing.feedback_digest,
        submitted_seq=existing.submitted_seq,
        settled_at=str(record["created_at"]),
        settled_seq=seq,
        reason=REASON_EXPIRED if kind == KIND_EXPIRED else reason,
    )
    # Keep the in-lock view equal to the file. A caller settling several
    # requests under one lock must see each answer before deciding the next.
    replay.requests[operation_id] = settled
    return settled


def accept_request(
    plan_dir: str | Path,
    operation_id: str,
    *,
    target: RedirectTarget,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> RedirectRequest:
    """Record the runner's acceptance of one submitted request.

    Acceptance is the runner's statement, not the producer's: only the process
    that owns the live turn calls this, and only the record it writes makes a
    submission an accepted redirect. Three refusals are load-bearing —

    - a request already accepted returns its existing record and appends
      nothing, so a controller that retries after a crash cannot redirect the
      turn twice;
    - a request whose target is not the live turn is refused as a mismatch, so
      a correction aimed at a finished turn never lands on its successor;
    - a request past ``expires_at`` is *expired in the journal* and then
      refused, so the refusal is durable and no later caller can accept it.
    """
    validate_target(target)
    moment = _moment(now)
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        existing = replay.requests.get(operation_id)
        if existing is None:
            raise RedirectStoreError(
                ERROR_UNKNOWN_REQUEST,
                f"no redirect request named {operation_id!r}",
                path=journal_path,
                details={"operation_id": operation_id},
            )
        if existing.accepted and existing.target.identifies_same_turn(target):
            return existing
        if existing.terminal:
            # An already-expired request reports expiry rather than the generic
            # settlement: whether the deadline was recorded by a sweep or is
            # noticed here should not change what the caller is told.
            raise RedirectStoreError(
                (
                    ERROR_REQUEST_EXPIRED
                    if existing.state == STATE_EXPIRED
                    else ERROR_ALREADY_SETTLED
                ),
                f"redirect request {operation_id!r} is already {existing.state}",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "state": existing.state,
                    "expires_at": existing.expires_at,
                },
            )
        if not existing.target.identifies_same_turn(target):
            settled = _settle_locked(
                fd,
                replay,
                journal_path=journal_path,
                operation_id=operation_id,
                kind=KIND_REJECTED,
                reason=REASON_STALE_TARGET,
                moment=moment,
            )
            raise RedirectStoreError(
                ERROR_TARGET_MISMATCH,
                "redirect request names a turn that is not the live one",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "request_target": existing.target.to_payload(),
                    "live_target": target.to_payload(),
                    "state": settled.state,
                },
            )
        if existing.is_expired_at(moment):
            settled = _settle_locked(
                fd,
                replay,
                journal_path=journal_path,
                operation_id=operation_id,
                kind=KIND_EXPIRED,
                reason=REASON_EXPIRED,
                moment=moment,
            )
            raise RedirectStoreError(
                ERROR_REQUEST_EXPIRED,
                f"redirect request {operation_id!r} expired at {existing.expires_at}",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "expires_at": existing.expires_at,
                    "state": settled.state,
                },
            )
        return _settle_locked(
            fd,
            replay,
            journal_path=journal_path,
            operation_id=operation_id,
            kind=KIND_ACCEPTED,
            reason=None,
            moment=moment,
        )


def reject_request(
    plan_dir: str | Path,
    operation_id: str,
    *,
    reason: SettlementReason,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> RedirectRequest:
    """Settle one submitted request without accepting it."""
    if reason not in SETTLEMENT_REASONS:
        raise RedirectStoreError(
            ERROR_INVALID_REASON, f"unknown redirect settlement reason {reason!r}"
        )
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        return _settle_locked(
            fd,
            replay,
            journal_path=journal_path,
            operation_id=operation_id,
            kind=KIND_REJECTED,
            reason=reason,
            moment=_moment(now),
        )


def expire_due_requests(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[RedirectRequest, ...]:
    """Expire every submitted request whose window has closed, oldest first.

    Expiry is recorded rather than inferred. A reader that computes "this is
    past its deadline" reaches the same conclusion, but only a written record
    makes the refusal survive the process that reached it — which is what
    ``expired requests cannot be accepted later`` has to mean across a crash.
    """
    moment = _moment(now)
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        due = [
            operation_id
            for operation_id in replay.order
            if replay.requests[operation_id].state == STATE_SUBMITTED
            and replay.requests[operation_id].is_expired_at(moment)
        ]
        return tuple(
            _settle_locked(
                fd,
                replay,
                journal_path=journal_path,
                operation_id=operation_id,
                kind=KIND_EXPIRED,
                reason=REASON_EXPIRED,
                moment=moment,
            )
            for operation_id in due
        )
