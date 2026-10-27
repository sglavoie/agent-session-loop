from .supervisor_audit_agent_catalog import *
from .supervisor_audit_agent_contract import *


def _agent_schema_block() -> str:
    return json.dumps(
        {
            "schema_version": AGENT_OUTPUT_SCHEMA_VERSION,
            "lanes": [
                {
                    "lane": assurance_models_vocab.RUBRIC_LANES[0],
                    "status": "pass|fail|unavailable",
                    "summary": "what the evidence showed for this lane",
                    "evidence": ["<locator from the evidence index>"],
                }
            ],
            "findings": [
                {
                    "lane": assurance_models_vocab.RUBRIC_LANES[0],
                    "severity": "low|medium|high|critical",
                    "category": "identity",
                    "component": "workspace-registry",
                    "invariant_key": "identity.root-registry-agreement",
                    "summary": "the defect, stated as what the evidence shows",
                    "recommendation": "what a maintainer should change",
                    "evidence": ["<locator from the evidence index>"],
                    "localization": ["<source pointer>"],
                    "identity": {
                        "decision": "existing|new",
                        "issue_id": "<catalog issue_id, only when existing>",
                    },
                }
            ],
        },
        indent=2,
        sort_keys=True,
    )


def build_audit_prompt(
    bundle: audit_evidence_models.AuditInput | Mapping[str, Any],
    *,
    catalog: AgentCatalog,
    correction: str | None = None,
    candidates: Sequence[assurance_projection.IdentityCandidate] = (),
) -> str:
    """Render the prompt for one audit attempt.

    ``correction`` carries the validation failure of the previous attempt and
    nothing else: the rejected answer itself is deliberately withheld, so the
    retry re-derives its verdict from the evidence rather than being handed its
    own earlier output to launder into a better-formed version of the same
    mistake. The catalog is repeated verbatim on that retry, since a correction
    that told an auditor to select from a catalog it no longer had in front of
    it would be asking it to remember an ID instead of reading one.

    ``candidates`` is the other reason a retry happens and is a separate channel
    on purpose. Nothing was rejected: the answer was well-formed and ASL merely
    recognized issues that may already own what it reported. Sending that
    through the correction string would tell an auditor its valid answer broke a
    validation rule, which is the one thing it did not do. The candidates are
    ASL's own catalog records rather than any part of the previous answer.
    """
    payload = (
        bundle.payload
        if isinstance(bundle, audit_evidence_models.AuditInput)
        else dict(bundle)
    )
    prompt = AUDIT_PROMPT_TEMPLATE.format(
        package_root=audit_evidence_constants.EVIDENCE_ROOT_PACKAGE,
        skills_root=audit_evidence_constants.EVIDENCE_ROOT_SKILLS,
        lane_list="\n".join(
            f"- {lane}" for lane in assurance_models_vocab.RUBRIC_LANES
        ),
        schema=_agent_schema_block(),
        catalog=assurance_models_primitives.canonical_json_bytes(
            catalog.to_dict()
        ).decode("utf-8"),
        run_id=payload.get("run_id", "unknown"),
        bundle=assurance_models_primitives.canonical_json_bytes(payload).decode(
            "utf-8"
        ),
    )
    if candidates:
        prompt = f"{prompt}\n{_candidate_section(candidates)}"
    if correction is None:
        return prompt
    return f"{prompt}\n{AUDIT_CORRECTION_TEMPLATE.format(detail=_inline(correction))}"


def _candidate_section(
    candidates: Sequence[assurance_projection.IdentityCandidate],
) -> str:
    return AUDIT_CANDIDATE_TEMPLATE.format(
        candidates=assurance_models_primitives.canonical_json_bytes(
            [candidate.to_dict() for candidate in candidates]
        ).decode("utf-8"),
        invariant=assurance_projection.MATCH_INVARIANT,
        localization=assurance_projection.MATCH_LOCALIZATION,
        summary=assurance_projection.MATCH_SUMMARY,
        threshold=f"{assurance_projection.MIN_SUMMARY_SIMILARITY:.2f}",
    )


def _fenced_json(text: str) -> str:
    """Return the last fenced block meant as JSON, else ``text`` itself.

    Fences are paired line by line, so a block in another language (or the
    closer of one) is skipped rather than read as the start of the answer. A
    ``json`` block wins over an untagged one.
    """
    tagged: list[str] = []
    untagged: list[str] = []
    tag: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = _FENCE_LINE_RE.fullmatch(line)
        if tag is None:
            if match is not None:
                tag = match.group(1).lower()
                body = []
        elif match is not None and not match.group(1):
            if tag == "json":
                tagged.append("\n".join(body))
            elif not tag:
                untagged.append("\n".join(body))
            tag = None
        else:
            body.append(line)
    blocks = tagged or untagged
    return blocks[-1] if blocks else text


def _agent_payload(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise AgentOutputError("malformed-output", "the agent returned no output")
    if len(text.encode("utf-8")) > MAX_AGENT_OUTPUT_BYTES:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answered with more than the {MAX_AGENT_OUTPUT_BYTES}-byte "
            "cap on audit output",
        )
    candidate = _fenced_json(text)
    try:
        payload = json.loads(candidate)
    except (ValueError, RecursionError) as error:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer is not parseable JSON: {error}",
        ) from error
    if not isinstance(payload, Mapping):
        raise AgentOutputError(
            "malformed-output", "the agent answer must be a single JSON object"
        )
    unknown = sorted(
        str(key)
        for key in payload
        if key not in {"schema_version", "lanes", "findings"}
    )
    if unknown:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer has unknown key(s) {', '.join(unknown)}; only "
            "'schema_version', 'lanes', and 'findings' are read",
        )
    if payload.get("schema_version") != AGENT_OUTPUT_SCHEMA_VERSION:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer must declare schema_version "
            f"{AGENT_OUTPUT_SCHEMA_VERSION}; got "
            f"{payload.get('schema_version')!r}",
        )
    return dict(payload)


def _agent_lanes(
    payload: Mapping[str, Any],
) -> tuple[assurance_models_findings.LaneEvidence, ...]:
    entries = payload.get("lanes")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise AgentOutputError(
            "malformed-output", "the agent answer must carry a 'lanes' list"
        )
    lanes = []
    for index, entry in enumerate(entries):
        try:
            lanes.append(
                assurance_models_findings.parse_lane_evidence(
                    entry, label=f"lane #{index + 1}"
                )
            )
        except assurance_models_primitives.AssuranceError as error:
            raise AgentOutputError("malformed-output", str(error)) from error
    try:
        assurance_models_findings.validate_lane_coverage(lanes, require_complete=True)
    except assurance_models_primitives.AssuranceError as error:
        raise AgentOutputError("missing-lanes", str(error)) from error
    return tuple(lanes)


def _identity_decision(
    entry: Mapping[str, Any], *, label: str, catalog: AgentCatalog
) -> IdentityDecision:
    """Validate one finding's identity statement against the supplied catalog.

    The two rejections a caller must be able to tell apart are both here: an ID
    the catalog does not list at all — invented, malformed, or merged away — and
    an ID that exists but did not fit the listing. Only the first is evidence
    the auditor made something up, and answering both with "no such issue" is
    what would teach it to guess.
    """
    payload = entry.get("identity")
    if not isinstance(payload, Mapping):
        raise AgentOutputError(
            "malformed-output",
            f"{label} must carry exactly one 'identity' object, either selecting "
            "an issue the supplied catalog lists or requesting a new allocation",
        )
    unknown = sorted(str(key) for key in payload if key not in _IDENTITY_KEYS)
    if unknown:
        raise AgentOutputError(
            "malformed-output",
            f"{label} 'identity' has unknown key(s) {', '.join(unknown)}; only "
            f"{supervisor_audit_models._names(sorted(_IDENTITY_KEYS))} are read",
        )
    decision = payload.get("decision")
    if decision not in IDENTITY_DECISIONS:
        raise AgentOutputError(
            "malformed-output",
            f"{label} 'identity' must set 'decision' to one of "
            f"{', '.join(IDENTITY_DECISIONS)}; got "
            f"{_inline(str(decision), limit=assurance_models_vocab.MAX_TOKEN_CHARS)!r}",
        )
    declared = payload.get("issue_id")
    if decision == "new":
        if declared is not None:
            raise AgentOutputError(
                "malformed-output",
                f"{label} 'identity' requests a new allocation and must name no "
                "'issue_id'; ASL allocates the ID for it",
            )
        return IdentityDecision(decision="new")
    try:
        issue_id = assurance_models_primitives.parse_issue_id(declared)
    except assurance_models_primitives.AssuranceError as error:
        raise AgentOutputError(
            "malformed-output",
            f"{label} 'identity' selects an existing issue and must name the "
            f"'issue_id' the catalog lists for it: {error}",
        ) from error
    if issue_id in catalog.issue_ids:
        return IdentityDecision(decision="existing", issue_id=issue_id)
    if issue_id in catalog.omitted:
        raise AgentOutputError(
            "invalid-reference",
            f"{label} 'identity' selects {issue_id}, which the size bound kept "
            f"out of the {len(catalog.entries)} issues the catalog listed; select "
            "one it did list or request a new allocation instead",
        )
    raise AgentOutputError(
        "invalid-reference",
        f"{label} 'identity' selects {issue_id}, which the supplied catalog does "
        "not list; an ID ASL never allocated, and one that has been merged away, "
        "are both unselectable, so request a new allocation instead",
    )


def _agent_findings(
    payload: Mapping[str, Any], *, catalog: AgentCatalog
) -> tuple[tuple[Finding, ...], tuple[IdentityDecision, ...]]:
    entries = payload.get("findings", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise AgentOutputError(
            "malformed-output", "the agent answer must carry a 'findings' list"
        )
    if len(entries) > assurance_models_vocab.MAX_FINDINGS:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer must hold at most {assurance_models_vocab.MAX_FINDINGS} "
            f"findings; got {len(entries)}",
        )
    findings = []
    identities = []
    for index, entry in enumerate(entries):
        label = f"finding #{index + 1}"
        if not isinstance(entry, Mapping):
            raise AgentOutputError("malformed-output", f"{label} must be a JSON object")
        claimed = _AGENT_IDENTITY_KEYS.intersection(str(key) for key in entry)
        if claimed:
            raise AgentOutputError(
                "malformed-output",
                f"{label} sets {', '.join(sorted(claimed))}; the audit agent never "
                "assigns issue IDs or fingerprints of its own, ASL computes them "
                "and an existing issue is named only inside 'identity'",
            )
        unknown = sorted(str(key) for key in entry if key not in _AGENT_FINDING_KEYS)
        if unknown:
            raise AgentOutputError(
                "malformed-output",
                f"{label} has unknown key(s) {', '.join(unknown)}",
            )
        identities.append(_identity_decision(entry, label=label, catalog=catalog))
        try:
            findings.append(
                assurance_models_findings.parse_finding(
                    {
                        **{
                            str(key): value
                            for key, value in entry.items()
                            if key != "identity"
                        },
                        "schema_version": (
                            assurance_models_vocab.ASSURANCE_SCHEMA_VERSION
                        ),
                    },
                    label=label,
                )
            )
        except assurance_models_primitives.AssuranceError as error:
            raise AgentOutputError("malformed-output", str(error)) from error
    return tuple(findings), tuple(identities)


def _validate_agent_references(
    lanes: Sequence[assurance_models_findings.LaneEvidence],
    findings: Sequence[Finding],
    *,
    declared: Iterable[str],
) -> None:
    known = set(declared)
    referenced = {ref for lane in lanes for ref in lane.evidence}
    referenced.update(ref for finding in findings for ref in finding.evidence)
    unknown = sorted(referenced - known)
    if unknown:
        raise AgentOutputError(
            "invalid-reference",
            "the agent cited evidence the bundle never declared: "
            f"{supervisor_audit_models._names(unknown)}",
        )
    outside = sorted(
        {
            ref
            for finding in findings
            for ref in finding.localization
            if not any(
                supervisor_audit_models._inside(ref, root)
                for root in LOCALIZATION_ROOTS
            )
        }
    )
    if outside:
        raise AgentOutputError(
            "invalid-reference",
            f"the agent localized outside the read-only source roots "
            f"{', '.join(LOCALIZATION_ROOTS)}: "
            f"{supervisor_audit_models._names(outside)}",
        )


def parse_agent_audit(
    text: str, *, declared: Iterable[str], catalog: AgentCatalog
) -> AgentAudit:
    """Validate one agent answer strictly, or raise :class:`AgentOutputError`.

    Nothing the agent writes is trusted as output: the Markdown it may have
    produced is never rendered, every locator it names must already appear in the
    evidence index ASL built, and the only identity it may state is a selection
    from ``catalog`` — which is the catalog it was actually shown, not the
    current one, so an answer is judged against what it could read.
    """
    payload = _agent_payload(text)
    lanes = _agent_lanes(payload)
    findings, identities = _agent_findings(payload, catalog=catalog)
    _validate_agent_references(lanes, findings, declared=declared)
    return AgentAudit(lanes=lanes, findings=findings, identities=identities)


__all__ = (
    "_agent_schema_block",
    "build_audit_prompt",
    "_candidate_section",
    "_agent_payload",
    "_agent_lanes",
    "_identity_decision",
    "_agent_findings",
    "_validate_agent_references",
    "parse_agent_audit",
)
