"""Plan-directory transaction journal, lock, and recovery."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from . import durability
from .plans_errors import (
    PlanError,
)
from .plans_files import (
    _fsync_directory,
    canonicalize_path,
)

PLAN_TRANSACTION_DIR_NAME = ".asl-plan-transaction"


PLAN_TRANSACTION_STAGING_DIR_NAME = ".asl-plan-transaction-staging"


PLAN_TRANSACTION_CLEANUP_DIR_NAME = ".asl-plan-transaction-cleanup"


PLAN_TRANSACTION_JOURNAL_NAME = "journal.json"


PLAN_TRANSACTION_LOCK_NAME = "transaction.lock"


PLAN_TRANSACTION_SCHEMA_VERSION = 1


# Transaction publication is normally a short rename-and-replace sequence. A
# holder that exceeds this bound is treated as stalled so read-side recovery
# fails diagnostically instead of hanging status and Workbench consumers.
PLAN_TRANSACTION_LOCK_WAIT_SECONDS = 5.0


PLAN_TRANSACTION_LOCK_POLL_SECONDS = 0.01


def _retire_plan_transaction(canonical_dir: Path, transaction_dir: Path) -> None:
    """Atomically remove the live marker before best-effort payload cleanup."""
    cleanup_dir = canonical_dir / PLAN_TRANSACTION_CLEANUP_DIR_NAME
    shutil.rmtree(cleanup_dir, ignore_errors=True)
    os.replace(transaction_dir, cleanup_dir)
    _fsync_directory(canonical_dir)
    shutil.rmtree(cleanup_dir)
    _fsync_directory(canonical_dir)


@contextmanager
def _plan_transaction_mutex(
    transaction_dir: Path, *, wait_seconds: float | None = None
) -> Iterator[tuple[int, int]]:
    """Exclude readers and writers through a bounded marker-scoped lock.

    The holder record is advisory diagnostic data. The kernel lock remains the
    authority, while the record lets a timed-out reader identify the process
    and thread that last acquired it.
    """
    lock_path = transaction_dir / PLAN_TRANSACTION_LOCK_NAME
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        bound = (
            PLAN_TRANSACTION_LOCK_WAIT_SECONDS
            if wait_seconds is None
            else max(wait_seconds, 0.0)
        )
        deadline = time.monotonic() + bound
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError as error:
                if error.errno not in (
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EINTR,
                    errno.EWOULDBLOCK,
                ):
                    raise
                if time.monotonic() >= deadline:
                    holder = _plan_transaction_lock_holder(descriptor)
                    raise PlanError(
                        f"timed out after {bound:g}s waiting for plan transaction "
                        f"lock at {lock_path}; holder {holder}"
                    ) from error
                time.sleep(PLAN_TRANSACTION_LOCK_POLL_SECONDS)

        holder_payload = json.dumps(
            {
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(holder_payload)
        while view:
            view = view[os.write(descriptor, view) :]
        stat_result = os.fstat(descriptor)
        yield stat_result.st_dev, stat_result.st_ino
    finally:
        try:
            if held:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _plan_transaction_lock_holder(descriptor: int) -> str:
    """Describe the advisory holder record visible through ``descriptor``."""
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = json.loads(os.read(descriptor, 4096).decode("utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return "unknown (metadata unavailable)"
    if not isinstance(payload, dict):
        return "unknown (metadata unavailable)"
    pid = payload.get("pid")
    hostname = payload.get("hostname")
    thread = payload.get("thread")
    if not isinstance(pid, int) or not isinstance(hostname, str):
        return "unknown (metadata unavailable)"
    description = f"pid {pid} on {hostname}"
    if isinstance(thread, str) and thread:
        description += f" thread {thread!r}"
    return description


def _transaction_lock_is_current(
    transaction_dir: Path, identity: tuple[int, int]
) -> bool:
    """Return whether ``identity`` still names the live marker's lock file."""
    try:
        stat_result = (transaction_dir / PLAN_TRANSACTION_LOCK_NAME).stat()
    except FileNotFoundError:
        return False
    return identity == (stat_result.st_dev, stat_result.st_ino)


def _recover_pending_plan_transaction_locked(
    canonical_dir: Path, transaction_dir: Path
) -> bool:
    """Roll forward the live transaction while its marker mutex is held."""
    if not transaction_dir.exists():
        return False
    if not transaction_dir.is_dir():
        raise PlanError(
            f"invalid plan transaction marker: {transaction_dir} is not a directory"
        )

    journal_path = transaction_dir / PLAN_TRANSACTION_JOURNAL_NAME
    if not journal_path.exists():
        raise PlanError("pending plan transaction marker has no durable journal")

    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PlanError(
            f"cannot read pending plan transaction journal: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise PlanError("pending plan transaction journal must contain a JSON object")
    if payload.get("version") != PLAN_TRANSACTION_SCHEMA_VERSION:
        raise PlanError("pending plan transaction journal uses an unsupported version")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise PlanError("pending plan transaction journal has no entries")

    entries: list[tuple[Path, bytes]] = []
    for index, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            raise PlanError(f"pending plan transaction entry #{index} is malformed")
        relative_path = raw_entry.get("path")
        payload_name = raw_entry.get("payload")
        expected_sha = raw_entry.get("sha256")
        if (
            not isinstance(relative_path, str)
            or not isinstance(payload_name, str)
            or not isinstance(expected_sha, str)
        ):
            raise PlanError(f"pending plan transaction entry #{index} is malformed")
        parsed_path = PurePosixPath(relative_path)
        if (
            not relative_path
            or relative_path == "."
            or parsed_path.is_absolute()
            or ".." in parsed_path.parts
            or payload_name != f"{index - 1:06d}.bin"
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        ):
            raise PlanError(f"pending plan transaction entry #{index} is unsafe")
        destination = canonical_dir.joinpath(*parsed_path.parts)
        try:
            destination.resolve().relative_to(canonical_dir)
        except ValueError as error:
            raise PlanError(
                f"pending plan transaction entry #{index} escapes the plan directory"
            ) from error
        payload_path = transaction_dir / payload_name
        try:
            content = payload_path.read_bytes()
        except OSError as error:
            raise PlanError(
                f"cannot read pending plan transaction payload #{index}: {error}"
            ) from error
        if hashlib.sha256(content).hexdigest() != expected_sha:
            raise PlanError(
                f"pending plan transaction payload #{index} failed its checksum"
            )
        entries.append((destination, content))

    try:
        for destination, content in entries:
            destination.parent.mkdir(parents=True, exist_ok=True)
            durability.replace_bytes(
                destination, content, sync_file=True, sync_parent=True
            )
        _retire_plan_transaction(canonical_dir, transaction_dir)
    except OSError as error:
        raise PlanError(f"cannot recover pending plan transaction: {error}") from error
    return True


def recover_pending_plan_transaction(plan_dir: str | Path) -> bool:
    """Roll forward a durable multi-file plan transaction, if one is pending.

    The transaction payloads remain immutable until every destination has been
    replaced. Replaying all entries is therefore idempotent, including after a
    second interruption during recovery. A staging directory without a journal
    predates publication and is safe to discard.
    """
    canonical_dir = canonicalize_path(plan_dir)
    transaction_dir = canonical_dir / PLAN_TRANSACTION_DIR_NAME
    while transaction_dir.exists():
        try:
            with _plan_transaction_mutex(transaction_dir) as identity:
                # The writer may have retired this marker while we waited and a
                # later transaction may already have published another one.
                # Never act on a marker protected by a different lock inode.
                if not _transaction_lock_is_current(transaction_dir, identity):
                    continue
                return _recover_pending_plan_transaction_locked(
                    canonical_dir, transaction_dir
                )
        except FileNotFoundError:
            # Retirement between the existence check and lock open means the
            # commit completed; re-check in case another marker replaced it.
            continue
        except OSError as error:
            raise PlanError(f"cannot lock pending plan transaction: {error}") from error
    return False
