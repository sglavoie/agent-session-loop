"""Steps the review and repair branches of the completion pass share.

Both a freshly appended review session and freshly materialized repair
children grow a plan that had no pending work, and both end by re-writing the
plan's ``running`` readiness state; both also honor a stop that was queued
while the last session ran. Each lives here once instead of once per branch.
"""

from __future__ import annotations

from . import (
    clock,
    handoff_guards,
    plans_models,
    readiness_gate,
    review_cycle,
    run_control,
    run_termination,
)
from .run_completion_types import CompletionOutcome, RunContext


def mark_plan_running(ctx: RunContext, snapshot: plans_models.PlanSnapshot) -> None:
    """Write ``running`` readiness state and mirror the result into the payload."""
    running_success, running_error = readiness_gate.write_readiness_state(
        snapshot.plan_dir, "running"
    )
    with ctx.persistence.lock:
        if running_success:
            ctx.payload["readiness_state_active"] = True
            ctx.payload["readiness_state_current"] = "running"
            ctx.payload["readiness_state_refreshed_at"] = clock.isoformat(
                clock.utc_now()
            )
            ctx.payload["readiness_state_refresh_error"] = None
        else:
            ctx.payload["readiness_state_refresh_error"] = running_error
        ctx.persistence.write_state(ctx.payload)


def stop_before_review(
    ctx: RunContext,
    snapshot: plans_models.PlanSnapshot,
    run_controls: run_control.RunControlListener,
) -> CompletionOutcome:
    """Honor a stop request seen before any review work begins."""
    run_termination._stop_before_review_work(
        run_controls,
        snapshot=snapshot,
        payload=ctx.payload,
        event_path=ctx.event_path,
        control_payload=ctx.control_payload,
        record_existing_review_verdict=(
            review_cycle.existing_review_verdict_recorder(
                handoff_guards._read_review_verdict
            )
        ),
        persistence=ctx.persistence,
    )
    return CompletionOutcome.stop(snapshot)
