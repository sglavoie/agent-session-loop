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
    assurance_models_artifacts,
    assurance_models_audit,
    assurance_models_findings,
    assurance_models_primitives,
    assurance_models_vocab,
    assurance_projection,
    durability,
    supervisor_audit_models,
    supervisor_audit_vocabulary,
)
from . import supervisor_audit_checks as audit_checks
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_models as audit_evidence_models
from .assurance_models_findings import Finding
from .assurance_models_vocab import AttemptOutcome, CheckStatus

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
    audit_evidence_constants.EVIDENCE_ROOT_PACKAGE,
    audit_evidence_constants.EVIDENCE_ROOT_SKILLS,
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

    lanes: tuple[assurance_models_findings.LaneEvidence, ...]
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


def _inline(text: str, *, limit: int = assurance_models_vocab.MAX_TEXT_CHARS) -> str:
    """Collapse validated text to one line so it can be rendered into a cell."""
    collapsed = " ".join(
        supervisor_audit_vocabulary._CONTROL_CHAR_RE.sub(" ", str(text)).split()
    )
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


# This is deliberately explicit: focused implementation modules import this
# compatibility vocabulary with ``*``, and static analyzers need that surface.
# fmt: off
__all__ = ("contextlib", "json", "os", "re", "shutil", "Iterable", "Mapping", "Sequence", "dataclass", "field", "replace", "Path", "Any", "artifacts", "assurance_ledger", "assurance_models_artifacts", "assurance_models_audit", "assurance_models_findings", "assurance_models_primitives", "assurance_models_vocab", "assurance_projection", "durability", "supervisor_audit_models", "supervisor_audit_vocabulary", "audit_checks", "audit_evidence_constants", "audit_evidence_models", "Finding", "AttemptOutcome", "CheckStatus", "AUDIT_STAGING_DIR_NAME", "AGENT_OUTPUT_SCHEMA_VERSION", "MAX_AGENT_CATALOG_BYTES", "MAX_CATALOG_SUMMARY_CHARS", "IDENTITY_DECISIONS", "MAX_AGENT_OUTPUT_BYTES", "LOCALIZATION_ROOTS", "REASON_AGENT_UNAVAILABLE", "REASON_LANE_COVERAGE", "REASON_LANE_EVIDENCE_ABSENT", "MAX_PROMPTED_CANDIDATES", "_JSON_FENCE_RE", "_AGENT_FINDING_KEYS", "_IDENTITY_KEYS", "_AGENT_IDENTITY_KEYS", "AUDIT_PROMPT_TEMPLATE", "AUDIT_CORRECTION_TEMPLATE", "AUDIT_CANDIDATE_TEMPLATE", "AgentOutputError", "IDENTITY_SOURCES", "IdentityDecision", "AgentAudit", "_inline", "AgentCatalogEntry", "AgentCatalog")
# fmt: on
