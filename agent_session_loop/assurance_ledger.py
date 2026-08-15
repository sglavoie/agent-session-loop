"""Append-only global storage for the assurance inbox and its issue IDs.

The inbox outlives every run that writes to it, so its storage is designed
around what a crash, a race, or a lost cache may do to it:

* **Nothing is ever rewritten.** Records are appended under an exclusive
  ``flock`` and flushed through a platform durability barrier before the lock is
  released. Dispositions, merges, and closeouts are new lines, never edits, so
  the ledger is the complete history rather than the current opinion.
* **Every physical line has a sequence, including the bad ones.** Sequences are
  positional, not stored, because a malformed or crash-truncated line cannot
  carry a field. That is what lets a rejected line keep its place in history
  instead of being deleted to make the file parse.
* **An interrupted append is repaired, not truncated.** A final line missing its
  newline is terminated by the next append; the prior bytes are never cut back,
  and the incomplete line survives as rejected history.
* **Issue IDs are allocated under the append lock.** ``ASI-N`` is chosen and
  written inside the same critical section, so two processes cannot mint the
  same ID and no ID is ever reused.
* **Identity keys are bound once and never rebound.** A binding says which
  allocated issue an allocated key names; it is decided against the whole ledger
  inside the append lock, and a key that already names an issue is refused
  rather than repointed. Records written before schema v2 knew no keys, so a v1
  line claiming the binding kind is rejected instead of being read as one.
* **The index is a cache with no authority.** ``assurance-index.json`` is
  rebuilt whenever it is missing, stale, or malformed, and every answer that
  matters is derived from the ledger bytes. Deleting the index changes nothing
  but speed.
* **Only bounded metadata is stored.** Each record kind is parsed through the
  matching :mod:`agent_session_loop.assurance_models` model, and every record is
  additionally capped in bytes, so prompts, logs, diffs, and harness responses
  cannot reach the ledger even by accident. The ledger holds references to those
  artifacts, not their contents.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from . import durability, state
from .artifacts import sha256_hex
from .assurance_models import (
    COMPLETED_AUDIT_STATES,
    ISSUE_ID_PREFIX,
    AssuranceError,
    AssuranceSummary,
    Closeout,
    Disposition,
    ImprovementPlanDraft,
    IssueIdentityBinding,
    IssueMerge,
    IssueObservation,
    canonical_json_bytes,
    issue_id_number,
    parse_assurance_summary,
    parse_closeout,
    parse_disposition,
    parse_identity_key,
    parse_improvement_plan_draft,
    parse_issue_id,
    parse_issue_identity_binding,
    parse_issue_merge,
    parse_issue_observation,
)

#: Canonical ledger, its lock, and its rebuildable cache, all directly under the
#: state root because the inbox spans every plan rather than belonging to one.
LEDGER_FILE_NAME = "assurance-inbox.jsonl"
LEDGER_LOCK_FILE_NAME = "assurance-inbox.lock"
LEDGER_INDEX_FILE_NAME = "assurance-index.json"

#: Bumped when the meaning of the record envelope changes. Records written by a
#: newer build are rejected rather than guessed at. v2 adds the identity-binding
#: kind and nothing else, so every v1 line means in v2 exactly what it meant when
#: it was written and the whole prior history reads without reinterpretation.
LEDGER_SCHEMA_VERSION = 2
LEDGER_LEGACY_SCHEMA_VERSION = 1
SUPPORTED_LEDGER_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2})

#: The cache format is versioned separately: it may change freely because a
#: version this build does not recognize simply triggers a rebuild.
LEDGER_INDEX_VERSION = 1

#: A single record is bounded well above any legitimate payload and far below a
#: captured log. The typed parsers already exclude raw content; this is the
#: structural backstop that holds even if a model later grows a text field.
MAX_LEDGER_RECORD_BYTES = 256 * 1024

#: How much of a rejected line is retained for diagnostics. The line itself
#: stays on disk untouched; only the in-memory view is truncated.
MAX_REJECTED_RAW_CHARS = 2000


LedgerKind = Literal[
    "audit_pending",
    "audit_completed",
    "audit_skipped",
    "audit_unavailable",
    "issue_observed",
    "issue_disposition",
    "issue_merged",
    "human_closeout",
    "improvement_plan_drafted",
    "issue_identity_bound",
]

KIND_AUDIT_PENDING = "audit_pending"
KIND_AUDIT_COMPLETED = "audit_completed"
KIND_AUDIT_SKIPPED = "audit_skipped"
KIND_AUDIT_UNAVAILABLE = "audit_unavailable"
KIND_ISSUE_OBSERVED = "issue_observed"
KIND_ISSUE_DISPOSITION = "issue_disposition"
KIND_ISSUE_MERGED = "issue_merged"
KIND_HUMAN_CLOSEOUT = "human_closeout"
KIND_IMPROVEMENT_PLAN_DRAFTED = "improvement_plan_drafted"
KIND_ISSUE_IDENTITY_BOUND = "issue_identity_bound"

#: Every record kind the ledger stores, in the order they occur in a run's life.
LEDGER_KINDS: tuple[LedgerKind, ...] = (
    KIND_AUDIT_PENDING,
    KIND_AUDIT_COMPLETED,
    KIND_AUDIT_SKIPPED,
    KIND_AUDIT_UNAVAILABLE,
    KIND_ISSUE_OBSERVED,
    KIND_ISSUE_DISPOSITION,
    KIND_ISSUE_MERGED,
    KIND_HUMAN_CLOSEOUT,
    KIND_IMPROVEMENT_PLAN_DRAFTED,
    KIND_ISSUE_IDENTITY_BOUND,
)

#: The kinds a v1 envelope may carry. A v1 line claiming a kind introduced later
#: is rejected as an unknown kind for its version rather than read as if the
#: build that wrote it had meant the new one.
LEGACY_LEDGER_KINDS: frozenset[str] = frozenset(LEDGER_KINDS) - {
    KIND_ISSUE_IDENTITY_BOUND
}

#: The only kind that mints an issue ID. Keeping the set explicit is what makes
#: "allocated under the append lock" a property of the module rather than of a
#: caller remembering to ask.
ISSUE_ALLOCATING_KINDS: frozenset[str] = frozenset({KIND_ISSUE_OBSERVED})

#: Kinds whose validity depends on the ledger as a whole rather than on the
#: record alone, and the entry point that decides them inside the append lock.
#: The generic writer refuses them so that lock-scoped validation is structural.
GUARDED_KINDS: Mapping[str, str] = {
    KIND_ISSUE_OBSERVED: "append_issue_observation",
    KIND_ISSUE_IDENTITY_BOUND: "append_identity_binding",
}

ENVELOPE_KEYS: tuple[str, ...] = (
    "version",
    "kind",
    "recorded_at",
    "issue_id",
    "payload",
)

#: An audit record's kind and its summary's state are two spellings of one fact.
#: Storing them without checking would let the ledger contradict itself.
_AUDIT_KIND_STATES: Mapping[str, frozenset[str]] = {
    KIND_AUDIT_PENDING: frozenset({"pending"}),
    KIND_AUDIT_COMPLETED: COMPLETED_AUDIT_STATES,
    KIND_AUDIT_SKIPPED: frozenset({"skipped"}),
    KIND_AUDIT_UNAVAILABLE: frozenset({"unavailable"}),
}

_PAYLOAD_PARSERS: Mapping[str, Callable[[Any], Any]] = {
    KIND_AUDIT_PENDING: parse_assurance_summary,
    KIND_AUDIT_COMPLETED: parse_assurance_summary,
    KIND_AUDIT_SKIPPED: parse_assurance_summary,
    KIND_AUDIT_UNAVAILABLE: parse_assurance_summary,
    KIND_ISSUE_OBSERVED: parse_issue_observation,
    KIND_ISSUE_DISPOSITION: parse_disposition,
    KIND_ISSUE_MERGED: parse_issue_merge,
    KIND_HUMAN_CLOSEOUT: parse_closeout,
    KIND_IMPROVEMENT_PLAN_DRAFTED: parse_improvement_plan_draft,
    KIND_ISSUE_IDENTITY_BOUND: parse_issue_identity_binding,
}

_PAYLOAD_TYPES: Mapping[str, type[Any]] = {
    KIND_AUDIT_PENDING: AssuranceSummary,
    KIND_AUDIT_COMPLETED: AssuranceSummary,
    KIND_AUDIT_SKIPPED: AssuranceSummary,
    KIND_AUDIT_UNAVAILABLE: AssuranceSummary,
    KIND_ISSUE_OBSERVED: IssueObservation,
    KIND_ISSUE_DISPOSITION: Disposition,
    KIND_ISSUE_MERGED: IssueMerge,
    KIND_HUMAN_CLOSEOUT: Closeout,
    KIND_IMPROVEMENT_PLAN_DRAFTED: ImprovementPlanDraft,
    KIND_ISSUE_IDENTITY_BOUND: IssueIdentityBinding,
}


REJECT_EMPTY_LINE = "empty-line"
REJECT_INVALID_JSON = "invalid-json"
REJECT_NOT_OBJECT = "not-object"
REJECT_UNSUPPORTED_VERSION = "unsupported-version"
REJECT_UNKNOWN_KIND = "unknown-kind"
REJECT_INVALID_RECORD = "invalid-record"

#: Why a physical line could not become a record. Every reason preserves the
#: line; none of them removes it.
REJECTION_REASONS: tuple[str, ...] = (
    REJECT_EMPTY_LINE,
    REJECT_INVALID_JSON,
    REJECT_NOT_OBJECT,
    REJECT_UNSUPPORTED_VERSION,
    REJECT_UNKNOWN_KIND,
    REJECT_INVALID_RECORD,
)

ERROR_LEDGER_READ = "ledger-read-failed"
ERROR_LEDGER_WRITE = "ledger-write-failed"
ERROR_RECORD_TOO_LARGE = "record-too-large"
ERROR_INVALID_KIND = "invalid-kind"
ERROR_INVALID_PAYLOAD = "invalid-payload"
ERROR_UNKNOWN_ISSUE = "unknown-issue"
ERROR_ISSUE_NOT_CANONICAL = "issue-not-canonical"
ERROR_IDENTITY_ALREADY_BOUND = "identity-key-already-bound"
ERROR_IDENTITY_KEY_CONFLICT = "identity-key-conflict"


class LedgerError(AssuranceError):
    """A stable assurance-ledger failure suitable for CLI translation."""

    def __init__(
        self,
        reason: str,
        detail: str | None = None,
        *,
        path: Path | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.path = path
        super().__init__(reason if detail is None else f"{reason}: {detail}")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def ledger_path(state_dir: str | Path | None = None) -> Path:
    """Return the canonical ledger path under the resolved state root."""
    return state.state_root(state_dir) / LEDGER_FILE_NAME


def ledger_lock_path(state_dir: str | Path | None = None) -> Path:
    """Return the sibling lock that serializes every ledger mutation."""
    return state.state_root(state_dir) / LEDGER_LOCK_FILE_NAME


def ledger_index_path(state_dir: str | Path | None = None) -> Path:
    """Return the rebuildable cache path beside the ledger."""
    return state.state_root(state_dir) / LEDGER_INDEX_FILE_NAME


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


def _index_from_view(view: LedgerView, data: bytes, *, path: Path) -> LedgerIndex:
    return LedgerIndex(
        path=path,
        ledger_bytes=len(data),
        ledger_sha256=sha256_hex(data),
        last_seq=view.last_seq,
        record_count=len(view.records),
        rejected_count=len(view.rejected),
        next_issue_number=view.next_issue_number,
        issue_ids=view.issue_ids_by_fingerprint,
        rebuilt_at=_utc_now(),
    )


def _parse_index_payload(payload: object, *, path: Path) -> LedgerIndex | None:
    """Return the cached index, or ``None`` when it cannot be trusted verbatim.

    Nothing here raises: an unreadable cache is indistinguishable from a missing
    one as far as correctness goes, and both are answered by a rebuild.
    """
    if not isinstance(payload, Mapping):
        return None
    if payload.get("version") != LEDGER_INDEX_VERSION:
        return None
    issue_ids = payload.get("issue_ids")
    if not isinstance(issue_ids, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in issue_ids.items()
    ):
        return None
    counters: dict[str, int] = {}
    for name in (
        "ledger_bytes",
        "last_seq",
        "record_count",
        "rejected_count",
        "next_issue_number",
    ):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counters[name] = value
    digest = payload.get("ledger_sha256")
    rebuilt_at = payload.get("rebuilt_at")
    if not isinstance(digest, str) or not isinstance(rebuilt_at, str):
        return None
    return LedgerIndex(
        path=path,
        ledger_sha256=digest,
        rebuilt_at=rebuilt_at,
        issue_ids=dict(issue_ids),
        **counters,
    )


def _read_index_file(path: Path) -> LedgerIndex | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError, UnicodeDecodeError:
        return None
    return _parse_index_payload(payload, path=path)


def _write_index_file(index: LedgerIndex) -> None:
    """Replace the cache atomically, treating any failure as a cache miss.

    The ledger is already durable by the time this runs. A read-only state root
    or a full disk must not turn a recorded truth into an error, so the cache
    write is best effort in every caller.
    """
    tmp_path = index.path.with_name(f".{index.path.name}.tmp")
    try:
        tmp_path.write_bytes(canonical_json_bytes(index.to_payload()))
        os.replace(tmp_path, index.path)
    except OSError:
        with suppress(OSError):
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Locking and raw IO
# ---------------------------------------------------------------------------


@contextmanager
def _ledger_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    lock_path = root / LEDGER_LOCK_FILE_NAME
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise LedgerError(
            ERROR_LEDGER_WRITE if exclusive else ERROR_LEDGER_READ,
            str(error),
            path=lock_path,
        ) from error
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as error:
            raise LedgerError(
                ERROR_LEDGER_WRITE if exclusive else ERROR_LEDGER_READ,
                str(error),
                path=lock_path,
            ) from error
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_ledger_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise LedgerError(ERROR_LEDGER_READ, str(error), path=path) from error


def _physical_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    return lines


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_envelope_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AssuranceError(
            f"record 'recorded_at' must be an unpadded ISO-8601 timestamp; got "
            f"{value!r}"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AssuranceError(
            f"record 'recorded_at' must be an ISO-8601 timestamp; got {value!r} "
            f"({error})"
        ) from error
    if parsed.tzinfo is None:
        raise AssuranceError(
            f"record 'recorded_at' must carry an explicit UTC offset; got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _record_from_envelope(envelope: Mapping[str, Any], *, seq: int) -> LedgerRecord:
    unknown = sorted(str(key) for key in envelope if key not in ENVELOPE_KEYS)
    if unknown:
        raise AssuranceError(
            f"record has unknown key(s) {', '.join(repr(key) for key in unknown)}; "
            f"allowed keys are {', '.join(repr(key) for key in ENVELOPE_KEYS)}"
        )
    kind = envelope["kind"]
    recorded_at = _require_envelope_timestamp(envelope.get("recorded_at"))
    payload = _PAYLOAD_PARSERS[kind](envelope.get("payload"))
    _require_kind_agreement(kind, payload)
    issue_id = envelope.get("issue_id")
    if kind in ISSUE_ALLOCATING_KINDS:
        if issue_id is None:
            raise AssuranceError(
                f"record kind {kind!r} must carry its allocated issue id"
            )
        issue_id = parse_issue_id(issue_id)
    elif issue_id is not None:
        raise AssuranceError(
            f"record kind {kind!r} must not carry an 'issue_id'; only "
            f"{', '.join(sorted(ISSUE_ALLOCATING_KINDS))} allocates one"
        )
    return LedgerRecord(
        seq=seq,
        kind=kind,
        recorded_at=recorded_at,
        payload=payload,
        issue_id=issue_id,
        version=envelope["version"],
    )


def _require_kind_agreement(kind: str, payload: Any) -> None:
    expected_states = _AUDIT_KIND_STATES.get(kind)
    if expected_states is None:
        return
    if payload.state not in expected_states:
        raise AssuranceError(
            f"record kind {kind!r} requires assurance state "
            f"{' or '.join(repr(item) for item in sorted(expected_states))}; "
            f"got {payload.state!r}"
        )


def _reject(
    rejected: list[RejectedLedgerRecord],
    *,
    seq: int,
    reason: str,
    raw: str,
    detail: str | None = None,
) -> None:
    if len(raw) > MAX_REJECTED_RAW_CHARS:
        raw = f"{raw[:MAX_REJECTED_RAW_CHARS]}…"
    rejected.append(
        RejectedLedgerRecord(seq=seq, reason=reason, raw=raw, detail=detail)
    )


def parse_ledger_bytes(data: bytes, path: Path) -> LedgerView:
    """Parse ledger bytes into accepted records and preserved rejections."""
    records: list[LedgerRecord] = []
    rejected: list[RejectedLedgerRecord] = []
    for seq, raw_bytes in enumerate(_physical_lines(data), start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        if raw.strip() == "":
            _reject(rejected, seq=seq, reason=REJECT_EMPTY_LINE, raw=raw)
            continue
        try:
            envelope = json.loads(raw)
        except ValueError as error:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_JSON,
                raw=raw,
                detail=str(error),
            )
            continue
        if not isinstance(envelope, Mapping):
            _reject(rejected, seq=seq, reason=REJECT_NOT_OBJECT, raw=raw)
            continue
        version = envelope.get("version")
        if isinstance(version, bool) or version not in SUPPORTED_LEDGER_SCHEMA_VERSIONS:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNSUPPORTED_VERSION,
                raw=raw,
                detail=f"version {version!r} is not one this build reads",
            )
            continue
        allowed_kinds: frozenset[str] | tuple[LedgerKind, ...] = (
            LEGACY_LEDGER_KINDS
            if version == LEDGER_LEGACY_SCHEMA_VERSION
            else LEDGER_KINDS
        )
        if envelope.get("kind") not in allowed_kinds:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNKNOWN_KIND,
                raw=raw,
                detail=(
                    f"kind {envelope.get('kind')!r} is not a ledger record kind "
                    f"at schema v{version}"
                ),
            )
            continue
        try:
            records.append(_record_from_envelope(envelope, seq=seq))
        except AssuranceError as error:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_RECORD,
                raw=raw,
                detail=str(error),
            )
    return LedgerView(path=path, records=tuple(records), rejected=tuple(rejected))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_ledger(*, state_dir: str | Path | None = None) -> LedgerView:
    """Read and validate the whole ledger under a shared lock.

    This is the only authority. Callers that need truth read here; the index
    exists to answer cheaper questions, never different ones.
    """
    path = ledger_path(state_dir)
    if not path.exists():
        return LedgerView(path=path)
    with _ledger_lock(path.parent, exclusive=False):
        data = _read_ledger_bytes(path)
    return parse_ledger_bytes(data, path)


def read_index(*, state_dir: str | Path | None = None) -> LedgerIndex:
    """Return the cached index, rebuilding it when it does not match the ledger."""
    path = ledger_path(state_dir)
    index_path = ledger_index_path(state_dir)
    if not path.exists():
        return rebuild_index(state_dir=state_dir)
    with _ledger_lock(path.parent, exclusive=False):
        data = _read_ledger_bytes(path)
        cached = _read_index_file(index_path)
    if cached is not None and cached.matches(data):
        return cached
    return rebuild_index(state_dir=state_dir)


def rebuild_index(*, state_dir: str | Path | None = None) -> LedgerIndex:
    """Derive the index from the ledger and replace the cache atomically."""
    root = state.state_root(state_dir)
    path = root / LEDGER_FILE_NAME
    index_path = root / LEDGER_INDEX_FILE_NAME
    _ensure_root(root)
    with _ledger_lock(root, exclusive=True):
        data = _read_ledger_bytes(path)
        index = _index_from_view(parse_ledger_bytes(data, path), data, path=index_path)
        _write_index_file(index)
    return index


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def _ensure_root(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LedgerError(ERROR_LEDGER_WRITE, str(error), path=root) from error


def _encode_record(
    kind: str, payload: Any, *, recorded_at: str, issue_id: str | None
) -> tuple[bytes, Any]:
    expected_type = _PAYLOAD_TYPES[kind]
    if not isinstance(payload, expected_type):
        raise LedgerError(
            ERROR_INVALID_PAYLOAD,
            f"kind {kind!r} stores a {expected_type.__name__}; got "
            f"{type(payload).__name__}",
        )
    envelope = {
        "version": LEDGER_SCHEMA_VERSION,
        "kind": kind,
        "recorded_at": recorded_at,
        "issue_id": issue_id,
        "payload": payload.to_dict(),
    }
    # Serializing and reparsing before the write is what makes "the ledger only
    # ever holds records this build can read back" true by construction rather
    # than by review.
    record = _record_from_envelope(envelope, seq=0)
    encoded = canonical_json_bytes(envelope)
    if len(encoded) > MAX_LEDGER_RECORD_BYTES:
        raise LedgerError(
            ERROR_RECORD_TOO_LARGE,
            f"a {kind!r} record of {len(encoded)} bytes exceeds the "
            f"{MAX_LEDGER_RECORD_BYTES}-byte record cap; the ledger stores "
            "bounded metadata and artifact references, never captured content",
        )
    return encoded, record.payload


def _append_locked(path: Path, blob: bytes) -> None:
    """Write ``blob`` at end of file and flush it through the device cache.

    A failure rolls back to the length the file had on entry, so prior bytes are
    never cut: an interrupted append stays on disk as a rejected line rather
    than being erased to make the file parse.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as error:
        raise LedgerError(ERROR_LEDGER_WRITE, str(error), path=path) from error
    try:
        os.fchmod(fd, 0o600)
        original_size = os.fstat(fd).st_size
        try:
            written = os.write(fd, blob)
            if written != len(blob):
                os.ftruncate(fd, original_size)
                raise OSError(
                    f"short ledger append: wrote {written} of {len(blob)} bytes"
                )
            # The inbox is the record of what ASL knows about itself. Flush the
            # line through the device cache before the lock is released so a
            # sudden shutdown cannot lose an issue another process was told
            # already exists.
            durability.fullsync(fd)
        except OSError:
            with suppress(OSError):
                os.ftruncate(fd, original_size)
            raise
    except OSError as error:
        raise LedgerError(ERROR_LEDGER_WRITE, str(error), path=path) from error
    finally:
        os.close(fd)


@dataclass(frozen=True)
class PendingRecord:
    """One record a transaction builder asks the ledger to append."""

    kind: str
    payload: Any
    issue_id: str | None = None
    recorded_at: str | None = None


def append_transaction(
    build: Callable[[LedgerView], Sequence[PendingRecord]],
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> tuple[LedgerRecord, ...]:
    """Append whatever ``build`` returns as one write under one exclusive lock.

    ``build`` runs inside the critical section and is handed the ledger as it
    stands, so a caller can decide what to write from what is already there —
    reuse an issue ID, or decline to write at all because an identical
    publication is already recorded. Returning no records leaves the file
    untouched, which is how a losing racer becomes a no-op rather than a
    duplicate.

    Every record lands in a single ``write``, so a derived state that spans
    several lines is never observed half-written by a concurrent reader.
    """
    root = state.state_root(state_dir)
    path = root / LEDGER_FILE_NAME
    index_path = root / LEDGER_INDEX_FILE_NAME
    stamp = recorded_at or _utc_now()
    _ensure_root(root)
    with _ledger_lock(root, exclusive=True):
        data = _read_ledger_bytes(path)
        view = parse_ledger_bytes(data, path)
        pending = tuple(build(view))
        if not pending:
            return ()
        chunks: list[bytes] = []
        records: list[LedgerRecord] = []
        seq = view.last_seq
        for item in pending:
            if item.kind not in LEDGER_KINDS:
                raise LedgerError(
                    ERROR_INVALID_KIND,
                    f"{item.kind!r} is not one of {', '.join(LEDGER_KINDS)}",
                )
            item_stamp = item.recorded_at or stamp
            encoded, stored_payload = _encode_record(
                item.kind, item.payload, recorded_at=item_stamp, issue_id=item.issue_id
            )
            seq += 1
            chunks.append(encoded)
            records.append(
                LedgerRecord(
                    seq=seq,
                    kind=item.kind,
                    recorded_at=item_stamp,
                    payload=stored_payload,
                    issue_id=item.issue_id,
                )
            )
        # Terminating a partial final line and writing the records are one
        # write, so a rollback restores the incomplete line rather than
        # removing it.
        separator = b"\n" if data and not data.endswith(b"\n") else b""
        blob = separator + b"".join(chunks)
        _append_locked(path, blob)
        # Terminating a line never changes how it parses, so the refreshed cache
        # is exact without reparsing everything that was already read.
        _write_index_file(
            _index_from_view(
                LedgerView(
                    path=path,
                    records=(*view.records, *records),
                    rejected=view.rejected,
                ),
                data + blob,
                path=index_path,
            )
        )
    return tuple(records)


def _append(
    kind: str,
    payload: Any,
    *,
    state_dir: str | Path | None,
    recorded_at: str | None,
    allocate_issue_id: bool,
) -> LedgerRecord:
    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        issue_id = None
        if allocate_issue_id:
            issue_id = view.canonical_issue_id_for_fingerprint(payload.fingerprint) or (
                f"{ISSUE_ID_PREFIX}{view.next_issue_number}"
            )
        return (PendingRecord(kind=kind, payload=payload, issue_id=issue_id),)

    return append_transaction(build, state_dir=state_dir, recorded_at=recorded_at)[0]


def append_record(
    kind: str,
    payload: Any,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append one validated record and return it with its physical sequence."""
    if kind not in LEDGER_KINDS:
        raise LedgerError(
            ERROR_INVALID_KIND,
            f"{kind!r} is not one of {', '.join(LEDGER_KINDS)}",
        )
    entry_point = GUARDED_KINDS.get(kind)
    if entry_point is not None:
        raise LedgerError(
            ERROR_INVALID_KIND,
            f"{kind!r} is only valid against the ledger as a whole and must be "
            f"appended through {entry_point} so it is decided under the append "
            "lock that writes it",
        )
    return _append(
        kind,
        payload,
        state_dir=state_dir,
        recorded_at=recorded_at,
        allocate_issue_id=False,
    )


def append_issue_observation(
    observation: IssueObservation,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append a sighting, minting or reusing its ``ASI-N`` under the append lock.

    A fingerprint already present in the ledger keeps the ID its first sighting
    was given; a new one takes the next number. Both decisions happen inside the
    same exclusive lock that writes the line, which is the only arrangement
    under which two processes cannot mint the same ID.
    """
    if not isinstance(observation, IssueObservation):
        raise LedgerError(
            ERROR_INVALID_PAYLOAD,
            f"an observation must be an IssueObservation; got "
            f"{type(observation).__name__}",
        )
    return _append(
        KIND_ISSUE_OBSERVED,
        observation,
        state_dir=state_dir,
        recorded_at=recorded_at,
        allocate_issue_id=True,
    )


def append_identity_binding(
    binding: IssueIdentityBinding,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append one identity binding, deciding it against the whole ledger.

    Both names in a binding must already exist: an identity key names an issue
    ASL minted, and the issue it resolves to must be the one that owns its
    history today. A key that is already bound is refused rather than rebound,
    because the point of the key is that it keeps meaning what it meant — and a
    request aimed at a merged-away issue is refused rather than redirected, since
    silently repointing an identity statement would record something other than
    what the caller reviewed.
    """
    if not isinstance(binding, IssueIdentityBinding):
        raise LedgerError(
            ERROR_INVALID_PAYLOAD,
            f"a binding must be an IssueIdentityBinding; got {type(binding).__name__}",
        )

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        allocated = view.allocated_issue_ids
        if binding.issue_id not in allocated:
            raise LedgerError(
                ERROR_UNKNOWN_ISSUE,
                f"{binding.issue_id} names no issue in the inbox",
            )
        if binding.named_issue_id not in allocated:
            raise LedgerError(
                ERROR_UNKNOWN_ISSUE,
                f"{binding.identity_key} names {binding.named_issue_id}, which "
                "no observation minted",
            )
        canonical = view.canonical_issue_id(binding.issue_id)
        if canonical != binding.issue_id:
            raise LedgerError(
                ERROR_ISSUE_NOT_CANONICAL,
                f"{binding.issue_id} merged into {canonical}, which owns its "
                "history now; bind the key to that issue instead",
            )
        bound = view.identity_bindings.get(binding.identity_key)
        if bound == binding.issue_id:
            raise LedgerError(
                ERROR_IDENTITY_ALREADY_BOUND,
                f"{binding.identity_key} already names {bound}",
            )
        if bound is not None:
            raise LedgerError(
                ERROR_IDENTITY_KEY_CONFLICT,
                f"{binding.identity_key} already names {bound} and cannot also "
                f"name {binding.issue_id}",
            )
        return (PendingRecord(kind=KIND_ISSUE_IDENTITY_BOUND, payload=binding),)

    return append_transaction(build, state_dir=state_dir, recorded_at=recorded_at)[0]
