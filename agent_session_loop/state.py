"""Operational state and locking boundaries.

Runner state exists to coordinate local execution, capture logs, and recover
from pauses. It must never replace the plan files as the durable completion
record.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import shlex
import socket
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    config,
    events,
    harnesses,
    pause_policy,
    plans,
    process_tree,
    responses,
    workspaces,
)
from .formatting import format_duration_seconds

LOCK_FILE_NAME = "lock.json"
# Sidecar `flock` target that serializes abandonment. Deliberately not the
# plan lock: abandonment runs *while* another process holds or is taking that
# lock, so the two claims have to be independent.
ABANDON_CLAIM_FILE_NAME = "abandon.lock"
# Sidecar `flock` target that serializes every mutation of the plan lock. The
# lock file itself cannot play this role: it is created and unlinked, so two
# callers holding that path can be looking at different files.
LOCK_MUTEX_FILE_NAME = "lock.mutex"
# How long a lock mutation waits for that mutex. Each critical section is a
# read plus one create or unlink, so contention past this bound means a holder
# is wedged rather than busy.
LOCK_MUTEX_WAIT_SECONDS = 5.0
LOCK_MUTEX_POLL_SECONDS = 0.01
RUNS_DIR_NAME = "runs"
RESULT_FILE_NAME = "result.json"
TURN_DIR_PREFIX = "turn-"
STATE_MIGRATED_EVENT = "state_migrated"
# How long an unfinished run may go without any lock file before its absence
# counts as proof of death. A booting runner writes `state.json` before its
# lock, so the window has to outlast that gap by a wide margin.
ABANDONED_NO_LOCK_GRACE_SECONDS = 120.0
ABANDONED_REASON = "abandoned"
ABANDONED_TRIGGER_LOCK_TAKEOVER = "lock-takeover"
ABANDONED_TRIGGER_STATUS = "status"

# Run-relative record naming a run whose own process died owing an audit. It
# lives in the run directory rather than in `state.json` because the next run
# of the same plan overwrites `state.json`, and it carries its own copy of the
# run configuration so the audit role can still be resolved afterwards.
ASSURANCE_PENDING_FILE_NAME = "assurance-pending.json"
ASSURANCE_PENDING_SCHEMA_VERSION = 1
# The run wrote no assurance object at all, so it comes from an ASL that had no
# audit. Catch-up reports it rather than inventing a verdict for history.
ASSURANCE_SKIP_PREDATES = "predates-assurance"
# The run already reached a settled assurance state (a verdict, or an opt-out).
ASSURANCE_SKIP_SETTLED = "audit-already-settled"
# Assurance states that still owe an audit. Mirrors
# ``runner._AUDITABLE_ASSURANCE_STATES``; kept as a literal here because state
# must not import the runner.
_ASSURANCE_OWED_STATES = frozenset({"pending", "unavailable"})
WAITING_FOR_REPLY_STATUS_LIVE = "live"
WAITING_FOR_REPLY_STATUS_EXPIRED = "expired"
WAITING_FOR_REPLY_STATUS_INVALID = "invalid"
WAITING_FOR_REPLY_STATUSES: tuple[str, ...] = (
    WAITING_FOR_REPLY_STATUS_LIVE,
    WAITING_FOR_REPLY_STATUS_EXPIRED,
    WAITING_FOR_REPLY_STATUS_INVALID,
)

# state.json path fields the migration rewrites to point at the new state dir.
# ``event_path`` is the legacy duplicate of ``events_path``; both are rewritten
# so neither is left dangling at the old location. Historical per-turn
# ``result.json`` paths are intentionally excluded (see ``migrate_plan_state``).
_MIGRATION_PATH_FIELDS: tuple[str, ...] = (
    "plan_dir",
    "state_dir",
    "state_path",
    "events_path",
    "event_path",
    "lock_path",
)


class StateError(Exception):
    """Raised when operational state cannot be read or updated safely."""


class AmbiguousPlanError(StateError):
    """Raised when a bare plan label matches multiple plan directories.

    A subclass of :class:`StateError` so existing ``except StateError`` guards
    keep working, while callers that need the ``ambiguous-plan`` reason can
    catch this type first. Carries the sorted canonical candidates and the
    resolved state root that produced them.
    """

    def __init__(
        self,
        label: str,
        candidates: list[Path],
        state_root: Path,
    ) -> None:
        self.label = label
        self.candidates = sorted(candidates, key=str)
        self.state_root = state_root
        joined = ", ".join(str(candidate) for candidate in self.candidates)
        super().__init__(
            f"plan label {label!r} is ambiguous under {state_root}: matches {joined}"
        )


@dataclass(frozen=True)
class LockInfo:
    """Parsed operational lock state for one plan directory."""

    path: Path
    pid: int | None
    plan_dir: str
    created_at: str
    stale: bool
    reason: str
    raw: dict[str, Any]

    @property
    def active(self) -> bool:
        return not self.stale

    @property
    def owner_hostname(self) -> str | None:
        """Host that recorded ``pid``, or ``None`` for legacy locks."""
        return _nonempty_str(self.raw.get("hostname"))

    @property
    def owner_process_start(self) -> str | None:
        """Birth time recorded for ``pid``, or ``None`` if the lock names none.

        Distinguishes the process that published the lock from a later one that
        reused its pid, for readers that report on the owner. It is deliberately
        not an authentication token for acting *on* that process: ``ps`` reports
        a start time to one-second resolution, so two processes can present the
        same value, and any check of it is separated from whatever it authorizes
        by a window in which the pid can be recycled.
        """
        return _nonempty_str(self.raw.get("process_start"))

    @property
    def wakeup_endpoint(self) -> str | None:
        """FIFO this run reads control wakeups from, or ``None`` if it has none.

        Written by the owner before the lock became visible, so the path names
        an endpoint whose read end that owner already holds. ``None`` covers a
        version 2 lock, which predates the endpoint, and a runner whose state
        directory could not host a FIFO; both mean the durable queue poll is the
        only transport for that run.
        """
        return _nonempty_str(self.raw.get("wakeup_endpoint"))

    @property
    def foreign_host(self) -> bool:
        """Whether ``pid`` belongs to another host's process table.

        Nothing this host can observe says anything about such a lock's owner,
        so it is never proof of death and never safe to steal.
        """
        hostname = self.owner_hostname
        return hostname is not None and hostname != socket.gethostname()

    @property
    def repository_roots(self) -> tuple[str, ...]:
        """Repository roots this lock claims, or ``()`` for a version 1 lock.

        Empty means "declares nothing", not "claims nothing": a version 1 lock
        predates the claim, so a caller comparing registries cannot read it as
        proof that the owning run touches no repository of its own.
        """
        raw_roots = self.raw.get("repositories")
        if not isinstance(raw_roots, list):
            return ()
        return tuple(root for root in raw_roots if isinstance(root, str) and root)


@dataclass(frozen=True)
class UnlockResult:
    """Result from removing or checking a plan lock."""

    path: Path
    had_lock: bool
    removed: bool
    was_active: bool
    was_stale: bool
    message: str


def state_root(state_dir: str | Path | None = None) -> Path:
    if state_dir is None:
        return config.default_state_root()
    return plans.canonicalize_path(state_dir)


def plan_state_dir(plan_dir: str | Path, state_dir: str | Path | None = None) -> Path:
    return state_root(state_dir) / plans.path_sha256(plan_dir)


def lock_path_for_plan(
    plan_dir: str | Path, state_dir: str | Path | None = None
) -> Path:
    return plan_state_dir(plan_dir, state_dir) / LOCK_FILE_NAME


def _is_bare_label(raw_value: str) -> bool:
    """Return True when ``raw_value`` is a bare basename with no path parts.

    Only bare labels trigger state lookup. Anything containing a directory
    component (``foo/bar``, ``./foo``, ``/abs``) or the ``.``/``..`` relative
    references retains the current path-only behavior.
    """
    if raw_value in ("", ".", ".."):
        return False
    head, tail = os.path.split(raw_value)
    if head:
        return False
    return tail == raw_value


def _read_json_object_safe(path: Path) -> dict[str, Any] | None:
    """Best-effort read of a JSON object; None on any failure or non-object.

    Tolerates malformed, unreadable, or concurrently removed files so one bad
    metadata record cannot block discovery of other candidates.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError, ValueError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _recorded_content_hash(state_obj: dict[str, Any]) -> str | None:
    """Return the plan content hash a stored state should match against.

    Prefers ``final_snapshot.content_sha256``: a successful run mutates session
    handoffs, so the on-disk plan content matches the snapshot taken when the
    run finished, not the one taken at its start. Falls back to
    ``initial_snapshot.content_sha256`` only for legacy state written before a
    final snapshot was persisted. Returns ``None`` when neither is present.
    """
    for section_key in ("final_snapshot", "initial_snapshot"):
        section = state_obj.get(section_key)
        if isinstance(section, dict):
            recorded = _nonempty_str(section.get("content_sha256"))
            if recorded is not None:
                return recorded
    return None


def _plan_dir_from_result(payload: dict[str, Any]) -> str | None:
    """Recover a plan directory from a turn result, ``after`` before ``before``."""
    for section_key in ("after", "before"):
        section = payload.get(section_key)
        if isinstance(section, dict):
            value = _nonempty_str(section.get("plan_dir"))
            if value:
                return value
    return None


def _iter_result_paths(runs_dir: Path) -> Iterator[Path]:
    """Yield ``runs/*/turn-*/result.json`` files under one state directory."""
    try:
        if not runs_dir.is_dir():
            return
        run_dirs = sorted(runs_dir.iterdir())
    except OSError:
        return
    for run_dir in run_dirs:
        try:
            if not run_dir.is_dir():
                continue
            turn_dirs = sorted(run_dir.iterdir())
        except OSError:
            continue
        for turn_dir in turn_dirs:
            try:
                if not turn_dir.name.startswith(TURN_DIR_PREFIX):
                    continue
                if not turn_dir.is_dir():
                    continue
                result_path = turn_dir / RESULT_FILE_NAME
                if result_path.is_file():
                    yield result_path
            except OSError:
                continue


def _collect_plan_dir_strings(state_dir: Path) -> list[str]:
    """Gather raw ``plan_dir`` strings from one per-plan state directory.

    Sources, in order: ``state.json``, ``lock.json``, then every
    ``runs/*/turn-*/result.json`` recovering ``after.plan_dir`` before
    ``before.plan_dir``. Missing or malformed records are silently skipped.
    """
    results: list[str] = []
    state_obj = _read_json_object_safe(state_dir / events.STATE_FILE_NAME)
    if state_obj is not None:
        value = _nonempty_str(state_obj.get("plan_dir"))
        if value:
            results.append(value)
    lock_obj = _read_json_object_safe(state_dir / LOCK_FILE_NAME)
    if lock_obj is not None:
        value = _nonempty_str(lock_obj.get("plan_dir"))
        if value:
            results.append(value)
    for result_path in _iter_result_paths(state_dir / RUNS_DIR_NAME):
        result_obj = _read_json_object_safe(result_path)
        if result_obj is None:
            continue
        value = _plan_dir_from_result(result_obj)
        if value:
            results.append(value)
    return results


def iter_plan_state_dirs(state_root: Path) -> Iterator[Path]:
    """Yield per-plan state directories under ``state_root``.

    A missing root scans as an empty corpus. A root that exists but is not a
    directory, or that cannot be enumerated, raises :class:`StateError`.
    """
    if not state_root.exists():
        return
    if not state_root.is_dir():
        raise StateError(f"state root is not a directory: {state_root}")
    try:
        children = sorted(state_root.iterdir())
    except OSError as error:
        raise StateError(f"could not scan state root {state_root}: {error}") from error
    for child in children:
        try:
            if child.is_dir():
                yield child
        except OSError:
            continue


def resolve_plan_reference(raw_value: str, state_root: Path) -> Path:
    """Resolve a plan argument to a plan directory, honoring direct paths.

    Existing directories always win and are returned canonicalized without any
    state lookup. Path-like nonexistent arguments (those with a directory
    component) also bypass lookup and return the canonicalized argument. Only a
    bare, nonexistent label scans the state corpus under ``state_root``,
    matching persisted ``plan_dir`` basenames exactly and case-sensitively
    against existing plan directories.

    Returns the sole matching plan directory, or the canonicalized argument
    when nothing matches. Raises :class:`AmbiguousPlanError` when more than one
    existing plan directory shares the requested basename. Discovery is
    read-only and independent of process liveness.
    """
    original = plans.canonicalize_path(raw_value)
    if original.is_dir():
        return original
    if not _is_bare_label(raw_value):
        return original

    matches: dict[str, Path] = {}
    for state_dir in iter_plan_state_dirs(state_root):
        for raw_plan_dir in _collect_plan_dir_strings(state_dir):
            try:
                candidate = plans.canonicalize_path(raw_plan_dir)
            except OSError, ValueError:
                continue
            key = str(candidate)
            if key in matches:
                continue
            if candidate.name != raw_value:
                continue
            try:
                is_existing_dir = candidate.is_dir()
            except OSError:
                continue
            if is_existing_dir:
                matches[key] = candidate

    if not matches:
        return original
    if len(matches) == 1:
        return next(iter(matches.values()))
    raise AmbiguousPlanError(raw_value, list(matches.values()), state_root)


@dataclass(frozen=True)
class RunLocation:
    """Where one run's artifacts live, and which plan produced them."""

    run_id: str
    state_dir: Path
    plan_dir: Path | None

    @property
    def plan_id(self) -> str | None:
        """The compact plan label, or ``None`` when no plan dir was recorded."""
        return None if self.plan_dir is None else self.plan_dir.name


def _recorded_plan_dir(state_dir: Path) -> Path | None:
    for raw_plan_dir in _collect_plan_dir_strings(state_dir):
        try:
            return plans.canonicalize_path(raw_plan_dir)
        except OSError, ValueError:
            continue
    return None


def find_run_locations(
    run_id: str,
    *,
    state_dir: str | Path | None = None,
    plan_dir: str | Path | None = None,
) -> list[RunLocation]:
    """Return every plan state directory that holds a run directory for ``run_id``.

    Run IDs are minted per plan, so the corpus is the only thing that can say
    which plan a bare run ID belongs to. Passing ``plan_dir`` restricts the scan
    to that one plan, which is how an ID that several plans happen to share is
    disambiguated. Discovery is read-only, and a run ID that could name anything
    other than a single directory entry matches nothing rather than escaping the
    state root.
    """
    if not run_id or run_id in {".", ".."} or "/" in run_id or os.sep in run_id:
        return []
    root = state_root(state_dir)
    if plan_dir is not None:
        candidates = [plan_state_dir(plan_dir, state_dir)]
    else:
        candidates = list(iter_plan_state_dirs(root))
    located: list[RunLocation] = []
    for candidate in candidates:
        try:
            if not (candidate / RUNS_DIR_NAME / run_id).is_dir():
                continue
        except OSError:
            continue
        located.append(
            RunLocation(
                run_id=run_id,
                state_dir=candidate,
                plan_dir=(
                    plans.canonicalize_path(plan_dir)
                    if plan_dir is not None
                    else _recorded_plan_dir(candidate)
                ),
            )
        )
    return located


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _coerce_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except TypeError, ValueError:
        return None
    return pid if pid > 0 else None


@contextmanager
def _plan_lock_mutex(
    lock_path: Path, *, wait_seconds: float | None = None
) -> Iterator[bool]:
    """Hold the per-plan exclusive claim on mutating the lock file.

    Reclaim, takeover, publication, unlock and release all replace the lock
    file, so none of them can be ordered by the lock file itself: a validated
    payload says nothing about what occupies that path one syscall later. This
    sidecar is never unlinked, so an ``flock`` on it is a stable identity every
    mutator can queue behind. Yields ``False`` when the claim could not be
    taken within ``wait_seconds`` so each caller picks its own degradation;
    ``None`` reads :data:`LOCK_MUTEX_WAIT_SECONDS` at call time, so a test can
    observe a contended claim without holding the real bound.
    """
    mutex_path = lock_path.parent / LOCK_MUTEX_FILE_NAME
    bound = LOCK_MUTEX_WAIT_SECONDS if wait_seconds is None else wait_seconds
    fd: int | None = None
    held = False
    try:
        fd = os.open(mutex_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        fd = None
    if fd is not None:
        deadline = time.monotonic() + max(bound, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_MUTEX_POLL_SECONDS)
    try:
        yield held
    finally:
        if fd is not None:
            if held:
                with suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _lock_mutex_unavailable(lock_path: Path) -> str:
    return (
        f"could not claim the lock mutex at "
        f"{lock_path.parent / LOCK_MUTEX_FILE_NAME} within "
        f"{LOCK_MUTEX_WAIT_SECONDS:g}s; another process is mutating this plan's lock"
    )


def _lock_mutex_error(lock_path: Path) -> StateError:
    return StateError(_lock_mutex_unavailable(lock_path))


def _reclaim_dead_local_lock(lock_path: Path, expected_raw: dict[str, Any]) -> None:
    """Remove a lock this host has just proven its owner cannot be holding.

    The proof is re-established under the mutex and the file is unlinked before
    it is released, so no takeover or publication can land in between. Without
    that ordering two readers can both validate the same dead payload and the
    slower one deletes whatever replaced it, leaving two runners on one plan.
    Best-effort throughout — hygiene must never break a read path, so a
    contended mutex leaves the lock for the next reader instead of waiting it
    out or removing it unserialized.
    """
    reclaimed = False
    with _plan_lock_mutex(lock_path) as held:
        if held and _read_json_object_safe(lock_path) == expected_raw:
            pid = _coerce_pid(expected_raw.get("pid"))
            if pid is not None and not is_pid_running(pid):
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                else:
                    reclaimed = True
    if reclaimed:
        print(
            f"agent-session-loop: reclaimed stale lock at {lock_path} "
            f"(pid {expected_raw.get('pid')} is not running on this host)",
            file=sys.stderr,
        )


def _read_lock_path(
    lock_path: Path, expected_plan_dir: Path, *, reclaim: bool = True
) -> LockInfo | None:
    if not lock_path.exists():
        return None

    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return LockInfo(
            path=lock_path,
            pid=None,
            plan_dir="",
            created_at="",
            stale=True,
            reason=f"invalid lock file: {error}",
            raw={},
        )

    if not isinstance(raw, dict):
        raw = {}

    pid = _coerce_pid(raw.get("pid"))
    plan_dir = str(raw.get("plan_dir", ""))
    created_at = str(raw.get("created_at", ""))
    hostname = _nonempty_str(raw.get("hostname"))
    stale = False
    reason = "active"

    if plan_dir and plans.canonicalize_path(plan_dir) != expected_plan_dir:
        stale = True
        reason = "lock plan directory does not match requested plan"
    elif pid is None:
        stale = True
        reason = "lock has no valid pid"
    elif hostname is not None and hostname != socket.gethostname():
        # The local process table describes local pids only, so it can neither
        # confirm nor refute this owner. Stale here means "not usable by this
        # host", never "dead".
        stale = True
        reason = f"lock was recorded on host {hostname}"
    elif not is_pid_running(pid):
        stale = True
        reason = f"pid {pid} is not running"
        if reclaim and hostname is not None:
            # Reaching here with a hostname means it is this host's, so the
            # local process table is the one that owns the pid and its absence
            # is proof. A legacy lock carrying no hostname names no process
            # table at all and keeps needing a deliberate `asl unlock`.
            _reclaim_dead_local_lock(lock_path, raw)

    return LockInfo(
        path=lock_path,
        pid=pid,
        plan_dir=plan_dir,
        created_at=created_at,
        stale=stale,
        reason=reason,
        raw=raw,
    )


def read_lock_info(
    plan_dir: str | Path, state_dir: str | Path | None = None
) -> LockInfo | None:
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    return _read_lock_path(
        lock_path_for_plan(canonical_plan_dir, state_dir), canonical_plan_dir
    )


def inspect_lock_info(
    plan_dir: str | Path, state_dir: str | Path | None = None
) -> LockInfo | None:
    """Classify a plan's lock without ever removing it.

    :func:`read_lock_info` reclaims a lock this host has proven dead, which is
    the hygiene most commands want. A caller that has promised to write nothing
    — `asl gc` in either mode — needs the classification without the side
    effect, so that its projection is a function of the corpus rather than of
    the order in which it happened to read.
    """
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    return _read_lock_path(
        lock_path_for_plan(canonical_plan_dir, state_dir),
        canonical_plan_dir,
        reclaim=False,
    )


def _sorted_repository_roots(repository_roots: Sequence[str]) -> list[str]:
    return sorted({str(root) for root in repository_roots if str(root)})


def _publish_lock_payload(lock_path: Path, payload: dict[str, Any]) -> None:
    """Make the lock file appear complete or not appear at all.

    Creating the file and then writing it leaves a window where a reader sees
    zero bytes, which classifies as unparseable garbage — the same shape a
    takeover treats as reclaimable. Renaming a fully written sibling onto the
    path removes the window: the only states an unsynchronized reader can
    observe are absent and complete.
    """
    temp_path = lock_path.with_name(f"{lock_path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            json.dump(payload, lock_file, sort_keys=True)
            lock_file.write("\n")
        os.replace(temp_path, lock_path)
    except BaseException:
        with suppress(OSError):
            temp_path.unlink()
        raise


def acquire_lock(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
    *,
    owner: str = "agent-session-loop",
    repository_roots: Sequence[str] = (),
    wakeup_endpoint: str | Path | None = None,
) -> LockInfo:
    """Create a lock for a plan, removing stale locks this host can judge.

    A lock recorded on another host is refused rather than removed: nothing
    here can tell a dead remote runner from a live one.

    The takeover and the publication run under the per-plan mutex, so a reader
    that proved the old lock dead cannot unlink the replacement published here.

    ``repository_roots`` are the repositories this run claims. They are written
    with the lock rather than with the run's first ``state.json``, because
    everything between those two writes is time in which another starter can
    see a live run and no claim at all.

    ``wakeup_endpoint`` is the FIFO this run already reads control wakeups
    from. It is recorded rather than derived so that a producer writes only to
    an endpoint the owner had open before this lock existed; ``None`` records
    no endpoint and leaves that run on its poll interval.
    """
    snapshot = plans.inspect_plan(plan_dir)
    lock_path = lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 3,
        "owner": owner,
        "pid": os.getpid(),
        # A pid only identifies a process on the host that recorded it, so
        # readers that reason about liveness need to know which host that was.
        "hostname": socket.gethostname(),
        # Birth time of that pid, for readers describing the owner: liveness
        # alone cannot tell this runner from a later process that reused its
        # pid. `None` where `ps` reports no start time.
        "process_start": process_tree.process_birth(os.getpid()),
        # Endpoint this run is already reading wakeups from, so a producer
        # writes to a descriptor this process holds rather than addressing it
        # by pid. `None` leaves that run on its durable queue poll.
        "wakeup_endpoint": (
            str(wakeup_endpoint) if wakeup_endpoint is not None else None
        ),
        "plan_dir": str(snapshot.plan_dir),
        "plan_path_hash": snapshot.path_hash,
        "plan_hash": snapshot.plan_hash,
        "repositories": _sorted_repository_roots(repository_roots),
        "created_at": datetime.now(UTC).isoformat(),
    }

    stolen_lock: LockInfo | None = None
    with _plan_lock_mutex(lock_path) as held:
        if not held:
            raise _lock_mutex_error(lock_path)
        for _attempt in range(2):
            # Reading with `reclaim=False`: the mutex is already held, and this
            # path does its own proven removal below. Every mutator of the lock
            # path holds that mutex, so this read is as exclusive as the
            # `O_EXCL` create it replaces, and it leaves the publication free
            # to land the payload in a single rename.
            existing = _read_lock_path(lock_path, snapshot.plan_dir, reclaim=False)
            if existing is not None:
                if existing.stale and not existing.foreign_host:
                    stolen_lock = existing
                    with suppress(FileNotFoundError):
                        lock_path.unlink()
                    continue
                if existing.foreign_host:
                    raise StateError(
                        f"plan is locked by pid {existing.pid} on host "
                        f"{existing.owner_hostname} at {lock_path}; this host cannot "
                        "tell whether that runner is alive — clear it with "
                        "'asl unlock' once you have confirmed it is gone"
                    )
                raise StateError(f"plan is locked by pid {existing.pid} at {lock_path}")

            _publish_lock_payload(lock_path, payload)
            lock_info = _read_lock_path(lock_path, snapshot.plan_dir, reclaim=False)
            if lock_info is None:
                raise StateError(f"created lock disappeared: {lock_path}")
            if stolen_lock is not None:
                mark_run_abandoned(
                    lock_path.parent,
                    lock_info=stolen_lock,
                    trigger=ABANDONED_TRIGGER_LOCK_TAKEOVER,
                )
            return lock_info

    raise StateError(f"could not acquire lock at {lock_path}")


def release_lock(plan_dir: str | Path, state_dir: str | Path | None = None) -> None:
    """Remove this run's lock, but never without holding the per-plan mutex.

    A timed-out claim means another process is mutating this path right now, so
    the file here at the end of the wait may already be a replacement owner's
    live lock rather than this run's record. Fail closed and leave it: release
    runs in teardown, where raising would replace a finished run's result with
    an error, and the lock left behind names a pid on this host — the next read
    reclaims it once this process exits.
    """
    lock_path = lock_path_for_plan(plan_dir, state_dir)
    with _plan_lock_mutex(lock_path) as held:
        if not held:
            print(
                f"agent-session-loop: left the lock at {lock_path} in place — "
                f"{_lock_mutex_unavailable(lock_path)}. The next read reclaims "
                "it once this process exits.",
                file=sys.stderr,
            )
            return
        try:
            lock_path.unlink()
        except FileNotFoundError:
            return


def unlock_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> UnlockResult:
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    lock_path = lock_path_for_plan(canonical_plan_dir, state_dir)

    with _plan_lock_mutex(lock_path) as held:
        # `reclaim=False` under the mutex: the removal below is this call's own
        # job and its report has to describe the lock it actually removed.
        lock_info = _read_lock_path(lock_path, canonical_plan_dir, reclaim=False)

        if lock_info is None:
            # Nothing to serialize, and a plan that never ran has no state
            # directory to hold the sidecar in the first place.
            return UnlockResult(
                path=lock_path,
                had_lock=False,
                removed=False,
                was_active=False,
                was_stale=False,
                message=f"no lock present at {lock_path}",
            )

        if not held:
            raise _lock_mutex_error(lock_path)

        if lock_info.active and not force:
            raise StateError(
                f"active lock held by pid {lock_info.pid} at {lock_path}; use --force "
                f"to remove it"
            )

        with suppress(FileNotFoundError):
            lock_path.unlink()

    state_label = "active" if lock_info.active else "stale"
    return UnlockResult(
        path=lock_path,
        had_lock=True,
        removed=True,
        was_active=lock_info.active,
        was_stale=lock_info.stale,
        message=f"removed {state_label} lock at {lock_path}",
    )


def _status_counts(snapshot: plans.PlanSnapshot) -> dict[str, int]:
    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1
    return counts


def _session_number_alias(session_id: str) -> int | None:
    parsed = plans.parse_session_id(session_id)
    return parsed.as_int() if parsed.is_integer else None


def _session_compat_value(session_id: str) -> int | str:
    number = _session_number_alias(session_id)
    return number if number is not None else session_id


def _session_payload(session: plans.SessionStatus) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": session.id,
        "status": session.status,
        "raw_value": session.raw_value,
        "line_number": session.line_number,
        "path": str(session.path),
    }
    number = _session_number_alias(session.id)
    if number is not None:
        payload["number"] = number
    return payload


def _session_label_from_payload(payload: dict[str, Any]) -> str:
    session_id = payload.get("session_id")
    if session_id is not None:
        return str(session_id)
    session = payload.get("session")
    if session is not None:
        return str(session)
    return "unknown"


def _lock_payload(lock_info: LockInfo | None, lock_path: Path) -> dict[str, Any]:
    if lock_info is None:
        return {
            "present": False,
            "active": False,
            "stale": False,
            "path": str(lock_path),
        }
    return {
        "present": True,
        "active": lock_info.active,
        "stale": lock_info.stale,
        "pid": lock_info.pid,
        "plan_dir": lock_info.plan_dir,
        "created_at": lock_info.created_at,
        "reason": lock_info.reason,
        "path": str(lock_info.path),
    }


def _parse_wait_moment(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def waiting_for_reply_ceiling(
    waiting_for_reply: dict[str, Any],
) -> datetime | None:
    """Project the instant a wait runs out of unsupervised budget.

    Records written before the ceiling became a rolling bound carry only a
    static ``hard_deadline`` and are read exactly as they were written. Newer
    records carry the ceiling span too, so the deadline rolls forward with the
    last lease activity the runner observed: while a supervisor keeps renewing,
    the wait has no terminal instant to compare against.
    """
    raw_deadline = waiting_for_reply.get("unsupervised_deadline_at")
    if raw_deadline is None:
        return _parse_wait_moment(waiting_for_reply.get("hard_deadline"))
    deadline = _parse_wait_moment(raw_deadline)
    if deadline is None:
        return None
    ceiling = waiting_for_reply.get("unsupervised_ceiling_seconds")
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, (int, float))
        or not math.isfinite(ceiling)
        or ceiling <= 0
    ):
        return deadline
    last_lease = _parse_wait_moment(waiting_for_reply.get("last_lease_activity_at"))
    if last_lease is None:
        return deadline
    return max(deadline, last_lease + timedelta(seconds=ceiling))


def waiting_for_reply_status(
    waiting_for_reply: dict[str, Any],
    *,
    lock_info: LockInfo | None,
    now: datetime | None = None,
) -> str:
    """Classify whether a persisted wait still has a runner to accept a reply."""
    ceiling_deadline = waiting_for_reply_ceiling(waiting_for_reply)
    if ceiling_deadline is None:
        return WAITING_FOR_REPLY_STATUS_INVALID
    resolved_now = now if now is not None else datetime.now(UTC)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=UTC)
    if ceiling_deadline <= resolved_now:
        return WAITING_FOR_REPLY_STATUS_EXPIRED
    if lock_info is None or not lock_info.active:
        return WAITING_FOR_REPLY_STATUS_EXPIRED
    return WAITING_FOR_REPLY_STATUS_LIVE


def _runner_state_payload(
    state_path: Path,
    *,
    lock_info: LockInfo | None,
) -> dict[str, Any]:
    if not state_path.exists():
        return {
            "present": False,
            "path": str(state_path),
            "error": None,
            "active_turn": None,
            "active_verification": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The file vanished between ``exists()`` and ``read_text()``; treat
        # it as absent rather than malformed so concurrent runner cleanup
        # does not surface a 500 to the Workbench UI.
        return {
            "present": False,
            "path": str(state_path),
            "error": None,
            "active_turn": None,
            "active_verification": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }
    except (OSError, json.JSONDecodeError) as error:
        return {
            "present": True,
            "path": str(state_path),
            "error": str(error),
            "active_turn": None,
            "active_verification": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }

    if not isinstance(raw, dict):
        return {
            "present": True,
            "path": str(state_path),
            "error": "state file did not contain a JSON object",
            "active_turn": None,
            "active_verification": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }

    active_turn = raw.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = None

    active_verification = raw.get("active_verification")
    if not isinstance(active_verification, dict):
        active_verification = None

    abandoned = raw.get("abandoned")
    if not isinstance(abandoned, dict):
        abandoned = None

    waiting_for_reply = raw.get("waiting_for_reply")
    if not isinstance(waiting_for_reply, dict):
        waiting_for_reply = None
    waiting_status = (
        waiting_for_reply_status(waiting_for_reply, lock_info=lock_info)
        if waiting_for_reply is not None
        else None
    )

    # Prefer the pause_policy block the runner persisted alongside the pause;
    # fall back to classifying the reason for older state files that predate it,
    # and leave it absent when the run is not paused.
    runner_pause_policy = raw.get("pause_policy")
    if not isinstance(runner_pause_policy, dict) and raw.get("paused"):
        runner_pause_policy = pause_policy.classify_pause(raw.get("reason"))
    if not isinstance(runner_pause_policy, dict):
        runner_pause_policy = None

    run_config = raw.get("run_config")
    if not isinstance(run_config, dict):
        run_config = {}

    assurance = raw.get("assurance")
    if not isinstance(assurance, dict):
        assurance = None

    return {
        "present": True,
        "path": str(state_path),
        "error": None,
        "run_id": raw.get("run_id"),
        "harness": raw.get("harness"),
        "review_harness": run_config.get("review_harness"),
        "supervisor_audit": run_config.get("supervisor_audit"),
        "assurance": assurance,
        **(
            {
                "cwd": raw.get("cwd"),
                "workspace_root": raw.get("workspace_root"),
                "registry_digest": raw.get("registry_digest"),
                "repositories": raw.get("repositories"),
            }
            if raw.get("workspace_root") is not None
            else {}
        ),
        "ok": raw.get("ok"),
        "paused": raw.get("paused"),
        "reason": raw.get("reason"),
        "pause_policy": runner_pause_policy,
        "run_started_at": raw.get("run_started_at"),
        "run_finished_at": raw.get("run_finished_at"),
        "duration_seconds": raw.get("duration_seconds"),
        "active_turn": active_turn,
        "active_verification": active_verification,
        "waiting_for_reply": waiting_for_reply,
        "waiting_for_reply_status": waiting_status,
        "abandoned": abandoned,
    }


def _mailbox_status_payload(
    plan_dir: Path,
    state_dir: str | Path | None,
    active_run_id: str | None = None,
) -> dict[str, Any]:
    """Project the append-only mailbox and lease without mutating either file."""
    # Local import avoids a module cycle: mailbox path resolution depends on
    # this module's state-root helpers, while status only needs read-only views.
    from . import mailbox

    mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
    lease_path = mailbox.lease_path_for_plan(plan_dir, state_dir)
    mailbox_present = mailbox_path.exists()
    lease_present = lease_path.exists()
    payload: dict[str, Any] = {
        "present": mailbox_present,
        "active": False,
        "path": str(mailbox_path),
        "lease_present": lease_present,
        "lease_path": str(lease_path),
        "size_bytes": 0,
        "max_bytes": mailbox.MAILBOX_MAX_BYTES,
        "full": False,
        "accepts_questions": True,
        "accepts_replies": True,
        "accepts_resolutions": True,
        "last_seq": 0,
        "pending_guidance": 0,
        "unanswered_questions": 0,
        "open_questions": 0,
        "resolutions": 0,
        "notices": 0,
        "supervision_ends": 0,
        "rejected_records": 0,
        "error": None,
        "lease_updated_at": None,
        "lease_fresh": False,
        "lease_reason": None,
        "lease_error": None,
    }

    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except mailbox.MailboxError as error:
        # read_mailbox only returns an absent view for FileNotFoundError, so an
        # exception means something exists at the path but could not be read.
        payload["present"] = True
        payload["size_bytes"] = None
        payload["full"] = None
        payload["accepts_questions"] = None
        payload["accepts_replies"] = None
        payload["accepts_resolutions"] = None
        payload["last_seq"] = None
        payload["pending_guidance"] = None
        payload["unanswered_questions"] = None
        payload["open_questions"] = None
        payload["resolutions"] = None
        payload["notices"] = None
        payload["supervision_ends"] = None
        payload["rejected_records"] = None
        payload["error"] = str(error)
    else:
        counts = view.counts
        payload.update(
            {
                "present": (
                    mailbox_present or mailbox_path.exists() or view.size_bytes > 0
                ),
                "size_bytes": view.size_bytes,
                "full": view.full,
                "accepts_questions": view.accepts_questions,
                "accepts_replies": view.accepts_replies,
                "accepts_resolutions": view.accepts_resolutions,
                "last_seq": view.last_seq,
                "pending_guidance": counts.pending_guidance,
                "unanswered_questions": counts.unanswered_questions,
                "open_questions": len(view.open_questions(active_run_id)),
                "resolutions": counts.resolutions,
                "notices": counts.notices,
                "supervision_ends": counts.supervision_ends,
                "rejected_records": counts.rejected,
            }
        )
        if view.rejected:
            first = view.rejected[0]
            noun = "record" if len(view.rejected) == 1 else "records"
            payload["error"] = (
                f"{len(view.rejected)} rejected mailbox {noun}; "
                f"first at seq {first.seq}: {first.reason}"
            )

    try:
        lease = mailbox.read_lease(plan_dir, state_dir=state_dir)
    except mailbox.MailboxError as error:
        payload["lease_present"] = True
        payload["lease_reason"] = error.reason
        payload["lease_error"] = str(error)
    else:
        lease_present = lease.reason != mailbox.LEASE_MISSING
        payload["lease_present"] = lease_present
        payload["lease_updated_at"] = lease.updated_at
        payload["lease_fresh"] = lease.fresh
        payload["lease_reason"] = (
            None if lease.reason == mailbox.LEASE_MISSING else lease.reason
        )
        if lease.reason not in (
            None,
            mailbox.LEASE_MISSING,
            mailbox.LEASE_EXPIRED,
        ):
            payload["lease_error"] = lease.reason

    payload["active"] = bool(payload["present"] or payload["lease_fresh"])
    return payload


def _progress_journal_payload(plan_dir: Path) -> dict[str, Any]:
    """Summarize the runner-managed `progress.md` journal for `asl status`.

    Shape stays stable across the three states a journal can be in so JSON
    consumers can branch cleanly: absent (`present=False`), readable
    (`entry_count`/`last_appended` populated), or present-but-malformed
    (`error` set, counts None).
    """
    journal_path = plans.progress_path(plan_dir)
    if not journal_path.exists():
        return {
            "present": False,
            "path": str(journal_path),
            "entry_count": None,
            "last_appended": None,
            "error": None,
        }
    try:
        entries = plans.read_progress_entries(plan_dir)
    except plans.PlanError as error:
        return {
            "present": True,
            "path": str(journal_path),
            "entry_count": None,
            "last_appended": None,
            "error": str(error),
        }
    last_appended = entries[-1].completed_at if entries else None
    return {
        "present": True,
        "path": str(journal_path),
        "entry_count": len(entries),
        "last_appended": last_appended,
        "error": None,
    }


def _stale_runner_state(
    runner_state: dict[str, Any],
    lock_info: LockInfo | None,
) -> tuple[bool, str | None]:
    """Detect unfinished `state.json` that no active lock is guarding.

    Returns ``(stale_unfinished, stale_reason)``. The pair is ``(False, None)``
    when the runner state is absent, unreadable, already finished, or still
    held by an active lock. When an active lock is missing but the persisted
    state never set ``run_finished_at``, surface a short, human-readable
    reason so the operator can see the orphan in both JSON and text output.
    """
    if not runner_state.get("present"):
        return False, None
    if runner_state.get("error"):
        return False, None
    if runner_state.get("run_finished_at"):
        return False, None
    if lock_info is not None and lock_info.active:
        return False, None
    if lock_info is None:
        return True, "runner state has not finished but no lock file is present"
    return (
        True,
        f"runner state has not finished but lock is stale: {lock_info.reason}",
    )


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a ``Z`` suffix and naive input."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _unfinished_run_id(state_obj: dict[str, Any] | None) -> str | None:
    """Return the run id of a state that recorded a start but never a finish."""
    if not isinstance(state_obj, dict):
        return None
    if state_obj.get("run_finished_at") is not None:
        return None
    return _nonempty_str(state_obj.get("run_id"))


def _abandonment_is_proven(
    state_obj: dict[str, Any],
    *,
    lock_path: Path,
    lock_info: LockInfo | None,
) -> bool:
    """Decide whether the run that wrote ``state_obj`` is provably dead.

    Deliberately narrow, because the write it authorizes is irreversible: a
    stale lock with no usable pid (wrong plan directory, unparseable payload)
    proves nothing, and a pid recorded on another host says nothing about this
    host's process table. Holding the lock is not itself proof either — the
    caller that just took a stale lock over still has to prove the previous
    owner is dead.
    """
    if lock_info is None:
        started_at = _parse_timestamp(state_obj.get("run_started_at"))
        if started_at is None:
            return False
        age_seconds = (datetime.now(UTC) - started_at).total_seconds()
        if age_seconds < ABANDONED_NO_LOCK_GRACE_SECONDS:
            return False
        # A booting runner writes state.json before its lock, so re-read the
        # lock rather than trusting a snapshot taken before that gap closed.
        return not lock_path.exists()
    if lock_info.active or lock_info.pid is None or lock_info.foreign_host:
        return False
    return not is_pid_running(lock_info.pid)


def _abandoned_event_payload(
    payload: dict[str, Any],
    *,
    state_path: Path,
    events_path: Path,
) -> dict[str, Any]:
    """Build the synthetic ``run_finished`` payload for an abandoned run.

    A strict subset of :func:`runner._run_finished_event_payload` so existing
    consumers keep reading the same field names, plus the two keys that mark
    the record as reconstructed rather than observed.
    """
    counts: dict[str, Any] | None = None
    final_snapshot = payload.get("final_snapshot")
    if isinstance(final_snapshot, dict):
        snapshot_counts = final_snapshot.get("counts")
        if isinstance(snapshot_counts, dict):
            counts = snapshot_counts
    return {
        "run_id": payload.get("run_id"),
        "run_dir": payload.get("run_dir"),
        "ok": payload.get("ok"),
        "paused": payload.get("paused"),
        "reason": payload.get("reason"),
        "run_started_at": payload.get("run_started_at"),
        "run_finished_at": payload.get("run_finished_at"),
        "duration_seconds": payload.get("duration_seconds"),
        "counts": counts,
        "completed_sessions": payload.get("completed_sessions"),
        "completed_session_ids": payload.get("completed_session_ids"),
        "state_path": str(state_path),
        "events_path": str(events_path),
        "abandoned": payload.get("abandoned"),
        "assurance_pending": payload.get("assurance_pending"),
        "synthetic": True,
        "outcome": events.RUN_OUTCOME_ABANDONED,
    }


def _assurance_skip_reason(state_obj: dict[str, Any]) -> str | None:
    """Say why catch-up must not audit this run, or ``None`` when it should.

    A run that recorded no assurance object at all predates auditing entirely,
    and silently auditing it would flood the shared inbox with history nobody
    asked about. A run that already reached a verdict, or opted out, has
    already been decided. Only a run that was eligible and still owed an audit
    is catch-up's to run.
    """
    assurance = state_obj.get("assurance")
    if not isinstance(assurance, dict):
        return ASSURANCE_SKIP_PREDATES
    if not assurance.get("eligible"):
        return ASSURANCE_SKIP_SETTLED
    if assurance.get("state") not in _ASSURANCE_OWED_STATES:
        return ASSURANCE_SKIP_SETTLED
    return None


def _assurance_pending_record(
    state_obj: dict[str, Any],
    *,
    plan_state_dir_path: Path,
    run_id: str,
    trigger: str,
    detected_at: str,
) -> dict[str, Any]:
    """Describe the audit an abandoned run still owes, self-contained.

    Everything an audit needs is copied in rather than referenced, because the
    next run of this plan rewrites ``state.json`` and would otherwise take the
    dead run's harness selection and workspace binding with it.
    """
    raw_config = state_obj.get("run_config")
    run_config = raw_config if isinstance(raw_config, dict) else {}
    # A workspace run binds its root explicitly; a legacy single-repository run
    # only ever recorded the working directory it audited from.
    workspace_root = next(
        (
            value
            for value in (
                _nonempty_str(state_obj.get("workspace_root")),
                _nonempty_str(run_config.get("workspace_root")),
                _nonempty_str(state_obj.get("cwd")),
                _nonempty_str(run_config.get("cwd")),
            )
            if value is not None
        ),
        None,
    )
    return {
        "schema_version": ASSURANCE_PENDING_SCHEMA_VERSION,
        "run_id": run_id,
        "recorded_at": detected_at,
        "trigger": trigger,
        "outcome": events.RUN_OUTCOME_ABANDONED,
        "skip": _assurance_skip_reason(state_obj),
        "plan_dir": _nonempty_str(state_obj.get("plan_dir")),
        "state_dir": str(plan_state_dir_path),
        "run_dir": str(plan_state_dir_path / RUNS_DIR_NAME / run_id),
        "workspace_root": workspace_root,
        "run_config": run_config or None,
    }


def _write_assurance_pending_record(
    plan_state_dir_path: Path,
    payload: dict[str, Any],
    *,
    run_id: str,
    trigger: str,
    detected_at: str,
) -> dict[str, Any] | None:
    """Write the pending-audit record beside the abandoned run's artifacts.

    Best effort like everything else on this path: an unwritable run directory
    costs the catch-up audit, not the terminal record that proves the run
    ended. Returns the record so the same facts ride the terminal state and
    event rather than being derived twice.
    """
    record = _assurance_pending_record(
        payload,
        plan_state_dir_path=plan_state_dir_path,
        run_id=run_id,
        trigger=trigger,
        detected_at=detected_at,
    )
    try:
        run_dir = Path(record["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts.atomic_write_bytes(
            run_dir / ASSURANCE_PENDING_FILE_NAME,
            json.dumps(record, sort_keys=True).encode("utf-8") + b"\n",
            durable=True,
        )
    except OSError:
        return None
    return record


@contextmanager
def _abandonment_claim(plan_state_dir_path: Path) -> Iterator[bool]:
    """Hold the per-plan exclusive claim on terminalizing an abandoned run.

    ``flock`` is held by the open file description rather than the process, so
    this excludes concurrent threads as well as concurrent processes, and the
    kernel drops it if the holder dies mid-write. Failing to take it yields
    ``False`` instead of waiting: the claim is only ever held for the two writes
    below, so contention means another caller is already publishing the terminal
    record, and blocking would put an unbounded wait inside ``asl status`` and
    lock acquisition for no gain.
    """
    claim_path = plan_state_dir_path / ABANDON_CLAIM_FILE_NAME
    fd: int | None = None
    held = False
    try:
        fd = os.open(claim_path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError:
        pass
    try:
        yield held
    finally:
        if fd is not None:
            if held:
                with suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def mark_run_abandoned(
    plan_state: str | Path,
    *,
    lock_info: LockInfo | None,
    trigger: str,
) -> bool:
    """Give a run that died mid-turn the terminal record it never wrote.

    A killed runner leaves ``run_finished_at`` null forever, so every consumer
    that waits for a terminal event — ``asl tail --follow``, the supervisor,
    state-based readiness — waits forever. This reconstructs that ending, but
    only once death is proven; see :func:`_abandonment_is_proven`.

    Returns whether the terminal state was written — exactly one caller ever
    gets ``True`` for a given run, including under concurrency, because the
    recheck and both writes run under :func:`_abandonment_claim`. Best-effort
    throughout and never raises: hygiene must not be able to break lock
    acquisition or ``asl status``, both of which call it on paths that must
    keep working.
    """
    try:
        plan_state_dir_path = Path(plan_state)
        state_path = plan_state_dir_path / events.STATE_FILE_NAME
        state_obj = _read_json_object_safe(state_path)
        run_id = _unfinished_run_id(state_obj)
        if run_id is None or state_obj is None:
            return False
        if not _abandonment_is_proven(
            state_obj,
            lock_path=plan_state_dir_path / LOCK_FILE_NAME,
            lock_info=lock_info,
        ):
            return False

        # Everything from the final recheck through the event append runs under
        # one claim. Without it two callers can both pass the recheck and each
        # publish a terminal record; a caller that cannot take the claim is by
        # definition racing one that already holds it, so it declines.
        with _abandonment_claim(plan_state_dir_path) as claimed:
            if not claimed:
                return False

            # Re-read immediately before writing. A runner that finished
            # between the first read and now owns the terminal record, and
            # idempotence depends on losing that race instead of overwriting
            # the winner.
            current = _read_json_object_safe(state_path)
            if current is None or _unfinished_run_id(current) != run_id:
                return False

            detected_at = datetime.now(UTC).isoformat()
            payload = dict(current)
            payload.update(
                {
                    "ok": False,
                    "reason": ABANDONED_REASON,
                    "run_finished_at": detected_at,
                    # The same two fields ``runner._finish_run`` clears. A wait
                    # record is deliberately left behind: it is the only
                    # evidence of what the run died holding, and it already
                    # reads as expired once no active lock backs it.
                    "active_turn": None,
                    "active_verification": None,
                    "abandoned": {
                        "detected_at": detected_at,
                        "trigger": trigger,
                        "lock_reason": (
                            lock_info.reason if lock_info is not None else None
                        ),
                        "detected_by_pid": os.getpid(),
                    },
                }
            )
            # The audit this run never ran is recorded, never launched: this
            # code path runs inside `asl status` and inside lock acquisition,
            # and neither may block on a harness.
            record = _write_assurance_pending_record(
                plan_state_dir_path,
                payload,
                run_id=run_id,
                trigger=trigger,
                detected_at=detected_at,
            )
            if record is not None:
                payload["assurance_pending"] = {
                    "run_id": run_id,
                    "recorded_at": detected_at,
                    "skip": record["skip"],
                    "path": str(Path(record["run_dir"]) / ASSURANCE_PENDING_FILE_NAME),
                }
            events_path = plan_state_dir_path / events.EVENTS_FILE_NAME
            artifacts.atomic_write_bytes(
                state_path, _dump_state_bytes(payload), durable=True
            )
            # The terminal state is already durable; an unwritable events
            # file must not downgrade that to a failure the caller retries.
            with contextlib.suppress(OSError):
                events.append_event(
                    events_path,
                    events.Event(
                        name=events.RUN_FINISHED_EVENT,
                        payload=_abandoned_event_payload(
                            payload,
                            state_path=state_path,
                            events_path=events_path,
                        ),
                    ),
                )
            return True
    except Exception:
        # Death has already cost the caller its run; refusing to record that
        # is strictly better than raising into a lock acquire or a status read.
        return False


def _live_run_id(
    runner_state: dict[str, Any],
    lock_info: LockInfo | None,
) -> str | None:
    """Return the run id of a process that can still consume a mailbox answer.

    Stricter than :func:`_stale_runner_state`, which reports a *finished* run as
    non-stale: a finished or paused run is equally unable to act on a reply, so
    its questions are history rather than a live block.
    """
    run_id = runner_state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    if not runner_state.get("present") or runner_state.get("error"):
        return None
    if runner_state.get("paused") or runner_state.get("run_finished_at"):
        return None
    if lock_info is None or not lock_info.active:
        return None
    return run_id


def _migrate_command_text(
    plan_dir: str,
    from_plan_dir: str,
    state_dir: str | Path | None,
) -> str:
    """Build the ``asl migrate-state`` command that relocates orphaned state."""
    parts = ["asl", "migrate-state", plan_dir, "--from", from_plan_dir]
    if state_dir:
        parts.extend(["--state-dir", str(state_dir)])
    return shlex.join(parts)


def orphaned_state_candidates(
    snapshot: plans.PlanSnapshot,
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Find moved-plan state directories whose content matches this plan.

    When this plan's own state directory already holds ``state.json`` the scan
    is skipped (the common case) and an empty list is returned. Otherwise every
    per-plan state directory under the state root is inspected for a recorded
    ``plan_dir`` that no longer exists on disk whose recorded content hash
    matches the current plan content hash; each match is a migration candidate.
    The recorded hash prefers ``final_snapshot.content_sha256`` (a completed run
    mutates handoffs, so the moved plan matches its final content) and falls
    back to ``initial_snapshot.content_sha256`` only for legacy state. Discovery
    is read-only and tolerates malformed or concurrently removed records.
    """
    root = state_root(state_dir)
    plan_state = plan_state_dir(snapshot.plan_dir, state_dir)
    if (plan_state / events.STATE_FILE_NAME).exists():
        return []
    try:
        state_dirs = list(iter_plan_state_dirs(root))
    except StateError:
        return []
    candidates: list[dict[str, Any]] = []
    for candidate_dir in state_dirs:
        if candidate_dir == plan_state:
            continue
        state_obj = _read_json_object_safe(candidate_dir / events.STATE_FILE_NAME)
        if state_obj is None:
            continue
        recorded_plan_dir = _nonempty_str(state_obj.get("plan_dir"))
        if recorded_plan_dir is None:
            continue
        recorded_hash = _recorded_content_hash(state_obj)
        if recorded_hash != snapshot.content_sha256:
            continue
        try:
            still_exists = plans.canonicalize_path(recorded_plan_dir).is_dir()
        except OSError, ValueError:
            still_exists = False
        if still_exists:
            continue
        candidates.append(
            {
                "old_plan_dir": recorded_plan_dir,
                "state_dir": str(candidate_dir),
                "run_finished_at": state_obj.get("run_finished_at"),
                "migrate_command": _migrate_command_text(
                    str(snapshot.plan_dir), recorded_plan_dir, state_dir
                ),
            }
        )
    candidates.sort(key=lambda item: item["state_dir"])
    return candidates


def _next_command(snapshot: plans.PlanSnapshot) -> dict[str, Any]:
    """Describe what to run next for ``asl status``.

    When a pending session remains, returns per-harness ``session-run``
    invocations under the shared ``next_steps`` envelope, ordered by best-effort
    invoking-harness detection so the most likely command surfaces first. When
    every session is complete, returns the exact ``{"complete": true,
    "message": ...}`` payload whose message matches what ``session-run`` prints.
    """
    next_pending = snapshot.next_pending
    if next_pending is None:
        return {
            "complete": True,
            "message": responses.ALL_SESSIONS_COMPLETE_MESSAGE,
        }
    step = responses.invocation_step(
        f"Implement pending session {next_pending.id} with session-run",
        harnesses.skill_invocations("session-run", f"{snapshot.plan_dir}/"),
    )
    return {
        "complete": False,
        "next_pending_id": next_pending.id,
        **responses.next_steps_payload([step]),
    }


def status_payload(
    snapshot: plans.PlanSnapshot,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    # Local imports avoid module cycles: repair planning depends on state for
    # its mutating materializer, and the assurance ledger depends on state for
    # its root, while this status path only uses their read-only projections
    # after state has finished importing. Neither can launch an audit.
    from . import assurance_report, review_continuation

    plan_state = plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    event_path = plan_state / events.EVENTS_FILE_NAME
    lock_path = lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = _read_lock_path(lock_path, snapshot.plan_dir)
    next_pending = snapshot.next_pending
    runner_state = _runner_state_payload(state_path, lock_info=lock_info)
    stale_unfinished, stale_reason = _stale_runner_state(runner_state, lock_info)
    if stale_unfinished and mark_run_abandoned(
        plan_state,
        lock_info=lock_info,
        trigger=ABANDONED_TRIGGER_STATUS,
    ):
        # Recompute so this same call reports the terminal state it just wrote
        # instead of the orphan it found; this is also the only backfill path
        # for runs that died before abandonment tracking existed.
        runner_state = _runner_state_payload(state_path, lock_info=lock_info)
        stale_unfinished, stale_reason = _stale_runner_state(runner_state, lock_info)
    runner_state["stale_unfinished"] = stale_unfinished
    runner_state["stale_reason"] = stale_reason
    mailbox_status = _mailbox_status_payload(
        snapshot.plan_dir,
        state_dir,
        _live_run_id(runner_state, lock_info),
    )
    progress_journal = _progress_journal_payload(snapshot.plan_dir)
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, event_path
    ).to_payload()
    run_id = runner_state.get("run_id")
    assurance_projection_payload = assurance_report.build_run_report(
        runner_state.get("assurance"),
        run_dir=(
            plan_state / RUNS_DIR_NAME / str(run_id)
            if isinstance(run_id, str) and run_id
            else None
        ),
        state_dir=state_dir,
    )
    workspace_fields = {
        key: runner_state.get(key)
        for key in ("workspace_root", "registry_digest", "repositories")
        if runner_state.get(key) is not None
    }
    if not workspace_fields:
        registry = plans.read_repository_registry(snapshot.plan_dir)
        metadata = plans.load_plan_metadata(snapshot.plan_dir)
        if registry is not None and isinstance(metadata, plans.WorkspacePlanMetadata):
            workspace_root = plans.canonicalize_path(metadata.workspace_root)
            workspace_fields = {
                "workspace_root": str(workspace_root),
                "registry_digest": metadata.registry_digest,
                "repositories": [
                    {
                        "alias": repository.alias,
                        "path": repository.path,
                        "origin": repository.origin,
                        "resolved_root": str(
                            plans.canonicalize_path(workspace_root / repository.path)
                        ),
                    }
                    for repository in registry.repositories
                ],
            }
    cwd_fields = {"cwd": workspace_fields["workspace_root"]} if workspace_fields else {}
    return responses.success_payload(
        "status",
        plan_dir=str(snapshot.plan_dir),
        context_path=str(snapshot.context_path),
        manifest_path=str(snapshot.manifest_path),
        content_sha256=snapshot.content_sha256,
        path_hash=snapshot.path_hash,
        plan_hash=snapshot.plan_hash,
        state_dir=str(plan_state),
        state_path=str(state_path),
        events_path=str(event_path),
        runner_state=runner_state,
        spark=runner_state.get("spark"),
        active_turn=runner_state.get("active_turn"),
        active_verification=runner_state.get("active_verification"),
        waiting_for_reply=runner_state.get("waiting_for_reply"),
        waiting_for_reply_status=runner_state.get("waiting_for_reply_status"),
        mailbox=mailbox_status,
        counts=_status_counts(snapshot),
        next_pending=(
            _session_compat_value(next_pending.id) if next_pending is not None else None
        ),
        next_pending_id=next_pending.id if next_pending is not None else None,
        sessions=[_session_payload(session) for session in snapshot.sessions],
        lock=_lock_payload(lock_info, lock_path),
        progress_journal=progress_journal,
        review_resume=review_resume,
        assurance=runner_state.get("assurance"),
        assurance_report=assurance_projection_payload,
        orphaned_state_candidates=orphaned_state_candidates(snapshot, state_dir),
        next_command=_next_command(snapshot),
        **cwd_fields,
        **workspace_fields,
    )


def _print_text_status(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    print(f"Plan hash: {payload['plan_hash']}")
    print(f"Content SHA-256: {payload['content_sha256']}")
    print(f"State dir: {payload['state_dir']}")
    print(f"State file: {payload['state_path']}")
    print(f"Events: {payload['events_path']}")
    spark = payload.get("spark")
    if isinstance(spark, dict):
        enabled = "enabled" if spark.get("enabled") else "disabled"
        line = (
            f"Spark: {enabled}; route "
            f"{(spark.get('active_route') or {}).get('decision', 'none')}; "
            f"availability {spark.get('reason', 'unknown')}"
        )
        if spark.get("retry_at"):
            line += f" until {spark['retry_at']}"
        route = spark.get("active_route")
        if isinstance(route, dict) and route.get("fallback_reason"):
            line += f"; fallback {route['fallback_reason']}"
        if isinstance(route, dict) and route.get("suppression_cause"):
            line += f"; suppression {route['suppression_cause']}"
        print(line)
        if spark.get("warning"):
            print(f"Spark warning: {spark['warning']}")
    if payload.get("workspace_root") is not None:
        print(f"Workspace root: {payload['workspace_root']}")
        print(f"Registry digest: {payload.get('registry_digest')}")
        repositories = payload.get("repositories")
        if isinstance(repositories, list):
            print("Repositories:")
            visible, omitted = workspaces.bounded_repository_rows(repositories)
            for repository in visible:
                if isinstance(repository, dict):
                    print(
                        f"  {repository.get('alias')}: "
                        f"{repository.get('resolved_root')}"
                    )
            if omitted:
                print(f"  ... {omitted} more repositories")

    lock = payload["lock"]
    if not lock["present"]:
        print("Lock: none")
    elif lock["active"]:
        print(f"Lock: active pid {lock['pid']} ({lock['path']})")
    else:
        print(f"Lock: stale ({lock['reason']}) ({lock['path']})")

    runner_state = payload.get("runner_state", {})
    if runner_state.get("present") and runner_state.get("error"):
        print(f"Run state: unreadable ({runner_state['error']})")
    elif runner_state.get("present"):
        run_id = runner_state.get("run_id") or "unknown"
        run_started_at = runner_state.get("run_started_at") or "unknown"
        print(f"Run state: run {run_id} started {run_started_at}")
        if runner_state.get("stale_unfinished"):
            stale_reason = runner_state.get("stale_reason") or "stale unfinished"
            print(f"Run state: stale/orphaned ({stale_reason})")
        abandoned = runner_state.get("abandoned")
        if isinstance(abandoned, dict):
            detected_at = abandoned.get("detected_at") or "unknown"
            trigger = abandoned.get("trigger") or "unknown"
            lock_reason = abandoned.get("lock_reason") or "no lock file"
            print(
                f"Run state: abandoned (detected {detected_at} "
                f"by {trigger}: {lock_reason})"
            )

    # Reported, never produced: this prints what the last run's audit already
    # recorded and what the shared inbox already holds.
    from . import assurance_report

    audit_line = assurance_report.status_line(
        payload.get("runner_state", {}).get("assurance"),
        report=payload.get("assurance_report"),
    )
    if audit_line:
        print(audit_line)
    for line in assurance_report.report_lines(payload.get("assurance_report")):
        print(line)

    mailbox_status = payload.get("mailbox")
    if isinstance(mailbox_status, dict):
        mailbox_error = mailbox_status.get("error")
        if mailbox_error:
            print(f"Mailbox: unreadable/malformed ({mailbox_error})")
        elif mailbox_status.get("active"):
            full_suffix = ", full" if mailbox_status.get("full") else ""
            # Non-closure writes stop before the shared closure reserve. Report
            # reply and resolution capacity independently because their minimum
            # valid record sizes differ.
            if not full_suffix and mailbox_status.get("accepts_questions") is False:
                accepts_replies = mailbox_status.get("accepts_replies") is True
                accepts_resolutions = mailbox_status.get("accepts_resolutions") is True
                if accepts_replies and accepts_resolutions:
                    full_suffix = (
                        ", non-closure writes refused "
                        "(replies and resolutions still accepted)"
                    )
                elif accepts_replies:
                    full_suffix = (
                        ", non-closure writes refused (only replies still accepted)"
                    )
                else:
                    full_suffix = (
                        ", non-closure writes refused "
                        "(reply and resolution capacity exhausted)"
                    )
            open_questions = mailbox_status.get("open_questions")
            unanswered = mailbox_status.get("unanswered_questions")
            # Disclose only the historical remainder beyond what the live run
            # can act on, so stale records read as history at a glance.
            history_suffix = (
                f" ({unanswered - open_questions} unanswered historical)"
                if isinstance(open_questions, int)
                and isinstance(unanswered, int)
                and unanswered > open_questions
                else ""
            )
            print(
                "Mailbox: active"
                f"{full_suffix}, seq {mailbox_status.get('last_seq')}, "
                f"{mailbox_status.get('pending_guidance')} pending guidance, "
                f"{open_questions} open questions{history_suffix}, "
                f"{mailbox_status.get('resolutions')} resolutions, "
                f"{mailbox_status.get('notices')} notices, "
                f"{mailbox_status.get('supervision_ends')} supervision ends, "
                f"{mailbox_status.get('size_bytes')}/"
                f"{mailbox_status.get('max_bytes')} bytes"
            )
        elif mailbox_status.get("lease_present"):
            print("Mailbox: inactive (supervisor lease is not fresh)")
        else:
            print("Mailbox: disabled")

        if mailbox_status.get("lease_error"):
            print(
                f"Mailbox lease: malformed/unreadable ({mailbox_status['lease_error']})"
            )
        elif mailbox_status.get("lease_fresh"):
            print(
                "Mailbox lease: fresh, updated "
                f"{mailbox_status.get('lease_updated_at') or 'unknown'}"
            )
        elif mailbox_status.get("lease_present"):
            reason = mailbox_status.get("lease_reason") or "stale"
            updated_at = mailbox_status.get("lease_updated_at") or "unknown"
            print(f"Mailbox lease: stale ({reason}), updated {updated_at}")
        else:
            print("Mailbox lease: absent")

    progress_journal = payload.get("progress_journal")
    if isinstance(progress_journal, dict):
        if not progress_journal.get("present"):
            print("Progress journal: not initialized")
        elif progress_journal.get("error"):
            print(f"Progress journal: unreadable ({progress_journal['error']})")
        else:
            entry_count = progress_journal.get("entry_count") or 0
            last_appended = progress_journal.get("last_appended")
            entry_word = "entry" if entry_count == 1 else "entries"
            if last_appended:
                print(
                    f"Progress journal: {entry_count} {entry_word}, "
                    f"last appended {last_appended}"
                )
            else:
                print(f"Progress journal: {entry_count} {entry_word}")

    active_turn = payload.get("active_turn")
    if isinstance(active_turn, dict):
        print(
            "Active turn: "
            f"session {_session_label_from_payload(active_turn)} "
            f"turn {active_turn.get('turn_index')} "
            f"elapsed {format_duration_seconds(active_turn.get('elapsed_seconds'))} "
            f"idle {format_duration_seconds(active_turn.get('idle_seconds'))}"
        )
        print(f"  Last activity: {active_turn.get('last_activity_at') or 'none'}")
        action_required = active_turn.get("action_required")
        if isinstance(action_required, dict):
            print(
                "  Action required: "
                f"{action_required.get('reason')} "
                f"({action_required.get('source')})"
            )
        print(f"  Stdout: {active_turn.get('stdout_path')}")
        print(f"  Stderr: {active_turn.get('stderr_path')}")
        print(f"  Last message: {active_turn.get('last_message_path')}")
        print(f"  Result: {active_turn.get('result_path')}")

    active_verification = payload.get("active_verification")
    if isinstance(active_verification, dict):
        check_count = active_verification.get("check_count")
        check_index = active_verification.get("current_check_index")
        if check_index is None:
            check_position = f"-/{check_count}" if check_count is not None else "-"
        else:
            check_position = (
                f"{int(check_index) + 1}/{check_count}"
                if check_count is not None
                else str(int(check_index) + 1)
            )
        check_name = active_verification.get("current_check_name") or "pending"
        check_elapsed = format_duration_seconds(
            active_verification.get("check_elapsed_seconds")
        )
        print(
            "Active verification: "
            f"session {_session_label_from_payload(active_verification)} "
            f"turn {active_verification.get('turn_index')} "
            f"check {check_position} {check_name} "
            f"elapsed "
            f"{format_duration_seconds(active_verification.get('elapsed_seconds'))} "
            f"check elapsed {check_elapsed}"
        )
        check_kind = active_verification.get("current_check_kind")
        if check_kind:
            print(f"  Check kind: {check_kind}")
        heartbeat_count = active_verification.get("heartbeat_count")
        if heartbeat_count is not None:
            print(f"  Heartbeats: {heartbeat_count}")

    waiting_for_reply = payload.get("waiting_for_reply")
    if isinstance(waiting_for_reply, dict):
        waiting_status = payload.get("waiting_for_reply_status") or "unknown"
        question_id = waiting_for_reply.get("question_id") or "unknown"
        print(
            "Waiting for reply: "
            f"question {question_id} "
            f"session {_session_label_from_payload(waiting_for_reply)} "
            f"turn {waiting_for_reply.get('turn_index')} "
            f"since {waiting_for_reply.get('started_at') or 'unknown'}"
        )
        base_deadline = waiting_for_reply.get("base_deadline")
        ceiling_deadline = waiting_for_reply.get("unsupervised_deadline_at")
        ceiling_label = "unsupervised"
        if ceiling_deadline is None:
            ceiling_deadline = waiting_for_reply.get("hard_deadline")
            ceiling_label = "hard"
        if base_deadline or ceiling_deadline:
            print(
                "  Deadlines: "
                f"base {base_deadline or 'unknown'}, "
                f"{ceiling_label} {ceiling_deadline or 'unknown'}"
            )
        print(f"  Status: {waiting_status}")

    print("Sessions:")
    for session in payload["sessions"]:
        session_id = session.get("id", session.get("number", "unknown"))
        print(f"  {session_id}: {session['status']} {Path(session['path']).name}")

    if payload["next_pending"] is None:
        print("Next pending: none")
    else:
        next_pending = payload.get("next_pending_id") or payload["next_pending"]
        print(f"Next pending: session {next_pending}")

    next_command = payload.get("next_command")
    if isinstance(next_command, dict):
        if next_command.get("complete"):
            print(f"Next command: {next_command.get('message')}")
        else:
            for step in next_command.get("next_steps", []):
                for harness_name, command in (step.get("invocations") or {}).items():
                    print(f"Next command ({harness_name}): {command}")

    orphaned = payload.get("orphaned_state_candidates")
    if orphaned:
        count = len(orphaned)
        plural = "y" if count == 1 else "ies"
        print(f"Orphaned state: {count} candidate director{plural} from a moved plan")
        for candidate in orphaned:
            print(
                f"  {candidate['state_dir']} "
                f"(old plan {candidate['old_plan_dir']}); "
                f"migrate with: {candidate['migrate_command']}"
            )


def status_from_args(args: argparse.Namespace) -> int:
    """Show parsed plan and runner status from CLI arguments.

    ``args.plan_dir`` (advertised as ``PLAN_DIR_OR_ID``) may be either an
    existing plan directory or a bare compact plan label printed after ``plan``
    by ``asl sessions``. Labels are resolved through the state corpus before
    inspection; direct paths always take precedence and never trigger a lookup.
    """
    try:
        resolved_plan_dir = resolve_plan_reference(
            args.plan_dir, state_root(args.state_dir)
        )
        snapshot = plans.inspect_plan(resolved_plan_dir)
        payload = status_payload(snapshot, args.state_dir)
    except AmbiguousPlanError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "status",
                    reason="ambiguous-plan",
                    error=str(error),
                    paused=False,
                    details={
                        "state_root": str(error.state_root),
                        "candidates": [
                            str(candidate) for candidate in error.candidates
                        ],
                    },
                    plan_reference=str(args.plan_dir),
                )
            )
        else:
            print(
                f"agent-session-loop status: {error}; "
                "pass a full plan directory path to disambiguate",
                file=sys.stderr,
            )
        return 1
    except (plans.PlanError, StateError) as error:
        if args.json:
            reason = (
                "malformed-plan"
                if isinstance(error, plans.PlanError)
                else "state-error"
            )
            responses.print_json(
                responses.error_payload(
                    "status",
                    reason=reason,
                    error=str(error),
                    paused=False,
                    plan_reference=str(args.plan_dir),
                )
            )
        else:
            print(f"agent-session-loop status: {error}", file=sys.stderr)
        return 1

    if args.json:
        responses.print_json(payload)
    else:
        _print_text_status(payload)
    return 0


def _unlock_payload(
    result: UnlockResult,
    *,
    plan_dir: str | Path,
    state_dir: str | Path | None,
) -> dict[str, Any]:
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    return responses.success_payload(
        "unlock",
        reason="ok",
        plan_dir=str(canonical_plan_dir),
        state_dir=str(plan_state_dir(canonical_plan_dir, state_dir)),
        lock_path=str(result.path),
        had_lock=result.had_lock,
        removed=result.removed,
        active=result.was_active,
        stale=result.was_stale,
        message=result.message,
    )


def _unlock_error_reason(error: Exception) -> str:
    if isinstance(error, plans.PlanError):
        return "malformed-plan"
    if isinstance(error, OSError):
        return "os-error"
    if "active lock" in str(error).lower():
        return "active-lock"
    return "state-error"


def _unlock_error_details(args: argparse.Namespace) -> dict[str, Any]:
    try:
        canonical_plan_dir = plans.canonicalize_path(args.plan_dir)
        return {
            "plan_dir": str(canonical_plan_dir),
            "lock_path": str(lock_path_for_plan(canonical_plan_dir, args.state_dir)),
        }
    except plans.PlanError, OSError:
        return {"plan_dir": str(args.plan_dir)}


def unlock_from_args(args: argparse.Namespace) -> int:
    """Remove a plan lock from CLI arguments."""
    try:
        result = unlock_plan(args.plan_dir, args.state_dir, force=args.force)
    except (plans.PlanError, StateError, OSError) as error:
        if args.json:
            details = _unlock_error_details(args)
            responses.print_json(
                responses.error_payload(
                    "unlock",
                    reason=_unlock_error_reason(error),
                    error=str(error),
                    paused=False,
                    details=details,
                    plan_dir=details.get("plan_dir", str(args.plan_dir)),
                    lock_path=details.get("lock_path"),
                )
            )
        else:
            print(f"agent-session-loop unlock: {error}", file=sys.stderr)
        return 1

    if args.json:
        responses.print_json(
            _unlock_payload(result, plan_dir=args.plan_dir, state_dir=args.state_dir)
        )
    else:
        print(result.message)
    return 0


class MigrationError(StateError):
    """Raised when a state-directory migration cannot proceed safely.

    Carries a machine-readable ``reason`` and structured ``details`` so the
    ``migrate-state`` CLI handler can surface an actionable JSON payload while
    ``except StateError`` guards keep working.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


@dataclass(frozen=True)
class MigrationReport:
    """Outcome of migrating one plan's operational state directory."""

    plan_dir: str
    from_plan_dir: str
    state_dir: str
    from_state_dir: str
    applied: bool
    rewritten_fields: tuple[str, ...]
    result_artifacts: int
    unmigrated_artifacts_note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "migrate-state",
            "plan_dir": self.plan_dir,
            "from_plan_dir": self.from_plan_dir,
            "state_dir": self.state_dir,
            "from_state_dir": self.from_state_dir,
            "applied": self.applied,
            "rewritten_fields": list(self.rewritten_fields),
            "result_artifacts": self.result_artifacts,
            "unmigrated_artifacts_note": self.unmigrated_artifacts_note,
        }


def _dump_state_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a state payload the way the runner persists it on disk."""
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def migrate_plan_state(
    plan_dir: str | Path,
    *,
    from_plan_dir: str | Path,
    state_dir: str | Path | None = None,
    apply: bool = False,
) -> MigrationReport:
    """Relocate an orphaned state directory from an old plan path to a new one.

    SHA-of-path keying means moving a plan directory strands its operational
    state under the hash of its old path. This renames that state directory to
    the hash of the current path and rewrites the absolute path fields inside
    ``state.json`` so ``asl status`` and resume work again. Historical per-turn
    ``result.json`` paths are intentionally left untouched and reported via
    ``unmigrated_artifacts_note``. The default is a dry-run; ``apply`` performs
    the rename, rewrite, and ``state_migrated`` event append.

    Refuses (in both dry-run and apply, before touching either directory) on a
    source plan directory that still exists on disk (its state is not orphaned),
    an active source lock, an already-occupied destination, a target that is not
    a valid plan, a source state that does not record the requested old plan, or
    a source whose content hash is incompatible with the target plan. The
    content check prefers ``final_snapshot.content_sha256`` and falls back to
    ``initial_snapshot.content_sha256`` only for legacy state.
    """
    # Inspecting the target validates it is a real plan (raising PlanError,
    # surfaced as ``malformed-plan``) and yields the content hash the source
    # state must be compatible with.
    target_snapshot = plans.inspect_plan(plan_dir)
    new_plan_dir = target_snapshot.plan_dir
    old_plan_dir = plans.canonicalize_path(from_plan_dir)
    old_state_dir = plan_state_dir(old_plan_dir, state_dir)
    new_state_dir = plan_state_dir(new_plan_dir, state_dir)

    if old_state_dir == new_state_dir:
        raise MigrationError(
            "noop-migration",
            f"source and destination state directories are identical: {old_state_dir}",
            {"state_dir": str(old_state_dir)},
        )

    old_state_path = old_state_dir / events.STATE_FILE_NAME
    if not old_state_dir.is_dir() or not old_state_path.is_file():
        raise MigrationError(
            "no-source-state",
            f"no source state to migrate at {old_state_dir}",
            {
                "from_state_dir": str(old_state_dir),
                "from_state_path": str(old_state_path),
            },
        )

    # Refuse when the source plan directory still exists on disk: its state is
    # not orphaned, so migrating would steal a live plan's operational state.
    # Discovery already skips still-existing source plans; this keeps the
    # mutating API consistent with read-only discovery. Two identical plans
    # otherwise satisfy the identity and content checks below, and --apply would
    # rename the source's state dir out from under it.
    try:
        source_plan_exists = old_plan_dir.is_dir()
    except OSError:
        source_plan_exists = False
    if source_plan_exists:
        raise MigrationError(
            "source-plan-exists",
            f"source plan directory still exists and is not orphaned: {old_plan_dir}",
            {"from_plan_dir": str(old_plan_dir)},
        )

    # Refuse to migrate a live run out from under itself.
    old_lock = _read_lock_path(old_state_dir / LOCK_FILE_NAME, old_plan_dir)
    if old_lock is not None and old_lock.active:
        raise MigrationError(
            "active-lock",
            f"source plan is locked by pid {old_lock.pid} at {old_lock.path}",
            {"lock_path": str(old_lock.path), "pid": old_lock.pid},
        )

    if new_state_dir.exists():
        raise MigrationError(
            "destination-occupied",
            f"destination state directory already exists: {new_state_dir}",
            {"state_dir": str(new_state_dir)},
        )

    state_obj = _read_json_object_safe(old_state_path)
    if state_obj is None:
        raise MigrationError(
            "malformed-source-state",
            f"source state file is not a JSON object: {old_state_path}",
            {"from_state_path": str(old_state_path)},
        )

    # Confirm the source state truly belongs to the requested old plan, so a
    # mistaken --from cannot attach unrelated operational state to this target.
    recorded_plan_dir = _nonempty_str(state_obj.get("plan_dir"))
    recorded_matches = False
    if recorded_plan_dir is not None:
        try:
            recorded_matches = (
                plans.canonicalize_path(recorded_plan_dir) == old_plan_dir
            )
        except OSError, ValueError:
            recorded_matches = False
    if not recorded_matches:
        raise MigrationError(
            "source-plan-mismatch",
            "source state records plan directory "
            f"{recorded_plan_dir!r}, not the requested {old_plan_dir}",
            {
                "recorded_plan_dir": recorded_plan_dir,
                "from_plan_dir": str(old_plan_dir),
            },
        )

    # Confirm the source content belongs to the target plan (final snapshot
    # preferred, initial snapshot as the legacy fallback).
    recorded_hash = _recorded_content_hash(state_obj)
    if recorded_hash != target_snapshot.content_sha256:
        raise MigrationError(
            "content-mismatch",
            "source state content hash "
            f"{recorded_hash!r} does not match target plan content hash "
            f"{target_snapshot.content_sha256}",
            {
                "recorded_content_sha256": recorded_hash,
                "target_content_sha256": target_snapshot.content_sha256,
            },
        )

    # Count historical per-turn result.json artifacts; their embedded paths
    # point at the old plan directory and are intentionally left unrewritten.
    result_artifacts = sum(1 for _ in _iter_result_paths(old_state_dir / RUNS_DIR_NAME))
    unmigrated_note = None
    if result_artifacts:
        plural = "" if result_artifacts == 1 else "s"
        unmigrated_note = (
            f"{result_artifacts} per-turn result.json artifact{plural} under "
            f"runs/ retain references to the old plan directory "
            f"{old_plan_dir}; they are historical and were not rewritten."
        )

    rewritten_fields = tuple(
        field for field in _MIGRATION_PATH_FIELDS if field in state_obj
    )

    if not apply:
        return MigrationReport(
            plan_dir=str(new_plan_dir),
            from_plan_dir=str(old_plan_dir),
            state_dir=str(new_state_dir),
            from_state_dir=str(old_state_dir),
            applied=False,
            rewritten_fields=rewritten_fields,
            result_artifacts=result_artifacts,
            unmigrated_artifacts_note=unmigrated_note,
        )

    new_state_dir.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old_state_dir, new_state_dir)

    new_state_path = new_state_dir / events.STATE_FILE_NAME
    new_events_path = new_state_dir / events.EVENTS_FILE_NAME
    migrated = _read_json_object_safe(new_state_path)
    if migrated is None:
        migrated = state_obj
    field_targets = {
        "plan_dir": str(new_plan_dir),
        "state_dir": str(new_state_dir),
        "state_path": str(new_state_path),
        "events_path": str(new_events_path),
        "event_path": str(new_events_path),
        "lock_path": str(new_state_dir / LOCK_FILE_NAME),
    }
    for field, value in field_targets.items():
        if field in migrated:
            migrated[field] = value
    artifacts.atomic_write_bytes(
        new_state_path, _dump_state_bytes(migrated), durable=True
    )

    events.append_event(
        new_events_path,
        events.Event(
            STATE_MIGRATED_EVENT,
            {
                "from_plan_dir": str(old_plan_dir),
                "to_plan_dir": str(new_plan_dir),
                "from_state_dir": str(old_state_dir),
                "to_state_dir": str(new_state_dir),
            },
        ),
    )

    return MigrationReport(
        plan_dir=str(new_plan_dir),
        from_plan_dir=str(old_plan_dir),
        state_dir=str(new_state_dir),
        from_state_dir=str(old_state_dir),
        applied=True,
        rewritten_fields=rewritten_fields,
        result_artifacts=result_artifacts,
        unmigrated_artifacts_note=unmigrated_note,
    )


def _print_migration_report(report: MigrationReport) -> None:
    mode = "applied" if report.applied else "dry-run"
    print(f"Migrate state ({mode})")
    print(f"From plan: {report.from_plan_dir}")
    print(f"To plan:   {report.plan_dir}")
    print(f"From state dir: {report.from_state_dir}")
    print(f"To state dir:   {report.state_dir}")
    if report.rewritten_fields:
        print(f"Rewritten fields: {', '.join(report.rewritten_fields)}")
    if report.unmigrated_artifacts_note:
        print(report.unmigrated_artifacts_note)
    if not report.applied:
        print("Re-run with --apply to migrate the state directory.")


def _migrate_state_error_reason(error: Exception) -> str:
    if isinstance(error, MigrationError):
        return error.reason
    if isinstance(error, plans.PlanError):
        return "malformed-plan"
    if isinstance(error, OSError):
        return "os-error"
    return "state-error"


def migrate_state_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl migrate-state``."""
    try:
        report = migrate_plan_state(
            args.plan_dir,
            from_plan_dir=args.from_plan_dir,
            state_dir=getattr(args, "state_dir", None),
            apply=args.apply,
        )
    except (MigrationError, plans.PlanError, StateError, OSError) as error:
        if getattr(args, "json", False):
            responses.print_json(
                responses.error_payload(
                    "migrate-state",
                    reason=_migrate_state_error_reason(error),
                    error=str(error),
                    paused=False,
                    details=getattr(error, "details", {}),
                    plan_dir=str(args.plan_dir),
                    from_plan_dir=str(args.from_plan_dir),
                )
            )
        else:
            print(f"agent-session-loop migrate-state: {error}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        responses.print_json(report.to_dict())
    else:
        _print_migration_report(report)
    return 0
