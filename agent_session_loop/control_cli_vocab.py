"""Stable control-command names and operator-facing notes."""

from __future__ import annotations

# Command names used in both success and error payloads, so a supervisor can
# branch on `command` without inspecting argv.
COMMAND_STOP_AFTER_CURRENT = "control-stop-after-current"
COMMAND_ABORT = "control-abort"
COMMAND_SELECT_HARNESS = "control-select-harness"
COMMAND_SET_MODEL = "control-set-model"
COMMAND_GUARD_ADD = "control-guard-add"
COMMAND_GUARD_REMOVE = "control-guard-remove"
COMMAND_GUARD_LIST = "control-guard-list"

# Shared note for both subcommands: enqueueing always succeeds, but nothing
# consumes the queue until some runner drains it.
NO_ACTIVE_RUN_NOTE = (
    "no active run; command applies when a runner next drains the queue"
)
STOP_WILL_APPLY_NOTE = "will stop after current session"
STOP_BEFORE_REVIEW_NOTE = (
    "current session is the last; stop also prevents the pending review"
)
STOP_NOOP_NOTE = "current session is the last; stop is a no-op"
ABORT_WILL_APPLY_NOTE = "a turn is active; abort will end it"
ABORT_DEGRADES_NOTE = "no turn active; abort degrades to stop-after-current"
ABORT_CANCELS_FINALIZATION_NOTE = (
    "no turn active; the run is finalizing, so abort cancels its terminal audit"
)
MODEL_APPLIES_TO = "next-implementation-turn"
GUARD_ADD_NOTE = "enforcement begins once the runner reports the guard applied"
GUARD_REMOVE_NOTE = "enforcement ends once the runner reports the guard removed"

# A wakeup usually makes the runner drain within milliseconds, so the receipt
# tail polls far more often than the mailbox waits do.
CONTROL_WAIT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS = 60.0
MAX_CONTROL_WAIT_TIMEOUT_SECONDS = 30.0 * 60.0

_EVENTS_TOKEN_UNSEEN = object()
_EVENTS_TOKEN_UNAVAILABLE = object()


FINALIZING_NOTE = (
    "the turn ended, but the run is still finalizing its terminal audit and "
    "holds the plan lock; repeat `asl control abort` to cancel the audit"
)
