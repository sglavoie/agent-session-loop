"""Assurance ledger paths, locks, and raw file reads."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from . import state
from .assurance_ledger_vocabulary import (
    ERROR_LEDGER_READ,
    ERROR_LEDGER_WRITE,
    LEDGER_FILE_NAME,
    LEDGER_INDEX_FILE_NAME,
    LEDGER_LOCK_FILE_NAME,
    LedgerError,
)

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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def _ensure_root(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise LedgerError(ERROR_LEDGER_WRITE, str(error), path=root) from error
