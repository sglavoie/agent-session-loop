"""Shared assembly of the ``WorkbenchRecovery`` payload from one build context."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    workbench_workspace,
)
from .workbench_recovery_models import (
    RecoveryActionCard,
    RecoveryArtifactReference,
    RecoveryCopyCommand,
    RecoveryEvidenceRow,
    RecoveryKind,
    RecoverySeverity,
    RecoveryWarning,
    WorkbenchRecovery,
    _RecoveryBuildContext,
)
from .workbench_recovery_state import (
    _pause_details,
    _payload_int,
    _payload_str,
    _session_identifier_from_payload,
)


def _recovery_common_context(
    *,
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
    selected_turn: Mapping[str, Any] | None,
    selected_commit: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None, int | None, int | None]:
    run_id = (
        _payload_str(raw_state, "run_id")
        or _payload_str(runner_state, "run_id")
        or (
            _payload_str(selected_turn, "run_id") if selected_turn is not None else None
        )
        or (
            _payload_str(selected_commit, "run_id")
            if selected_commit is not None
            else None
        )
    )
    session_id = None
    if selected_turn is not None:
        session_id = _session_identifier_from_payload(selected_turn)
    if session_id is None and selected_commit is not None:
        session_id = _session_identifier_from_payload(selected_commit)
    if session_id is None:
        details = _pause_details(raw_state)
        session_id = _session_identifier_from_payload(details)
    turn_index = (
        _payload_int(selected_turn, "turn_index") if selected_turn is not None else None
    )
    commit_index = (
        _payload_int(selected_commit, "commit_index")
        if selected_commit is not None
        else None
    )
    return run_id, session_id, turn_index, commit_index


def _build_recovery_payload(
    *,
    plan_dir: Path,
    kind: RecoveryKind,
    title: str,
    summary: str,
    severity: RecoverySeverity,
    recoverable: bool,
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
    artifacts: tuple[RecoveryArtifactReference, ...],
    evidence: tuple[RecoveryEvidenceRow, ...] = (),
    actions: tuple[RecoveryActionCard, ...] = (),
    commands: tuple[RecoveryCopyCommand, ...] = (),
    selected_turn: Mapping[str, Any] | None = None,
    selected_commit: Mapping[str, Any] | None = None,
    warnings: tuple[str, ...] = (),
    warning_details: tuple[RecoveryWarning, ...] = (),
    review_resume: Mapping[str, Any] | None = None,
) -> WorkbenchRecovery:
    run_id, session_id, turn_index, commit_index = _recovery_common_context(
        raw_state=raw_state,
        runner_state=runner_state,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
    )
    return WorkbenchRecovery(
        plan_dir=plan_dir,
        kind=kind,
        title=title,
        summary=summary,
        severity=severity,
        recoverable=recoverable,
        evidence=evidence,
        actions=actions,
        commands=commands,
        artifacts=artifacts,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        commit_index=commit_index,
        warnings=warnings,
        warning_details=warning_details,
        review_resume=review_resume or {},
        workspace=workbench_workspace._workspace_projection(
            plan_dir, runtime=raw_state
        ),
        commit_groups=tuple(
            dict(group)
            for group in raw_state.get("commit_groups", ())
            if isinstance(group, Mapping)
        ),
    )


_VERIFICATION_FAILURE_REASONS = frozenset(
    {"verify-failed", "no-contract", "malformed-contract"}
)


def _build_context_recovery_payload(
    context: _RecoveryBuildContext,
    *,
    kind: RecoveryKind,
    title: str,
    summary: str,
    severity: RecoverySeverity,
    recoverable: bool,
    artifacts: tuple[RecoveryArtifactReference, ...],
    evidence: tuple[RecoveryEvidenceRow, ...] = (),
    actions: tuple[RecoveryActionCard, ...] = (),
    commands: tuple[RecoveryCopyCommand, ...] = (),
    selected_turn: Mapping[str, Any] | None = None,
    selected_commit: Mapping[str, Any] | None = None,
) -> WorkbenchRecovery:
    return _build_recovery_payload(
        plan_dir=context.snapshot.plan_dir,
        kind=kind,
        title=title,
        summary=summary,
        severity=severity,
        recoverable=recoverable,
        raw_state=context.raw_state,
        runner_state=context.runner_state,
        artifacts=artifacts,
        evidence=evidence,
        actions=actions,
        commands=commands,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
        warnings=context.warnings,
        warning_details=context.warning_details,
        review_resume=context.review_resume.to_payload(),
    )
