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
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import harnesses, run_control
from .control_queue_drain import (
    _read_and_truncate as _read_and_truncate,
)
from .control_queue_drain import (
    drain_queue as drain_queue,
)
from .control_queue_drain import (
    requeue_lines as requeue_lines,
)
from .control_queue_fields import (
    _abort_fields as _abort_fields,
)
from .control_queue_fields import (
    _guard_fields as _guard_fields,
)
from .control_queue_fields import (
    _GuardFields as _GuardFields,
)
from .control_queue_fields import (
    _model_control_operations as _model_control_operations,
)
from .control_queue_fields import (
    _operation_from_fields as _operation_from_fields,
)
from .control_queue_models import (
    COMMAND_ADD_GUARD as COMMAND_ADD_GUARD,
)
from .control_queue_models import (
    COMMAND_NAMES as COMMAND_NAMES,
)
from .control_queue_models import (
    COMMAND_REMOVE_GUARD as COMMAND_REMOVE_GUARD,
)
from .control_queue_models import (
    COMMAND_REQUEST_ABORT_TURN as COMMAND_REQUEST_ABORT_TURN,
)
from .control_queue_models import (
    COMMAND_REQUEST_STOP_AFTER_CURRENT as COMMAND_REQUEST_STOP_AFTER_CURRENT,
)
from .control_queue_models import (
    COMMAND_SELECT_FUTURE_HARNESS as COMMAND_SELECT_FUTURE_HARNESS,
)
from .control_queue_models import (
    COMMAND_SET_IMPLEMENTATION_MODEL as COMMAND_SET_IMPLEMENTATION_MODEL,
)
from .control_queue_models import (
    CONTROL_QUEUE_FILE_NAME as CONTROL_QUEUE_FILE_NAME,
)
from .control_queue_models import (
    DEFAULT_ABORT_GRACE_SECONDS as DEFAULT_ABORT_GRACE_SECONDS,
)
from .control_queue_models import (
    GUARD_COMMAND_NAMES as GUARD_COMMAND_NAMES,
)
from .control_queue_models import (
    GUARD_RULE_REJECT_REASONS as GUARD_RULE_REJECT_REASONS,
)
from .control_queue_models import (
    MAX_ABORT_GRACE_SECONDS as MAX_ABORT_GRACE_SECONDS,
)
from .control_queue_models import (
    MIN_ABORT_GRACE_SECONDS as MIN_ABORT_GRACE_SECONDS,
)
from .control_queue_models import (
    REJECT_CONFLICTING_EFFORT_OPERATION as REJECT_CONFLICTING_EFFORT_OPERATION,
)
from .control_queue_models import (
    REJECT_CONFLICTING_MODEL_OPERATION as REJECT_CONFLICTING_MODEL_OPERATION,
)
from .control_queue_models import (
    REJECT_DUPLICATE_ID as REJECT_DUPLICATE_ID,
)
from .control_queue_models import (
    REJECT_INVALID_CLEAR_OPERATION as REJECT_INVALID_CLEAR_OPERATION,
)
from .control_queue_models import (
    REJECT_INVALID_EFFORT as REJECT_INVALID_EFFORT,
)
from .control_queue_models import (
    REJECT_INVALID_GRACE as REJECT_INVALID_GRACE,
)
from .control_queue_models import (
    REJECT_INVALID_GUARD_ID as REJECT_INVALID_GUARD_ID,
)
from .control_queue_models import (
    REJECT_INVALID_HARNESS as REJECT_INVALID_HARNESS,
)
from .control_queue_models import (
    REJECT_INVALID_JSON as REJECT_INVALID_JSON,
)
from .control_queue_models import (
    REJECT_INVALID_MODEL as REJECT_INVALID_MODEL,
)
from .control_queue_models import (
    REJECT_INVALID_REASON as REJECT_INVALID_REASON,
)
from .control_queue_models import (
    REJECT_MISSING_EXPECTED_RUN_ID as REJECT_MISSING_EXPECTED_RUN_ID,
)
from .control_queue_models import (
    REJECT_MISSING_GUARD_ID as REJECT_MISSING_GUARD_ID,
)
from .control_queue_models import (
    REJECT_MISSING_GUARD_RULE as REJECT_MISSING_GUARD_RULE,
)
from .control_queue_models import (
    REJECT_MISSING_HARNESS as REJECT_MISSING_HARNESS,
)
from .control_queue_models import (
    REJECT_MISSING_ID as REJECT_MISSING_ID,
)
from .control_queue_models import (
    REJECT_MISSING_MODEL_CONTROL_OPERATION as REJECT_MISSING_MODEL_CONTROL_OPERATION,
)
from .control_queue_models import (
    REJECT_NOT_OBJECT as REJECT_NOT_OBJECT,
)
from .control_queue_models import (
    REJECT_UNKNOWN_COMMAND as REJECT_UNKNOWN_COMMAND,
)
from .control_queue_models import (
    CommandName as CommandName,
)
from .control_queue_models import (
    ControlCommand as ControlCommand,
)
from .control_queue_models import (
    ControlQueueError as ControlQueueError,
)
from .control_queue_models import (
    DrainResult as DrainResult,
)
from .control_queue_models import (
    RejectedCommand as RejectedCommand,
)
from .control_queue_models import (
    queue_path_for_plan as queue_path_for_plan,
)


def _new_command_id() -> str:
    return uuid.uuid4().hex


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    protect_paths: Sequence[str] | None = None,
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
        if protect_paths is not None:
            guard_record["protect_paths"] = list(protect_paths)
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
        # The caller is told the command is queued once this returns, so the
        # line has to survive a crash from here on.
        os.fsync(fd)
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
        protect_paths=guard_parsed.protect_paths,
        guard_id=guard_parsed.guard_id,
        expected_run_id=guard_parsed.expected_run_id,
    )
