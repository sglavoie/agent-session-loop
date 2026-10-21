"""Summary classifiers for runs that hold a lock, are stale, or failed."""

from __future__ import annotations

from . import state_payloads, workbench_recovery_state
from .workbench_summary_context import SummaryContext
from .workbench_summary_helpers import _first_activity, _verification_position
from .workbench_summary_model import WorkbenchSummary


def active_summary(ctx: SummaryContext) -> WorkbenchSummary | None:
    """Classify a plan whose lock is live: verifying or running."""
    lock_info = ctx.lock_info
    runner_state = ctx.runner_state
    active_turn = ctx.active_turn
    active_verification = ctx.active_verification

    if (
        lock_info is not None
        and lock_info.active
        and isinstance(active_verification, dict)
    ):
        session_label = (
            workbench_recovery_state._session_identifier_from_payload(
                active_verification
            )
            or "?"
        )
        position = _verification_position(active_verification)
        check_name = active_verification.get("current_check_name") or "pending"
        verifying_activity, verifying_source = _first_activity(
            (
                (
                    active_verification.get("current_check_started_at"),
                    "active-verification",
                ),
                (active_verification.get("started_at"), "active-verification"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return ctx.make(
            primary_state="verifying",
            recommended_action="watch",
            next_action_hint=(
                f"Verifying session {session_label} check {position} ({check_name})"
            ),
            detail=f"Active verification on session {session_label}",
            last_activity_at=verifying_activity,
            last_activity_source=verifying_source,
            concise_reason=(
                f"On check {position} ({check_name}) of session {session_label}"
            ),
        )

    if lock_info is not None and lock_info.active and isinstance(active_turn, dict):
        session_label = (
            workbench_recovery_state._session_identifier_from_payload(active_turn)
            or "?"
        )
        turn_index = active_turn.get("turn_index")
        running_activity, running_source = _first_activity(
            (
                (active_turn.get("last_activity_at"), "active-turn"),
                (active_turn.get("started_at"), "active-turn"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return ctx.make(
            primary_state="running",
            recommended_action="watch",
            next_action_hint=(f"Running session {session_label} turn {turn_index}"),
            detail=f"Active turn on session {session_label}",
            last_activity_at=running_activity,
            last_activity_source=running_source,
            concise_reason=(f"Session {session_label}, turn {turn_index}"),
        )

    waiting_for_reply = runner_state.get("waiting_for_reply")
    if (
        lock_info is not None
        and lock_info.active
        and isinstance(waiting_for_reply, dict)
        and runner_state.get("waiting_for_reply_status")
        == state_payloads.WAITING_FOR_REPLY_STATUS_LIVE
    ):
        session_label = (
            workbench_recovery_state._session_identifier_from_payload(waiting_for_reply)
            or "?"
        )
        waiting_activity, waiting_source = _first_activity(
            ((runner_state.get("run_started_at"), "run-started"),)
        )
        return ctx.make(
            primary_state="running",
            recommended_action="watch",
            next_action_hint=f"Awaiting reply for session {session_label}",
            detail=f"Live mailbox reply wait on session {session_label}",
            last_activity_at=waiting_activity,
            last_activity_source=waiting_source,
            concise_reason=f"Awaiting reply for session {session_label}",
        )
    return None


def stale_or_failed_summary(ctx: SummaryContext) -> WorkbenchSummary | None:
    """Classify a stale lock, stale unfinished state, or a failed finished run."""
    lock_info = ctx.lock_info
    runner_state = ctx.runner_state

    if ctx.stale_unfinished or (lock_info is not None and lock_info.stale):
        if ctx.stale_unfinished:
            reason_text = ctx.stale_reason or "Runner state has not finished"
        else:
            reason_text = lock_info.reason if lock_info is not None else "stale lock"
        lock_created_at = (
            lock_info.created_at
            if lock_info is not None and lock_info.created_at
            else None
        )
        stale_activity, stale_source = _first_activity(
            (
                (lock_created_at, "lock-created"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return ctx.make(
            primary_state="stale",
            recommended_action="unlock",
            next_action_hint=f"Stale state detected: {reason_text}",
            detail=reason_text,
            last_activity_at=stale_activity,
            last_activity_source=stale_source,
            concise_reason=reason_text,
        )

    if (
        runner_state.get("present")
        and runner_state.get("run_finished_at")
        and runner_state.get("ok") is False
    ):
        reason_text = runner_state.get("reason") or "Last run finished with errors"
        failed_activity, failed_source = _first_activity(
            (
                (runner_state.get("run_finished_at"), "run-finished"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return ctx.make(
            primary_state="failed",
            recommended_action="inspect-failure",
            next_action_hint=f"Last run failed: {reason_text}",
            detail=reason_text,
            last_activity_at=failed_activity,
            last_activity_source=failed_source,
            concise_reason=reason_text,
        )
    return None
