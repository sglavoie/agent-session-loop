"""Aggregate execution timing by plan, phase, review, and verification scope."""

from __future__ import annotations

from typing import Any

from . import (
    execution_timing,
    metrics_models,
    metrics_timing_evidence,
)


def _check_scope_rows(
    evidence: dict[str, metrics_models._PlanTimingEvidence],
    included_run_keys: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Aggregate finished checks into ranked scopes.

    Returns the rows, the number of checks they cover, and how many of those
    had no recorded command, which is the label that keeps a partial ranking
    from reading as a complete one.
    """
    rows: dict[metrics_models._CheckScope, dict[str, Any]] = {}
    runs_by_scope: dict[metrics_models._CheckScope, set[tuple[str, str]]] = {}
    scoped_checks = 0
    without_command = 0
    for plan_key, item in sorted(evidence.items()):
        for key in sorted(item.checks, key=repr):
            check = item.checks[key]
            if not check.finish_seen:
                continue
            if (plan_key, check.run_id) not in included_run_keys:
                continue
            scoped_checks += 1
            if check.argv is None:
                without_command += 1
            scope = metrics_timing_evidence._check_scope(plan_key, check)
            row = rows.get(scope)
            if row is None:
                row = scope.to_dict() | {
                    "checks": 0,
                    "seconds": 0.0,
                    "checks_without_duration": 0,
                    "by_status": {},
                }
                rows[scope] = row
                runs_by_scope[scope] = set()
            row["checks"] += 1
            runs_by_scope[scope].add((plan_key, check.run_id))
            if check.duration_seconds is None:
                row["checks_without_duration"] += 1
            else:
                row["seconds"] += check.duration_seconds
            status = check.status or metrics_models.UNKNOWN_LABEL
            row["by_status"][status] = row["by_status"].get(status, 0) + 1
    ordered: list[dict[str, Any]] = []
    for scope, row in rows.items():
        row["runs"] = len(runs_by_scope[scope])
        row["seconds"] = round(row["seconds"], 3)
        row["by_status"] = dict(sorted(row["by_status"].items()))
        ordered.append(row)
    ordered.sort(
        key=lambda row: (
            -row["seconds"],
            -row["checks"],
            row["repository"] or "",
            row["working_directory"] or "",
            row["plan_key"] or "",
            row["command"],
        )
    )
    return ordered, scoped_checks, without_command


def _review_scope_rows(
    evidence: dict[str, metrics_models._PlanTimingEvidence],
    included_run_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    selections = 0
    refreshed = 0
    by_scope: dict[str, int] = {}
    fallback_reasons: dict[str, int] = {}
    without_fallback = 0
    for plan_key, item in sorted(evidence.items()):
        for run_id, scope, reason, was_refreshed in item.review_scopes:
            if (plan_key, run_id) not in included_run_keys:
                continue
            selections += 1
            by_scope[scope] = by_scope.get(scope, 0) + 1
            if was_refreshed:
                refreshed += 1
            if reason is None:
                without_fallback += 1
            else:
                fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
    return {
        "selections": selections,
        "refreshed_selections": refreshed,
        "by_scope": dict(sorted(by_scope.items())),
        "fallback_reasons": dict(sorted(fallback_reasons.items())),
        "selections_without_fallback_reason": without_fallback,
    }


def _plan_rows(
    timing: execution_timing.ExecutionTimingReport,
    evidence: dict[str, metrics_models._PlanTimingEvidence],
    per_run_seconds: dict[tuple[str, str], dict[str, float]],
    additional_by_plan: dict[str, int],
) -> list[dict[str, Any]]:
    """One row per plan, summed across every invocation it resumed through.

    A plan that was paused and resumed is one body of work in several runs, so
    its rows aggregate by plan rather than by run, and ``runs`` says how many
    invocations the total covers.
    """
    rows: dict[str, dict[str, Any]] = {}
    for run in timing.included_runs:
        row = rows.get(run.plan_key)
        if row is None:
            known = evidence.get(run.plan_key)
            row = {
                "plan_key": run.plan_key,
                "plan_dir": known.plan_dir if known is not None else None,
                "runs": 0,
                "runs_with_known_wall": 0,
                "wall_seconds": 0.0,
                "attributed_seconds": 0.0,
                "seconds": dict.fromkeys(metrics_models.TIMING_PHASES, 0.0),
                "attempts": 0,
                "additional_attempts": additional_by_plan.get(run.plan_key, 0),
                "verification_checks": 0,
                "verification_check_seconds": 0.0,
                "by_classification": dict.fromkeys(execution_timing.CLASSIFICATIONS, 0),
            }
            rows[run.plan_key] = row
        row["runs"] += 1
        row["by_classification"][run.classification] += 1
        if run.wall_seconds is not None:
            row["runs_with_known_wall"] += 1
            row["wall_seconds"] += run.wall_seconds
        phases = per_run_seconds[(run.plan_key, run.run_id)]
        for phase in metrics_models.TIMING_PHASES:
            row["seconds"][phase] += phases[phase]
            if phase != execution_timing.PHASE_UNATTRIBUTED:
                row["attributed_seconds"] += phases[phase]
        row["attempts"] += len(run.attempts)
        row["verification_checks"] += run.verification_check_count
        row["verification_check_seconds"] += run.verification_check_seconds
    ordered = list(rows.values())
    for row in ordered:
        row["wall_seconds"] = round(row["wall_seconds"], 3)
        row["attributed_seconds"] = round(row["attributed_seconds"], 3)
        row["verification_check_seconds"] = round(row["verification_check_seconds"], 3)
        row["seconds"] = {
            phase: round(value, 3) for phase, value in row["seconds"].items()
        }
    ordered.sort(
        key=lambda row: (
            -row["wall_seconds"],
            -row["attributed_seconds"],
            row["plan_key"],
        )
    )
    return ordered


def collect_execution_timing(
    timing: execution_timing.ExecutionTimingReport,
    evidence: dict[str, metrics_models._PlanTimingEvidence],
) -> dict[str, Any]:
    """Render the timing collector's per-run accounting as one report section.

    Nothing here re-measures anything: the seconds all come from
    :func:`execution_timing.collect_execution_timing`. What this adds is the
    lineage the collector has no evidence for (which worker turns were generated
    repairs), the scoping a ranking needs (which project a check ran in), and
    the coverage numbers that keep a share from being read as a census.
    """
    repair_ids = {key: item.repair_session_ids for key, item in evidence.items()}
    per_run_seconds = {
        (run.plan_key, run.run_id): metrics_timing_evidence._run_phase_seconds(
            run, repair_ids.get(run.plan_key, set())
        )
        for run in timing.runs
    }
    included_run_keys = {(run.plan_key, run.run_id) for run in timing.included_runs}

    known_seconds = dict.fromkeys(metrics_models.TIMING_PHASES, 0.0)
    runs_with_phase = dict.fromkeys(metrics_models.TIMING_PHASES, 0)
    wall_seconds = 0.0
    runs_with_known_wall = 0
    for run in timing.included_runs:
        phases = per_run_seconds[(run.plan_key, run.run_id)]
        for phase in metrics_models.TIMING_PHASES:
            if phases[phase]:
                known_seconds[phase] += phases[phase]
                runs_with_phase[phase] += 1
        if run.wall_seconds is not None:
            wall_seconds += run.wall_seconds
            runs_with_known_wall += 1

    share_seconds = dict.fromkeys(metrics_models.TIMING_PHASES, 0.0)
    share_basis = 0.0
    trustworthy = timing.trustworthy_runs
    for run in trustworthy:
        phases = per_run_seconds[(run.plan_key, run.run_id)]
        for phase in metrics_models.TIMING_PHASES:
            share_seconds[phase] += phases[phase]
        if run.wall_seconds is not None:
            share_basis += run.wall_seconds
    # An empty or zero basis reports no shares at all: zeroes would read as
    # "no time went anywhere", which absent evidence does not support.
    shares = (
        {
            phase: round(share_seconds[phase] / share_basis, 4)
            for phase in metrics_models.TIMING_PHASES
        }
        if trustworthy and share_basis > 0
        else None
    )

    repeated = metrics_timing_evidence._repeated_attempts(
        timing, repair_ids, included_run_keys
    )
    additional_by_plan: dict[str, int] = repeated.pop("_per_plan")
    scope_rows, scoped_checks, checks_without_command = _check_scope_rows(
        evidence, included_run_keys
    )
    check_work = timing.verification_check_work()
    included_by_classification = timing.included_counts_by_classification()

    return {
        "window_start": (
            timing.window_start.isoformat() if timing.window_start is not None else None
        ),
        "window_end": (
            timing.window_end.isoformat() if timing.window_end is not None else None
        ),
        "tolerance_seconds": timing.tolerance_seconds,
        "coverage": {
            "plans_with_evidence": len(evidence),
            "runs_with_evidence": len(timing.runs),
            "runs_included": len(timing.included_runs),
            "runs_by_classification": timing.counts_by_classification(),
            "included_runs_by_classification": included_by_classification,
            "exclusions": timing.exclusions(),
            "runs_with_known_wall": runs_with_known_wall,
            "runs_outside_share_basis": len(timing.included_runs) - len(trustworthy),
            "unattributable_records": timing.unattributable_records,
            "unattributable_turn_results": timing.unattributable_turn_results,
        },
        "phases": {
            "wall_seconds": round(wall_seconds, 3),
            # Measured over every included run, so a run that never recorded one
            # phase still contributes the phases it did record. The per-phase
            # denominators therefore differ and are published beside them.
            "seconds": {
                phase: round(known_seconds[phase], 3)
                for phase in metrics_models.TIMING_PHASES
            },
            "runs_with_phase": dict(runs_with_phase),
            "share_basis_runs": len(trustworthy),
            "share_basis_seconds": round(share_basis, 3),
            "share_seconds": {
                phase: round(share_seconds[phase], 3)
                for phase in metrics_models.TIMING_PHASES
            },
            "shares": shares,
        },
        "verification_checks": {
            "runs_with_checks": check_work["runs_with_checks"],
            "checks": check_work["checks"],
            # Cumulative check work, not run time: a parallel recipe's checks
            # overlap, so this can exceed the phase elapsed seconds beside it.
            "cumulative_seconds": check_work["seconds"],
            "phase_seconds": check_work["phase_seconds"],
            "scoped_checks": scoped_checks,
            "checks_without_command": checks_without_command,
            "scopes": scope_rows,
        },
        "repeated_attempts": repeated,
        "review_scope": _review_scope_rows(evidence, included_run_keys),
        "plans": _plan_rows(timing, evidence, per_run_seconds, additional_by_plan),
    }
