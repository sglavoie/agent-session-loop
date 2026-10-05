"""The independent agent audit's contract, identities, and reports.

This module owns everything about the auditor's answer that does not involve
launching it: the prompt and correction templates, the bounded identity
catalog the auditor chooses issues from, the parser that rejects any answer
outside the JSON contract, the resolution of each finding's identity decision,
and the merge of agent findings with the deterministic verdicts into the
canonical audit and its staged reports.

It reads :mod:`.supervisor_audit_evidence` and :mod:`.supervisor_audit_checks`
and is read by the :mod:`.supervisor_audit` entry module, which launches the
auditor, runs the attempt loop, and publishes what this module renders.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    assurance_ledger,
    assurance_models,
    assurance_projection,
    durability,
)
from . import supervisor_audit_checks as audit_checks
from . import supervisor_audit_evidence as audit_evidence
from .assurance_models import (
    AttemptOutcome,
    CheckStatus,
    Finding,
)

#: Run-relative directory the canonical reports are built in before they become
#: canonical. Nothing durable ever names a path inside it, so its contents are
#: scratch that only the publication that created them can be interested in.
AUDIT_STAGING_DIR_NAME = "supervisor-audit-staging"


#: Version of the JSON contract the auditor answers in. v2 requires exactly one
#: identity decision per finding; a v1 answer carried none, so it is rejected
#: rather than read as an implicit request to mint a new issue.
AGENT_OUTPUT_SCHEMA_VERSION = 2

#: Largest identity catalog handed to an auditor. The inbox grows without bound
#: while a prompt does not, so the catalog is a bounded sample of a complete
#: index and says how much it dropped rather than pretending to be whole.
MAX_AGENT_CATALOG_BYTES = 64 * 1024

#: Longest summary carried per catalog entry. An auditor needs enough of a
#: summary to recognize its own defect in it, not the whole record.
MAX_CATALOG_SUMMARY_CHARS = 240

#: The only two identity statements a finding may make.
IDENTITY_DECISIONS: tuple[str, ...] = ("existing", "new")

#: Largest agent message parsed. An auditor that answers with more than this is
#: not answering the contract, and parsing an unbounded reply is how untrusted
#: output starts costing memory instead of being rejected.
MAX_AGENT_OUTPUT_BYTES = 256 * 1024

#: The only roots a localization may name. They are exactly the read-only
#: source trees the audit profile grants, so a localization pointing anywhere
#: else describes something the auditor could not have read.
LOCALIZATION_ROOTS: tuple[str, ...] = (
    audit_evidence.EVIDENCE_ROOT_PACKAGE,
    audit_evidence.EVIDENCE_ROOT_SKILLS,
)

#: Why a launched audit reports no agent coverage.
REASON_AGENT_UNAVAILABLE = "agent-coverage-unavailable"

#: Why an audit with agent coverage and no findings was once recorded as
#: unavailable. No new audit is written under it; it stays so the records that
#: already carry it keep parsing.
REASON_LANE_COVERAGE = "incomplete-lane-coverage"

#: Why an audit that ran, found nothing, and could not judge every lane is
#: ``partial``: some lane had neither a reproducible verdict nor an agent claim.
REASON_LANE_EVIDENCE_ABSENT = "lane-evidence-absent"

#: Most candidates one retry prompt carries, across every finding that asked for
#: a new allocation. The listing is widened to make each one selectable, so this
#: also bounds how far a retry's catalog may exceed
#: :data:`MAX_AGENT_CATALOG_BYTES`.
MAX_PROMPTED_CANDIDATES = 8

_JSON_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.DOTALL)

#: Keys the agent may set on a finding. ``fingerprint``, ``issue_id``, and
#: ``schema_version`` are absent because ASL computes them; an agent that sends
#: one is claiming an authority it does not have. ``identity`` is the one place
#: an issue may be named, and only by selecting one ASL already supplied.
_AGENT_FINDING_KEYS: frozenset[str] = frozenset(
    {
        "category",
        "component",
        "identity",
        "invariant_key",
        "lane",
        "severity",
        "summary",
        "recommendation",
        "evidence",
        "localization",
    }
)

_IDENTITY_KEYS: frozenset[str] = frozenset({"decision", "issue_id"})

_AGENT_IDENTITY_KEYS: frozenset[str] = frozenset(
    {"fingerprint", "id", "issue", "issue_id", "issue_ids"}
)

AUDIT_PROMPT_TEMPLATE = """\
You are auditing the control plane of one finished agent-session-loop (ASL) run.
You are not reviewing the application the run worked on.

Order of work, which is not optional:

1. Analyze the operational evidence below first, on its own terms. Every claim
   you make must rest on a record in that bundle: identities, paths, digests,
   exit codes, timings, guard results, events, and status projections.
2. Only after you have identified a specific anomaly in that evidence may you
   read ASL source to localize it. That reading is read-only and is limited to
   the `{package_root}` and `{skills_root}` locator roots.

Prohibited, without exception:

- Reviewing, grading, or commenting on the application implementation the run
  produced: its code quality, design, tests, or diffs are out of scope.
- Editing, creating, or deleting any file. You have read access only.
- Assigning issue IDs, fingerprints, or any identifier of your own invention.
  ASL computes fingerprints and allocates every issue ID. The only identity you
  ever state is a selection from the catalog ASL supplies below.

Judge these eight rubric lanes, all of them, every time:

{lane_list}

Answer with exactly one JSON object, in a single fenced ```json block, and
nothing that must be parsed outside it:

{schema}

Rules the answer must satisfy:

- Workspace snapshot differences identify activity, not its writer. Neither
  `supervisor-audit-workspace-change.json` nor the historical
  `supervisor-audit-mutation.json` alone proves that the auditor wrote anything.
  Such activity invalidates the audit, but do not report an auditor breach or a
  critical mutation finding without independent writer-specific evidence.

- `lanes` holds exactly one record per rubric lane named above. A lane you
  could not judge is `unavailable` and still names the evidence that proves it
  could not be judged; it is never `pass`.
- Every `evidence` entry is a locator copied verbatim from the `evidence` index
  of the bundle below. Invented, absolute, or `..`-bearing locators are
  rejected.
- `category`, `component`, and `invariant_key` are lowercase tokens of
  alphanumerics separated by `.`, `_`, `-`, or `/`. Reuse an existing
  `invariant_key` when you are reporting the same invariant a deterministic
  check already names.
- `severity` is one of low, medium, high, critical.
- `localization` is optional, appears only on a finding, and holds source
  pointers under `{package_root}/` or `{skills_root}/` such as
  `{package_root}/runner.py:8129`. Omit it when you did not localize anything.
- Report a finding only for a control-plane defect. An audit with no defects
  returns an empty `findings` list and passing lanes.
- A run that refused or paused before launching an unauthorized turn is the
  repository-intent control working, not a repository-intent defect. Report a
  defect only when evidence shows unauthorized work launched, the gate was
  bypassable, or the recorded authority disagreed with the work performed.
- A registry-free legacy plan confined to the Git repository containing its
  bound execution root is allowed by ASL's compatibility contract. Do not infer
  missing authority merely from the absence of registry and scope projections.
- In `state`, `requested_model`/`requested_model_source` (and their compatibility
  aliases `model`/`model_source`) describe the requested default. Per-role
  `model_projection` is authoritative for the effective model. An unresolved
  requested default beside a resolved effective role model is not a
  contradiction.
- A verification with `passed: false` is never a pass. It may carry an explicit
  `findings-defer-broad-failures` acceptance only when every deferred check is a
  `broad:*` replay under a valid findings verdict. Treat that deferral as
  resolved only when a later review reaches all-clear. A valid deferral on a
  paused or still in-flight run is provisional, even if repair materialization
  failed or its budget was exhausted. If `required-checks-incomplete` preserves
  the failed checks and withholds run success, that is the terminal gate working:
  report the unresolved verification as `unavailable`, not as a contradiction
  merely because the review session's status is complete. A finished run that
  leaves the deferral unredeemed, a success claim despite outstanding failures,
  or a malformed acceptance (wrong session, non-broad checks, or checks that
  never failed) remains a defect. A pause never excuses malformed acceptance.
- A `failure_diagnostic` of `null` on a check ASL did not generate is the
  content boundary working, not a lost diagnostic. Only a gate the runner
  rendered itself may promote its last stderr line into control-plane text, so
  a `ci` command — or any other command authored in a plan's
  `## Completion Contract` — reports `null` by design, however much output it
  retained. The rejection survives in the retained stderr artifact, which the
  bundle indexes for every failed check. Report a defect here only if a
  generated gate lost its diagnostic, or if that stderr artifact is itself
  missing from the evidence index.

Every finding carries exactly one `identity` object, and there is no default:

- `{{"decision": "existing", "issue_id": "ASI-N"}}` says this defect is the one
  the catalog below already names under `ASI-N`. Copy that ID from the catalog
  verbatim; an ID the catalog does not list is rejected, and so is an ID
  anywhere on a finding other than inside its own `identity` object.
- `{{"decision": "new"}}` says the catalog names nothing that is this defect,
  and asks ASL to allocate an issue for it. It names no ID, because you do not
  have one to name.
- `omitted` counts the issues the size bound kept out of the listing below. It
  is not zero when the catalog is a sample: an ID you remember but cannot find
  listed is still not selectable, so answer `new` rather than naming it.

The catalog is ASL's canonical inbox, every lifecycle state included, so a
defect that recurs after being resolved, dismissed, or accepted still selects
the issue that already owns it:

```json
{catalog}
```

Operational evidence for run {run_id}:

```json
{bundle}
```
"""

AUDIT_CORRECTION_TEMPLATE = """\
Your previous answer was rejected by ASL's validator:

{detail}

This is a fresh invocation and your previous answer is gone. Redo the audit
from the evidence above and return only a corrected JSON object in a single
fenced ```json block.

If that rejection is about identity, the catalog above is the entire set of
issues you may select from, and it is the same catalog you were given before.
An ID it does not list cannot be selected no matter where you saw it: answer
`{{"decision": "new"}}` for that finding instead.
"""

AUDIT_CANDIDATE_TEMPLATE = """\
Your previous answer was well-formed and no validation rule rejected it. It
asked ASL to allocate a new issue for at least one defect, and ASL's catalog
already names issues that may be that same defect under another wording:

```json
{candidates}
```

`reasons` says what ASL matched on. `{invariant}` is an identical invariant key
under a different category or component, which is the rename a fingerprint
cannot see. `{localization}` is the same source file plus an overlapping
invariant-key segment. `{summary}` is normalized-summary
overlap of at least {threshold}, reported in `similarity`; that field is 0 when
the summaries are not what matched.

Every issue listed there is in the catalog above and is selectable, including
any the size bound had left out. This is a fresh invocation and your previous
answer is gone, so redo the audit from the evidence and decide each defect's
identity again. Select the issue that already owns a defect with
`{{"decision": "existing", "issue_id": "ASI-N"}}`, or answer
`{{"decision": "new"}}` if none of them is it.

ASL never merges a defect into a candidate on its own, and it will not allocate
past one either: a defect you report as new a second time leaves the audit
recorded as identity-ambiguous with no issue opened for it. Answer `new` when
that is what the evidence shows, not to move past this list.
"""


class AgentOutputError(Exception):
    """Raised when agent output fails validation, carrying the retry outcome.

    The outcome is the vocabulary the attempt record and the retry decision both
    read, so ``malformed-output``, ``missing-lanes``, and ``invalid-reference``
    stay distinguishable instead of collapsing into one opaque parse failure.
    """

    def __init__(self, outcome: AttemptOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


#: Who decided an identity. ``agent`` is a selection from the supplied catalog;
#: ``recurrence`` is ASL overruling a new-allocation request because the
#: finding's fingerprint already names an issue, which is a fact rather than a
#: judgement and therefore needs nobody's agreement.
IDENTITY_SOURCES: tuple[str, ...] = ("agent", "recurrence")


@dataclass(frozen=True)
class IdentityDecision:
    """The one identity statement a finding is allowed to make.

    ``existing`` names an issue the supplied catalog listed and nothing else;
    ``new`` asks ASL to allocate one and therefore names nothing. Both are
    requests: the decision records what the auditor asked for, and allocation
    remains ASL's.
    """

    decision: str
    issue_id: str | None = None
    source: str = "agent"

    def __post_init__(self) -> None:
        if self.source not in IDENTITY_SOURCES:
            raise AgentOutputError(
                "malformed-output",
                f"an identity decision comes from one of "
                f"{', '.join(IDENTITY_SOURCES)}; got {self.source!r}",
            )

    @property
    def is_new(self) -> bool:
        return self.decision == "new"


@dataclass(frozen=True)
class AgentAudit:
    """One validated agent answer: lanes, findings, and their identities.

    ``identities`` is positional against ``findings`` and the two are refused
    at different lengths, because an identity decision that cannot be attributed
    to a specific finding is worse than none: it would be attached to whichever
    defect happened to line up.
    """

    lanes: tuple[assurance_models.LaneEvidence, ...]
    findings: tuple[Finding, ...]
    identities: tuple[IdentityDecision, ...]

    def __post_init__(self) -> None:
        if len(self.identities) != len(self.findings):
            raise AgentOutputError(
                "malformed-output",
                "every finding carries exactly one identity decision; the answer "
                f"held {len(self.findings)} finding(s) and "
                f"{len(self.identities)} decision(s)",
            )

    @property
    def decisions(self) -> tuple[tuple[Finding, IdentityDecision], ...]:
        """Each finding paired with the identity decision it arrived with."""
        return tuple(zip(self.findings, self.identities, strict=True))


def _inline(text: str, *, limit: int = assurance_models.MAX_TEXT_CHARS) -> str:
    """Collapse validated text to one line so it can be rendered into a cell."""
    collapsed = " ".join(audit_checks._CONTROL_CHAR_RE.sub(" ", str(text)).split())
    return collapsed[:limit]


@dataclass(frozen=True)
class AgentCatalogEntry:
    """One canonical issue as an auditor sees it.

    Normalized localization and summary tokens are deliberately absent: they
    exist so ASL can recognize a recurrence, and an auditor picking an ID by
    reading the record has no use for them but would pay for them out of the
    size bound.
    """

    issue_id: str
    state: str
    severity: str
    category: str
    component: str
    invariant_key: str
    lane: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {
            "issue_id": self.issue_id,
            "state": self.state,
            "severity": self.severity,
            "category": self.category,
            "component": self.component,
            "invariant_key": self.invariant_key,
            "lane": self.lane,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class AgentCatalog:
    """The bounded catalog supplied with a prompt, and what it left out.

    ``omitted`` names the issues the size bound dropped, which is not the same
    fact as an issue not existing. Answering "no such issue" to a selection the
    listing merely could not fit is what would teach an auditor to invent IDs,
    so the two are kept apart here and rejected with different explanations.
    """

    digest: str
    entries: tuple[AgentCatalogEntry, ...] = ()
    omitted: tuple[str, ...] = ()
    issue_ids: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", frozenset(entry.issue_id for entry in self.entries)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "omitted": len(self.omitted),
            "issues": [entry.to_dict() for entry in self.entries],
        }


def _agent_catalog_entry(
    entry: assurance_projection.CatalogEntry,
) -> AgentCatalogEntry:
    return AgentCatalogEntry(
        issue_id=entry.issue_id,
        state=entry.state,
        severity=entry.severity,
        category=entry.category,
        component=entry.component,
        invariant_key=entry.invariant_key,
        lane=entry.lane,
        summary=_inline(entry.summary, limit=MAX_CATALOG_SUMMARY_CHARS),
    )


def build_agent_catalog(catalog: assurance_projection.IdentityCatalog) -> AgentCatalog:
    """Return the agent-facing form of ``catalog``, bounded and self-describing.

    The bound is spent on the most recently seen issues, because a defect sighted
    lately is the one an auditor is most likely to be looking at again, and the
    survivors are listed in issue order so the listing reads as a catalog rather
    than as a ranking. The digest covers the whole catalog, not the sample: it
    names the identity state a decision was made against, which is what a later
    publisher revalidates, and a sample cannot answer that question.
    """
    number = assurance_models.issue_id_number
    ranked = sorted(
        catalog.entries,
        key=lambda entry: (entry.last_seen_at, number(entry.issue_id)),
        reverse=True,
    )
    envelope = len(
        assurance_models.canonical_json_bytes(
            {"digest": catalog.digest, "omitted": len(ranked), "issues": []}
        )
    )
    budget = MAX_AGENT_CATALOG_BYTES - envelope
    kept: list[AgentCatalogEntry] = []
    omitted: list[str] = []
    used = 0
    for entry in ranked:
        supplied = _agent_catalog_entry(entry)
        body = len(assurance_models.canonical_json_bytes(supplied.to_dict())) - 1
        # Every entry after the first also pays for the comma that precedes it.
        cost = body + (1 if kept else 0)
        if omitted or used + cost > budget:
            omitted.append(entry.issue_id)
            continue
        used += cost
        kept.append(supplied)
    return AgentCatalog(
        digest=catalog.digest,
        entries=tuple(sorted(kept, key=lambda entry: number(entry.issue_id))),
        omitted=tuple(sorted(omitted, key=number)),
    )


#: What an auditor is given when the ledger holds no issue, or holds issues
#: nobody can read. Every finding then has to request a new allocation, which is
#: the honest answer when no existing issue can be shown.
EMPTY_AGENT_CATALOG = build_agent_catalog(assurance_projection.IdentityCatalog())


def widen_agent_catalog(
    supplied: AgentCatalog,
    catalog: assurance_projection.IdentityCatalog,
    issue_ids: Sequence[str],
) -> AgentCatalog:
    """Return ``supplied`` with ``issue_ids`` promoted into the listing.

    A candidate the size bound had dropped is worth more to a retry than the
    entry recency ranking kept in its place: proposing an ID the validator would
    then refuse as unselectable would burn the attempt and teach nothing. The
    digest is unchanged because it names the whole inbox rather than the sample,
    so widening what is listed does not restate what the decision is valid for.
    """
    promoted = []
    for issue_id in issue_ids:
        if issue_id in supplied.issue_ids:
            continue
        entry = catalog.entry(issue_id)
        if entry is not None:
            promoted.append(_agent_catalog_entry(entry))
    if not promoted:
        return supplied
    number = assurance_models.issue_id_number
    added = frozenset(entry.issue_id for entry in promoted)
    return AgentCatalog(
        digest=supplied.digest,
        entries=tuple(
            sorted(
                (*supplied.entries, *promoted), key=lambda entry: number(entry.issue_id)
            )
        ),
        omitted=tuple(item for item in supplied.omitted if item not in added),
    )


def read_identity_catalog(
    state_dir: Path | str | None,
) -> assurance_projection.IdentityCatalog:
    """Return the inbox's identity catalog, or an empty one.

    An unreadable ledger costs the auditor its ability to recognize a recurrence,
    and costs ASL its ability to recognize one on the auditor's behalf. It must
    not also cost the run its audit, so this degrades to the empty catalog: the
    audit still runs, and every finding it reports asks for a new issue instead
    of selecting one nobody could show it.
    """
    try:
        projection = assurance_projection.read_projection(state_dir=state_dir)
    except (
        OSError,
        assurance_ledger.LedgerError,
        assurance_models.AssuranceError,
    ):
        return assurance_projection.IdentityCatalog()
    return projection.catalog


def read_agent_catalog(state_dir: Path | str | None) -> AgentCatalog:
    """Return the agent-facing catalog to supply for one audit."""
    return build_agent_catalog(read_identity_catalog(state_dir))


def _agent_schema_block() -> str:
    return json.dumps(
        {
            "schema_version": AGENT_OUTPUT_SCHEMA_VERSION,
            "lanes": [
                {
                    "lane": assurance_models.RUBRIC_LANES[0],
                    "status": "pass|fail|unavailable",
                    "summary": "what the evidence showed for this lane",
                    "evidence": ["<locator from the evidence index>"],
                }
            ],
            "findings": [
                {
                    "lane": assurance_models.RUBRIC_LANES[0],
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
    bundle: audit_evidence.AuditInput | Mapping[str, Any],
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
        if isinstance(bundle, audit_evidence.AuditInput)
        else dict(bundle)
    )
    prompt = AUDIT_PROMPT_TEMPLATE.format(
        package_root=audit_evidence.EVIDENCE_ROOT_PACKAGE,
        skills_root=audit_evidence.EVIDENCE_ROOT_SKILLS,
        lane_list="\n".join(f"- {lane}" for lane in assurance_models.RUBRIC_LANES),
        schema=_agent_schema_block(),
        catalog=assurance_models.canonical_json_bytes(catalog.to_dict()).decode(
            "utf-8"
        ),
        run_id=payload.get("run_id", "unknown"),
        bundle=assurance_models.canonical_json_bytes(payload).decode("utf-8"),
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
        candidates=assurance_models.canonical_json_bytes(
            [candidate.to_dict() for candidate in candidates]
        ).decode("utf-8"),
        invariant=assurance_projection.MATCH_INVARIANT,
        localization=assurance_projection.MATCH_LOCALIZATION,
        summary=assurance_projection.MATCH_SUMMARY,
        threshold=f"{assurance_projection.MIN_SUMMARY_SIMILARITY:.2f}",
    )


def _agent_payload(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise AgentOutputError("malformed-output", "the agent returned no output")
    if len(text.encode("utf-8")) > MAX_AGENT_OUTPUT_BYTES:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answered with more than the {MAX_AGENT_OUTPUT_BYTES}-byte "
            "cap on audit output",
        )
    fenced = _JSON_FENCE_RE.findall(text)
    candidate = fenced[-1] if fenced else text
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
) -> tuple[assurance_models.LaneEvidence, ...]:
    entries = payload.get("lanes")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise AgentOutputError(
            "malformed-output", "the agent answer must carry a 'lanes' list"
        )
    lanes = []
    for index, entry in enumerate(entries):
        try:
            lanes.append(
                assurance_models.parse_lane_evidence(entry, label=f"lane #{index + 1}")
            )
        except assurance_models.AssuranceError as error:
            raise AgentOutputError("malformed-output", str(error)) from error
    try:
        assurance_models.validate_lane_coverage(lanes, require_complete=True)
    except assurance_models.AssuranceError as error:
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
            f"{audit_checks._names(sorted(_IDENTITY_KEYS))} are read",
        )
    decision = payload.get("decision")
    if decision not in IDENTITY_DECISIONS:
        raise AgentOutputError(
            "malformed-output",
            f"{label} 'identity' must set 'decision' to one of "
            f"{', '.join(IDENTITY_DECISIONS)}; got "
            f"{_inline(str(decision), limit=assurance_models.MAX_TOKEN_CHARS)!r}",
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
        issue_id = assurance_models.parse_issue_id(declared)
    except assurance_models.AssuranceError as error:
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
    if len(entries) > assurance_models.MAX_FINDINGS:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer must hold at most {assurance_models.MAX_FINDINGS} "
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
                assurance_models.parse_finding(
                    {
                        **{
                            str(key): value
                            for key, value in entry.items()
                            if key != "identity"
                        },
                        "schema_version": assurance_models.ASSURANCE_SCHEMA_VERSION,
                    },
                    label=label,
                )
            )
        except assurance_models.AssuranceError as error:
            raise AgentOutputError("malformed-output", str(error)) from error
    return tuple(findings), tuple(identities)


def _validate_agent_references(
    lanes: Sequence[assurance_models.LaneEvidence],
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
            f"{audit_checks._names(unknown)}",
        )
    outside = sorted(
        {
            ref
            for finding in findings
            for ref in finding.localization
            if not any(audit_checks._inside(ref, root) for root in LOCALIZATION_ROOTS)
        }
    )
    if outside:
        raise AgentOutputError(
            "invalid-reference",
            f"the agent localized outside the read-only source roots "
            f"{', '.join(LOCALIZATION_ROOTS)}: {audit_checks._names(outside)}",
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
                audit_checks._names(candidate.issue_id for candidate in self.candidates)
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
    return max(statuses, key=lambda status: audit_checks._STATUS_PRECEDENCE[status])


def _status_counts(results: Sequence[assurance_models.DeterministicResult]) -> str:
    if not results:
        return "no deterministic check reported"
    counts = {status: 0 for status in assurance_models.CHECK_STATUSES}
    for result in results:
        counts[result.status] += 1
    return ", ".join(f"{count} {status}" for status, count in counts.items() if count)


def _anchor(declared: Sequence[str]) -> tuple[str, ...]:
    if audit_checks.RUN_ANCHOR in declared:
        return (audit_checks.RUN_ANCHOR,)
    return (declared[0],) if declared else ()


def _deterministic_signal(
    results: Sequence[assurance_models.DeterministicResult],
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
    results: Sequence[assurance_models.DeterministicResult],
    agent: AgentAudit | None,
    *,
    declared: Sequence[str],
) -> tuple[assurance_models.LaneEvidence, ...]:
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
    for name in assurance_models.RUBRIC_LANES:
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
        )[: assurance_models.MAX_REFERENCES_PER_ITEM]
        deterministic_note = f"Deterministic checks: {_status_counts(lane_results)}."
        summary = (
            f"{_inline(claim.summary)} {deterministic_note}"
            if claim
            else f"Agent coverage was unavailable for this lane. {deterministic_note}"
        )
        lanes.append(
            assurance_models.LaneEvidence(
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
    return tuple(merged.values())[: assurance_models.MAX_FINDINGS]


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
    for finding, decision in agent.decisions:
        if decision.issue_id is None or finding.fingerprint not in carried:
            continue
        selections.setdefault(finding.fingerprint, decision.issue_id)
    return selections


def render_supervisor_audit(
    *,
    run_id: str,
    bundle: audit_evidence.AuditInput,
    results: Sequence[assurance_models.DeterministicResult],
    generated_at: str,
    agent: AgentAudit | None = None,
    reason: str | None = None,
) -> assurance_models.SupervisorAudit:
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
        state: assurance_models.AuditState = "unavailable"
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
    return assurance_models.SupervisorAudit(
        run_id=run_id,
        state=state,
        input_digest=bundle.sha256,
        generated_at=generated_at,
        reason=state_reason,
        evidence=evidence,
        lanes=lanes,
        findings=findings,
        deterministic=tuple(results)[: assurance_models.MAX_DETERMINISTIC_RESULTS],
    )


def _markdown_row(*cells: str) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def render_audit_markdown(audit: assurance_models.SupervisorAudit) -> str:
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


@dataclass(frozen=True)
class StagedAuditReports:
    """Both canonical reports, written in full but not yet canonical.

    The reports have to be readable before the ledger record that makes them
    true exists, because the record names them by digest. Staging separates the
    two facts: the bytes are final and hashed here, while the canonical names
    appear only once identity has been validated under the append lock. A
    publication that never gets that far therefore leaves no report for a reader
    to find, which is the difference between an audit that did not publish and a
    summary nobody wrote.
    """

    run_dir: Path
    directory: Path
    #: Each report as its staged path, the canonical name it is promoted to, and
    #: the digest of the bytes that were written.
    entries: tuple[tuple[Path, Path, str], ...]
    durable: bool = True
    #: Canonical names this staging has taken, so a publication that promoted
    #: and then failed can give them back.
    promoted: list[Path] = field(default_factory=list, repr=False, compare=False)

    @property
    def references(self) -> tuple[assurance_models.EvidenceRef, ...]:
        """Evidence naming the canonical paths, not the staged ones."""
        return tuple(
            _artifact_reference(canonical, self.run_dir, digest)
            for _, canonical, digest in self.entries
        )

    def promote(self) -> None:
        """Give both reports their canonical names, or neither of them.

        Each link is refused by the filesystem if the canonical name is already
        taken, which is the same rule :func:`assurance_models.write_new_bytes`
        enforces and for the same reason: two reports under one name would mean
        two audits disagree about one run. A failure part-way unlinks whatever
        was already promoted, so the caller can abort with the run directory
        holding no half-published audit.
        """
        for staged, canonical, _ in self.entries:
            try:
                os.link(staged, canonical)
            except OSError as error:
                self.unpromote()
                raise assurance_models.AssuranceError(
                    f"could not promote audit report {canonical}: {error}"
                ) from error
            self.promoted.append(canonical)
        if not self.durable:
            return
        try:
            descriptor = os.open(self.run_dir, os.O_RDONLY)
            try:
                durability.fullsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            self.unpromote()
            raise assurance_models.AssuranceError(
                f"could not sync audit report directory {self.run_dir}: {error}"
            ) from error

    def unpromote(self) -> None:
        """Release the canonical names again.

        Promotion happens before the ledger record is durable, so an append that
        fails afterwards would otherwise leave reports no summary names and a
        canonical name a later attempt could never take. Giving the names back
        keeps a transient failure retryable instead of permanent.
        """
        for path in self.promoted:
            with contextlib.suppress(OSError):
                path.unlink()
        self.promoted.clear()

    def discard(self) -> None:
        """Remove the staging directory, promoted or not.

        Best effort: the promoted reports are separate directory entries for the
        same data, so losing the staging copies costs nothing, and failing to
        remove them must not fail a publication that already succeeded.
        """
        shutil.rmtree(self.directory, ignore_errors=True)


def stage_audit_reports(
    run_dir: Path | str,
    audit: assurance_models.SupervisorAudit,
    *,
    durable: bool = True,
) -> StagedAuditReports:
    """Write both canonical reports into this run's staging directory.

    A staging directory left behind by an earlier attempt is cleared rather than
    refused. Nothing durable ever names a path inside it, so its contents are
    unreferenced scratch, and treating them as state to protect would make one
    crashed publication permanently unable to publish this run.
    """
    root = Path(run_dir)
    directory = root / AUDIT_STAGING_DIR_NAME
    shutil.rmtree(directory, ignore_errors=True)
    try:
        directory.mkdir(parents=True)
    except OSError as error:
        raise assurance_models.AssuranceError(
            f"could not create audit staging directory {directory}: {error}"
        ) from error
    entries: list[tuple[Path, Path, str]] = []
    for name, data in (
        (
            assurance_models.AUDIT_REPORT_FILENAME,
            assurance_models.canonical_json_bytes(audit.to_dict()),
        ),
        (
            assurance_models.AUDIT_MARKDOWN_FILENAME,
            render_audit_markdown(audit).encode("utf-8"),
        ),
    ):
        staged = assurance_models.write_audit_artifact(
            directory, name, data, durable=durable
        )
        entries.append(
            (
                staged,
                root / assurance_models.audit_artifact_name(name),
                artifacts.sha256_hex(data),
            )
        )
    return StagedAuditReports(
        run_dir=root,
        directory=directory,
        entries=tuple(entries),
        durable=durable,
    )


def write_audit_reports(
    run_dir: Path | str,
    audit: assurance_models.SupervisorAudit,
    *,
    durable: bool = True,
) -> tuple[Path, Path]:
    """Write both canonical reports, each exactly once."""
    staged = stage_audit_reports(run_dir, audit, durable=durable)
    try:
        staged.promote()
    finally:
        staged.discard()
    (_, report, _), (_, markdown, _) = staged.entries
    return report, markdown


def _artifact_reference(
    path: Path, run_dir: Path, digest: str
) -> assurance_models.EvidenceRef:
    return assurance_models.EvidenceRef(
        kind="artifact",
        locator=f"{audit_evidence.EVIDENCE_ROOT_RUN}/{path.relative_to(run_dir).as_posix()}",
        digest=digest,
    )
