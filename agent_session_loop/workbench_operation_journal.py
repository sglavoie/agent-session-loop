"""Locked operation-journal storage, replay, compaction, and read projections."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from asl_workbench import catalog as portable_catalog
from asl_workbench import operations

from . import clock, durability, plans_files, state

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

#: Beside ``redirect-journal.jsonl`` in the plan's state directory. The
#: location is the identity: a journal found at this path is about the plan
#: that hashes to this directory, so nothing has to trust a plan name stored
#: inside a row.
OPERATIONS_JOURNAL_FILE_NAME = "operations.jsonl"


OPERATIONS_JOURNAL_VERSION = 1


OPERATIONS_JOURNAL_SUPPORTED_VERSIONS: tuple[int, ...] = (OPERATIONS_JOURNAL_VERSION,)


#: The ceiling on the file. Reached only by a plan mutated tens of thousands of
#: times, and reaching it compacts rather than refuses: unlike the redirect
#: journal, which is an event log where every line is evidence, only the latest
#: row per operation is meaningful here, so the older ones are redundant rather
#: than lost.
OPERATIONS_JOURNAL_MAX_BYTES = 4 * 1024 * 1024


#: How many settled operations survive a compaction, newest first. Open
#: operations always survive -- dropping one would resurrect the scope it is
#: holding -- and the settled tail is kept only so an operator who asks "what
#: happened to the thing I clicked" gets an answer for longer than one append.
OPERATIONS_RETAINED_SETTLED = 256


ERROR_JOURNAL_READ = "operation-journal-read"


ERROR_JOURNAL_WRITE = "operation-journal-write"


ERROR_JOURNAL_FULL = "operation-journal-full"


def journal_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Where one plan's operation journal lives."""
    return state.plan_state_dir(plan_dir, state_dir) / OPERATIONS_JOURNAL_FILE_NAME


def plan_target(
    plan_dir: str | Path, *, selector: str = ""
) -> operations.OperationTarget:
    """Name one plan the way the catalog names it.

    The target is built from :func:`asl_workbench.catalog.plan_key`, not from
    the directory, so a journal row and a published catalog row agree on which
    plan they are about and neither holds a Mac path.
    """
    canonical = plans_files.canonicalize_path(plan_dir)
    return operations.OperationTarget(
        plan_key=portable_catalog.plan_key(str(canonical)), selector=selector
    )


def new_operation_id() -> str:
    """Mint an operation id for a caller that did not supply one.

    A client that omits an idempotency key gets a unique one rather than a
    refusal, so a submission without a key behaves exactly as it did before
    this gate existed -- it simply cannot be replayed.
    """
    return os.urandom(16).hex()


def _hostname() -> str:
    return socket.gethostname()


def _moment(now: float | None) -> float:
    return clock.utc_now().timestamp() if now is None else float(now)


def _fail(reason: str, message: str, *, path: Path, **details: Any) -> Never:
    raise operations.OperationError(
        reason,
        message,
        status=500,
        details={"path": str(path), **details},
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@dataclass
class _Replay:
    """The journal's current meaning: the latest row for each operation."""

    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_seq: int = 0
    size_bytes: int = 0
    skipped: int = 0

    def ordered(self) -> list[dict[str, Any]]:
        """Rows in the order their operations were first admitted."""
        return [dict(record) for record in self.records.values()]


def _replay(data: bytes) -> _Replay:
    """Fold the journal into one row per operation, last write winning.

    Every line that cannot be understood is skipped rather than fatal: a torn
    tail from a process killed mid-append must not make a plan unmutatable
    forever. Skipping is safe here in a way it would not be in an event log,
    because a row is a complete statement of an operation's state rather than a
    delta -- losing the newest one loses recency, not coherence.
    """
    replay = _Replay(size_bytes=len(data))
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        record = _parse_line(line)
        if record is None:
            replay.skipped += 1
            continue
        seq, row = record
        replay.last_seq = max(replay.last_seq, seq)
        # Assigning into an existing key keeps its original position, so the
        # ordering stays "first admitted first" rather than "last touched".
        replay.records[str(row["operation_id"])] = row
    return replay


def _parse_line(line: bytes) -> tuple[int, dict[str, Any]] | None:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") not in OPERATIONS_JOURNAL_SUPPORTED_VERSIONS:
        return None
    row = payload.get("operation")
    if not isinstance(row, dict):
        return None
    operation_id = row.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        return None
    if row.get("status") not in operations.OPERATION_STATUSES:
        return None
    try:
        projected = operations.persist_operation(row)
    except operations.OperationError:
        # A row carrying something the allowlist forbids is not repaired into
        # one that does not: it is disowned, because trusting the rest of a
        # record that already broke the rule is how a leak becomes permanent.
        return None
    raw_seq = payload.get("seq")
    seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else 0
    return seq, projected


# ---------------------------------------------------------------------------
# Locked access
# ---------------------------------------------------------------------------


def _read_locked(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _read_shared(journal_path: Path) -> bytes:
    try:
        fd = os.open(journal_path, os.O_RDONLY)
    except FileNotFoundError:
        return b""
    except OSError as error:
        _fail(ERROR_JOURNAL_READ, str(error), path=journal_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        # A failed compaction can leave the inode empty or partially rewritten.
        # Its durable replacement is authoritative until recovery finishes.
        pending = _read_compaction(journal_path)
        if pending is not None:
            return pending
        return _read_locked(fd)
    except OSError as error:
        _fail(ERROR_JOURNAL_READ, str(error), path=journal_path)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _locked_journal(journal_path: Path) -> Iterator[tuple[int, _Replay]]:
    """Hold the journal exclusively and yield its current replay.

    The fd is deliberately *not* opened ``O_APPEND``: the exclusive lock is
    what orders writers here, and compaction has to be able to rewrite the file
    from offset zero without replacing the inode the lock is held on. Replacing
    it would hand two processes two different locks over the same journal,
    which is the one failure this whole module exists to rule out.
    """
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(journal_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        _fail(ERROR_JOURNAL_WRITE, str(error), path=journal_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        _recover_compaction(fd, journal_path=journal_path)
        yield fd, _replay(_read_locked(fd))
    except OSError as error:
        _fail(ERROR_JOURNAL_WRITE, str(error), path=journal_path)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _encode(seq: int, record: Mapping[str, Any]) -> bytes:
    payload = {
        "version": OPERATIONS_JOURNAL_VERSION,
        "seq": seq,
        "operation": operations.persist_operation(record),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _append_locked(
    fd: int,
    replay: _Replay,
    record: Mapping[str, Any],
    *,
    journal_path: Path,
) -> dict[str, Any]:
    """Append one row durably and return it, compacting first if it will not fit.

    A short write is rolled back to the pre-append size. A caller told its
    mutation was not recorded must not find it recorded later: that is exactly
    the "lost response, repeated action" failure in reverse, and it is just as
    bad.
    """
    projected = operations.persist_operation(record)
    encoded = _encode(replay.last_seq + 1, projected) + b"\n"
    existing_size = os.fstat(fd).st_size
    if existing_size and not _ends_with_newline(fd, existing_size):
        # Count the torn-tail separator before deciding whether the append fits.
        encoded = b"\n" + encoded
    if existing_size + len(encoded) > OPERATIONS_JOURNAL_MAX_BYTES:
        _compact_locked(fd, replay, journal_path=journal_path)
        existing_size = os.fstat(fd).st_size
        encoded = _encode(replay.last_seq + 1, projected) + b"\n"
        if existing_size + len(encoded) > OPERATIONS_JOURNAL_MAX_BYTES:
            _fail(
                ERROR_JOURNAL_FULL,
                f"append would exceed {OPERATIONS_JOURNAL_MAX_BYTES} bytes",
                path=journal_path,
                size_bytes=existing_size,
            )
    _write_at(fd, existing_size, encoded, journal_path=journal_path)
    replay.last_seq += 1
    replay.size_bytes = existing_size + len(encoded)
    replay.records[str(projected["operation_id"])] = projected
    return projected


def _write_at(fd: int, offset: int, encoded: bytes, *, journal_path: Path) -> None:
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        written = os.write(fd, encoded)
        if written != len(encoded):
            os.ftruncate(fd, offset)
            raise OSError(
                f"short operation-journal write: wrote {written} of "
                f"{len(encoded)} bytes"
            )
        # An admission a caller has been told about must be on the device
        # before the lock that serialized it is released. Otherwise a power
        # loss reopens the window where two processes both believe they are
        # first.
        durability.fullsync(fd)
    except OSError as error:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, offset)
        _fail(ERROR_JOURNAL_WRITE, str(error), path=journal_path)


def _settled_order(record: Mapping[str, Any]) -> float:
    for key in ("settled_at", "dispatched_at", "requested_at"):
        value = record.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return 0.0


def _ends_with_newline(fd: int, size: int) -> bool:
    os.lseek(fd, size - 1, os.SEEK_SET)
    return os.read(fd, 1) == b"\n"


def _compaction_path(journal_path: Path) -> Path:
    return journal_path.with_name(journal_path.name + ".compact")


def _read_compaction(journal_path: Path) -> bytes | None:
    try:
        return _compaction_path(journal_path).read_bytes()
    except FileNotFoundError:
        return None


def _recover_compaction(fd: int, *, journal_path: Path) -> None:
    """Finish a published compaction under the journal's exclusive lock.

    Keep the replacement until the rewritten inode is durable. Readers use
    that replacement after an interruption; writers finish the rewrite before
    appending. The inode itself never changes, so waiting flock holders still
    serialize with the recovering writer.
    """
    pending = _read_compaction(journal_path)
    # A writer can die after publishing the replacement but before syncing its
    # directory entry. Make it durable before touching the journal. When absent,
    # this also finishes a prior writer's interrupted unlink barrier, so an old
    # replacement cannot reappear after a later append becomes durable.
    durability.sync_directory(journal_path.parent)
    if pending is not None:
        os.ftruncate(fd, 0)
        _write_at(fd, 0, pending, journal_path=journal_path)
        _compaction_path(journal_path).unlink()
        durability.sync_directory(journal_path.parent)


def _compact_locked(fd: int, replay: _Replay, *, journal_path: Path) -> None:
    """Rewrite the journal as one row per surviving operation, in place.

    In place, on the same fd, under the lock already held. A durable recovery
    file holds the replacement before any bytes are removed. Every open operation
    survives; the settled ones survive most-recently-settled first, up to
    :data:`OPERATIONS_RETAINED_SETTLED`. Sequence numbers are renumbered from
    one because the file after this is the whole history there is.
    """
    rows = replay.ordered()
    kept: set[str] = {
        str(row["operation_id"])
        for row in rows
        if not operations.terminal(str(row["status"]))
    }
    settled = sorted(
        (row for row in rows if operations.terminal(str(row["status"]))),
        key=_settled_order,
    )
    kept.update(
        str(row["operation_id"]) for row in settled[-OPERATIONS_RETAINED_SETTLED:]
    )
    survivors = [row for row in rows if str(row["operation_id"]) in kept]

    encoded = b"".join(
        _encode(index, row) + b"\n" for index, row in enumerate(survivors, start=1)
    )
    try:
        durability.replace_bytes(
            _compaction_path(journal_path),
            encoded,
            sync_file=True,
            sync_parent=True,
            permissions=0o600,
        )
        _recover_compaction(fd, journal_path=journal_path)
    except OSError as error:
        _fail(ERROR_JOURNAL_WRITE, str(error), path=journal_path)
    replay.records = {str(row["operation_id"]): row for row in survivors}
    replay.last_seq = len(survivors)
    replay.size_bytes = len(encoded)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_operations(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Every operation recorded for one plan, latest state each, oldest first."""
    return _replay(_read_shared(journal_path_for_plan(plan_dir, state_dir))).ordered()


def export_operations(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """The same rows, projected onto what a cockpit may read back."""
    return [
        operations.export_operation(record)
        for record in read_operations(plan_dir, state_dir=state_dir)
    ]


def read_operation(
    plan_dir: str | Path,
    operation_id: str,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """One operation's latest recorded state, or ``None`` if it is not there."""
    replay = _replay(_read_shared(journal_path_for_plan(plan_dir, state_dir)))
    record = replay.records.get(operation_id)
    return dict(record) if record is not None else None


def open_operations(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """The operations still counting against the one-per-scope rule."""
    return [
        record
        for record in read_operations(plan_dir, state_dir=state_dir)
        if operations.outstanding(str(record.get("status", "")))
    ]
