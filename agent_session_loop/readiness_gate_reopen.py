"""Re-gate of a plan the control plane grew while the run was live.

See :mod:`agent_session_loop.readiness_gate` for the gate this extends and the
invariants it shares.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_models,
    plans_payloads,
    preflight_environment,
    run_persistence,
    run_termination,
)
from .readiness_gate import _allow_not_loop_ready


def _reopened_plan_gate_payload(
    snapshot: plans_models.PlanSnapshot,
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
    snapshot: plans_models.PlanSnapshot,
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
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)
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
