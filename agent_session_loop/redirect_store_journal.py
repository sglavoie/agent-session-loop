"""Journal file access: locking, reading, and durable appends.

Split out of the original ``redirect_store`` module; see
:mod:`agent_session_loop.redirect_store_vocab` for the journal's design.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import clock, durability, state
from .redirect_store_models import RedirectJournalView, RedirectStoreError
from .redirect_store_replay import _Replay, _replay, _view
from .redirect_store_vocab import (
    ERROR_JOURNAL_FULL,
    ERROR_JOURNAL_READ,
    ERROR_JOURNAL_WRITE,
    REDIRECT_JOURNAL_FILE_NAME,
    REDIRECT_JOURNAL_VERSION,
    RecordKind,
)


def journal_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the canonical redirect-journal path for ``plan_dir``."""
    return state.plan_state_dir(plan_dir, state_dir) / REDIRECT_JOURNAL_FILE_NAME


def _read_locked(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def read_journal(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> RedirectJournalView:
    """Replay the whole journal under a shared lock."""
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    return _view(_replay(_read_shared(journal_path)), journal_path)


def read_journal_at(state_dir: str | Path) -> RedirectJournalView:
    """Replay the journal in one plan-state directory named directly.

    :func:`read_journal` resolves a plan directory to its hashed state
    directory, which is the right addressing for everything that runs while a
    plan is where it says it is. A migrator holds the hashed directory itself
    and no longer has a plan path that maps to it, so it names the directory.
    Read-only on the same terms as every other read here.
    """
    journal_path = Path(state_dir) / REDIRECT_JOURNAL_FILE_NAME
    return _view(_replay(_read_shared(journal_path)), journal_path)


def read_feedback(
    plan_dir: str | Path,
    operation_id: str,
    *,
    state_dir: str | Path | None = None,
) -> str | None:
    """Return one request's correction text, or ``None`` if there is none.

    The only way the operator's words leave this module. Named per operation on
    purpose: a caller has to say which correction it is about to render, so a
    projection cannot pick the text up by walking a view.
    """
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    return _replay(_read_shared(journal_path)).feedback.get(operation_id)


def _read_shared(journal_path: Path) -> bytes:
    try:
        fd = os.open(journal_path, os.O_RDONLY)
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_READ, str(error), path=journal_path
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        return _read_locked(fd)
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_READ, str(error), path=journal_path
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _locked_journal(journal_path: Path) -> Iterator[tuple[int, _Replay]]:
    """Hold the journal exclusively and yield its current replay.

    Read, decide, and append all happen inside this block. Deciding outside it
    would validate against a view another producer has already invalidated,
    which is the whole failure mode idempotent submission has to survive.
    """
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(journal_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_WRITE, str(error), path=journal_path
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        yield fd, _replay(_read_locked(fd))
    except OSError as error:
        raise RedirectStoreError(
            ERROR_JOURNAL_WRITE, str(error), path=journal_path
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _append_locked(
    fd: int,
    replay: _Replay,
    record: Mapping[str, Any],
    *,
    journal_path: Path,
    effective_max_bytes: int,
) -> int:
    """Append one record durably, returning its sequence number.

    The sequence is ``replay.last_seq + 1`` rather than a re-count of the file:
    the exclusive lock is still held, so the replay the caller decided against
    is still the file's contents, and a torn last line is terminated below
    rather than merged — either way this record lands on the next physical
    line.

    A short write is rolled back to the pre-append size. A torn tail would be
    replayed as a rejected line forever, and a producer told its submission
    failed must not find that submission accepted later.
    """
    existing_size = os.fstat(fd).st_size
    encoded = json.dumps(dict(record), separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    if existing_size and not _ends_with_newline(fd, existing_size):
        # An unterminated tail from a crashed writer: close its line so this
        # record is its own, rather than appending onto a half-written one.
        encoded = b"\n" + encoded
    encoded += b"\n"
    if existing_size + len(encoded) > effective_max_bytes:
        raise RedirectStoreError(
            ERROR_JOURNAL_FULL,
            f"append would exceed {effective_max_bytes} bytes",
            path=journal_path,
            details={"size_bytes": existing_size, "max_bytes": effective_max_bytes},
        )
    try:
        written = os.write(fd, encoded)
        if written != len(encoded):
            os.ftruncate(fd, existing_size)
            raise OSError(
                f"short redirect-journal append: wrote {written} of "
                f"{len(encoded)} bytes"
            )
        # A correction a producer has been told is durable must be on the
        # device before the lock that serialized it is released.
        durability.fullsync(fd)
    except OSError as error:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, existing_size)
        raise RedirectStoreError(
            ERROR_JOURNAL_WRITE, str(error), path=journal_path
        ) from error
    replay.last_seq += 1
    replay.size_bytes = existing_size + len(encoded)
    return replay.last_seq


def _ends_with_newline(fd: int, size: int) -> bool:
    os.lseek(fd, size - 1, os.SEEK_SET)
    return os.read(fd, 1) == b"\n"


def _record(kind: RecordKind, operation_id: str, created_at: str) -> dict[str, Any]:
    return {
        "version": REDIRECT_JOURNAL_VERSION,
        "kind": kind,
        "id": operation_id,
        "created_at": created_at,
    }


def _moment(now: datetime | None) -> datetime:
    return clock.utc_now() if now is None else now.astimezone(UTC)
