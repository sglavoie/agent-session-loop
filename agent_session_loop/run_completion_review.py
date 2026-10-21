"""The generated review session the completion pass appends.

When a review is owed and no recorded repair history redirects it, the pass
captures the review's base (single repository or combined workspace), creates
or reuses the generated review session, records its two events, and re-gates
the grown plan before the loop runs it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import (
    events,
    handoff_guards,
    plans_errors,
    plans_models,
    readiness_gate_reopen,
    repository_evidence,
    review_delta,
    review_session_body,
    review_session_contract,
    workspace_evidence,
)
from .run_completion_support import mark_plan_running
from .run_completion_types import CompletionOutcome, RunContext
from .run_errors import RunError


def append_review_session(
    ctx: RunContext,
    snapshot: plans_models.PlanSnapshot,
    *,
    args: argparse.Namespace,
    cwd: Path,
    review_guard_excludes: tuple[str, ...],
    review_guard_include_ignored: bool,
) -> CompletionOutcome:
    """Append (or reuse) the generated review session and re-gate the plan."""
    review_workspace = ctx.verification_context.workspace
    review_bases: list[dict[str, Any]] = []
    if review_workspace is None:
        review_scope_delta, review_scope_fallback = (
            review_delta._compute_review_delta_context(
                event_path=ctx.event_path,
                plan_dir=snapshot.plan_dir,
                cwd=cwd,
            )
        )
    else:
        try:
            review_base_snapshot = (
                handoff_guards._capture_workspace_write_guard_snapshot(
                    workspace=review_workspace,
                    plan_dir=snapshot.plan_dir,
                    exclude=review_guard_excludes,
                    include_ignored=(review_guard_include_ignored),
                )
            )
        except repository_evidence.RepositoryEvidenceError as error:
            unavailable = {
                ".": {
                    "reason": "workspace-inventory-failed",
                    "error": str(error),
                }
            }
        else:
            unavailable = workspace_evidence._workspace_snapshot_unavailable(
                review_base_snapshot,
                required_repository_paths=tuple(
                    repository.repository.path
                    for repository in review_workspace.repositories
                ),
            )
        if unavailable:
            unavailable_summary = workspace_evidence._workspace_unavailable_summary(
                unavailable,
                workspace=review_workspace,
            )
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                message="combined workspace review base could not "
                "be captured safely"
                f"{unavailable_summary}",
                details={
                    "workspace_root": str(review_workspace.root),
                    "unavailable": unavailable,
                },
            )
            return CompletionOutcome.stop(snapshot)
        try:
            review_bases = workspace_evidence._workspace_review_bases(
                review_workspace, review_base_snapshot
            )
        except RunError as error:
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                message=str(error),
                details={"error": str(error)},
            )
            return CompletionOutcome.stop(snapshot)
        review_scope_delta, review_scope_fallback = (
            review_delta._compute_registered_review_delta_context(
                event_path=ctx.event_path,
                plan_dir=snapshot.plan_dir,
                workspace=review_workspace,
            )
        )
    review_scope = (
        review_scope_delta["mode"]
        if review_scope_delta is not None
        else review_session_body.REVIEW_SCOPE_FULL
    )
    review_base_sha = (
        review_scope_delta.get("base_sha") if review_scope_delta is not None else None
    )
    try:
        review_file, review_created = (
            review_session_body.ensure_generated_review_session(
                snapshot.plan_dir,
                review_harness=args.review_harness,
                delta=review_scope_delta,
                workspace=review_workspace,
                review_bases=review_bases,
            )
        )
    except (plans_errors.PlanError, OSError) as error:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="malformed-plan",
            message=f"could not create or reuse a generated review session: {error}",
            details={"error": str(error)},
        )
        return CompletionOutcome.stop(snapshot)
    ctx.review_appended = True
    with ctx.persistence.lock:
        ctx.payload["review_session_id"] = review_file.session_id.value
    if review_created:
        ctx.persistence.append_event(
            ctx.event_path,
            events.REVIEW_SCOPE_EVENT,
            {
                "run_id": ctx.run_id,
                "session_id": review_file.session_id.value,
                "scope": review_scope,
                "base_sha": review_base_sha,
                **(
                    {"repositories": review_scope_delta.get("repositories")}
                    if review_scope_delta is not None
                    and "repositories" in review_scope_delta
                    else {}
                ),
                "fallback_reason": review_scope_fallback,
                "refreshed": False,
            },
        )
        ctx.persistence.append_event(
            ctx.event_path,
            events.REVIEW_SESSION_CREATED_EVENT,
            {
                "run_id": ctx.run_id,
                "session_id": review_file.session_id.value,
                "session": review_file.session_id.value,
                "review_harness": args.review_harness,
                "review_session_path": str(review_file.path),
                "review_output_path": (
                    ctx.payload.get("review_output_path")
                    or str(
                        review_session_contract.final_review_output_path(
                            snapshot.plan_dir
                        )
                    )
                ),
                "review_scope": review_scope,
                "review_base_sha": review_base_sha,
                **(
                    {"repositories": review_bases}
                    if review_workspace is not None
                    else {}
                ),
            },
        )
    try:
        snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
    except plans_errors.PlanError as error:
        ctx.pause(
            reason="malformed-plan",
            message="plan could not be inspected after appending the "
            f"review session: {error}",
            details={"error": str(error)},
        )
        return CompletionOutcome.stop(snapshot)
    ctx.record_final_snapshot(snapshot)
    # Appending the review session grows the plan exactly the way
    # materializing repair children does, and reaches here under
    # the same reused readiness report -- the one that said every
    # session was complete and no turns were required. Re-gate the
    # grown plan before its turn launches, so the authority the
    # review runs under describes the plan that now exists rather
    # than the one the startup gate saw. A reused pending review
    # session was already authorized when it was created, so only
    # a fresh append is re-gated.
    if review_created and not readiness_gate_reopen._regate_reopened_plan(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        args=args,
        snapshot=snapshot,
        repair_session_ids=[],
        details={
            "review_session_id": review_file.session_id.value,
            "review_session_path": str(review_file.path),
        },
        trigger=events.REVIEW_SESSION_CREATED_EVENT,
        growth=(
            f"a generated review session ({review_file.session_id.value}) was appended"
        ),
    ):
        return CompletionOutcome.stop(snapshot)
    mark_plan_running(ctx, snapshot)
    return CompletionOutcome.again(snapshot)
