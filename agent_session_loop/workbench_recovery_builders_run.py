"""Recovery builders for active-run, readiness, verification, commit blockers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .workbench_recovery_actions import (
    _inspect_action,
    _preflight_repair_commands,
    _profile_action,
    _retry_run_action,
    _verification_repair_commands,
)
from .workbench_recovery_evidence_checks import (
    _readiness_gate_evidence,
    _selected_verification_context,
    _verification_check_artifacts,
    _verification_recovery_evidence,
    _verification_recovery_title,
)
from .workbench_recovery_evidence_commit import (
    _commit_failed_evidence,
    _commit_group_evidence,
    _harness_config_artifacts,
    _harness_config_evidence,
)
from .workbench_recovery_models import (
    RecoveryCopyCommand,
    RecoveryEvidenceRow,
    WorkbenchRecovery,
    _RecoveryBuildContext,
)
from .workbench_recovery_payload import _build_context_recovery_payload
from .workbench_recovery_state import (
    _artifact_references_from_payload,
    _latest_failed_commit,
    _latest_relevant_turn,
    _payload_mapping,
    _payload_str,
)


def _build_active_run_recovery(
    context: _RecoveryBuildContext,
    *,
    selected_turn: Mapping[str, Any] | None,
) -> WorkbenchRecovery:
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_turn,
        source="active-turn",
        label_prefix="Active turn",
    )
    lock_info = context.lock_info
    return _build_context_recovery_payload(
        context,
        kind="none",
        title="Run in progress",
        summary="No recovery action is required while the runner is active.",
        severity="none",
        recoverable=False,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Runner lock",
                value="active",
                detail=lock_info.reason if lock_info is not None else None,
                severity="info",
            ),
        ),
    )


def _build_not_loop_ready_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    gate = (
        _payload_mapping(context.raw_state, "readiness_gate") or context.pause_details
    )
    evidence = (
        RecoveryEvidenceRow(
            label="Pause message",
            value=context.pause_message,
            severity="warning",
        ),
    ) + _readiness_gate_evidence(gate)
    return _build_context_recovery_payload(
        context,
        kind="not-loop-ready",
        title="Plan is not loop-ready",
        summary=context.pause_message,
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=evidence,
        actions=(_retry_run_action(context.plan_dir_payload),),
        commands=_preflight_repair_commands(context.snapshot.plan_dir),
    )


def _build_verification_failure_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str,
) -> WorkbenchRecovery:
    selected_turn, verification, selected_session_id = _selected_verification_context(
        context.raw_state,
        context.pause_details,
        reason=reason,
    )
    artifacts = (
        context.base_artifacts
        + _artifact_references_from_payload(
            selected_turn,
            source="failed-turn",
            label_prefix="Failed turn",
        )
        + _verification_check_artifacts(verification)
    )
    return _build_context_recovery_payload(
        context,
        kind=reason,  # type: ignore[arg-type]
        title=_verification_recovery_title(reason),
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=_verification_recovery_evidence(
            plan_dir=context.snapshot.plan_dir,
            reason=reason,
            details=context.pause_details,
            verification=verification,
            session_id=selected_session_id,
        ),
        actions=(
            _inspect_action("Open the failed turn logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
        ),
        commands=_verification_repair_commands(
            context.snapshot.plan_dir,
            session_id=selected_session_id,
        ),
    )


def _build_commit_preflight_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="commit-preflight",
        title="Commit preflight failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Pause reason",
                value="commit-preflight",
                severity="error",
            ),
            RecoveryEvidenceRow(
                label="Pause message",
                value=context.pause_message,
                severity="error",
            ),
        ),
        actions=(
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review launch and commit settings before retrying."),
        ),
        commands=(
            RecoveryCopyCommand(
                label="Inspect worktree",
                command="git status --porcelain",
                description=(
                    "Copy this into the repository shell to inspect dirty files "
                    "before retrying commit mode."
                ),
            ),
        ),
    )


def _build_commit_failed_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    failed_commit = _latest_failed_commit(context.raw_state)
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        failed_commit,
        source="failed-commit",
        label_prefix="Failed commit",
    )
    return _build_context_recovery_payload(
        context,
        kind="commit-failed",
        title="Auto-commit failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_commit=failed_commit,
        artifacts=artifacts,
        evidence=(
            _commit_failed_evidence(failed_commit)
            + _commit_group_evidence(context.raw_state)
        ),
        actions=(
            _inspect_action("Open the failed commit logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review commit mode and launch profile before retrying."),
        ),
    )


def _is_harness_config_recovery(
    *,
    reason: str | None,
    details: Mapping[str, Any],
) -> bool:
    return (
        reason == "harness-unavailable"
        or _payload_str(details, "requested_harness") is not None
        or _payload_str(details, "config_path") is not None
    )


def _build_harness_config_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="generic",
        reason=reason,
    )
    artifacts = (
        context.base_artifacts
        + _harness_config_artifacts(context.pause_details)
        + _artifact_references_from_payload(
            selected_turn,
            source="failed-turn",
            label_prefix="Failed turn",
        )
    )
    return _build_context_recovery_payload(
        context,
        kind="generic",
        title="Harness configuration needs attention",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=_harness_config_evidence(
            reason=reason,
            message=context.pause_message,
            details=context.pause_details,
        ),
        actions=(
            _profile_action("Review harness and profile fields before retrying."),
            _inspect_action("Open runner state, events, and harness config artifacts."),
            _retry_run_action(context.plan_dir_payload),
        ),
    )
