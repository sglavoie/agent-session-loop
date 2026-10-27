"""Preview and apply reviewed repairs to the assurance inbox's identity state.

Two things go wrong with identity over a ledger's life. Issues minted before
identity keys existed have no durable name, and two issues sometimes describe
one defect because the second sighting was worded differently enough to miss the
fingerprint. Neither can be fixed by editing history: the ledger is append-only,
and the point of an identity key is that it keeps meaning what it meant.

So repair is a two-step protocol with a person in the middle. The preview reads
the inbox and writes nothing: it reports the bindings that are safe to add, the
pairs the catalog thinks might be one defect, and the digest of the identity
state it read all that from. A reviewer turns each suggestion into an explicit
``merge`` or ``keep-distinct`` and hands the manifest back. Apply replays only
what the manifest says, as one transaction, against the catalog the reviewer saw.

Nothing here decides a merge. Every candidate reason the catalog offers is
semantic -- normalized summaries, invariant keys, file paths -- so a rule that
acted on one would let rewording a finding merge two unrelated defects. A
suggestion that still reads ``review`` when apply runs is refused, not guessed at.

Both verdicts are recorded. ``keep-distinct`` writes a ruling of its own, because
a review nobody stored is a review the next preview asks for again; the ruling
carries the identity digest of each issue it was reached about, so it suppresses
the pair while those issues still read that way and lapses when one of them does
not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_projection as projection
from .assurance_ledger import (
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_KEPT_DISTINCT,
    KIND_ISSUE_MERGED,
    LedgerRecord,
    LedgerView,
    PendingRecord,
    follow_merge_chain,
)
from .assurance_models_inbox import IssueIdentityBinding, IssueKeptDistinct, IssueMerge
from .assurance_models_primitives import (
    identity_key_for_issue,
    issue_id_for_identity_key,
    issue_id_number,
)
from .assurance_models_reconcile import (
    DECISION_KEEP_DISTINCT,
    DECISION_MERGE,
    DECISION_REVIEW,
    BindDecision,
    KeepDistinctDecision,
    MergeDecision,
    ReconciliationManifest,
    ReviewDecision,
)
from .assurance_projection import (
    UNKNOWN_ISSUE,
    AssuranceProjection,
    IdentityCandidate,
    IdentityCatalog,
    ProjectionError,
    candidate_order,
    issue_pair_key,
)

#: The manifest still carries a suggestion nobody ruled on. Applying it would
#: have to guess which way the reviewer would have gone.
UNRESOLVED_DECISION = "unresolved-decision"

#: The identity state moved after the manifest was reviewed, so at least one
#: decision describes an inbox that no longer exists.
STALE_CATALOG_DIGEST = "stale-catalog-digest"

#: The manifest binds a key that already names a different issue. Rebinding is
#: refused rather than performed: a key that can be repointed is not a name.
IDENTITY_KEY_CONFLICT = ledger.ERROR_IDENTITY_KEY_CONFLICT

#: The manifest binds a key to an issue that has since merged away.
ISSUE_NOT_CANONICAL = ledger.ERROR_ISSUE_NOT_CANONICAL

#: Suggested pairs one preview reports. A reviewer handed more pairs than this
#: is being asked to redo the matching rather than to confirm it.
MAX_PREVIEW_PAIRS = 200

#: Unresolved pairs named in the refusal when a manifest is applied unreviewed.
#: Enough to recognize which review was skipped, short enough to read.
MAX_NAMED_UNRESOLVED = 5


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class BlockedBinding:
    """One issue the preview cannot offer a safe binding for, and why."""

    issue_id: str
    identity_key: str
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "identity_key": self.identity_key,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReconciliationPreview:
    """What one reconciliation pass would have to decide, with nothing decided.

    The preview carries a whole manifest rather than describing one, so the
    reviewer's job is to rewrite the ``review`` entries of a file that already
    validates rather than to author one against a schema.
    """

    catalog_digest: str
    generated_at: str
    issue_count: int = 0
    bound_count: int = 0
    bindings: tuple[BindDecision, ...] = ()
    blocked: tuple[BlockedBinding, ...] = ()
    unresolved: tuple[ReviewDecision, ...] = ()
    truncated_pairs: int = 0
    #: Pairs the catalog suggested that a standing ruling already answered.
    #: Reported rather than hidden, so a reviewer can see the difference between
    #: a quiet inbox and matching that stopped working.
    suppressed_pairs: int = 0

    @property
    def manifest(self) -> ReconciliationManifest:
        """The manifest a reviewer edits: every safe binding, every open pair."""
        return ReconciliationManifest(
            catalog_digest=self.catalog_digest,
            decisions=(*self.bindings, *self.unresolved),
            generated_at=self.generated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_digest": self.catalog_digest,
            "generated_at": self.generated_at,
            "issue_count": self.issue_count,
            "bound_count": self.bound_count,
            "unbound_count": len(self.bindings) + len(self.blocked),
            "bindings": [item.to_dict() for item in self.bindings],
            "blocked": [item.to_dict() for item in self.blocked],
            "unresolved": [item.to_dict() for item in self.unresolved],
            "truncated_pairs": self.truncated_pairs,
            "suppressed_pairs": self.suppressed_pairs,
            "manifest": self.manifest.to_dict(),
        }


def _safe_bindings(
    inbox: AssuranceProjection,
) -> tuple[tuple[BindDecision, ...], tuple[BlockedBinding, ...]]:
    """Propose each canonical issue its own key, and report the ones already taken.

    Only an issue's minted ``issue.asi-N`` is ever proposed. Any other key would
    be a name chosen for the issue rather than derived from it, which is the one
    thing identity keys exist to rule out, and no automatic rule can say which of
    two issues should inherit a key that names a third.
    """
    bindings: list[BindDecision] = []
    blocked: list[BlockedBinding] = []
    for issue in inbox.issues:
        key = identity_key_for_issue(issue.issue_id)
        owner = inbox.identity_bindings.get(key)
        if owner is None:
            bindings.append(BindDecision(issue_id=issue.issue_id, identity_key=key))
        elif inbox.resolve(owner) != issue.issue_id:
            blocked.append(
                BlockedBinding(
                    issue_id=issue.issue_id,
                    identity_key=key,
                    reason=IDENTITY_KEY_CONFLICT,
                    detail=(
                        f"{key} already names {inbox.resolve(owner)}, so "
                        f"{issue.issue_id} has no key it can be given without "
                        "renaming another issue"
                    ),
                )
            )
    return tuple(bindings), tuple(blocked)


def _suggested_pairs(
    inbox: AssuranceProjection, *, limit: int = MAX_PREVIEW_PAIRS
) -> tuple[tuple[ReviewDecision, ...], int, int]:
    """Return the pairs the catalog thinks might be one defect, strongest first.

    Each pair is asked about once. The catalog is symmetric for most reasons --
    issue A lists B and B lists A -- and offering both would make a reviewer rule
    on one question twice, with the two orderings looking like two decisions.

    A pair a standing ruling already answers is counted and dropped. The catalog
    goes on matching it, because the reasons that matched it are still true; what
    changed is that someone read them and said no.
    """
    catalog = inbox.catalog
    strongest: dict[tuple[str, str], IdentityCandidate] = {}
    for issue in inbox.issues:
        for candidate in catalog.candidates(issue.finding):
            if candidate.issue_id == issue.issue_id:
                continue
            pair = issue_pair_key(issue.issue_id, candidate.issue_id)
            # Both directions are recorded under the higher-numbered ID so the
            # two sightings of one pair can be folded together at all.
            named = IdentityCandidate(
                issue_id=pair[1],
                reasons=candidate.reasons,
                similarity=candidate.similarity,
            )
            previous = strongest.get(pair)
            strongest[pair] = named if previous is None else previous.merged(named)
    settled = {pair for pair in strongest if inbox.is_kept_distinct(*pair)}
    ordered = sorted(
        (
            (pair, candidate)
            for pair, candidate in strongest.items()
            if pair not in settled
        ),
        key=lambda item: (candidate_order(item[1]), issue_id_number(item[0][0])),
    )
    return (
        tuple(
            ReviewDecision(
                issue_ids=pair,
                reasons=candidate.reasons,
                similarity=candidate.similarity,
            )
            for pair, candidate in ordered[:limit]
        ),
        max(len(ordered) - limit, 0),
        len(settled),
    )


def build_preview(
    inbox: AssuranceProjection,
    *,
    generated_at: str | None = None,
    limit: int = MAX_PREVIEW_PAIRS,
) -> ReconciliationPreview:
    """Describe what reconciling ``inbox`` would have to decide, writing nothing."""
    bindings, blocked = _safe_bindings(inbox)
    unresolved, truncated, suppressed = _suggested_pairs(inbox, limit=limit)
    return ReconciliationPreview(
        catalog_digest=inbox.catalog.digest,
        generated_at=generated_at or _utc_now(),
        issue_count=len(inbox.issues),
        bound_count=sum(1 for issue in inbox.issues if issue.identity_keys),
        bindings=bindings,
        blocked=blocked,
        unresolved=unresolved,
        truncated_pairs=truncated,
        suppressed_pairs=suppressed,
    )


@dataclass(frozen=True)
class ApplyResult:
    """What one apply wrote, or the fact that it had nothing to write."""

    records: tuple[LedgerRecord, ...] = ()
    bindings_written: int = 0
    merges_written: int = 0
    #: Rulings written, not decisions read: a keep-distinct the ledger already
    #: carries is counted nowhere, the same way an existing binding is.
    kept_distinct: int = 0

    @property
    def applied(self) -> bool:
        return bool(self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "records": len(self.records),
            "bindings": self.bindings_written,
            "merges": self.merges_written,
            "kept_distinct": self.kept_distinct,
        }


def _recorded_rulings(
    inbox: AssuranceProjection,
) -> dict[frozenset[str], Mapping[str, str]]:
    """Map each ruled set of issues to the digests its latest ruling stored.

    Keyed by set because a ruling is a statement about issues rather than an
    ordered edge, and the reviewer who names the same pair the other way round
    is repeating a decision rather than making a new one.

    Only rulings the projection accepted are read. The ledger stores a record
    that names an issue nothing had observed yet, and the projection reports it
    as an anomaly and suppresses nothing with it; treating that same record as a
    standing ruling here would make apply skip a fresh verdict the preview still
    asks for, and the pair would come back on every preview with no way to
    settle it.
    """
    rulings: dict[frozenset[str], Mapping[str, str]] = {}
    for payload in inbox.rulings:
        rulings[frozenset(payload.issue_ids)] = dict(payload.reviewed_digests)
    return rulings


def _ruling_is_recorded(
    decision: KeepDistinctDecision,
    *,
    rulings: Mapping[frozenset[str], Mapping[str, str]],
    catalog: IdentityCatalog,
) -> bool:
    """Whether the ledger already rules on these issues as they read right now.

    A stored ruling whose digests have moved is not this decision: the issues
    were reviewed again because they changed, and the fresh verdict has to land
    or the pair keeps coming back. An issue the catalog no longer names has
    merged away and has no current digest to compare, so its ruling stands as
    written rather than being rewritten on every apply.
    """
    stored = rulings.get(frozenset(decision.issue_ids))
    if stored is None:
        return False
    return all(
        catalog.identity_digest(issue_id) in (digest, None)
        for issue_id, digest in stored.items()
    )


def _pending(
    view: LedgerView, inbox: AssuranceProjection, manifest: ReconciliationManifest
) -> tuple[
    tuple[BindDecision, ...],
    tuple[MergeDecision, ...],
    tuple[KeepDistinctDecision, ...],
]:
    """Return the decisions the ledger does not already record.

    Pure and non-raising, because it runs before validation: a manifest that was
    already applied is answered from what is on disk rather than re-checked
    against a catalog its own application moved.
    """
    bindings = view.identity_bindings
    edges = view.merge_edges
    rulings = _recorded_rulings(inbox)
    binds = tuple(
        item
        for item in manifest.decisions
        if isinstance(item, BindDecision)
        and bindings.get(item.identity_key) != item.issue_id
    )
    merges = tuple(
        item
        for item in manifest.decisions
        if isinstance(item, MergeDecision)
        and edges.get(item.source_issue_id) != item.target_issue_id
    )
    keeps = tuple(
        item
        for item in manifest.decisions
        if isinstance(item, KeepDistinctDecision)
        and not _ruling_is_recorded(item, rulings=rulings, catalog=inbox.catalog)
    )
    return binds, merges, keeps


def _bind_rejection(
    decision: BindDecision,
    *,
    known: frozenset[str] | set[str],
    edges: Mapping[str, str],
    bindings: Mapping[str, str],
) -> ProjectionError | None:
    """Return the error refusing this binding, or ``None`` when it can be written.

    These are the rules
    :func:`agent_session_loop.assurance_ledger.append_identity_binding` enforces,
    restated against maps a caller advances, so several bindings in one
    transaction are checked against each other rather than each against the
    ledger they all started from.
    """
    if decision.issue_id not in known:
        return ProjectionError(
            UNKNOWN_ISSUE, f"{decision.issue_id} names no issue in the inbox"
        )
    named = issue_id_for_identity_key(decision.identity_key)
    if named not in known:
        return ProjectionError(
            UNKNOWN_ISSUE,
            f"{decision.identity_key} names {named}, which no observation minted",
        )
    canonical = follow_merge_chain(edges, decision.issue_id)
    if canonical != decision.issue_id:
        return ProjectionError(
            ISSUE_NOT_CANONICAL,
            f"{decision.issue_id} merged into {canonical}, which owns its history "
            "now; bind the key to that issue instead",
        )
    bound = bindings.get(decision.identity_key)
    if bound is not None:
        return ProjectionError(
            IDENTITY_KEY_CONFLICT,
            f"{decision.identity_key} already names {bound} and cannot also name "
            f"{decision.issue_id}",
        )
    return None


def _keep_distinct_rejection(
    decision: KeepDistinctDecision,
    *,
    known: frozenset[str] | set[str],
    edges: Mapping[str, str],
) -> ProjectionError | None:
    """Return the error refusing this ruling, or ``None`` when it can be written.

    A ruling changes no issue, so the only ways it can be wrong are naming an
    issue nobody observed and naming one that has since merged: the pair a
    reviewer ruled two defects became one defect before the ruling landed, and
    storing it would suppress a question the merge already answered differently.
    """
    for issue_id in decision.issue_ids:
        if issue_id not in known:
            return ProjectionError(
                UNKNOWN_ISSUE, f"{issue_id} names no issue in the inbox"
            )
        canonical = follow_merge_chain(edges, issue_id)
        if canonical != issue_id:
            return ProjectionError(
                ISSUE_NOT_CANONICAL,
                f"{issue_id} merged into {canonical}, so the pair reviewed as "
                "distinct is one issue now",
            )
    return None


def _reviewed_digests(
    decision: KeepDistinctDecision,
    *,
    catalog: IdentityCatalog,
    edges: Mapping[str, str],
) -> tuple[str, ...]:
    """The digest each named issue is recognized by, in the order it was named.

    ``catalog`` and ``edges`` describe the inbox once this transaction's merges
    land, because that is the inbox the ruling is next compared against: a
    digest taken before a merge that absorbs into one of the ruled issues would
    lapse the ruling the moment it was written.
    """
    digests: list[str] = []
    for issue_id in decision.issue_ids:
        digest = catalog.identity_digest(follow_merge_chain(edges, issue_id))
        if digest is None:
            raise ProjectionError(
                UNKNOWN_ISSUE,
                f"{issue_id} has no catalog identity for a ruling to hold against",
            )
        digests.append(digest)
    return tuple(digests)


def apply_manifest(
    manifest: ReconciliationManifest,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> ApplyResult:
    """Apply one reviewed manifest as a single append, or write nothing at all.

    Three questions are asked in this order, and the order is the design.

    First, what is left to do. A manifest whose bindings, rulings and merges are
    all already in the ledger writes nothing and succeeds, so re-running an apply
    after a crash, a retry, or an operator who lost track is a no-op rather than
    a second attempt at history that already happened.

    Second, whether the review still holds. The catalog digest is compared only
    once there is something to write, because a successful apply moves the very
    digest the manifest carries: checking staleness first would make every
    manifest refuse its own second run and idempotence impossible. The cost is
    that a manifest applied halfway by other means is refused as stale rather
    than completed, which is the safe direction to fail in.

    Third, whether the decisions are legal against the ledger as it stands. Each
    decision is judged against maps that advance as the transaction is built, so
    a manifest cannot pass by having every decision checked against a state its
    siblings already changed.

    Bindings are written before merges, and rulings between them. Both kinds
    name issues that must still be canonical when their record replays, and every
    decision in one manifest was reviewed against the pre-merge inbox -- so
    binding ``issue.asi-123`` to ASI-123 and then merging ASI-123 into ASI-80
    leaves the older name resolving to the surviving issue, which is what the
    merge chain is for.
    """
    unresolved = manifest.unresolved
    if unresolved:
        named = ", ".join(
            "+".join(item.issue_ids) for item in unresolved[:MAX_NAMED_UNRESOLVED]
        )
        raise ProjectionError(
            UNRESOLVED_DECISION,
            f"{len(unresolved)} suggestion(s) still read {DECISION_REVIEW!r} "
            f"({named}); rule each one {DECISION_MERGE!r} or "
            f"{DECISION_KEEP_DISTINCT!r} before applying",
        )
    stamp = recorded_at or _utc_now()
    counts = {"bindings": 0, "merges": 0, "kept_distinct": 0}

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        inbox = projection.project(view)
        catalog = inbox.catalog
        binds, merges, keeps = _pending(view, inbox, manifest)
        if not binds and not merges and not keeps:
            return ()
        digest = catalog.digest
        if digest != manifest.catalog_digest:
            raise ProjectionError(
                STALE_CATALOG_DIGEST,
                f"the manifest was reviewed against catalog {manifest.catalog_digest} "
                f"and the inbox now reads {digest}; regenerate the preview and "
                "review the decisions again",
            )
        known = view.allocated_issue_ids
        edges = dict(view.merge_edges)
        bindings = dict(view.identity_bindings)
        records: list[PendingRecord] = []
        rulings: list[PendingRecord] = []
        for decision in binds:
            refusal = _bind_rejection(
                decision, known=known, edges=edges, bindings=bindings
            )
            if refusal is not None:
                raise refusal
            bindings[decision.identity_key] = decision.issue_id
            records.append(
                PendingRecord(
                    kind=KIND_ISSUE_IDENTITY_BOUND,
                    payload=IssueIdentityBinding(
                        identity_key=decision.identity_key,
                        issue_id=decision.issue_id,
                        source="reconcile",
                        bound_at=stamp,
                        note=decision.note,
                    ),
                    recorded_at=stamp,
                )
            )
        for keep in keeps:
            refused = _keep_distinct_rejection(keep, known=known, edges=edges)
            if refused is not None:
                raise refused
        merged: list[PendingRecord] = []
        for merge in merges:
            rejected = projection.merge_rejection(
                source=merge.source_issue_id,
                target=merge.target_issue_id,
                known=known,
                edges=edges,
            )
            if rejected is not None:
                raise rejected
            edges[merge.source_issue_id] = merge.target_issue_id
            merged.append(
                PendingRecord(
                    kind=KIND_ISSUE_MERGED,
                    payload=IssueMerge(
                        source_issue_id=merge.source_issue_id,
                        target_issue_id=merge.target_issue_id,
                        recorded_at=stamp,
                        note=merge.note,
                    ),
                    recorded_at=stamp,
                )
            )
        if keeps and merged:
            # Rulings are digested against the inbox this append leaves behind,
            # so the merges are replayed onto the view first; the rulings still
            # land ahead of the merges, against the issues they were reviewed as.
            catalog = projection.project(
                LedgerView(
                    path=view.path,
                    records=(
                        *view.records,
                        *(
                            LedgerRecord(
                                seq=view.last_seq + offset,
                                kind=KIND_ISSUE_MERGED,
                                recorded_at=stamp,
                                payload=item.payload,
                            )
                            for offset, item in enumerate(merged, start=1)
                        ),
                    ),
                    rejected=view.rejected,
                )
            ).catalog
        for keep in keeps:
            rulings.append(
                PendingRecord(
                    kind=KIND_ISSUE_KEPT_DISTINCT,
                    payload=IssueKeptDistinct(
                        issue_ids=keep.issue_ids,
                        identity_digests=_reviewed_digests(
                            keep, catalog=catalog, edges=edges
                        ),
                        recorded_at=stamp,
                        note=keep.note,
                    ),
                    recorded_at=stamp,
                )
            )
        counts["bindings"] = len(binds)
        counts["merges"] = len(merges)
        counts["kept_distinct"] = len(keeps)
        return (*records, *rulings, *merged)

    records = ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)
    return ApplyResult(
        records=records,
        bindings_written=counts["bindings"],
        merges_written=counts["merges"],
        kept_distinct=counts["kept_distinct"],
    )
