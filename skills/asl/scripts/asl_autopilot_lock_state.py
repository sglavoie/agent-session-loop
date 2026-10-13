"""Run-lock classification and stop-honored attribution for the autopilot."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from asl_autopilot_command_parse import _canonical_path
from asl_autopilot_constants import (
    ABORT_STOP_ACTIONS,
    STOP_KIND_ABORT,
    STOP_KIND_AFTER_CURRENT,
)
from asl_autopilot_launch_policy import _expected_turn_harness


def _classify_lock(parsed: dict, status: dict) -> str:
    """Decide how to treat the current lock: clear/stale/attach/mismatch.

    - ``clear``  – no active lock; safe to start our own run.
    - ``stale``  – an active-but-stale lock; safe to start (``asl run`` clears
      stale locks before acquiring), so we let the run own the cleanup.
    - ``attach`` – an active lock for the same plan/state; another run is
      already driving this plan, so we poll rather than start a second one.
    - ``mismatch`` – an active lock for a different plan, or a live turn whose
      observed harness differs from the one the user requested for that turn's
      `session_kind` (see `_expected_turn_harness`); needs a human.
    """
    lock = status.get("lock") or {}
    if not lock.get("present") or not lock.get("active"):
        return "clear"
    if lock.get("stale"):
        return "stale"

    lock_plan = lock.get("plan_dir")
    if lock_plan is not None and _canonical_path(lock_plan) != _canonical_path(
        parsed.get("plan_dir")
    ):
        return "mismatch"

    active_turn = (
        (status.get("runner_state") or {}).get("active_turn")
        or status.get("active_turn")
        or {}
    )
    observed = active_turn.get("harness")
    expected = _expected_turn_harness(parsed, active_turn)
    if observed is not None and expected is not None and observed != expected:
        return "mismatch"
    return "attach"


def _runner_state(status: dict) -> dict:
    """Return the ``runner_state`` block of an ``asl status`` payload."""
    runner_state = status.get("runner_state") if isinstance(status, dict) else None
    return runner_state if isinstance(runner_state, dict) else {}


def _runner_run_id(status: dict) -> str | None:
    """Identity of the run that wrote the plan's persisted ``state.json``."""
    run_id = _runner_state(status).get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _lock_created_at(status: dict) -> str | None:
    """Creation timestamp of the plan lock reported by ``asl status``."""
    lock = status.get("lock") if isinstance(status, dict) else None
    created_at = lock.get("created_at") if isinstance(lock, dict) else None
    return created_at if isinstance(created_at, str) and created_at else None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp from a status payload, normalized to UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _state_written_after_lock(status: dict, lock_created_at: str | None) -> bool:
    """Positive evidence the persisted state belongs to the lock's holder.

    A runner takes the plan lock before it persists any state, and locks are
    exclusive, so a ``run_started_at`` at or after the lock's ``created_at``
    can only have been written by the run holding that lock — while an earlier
    run's leftover ``state.json``, which necessarily started before this lock
    existed, is excluded. Returns ``False`` when either timestamp is missing or
    unreadable, leaving callers on whatever evidence they already had.
    """
    created_at = _parse_timestamp(lock_created_at)
    started_at = _parse_timestamp(_runner_state(status).get("run_started_at"))
    if created_at is None or started_at is None:
        return False
    return started_at >= created_at


def _attached_run_wrote_state(
    status: dict,
    attached_run_id: str | None,
    attached_lock_created_at: str | None,
) -> bool:
    """Was the persisted ``state.json`` written by the run we attached to?

    Preferred evidence is the ``run_id`` observed in ``runner_state`` while the
    lock was held: an exact identity match. But the runner acquires the plan
    lock *before* it persists the state carrying that ``run_id``, so a run that
    starts, finishes one session and honors a stop between two attach polls can
    leave the supervisor with no trustworthy identity at all. In that startup
    window fall back to the lock itself (see
    :func:`_state_written_after_lock`), which still rejects an older run's
    leftover state.
    """
    if attached_run_id is not None:
        return _runner_run_id(status) == attached_run_id
    return _state_written_after_lock(status, attached_lock_created_at)


def _attached_stop_honored(
    status: dict,
    attached_run_id: str | None,
    attached_lock_created_at: str | None,
) -> bool:
    """Did the attached run exit by honoring an out-of-band control request?

    Both `asl control stop-after-current` and `asl control abort` end the run
    with the same clean ``reason: "stopped"``; :func:`_stop_kind` reads the
    persisted control block afterwards to tell them apart.

    Only the run the supervisor was actually attached to can answer for the
    lock it just released, so the persisted state must be attributable to that
    run (see :func:`_attached_run_wrote_state`); that attribution is what keeps
    a *stale* ``stopped`` state — left by an older run, or still on disk because
    the attached run died before persisting its own outcome — from being
    mistaken for the current outcome and blocking a legitimate launch.

    Beyond identity the state must describe a *clean, finished* stop: present
    and readable, ``run_finished_at`` set (an unfinished/orphaned state says
    nothing about how the run ended), ``ok`` true, not paused, and
    ``reason == "stopped"``. Any other terminal reason — including the other
    clean early stop, ``max-sessions`` — is not an honored control request and
    leaves the normal launch path alone.
    """
    runner_state = _runner_state(status)
    if not runner_state.get("present") or runner_state.get("error"):
        return False
    if not _attached_run_wrote_state(status, attached_run_id, attached_lock_created_at):
        return False
    if not runner_state.get("run_finished_at"):
        return False
    return (
        runner_state.get("ok") is True
        and not runner_state.get("paused")
        and runner_state.get("reason") == "stopped"
    )


def _lost_launch_stop_honored(status: dict, launched_at: str | None) -> bool:
    """Did a chunk whose child died report a stop the operator asked for?

    A supervisor that kills a runner it believes is hung leaves no launch
    payload behind, so the only account of how the run ended is the state the
    runner persisted before it died. Two shapes count as a requested stop:
    ``reason: "stopped"`` (the runner finished the stop and released the lock)
    and ``reason: "abandoned"`` whose ``abandoned.prior_reason`` is ``stopped``
    (the runner had already recorded the stop and was finalizing when it was
    killed, and a later reclaim rewrote the reason).

    Attribution reuses :func:`_attached_run_wrote_state` with the wall-clock
    instant of this chunk's launch standing in for a lock creation time: the
    runner persists ``run_started_at`` only after we invoked it, so an earlier
    run's leftover ``stopped`` state cannot be adopted as this chunk's outcome.
    """
    runner_state = _runner_state(status)
    if not runner_state.get("present") or runner_state.get("error"):
        return False
    if not _attached_run_wrote_state(status, None, launched_at):
        return False
    reason = runner_state.get("reason")
    if reason == "stopped":
        return True
    if reason != "abandoned":
        return False
    abandoned = runner_state.get("abandoned")
    if not isinstance(abandoned, dict):
        return False
    return abandoned.get("prior_reason") == "stopped"


def _stop_kind(payload: dict | None) -> str:
    """Classify a clean ``stopped`` outcome from the payload's control block.

    Both control requests exit the run `ok` with `reason: "stopped"`, so the
    only thing separating them is the last run-control transition the runner
    recorded. An abort resolves as `abort-turn-honored` (the grace window
    expired and the harness process group was killed) or
    `abort-turn-satisfied-by-worker` (the worker wound the turn down itself,
    which leaves the ordinary pause or completion path in charge and so rarely
    reaches a `stopped` run at all) or `finalization-abort-honored` (the abort
    landed after the last turn, cancelling the terminal audit); anything else —
    including a plain
    `stop-after-current-consumed` and a
    control block that is missing, unreadable, or from an older runner — is the
    session-boundary stop, which is the safe reading because it is the outcome
    that leaves finished work behind.

    Accepts either a runner JSON payload or the ``runner_state`` block of an
    `asl status` payload: both expose the same `control` object.
    """
    control = payload.get("control") if isinstance(payload, dict) else None
    last_action = control.get("last_action") if isinstance(control, dict) else None
    if last_action in ABORT_STOP_ACTIONS:
        return STOP_KIND_ABORT
    return STOP_KIND_AFTER_CURRENT
