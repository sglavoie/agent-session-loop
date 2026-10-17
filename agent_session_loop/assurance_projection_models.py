"""Immutable issue histories, run audits, and the projected assurance inbox."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from . import assurance_identity_catalog
from .assurance_ledger import (
    RejectedLedgerRecord,
    follow_merge_chain,
)
from .assurance_models_findings import (
    Finding,
)
from .assurance_models_inbox import (
    Closeout,
    Disposition,
    ImprovementPlanDraft,
    IssueKeptDistinct,
)
from .assurance_models_primitives import (
    identity_key_for_issue,
    issue_id_number,
    parse_identity_key,
    parse_issue_id,
)
from .assurance_models_summary import AssuranceSummary
from .assurance_models_vocab import (
    AuditState,
    IssueState,
)

# ---------------------------------------------------------------------------
# Projected state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueOccurrence:
    """One sighting of an issue, kept in the order the ledger recorded it."""

    seq: int
    run_id: str
    observed_at: str
    fingerprint: str
    severity: str
    audit_artifact: str | None = None


@dataclass(frozen=True)
class Issue:
    """One inbox issue as the ledger currently describes it."""

    issue_id: str
    state: IssueState
    finding: Finding
    occurrences: tuple[IssueOccurrence, ...]
    fingerprints: tuple[str, ...]
    dispositions: tuple[Disposition, ...] = ()
    plans: tuple[ImprovementPlanDraft, ...] = ()
    merged_from: tuple[str, ...] = ()
    reopened_count: int = 0
    #: Every bound key that resolves here, lowest-numbered first. A merge moves
    #: the absorbed issue's key onto the issue that owns its history, so this
    #: holds more than one key exactly when this issue absorbed a named one.
    identity_keys: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        """The identity this issue was opened under."""
        return self.fingerprints[0]

    @property
    def primary_identity_key(self) -> str | None:
        """The one key this issue should be named by, or ``None`` when unbound.

        An issue's own key wins whenever it is bound, because that is the name
        minted with the issue itself; an issue named only by keys it absorbed
        falls back to the lowest-numbered of them. Binding is first-write-wins
        per key, which settles who owns a key but not which of several keys an
        issue prefers, so the tie-break is decided here and never by write
        order.
        """
        if not self.identity_keys:
            return None
        own = identity_key_for_issue(self.issue_id)
        return own if own in self.identity_keys else self.identity_keys[0]

    @property
    def occurrence_count(self) -> int:
        return len(self.occurrences)

    @property
    def first_seen_at(self) -> str:
        return self.occurrences[0].observed_at

    @property
    def last_seen_at(self) -> str:
        return self.occurrences[-1].observed_at

    @property
    def run_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for occurrence in self.occurrences:
            seen[occurrence.run_id] = None
        return tuple(seen)

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True)
class RunAudit:
    """Everything the ledger records about one run's audit."""

    run_id: str
    state: AuditState
    summary: AssuranceSummary
    issue_ids: tuple[str, ...] = ()
    summaries: tuple[AssuranceSummary, ...] = ()
    closeouts: tuple[Closeout, ...] = ()

    @property
    def closeout(self) -> Closeout | None:
        """The closeout in force, later records replacing earlier ones."""
        return self.closeouts[-1] if self.closeouts else None


@dataclass(frozen=True)
class ProjectionAnomaly:
    """A stored record that could not mean anything and was therefore skipped."""

    seq: int
    kind: str
    reason: str
    detail: str


def issue_pair_key(left: str, right: str) -> tuple[str, str]:
    """Order two issue IDs by number, so both directions name one pair."""
    return (
        (left, right)
        if issue_id_number(left) <= issue_id_number(right)
        else (right, left)
    )


@dataclass(frozen=True)
class AssuranceProjection:
    """The whole inbox, derived from ledger bytes and nothing else."""

    issues: tuple[Issue, ...] = ()
    runs: tuple[RunAudit, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)
    anomalies: tuple[ProjectionAnomaly, ...] = ()
    rejected: tuple[RejectedLedgerRecord, ...] = ()
    #: Every bound identity key mapped to the issue that owns its history now,
    #: merges already followed.
    identity_bindings: Mapping[str, str] = field(default_factory=dict)
    #: Every reviewed keep-distinct pair, named by the issues that own those
    #: histories now, mapped to the identity digest each one was reviewed at.
    kept_distinct: Mapping[tuple[str, str], Mapping[str, str]] = field(
        default_factory=dict
    )
    #: Every keep-distinct ruling the replay accepted, as it was written and in
    #: ledger order. A caller asking whether a pair has already been ruled on has
    #: to read these rather than the stored records: a record replay rejected
    #: means nothing, and reading it as a standing ruling would let it answer for
    #: a review nobody made.
    rulings: tuple[IssueKeptDistinct, ...] = ()
    _catalog: assurance_identity_catalog.IdentityCatalog | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))
        object.__setattr__(
            self,
            "identity_bindings",
            MappingProxyType(dict(self.identity_bindings)),
        )
        object.__setattr__(
            self,
            "kept_distinct",
            MappingProxyType(
                {
                    pair: MappingProxyType(dict(digests))
                    for pair, digests in self.kept_distinct.items()
                }
            ),
        )

    def resolve(self, issue_id: str) -> str:
        """Return the issue that owns ``issue_id``'s history today."""
        return follow_merge_chain(self.aliases, parse_issue_id(issue_id))

    def issue(self, issue_id: str) -> Issue | None:
        """Return the issue behind any reference, merged-away IDs included."""
        canonical = self.resolve(issue_id)
        for item in self.issues:
            if item.issue_id == canonical:
                return item
        return None

    def issue_for_identity_key(self, identity_key: str) -> Issue | None:
        """Return the issue a bound key names today, or ``None`` when unbound."""
        issue_id = self.identity_bindings.get(parse_identity_key(identity_key))
        return None if issue_id is None else self.issue(issue_id)

    def run(self, run_id: str) -> RunAudit | None:
        for item in self.runs:
            if item.run_id == run_id:
                return item
        return None

    def is_kept_distinct(self, left: str, right: str) -> bool:
        """Whether a reviewed ruling still says these two are separate defects.

        The ruling was reached about the issues as they read then, so it holds
        only while both still read that way. A digest that has moved means this
        pair, in the shape it has now, is a question nobody has answered — which
        is why a stale ruling lapses into another review rather than suppressing
        the suggestion forever.
        """
        reviewed = self.kept_distinct.get(
            issue_pair_key(self.resolve(left), self.resolve(right))
        )
        if reviewed is None:
            return False
        catalog = self.catalog
        return all(
            catalog.identity_digest(issue_id) == digest
            for issue_id, digest in reviewed.items()
        )

    @property
    def open_issues(self) -> tuple[Issue, ...]:
        return tuple(item for item in self.issues if item.is_open)

    @property
    def catalog(self) -> assurance_identity_catalog.IdentityCatalog:
        """The one identity catalog for this projection, built on first use.

        Built once and kept, because a projection is immutable: two catalogs of
        one projection could only ever be equal, and callers that recognize a
        defect against the catalog do so per finding rather than per audit.
        """
        catalog = self._catalog
        if catalog is None:
            catalog = assurance_identity_catalog.build_catalog(self)
            object.__setattr__(self, "_catalog", catalog)
        return catalog
