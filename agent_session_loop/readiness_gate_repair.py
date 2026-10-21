"""Reduced preflight adoption and readiness auto-repair for the run gate.

See :mod:`agent_session_loop.readiness_gate` for the gate these serve and the
invariants they uphold.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_errors,
    plans_models,
    plans_snapshot,
    preflight,
    preflight_auto_repair,
    preflight_models,
    progress_repair,
    run_inputs,
    run_persistence,
    state,
)
from .readiness_gate import (
    _readiness_gate_passed,
    _resume_gate_passed,
    _review_only_complete_start,
    write_readiness_state,
)


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
    snapshot: plans_models.PlanSnapshot,
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
    snapshot: plans_models.PlanSnapshot,
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
                    snapshot=plans_snapshot.inspect_plan(plan_dir),
                    lock_info=lock_info,
                    preflight_kwargs=preflight_kwargs,
                )
                if reduced is not None:
                    return reduced
            rerun_report = preflight.run_preflight(plan_dir, **preflight_kwargs)
        except plans_errors.PlanError:
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
        except (plans_errors.PlanError, OSError) as error:
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
        target_state: plans_models.ReadinessState = "loop-ready"
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
