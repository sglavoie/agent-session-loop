"""The assurance reconciliation manifest and its decision models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .assurance_models_primitives import (
    AssuranceError,
    _duplicates,
    _optional_text,
    _require_digest,
    _require_int,
    _require_issue_ids,
    _require_number,
    _require_object,
    _require_sequence,
    _require_text,
    _require_timestamp,
    _require_token,
    parse_identity_key,
    parse_issue_id,
)
from .assurance_models_vocab import MAX_REFERENCES_PER_ITEM
from .literals import as_literal

# ---------------------------------------------------------------------------
# Reconciliation manifest
# ---------------------------------------------------------------------------

#: A reconciliation manifest is an operator artifact, not a ledger record: a
#: preview generates it, a human edits it, and one apply replays it. Its version
#: is therefore its own and moves independently of the ledger schema version.
RECONCILIATION_MANIFEST_KIND = "assurance-reconciliation-manifest"
RECONCILIATION_MANIFEST_VERSION = 1
SUPPORTED_RECONCILIATION_MANIFEST_VERSIONS: frozenset[int] = frozenset({1})

#: Give one canonical issue a durable identity key it does not have yet.
DECISION_BIND = "bind"
#: Redirect one issue's history into another, irreversibly.
DECISION_MERGE = "merge"
#: Record that a suggested pair was read and is two defects. Writes nothing:
#: the ledger has no record for "not the same", and inventing one to store a
#: negative would put a growing table of non-facts in every replay.
DECISION_KEEP_DISTINCT = "keep-distinct"
#: A pair the catalog suggested and nobody has ruled on yet. It is a member of
#: the schema so a preview can be applied without being rewritten first — and
#: apply refuses any manifest still carrying one, so the refusal names the
#: unread suggestion instead of a syntax error somewhere in the file.
DECISION_REVIEW = "review"

RECONCILIATION_DECISIONS: tuple[str, ...] = (
    DECISION_BIND,
    DECISION_MERGE,
    DECISION_KEEP_DISTINCT,
    DECISION_REVIEW,
)

#: Decisions one manifest may carry. A reconciliation pass is something a person
#: reads before running it, and a file past this size is being generated into
#: rather than reviewed.
MAX_MANIFEST_DECISIONS = 1000


@dataclass(frozen=True)
class BindDecision:
    """Bind one identity key to the canonical issue that owns it."""

    issue_id: str
    identity_key: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        object.__setattr__(self, "identity_key", parse_identity_key(self.identity_key))
        if self.note is not None:
            _require_text(self.note, label="bind decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_BIND

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_BIND,
            "issue_id": self.issue_id,
            "identity_key": self.identity_key,
            "note": self.note,
        }


@dataclass(frozen=True)
class MergeDecision:
    """Redirect one issue's history into another."""

    source_issue_id: str
    target_issue_id: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_issue_id", parse_issue_id(self.source_issue_id)
        )
        object.__setattr__(
            self, "target_issue_id", parse_issue_id(self.target_issue_id)
        )
        if self.source_issue_id == self.target_issue_id:
            raise AssuranceError(
                f"merge decision cannot merge {self.source_issue_id} into itself"
            )
        if self.note is not None:
            _require_text(self.note, label="merge decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_MERGE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_MERGE,
            "source_issue_id": self.source_issue_id,
            "target_issue_id": self.target_issue_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class KeepDistinctDecision:
    """Record that a suggested pair was read and left as separate defects."""

    issue_ids: tuple[str, ...]
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", _require_issue_ids(list(self.issue_ids), label="issues")
        )
        if len(self.issue_ids) < 2:
            raise AssuranceError(
                "keep-distinct decision must name at least two issues; "
                f"got {len(self.issue_ids)}"
            )
        if self.note is not None:
            _require_text(self.note, label="keep-distinct decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_KEEP_DISTINCT

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_KEEP_DISTINCT,
            "issue_ids": list(self.issue_ids),
            "note": self.note,
        }


@dataclass(frozen=True)
class ReviewDecision:
    """A pair the catalog suggested, carrying why, awaiting a human verdict."""

    issue_ids: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    similarity: float = 0.0
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", _require_issue_ids(list(self.issue_ids), label="issues")
        )
        if len(self.issue_ids) < 2:
            raise AssuranceError(
                "review decision must name at least two issues; "
                f"got {len(self.issue_ids)}"
            )
        object.__setattr__(
            self,
            "reasons",
            tuple(
                _require_token(reason, label="review decision 'reasons' entry")
                for reason in self.reasons
            ),
        )
        similarity = _require_number(
            self.similarity, label="review decision 'similarity'", minimum=0.0
        )
        if similarity > 1.0:
            raise AssuranceError(
                f"review decision 'similarity' must be <= 1.0; got {similarity}"
            )
        object.__setattr__(self, "similarity", similarity)
        if self.note is not None:
            _require_text(self.note, label="review decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_REVIEW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_REVIEW,
            "issue_ids": list(self.issue_ids),
            "reasons": list(self.reasons),
            "similarity": round(self.similarity, 2),
            "note": self.note,
        }


ReconciliationDecision = (
    BindDecision | MergeDecision | KeepDistinctDecision | ReviewDecision
)


@dataclass(frozen=True)
class ReconciliationManifest:
    """Every decision one reconciliation pass applies, and what it was read against.

    ``catalog_digest`` is the identity state the decisions were reviewed against.
    It is part of the manifest rather than a flag on the command because the
    review is what the digest qualifies: a file that outlived the catalog it was
    written from describes issues that may since have moved, and applying it
    would record a verdict nobody gave about the inbox as it stands.
    """

    catalog_digest: str
    decisions: tuple[ReconciliationDecision, ...] = ()
    generated_at: str | None = None

    def __post_init__(self) -> None:
        _require_digest(self.catalog_digest, label="manifest 'catalog_digest'")
        decisions = tuple(self.decisions)
        if len(decisions) > MAX_MANIFEST_DECISIONS:
            raise AssuranceError(
                f"manifest 'decisions' must hold at most {MAX_MANIFEST_DECISIONS} "
                f"entries; got {len(decisions)}"
            )
        object.__setattr__(self, "decisions", decisions)
        if self.generated_at is not None:
            _require_timestamp(self.generated_at, label="manifest 'generated_at'")
        bound_keys = _duplicates(
            item.identity_key for item in decisions if isinstance(item, BindDecision)
        )
        if bound_keys:
            raise AssuranceError(
                f"manifest binds identity key(s) {', '.join(bound_keys)} more than "
                "once; one key names one issue"
            )
        merged = _duplicates(
            item.source_issue_id
            for item in decisions
            if isinstance(item, MergeDecision)
        )
        if merged:
            raise AssuranceError(
                f"manifest merges issue(s) {', '.join(merged)} more than once; a "
                "merge is irreversible and only the first would take effect"
            )

    @property
    def unresolved(self) -> tuple[ReviewDecision, ...]:
        """Suggestions still awaiting a verdict, which apply refuses to act on."""
        return tuple(
            item for item in self.decisions if isinstance(item, ReviewDecision)
        )

    def of_kind(self, decision: str) -> tuple[ReconciliationDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == decision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": RECONCILIATION_MANIFEST_KIND,
            "version": RECONCILIATION_MANIFEST_VERSION,
            "catalog_digest": self.catalog_digest,
            "generated_at": self.generated_at,
            "decisions": [item.to_dict() for item in self.decisions],
        }


def _parse_decision(payload: object, *, label: str) -> ReconciliationDecision:
    if not isinstance(payload, Mapping):
        raise AssuranceError(f"{label} must be a JSON object")
    kind = as_literal(
        payload.get("decision"),
        RECONCILIATION_DECISIONS,
        f"{label} 'decision'",
        error=AssuranceError,
    )
    if kind == DECISION_BIND:
        data = _require_object(
            payload,
            label=label,
            allowed_keys=("decision", "issue_id", "identity_key", "note"),
        )
        return BindDecision(
            issue_id=parse_issue_id(data.get("issue_id")),
            identity_key=parse_identity_key(data.get("identity_key")),
            note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        )
    if kind == DECISION_MERGE:
        data = _require_object(
            payload,
            label=label,
            allowed_keys=("decision", "source_issue_id", "target_issue_id", "note"),
        )
        return MergeDecision(
            source_issue_id=parse_issue_id(data.get("source_issue_id")),
            target_issue_id=parse_issue_id(data.get("target_issue_id")),
            note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        )
    if kind == DECISION_KEEP_DISTINCT:
        data = _require_object(
            payload, label=label, allowed_keys=("decision", "issue_ids", "note")
        )
        return KeepDistinctDecision(
            issue_ids=_require_issue_ids(
                data.get("issue_ids"), label=f"{label} 'issue_ids'"
            ),
            note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        )
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("decision", "issue_ids", "reasons", "similarity", "note"),
    )
    reasons = _require_sequence(
        data.get("reasons", []),
        label=f"{label} 'reasons'",
        maximum=MAX_REFERENCES_PER_ITEM,
    )
    return ReviewDecision(
        issue_ids=_require_issue_ids(
            data.get("issue_ids"), label=f"{label} 'issue_ids'"
        ),
        reasons=tuple(
            _require_token(reason, label=f"{label} 'reasons' entry")
            for reason in reasons
        ),
        similarity=_require_number(
            data.get("similarity", 0.0), label=f"{label} 'similarity'", minimum=0.0
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


def parse_reconciliation_manifest(
    payload: object, *, label: str = "manifest"
) -> ReconciliationManifest:
    """Parse one reconciliation manifest, failing closed.

    The ``kind`` and ``version`` fields are checked before anything else, so a
    file that is some other JSON document is refused as the wrong document
    rather than as a manifest missing every field it needs.
    """
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("kind", "version", "catalog_digest", "generated_at", "decisions"),
    )
    kind = _require_text(data.get("kind"), label=f"{label} 'kind'")
    if kind != RECONCILIATION_MANIFEST_KIND:
        raise AssuranceError(
            f"{label} 'kind' must be {RECONCILIATION_MANIFEST_KIND!r}; got {kind!r}"
        )
    version = _require_int(
        data.get("version"), label=f"{label} 'version'", minimum=1, maximum=1_000_000
    )
    if version not in SUPPORTED_RECONCILIATION_MANIFEST_VERSIONS:
        supported = ", ".join(
            str(item) for item in sorted(SUPPORTED_RECONCILIATION_MANIFEST_VERSIONS)
        )
        raise AssuranceError(
            f"{label} 'version' {version} is unsupported; this build reads "
            f"version(s) {supported}"
        )
    decisions = _require_sequence(
        data.get("decisions", []),
        label=f"{label} 'decisions'",
        maximum=MAX_MANIFEST_DECISIONS,
    )
    return ReconciliationManifest(
        catalog_digest=_require_digest(
            data.get("catalog_digest"), label=f"{label} 'catalog_digest'"
        ),
        decisions=tuple(
            _parse_decision(item, label=f"{label} decision {position}")
            for position, item in enumerate(decisions, start=1)
        ),
        generated_at=(
            None
            if data.get("generated_at") is None
            else _require_timestamp(
                data.get("generated_at"), label=f"{label} 'generated_at'"
            )
        ),
    )
