from .supervisor_audit_agent_catalog import *
from .supervisor_audit_agent_contract import *


@dataclass(frozen=True)
class IdentityResolution:
    """What ASL made of one answer's identity decisions.

    ``audit`` is the answer with every decision ASL could settle already settled,
    so a caller downstream reads one list rather than re-deriving recurrences.
    ``candidates`` is non-empty exactly when at least one finding asked for an
    allocation the catalog can argue with, and the audit is unusable until that
    is answered — semantic similarity identifies a candidate and never merges
    one, so there is no third option where ASL decides for itself.
    """

    audit: AgentAudit
    candidates: tuple[assurance_projection.IdentityCandidate, ...] = ()
    ambiguous: int = 0

    @property
    def settled(self) -> bool:
        return self.ambiguous == 0

    @property
    def detail(self) -> str:
        return (
            f"{self.ambiguous} finding(s) requested a new allocation for a defect "
            f"the catalog already names a candidate for: "
            f"{
                supervisor_audit_models._names(
                    candidate.issue_id for candidate in self.candidates
                )
            }"
        )


def resolve_identities(
    audit: AgentAudit, *, catalog: assurance_projection.IdentityCatalog
) -> IdentityResolution:
    """Settle each finding's identity against the catalog, or report ambiguity.

    Two decisions need nobody: a selection the validator already checked against
    the supplied catalog, and a new-allocation request whose fingerprint the
    catalog already names an issue for. The second overrules the answer, because
    a fingerprint is derived by ASL from the finding's own category, component,
    and invariant key — an auditor that reworded a summary did not make the
    defect new, and allocating a second issue for it is exactly the split the
    inbox exists to prevent.

    Everything else the catalog recognizes is a candidate, and a candidate is
    never applied. It goes back as a question, and if the answer is still ``new``
    the audit fails closed rather than either merging or allocating.
    """
    resolved: list[IdentityDecision] = []
    candidates: dict[str, assurance_projection.IdentityCandidate] = {}
    ambiguous = 0
    for finding, decision in audit.decisions:
        if not decision.is_new:
            resolved.append(decision)
            continue
        recurrence = catalog.issue_for_fingerprint(finding.fingerprint)
        if recurrence is not None:
            resolved.append(
                IdentityDecision(
                    decision="existing", issue_id=recurrence, source="recurrence"
                )
            )
            continue
        matched = catalog.candidates(finding)
        resolved.append(decision)
        if not matched:
            continue
        ambiguous += 1
        for candidate in matched:
            known = candidates.get(candidate.issue_id)
            candidates[candidate.issue_id] = (
                candidate if known is None else known.merged(candidate)
            )
    ranked = sorted(candidates.values(), key=assurance_projection.candidate_order)
    return IdentityResolution(
        audit=replace(audit, identities=tuple(resolved)),
        candidates=tuple(ranked[:MAX_PROMPTED_CANDIDATES]),
        ambiguous=ambiguous,
    )


def _worst(*statuses: CheckStatus) -> CheckStatus:
    return max(
        statuses,
        key=lambda status: supervisor_audit_vocabulary._STATUS_PRECEDENCE[status],
    )


def _status_counts(
    results: Sequence[assurance_models_findings.DeterministicResult],
) -> str:
    if not results:
        return "no deterministic check reported"
    counts = {status: 0 for status in assurance_models_vocab.CHECK_STATUSES}
    for result in results:
        counts[result.status] += 1
    return ", ".join(f"{count} {status}" for status, count in counts.items() if count)


def _anchor(declared: Sequence[str]) -> tuple[str, ...]:
    if supervisor_audit_vocabulary.RUN_ANCHOR in declared:
        return (supervisor_audit_vocabulary.RUN_ANCHOR,)
    return (declared[0],) if declared else ()


def _deterministic_signal(
    results: Sequence[assurance_models_findings.DeterministicResult],
) -> CheckStatus | None:
    """Return what the checks on one lane prove, or ``None`` when they prove nothing.

    A failure is the strongest signal and wins outright; otherwise a single pass
    is a real, reproducible verdict. Checks that all answered ``unavailable``
    prove neither, so they contribute nothing rather than a pessimistic verdict.
    """
    statuses = {result.status for result in results}
    if "fail" in statuses:
        return "fail"
    if "pass" in statuses:
        return "pass"
    return None


def _merged_lanes(
    results: Sequence[assurance_models_findings.DeterministicResult],
    agent: AgentAudit | None,
    *,
    declared: Sequence[str],
) -> tuple[assurance_models_findings.LaneEvidence, ...]:
    """Reduce deterministic results and agent lanes to one record per lane.

    Where both sides carry a signal the worse one wins: an agent that reports
    ``pass`` on a lane a deterministic check disproved does not get to overrule
    the check. A check that answered ``unavailable`` is not a signal, though — it
    says the check could not read its evidence, which is a statement about the
    check and not about the run. Letting it drag the lane down would mean a
    legacy plan with no registry binding could never produce a clean audit no
    matter what the auditor found, so an all-``unavailable`` lane defers to the
    agent's claim, and a lane with neither signal stays ``unavailable``.
    """
    reported = {lane.lane: lane for lane in (agent.lanes if agent else ())}
    lanes = []
    for name in assurance_models_vocab.RUBRIC_LANES:
        lane_results = [result for result in results if result.lane == name]
        claim = reported.get(name)
        claimed: CheckStatus = claim.status if claim else "unavailable"
        deterministic = _deterministic_signal(lane_results)
        status = claimed if deterministic is None else _worst(deterministic, claimed)
        evidence = list(
            dict.fromkeys(
                [
                    *(claim.evidence if claim else ()),
                    *(ref for result in lane_results for ref in result.evidence),
                ]
            )
        )[: assurance_models_vocab.MAX_REFERENCES_PER_ITEM]
        deterministic_note = f"Deterministic checks: {_status_counts(lane_results)}."
        summary = (
            f"{_inline(claim.summary)} {deterministic_note}"
            if claim
            else f"Agent coverage was unavailable for this lane. {deterministic_note}"
        )
        lanes.append(
            assurance_models_findings.LaneEvidence(
                lane=name,
                status=status,
                summary=_inline(summary),
                evidence=tuple(evidence) or _anchor(declared),
            )
        )
    return tuple(lanes)


def _merged_findings(
    deterministic: Sequence[Finding], agent: Sequence[Finding]
) -> tuple[Finding, ...]:
    """Combine both sources, letting the reproducible finding own a fingerprint."""
    merged: dict[str, Finding] = {}
    for finding in (*deterministic, *agent):
        merged.setdefault(finding.fingerprint, finding)
    return tuple(merged.values())[: assurance_models_vocab.MAX_FINDINGS]


def _identity_selections(
    agent: AgentAudit, findings: Sequence[Finding]
) -> dict[str, str]:
    """Restate the agent's selections in the form the publication revalidates.

    Keyed by fingerprint because that is how a decision is attributed to a
    finding under the append lock, and restricted to the findings the canonical
    audit actually carries: merging and the finding cap can drop an agent
    finding, and a selection naming one the publication never sees is refused.
    The first decision for a fingerprint wins, which is the decision belonging to
    the finding :func:`_merged_findings` kept.
    """
    carried = {finding.fingerprint for finding in findings}
    selections: dict[str, str] = {}
    # A ``new`` decision selects no issue yet still claims its fingerprint, so
    # a later duplicate's ``existing`` decision cannot attach to the kept one.
    decided: set[str] = set()
    for finding, decision in agent.decisions:
        if finding.fingerprint in decided:
            continue
        decided.add(finding.fingerprint)
        if decision.issue_id is None or finding.fingerprint not in carried:
            continue
        selections[finding.fingerprint] = decision.issue_id
    return selections


__all__ = (
    "IdentityResolution",
    "resolve_identities",
    "_worst",
    "_status_counts",
    "_anchor",
    "_deterministic_signal",
    "_merged_lanes",
    "_merged_findings",
    "_identity_selections",
)
