"""Prepare and signal identity-verified process recovery candidates."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import process_sampling


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

    descendants = set(process_sampling.descendant_pids(root_pid, current_table))
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
    timeout: float = process_sampling.SAMPLE_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], tuple[int, ...]]:
    """Re-sample and prepare a safe one-shot post-result SIGTERM attempt.

    The returned report is ready to publish under ``hang_suspected.recovery``;
    the second item contains the only pids a caller may signal. Splitting
    revalidation from delivery lets the runner invalidate an in-flight sample
    on resumed output immediately before it sends any signal.
    """
    report: dict[str, Any] = {
        "policy": process_sampling.POST_RESULT_RECOVERY_POLICY,
        "attempted_at": attempted_at,
        "signal": process_sampling.POST_RESULT_RECOVERY_SIGNAL,
        "candidate_pids": [],
        "signaled_pids": [],
        "already_gone_pids": [],
        "identity_mismatches": [],
        "signal_failures": [],
    }
    if not process_sampling.sweep_enabled(environ):
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
        table = process_sampling.sample_process_table(
            run_command=run_command, timeout=timeout
        )
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
        "policy": process_sampling.POST_RESULT_ROOT_RECOVERY_POLICY,
        "root_pid": root_pid,
        "attempted_at": attempted_at,
        "signal": "SIGKILL" if force else "SIGTERM",
        "candidate_pids": [],
        "signaled_pids": [],
        "already_gone_pids": [],
        "identity_mismatches": [],
        "signal_failures": [],
    }
    if not process_sampling.sweep_enabled(environ):
        report["disabled"] = "process_sweep_opt_out"
        return report, ()
    if root_pid <= 1 or root_pid == os.getpid():
        report["disabled"] = "protected_pid"
        return report, ()
    table = process_sampling.sample_process_table()
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
    if process_sampling.descendant_pids(root_pid, table):
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
