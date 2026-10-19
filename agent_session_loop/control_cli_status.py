"""Control-command status projections shared by action handlers."""

from __future__ import annotations

from typing import Any


def _run_active(status: dict[str, Any]) -> bool:
    lock = status["lock"]
    return bool(lock["present"] and lock["active"])


def _turn_active(status: dict[str, Any]) -> bool:
    return status.get("active_turn") is not None


def _status_finalizing(status: dict[str, Any]) -> bool:
    runner_state = status.get("runner_state")
    if not isinstance(runner_state, dict):
        return False
    return isinstance(runner_state.get("active_finalization"), dict)


def _pending_after_current(status: dict[str, Any]) -> int:
    """Count sessions still pending once the executing one finishes.

    A session that is mid-flight is still recorded as ``pending`` in its handoff
    (the runner only marks it complete afterwards), so subtract it when a turn
    is active.
    """
    pending = int(status["counts"]["pending"])
    if status.get("active_turn") is not None:
        pending -= 1
    return max(0, pending)


def _review_continuation_pending(status: dict[str, Any]) -> bool:
    """Would review work still start once the executing session finishes?

    Only a run carrying a review harness can append a review or materialize its
    repair children, and only while the shared ``review_resume`` projection asks
    for one — a ``terminal`` projection means the plan is done. This is what
    keeps a stop on the last session from being reported as a no-op when the
    runner would in fact stop before the review.
    """
    runner_state = status.get("runner_state")
    if not isinstance(runner_state, dict):
        return False
    if not runner_state.get("review_harness"):
        return False
    review_resume = status.get("review_resume")
    if not isinstance(review_resume, dict):
        return False
    return review_resume.get("action") != "terminal"
