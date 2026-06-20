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


RUNNER_STATUS_PREFIX = "Runner status:"
RUNNER_PAUSE_PREFIX = "RUNNER_PAUSE:"
RUN_PAUSED_EVENT = "run_paused"
TURN_HEARTBEAT_EVENT = "turn_heartbeat"
ACTION_REQUIRED_EVENT = "action_required"
COMMIT_STARTED_EVENT = "commit_started"
COMMIT_FINISHED_EVENT = "commit_finished"
RUN_CONTROL_EVENT = "run_control"
REVIEW_SESSION_CREATED_EVENT = "review_session_created"
REPLY_PROVIDED_EVENT = "reply_provided"
STATE_FILE_NAME = "state.json"
EVENTS_FILE_NAME = "events.jsonl"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED = "stop-after-current-requested"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED = "stop-after-current-canceled"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED = "stop-after-current-consumed"
RUN_CONTROL_ACTION_TAIL_ENABLED = "tail-enabled"
RUN_CONTROL_ACTION_TAIL_DISABLED = "tail-disabled"
RUN_CONTROL_ACTION_HARNESS_SELECTED = "harness-selected"
RUN_CONTROL_ACTIONS: tuple[str, ...] = (
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED,
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
    RUN_CONTROL_ACTION_TAIL_ENABLED,
    RUN_CONTROL_ACTION_TAIL_DISABLED,
    RUN_CONTROL_ACTION_HARNESS_SELECTED,
)
PAUSE_REASONS: tuple[str, ...] = (
    "nonzero-exit",
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
    "interrupted",
    "review-harness-required",
    "review-mutated-worktree",
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "name": self.name,
            "payload": self.payload,
        }


def append_event(path: str | Path, event: Event) -> None:
    """Append one event record as a JSON line."""
    event_path = Path(path)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True) + "\n"
    with _EVENT_WRITE_LOCK:
        with event_path.open("a", encoding="utf-8") as event_file:
            event_file.write(line)
