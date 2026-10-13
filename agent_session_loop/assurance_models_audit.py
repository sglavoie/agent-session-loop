"""The per-run ``SupervisorAudit`` artifact model and its parser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .assurance_models_findings import (
    DeterministicResult,
    EvidenceRef,
    Finding,
    LaneEvidence,
    parse_deterministic_result,
    parse_evidence_ref,
    parse_finding,
    parse_lane_evidence,
    validate_lane_coverage,
)
from .assurance_models_primitives import (
    AssuranceError,
    _duplicates,
    _require_digest,
    _require_identifier,
    _require_object,
    _require_schema_version,
    _require_sequence,
    _require_timestamp,
    _require_token,
)
from .assurance_models_vocab import (
    ASSURANCE_SCHEMA_VERSION,
    AUDIT_STATES,
    MAX_DETERMINISTIC_RESULTS,
    MAX_EVIDENCE_ENTRIES,
    MAX_FINDINGS,
    RUBRIC_LANES,
    TERMINAL_AUDIT_STATES,
    AuditState,
)
from .literals import as_literal

# ---------------------------------------------------------------------------
# The per-run audit artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorAudit:
    """The canonical, immutable verdict written to ``supervisor-audit.json``."""

    run_id: str
    state: AuditState
    input_digest: str
    generated_at: str
    evidence: tuple[EvidenceRef, ...]
    lanes: tuple[LaneEvidence, ...] = ()
    findings: tuple[Finding, ...] = ()
    deterministic: tuple[DeterministicResult, ...] = ()
    reason: str | None = None
    schema_version: int = ASSURANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "lanes", tuple(self.lanes))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "deterministic", tuple(self.deterministic))
        _require_schema_version(self.schema_version, label="audit 'schema_version'")
        _require_identifier(self.run_id, label="audit 'run_id'")
        as_literal(self.state, AUDIT_STATES, "audit 'state'", error=AssuranceError)
        _require_digest(self.input_digest, label="audit 'input_digest'")
        _require_timestamp(self.generated_at, label="audit 'generated_at'")
        if len(self.evidence) > MAX_EVIDENCE_ENTRIES:
            raise AssuranceError(
                f"audit 'evidence' must hold at most {MAX_EVIDENCE_ENTRIES} entries; "
                f"got {len(self.evidence)}"
            )
        if len(self.findings) > MAX_FINDINGS:
            raise AssuranceError(
                f"audit 'findings' must hold at most {MAX_FINDINGS} entries; "
                f"got {len(self.findings)}"
            )
        if len(self.deterministic) > MAX_DETERMINISTIC_RESULTS:
            raise AssuranceError(
                f"audit 'deterministic' must hold at most "
                f"{MAX_DETERMINISTIC_RESULTS} entries; got {len(self.deterministic)}"
            )
        self._validate_evidence_index()
        self._validate_findings()
        self._validate_state()

    def _validate_evidence_index(self) -> None:
        duplicates = _duplicates(entry.locator for entry in self.evidence)
        if duplicates:
            raise AssuranceError(
                f"audit 'evidence' repeats locator(s) "
                f"{', '.join(repr(item) for item in duplicates)}"
            )
        declared = {entry.locator for entry in self.evidence}
        referenced: list[tuple[str, str]] = []
        for lane in self.lanes:
            referenced.extend((f"lane {lane.lane!r}", ref) for ref in lane.evidence)
        for finding in self.findings:
            referenced.extend(
                (f"finding {finding.invariant_key!r}", ref) for ref in finding.evidence
            )
        for result in self.deterministic:
            referenced.extend(
                (f"deterministic result {result.check!r}", ref)
                for ref in result.evidence
            )
        unknown = sorted({ref for owner, ref in referenced if ref not in declared})
        if unknown:
            raise AssuranceError(
                f"audit references evidence it never declared: "
                f"{', '.join(repr(item) for item in unknown)}"
            )

    def _validate_findings(self) -> None:
        duplicates = _duplicates(finding.fingerprint for finding in self.findings)
        if duplicates:
            raise AssuranceError(
                "audit 'findings' repeats fingerprint(s) "
                f"{', '.join(duplicates)}; one audit reports a defect once"
            )

    def _validate_state(self) -> None:
        terminal = self.state in TERMINAL_AUDIT_STATES
        validate_lane_coverage(self.lanes, require_complete=terminal)
        if self.state in {"skipped", "unavailable", "partial"}:
            if self.reason is None:
                raise AssuranceError(
                    f"an audit in state {self.state!r} must record why"
                )
            _require_token(self.reason, label="audit 'reason'")
        elif self.reason is not None:
            raise AssuranceError(
                f"an audit in state {self.state!r} must not record a reason"
            )
        if self.state == "partial" and self.findings:
            raise AssuranceError(
                "a partial audit cannot carry findings; its state is 'findings'"
            )
        if self.state == "all-clear":
            if self.findings:
                raise AssuranceError(
                    "an all-clear audit cannot carry findings; its state is 'findings'"
                )
            unproven = [lane.lane for lane in self.lanes if lane.status != "pass"]
            if unproven:
                raise AssuranceError(
                    "all-clear requires every rubric lane to pass on its own "
                    f"evidence; lane(s) {', '.join(unproven)} did not"
                )
        elif self.state == "findings" and not self.findings:
            raise AssuranceError(
                "an audit in state 'findings' must carry at least one finding"
            )
        elif self.state in {"skipped", "pending"}:
            if self.findings or self.lanes or self.deterministic:
                raise AssuranceError(
                    f"an audit in state {self.state!r} performed no analysis and "
                    "must carry no lanes, findings, or deterministic results"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "input_digest": self.input_digest,
            "generated_at": self.generated_at,
            "reason": self.reason,
            "evidence": [entry.to_dict() for entry in self.evidence],
            "lanes": [lane.to_dict() for lane in self.lanes],
            "findings": [finding.to_dict() for finding in self.findings],
            "deterministic": [result.to_dict() for result in self.deterministic],
        }


def parse_supervisor_audit(payload: object) -> SupervisorAudit:
    """Parse a ``supervisor-audit.json`` payload, failing closed."""
    label = "audit"
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "schema_version",
            "run_id",
            "state",
            "input_digest",
            "generated_at",
            "reason",
            "evidence",
            "lanes",
            "findings",
            "deterministic",
        ),
    )
    evidence = tuple(
        parse_evidence_ref(item, label=f"{label} evidence #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("evidence"),
                label=f"{label} 'evidence'",
                maximum=MAX_EVIDENCE_ENTRIES,
            )
        )
    )
    lanes = tuple(
        parse_lane_evidence(item, label=f"{label} lane #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("lanes", []),
                label=f"{label} 'lanes'",
                maximum=len(RUBRIC_LANES),
            )
        )
    )
    findings = tuple(
        parse_finding(item, label=f"{label} finding #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("findings", []),
                label=f"{label} 'findings'",
                maximum=MAX_FINDINGS,
            )
        )
    )
    deterministic = tuple(
        parse_deterministic_result(
            item, label=f"{label} deterministic result #{index + 1}"
        )
        for index, item in enumerate(
            _require_sequence(
                data.get("deterministic", []),
                label=f"{label} 'deterministic'",
                maximum=MAX_DETERMINISTIC_RESULTS,
            )
        )
    )
    return SupervisorAudit(
        schema_version=_require_schema_version(
            data.get("schema_version"), label=f"{label} 'schema_version'"
        ),
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        state=as_literal(
            data.get("state"), AUDIT_STATES, f"{label} 'state'", error=AssuranceError
        ),
        input_digest=_require_digest(
            data.get("input_digest"), label=f"{label} 'input_digest'"
        ),
        generated_at=_require_timestamp(
            data.get("generated_at"), label=f"{label} 'generated_at'"
        ),
        reason=(
            None
            if data.get("reason") is None
            else _require_token(data.get("reason"), label=f"{label} 'reason'")
        ),
        evidence=evidence,
        lanes=lanes,
        findings=findings,
        deterministic=deterministic,
    )
