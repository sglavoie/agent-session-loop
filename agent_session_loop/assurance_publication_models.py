"""Validate audit publications and describe their persisted results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from . import assurance_projection_vocab, assurance_publication_identity
from .assurance_ledger import (
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_OBSERVED,
    LedgerRecord,
)
from .assurance_models_findings import (
    EvidenceRef,
    Finding,
)
from .assurance_models_primitives import (
    AssuranceError,
    parse_issue_id,
)
from .assurance_models_summary import AssuranceSummary, AuditAttempt, AuditRole
from .assurance_models_vocab import (
    AuditState,
)


@dataclass(frozen=True)
class AuditPublication:
    """One audit's verdict, before any issue ID for it exists.

    This is the per-run :class:`AssuranceSummary` minus its issue accounting.
    The summary has to name the issues the audit opened, but those IDs are
    minted under the ledger lock during the publication itself, so the summary
    cannot be built until the write is already in progress. Everything the
    summary validates is validated when :meth:`to_summary` runs on the
    publication path; only the findings, which the summary never sees, are
    checked here.
    """

    run_id: str
    state: AuditState
    eligible: bool
    findings: tuple[Finding, ...] = ()
    reason: str | None = None
    role: AuditRole | None = None
    attempts: tuple[AuditAttempt, ...] = ()
    artifacts: tuple[EvidenceRef, ...] = ()
    #: Issues this run's own guards already appended to the ledger before the
    #: publication ran. They are named by the summary and observed again by
    #: nobody: the observation that minted them is already history. Syntax is
    #: checked here; that the ledger really allocated them is checked under the
    #: lock, because only the view taken there can answer it.
    linked_issue_ids: tuple[str, ...] = ()
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    #: The issue an audit selected for a finding, keyed by that finding's
    #: fingerprint. A finding absent from this map asked for a new allocation,
    #: which is the only request that can mint an ID. Each selection is a request
    #: revalidated under the append lock, never an identity taken on trust.
    identity_selections: Mapping[str, str] = field(default_factory=dict)
    #: Digest of the identity catalog those selections were decided against, or
    #: ``None`` when no decision was reviewed against one. It is what makes a
    #: stale decision recognizable under the append lock.
    catalog_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self,
            "linked_issue_ids",
            tuple(
                dict.fromkeys(parse_issue_id(item) for item in self.linked_issue_ids)
            ),
        )
        for index, finding in enumerate(self.findings):
            if not isinstance(finding, Finding):
                raise AssuranceError(
                    f"publication finding #{index + 1} must be a Finding; got "
                    f"{type(finding).__name__}"
                )
        seen: set[str] = set()
        for finding in self.findings:
            if finding.fingerprint in seen:
                raise AssuranceError(
                    f"publication repeats the finding identity "
                    f"{finding.category}/{finding.component}/{finding.invariant_key}; "
                    "one audit sees a defect once"
                )
            seen.add(finding.fingerprint)
        if self.state in assurance_projection_vocab.NO_FINDING_STATES and self.findings:
            raise AssuranceError(
                f"an audit in state {self.state!r} opens no issue; got "
                f"{len(self.findings)} finding(s)"
            )
        if self.state == "findings" and not self.findings:
            raise AssuranceError(
                "an audit in state 'findings' must carry the finding(s) it found"
            )
        if self.state == "unavailable" and self.reason is None:
            raise AssuranceError(
                "an unavailable audit must record the reason class its system "
                "issue deduplicates by"
            )
        object.__setattr__(
            self,
            "identity_selections",
            MappingProxyType(
                {
                    fingerprint: parse_issue_id(issue_id)
                    for fingerprint, issue_id in self.identity_selections.items()
                }
            ),
        )
        for fingerprint in self.identity_selections:
            if fingerprint not in seen:
                raise AssuranceError(
                    f"identity selection {fingerprint} names a finding this "
                    "publication does not carry; a decision that cannot be "
                    "attributed to a finding would be applied to whichever "
                    "defect happened to line up"
                )

    @property
    def audit_input_digest(self) -> str | None:
        return assurance_publication_identity.audit_input_digest(self.artifacts)

    @property
    def record_kind(self) -> str:
        return assurance_projection_vocab.AUDIT_KIND_FOR_STATE[self.state]

    def issue_findings(self) -> tuple[Finding, ...]:
        """Return every finding this publication turns into an observation."""
        if self.state != "unavailable":
            return self.findings
        return (self._unavailable_finding(), *self.findings)

    def _unavailable_finding(self) -> Finding:
        evidence = assurance_publication_identity._unavailable_evidence(
            self.artifacts, self.attempts
        )
        if not evidence:
            raise assurance_projection_vocab.ProjectionError(
                assurance_projection_vocab.UNAVAILABLE_EVIDENCE_REQUIRED,
                f"run {self.run_id} recorded an unavailable audit without naming "
                "an artifact or an attempt directory; an issue that points at "
                "nothing cannot be investigated",
            )
        return Finding(
            category=assurance_projection_vocab.SYSTEM_ISSUE_CATEGORY,
            component=assurance_projection_vocab.SYSTEM_ISSUE_COMPONENT,
            invariant_key=f"{assurance_projection_vocab.UNAVAILABLE_INVARIANT_PREFIX}.{self.reason}",
            lane=assurance_projection_vocab.SYSTEM_ISSUE_LANE,
            severity=assurance_projection_vocab.SYSTEM_ISSUE_SEVERITY,
            summary=(
                "The supervisor audit could not be judged; its reason class is "
                f"{self.reason}."
            ),
            recommendation=assurance_projection_vocab.UNAVAILABLE_RECOMMENDATIONS.get(
                self.reason or "",
                assurance_projection_vocab.DEFAULT_UNAVAILABLE_RECOMMENDATION,
            ),
            evidence=evidence,
        )

    def to_summary(
        self,
        *,
        issue_ids: Sequence[str] = (),
        new_issue_count: int = 0,
        recurring_issue_count: int = 0,
    ) -> AssuranceSummary:
        """Return the persisted summary once issue IDs are known."""
        return AssuranceSummary(
            run_id=self.run_id,
            state=self.state,
            eligible=self.eligible,
            reason=self.reason,
            role=self.role,
            attempts=self.attempts,
            artifacts=self.artifacts,
            issue_ids=tuple(issue_ids),
            new_issue_count=new_issue_count,
            recurring_issue_count=recurring_issue_count,
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_seconds=self.duration_seconds,
        )


@dataclass(frozen=True)
class PublicationResult:
    """What a publication attempt produced, whether or not it wrote anything."""

    summary: AssuranceSummary
    records: tuple[LedgerRecord, ...] = ()
    created: bool = True
    #: Whether identity had moved between the decision and the append lock. The
    #: publication is unaffected — every route is answered from the locked view
    #: either way — and this only reports that the refreshed state was the one
    #: that decided.
    identity_refreshed: bool = False

    @property
    def issue_ids(self) -> tuple[str, ...]:
        return self.summary.issue_ids

    @property
    def observations(self) -> tuple[LedgerRecord, ...]:
        return tuple(
            record for record in self.records if record.kind == KIND_ISSUE_OBSERVED
        )

    @property
    def bindings(self) -> tuple[LedgerRecord, ...]:
        return tuple(
            record
            for record in self.records
            if record.kind == KIND_ISSUE_IDENTITY_BOUND
        )
