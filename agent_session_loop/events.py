"""Event records emitted by the runner.

Events are operational metadata only. Durable session completion truth remains
in the plan directory's `session-N.md` handoff status lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any


# Schema version stamped on every event record. Readers must treat the
# absence of a ``version`` key as v1 and accept v2 records that carry it.
EVENT_SCHEMA_VERSION = 2

RUNNER_STATUS_PREFIX = "Runner status:"
RUNNER_PAUSE_PREFIX = "RUNNER_PAUSE:"
RUN_PAUSED_EVENT = "run_paused"
TURN_HEARTBEAT_EVENT = "turn_heartbeat"
TURN_HANG_SUSPECTED_EVENT = "turn_hang_suspected"
TURN_HANG_CLEARED_EVENT = "turn_hang_cleared"
ACTION_REQUIRED_EVENT = "action_required"
COMMIT_STARTED_EVENT = "commit_started"
COMMIT_FINISHED_EVENT = "commit_finished"
RUN_CONTROL_EVENT = "run_control"
MAILBOX_EVENT = "mailbox"
REVIEW_SESSION_CREATED_EVENT = "review_session_created"
REVIEW_SCOPE_EVENT = "review_scope_selected"
REVIEW_CYCLE_EVENT = "review_cycle"
REVIEW_REPAIR_REOPENED_EVENT = "review_repair_reopened"
REPLY_PROVIDED_EVENT = "reply_provided"
VERIFICATION_STARTED_EVENT = "verification_started"
VERIFICATION_CHECK_STARTED_EVENT = "verification_check_started"
VERIFICATION_HEARTBEAT_EVENT = "verification_heartbeat"
VERIFICATION_CHECK_FINISHED_EVENT = "verification_check_finished"
VERIFICATION_FINISHED_EVENT = "verification_finished"
PREFLIGHT_AUTO_REPAIRED_EVENT = "preflight_auto_repaired"
PREFLIGHT_SKIPPED_EVENT = "preflight_skipped"
AGENT_MUX_DELIVERY_EVENT = "agent_mux_delivery"
STATE_FILE_NAME = "state.json"
EVENTS_FILE_NAME = "events.jsonl"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED = "stop-after-current-requested"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED = "stop-after-current-canceled"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED = "stop-after-current-consumed"
RUN_CONTROL_ACTION_TAIL_ENABLED = "tail-enabled"
RUN_CONTROL_ACTION_TAIL_DISABLED = "tail-disabled"
RUN_CONTROL_ACTION_HARNESS_SELECTED = "harness-selected"
RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED = "implementation-model-updated"
RUN_CONTROL_ACTIONS: tuple[str, ...] = (
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED,
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
    RUN_CONTROL_ACTION_TAIL_ENABLED,
    RUN_CONTROL_ACTION_TAIL_DISABLED,
    RUN_CONTROL_ACTION_HARNESS_SELECTED,
    RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
)
MAILBOX_ACTION_GUIDANCE_OBSERVED = "guidance-observed"
MAILBOX_ACTION_WAIT_PREPARATION_FAILED = "wait-preparation-failed"
MAILBOX_ACTION_WAIT_STARTED = "wait-started"
MAILBOX_ACTION_WAIT_CLEARED = "wait-cleared"
MAILBOX_ACTIONS: tuple[str, ...] = (
    MAILBOX_ACTION_GUIDANCE_OBSERVED,
    MAILBOX_ACTION_WAIT_PREPARATION_FAILED,
    MAILBOX_ACTION_WAIT_STARTED,
    MAILBOX_ACTION_WAIT_CLEARED,
)
# Stable, evidence-based harness-failure categories produced by
# ``agent_session_loop.runner.classify_harness_failure``. A nonzero harness
# exit is classified into exactly one of these; ``nonzero-exit`` remains the
# fail-closed fallback for unrecognized (and legacy) failures. The higher-level
# recoverable / user-required pause policy that composes from these atoms lives
# in ``agent_session_loop.pause_policy`` (the single source of truth consumed by
# the runner, ``asl status``/``asl questions``, and the supervisor helper).
PAUSE_REASON_NONZERO_EXIT = "nonzero-exit"
PAUSE_REASON_QUOTA_EXHAUSTED = "quota-exhausted"
PAUSE_REASON_AUTH_FAILED = "auth-failed"
PAUSE_REASON_INVALID_MODEL = "invalid-model"
PAUSE_REASON_TRANSPORT_FAILED = "transport-failed"
PAUSE_REASON_INTERRUPTED = "interrupted"
# Review repair-cycle materialization could not produce safe repair sessions
# (see ``agent_session_loop.review_repair.REVIEW_REPAIR_REASON_MALFORMED``).
PAUSE_REASON_MALFORMED_REVIEW_REPAIR = "malformed-review-repair"
# A git probe the review mutation guard depends on failed, so the guard cannot
# prove the review turn left the worktree alone.
PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE = "review-guard-unavailable"

# The subset of harness-failure categories that a supervisor may safely retry
# by relaunching the same session. Quota, authentication, invalid-model, and
# unclassified (fail-closed ``nonzero-exit``) failures are never in this set.
RETRYABLE_HARNESS_FAILURE_REASONS: tuple[str, ...] = (
    PAUSE_REASON_TRANSPORT_FAILED,
)

PAUSE_REASONS: tuple[str, ...] = (
    PAUSE_REASON_NONZERO_EXIT,
    "timeout",
    "no-status-change",
    "multiple-status-changes",
    "malformed-plan",
    "unknown-status",
    "lock-contention",
    "runner-pause",
    "unexpected-status-change",
    "no-pending-but-incomplete",
    "commit-preflight",
    "commit-failed",
    "harness-unavailable",
    "not-loop-ready",
    "unsafe",
    "blocked",
    "dependent",
    "unresolved-prerequisite",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    PAUSE_REASON_INTERRUPTED,
    "review-harness-required",
    "review-mutated-worktree",
    PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
    PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
    PAUSE_REASON_QUOTA_EXHAUSTED,
    PAUSE_REASON_AUTH_FAILED,
    PAUSE_REASON_INVALID_MODEL,
    PAUSE_REASON_TRANSPORT_FAILED,
)
_EVENT_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Event:
    """Structured event record for JSONL persistence."""

    name: str
    payload: dict[str, Any]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    version: int = EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "name": self.name,
            "payload": self.payload,
            "version": self.version,
        }


def append_event(path: str | Path, event: Event) -> None:
    """Append one event record as a JSON line."""
    event_path = Path(path)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True) + "\n"
    with _EVENT_WRITE_LOCK:
        with event_path.open("a", encoding="utf-8") as event_file:
            event_file.write(line)
