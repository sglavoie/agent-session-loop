"""Whether a plan may run at all, and the readiness state a run leaves behind.

A run is authorized once, before its first turn, by a gate that asks two
questions: does preflight find a blocker, and is the plan's manifest readiness
state one the loop may execute. This module owns that gate, the two narrower
gates that stand in for it -- the reduced report a resumed run may adopt, and
the re-gate a plan that grew mid-run must pass -- and the readiness metadata the
run writes as it moves through ``loop-ready``, ``running`` and its final state.

The gate is deliberately three decisions rather than one. :func:`_readiness_gate_passed`
is the strict one and the only one that authorizes a normal start.
:func:`_resume_gate_passed` is wider on purpose: what licenses a resumed run to
skip the core preflight is the readiness SHA that ``run_resume_preflight``
already verified, not the lifecycle state, so a crash-resumed plan sitting at
``running`` still qualifies and is then normalized by the strict gate's own
auto-repair. :func:`_review_only_complete_start` is narrower than either: it
exists so ``--review-harness`` can enter a plan whose every session is already
complete, which the strict gate would otherwise refuse.

Durable records this boundary writes:

* ``payload["readiness_gate"]`` and ``payload["readiness_gate_repair"]`` -- the
  gate's verdict, every finding behind it, and the substituted repair hint each
  blocking finding resolves to, plus what the auto-repair attempted and what it
  changed. Assembled here and written by the caller under the run's mutex.
* ``payload["readiness_gate_reopened"]`` -- the verdict of the re-gate a plan
  that grew while the run was live must pass. Written under the run's mutex by
  :func:`_regate_reopened_plan`, which is also the one place a failed re-gate
  turns into a ``not-loop-ready`` pause.
* ``payload["readiness_state_current"]``, ``readiness_state_refreshed_at`` and
  ``readiness_state_refresh_error`` -- how current the plan's recorded
  structural SHA is, refreshed between turns by
  :func:`_refresh_active_readiness_state`.
* The plan manifest's own readiness metadata, through
  :func:`write_readiness_state`. That is the only durable write here that
  leaves the run directory, and it is the seam
  :class:`~agent_session_loop.run_termination.ReadinessStateWriter` names.

Nothing here imports the runner. The plan snapshot, the preflight keyword
arguments, the run payload, the argparse namespace and the ``_RunPersistence``
handle all enter as parameters, and the pause a failed re-gate causes is taken
through :mod:`agent_session_loop.run_termination` rather than through a callback,
because that boundary already left the runner.

Invariants that survive the move:

* **A skip is never a merge.** :func:`_reduced_preflight_report` returns either
  a report the caller may adopt whole or ``None``. Findings from a reduced run
  are never merged into a full one, so the ``not-loop-ready`` pause message and
  :func:`_repair_readiness_gate` always see every finding together rather than
  two half-populated reports.
* **Auto-repair only rebuilds what is derivable.** The repair rewrites the
  progress journal from completed handoffs and the manifest readiness marker
  from authoritative state, and nothing else. Any other red or yellow finding is
  left standing for the caller's pause, and a review-only complete start rewrites
  ``complete`` rather than downgrading the lifecycle marker to pass the gate.
* **The escape hatch has a floor.** ``--allow-not-loop-ready`` waves through
  findings an operator may reasonably accept, but never the categories in
  ``preflight_models.NON_BYPASSABLE_PREFLIGHT_CATEGORIES``: a plan reaching beyond its
  bound root has no execution root the runner can honour, so running it anyway
  would write somewhere nobody declared.
* **Growth is re-gated, not grandfathered.** The startup gate sees the plan as
  authored. Both routes that grow it mid-run -- review repairs materializing
  children, and the generated review session being appended -- recompute the
  turn-budget and session-inventory findings the growth invalidated, and record
  which route asked.
* **The run's own lock is not a finding.** :func:`_strip_own_lock_findings`
  drops env-lock findings that point at the lock this run is holding, so a run
  never refuses to start because it started.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import (
    clock,
    events,
    plans,
    preflight,
    preflight_auto_repair,
    preflight_environment,
    preflight_hint_resolution,
    preflight_models,
    progress_repair,
    run_inputs,
    run_persistence,
    run_termination,
    state,
)


def _allow_not_loop_ready(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "allow_not_loop_ready", False))


def _readiness_repair_hints(
    report: preflight_models.PreflightReport, *, cwd: Path | None
) -> list[dict[str, Any]]:
    """Substituted remediation for each blocking finding, deduped by check id.

    Preflight already resolves every finding to a repair hint and can substitute
    the plan directory and repository root into the command it embeds. Carrying
    the substituted form here is what lets a pause name the exact command to run
    instead of leaving an operator to reconstruct it from the finding text.
    """

    hints: dict[str, dict[str, Any]] = {}
    for finding in report.findings:
        if finding.severity not in {"red", "yellow"}:
            continue
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        if hint.check_id in hints:
            continue
        substituted = preflight_auto_repair.repair_hint_payload(
            hint,
            plan_dir=str(report.plan_dir),
            cwd=None if cwd is None else str(cwd),
        )
        hints[hint.check_id] = {
            "check_id": hint.check_id,
            "category": hint.category,
            "fix_category": hint.fix_category,
            "message": substituted["message"],
        }
    return list(hints.values())


def _readiness_gate_payload(
    report: preflight_models.PreflightReport,
    *,
    cwd: Path | None = None,
) -> dict[str, Any]:
    readiness = report.readiness
    return {
        "ok": _readiness_gate_passed(report),
        "preflight_ok": report.ok,
        "readiness_state": readiness.state if readiness is not None else None,
        "expected_readiness_sha256": report.expected_readiness_sha256,
        "recorded_readiness_sha256": (
            readiness.readiness_sha256 if readiness is not None else None
        ),
        "findings_by_severity": {
            "red": len(report.red_findings),
            "yellow": len(report.yellow_findings),
            "green": len(report.green_findings),
        },
        "findings": [
            {
                "severity": finding.severity,
                "category": finding.category,
                "message": finding.message,
                "session_id": finding.session_id,
                "path": finding.path,
                "line": finding.line,
                "detail": finding.detail,
            }
            for finding in report.findings
        ],
        "repair_hints": _readiness_repair_hints(report, cwd=cwd),
    }


def _non_bypassable_blockers(
    report: preflight_models.PreflightReport,
) -> tuple[str, ...]:
    """Findings ``--allow-not-loop-ready`` may not wave through.

    A plan that reaches beyond its bound root has no execution root the runner
    can honour, so running it anyway would write somewhere nobody declared.
    """
    return tuple(
        dict.fromkeys(
            finding.category
            for finding in report.findings
            if finding.severity == "red"
            and finding.category in preflight_models.NON_BYPASSABLE_PREFLIGHT_CATEGORIES
        )
    )


def _readiness_gate_passed(report: preflight_models.PreflightReport) -> bool:
    if not report.ok:
        return False
    readiness = report.readiness
    if readiness is None:
        return False
    return readiness.state == "loop-ready"


def _resume_gate_passed(report: preflight_models.PreflightReport) -> bool:
    """Return True when a reduced report may stand in for a full one.

    Deliberately wider than :func:`_readiness_gate_passed`: what licenses the
    skip is the readiness SHA match that ``run_resume_preflight`` already
    verified, not the lifecycle state, so a crash-resumed plan left at
    ``running`` qualifies too. The strict loop-ready gate still runs against
    whichever report is adopted, and normalizes ``running`` through the usual
    manifest-readiness auto-repair.
    """
    if not report.ok:
        return False
    readiness = report.readiness
    if readiness is None:
        return False
    return readiness.state in preflight.RESUME_READINESS_STATES


def _reopened_plan_gate_payload(
    snapshot: plans.PlanSnapshot,
    *,
    max_pending_sessions: int | None,
    repair_session_ids: Sequence[str],
    trigger: str = events.REVIEW_REPAIR_REOPENED_EVENT,
) -> dict[str, Any]:
    """Re-evaluate the plan-size half of the readiness gate after a reopen.

    ``trigger`` names which growth path asked, because the control plane has
    two: repair children materialized from a review, and the generated review
    session itself. The verdicts are otherwise identical, so without it an
    audit reading one gate payload cannot tell which growth it authorized.
    """
    findings = preflight_environment.check_env_turn_budget(
        snapshot, max_pending_sessions=max_pending_sessions
    )
    blocking = [finding for finding in findings if finding.severity != "green"]
    return {
        "trigger": trigger,
        "ok": not blocking,
        "total_sessions": len(snapshot.sessions),
        "pending_sessions": sum(
            1 for status in snapshot.sessions if status.status == "pending"
        ),
        "repair_session_ids": list(repair_session_ids),
        "findings": [
            {
                "severity": finding.severity,
                "category": finding.category,
                "message": finding.message,
                "detail": finding.detail,
            }
            for finding in findings
        ],
    }


def _regate_reopened_plan(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
    repair_session_ids: Sequence[str],
    details: Mapping[str, Any],
    trigger: str = events.REVIEW_REPAIR_REOPENED_EVENT,
    growth: str | None = None,
) -> bool:
    """Re-gate a plan the control plane just grew; True when it may proceed.

    The startup readiness gate runs once, against the plan as authored. The
    control plane then grows that plan while the run is live by two routes --
    review repairs reopening sessions, and appending the generated review
    session itself -- so a run authorized by "all sessions complete; no turns
    required" can go on to execute a plan the gate never saw. Turn budget and
    session inventory are the two findings plan growth invalidates, so they are
    recomputed here for either route and honour the same
    ``--allow-not-loop-ready`` escape hatch as the startup gate.

    ``growth`` names what grew, for the pause message; it defaults to the
    repair wording ``trigger`` also defaults to.
    """
    gate = _reopened_plan_gate_payload(
        snapshot,
        max_pending_sessions=getattr(args, "max_pending_sessions", None),
        repair_session_ids=repair_session_ids,
        trigger=trigger,
    )
    with persistence.lock:
        payload["readiness_gate_reopened"] = gate
        persistence.write_state(payload)
    if gate["ok"] or _allow_not_loop_ready(args):
        return True
    blocking = next(
        finding for finding in gate["findings"] if finding["severity"] != "green"
    )
    grew = growth or (
        f"review repairs reopened {len(gate['repair_session_ids'])} session(s)"
    )
    with persistence.lock:
        payload["final_snapshot"] = plans.snapshot_payload(snapshot)
    run_termination._pause_run(
        event_path,
        payload,
        reason="not-loop-ready",
        message=(
            f"{grew}; "
            f"the reopened plan no longer passes the readiness gate: "
            f"{blocking['message']}"
        ),
        details={**details, "readiness_gate_reopened": gate},
        persistence=persistence,
    )
    return False


def _review_only_complete_start(
    report: preflight_models.PreflightReport,
    *,
    review_requested: bool,
    snapshot: plans.PlanSnapshot,
) -> bool:
    """Return True for a review-only entry from a fully-complete plan.

    Allows ``--review-harness`` to start a run when the manifest readiness state
    is ``complete`` and the recorded SHA matches the expected stable SHA, even
    though the normal loop-ready gate would refuse it.
    """
    if not review_requested:
        return False
    if not report.ok:
        return False
    if not run_inputs._all_sessions_complete(snapshot):
        return False
    readiness = report.readiness
    if readiness is None:
        return False
    if readiness.state != "complete":
        return False
    return readiness.readiness_sha256 == report.expected_readiness_sha256


def _readiness_gate_failure_message(
    report: preflight_models.PreflightReport,
) -> str:
    if report.readiness is None:
        return "plan manifest is missing readiness metadata"
    if not report.ok:
        return (
            "preflight failed: "
            f"{len(report.red_findings)} red, "
            f"{len(report.yellow_findings)} yellow finding(s)"
        )
    return (
        f"plan readiness state is {report.readiness.state!r}; "
        "run preflight --mark-loop-ready first"
    )


def write_readiness_state(
    plan_dir: Path,
    new_state: plans.ReadinessState,
    *,
    expected_sha256: str | None = None,
) -> tuple[bool, str | None]:
    """Rewrite a plan's readiness metadata, reporting failure rather than raising.

    Published, unlike the rest of this boundary, because it *is* the boundary
    other modules hold: it satisfies
    :class:`~agent_session_loop.run_termination.ReadinessStateWriter`, which the
    terminal path binds through ``run_termination.readiness_finalizer``. A
    private name here would be a name every terminal path reaches around.
    """
    sha = expected_sha256
    if sha is None:
        try:
            sha = plans.stable_readiness_sha256(plan_dir)
        except plans.PlanError as error:
            return False, str(error)
    metadata = plans.ReadinessMetadata(
        state=new_state,
        readiness_sha256=sha,
        checked_at=clock.isoformat(clock.utc_now()),
    )
    try:
        plans.write_readiness_metadata(plan_dir, metadata)
    except plans.PlanError as error:
        return False, str(error)
    return True, None


def _refresh_active_readiness_state(
    payload: dict[str, Any],
    plan_dir: Path,
    persistence: run_persistence._RunPersistence,
) -> None:
    """Keep an active run's recorded structural SHA current between turns."""
    if not payload.get("readiness_state_active"):
        return
    success, error = write_readiness_state(plan_dir, "running")
    with persistence.lock:
        if success:
            payload["readiness_state_current"] = "running"
            payload["readiness_state_refresh_error"] = None
            payload["readiness_state_refreshed_at"] = clock.isoformat(clock.utc_now())
        else:
            payload["readiness_state_refresh_error"] = error
        persistence.write_state(payload)


def _final_readiness_state(
    payload: dict[str, Any],
    snapshot: plans.PlanSnapshot,
) -> plans.ReadinessState:
    if payload.get("paused") or not payload.get("ok"):
        return "reviewed"
    if run_inputs._all_sessions_complete(snapshot):
        return "complete"
    return "loop-ready"


def _strip_own_lock_findings(
    report: preflight_models.PreflightReport,
    *,
    lock_info: state.LockInfo,
) -> preflight_models.PreflightReport:
    """Drop env-lock findings that point at the runner's own active lock."""
    own_pid = os.getpid()
    own_lock_path = str(lock_info.path)

    def is_self_lock(finding: preflight_models.PreflightFinding) -> bool:
        if finding.category != "env-lock":
            return False
        if finding.path != own_lock_path:
            return False
        return not (lock_info.pid is not None and lock_info.pid != own_pid)

    filtered = tuple(
        finding for finding in report.findings if not is_self_lock(finding)
    )
    if len(filtered) == len(report.findings):
        return report
    return preflight_models.PreflightReport(
        plan_dir=report.plan_dir,
        findings=filtered,
        snapshot=report.snapshot,
        readiness=report.readiness,
        expected_readiness_sha256=report.expected_readiness_sha256,
        contracts_by_session=report.contracts_by_session,
        repository_registry=report.repository_registry,
        verification_context=report.verification_context,
        repositories=report.repositories,
    )


def _readiness_gate_blockers(
    report: preflight_models.PreflightReport,
) -> tuple[preflight_models.PreflightFinding, ...]:
    return tuple(
        finding for finding in report.findings if finding.severity in {"red", "yellow"}
    )


def _reduced_preflight_report(
    plan_dir: Path,
    *,
    snapshot: plans.PlanSnapshot,
    lock_info: state.LockInfo,
    preflight_kwargs: dict[str, Any],
) -> preflight_models.PreflightReport | None:
    """Return a reduced report that may replace the full preflight, else None.

    ``None`` means the caller must run the complete ``run_preflight`` once: the
    plan is not resume-eligible, or the environment checks or the state outside
    the readiness hash raised a finding. Those findings are recomputed by the
    full run rather than merged here, so the ``not-loop-ready`` pause message
    and ``_repair_readiness_gate`` always see every finding together.
    """
    report = preflight.run_resume_preflight(
        plan_dir, snapshot=snapshot, **preflight_kwargs
    )
    if report is None:
        return None
    report = _strip_own_lock_findings(report, lock_info=lock_info)
    if not _resume_gate_passed(report):
        return None
    if preflight.run_unhashed_state_checks(
        plan_dir,
        snapshot=snapshot,
        cwd=preflight_kwargs["cwd"],
        repository_registry=report.repository_registry,
        context=report.verification_context,
    ):
        return None
    return report


def _repair_readiness_gate(
    report: preflight_models.PreflightReport,
    *,
    plan_dir: Path,
    lock_info: state.LockInfo,
    cwd: Path,
    harness: str | None,
    commit_mode: str | None,
    state_dir: str | Path | None,
    max_pending_sessions: int | None,
    event_path: Path,
    persistence: run_persistence._RunPersistence,
    snapshot: plans.PlanSnapshot,
    review_requested: bool,
    core_skipped: bool = False,
) -> tuple[preflight_models.PreflightReport, dict[str, Any]]:
    """Try to auto-repair progress/manifest-readiness-only gate failures.

    The helper only repairs categories the operator can safely have the
    runner rebuild from authoritative state:

    - ``progress-journal-missing`` via force rebuild from completed
      handoffs.
    - ``manifest-readiness`` via a canonical ``loop-ready`` metadata
      write, but only when no other blockers remain after the progress
      phase. This also covers strict gate failures that have no preflight
      finding, such as ``running`` or ``complete`` readiness with a current
      SHA on a pending plan. When the run is a review-only complete start
      (``--review-harness`` against a fully-complete plan whose original
      readiness was ``complete``), the rewrite uses ``complete`` instead
      so the auto-repair does not downgrade the lifecycle marker just to
      pass the gate.

    Any other red/yellow finding is left in place, and the caller is
    expected to fall through to the usual ``not-loop-ready`` pause.

    ``core_skipped`` says the incoming report is a reduced one; the rerun then
    stays reduced too, because re-reading the same already-cleared plan files
    is exactly the work the skip exists to avoid. It is reported back through
    ``repair_payload`` and flips to False if a full rerun replaces the report.
    """
    preflight_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "harness": harness,
        "commit_mode": commit_mode,
        "state_dir": state_dir,
        "max_pending_sessions": max_pending_sessions,
    }

    def rerun() -> preflight_models.PreflightReport | None:
        nonlocal core_skipped
        try:
            if core_skipped:
                # The repair rewrote manifest.md, so the readiness state the
                # reduced path reads must come from a fresh snapshot.
                reduced = _reduced_preflight_report(
                    plan_dir,
                    snapshot=plans.inspect_plan(plan_dir),
                    lock_info=lock_info,
                    preflight_kwargs=preflight_kwargs,
                )
                if reduced is not None:
                    return reduced
            rerun_report = preflight.run_preflight(plan_dir, **preflight_kwargs)
        except plans.PlanError:
            return None
        core_skipped = False
        return _strip_own_lock_findings(rerun_report, lock_info=lock_info)

    attempted: list[str] = []
    auto_repairs: list[preflight_auto_repair.AutoRepair] = []
    errors: list[dict[str, str]] = []

    blockers = _readiness_gate_blockers(report)
    if any(blocker.category == "progress-journal-missing" for blocker in blockers):
        attempted.append("progress-journal-missing")
        try:
            repair_result = progress_repair.repair_progress_journal(plan_dir)
        except (plans.PlanError, OSError) as error:
            errors.append(
                {
                    "category": "progress-journal-missing",
                    "error": str(error),
                }
            )
        else:
            repair = preflight_auto_repair.AutoRepair(
                action=repair_result.action,
                category="progress-journal-missing",
                path=str(repair_result.path),
                entries_written=repair_result.entries_written,
                completed_session_ids=repair_result.completed_session_ids,
                journal_replaced=repair_result.journal_replaced,
            )
            auto_repairs.append(repair)
            persistence.append_event(
                event_path,
                events.PREFLIGHT_AUTO_REPAIRED_EVENT,
                asdict(repair),
            )
            rerun_report = rerun()
            if rerun_report is not None:
                report = rerun_report

    blockers = _readiness_gate_blockers(report)
    non_readiness_blockers = tuple(
        blocker for blocker in blockers if blocker.category != "manifest-readiness"
    )
    if (
        not _readiness_gate_passed(report)
        and not non_readiness_blockers
        and not _review_only_complete_start(
            report,
            review_requested=review_requested,
            snapshot=snapshot,
        )
    ):
        attempted.append("manifest-readiness")
        target_state: plans.ReadinessState = "loop-ready"
        if (
            review_requested
            and run_inputs._all_sessions_complete(snapshot)
            and report.readiness is not None
            and report.readiness.state == "complete"
        ):
            target_state = "complete"
        success, error = write_readiness_state(plan_dir, target_state)
        if not success:
            errors.append(
                {
                    "category": "manifest-readiness",
                    "error": error or "readiness write failed",
                }
            )
        else:
            repair = preflight_auto_repair.AutoRepair(
                action="rewrite",
                category="manifest-readiness",
                path=str(plan_dir / "manifest.md"),
                entries_written=0,
                completed_session_ids=(),
                journal_replaced=False,
                target_state=target_state,
            )
            auto_repairs.append(repair)
            persistence.append_event(
                event_path,
                events.PREFLIGHT_AUTO_REPAIRED_EVENT,
                asdict(repair),
            )
            rerun_report = rerun()
            if rerun_report is not None:
                report = rerun_report

    final_counts = {
        "red": len(report.red_findings),
        "yellow": len(report.yellow_findings),
        "green": len(report.green_findings),
    }
    repair_payload: dict[str, Any] = {
        "attempted": list(attempted),
        "auto_repairs": [asdict(repair) for repair in auto_repairs],
        "errors": errors,
        "final_findings_by_severity": final_counts,
        "ok": _readiness_gate_passed(report),
        "core_skipped": core_skipped,
    }
    return report, repair_payload
