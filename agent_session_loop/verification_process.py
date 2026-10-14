"""Subprocess plumbing for command checks: pipe draining, kills, and sweeps.

Command checks spawn with ``start_new_session=True``, so everything here works
on the child's process group: bounded pipe reads, killing the group, sweeping
descendants that escaped it, and the kill switch a parallel dispatch shares.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from typing import Any

from .artifacts import ArtifactError, StreamCapture
from .process_tree import (
    descendant_pids,
    sample_process_table,
    sweep_enabled,
    sweep_survivors,
)

# Pipe reads are folded into a StreamCapture in chunks of this size, bounding
# the transient memory per read regardless of how much the command emits.
_STREAM_CHUNK_BYTES = 64 * 1024


def _drain_stream(stream: Any, capture: StreamCapture) -> None:
    """Feed a process pipe into ``capture`` chunk by chunk until EOF.

    ``ArtifactError`` is tolerated because a check abandoned after
    :data:`_ABANDONED_READER_JOIN_SECONDS` discards its capture while this
    thread may still be blocked on a pipe some escaped descendant holds open.
    """
    try:
        while True:
            chunk = stream.read1(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            capture.feed(chunk)
    except OSError, ValueError, ArtifactError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class _VerificationCancelled(BaseException):
    """Raised inside a worker whose check an interrupt abandoned.

    Derives from ``BaseException`` so no ``except Exception`` handler can
    mistake it for a check failure. The future carrying it is never read: the
    interrupt that triggered the cancellation is re-raised instead.
    """


# A killed process group closes the inherited pipes, so the reader threads see
# EOF at once. This bound only exists for a descendant that escaped the group
# (by calling `setsid` itself): such a process is never stopped, so this caps
# what an abandoned check pays to walk away from it.
_ABANDONED_READER_JOIN_SECONDS = 5.0

# A check that exited on its own is not killed, so nothing closes the pipes an
# escaped descendant inherited and the join for it would otherwise never
# return — the 2h20m hang class. The bound is generous because the common case
# is a same-group straggler still flushing legitimate output, and truncating
# that would lose the evidence the check was run for.
_EXITED_READER_JOIN_SECONDS = 30.0


def _sweep_process_table() -> dict[int, dict[str, Any]] | None:
    """The table to snapshot descendants from, or ``None`` when sweeping is off."""
    if not sweep_enabled():
        return None
    return sample_process_table()


def _snapshot_descendants(
    root_pid: int, table: Mapping[int, Mapping[str, Any]] | None
) -> tuple[Mapping[str, Any], ...]:
    """Record ``root_pid``'s descendants while it is still alive.

    Killing the group reparents an escapee to pid 1, after which no descendant
    walk can reach it. A snapshot taken beforehand is the sweep's only route to
    one, so this must run before the kill, not after it.
    """
    if table is None:
        return ()
    return tuple(table[pid] for pid in descendant_pids(root_pid, table))


def _sweep_check_descendants(
    process: subprocess.Popen,
    prior_snapshot: tuple[Mapping[str, Any], ...],
) -> dict[str, Any] | None:
    """Kill whatever outlived the check, or ``None`` when the sweep is off.

    ``expected_pgid`` is the child's PID because command checks spawn with
    ``start_new_session=True``. ``root_pid`` is already reaped by the caller, so
    the sweep excludes it rather than signalling a PID that may be reused.
    """
    if not sweep_enabled():
        return None
    return sweep_survivors(
        root_pid=process.pid,
        expected_pgid=process.pid,
        prior_snapshot=prior_snapshot,
    )


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill ``process`` and every descendant still in its process group.

    Command checks are spawned with ``start_new_session=True``, so the child's
    process group ID is its PID and every descendant inherits that group unless
    it starts a session of its own. Killing the group is what stops a check
    that fans out into helper processes; without it the direct child dies while
    its children keep running, hold the captured pipes open, and can still
    write to the repository.

    A descendant that calls ``setsid`` leaves the group and survives this call.
    :func:`_sweep_check_descendants` is what reaches it afterwards, by matching
    the descendants :func:`_snapshot_descendants` recorded before the kill
    against the live process table. That stays racy regardless — a descendant
    can detach or fork between the snapshot and the signal — so callers still
    bound the *cost* of an escapee: see :data:`_ABANDONED_READER_JOIN_SECONDS`
    and :data:`_EXITED_READER_JOIN_SECONDS`.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        # The group is already empty, or the child left it for one of its own.
        with contextlib.suppress(OSError):
            process.kill()


def _kill_and_reap(process: subprocess.Popen) -> None:
    """Kill ``process``'s group and wait for it, tolerating an already-dead child."""
    _kill_process_group(process)
    process.wait()


def _join_readers(
    readers: tuple[threading.Thread, ...], *, timeout: float | None
) -> None:
    """Join the pipe readers, optionally under a shared deadline."""
    if timeout is None:
        for reader in readers:
            reader.join()
        return
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


class _ParallelCancellation:
    """Kill switch shared by the command checks of one parallel dispatch.

    :meth:`cancel` runs on the calling thread once the dispatch has raised: it
    kills every command process still in flight so the workers unblock at once
    and ``run_verification`` can join them before the exception escapes.
    Killing is what makes that join bounded — without it, waiting would stall
    behind the longest check and not waiting would leave it running.

    Because the kill happens here rather than in the owning worker, the
    pre-kill descendant snapshot each worker needs for its sweep can only be
    taken here too; :meth:`snapshot_for` hands it back.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._processes: set[subprocess.Popen] = set()
        self._snapshots: dict[int, tuple[Mapping[str, Any], ...]] = {}

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def register(self, process: subprocess.Popen) -> bool:
        """Track a spawned process; ``False`` when the run is already cancelled."""
        with self._lock:
            if self._cancelled:
                return False
            self._processes.add(process)
            return True

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)

    def snapshot_for(self, process: subprocess.Popen) -> tuple[Mapping[str, Any], ...]:
        """The descendants recorded for ``process`` just before it was killed."""
        with self._lock:
            return self._snapshots.get(process.pid, ())

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            in_flight = tuple(self._processes)
            self._processes.clear()
        # One process table for the whole dispatch, read before any kill lands
        # so every root is still alive and its descendants still reachable.
        table = _sweep_process_table()
        snapshots = {
            process.pid: _snapshot_descendants(process.pid, table)
            for process in in_flight
        }
        with self._lock:
            self._snapshots.update(snapshots)
        for process in in_flight:
            # The owning worker is inside `process.wait()` and reaps it there.
            _kill_process_group(process)
