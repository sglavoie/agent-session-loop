from .supervisor_audit_agent_contract import *
from .supervisor_audit_agent_resolution import *


def render_supervisor_audit(
    *,
    run_id: str,
    bundle: audit_evidence_models.AuditInput,
    results: Sequence[assurance_models_findings.DeterministicResult],
    generated_at: str,
    agent: AgentAudit | None = None,
    reason: str | None = None,
) -> assurance_models_audit.SupervisorAudit:
    """Build the canonical audit from validated internal data only.

    Both reports are rendered from this object, so no byte of agent Markdown
    ever reaches an operator: the agent contributes validated lane statuses,
    summaries, findings, and locators, and ASL decides everything else.
    """
    evidence = bundle.evidence
    declared = [entry.locator for entry in evidence]
    lanes = _merged_lanes(results, agent, declared=declared)
    findings = _merged_findings(
        audit_checks.findings_from_results(results), agent.findings if agent else ()
    )
    if agent is None:
        state: assurance_models_vocab.AuditState = "unavailable"
        state_reason = reason or REASON_AGENT_UNAVAILABLE
    elif findings:
        state, state_reason = "findings", None
    elif all(lane.status == "pass" for lane in lanes):
        state, state_reason = "all-clear", None
    elif any(lane.status == "fail" for lane in lanes):
        # Unreachable while every failing lane mints a finding. Kept so a future
        # lane that can fail without one is held back rather than falling
        # through to ``partial``, which would read a disproved lane as merely
        # unjudged.
        state, state_reason = "unavailable", REASON_LANE_COVERAGE
    else:
        state, state_reason = "partial", REASON_LANE_EVIDENCE_ABSENT
    return assurance_models_audit.SupervisorAudit(
        run_id=run_id,
        state=state,
        input_digest=bundle.sha256,
        generated_at=generated_at,
        reason=state_reason,
        evidence=evidence,
        lanes=lanes,
        findings=findings,
        deterministic=tuple(results)[
            : assurance_models_vocab.MAX_DETERMINISTIC_RESULTS
        ],
    )


def _markdown_row(*cells: str) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def render_audit_markdown(audit: assurance_models_audit.SupervisorAudit) -> str:
    """Render the operator-facing report from the validated audit object."""
    lines = [
        f"# Supervisor audit — {audit.run_id}",
        "",
        f"- State: `{audit.state}`",
        f"- Reason: {audit.reason if audit.reason else '(none)'}",
        f"- Generated: {audit.generated_at}",
        f"- Evidence digest: `{audit.input_digest}`",
        "",
        "## Rubric lanes",
        "",
        _markdown_row("Lane", "Status", "Summary", "Evidence"),
        _markdown_row("---", "---", "---", "---"),
    ]
    for lane in audit.lanes:
        lines.append(
            _markdown_row(
                lane.lane,
                lane.status,
                _inline(lane.summary),
                ", ".join(f"`{ref}`" for ref in lane.evidence),
            )
        )
    lines.extend(["", "## Findings", ""])
    if not audit.findings:
        lines.append("No findings.")
    for finding in audit.findings:
        lines.extend(
            [
                f"### {finding.invariant_key} ({finding.severity})",
                "",
                f"- Lane: {finding.lane}",
                f"- Component: {finding.component}",
                f"- Category: {finding.category}",
                f"- Fingerprint: `{finding.fingerprint}`",
                f"- Evidence: {', '.join(f'`{ref}`' for ref in finding.evidence)}",
            ]
        )
        if finding.localization:
            lines.append(
                "- Localization: "
                + ", ".join(f"`{ref}`" for ref in finding.localization)
            )
        lines.extend(
            [
                "",
                _inline(finding.summary),
                "",
                f"**Recommendation:** {_inline(finding.recommendation)}",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## Deterministic checks",
            "",
            _markdown_row("Check", "Status", "Invariant", "Detail"),
            _markdown_row("---", "---", "---", "---"),
        ]
    )
    for result in audit.deterministic:
        lines.append(
            _markdown_row(
                result.check,
                result.status,
                result.invariant_key,
                _inline(result.detail or ""),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = ("render_supervisor_audit", "_markdown_row", "render_audit_markdown")
