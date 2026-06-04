"""Event records emitted by the runner.

Events are operational metadata only. Durable session completion truth remains
in the plan directory's `session-N.md` handoff status lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


RUNNER_STATUS_PREFIX = "Runner status:"
RUNNER_PAUSE_PREFIX = "RUNNER_PAUSE:"
RUN_PAUSED_EVENT = "run_paused"
STATE_FILE_NAME = "state.json"
EVENTS_FILE_NAME = "events.jsonl"
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
)


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
    with event_path.open("a", encoding="utf-8") as event_file:
        event_file.write(json.dumps(event.to_dict(), sort_keys=True))
        event_file.write("\n")
