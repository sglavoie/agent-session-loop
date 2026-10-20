"""Payload field validators for control queue commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import guards, run_control
from .control_queue_models import (
    COMMAND_REMOVE_GUARD,
    DEFAULT_ABORT_GRACE_SECONDS,
    MAX_ABORT_GRACE_SECONDS,
    MIN_ABORT_GRACE_SECONDS,
    REJECT_INVALID_CLEAR_OPERATION,
    REJECT_INVALID_GRACE,
    REJECT_INVALID_GUARD_ID,
    REJECT_INVALID_REASON,
    REJECT_MISSING_EXPECTED_RUN_ID,
    REJECT_MISSING_GUARD_ID,
    REJECT_MISSING_GUARD_RULE,
    REJECT_MISSING_MODEL_CONTROL_OPERATION,
)


def _operation_from_fields(
    *,
    set_value: Any,
    set_present: bool,
    clear_value: Any,
    clear_present: bool,
    value_name: str,
) -> run_control.OverrideOperation:
    if set_present and (not isinstance(set_value, str) or set_value == ""):
        raise ValueError(f"invalid-{value_name}")
    if clear_present and not isinstance(clear_value, bool):
        raise ValueError(REJECT_INVALID_CLEAR_OPERATION)
    clear_requested = clear_value is True
    if set_present and clear_requested:
        raise ValueError(f"conflicting-{value_name}-operation")
    if set_present:
        return run_control.OverrideOperation.set(set_value)
    if clear_requested:
        return run_control.OverrideOperation.clear()
    return run_control.OverrideOperation.unchanged()


def _model_control_operations(
    payload: dict[str, Any],
) -> tuple[run_control.OverrideOperation, run_control.OverrideOperation]:
    model = _operation_from_fields(
        set_value=payload.get("harness_model"),
        set_present="harness_model" in payload,
        clear_value=payload.get("clear_harness_model"),
        clear_present="clear_harness_model" in payload,
        value_name="model",
    )
    effort = _operation_from_fields(
        set_value=payload.get("effort"),
        set_present="effort" in payload,
        clear_value=payload.get("clear_effort"),
        clear_present="clear_effort" in payload,
        value_name="effort",
    )
    if model.operation == "unchanged" and effort.operation == "unchanged":
        raise ValueError(REJECT_MISSING_MODEL_CONTROL_OPERATION)
    return model, effort


def _abort_fields(payload: dict[str, Any]) -> tuple[int, str | None]:
    """Validate an abort command's optional grace/reason fields.

    An omitted grace resolves to :data:`DEFAULT_ABORT_GRACE_SECONDS` so every
    accepted abort carries a usable window. An out-of-range grace is rejected
    rather than clamped: silently shortening a supervisor's stated window would
    kill a turn earlier than asked.
    """
    raw_grace = payload.get("grace_seconds")
    if raw_grace is None:
        grace = DEFAULT_ABORT_GRACE_SECONDS
    elif (
        isinstance(raw_grace, bool)
        or not isinstance(raw_grace, int)
        or not MIN_ABORT_GRACE_SECONDS <= raw_grace <= MAX_ABORT_GRACE_SECONDS
    ):
        raise ValueError(REJECT_INVALID_GRACE)
    else:
        grace = raw_grace

    raw_reason = payload.get("reason")
    if raw_reason is None:
        return grace, None
    if not isinstance(raw_reason, str) or raw_reason == "":
        raise ValueError(REJECT_INVALID_REASON)
    return grace, raw_reason


@dataclass(frozen=True)
class _GuardFields:
    """The validated guard-specific slice of one queue line."""

    deny_commands: tuple[tuple[str, ...], ...] = ()
    protect_paths: tuple[guards.ProtectedPath, ...] = ()
    guard_id: str | None = None
    expected_run_id: str | None = None
    reason: str | None = None


def _guard_fields(command: str, payload: dict[str, Any]) -> _GuardFields:
    """Validate a guard mutation's payload, raising a stable reason.

    ``expected_run_id`` is mandatory for both mutations and is what makes a
    guard command run-scoped: a queue line that outlives the run it was
    written against must be refused, and a producer that omits the field is
    asking for exactly the misapplication the field exists to prevent.
    """
    expected_run_id = payload.get("expected_run_id")
    if not isinstance(expected_run_id, str) or not expected_run_id:
        raise ValueError(REJECT_MISSING_EXPECTED_RUN_ID)

    raw_reason = payload.get("reason")
    if raw_reason is not None and (not isinstance(raw_reason, str) or raw_reason == ""):
        raise ValueError(REJECT_INVALID_REASON)

    if command == COMMAND_REMOVE_GUARD:
        guard_id = payload.get("guard_id")
        if guard_id is None or guard_id == "":
            raise ValueError(REJECT_MISSING_GUARD_ID)
        if not isinstance(guard_id, str):
            raise ValueError(REJECT_INVALID_GUARD_ID)
        return _GuardFields(
            guard_id=guard_id,
            expected_run_id=expected_run_id,
            reason=raw_reason,
        )

    raw_rules = payload.get("deny_commands")
    raw_paths = payload.get("protect_paths")
    if raw_rules is None:
        raw_rules = []
    if raw_paths is None:
        raw_paths = []
    if not isinstance(raw_rules, list) or not isinstance(raw_paths, list):
        raise ValueError(REJECT_MISSING_GUARD_RULE)
    if not raw_rules and not raw_paths:
        raise ValueError(REJECT_MISSING_GUARD_RULE)
    parsed: list[tuple[str, ...]] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, str):
            raise ValueError(guards.REJECT_EMPTY_COMMAND_RULE)
        try:
            parsed.append(guards.parse_command_rule(raw_rule))
        except guards.GuardRuleError as error:
            raise ValueError(error.reason) from error
    # Syntax is validated here; the alias check is deliberately left to the
    # consumer, which is the only side that knows the plan's registry. A queue
    # line is read by the run it names, and re-deriving that registry from a
    # queue writer's cwd would validate against the wrong plan.
    parsed_paths: list[guards.ProtectedPath] = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            raise ValueError(guards.REJECT_EMPTY_PATH_RULE)
        try:
            parsed_paths.append(guards.parse_protected_path_rule(raw_path))
        except guards.GuardRuleError as error:
            raise ValueError(error.reason) from error
    return _GuardFields(
        deny_commands=tuple(parsed),
        protect_paths=tuple(parsed_paths),
        expected_run_id=expected_run_id,
        reason=raw_reason,
    )
