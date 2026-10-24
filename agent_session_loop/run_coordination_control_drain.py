"""Abort arming and the draining of the external control queue."""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import (
    clock,
    control_queue,
    events,
    guards,
    harnesses,
    mailbox_appenders,
    mailbox_models,
    run_control,
    run_coordination_control,
    run_coordination_seams,
)

#: Reason recorded for an abort command that carried none. The value reaches the
#: worker's guidance record and the aborted session's Handoff note, so it has to
#: read as an explanation on its own.
ABORT_TURN_DEFAULT_REASON = "supervisor requested abort"


def abort_turn_guidance_text(
    *,
    reason: str,
    grace_seconds: int,
    deadline: str,
) -> str:
    return (
        "Supervisor abort requested for the turn now in flight "
        f"(reason: {reason}). Wind down immediately: make no further "
        "changes, leave this session pending, and emit `RUNNER_PAUSE: "
        f"supervisor requested abort - {reason}`. The harness process group "
        f"is killed at {deadline} ({grace_seconds}s grace) if the turn is "
        "still running then."
    )


def arm_requested_abort(
    action: run_control.RunControlAction,
    *,
    abort: harnesses.AbortSignal,
    plan_dir: Path,
    state_dir_arg: str | None,
    control_payload: dict[str, Any],
    mailbox_enabled: bool,
    finalization: bool = False,
) -> None:
    """Start the grace window for an observed abort and warn the worker.

    Called from the poll thread while the harness turn it aborts is still
    running. ``abort`` is the only state shared with that turn's wait loop,
    which owns expiry from here; the guidance record is what lets a compliant
    worker end the turn itself before the kill lands.

    ``finalization`` arms the kill immediately and warns nobody. The grace
    window exists so a worker can wind its own turn down and leave usable work
    behind; the terminal audit is advisory and read-only, there is no session
    to preserve, and the run is holding the plan lock while it waits — so the
    window would only delay the cancellation the supervisor just asked for.
    """
    grace_seconds = (
        0
        if finalization
        else (
            control_queue.DEFAULT_ABORT_GRACE_SECONDS
            if action.abort_grace_seconds is None
            else action.abort_grace_seconds
        )
    )
    reason = action.abort_reason or ABORT_TURN_DEFAULT_REASON
    deadline = clock.isoformat(clock.utc_now() + timedelta(seconds=grace_seconds))
    abort.arm(reason, grace_seconds=float(grace_seconds))
    control_payload["abort_deadline"] = deadline
    if finalization or not mailbox_enabled:
        return
    # The kill is already armed; failing to warn the worker must not
    # terminate the turn or lose the abort.
    with contextlib.suppress(OSError, mailbox_models.MailboxError):
        mailbox_appenders.append_guidance(
            plan_dir,
            abort_turn_guidance_text(
                reason=reason,
                grace_seconds=grace_seconds,
                deadline=deadline,
            ),
            state_dir=state_dir_arg,
        )


def degrade_abort_to_stop_after_current(
    action: run_control.RunControlAction,
    *,
    control_state: run_control.RunControlState,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Turn an abort observed with no turn in flight into a clean early stop.

    There is nothing to kill, and dropping the command would make
    ``asl control abort`` a silent no-op between turns, so the request becomes
    the strongest thing it can still mean: no further session starts.
    """
    consumed = control_state.consume_abort_turn()
    if consumed is not None:
        run_coordination_control.record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=consumed,
            context=context,
            persistence=persistence,
            message=("turn abort degraded to stop-after-current: no turn in flight"),
        )
    requested = control_state.request_stop_after_current()
    if requested is not None:
        run_coordination_control.record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=requested,
            context=context,
            persistence=persistence,
        )


def drain_external_control_queue(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    control_state: run_control.RunControlState,
    seen_ids: set[str],
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None,
    persistence: run_coordination_seams.CoordinationPersistence,
    run_control_lock: AbstractContextManager[Any],
    wakeup: threading.Event | None = None,
    abort: harnesses.AbortSignal | None = None,
    mailbox_enabled: bool = False,
    finalization: bool = False,
    guard_controller: guards.GuardController | None = None,
) -> control_queue.DrainResult:
    """Drain queued external control commands and record any state changes.

    Each accepted command is translated into the same ``RunControlAction``
    the keyboard listener would emit and persisted through
    ``record_run_control_action`` so events and the ``control`` payload
    look identical regardless of source. Commands whose IDs are already
    in ``seen_ids`` are rejected by the drain function itself. Accepted
    IDs are added to ``seen_ids`` so even a queue file rewritten by an
    external producer cannot replay the same command in this process.

    Every accepted command first gets a ``control-command-observed`` receipt
    carrying its ID, which is what ``asl control --wait`` tails; the state
    transition, if there is one, is recorded after it.

    ``wakeup`` is cleared *before* the read, not after: a wakeup that arrives
    while this drain runs may name a command appended after the read, so
    clearing afterwards would swallow it. Clearing first costs at most one
    redundant drain.

    ``abort`` is the in-flight turn's signal, supplied only by the poll thread
    that runs alongside a turn. Its presence is what tells an observed abort
    command there is something to kill: without it the request degrades to
    stop-after-current instead of dying quietly.

    ``guard_controller`` is the live run's command gate. Guard mutations are
    handled here instead of through ``apply_external_control_command`` because
    they act on the filesystem rather than on the run-control state machine,
    and because their acceptance depends on the run being the one the operator
    queued against.

    ``finalization`` says the signal belongs to the run's terminal work rather
    than to a turn. What changes is that the kill is armed without a grace
    window and warns nobody — there is no turn in flight to wind down — and the
    action is recorded as a finalization abort so a supervisor can tell the two
    apart.
    """
    if wakeup is not None:
        wakeup.clear()
    try:
        result = control_queue.drain_queue(
            plan_dir,
            state_dir=state_dir_arg,
            seen_ids=seen_ids,
        )
    except OSError:
        return control_queue.DrainResult(
            queue_path=control_queue.queue_path_for_plan(plan_dir, state_dir_arg)
        )

    for rejection in result.rejected:
        detail = f": {rejection.detail}" if rejection.detail else ""
        print(
            f"asl: ignored control queue line {rejection.line_number} "
            f"({rejection.reason}{detail})",
            file=sys.stderr,
            flush=True,
        )

    pending = list(result.accepted)
    try:
        while pending:
            _apply_drained_command(
                pending.pop(0),
                plan_dir=plan_dir,
                state_dir_arg=state_dir_arg,
                control_state=control_state,
                seen_ids=seen_ids,
                payload=payload,
                event_path=event_path,
                control_payload=control_payload,
                context=context,
                persistence=persistence,
                run_control_lock=run_control_lock,
                abort=abort,
                mailbox_enabled=mailbox_enabled,
                finalization=finalization,
                guard_controller=guard_controller,
            )
    except BaseException:
        # The queue was truncated before anything was applied, so the commands
        # this drain never reached would otherwise be lost although their
        # producers were told they were queued. The one that raised is not put
        # back: how far it got is unknown, and the error surfaces regardless.
        if result.queue_path is not None:
            with contextlib.suppress(OSError):
                control_queue.requeue_lines(
                    result.queue_path, [command.raw for command in pending]
                )
        raise
    return result


def _apply_drained_command(
    command: control_queue.ControlCommand,
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    control_state: run_control.RunControlState,
    seen_ids: set[str],
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None,
    persistence: run_coordination_seams.CoordinationPersistence,
    run_control_lock: AbstractContextManager[Any],
    abort: harnesses.AbortSignal | None,
    mailbox_enabled: bool,
    finalization: bool,
    guard_controller: guards.GuardController | None,
) -> None:
    """Record and apply one accepted command from a drain."""
    seen_ids.add(command.id)
    with run_control_lock:
        run_coordination_control.record_control_command_observed(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            command=command,
            context=context,
            persistence=persistence,
        )
        if command.command in control_queue.GUARD_COMMAND_NAMES:
            run_coordination_control.apply_guard_command(
                command,
                controller=guard_controller,
                event_path=event_path,
                payload=payload,
                control_payload=control_payload,
                context=context,
                persistence=persistence,
            )
            return
        action = run_coordination_control.apply_external_control_command(
            command, control_state
        )
        if action is None:
            return
        aborting = action.action == "abort-requested"
        finalization_abort = aborting and finalization and abort is not None
        if aborting and abort is not None:
            # Armed before the action is recorded so the requested event and
            # the `control` payload already carry the deadline the turn is
            # counting down to.
            arm_requested_abort(
                action,
                abort=abort,
                plan_dir=plan_dir,
                state_dir_arg=state_dir_arg,
                control_payload=control_payload,
                mailbox_enabled=mailbox_enabled,
                finalization=finalization,
            )
        run_coordination_control.record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=action,
            context=context,
            persistence=persistence,
            event_action=(
                events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED
                if finalization_abort
                else None
            ),
            message=(
                "abort observed while finalizing: cancelling the terminal audit"
                if finalization_abort
                else None
            ),
        )
        if aborting and abort is None:
            degrade_abort_to_stop_after_current(
                action,
                control_state=control_state,
                payload=payload,
                event_path=event_path,
                control_payload=control_payload,
                context=context,
                persistence=persistence,
            )
