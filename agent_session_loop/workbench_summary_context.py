"""Gathered inputs for one Workbench summary and the record builder."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import (
    assurance_report,
    plans_errors,
    plans_snapshot,
    review_continuation,
    state,
    state_abandonment,
    state_locks,
    state_payloads,
    workbench_recovery_build,
    workbench_recovery_state,
    workbench_workspace,
)
from .workbench_registry_models import DiscoveredPlan
from .workbench_summary_helpers import (
    _build_invalid_summary,
    _final_review_path_if_exists,
    _searchable_text,
)
from .workbench_summary_model import (
    ACTIVITY_LABEL_BY_STATE,
    URGENCY_GROUP_BY_STATE,
    URGENCY_RANK_BY_STATE,
    LastActivitySource,
    PrimaryState,
    RecommendedAction,
    WorkbenchSummary,
)


@dataclass
class SummaryContext:
    """Everything the state classifiers read, gathered once per plan."""

    plan: DiscoveredPlan
    snapshot: Any
    state_dir: str | Path | None
    reclaim: bool
    runner_state: dict[str, Any]
    lock_info: Any
    state_path: Path
    events_path: Path
    progress_path: Path
    review_resume: dict[str, Any]
    workspace: dict[str, Any]
    summary_cwd: Any
    expired_wait_context: Any
    stale_unfinished: bool
    stale_reason: str | None
    recovery: dict[str, Any]
    counts: dict[str, int]
    next_pending: Any
    final_review_path: Path | None
    active_turn: Any
    active_verification: Any
    base_run_id: str | None
    base_harness: str | None
    artifact_dir: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    last_message_path: Path | None
    result_path: Path | None

    def make(
        self,
        *,
        primary_state: PrimaryState,
        recommended_action: RecommendedAction,
        next_action_hint: str,
        detail: str | None,
        warnings: tuple[str, ...] = (),
        run_id: str | None = None,
        harness: str | None = None,
        artifact_dir: Path | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        last_message_path: Path | None = None,
        result_path: Path | None = None,
        pause_reason: str | None = None,
        pause_message: str | None = None,
        last_message_preview: str | None = None,
        last_activity_at: str | None = None,
        last_activity_source: LastActivitySource | None = None,
        completed_at: str | None = None,
        concise_reason: str | None = None,
        activity_label: str | None = None,
    ) -> WorkbenchSummary:
        """Build the summary record; unset run and artifact fields default to
        the values gathered from runner state."""
        plan = self.plan
        snapshot = self.snapshot
        workspace = self.workspace
        next_pending = self.next_pending

        run_id = run_id or self.base_run_id
        harness = harness or self.base_harness
        artifact_dir = artifact_dir or self.artifact_dir
        stdout_path = stdout_path or self.stdout_path
        stderr_path = stderr_path or self.stderr_path
        last_message_path = last_message_path or self.last_message_path
        result_path = result_path or self.result_path
        urgency_group = URGENCY_GROUP_BY_STATE[primary_state]
        resolved_activity_label = (
            activity_label or ACTIVITY_LABEL_BY_STATE[primary_state]
        )
        resolved_reason = concise_reason or detail or next_action_hint
        next_pending_session_id = next_pending.id if next_pending is not None else None
        searchable_text = _searchable_text(
            plan_dir=snapshot.plan_dir,
            label=plan.label,
            primary_state=primary_state,
            recommended_action=recommended_action,
            urgency_group=urgency_group,
            activity_label=resolved_activity_label,
            concise_reason=resolved_reason,
            detail=detail,
            next_action_hint=next_action_hint,
            next_pending_session_id=next_pending_session_id,
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=last_message_preview,
            run_id=run_id,
            harness=harness,
            sources=plan.sources,
        )
        return WorkbenchSummary(
            plan_dir=snapshot.plan_dir,
            label=plan.label,
            profile=plan.profile,
            sources=plan.sources,
            exists=True,
            primary_state=primary_state,
            recommended_action=recommended_action,
            next_action_hint=next_action_hint,
            detail=detail,
            next_pending_session_id=next_pending_session_id,
            next_pending_session_path=(
                next_pending.path if next_pending is not None else None
            ),
            counts=self.counts,
            state_path=self.state_path,
            events_path=self.events_path,
            progress_path=self.progress_path,
            final_review_path=self.final_review_path,
            artifact_dir=artifact_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            last_message_path=last_message_path,
            result_path=result_path,
            run_id=run_id,
            harness=harness,
            cwd=str(self.summary_cwd) if self.summary_cwd is not None else None,
            workspace_root=workspace.get("workspace_root"),
            registry_digest=workspace.get("registry_digest"),
            repositories=workbench_workspace._repository_rows(
                workspace.get("repositories")
            ),
            repository_count=int(workspace.get("repository_count") or 0),
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=last_message_preview,
            urgency_rank=URGENCY_RANK_BY_STATE[primary_state],
            urgency_group=urgency_group,
            last_activity_at=last_activity_at,
            last_activity_source=last_activity_source,
            completed_at=completed_at,
            activity_label=resolved_activity_label,
            concise_reason=resolved_reason,
            searchable_text=searchable_text,
            recovery=self.recovery,
            review_resume=self.review_resume,
            assurance=assurance_report.bounded_summary(
                self.runner_state.get("assurance")
            ),
            warnings=warnings,
        )


def gather_context(
    plan: DiscoveredPlan,
    *,
    state_dir: str | Path | None,
    reclaim: bool,
) -> SummaryContext | WorkbenchSummary:
    """Read plan and runner state; return an invalid summary if unreadable."""
    plan_dir = plan.plan_dir
    if not plan.exists:
        return _build_invalid_summary(
            plan,
            detail=f"plan directory does not exist: {plan_dir}",
            exists=False,
            state_dir=state_dir,
            reclaim=reclaim,
        )

    try:
        snapshot = plans_snapshot.inspect_plan(plan_dir)
    except plans_errors.PlanError as error:
        state_paths = workbench_recovery_state._summary_base_paths(plan_dir, state_dir)
        return _build_invalid_summary(
            plan,
            detail=str(error),
            exists=True,
            state_paths=state_paths,
            state_dir=state_dir,
            reclaim=reclaim,
        )

    state_path, events_path, progress_path = (
        workbench_recovery_state._summary_base_paths(snapshot.plan_dir, state_dir)
    )
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, events_path
    ).to_payload()
    lock_path = state.lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = state_locks._read_lock_path(
        lock_path, snapshot.plan_dir, reclaim=reclaim
    )
    runner_state = state_payloads._runner_state_payload(
        state_path,
        lock_info=lock_info,
        include_turn_activity=True,
    )
    try:
        workspace = workbench_workspace._workspace_projection(
            snapshot.plan_dir, runtime=runner_state
        )
    except (plans_errors.PlanError, OSError, ValueError) as error:
        return _build_invalid_summary(
            replace(plan, plan_dir=snapshot.plan_dir, exists=True),
            detail=f"workspace metadata unreadable: {error}",
            exists=True,
            state_paths=(state_path, events_path, progress_path),
            state_dir=state_dir,
            reclaim=reclaim,
        )
    summary_cwd = (
        runner_state.get("cwd")
        or workspace.get("workspace_root")
        or (plan.profile.cwd if plan.profile is not None else None)
    )
    expired_wait_context = workbench_recovery_state._expired_wait_qna_context(
        snapshot,
        runner_state,
        state_dir,
        reclaim=reclaim,
    )
    stale_unfinished, stale_reason = state_abandonment._stale_runner_state(
        runner_state, lock_info
    )
    recovery = workbench_recovery_build.build_workbench_recovery(
        replace(plan, plan_dir=snapshot.plan_dir, exists=True),
        state_dir=state_dir,
        reclaim=reclaim,
    ).compact_payload()

    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1

    active_turn = runner_state.get("active_turn")
    artifact_dir = None
    stdout_path = None
    stderr_path = None
    last_message_path = None
    result_path = None
    if isinstance(active_turn, dict):
        coerce = workbench_recovery_state._coerce_path_value
        artifact_dir = coerce(active_turn.get("artifact_dir"))
        stdout_path = coerce(active_turn.get("stdout_path"))
        stderr_path = coerce(active_turn.get("stderr_path"))
        last_message_path = coerce(active_turn.get("last_message_path"))
        result_path = coerce(active_turn.get("result_path"))

    return SummaryContext(
        plan=plan,
        snapshot=snapshot,
        state_dir=state_dir,
        reclaim=reclaim,
        runner_state=runner_state,
        lock_info=lock_info,
        state_path=state_path,
        events_path=events_path,
        progress_path=progress_path,
        review_resume=review_resume,
        workspace=workspace,
        summary_cwd=summary_cwd,
        expired_wait_context=expired_wait_context,
        stale_unfinished=stale_unfinished,
        stale_reason=stale_reason,
        recovery=recovery,
        counts=counts,
        next_pending=snapshot.next_pending,
        final_review_path=_final_review_path_if_exists(snapshot.plan_dir),
        active_turn=active_turn,
        active_verification=runner_state.get("active_verification"),
        base_run_id=runner_state.get("run_id"),
        base_harness=runner_state.get("harness"),
        artifact_dir=artifact_dir,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        last_message_path=last_message_path,
        result_path=result_path,
    )
