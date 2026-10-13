"""Recovery card vocabulary: kinds, severities, action targets, card dataclasses."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    plans_models,
    review_continuation,
    state,
    workbench_workspace,
)

RecoveryKind = Literal[
    "none",
    "review-continuation",
    "reply-needed",
    "not-loop-ready",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    "stale-lock",
    "stale-state",
    "commit-preflight",
    "commit-failed",
    "conversation-recovery",
    "generic",
]
RECOVERY_KINDS: tuple[RecoveryKind, ...] = (
    "none",
    "review-continuation",
    "reply-needed",
    "not-loop-ready",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    "stale-lock",
    "stale-state",
    "commit-preflight",
    "commit-failed",
    "conversation-recovery",
    "generic",
)


PAUSE_REASON_RECOVERY_KIND_BY_REASON: dict[str, RecoveryKind] = {
    "blocked": "reply-needed",
    "commit-failed": "commit-failed",
    "commit-preflight": "commit-preflight",
    # Both pauses hand the operator a decision about a conversation that cannot
    # be resumed, and `intervention.recovery_decision` is what says which one.
    # They share a kind rather than a generic card because the generic card's
    # advice -- inspect, then relaunch -- is wrong here: relaunching without the
    # one-use authorization re-derives the same pause, and relaunching beside an
    # unstopped predecessor is the thing the second pause exists to prevent.
    "conversation-recovery-required": "conversation-recovery",
    "dependent": "reply-needed",
    "malformed-contract": "malformed-contract",
    "no-contract": "no-contract",
    "not-loop-ready": "not-loop-ready",
    "redirect-successor-blocked": "conversation-recovery",
    "redirect-submission-unresolved": "conversation-recovery",
    "redirect-delivery-unrecordable": "conversation-recovery",
    "runner-pause": "reply-needed",
    "unresolved-prerequisite": "reply-needed",
    "verify-failed": "verify-failed",
}


GENERIC_RECOVERY_PAUSE_REASONS: tuple[str, ...] = (
    "nonzero-exit",
    "timeout",
    "no-status-change",
    "multiple-status-changes",
    "malformed-plan",
    "unknown-status",
    "lock-contention",
    "unexpected-status-change",
    "no-pending-but-incomplete",
    "harness-unavailable",
    "unsafe",
    "interrupted",
    "review-harness-required",
    "review-mutated-worktree",
    # The guard's git probes failed, so the review needs a working repository
    # before it can be relaunched — inspect, do not reply.
    "review-guard-unavailable",
    # Registered implementation turns remain unavailable until the workspace
    # write guard is installed — inspect or upgrade, do not request a reply.
    "workspace-write-guard-unavailable",
    # The retained repository changes need a durable scope repair plus fresh
    # expansion/preflight, not an operator reply to the paused harness turn.
    "workspace-write-scope-violation",
    # The plan must declare a repository registry and per-session scopes and be
    # re-prepared. No operator reply resolves it, and marking readiness cannot
    # either, so this is not the `not-loop-ready` recovery kind.
    "repository-registry-required",
    # Another live run claims a repository this plan registers. Only stopping
    # that run or narrowing this registry clears it; no reply does.
    "workspace-repository-contention",
    # Review repair-cycle materialization failed closed (Session 18); the
    # review artifacts or plan structure need inspection, not a reply.
    "malformed-review-repair",
    # Harness-failure classification categories (Session 5). These are
    # inspect/relaunch-from-the-workbench failures rather than reply-needed
    # question pauses; quota/auth/invalid-model are non-retryable and surface
    # generic recovery guidance, transport-failed is retryable upstream.
    "quota-exhausted",
    "auth-failed",
    "invalid-model",
    "transport-failed",
    # The turn rewrote another session's instructions. The plan needs inspecting
    # and the tampered artifact restoring before a relaunch; no reply undoes it.
    "plan-artifact-tampered",
    # The terminal adjudicator refused an unearned success. Relaunching re-derives
    # the same blocker from the same evidence, so the workbench offers the generic
    # inspect-then-relaunch guidance: the operator has to make the checks pass or
    # the review come back all-clear first, and no reply substitutes for either.
    "required-checks-incomplete",
    "review-not-clear",
    # A protected-path guard convicted the turn and the run deliberately kept
    # the retained changes. Only a human diffing the recorded before/after
    # evidence can decide whether to restore the path or keep the change, so
    # this is inspect-then-relaunch; no reply restores a file.
    "guard-protected-path-changed",
    # The turn-start snapshot the protected-path guard compares against was
    # unreadable, so the repair is to the evidence capture — a readable
    # worktree, a working git probe — not to anything an operator can say.
    "guard-protected-path-unavailable",
)


RecoverySeverity = Literal["none", "info", "warning", "error"]
RecoveryEvidenceSeverity = Literal["info", "warning", "error"]
RecoveryWarningSeverity = Literal["info", "warning", "error"]
RecoveryWarningCode = Literal[
    "state-missing-race",
    "state-unreadable",
    "state-malformed-json",
    "state-invalid-json-shape",
]
RecoveryActionKind = Literal["endpoint", "inspect", "navigate"]
RecoveryActionTarget = Literal[
    "reply",
    "events",
    "active",
    "verification",
    "progress",
    "artifacts",
    "stdout",
    "stderr",
    "profile",
    "launch",
    "unlock",
]
RECOVERY_ACTION_TARGETS: tuple[RecoveryActionTarget, ...] = (
    "reply",
    "events",
    "active",
    "verification",
    "progress",
    "artifacts",
    "stdout",
    "stderr",
    "profile",
    "launch",
    "unlock",
)


@dataclass(frozen=True)
class RecoveryWarning:
    """Structured warning metadata paired with legacy warning strings."""

    code: RecoveryWarningCode
    message: str
    severity: RecoveryWarningSeverity = "warning"

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class RecoveryEvidenceRow:
    """Small diagnosis row suitable for a compact recovery card."""

    label: str
    value: str | None
    detail: str | None = None
    severity: RecoveryEvidenceSeverity = "info"

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class RecoveryArtifactReference:
    """Reference to an existing ASL state, log, result, or artifact path."""

    label: str
    path: Path
    kind: str
    source: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": str(self.path),
            "kind": self.kind,
            "source": self.source,
        }


@dataclass(frozen=True)
class RecoveryCopyCommand:
    """Command text for the operator to copy; Workbench never executes it."""

    label: str
    command: str
    description: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "command": self.command,
            "description": self.description,
        }


@dataclass(frozen=True)
class RecoveryActionCard:
    """UI action backed only by existing Workbench affordances."""

    id: str
    label: str
    description: str
    kind: RecoveryActionKind
    target: RecoveryActionTarget | None = None
    endpoint: str | None = None
    method: str | None = None
    payload: Mapping[str, Any] | None = None
    disabled: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "target": self.target,
            "endpoint": self.endpoint,
            "method": self.method,
            "payload": dict(self.payload) if self.payload is not None else None,
            "disabled": self.disabled,
        }


@dataclass(frozen=True)
class WorkbenchRecovery:
    """Read-only recovery diagnosis for the Workbench detail view."""

    plan_dir: Path
    kind: RecoveryKind
    title: str
    summary: str
    severity: RecoverySeverity
    recoverable: bool
    evidence: tuple[RecoveryEvidenceRow, ...] = ()
    actions: tuple[RecoveryActionCard, ...] = ()
    commands: tuple[RecoveryCopyCommand, ...] = ()
    artifacts: tuple[RecoveryArtifactReference, ...] = ()
    run_id: str | None = None
    session_id: str | None = None
    turn_index: int | None = None
    commit_index: int | None = None
    warnings: tuple[str, ...] = ()
    warning_details: tuple[RecoveryWarning, ...] = ()
    review_resume: Mapping[str, Any] = field(default_factory=dict)
    workspace: Mapping[str, Any] = field(default_factory=dict)
    commit_groups: tuple[Mapping[str, Any], ...] = ()

    def compact_payload(self) -> dict[str, Any]:
        primary_action = self.actions[0] if self.actions else None
        return {
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "recoverable": self.recoverable,
            "primary_action": (
                primary_action.to_payload() if primary_action is not None else None
            ),
            "artifact_count": len(self.artifacts),
            "command_count": len(self.commands),
            "warning_count": len(self.warnings),
            "review_resume": dict(self.review_resume),
            "workspace_root": self.workspace.get("workspace_root"),
            "repository_count": int(self.workspace.get("repository_count") or 0),
            "pending_commit_group_count": sum(
                row.get("status") != "complete" for row in self.commit_groups
            ),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "recoverable": self.recoverable,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "commit_index": self.commit_index,
            "evidence": [row.to_payload() for row in self.evidence],
            "actions": [action.to_payload() for action in self.actions],
            "commands": [command.to_payload() for command in self.commands],
            "artifacts": [artifact.to_payload() for artifact in self.artifacts],
            "warnings": list(self.warnings),
            "warning_details": [
                warning.to_payload() for warning in self.warning_details
            ],
            "review_resume": dict(self.review_resume),
            "workspace": dict(self.workspace),
            **workbench_workspace._workspace_payload(self.workspace),
            "commit_groups": [dict(group) for group in self.commit_groups],
            "compact": self.compact_payload(),
        }


@dataclass(frozen=True)
class _RecoveryBuildContext:
    """State gathered once for one recovery payload build."""

    snapshot: plans_models.PlanSnapshot
    state_path: Path
    events_path: Path
    progress_path: Path
    lock_path: Path
    lock_info: state.LockInfo | None
    runner_state: Mapping[str, Any]
    raw_state: Mapping[str, Any]
    pause_reason: str | None
    pause_details: Mapping[str, Any]
    pause_message: str
    base_artifacts: tuple[RecoveryArtifactReference, ...]
    plan_dir_payload: Mapping[str, Any]
    stale_unfinished: bool
    stale_reason: str | None
    warnings: tuple[str, ...]
    warning_details: tuple[RecoveryWarning, ...]
    review_resume: review_continuation.ReviewResume
