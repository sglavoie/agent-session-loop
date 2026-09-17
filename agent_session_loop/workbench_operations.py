"""The durable half of the Workbench's mutation admission.

:mod:`asl_workbench.operations` decides what may be done and what an honest
answer about an interrupted mutation is. It touches no filesystem on purpose,
so those rules load on the Pi. This module is where those decisions become
durable: a per-plan journal, an ``flock`` that serializes read-decide-write
across processes, a dispatch record written *before* the side effect, and a
restart pass that reconciles what it finds against ASL's own state.

Three properties are the point, and each is a file fact rather than an
in-process one:

* **The journal outlives the process.** ``workbench_server`` has kept launch
  receipts in a dict since the day it grew a launch route, which makes a retry
  idempotent only while the server that answered the first request is still
  running. The journal lives beside the redirect journal in the plan's state
  directory, so a restarted server -- or a second one, or the connector --
  reads the same record.

* **Admission is serialized between processes.** Every mutating entry point
  here holds ``LOCK_EX`` on the journal fd across the whole read-decide-append,
  which is the same discipline ``redirect_store`` applies to corrections and
  ``workbench.mutate_registry`` applies to the registry. Two submissions racing
  from two processes therefore produce one admission and one refusal, never two
  admissions.

* **The plan lock stays the execution authority.** Nothing here acquires,
  releases, inspects-for-reclaim, or otherwise touches ASL's plan lock;
  :func:`reconcile_plan` reads it through ``intervention.inspect_run``, which
  classifies without reclaiming. Admission is a gate *in front of* that
  authority: it stops a second attempt from being started, and the lock stops a
  second run from executing. Removing either would be a regression, and they
  are not alternatives to one another.

The journal holds identifiers only. Every row goes through
:func:`asl_workbench.operations.persist_operation` on the way in and on the way
out, so the reply text an operator typed and the action token that would let it
be sent again cannot reach the file even if a caller hands them over.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from asl_workbench import catalog as portable_catalog
from asl_workbench import operations

from . import clock, durability, intervention, plans, state

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
    canonical = plans.canonicalize_path(plan_dir)
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
    if existing_size and not _ends_with_newline(fd, existing_size):
        # An unterminated tail from a crashed writer: close its line so this
        # row is its own rather than appending onto a half-written one.
        encoded = b"\n" + encoded
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


def _compact_locked(fd: int, replay: _Replay, *, journal_path: Path) -> None:
    """Rewrite the journal as one row per surviving operation, in place.

    In place, on the same fd, under the lock already held. Every open operation
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
        os.ftruncate(fd, 0)
    except OSError as error:
        _fail(ERROR_JOURNAL_WRITE, str(error), path=journal_path)
    _write_at(fd, 0, encoded, journal_path=journal_path)
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


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def _owner_alive(record: Mapping[str, Any]) -> bool:
    """Whether the process that recorded this operation is still running.

    Unknowable cases answer *alive*, which is the conservative direction: an
    operation whose owner cannot be checked keeps holding its scope, so the
    worst outcome is a refusal an operator can resolve explicitly through
    :func:`settle_operation`. Answering *gone* instead would convert "we cannot
    tell" into "nothing happened", and that is the one claim this module must
    never make on no evidence.
    """
    pid = record.get("pid")
    hostname = record.get("hostname")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    if not isinstance(hostname, str) or not hostname:
        return True
    if hostname != _hostname():
        # Another host's process table is not ours to read.
        return True
    return state.is_pid_running(pid)


def _run_facts(
    plan_dir: str | Path, state_dir: str | Path | None
) -> intervention.RunFacts:
    return intervention.inspect_run(plan_dir, state_dir=state_dir)


def _evidence_for(
    record: Mapping[str, Any], facts: intervention.RunFacts
) -> operations.Evidence:
    return operations.Evidence(
        owner_alive=_owner_alive(record),
        run_active=facts.run_active,
        run_id=facts.run_id,
    )


#: How many times one record may be advanced in a single reconciliation pass.
#: ``dispatched -> accepted -> consumed`` is the longest real chain and
#: :data:`asl_workbench.operations.OPERATION_TRANSITIONS` is acyclic, so this is
#: a backstop against a future table change rather than a limit anything
#: legitimate reaches.
_MAX_RECONCILE_STEPS = 4


def _reconcile_locked(
    fd: int,
    replay: _Replay,
    *,
    journal_path: Path,
    plan_dir: str | Path,
    state_dir: str | Path | None,
    now: float,
    superseding: bool = False,
    resubmitted: str = "",
) -> list[dict[str, Any]]:
    """Settle what the authorities can settle, under the lock already held.

    Called before every admission decision, which is what stops a process that
    died mid-launch from holding a plan's run-start scope forever. The evidence
    comes from ASL -- the plan lock through ``intervention.inspect_run``, and
    the owning pid through ``state.is_pid_running`` -- because ASL is the
    authority on what actually happened and this journal is only the record of
    what was asked for.

    A record is advanced one legal step at a time until nothing more can be
    said about it, and only the resulting state is appended. The intermediate
    steps are not history anyone can use: they are all conclusions from the
    same single reading of the evidence, drawn in the same instant.

    ``resubmitted`` names the operation the caller is submitting right now, and
    it is the one row superseding never touches. A resend of the same id is the
    same submission, so letting it replace its own uncertain record would mean
    a retry answering itself with "you were replaced by you" -- and would hand
    the resend a fresh attempt at a mutation that may already have run.
    """
    outstanding_rows = [
        record
        for record in replay.ordered()
        if operations.outstanding(str(record.get("status", "")))
    ]
    if not outstanding_rows:
        return []
    facts = _run_facts(plan_dir, state_dir)
    changed: list[dict[str, Any]] = []
    for record in outstanding_rows:
        evidence = _evidence_for(record, facts)
        replaceable = superseding and record.get("operation_id") != resubmitted
        settled: dict[str, Any] | None = None
        current: Mapping[str, Any] = record
        for _ in range(_MAX_RECONCILE_STEPS):
            verdict = operations.reconcile(
                current, evidence, now=now, superseding=replaceable
            )
            if verdict is None:
                break
            settled = operations.advance_record(
                current,
                verdict.status,
                now=now,
                detail_code=verdict.detail_code,
            )
            current = settled
        if settled is not None:
            changed.append(
                _append_locked(fd, replay, settled, journal_path=journal_path)
            )
    return changed


def reconcile_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Reconcile one plan's open operations and return the rows that moved.

    Safe to call at any time and idempotent by construction: an operation the
    evidence cannot settle is left exactly as it was. It never supersedes --
    replacing an operation nobody could account for takes a person submitting
    something in its place, and a passive sweep is not that.
    """
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    if not journal_path.exists():
        return []
    moment = _moment(now)
    with _locked_journal(journal_path) as (fd, replay):
        return _reconcile_locked(
            fd,
            replay,
            journal_path=journal_path,
            plan_dir=plan_dir,
            state_dir=state_dir,
            now=moment,
        )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def admit_operation(
    plan_dir: str | Path,
    *,
    operation_id: str,
    method: str,
    selector: str = "",
    state_dir: str | Path | None = None,
    now: float | None = None,
    deliver_by: float | None = None,
    command_id: str | None = None,
    run_id: str | None = None,
    question_id: str | None = None,
) -> operations.Admission:
    """Decide and durably record what happens to one submitted mutation.

    Reconcile, decide, and append all happen under one exclusive lock on the
    journal. That is the whole cross-process guard: two Workbench processes, or
    a Workbench and the connector, submitting at the same moment serialize
    here, so exactly one of them is admitted and the other is told why not --
    replay for the same request, refusal for a different one.

    An admitted operation is written ``pending``, not ``dispatched``. The
    caller is expected to call :func:`mark_dispatched` immediately before the
    side effect, so an interruption between the two writes is distinguishable
    from an interruption after them.
    """
    resolved = operations.require_method(method)
    identifier = operations.require_operation_id(operation_id)
    target = plan_target(plan_dir, selector=selector)
    moment = _moment(now)
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        _reconcile_locked(
            fd,
            replay,
            journal_path=journal_path,
            plan_dir=plan_dir,
            state_dir=state_dir,
            now=moment,
            superseding=True,
            resubmitted=identifier,
        )
        admission = operations.admit(
            replay.ordered(),
            operation_id=identifier,
            method=resolved,
            target=target,
            now=moment,
            deliver_by=deliver_by,
            command_id=command_id,
            run_key=portable_catalog.run_key(target.plan_key, run_id),
            question_id=question_id,
            pid=os.getpid(),
            hostname=_hostname(),
        )
        if admission.admitted and admission.record is not None:
            stored = _append_locked(
                fd, replay, admission.record, journal_path=journal_path
            )
            return operations.Admission(
                decision=admission.decision,
                operation_id=admission.operation_id,
                method=admission.method,
                scope=admission.scope,
                fingerprint=admission.fingerprint,
                record=stored,
            )
        return admission


def _update(
    plan_dir: str | Path,
    operation_id: str,
    *,
    state_dir: str | Path | None,
    mutate: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
) -> dict[str, Any]:
    journal_path = journal_path_for_plan(plan_dir, state_dir)
    with _locked_journal(journal_path) as (fd, replay):
        record = replay.records.get(operation_id)
        if record is None:
            raise operations.OperationError(
                operations.REASON_UNKNOWN_OPERATION,
                f"no operation {operation_id!r} is recorded for this plan",
                details={"operation_id": operation_id},
            )
        updated = mutate(record)
        if updated is None:
            return dict(record)
        return _append_locked(fd, replay, updated, journal_path=journal_path)


def mark_dispatched(
    plan_dir: str | Path,
    operation_id: str,
    *,
    state_dir: str | Path | None = None,
    now: float | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Record that the mutation is about to be attempted.

    Called immediately before the side effect and never after it. The gap this
    write closes is the one that matters: a process killed between admission
    and this write leaves a ``pending`` row, which reconciles to ``expired``
    because the mutation demonstrably did not run; killed after it, the row is
    ``dispatched`` and reconciles to ``unconfirmed``, because it may have. The
    ordering is what buys the first answer, and nothing else can.
    """
    moment = _moment(now)
    return _update(
        plan_dir,
        operation_id,
        state_dir=state_dir,
        mutate=lambda record: operations.advance_record(
            record,
            operations.OPERATION_DISPATCHED,
            now=moment,
            run_id=run_id,
            pid=os.getpid(),
            hostname=_hostname(),
        ),
    )


def settle_operation(
    plan_dir: str | Path,
    operation_id: str,
    status: str,
    *,
    detail_code: str | None = None,
    run_id: str | None = None,
    state_dir: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Move one operation to a settled status, refusing an illegal move.

    The route calls this with what actually happened: ``accepted`` when ASL
    started the run, ``rejected`` when ASL refused it. It is also how an
    operator resolves an ``unconfirmed`` operation that no evidence can settle
    -- deliberately an explicit act, because deciding on someone's behalf that
    an uncertain mutation did not happen is the decision this module refuses to
    make automatically.
    """
    moment = _moment(now)
    return _update(
        plan_dir,
        operation_id,
        state_dir=state_dir,
        mutate=lambda record: operations.advance_record(
            record,
            status,
            now=moment,
            detail_code=detail_code,
            run_id=run_id,
        ),
    )


def confirm_operation(
    plan_dir: str | Path,
    operation_id: str,
    *,
    run_id: str | None = None,
    state_dir: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Record that the mutation ran and its receipt is in hand.

    ``accepted`` then ``consumed``, as one append. The two are separate states
    because on the Companion's side they are separated by a network: a command
    the bridge acknowledged is not yet a command ASL carried out. Here the
    mutation is local and synchronous, so the call that returns the receipt
    establishes both at once, and writing one row that says ``consumed`` is a
    truer record than two rows implying a gap that did not exist.

    ``consumed`` is terminal, which is what stops an accounted-for mutation
    from holding its scope: the plan lock, not this journal, is what refuses a
    second run while the first is still going.
    """
    moment = _moment(now)
    return _update(
        plan_dir,
        operation_id,
        state_dir=state_dir,
        mutate=lambda record: operations.advance_record(
            operations.advance_record(
                record,
                operations.OPERATION_ACCEPTED,
                now=moment,
                run_id=run_id,
            ),
            operations.OPERATION_CONSUMED,
            now=moment,
        ),
    )
