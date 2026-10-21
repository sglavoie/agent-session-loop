"""Summary classifier for plans with no live, paused, stale, or failed run."""

from __future__ import annotations

from . import review_continuation
from .workbench_summary_context import SummaryContext
from .workbench_summary_helpers import _first_activity, _last_progress_entry_at
from .workbench_summary_model import (
    REVIEW_CONTINUATION_ACTIVITY_LABEL,
    WorkbenchSummary,
)


def settled_summary(ctx: SummaryContext) -> WorkbenchSummary:
    """Classify as complete, recent, or ready; the final fallthrough."""
    runner_state = ctx.runner_state
    review_resume = ctx.review_resume
    recovery = ctx.recovery
    next_pending = ctx.next_pending

    if next_pending is None:
        complete_activity, complete_source = _first_activity(
            (
                (_last_progress_entry_at(ctx.snapshot.plan_dir), "progress-journal"),
                (runner_state.get("run_finished_at"), "run-finished"),
            )
        )
        review_action = review_resume.get("action")
        if review_resume.get("eligible") and review_action in {
            "materialize-repairs",
            "rereview",
        }:
            if review_action == "materialize-repairs":
                hint = (
                    "Resume review repairs from session "
                    f"{review_resume.get('review_session_id')}"
                )
                detail = "Review repair materialization is ready"
            else:
                hint = "Rerun review after completed repair sessions"
                detail = "Completed review repairs are awaiting rereview"
            return ctx.make(
                primary_state="recent",
                recommended_action="run",
                next_action_hint=hint,
                detail=detail,
                last_activity_at=complete_activity,
                last_activity_source=complete_source,
                concise_reason=detail,
                activity_label=REVIEW_CONTINUATION_ACTIVITY_LABEL,
            )
        if review_resume.get("blocker") == "higher-cap-required":
            minimum = review_resume.get("minimum_required_cap")
            selected_cap = review_resume.get("selected_cap")
            if selected_cap is None:
                hint = f"Supply a review-fix cap of {minimum} or higher"
                detail = "Review repairs require a review-fix cap to resume"
            else:
                hint = f"Increase to a review-fix cap of {minimum} or higher"
                detail = (
                    "Review repair budget is exhausted at the selected cap "
                    f"of {selected_cap}"
                )
        elif review_resume.get("blocker") == "artifact-invalid":
            hint = "Correct the structured review artifact before rerunning"
            detail = review_resume.get("artifact_error") or (
                "Review artifact validation failed"
            )
        elif (
            review_action == "terminal"
            and review_resume.get("blocker")
            not in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
        ):
            hint = recovery.get("title") or ("Inspect inconsistent review continuation")
            detail = recovery.get("summary") or (
                "Recorded review repair history cannot be resumed safely: "
                f"{review_resume.get('blocker') or 'unknown blocker'}."
            )
        else:
            hint = "All sessions complete"
            detail = "No pending sessions remain"
        return ctx.make(
            primary_state="complete",
            recommended_action="view",
            next_action_hint=hint,
            detail=detail,
            last_activity_at=complete_activity,
            last_activity_source=complete_source,
            completed_at=complete_activity,
            concise_reason=detail,
        )

    if runner_state.get("present") and runner_state.get("run_finished_at"):
        recent_activity, recent_source = _first_activity(
            ((runner_state.get("run_finished_at"), "run-finished"),)
        )
        review_hint = (
            f"Resume review repairs with session {next_pending.id}"
            if review_resume.get("action") == "run-repairs"
            else f"Resume with session {next_pending.id}"
        )
        return ctx.make(
            primary_state="recent",
            recommended_action="run",
            next_action_hint=review_hint,
            detail=f"Last run finished at {runner_state.get('run_finished_at')}",
            last_activity_at=recent_activity,
            last_activity_source=recent_source,
            concise_reason=(
                f"Last run finished at {runner_state.get('run_finished_at')}"
            ),
        )

    ready_activity, ready_source = _first_activity(
        ((_last_progress_entry_at(ctx.snapshot.plan_dir), "progress-journal"),)
    )
    ready_hint = (
        f"Start review repairs with session {next_pending.id}"
        if review_resume.get("action") == "run-repairs"
        else f"Start session {next_pending.id}"
    )
    return ctx.make(
        primary_state="ready",
        recommended_action="run",
        next_action_hint=ready_hint,
        detail="Ready to run; no prior runner state",
        last_activity_at=ready_activity,
        last_activity_source=ready_source,
        concise_reason=f"Next: session {next_pending.id}",
    )
