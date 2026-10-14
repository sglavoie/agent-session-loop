"""Delivery lifecycle: record delivery, release, and worker acknowledgement.

Split out of the original ``redirect_store`` module; see
:mod:`agent_session_loop.redirect_store_vocab` for the journal's design.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .redirect_store_carry import _annotate_locked, _require_carried_by
from .redirect_store_journal import _locked_journal, _moment, journal_path_for_plan
from .redirect_store_models import (
    RedirectRequest,
    RedirectStoreError,
    RedirectTarget,
)
from .redirect_store_validation import validate_target
from .redirect_store_vocab import (
    ERROR_ALREADY_ACKNOWLEDGED,
    ERROR_ALREADY_DELIVERED,
    ERROR_ALREADY_SETTLED,
    ERROR_CONTINUATION_MISMATCH,
    ERROR_NOT_ACCEPTED,
    ERROR_NOT_CARRIED,
    ERROR_REQUEST_EXPIRED,
    ERROR_UNKNOWN_REQUEST,
    KIND_ACKNOWLEDGED,
    KIND_DELIVERED,
    KIND_RELEASED,
    STATE_EXPIRED,
)


def deliver_request(
    plan_dir: str | Path,
    operation_id: str,
    *,
    continuation: RedirectTarget,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> RedirectRequest:
    """Record that ``continuation`` actually launched a worker with this carry.

    :func:`carry_request` records an undertaking made while a prompt is still
    being assembled; this records that the undertaking was kept. Everything
    between the two is a window in which the attempt can still die -- a prompt
    build that refuses, a conversation the run cannot prove is usable, a crash --
    and only a mark on this side of it justifies treating the correction as
    spent. Without it a reader has to choose between stranding words a live
    worker is acting on and losing words nothing ever saw; with it, neither
    guess is needed.

    Repeating the mark for the same continuation returns the existing record and
    appends nothing, so a retried launch does not claim two deliveries.
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
        if existing.delivered:
            return existing
        return _annotate_locked(
            fd,
            replay,
            journal_path=journal_path,
            existing=existing,
            kind=KIND_DELIVERED,
            # The recorded continuation, not the caller's copy: they identify
            # the same turn, and the journal should say what it already knows.
            continuation=existing.continuation or continuation,
            moment=_moment(now),
            recovered=existing.continuation_recovered,
        )


def release_request(
    plan_dir: str | Path,
    operation_id: str,
    *,
    continuation: RedirectTarget,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> RedirectRequest:
    """Withdraw a carry whose attempt gave up before launching a worker.

    The carry's undo, and the reason a refusal between prompt assembly and
    launch does not cost the operator their words. The request goes back to
    accepted-and-uncarried, so the next attempt coalesces it again exactly as if
    the abandoned one had never touched it, and a crash in the same window
    leaves it recoverable instead of claimed by a turn that never ran.

    Refused once the carry was delivered or receipted: at that point a worker is
    acting on the words, and handing them to a second attempt would be a
    duplicate rather than a rescue. Repeating the release for the same
    continuation returns the existing record and appends nothing.

    Allowed, and important, after :func:`launch_request`: the attempt that
    declared the launch window is the only thing that can close it, and this is
    how it says the prompt never went out after all. The declaration is cleared
    along with the carry, so what a later reader sees is a plain undelivered
    correction rather than an unanswerable one.
    """
    validate_target(continuation)
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        recorded = replay.requests.get(operation_id)
        if (
            recorded is not None
            and not recorded.carried
            and recorded.released_from is not None
            and recorded.released_from.identifies_same_turn(continuation)
        ):
            # Already released from this very attempt. Checked before the carry
            # requirement, because releasing clears the carry that requirement
            # looks for, and a retried release must not read as a mismatch.
            return recorded
        existing = _require_carried_by(
            replay,
            operation_id,
            journal_path=journal_path,
            continuation=continuation,
        )
        if existing.delivered:
            raise RedirectStoreError(
                ERROR_ALREADY_DELIVERED,
                f"redirect request {operation_id!r} was already delivered to a "
                f"launched worker",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "continuation": existing.to_payload()["continuation"],
                    "delivered_at": existing.delivered_at,
                },
            )
        if existing.acknowledged:
            raise RedirectStoreError(
                ERROR_ALREADY_ACKNOWLEDGED,
                f"redirect request {operation_id!r} was already acknowledged by "
                f"the worker that carried it",
                path=journal_path,
                details={
                    "operation_id": operation_id,
                    "continuation": existing.to_payload()["continuation"],
                    "acknowledged_at": existing.acknowledged_at,
                },
            )
        return _annotate_locked(
            fd,
            replay,
            journal_path=journal_path,
            existing=existing,
            kind=KIND_RELEASED,
            continuation=existing.continuation or continuation,
            moment=_moment(now),
            recovered=existing.continuation_recovered,
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
