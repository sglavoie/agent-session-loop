"""Durable external control queue for active runners.

External producers (such as the Workbench HTTP API) append commands to a
per-plan JSONL queue file under the runner state directory. The active
runner drains the queue between lifecycle checkpoints and translates
accepted commands into the same ``RunControlAction`` transitions that
the in-process keyboard listener already emits, so events and the
persisted ``control`` payload look identical regardless of the source.

Concurrency is coordinated through POSIX ``fcntl.flock``. Producers
acquire an exclusive lock around each append; consumers acquire an
exclusive lock around the drain (read + truncate). The file lives in
the same directory as ``state.json`` and ``lock.json`` so the existing
plan-state ownership semantics apply.

Workbench callers must never mutate ``state.json`` directly: enqueue a
command here and let the runner own the state transition.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import guards, harnesses, run_control, state

CONTROL_QUEUE_FILE_NAME = "control-queue.jsonl"

CommandName = Literal[
    "request-stop-after-current",
    "request-abort-turn",
    "select-future-harness",
    "set-implementation-model",
    "add-guard",
    "remove-guard",
]
COMMAND_REQUEST_STOP_AFTER_CURRENT: CommandName = "request-stop-after-current"
COMMAND_REQUEST_ABORT_TURN: CommandName = "request-abort-turn"
COMMAND_SELECT_FUTURE_HARNESS: CommandName = "select-future-harness"
COMMAND_SET_IMPLEMENTATION_MODEL: CommandName = "set-implementation-model"
COMMAND_ADD_GUARD: CommandName = "add-guard"
COMMAND_REMOVE_GUARD: CommandName = "remove-guard"
COMMAND_NAMES: tuple[CommandName, ...] = (
    COMMAND_REQUEST_STOP_AFTER_CURRENT,
    COMMAND_REQUEST_ABORT_TURN,
    COMMAND_SELECT_FUTURE_HARNESS,
    COMMAND_SET_IMPLEMENTATION_MODEL,
    COMMAND_ADD_GUARD,
    COMMAND_REMOVE_GUARD,
)

#: Guard mutations are the only commands scoped to one specific run rather
#: than to whichever runner happens to drain them. They carry the run ID the
#: operator saw, and the runner refuses one that names a different run.
GUARD_COMMAND_NAMES: tuple[CommandName, ...] = (
    COMMAND_ADD_GUARD,
    COMMAND_REMOVE_GUARD,
)

# Grace window an abort gives the worker before the turn is killed. Bounded on
# both ends: a non-positive window is a kill with no chance to wind down, and a
# window longer than a day cannot express "abort" in any useful sense.
DEFAULT_ABORT_GRACE_SECONDS = 120
MIN_ABORT_GRACE_SECONDS = 1
MAX_ABORT_GRACE_SECONDS = 86_400

# Reject reasons surfaced verbatim in the drain result so JSON consumers
# can branch on stable strings.
REJECT_INVALID_JSON = "invalid-json"
REJECT_NOT_OBJECT = "not-object"
REJECT_UNKNOWN_COMMAND = "unknown-command"
REJECT_MISSING_ID = "missing-id"
REJECT_DUPLICATE_ID = "duplicate-id"
REJECT_INVALID_HARNESS = "invalid-harness"
REJECT_MISSING_HARNESS = "missing-harness"
REJECT_INVALID_MODEL = "invalid-model"
REJECT_INVALID_EFFORT = "invalid-effort"
REJECT_INVALID_CLEAR_OPERATION = "invalid-clear-operation"
REJECT_CONFLICTING_MODEL_OPERATION = "conflicting-model-operation"
REJECT_CONFLICTING_EFFORT_OPERATION = "conflicting-effort-operation"
REJECT_MISSING_MODEL_CONTROL_OPERATION = "missing-model-control-operation"
REJECT_INVALID_GRACE = "invalid-grace"
REJECT_INVALID_REASON = "invalid-reason"
REJECT_MISSING_EXPECTED_RUN_ID = "missing-expected-run-id"
REJECT_MISSING_GUARD_RULE = "missing-guard-rule"
REJECT_MISSING_GUARD_ID = "missing-guard-id"
REJECT_INVALID_GUARD_ID = "invalid-guard-id"

#: A guard rule that cannot be parsed is rejected with the parser's own stable
#: reason, so the queue, the runner event, and the CLI all say the same thing
#: about the same malformed rule.
GUARD_RULE_REJECT_REASONS: tuple[str, ...] = (
    guards.REJECT_EMPTY_COMMAND_RULE,
    guards.REJECT_UNPARSABLE_COMMAND_RULE,
    guards.REJECT_EMPTY_TOKEN,
    guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
    guards.REJECT_SHELL_OPERATOR,
    guards.REJECT_GLOB,
    guards.REJECT_ENVIRONMENT_ASSIGNMENT,
)


class ControlQueueError(Exception):
    """Raised when the control queue cannot be appended to safely."""


@dataclass(frozen=True)
class ControlCommand:
    """One parsed, validated command read from the queue."""

    id: str
    command: CommandName
    harness: str | None
    created_at: str
    line_number: int
    model_operation: run_control.OverrideOperation = field(
        default_factory=run_control.OverrideOperation.unchanged
    )
    effort_operation: run_control.OverrideOperation = field(
        default_factory=run_control.OverrideOperation.unchanged
    )
    grace_seconds: int | None = None
    reason: str | None = None
    #: Parsed argv prefixes for an ``add-guard`` command. Parsed once at
    #: append time and again at drain, because the queue file is a durable
    #: interface an external producer may write directly.
    deny_commands: tuple[tuple[str, ...], ...] = ()
    guard_id: str | None = None
    #: The run the operator saw when they queued a guard mutation. A command
    #: that outlives that run is refused rather than applied to its successor.
    expected_run_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "harness": self.harness,
            "created_at": self.created_at,
            "line_number": self.line_number,
            "model_operation": self.model_operation.to_payload(),
            "effort_operation": self.effort_operation.to_payload(),
            "grace_seconds": self.grace_seconds,
            "reason": self.reason,
            "deny_commands": [list(rule) for rule in self.deny_commands],
            "guard_id": self.guard_id,
            "expected_run_id": self.expected_run_id,
        }


@dataclass(frozen=True)
class RejectedCommand:
    """One queue line that failed validation."""

    line_number: int
    reason: str
    raw: str
    detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "line_number": self.line_number,
            "reason": self.reason,
            "raw": self.raw,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DrainResult:
    """Outcome of one queue drain pass."""

    accepted: tuple[ControlCommand, ...] = ()
    rejected: tuple[RejectedCommand, ...] = ()
    queue_path: Path | None = None

    @property
    def consumed_ids(self) -> tuple[str, ...]:
        return tuple(cmd.id for cmd in self.accepted)

    def to_payload(self) -> dict[str, Any]:
        return {
            "accepted": [cmd.to_payload() for cmd in self.accepted],
            "rejected": [cmd.to_payload() for cmd in self.rejected],
            "queue_path": str(self.queue_path) if self.queue_path is not None else None,
        }


def queue_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the canonical queue path for ``plan_dir``."""
    return state.plan_state_dir(plan_dir, state_dir) / CONTROL_QUEUE_FILE_NAME


def _new_command_id() -> str:
    return uuid.uuid4().hex


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError(REJECT_MISSING_GUARD_RULE)
    parsed: list[tuple[str, ...]] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, str):
            raise ValueError(guards.REJECT_EMPTY_COMMAND_RULE)
        try:
            parsed.append(guards.parse_command_rule(raw_rule))
        except guards.GuardRuleError as error:
            raise ValueError(error.reason) from error
    return _GuardFields(
        deny_commands=tuple(parsed),
        expected_run_id=expected_run_id,
        reason=raw_reason,
    )


def append_command(
    plan_dir: str | Path,
    command: CommandName,
    *,
    state_dir: str | Path | None = None,
    harness: str | None = None,
    harness_model: str | None = None,
    clear_harness_model: bool = False,
    effort: str | None = None,
    clear_effort: bool = False,
    grace_seconds: int | None = None,
    reason: str | None = None,
    deny_commands: Sequence[str] | None = None,
    guard_id: str | None = None,
    expected_run_id: str | None = None,
    command_id: str | None = None,
    created_at: str | None = None,
) -> ControlCommand:
    """Append one command to the queue.

    Raises :class:`ControlQueueError` for malformed input. The queue file
    is created if missing. The append is serialized across processes
    through an advisory ``flock`` on the queue file descriptor.
    """
    if command not in COMMAND_NAMES:
        raise ControlQueueError(f"unsupported control command: {command!r}")
    if command_id is not None and not isinstance(command_id, str):
        raise ControlQueueError("command_id must be a string when provided")
    if command_id == "":
        raise ControlQueueError("command_id must not be empty")
    model_operation = run_control.OverrideOperation.unchanged()
    effort_operation = run_control.OverrideOperation.unchanged()
    model_control_record: dict[str, Any] = {}
    abort_record: dict[str, Any] = {}
    guard_record: dict[str, Any] = {}
    guard_parsed = _GuardFields()
    resolved_grace: int | None = None
    resolved_reason: str | None = None
    if command == COMMAND_SELECT_FUTURE_HARNESS:
        if harness is None or harness == "":
            raise ControlQueueError("select-future-harness requires a harness value")
        if harness not in harnesses.HARNESS_NAMES:
            raise ControlQueueError(
                f"unsupported harness for select-future-harness: {harness!r}"
            )
    elif command == COMMAND_SET_IMPLEMENTATION_MODEL:
        if harness is not None and harness not in harnesses.HARNESS_NAMES:
            raise ControlQueueError(
                f"unsupported harness for set-implementation-model: {harness!r}"
            )
        if not isinstance(clear_harness_model, bool):
            raise ControlQueueError(REJECT_INVALID_CLEAR_OPERATION)
        if not isinstance(clear_effort, bool):
            raise ControlQueueError(REJECT_INVALID_CLEAR_OPERATION)
        if harness_model is not None:
            model_control_record["harness_model"] = harness_model
        if clear_harness_model:
            model_control_record["clear_harness_model"] = True
        if effort is not None:
            model_control_record["effort"] = effort
        if clear_effort:
            model_control_record["clear_effort"] = True
        try:
            model_operation, effort_operation = _model_control_operations(
                model_control_record
            )
        except ValueError as error:
            raise ControlQueueError(str(error)) from error
    elif command == COMMAND_REQUEST_ABORT_TURN:
        harness = None
        if grace_seconds is not None:
            abort_record["grace_seconds"] = grace_seconds
        if reason is not None:
            abort_record["reason"] = reason
        try:
            resolved_grace, resolved_reason = _abort_fields(abort_record)
        except ValueError as error:
            raise ControlQueueError(str(error)) from error
        # The resolved grace is written back so the queue line is
        # self-describing for every later reader.
        abort_record["grace_seconds"] = resolved_grace
    elif command in GUARD_COMMAND_NAMES:
        harness = None
        if deny_commands is not None:
            guard_record["deny_commands"] = list(deny_commands)
        if guard_id is not None:
            guard_record["guard_id"] = guard_id
        if expected_run_id is not None:
            guard_record["expected_run_id"] = expected_run_id
        if reason is not None:
            guard_record["reason"] = reason
        try:
            guard_parsed = _guard_fields(command, guard_record)
        except ValueError as error:
            raise ControlQueueError(str(error)) from error
        resolved_reason = guard_parsed.reason
    else:
        # Other commands ignore harness; normalize to None for parity.
        harness = None

    record = {
        "id": command_id or _new_command_id(),
        "command": command,
        "harness": harness,
        "created_at": created_at or _utc_now_iso(),
        **model_control_record,
        **abort_record,
        **guard_record,
    }
    line = json.dumps(record, sort_keys=True) + "\n"

    queue_path = queue_path_for_plan(plan_dir, state_dir)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(queue_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    return ControlCommand(
        id=record["id"],
        command=record["command"],
        harness=record["harness"],
        created_at=record["created_at"],
        line_number=0,
        model_operation=model_operation,
        effort_operation=effort_operation,
        grace_seconds=resolved_grace,
        reason=resolved_reason,
        deny_commands=guard_parsed.deny_commands,
        guard_id=guard_parsed.guard_id,
        expected_run_id=guard_parsed.expected_run_id,
    )


def _read_and_truncate(queue_path: Path) -> str | None:
    """Atomically read the queue file contents and truncate it.

    Returns ``None`` when the file does not exist. The exclusive flock
    serializes with concurrent ``append_command`` callers so no command
    line is lost between the read and the truncate.
    """
    try:
        fd = os.open(queue_path, os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def drain_queue(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    seen_ids: Iterable[str] = (),
) -> DrainResult:
    """Drain the queue and return parsed accepted/rejected commands.

    ``seen_ids`` lists command IDs already consumed earlier in the same
    runner process. Lines whose IDs appear in ``seen_ids`` are rejected
    with ``REJECT_DUPLICATE_ID`` rather than re-applied. The queue file
    is truncated atomically so producers can keep appending in parallel.
    """
    queue_path = queue_path_for_plan(plan_dir, state_dir)
    text = _read_and_truncate(queue_path)
    if text is None:
        return DrainResult(queue_path=queue_path)

    accepted: list[ControlCommand] = []
    rejected: list[RejectedCommand] = []
    seen = set(seen_ids)

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as error:
            rejected.append(
                RejectedCommand(
                    line_number=line_number,
                    reason=REJECT_INVALID_JSON,
                    raw=raw_line,
                    detail=str(error),
                )
            )
            continue
        if not isinstance(payload, dict):
            rejected.append(
                RejectedCommand(
                    line_number=line_number,
                    reason=REJECT_NOT_OBJECT,
                    raw=raw_line,
                )
            )
            continue
        command_value = payload.get("command")
        if command_value not in COMMAND_NAMES:
            rejected.append(
                RejectedCommand(
                    line_number=line_number,
                    reason=REJECT_UNKNOWN_COMMAND,
                    raw=raw_line,
                    detail=str(command_value),
                )
            )
            continue
        cmd_id = payload.get("id")
        if not isinstance(cmd_id, str) or cmd_id == "":
            rejected.append(
                RejectedCommand(
                    line_number=line_number,
                    reason=REJECT_MISSING_ID,
                    raw=raw_line,
                )
            )
            continue
        if cmd_id in seen:
            rejected.append(
                RejectedCommand(
                    line_number=line_number,
                    reason=REJECT_DUPLICATE_ID,
                    raw=raw_line,
                    detail=cmd_id,
                )
            )
            continue
        harness_value = payload.get("harness")
        model_operation = run_control.OverrideOperation.unchanged()
        effort_operation = run_control.OverrideOperation.unchanged()
        grace_value: int | None = None
        reason_value: str | None = None
        guard_parsed = _GuardFields()
        if command_value == COMMAND_SELECT_FUTURE_HARNESS:
            if harness_value is None or harness_value == "":
                rejected.append(
                    RejectedCommand(
                        line_number=line_number,
                        reason=REJECT_MISSING_HARNESS,
                        raw=raw_line,
                    )
                )
                continue
            if (
                not isinstance(harness_value, str)
                or harness_value not in harnesses.HARNESS_NAMES
            ):
                rejected.append(
                    RejectedCommand(
                        line_number=line_number,
                        reason=REJECT_INVALID_HARNESS,
                        raw=raw_line,
                        detail=str(harness_value),
                    )
                )
                continue
        elif command_value == COMMAND_SET_IMPLEMENTATION_MODEL:
            if harness_value is not None and (
                not isinstance(harness_value, str)
                or harness_value not in harnesses.HARNESS_NAMES
            ):
                rejected.append(
                    RejectedCommand(
                        line_number=line_number,
                        reason=REJECT_INVALID_HARNESS,
                        raw=raw_line,
                        detail=str(harness_value),
                    )
                )
                continue
            try:
                model_operation, effort_operation = _model_control_operations(payload)
            except ValueError as error:
                reason = str(error)
                rejected.append(
                    RejectedCommand(
                        line_number=line_number,
                        reason=reason,
                        raw=raw_line,
                    )
                )
                continue
        elif command_value == COMMAND_REQUEST_ABORT_TURN:
            harness_value = None
            try:
                grace_value, reason_value = _abort_fields(payload)
            except ValueError as error:
                rejected.append(
                    RejectedCommand(
                        line_number=line_number,
                        reason=str(error),
                        raw=raw_line,
                    )
                )
                continue
        elif command_value in GUARD_COMMAND_NAMES:
            harness_value = None
            try:
                guard_parsed = _guard_fields(command_value, payload)
            except ValueError as error:
                rejected.append(
                    RejectedCommand(
                        line_number=line_number,
                        reason=str(error),
                        raw=raw_line,
                    )
                )
                continue
            reason_value = guard_parsed.reason
        else:
            harness_value = None

        created_at = payload.get("created_at")
        if not isinstance(created_at, str):
            created_at = ""

        accepted.append(
            ControlCommand(
                id=cmd_id,
                command=command_value,
                harness=harness_value,
                created_at=created_at,
                line_number=line_number,
                model_operation=model_operation,
                effort_operation=effort_operation,
                grace_seconds=grace_value,
                reason=reason_value,
                deny_commands=guard_parsed.deny_commands,
                guard_id=guard_parsed.guard_id,
                expected_run_id=guard_parsed.expected_run_id,
            )
        )
        seen.add(cmd_id)

    return DrainResult(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        queue_path=queue_path,
    )
