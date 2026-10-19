"""Immutable assurance ledger records, issue identities, views, and index data."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .artifacts import sha256_hex
from .assurance_ledger_vocabulary import (
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_MERGED,
    KIND_ISSUE_OBSERVED,
    LEDGER_INDEX_VERSION,
    LEDGER_SCHEMA_VERSION,
    LedgerKind,
)
from .assurance_models_inbox import (
    Closeout,
    Disposition,
    ImprovementPlanDraft,
    IssueIdentityBinding,
    IssueMerge,
)
from .assurance_models_primitives import (
    issue_id_number,
    parse_identity_key,
    parse_issue_id,
)
from .assurance_models_summary import AssuranceSummary

# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerRecord:
    """One validated ledger line.

    ``seq`` is the line's 1-based position in the file rather than a stored
    field: it has to describe rejected lines too, and a line that failed to
    parse cannot be trusted to number itself.
    """

    seq: int
    kind: LedgerKind
    recorded_at: str
    payload: Any
    issue_id: str | None = None
    version: int = LEDGER_SCHEMA_VERSION


@dataclass(frozen=True)
class RejectedLedgerRecord:
    """One physical ledger line that failed validation and was kept anyway."""

    seq: int
    reason: str
    raw: str
    detail: str | None = None

    @property
    def line_number(self) -> int:
        """Compatibility name for other repository JSONL rejection records."""
        return self.seq

    def to_payload(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "raw": self.raw,
            "reason": self.reason,
            "seq": self.seq,
        }


@dataclass(frozen=True)
class LedgerView:
    """An immutable snapshot of the whole ledger, valid lines and bad ones."""

    path: Path
    records: tuple[LedgerRecord, ...] = ()
    rejected: tuple[RejectedLedgerRecord, ...] = ()

    @property
    def last_seq(self) -> int:
        """The highest physical line number present, rejected lines included."""
        return max(
            (
                *(record.seq for record in self.records),
                *(rejected.seq for rejected in self.rejected),
            ),
            default=0,
        )

    def records_of_kind(self, kind: str) -> tuple[LedgerRecord, ...]:
        return tuple(record for record in self.records if record.kind == kind)

    @property
    def issue_ids_by_fingerprint(self) -> dict[str, str]:
        """Map each observed fingerprint to the ID its first sighting minted."""
        mapping: dict[str, str] = {}
        for record in self.records_of_kind(KIND_ISSUE_OBSERVED):
            fingerprint = record.payload.fingerprint
            if fingerprint not in mapping and record.issue_id is not None:
                mapping[fingerprint] = record.issue_id
        return mapping

    def issue_id_for_fingerprint(self, fingerprint: str) -> str | None:
        return self.issue_ids_by_fingerprint.get(fingerprint)

    @property
    def allocated_issue_ids(self) -> frozenset[str]:
        """Every ``ASI-N`` an observation actually minted."""
        return frozenset(
            record.issue_id
            for record in self.records_of_kind(KIND_ISSUE_OBSERVED)
            if record.issue_id is not None
        )

    @property
    def merge_edges(self) -> dict[str, str]:
        """Source-to-target redirections, the first merge of a source winning.

        Merge is irreversible, so a later record naming an already-merged source
        is history to report rather than history that moves the issue again.
        """
        edges: dict[str, str] = {}
        for record in self.records_of_kind(KIND_ISSUE_MERGED):
            payload = record.payload
            edges.setdefault(payload.source_issue_id, payload.target_issue_id)
        return edges

    def canonical_issue_id(self, issue_id: str) -> str:
        """Follow the merge chain to the issue that now owns this history.

        The walk is cycle-safe because the ledger cannot be edited: a cyclic
        pair of merge records is permanent, and a reader that looped on it could
        never read the inbox again.
        """
        return follow_merge_chain(self.merge_edges, parse_issue_id(issue_id))

    def canonical_issue_id_for_fingerprint(self, fingerprint: str) -> str | None:
        issue_id = self.issue_id_for_fingerprint(fingerprint)
        return None if issue_id is None else self.canonical_issue_id(issue_id)

    @property
    def identity_bindings(self) -> dict[str, str]:
        """Map each bound identity key to the issue it was bound to.

        The first binding of a key wins. A key is a durable name rather than a
        mutable pointer, so a later record naming an already-bound key is history
        to report rather than history that renames the issue.
        """
        bindings: dict[str, str] = {}
        for record in self.records_of_kind(KIND_ISSUE_IDENTITY_BOUND):
            bindings.setdefault(record.payload.identity_key, record.payload.issue_id)
        return bindings

    def issue_id_for_identity_key(self, identity_key: str) -> str | None:
        """Return the issue a bound key names today, or ``None`` when unbound.

        The answer follows the merge chain: binding a key is not a promise that
        its issue never merges, only that the key keeps pointing at whoever owns
        that history.
        """
        issue_id = self.identity_bindings.get(parse_identity_key(identity_key))
        return None if issue_id is None else self.canonical_issue_id(issue_id)

    @property
    def next_issue_number(self) -> int:
        """The next ``ASI-N`` number, chosen so no allocated ID can recur.

        The maximum is taken over every issue reference in the ledger, not only
        over the observations that minted one. A disposition or closeout can
        only name an ID that was allocated, so counting them costs nothing and
        keeps the allocator from reusing an ID whose own observation line was
        lost to corruption.
        """
        numbers = [
            issue_id_number(issue_id)
            for record in self.records
            for issue_id in _issue_ids_in(record)
        ]
        return max(numbers, default=0) + 1


def follow_merge_chain(edges: Mapping[str, str], issue_id: str) -> str:
    """Return the last issue reachable from ``issue_id`` through ``edges``."""
    current = issue_id
    seen = {current}
    while True:
        target = edges.get(current)
        if target is None or target in seen:
            return current
        seen.add(target)
        current = target


def _issue_ids_in(record: LedgerRecord) -> Iterator[str]:
    if record.issue_id is not None:
        yield record.issue_id
    payload = record.payload
    if isinstance(payload, (Disposition, ImprovementPlanDraft)):
        yield payload.issue_id
    elif isinstance(payload, IssueMerge):
        yield payload.source_issue_id
        yield payload.target_issue_id
    elif isinstance(payload, IssueIdentityBinding):
        yield payload.issue_id
        # The key names an issue too, so counting it keeps the allocator from
        # minting an ID a stored key already claims.
        yield payload.named_issue_id
    elif isinstance(payload, (Closeout, AssuranceSummary)):
        yield from payload.issue_ids


# ---------------------------------------------------------------------------
# Index cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerIndex:
    """A derived summary of the ledger, safe to delete at any moment."""

    path: Path
    ledger_bytes: int
    ledger_sha256: str
    last_seq: int
    record_count: int
    rejected_count: int
    next_issue_number: int
    rebuilt_at: str
    issue_ids: Mapping[str, str] = field(default_factory=dict)
    version: int = LEDGER_INDEX_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_ids", MappingProxyType(dict(self.issue_ids)))

    def matches(self, data: bytes) -> bool:
        """Return whether this cache still describes ``data`` exactly."""
        return (
            self.version == LEDGER_INDEX_VERSION
            and self.ledger_bytes == len(data)
            and self.ledger_sha256 == sha256_hex(data)
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ledger_bytes": self.ledger_bytes,
            "ledger_sha256": self.ledger_sha256,
            "last_seq": self.last_seq,
            "record_count": self.record_count,
            "rejected_count": self.rejected_count,
            "next_issue_number": self.next_issue_number,
            "issue_ids": dict(self.issue_ids),
            "rebuilt_at": self.rebuilt_at,
        }


@dataclass(frozen=True)
class PendingRecord:
    """One record a transaction builder asks the ledger to append."""

    kind: str
    payload: Any
    issue_id: str | None = None
    recorded_at: str | None = None
