"""Status-change validation: the first refusals of the settlement pass.

Exactly one session moved, and it moved from ``pending`` to ``complete``. The
three refusals live here: no change at all, more than one, and the wrong one.
Each pauses the run, then runs the plan-artifact guard in adjudicate-only mode
so a packet the turn rewrote gets a verdict beside that pause (ASI-119).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    handoff_correction,
    handoff_guards,
    plans_models,
    plans_payloads,
    session_verification,
    turn_execution_commit,
)
from .turn_settlement_types import ACTION_STOP, RunContext, TurnSettlement


def validate_status_change(
    ctx: RunContext,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot,
    snapshot: plans_models.PlanSnapshot,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_artifacts_before: Mapping[str, str],
    plan_artifacts_written_by_runner: tuple[str, ...],
) -> TurnSettlement | None:
    """Return a stop when the turn's status change is not the expected one."""
    expected_change = (
        plans_payloads.session_status_change_payload(
            session_id,
            before="pending",
            after="complete",
        ),
    )
    status_changes = plans_payloads.status_changes(before, after)

    def _adjudicate_plan_artifacts_after_pause() -> None:
        # A status refusal ends the turn before the plan artifact guard's
        # ordinary slot, but the packet question is a different one and a
        # turn that fails both has to report both (ASI-119). Mirror the
        # post-turn guards: adjudicate only, record beside the pause already
        # taken, never revert a second time or displace its reason.
        handoff_guards._enforce_plan_artifact_guard(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            before=before,
            after=after,
            digests_before=plan_artifacts_before,
            runner_written=plan_artifacts_written_by_runner,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            turn_payload=turn_payload,
            paths=paths,
            plan_cache=ctx.plan_cache,
            append_handoff_note=handoff_correction._append_handoff_note,
            reinspect=session_verification._reinspect_after_revert,
            adjudicate_only=True,
        )

    def _refuse(reason: str, message: str) -> TurnSettlement:
        with ctx.persistence.lock:
            ctx.payload["expected_status_changes"] = list(expected_change)
            ctx.payload["observed_status_changes"] = list(status_changes)
            ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(after)
        turn_execution_commit.record_turn_rejected(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="status-change",
            reason=reason,
            detail={
                "expected_status_changes": list(expected_change),
                "observed_status_changes": list(status_changes),
            },
        )
        ctx.pause(
            reason=reason,
            message=message,
            details={
                **session_details,
                "expected_status_changes": list(expected_change),
                "observed_status_changes": list(status_changes),
            },
        )
        _adjudicate_plan_artifacts_after_pause()
        return TurnSettlement(ACTION_STOP, snapshot, None)

    if not status_changes:
        return _refuse("no-status-change", f"{session_label} did not change status")
    if len(status_changes) > 1:
        return _refuse(
            "multiple-status-changes",
            "more than one session status changed during one harness turn",
        )
    if status_changes != expected_change:
        return _refuse(
            "unexpected-status-change",
            "the changed session status did not match the expected pending completion",
        )
    return None
