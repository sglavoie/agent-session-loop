"""Bounds checks for redirect targets, continuations, feedback, and TTLs.

Split out of the original ``redirect_store`` module; see
:mod:`agent_session_loop.redirect_store_vocab` for the journal's design.
"""

from __future__ import annotations

import hashlib
import uuid

from .redirect_store_models import (
    RedirectStoreError,
    RedirectTarget,
    _precedes_continuation,
    _recovers_continuation,
)
from .redirect_store_vocab import (
    CONFLICT_FEEDBACK,
    CONFLICT_TARGET,
    ERROR_INVALID_CONTINUATION,
    ERROR_INVALID_FEEDBACK,
    ERROR_INVALID_TARGET,
    ERROR_INVALID_TTL,
    REDIRECT_DEFAULT_TTL_SECONDS,
    REDIRECT_MAX_FEEDBACK_BYTES,
    REDIRECT_MAX_HARNESS_CHARS,
    REDIRECT_MAX_RUN_ID_CHARS,
    REDIRECT_MAX_SESSION_ID_CHARS,
    REDIRECT_MAX_TTL_SECONDS,
    REDIRECT_MAX_TURN_INDEX,
    REDIRECT_MIN_TTL_SECONDS,
)


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


def replay_conflict(
    recorded_digest: str,
    recorded_target: RedirectTarget,
    *,
    feedback: str,
    expected_target: RedirectTarget | None,
) -> str | None:
    """Name what a resubmission changed about a request already recorded here.

    :func:`submit_request` already refuses an operation id that names different
    content, and that refusal is the rule. This function exists because the
    public surfaces never reach it: a browser or a bridge client whose response
    was lost retries the whole request, so those surfaces answer an id the
    journal already holds *before* re-checking a freshness proof the landed
    correction itself invalidated. That short-circuit is what makes a retry
    possible, and it is also the only place an operator's second, different
    correction can be reported as a successful replay of their first. Asking
    this before serving the replay is what keeps the two apart.

    The comparison is on identity, never on words: the digest names the
    correction, and ``expected_target`` is checked only when the caller supplied
    one, exactly as a fresh submission checks it. Returns ``None`` when the
    resubmission is the recorded request, and otherwise which half of its
    identity disagrees, so a surface can tell an operator what to reconcile.
    """
    if feedback_digest(feedback) != recorded_digest:
        return CONFLICT_FEEDBACK
    if expected_target is not None and not recorded_target.identifies_same_turn(
        expected_target
    ):
        return CONFLICT_TARGET
    return None
