"""Durable assurance ledger transactions and public ledger API.

Transactions retain one exclusive lock and one append. The sibling modules
own envelope parsing, records, raw reads, and the rebuildable index."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import durability, state
from .assurance_ledger_index import _index_from_view, _write_index_file
from .assurance_ledger_index import read_index as read_index
from .assurance_ledger_index import read_ledger as read_ledger
from .assurance_ledger_index import rebuild_index as rebuild_index
from .assurance_ledger_io import (
    _ensure_root,
    _ledger_lock,
    _read_ledger_bytes,
    _utc_now,
)
from .assurance_ledger_io import ledger_index_path as ledger_index_path
from .assurance_ledger_io import ledger_lock_path as ledger_lock_path
from .assurance_ledger_io import ledger_path as ledger_path
from .assurance_ledger_models import LedgerIndex as LedgerIndex
from .assurance_ledger_models import LedgerRecord, LedgerView, PendingRecord
from .assurance_ledger_models import RejectedLedgerRecord as RejectedLedgerRecord
from .assurance_ledger_models import follow_merge_chain as follow_merge_chain
from .assurance_ledger_parse import _record_from_envelope, parse_ledger_bytes
from .assurance_ledger_vocabulary import (
    _PAYLOAD_TYPES,
    ERROR_IDENTITY_ALREADY_BOUND,
    ERROR_IDENTITY_KEY_CONFLICT,
    ERROR_INVALID_KIND,
    ERROR_INVALID_PAYLOAD,
    ERROR_ISSUE_NOT_CANONICAL,
    ERROR_LEDGER_WRITE,
    ERROR_RECORD_TOO_LARGE,
    ERROR_UNKNOWN_ISSUE,
    GUARDED_KINDS,
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_OBSERVED,
    LEDGER_FILE_NAME,
    LEDGER_INDEX_FILE_NAME,
    LEDGER_KINDS,
    LEDGER_SCHEMA_VERSION,
    MAX_LEDGER_RECORD_BYTES,
    LedgerError,
)
from .assurance_ledger_vocabulary import ENVELOPE_KEYS as ENVELOPE_KEYS
from .assurance_ledger_vocabulary import ERROR_LEDGER_READ as ERROR_LEDGER_READ
from .assurance_ledger_vocabulary import (
    ISSUE_ALLOCATING_KINDS as ISSUE_ALLOCATING_KINDS,
)
from .assurance_ledger_vocabulary import KIND_AUDIT_COMPLETED as KIND_AUDIT_COMPLETED
from .assurance_ledger_vocabulary import KIND_AUDIT_PENDING as KIND_AUDIT_PENDING
from .assurance_ledger_vocabulary import KIND_AUDIT_SKIPPED as KIND_AUDIT_SKIPPED
from .assurance_ledger_vocabulary import (
    KIND_AUDIT_UNAVAILABLE as KIND_AUDIT_UNAVAILABLE,
)
from .assurance_ledger_vocabulary import KIND_HUMAN_CLOSEOUT as KIND_HUMAN_CLOSEOUT
from .assurance_ledger_vocabulary import (
    KIND_IMPROVEMENT_PLAN_DRAFTED as KIND_IMPROVEMENT_PLAN_DRAFTED,
)
from .assurance_ledger_vocabulary import (
    KIND_ISSUE_DISPOSITION as KIND_ISSUE_DISPOSITION,
)
from .assurance_ledger_vocabulary import (
    KIND_ISSUE_KEPT_DISTINCT as KIND_ISSUE_KEPT_DISTINCT,
)
from .assurance_ledger_vocabulary import KIND_ISSUE_MERGED as KIND_ISSUE_MERGED
from .assurance_ledger_vocabulary import (
    KINDS_BY_SCHEMA_VERSION as KINDS_BY_SCHEMA_VERSION,
)
from .assurance_ledger_vocabulary import LEDGER_INDEX_VERSION as LEDGER_INDEX_VERSION
from .assurance_ledger_vocabulary import LEDGER_LOCK_FILE_NAME as LEDGER_LOCK_FILE_NAME
from .assurance_ledger_vocabulary import (
    MAX_REJECTED_RAW_CHARS as MAX_REJECTED_RAW_CHARS,
)
from .assurance_ledger_vocabulary import REJECT_EMPTY_LINE as REJECT_EMPTY_LINE
from .assurance_ledger_vocabulary import REJECT_INVALID_JSON as REJECT_INVALID_JSON
from .assurance_ledger_vocabulary import REJECT_INVALID_RECORD as REJECT_INVALID_RECORD
from .assurance_ledger_vocabulary import REJECT_NOT_OBJECT as REJECT_NOT_OBJECT
from .assurance_ledger_vocabulary import REJECT_UNKNOWN_KIND as REJECT_UNKNOWN_KIND
from .assurance_ledger_vocabulary import (
    REJECT_UNSUPPORTED_VERSION as REJECT_UNSUPPORTED_VERSION,
)
from .assurance_ledger_vocabulary import REJECTION_REASONS as REJECTION_REASONS
from .assurance_ledger_vocabulary import (
    SUPPORTED_LEDGER_SCHEMA_VERSIONS as SUPPORTED_LEDGER_SCHEMA_VERSIONS,
)
from .assurance_ledger_vocabulary import LedgerKind as LedgerKind
from .assurance_models_inbox import IssueIdentityBinding, IssueObservation
from .assurance_models_primitives import ISSUE_ID_PREFIX, canonical_json_bytes


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
