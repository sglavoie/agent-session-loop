"""External control commands, run-guard commands, and their durable receipts."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    control_queue,
    events,
    guards,
    harnesses,
    run_control,
    run_coordination_seams,
)

RUN_CONTROL_EVENT_ACTIONS: dict[str, str] = {
    "request": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
    "cancel": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED,
    "consumed": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
    "abort-requested": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
    # Consumption has two outcomes with one state transition behind them, so
    # the satisfied-by-worker case overrides this default at its call site.
    "abort-consumed": events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
    "tail-enabled": events.RUN_CONTROL_ACTION_TAIL_ENABLED,
    "tail-disabled": events.RUN_CONTROL_ACTION_TAIL_DISABLED,
    "harness-selected": events.RUN_CONTROL_ACTION_HARNESS_SELECTED,
    "implementation-model-updated": (
        events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED
    ),
}
RUN_CONTROL_EVENT_MESSAGES: dict[str, str] = {
    "request": "stop-after-current requested",
    "cancel": "stop-after-current canceled",
    "consumed": "stop-after-current consumed",
    "abort-requested": "turn abort requested",
    "abort-consumed": "turn abort honored",
    "tail-enabled": "live tail enabled",
    "tail-disabled": "live tail disabled",
    "harness-selected": "next harness selected",
    "implementation-model-updated": "implementation model controls updated",
}


def initial_control_payload() -> dict[str, Any]:
    """The ``control`` slot as it looks before any request has been observed."""
    return {
        "stop_after_current": False,
        "tail_enabled": False,
        "abort_turn_requested": False,
        "abort_grace_seconds": None,
        "abort_reason": None,
        "abort_deadline": None,
        "next_harness": None,
        "target_harness": None,
        "changed_fields": [],
        "implementation_overrides": {},
        "guards": [],
        "consumed": False,
        "last_action": None,
    }


def apply_run_control_action(
    control_payload: dict[str, Any],
    action: run_control.RunControlAction,
    *,
    event_action: str | None = None,
) -> str:
    """Fold one action into the ``control`` slot and name its durable action."""
    if event_action is None:
        event_action = RUN_CONTROL_EVENT_ACTIONS[action.action]
    control_payload["stop_after_current"] = action.stop_after_current
    control_payload["tail_enabled"] = action.tail_enabled
    # A consumed abort still carries the grace and reason it was acted on so
    # the event stays legible, but the payload mirrors the cleared state.
    abort_pending = action.abort_requested and action.action != "abort-consumed"
    control_payload["abort_turn_requested"] = abort_pending
    control_payload["abort_grace_seconds"] = (
        action.abort_grace_seconds if abort_pending else None
    )
    control_payload["abort_reason"] = action.abort_reason if abort_pending else None
    if not abort_pending:
        # The deadline is stamped beside the request; nothing pending, nothing
        # left for a status surface to count down to.
        control_payload["abort_deadline"] = None
    control_payload["next_harness"] = action.next_harness
    control_payload["target_harness"] = action.target_harness
    control_payload["changed_fields"] = list(action.changed_fields)
    if action.implementation_overrides is not None:
        control_payload["implementation_overrides"] = {
            harness: dict(values)
            for harness, values in action.implementation_overrides.items()
        }
    if action.action == "consumed":
        control_payload["consumed"] = True
    control_payload["last_action"] = event_action
    return event_action


def record_run_control_action(
    *,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    action: run_control.RunControlAction,
    context: Mapping[str, Any] | None = None,
    persistence: run_coordination_seams.CoordinationPersistence,
    event_action: str | None = None,
    message: str | None = None,
) -> None:
    """Persist one control transition and the event that explains it, atomically."""
    with persistence.lock:
        previous_harness = control_payload.get("next_harness") or payload.get("harness")
        event_action = apply_run_control_action(
            control_payload, action, event_action=event_action
        )
        payload["control"] = dict(control_payload)
        event_payload = {
            "run_id": payload.get("run_id"),
            "action": event_action,
            "stop_after_current": action.stop_after_current,
            "tail_enabled": action.tail_enabled,
            "abort_requested": action.abort_requested,
            "abort_grace_seconds": action.abort_grace_seconds,
            "abort_reason": action.abort_reason,
            "abort_deadline": control_payload.get("abort_deadline"),
            "next_harness": action.next_harness,
            "target_harness": action.target_harness,
            "changed_fields": list(action.changed_fields),
            "implementation_overrides": {
                harness: dict(values)
                for harness, values in control_payload.get(
                    "implementation_overrides", {}
                ).items()
            },
            "message": (
                RUN_CONTROL_EVENT_MESSAGES[action.action]
                if message is None
                else message
            ),
        }
        if context is not None:
            for key in ("session", "turn_index", "total_sessions"):
                value = context.get(key)
                if value is not None:
                    event_payload[key] = value
        if action.action == "harness-selected":
            current_harness = (
                context.get("harness")
                if context is not None and context.get("harness") is not None
                else previous_harness
            )
            event_payload["current_harness"] = current_harness
            event_payload["selected_harness"] = action.next_harness
        persistence.append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)
        persistence.write_state(payload)


def harness_name(value: str | None) -> run_control.HarnessName | None:
    """Narrow a drained command's harness to the name the control state takes.

    ``control_queue.drain_queue`` already rejects every other value, so this
    restates that guarantee in the type system rather than validating twice.
    It is still a guard and not a cast: the queue file is a durable artifact
    another process writes, and a name that slipped past the drain must degrade
    to "no harness" rather than reach the run's control payload as a harness
    nothing can execute.
    """
    # ``HARNESS_NAMES`` is the membership the drain checked, but mypy cannot
    # read a runtime collection as a type guard, so the literals are restated
    # and the two are asserted to agree by the module's tests.
    if value in harnesses.HARNESS_NAMES and value in ("codex", "claude", "copilot"):
        return value
    return None


def apply_external_control_command(
    command: control_queue.ControlCommand,
    control_state: run_control.RunControlState,
) -> run_control.RunControlAction | None:
    """Translate one queued command into a RunControlAction, if it changes state."""
    if command.command == control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT:
        return control_state.request_stop_after_current()
    if command.command == control_queue.COMMAND_SELECT_FUTURE_HARNESS:
        selected = harness_name(command.harness)
        assert selected is not None
        return control_state.select_harness(selected)
    if command.command == control_queue.COMMAND_SET_IMPLEMENTATION_MODEL:
        return control_state.set_implementation_model(
            # ``None`` here means "every harness", which is also what an
            # unrecognized name degrades to; the drain guarantees it cannot be
            # one in practice.
            target_harness=harness_name(command.harness),
            model=command.model_operation,
            effort=command.effort_operation,
        )
    if command.command == control_queue.COMMAND_REQUEST_ABORT_TURN:
        return control_state.request_abort_turn(
            grace_seconds=command.grace_seconds,
            reason=command.reason,
        )
    return None


def record_control_command_observed(
    *,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    command: control_queue.ControlCommand,
    context: Mapping[str, Any] | None,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Append the delivery receipt for one accepted external control command.

    Recorded before the command is translated, and for every accepted command
    rather than only the ones that change state: a duplicate, an
    already-applied request, and a command this runner has no branch for all
    leave no other trace, which is what makes them indistinguishable from a
    command that never arrived. No state transition happened, so this appends
    an event and writes no state.
    """
    event_payload: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
        "command_id": command.id,
        "queue_command": command.command,
        "stop_after_current": bool(control_payload.get("stop_after_current")),
        "message": "control command observed",
    }
    if context is not None:
        for key in ("session", "turn_index", "total_sessions"):
            value = context.get(key)
            if value is not None:
                event_payload[key] = value
    persistence.append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)


def record_guard_outcome(
    *,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    command_id: str | None,
    outcome: guards.GuardOutcome,
    active: Sequence[dict[str, Any]],
    context: Mapping[str, Any] | None,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Persist one guard mutation's terminal outcome and the active set.

    The event is what ``guard add|remove --wait`` resolves on, so it is
    appended only after enforcement is genuinely in place (or genuinely
    refused) — never on queue observation alone. The ``control`` slot carries
    the full active set afterwards so status can report guards without
    replaying events.
    """
    action = {
        guards.ACTION_APPLIED: events.RUN_CONTROL_ACTION_GUARD_APPLIED,
        guards.ACTION_REMOVED: events.RUN_CONTROL_ACTION_GUARD_REMOVED,
    }.get(outcome.action, events.RUN_CONTROL_ACTION_GUARD_REJECTED)
    with persistence.lock:
        control_payload["guards"] = [dict(entry) for entry in active]
        control_payload["last_action"] = action
        payload["control"] = dict(control_payload)
        event_payload: dict[str, Any] = {
            "run_id": payload.get("run_id"),
            "action": action,
            "command_id": command_id,
            "guard_ids": list(outcome.guard_ids),
            "guards": [dict(entry) for entry in active],
            "reason": outcome.reason,
            "detail": outcome.detail,
            "message": (
                f"guard {outcome.action}"
                if outcome.ok
                else f"guard rejected: {outcome.reason}"
            ),
        }
        if context is not None:
            for key in ("session", "turn_index", "total_sessions"):
                value = context.get(key)
                if value is not None:
                    event_payload[key] = value
        persistence.append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)
        persistence.write_state(payload)


def _apply_guard_add(
    controller: guards.GuardController,
    command: control_queue.ControlCommand,
) -> guards.GuardOutcome:
    """Activate one ``add-guard`` line against the run that owns the registry.

    The queue parses a path rule's syntax; the alias half is checked here,
    because this is the first point that holds the run's own repository
    binding. An external producer writing the queue file directly cannot know
    that binding, so accepting its rule unchecked would arm a guard watching a
    repository the plan does not have.
    """
    aliases = tuple(controller.workspace_aliases()) or (guards.LEGACY_PATH_QUALIFIER,)
    for protected in command.protect_paths:
        if protected.qualifier not in aliases:
            return guards.GuardOutcome(
                action=guards.ACTION_REJECTED,
                reason=guards.REJECT_PATH_UNKNOWN_ALIAS,
                detail=(
                    f"protected-path rule {protected.rendered!r} names a "
                    f"repository this run does not hold; known qualifiers: "
                    f"{', '.join(sorted(aliases))}"
                ),
            )
    return controller.add(
        command.deny_commands,
        paths=command.protect_paths,
        reason=command.reason,
    )


def apply_guard_command(
    command: control_queue.ControlCommand,
    *,
    controller: guards.GuardController | None,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Run one queued guard mutation against the run that is actually live.

    The expected run ID is compared here rather than at append time because
    only the drain happens inside the run: a command queued against a run that
    has since ended must be refused, not silently inherited by its successor.
    """
    active: Sequence[dict[str, Any]] = (
        () if controller is None else controller.to_payload()
    )
    if controller is None or command.expected_run_id != controller.run_id:
        outcome = guards.GuardOutcome(
            action=guards.ACTION_REJECTED,
            reason=(
                guards.REJECT_NO_ACTIVE_RUN
                if controller is None
                else guards.REJECT_RUN_CHANGED
            ),
            detail=command.expected_run_id,
        )
    elif command.command == control_queue.COMMAND_REMOVE_GUARD:
        outcome = controller.remove(command.guard_id or "")
        active = controller.to_payload()
    else:
        outcome = _apply_guard_add(controller, command)
        active = controller.to_payload()
    record_guard_outcome(
        event_path=event_path,
        payload=payload,
        control_payload=control_payload,
        command_id=command.id,
        outcome=outcome,
        active=active,
        context=context,
        persistence=persistence,
    )


def expire_run_guards(
    *,
    controller: guards.GuardController | None,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Disable this run's command gate as the run ends.

    Expiry is the reason a guard is safe to add: it cannot leak into a later
    run. It runs on every exit path, including the failing ones, and swallows
    its own errors — a run must not be reported as crashed because a policy
    file could not be unlinked. The runtime files that stay behind are inert:
    no later worker inherits this run's ``PATH``.
    """
    if controller is None:
        return
    try:
        expired = controller.expire()
    except OSError:
        return
    if not expired:
        return
    with contextlib.suppress(Exception), persistence.lock:
        control_payload["guards"] = []
        control_payload["last_action"] = events.RUN_CONTROL_ACTION_GUARDS_EXPIRED
        payload["control"] = dict(control_payload)
        persistence.append_event(
            event_path,
            events.RUN_CONTROL_EVENT,
            {
                "run_id": payload.get("run_id"),
                "action": events.RUN_CONTROL_ACTION_GUARDS_EXPIRED,
                "guard_ids": [rule.id for rule in expired],
                "message": "run-scoped command guards expired with the run",
            },
        )
        persistence.write_state(payload)
