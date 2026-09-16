"""One-shot, portable process sampling for turn-hang diagnostics and sweeps.

A single headerless ``ps`` invocation (macOS and Linux compatible) is parsed
into a ppid->children map and walked breadth-first from the harness root pid.
Any failure — spawn error, timeout, nonzero exit, unparsable output, or an
absent root — yields ``None`` so callers stay fail-open.

:func:`prepare_verified_leaf_recovery` re-samples a suspected-live harness tree
and selects only identity-verified leaves for a one-shot SIGTERM recovery.
:func:`sweep_survivors` separately reuses sampling to SIGKILL processes that
outlived the harness turn or verification check they belonged to. Both paths
are best-effort cleanup, so nothing in this module raises: every failure
degrades to a report rather than destabilizing the run.

:func:`accumulate_cpu_seconds` folds successive samples into an identity-keyed
CPU high-water map, so turn-hang accounting survives tree churn: a descendant
that exits between two samples no longer takes the CPU it burned with it.

:func:`collect_descendant_evidence`, :func:`force_stop_process_group`,
:func:`terminate_escaped_descendants` and :func:`confirm_termination` are the
urgent counterparts, split so that *none* of the sampling is on the deadline
path: evidence is taken while the worker is still alive, the kill spawns
nothing at all, and every identity check runs after the group is already dead.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from collections.abc import Set as AbstractSet
from typing import Any, Literal

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

POST_RESULT_RECOVERY_POLICY = "verified_leaf_sigterm"
POST_RESULT_RECOVERY_SIGNAL = "SIGTERM"
POST_RESULT_ROOT_RECOVERY_POLICY = "verified_root_shutdown"

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


def descendant_pids(
    root_pid: int,
    table: Mapping[int, Mapping[str, Any]],
    *,
    stop: AbstractSet[int] = frozenset(),
) -> list[int]:
    """Return ``root_pid``'s descendants in BFS order, root itself excluded.

    ``table`` is any pid-keyed mapping whose entries carry ``ppid`` — both
    :func:`sample_process_tree`'s parse and :func:`sample_process_table` qualify.
    Children keep the order ``ps`` listed them in.

    A pid in ``stop`` is neither returned nor descended into, so a caller that
    walks the tree to find things to signal can fence off a subtree it must
    never touch — its own, above all.
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
            if child_pid in seen or child_pid in stop:
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


def _verified_leaf_candidates(
    root_pid: int,
    prior_tree: Iterable[Mapping[str, Any]],
    current_table: Mapping[int, Mapping[str, Any]],
) -> tuple[list[int], list[int], list[dict[str, Any]]]:
    """Return verified prior leaves, already-gone pids, and mismatches.

    A candidate must have been a leaf in the sampled suspicion tree, still
    carry the same pid/birth/command identity, and remain a descendant of the
    same identity-verified harness root. Parents are never promoted when a leaf
    disappears, which keeps harness shells and ASL itself out of the target set.
    """
    prior: dict[int, Mapping[str, Any]] = {}
    for entry in prior_tree:
        try:
            pid = int(entry["pid"])
        except KeyError, TypeError, ValueError:
            continue
        prior[pid] = entry

    children = {
        int(entry["ppid"])
        for entry in prior.values()
        if isinstance(entry.get("ppid"), int)
    }
    candidate_pids = sorted(
        pid
        for pid in prior
        if pid > 1 and pid != root_pid and pid != os.getpid() and pid not in children
    )
    if not candidate_pids:
        return [], [], []

    mismatches: list[dict[str, Any]] = []
    prior_root = prior.get(root_pid)
    current_root = current_table.get(root_pid)
    root_matches = (
        prior_root is not None
        and current_root is not None
        and isinstance(prior_root.get("birth"), str)
        and bool(prior_root.get("birth"))
        and current_root.get("birth") == prior_root.get("birth")
        and current_root.get("command") == prior_root.get("command")
    )
    if not root_matches:
        mismatches.append({"pid": root_pid, "reason": "harness_root_identity"})
        return [], [], mismatches

    descendants = set(descendant_pids(root_pid, current_table))
    verified: list[int] = []
    already_gone: list[int] = []
    for pid in candidate_pids:
        recorded = prior[pid]
        current = current_table.get(pid)
        if current is None:
            already_gone.append(pid)
            continue
        if pid not in descendants:
            mismatches.append({"pid": pid, "reason": "ancestry"})
            continue
        recorded_birth = recorded.get("birth")
        if (
            not isinstance(recorded_birth, str)
            or not recorded_birth
            or current.get("birth") != recorded_birth
        ):
            mismatches.append({"pid": pid, "reason": "birth"})
            continue
        if current.get("command") != recorded.get("command"):
            mismatches.append({"pid": pid, "reason": "command"})
            continue
        verified.append(pid)
    return verified, already_gone, mismatches


def prepare_verified_leaf_recovery(
    *,
    root_pid: int,
    prior_tree: Iterable[Mapping[str, Any]],
    attempted_at: str,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = SAMPLE_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], tuple[int, ...]]:
    """Re-sample and prepare a safe one-shot post-result SIGTERM attempt.

    The returned report is ready to publish under ``hang_suspected.recovery``;
    the second item contains the only pids a caller may signal. Splitting
    revalidation from delivery lets the runner invalidate an in-flight sample
    on resumed output immediately before it sends any signal.
    """
    report: dict[str, Any] = {
        "policy": POST_RESULT_RECOVERY_POLICY,
        "attempted_at": attempted_at,
        "signal": POST_RESULT_RECOVERY_SIGNAL,
        "candidate_pids": [],
        "signaled_pids": [],
        "already_gone_pids": [],
        "identity_mismatches": [],
        "signal_failures": [],
    }
    if not sweep_enabled(environ):
        report["disabled"] = "process_sweep_opt_out"
        return report, ()
    try:
        prior_entries = tuple(dict(entry) for entry in prior_tree)
        prior_pids = {entry.get("pid") for entry in prior_entries}
        children = {
            entry.get("ppid")
            for entry in prior_entries
            if entry.get("ppid") is not None
        }
        report["candidate_pids"] = sorted(
            pid
            for pid in prior_pids
            if isinstance(pid, int)
            and pid > 1
            and pid != root_pid
            and pid != os.getpid()
            and pid not in children
        )
        table = sample_process_table(run_command=run_command, timeout=timeout)
        if table is None:
            report["sample_error"] = "process table sample failed"
            return report, ()
        verified, already_gone, mismatches = _verified_leaf_candidates(
            root_pid, prior_entries, table
        )
        report["already_gone_pids"] = already_gone
        report["identity_mismatches"] = mismatches
        return report, tuple(verified)
    except Exception as exc:  # noqa: BLE001 - recovery is strictly best effort
        report["sample_error"] = str(exc)
        return report, ()


def prepare_verified_root_recovery(
    *,
    root_pid: int,
    prior_tree: Iterable[Mapping[str, Any]],
    attempted_at: str,
    force: bool = False,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], tuple[int, ...]]:
    """Prepare shutdown of an identity-verified harness with no descendants.

    Only the terminal-result sentinel calls this. The original root identity
    must survive both the initial shutdown and any later escalation.
    """
    report: dict[str, Any] = {
        "policy": POST_RESULT_ROOT_RECOVERY_POLICY,
        "root_pid": root_pid,
        "attempted_at": attempted_at,
        "signal": "SIGKILL" if force else "SIGTERM",
        "candidate_pids": [],
        "signaled_pids": [],
        "already_gone_pids": [],
        "identity_mismatches": [],
        "signal_failures": [],
    }
    if not sweep_enabled(environ):
        report["disabled"] = "process_sweep_opt_out"
        return report, ()
    if root_pid <= 1 or root_pid == os.getpid():
        report["disabled"] = "protected_pid"
        return report, ()
    table = sample_process_table()
    if table is None:
        report["sample_error"] = "process table sample failed"
        return report, ()
    recorded = next((row for row in prior_tree if row.get("pid") == root_pid), None)
    current = table.get(root_pid)
    if current is None:
        report["already_gone_pids"] = [root_pid]
        return report, ()
    if (
        recorded is None
        or not isinstance(recorded.get("birth"), str)
        or not recorded.get("birth")
        or current.get("birth") != recorded.get("birth")
        or not recorded.get("command")
        or current.get("command") != recorded.get("command")
    ):
        report["identity_mismatches"] = [
            {"pid": root_pid, "reason": "harness_root_identity"}
        ]
        return report, ()
    if descendant_pids(root_pid, table):
        report["disabled"] = "harness_has_descendants"
        return report, ()
    report["candidate_pids"] = [root_pid]
    return report, (root_pid,)


def signal_verified_leaf_recovery(
    report: Mapping[str, Any],
    pids: Iterable[int],
    *,
    kill: Callable[[int, int], Any] = os.kill,
) -> dict[str, Any]:
    """Send SIGTERM once to prepared leaves and return the completed report."""
    return _signal_verified_recovery(report, pids, kill=kill, force=False)


def signal_verified_root_recovery(
    report: Mapping[str, Any],
    pids: Iterable[int],
    *,
    force: bool = False,
    kill: Callable[[int, int], Any] = os.kill,
) -> dict[str, Any]:
    """Shut down the prepared harness root, with explicit escalation."""
    return _signal_verified_recovery(report, pids, kill=kill, force=force)


def _signal_verified_recovery(
    report: Mapping[str, Any],
    pids: Iterable[int],
    *,
    kill: Callable[[int, int], Any],
    force: bool,
) -> dict[str, Any]:
    completed = dict(report)
    signaled: list[int] = list(completed.get("signaled_pids") or ())
    already_gone: list[int] = list(completed.get("already_gone_pids") or ())
    failures: list[dict[str, Any]] = list(completed.get("signal_failures") or ())
    for pid in pids:
        try:
            kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            already_gone.append(pid)
        except Exception as exc:  # noqa: BLE001 - report and fall through
            failures.append({"pid": pid, "error": str(exc)})
        else:
            signaled.append(pid)
    completed["signaled_pids"] = sorted(set(signaled))
    completed["already_gone_pids"] = sorted(set(already_gone))
    completed["signal_failures"] = failures
    return completed


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

    Candidates come from three identifications, unioned:

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
    * every process the live table shows descended from one of the above. A
      snapshot names only what existed when it was taken, so a child forked
      afterwards is in no snapshot — and if it called ``setsid``, in no process
      group either. Its parent link survives until that parent is signalled,
      and this walk runs against the same pre-kill table, so the link is still
      there to follow. No identity check applies or is needed: ancestry in the
      current table *is* the identification, and it is only ever rooted at a
      process already verified by group membership or by pid/birth/command, so
      a reused pid never becomes a root to walk from.

    The root pid, this process and pids <= 1 are never signalled, and the
    descendant walk stops at both rather than descending through them.
    ``root_pid`` is excluded because it is reaped by its own caller, and a
    reaped pid can be reused by anything.

    Returns a report with ``killed``, ``kill_failed``, ``survivors_after`` and
    ``enumeration``, or ``None`` if the process table could not be read — the
    fail-open contract of :func:`sample_process_tree`. Never raises.

    Honest limit: a descendant that called ``setsid`` (leaving the group) *and*
    whose parent chain was already dead when the table was sampled is identified
    by none of the three routes and survives this call, as does one forked after
    that sample. Neither can block the run — the pipes they hold are no longer
    waited on — but neither is killed here.
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
        identified = {
            pid
            for pid in set(group_members) | set(snapshot_verified)
            if pid > 1 and pid not in protected
        }
        late_descendants: set[int] = set()
        for seed in sorted(identified):
            for pid in descendant_pids(seed, table, stop=protected):
                if pid > 1 and pid not in protected and pid not in identified:
                    late_descendants.add(pid)
        enumeration["late_descendants"] = sorted(late_descendants)
        candidates = sorted(identified | late_descendants)

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


# --- Urgent termination -----------------------------------------------------
#
# Everything above serves cleanup that happens *after* a turn has ended, where a
# five-second sampling bound costs nothing because nobody is waiting on it. The
# four functions below serve the opposite case: a deadline measured in seconds
# that a correction has to beat, where the same bound would be the whole budget
# spent on a ``ps`` that may never answer. They are separate functions rather
# than options on the ones above because the ordering is the design — sample
# before, kill without sampling, verify after — and an option would let a caller
# put the probe back on the path it must not be on.

#: The bound on any ``ps`` the urgent path is allowed to spawn.
#: :data:`SAMPLE_TIMEOUT_SECONDS` is longer than the entire escalation window a
#: probe would have to fit inside, so a wedged ``ps`` would turn a two-second
#: deadline into a seven-second one. Nothing on the deadline path waits for a
#: probe at all; this is the second guard, so a probe that runs late — before
#: the deadline or after the kill — still cannot hold a report open.
URGENT_SAMPLE_TIMEOUT_SECONDS = 1.0

#: How :func:`terminate_escaped_descendants` picks what it signals. Named on the
#: report so a reader of a run's artifacts can tell this cleanup from the
#: post-turn sweep's, which enumerates differently and signals more.
URGENT_ESCAPEE_POLICY = "identity_checked_sigkill"
URGENT_ESCAPEE_SIGNAL = "SIGKILL"

#: What a signal-0 probe learned about a process group. ``ownership-unknown``
#: is its own answer rather than a flavour of ``running`` because the two lead
#: to different operator actions: one is a process to wait for, the other is a
#: process this run may not signal and must not claim to have stopped.
GroupLiveness = Literal["stopped", "running", "ownership-unknown", "not-probed"]
GROUP_STOPPED: GroupLiveness = "stopped"
GROUP_RUNNING: GroupLiveness = "running"
GROUP_OWNERSHIP_UNKNOWN: GroupLiveness = "ownership-unknown"
GROUP_NOT_PROBED: GroupLiveness = "not-probed"

#: What re-reading a pid's start time said about the process now wearing it.
#: ``unverified`` is deliberately not ``mismatch``: :func:`process_birth`
#: returns ``None`` both for a process that is gone and for a ``ps`` that could
#: not be read, and neither of those is evidence that the pid was reused.
IdentityVerdict = Literal["match", "mismatch", "unverified"]

#: Why a termination could not be called complete, most concrete first.
INCOMPLETE_GROUP_NOT_STOPPED = "group-not-stopped"
INCOMPLETE_DESCENDANTS_RUNNING = "descendants-running"
INCOMPLETE_OWNERSHIP_UNKNOWN = "ownership-unknown"
INCOMPLETE_SWEEP_OPT_OUT = "process-sweep-opt-out"
INCOMPLETE_EVIDENCE_UNAVAILABLE = "evidence-unavailable"

REFUSE_INVALID_PGID = "invalid_pgid"
REFUSE_OWN_PROCESS_GROUP = "own_process_group"


def collect_descendant_evidence(
    root_pid: int,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = URGENT_SAMPLE_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Record what runs under ``root_pid`` while ``root_pid`` is still alive.

    This is the whole reason the urgent path can kill without sampling: killing
    the group reparents an escapee to pid 1, after which no descendant walk
    reaches it, so the walk has to happen first. Taken early and off the
    deadline path, it costs the escalation nothing — the deadline fires whether
    or not this ever returned.

    The report carries ``entries`` (every descendant, as
    :func:`sample_process_table` describes them, each with the ``birth`` that
    makes it identifiable later), ``escaped_pids`` (those in a different process
    group, which is exactly the set a group kill will miss), and ``collected``,
    which is the only honest answer to "did we look?". A caller that needs to
    establish that work has stopped must read ``collected`` rather than the
    emptiness of ``entries``: no descendants and no sample look identical
    otherwise, and they mean opposite things.

    Honors :data:`SWEEP_DISABLED_ENV_VAR`. The opt-out withholds the evidence
    and therefore the proof, which is a fact the caller has to act on — it does
    not stop the worker's own group from being killed, because that group is
    this run's to stop and was never what the opt-out was about.
    """
    evidence: dict[str, Any] = {
        "root_pid": root_pid,
        "root_pgid": None,
        "entries": [],
        "escaped_pids": [],
        "collected": False,
        "root_absent": False,
        "disabled": None,
        "sample_error": None,
    }
    if not sweep_enabled(environ):
        evidence["disabled"] = "process_sweep_opt_out"
        return evidence
    try:
        table = sample_process_table(run_command=run_command, timeout=timeout)
        if table is None:
            evidence["sample_error"] = "process table sample failed"
            return evidence
        evidence["collected"] = True
        root = table.get(root_pid)
        if root is None:
            # The read succeeded and found no such process, which is a complete
            # answer rather than a failed one: nothing descends from a pid that
            # is not there.
            evidence["root_absent"] = True
            return evidence
        root_pgid = root.get("pgid")
        evidence["root_pgid"] = root_pgid
        protected = {root_pid, os.getpid()}
        entries = [
            dict(table[pid])
            for pid in descendant_pids(root_pid, table, stop=protected)
            if pid > 1 and pid not in protected
        ]
        evidence["entries"] = entries
        evidence["escaped_pids"] = sorted(
            int(entry["pid"]) for entry in entries if entry.get("pgid") != root_pgid
        )
        return evidence
    except Exception as exc:  # noqa: BLE001 - evidence degrades, never raises
        evidence["collected"] = False
        evidence["sample_error"] = str(exc)
        return evidence


def force_stop_process_group(
    pgid: int,
    *,
    killpg: Callable[[int, int], Any] = os.killpg,
) -> dict[str, Any]:
    """SIGKILL one owned process group now, spawning and reading nothing.

    The deadline path in its entirety. It issues a single ``killpg`` and
    returns: no ``ps``, no table, no descendant walk, nothing that a loaded
    machine or a wedged binary can make slow. Whatever a caller wants to know
    about descendants it has to have collected beforehand
    (:func:`collect_descendant_evidence`) or check afterwards
    (:func:`confirm_termination`).

    Two refusals, both about aiming: a ``pgid`` of 1 or less names no group this
    run owns, and a ``pgid`` equal to this process's own group would sweep the
    runner along with the worker. Neither raises — the report says ``refused``
    and nothing was signalled.
    """
    report: dict[str, Any] = {
        "pgid": pgid,
        "signal": URGENT_ESCAPEE_SIGNAL,
        "signalled": False,
        "already_gone": False,
        "refused": None,
        "error": None,
    }
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        report["refused"] = REFUSE_INVALID_PGID
        return report
    try:
        own_pgid: int | None = os.getpgrp()
    except OSError:  # pragma: no cover - getpgrp does not fail on a live process
        own_pgid = None
    if own_pgid is not None and pgid == own_pgid:
        report["refused"] = REFUSE_OWN_PROCESS_GROUP
        return report
    try:
        killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        report["already_gone"] = True
    except Exception as exc:  # noqa: BLE001 - reported; the deadline still passed
        report["error"] = str(exc)
    else:
        report["signalled"] = True
    return report


def _verify_identity(
    pid: int,
    recorded_birth: Any,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    timeout: float,
) -> IdentityVerdict:
    """Say whether ``pid`` is still the process the evidence recorded."""
    if not isinstance(recorded_birth, str) or not recorded_birth:
        return "unverified"
    current = process_birth(pid, run_command=run_command, timeout=timeout)
    if current is None:
        return "unverified"
    return "match" if current == recorded_birth else "mismatch"


def terminate_escaped_descendants(
    evidence: Mapping[str, Any],
    *,
    kill: Callable[[int, int], Any] = os.kill,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = URGENT_SAMPLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """SIGKILL the descendants a group kill could not reach, identity first.

    Only the ``escaped_pids`` of ``evidence`` are candidates: everything still
    in the worker's group died with it, and re-signalling those would only add
    probes. Each candidate's pid is re-authenticated against the start time the
    evidence recorded before anything is sent, because the pid was read from a
    sample of a process that has since been killed — the most likely moment in
    a run's life for that number to be handed to someone else.

    Only ``match`` is signalled. A ``mismatch`` is a reused pid and a
    ``unverified`` is a pid whose identity could not be established at all, and
    neither earns a SIGKILL: an unverified survivor is reported so the caller
    can refuse to continue, which is a strictly better outcome than killing a
    stranger. Runs after the group is already dead, so its probes are late by
    construction and delay nothing.
    """
    report: dict[str, Any] = {
        "policy": URGENT_ESCAPEE_POLICY,
        "signal": URGENT_ESCAPEE_SIGNAL,
        "candidate_pids": [],
        "killed": [],
        "already_gone": [],
        "identity_mismatches": [],
        "unverified": [],
        "kill_failures": [],
        "disabled": None,
        "error": None,
    }
    disabled = evidence.get("disabled")
    if disabled:
        report["disabled"] = disabled
        return report
    try:
        protected = {os.getpid(), evidence.get("root_pid")}
        candidates = sorted(
            {
                pid
                for pid in evidence.get("escaped_pids") or ()
                if isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 1
                and pid not in protected
            }
        )
        report["candidate_pids"] = candidates
        if not candidates:
            return report
        births = {
            int(entry["pid"]): entry.get("birth")
            for entry in evidence.get("entries") or ()
            if isinstance(entry, Mapping) and isinstance(entry.get("pid"), int)
        }
        for pid in candidates:
            verdict = _verify_identity(
                pid, births.get(pid), run_command=run_command, timeout=timeout
            )
            if verdict == "mismatch":
                report["identity_mismatches"].append({"pid": pid, "reason": "birth"})
                continue
            if verdict == "unverified":
                report["unverified"].append(pid)
                continue
            try:
                kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                report["already_gone"].append(pid)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                report["kill_failures"].append({"pid": pid, "error": str(exc)})
            else:
                report["killed"].append(pid)
        return report
    except Exception as exc:  # noqa: BLE001 - cleanup never breaks the run
        report["error"] = str(exc)
        return report


def _probe_group(
    pgid: int, *, killpg: Callable[[int, int], Any] = os.killpg
) -> GroupLiveness:
    """Read whether ``pgid`` still holds a process, without spawning anything.

    Signal 0 answers for the group as a whole, including a member that has
    exited but not been reaped — which is the point. A caller's own worker stays
    visible to this probe until the caller reaps it, so "the group is gone" is a
    statement about collected processes and not merely about signalled ones.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return GROUP_NOT_PROBED
    try:
        killpg(pgid, 0)
    except ProcessLookupError:
        return GROUP_STOPPED
    except PermissionError:
        return GROUP_OWNERSHIP_UNKNOWN
    except Exception:  # noqa: BLE001 - an unreadable probe proves nothing
        return GROUP_OWNERSHIP_UNKNOWN
    return GROUP_RUNNING


def confirm_termination(
    *,
    pgid: int,
    evidence: Mapping[str, Any],
    kill: Callable[[int, int], Any] = os.kill,
    killpg: Callable[[int, int], Any] = os.killpg,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = URGENT_SAMPLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Report whether the worker and its tracked descendants have stopped.

    ``complete`` is the only field a continuation decision should read, and it
    is true only when all four of these hold: the evidence was actually
    collected, the process group answers signal 0 with "no such process", no
    tracked descendant is still running under its recorded identity, and nothing
    answered with a permission error. Anything else is reported as incomplete
    with the most concrete ``incomplete_reason`` available.

    A still-running descendant is authenticated before it is counted, so a pid
    handed to an unrelated process after the kill is reported as ``pid_reused``
    rather than as a survivor blocking the run; a pid whose identity cannot be
    read at all counts against completion, because "we could not tell" is not
    "it is gone". The probes are signal-0 and at most one ``ps`` per live pid,
    all of it after the kill.
    """
    confirmation: dict[str, Any] = {
        "pgid": pgid,
        "group": _probe_group(pgid, killpg=killpg),
        "remaining_pids": [],
        "pid_reused": [],
        "unverified_pids": [],
        "ownership_unknown": False,
        "complete": False,
        "incomplete_reason": None,
        "error": None,
    }
    try:
        for entry in evidence.get("entries") or ():
            if not isinstance(entry, Mapping):
                continue
            pid = entry.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
                continue
            try:
                kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                confirmation["ownership_unknown"] = True
                confirmation["remaining_pids"].append(pid)
                continue
            except Exception:  # noqa: BLE001 - an unreadable probe proves nothing
                confirmation["ownership_unknown"] = True
                confirmation["remaining_pids"].append(pid)
                continue
            verdict = _verify_identity(
                pid, entry.get("birth"), run_command=run_command, timeout=timeout
            )
            if verdict == "mismatch":
                confirmation["pid_reused"].append(pid)
                continue
            if verdict == "unverified":
                confirmation["unverified_pids"].append(pid)
            confirmation["remaining_pids"].append(pid)
        confirmation["remaining_pids"] = sorted(set(confirmation["remaining_pids"]))
        confirmation["pid_reused"] = sorted(set(confirmation["pid_reused"]))
        confirmation["unverified_pids"] = sorted(set(confirmation["unverified_pids"]))
    except Exception as exc:  # noqa: BLE001 - an unfinished check is incomplete
        confirmation["incomplete_reason"] = INCOMPLETE_EVIDENCE_UNAVAILABLE
        confirmation["error"] = str(exc)
        return confirmation

    if confirmation["group"] != GROUP_STOPPED:
        confirmation["incomplete_reason"] = (
            INCOMPLETE_OWNERSHIP_UNKNOWN
            if confirmation["group"] == GROUP_OWNERSHIP_UNKNOWN
            else INCOMPLETE_GROUP_NOT_STOPPED
        )
    elif confirmation["remaining_pids"]:
        confirmation["incomplete_reason"] = (
            INCOMPLETE_OWNERSHIP_UNKNOWN
            if confirmation["ownership_unknown"]
            else INCOMPLETE_DESCENDANTS_RUNNING
        )
    elif evidence.get("disabled"):
        confirmation["incomplete_reason"] = INCOMPLETE_SWEEP_OPT_OUT
    elif not evidence.get("collected"):
        confirmation["incomplete_reason"] = INCOMPLETE_EVIDENCE_UNAVAILABLE
    else:
        confirmation["complete"] = True
    return confirmation
