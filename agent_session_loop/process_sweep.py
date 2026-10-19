"""Sweep verified survivors after a harness turn or verification check."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import process_sampling


def sweep_survivors(
    *,
    root_pid: int,
    expected_pgid: int | None,
    prior_snapshot: Iterable[Mapping[str, Any]] = (),
    kill: Callable[[int, int], Any] = os.kill,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = process_sampling.SAMPLE_TIMEOUT_SECONDS,
    settle_seconds: float = process_sampling.SWEEP_SETTLE_SECONDS,
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
        table = process_sampling.sample_process_table(
            run_command=run_command, timeout=timeout
        )
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
                group_members = process_sampling.group_member_pids(expected_pgid, table)
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
            for pid in process_sampling.descendant_pids(seed, table, stop=protected):
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
            after = process_sampling.sample_process_table(
                run_command=run_command, timeout=timeout
            )
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
