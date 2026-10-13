"""Evidence rows for readiness gates, runtime failures and verification failures."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    plans_session_ids,
    preflight_models,
    preflight_runtime_hints,
)
from .workbench_recovery_models import (
    RecoveryArtifactReference,
    RecoveryEvidenceRow,
    RecoveryEvidenceSeverity,
)
from .workbench_recovery_state import (
    _coerce_path_value,
    _latest_relevant_turn,
    _payload_mapping,
    _payload_mappings,
    _payload_str,
    _runtime_failure_reason,
    _session_identifier_from_payload,
)


def _hint_evidence_row(
    hint: preflight_models.RepairHint | None,
) -> RecoveryEvidenceRow | None:
    if hint is None:
        return None
    severity: RecoveryEvidenceSeverity
    if hint.severity_hint == "red":
        severity = "error"
    elif hint.severity_hint == "yellow":
        severity = "warning"
    else:
        severity = "info"
    return RecoveryEvidenceRow(
        label="Repair hint",
        value=hint.message,
        detail=hint.check_id,
        severity=severity,
    )


def _evidence_severity(value: Any) -> RecoveryEvidenceSeverity:
    if value == "red":
        return "error"
    if value == "yellow":
        return "warning"
    return "info"


def _readiness_gate_evidence(
    gate: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = [
        RecoveryEvidenceRow(
            label="Readiness state",
            value=(
                str(gate.get("readiness_state"))
                if gate.get("readiness_state") is not None
                else None
            ),
            severity="warning",
        )
    ]
    expected = _payload_str(gate, "expected_readiness_sha256")
    recorded = _payload_str(gate, "recorded_readiness_sha256")
    if expected is not None or recorded is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Readiness SHA",
                value=recorded,
                detail=f"expected {expected}" if expected else None,
                severity="warning" if expected != recorded else "info",
            )
        )
    counts = _payload_mapping(gate, "findings_by_severity")
    if counts is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Findings",
                value=(
                    f"red {counts.get('red', 0)}, "
                    f"yellow {counts.get('yellow', 0)}, "
                    f"green {counts.get('green', 0)}"
                ),
                severity=(
                    "error"
                    if counts.get("red")
                    else "warning"
                    if counts.get("yellow")
                    else "info"
                ),
            )
        )
    findings = _payload_mappings(gate, "findings")
    for finding in findings[:5]:
        category = _payload_str(finding, "category") or "finding"
        message = _payload_str(finding, "message")
        severity_value = _payload_str(finding, "severity")
        rows.append(
            RecoveryEvidenceRow(
                label=f"{severity_value or 'info'} finding",
                value=category,
                detail=message,
                severity=_evidence_severity(severity_value),
            )
        )
    if len(findings) > 5:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional findings",
                value=str(len(findings) - 5),
                severity="warning",
            )
        )
    return tuple(rows)


def _session_file_path(plan_dir: Path, session_id: str | None) -> Path | None:
    if not session_id:
        return None
    try:
        return plan_dir / plans_session_ids.session_id_to_filename(session_id)
    except ValueError:
        return None


def _session_reference_evidence(
    plan_dir: Path,
    *,
    session_id: str | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = []
    if session_id:
        rows.append(
            RecoveryEvidenceRow(
                label="Session",
                value=session_id,
                severity="error",
            )
        )
    session_path = _session_file_path(plan_dir, session_id)
    if session_path is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Session file",
                value=str(session_path),
                severity="error",
            )
        )
    return tuple(rows)


def _verification_check_evidence(
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    if verification is None:
        return ()
    rows: list[RecoveryEvidenceRow] = []
    failed_checks = [
        check
        for check in _payload_mappings(verification, "checks")
        if check.get("status") != "pass" or _payload_str(check, "failure_reason")
    ]
    for check in failed_checks[:5]:
        name = _payload_str(check, "name") or "check"
        failure_reason = _payload_str(check, "failure_reason")
        repository = _payload_str(check, "repo")
        rows.append(
            RecoveryEvidenceRow(
                label="Failed check",
                value=(f"{repository}:{name}" if repository else name),
                detail=failure_reason,
                severity="error",
            )
        )
    if len(failed_checks) > 5:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional failed checks",
                value=str(len(failed_checks) - 5),
                severity="error",
            )
        )
    return tuple(rows)


_VERIFICATION_ARTIFACT_STREAMS: tuple[tuple[str, str], ...] = (
    ("stdout_artifact_path", "stdout"),
    ("stderr_artifact_path", "stderr"),
)


def _verification_check_artifacts(
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryArtifactReference, ...]:
    """Follow v2 ``*_artifact_path`` references on failed verification checks.

    Schema v2 checks reference the full failed-command output as a gzip
    artifact instead of embedding it. Surfacing those paths lets the Recovery
    Center point operators at the recoverable evidence (readable through the
    bounded gzip-aware log API) rather than only the turn-level logs.
    """
    if verification is None:
        return ()
    refs: list[RecoveryArtifactReference] = []
    for check in _payload_mappings(verification, "checks"):
        if check.get("status") == "pass" and not _payload_str(check, "failure_reason"):
            continue
        name = _payload_str(check, "name") or "check"
        for key, stream in _VERIFICATION_ARTIFACT_STREAMS:
            path = _coerce_path_value(check.get(key))
            if path is None:
                continue
            refs.append(
                RecoveryArtifactReference(
                    label=f"Check {name} {stream}",
                    path=path,
                    kind="verification-output",
                    source="verification-check",
                )
            )
    return tuple(refs)


def _runtime_recovery_evidence(
    *,
    reason: str,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    rows = [RecoveryEvidenceRow(label="Pause reason", value=reason, severity="error")]
    failure_reason = _runtime_failure_reason(details=details, verification=verification)
    if failure_reason is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Failure reason",
                value=failure_reason,
                severity="error",
            )
        )
    hint = preflight_runtime_hints.runtime_repair_hint(
        reason,
        failure_reason=failure_reason,
    )
    hint_row = _hint_evidence_row(hint)
    if hint_row is not None:
        rows.append(hint_row)
    if verification is not None:
        passed = verification.get("passed")
        if isinstance(passed, bool):
            rows.append(
                RecoveryEvidenceRow(
                    label="Verification passed",
                    value=str(passed).lower(),
                    severity="info" if passed else "error",
                )
            )
    return tuple(rows)


def _verification_recovery_evidence(
    *,
    plan_dir: Path,
    reason: str,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
    session_id: str | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    return (
        _runtime_recovery_evidence(
            reason=reason,
            details=details,
            verification=verification,
        )
        + _session_reference_evidence(plan_dir, session_id=session_id)
        + _verification_check_evidence(verification)
    )


_VERIFICATION_RECOVERY_TITLES: dict[str, str] = {
    "verify-failed": "Verification failed",
    "no-contract": "Completion contract missing",
    "malformed-contract": "Completion contract malformed",
}


def _verification_recovery_title(reason: str) -> str:
    return _VERIFICATION_RECOVERY_TITLES[reason]


def _selected_verification_context(
    raw_state: Mapping[str, Any],
    details: Mapping[str, Any],
    *,
    reason: str,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
    selected_turn = _latest_relevant_turn(
        raw_state,
        kind=reason,  # type: ignore[arg-type]
        reason=reason,
    )
    verification = (
        _payload_mapping(selected_turn, "verification")
        if selected_turn is not None
        else None
    )
    selected_session_id = (
        _session_identifier_from_payload(selected_turn)
        if selected_turn is not None
        else _session_identifier_from_payload(details)
    )
    return selected_turn, verification, selected_session_id
