# ruff: noqa: F401, F403, F405
"""Honor abort and stop controls after a turn has reached a safe boundary."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from .run_termination_pause import _pause_run, set_pause
from .run_termination_shared import *
from .run_termination_terminal import _terminalize_run


def _detach_turn_controls(poll: run_coordination_mailbox_poll.MailboxPoll) -> None:
    """Take the finished turn's mid-turn inputs away from the poller.

    Called from ``after_stop``, which runs once the poll thread is joined and
    before ``active_turn`` is cleared: after this, an observed abort arms
    nothing and observed guidance is delivered nowhere until the next turn
    registers its own signal and endpoint.
    """
    poll.set_abort(None)
    poll.set_live_delivery(None)


def _record_abort_satisfied_by_worker(
    listener: run_control.RunControlListener,
    *,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Clear a pending abort whose turn ended on its own inside the window."""
    action = listener.consume_abort_turn()
    if action is None:
        return
    run_coordination_control.record_run_control_action(
        event_path=event_path,
        payload=payload,
        control_payload=control_payload,
        action=action,
        context=context,
        persistence=run_persistence._run_persistence(persistence),
        event_action=events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
        message="turn abort satisfied by the worker inside the grace window",
    )


def _record_aborted_turn_exit(
    listener: run_control.RunControlListener,
    *,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any],
    persistence: run_persistence._RunPersistence,
) -> None:
    """Acknowledge a killed turn even when a write guard owns its pause.

    Consuming the abort records what happened to the process; it grants no
    completion, commit, scope change, or successful stop disposition.
    """
    action = listener.consume_abort_turn()
    if action is not None:
        run_coordination_control.record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=action,
            context=context,
            persistence=persistence,
        )


def _honor_abort_turn(
    listener: run_control.RunControlListener,
    *,
    snapshot: plans_models.PlanSnapshot,
    session_id: str,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    revert_session_status: SessionStatusReverter,
    reinspect: SnapshotReinspector,
    context: Mapping[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Record the clean early stop that follows a killed turn.

    A supervisor-requested abort is not a fault, so this is deliberately not a
    pause: the killed session goes back to ``pending`` with a Handoff note
    explaining why it holds no completion, and the run exits ``stopped``.

    The revert and the re-inspection are seams because both targets leave the
    runner after this module does. Neither is optional: a stop recorded against
    a snapshot taken before the revert would report the aborted session as still
    holding the completion the abort just took away.
    """
    persistence = run_persistence._run_persistence(persistence)
    action = listener.consume_abort_turn()
    reason = run_coordination_control_drain.ABORT_TURN_DEFAULT_REASON
    if action is not None:
        reason = (
            action.abort_reason
            or run_coordination_control_drain.ABORT_TURN_DEFAULT_REASON
        )
        run_coordination_control.record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=action,
            context=context,
            persistence=persistence,
        )
    reverted = revert_session_status(
        snapshot,
        session_id,
        failure_note=(
            "turn aborted by supervisor request before it completed "
            f"({reason}); the session holds no completion from it"
        ),
    )
    snapshot_after = reinspect(snapshot.plan_dir, snapshot, reverted, plan_cache)
    _honor_stop_after_current(
        listener,
        snapshot=snapshot_after,
        payload=payload,
        event_path=event_path,
        control_payload=control_payload,
        context=context,
        persistence=persistence,
    )


def _honor_stop_after_current(
    listener: run_control.RunControlListener,
    *,
    snapshot: plans_models.PlanSnapshot,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Consume the pending stop request and record the run's clean early exit.

    Consumption happens only here, at the moment the loop actually stops, so a
    request is never recorded as ``consumed`` by a run that then keeps working.
    """
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        action = listener.consume_stop_after_current()
        if action is not None:
            run_coordination_control.record_run_control_action(
                event_path=event_path,
                payload=payload,
                control_payload=control_payload,
                action=action,
                context=context,
                persistence=persistence,
            )
        payload["reason"] = "stopped"
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)
        persistence.write_state(payload)


def _stop_before_review_work(
    listener: run_control.RunControlListener,
    *,
    snapshot: plans_models.PlanSnapshot,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    record_existing_review_verdict: ExistingReviewVerdictRecorder,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Honor a stop that lands when only review work would follow.

    The generated review session and its repair children are work that starts
    after the session the operator asked to stop after, so an all-complete plan
    is not a no-op: the run exits ``stopped`` with the review still pending, and
    re-running the same command later performs it.

    The verdict projection is a seam because the review cycle leaves the runner
    after this module does; it runs inside the run's mutex, ahead of the stop it
    is reporting, so the recorded exit and the verdict it carries agree.
    """
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        record_existing_review_verdict(payload, snapshot.plan_dir)
        _honor_stop_after_current(
            listener,
            snapshot=snapshot,
            payload=payload,
            event_path=event_path,
            control_payload=control_payload,
            persistence=persistence,
        )
