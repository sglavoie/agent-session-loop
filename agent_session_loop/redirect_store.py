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
  what stops two turns from being redirected by one request. ``continued`` and
  ``acknowledged`` are annotations *on* an accepted request rather than states
  of it: they record which continuation attempt carried the correction and that
  a worker running that exact attempt receipted it, and neither may precede the
  acceptance it annotates or repeat itself.
- **The feedback text never leaves this module by accident.** No parsed object
  carries it: :class:`RedirectRequest` holds a byte count and a digest, and
  rejected lines are reported without their raw bytes because a malformed line
  can still contain the operator's words. Reading the text at all requires
  calling :func:`read_feedback` and saying which operation you want.
- **A submission cannot consume the capacity that settles it.** The journal is
  bounded, and submissions stop one tier below the cap so a full journal can
  still record the acceptance, rejection, or expiry of what is already in it.

A ``continued`` line ordinarily names a later turn of the request's own run.
The one exception is marked on the line itself: ``recovered`` says a run that
died was holding this correction and the run resuming the same session took
delivery of it, which is the only continuation whose run id differs from the
target's and whose turn index may be lower. Replay holds an unmarked line to the
ordinary ordering, so the latitude is reachable only from a line that asked for
it, and the matching ``acknowledged`` line has to restate the same marker.

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
from dataclasses import dataclass, field, replace
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

RecordKind = Literal[
    "submitted",
    "accepted",
    "rejected",
    "expired",
    "continued",
    "acknowledged",
]
KIND_SUBMITTED: RecordKind = "submitted"
KIND_ACCEPTED: RecordKind = "accepted"
KIND_REJECTED: RecordKind = "rejected"
KIND_EXPIRED: RecordKind = "expired"
KIND_CONTINUED: RecordKind = "continued"
KIND_ACKNOWLEDGED: RecordKind = "acknowledged"
RECORD_KINDS: tuple[RecordKind, ...] = (
    KIND_SUBMITTED,
    KIND_ACCEPTED,
    KIND_REJECTED,
    KIND_EXPIRED,
    KIND_CONTINUED,
    KIND_ACKNOWLEDGED,
)
#: The kinds that describe what happened to an already-accepted request. They
#: are named as a group because the reducer routes on the group rather than on
#: each kind, and because they change no state: ``a request is settled once``
#: holds no matter how many of these a request collects. The settlements are the
#: remainder, which is how :meth:`_Replay._apply` reaches them.
ANNOTATION_KINDS: tuple[RecordKind, ...] = (
    KIND_CONTINUED,
    KIND_ACKNOWLEDGED,
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
ERROR_INVALID_CONTINUATION = "invalid-redirect-continuation"
ERROR_NOT_ACCEPTED = "redirect-not-accepted"
ERROR_ALREADY_CARRIED = "redirect-already-carried"
ERROR_NOT_CARRIED = "redirect-not-carried"
ERROR_CONTINUATION_MISMATCH = "redirect-continuation-mismatch"

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
REJECT_INVALID_CONTINUATION = "invalid-continuation"
REJECT_NOT_ACCEPTED = "not-accepted"
REJECT_ALREADY_CARRIED = "already-carried"
REJECT_NOT_CARRIED = "not-carried"
REJECT_CONTINUATION_MISMATCH = "continuation-mismatch"
REJECT_ALREADY_ACKNOWLEDGED = "already-acknowledged"


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

    ``continuation`` is the attempt that actually carried this correction into a
    prompt, recorded when the run finalizes that prompt. It is what makes a
    worker receipt checkable: acknowledging a correction means naming the
    attempt it was delivered in, and an attempt that never carried it -- a stale
    predecessor, a transcript echo of an older attempt -- names the wrong one.
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
    continuation: RedirectTarget | None = None
    continued_at: str | None = None
    continued_seq: int | None = None
    continuation_recovered: bool = False
    acknowledged_at: str | None = None
    acknowledged_seq: int | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def accepted(self) -> bool:
        return self.state == STATE_ACCEPTED

    @property
    def carried(self) -> bool:
        """Whether a continuation attempt has taken delivery of this correction."""
        return self.continuation is not None

    @property
    def recovered(self) -> bool:
        """Whether the attempt that carried this correction crossed a crash.

        A continuation of the ordinary kind is a later turn of the *same* run,
        so the marker is only ever true for a correction a new run picked up
        from a run that died holding it. It is kept as its own fact rather than
        inferred from a run-id comparison because the inference would also fire
        on a hand-written line, and the whole point of the marker is that a
        cross-run delivery must have been asked for explicitly.
        """
        return self.continuation_recovered

    @property
    def acknowledged(self) -> bool:
        """Whether a worker receipted this correction on its own attempt."""
        return self.acknowledged_at is not None

    def continues_attempt(self, attempt: RedirectTarget) -> bool:
        """Whether ``attempt`` is the continuation this correction was carried in."""
        return self.continuation is not None and self.continuation.identifies_same_turn(
            attempt
        )

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
            "continuation": (
                self.continuation.to_payload()
                if self.continuation is not None
                else None
            ),
            "continued_at": self.continued_at,
            "continued_seq": self.continued_seq,
            "continuation_recovered": self.continuation_recovered,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_seq": self.acknowledged_seq,
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
    torn_tail: bool = False
    """Whether the file's last physical line was never terminated.

    A writer that died between ``write`` and its ``fullsync`` -- or a device
    that lost the tail of a page -- leaves a final line with no newline after
    it. Replay already refuses that line, and the next append terminates it
    rather than merging with it, so nothing here is broken. What the flag adds
    is that the refusal is *attributable*: a projection built after a crash can
    say "one record may be missing from this answer" instead of presenting a
    possibly-truncated history as the whole truth. Mid-file corruption shows up
    in :attr:`rejected` instead, which is why the two are separate.
    """

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

    def pending_before(
        self,
        continuation: RedirectTarget,
    ) -> tuple[RedirectRequest, ...]:
        """Unsettled requests ``continuation`` may still carry, oldest first.

        The coalescing boundary, read rather than computed by a caller: a
        correction aimed at an earlier attempt of this run's same session was
        submitted before this prompt was finalized, so it belongs in it. One
        aimed at this attempt is a redirect against a turn that is about to be
        live and is left alone.
        """
        return tuple(
            request
            for request in self.pending()
            if _precedes_continuation(request.target, continuation)
        )

    def carried_for(self, attempt: RedirectTarget) -> tuple[RedirectRequest, ...]:
        """Requests this exact continuation attempt carried, oldest first."""
        return tuple(
            request for request in self.requests if request.continues_attempt(attempt)
        )

    def strandable_by(self, run_id: str) -> tuple[RedirectRequest, ...]:
        """Requests a crash left unfinished for some *other* run, oldest first.

        "Unfinished" is exactly two states: never settled, and settled as
        accepted but never carried into a prompt. Both describe a correction the
        operator was promised something about and whose promise no process is
        keeping any more, because the run named in the target is not this one.

        A carried request is excluded on purpose even when nobody receipted it.
        The journal records that some attempt took delivery of it, and the whole
        acknowledgment protocol exists because delivery is not receipt: deciding
        here that an unreceipted delivery never happened would be the guess that
        protocol refuses to make. :mod:`redirect_recovery` reports those as
        uncertain instead.
        """
        return tuple(
            request
            for request in self.requests
            if request.target.run_id != run_id
            and not request.carried
            and (not request.terminal or request.accepted)
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "journal_path": str(self.journal_path),
            "requests": [request.to_payload() for request in self.requests],
            "rejected": [record.to_payload() for record in self.rejected],
            "last_seq": self.last_seq,
            "size_bytes": self.size_bytes,
            "torn_tail": self.torn_tail,
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


def _precedes_continuation(
    target: RedirectTarget,
    continuation: RedirectTarget,
) -> bool:
    """Whether ``target`` names an earlier attempt of ``continuation``'s session.

    The turn index has to be strictly lower. A correction is carried forward
    into the attempt that *succeeds* the one it interrupted, so a continuation
    that claimed the same index as the request's own target would be describing
    the interrupted turn as its own successor.
    """
    return (
        target.run_id == continuation.run_id
        and target.session_id == continuation.session_id
        and target.turn_index < continuation.turn_index
    )


def _recovers_continuation(
    target: RedirectTarget,
    continuation: RedirectTarget,
) -> bool:
    """Whether ``continuation`` is a *different run's* retry of ``target``'s session.

    The turn index is deliberately not compared. A run numbers its turns from
    one, so a correction aimed at turn 7 of a run that died is legitimately
    carried by turn 1 of the run that resumes the same session, and requiring a
    greater index would refuse every real crash recovery there is. What still
    has to hold is the session: a correction is about the work of one session,
    and carrying it into another is delivering it to a worker it was not
    written for.

    Requiring a *different* run id is what keeps this from widening the ordinary
    rule. Within one run the strict ordering of :func:`_precedes_continuation`
    remains the only way a correction moves forward.
    """
    return (
        target.session_id == continuation.session_id
        and target.run_id != continuation.run_id
    )


def validate_continuation(
    target: RedirectTarget,
    continuation: RedirectTarget,
    *,
    allow_recovery: bool = False,
) -> bool:
    """Check ``continuation`` may carry ``target``; return whether it recovers it.

    ``allow_recovery`` is opt-in and off by default, so every existing caller
    keeps the session-12 rule exactly: a continuation is a later turn of the
    same run and session. A caller that has established the target's run is gone
    -- :mod:`redirect_recovery`, under the new run's own lock -- passes it to
    admit the one other continuation that can honour the correction, and the
    ``True`` it gets back is what the journal records on the line, so a
    cross-crash delivery is legible as one rather than looking like an ordinary
    carry with a surprising run id in it.
    """
    validate_target(continuation)
    if _precedes_continuation(target, continuation):
        return False
    if allow_recovery and _recovers_continuation(target, continuation):
        return True
    raise RedirectStoreError(
        ERROR_INVALID_CONTINUATION,
        "continuation must be a later attempt of the same run and session",
        details={
            "request_target": target.to_payload(),
            "continuation": continuation.to_payload(),
            "recovery_allowed": allow_recovery,
        },
    )


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


def _parse_target(payload: object) -> RedirectTarget | None:
    """Read one target out of a journal line's mapping, or ``None`` if malformed.

    Accepts ``object`` rather than a mapping because two of its callers read a
    nested value out of a line another process wrote: a ``continuation`` that is
    absent, null, or not an object is a line to reject, not a crash to raise.
    """
    if not isinstance(payload, Mapping):
        return None
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
        self.torn_tail = False

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
        if kind in ANNOTATION_KINDS:
            self._apply_annotation(seq, payload, operation_id, str(created_at), kind)
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

    def _apply_annotation(
        self,
        seq: int,
        payload: Mapping[str, Any],
        operation_id: str,
        created_at: str,
        kind: str,
    ) -> None:
        """Apply a ``continued`` or ``acknowledged`` line to an accepted request.

        Every condition the appenders check is re-checked here, because a replay
        that trusted the file would let a hand-edited or racing line invent a
        receipt for an attempt that never carried the correction — and a receipt
        is the whole evidence that a worker read it.
        """
        existing = self.requests.get(operation_id)
        if existing is None:
            self.reject(seq, REJECT_UNKNOWN_REQUEST, operation_id)
            return
        continuation = _parse_target(payload.get("continuation"))
        if continuation is None:
            self.reject(seq, REJECT_INVALID_CONTINUATION, operation_id)
            return
        # The relaxed cross-run rule is reachable only from a line that says so.
        # An unmarked line is held to the ordinary ordering, so a hand-written
        # continuation cannot borrow recovery's latitude by omitting the marker.
        recovered = payload.get("recovered") is True
        admissible = (
            _recovers_continuation(existing.target, continuation)
            if recovered
            else _precedes_continuation(existing.target, continuation)
        )
        if not admissible:
            self.reject(seq, REJECT_INVALID_CONTINUATION, operation_id)
            return
        if kind == KIND_CONTINUED:
            if not existing.accepted:
                self.reject(
                    seq,
                    (
                        REJECT_ALREADY_SETTLED
                        if existing.terminal
                        else REJECT_NOT_ACCEPTED
                    ),
                    operation_id,
                )
                return
            if existing.carried:
                self.reject(seq, REJECT_ALREADY_CARRIED, operation_id)
                return
            self.requests[operation_id] = replace(
                existing,
                continuation=continuation,
                continued_at=created_at,
                continued_seq=seq,
                continuation_recovered=recovered,
            )
            return
        if not existing.carried:
            self.reject(seq, REJECT_NOT_CARRIED, operation_id)
            return
        if not existing.continues_attempt(continuation):
            self.reject(seq, REJECT_CONTINUATION_MISMATCH, operation_id)
            return
        if recovered != existing.continuation_recovered:
            # The receipt has to name delivery as the journal recorded it. A
            # line that agrees on the turn but disagrees on whether that turn
            # crossed a crash is describing a delivery that never happened.
            self.reject(seq, REJECT_CONTINUATION_MISMATCH, operation_id)
            return
        if existing.acknowledged:
            self.reject(seq, REJECT_ALREADY_ACKNOWLEDGED, operation_id)
            return
        self.requests[operation_id] = replace(
            existing,
            acknowledged_at=created_at,
            acknowledged_seq=seq,
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
        self.requests[operation_id] = replace(
            existing,
            state=literals.as_literal(kind, REQUEST_STATES, "kind"),
            settled_at=created_at,
            settled_seq=seq,
            reason=reason,
        )


def _replay(data: bytes) -> _Replay:
    replay = _Replay()
    lines = _physical_lines(data)
    replay.last_seq = len(lines)
    replay.size_bytes = len(data)
    replay.torn_tail = bool(data) and not data.endswith(b"\n")
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
        torn_tail=replay.torn_tail,
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


def read_journal_at(state_dir: str | Path) -> RedirectJournalView:
    """Replay the journal in one plan-state directory named directly.

    :func:`read_journal` resolves a plan directory to its hashed state
    directory, which is the right addressing for everything that runs while a
    plan is where it says it is. A migrator holds the hashed directory itself
    and no longer has a plan path that maps to it, so it names the directory.
    Read-only on the same terms as every other read here.
    """
    journal_path = Path(state_dir) / REDIRECT_JOURNAL_FILE_NAME
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
    settled = replace(
        existing,
        state=literals.as_literal(kind, REQUEST_STATES, "kind"),
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


def _annotate_locked(
    fd: int,
    replay: _Replay,
    *,
    journal_path: Path,
    existing: RedirectRequest,
    kind: RecordKind,
    continuation: RedirectTarget,
    moment: datetime,
    recovered: bool = False,
) -> RedirectRequest:
    """Append one annotation to an accepted request and return the new record.

    Annotations are charged against the settlement reserve rather than the
    submission tier. They are records *about* a request already in the journal,
    at most one of each per accepted request, so they add no unbounded demand —
    and refusing one would leave an accepted correction with no durable trace of
    which attempt carried it, which is the only thing a receipt can be checked
    against.
    """
    record: dict[str, Any] = {
        **_record(kind, existing.operation_id, clock.isoformat(moment)),
        "continuation": continuation.to_payload(),
    }
    if recovered:
        # Written only when true. An absent key and a false one mean the same
        # thing to replay, and the journal's older lines have neither.
        record["recovered"] = True
    seq = _append_locked(
        fd,
        replay,
        record,
        journal_path=journal_path,
        effective_max_bytes=REDIRECT_JOURNAL_MAX_BYTES,
    )
    created_at = str(record["created_at"])
    if kind == KIND_CONTINUED:
        annotated = replace(
            existing,
            continuation=continuation,
            continued_at=created_at,
            continued_seq=seq,
            continuation_recovered=recovered,
        )
    else:
        annotated = replace(
            existing,
            acknowledged_at=created_at,
            acknowledged_seq=seq,
        )
    # Keep the in-lock view equal to the file, so a caller carrying several
    # corrections under one lock decides each one against the last.
    replay.requests[existing.operation_id] = annotated
    return annotated


def carry_request(
    plan_dir: str | Path,
    operation_id: str,
    *,
    continuation: RedirectTarget,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
    allow_recovery: bool = False,
) -> RedirectRequest:
    """Bind one correction to the continuation attempt that will deliver it.

    Called by the runner at the moment the resumed prompt is finalized, which
    is what makes this the coalescing boundary: a correction carried here is in
    that prompt, and one submitted afterwards is a redirect against the attempt
    the prompt is about to start rather than a late addition to it.

    A still-submitted request is accepted and carried under the same lock, so a
    correction the operator sent after the interrupted turn was already gone is
    delivered rather than left to expire. An already-accepted request -- the
    ordinary case, accepted mid-turn by the gate -- is only annotated.

    Four refusals matter:

    - carrying the same request into the same continuation again returns the
      existing record and appends nothing, so a runner that retries after a
      crash cannot deliver one correction as two;
    - a request already carried by a *different* attempt is refused, because
      that attempt's worker may already have receipted it;
    - a request whose target is not an earlier attempt of this continuation's
      own run and session is refused without being settled, since it is still a
      valid correction for the turn it actually names;
    - a submitted request past ``expires_at`` is expired in the journal and then
      refused, exactly as :func:`accept_request` does it.

    ``allow_recovery`` widens only the third of those, and only to a
    continuation of the same session under a *different* run id, which is the
    shape a crash leaves behind: the run holding the correction died, and the
    run that resumes the session mints its own id and numbers its turns from
    one. The resulting line records ``recovered``, so what crossed the crash is
    a fact in the journal rather than an inference from two run ids.
    """
    validate_target(continuation)
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
        if existing.carried:
            if existing.continues_attempt(continuation):
                return existing
            raise RedirectStoreError(
                ERROR_ALREADY_CARRIED,
                f"redirect request {operation_id!r} was already carried by "
                f"another continuation attempt",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "continuation": existing.to_payload()["continuation"],
                    "requested_continuation": continuation.to_payload(),
                },
            )
        if existing.terminal and not existing.accepted:
            raise RedirectStoreError(
                (
                    ERROR_REQUEST_EXPIRED
                    if existing.state == STATE_EXPIRED
                    else ERROR_ALREADY_SETTLED
                ),
                f"redirect request {operation_id!r} is already {existing.state}",
                path=journal_path,
                details={"operation_id": operation_id, "state": existing.state},
            )
        # Checked before any append: a correction aimed elsewhere keeps its
        # pending state, so refusing to carry it here cannot retire it.
        recovered = validate_continuation(
            existing.target,
            continuation,
            allow_recovery=allow_recovery,
        )
        if not existing.accepted:
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
                    f"redirect request {operation_id!r} expired at "
                    f"{existing.expires_at}",
                    path=journal_path,
                    details={
                        "operation_id": operation_id,
                        "expires_at": existing.expires_at,
                        "state": settled.state,
                    },
                )
            existing = _settle_locked(
                fd,
                replay,
                journal_path=journal_path,
                operation_id=operation_id,
                kind=KIND_ACCEPTED,
                reason=None,
                moment=moment,
            )
        return _annotate_locked(
            fd,
            replay,
            journal_path=journal_path,
            existing=existing,
            kind=KIND_CONTINUED,
            continuation=continuation,
            moment=moment,
            recovered=recovered,
        )


def acknowledge_request(
    plan_dir: str | Path,
    operation_id: str,
    *,
    attempt: RedirectTarget,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> RedirectRequest:
    """Record a worker's receipt for one correction on the attempt it ran.

    The receipt is explicit and doubly identified: the worker names the
    correction *and* the attempt it is running. Nothing else counts as
    acknowledgment — a pipe write, a transcript replaying an older attempt's
    prompt, or a harness accepting the prompt all happen without a worker having
    read anything. Naming an attempt that did not carry this correction is
    therefore refused rather than treated as close enough, which is what keeps a
    stale predecessor and a transcript echo from settling it.

    Repeating the receipt for the same attempt returns the existing record and
    appends nothing: a worker that retries an interrupted command must not make
    the journal claim it read the correction twice.
    """
    validate_target(attempt)
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
        if not existing.accepted:
            raise RedirectStoreError(
                (
                    ERROR_REQUEST_EXPIRED
                    if existing.state == STATE_EXPIRED
                    else ERROR_ALREADY_SETTLED
                    if existing.terminal
                    else ERROR_NOT_ACCEPTED
                ),
                f"redirect request {operation_id!r} is {existing.state}, not accepted",
                path=journal_path,
                details={"operation_id": operation_id, "state": existing.state},
            )
        carried_into = existing.continuation
        if carried_into is None:
            raise RedirectStoreError(
                ERROR_NOT_CARRIED,
                f"redirect request {operation_id!r} has not been carried into a "
                f"continuation attempt yet",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "attempt": attempt.to_payload(),
                },
            )
        if not existing.continues_attempt(attempt):
            raise RedirectStoreError(
                ERROR_CONTINUATION_MISMATCH,
                f"redirect request {operation_id!r} was carried by a different "
                f"attempt than the one acknowledging it",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "continuation": existing.to_payload()["continuation"],
                    "attempt": attempt.to_payload(),
                },
            )
        if existing.acknowledged:
            return existing
        return _annotate_locked(
            fd,
            replay,
            journal_path=journal_path,
            existing=existing,
            kind=KIND_ACKNOWLEDGED,
            # The recorded continuation, not the caller's copy: they identify
            # the same turn, and the journal should say what it already knows.
            continuation=carried_into,
            moment=_moment(now),
            # Same reasoning for the marker: the receipt restates the delivery
            # the journal recorded, so a worker cannot receipt an ordinary carry
            # as a recovered one or the other way round.
            recovered=existing.continuation_recovered,
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
