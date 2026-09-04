"""Run-loop orchestration boundaries.

The runner drives one harness turn per pending session. After a turn
completes, it gates the handoff through `verification`, then — only on a
verified-complete handoff — appends a row to the plan directory's
runner-managed `progress.md` journal, and finally syncs the derived
status ledgers. The verify → progress → ledgers ordering is required so
that only verified work reaches the journal and the journal write
happens before the ledger re-inspect.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    agent_mux_delivery,
    agent_mux_projection,
    clock,
    commit_groups,
    commit_settlement,
    durability,
    events,
    guards,
    guidance_delivery,
    handoff_correction,
    handoff_guards,
    harness_failures,
    harnesses,
    # Re-exported, not used here: the last call site moved to
    # `turn_dispositions` in Session 35, but in-tree callers patch
    # `runner.mailbox` to stub a mailbox read, and this session moves code
    # rather than names.
    mailbox,  # noqa: F401
    mailbox_rendering,
    model_availability,
    plans,
    preflight,
    qna,
    readiness_gate,
    review,
    review_continuation,
    review_cycle,
    review_guard,
    review_repair,
    review_session_body,
    review_transitions,
    run_commit,
    run_completion,
    run_control,
    run_coordination,
    run_doctor,
    run_entry,
    run_inputs,
    run_outcome,
    run_payloads,
    run_persistence,
    run_prompts,
    run_reporting,
    run_selection,
    run_termination,
    session_verification,
    state,
    supervisor_audit,
    turn_dispositions,
    turn_execution,
    turn_postflight,
    turn_preflight,
    turn_preparation,
    turn_settlement,
    verification,
    verification_transitions,
    workspace_evidence,
    write_guards,
)
from .run_errors import RunError

# Turn timing, hang thresholds and the control-poll cadence are the turn
# boundary's own tuning; re-exported here because operators and tests read
# them at this name.
INTERRUPTED_EXIT = turn_execution.INTERRUPTED_EXIT
HEARTBEAT_INTERVAL_SECONDS = turn_execution.HEARTBEAT_INTERVAL_SECONDS
HANG_TRIGGER_IDLE_SECONDS = turn_execution.HANG_TRIGGER_IDLE_SECONDS
HANG_CONFIRM_SAMPLES = turn_execution.HANG_CONFIRM_SAMPLES
HANG_CPU_EPSILON_SECONDS = turn_execution.HANG_CPU_EPSILON_SECONDS
HANG_RESULT_GRACE_SECONDS = turn_execution.HANG_RESULT_GRACE_SECONDS
HANG_TRIGGER_IDLE_ENV_VAR = turn_execution.HANG_TRIGGER_IDLE_ENV_VAR
HANG_CONFIRM_SAMPLES_ENV_VAR = turn_execution.HANG_CONFIRM_SAMPLES_ENV_VAR
HANG_RESULT_GRACE_ENV_VAR = turn_execution.HANG_RESULT_GRACE_ENV_VAR
HANG_REASON_RESULT_RECEIVED = turn_execution.HANG_REASON_RESULT_RECEIVED
HANG_REASON_ZERO_CPU = turn_execution.HANG_REASON_ZERO_CPU
LIVE_CONTROL_POLL_INTERVAL_SECONDS = turn_execution.LIVE_CONTROL_POLL_INTERVAL_SECONDS
# The guidance rendering budget belongs to ``agent_session_loop`` mailbox
# rendering; these re-exports keep the runner's historical public names stable.
MAX_RENDERED_GUIDANCE = mailbox_rendering.MAX_RENDERED_GUIDANCE
MAX_RENDERED_GUIDANCE_BYTES = mailbox_rendering.MAX_RENDERED_GUIDANCE_BYTES
# Registered implementation turns are enabled only because every such turn is
# wrapped by the authoritative workspace snapshot guard below. Registered
# review turns remain fail-closed until the combined review guard in Session 13.
MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED = True
MULTI_REPO_CAPABILITY_PAUSE_REASON = (
    events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
)
# Review artifact contract. The grammar, the structured final-review.json
# schema, and the shared verdict payload all live in
# ``agent_session_loop.review``; these re-exports keep the runner's historical
# public names stable.
FINAL_REVIEW_FILENAME = review.FINAL_REVIEW_FILENAME
FINAL_REVIEW_ARTIFACT_FILENAME = review.FINAL_REVIEW_ARTIFACT_FILENAME
REVIEW_STATUS_ALL_CLEAR = review.REVIEW_STATUS_ALL_CLEAR
_REVIEW_STATUS_FINDINGS_RE = review._REVIEW_STATUS_FINDINGS_RE
REVIEW_SESSION_MARKER = review.REVIEW_SESSION_MARKER
GENERATED_REVIEW_SESSION_VERSION = review.GENERATED_REVIEW_SESSION_VERSION
REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION = (
    review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
)
STRUCTURED_REVIEW_MIN_VERSION = review.STRUCTURED_REVIEW_MIN_VERSION
REVIEW_ARTIFACT_VERSION = review.REVIEW_ARTIFACT_VERSION
REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION = (
    review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION
)
SESSION_KIND_IMPLEMENTATION = plans.SESSION_KIND_IMPLEMENTATION
SESSION_KIND_REVIEW = plans.SESSION_KIND_REVIEW
SESSION_KINDS: tuple[str, ...] = plans.SESSION_KINDS
TRUSTED_RUNNER_SIGNAL_SOURCES = turn_execution.TRUSTED_RUNNER_SIGNAL_SOURCES
RUNNER_STATUS_SOURCE_ABSENT = turn_execution.RUNNER_STATUS_SOURCE_ABSENT
APPROVAL_SIGNAL_PATTERNS = turn_execution.APPROVAL_SIGNAL_PATTERNS
RUN_CONTROL_EVENT_ACTIONS = run_coordination.RUN_CONTROL_EVENT_ACTIONS
RUN_CONTROL_EVENT_MESSAGES = run_coordination.RUN_CONTROL_EVENT_MESSAGES
ABORT_TURN_DEFAULT_REASON = run_coordination.ABORT_TURN_DEFAULT_REASON
AGENT_MUX_START_EVENT = agent_mux_delivery.START_EVENT
AGENT_MUX_TURN_START_EVENT = agent_mux_delivery.TURN_START_EVENT
AGENT_MUX_TURN_HEARTBEAT_EVENT = agent_mux_delivery.TURN_HEARTBEAT_EVENT
AGENT_MUX_TURN_FINISH_EVENT = agent_mux_delivery.TURN_FINISH_EVENT
AGENT_MUX_TURN_HANG_SUSPECTED_EVENT = agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT
AGENT_MUX_TURN_HANG_CLEARED_EVENT = agent_mux_delivery.TURN_HANG_CLEARED_EVENT
AGENT_MUX_VERIFICATION_START_EVENT = agent_mux_delivery.VERIFICATION_START_EVENT
AGENT_MUX_VERIFICATION_HEARTBEAT_EVENT = agent_mux_delivery.VERIFICATION_HEARTBEAT_EVENT
AGENT_MUX_VERIFICATION_FINISH_EVENT = agent_mux_delivery.VERIFICATION_FINISH_EVENT
AGENT_MUX_WAIT_START_EVENT = agent_mux_delivery.WAIT_START_EVENT
AGENT_MUX_WAIT_END_EVENT = agent_mux_delivery.WAIT_END_EVENT
AGENT_MUX_PAUSE_EVENT = agent_mux_delivery.PAUSE_EVENT
AGENT_MUX_FINISH_EVENT = agent_mux_delivery.FINISH_EVENT
AGENT_MUX_SOURCE = agent_mux_delivery.SOURCE
AGENT_MUX_START_REASON = agent_mux_delivery.START_REASON
AGENT_MUX_TURN_START_REASON = agent_mux_delivery.TURN_START_REASON
AGENT_MUX_TURN_HEARTBEAT_REASON = agent_mux_delivery.TURN_HEARTBEAT_REASON
AGENT_MUX_TURN_FINISH_REASON = agent_mux_delivery.TURN_FINISH_REASON
AGENT_MUX_TURN_HANG_SUSPECTED_REASON = agent_mux_delivery.TURN_HANG_SUSPECTED_REASON
AGENT_MUX_TURN_HANG_CLEARED_REASON = agent_mux_delivery.TURN_HANG_CLEARED_REASON
AGENT_MUX_VERIFICATION_START_REASON = agent_mux_delivery.VERIFICATION_START_REASON
AGENT_MUX_VERIFICATION_HEARTBEAT_REASON = (
    agent_mux_delivery.VERIFICATION_HEARTBEAT_REASON
)
AGENT_MUX_VERIFICATION_FINISH_REASON = agent_mux_delivery.VERIFICATION_FINISH_REASON
AGENT_MUX_WAIT_START_REASON = agent_mux_delivery.WAIT_START_REASON
AGENT_MUX_WAIT_END_REASON = agent_mux_delivery.WAIT_END_REASON
AGENT_MUX_FINISH_REASON = agent_mux_delivery.FINISH_REASON
# Phase vocabulary must match the AgentMux daemon's event mapping
# (internal/daemon/normalize.go::aslPhaseForEvent).
AGENT_MUX_PHASE_BY_EVENT = agent_mux_delivery.PHASE_BY_EVENT
AGENT_MUX_NOTIFY_TIMEOUT_SECONDS = agent_mux_delivery.NOTIFY_TIMEOUT_SECONDS
AGENT_MUX_NOTIFY_TIMEOUT_ENV_VAR = agent_mux_delivery.NOTIFY_TIMEOUT_ENV_VAR
AGENT_MUX_NOTIFY_ENV_VAR = agent_mux_delivery.NOTIFY_ENV_VAR
AGENT_MUX_NOTIFY_OFF = agent_mux_delivery.NOTIFY_OFF

# Schema version stamped on persisted ``state.json`` and per-turn
# ``result.json`` records. Absence of a ``version`` key means v1; readers must
# accept all three. A v2 state record stores only compact turn summaries and
# never embeds raw verification output — the full per-turn detail, including
# verification stdout/stderr previews, lives in ``result.json`` (a v2 result
# record, which may carry detailed output) referenced by ``result_path``. A v3
# state record keeps every v2 guarantee and adds the nested body-free
# ``agent_mux`` projection, written inside the same atomic replacement so a
# reader never sees a state and a projection from different transitions.
# ``asl migrate-state`` upgrades an absent-version, v1, or v2 record in place.
STATE_SCHEMA_VERSION = agent_mux_projection.STATE_SCHEMA_VERSION
RESULT_SCHEMA_VERSION = turn_execution.RESULT_SCHEMA_VERSION

# Bounded mailbox reply waits, supervisor leases, the external control plane,
# and terminal assurance are coordination, not run-loop logic: they live in
# :mod:`agent_session_loop.run_coordination` behind typed APIs that own their
# durable slots. The aliases here and below keep the runner's historical
# vocabulary resolving for in-tree callers and for tests that patch these
# names on this module.
MAILBOX_REPLY_WAIT_BASE_SECONDS = run_coordination.MAILBOX_REPLY_WAIT_BASE_SECONDS
MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS = (
    run_coordination.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS
)
MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS = (
    run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS
)
MAILBOX_REPLY_WAIT_POLL_SECONDS = run_coordination.MAILBOX_REPLY_WAIT_POLL_SECONDS
MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS = (
    run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS
)
MAILBOX_REPLY_WAIT_SUSPEND_SKEW_SECONDS = (
    run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_SKEW_SECONDS
)
MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS = (
    run_coordination.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS
)
MAILBOX_QUESTION_TEXT_MAX_CHARS = run_coordination.MAILBOX_QUESTION_TEXT_MAX_CHARS
MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST = (
    run_coordination.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST
)
MAILBOX_REPLY_WAIT_PREPARATION_REASONS = (
    run_coordination.MAILBOX_REPLY_WAIT_PREPARATION_REASONS
)
MAILBOX_REPLY_WAIT_BASE_ENV_VAR = run_coordination.MAILBOX_REPLY_WAIT_BASE_ENV_VAR
MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR = (
    run_coordination.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR
)
MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR = (
    run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR
)
MAILBOX_REPLY_WAIT_POLL_ENV_VAR = run_coordination.MAILBOX_REPLY_WAIT_POLL_ENV_VAR
MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR = (
    run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR
)
_MAILBOX_TOKEN_UNSEEN = run_coordination.MAILBOX_TOKEN_UNSEEN

# Verification-repair and review-verdict-repair loop control. Both budgets and
# both fingerprint prefixes are declared beside the transitions that spend them
# — :mod:`agent_session_loop.verification_transitions` and
# :mod:`agent_session_loop.review_transitions` — and are re-exported here for
# the runner's own call sites and for the tooling that reads them off this
# module.
MAX_VERIFICATION_REPAIR_ATTEMPTS = (
    verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS
)
VERIFICATION_FINGERPRINT_PREFIX = (
    verification_transitions.VERIFICATION_FINGERPRINT_PREFIX
)
MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS = (
    review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS
)
REVIEW_VERDICT_FINGERPRINT_PREFIX = review_transitions.REVIEW_VERDICT_FINGERPRINT_PREFIX

# How one repair/rereview cycle ended. The caps that bound those cycles are
# resolved in ``run_inputs``; the dispositions themselves are declared beside
# the continuation policy in ``review_continuation`` and re-exported here for
# the call sites and tests that read them at this name. Every review completion
# appends a ``review_cycle`` event whose fingerprints
# (:func:`review.finding_fingerprint`) let the next cycle detect repeated
# findings across processes; a repeated fingerprint stops the loop without
# another cycle.
REVIEW_CYCLE_DISPOSITION_ALL_CLEAR = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
)
REVIEW_CYCLE_DISPOSITION_REPAIRS = review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED
)
REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED
)
REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED
)
REVIEW_CYCLE_DISPOSITION_REPEATED = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED
)
REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED
)
REVIEW_CYCLE_DISPOSITION_MALFORMED = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED
)
REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY = (
    review_continuation.REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
)
REVIEW_CYCLE_DISPOSITIONS = review_continuation.REVIEW_CYCLE_DISPOSITIONS

#: Prefix distinguishing a gate replayed from another session's contract from
#: the review's own artifact validators, and the reason a findings review's
#: deferral of those gates records. Both are declared beside the deferral
#: policy in :mod:`agent_session_loop.verification_transitions`.
BROAD_REVIEW_CHECK_PREFIX = verification_transitions.BROAD_REVIEW_CHECK_PREFIX
REVIEW_BROAD_FAILURES_DEFERRED_REASON = (
    verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON
)


# The run loop and the turn boundary have to stamp the same moments the same
# way, so the clock primitives live in the leaf ``clock`` module.
_utc_run_id = clock.utc_run_id
_utc_now = clock.utc_now
_utc_iso_now = clock.utc_iso_now
_isoformat = clock.isoformat
_duration_seconds = clock.duration_seconds
_parse_datetime = clock.parse_datetime


# Session identity and plan-snapshot projections live in ``plans``: they are
# statements about a plan, and the turn boundary has to build the same ones
# without importing the runner. Re-exported here for the call sites and tests
# that have always read them at this name.
_session_number_alias = plans.session_number_alias
_session_compat_value = plans.session_compat_value
_session_label = plans.session_label
_session_path_label = plans.session_path_label
_session_identity_payload = plans.session_identity_payload
_session_details_payload = plans.session_details_payload
_session_status_change_payload = plans.session_status_change_payload
_status_map = plans.status_map
_status_counts = plans.status_counts
_sessions_with_status = plans.sessions_with_status
_session_payload = plans.session_payload
_snapshot_payload = plans.snapshot_payload
_status_changes = plans.status_changes
_pending_completed = plans.pending_completed


# Reading the control markers a turn's own output carries is part of executing
# the turn, so the parsers moved with it into ``turn_execution``.
_signal_line_candidates = turn_execution.signal_line_candidates
_extract_prefixed_line = turn_execution.extract_prefixed_line
_is_noop_runner_pause = turn_execution.is_noop_runner_pause
_runner_signal_lines = turn_execution.runner_signal_lines
_signal_source_trusted = turn_execution.signal_source_trusted
_runner_signals = turn_execution.runner_signals
_runner_pause_reason = turn_execution.runner_pause_reason
_preview_text = turn_execution.preview_text
_approval_match_is_content = turn_execution.approval_match_is_content
_live_action_signal = turn_execution.live_action_signal
_LIVE_ACTION_REQUIRED_REASONS = turn_execution.LIVE_ACTION_REQUIRED_REASONS
_clear_live_action_required = turn_execution.clear_live_action_required


# The turn boundary writes the same artifacts this loop does, so the atomic
# writers live beside the durability barrier they depend on.
_write_text = durability.write_text
_append_text = durability.append_text


# One turn's execution -- launch, heartbeat, hang detection, process result,
# artifact capture, and the two terminal transitions -- lives behind the typed
# boundary in ``turn_execution``. The runner drives that boundary and owns the
# guards around it; it does not reimplement any of it. These names are
# re-exported for the call sites and tests that have always read them here.
TurnExecutor = turn_execution.TurnExecutor
TurnIdentity = turn_execution.TurnIdentity
TurnLaunch = turn_execution.TurnLaunch
TurnExecution = turn_execution.TurnExecution
TurnFinalization = turn_execution.TurnFinalization
TurnCommit = turn_execution.TurnCommit
_TurnCommitRejected = turn_execution.TurnCommitRejected
_HangAssessment = turn_execution.HangAssessment
_HangSentinel = turn_execution.HangSentinel
_prepare_stream_logs = turn_execution.prepare_stream_logs
_create_turn_logs = turn_execution.create_turn_logs
_live_stream_callback = turn_execution.live_stream_callback
_turn_paths = turn_execution.turn_paths
_turn_status_result = turn_execution.turn_status_result
_observed_model_payload = turn_execution.observed_model_payload
_write_turn_artifacts = turn_execution.write_turn_artifacts
_finalize_turn_execution = turn_execution.finalize_turn_execution
_replace_turn_record = turn_execution.replace_turn_record
_publication_snapshot = turn_execution.publication_snapshot
_commit_turn = turn_execution.commit_turn
_reopened_snapshot_identity = turn_execution.reopened_snapshot_identity
_record_turn_rejected = turn_execution.record_turn_rejected
_reply_argv_placeholder = turn_execution.reply_argv_placeholder
_sanitize_reply_argv = turn_execution.sanitize_reply_argv
_resolution_argv_placeholder = turn_execution.resolution_argv_placeholder
_sanitize_resolution_argv = turn_execution.sanitize_resolution_argv
_persisted_result_argv = turn_execution.persisted_result_argv


# The harness failure classifier lives in the leaf module ``harness_failures``
# so the supervisor audit can classify its own launches without importing the
# runner. These names are re-exported for the call sites and tests that have
# always read them here.
FAILURE_EVIDENCE_MAX_CHARS = harness_failures.FAILURE_EVIDENCE_MAX_CHARS
FailureClassification = harness_failures.FailureClassification
classify_harness_failure = harness_failures.classify_harness_failure


_write_json_file = durability.write_json_file
_write_bytes_file = durability.write_bytes_file


_read_optional_bytes = run_coordination.read_optional_bytes
_restore_optional_bytes = run_coordination.restore_optional_bytes


# The bounded mailbox reply wait, its durable ``waiting_for_reply`` slot, and
# the reply rollback that guards an unconsumed attempt are all coordination
# across the run's process boundary, so they live behind the typed APIs in
# :mod:`agent_session_loop.run_coordination`. These aliases keep every in-tree
# call site and every ``mock.patch.object(runner, ...)`` target resolving.
_waiting_for_reply_payload = run_coordination.waiting_for_reply_payload
_set_waiting_for_reply = run_coordination.set_waiting_for_reply
_clear_waiting_for_reply = run_coordination.clear_waiting_for_reply
_record_mailbox_reply_wait_preparation_failed = (
    run_coordination.record_mailbox_reply_wait_preparation_failed
)
_MailboxReplyWaitSettings = run_coordination.MailboxWaitSettings
_PreparedMailboxReplyWait = run_coordination.PreparedMailboxWait
_MailboxReplyWaitResult = run_coordination.MailboxWaitResult
_MailboxReplyWaitUnavailable = run_coordination.MailboxWaitUnavailable
_finite_positive_env = run_coordination.finite_positive_env
_mailbox_reply_wait_settings = run_coordination.mailbox_reply_wait_settings
_mailbox_question_id = run_coordination.mailbox_question_id
_runner_pause_can_wait_for_mailbox_reply = (
    run_coordination.runner_pause_can_wait_for_mailbox_reply
)
_worker_pause_resolution_for_turn = run_coordination.worker_pause_resolution_for_turn
_mailbox_question_text = run_coordination.mailbox_question_text
_ensure_mailbox_question = run_coordination.ensure_mailbox_question
_prepare_mailbox_reply_wait = run_coordination.prepare_mailbox_reply_wait
_record_waiting_lease_activity = run_coordination.record_waiting_lease_activity
_rebase_waiting_deadlines = run_coordination.rebase_waiting_deadlines
_lease_evidence = run_coordination.lease_evidence
_wait_for_mailbox_reply = run_coordination.wait_for_mailbox_reply
_finish_mailbox_reply_wait_lifecycle = (
    run_coordination.finish_mailbox_reply_wait_lifecycle
)
_ReplyStatePreserver = run_coordination.ReplyStatePreserver


_initial_control_payload = run_coordination.initial_control_payload
_apply_run_control_action = run_coordination.apply_run_control_action
_record_run_control_action = run_coordination.record_run_control_action

#: Assurance states a terminating run still owes an audit for. ``skipped`` is a
#: settled decision to run no checks; anything else is already a verdict.
_AUDITABLE_ASSURANCE_STATES = run_coordination.AUDITABLE_ASSURANCE_STATES
_TerminalAuditContext = run_coordination.TerminalAuditContext


_attach_assurance_report = run_coordination.attach_assurance_report


#: Stable terminal pause reasons the adjudicator imposes on a run that reached
#: an ending it could not justify. Both are durable and re-evaluated: the next
#: run over the same plan re-derives them from the same append-only evidence,
#: so a blocker clears only when the evidence that produced it changes. The
#: vocabulary itself lives in :mod:`agent_session_loop.events` with every other
#: pause reason; these are the runner's names for the same two atoms.
REQUIRED_CHECKS_INCOMPLETE_REASON = (
    verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON
)
REVIEW_NOT_CLEAR_REASON = verification_transitions.REVIEW_NOT_CLEAR_REASON

#: Terminal reasons that assert the plan's own work is finished. Only these
#: claim a final review; ``stopped`` and ``max-sessions`` report an early exit
#: an operator asked for and never pretend the plan was reviewed to the end.
_PLAN_COMPLETION_REASONS = verification_transitions.PLAN_COMPLETION_REASONS

#: Assurance states that leave the optional post-run audit unable to speak.
#: ``skipped`` is a settled decision not to audit and is not a degradation.
_DEGRADED_ASSURANCE_STATES = run_coordination.DEGRADED_ASSURANCE_STATES

#: Session 2's terminal success policy, now declared beside the verification
#: states it reads. Bound here because the run's terminal paths and the suite's
#: patch targets resolve it off the runner.
_TerminalBlocker = verification_transitions.TerminalBlocker
_accepted_broad_failure_deferral = (
    verification_transitions.accepted_broad_failure_deferral
)
_recorded_review_cycles = verification_transitions.recorded_review_cycles
_unresolved_deferred_verifications = (
    verification_transitions.unresolved_deferred_verifications
)
_read_verification_finished_events = (
    verification_transitions.read_verification_finished_events
)
_unpassed_run_verifications = verification_transitions.unpassed_run_verifications
_final_review_is_clear = verification_transitions.final_review_is_clear
_terminal_blocker = verification_transitions.terminal_blocker


_record_degraded_assurance = run_coordination.record_degraded_assurance


# The external control plane owns ``payload["control"]`` and the receipts an
# `asl control --wait` client tails, so it moves with the rest of the
# coordination boundary. Aliases only; nothing here is runner-specific.
_apply_external_control_command = run_coordination.apply_external_control_command
_record_control_command_observed = run_coordination.record_control_command_observed
_abort_turn_guidance_text = run_coordination.abort_turn_guidance_text
_arm_requested_abort = run_coordination.arm_requested_abort
_degrade_abort_to_stop_after_current = (
    run_coordination.degrade_abort_to_stop_after_current
)
_drain_external_control_queue = run_coordination.drain_external_control_queue
_MailboxPoll = run_coordination.MailboxPoll


# Verification and review-verdict adjudication now lives in the two transition
# modules. The names below stay bound here because in-tree callers and the
# suite's patch targets resolve them off the runner; each is the extracted
# implementation, not a second one.
_verification_failure_summary = verification_transitions.verification_failure_summary
_verification_failure_details = verification_transitions.verification_failure_details
_verification_fingerprint = verification_transitions.verification_fingerprint
_read_prior_verification_failures = (
    verification_transitions.read_prior_verification_failures
)
_verification_repair_references = (
    verification_transitions.verification_repair_references
)
_is_verification_repair_exhausted = (
    verification_transitions.is_verification_repair_exhausted
)
_review_verdict_fingerprint = review_transitions.review_verdict_fingerprint
_read_prior_review_verdict_retries = (
    review_transitions.read_prior_review_verdict_retries
)
_is_review_verdict_repair_exhausted = (
    review_transitions.is_review_verdict_repair_exhausted
)
_review_verdict_repair_references = review_transitions.review_verdict_repair_references
REVIEW_FINDINGS_UNRESOLVED_REASON = review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON


@dataclasses.dataclass(kw_only=True)
class RunContext:
    """One locked run's own state, grouped by how long each value lives.

    ``_run_locked`` is a single function with roughly eighty locals, and the
    helpers it owns used to reach them as closure captures. A capture is
    invisible at the call site and unsliceable: a helper that reads a loop
    local cannot leave the function that binds it. This object makes that
    dependency explicit so each helper states what it needs.

    The groups below are the run's three clocks. Run-start fields are bound
    before the first helper runs and never rebound; loop-pass fields advance
    once per pass around the session loop; turn fields advance once per
    harness turn. Nothing here is durable on its own: every write still goes
    through :attr:`persistence`, and the object never outlives the run lock.
    """

    # Fixed at run start. `plan_dir` is the canonicalized directory every
    # snapshot of this run shares, so it stays here rather than being re-read
    # off whichever snapshot the loop currently holds.
    run_id: str
    plan_dir: Path
    run_dir: Path
    event_path: Path
    payload: dict[str, Any]
    persistence: run_persistence._RunPersistence
    progress: run_reporting._ProgressReporter
    inline_tail: run_reporting._InlineTailController
    reply_state_preserver: run_coordination.ReplyStatePreserver
    audit_context: run_coordination.TerminalAuditContext
    guard_controller: guards.GuardController
    plan_cache: plans.PlanSnapshotCache
    verification_context: verification.VerificationContext
    commit_generation: run_commit._CommitGeneration
    #: Read off the payload once the readiness gate has passed, because the
    #: gate is what settles which control block the rest of the run answers to.
    control_payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    #: Wired once the control listener exists, because the listener is built
    #: from the two handlers that already take this context.
    run_controls: run_control.RunControlListener | None = None

    # Updated once per loop pass: the counters and flags that decide whether
    # the next pass runs, and the readiness gate's latest verdict.
    completed_count: int = 0
    #: Stamped when the runner stops working on a turn, not when the harness
    #: exits: verification and finalization are runner-owned work, so counting
    #: them as idle time would report the previous turn's own cost as a gap.
    runner_work_finished_at: datetime | None = None
    first_loop_pass: bool = True
    idle_baseline_pinned: bool = False
    commit_at_end_attempted: bool = False
    review_appended: bool = False
    gate_payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    gate_passed: bool = False
    core_skipped: bool = False

    # Updated once per turn.
    run_control_context: dict[str, Any] | None = None
    last_session_details: dict[str, Any] = dataclasses.field(default_factory=dict)
    seen_external_command_ids: set[str] = dataclasses.field(default_factory=set)
    #: The group this run still owes, carried between commit boundaries. A
    #: commit failure soft-fails, so this is what makes the failure retryable
    #: inside the same run instead of only after a restart.
    owed_commit_group: dict[str, Any] | None = None
    #: Retries already spent per group, so a repository that fails the same way
    #: every time cannot turn the run loop into a commit loop.
    owed_commit_group_attempts: dict[str, int] = dataclasses.field(default_factory=dict)

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None:
        """Publish the plan image this run ends on, under the write lock."""
        with self.persistence.lock:
            self.payload["final_snapshot"] = plans.snapshot_payload(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        """Stop this run with a reason, through the one pause coordinator.

        Every pause site named the same three run-start values -- the event
        path, the payload, the persistence seam -- and differed only in the
        reason it was pausing for. Those three now live here, so a caller
        states its reason and nothing else.
        """
        run_termination._pause_run(
            self.event_path,
            self.payload,
            reason=reason,
            message=message,
            details=details,
            persistence=self.persistence,
        )


def _finish_run_locked(
    ctx: RunContext,
    exit_code: int,
    result_payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    ctx.reply_state_preserver.restore_if_unconsumed()
    return exit_code, result_payload


def _terminalize(
    ctx: RunContext,
    exit_code: int,
    *,
    cancel: harnesses.AbortSignal | None = None,
    poll: Callable[[], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Close this run through the one coordinator every ending shares.

    The refusals above the loop end runs as truly as the loop's own exits
    do, so they terminalize here rather than finalizing on their own; the
    exit code each one already decided passes through untouched. Those
    refusals run before the control queue has a poller, so only the loop's
    own exit supplies ``cancel`` and ``poll``.

    A commit failure no longer stops the run, so the last thing decided
    before the audit runs is whether any group is still owed. The answer is
    written into the payload here, ahead of
    ``run_termination._terminalize_run``, because every reader that must not
    miss it — terminal adjudication, the assurance audit, and the final
    summary — is downstream of this point.

    The exit code each caller passes is a proposal, not a verdict: the
    adjudicator inside ``run_termination._terminalize_run`` may refuse a
    success and return a failing code instead, which is why the returned
    value is used rather than the one passed in.
    """
    with ctx.persistence.lock:
        ctx.payload[commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY] = (
            commit_settlement._unresolved_commit_groups(ctx.payload)
        )
    return _finish_run_locked(
        ctx,
        run_termination._terminalize_run(
            exit_code=exit_code,
            event_path=ctx.event_path,
            payload=ctx.payload,
            write_readiness_state=readiness_gate.write_readiness_state,
            progress=ctx.progress,
            persistence=ctx.persistence,
            audit_context=ctx.audit_context,
            cancel=cancel,
            poll=poll,
        ),
        ctx.payload,
    )


def _soft_fail_commit(
    ctx: RunContext,
    *,
    scope: str,
    session_id: str | None,
    group: Mapping[str, Any] | None,
    commit: Mapping[str, Any] | None,
    error: str | None,
    message: str,
) -> None:
    """Absorb a commit failure: record it, warn, and keep the run going.

    The group is already marked ``failed`` in ``commit_groups`` by the
    coordinator, and the commit's own ``commit_finished`` record already
    carries the outcome that classified it. What is added here is the
    run-level admission — durable in state, visible in the progress output,
    and retried at the next commit boundary.

    The retry is bounded. An attempt that never built a group has nothing
    durable to retry, and a group that has already spent its retries is
    left owed rather than attempted forever; either way the group stays
    ``failed`` in ``commit_groups``, so ``terminalize`` reports it and a
    later run can still recover it.
    """
    record = commit_settlement._commit_failure_record(
        scope=scope,
        session_id=session_id,
        group=group,
        commit=commit,
        error=error,
        message=message,
    )
    group_id = record["group_id"]
    spent = (
        ctx.owed_commit_group_attempts.get(group_id, 0)
        if isinstance(group_id, str)
        else commit_settlement.MAX_COMMIT_GROUP_RETRIES
    )
    retrying = group is not None and spent < commit_settlement.MAX_COMMIT_GROUP_RETRIES
    record["retrying"] = retrying
    with ctx.persistence.lock:
        commit_settlement._append_commit_failure(ctx.payload, record)
        ctx.persistence.write_state(ctx.payload)
    ctx.progress.commit_failed(record)
    if not retrying:
        ctx.owed_commit_group = None
        return
    ctx.owed_commit_group_attempts[str(group_id)] = spent + 1
    ctx.owed_commit_group = dict(group) if group is not None else None


def _settle_owed_commit_group(ctx: RunContext) -> None:
    """Complete the group this run still owes before it commits anything else.

    This is the restart invariant applied inside a single run: an
    unresolved group is finished at the next commit boundary, ahead of the
    next worker and ahead of the end-of-run commit, so a transient failure
    heals without a human. A second failure re-arms the same retry rather
    than escalating.
    """
    group = ctx.owed_commit_group
    if group is None:
        return
    ctx.owed_commit_group = None
    session_id = (
        str(group["session_id"]) if group.get("session_id") is not None else None
    )
    recovered_group: dict[str, Any] = dict(group)
    failed_commit: dict[str, Any] | None = None
    recovery_error: str | None = None
    try:
        recovered_group, failed_commit = commit_settlement._run_commit_group(
            event_path=ctx.event_path,
            payload=ctx.payload,
            progress=ctx.progress,
            verification_context=ctx.verification_context,
            run_dir=ctx.run_dir,
            run_id=ctx.run_id,
            session_id=session_id,
            targets=commit_settlement._resolve_recovered_commit_targets(
                group, ctx.verification_context
            ),
            generation=ctx.commit_generation,
            recovered_group=group,
            persistence=ctx.persistence,
        )
    except (RunError, commit_groups.CommitGroupError) as error:
        recovery_error = str(error)
    resolved = recovery_error is None and failed_commit is None
    with ctx.persistence.lock:
        ctx.payload["commit_group_recovery"] = {
            "group_id": group.get("group_id"),
            "success": resolved,
            "error": recovery_error,
            "group": recovered_group,
        }
        ctx.persistence.write_state(ctx.payload)
    if resolved:
        ctx.progress.commit_group_recovered(recovered_group)
        return
    _soft_fail_commit(
        ctx,
        scope=str(group.get("scope") or "session"),
        session_id=session_id,
        group=recovered_group,
        commit=failed_commit,
        error=recovery_error,
        message=(
            "unresolved auto-commit group could not be completed "
            "before the next session"
        ),
    )


def _handle_run_control_action(
    ctx: RunContext, action: run_control.RunControlAction
) -> None:
    if action.action in ("consumed", "abort-consumed"):
        # Both consumptions are recorded by the site that acts on them, and
        # an abort's consumption has three distinct outcomes this callback
        # cannot tell apart.
        return
    if action.action == "tail-disabled":
        ctx.inline_tail.disable()
    with ctx.persistence.lock:
        context = (
            None if ctx.run_control_context is None else dict(ctx.run_control_context)
        )
        _record_run_control_action(
            event_path=ctx.event_path,
            payload=ctx.payload,
            control_payload=ctx.control_payload,
            action=action,
            context=context,
            persistence=ctx.persistence,
        )
    if action.action == "tail-enabled":
        ctx.inline_tail.enable()


def _handle_run_control_status(
    ctx: RunContext, action: run_control.RunControlAction
) -> None:
    with ctx.persistence.lock:
        context = (
            None if ctx.run_control_context is None else dict(ctx.run_control_context)
        )
        status_control = dict(ctx.control_payload)
        status_control["stop_after_current"] = action.stop_after_current
        status_control["tail_enabled"] = action.tail_enabled
        status_control["next_harness"] = action.next_harness
        status_payload = dict(ctx.payload)
        active_turn = ctx.payload.get("active_turn")
        if isinstance(active_turn, dict):
            status_payload["active_turn"] = dict(active_turn)
        elif context is not None:
            status_payload["active_turn"] = dict(context)
        turns = ctx.payload.get("turns")
        if isinstance(turns, list):
            status_payload["turns"] = [
                dict(turn) if isinstance(turn, dict) else turn for turn in turns
            ]

    try:
        status_snapshot = ctx.plan_cache.get(ctx.plan_dir)
        plan_error = None
    except plans.PlanError as error:
        status_snapshot = None
        plan_error = str(error)

    ctx.progress.status_snapshot(
        run_reporting._compact_status_lines(
            payload=status_payload,
            control=status_control,
            snapshot=status_snapshot,
            plan_error=plan_error,
        )
    )


def _expire_run_guards(ctx: RunContext) -> None:
    """Disable this run's command gate on every exit path."""
    run_coordination.expire_run_guards(
        controller=ctx.guard_controller,
        event_path=ctx.event_path,
        payload=ctx.payload,
        control_payload=ctx.control_payload,
        persistence=ctx.persistence,
    )


def _abandon_turn(ctx: RunContext) -> None:
    """Drop this run's interactive surfaces when a turn cannot be launched."""
    if ctx.run_controls is not None:
        ctx.run_controls.stop()
    ctx.inline_tail.stop()


def _run_locked(
    *,
    args: argparse.Namespace,
    cwd: Path,
    timeout: int | None,
    max_sessions: int | None,
    commit_mode: str | None,
    lock_info: state.LockInfo,
    run_id: str,
    reply_text: str | None = None,
    reply_origin: str = run_prompts.REPLY_ORIGIN_ARGV,
    verification_context: verification.VerificationContext | None = None,
    wakeup: threading.Event | None = None,
) -> tuple[int, dict[str, Any]]:
    # The caller holds this run's lock and opened the wakeup endpoint before
    # publishing it, so `wakeup` is already being set by producers from here
    # on. A wakeup that lands outside the poll loop below only leaves the
    # event set for the next waiter to observe.
    if verification_context is None:
        verification_context = verification.VerificationContext.legacy(cwd)
    plan_cache = plans.PlanSnapshotCache()
    normalized_review_handoffs = list(
        plans.normalize_generated_review_handoff_artifacts(args.plan_dir)
    )
    executed_review_requires_artifact = False
    # ASI-136: a repair publication that died mid-flight owns this plan's files
    # until it is rolled back, and its children are pending but not loop-ready.
    # Recover under this run's lock, before the first snapshot, so selection
    # never sees a child whose loop-ready state was never established.
    repair_publication_recovery: dict[str, Any] | None = None
    if not args.dry_run:
        try:
            repair_publication_recovery = (
                review_repair.recover_review_repair_publication(args.plan_dir)
            )
        except plans.PlanError as error:
            repair_publication_recovery = {
                "recovered": False,
                "reason": "recovery-failed",
                "error": str(error),
            }
    snapshot = plan_cache.get(args.plan_dir)
    if repair_publication_recovery is not None and not (
        repair_publication_recovery.get("recovered")
    ):
        # The image could not be put back, so the plan on disk is neither the
        # state the publication started from nor a published one. Refuse to run
        # rather than select out of a half-published generation.
        payload = run_outcome._error_payload(
            args,
            reason="malformed-plan",
            message=(
                "an interrupted review repair publication could not be rolled "
                f"back: {repair_publication_recovery.get('reason')}"
            ),
            paused=True,
            cwd=cwd,
            snapshot=snapshot,
        )
        payload["details"] = {
            "review_repair_publication_recovery": repair_publication_recovery
        }
        payload["pause_details"] = dict(payload["details"])
        return 1, payload
    if not args.dry_run:
        run_inputs._ensure_legacy_run_metadata(snapshot.plan_dir, verification_context)
    state_dir = state.plan_state_dir(snapshot.plan_dir, args.state_dir)
    run_dir = state_dir / "runs" / run_id
    # The run's command gate. Constructed for every run and left empty until a
    # guard is added, so the drain always has somewhere to apply one and never
    # has to decide whether this run is guardable.
    #
    # The repository binding is supplied here rather than at the first turn
    # because the queue is drained before any turn starts: a protected-path
    # rule naming a declared alias would otherwise be refused as unknown
    # simply for having been queued before the run began.
    guard_controller = guards.GuardController(
        run_id=run_id,
        run_dir=run_dir,
        workspace_aliases=(
            {
                repository.alias: str(repository.root)
                for repository in verification_context.workspace.repositories
            }
            if verification_context.workspace is not None
            else {guards.LEGACY_PATH_QUALIFIER: str(cwd)}
        ),
    )
    event_path = state_dir / events.EVENTS_FILE_NAME
    state_path = state_dir / events.STATE_FILE_NAME
    try:
        unresolved_commit_group = commit_groups.load_unresolved_group(
            state_path=state_path,
            event_path=event_path,
        )
    except commit_groups.CommitGroupError as error:
        unresolved_commit_group = None
        commit_group_load_error = str(error)
    else:
        commit_group_load_error = None
    mailbox_activation = run_selection._mailbox_activation_payload(
        args, snapshot.plan_dir
    )
    mailbox_enabled = bool(mailbox_activation["enabled"])
    review_guard_excludes = review_guard._review_guard_excludes(args)
    review_guard_include_ignored = review_guard._review_guard_include_ignored(args)
    # One selection for every commit this run makes, including the recovery of
    # a group a previous process left unresolved: the declared implementation
    # harness, not whichever harness a live switch moved a turn onto.
    commit_generation = run_commit._commit_generation(args)
    audited_repositories, audited_registry_declared = (
        run_termination._audited_repositories(verification_context)
    )
    audit_context = _TerminalAuditContext(
        plan=supervisor_audit.plan_audit(args),
        run_dir=run_dir,
        state_dir=state_dir,
        plan_dir=snapshot.plan_dir,
        workspace_root=(
            cwd
            if verification_context.workspace is None
            else verification_context.workspace.root
        ),
        ledger_state_dir=(None if args.state_dir is None else Path(args.state_dir)),
        repositories=audited_repositories,
        registry_declared=audited_registry_declared,
    )

    # Detect state stranded under a moved plan's old path hash *before* this
    # run writes its own state.json (which would suppress the scan). A scan
    # failure must never block the run, so any error yields no candidates.
    try:
        orphan_candidates = state.orphaned_state_candidates(snapshot, args.state_dir)
    except Exception:  # pragma: no cover - defensive; scan is best-effort
        orphan_candidates = []

    reply_context: qna.QnAContext | None = None
    if reply_text is not None:
        try:
            reply_context = qna.load_qna_context(
                snapshot.plan_dir,
                state_dir=args.state_dir,
            )
        except qna.QnAError as error:
            payload = run_outcome._error_payload(
                args,
                reason=error.reason,
                message=error.error,
                paused=False,
                cwd=cwd,
                snapshot=snapshot,
            )
            payload["details"] = dict(error.details)
            return 1, payload

    payload = run_payloads._initial_payload(
        args=args,
        snapshot=snapshot,
        state_dir=state_dir,
        run_dir=run_dir,
        event_path=event_path,
        state_path=state_path,
        lock_info=lock_info,
        cwd=cwd,
        run_id=run_id,
        commit_mode=commit_mode,
        mailbox_activation=mailbox_activation,
        verification_context=verification_context,
    )
    spark_run_suppression: str | None = None
    spark_fallback_reason: str | None = None
    if normalized_review_handoffs:
        payload["normalized_review_handoffs"] = [
            str(path) for path in normalized_review_handoffs
        ]
    if unresolved_commit_group is not None:
        commit_settlement._upsert_commit_group(payload, unresolved_commit_group)
    if commit_group_load_error is not None:
        payload["commit_group_load_error"] = commit_group_load_error
    reply_state_preserver = _ReplyStatePreserver(
        state_path=state_path,
        event_path=event_path,
        enabled=reply_text is not None and not args.dry_run,
    )
    pending_reply = (
        run_prompts._PendingReply(
            text=reply_text,
            context=reply_context,
            origin=reply_origin,
            process_start=True,
        )
        if reply_text is not None and reply_context is not None
        else None
    )
    # Session 4 wires committed live-wait resolutions into this slot. Keeping
    # a distinct one-shot type here prevents answerless continuations from
    # inheriting reply semantics or leaking into a later turn.
    pending_resolution: run_prompts._PendingResolution | None = None

    persist_run_state = not (args.dry_run and reply_text is not None)
    output = run_reporting._StderrOutputCoordinator(enabled=not args.json)
    ctx = RunContext(
        run_id=run_id,
        plan_dir=snapshot.plan_dir,
        run_dir=run_dir,
        event_path=event_path,
        payload=payload,
        persistence=run_persistence._RunPersistence(
            write_state=persist_run_state,
            append_events=persist_run_state,
        ),
        # The reporter renders commit failures but owns none of the commit
        # lifecycle's vocabulary, so the run hands it the key terminalization
        # writes owed groups under and the labeller that names one.
        progress=run_reporting._ProgressReporter(
            enabled=not args.json,
            output=output,
            commit_group_label=commit_settlement._unresolved_commit_group_label,
            unresolved_groups_key=commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY,
        ),
        inline_tail=run_reporting._InlineTailController(
            plan_dir=snapshot.plan_dir,
            state_dir=args.state_dir,
            output=output,
        ),
        reply_state_preserver=reply_state_preserver,
        audit_context=audit_context,
        guard_controller=guard_controller,
        plan_cache=plan_cache,
        verification_context=verification_context,
        commit_generation=commit_generation,
    )

    ctx.persistence.append_event(
        ctx.event_path,
        events.RUN_STARTED_EVENT,
        {
            "plan_dir": str(snapshot.plan_dir),
            "harness": args.harness,
            "model": ctx.payload["model"],
            "model_source": ctx.payload["model_source"],
            "effort": ctx.payload["effort"],
            "effort_source": ctx.payload["effort_source"],
            "spark": ctx.payload["spark"],
            "dry_run": args.dry_run,
            "max_sessions": max_sessions,
            "max_pending_sessions": getattr(args, "max_pending_sessions", None),
            "commit_mode": commit_mode,
            "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            "note": ctx.payload.get("note"),
            "run_id": ctx.run_id,
            "run_dir": str(ctx.run_dir),
            "run_started_at": ctx.payload["run_started_at"],
            "approval_mode": ctx.payload["approval_mode"],
            "no_mailbox": run_selection._no_mailbox(args),
            "mailbox_enabled": mailbox_enabled,
            "counts": ctx.payload["initial_snapshot"]["counts"],
            "next_pending": ctx.payload["initial_snapshot"]["next_pending"],
            **(
                {
                    "cwd": str(cwd),
                    **run_prompts._workspace_payload_fields(ctx.verification_context),
                }
                if ctx.verification_context.workspace is not None
                else {}
            ),
        },
    )
    ctx.persistence.write_state(ctx.payload)
    run_persistence._notify_agent_mux_loop_started(ctx.payload, ctx.persistence)
    ctx.progress.run_started(ctx.payload)
    ctx.progress.orphaned_state(orphan_candidates)

    invalid_pause = run_inputs._invalid_status_pause(snapshot)
    if invalid_pause is not None:
        reason, message, details = invalid_pause
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=reason,
            message=message,
            details=details,
        )
        return _terminalize(ctx, 1)

    if commit_group_load_error is not None:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="commit-failed",
            message="durable commit-group evidence could not be reconstructed",
            details={"error": commit_group_load_error},
        )
        return _terminalize(ctx, 1)

    if unresolved_commit_group is not None and args.dry_run:
        with ctx.persistence.lock:
            ctx.payload["commit_group_recovery"] = {
                "group_id": unresolved_commit_group.get("group_id"),
                "success": False,
                "pending": True,
                "error": None,
                "group": unresolved_commit_group,
            }
    elif unresolved_commit_group is not None:
        # The restart half of the invariant: a group another run left owed is
        # completed here, before this run launches a worker of its own. A
        # second failure soft-fails like any other, so the group is retried
        # again at this run's next commit boundary.
        ctx.owed_commit_group = dict(unresolved_commit_group)
        _settle_owed_commit_group(ctx)

    review_requested = getattr(args, "review_harness", None) is not None
    max_review_fix_cycles = run_inputs._max_review_fix_cycles(args)
    review_resume: review_continuation.ReviewResume | None = None
    if review_requested:
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=True,
        )

    if snapshot.next_pending is None:
        if not run_inputs._all_sessions_complete(snapshot):
            ctx.pause(
                reason="no-pending-but-incomplete",
                message="no pending session was found, but not every session is "
                "complete",
                details={"snapshot": _snapshot_payload(snapshot)},
            )
            return _terminalize(ctx, 1)
        if not review_requested:
            with ctx.persistence.lock:
                ctx.payload["reason"] = "all-complete"
            return _terminalize(ctx, 0)

    if not review_requested:
        pending_review = review_session_body.find_pending_generated_review_session(
            snapshot.plan_dir
        )
        if pending_review is not None:
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason="review-harness-required",
                message="a pending generated review session is present; "
                "rerun with --review-harness to execute it",
                details={
                    "session_id": pending_review.session_id.value,
                    "session_path": str(pending_review.path),
                },
            )
            return _terminalize(ctx, 1)

    if args.dry_run:
        with ctx.persistence.lock:
            ctx.payload["reason"] = "dry-run"
            ctx.payload["planned"] = run_payloads._dry_run_payload(
                args=args,
                snapshot=snapshot,
                cwd=cwd,
                run_dir=ctx.run_dir,
                mailbox_activation=mailbox_activation,
                reply_text=reply_text,
                reply_context=reply_context,
                reply_origin=reply_origin,
                verification_context=ctx.verification_context,
            )
        return _terminalize(ctx, 0)

    if (
        ctx.verification_context.workspace is not None
        and not MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED
    ):
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=MULTI_REPO_CAPABILITY_PAUSE_REASON,
            message="multi-repository harness turns are disabled until the "
            "authoritative workspace write guard is installed",
            details={
                "workspace_root": str(ctx.verification_context.root),
                "registry_digest": ctx.verification_context.registry_digest,
            },
        )
        return _terminalize(ctx, 1)

    if (
        review_resume is not None
        and review_resume.action in ("run-repairs", "rereview")
        and (
            review_resume.source_disposition is None
            or review_resume.source_disposition
            in (
                *review_continuation.BUDGET_REVIEW_DISPOSITIONS,
                review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED,
            )
        )
        and review_resume.repair_session_ids
        and not review_cycle._has_successful_review_reopen(
            ctx.event_path, review_resume.review_session_id
        )
    ):
        # The atomic child write can win a crash race against event append.
        # Generated children are authoritative.  This also recovers an
        # eventless findings review whose cycle record was lost, preserving
        # its fingerprints before the next review overwrites the artifact.
        recovered_record = review_cycle._review_reopen_record(
            event_path=ctx.event_path,
            payload=ctx.payload,
            resume=review_resume,
            max_review_fix_cycles=max_review_fix_cycles,
            materialized=True,
            repair_session_ids=review_resume.repair_session_ids,
            result="recovered-existing-children",
            recovered=True,
        )
        review_cycle._record_review_reopen(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            record=recovered_record,
        )
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=True,
        )

    allow_not_loop_ready = readiness_gate._allow_not_loop_ready(args)
    max_pending_sessions = getattr(args, "max_pending_sessions", None)
    preflight_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "harness": args.harness,
        "commit_mode": commit_mode,
        "state_dir": args.state_dir,
        "max_pending_sessions": max_pending_sessions,
    }
    resume_report = readiness_gate._reduced_preflight_report(
        snapshot.plan_dir,
        snapshot=snapshot,
        lock_info=lock_info,
        preflight_kwargs=preflight_kwargs,
    )
    ctx.core_skipped = resume_report is not None
    if ctx.core_skipped:
        preflight_report = resume_report
        ctx.persistence.append_event(
            ctx.event_path,
            events.PREFLIGHT_SKIPPED_EVENT,
            {
                "readiness_state": preflight_report.readiness.state,
                "readiness_sha256": preflight_report.expected_readiness_sha256,
            },
        )
    else:
        preflight_report = readiness_gate._strip_own_lock_findings(
            preflight.run_preflight(snapshot.plan_dir, **preflight_kwargs),
            lock_info=lock_info,
        )
    ctx.gate_payload = readiness_gate._readiness_gate_payload(preflight_report, cwd=cwd)
    if ctx.core_skipped:
        ctx.gate_payload["core_skipped"] = True
    ctx.gate_passed = ctx.gate_payload["ok"]
    review_only_complete_start = readiness_gate._review_only_complete_start(
        preflight_report,
        review_requested=review_requested,
        snapshot=snapshot,
    )
    with ctx.persistence.lock:
        ctx.payload["readiness_gate"] = ctx.gate_payload
        if preflight_report.readiness is not None:
            ctx.payload["readiness_state_initial"] = preflight_report.readiness.state

    initial_workspace_scope_repair = (
        workspace_evidence._latest_workspace_scope_repair_requirement(
            ctx.event_path, snapshot.next_pending.id
        )
        if ctx.verification_context.workspace is not None
        and snapshot.next_pending is not None
        else None
    )
    if initial_workspace_scope_repair is not None and not ctx.gate_passed:
        session_id = snapshot.next_pending.id
        details = {
            **_session_details_payload(session_id),
            "readiness_gate": ctx.gate_payload,
            "workspace_write_guard": initial_workspace_scope_repair,
        }
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            message=f"{_session_label(session_id)} has retained changes to "
            "registered repositories outside its write aliases; repair "
            "its durable repository scope and rerun expansion plus "
            "preflight before continuing",
            details=details,
        )
        return _terminalize(ctx, 1)

    non_bypassable = readiness_gate._non_bypassable_blockers(preflight_report)
    if non_bypassable:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="not-loop-ready",
            message=readiness_gate._readiness_gate_failure_message(preflight_report),
            details={**ctx.gate_payload, "blocker": list(non_bypassable)},
        )
        return _terminalize(ctx, 1)

    if (
        not ctx.gate_passed
        and not allow_not_loop_ready
        and not review_only_complete_start
    ):
        preflight_report, repair_payload = readiness_gate._repair_readiness_gate(
            preflight_report,
            plan_dir=snapshot.plan_dir,
            lock_info=lock_info,
            cwd=cwd,
            harness=args.harness,
            commit_mode=commit_mode,
            state_dir=args.state_dir,
            max_pending_sessions=max_pending_sessions,
            event_path=ctx.event_path,
            persistence=ctx.persistence,
            snapshot=snapshot,
            review_requested=review_requested,
            core_skipped=ctx.core_skipped,
        )
        ctx.core_skipped = bool(repair_payload["core_skipped"])
        ctx.gate_payload = readiness_gate._readiness_gate_payload(
            preflight_report, cwd=cwd
        )
        if ctx.core_skipped:
            ctx.gate_payload["core_skipped"] = True
        ctx.gate_passed = ctx.gate_payload["ok"]
        review_only_complete_start = readiness_gate._review_only_complete_start(
            preflight_report,
            review_requested=review_requested,
            snapshot=snapshot,
        )
        with ctx.persistence.lock:
            ctx.payload["readiness_gate"] = ctx.gate_payload
            ctx.payload["readiness_gate_repair"] = repair_payload

        if not ctx.gate_passed and not review_only_complete_start:
            gate_message = readiness_gate._readiness_gate_failure_message(
                preflight_report
            )
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason="not-loop-ready",
                message=gate_message,
                details=ctx.gate_payload,
            )
            return _terminalize(ctx, 1)

    try:
        if commit_mode == run_selection.COMMIT_MODE_END:
            review_guard._validate_commit_at_end_scope(
                snapshot=snapshot,
                max_sessions=max_sessions,
            )
        if commit_mode is not None:
            commit_preflight = review_guard._validate_commit_preflight(
                cwd,
                verification_context=ctx.verification_context,
            )
            with ctx.persistence.lock:
                ctx.payload["commit_preflight"] = commit_preflight
    except RunError as error:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="commit-preflight",
            message=str(error),
            details={
                "commit_mode": commit_mode,
                "max_sessions": max_sessions,
                "pending_sessions": _status_counts(snapshot)["pending"],
            },
        )
        return _terminalize(ctx, 1)

    should_transition = ctx.gate_passed or (
        allow_not_loop_ready and preflight_report.readiness is not None
    )
    if should_transition:
        running_success, running_error = readiness_gate.write_readiness_state(
            snapshot.plan_dir,
            "running",
            expected_sha256=preflight_report.expected_readiness_sha256,
        )
        with ctx.persistence.lock:
            if running_success:
                ctx.payload["readiness_state_active"] = True
                ctx.payload["readiness_state_current"] = "running"
            else:
                ctx.payload["readiness_state_active"] = False
                ctx.payload["readiness_state_write_error"] = running_error

    run_control_lock = ctx.persistence.lock
    ctx.control_payload = dict(ctx.payload["control"])

    ctx.run_controls = run_control.RunControlListener.for_run(
        args,
        state=run_control.RunControlState(current_harness=args.harness),
        stdin=sys.stdin,
        stderr=output.stream,
        on_action=functools.partial(_handle_run_control_action, ctx),
        on_status=functools.partial(_handle_run_control_status, ctx),
    )
    ctx.run_controls.start()

    # Served unconditionally, unlike the keyboard listener: a `--json`
    # supervised run is precisely the case with no interactive control surface,
    # so it is the one that most needs an `asl control` command observed now
    # rather than at the end of the poll interval. The caller owns the endpoint
    # feeding this event; a run reached without one still polls.
    if wakeup is None:
        wakeup = threading.Event()

    mailbox_poll = _MailboxPoll(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=args.state_dir,
        control_state=ctx.run_controls.state,
        seen_control_ids=ctx.seen_external_command_ids,
        payload=ctx.payload,
        event_path=ctx.event_path,
        control_payload=ctx.control_payload,
        persistence=ctx.persistence,
        run_control_lock=run_control_lock,
        enabled=mailbox_enabled,
        wakeup=wakeup,
        guard_controller=ctx.guard_controller,
    )

    # Drain any commands that were queued before the run started so the
    # first turn already reflects them.
    _drain_external_control_queue(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=args.state_dir,
        control_state=ctx.run_controls.state,
        seen_ids=ctx.seen_external_command_ids,
        payload=ctx.payload,
        event_path=ctx.event_path,
        control_payload=ctx.control_payload,
        context=None,
        persistence=ctx.persistence,
        run_control_lock=run_control_lock,
        wakeup=wakeup,
        guard_controller=ctx.guard_controller,
    )
    pending_exception_guard: dict[str, Any] | None = None

    def disarm_exception_guard() -> None:
        """Retire the crash-path copy of the write guard for this turn.

        The post-turn sequence disarms this *before* it enforces the live
        guard, so the handler below cannot re-enforce the same guard against
        the same before-image if the live one raises. That ordering is why it
        is a callback rather than a value the boundary hands back.
        """
        nonlocal pending_exception_guard
        pending_exception_guard = None

    try:
        while True:
            if ctx.idle_baseline_pinned:
                # A path that reached here after deliberately establishing its
                # own baseline opts out by pinning it. Refreshing would
                # overwrite that earlier instant with this one and charge the
                # gap it was measuring to the runner.
                ctx.idle_baseline_pinned = False
            elif not ctx.first_loop_pass:
                # Every path back to the top got here by finishing
                # runner-owned work — a turn and its verification and
                # finalization, or the all-complete branch's final commit,
                # repair materialization, re-gating, readiness writes and
                # review creation. Refreshing the baseline in this one place
                # covers every path, including ones added later.
                ctx.runner_work_finished_at = _utc_now()
            entering_first_pass = ctx.first_loop_pass
            ctx.first_loop_pass = False
            if not entering_first_pass:
                # One place covers both boundaries a still-owed group must be
                # settled at: ahead of the next worker, and ahead of the
                # end-of-run commit the all-complete branch below runs. The
                # first pass is excluded because the recovery above the loop
                # is that pass's boundary and has just run.
                _settle_owed_commit_group(ctx)
            pending_session = snapshot.next_pending

            if pending_session is None:
                # No session is selectable, which is not yet an ending: the
                # plan may still owe a commit, a review, or the repair
                # children a review reopens. The completion boundary decides
                # which, grows the plan when it must, and hands back the
                # image it ended on.
                completion = run_completion.complete_plan(
                    ctx,
                    snapshot=snapshot,
                    args=args,
                    cwd=cwd,
                    commit_mode=commit_mode,
                    review_requested=review_requested,
                    max_review_fix_cycles=max_review_fix_cycles,
                    allow_not_loop_ready=allow_not_loop_ready,
                    review_guard_excludes=review_guard_excludes,
                    review_guard_include_ignored=review_guard_include_ignored,
                    run_controls=ctx.run_controls,
                    run_control_lock=run_control_lock,
                    wakeup=wakeup,
                    soft_fail_commit=functools.partial(_soft_fail_commit, ctx),
                )
                snapshot = completion.snapshot
                if completion.action == run_completion.ACTION_CONTINUE:
                    continue
                break

            classification = turn_preparation.classify_session(pending_session.path)
            is_review_session = classification.is_review_session
            is_repair_session = classification.is_repair_session
            review_preparation = turn_preparation.prepare_review_session(
                ctx,
                session=pending_session,
                snapshot=snapshot,
                classification=classification,
                cwd=cwd,
                review_guard_excludes=review_guard_excludes,
                review_guard_include_ignored=review_guard_include_ignored,
            )
            snapshot = review_preparation.snapshot
            if review_preparation.paused:
                break
            prepared_review_workspace_snapshot = review_preparation.workspace_snapshot
            review_session_version = review_preparation.session_version

            if (
                not is_review_session
                and max_sessions is not None
                and ctx.completed_count >= max_sessions
            ):
                with ctx.persistence.lock:
                    ctx.payload["reason"] = "max-sessions"
                break

            before = snapshot
            session_id = pending_session.id
            session_label = _session_label(session_id)
            session_identity = _session_identity_payload(session_id)
            session_details = _session_details_payload(session_id)
            ctx.last_session_details = session_details

            if is_review_session:
                with ctx.persistence.lock:
                    if ctx.payload.get("review_session_id") != session_id:
                        ctx.payload["review_session_id"] = session_id

            with ctx.persistence.lock:
                turn_index = len(ctx.payload["turns"]) + 1
                turns_already_run = bool(ctx.payload["turns"])
            paths = _turn_paths(
                ctx.run_dir,
                turn_index=turn_index,
                session_id=session_id,
            )
            implementation_overrides: Mapping[str, Mapping[str, str]] = {}
            pre_turn_control_context = {
                **session_identity,
                "turn_index": turn_index,
                "total_sessions": sum(_status_counts(before).values()),
            }
            _drain_external_control_queue(
                plan_dir=before.plan_dir,
                state_dir_arg=args.state_dir,
                control_state=ctx.run_controls.state,
                seen_ids=ctx.seen_external_command_ids,
                payload=ctx.payload,
                event_path=ctx.event_path,
                control_payload=ctx.control_payload,
                context=pre_turn_control_context,
                persistence=ctx.persistence,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                guard_controller=ctx.guard_controller,
            )
            with run_control_lock:
                # A stop observed here must never let another turn start. Only
                # a stop that lands after a turn already ran in this process
                # qualifies: one queued before the run began still means "run
                # the first session, then stop". A pending reply or resolution
                # continuation belongs to the session already in flight, so it
                # runs before the stop is honored.
                stop_before_turn = (
                    turns_already_run
                    and pending_reply is None
                    and pending_resolution is None
                    and ctx.run_controls.state.stop_after_current
                )
                if stop_before_turn:
                    run_termination._honor_stop_after_current(
                        ctx.run_controls,
                        snapshot=before,
                        payload=ctx.payload,
                        event_path=ctx.event_path,
                        control_payload=ctx.control_payload,
                        context=pre_turn_control_context,
                        persistence=ctx.persistence,
                    )
            if stop_before_turn:
                break
            if is_review_session:
                turn_harness = args.review_harness
            else:
                with run_control_lock:
                    turn_harness = run_selection._selected_future_harness(
                        args, ctx.control_payload
                    )
                    implementation_overrides = (
                        ctx.run_controls.state.implementation_overrides
                    )
            if turn_harness is None:
                raise RunError("--review-harness is required for a review session")
            if getattr(args, "spark", False) and spark_run_suppression is None:
                try:
                    refreshed_availability = model_availability.availability(
                        state.state_root(args.state_dir), run_selection.SPARK_MODEL
                    )
                except OSError as error:
                    refreshed_availability = model_availability.Availability(
                        False,
                        "ledger-unreadable",
                        warning=f"model availability ledger is unreadable: {error}",
                    )
                with ctx.persistence.lock:
                    ctx.payload["spark"].update(refreshed_availability.to_dict())
            requested_model, turn_model_effort, spark_route = (
                run_selection._spark_turn_route(
                    args,
                    plan_dir=before.plan_dir,
                    session_path=pending_session.path,
                    session_id=session_id,
                    harness=turn_harness,
                    is_review_turn=is_review_session,
                    implementation_overrides=implementation_overrides,
                    spark_state=ctx.payload["spark"],
                    run_suppression=spark_run_suppression,
                    fallback_reason=spark_fallback_reason,
                )
            )
            spark_fallback_reason = None
            with ctx.persistence.lock:
                ctx.payload["spark"]["active_route"] = spark_route
            requested_effort = run_selection._requested_effort(
                args,
                harness=turn_harness,
                is_review_turn=is_review_session,
                implementation_overrides=implementation_overrides,
            )
            preflight_failure = run_selection._harness_preflight_failure(
                turn_harness,
                session_id=session_id,
            )
            if preflight_failure is not None:
                message, details = preflight_failure
                ctx.record_final_snapshot(before)
                ctx.pause(
                    reason="harness-unavailable",
                    message=f"harness unavailable before {session_label}: {message}",
                    details=details,
                )
                break
            packet_inputs = turn_preparation.collect_packet_inputs(
                ctx,
                snapshot=before,
                classification=classification,
                args=args,
                session_id=session_id,
                mailbox_enabled=mailbox_enabled,
            )
            workspace = ctx.verification_context.workspace
            session_scope = (
                run_prompts._session_scope(pending_session, workspace)
                if workspace is not None
                else None
            )
            retained_scope_repair = turn_preflight.resolve_retained_scope_repair(
                event_path=ctx.event_path,
                session_id=session_id,
                workspace=workspace,
            )
            repair_resumption = turn_preparation.resume_repair_session(
                ctx,
                session=pending_session,
                snapshot=before,
                classification=classification,
                args=args,
                cwd=cwd,
                workspace=workspace,
                session_scope=session_scope,
                session_label=session_label,
                session_identity=session_identity,
                session_details=session_details,
                retained_scope_repair=retained_scope_repair,
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=review_requested,
            )
            session_scope = repair_resumption.session_scope
            regenerated_write_scope = repair_resumption.regenerated_write_scope
            if repair_resumption.paused:
                break
            turn_reply = pending_reply
            turn_resolution = pending_resolution
            use_reply_prompt = turn_reply is not None
            use_resolution_prompt = turn_resolution is not None
            turn_prompt = turn_preparation.prepare_turn_prompt(
                ctx,
                snapshot=before,
                classification=classification,
                inputs=packet_inputs,
                args=args,
                cwd=cwd,
                harness=turn_harness,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                session_scope=session_scope,
                workspace=workspace,
                mailbox_enabled=mailbox_enabled,
                reply=turn_reply,
                resolution=turn_resolution,
            )
            if turn_prompt.paused:
                break
            prompt = turn_prompt.prompt
            reply_source_meta = turn_prompt.reply_source
            resolution_source_meta = turn_prompt.resolution_source
            turn_record = turn_preflight.build_active_turn(
                ctx,
                snapshot=before,
                classification=classification,
                prompt=turn_prompt,
                session_identity=session_identity,
                turn_index=turn_index,
                paths=paths,
                harness=turn_harness,
                model_effort=turn_model_effort,
                spark_route=spark_route,
                cwd=cwd,
            )
            active_turn = turn_record.payload
            total_sessions = turn_record.total_sessions
            log_stream_callback = _prepare_stream_logs(paths)
            mailbox_poll.set_context(active_turn)
            # Handed to the turn before it starts even though nothing has
            # requested an abort yet: the wait loop can only be cancellable if
            # the signal object already exists when the harness is launched.
            abort_signal = harnesses.AbortSignal()
            mailbox_poll.set_abort(abort_signal)
            # The other turn-scoped control input beside the abort signal, and
            # bound to the same identity the delivery ledger records. It stays
            # inert until the harness opens it around the live process.
            delivery_endpoint = guidance_delivery.LiveDeliveryEndpoint(
                guidance_delivery.TurnIdentity(
                    run_id=ctx.run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                )
            )
            mailbox_poll.set_live_delivery(delivery_endpoint)

            live_turn = run_commit._LiveTurn(
                payload=ctx.payload,
                event_path=ctx.event_path,
                progress=ctx.progress,
                active_turn=active_turn,
                persistence=ctx.persistence,
                poll_callback=mailbox_poll,
                poll_interval=LIVE_CONTROL_POLL_INTERVAL_SECONDS,
                wakeup=wakeup,
            )
            # One executor per turn: the identity and the artifact paths are
            # what make this turn this turn, so they are bound once here
            # rather than repeated at each of the three transitions below.
            turn_executor = turn_execution.TurnExecutor(
                identity=turn_execution.TurnIdentity(
                    run_id=ctx.run_id,
                    turn_index=turn_index,
                    session_id=session_id,
                ),
                paths=paths,
                event_path=ctx.event_path,
                persistence=ctx.persistence,
                progress=ctx.progress,
                plan_cache=ctx.plan_cache,
            )

            stream_callback = _live_stream_callback(log_stream_callback, live_turn)

            with run_control_lock:
                ctx.run_control_context = dict(active_turn)
            baselines = turn_preflight.capture_turn_baselines(
                ctx,
                snapshot=before,
                state_dir_arg=args.state_dir,
                cwd=cwd,
                classification=classification,
                workspace=workspace,
                session_scope=session_scope,
                review_base=prepared_review_workspace_snapshot,
                retained_scope_repair=retained_scope_repair,
                regenerated_write_scope=regenerated_write_scope,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                review_guard_excludes=review_guard_excludes,
                review_guard_include_ignored=review_guard_include_ignored,
                active_turn=active_turn,
                live_turn=live_turn,
                paths=paths,
            )
            if baselines.paused:
                break
            workspace_snapshot_before = baselines.workspace_snapshot
            legacy_snapshot_before = baselines.legacy_snapshot
            review_fingerprint_before = baselines.review_fingerprint
            external_digests_before = baselines.external_digests
            turn_started_ns = baselines.turn_started_ns
            pending_exception_guard = baselines.pending_exception_guard
            protected_path_baseline = baselines.protected_path_baseline
            plan_artifacts_before = plans.session_artifact_digests(
                before.plan_dir, exclude_session_id=session_id
            )
            if use_reply_prompt:
                assert reply_source_meta is not None and turn_reply is not None
                # Consumption is explicit rather than positional: a mailbox
                # continuation can begin on any later turn index, and no reply
                # may leak into a subsequent session prompt.
                pending_reply = None
                if turn_reply.process_start:
                    ctx.reply_state_preserver.mark_consumed()
                reply_event_payload = {
                    "run_id": ctx.run_id,
                    "turn_index": turn_index,
                    "session_id": session_id,
                    "reply_length": len(turn_reply.text),
                    "source": reply_source_meta,
                }
                if turn_reply.question_id is not None:
                    reply_event_payload.update(
                        {
                            "origin": turn_reply.origin,
                            "question_id": turn_reply.question_id,
                        }
                    )
                ctx.persistence.append_event(
                    ctx.event_path,
                    events.REPLY_PROVIDED_EVENT,
                    reply_event_payload,
                )
            elif use_resolution_prompt:
                assert (
                    resolution_source_meta is not None and turn_resolution is not None
                )
                # Clear the pending object before recording turn start. A
                # successful start therefore consumes this exact resolution
                # once, even when the same session needs another turn later.
                pending_resolution = None
                ctx.persistence.append_event(
                    ctx.event_path,
                    events.RESOLUTION_CONSUMED_EVENT,
                    {
                        "run_id": ctx.run_id,
                        "turn_index": turn_index,
                        "session_id": session_id,
                        "question_id": turn_resolution.question_id,
                        "resolution_message_id": (
                            turn_resolution.resolution_message_id
                        ),
                        "resolution_seq": turn_resolution.resolution_seq,
                        "action": turn_resolution.action,
                        "note_length": resolution_source_meta["note_length"],
                        "source": resolution_source_meta,
                    },
                )

            # Materialized per turn, not once per run: the guard-bin directory
            # must exist before the child's PATH names it, and a guard added
            # during the previous turn has to be armed for this one. The
            # workspace binding is refreshed here too, because it is what turns
            # the absolute path a tool call carries into the `alias:path` a
            # rule is written in, and a run can change harness mid-flight.
            with run_control_lock:
                ctx.guard_controller.set_workspace_aliases(
                    {
                        repository.alias: str(repository.root)
                        for repository in workspace.repositories
                    }
                    if workspace is not None
                    else {guards.LEGACY_PATH_QUALIFIER: str(cwd)}
                )
                ctx.guard_controller.mark_turn_snapshot(protected_path_baseline)
                turn_env = ctx.guard_controller.prepare_turn(
                    harness=turn_harness,
                    pretool_armed=harnesses.get_adapter(
                        turn_harness
                    ).enforces_guard_pretool,
                )

            execution = turn_executor.launch(
                turn_execution.TurnLaunch(
                    env=turn_env,
                    harness=turn_harness,
                    cwd=cwd,
                    plan_dir=before.plan_dir,
                    prompt=prompt,
                    unsafe_bypass=run_selection._unsafe_bypass(args),
                    timeout=timeout,
                    model=requested_model,
                    effort=requested_effort,
                    abort=abort_signal,
                    live_delivery=delivery_endpoint,
                    stream_callback=stream_callback,
                    on_prompt_accepted=(
                        functools.partial(
                            run_selection._record_next_turn_guidance_delivery,
                            plan_dir=before.plan_dir,
                            state_dir_arg=args.state_dir,
                            event_path=ctx.event_path,
                            append_event=ctx.persistence.append_event,
                            pending_guidance=packet_inputs.pending_guidance,
                            harness=turn_harness,
                            run_id=ctx.run_id,
                            session_id=session_id,
                            turn_index=turn_index,
                        )
                        if (
                            packet_inputs.pending_guidance
                            and harnesses.delivers_guidance_next_turn(turn_harness)
                        )
                        else None
                    ),
                    build_invocation=functools.partial(
                        run_prompts.build_harness_invocation,
                        turn_harness,
                        cwd=cwd,
                        plan_dir=before.plan_dir,
                        unsafe_bypass=run_selection._unsafe_bypass(args),
                        last_message_path=paths["last_message"],
                        model=requested_model,
                        effort=requested_effort,
                    ),
                ),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=ctx.payload,
                after_stop=functools.partial(
                    run_termination._detach_turn_controls, mailbox_poll
                ),
                on_failure=functools.partial(_abandon_turn, ctx),
            )
            result = execution.result
            live_turn_payload = execution.live_turn
            with run_control_lock:
                ctx.run_control_context = None
            plan_artifacts_written_by_runner: tuple[str, ...] = ()
            if is_review_session:
                # Stamped from the turn's own completion rather than from the
                # commit, because every guard and terminating branch below can
                # end the run before the commit that would otherwise be the
                # only record that an independent review happened at all.
                executed_review_requires_artifact = (
                    review_session_version is not None
                    and review_session_version >= STRUCTURED_REVIEW_MIN_VERSION
                )
                with ctx.persistence.lock:
                    ctx.payload["review_session_executed"] = session_id
                normalized_after_review = (
                    plans.normalize_generated_review_handoff_artifacts(before.plan_dir)
                )
                if normalized_after_review:
                    normalized_review_handoffs.extend(normalized_after_review)
                    plan_artifacts_written_by_runner = tuple(
                        path.name for path in normalized_after_review
                    )
                    with ctx.persistence.lock:
                        ctx.payload["normalized_review_handoffs"] = [
                            str(path) for path in normalized_review_handoffs
                        ]
            finalization = turn_executor.finalize(
                execution,
                before=before,
                unredacted_prompt=(
                    prompt if use_reply_prompt or use_resolution_prompt else None
                ),
                pending_exception_guard=pending_exception_guard,
            )
            after = finalization.after
            plan_error = finalization.plan_error
            turn_payload = finalization.turn_payload
            # The turn enters run state as executed-but-unaccepted. Every guard
            # below may still amend or refuse it; `_commit_turn` is what
            # replaces this record with the published one.
            with ctx.persistence.lock:
                ctx.payload["turns"].append(turn_payload)
                # A harness that reports no model leaves the last observation
                # standing rather than erasing it.
                if turn_payload.get("observed_model") is not None:
                    ctx.payload.update(
                        _observed_model_payload(turn_payload["observed_model"])
                    )
                ctx.payload["model_projection"] = run_commit._merged_model_projection(
                    ctx.payload.get("model_projection"), turn_payload
                )
                turn_identity = {
                    "run_id": ctx.run_id,
                    "turn_index": turn_index,
                    **_session_identity_payload(session_id),
                }
                run_persistence._append_process_sweep_event(
                    event_path=ctx.event_path,
                    persistence=ctx.persistence,
                    report=result.process_sweep,
                    phase="turn",
                    identity=turn_identity,
                )
                run_persistence._append_harness_degradation_event(
                    event_path=ctx.event_path,
                    persistence=ctx.persistence,
                    degradation=turn_payload.get("harness_degradation"),
                    identity=turn_identity,
                )
                ctx.persistence.write_state(ctx.payload)

            if abort_signal.armed and not result.aborted:
                # The worker ended the turn inside the grace window, so nothing
                # was killed and whatever it recorded stands: the pause or
                # completion path below owns the outcome from here.
                with run_control_lock:
                    run_termination._record_abort_satisfied_by_worker(
                        ctx.run_controls,
                        payload=ctx.payload,
                        event_path=ctx.event_path,
                        control_payload=ctx.control_payload,
                        context=turn_payload,
                        persistence=ctx.persistence,
                    )

            postflight = turn_postflight.enforce_post_turn_guards(
                ctx,
                before=before,
                after=after,
                result=result,
                turn_payload=turn_payload,
                workspace=workspace,
                session_scope=session_scope,
                workspace_snapshot_before=workspace_snapshot_before,
                legacy_snapshot_before=legacy_snapshot_before,
                review_fingerprint_before=review_fingerprint_before,
                external_digests_before=external_digests_before,
                turn_started_ns=turn_started_ns,
                plan_artifacts_before=plan_artifacts_before,
                plan_artifacts_written_by_runner=plan_artifacts_written_by_runner,
                is_review_session=is_review_session,
                cwd=cwd,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                review_guard_excludes=review_guard_excludes,
                review_guard_include_ignored=review_guard_include_ignored,
                paths=paths,
                state_dir_arg=args.state_dir,
                disarm_exception_guard=disarm_exception_guard,
            )
            if postflight.stopped:
                break
            terminating_disposition = postflight.terminating_disposition

            if terminating_disposition == "interrupted":
                turn_dispositions.pause_interrupted_run(
                    ctx,
                    before=before,
                    after=after,
                    session_details=session_details,
                )
                break

            if terminating_disposition == "timeout":
                turn_dispositions.pause_timed_out_turn(
                    ctx,
                    before=before,
                    after=after,
                    session_label=session_label,
                    session_details=session_details,
                )
                break

            if terminating_disposition == "aborted":
                turn_dispositions.record_aborted_turn(
                    ctx,
                    ctx.run_controls,
                    before=before,
                    after=after,
                    session_id=session_id,
                    turn_payload=turn_payload,
                )
                break
            if terminating_disposition == "malformed-plan":
                corrected = turn_dispositions.settle_malformed_plan(
                    ctx,
                    before=before,
                    workspace=workspace,
                    session_id=session_id,
                    turn_index=turn_index,
                    plan_error=plan_error,
                    turn_payload=turn_payload,
                    paths=paths,
                    session_details=session_details,
                )
                if corrected is not None:
                    snapshot = corrected
                    continue
                break

            # Every disposition that survives the branches above names a turn
            # the plan could be inspected after, so the malformed-plan branch
            # owns the only `after is None` turn that reaches here.
            assert after is not None
            after, paused_status_repair = (
                handoff_correction._repair_paused_handoff_status(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    plan_cache=ctx.plan_cache,
                    after=after,
                    session_id=session_id,
                    turn_index=turn_index,
                    runner_pause=turn_payload.get("runner_pause"),
                )
            )
            invalid_pause = run_inputs._invalid_status_pause(after)
            if invalid_pause is not None:
                reason, message, details = invalid_pause
                ctx.record_final_snapshot(after)
                _record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="plan-status-validity",
                    reason=reason,
                    detail=details,
                )
                ctx.pause(
                    reason=reason,
                    message=message,
                    details=details | session_details,
                )
                break

            selected_after = next(
                (item for item in after.sessions if item.id == session_id),
                None,
            )
            if (
                mailbox_enabled
                and selected_after is not None
                and selected_after.status == "complete"
                and handoff_guards._enforce_mailbox_completion_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    after=after,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_index=turn_index,
                    state_dir_arg=args.state_dir,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                )
            ):
                break

            if terminating_disposition == "runner-pause":
                continuation = turn_dispositions.settle_runner_pause(
                    ctx,
                    after=after,
                    session_id=session_id,
                    turn_index=turn_index,
                    result=result,
                    turn_payload=turn_payload,
                    session_details=session_details,
                    paused_status_repair=paused_status_repair,
                    mailbox_enabled=mailbox_enabled,
                    mailbox_poll=mailbox_poll,
                    plan_state=state_dir,
                    state_path=state_path,
                    state_dir_arg=args.state_dir,
                    # Resolved here rather than inside the boundary so this
                    # module keeps owning all three names: callers patch them
                    # on the runner to assert a wait was or was not entered.
                    prepare_wait=_prepare_mailbox_reply_wait,
                    wait_for_reply=_wait_for_mailbox_reply,
                    finish_lifecycle=_finish_mailbox_reply_wait_lifecycle,
                )
                if continuation is not None:
                    snapshot = continuation.snapshot
                    pending_reply = continuation.pending_reply
                    pending_resolution = continuation.pending_resolution
                    continue
                break
            if terminating_disposition == "nonzero-exit":
                retry = turn_dispositions.settle_nonzero_exit(
                    ctx,
                    before=before,
                    after=after,
                    result=result,
                    spark_route=spark_route,
                    session_id=session_id,
                    session_label=session_label,
                    turn_index=turn_index,
                    turn_payload=turn_payload,
                    session_details=session_details,
                    paths=paths,
                    state_dir_arg=args.state_dir,
                    spark_run_suppression=spark_run_suppression,
                )
                if retry is not None:
                    snapshot = retry.snapshot
                    spark_fallback_reason = retry.fallback_reason
                    spark_run_suppression = retry.run_suppression
                    continue
                break

            settlement = turn_settlement.settle_turn(
                ctx,
                args=args,
                cwd=cwd,
                before=before,
                after=after,
                snapshot=snapshot,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                turn_index=turn_index,
                total_sessions=total_sessions,
                turn_payload=turn_payload,
                paths=paths,
                plan_artifacts_before=plan_artifacts_before,
                plan_artifacts_written_by_runner=plan_artifacts_written_by_runner,
                is_review_session=is_review_session,
                is_repair_session=is_repair_session,
                review_session_version=review_session_version,
                allow_not_loop_ready=allow_not_loop_ready,
                commit_mode=commit_mode,
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=review_requested,
                turn_executor=turn_executor,
                live_turn_payload=live_turn_payload,
                run_controls=ctx.run_controls,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                soft_fail_commit=functools.partial(_soft_fail_commit, ctx),
            )
            snapshot = settlement.snapshot
            if settlement.action == turn_settlement.ACTION_STOP:
                break
            if settlement.action == turn_settlement.ACTION_RETRY:
                continue

            _drain_external_control_queue(
                plan_dir=snapshot.plan_dir,
                state_dir_arg=args.state_dir,
                control_state=ctx.run_controls.state,
                seen_ids=ctx.seen_external_command_ids,
                payload=ctx.payload,
                event_path=ctx.event_path,
                control_payload=ctx.control_payload,
                context=settlement.turn_record,
                persistence=ctx.persistence,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                guard_controller=ctx.guard_controller,
            )
            with run_control_lock:
                # An unpending request is left set on purpose: the all-complete
                # branch at the top of the loop owns it, so a stop is never
                # swallowed by a run that still has a review to append.
                stop_now = (
                    ctx.run_controls.state.stop_after_current
                    and snapshot.next_pending is not None
                )
                if stop_now:
                    run_termination._honor_stop_after_current(
                        ctx.run_controls,
                        snapshot=snapshot,
                        payload=ctx.payload,
                        event_path=ctx.event_path,
                        control_payload=ctx.control_payload,
                        context=settlement.turn_record,
                        persistence=ctx.persistence,
                    )
            if stop_now:
                break

    except BaseException as error:
        guard_context = pending_exception_guard
        pending_exception_guard = None
        if guard_context is not None:
            live_turn = guard_context["live_turn"]
            live_turn.stop()
            finished_at = _utc_now()
            live_turn_payload = live_turn.finish(finished_at)
            with run_control_lock:
                ctx.run_control_context = None

            after = guard_context.get("after")
            if after is None:
                try:
                    after = ctx.plan_cache.get_fresh(snapshot.plan_dir)
                except Exception:
                    after = None
            turn_payload = guard_context.get("turn_payload")
            if not isinstance(turn_payload, dict):
                turn_payload = {
                    **live_turn_payload,
                    "run_id": ctx.run_id,
                    "turn_index": live_turn_payload.get("turn_index"),
                    **_session_identity_payload(str(guard_context["session_id"])),
                    "status_result": "exception",
                    "exception": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                    "before": _snapshot_payload(guard_context["before"]),
                    "after": (_snapshot_payload(after) if after is not None else None),
                }
            guard_mode = guard_context.get("mode")
            # ``review`` matches neither branch on purpose: a legacy review turn
            # declares no write scope and captured no write-guard snapshot, so
            # there is no write guard to enforce for it. Its comparison is the
            # review fingerprint, which the protected-path step below reads.
            try:
                if guard_mode == "legacy":
                    write_guards._enforce_legacy_implementation_write_guard(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        cwd=guard_context["cwd"],
                        before=guard_context["before"],
                        after=after,
                        snapshot_before=guard_context["snapshot_before"],
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
                        session_id=str(guard_context["session_id"]),
                        session_label=str(guard_context["session_label"]),
                        session_details=guard_context["session_details"],
                        turn_payload=turn_payload,
                        paths=guard_context["paths"],
                        plan_cache=ctx.plan_cache,
                        append_handoff_note=handoff_correction._append_handoff_note,
                        reinspect=session_verification._reinspect_after_revert,
                        state_dir_arg=args.state_dir,
                        run_dir=ctx.run_dir,
                        external_digests_before=guard_context.get(
                            "external_digests_before"
                        ),
                        turn_started_ns=guard_context.get("turn_started_ns"),
                    )
                elif guard_mode == "workspace":
                    write_guards._enforce_workspace_write_guard(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        workspace=guard_context["workspace"],
                        scope=guard_context["scope"],
                        before=guard_context["before"],
                        after=after,
                        snapshot_before=guard_context["snapshot_before"],
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
                        session_id=str(guard_context["session_id"]),
                        session_label=str(guard_context["session_label"]),
                        session_details=guard_context["session_details"],
                        turn_payload=turn_payload,
                        paths=guard_context["paths"],
                        plan_cache=ctx.plan_cache,
                        append_handoff_note=handoff_correction._append_handoff_note,
                        reinspect=session_verification._reinspect_after_revert,
                        state_dir_arg=args.state_dir,
                        run_dir=ctx.run_dir,
                        external_digests_before=guard_context.get(
                            "external_digests_before"
                        ),
                        turn_started_ns=guard_context.get("turn_started_ns"),
                    )
            except Exception as guard_error:
                error.add_note(
                    "post-turn workspace write guard failed while handling "
                    f"the exception: {guard_error}"
                )
            # The run's guards are expired a few lines below, so this is the
            # last moment an operator's standing instruction can be answered
            # at all. It is answered in adjudicate-only mode because the
            # exception, not the guard, is what ended this turn: the verdict
            # is recorded and the failure it belongs to is re-raised intact.
            try:
                write_guards._enforce_protected_path_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    guard_controller=ctx.guard_controller,
                    before=guard_context["before"],
                    after=after,
                    session_id=str(guard_context["session_id"]),
                    session_label=str(guard_context["session_label"]),
                    session_details=guard_context["session_details"],
                    turn_payload=turn_payload,
                    paths=guard_context["paths"],
                    plan_cache=ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                    # A write guard that never recorded its comparison — it
                    # raised, or the inventory failed — leaves this guard
                    # blind, and blind is recorded as unavailable rather than
                    # read as clean. A legacy review turn has no write-guard
                    # comparison by construction, so it is answered from its
                    # own turn-start fingerprint instead.
                    changed_paths=(
                        write_guards._protected_path_review_changed_paths(
                            guard_context,
                            exclude=review_guard_excludes,
                            include_ignored=review_guard_include_ignored,
                        )
                        if guard_mode == "review"
                        else write_guards._protected_path_changed_paths(
                            turn_payload.get("workspace_write_guard")
                        )
                    ),
                    adjudicate_only=True,
                )
            except Exception as guard_error:
                error.add_note(
                    "post-turn protected-path guard failed while handling "
                    f"the exception: {guard_error}"
                )
        if isinstance(error, KeyboardInterrupt):
            if not ctx.payload.get("paused"):
                ctx.pause(
                    reason="interrupted",
                    message="run interrupted by user",
                    details=dict(ctx.last_session_details),
                )
        else:
            _expire_run_guards(ctx)
            ctx.run_controls.stop()
            ctx.inline_tail.stop()
            raise

    if review_requested:
        # An ordinary repair child can change pending/completed membership
        # without traversing a review-cycle branch. Re-inspect here so every
        # loop exit (including max-sessions, stop-after-current, and pauses)
        # publishes the plan's final continuation state.
        try:
            final_review_snapshot = ctx.plan_cache.get(snapshot.plan_dir)
        except plans.PlanError:
            # Preserve the run's existing malformed-plan pause and its last
            # safely computed projection when the final plan is unreadable.
            pass
        else:
            review_resume = review_cycle._set_review_resume(
                snapshot=final_review_snapshot,
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=True,
            )

    with ctx.persistence.lock:
        if ctx.payload["ok"] and ctx.payload["reason"] == "":
            ctx.payload["reason"] = "stopped"
        review_executed = ctx.payload.get("review_session_executed") is not None
        if (
            review_requested
            and review_executed
            and not ctx.payload.get("review_completed")
        ):
            review_cycle._attest_executed_review(
                ctx.payload,
                snapshot.plan_dir,
                require_artifact=executed_review_requires_artifact,
                read_review_verdict=handoff_guards._read_review_verdict,
            )
        if (
            review_requested
            and not review_executed
            and not ctx.payload.get("review_completed")
            and ctx.payload.get("review_skipped_reason") is None
        ):
            if ctx.payload.get("paused"):
                ctx.payload["review_skipped_reason"] = (
                    ctx.payload.get("pause_reason") or "paused"
                )
            elif ctx.payload["reason"] not in (
                "",
                "all-complete",
                REVIEW_FINDINGS_UNRESOLVED_REASON,
            ):
                ctx.payload["review_skipped_reason"] = ctx.payload["reason"]

    with ctx.persistence.lock:
        exit_code = 0 if ctx.payload["ok"] else 1
        if ctx.payload.get("pause_reason") == "interrupted":
            exit_code = INTERRUPTED_EXIT
    # The controls outlive the loop so a supervisor can still reach a run that
    # is finalizing: the terminal audit is a harness turn of its own, and an
    # abort arriving during it has to find a live listener and a live drain.
    finalization_abort = harnesses.AbortSignal()
    mailbox_poll.set_context(None)
    mailbox_poll.set_abort(finalization_abort, finalization=True)
    try:
        return _terminalize(
            ctx, exit_code, cancel=finalization_abort, poll=mailbox_poll
        )
    finally:
        mailbox_poll.set_abort(None)
        _expire_run_guards(ctx)
        ctx.run_controls.stop()
        ctx.inline_tail.stop()


def run_from_args(
    args: argparse.Namespace,
    *,
    run_argv: list[str] | None = None,
) -> int:
    """Run one or more pending sessions from CLI arguments.

    The package's public entry point, reached by ``cli.py`` through the string
    ``handler_ref="runner:run_from_args"`` and called by name from
    ``workbench.py``, ``maintenance.py`` and ``skills/asl/scripts/asl_autopilot.py``.
    The admission sequence and the resume overlay live in
    :mod:`agent_session_loop.run_entry`; this shim supplies the one collaborator
    that module may not import, the locked run below.
    """
    return run_entry.run_from_args(args, run_argv=run_argv, run_locked=_run_locked)


def doctor_from_args(args: argparse.Namespace) -> int:
    """Check local harness prerequisites from CLI arguments.

    Reached by ``cli.py`` through the string
    ``handler_ref="runner:doctor_from_args"``, so the name is a contract. The
    staged resolution and every refusal it prints live in
    :mod:`agent_session_loop.run_doctor`; this boundary keeps only the name.
    """
    return run_doctor.doctor_from_args(args)
