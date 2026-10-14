"""Request lifecycle: submit, accept, reject, and expire.

Split out of the original ``redirect_store`` module; see
:mod:`agent_session_loop.redirect_store_vocab` for the journal's design.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from . import clock, literals
from .redirect_store_journal import (
    _append_locked,
    _locked_journal,
    _moment,
    _record,
    journal_path_for_plan,
)
from .redirect_store_models import (
    RedirectRequest,
    RedirectStoreError,
    RedirectTarget,
)
from .redirect_store_replay import _Replay
from .redirect_store_validation import (
    _require_bounded_text,
    _validate_ttl,
    feedback_digest,
    new_operation_id,
    validate_feedback,
    validate_target,
)
from .redirect_store_vocab import (
    ERROR_ALREADY_SETTLED,
    ERROR_INVALID_OPERATION_ID,
    ERROR_INVALID_REASON,
    ERROR_REQUEST_EXPIRED,
    ERROR_TARGET_MISMATCH,
    ERROR_UNKNOWN_REQUEST,
    KIND_ACCEPTED,
    KIND_EXPIRED,
    KIND_REJECTED,
    KIND_SUBMITTED,
    REASON_EXPIRED,
    REASON_STALE_TARGET,
    REDIRECT_JOURNAL_MAX_BYTES,
    REDIRECT_MAX_OPERATION_ID_CHARS,
    REDIRECT_SETTLEMENT_RESERVE_BYTES,
    REQUEST_STATES,
    SETTLEMENT_REASONS,
    STATE_EXPIRED,
    STATE_SUBMITTED,
    RecordKind,
    SettlementReason,
)


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
