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

from . import durability


# Schema version stamped on every event record. Readers must treat the
# absence of a ``version`` key as v1 and accept v2 records that carry it.
EVENT_SCHEMA_VERSION = 2

RUNNER_STATUS_PREFIX = "Runner status:"
RUNNER_PAUSE_PREFIX = "RUNNER_PAUSE:"
RUN_PAUSED_EVENT = "run_paused"
RUN_FINISHED_EVENT = "run_finished"
# ``run_finished.outcome`` for a run reconstructed from proof of death rather
# than finished by its own runner. Reusing the ``run_finished`` name keeps
# every terminal-event consumer (tail, compaction, autopilot) working unchanged.
RUN_OUTCOME_ABANDONED = "abandoned"
TURN_HEARTBEAT_EVENT = "turn_heartbeat"
TURN_HANG_SUSPECTED_EVENT = "turn_hang_suspected"
TURN_HANG_CLEARED_EVENT = "turn_hang_cleared"
ACTION_REQUIRED_EVENT = "action_required"
COMMIT_STARTED_EVENT = "commit_started"
COMMIT_FINISHED_EVENT = "commit_finished"
COMMIT_GROUP_STARTED_EVENT = "commit_group_started"
COMMIT_GROUP_UPDATED_EVENT = "commit_group_updated"
COMMIT_GROUP_FINISHED_EVENT = "commit_group_finished"
RUN_CONTROL_EVENT = "run_control"
MAILBOX_EVENT = "mailbox"
REVIEW_SESSION_CREATED_EVENT = "review_session_created"
REVIEW_SCOPE_EVENT = "review_scope_selected"
REVIEW_CYCLE_EVENT = "review_cycle"
REVIEW_REPAIR_REOPENED_EVENT = "review_repair_reopened"
WORKSPACE_WRITE_SCOPE_REPAIR_EVENT = "workspace_write_scope_repair"
# Appended when a review turn produced a verdict that failed validation and the
# runner re-ran the same review session in-run instead of pausing. The
# exhausted case keeps the ``verify-failed`` pause and carries a
# ``review_verdict_repair_exhausted`` detail instead of a new pause reason.
REVIEW_VERDICT_RETRY_EVENT = "review_verdict_retry"
REPLY_PROVIDED_EVENT = "reply_provided"
RESOLUTION_CONSUMED_EVENT = "resolution_consumed"
VERIFICATION_STARTED_EVENT = "verification_started"
VERIFICATION_CHECK_STARTED_EVENT = "verification_check_started"
VERIFICATION_HEARTBEAT_EVENT = "verification_heartbeat"
VERIFICATION_CHECK_FINISHED_EVENT = "verification_check_finished"
VERIFICATION_CHECK_SKIPPED_EVENT = "verification_check_skipped"
VERIFICATION_FINISHED_EVENT = "verification_finished"
PREFLIGHT_AUTO_REPAIRED_EVENT = "preflight_auto_repaired"
PREFLIGHT_SKIPPED_EVENT = "preflight_skipped"
AGENT_MUX_DELIVERY_EVENT = "agent_mux_delivery"
# Appended after a harness turn or a verification check whose post-run process
# sweep actually killed something (or failed to). A sweep that found nothing is
# the normal case and is not recorded.
PROCESS_SWEEP_EVENT = "process_sweep"
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
MAILBOX_ACTION_NOTICE_PUBLISHED = "notice-published"
MAILBOX_ACTION_WAIT_PREPARATION_FAILED = "wait-preparation-failed"
MAILBOX_ACTION_WAIT_STARTED = "wait-started"
MAILBOX_ACTION_WAIT_CLEARED = "wait-cleared"
MAILBOX_ACTIONS: tuple[str, ...] = (
    MAILBOX_ACTION_GUIDANCE_OBSERVED,
    MAILBOX_ACTION_NOTICE_PUBLISHED,
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
# Registered implementation turns stay disabled until their write guard can
# prove mutations remained inside the selected session's writable aliases.
PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE = (
    "workspace-write-guard-unavailable"
)
# A registered implementation turn changed a repository outside the selected
# session's authoritative write aliases. Contents are deliberately retained;
# the session is returned to pending so an operator can repair the durable
# scope and refresh expansion/preflight evidence.
PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION = (
    "workspace-write-scope-violation"
)
# A legacy run resolved declarations reaching more than one Git top-level. The
# value matches ``repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED``; it is
# spelled literally here so the low-level event vocabulary keeps no dependency
# on the resolution engine.
PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED = "repository-registry-required"

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
    PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
    PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
    PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
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
    """Append one event record and issue a platform device-level fullsync."""
    event_path = Path(path)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True) + "\n"
    with _EVENT_WRITE_LOCK:
        with event_path.open("a", encoding="utf-8") as event_file:
            event_file.write(line)
            event_file.flush()
            durability.fullsync(event_file.fileno())
