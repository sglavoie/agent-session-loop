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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import uuid
from typing import Any, Literal

from . import harnesses, state


CONTROL_QUEUE_FILE_NAME = "control-queue.jsonl"

CommandName = Literal[
    "request-stop-after-current",
    "select-future-harness",
]
COMMAND_REQUEST_STOP_AFTER_CURRENT: CommandName = "request-stop-after-current"
COMMAND_SELECT_FUTURE_HARNESS: CommandName = "select-future-harness"
COMMAND_NAMES: tuple[CommandName, ...] = (
    COMMAND_REQUEST_STOP_AFTER_CURRENT,
    COMMAND_SELECT_FUTURE_HARNESS,
)

# Reject reasons surfaced verbatim in the drain result so JSON consumers
# can branch on stable strings.
REJECT_INVALID_JSON = "invalid-json"
REJECT_NOT_OBJECT = "not-object"
REJECT_UNKNOWN_COMMAND = "unknown-command"
REJECT_MISSING_ID = "missing-id"
REJECT_DUPLICATE_ID = "duplicate-id"
REJECT_INVALID_HARNESS = "invalid-harness"
REJECT_MISSING_HARNESS = "missing-harness"


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

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "harness": self.harness,
            "created_at": self.created_at,
            "line_number": self.line_number,
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
    return datetime.now(timezone.utc).isoformat()


def append_command(
    plan_dir: str | Path,
    command: CommandName,
    *,
    state_dir: str | Path | None = None,
    harness: str | None = None,
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
    if command == COMMAND_SELECT_FUTURE_HARNESS:
        if harness is None or harness == "":
            raise ControlQueueError(
                "select-future-harness requires a harness value"
            )
        if harness not in harnesses.HARNESS_NAMES:
            raise ControlQueueError(
                f"unsupported harness for select-future-harness: {harness!r}"
            )
    else:
        # Other commands ignore harness; normalize to None for parity.
        harness = None

    record = {
        "id": command_id or _new_command_id(),
        "command": command,
        "harness": harness,
        "created_at": created_at or _utc_now_iso(),
    }
    line = json.dumps(record, sort_keys=True) + "\n"

    queue_path = queue_path_for_plan(plan_dir, state_dir)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(queue_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    return ControlCommand(
        id=record["id"],
        command=record["command"],
        harness=record["harness"],
        created_at=record["created_at"],
        line_number=0,
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
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
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
            )
        )
        seen.add(cmd_id)

    return DrainResult(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        queue_path=queue_path,
    )
