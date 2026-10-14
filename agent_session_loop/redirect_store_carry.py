"""Carry lifecycle: bind a correction to a continuation and declare its launch.

Split out of the original ``redirect_store`` module; see
:mod:`agent_session_loop.redirect_store_vocab` for the journal's design.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import clock
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
from .redirect_store_requests import _settle_locked
from .redirect_store_validation import validate_continuation, validate_target
from .redirect_store_vocab import (
    ERROR_ALREADY_CARRIED,
    ERROR_ALREADY_SETTLED,
    ERROR_CONTINUATION_MISMATCH,
    ERROR_NOT_ACCEPTED,
    ERROR_NOT_CARRIED,
    ERROR_REQUEST_EXPIRED,
    ERROR_UNKNOWN_REQUEST,
    KIND_ACCEPTED,
    KIND_CONTINUED,
    KIND_DELIVERED,
    KIND_EXPIRED,
    KIND_LAUNCHING,
    KIND_RELEASED,
    REASON_EXPIRED,
    REDIRECT_JOURNAL_MAX_BYTES,
    STATE_EXPIRED,
    RecordKind,
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
    and refusing one would leave an accepted correction with no durable trace of
    which attempt carried it, which is the only thing a receipt can be checked
    against.

    Their demand is bounded by the run loop rather than by the record kinds.
    ``launching``, ``delivered`` and ``acknowledged`` are still at most one
    apiece, and a release-then-recarry cycle costs two more lines per attempt
    that gave up before launching -- bounded by the run's own attempt limits,
    which already bound the submission tier.
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
    annotated: RedirectRequest
    if kind == KIND_CONTINUED:
        annotated = replace(
            existing,
            continuation=continuation,
            continued_at=created_at,
            continued_seq=seq,
            continuation_recovered=recovered,
            launching_at=None,
            launching_seq=None,
            launch_unrecorded=False,
        )
    elif kind == KIND_LAUNCHING:
        annotated = replace(
            existing,
            launching_at=created_at,
            launching_seq=seq,
        )
    elif kind == KIND_DELIVERED:
        annotated = replace(
            existing,
            delivered_at=created_at,
            delivered_seq=seq,
        )
    elif kind == KIND_RELEASED:
        annotated = replace(
            existing,
            continuation=None,
            continued_at=None,
            continued_seq=None,
            continuation_recovered=False,
            launching_at=None,
            launching_seq=None,
            launch_unrecorded=False,
            released_at=created_at,
            released_seq=seq,
            released_from=continuation,
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


def _require_carried_by(
    replay: _Replay,
    operation_id: str,
    *,
    journal_path: Path,
    continuation: RedirectTarget,
) -> RedirectRequest:
    """Return the request ``continuation`` currently holds a carry on.

    Shared by the annotations that speak about an existing carry rather than
    creating one. Each has exactly one thing to prove before it may append --
    that the carry on the record is *this* attempt's -- and each has to refuse a
    settled, uncarried or foreign-carried request the same way, because the
    caller cannot tell those apart from a successful no-op.
    """
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
    if not existing.carried:
        raise RedirectStoreError(
            ERROR_NOT_CARRIED,
            f"redirect request {operation_id!r} has not been carried into a "
            f"continuation attempt yet",
            path=journal_path,
            details={
                "operation_id": operation_id,
                "continuation": continuation.to_payload(),
            },
        )
    if not existing.continues_attempt(continuation):
        raise RedirectStoreError(
            ERROR_CONTINUATION_MISMATCH,
            f"redirect request {operation_id!r} was carried by a different "
            f"attempt than the one annotating it",
            path=journal_path,
            details={
                "operation_id": operation_id,
                "continuation": existing.to_payload()["continuation"],
                "attempt": continuation.to_payload(),
            },
        )
    return existing


def launch_request(
    plan_dir: str | Path,
    operation_id: str,
    *,
    continuation: RedirectTarget,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> RedirectRequest:
    """Declare that ``continuation`` is about to let this carry leave the process.

    The line a caller must get onto disk *before* it can submit the prompt, and
    the reason a missing :func:`deliver_request` means something. Up to here the
    words provably never went out, so a dead attempt's carry can be handed back
    to the next one. Past here nobody reading the file can tell a prompt that
    never left from one that left and outlived its writer, so the carry stops
    being automatically recoverable and becomes an operator's question --
    :attr:`RedirectRequest.launch_ambiguous`.

    That is the whole trade: one extra fsync per carried correction per attempt,
    in exchange for never delivering the same correction twice on the strength
    of a silence. A caller that cannot append this must not submit the prompt,
    because doing so would re-create the very ambiguity the line exists to
    bound -- except that nothing would record it.

    Repeating the declaration for the same continuation returns the existing
    record and appends nothing, and so does one made after the delivery it
    precedes, so a retried launch neither doubles the line nor un-delivers a
    correction.
    """
    validate_target(continuation)
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        existing = _require_carried_by(
            replay,
            operation_id,
            journal_path=journal_path,
            continuation=continuation,
        )
        if existing.launching or existing.reached_a_worker:
            return existing
        return _annotate_locked(
            fd,
            replay,
            journal_path=journal_path,
            existing=existing,
            kind=KIND_LAUNCHING,
            continuation=existing.continuation or continuation,
            moment=_moment(now),
            recovered=existing.continuation_recovered,
        )
