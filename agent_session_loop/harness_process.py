"""Coordinate abort deadlines, process-group cleanup, and bounded reader joins."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from . import (
    harness_types,
)
from .process_tree import (
    clear_process_tree,
    descendant_pids,
    last_process_tree,
    sample_process_table,
    sweep_enabled,
    sweep_survivors,
)


@dataclass
class AbortSignal:
    """A request to kill one in-flight harness turn at a monotonic deadline.

    Armed from another thread; read by the turn's wait loop. It lives in this
    module because the kill has to happen where the turn's ``process`` is in
    scope: signalling a bare PID from outside would bypass the pre-kill
    descendant snapshot and the post-turn sweep, which is how a descendant that
    left the process group is reached at all.

    Initiation and completion are deliberately two different observations.
    :meth:`arm` records the request and returns; it takes this signal's own leaf
    lock and nothing else, so an initiation can never queue behind a guidance
    transport holding the live-delivery endpoint's lock. Whether the worker
    actually stopped is read afterwards through :meth:`wait_completed`, which
    the turn's owner satisfies only once the process is reaped and its transport
    is shut down.
    """

    event: threading.Event = field(default_factory=threading.Event)
    deadline: float | None = None
    reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _completed: threading.Event = field(default_factory=threading.Event, repr=False)
    _stage: harness_types.CancelStage | None = None
    _turn_over: bool = False

    def arm(self, reason: str, *, grace_seconds: float = 0.0) -> None:
        """Request the kill, giving the harness ``grace_seconds`` to wind down.

        Returns as soon as the request is recorded, whatever the turn is doing.
        A request that arrives after the turn is already over cannot revive it:
        it is recorded, staged as ``shutdown``, and completed on the spot, so a
        caller waiting on it is answered rather than left waiting for a kill
        that will never be issued.
        """
        with self._lock:
            self.deadline = time.monotonic() + max(0.0, grace_seconds)
            self.reason = reason
            already_over = self._turn_over
            if already_over and self._stage is None:
                self._stage = harness_types.CANCEL_STAGE_SHUTDOWN
        self.event.set()
        if already_over:
            self._completed.set()

    @property
    def armed(self) -> bool:
        return self.event.is_set()

    def expires_at(self, now: float) -> float | None:
        """When the kill is due, or ``None`` while this signal is unarmed.

        An armed signal carrying no deadline is due immediately, so a caller
        that only sets the event still gets a kill.
        """
        if not self.event.is_set():
            return None
        with self._lock:
            deadline = self.deadline
        return now if deadline is None else deadline

    def is_due(self, now: float | None = None) -> bool:
        """Whether the kill is due, rather than merely requested.

        A request inside its grace period is not due: the grace exists so the
        harness gets to wind down and possibly finish on its own, and a turn
        that finishes inside it was never aborted. Every lifecycle checkpoint
        asks this rather than :attr:`armed`, so an unexpired request stops
        nothing that the wait loop would have let run.
        """
        moment = time.monotonic() if now is None else now
        deadline = self.expires_at(moment)
        return deadline is not None and moment >= deadline

    def begin_turn(self) -> None:
        """Bind this signal to a turn that is about to start.

        A signal belongs to one turn, but the codex adapter runs a second turn
        on the same one when an app-server handshake falls back to ``codex
        exec``. An unarmed signal therefore starts each turn with no stage and
        no completion; an armed one keeps both, because that request predates
        the turn and the spawn gate is about to refuse it rather than run it.
        """
        with self._lock:
            self._turn_over = False
            if not self.event.is_set():
                self._stage = None
                self._completed.clear()
            elif self._stage == harness_types.CANCEL_STAGE_SHUTDOWN:
                # That stage belongs to the turn that just ended, not to the
                # one starting now; this turn names its own window.
                self._stage = None

    def observed(self, stage: harness_types.CancelStage) -> None:
        """Name the window this request was acted on in. First naming wins."""
        with self._lock:
            if self._stage is None:
                self._stage = stage

    @property
    def cancel_stage(self) -> harness_types.CancelStage | None:
        """The window the request was acted on in, or ``None`` if it never was."""
        with self._lock:
            return self._stage

    def release(self) -> None:
        """Report that the turn this signal governs has stopped.

        Called once by the boundary that owns the process, after the process is
        reaped and its transport is shut down, so completion means the worker is
        gone rather than that a kill was merely issued. It also closes the
        signal: any later :meth:`arm` completes immediately instead of arming a
        turn that no longer exists.
        """
        with self._lock:
            self._turn_over = True
            if self.event.is_set() and self._stage is None:
                self._stage = harness_types.CANCEL_STAGE_SHUTDOWN
        self._completed.set()

    @property
    def completed(self) -> bool:
        """Whether the turn this signal governs has stopped."""
        return self._completed.is_set()

    def wait_completed(self, timeout: float | None = None) -> bool:
        """Wait out the turn, reporting whether it stopped within ``timeout``.

        The only blocking call on this signal, and the counterpart to
        :meth:`arm`: cancellation is initiated without waiting for anything and
        observed here, so the caller decides how long it is willing to wait for
        the worker to be gone.
        """
        return self._completed.wait(timeout)


def _wait_for_turn_exit(
    process: subprocess.Popen,
    *,
    timeout: float | None,
    abort: AbortSignal | None,
) -> Literal["timeout", "abort"] | None:
    """Wait for ``process``; name the deadline that fired, or ``None`` if it exited.

    The two deadlines are mutually exclusive: the earlier one is reported and
    the other is never looked at again. Without an abort signal this is the
    single blocking wait it replaced, so an uncancellable turn wakes no more
    often than it used to.
    """
    if abort is None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout"
        return None

    timeout_deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            return None
        now = time.monotonic()
        abort_deadline = abort.expires_at(now)
        abort_due = abort_deadline is not None and now >= abort_deadline
        timeout_due = timeout_deadline is not None and now >= timeout_deadline
        if (
            abort_deadline is not None
            and timeout_deadline is not None
            and abort_due
            and timeout_due
        ):
            return "timeout" if timeout_deadline <= abort_deadline else "abort"
        if abort_due:
            return "abort"
        if timeout_due:
            return "timeout"
        interval = harness_types._ABORT_POLL_INTERVAL_SECONDS
        if abort_deadline is not None:
            interval = min(interval, abort_deadline - now)
        if timeout_deadline is not None:
            interval = min(interval, timeout_deadline - now)
        try:
            process.wait(timeout=max(interval, 0.0))
        except subprocess.TimeoutExpired:
            continue
        return None


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


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill ``process`` and every descendant still in its process group.

    Harness turns spawn with ``start_new_session=True``, so the child's process
    group ID is its PID and every descendant inherits that group unless it
    starts a session of its own. Killing only the direct child leaves a harness
    that fanned out into helper processes running, still holding the captured
    pipes open (mirrors :func:`verification_process._kill_process_group`).
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        # The group is already empty, or the child left it for one of its own.
        with contextlib.suppress(OSError):
            process.kill()


def _sweep_turn_descendants(
    process: subprocess.Popen,
    prior_snapshot: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any] | None:
    """Kill whatever outlived the turn, or ``None`` when the sweep is off.

    ``expected_pgid`` is the child's PID because turns spawn with
    ``start_new_session=True``. ``root_pid`` is already reaped by the caller, so
    the sweep excludes it rather than signalling a PID that may be reused.

    Any tree the hang sentinel sampled for this same root is unioned into the
    snapshot: on the paths that take no pre-kill snapshot of their own it is
    the sweep's only route to a descendant that left the process group, and it
    exists exactly for the silent turns where such a descendant is suspected.
    It is consumed here, so a later turn that reuses this pid starts with no
    sample rather than one describing a tree that is gone.
    """
    try:
        if not sweep_enabled():
            return None
        return sweep_survivors(
            root_pid=process.pid,
            expected_pgid=process.pid,
            prior_snapshot=(*prior_snapshot, *last_process_tree(process.pid)),
        )
    finally:
        clear_process_tree(process.pid)


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
