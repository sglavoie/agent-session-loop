"""Plan lock acquisition, inspection, release and manual unlock."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    durability,
    plans_files,
    plans_snapshot,
    process_sampling,
    process_tree,
    state,
)
from .state import (
    LockInfo,
    StateError,
    UnlockResult,
    _coerce_pid,
    _nonempty_str,
    lock_path_for_plan,
)
from .state_abandonment import ABANDONED_TRIGGER_LOCK_TAKEOVER, mark_run_abandoned
from .state_plan_refs import _read_json_object_safe

# Sidecar `flock` target that serializes every mutation of the plan lock. The
# lock file itself cannot play this role: it is created and unlinked, so two
# callers holding that path can be looking at different files.
LOCK_MUTEX_FILE_NAME = "lock.mutex"


# How long a lock mutation waits for that mutex. Each critical section is a
# read plus one create or unlink, so contention past this bound means a holder
# is wedged rather than busy.
LOCK_MUTEX_WAIT_SECONDS = 5.0


LOCK_MUTEX_POLL_SECONDS = 0.01


class LockMutexOpenError(StateError):
    """The lock mutex sidecar could not be opened, as opposed to contended."""


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

    Raises :class:`LockMutexOpenError` when the sidecar cannot be opened at all
    (``EACCES``, ``EROFS``, ``ENOSPC``, a missing state directory). That is not
    contention, and reporting it as "another process is mutating this plan's
    lock" would send the operator looking for a process that does not exist.
    """
    mutex_path = lock_path.parent / LOCK_MUTEX_FILE_NAME
    bound = LOCK_MUTEX_WAIT_SECONDS if wait_seconds is None else wait_seconds
    held = False
    try:
        fd = os.open(mutex_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as error:
        raise LockMutexOpenError(
            f"could not open the lock mutex at {mutex_path}: {error}"
        ) from error
    # Guarded from the open on: an interrupt during the poll sleep, or between
    # a successful flock and the yield, must still close the descriptor, and
    # closing it is what drops a lock that ``held`` never got to record.
    try:
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
        yield held
    finally:
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
    try:
        with _plan_lock_mutex(lock_path) as held:
            if held and _read_json_object_safe(lock_path) == expected_raw:
                pid = _coerce_pid(expected_raw.get("pid"))
                if pid is not None and _owner_gone_reason(pid, expected_raw):
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    else:
                        reclaimed = True
    except LockMutexOpenError:
        return
    if reclaimed:
        print(
            f"agent-session-loop: reclaimed stale lock at {lock_path} "
            f"(pid {expected_raw.get('pid')} no longer holds it on this host)",
            file=sys.stderr,
        )


def _owner_gone_reason(pid: int, raw: dict[str, Any]) -> str | None:
    """Say why this host's ``pid`` cannot be the lock's owner, or ``None``.

    A live pid alone is not the owner: after a SIGKILL or a reboot the pid can
    be reused by an unrelated process, which would hold the plan locked
    forever. A recorded ``process_start`` that differs from the live
    process's is that proof; an unknown start time on either side, or one
    that differs only by a time-zone offset, proves nothing and leaves the
    owner alive.
    """
    if not state.is_pid_running(pid):
        return f"pid {pid} is not running"
    recorded = raw.get("process_start")
    if not isinstance(recorded, str) or not recorded:
        return None
    current = process_tree.process_birth(pid)
    # A start time recorded before births were pinned to UTC can differ from
    # the live one by its writer's time-zone offset alone; that proves nothing.
    if current is None or process_sampling.birth_may_match(recorded, current):
        return None
    return f"pid {pid} was reused by a process started at {current}"


def _read_lock_path(
    lock_path: Path, expected_plan_dir: Path, *, reclaim: bool = True
) -> LockInfo | None:
    if not lock_path.exists():
        return None

    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        # ValueError covers both JSONDecodeError and the UnicodeDecodeError
        # that bytes which are not UTF-8 raise before any JSON is parsed;
        # RecursionError is what nesting too deep for the decoder raises.
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

    if plan_dir and plans_files.canonicalize_path(plan_dir) != expected_plan_dir:
        stale = True
        reason = "lock plan directory does not match requested plan"
    elif pid is None:
        stale = True
        reason = "lock has no valid pid"
    elif state.recorded_on_foreign_host(
        hostname, pid=pid, process_start=raw.get("process_start")
    ):
        # The local process table describes local pids only, so it can neither
        # confirm nor refute this owner. Stale here means "not usable by this
        # host", never "dead".
        stale = True
        reason = f"lock was recorded on host {hostname}"
    elif (gone := _owner_gone_reason(pid, raw)) is not None:
        stale = True
        reason = gone
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
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
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
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
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
    durability.replace_text(
        lock_path, json.dumps(payload, sort_keys=True) + "\n", mode=0o644
    )


def acquire_lock(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
    *,
    owner: str = "agent-session-loop",
    repository_roots: Sequence[str] = (),
    wakeup_endpoint: str | Path | None = None,
    redirect_endpoint: str | Path | None = None,
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

    ``redirect_endpoint`` is the same arrangement for the urgent redirect
    channel, on its own FIFO. ``None`` there is stronger than for the control
    endpoint: it does not degrade a redirect to the poll interval, it makes
    redirect submissions against this run refusable, because there is no
    urgent path to deliver them on.
    """
    snapshot = plans_snapshot.inspect_plan(plan_dir)
    lock_path = lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 4,
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
        # Separate endpoint for urgent redirects. A producer reads this to
        # learn that the urgent channel exists at all, which is a different
        # question from whether control wakeups are served.
        "redirect_endpoint": (
            str(redirect_endpoint) if redirect_endpoint is not None else None
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

    The same reasoning applies with the mutex held: after `asl unlock --force`
    and a fresh `asl run`, the file at this path is the new runner's lock, and
    deleting it would leave that run unguarded. Only a lock naming this
    process is removed. A pid plus this host is a sufficient identity here
    without the recorded ``process_start``: this process is alive, so no other
    process on this host can be wearing its pid.
    """
    lock_path = lock_path_for_plan(plan_dir, state_dir)
    try:
        with _plan_lock_mutex(lock_path) as held:
            if not held:
                print(
                    f"agent-session-loop: left the lock at {lock_path} in place — "
                    f"{_lock_mutex_unavailable(lock_path)}. The next read reclaims "
                    "it once this process exits.",
                    file=sys.stderr,
                )
                return
            if not lock_path.exists():
                return
            raw = _read_json_object_safe(lock_path)
            if not _lock_names_this_process(raw):
                owner = raw.get("pid") if raw is not None else None
                print(
                    f"agent-session-loop: left the lock at {lock_path} in place — "
                    f"it belongs to pid {owner}, not this process "
                    f"(pid {os.getpid()})",
                    file=sys.stderr,
                )
                return
            try:
                lock_path.unlink()
            except FileNotFoundError:
                return
    except LockMutexOpenError as error:
        if lock_path.exists():
            print(
                f"agent-session-loop: left the lock at {lock_path} in place — {error}",
                file=sys.stderr,
            )


def _lock_names_this_process(raw: dict[str, Any] | None) -> bool:
    if raw is None or _coerce_pid(raw.get("pid")) != os.getpid():
        return False
    return not state.recorded_on_foreign_host(
        raw.get("hostname"), pid=os.getpid(), process_start=raw.get("process_start")
    )


def _no_lock_result(lock_path: Path) -> UnlockResult:
    return UnlockResult(
        path=lock_path,
        had_lock=False,
        removed=False,
        was_active=False,
        was_stale=False,
        message=f"no lock present at {lock_path}",
    )


def unlock_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> UnlockResult:
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    lock_path = lock_path_for_plan(canonical_plan_dir, state_dir)

    try:
        with _plan_lock_mutex(lock_path) as held:
            # `reclaim=False` under the mutex: the removal below is this call's
            # own job and its report has to describe the lock it actually
            # removed.
            lock_info = _read_lock_path(lock_path, canonical_plan_dir, reclaim=False)

            if lock_info is None:
                return _no_lock_result(lock_path)

            if not held:
                raise _lock_mutex_error(lock_path)

            if lock_info.active and not force:
                raise StateError(
                    f"active lock held by pid {lock_info.pid} at {lock_path}; use "
                    f"--force to remove it"
                )

            with suppress(FileNotFoundError):
                lock_path.unlink()
    except LockMutexOpenError:
        # A plan that never ran has no state directory to hold the sidecar in
        # the first place, and nothing to unlock. With a lock present, the
        # operator needs the real reason the mutex is unusable.
        if not lock_path.exists():
            return _no_lock_result(lock_path)
        raise

    state_label = "active" if lock_info.active else "stale"
    return UnlockResult(
        path=lock_path,
        had_lock=True,
        removed=True,
        was_active=lock_info.active,
        was_stale=lock_info.stale,
        message=f"removed {state_label} lock at {lock_path}",
    )
