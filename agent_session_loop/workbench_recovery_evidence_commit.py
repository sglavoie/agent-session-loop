"""Evidence rows and artifacts for harness-config, commit and workspace blockers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .workbench_recovery_models import RecoveryArtifactReference, RecoveryEvidenceRow
from .workbench_recovery_state import (
    _coerce_path_value,
    _payload_int,
    _payload_mapping,
    _payload_mappings,
    _payload_str,
)


def _harness_config_artifacts(
    details: Mapping[str, Any],
) -> tuple[RecoveryArtifactReference, ...]:
    config_path = _coerce_path_value(details.get("config_path"))
    if config_path is None:
        return ()
    return (
        RecoveryArtifactReference(
            label="Harness config",
            path=config_path,
            kind="config",
            source="pause-details",
        ),
    )


def _harness_config_evidence(
    *,
    reason: str | None,
    message: str,
    details: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows = [
        RecoveryEvidenceRow(
            label="Pause reason",
            value=reason,
            severity="error",
        ),
        RecoveryEvidenceRow(
            label="Pause message",
            value=message,
            severity="error",
        ),
    ]
    requested_harness = _payload_str(details, "requested_harness")
    if requested_harness is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Requested harness",
                value=requested_harness,
                severity="error",
            )
        )
    config_path = _payload_str(details, "config_path")
    if config_path is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Harness config",
                value=config_path,
                severity="warning",
            )
        )
    error = _payload_str(details, "error")
    if error is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Harness error",
                value=error,
                severity="error",
            )
        )
    return tuple(rows)


def _commit_failed_evidence(
    failed_commit: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    status_result = (
        _payload_str(failed_commit, "status_result")
        if failed_commit is not None
        else None
    )
    returncode = (
        _payload_int(failed_commit, "returncode") if failed_commit is not None else None
    )
    repository = (
        _payload_str(failed_commit, "repository_alias")
        if failed_commit is not None
        else None
    )
    rows = [
        RecoveryEvidenceRow(
            label="Pause reason",
            value="commit-failed",
            severity="error",
        ),
        RecoveryEvidenceRow(
            label="Commit status",
            value=status_result,
            detail=(f"return code {returncode}" if returncode is not None else None),
            severity="error",
        ),
    ]
    if repository is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Failed repository",
                value=repository,
                severity="error",
            )
        )
    return tuple(rows)


def _commit_group_evidence(
    raw_state: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = []
    groups = raw_state.get("commit_groups")
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        return ()
    unresolved = [
        group
        for group in groups
        if isinstance(group, Mapping) and group.get("status") != "complete"
    ]
    for group in unresolved[:3]:
        successful = group.get("successful_repositories")
        shas = (
            [
                f"{row.get('alias') or row.get('key')}={row.get('commit_sha')}"
                for row in successful
                if isinstance(row, Mapping) and row.get("commit_sha")
            ]
            if isinstance(successful, Sequence)
            else []
        )
        remaining = group.get("remaining_aliases")
        remaining_text = (
            ", ".join(str(alias) for alias in remaining[:6])
            if isinstance(remaining, list)
            else None
        )
        detail_parts = []
        if shas:
            detail_parts.append("committed " + ", ".join(shas[:4]))
        if remaining_text:
            detail_parts.append("remaining " + remaining_text)
        rows.append(
            RecoveryEvidenceRow(
                label="Pending commit group",
                value=str(
                    group.get("failed_alias") or group.get("status") or "pending"
                ),
                detail="; ".join(detail_parts) or None,
                severity="error" if group.get("failed_alias") else "warning",
            )
        )
    if len(unresolved) > 3:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional pending commit groups",
                value=str(len(unresolved) - 3),
                severity="warning",
            )
        )
    return tuple(rows)


def _workspace_guard_recovery_evidence(
    details: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    guard = _payload_mapping(details, "workspace_write_guard")
    if guard is None:
        return ()
    rows: list[RecoveryEvidenceRow] = []
    for violation in _payload_mappings(guard, "violations")[:5]:
        alias = _payload_str(violation, "alias") or _payload_str(
            violation, "repository"
        )
        qualified = violation.get("qualified_paths")
        paths = (
            ", ".join(str(path) for path in qualified[:4])
            if isinstance(qualified, list)
            else None
        )
        rows.append(
            RecoveryEvidenceRow(
                label="Scope violation",
                value=alias,
                detail=paths,
                severity="error",
            )
        )
    unavailable = guard.get("unavailable")
    if isinstance(unavailable, Mapping):
        for repository, evidence in list(unavailable.items())[:5]:
            rows.append(
                RecoveryEvidenceRow(
                    label="Repository evidence unavailable",
                    value=str(repository),
                    detail=json.dumps(evidence, sort_keys=True)[:280],
                    severity="error",
                )
            )
    repair = guard.get("repair_required")
    if isinstance(repair, Mapping) and any(repair.values()):
        rows.append(
            RecoveryEvidenceRow(
                label="Rebind and scope guidance",
                value="durable scope + fresh expansion + preflight required",
                detail=(
                    "Workspace changes require `asl prepare PLAN_DIR --resume "
                    "--cwd NEW_WORKSPACE --rebind-workspace`; write authority "
                    "requires a plan edit and fresh preflight."
                ),
                severity="warning",
            )
        )
    return tuple(rows)
