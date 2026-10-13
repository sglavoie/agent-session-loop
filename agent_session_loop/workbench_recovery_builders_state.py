"""Recovery builders for reply, stale state or lock, failed and clean plans."""

from __future__ import annotations

from .workbench_recovery_actions import (
    _endpoint_action,
    _inspect_action,
    _profile_action,
    _retry_run_action,
)
from .workbench_recovery_evidence_commit import (
    _commit_group_evidence,
    _workspace_guard_recovery_evidence,
)
from .workbench_recovery_models import (
    RecoveryArtifactReference,
    RecoveryEvidenceRow,
    WorkbenchRecovery,
    _RecoveryBuildContext,
)
from .workbench_recovery_payload import _build_context_recovery_payload
from .workbench_recovery_state import (
    _artifact_references_from_payload,
    _latest_failed_commit,
    _latest_relevant_turn,
)


def _build_reply_needed_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="reply-needed",
        reason=reason,
    )
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_turn,
        source="paused-turn",
        label_prefix="Paused turn",
    )
    return _build_context_recovery_payload(
        context,
        kind="reply-needed",
        title="Reply needed",
        summary=context.pause_message,
        severity="warning",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Pause reason",
                value=reason,
                severity="warning",
            ),
            RecoveryEvidenceRow(
                label="Pause message",
                value=context.pause_message,
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="reply",
                label="Reply",
                description=(
                    "Send an operator reply through the existing reply endpoint."
                ),
                endpoint="/api/reply",
                payload=context.plan_dir_payload,
                target="reply",
            ),
        ),
    )


def _build_stale_state_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="stale-state",
        title="Runner state is stale",
        summary=context.stale_reason or "Runner state is unfinished.",
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Stale state",
                value=context.stale_reason,
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="unlock",
                label="Unlock",
                description=(
                    "Clear stale runner state through the existing unlock endpoint."
                ),
                endpoint="/api/unlock",
                payload=context.plan_dir_payload,
                target="unlock",
            ),
        ),
    )


def _build_stale_lock_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    lock_info = context.lock_info
    if lock_info is None:
        return _build_clean_none_recovery(context)
    return _build_context_recovery_payload(
        context,
        kind="stale-lock",
        title="Runner lock is stale",
        summary=lock_info.reason or "Runner lock is stale.",
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts
        + (
            RecoveryArtifactReference(
                label="Runner lock",
                path=lock_info.path,
                kind="lock",
                source="plan-state",
            ),
        ),
        evidence=(
            RecoveryEvidenceRow(
                label="Lock reason",
                value=lock_info.reason,
                detail=(f"pid {lock_info.pid}" if lock_info.pid is not None else None),
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="unlock",
                label="Unlock",
                description=(
                    "Clear the stale lock through the existing unlock endpoint."
                ),
                endpoint="/api/unlock",
                payload=context.plan_dir_payload,
                target="unlock",
            ),
        ),
    )


def _build_failed_finished_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="generic",
        reason=reason,
    )
    failed_commit = _latest_failed_commit(context.raw_state)
    selected_commit = failed_commit if reason == "commit-failed" else None
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_commit or selected_turn,
        source="failed-commit" if selected_commit is not None else "failed-turn",
        label_prefix=(
            "Failed commit" if selected_commit is not None else "Failed turn"
        ),
    )
    return _build_context_recovery_payload(
        context,
        kind="generic",
        title="Run failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Failure reason",
                value=reason,
                severity="error",
            ),
            RecoveryEvidenceRow(
                label="Failure message",
                value=context.pause_message,
                severity="error",
            ),
        )
        + _workspace_guard_recovery_evidence(context.pause_details)
        + _commit_group_evidence(context.raw_state),
        actions=(
            _inspect_action("Open the latest relevant logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review launch profile fields before retrying."),
        ),
    )


#: What each conversation recovery decision means in the card's own words, and
#: what the operator is actually being asked to weigh. Keyed by the decision


def _build_clean_none_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="none",
        title="No recovery needed",
        summary="The plan has no structured blocker.",
        severity="none",
        recoverable=False,
        artifacts=context.base_artifacts,
        evidence=(),
    )
