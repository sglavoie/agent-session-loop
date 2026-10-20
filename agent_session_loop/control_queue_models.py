"""Constants, error type and value objects for the external control queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import guards, run_control, state

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
    guards.REJECT_EMPTY_GUARD_RULE,
    guards.REJECT_EMPTY_PATH_RULE,
    guards.REJECT_PATH_MISSING_QUALIFIER,
    guards.REJECT_PATH_UNKNOWN_ALIAS,
    guards.REJECT_PATH_ABSOLUTE,
    guards.REJECT_PATH_BACKSLASH,
    guards.REJECT_PATH_TRAVERSAL,
    guards.REJECT_PATH_EMPTY_SEGMENT,
    guards.REJECT_PATH_PATTERN,
    guards.REJECT_PROTECTED_PATH_EVIDENCE,
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
    #: Parsed ``Alias:glob`` protections for an ``add-guard`` command. Kept
    #: beside ``deny_commands`` rather than folded into it because the two are
    #: enforced by different machinery and one command may carry both.
    protect_paths: tuple[guards.ProtectedPath, ...] = ()
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
            "protect_paths": [path.rendered for path in self.protect_paths],
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
