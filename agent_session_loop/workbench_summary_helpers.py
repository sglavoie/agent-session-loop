"""Small helpers shared by the Workbench summary builders."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import (
    plans_errors,
    plans_progress,
    workbench_recovery_build,
    workbench_workspace,
)
from .workbench_discovery import _parse_archive_timestamp
from .workbench_registry_models import DiscoveredPlan, DiscoverySource
from .workbench_summary_model import (
    ACTIVITY_LABEL_BY_STATE,
    URGENCY_GROUP_BY_STATE,
    URGENCY_RANK_BY_STATE,
    LastActivitySource,
    PrimaryState,
    RecommendedAction,
    UrgencyGroup,
    WorkbenchSummary,
)

FINAL_REVIEW_FILE_NAME = "final-review.md"
FINAL_REVIEW_ARTIFACT_FILE_NAME = "final-review.json"
LAST_MESSAGE_PREVIEW_CHARS = 280


def _truncate_preview(text: str, *, limit: int = LAST_MESSAGE_PREVIEW_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"


def _verification_position(active_verification: Mapping[str, Any]) -> str:
    check_count = active_verification.get("check_count")
    check_index = active_verification.get("current_check_index")
    if check_index is None:
        return f"-/{check_count}" if check_count is not None else "-"
    try:
        position = int(check_index) + 1
    except TypeError, ValueError:
        return "-"
    if check_count is not None:
        return f"{position}/{check_count}"
    return str(position)


def _final_review_path_if_exists(plan_dir: Path) -> Path | None:
    candidate = plan_dir / FINAL_REVIEW_FILE_NAME
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _first_activity(
    candidates: Sequence[tuple[Any, LastActivitySource]],
) -> tuple[str | None, LastActivitySource | None]:
    for value, source in candidates:
        if isinstance(value, str) and value:
            return value, source
    return None, None


def _paused_activity(
    runner_state: Mapping[str, Any],
) -> tuple[str | None, LastActivitySource | None]:
    """Latest valid event in the displayed run; its start is only a fallback."""
    candidates: list[tuple[Any, LastActivitySource]] = [
        (runner_state.get("run_finished_at"), "run-finished"),
    ]
    turns = runner_state.get("turns")
    records = list(turns) if isinstance(turns, list) else []
    if isinstance(runner_state.get("active_turn"), Mapping):
        records.append(runner_state["active_turn"])
    for turn in records:
        if isinstance(turn, Mapping):
            candidates.extend(
                (turn.get(key), "turn-record")
                for key in ("finished_at", "last_activity_at", "started_at")
            )
    valid = [
        (parsed, value, source)
        for value, source in candidates
        if isinstance(value, str)
        and (parsed := _parse_archive_timestamp(value)) is not None
    ]
    if valid:
        _, value, source = max(valid, key=lambda item: item[0])
        return value, source
    started = runner_state.get("run_started_at")
    if isinstance(started, str) and _parse_archive_timestamp(started) is not None:
        return started, "run-started"
    return None, None


def _last_progress_entry_at(plan_dir: Path) -> str | None:
    try:
        entries = plans_progress.read_progress_entries(plan_dir)
    except plans_errors.PlanError:
        return None
    if not entries:
        return None
    return entries[-1].completed_at


def _searchable_text(
    *,
    plan_dir: Path,
    label: str | None,
    primary_state: PrimaryState,
    recommended_action: RecommendedAction,
    urgency_group: UrgencyGroup,
    activity_label: str,
    concise_reason: str,
    detail: str | None,
    next_action_hint: str,
    next_pending_session_id: str | None,
    pause_reason: str | None,
    pause_message: str | None,
    last_message_preview: str | None,
    run_id: str | None,
    harness: str | None,
    sources: tuple[DiscoverySource, ...],
) -> str:
    parts: list[str] = [
        str(plan_dir),
        plan_dir.name,
        primary_state,
        recommended_action,
        urgency_group,
        activity_label,
        concise_reason,
        next_action_hint,
    ]
    if label:
        parts.append(label)
    if detail:
        parts.append(detail)
    if next_pending_session_id:
        parts.extend(["session", next_pending_session_id])
    if pause_reason:
        parts.append(pause_reason)
    if pause_message:
        parts.append(pause_message)
    if last_message_preview:
        parts.append(last_message_preview)
    if run_id:
        parts.append(run_id)
    if harness:
        parts.append(harness)
    parts.extend(sources)
    # Only text is searchable: a field read from a corrupt state file may be a
    # number or a mapping, and one such plan must not fail the whole listing.
    joined = " ".join(part for part in parts if part and isinstance(part, str))
    return " ".join(joined.lower().split())


def _build_invalid_summary(
    plan: DiscoveredPlan,
    *,
    detail: str,
    exists: bool,
    state_paths: tuple[Path, Path, Path] | None = None,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> WorkbenchSummary:
    try:
        workspace = (
            workbench_workspace._workspace_projection(plan.plan_dir) if exists else {}
        )
    except plans_errors.PlanError, OSError, ValueError:
        workspace = {}
    state_path: Path | None
    events_path: Path | None
    progress_path: Path | None
    if state_paths is not None:
        state_path, events_path, progress_path = state_paths
    else:
        state_path = events_path = progress_path = None
    primary_state: PrimaryState = "invalid"
    recommended_action: RecommendedAction = "repair-plan"
    next_action_hint = "Plan cannot be inspected; repair it before continuing."
    activity_label = ACTIVITY_LABEL_BY_STATE[primary_state]
    concise_reason = detail
    searchable_text = _searchable_text(
        plan_dir=plan.plan_dir,
        label=plan.label,
        primary_state=primary_state,
        recommended_action=recommended_action,
        urgency_group=URGENCY_GROUP_BY_STATE[primary_state],
        activity_label=activity_label,
        concise_reason=concise_reason,
        detail=detail,
        next_action_hint=next_action_hint,
        next_pending_session_id=None,
        pause_reason=None,
        pause_message=None,
        last_message_preview=None,
        run_id=None,
        harness=None,
        sources=plan.sources,
    )
    recovery = workbench_recovery_build.build_workbench_recovery(
        replace(plan, exists=exists), state_dir=state_dir, reclaim=reclaim
    ).compact_payload()
    return WorkbenchSummary(
        plan_dir=plan.plan_dir,
        label=plan.label,
        profile=plan.profile,
        sources=plan.sources,
        exists=exists,
        primary_state=primary_state,
        recommended_action=recommended_action,
        next_action_hint=next_action_hint,
        detail=detail,
        next_pending_session_id=None,
        next_pending_session_path=None,
        counts={"pending": 0, "complete": 0, "empty": 0, "unknown": 0},
        state_path=state_path,
        events_path=events_path,
        progress_path=progress_path,
        final_review_path=_final_review_path_if_exists(plan.plan_dir)
        if exists
        else None,
        artifact_dir=None,
        stdout_path=None,
        stderr_path=None,
        last_message_path=None,
        result_path=None,
        run_id=None,
        harness=None,
        cwd=(
            str(workspace.get("workspace_root"))
            if workspace.get("workspace_root") is not None
            else (plan.profile.cwd if plan.profile is not None else None)
        ),
        workspace_root=workspace.get("workspace_root"),
        registry_digest=workspace.get("registry_digest"),
        repositories=workbench_workspace._repository_rows(
            workspace.get("repositories")
        ),
        repository_count=int(workspace.get("repository_count") or 0),
        pause_reason=None,
        pause_message=None,
        last_message_preview=None,
        urgency_rank=URGENCY_RANK_BY_STATE[primary_state],
        urgency_group=URGENCY_GROUP_BY_STATE[primary_state],
        last_activity_at=None,
        last_activity_source=None,
        activity_label=activity_label,
        concise_reason=concise_reason,
        searchable_text=searchable_text,
        recovery=recovery,
        warnings=(),
    )
