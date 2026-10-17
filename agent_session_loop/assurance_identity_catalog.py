"""Index assurance issue identities and rank recurrence candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .artifacts import sha256_hex
from .assurance_models_findings import (
    Finding,
    normalize_invariant_tokens,
    normalize_localization,
    normalize_summary_tokens,
)
from .assurance_models_primitives import (
    AssuranceError,
    canonical_json_bytes,
    issue_id_number,
    parse_identity_key,
    parse_issue_id,
)
from .assurance_models_vocab import (
    IssueState,
)

if TYPE_CHECKING:
    from . import assurance_projection_models

# ---------------------------------------------------------------------------
# Identity catalog
# ---------------------------------------------------------------------------

#: Version of the material :attr:`IdentityCatalog.digest` is taken over. It is
#: part of that material, so changing which fields the digest covers changes
#: every digest rather than silently making two different states collide.
CATALOG_DIGEST_VERSION = 1


#: Least normalized-summary overlap that makes one issue a recurrence candidate
#: for a finding. Two sightings of one defect are usually reworded rather than
#: rewritten, so a majority of shared content words is the signal; below this the
#: pair shares vocabulary rather than a subject.
MIN_SUMMARY_SIMILARITY = 0.60


#: Most candidates one finding is offered. A list long enough to read is worth
#: more than a complete one: a reader that has to weigh thirty near-matches is
#: being asked to redo the matching rather than to confirm it.
MAX_IDENTITY_CANDIDATES = 8


#: An identical invariant key under a different category or component. This is
#: the rename the fingerprint cannot see, since the fingerprint is all three.
MATCH_INVARIANT = "invariant"


#: The same source file, plus an overlapping invariant-key segment. This is the
#: rename the exact-invariant index cannot see. A file alone matches every
#: defect ASL ever localized to `runner.py`, and the segment overlap is what
#: separates a rewritten key from an unrelated defect in the same module.
MATCH_LOCALIZATION = "localization"


#: Normalized-summary overlap of at least :data:`MIN_SUMMARY_SIMILARITY`.
MATCH_SUMMARY = "summary"


@dataclass(frozen=True)
class IdentityCandidate:
    """One issue that could already own a finding, and why the catalog thinks so.

    A candidate is a question, never an answer. Every reason here is semantic —
    text an agent wrote, normalized — so acting on one automatically would let
    rewording a summary merge two unrelated defects. The exact paths, fingerprint
    and identity key, are deliberately not candidate reasons: they are matches,
    and a caller reuses them without asking anyone.
    """

    issue_id: str
    reasons: tuple[str, ...]
    #: Normalized-summary Jaccard overlap, or ``0.0`` when the summaries are not
    #: what matched. It is reported rather than thresholded away because two
    #: candidates that matched the same way are still not equally close.
    similarity: float = 0.0

    def merged(self, other: IdentityCandidate) -> IdentityCandidate:
        """Return one candidate for the same issue matched by two findings."""
        if other.issue_id != self.issue_id:
            raise AssuranceError(
                f"cannot merge candidates for {self.issue_id} and {other.issue_id}"
            )
        return IdentityCandidate(
            issue_id=self.issue_id,
            reasons=tuple(sorted(set(self.reasons) | set(other.reasons))),
            similarity=max(self.similarity, other.similarity),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "reasons": list(self.reasons),
            "similarity": round(self.similarity, 2),
        }


def candidate_order(candidate: IdentityCandidate) -> tuple[int, float, int]:
    """Rank the most corroborated candidate first, ties settled by issue number."""
    return (
        -len(candidate.reasons),
        -candidate.similarity,
        issue_id_number(candidate.issue_id),
    )


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    first, second = set(left), set(right)
    union = first | second
    return 0.0 if not union else len(first & second) / len(union)


@dataclass(frozen=True)
class CatalogEntry:
    """One canonical issue, in the shape a defect is recognized against.

    The entry carries the finding fields a recurrence can be recognized by and
    their normalized forms beside them, so a reader compares against the same
    text the index was built from rather than normalizing again and possibly
    differently.
    """

    issue_id: str
    state: IssueState
    severity: str
    category: str
    component: str
    invariant_key: str
    lane: str
    summary: str
    fingerprints: tuple[str, ...]
    identity_keys: tuple[str, ...]
    primary_identity_key: str | None
    localization: tuple[str, ...]
    summary_tokens: tuple[str, ...]
    invariant_tokens: tuple[str, ...]
    occurrence_count: int
    first_seen_at: str
    last_seen_at: str

    @property
    def fingerprint(self) -> str:
        """The identity this issue was opened under."""
        return self.fingerprints[0]


def _catalog_digest(entries: Sequence[CatalogEntry]) -> str:
    """Return the digest of the identity state ``entries`` describe.

    Only the fields that decide which issue owns a defect participate. An
    occurrence count and a last-seen timestamp change every time a known defect
    is seen again, and folding them in would expire a reviewed decision for the
    one event that proves the decision was right.
    """
    return sha256_hex(
        canonical_json_bytes(
            {
                "kind": "assurance-identity-catalog",
                "version": CATALOG_DIGEST_VERSION,
                "entries": [
                    {
                        "issue_id": entry.issue_id,
                        "state": entry.state,
                        "fingerprints": list(entry.fingerprints),
                        "identity_keys": list(entry.identity_keys),
                        "primary_identity_key": entry.primary_identity_key,
                    }
                    for entry in entries
                ],
            }
        )
    )


#: Version of the material :func:`issue_identity_digest` is taken over. It is
#: part of that material, so changing which fields participate changes every
#: digest rather than silently making two different issues collide.
ISSUE_IDENTITY_DIGEST_VERSION = 1


def issue_identity_digest(entry: CatalogEntry) -> str:
    """Return the digest of everything one issue is recognized as a defect by.

    This is the material a reviewer weighs when ruling on whether two issues are
    one defect: the fingerprints the issue has been seen under, and the exact and
    normalized text every candidate reason is drawn from.

    What is excluded is what a ruling cannot be invalidated by. Identity keys and
    lifecycle state are not here even though
    :func:`_catalog_digest` covers them, because naming an issue or deciding what
    to do about it says nothing about whether it and another issue describe the
    same defect; occurrence counts and timestamps are absent for the reason they
    are absent there. Folding any of them in would expire a reviewed ruling for
    events that cannot have changed it.
    """
    return sha256_hex(
        canonical_json_bytes(
            {
                "kind": "assurance-issue-identity",
                "version": ISSUE_IDENTITY_DIGEST_VERSION,
                "issue_id": entry.issue_id,
                "category": entry.category,
                "component": entry.component,
                "invariant_key": entry.invariant_key,
                "lane": entry.lane,
                "fingerprints": list(entry.fingerprints),
                "localization": list(entry.localization),
                "summary_tokens": list(entry.summary_tokens),
                "invariant_tokens": list(entry.invariant_tokens),
            }
        )
    )


@dataclass(frozen=True)
class IdentityCatalog:
    """Every canonical issue, indexed by the ways one defect is recognized.

    The catalog covers every lifecycle state rather than the open backlog: a
    defect that recurs after being resolved, dismissed, or accepted has to route
    back to the issue that already owns it, and an index of open issues alone
    would mint a second issue for exactly the recurrences the inbox exists to
    collapse.

    Lookups are exact and never resolve a merge chain. A merged-away ID names no
    catalog entry, which is what lets a caller fail closed on a stale reference
    instead of silently redirecting one.
    """

    entries: tuple[CatalogEntry, ...] = ()
    #: SHA-256 of the identity state these entries describe. Occurrence counts
    #: and timestamps are excluded on purpose: a decision reviewed against this
    #: catalog stays valid while a known defect merely recurs, and is invalidated
    #: only when an issue, a fingerprint, a key, or a lifecycle state moves.
    digest: str = field(default="", init=False, compare=False)
    by_issue_id: Mapping[str, CatalogEntry] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_fingerprint: Mapping[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_identity_key: Mapping[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_invariant: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_localization: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_summary_token: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        by_issue_id: dict[str, CatalogEntry] = {}
        by_fingerprint: dict[str, str] = {}
        by_identity_key: dict[str, str] = {}
        by_invariant: dict[str, list[str]] = {}
        by_localization: dict[str, list[str]] = {}
        by_summary_token: dict[str, list[str]] = {}
        for entry in self.entries:
            by_issue_id[entry.issue_id] = entry
            for fingerprint in entry.fingerprints:
                by_fingerprint.setdefault(fingerprint, entry.issue_id)
            for identity_key in entry.identity_keys:
                by_identity_key.setdefault(identity_key, entry.issue_id)
            by_invariant.setdefault(entry.invariant_key, []).append(entry.issue_id)
            for pointer in entry.localization:
                by_localization.setdefault(pointer, []).append(entry.issue_id)
            for token in entry.summary_tokens:
                by_summary_token.setdefault(token, []).append(entry.issue_id)
        object.__setattr__(self, "digest", _catalog_digest(self.entries))
        object.__setattr__(self, "by_issue_id", MappingProxyType(by_issue_id))
        object.__setattr__(self, "by_fingerprint", MappingProxyType(by_fingerprint))
        object.__setattr__(self, "by_identity_key", MappingProxyType(by_identity_key))
        for name, index in (
            ("by_invariant", by_invariant),
            ("by_localization", by_localization),
            ("by_summary_token", by_summary_token),
        ):
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    {key: tuple(issue_ids) for key, issue_ids in index.items()}
                ),
            )

    def entry(self, issue_id: str) -> CatalogEntry | None:
        return self.by_issue_id.get(parse_issue_id(issue_id))

    def identity_digest(self, issue_id: str) -> str | None:
        """Return one issue's recognition digest, or ``None`` when it names none."""
        entry = self.entry(issue_id)
        return None if entry is None else issue_identity_digest(entry)

    def issue_for_fingerprint(self, fingerprint: str) -> str | None:
        return self.by_fingerprint.get(fingerprint)

    def issue_for_identity_key(self, identity_key: str) -> str | None:
        return self.by_identity_key.get(parse_identity_key(identity_key))

    def issues_with_invariant(self, invariant_key: str) -> tuple[str, ...]:
        return self.by_invariant.get(invariant_key, ())

    def issues_localized_at(self, pointer: str) -> tuple[str, ...]:
        """Return every issue localized to the file ``pointer`` names."""
        return self.by_localization.get(normalize_localization(pointer), ())

    def issues_sharing_summary_tokens(self, tokens: Sequence[str]) -> tuple[str, ...]:
        """Return every issue whose summary shares a token, in catalog order.

        The inverted index answers which issues are worth comparing at all;
        scoring the overlap is the caller's, because how much overlap counts as
        one defect is a matching policy rather than a fact about the inbox.
        """
        matched: dict[str, None] = {}
        for token in tokens:
            for issue_id in self.by_summary_token.get(token, ()):
                matched[issue_id] = None
        return tuple(sorted(matched, key=issue_id_number))

    def candidates(
        self, finding: Finding, *, limit: int = MAX_IDENTITY_CANDIDATES
    ) -> tuple[IdentityCandidate, ...]:
        """Return the issues that could already own ``finding``, strongest first.

        Every issue considered arrives from an index, so the cost is the number
        of issues that share an invariant, a file, or a word with this finding
        rather than the size of the inbox. Scanning every pair would put the
        whole catalog in the publication path of every audit.

        The issue an exact fingerprint already names is excluded: that is a
        recurrence a caller reuses outright, and listing it as something to
        deliberate over would invite a reader to decline it.
        """
        recurrence = self.issue_for_fingerprint(finding.fingerprint)
        reasons: dict[str, set[str]] = {}
        similarity: dict[str, float] = {}

        def note(issue_id: str, reason: str) -> None:
            if issue_id != recurrence:
                reasons.setdefault(issue_id, set()).add(reason)

        for issue_id in self.issues_with_invariant(finding.invariant_key):
            note(issue_id, MATCH_INVARIANT)

        invariant_tokens = set(normalize_invariant_tokens(finding.invariant_key))
        localized: set[str] = set()
        for pointer in finding.localization:
            for issue_id in self.issues_localized_at(pointer):
                if issue_id in localized:
                    continue
                localized.add(issue_id)
                if invariant_tokens & set(self.by_issue_id[issue_id].invariant_tokens):
                    note(issue_id, MATCH_LOCALIZATION)

        summary_tokens = normalize_summary_tokens(finding.summary)
        for issue_id in self.issues_sharing_summary_tokens(summary_tokens):
            score = _jaccard(summary_tokens, self.by_issue_id[issue_id].summary_tokens)
            if score >= MIN_SUMMARY_SIMILARITY:
                note(issue_id, MATCH_SUMMARY)
                similarity[issue_id] = score

        candidates = sorted(
            (
                IdentityCandidate(
                    issue_id=issue_id,
                    reasons=tuple(sorted(found)),
                    similarity=similarity.get(issue_id, 0.0),
                )
                for issue_id, found in reasons.items()
            ),
            key=candidate_order,
        )
        return tuple(candidates[:limit])


def _catalog_entry(issue: assurance_projection_models.Issue) -> CatalogEntry:
    finding = issue.finding
    localization: list[str] = []
    for pointer in finding.localization:
        normalized = normalize_localization(pointer)
        if normalized not in localization:
            localization.append(normalized)
    return CatalogEntry(
        issue_id=issue.issue_id,
        state=issue.state,
        severity=finding.severity,
        category=finding.category,
        component=finding.component,
        invariant_key=finding.invariant_key,
        lane=finding.lane,
        summary=finding.summary,
        fingerprints=issue.fingerprints,
        identity_keys=issue.identity_keys,
        primary_identity_key=issue.primary_identity_key,
        localization=tuple(localization),
        summary_tokens=normalize_summary_tokens(finding.summary),
        invariant_tokens=normalize_invariant_tokens(finding.invariant_key),
        occurrence_count=issue.occurrence_count,
        first_seen_at=issue.first_seen_at,
        last_seen_at=issue.last_seen_at,
    )


def build_catalog(
    inbox: assurance_projection_models.AssuranceProjection,
) -> IdentityCatalog:
    """Return the identity catalog of one projection, every state included."""
    return IdentityCatalog(
        entries=tuple(_catalog_entry(issue) for issue in inbox.issues)
    )
