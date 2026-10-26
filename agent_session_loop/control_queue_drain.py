"""Atomic read-and-truncate drain of the external control queue."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import durability, harnesses, run_control
from .control_queue_fields import (
    _abort_fields,
    _guard_fields,
    _GuardFields,
    _model_control_operations,
)
from .control_queue_models import (
    COMMAND_NAMES,
    COMMAND_REQUEST_ABORT_TURN,
    COMMAND_SELECT_FUTURE_HARNESS,
    COMMAND_SET_IMPLEMENTATION_MODEL,
    GUARD_COMMAND_NAMES,
    REJECT_DUPLICATE_ID,
    REJECT_INVALID_HARNESS,
    REJECT_INVALID_JSON,
    REJECT_MISSING_HARNESS,
    REJECT_MISSING_ID,
    REJECT_NOT_OBJECT,
    REJECT_UNKNOWN_COMMAND,
    ControlCommand,
    DrainResult,
    RejectedCommand,
    queue_path_for_plan,
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


def requeue_lines(queue_path: Path, lines: Sequence[str]) -> None:
    """Put drained lines back at the head of the queue, ahead of newer appends.

    The drain truncates before anything is applied, so a consumer that fails
    partway through a batch calls this with the lines it never reached; without
    it those commands would be gone although their producers were told they
    were queued. They go back in front of whatever was appended since, under the
    same exclusive lock producers take, so queue order is preserved.

    The rewrite happens in place because producers lock the queue path itself,
    so swapping the inode would let an append land in the orphaned file. A
    rewrite that fails after the truncate therefore restores the bytes that were
    there before re-raising: the requeued lines are lost, but nothing a producer
    appended since is.
    """
    if not lines:
        return
    restored = "".join(line if line.endswith("\n") else f"{line}\n" for line in lines)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(queue_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        original = b"".join(chunks)
        try:
            _rewrite(fd, restored.encode("utf-8") + original)
        except BaseException as error:
            try:
                _rewrite(fd, original)
            except OSError as restore_error:
                error.add_note(
                    f"restoring the control queue {queue_path} also failed: "
                    f"{restore_error}"
                )
            raise
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _rewrite(fd: int, payload: bytes) -> None:
    """Replace the whole content of ``fd`` with ``payload`` and flush it."""
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    data = memoryview(payload)
    while data:
        data = data[os.write(fd, data) :]
    durability.fullsync(fd)


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
                protect_paths=guard_parsed.protect_paths,
                guard_id=guard_parsed.guard_id,
                expected_run_id=guard_parsed.expected_run_id,
                raw=raw_line,
            )
        )
        seen.add(cmd_id)

    return DrainResult(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        queue_path=queue_path,
    )
