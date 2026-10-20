"""Recorded review history that settles or reopens the plan.

When a review was already requested and the recorded continuation says the run
is not at "append a review" but at ``terminal`` or ``materialize-repairs``,
the work lives here: settling a terminal review disposition, or materializing
the repair children a review asked for and re-gating the grown plan.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import (
    events,
    handoff_guards,
    plans_errors,
    plans_models,
    plans_payloads,
    readiness_gate,
    review_continuation,
    review_cycle,
    review_repair,
    run_control,
    run_inputs,
)
from .run_completion_support import mark_plan_running, stop_before_review
from .run_completion_types import CompletionOutcome, RunContext


def settle_terminal_review(
    ctx: RunContext,
    snapshot: plans_models.PlanSnapshot,
    review_resume: review_continuation.ReviewResume,
) -> CompletionOutcome:
    """Record a terminal review disposition, or pause on malformed history."""
    terminal_disposition = (
        review_resume.blocker in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
    )
    with ctx.persistence.lock:
        review_cycle._set_existing_review_verdict(
            ctx.payload,
            snapshot.plan_dir,
            read_review_verdict=handoff_guards._read_review_verdict,
        )
        ctx.payload["review_fix_disposition"] = (
            review_resume.source_disposition if terminal_disposition else None
        )
        ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)
    if terminal_disposition:
        with ctx.persistence.lock:
            ctx.payload["reason"] = review_cycle._terminal_review_reason(ctx.payload)
    else:
        blocker = review_resume.blocker or "unknown blocker"
        ctx.pause(
            reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            message="Recorded review repair history cannot be "
            f"resumed safely: {blocker}.",
            details={"review_resume": review_resume.to_payload()},
        )
    return CompletionOutcome.stop(snapshot)


def _refuse_ineligible_reopen(
    ctx: RunContext,
    snapshot: plans_models.PlanSnapshot,
    review_resume: review_continuation.ReviewResume,
    max_review_fix_cycles: int,
) -> CompletionOutcome:
    with ctx.persistence.lock:
        review_cycle._set_existing_review_verdict(
            ctx.payload,
            snapshot.plan_dir,
            read_review_verdict=handoff_guards._read_review_verdict,
        )
        ctx.payload["review_fix_disposition"] = review_resume.source_disposition
        ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)
    if review_resume.blocker == "higher-cap-required":
        with ctx.persistence.lock:
            ctx.payload["reason"] = review_cycle._terminal_review_reason(ctx.payload)
        return CompletionOutcome.stop(snapshot)

    reopen_error = (
        review_resume.artifact_error or "review repair artifact is not materializable"
    )
    failed_record = review_cycle._review_reopen_record(
        event_path=ctx.event_path,
        payload=ctx.payload,
        resume=review_resume,
        max_review_fix_cycles=max_review_fix_cycles,
        materialized=False,
        result=(review_resume.blocker or "reopen-ineligible"),
        error=reopen_error,
    )
    review_cycle._record_review_reopen(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        record=failed_record,
    )
    ctx.pause(
        reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
        message="review repair reopening failed for "
        f"session {review_resume.review_session_id}: "
        f"{reopen_error}",
        details={
            "review_resume": review_resume.to_payload(),
            "error": reopen_error,
        },
    )
    return CompletionOutcome.stop(snapshot)


def reopen_review_repairs(
    ctx: RunContext,
    snapshot: plans_models.PlanSnapshot,
    review_resume: review_continuation.ReviewResume,
    *,
    args: argparse.Namespace,
    cwd: Path,
    max_review_fix_cycles: int,
    allow_not_loop_ready: bool,
    run_controls: run_control.RunControlListener,
    stop_requested: bool,
) -> CompletionOutcome:
    """Materialize the repair children a review asked for and re-gate the plan."""
    if not review_resume.eligible:
        return _refuse_ineligible_reopen(
            ctx, snapshot, review_resume, max_review_fix_cycles
        )

    if stop_requested:
        return stop_before_review(ctx, snapshot, run_controls)

    repair_result = review_repair.materialize_review_repair_sessions(
        snapshot.plan_dir,
        review_session_id=(review_resume.review_session_id),
        cwd=cwd,
        state_dir=args.state_dir,
        hold_lock=False,
        allow_not_loop_ready=allow_not_loop_ready,
    )
    repair_session_ids = tuple(
        child.session_id.value for child in repair_result.children
    )
    materialized = bool(repair_result.ok and repair_session_ids)
    reopen_record = review_cycle._review_reopen_record(
        event_path=ctx.event_path,
        payload=ctx.payload,
        resume=review_resume,
        max_review_fix_cycles=max_review_fix_cycles,
        materialized=materialized,
        repair_session_ids=repair_session_ids,
        result=repair_result.reason,
        error=repair_result.error,
    )
    review_cycle._record_review_reopen(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        record=reopen_record,
    )

    if (
        not repair_session_ids
        and repair_result.reason == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY
    ):
        # No child survived materialization: the pre-authoring
        # gate refused them, or the loop-ready gate withdrew
        # them (ASI-136). Either way the plan is the one this
        # cycle started from, so there is nothing to proceed
        # into even with --allow-not-loop-ready.
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="not-loop-ready",
            message="no review repair children survived "
            "materialization for session "
            f"{repair_result.review_session_id}: "
            f"{repair_result.error}",
            details={
                "review_resume": review_resume.to_payload(),
                "blockers": list(repair_result.blockers),
                "blocker": list(repair_result.non_bypassable_blockers),
                "repair_children_rolled_back": (repair_result.rolled_back),
                **(
                    {"repair_rollback": repair_result.rollback}
                    if repair_result.rollback is not None
                    else {}
                ),
            },
        )
        return CompletionOutcome.stop(snapshot)

    if not repair_result.ok or not repair_session_ids:
        reopen_error = (
            repair_result.error or "review repair reopening produced no children"
        )
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            message="review repair reopening failed for "
            f"session {review_resume.review_session_id}: "
            f"{reopen_error}",
            details={
                "review_resume": review_resume.to_payload(),
                "error": reopen_error,
                "detail": repair_result.detail,
            },
        )
        return CompletionOutcome.stop(snapshot)

    try:
        snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
    except plans_errors.PlanError as error:
        ctx.pause(
            reason="malformed-plan",
            message="plan could not be inspected after "
            f"reopening review repairs: {error}",
            details={"error": str(error)},
        )
        return CompletionOutcome.stop(snapshot)

    review_resume = review_cycle._set_review_resume(
        snapshot=snapshot,
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        max_repair_group_attempts=(run_inputs._max_repair_group_attempts(args)),
        max_review_fix_cycles=max_review_fix_cycles,
        review_requested=True,
    )
    with ctx.persistence.lock:
        ctx.payload["review_fix_disposition"] = (
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
        )
        ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)

    if (
        repair_result.reason == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY
        and not allow_not_loop_ready
    ):
        ctx.pause(
            reason="not-loop-ready",
            message="repair sessions were reopened from review "
            f"session {repair_result.review_session_id} "
            "but the plan could not be marked loop-ready",
            details={
                "review_resume": review_resume.to_payload(),
                "blockers": list(repair_result.blockers),
                "repair_session_ids": list(repair_session_ids),
            },
        )
        return CompletionOutcome.stop(snapshot)

    if not readiness_gate._regate_reopened_plan(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        args=args,
        snapshot=snapshot,
        repair_session_ids=repair_session_ids,
        details={"review_resume": review_resume.to_payload()},
    ):
        return CompletionOutcome.stop(snapshot)

    mark_plan_running(ctx, snapshot)
    # Execute the exact children just materialized. Once
    # they complete, the next all-complete pass evaluates
    # to ``rereview`` and creates a fresh review.
    return CompletionOutcome.again(snapshot)
