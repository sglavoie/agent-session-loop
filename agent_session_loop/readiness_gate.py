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

Two parts live in sibling modules that import this one: the mid-run re-gate in
:mod:`agent_session_loop.readiness_gate_reopen` and the reduced-report adoption
and auto-repair in :mod:`agent_session_loop.readiness_gate_repair`.

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
from pathlib import Path
from typing import Any

from . import (
    clock,
    plans_content_hash,
    plans_errors,
    plans_models,
    plans_readiness,
    preflight,
    preflight_auto_repair,
    preflight_hint_resolution,
    preflight_models,
    run_inputs,
    run_persistence,
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


def _review_only_complete_start(
    report: preflight_models.PreflightReport,
    *,
    review_requested: bool,
    snapshot: plans_models.PlanSnapshot,
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
    new_state: plans_models.ReadinessState,
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
            sha = plans_content_hash.stable_readiness_sha256(plan_dir)
        except plans_errors.PlanError as error:
            return False, str(error)
    metadata = plans_models.ReadinessMetadata(
        state=new_state,
        readiness_sha256=sha,
        checked_at=clock.isoformat(clock.utc_now()),
    )
    try:
        plans_readiness.write_readiness_metadata(plan_dir, metadata)
    except plans_errors.PlanError as error:
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
    snapshot: plans_models.PlanSnapshot,
) -> plans_models.ReadinessState:
    if payload.get("paused") or not payload.get("ok"):
        return "reviewed"
    if run_inputs._all_sessions_complete(snapshot):
        return "complete"
    return "loop-ready"
