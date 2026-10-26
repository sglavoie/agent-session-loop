"""Read the assurance ledger and rebuild its disposable identity index."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from . import durability, state
from .artifacts import sha256_hex
from .assurance_ledger_io import (
    _ensure_root,
    _ledger_lock,
    _read_ledger_bytes,
    _utc_now,
    ledger_index_path,
    ledger_path,
)
from .assurance_ledger_models import LedgerIndex, LedgerView
from .assurance_ledger_parse import parse_ledger_bytes
from .assurance_ledger_vocabulary import (
    LEDGER_FILE_NAME,
    LEDGER_INDEX_FILE_NAME,
    LEDGER_INDEX_VERSION,
)
from .assurance_models_primitives import canonical_json_bytes


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
    except ValueError, UnicodeDecodeError, RecursionError:
        return None
    return _parse_index_payload(payload, path=path)


def _write_index_file(index: LedgerIndex) -> None:
    """Replace the cache atomically, treating any failure as a cache miss.

    The ledger is already durable by the time this runs. A read-only state root
    or a full disk must not turn a recorded truth into an error, so the cache
    write is best effort in every caller.
    """
    with suppress(OSError):
        durability.replace_bytes(index.path, canonical_json_bytes(index.to_payload()))


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
