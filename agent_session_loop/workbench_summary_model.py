"""Workbench primary states and the per-plan summary record."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    workbench_workspace,
)
from .workbench_registry_models import DiscoverySource, LaunchProfile

PrimaryState = Literal[
    "invalid",
    "verifying",
    "running",
    "paused-for-reply",
    "failed",
    "stale",
    "complete",
    "recent",
    "ready",
]
PRIMARY_STATES: tuple[PrimaryState, ...] = (
    "invalid",
    "verifying",
    "running",
    "paused-for-reply",
    "failed",
    "stale",
    "complete",
    "recent",
    "ready",
)


RecommendedAction = Literal[
    "watch",
    "reply",
    "inspect-failure",
    "unlock",
    "view",
    "run",
    "repair-plan",
]
RECOMMENDED_ACTIONS: tuple[RecommendedAction, ...] = (
    "watch",
    "reply",
    "inspect-failure",
    "unlock",
    "view",
    "run",
    "repair-plan",
)


UrgencyGroup = Literal["needs-action", "in-progress", "ready", "complete"]
URGENCY_GROUPS: tuple[UrgencyGroup, ...] = (
    "needs-action",
    "in-progress",
    "ready",
    "complete",
)


LastActivitySource = Literal[
    "active-verification",
    "active-turn",
    "turn-record",
    "run-finished",
    "run-started",
    "lock-created",
    "progress-journal",
]
LAST_ACTIVITY_SOURCES: tuple[LastActivitySource, ...] = (
    "active-verification",
    "active-turn",
    "turn-record",
    "run-finished",
    "run-started",
    "lock-created",
    "progress-journal",
)


# Lower rank == surface higher in the triage queue.
URGENCY_RANK_BY_STATE: dict[PrimaryState, int] = {
    "paused-for-reply": 10,
    "failed": 20,
    "stale": 30,
    "invalid": 40,
    "verifying": 50,
    "running": 60,
    "recent": 70,
    "ready": 80,
    "complete": 90,
}


URGENCY_GROUP_BY_STATE: dict[PrimaryState, UrgencyGroup] = {
    "paused-for-reply": "needs-action",
    "failed": "needs-action",
    "stale": "needs-action",
    "invalid": "needs-action",
    "verifying": "in-progress",
    "running": "in-progress",
    "recent": "ready",
    "ready": "ready",
    "complete": "complete",
}


ACTIVITY_LABEL_BY_STATE: dict[PrimaryState, str] = {
    "invalid": "Plan unreadable",
    "verifying": "Verifying",
    "running": "Active turn",
    "paused-for-reply": "Reply needed",
    "failed": "Last run failed",
    "stale": "Stale state",
    "complete": "All sessions complete",
    "recent": "Run finished, sessions remain",
    "ready": "Ready to run",
}


# ``recent`` also represents an eligible review continuation after all of its
# repair sessions have completed.  Its normal label would incorrectly imply
# that the queue still contains pending sessions in that zero-pending case.
REVIEW_CONTINUATION_ACTIVITY_LABEL = "Review continuation ready"


@dataclass(frozen=True)
class WorkbenchSummary:
    """Aggregate per-plan view that drives the Workbench action queue.

    Combines durable plan state (sessions, progress journal), the latest
    runner state (paused, finished, active turn/verification), the active
    lock, and registry-derived label/profile metadata. Exposes a single
    ``primary_state`` plus a single ``recommended_action`` so the queue UI
    can render one row per plan without re-deriving classification logic.
    """

    plan_dir: Path
    label: str | None
    profile: LaunchProfile | None
    sources: tuple[DiscoverySource, ...]
    exists: bool
    primary_state: PrimaryState
    recommended_action: RecommendedAction
    next_action_hint: str
    detail: str | None
    next_pending_session_id: str | None
    next_pending_session_path: Path | None
    counts: dict[str, int]
    state_path: Path | None
    events_path: Path | None
    progress_path: Path | None
    final_review_path: Path | None
    artifact_dir: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    last_message_path: Path | None
    result_path: Path | None
    run_id: str | None
    harness: str | None
    cwd: str | None
    workspace_root: str | None
    registry_digest: str | None
    repositories: tuple[dict[str, Any], ...]
    repository_count: int
    pause_reason: str | None
    pause_message: str | None
    last_message_preview: str | None
    urgency_rank: int = 0
    urgency_group: UrgencyGroup = "needs-action"
    last_activity_at: str | None = None
    last_activity_source: LastActivitySource | None = None
    completed_at: str | None = None
    activity_label: str = ""
    concise_reason: str = ""
    searchable_text: str = ""
    recovery: dict[str, Any] = field(default_factory=dict)
    review_resume: dict[str, Any] = field(default_factory=dict)
    #: Bounded, display-only projection of the last run's supervisor audit. The
    #: Workbench reports what the audit found and where the inbox is; it has no
    #: field with which to configure, launch, retry, or dispose of an audit.
    assurance: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "label": self.label,
            "profile": (
                self.profile.to_payload() if self.profile is not None else None
            ),
            "sources": list(self.sources),
            "exists": self.exists,
            "primary_state": self.primary_state,
            "recommended_action": self.recommended_action,
            "urgency_rank": self.urgency_rank,
            "urgency_group": self.urgency_group,
            "next_action_hint": self.next_action_hint,
            "activity_label": self.activity_label,
            "concise_reason": self.concise_reason,
            "last_activity_at": self.last_activity_at,
            "last_activity_source": self.last_activity_source,
            "completed_at": self.completed_at,
            "searchable_text": self.searchable_text,
            "recovery": dict(self.recovery),
            "review_resume": dict(self.review_resume),
            "assurance": dict(self.assurance),
            "detail": self.detail,
            "next_pending_session_id": self.next_pending_session_id,
            "next_pending_session_path": (
                str(self.next_pending_session_path)
                if self.next_pending_session_path is not None
                else None
            ),
            "counts": dict(self.counts),
            "state_path": str(self.state_path) if self.state_path else None,
            "events_path": str(self.events_path) if self.events_path else None,
            "progress_path": (str(self.progress_path) if self.progress_path else None),
            "final_review_path": (
                str(self.final_review_path) if self.final_review_path else None
            ),
            "artifact_dir": (str(self.artifact_dir) if self.artifact_dir else None),
            "stdout_path": str(self.stdout_path) if self.stdout_path else None,
            "stderr_path": str(self.stderr_path) if self.stderr_path else None,
            "last_message_path": (
                str(self.last_message_path) if self.last_message_path else None
            ),
            "result_path": str(self.result_path) if self.result_path else None,
            "run_id": self.run_id,
            "harness": self.harness,
            **workbench_workspace._workspace_payload(
                {
                    "workspace_root": self.workspace_root,
                    "registry_digest": self.registry_digest,
                    "repositories": self.repositories,
                    "repository_count": self.repository_count,
                },
                cwd=self.cwd,
            ),
            "pause_reason": self.pause_reason,
            "pause_message": self.pause_message,
            "last_message_preview": self.last_message_preview,
            "warnings": list(self.warnings),
        }
