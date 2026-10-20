"""The bounded ``asl commit`` subprocess a run spawns, and what survives it.

This is the process half of auto-commit: it spawns the nested ``asl commit``
under a wall-clock bound, kills its process group on expiry, drains what it
had already written, and sweeps the descendants the group kill could not
reach. Building the argv, deciding what the exit meant, and writing the
artifacts stay in :mod:`agent_session_loop.run_commit`.

Two invariants live here:

* **The commit re-enters this checkout, never PATH.** :data:`CHECKOUT_ROOT` is
  resolved from this file at import and prepended to ``PYTHONPATH`` for the
  child. Because the resolution is ``parent.parent``, this module has to sit
  directly under the package directory, which its test asserts rather than
  assumes.
* **Descendants are snapshotted before the kill, never after.** A descendant
  that called ``setsid`` never receives the group signal and is reparented to
  pid 1 once the group dies, after which no descendant walk can reach it. The
  snapshot taken while the child is still alive is the sweep's only route to
  such a process, and the child is reaped before the sweep so a pid the sweep
  might signal cannot be one this module is still holding.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import process_tree

#: The checkout this module was imported from. Auto-commit re-enters that same
#: checkout as a subprocess rather than resolving a name on PATH, so a run
#: always commits with the ``asl commit`` it ships with.
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
#: How long a killed commit subprocess is given to flush what it had already
#: written. A descendant that left the process group survives the kill and
#: holds the pipes open, so this drain is bounded too; past it the streams are
#: abandoned rather than waited on.
_COMMIT_DRAIN_SECONDS = 5


def _asl_commit_env() -> dict[str, str]:
    """Make this checkout importable to the commit subprocess."""
    env = dict(os.environ)
    root = str(CHECKOUT_ROOT)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{existing}" if existing else root
    return env


def _snapshot_commit_descendants(root_pid: int) -> tuple[Mapping[str, Any], ...]:
    """Record ``root_pid``'s descendants while the commit subprocess is alive.

    Killing the group reparents a descendant that started a session of its own
    to pid 1, after which no descendant walk can reach it. The snapshot is the
    sweep's only route to such a process, so it must be taken before the kill
    (mirrors :func:`harnesses._snapshot_descendants`).

    It names only what was running at this instant. A child forked after it is
    covered instead by :func:`process_tree.sweep_survivors` walking the live
    ancestry of everything this snapshot did name.
    """
    if not process_tree.sweep_enabled():
        return ()
    table = process_tree.sample_process_table()
    if table is None:
        return ()
    return tuple(table[pid] for pid in process_tree.descendant_pids(root_pid, table))


def _sweep_commit_descendants(
    process: subprocess.Popen,
    prior_snapshot: tuple[Mapping[str, Any], ...],
) -> dict[str, Any] | None:
    """Kill whatever outlived the bounded commit, or ``None`` when off.

    ``expected_pgid`` is the child's PID because the commit spawns with
    ``start_new_session=True``. The caller reaps the child before this runs, so
    the sweep excludes it rather than signalling a PID that may be reused; what
    it reaches is the descendant the process-group kill could not — one that
    called ``setsid`` and therefore never received the group signal, together
    with anything that descendant has forked since ``prior_snapshot`` was
    taken.
    """
    if not process_tree.sweep_enabled():
        return None
    return process_tree.sweep_survivors(
        root_pid=process.pid,
        expected_pgid=process.pid,
        prior_snapshot=prior_snapshot,
    )


def _run_bounded_asl_commit(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
) -> tuple[subprocess.CompletedProcess[str], bool, dict[str, Any] | None]:
    """Run one ``asl commit`` under a wall-clock bound it cannot outlive.

    The nested command already bounds its own harness turn and exits with a
    classified failure, so this bound normally never fires. What it covers is
    the case the nested bound cannot reach: a wedged interpreter, or a harness
    descendant that escaped the nested kill and still holds the captured pipes
    open — the exact shape that turns one stalled commit into a stalled run.

    ``start_new_session=True`` makes the child's process group ID its own PID,
    so the expiry can kill the whole tree at once (mirrors
    :func:`harnesses._kill_process_group`). The group kill alone is not enough:
    the harness the nested command starts runs in a session of its own, so its
    tree never receives that signal. Expiry therefore snapshots the descendants
    before killing and sweeps the survivors after, the same two-step
    :meth:`HarnessAdapter.run` uses for a timed-out turn.

    A second, short bound covers the drain between the two: a descendant that
    left the group holds the captured pipes open, and waiting on it forever
    would reintroduce the stall the kill just ended. The child is reaped before
    the sweep so a pid the sweep might signal cannot be one this function is
    still holding. Expiry is reported as exit 124 — the same code
    ``run_harness`` uses for a timed-out turn — so it reads as a nonzero commit
    exit everywhere downstream and lands on the normal soft-failure path.
    """
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=_asl_commit_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        prior_snapshot = _snapshot_commit_descendants(process.pid)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            # The group is already empty, or the child left it for one of its
            # own; the direct child is still reachable either way.
            with contextlib.suppress(OSError):
                process.kill()
        try:
            stdout, stderr = process.communicate(timeout=_COMMIT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            # The streams are held open by something that outlived the kill;
            # abandon them, but still reap the signalled child so the sweep
            # below sees its pid free rather than treating the group as a
            # stranger's.
            stdout, stderr = "", ""
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_COMMIT_DRAIN_SECONDS)
        sweep = _sweep_commit_descendants(process, prior_snapshot)
        stderr = (
            f"{stderr}\nasl commit exceeded its {timeout}s bound and was "
            "terminated with its process group"
        ).lstrip()
        return subprocess.CompletedProcess(list(argv), 124, stdout, stderr), True, sweep
    return (
        subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr),
        False,
        None,
    )
