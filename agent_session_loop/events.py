"""Event records emitted by the runner.

Events are operational metadata only. Durable session completion truth remains
in the plan directory's `session-N.md` handoff status lines.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import durability

# Schema version stamped on every event record. Readers must treat the
# absence of a ``version`` key as v1 and accept v2 records that carry it.
EVENT_SCHEMA_VERSION = 2

RUNNER_STATUS_PREFIX = "Runner status:"
RUNNER_PAUSE_PREFIX = "RUNNER_PAUSE:"
RUN_STARTED_EVENT = "run_started"
RUN_PAUSED_EVENT = "run_paused"
RUN_FINISHED_EVENT = "run_finished"
# ``run_finished.outcome`` for a run reconstructed from proof of death rather
# than finished by its own runner. Reusing the ``run_finished`` name keeps
# every terminal-event consumer (tail, compaction, autopilot) working unchanged.
RUN_OUTCOME_ABANDONED = "abandoned"
TURN_HEARTBEAT_EVENT = "turn_heartbeat"
# Appended when the harness process for a turn has exited. It states execution
# only — exit code, timing, and where the raw artifacts landed. Whether the turn
# is accepted is a separate statement carried by ``turn_finished``.
TURN_EXECUTION_FINISHED_EVENT = "turn_execution_finished"
# Appended once a turn has passed every required post-execution guard. Its
# payload is the committed turn result, already written to ``result.json`` and
# stored in the run state before this event names either, and nothing rewrites
# them afterwards.
TURN_FINISHED_EVENT = "turn_finished"
# Appended when a post-execution guard refused a turn that had already
# executed. A refused turn never receives ``turn_finished``, so this is the
# terminal statement about it.
TURN_REJECTED_EVENT = "turn_rejected"
# Appended when a guard returned a session the worker had marked complete back
# to pending, so the reverse transition is in the event stream instead of being
# visible only by comparing the final plan against the last turn record.
SESSION_REOPENED_EVENT = "session_reopened"
# Appended when a mechanically malformed post-turn handoff was returned to the
# worker for one bounded correction instead of pausing the run outright.
TURN_HANDOFF_CORRECTION_EVENT = "turn_handoff_correction"
TURN_HANG_SUSPECTED_EVENT = "turn_hang_suspected"
TURN_HANG_CLEARED_EVENT = "turn_hang_cleared"
ACTION_REQUIRED_EVENT = "action_required"
COMMIT_STARTED_EVENT = "commit_started"
COMMIT_FINISHED_EVENT = "commit_finished"
COMMIT_GROUP_STARTED_EVENT = "commit_group_started"
COMMIT_GROUP_UPDATED_EVENT = "commit_group_updated"
COMMIT_GROUP_FINISHED_EVENT = "commit_group_finished"
# Appended when a recoverable pause settled the retained work of the session it
# paused on, so the relaunch meets a clean tree instead of the startup commit
# gate. Its group carries the `session-paused` scope, which is what keeps that
# commit's identity distinct from the accepted commit the same session earns
# later; the pause reason is unchanged, so this event is the only statement
# that the settlement happened.
PAUSED_WORK_COMMITTED_EVENT = "paused_work_committed"
REPAIR_COMMIT_PREFLIGHT_EVENT = "repair_commit_preflight"
RUN_CONTROL_EVENT = "run_control"
MAILBOX_EVENT = "mailbox"
REVIEW_SESSION_CREATED_EVENT = "review_session_created"
REVIEW_SCOPE_EVENT = "review_scope_selected"
REVIEW_CYCLE_EVENT = "review_cycle"
REVIEW_REPAIR_REOPENED_EVENT = "review_repair_reopened"
WORKSPACE_WRITE_SCOPE_REPAIR_EVENT = "workspace_write_scope_repair"
WORKSPACE_EXTERNAL_CHANGES_ACKNOWLEDGED_EVENT = (
    "workspace_external_changes_acknowledged"
)
WORKSPACE_REF_UPDATES_ACCEPTED_EVENT = "workspace_ref_updates_accepted"
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
# Appended around the terminal work a run performs after its last turn ended —
# today the terminal supervisor audit. Without them a run that has already
# recorded its outcome but has not yet released the lock is indistinguishable
# from a hang.
FINALIZATION_STARTED_EVENT = "finalization_started"
FINALIZATION_HEARTBEAT_EVENT = "finalization_heartbeat"
FINALIZATION_FINISHED_EVENT = "finalization_finished"
PREFLIGHT_AUTO_REPAIRED_EVENT = "preflight_auto_repaired"
PREFLIGHT_SKIPPED_EVENT = "preflight_skipped"
AGENT_MUX_DELIVERY_EVENT = "agent_mux_delivery"
# Appended after a harness turn or a verification check whose post-run process
# sweep actually killed something (or failed to). A sweep that found nothing is
# the normal case and is not recorded.
PROCESS_SWEEP_EVENT = "process_sweep"
# Appended when a harness lost an auxiliary transport on the way up and ran the
# turn anyway. It states a degradation, not a failure: the payload carries no
# pause or failure reason, so the turn keeps the status its own exit code
# earned, and a closure that recurs every launch is recorded instead of silent.
HARNESS_DEGRADED_EVENT = "harness_degraded"
# Appended once per turn, before the harness starts, stating which conversation
# the turn was decided to run on and why. Recorded even when the answer is
# "open a new one", because the interesting question months later is which turns
# did *not* continue what came before -- and a decision only recorded when it
# went wrong leaves no baseline to read that against.
CONVERSATION_CONTINUATION_EVENT = "conversation_continuation"
# Appended when a correction accepted mid-turn becomes the next attempt of the
# same session, is dropped because selection moved on, or is consumed by the
# attempt that carries it. Never carries the feedback itself: the operation ids
# are the handle, and the words stay behind `redirect_store.read_feedback`.
REDIRECT_CONTINUATION_EVENT = "redirect_continuation"
STATE_FILE_NAME = "state.json"
EVENTS_FILE_NAME = "events.jsonl"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED = "stop-after-current-requested"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED = "stop-after-current-canceled"
RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED = "stop-after-current-consumed"
RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED = "abort-turn-requested"
RUN_CONTROL_ACTION_ABORT_TURN_HONORED = "abort-turn-honored"
RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER = "abort-turn-satisfied-by-worker"
# An abort that arrived while the run was finalizing, after the last turn had
# already ended. It cancels the terminal work in flight rather than a turn.
RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED = "finalization-abort-honored"
# Delivery receipt for one accepted external control command, appended for every
# command the runner drains — including the ones that change no state — so a
# supervisor can tell a duplicate or already-applied command from a lost one.
RUN_CONTROL_ACTION_COMMAND_OBSERVED = "control-command-observed"
RUN_CONTROL_ACTION_TAIL_ENABLED = "tail-enabled"
RUN_CONTROL_ACTION_TAIL_DISABLED = "tail-disabled"
RUN_CONTROL_ACTION_HARNESS_SELECTED = "harness-selected"
RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED = "implementation-model-updated"
#: Terminal outcomes of a guard mutation. ``guard add --wait`` resolves on one
#: of these rather than on queue observation, because enforcement is only real
#: once the policy and shims are installed.
RUN_CONTROL_ACTION_GUARD_APPLIED = "guard-applied"
RUN_CONTROL_ACTION_GUARD_REMOVED = "guard-removed"
RUN_CONTROL_ACTION_GUARD_REJECTED = "guard-rejected"
RUN_CONTROL_ACTION_GUARDS_EXPIRED = "guards-expired"
RUN_CONTROL_ACTIONS: tuple[str, ...] = (
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED,
    RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
    RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
    RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
    RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
    RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED,
    RUN_CONTROL_ACTION_COMMAND_OBSERVED,
    RUN_CONTROL_ACTION_TAIL_ENABLED,
    RUN_CONTROL_ACTION_TAIL_DISABLED,
    RUN_CONTROL_ACTION_HARNESS_SELECTED,
    RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
    RUN_CONTROL_ACTION_GUARD_APPLIED,
    RUN_CONTROL_ACTION_GUARD_REMOVED,
    RUN_CONTROL_ACTION_GUARD_REJECTED,
    RUN_CONTROL_ACTION_GUARDS_EXPIRED,
)
#: The subset a guard mutation can end in. A ``--wait`` receipt matches one of
#: these carrying the same command ID.
GUARD_TERMINAL_ACTIONS: tuple[str, ...] = (
    RUN_CONTROL_ACTION_GUARD_APPLIED,
    RUN_CONTROL_ACTION_GUARD_REMOVED,
    RUN_CONTROL_ACTION_GUARD_REJECTED,
)
MAILBOX_ACTION_GUIDANCE_OBSERVED = "guidance-observed"
MAILBOX_ACTION_NOTICE_PUBLISHED = "notice-published"
MAILBOX_ACTION_WAIT_PREPARATION_FAILED = "wait-preparation-failed"
MAILBOX_ACTION_WAIT_STARTED = "wait-started"
MAILBOX_ACTION_WAIT_CLEARED = "wait-cleared"
# Guidance-delivery ledger actions. The vocabulary lands with the durable
# record so status and audit surfaces can be written against a fixed set; the
# runner does not emit any of them until live delivery exists. Their payloads
# are built by ``mailbox.guidance_delivery_event_payload`` and carry no
# guidance body — not the text, not its length, not a digest.
MAILBOX_ACTION_GUIDANCE_DELIVERED = "guidance-delivered"
MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED = "guidance-delivery-deferred"
MAILBOX_ACTIONS: tuple[str, ...] = (
    MAILBOX_ACTION_GUIDANCE_OBSERVED,
    MAILBOX_ACTION_NOTICE_PUBLISHED,
    MAILBOX_ACTION_WAIT_PREPARATION_FAILED,
    MAILBOX_ACTION_WAIT_STARTED,
    MAILBOX_ACTION_WAIT_CLEARED,
    MAILBOX_ACTION_GUIDANCE_DELIVERED,
    MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED,
)
# The ``action_required.reason`` values a live harness signal can carry. Each
# names an intervention the agent asked for mid-turn, so each is only current
# while the run is still stopped on it; a reason the runner itself declared —
# review findings above all — is not a live signal and is never retired.
# ``action_required`` is the current unresolved intervention, not the run's
# history, and this set is what both readers of that rule retire against:
# ``turn_execution.clear_live_action_required`` on the durable state a live run
# keeps, and the supervisor audit on the status section it projects into a
# bundle. Splitting the two would let a bundle advertise an ask the live run had
# already retired.
LIVE_ACTION_REQUIRED_REASONS = frozenset(
    {"runner-pause", "runner-status-paused", "approval-required"}
)
# Where a retired ``action_required`` is kept, on the run payload and in the
# status section the audit projects from it. Retirement never discards: the
# record that stood is preserved beside the reason it was replaced, because the
# history an operator or an auditor reads is not allowed to shrink because the
# current state moved on.
ACTION_REQUIRED_SUPERSEDED_KEY = "action_required_superseded"
# What retired it. A runner-declared pause — ``blocked``, ``unsafe``,
# ``malformed-contract`` — is strictly more specific than the harness's generic
# ask, and the run has stopped on it, so the generic ask can no longer be
# answered. Nothing else proves this: ``turn_execution.clear_live_action_required``
# fires only on a new turn or an unpaused finish, and a paused run reaches
# neither.
SUPERSEDED_BY_RUNNER_PAUSE = "runner-declared-pause"
# Stable, evidence-based harness-failure categories produced by
# ``agent_session_loop.harness_failures.classify_harness_failure``. A nonzero
# harness exit is classified into exactly one of these; ``nonzero-exit`` remains
# the fail-closed fallback for unrecognized (and legacy) failures. The
# higher-level recoverable / user-required pause policy that composes from these
# atoms lives in ``agent_session_loop.pause_policy`` (the single source of truth
# consumed by the run loop, ``asl status``/``asl questions``, and the supervisor
# helper).
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
PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE = "workspace-write-guard-unavailable"
# A registered implementation turn changed a repository outside the selected
# session's authoritative write aliases. Contents are deliberately retained;
# the session is returned to pending so an operator can repair the durable
# scope and refresh expansion/preflight evidence.
PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION = "workspace-write-scope-violation"
# A run-scoped protected-path guard was active at finalization and the workspace
# no longer matches the turn-start snapshot at a path the guard names. Like the
# scope violation above, contents are retained and the session returns to
# pending: the operator declared the path must not move, so the run must not
# decide on its own how to put it back.
PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED = "guard-protected-path-changed"
# The turn-start snapshot a protected-path guard has to compare against is
# missing or unreadable, so the guard can neither clear nor convict the turn.
# Enforcement fails closed rather than publishing a turn it could not check.
PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE = "guard-protected-path-unavailable"
# A legacy run resolved declarations reaching more than one Git top-level. The
# value matches ``repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED``; it is
# spelled literally here so the low-level event vocabulary keeps no dependency
# on the resolution engine.
PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED = "repository-registry-required"
# Another live run already claims a repository this run's registry declares.
# Two agents writing one working tree cannot be made safe, so the overlap is
# refused at startup rather than explained by a guard afterwards.
PAUSE_REASON_WORKSPACE_REPOSITORY_CONTENTION = "workspace-repository-contention"
# A turn changed a session artifact belonging to a session other than the one
# its task packet selected. The packet fixes exactly one writable session file,
# so any other session's instructions changing under it is tampering.
PAUSE_REASON_PLAN_ARTIFACT_TAMPERED = "plan-artifact-tampered"
# The terminal adjudicator refused a success the run could not show it earned:
# a required verification never reached passed, or a plan calling itself
# finished held no clear final review. Both are re-derived from the plan's
# append-only history, so a resumed run reaches the same verdict.
PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE = "required-checks-incomplete"
PAUSE_REASON_REVIEW_NOT_CLEAR = "review-not-clear"
# The durable conversation bound to this plan session cannot be resumed: the
# ledger holds no confirmed conversation for it, the recorded binding no longer
# matches this run's workspace/access profile, or the record is unreadable.
# Continuity is exact-or-refusal, so the run stops instead of silently opening a
# second conversation. Clearing it is an operator decision — relaunch with the
# one-use ``--fresh-conversation`` authorization — never an automatic retry.
PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED = "conversation-recovery-required"
# An accepted redirect force-stopped a turn, and the predecessor could not be
# shown to be gone: a process of the interrupted worker is still running, or
# nobody ever waited on it. The correction stays pending in the journal, but no
# successor turn may start beside a worker that still holds this plan's lock,
# its conversation and its worktree. Never automatic: the machine has to be
# looked at, which is why this reason is classified `inspect` rather than
# recoverable — and being non-recoverable is also what keeps the run from
# committing the interrupted turn's retained work.
PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED = "redirect-successor-blocked"
PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED = "redirect-submission-unresolved"
# A turn carrying an accepted redirect could not record, before starting, that
# its prompt was about to be submitted. The turn is not started: a submission
# this run could not declare is one no later reader can tell apart from a
# submission that never happened, and that ambiguity is what makes the same
# correction reach a second worker. The words stay in the journal, so the fix is
# to the journal's storage — a full disk, a read-only or unwritable state
# directory — after which an ordinary relaunch runs the turn with them.
PAUSE_REASON_REDIRECT_DELIVERY_UNRECORDABLE = "redirect-delivery-unrecordable"

# The subset of harness-failure categories that a supervisor may safely retry
# by relaunching the same session. Quota, authentication, invalid-model, and
# unclassified (fail-closed ``nonzero-exit``) failures are never in this set.
RETRYABLE_HARNESS_FAILURE_REASONS: tuple[str, ...] = (PAUSE_REASON_TRANSPORT_FAILED,)

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
    PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
    PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE,
    PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
    PAUSE_REASON_WORKSPACE_REPOSITORY_CONTENTION,
    PAUSE_REASON_PLAN_ARTIFACT_TAMPERED,
    PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE,
    PAUSE_REASON_REVIEW_NOT_CLEAR,
    PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
    PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
    PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED,
    PAUSE_REASON_REDIRECT_DELIVERY_UNRECORDABLE,
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
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
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
    with _EVENT_WRITE_LOCK, event_path.open("a", encoding="utf-8") as event_file:
        event_file.write(line)
        event_file.flush()
        durability.fullsync(event_file.fileno())
