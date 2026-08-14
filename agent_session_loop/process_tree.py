"""One-shot, portable process sampling for turn-hang diagnostics and sweeps.

A single headerless ``ps`` invocation (macOS and Linux compatible) is parsed
into a ppid->children map and walked breadth-first from the harness root pid.
Any failure — spawn error, timeout, nonzero exit, unparsable output, or an
absent root — yields ``None`` so callers stay fail-open.

:func:`sweep_survivors` reuses that sampling to SIGKILL processes that outlived
the harness turn or verification check they belonged to. The sweep is
best-effort cleanup of something already finished, so nothing in this module
raises: every failure degrades to ``None`` or an empty report instead.

:func:`accumulate_cpu_seconds` folds successive samples into an identity-keyed
CPU high-water map, so turn-hang accounting survives tree churn: a descendant
that exits between two samples no longer takes the CPU it burned with it.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

PS_COMMAND: tuple[str, ...] = ("ps", "-axo", "pid=,ppid=,state=,time=,lstart=,command=")
PS_TABLE_COMMAND: tuple[str, ...] = ("ps", "-axo", "pid=,ppid=,pgid=,lstart=,command=")
PS_BIRTH_COMMAND: tuple[str, ...] = ("ps", "-o", "lstart=", "-p")
COMMAND_MAX_CHARS = 160
SAMPLE_TIMEOUT_SECONDS = 5.0

SWEEP_DISABLED_ENV_VAR = "ASL_PROCESS_SWEEP_DISABLED"
_SWEEP_DISABLED_VALUES = frozenset({"1", "true", "yes", "on"})

# SIGKILL is delivered synchronously but the corpse stays visible to ``ps``
# until its parent reaps it, so the survivor re-sample waits out the reap of a
# process whose parent is already gone (the usual case for an escapee).
SWEEP_SETTLE_SECONDS = 0.05

# macOS ``ps -o time`` emits ``0:00.03`` (mm:ss.ff); Linux emits
# ``[[dd-]hh:]mm:ss``. One pattern covers both.
_CPU_TIME_RE = re.compile(
    r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?"
    r"(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)$"
)


# ``ps -o lstart=`` prints ctime form ("Sun Jul 26 17:57:01 2026") on both BSD
# and procps, and a start time is fixed for the life of a process — which makes
# it an identity that survives pid reuse. It is peeled off the head of the
# command column rather than split positionally so that a ps whose lstart is
# missing or shaped differently yields an entry with no birth identity instead
# of an unparsable row.
_BIRTH_PATTERN = r"\S+\s+\S+\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}\s+\d{4}"
_BIRTH_RE = re.compile(rf"^(?P<birth>{_BIRTH_PATTERN})\s+(?P<command>\S.*)$")
_LSTART_RE = re.compile(rf"^{_BIRTH_PATTERN}$")


def _split_birth(column: str) -> tuple[str | None, str]:
    """Split a ``lstart=,command=`` column into its birth time and command."""
    match = _BIRTH_RE.match(column)
    if match is None:
        return None, column
    return " ".join(match.group("birth").split()), match.group("command")


def _parse_cpu_time(raw: str) -> float | None:
    match = _CPU_TIME_RE.fullmatch(raw.strip())
    if match is None:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = float(match.group("seconds"))
    return float(days * 86400 + hours * 3600 + minutes * 60) + seconds


def descendant_pids(root_pid: int, table: Mapping[int, Mapping[str, Any]]) -> list[int]:
    """Return ``root_pid``'s descendants in BFS order, root itself excluded.

    ``table`` is any pid-keyed mapping whose entries carry ``ppid`` — both
    :func:`sample_process_tree`'s parse and :func:`sample_process_table` qualify.
    Children keep the order ``ps`` listed them in.
    """
    children: dict[int, list[int]] = {}
    for pid, entry in table.items():
        try:
            ppid = int(entry["ppid"])
        except KeyError, TypeError, ValueError:
            continue
        children.setdefault(ppid, []).append(pid)

    ordered: list[int] = []
    queue = [root_pid]
    seen = {root_pid}
    while queue:
        pid = queue.pop(0)
        for child_pid in children.get(pid, ()):
            if child_pid in seen:
                continue
            seen.add(child_pid)
            ordered.append(child_pid)
            queue.append(child_pid)
    return ordered


def sample_process_tree(
    root_pid: int,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = SAMPLE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]] | None:
    """Return the process tree rooted at ``root_pid`` or ``None`` on failure.

    Each entry carries ``pid``, ``ppid``, ``state``, ``cpu_seconds``, ``birth``
    (the start time, or ``None`` where ps does not report one) and a ``command``
    trimmed to ``COMMAND_MAX_CHARS``, in BFS order from the root.
    """
    try:
        completed = run_command(
            list(PS_COMMAND),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001 - sampling is diagnostics, never a failure
        return None
    if completed.returncode != 0:
        return None

    processes: dict[int, dict[str, Any]] = {}
    for line in (completed.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        raw_pid, raw_ppid, process_state, raw_time, tail = parts
        try:
            pid = int(raw_pid)
            ppid = int(raw_ppid)
        except ValueError:
            continue
        cpu_seconds = _parse_cpu_time(raw_time)
        if cpu_seconds is None:
            continue
        birth, command = _split_birth(tail)
        processes[pid] = {
            "pid": pid,
            "ppid": ppid,
            "state": process_state,
            "cpu_seconds": cpu_seconds,
            "birth": birth,
            "command": command[:COMMAND_MAX_CHARS],
        }

    if root_pid not in processes:
        return None

    walk = [root_pid, *descendant_pids(root_pid, processes)]
    return [processes[pid] for pid in walk]


def process_birth(
    pid: int,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = SAMPLE_TIMEOUT_SECONDS,
) -> str | None:
    """Return ``pid``'s start time as ``ps`` reports it, or ``None``.

    A start time is fixed for the life of a process, so a ``(pid, birth)`` pair
    is an identity a reused pid cannot forge. ``None`` means the identity could
    not be established at all — no such process, a ``ps`` that reports no
    parsable ``lstart``, a spawn failure or a timeout — never "this is a
    different process", so callers that authenticate an owner must treat it as
    unverified rather than as a mismatch.
    """
    if pid <= 0:
        return None
    try:
        completed = run_command(
            [*PS_BIRTH_COMMAND, str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001 - an unknown identity is the failure mode
        return None
    if completed.returncode != 0:
        return None
    for line in (completed.stdout or "").splitlines():
        birth = " ".join(line.split())
        if _LSTART_RE.match(birth):
            return birth
    return None


#: A process identity that survives pid reuse: its pid plus its start time, or
#: ``None`` where ``ps`` reported no parsable birth.
CpuIdentity = tuple[int, str | None]


def accumulate_cpu_seconds(
    tree: Iterable[Mapping[str, Any]],
    previous: Mapping[CpuIdentity, float] | None = None,
) -> tuple[float, dict[CpuIdentity, float]]:
    """Fold one sample into a ``(pid, birth) -> max cpu_seconds`` high-water map.

    Returns the total across every identity ever observed and the merged map to
    pass back on the next sample. Entries with no parsable ``pid`` are skipped.

    Postcondition: for a map carried forward across samples the total never
    decreases. A process that leaves the tree keeps contributing its last
    observed CPU — frozen, so it can never manufacture progress — while a newly
    appeared process contributes its CPU as genuine new work. Summing a live
    sample instead makes the total drop whenever a busy descendant exits, which
    reads as a stalled tree.

    Two honest limits, both of which under-count progress rather than invent it:
    a process that starts *and* exits between two samples is never seen at all,
    and where ``ps`` reported no birth a reused pid hides under the older
    identity's high-water mark.
    """
    merged: dict[CpuIdentity, float] = dict(previous or {})
    for entry in tree:
        try:
            pid = int(entry["pid"])
        except KeyError, TypeError, ValueError:
            continue
        identity: CpuIdentity = (pid, entry.get("birth"))
        cpu_seconds = float(entry.get("cpu_seconds") or 0.0)
        seen = merged.get(identity)
        if seen is None or cpu_seconds > seen:
            merged[identity] = cpu_seconds
    return sum(merged.values()), merged


_LAST_TREE_LOCK = threading.Lock()
_last_tree: tuple[int, tuple[dict[str, Any], ...]] | None = None


def record_process_tree(
    root_pid: int, tree: Iterable[Mapping[str, Any]] | None
) -> None:
    """Publish a live sample of ``root_pid``'s tree for a later sweep to reuse.

    The hang sentinel samples while the turn is still running, which is the
    only moment a descendant that left the process group is still reachable by
    a descendant walk. Recording here rather than inside
    :func:`sample_process_tree` keeps an injected sampler observable too.
    """
    global _last_tree
    entries = tuple(dict(entry) for entry in tree or ())
    with _LAST_TREE_LOCK:
        _last_tree = (root_pid, entries) if entries else None


def clear_process_tree(root_pid: int) -> None:
    """Drop the sample recorded for ``root_pid`` once its turn has been swept.

    Keying by root pid alone would let a later turn whose harness happens to
    reuse that pid union a sample of a tree that no longer exists; the sweep
    re-verifies every entry before signalling it, but a consumed sample has no
    business outliving the turn it described.
    """
    global _last_tree
    with _LAST_TREE_LOCK:
        if _last_tree is not None and _last_tree[0] == root_pid:
            _last_tree = None


def last_process_tree(root_pid: int) -> tuple[dict[str, Any], ...]:
    """Return the most recent tree recorded for ``root_pid``, or ``()``.

    Keyed by root pid so one turn's sweep can never union another turn's
    sample; :func:`sweep_survivors` still re-verifies every entry's command
    before signalling it.
    """
    with _LAST_TREE_LOCK:
        if _last_tree is None or _last_tree[0] != root_pid:
            return ()
        return _last_tree[1]


def sample_process_table(
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = SAMPLE_TIMEOUT_SECONDS,
) -> dict[int, dict[str, Any]] | None:
    """Return every process as ``pid -> {pid, ppid, pgid, birth, command}``.

    The sweep needs the process group id that :data:`PS_COMMAND` omits, so this
    reads its own format. Failure is fail-open in the same shape as
    :func:`sample_process_tree`: spawn error, timeout, nonzero exit or output
    with no parsable row all yield ``None``.
    """
    try:
        completed = run_command(
            list(PS_TABLE_COMMAND),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001 - sampling never fails a run
        return None
    if completed.returncode != 0:
        return None

    table: dict[int, dict[str, Any]] = {}
    for line in (completed.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        raw_pid, raw_ppid, raw_pgid, tail = parts
        try:
            pid = int(raw_pid)
            ppid = int(raw_ppid)
            pgid = int(raw_pgid)
        except ValueError:
            continue
        birth, command = _split_birth(tail)
        table[pid] = {
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "birth": birth,
            "command": command[:COMMAND_MAX_CHARS],
        }
    return table or None


def group_member_pids(pgid: int, table: Mapping[int, Mapping[str, Any]]) -> list[int]:
    """Return the pids whose process group is ``pgid``, ascending."""
    members: list[int] = []
    for pid, entry in table.items():
        try:
            member_pgid = int(entry["pgid"])
        except KeyError, TypeError, ValueError:
            continue
        if member_pgid == pgid:
            members.append(pid)
    return sorted(members)


def sweep_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the post-turn/post-check sweep runs; default is on.

    Only an explicit truthy :data:`SWEEP_DISABLED_ENV_VAR` turns it off, so a
    typo leaves the sweep enabled rather than silently disabling cleanup.
    """
    try:
        source = os.environ if environ is None else environ
        raw = source.get(SWEEP_DISABLED_ENV_VAR)
        if raw is None:
            return True
        return raw.strip().lower() not in _SWEEP_DISABLED_VALUES
    except Exception:  # noqa: BLE001 - an unreadable opt-out means default on
        return True


def sweep_survivors(
    *,
    root_pid: int,
    expected_pgid: int | None,
    prior_snapshot: Iterable[Mapping[str, Any]] = (),
    kill: Callable[[int, int], Any] = os.kill,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = SAMPLE_TIMEOUT_SECONDS,
    settle_seconds: float = SWEEP_SETTLE_SECONDS,
) -> dict[str, Any] | None:
    """SIGKILL what outlived ``root_pid`` and report on it, or ``None``.

    Candidates come from two independent identifications, unioned:

    * every current member of ``expected_pgid`` — positive identification, since
      a process group is only entered by inheritance from the root. Callers reap
      the root before sweeping, so a ``root_pid`` still present in the table
      means that number now belongs to something else and the group under it is
      not ours: enumeration is skipped rather than aimed at strangers.
    * every pid in ``prior_snapshot`` (entries carrying ``pid`` and ``command``,
      as :func:`sample_process_tree` and :func:`sample_process_table` produce)
      that is still running *under an identical command string and birth time*.
      The re-verification is what makes a snapshot safe to act on later: a pid
      reused by an unrelated process no longer matches, and one reused by a
      process running the same command is rejected on its birth time.

    The root pid, this process and pids <= 1 are never signalled. ``root_pid``
    is excluded because it is reaped by its own caller, and a reaped pid can be
    reused by anything.

    Returns a report with ``killed``, ``kill_failed``, ``survivors_after`` and
    ``enumeration``, or ``None`` if the process table could not be read — the
    fail-open contract of :func:`sample_process_tree`. Never raises.

    Honest limit: a descendant that called ``setsid`` (leaving the group) *and*
    whose parent chain died before any snapshot was taken is identified by
    neither route and survives this call. It can no longer block the run — the
    pipes it holds are no longer waited on — but it is not killed here.
    """
    try:
        table = sample_process_table(run_command=run_command, timeout=timeout)
        if table is None:
            return None

        enumeration: dict[str, Any] = {
            "expected_pgid": expected_pgid,
            "table_size": len(table),
        }
        group_members: list[int] = []
        if expected_pgid is not None and expected_pgid > 1:
            # A group id equal to our own would sweep the runner and whatever
            # else shares its group, so refuse rather than trust the caller.
            if expected_pgid == os.getpgrp():
                enumeration["pgid_skipped"] = "own_process_group"
            elif root_pid in table:
                # The caller reaped the root, so a live process under that
                # number is a stranger and this group is not the one we ran.
                enumeration["pgid_skipped"] = "root_pid_reused"
            else:
                group_members = group_member_pids(expected_pgid, table)
        enumeration["pgid_members"] = group_members

        snapshot_verified: list[int] = []
        pid_reused: list[int] = []
        snapshot_size = 0
        for entry in prior_snapshot or ():
            snapshot_size += 1
            try:
                pid = int(entry["pid"])
            except KeyError, TypeError, ValueError:
                continue
            current = table.get(pid)
            recorded_command = entry.get("command")
            if current is None or recorded_command is None:
                continue
            if current["command"] != recorded_command:
                continue
            recorded_birth = entry.get("birth")
            current_birth = current.get("birth")
            if recorded_birth and current_birth and recorded_birth != current_birth:
                pid_reused.append(pid)
                continue
            snapshot_verified.append(pid)
        enumeration["snapshot_size"] = snapshot_size
        enumeration["snapshot_pid_reused"] = sorted(set(pid_reused))
        # Callers may union several snapshots of the same tree, so a pid can be
        # verified more than once; the report counts each pid once.
        enumeration["snapshot_verified"] = sorted(set(snapshot_verified))

        protected = {root_pid, os.getpid()}
        candidates = sorted(
            pid
            for pid in set(group_members) | set(snapshot_verified)
            if pid > 1 and pid not in protected
        )

        killed: list[int] = []
        kill_failed: list[dict[str, Any]] = []
        already_gone: list[int] = []
        for pid in candidates:
            try:
                kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                already_gone.append(pid)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                kill_failed.append({"pid": pid, "error": str(exc)})
            else:
                killed.append(pid)
        enumeration["already_gone"] = already_gone

        survivors_after: list[int] = []
        enumeration["resampled"] = False
        if killed:
            if settle_seconds > 0:
                time.sleep(settle_seconds)
            after = sample_process_table(run_command=run_command, timeout=timeout)
            if after is not None:
                enumeration["resampled"] = True
                survivors_after = [
                    pid
                    for pid in killed
                    if pid in after and after[pid]["command"] == table[pid]["command"]
                ]

        return {
            "killed": killed,
            "kill_failed": kill_failed,
            "survivors_after": survivors_after,
            "enumeration": enumeration,
        }
    except Exception:  # noqa: BLE001 - cleanup never breaks the run it follows
        return None
