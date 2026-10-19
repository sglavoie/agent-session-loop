"""Collect evidence, stop owned groups, and confirm urgent termination.

Sampling stays outside the force-stop deadline path.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Mapping
from typing import Any, Literal

from . import process_sampling

# Urgent corrections have a deadline shorter than the post-turn sweep's
# five-second sampling bound. Keep evidence collection, the group kill, and
# confirmation separate: sample before, kill without sampling, verify after.

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
    if not process_sampling.sweep_enabled(environ):
        evidence["disabled"] = "process_sweep_opt_out"
        return evidence
    try:
        table = process_sampling.sample_process_table(
            run_command=run_command, timeout=timeout
        )
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
            for pid in process_sampling.descendant_pids(root_pid, table, stop=protected)
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
    current = process_sampling.process_birth(
        pid, run_command=run_command, timeout=timeout
    )
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
