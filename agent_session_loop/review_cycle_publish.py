"""Post-materialization publication for the review cycle.

Re-reads the plan repair children were written into, re-gates it, marks it
``running`` and records the resulting snapshot and readiness state."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import (
    clock,
    plans_errors,
    plans_models,
    plans_payloads,
    plans_snapshot,
    readiness_gate,
    run_persistence,
    run_termination,
)


def _regate_and_continue(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    args: argparse.Namespace,
    plan_cache: plans_snapshot.PlanSnapshotCache,
    snapshot: plans_models.PlanSnapshot,
    materialized: plans_models.PlanSnapshot,
    session_details: dict[str, Any],
    cycle_record: dict[str, Any],
) -> tuple[str, plans_models.PlanSnapshot]:
    """Hand a plan grown by repair children back to the loop, or pause."""

    try:
        refreshed = plan_cache.get_fresh(snapshot.plan_dir)
    except plans_errors.PlanError as error:
        with persistence.lock:
            payload["final_snapshot"] = plans_payloads.snapshot_payload(materialized)
        run_termination._pause_run(
            event_path,
            payload,
            reason="malformed-plan",
            message=(
                "plan could not be inspected after materializing repair "
                f"sessions: {error}"
            ),
            details={**session_details, "error": str(error)},
            persistence=persistence,
        )
        return "paused", materialized
    if not readiness_gate._regate_reopened_plan(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        args=args,
        snapshot=refreshed,
        repair_session_ids=cycle_record["repair_session_ids"],
        details={**session_details, "cycle": cycle_record["cycle"]},
    ):
        return "paused", refreshed
    running_success, running_error = readiness_gate.write_readiness_state(
        refreshed.plan_dir, "running"
    )
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(refreshed)
        if running_success:
            payload["readiness_state_active"] = True
            payload["readiness_state_current"] = "running"
            payload["readiness_state_refreshed_at"] = clock.isoformat(clock.utc_now())
            payload["readiness_state_refresh_error"] = None
        else:
            payload["readiness_state_refresh_error"] = running_error
        persistence.write_state(payload)
    return "continue", refreshed
