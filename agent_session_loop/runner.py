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
import json
import sys
import threading
import time
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
    external_write_evidence,
    guards,
    guidance_delivery,
    handoff_correction,
    handoff_guards,
    harness_failures,
    harnesses,
    mailbox,
    mailbox_rendering,
    model_availability,
    plans,
    preflight,
    qna,
    readiness_gate,
    repository_evidence,
    review,
    review_continuation,
    review_cycle,
    review_delta,
    review_guard,
    review_repair,
    review_session_body,
    review_session_contract,
    review_transitions,
    run_commit,
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
    task_packet,
    turn_execution,
    verification,
    verification_transitions,
    workspace_evidence,
    write_guards,
)
from .review_guard import RepairCommitPreflightError
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
                if not run_inputs._all_sessions_complete(snapshot):
                    ctx.record_final_snapshot(snapshot)
                    ctx.pause(
                        reason="no-pending-but-incomplete",
                        message="no pending session was found, but not every "
                        "session is complete",
                        details={"snapshot": _snapshot_payload(snapshot)},
                    )
                    break

                if (
                    commit_mode == run_selection.COMMIT_MODE_END
                    and not ctx.commit_at_end_attempted
                ):
                    ctx.commit_at_end_attempted = True
                    ctx.record_final_snapshot(snapshot)
                    try:
                        end_targets = commit_settlement._registered_end_commit_targets(
                            ctx.verification_context
                        )
                        commit_group, failed_commit = (
                            commit_settlement._run_commit_group(
                                event_path=ctx.event_path,
                                payload=ctx.payload,
                                progress=ctx.progress,
                                verification_context=ctx.verification_context,
                                run_dir=ctx.run_dir,
                                run_id=ctx.run_id,
                                session_id=None,
                                targets=end_targets,
                                generation=ctx.commit_generation,
                                persistence=ctx.persistence,
                            )
                        )
                    except RunError as error:
                        commit_group = None
                        failed_commit = None
                        commit_error = str(error)
                    else:
                        commit_error = None
                    if failed_commit is not None or commit_error is not None:
                        _soft_fail_commit(
                            ctx,
                            scope="end",
                            session_id=None,
                            group=commit_group,
                            commit=failed_commit,
                            error=commit_error,
                            message=(
                                "final auto-commit failed after all sessions completed"
                            ),
                        )
                    continue

                if review_requested and not ctx.review_appended:
                    # The review and its repair children are work that starts
                    # *after* the session the operator asked to stop after, so
                    # the request has to be visible before either begins.
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
                    with run_control_lock:
                        stop_before_review = ctx.run_controls.state.stop_after_current
                    review_resume = review_cycle._set_review_resume(
                        snapshot=snapshot,
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        max_repair_group_attempts=run_inputs._max_repair_group_attempts(
                            args
                        ),
                        max_review_fix_cycles=max_review_fix_cycles,
                        review_requested=True,
                    )

                    if review_resume.action == "terminal":
                        terminal_disposition = (
                            review_resume.blocker
                            in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
                        )
                        with ctx.persistence.lock:
                            review_cycle._set_existing_review_verdict(
                                ctx.payload,
                                snapshot.plan_dir,
                                read_review_verdict=handoff_guards._read_review_verdict,
                            )
                            ctx.payload["review_fix_disposition"] = (
                                review_resume.source_disposition
                                if terminal_disposition
                                else None
                            )
                            ctx.payload["final_snapshot"] = _snapshot_payload(snapshot)
                        if terminal_disposition:
                            with ctx.persistence.lock:
                                ctx.payload["reason"] = (
                                    review_cycle._terminal_review_reason(ctx.payload)
                                )
                        else:
                            blocker = review_resume.blocker or "unknown blocker"
                            ctx.pause(
                                reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                                message="Recorded review repair history cannot be "
                                f"resumed safely: {blocker}.",
                                details={"review_resume": review_resume.to_payload()},
                            )
                        break

                    if review_resume.action == "materialize-repairs":
                        if not review_resume.eligible:
                            with ctx.persistence.lock:
                                review_cycle._set_existing_review_verdict(
                                    ctx.payload,
                                    snapshot.plan_dir,
                                    read_review_verdict=handoff_guards._read_review_verdict,
                                )
                                ctx.payload["review_fix_disposition"] = (
                                    review_resume.source_disposition
                                )
                                ctx.payload["final_snapshot"] = _snapshot_payload(
                                    snapshot
                                )
                            if review_resume.blocker == "higher-cap-required":
                                with ctx.persistence.lock:
                                    ctx.payload["reason"] = (
                                        review_cycle._terminal_review_reason(
                                            ctx.payload
                                        )
                                    )
                                break

                            reopen_error = (
                                review_resume.artifact_error
                                or "review repair artifact is not materializable"
                            )
                            failed_record = review_cycle._review_reopen_record(
                                event_path=ctx.event_path,
                                payload=ctx.payload,
                                resume=review_resume,
                                max_review_fix_cycles=max_review_fix_cycles,
                                materialized=False,
                                result=(review_resume.blocker or "reopen-ineligible"),
                                error=reopen_error,
                            )
                            review_cycle._record_review_reopen(
                                event_path=ctx.event_path,
                                payload=ctx.payload,
                                persistence=ctx.persistence,
                                record=failed_record,
                            )
                            ctx.pause(
                                reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                                message="review repair reopening failed for "
                                f"session {review_resume.review_session_id}: "
                                f"{reopen_error}",
                                details={
                                    "review_resume": review_resume.to_payload(),
                                    "error": reopen_error,
                                },
                            )
                            break

                        if stop_before_review:
                            run_termination._stop_before_review_work(
                                ctx.run_controls,
                                snapshot=snapshot,
                                payload=ctx.payload,
                                event_path=ctx.event_path,
                                control_payload=ctx.control_payload,
                                record_existing_review_verdict=(
                                    review_cycle.existing_review_verdict_recorder(
                                        handoff_guards._read_review_verdict
                                    )
                                ),
                                persistence=ctx.persistence,
                            )
                            break

                        repair_result = (
                            review_repair.materialize_review_repair_sessions(
                                snapshot.plan_dir,
                                review_session_id=(review_resume.review_session_id),
                                cwd=cwd,
                                state_dir=args.state_dir,
                                hold_lock=False,
                                allow_not_loop_ready=allow_not_loop_ready,
                            )
                        )
                        repair_session_ids = tuple(
                            child.session_id.value for child in repair_result.children
                        )
                        materialized = bool(repair_result.ok and repair_session_ids)
                        reopen_record = review_cycle._review_reopen_record(
                            event_path=ctx.event_path,
                            payload=ctx.payload,
                            resume=review_resume,
                            max_review_fix_cycles=max_review_fix_cycles,
                            materialized=materialized,
                            repair_session_ids=repair_session_ids,
                            result=repair_result.reason,
                            error=repair_result.error,
                        )
                        review_cycle._record_review_reopen(
                            event_path=ctx.event_path,
                            payload=ctx.payload,
                            persistence=ctx.persistence,
                            record=reopen_record,
                        )

                        if (
                            not repair_session_ids
                            and repair_result.reason
                            == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY
                        ):
                            # No child survived materialization: the pre-authoring
                            # gate refused them, or the loop-ready gate withdrew
                            # them (ASI-136). Either way the plan is the one this
                            # cycle started from, so there is nothing to proceed
                            # into even with --allow-not-loop-ready.
                            ctx.record_final_snapshot(snapshot)
                            ctx.pause(
                                reason="not-loop-ready",
                                message="no review repair children survived "
                                "materialization for session "
                                f"{repair_result.review_session_id}: "
                                f"{repair_result.error}",
                                details={
                                    "review_resume": review_resume.to_payload(),
                                    "blockers": list(repair_result.blockers),
                                    "blocker": list(
                                        repair_result.non_bypassable_blockers
                                    ),
                                    "repair_children_rolled_back": (
                                        repair_result.rolled_back
                                    ),
                                    **(
                                        {"repair_rollback": repair_result.rollback}
                                        if repair_result.rollback is not None
                                        else {}
                                    ),
                                },
                            )
                            break

                        if not repair_result.ok or not repair_session_ids:
                            reopen_error = (
                                repair_result.error
                                or "review repair reopening produced no children"
                            )
                            ctx.record_final_snapshot(snapshot)
                            ctx.pause(
                                reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                                message="review repair reopening failed for "
                                f"session {review_resume.review_session_id}: "
                                f"{reopen_error}",
                                details={
                                    "review_resume": review_resume.to_payload(),
                                    "error": reopen_error,
                                    "detail": repair_result.detail,
                                },
                            )
                            break

                        try:
                            snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
                        except plans.PlanError as error:
                            ctx.pause(
                                reason="malformed-plan",
                                message="plan could not be inspected after "
                                f"reopening review repairs: {error}",
                                details={"error": str(error)},
                            )
                            break

                        review_resume = review_cycle._set_review_resume(
                            snapshot=snapshot,
                            event_path=ctx.event_path,
                            payload=ctx.payload,
                            persistence=ctx.persistence,
                            max_repair_group_attempts=(
                                run_inputs._max_repair_group_attempts(args)
                            ),
                            max_review_fix_cycles=max_review_fix_cycles,
                            review_requested=True,
                        )
                        with ctx.persistence.lock:
                            ctx.payload["review_fix_disposition"] = (
                                review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                            )
                            ctx.payload["final_snapshot"] = _snapshot_payload(snapshot)

                        if (
                            repair_result.reason
                            == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY
                            and not allow_not_loop_ready
                        ):
                            ctx.pause(
                                reason="not-loop-ready",
                                message="repair sessions were reopened from review "
                                f"session {repair_result.review_session_id} "
                                "but the plan could not be marked loop-ready",
                                details={
                                    "review_resume": review_resume.to_payload(),
                                    "blockers": list(repair_result.blockers),
                                    "repair_session_ids": list(repair_session_ids),
                                },
                            )
                            break

                        if not readiness_gate._regate_reopened_plan(
                            event_path=ctx.event_path,
                            payload=ctx.payload,
                            persistence=ctx.persistence,
                            args=args,
                            snapshot=snapshot,
                            repair_session_ids=repair_session_ids,
                            details={"review_resume": review_resume.to_payload()},
                        ):
                            break

                        running_success, running_error = (
                            readiness_gate.write_readiness_state(
                                snapshot.plan_dir, "running"
                            )
                        )
                        with ctx.persistence.lock:
                            if running_success:
                                ctx.payload["readiness_state_active"] = True
                                ctx.payload["readiness_state_current"] = "running"
                                ctx.payload["readiness_state_refreshed_at"] = (
                                    _isoformat(_utc_now())
                                )
                                ctx.payload["readiness_state_refresh_error"] = None
                            else:
                                ctx.payload["readiness_state_refresh_error"] = (
                                    running_error
                                )
                            ctx.persistence.write_state(ctx.payload)
                        # Execute the exact children just materialized. Once
                        # they complete, the next all-complete pass evaluates
                        # to ``rereview`` and creates a fresh review.
                        continue

                    if stop_before_review:
                        run_termination._stop_before_review_work(
                            ctx.run_controls,
                            snapshot=snapshot,
                            payload=ctx.payload,
                            event_path=ctx.event_path,
                            control_payload=ctx.control_payload,
                            record_existing_review_verdict=(
                                review_cycle.existing_review_verdict_recorder(
                                    handoff_guards._read_review_verdict
                                )
                            ),
                            persistence=ctx.persistence,
                        )
                        break

                    review_workspace = ctx.verification_context.workspace
                    review_bases: list[dict[str, Any]] = []
                    if review_workspace is None:
                        review_scope_delta, review_scope_fallback = (
                            review_delta._compute_review_delta_context(
                                event_path=ctx.event_path,
                                plan_dir=snapshot.plan_dir,
                                cwd=cwd,
                            )
                        )
                    else:
                        try:
                            review_base_snapshot = (
                                handoff_guards._capture_workspace_write_guard_snapshot(
                                    workspace=review_workspace,
                                    plan_dir=snapshot.plan_dir,
                                    exclude=review_guard_excludes,
                                    include_ignored=(review_guard_include_ignored),
                                )
                            )
                        except repository_evidence.RepositoryEvidenceError as error:
                            unavailable = {
                                ".": {
                                    "reason": "workspace-inventory-failed",
                                    "error": str(error),
                                }
                            }
                        else:
                            unavailable = (
                                workspace_evidence._workspace_snapshot_unavailable(
                                    review_base_snapshot,
                                    required_repository_paths=tuple(
                                        repository.repository.path
                                        for repository in review_workspace.repositories
                                    ),
                                )
                            )
                        if unavailable:
                            unavailable_summary = (
                                workspace_evidence._workspace_unavailable_summary(
                                    unavailable,
                                    workspace=review_workspace,
                                )
                            )
                            ctx.record_final_snapshot(snapshot)
                            ctx.pause(
                                reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                                message="combined workspace review base could not "
                                "be captured safely"
                                f"{unavailable_summary}",
                                details={
                                    "workspace_root": str(review_workspace.root),
                                    "unavailable": unavailable,
                                },
                            )
                            break
                        try:
                            review_bases = workspace_evidence._workspace_review_bases(
                                review_workspace, review_base_snapshot
                            )
                        except RunError as error:
                            ctx.record_final_snapshot(snapshot)
                            ctx.pause(
                                reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                                message=str(error),
                                details={"error": str(error)},
                            )
                            break
                        review_scope_delta, review_scope_fallback = (
                            review_delta._compute_registered_review_delta_context(
                                event_path=ctx.event_path,
                                plan_dir=snapshot.plan_dir,
                                workspace=review_workspace,
                            )
                        )
                    review_scope = (
                        review_scope_delta["mode"]
                        if review_scope_delta is not None
                        else review_session_body.REVIEW_SCOPE_FULL
                    )
                    review_base_sha = (
                        review_scope_delta.get("base_sha")
                        if review_scope_delta is not None
                        else None
                    )
                    try:
                        review_file, review_created = (
                            review_session_body.ensure_generated_review_session(
                                snapshot.plan_dir,
                                review_harness=args.review_harness,
                                delta=review_scope_delta,
                                workspace=review_workspace,
                                review_bases=review_bases,
                            )
                        )
                    except (plans.PlanError, OSError) as error:
                        ctx.record_final_snapshot(snapshot)
                        ctx.pause(
                            reason="malformed-plan",
                            message="could not create or reuse a generated review "
                            f"session: {error}",
                            details={"error": str(error)},
                        )
                        break
                    ctx.review_appended = True
                    with ctx.persistence.lock:
                        ctx.payload["review_session_id"] = review_file.session_id.value
                    if review_created:
                        ctx.persistence.append_event(
                            ctx.event_path,
                            events.REVIEW_SCOPE_EVENT,
                            {
                                "run_id": ctx.run_id,
                                "session_id": review_file.session_id.value,
                                "scope": review_scope,
                                "base_sha": review_base_sha,
                                **(
                                    {
                                        "repositories": review_scope_delta.get(
                                            "repositories"
                                        )
                                    }
                                    if review_scope_delta is not None
                                    and "repositories" in review_scope_delta
                                    else {}
                                ),
                                "fallback_reason": review_scope_fallback,
                                "refreshed": False,
                            },
                        )
                        ctx.persistence.append_event(
                            ctx.event_path,
                            events.REVIEW_SESSION_CREATED_EVENT,
                            {
                                "run_id": ctx.run_id,
                                "session_id": review_file.session_id.value,
                                "session": review_file.session_id.value,
                                "review_harness": args.review_harness,
                                "review_session_path": str(review_file.path),
                                "review_output_path": (
                                    ctx.payload.get("review_output_path")
                                    or str(
                                        review_session_contract.final_review_output_path(
                                            snapshot.plan_dir
                                        )
                                    )
                                ),
                                "review_scope": review_scope,
                                "review_base_sha": review_base_sha,
                                **(
                                    {"repositories": review_bases}
                                    if review_workspace is not None
                                    else {}
                                ),
                            },
                        )
                    try:
                        snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
                    except plans.PlanError as error:
                        ctx.pause(
                            reason="malformed-plan",
                            message="plan could not be inspected after appending the "
                            f"review session: {error}",
                            details={"error": str(error)},
                        )
                        break
                    ctx.record_final_snapshot(snapshot)
                    # Appending the review session grows the plan exactly the way
                    # materializing repair children does, and reaches here under
                    # the same reused readiness report -- the one that said every
                    # session was complete and no turns were required. Re-gate the
                    # grown plan before its turn launches, so the authority the
                    # review runs under describes the plan that now exists rather
                    # than the one the startup gate saw. A reused pending review
                    # session was already authorized when it was created, so only
                    # a fresh append is re-gated.
                    if review_created and not readiness_gate._regate_reopened_plan(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        args=args,
                        snapshot=snapshot,
                        repair_session_ids=[],
                        details={
                            "review_session_id": review_file.session_id.value,
                            "review_session_path": str(review_file.path),
                        },
                        trigger=events.REVIEW_SESSION_CREATED_EVENT,
                        growth=(
                            "a generated review session "
                            f"({review_file.session_id.value}) was appended"
                        ),
                    ):
                        break
                    running_success, running_error = (
                        readiness_gate.write_readiness_state(
                            snapshot.plan_dir, "running"
                        )
                    )
                    with ctx.persistence.lock:
                        if running_success:
                            ctx.payload["readiness_state_active"] = True
                            ctx.payload["readiness_state_current"] = "running"
                            ctx.payload["readiness_state_refreshed_at"] = _isoformat(
                                _utc_now()
                            )
                            ctx.payload["readiness_state_refresh_error"] = None
                        else:
                            ctx.payload["readiness_state_refresh_error"] = running_error
                        ctx.persistence.write_state(ctx.payload)
                    continue

                with ctx.persistence.lock:
                    ctx.payload["final_snapshot"] = _snapshot_payload(snapshot)
                    ctx.payload["reason"] = "all-complete"
                break

            is_review_session = (
                review_session_contract.is_generated_review_session_file(
                    pending_session.path
                )
            )
            is_repair_session = review_repair.is_generated_review_repair_session_file(
                pending_session.path
            )
            session_kind = (
                SESSION_KIND_REVIEW
                if is_review_session
                else SESSION_KIND_IMPLEMENTATION
            )
            prepared_review_workspace_snapshot: (
                repository_evidence.WorkspaceSnapshot | None
            ) = None
            if is_review_session:
                try:
                    review_workspace = ctx.verification_context.workspace
                    review_text = pending_session.path.read_text(encoding="utf-8")
                    refreshed_text = (
                        review_session_contract.refreshed_generated_review_contract(
                            review_text,
                            pending_session.session_id,
                            plan_dir=snapshot.plan_dir,
                            repository_aliases=(
                                review_workspace.registry.aliases
                                if review_workspace is not None
                                else ()
                            ),
                        )
                    )
                    contract_refreshed = refreshed_text != review_text
                    if contract_refreshed:
                        pending_session.path.write_text(
                            refreshed_text, encoding="utf-8"
                        )
                    rescoped = review_delta._refresh_pending_review_scope(
                        session=pending_session,
                        event_path=ctx.event_path,
                        plan_dir=snapshot.plan_dir,
                        cwd=cwd,
                        workspace=ctx.verification_context.workspace,
                        run_id=ctx.run_id,
                        persistence=ctx.persistence,
                    )
                    base_refreshed = False
                    if review_workspace is not None:
                        try:
                            prepared_review_workspace_snapshot = (
                                handoff_guards._capture_workspace_write_guard_snapshot(
                                    workspace=review_workspace,
                                    plan_dir=snapshot.plan_dir,
                                    exclude=review_guard_excludes,
                                    include_ignored=(review_guard_include_ignored),
                                )
                            )
                        except repository_evidence.RepositoryEvidenceError as error:
                            raise plans.PlanError(
                                "combined workspace review base capture "
                                f"failed: {error}"
                            ) from error
                        unavailable = (
                            workspace_evidence._workspace_snapshot_unavailable(
                                prepared_review_workspace_snapshot,
                                required_repository_paths=tuple(
                                    repository.repository.path
                                    for repository in review_workspace.repositories
                                ),
                            )
                        )
                        if unavailable:
                            raise plans.PlanError(
                                "combined workspace review base is unavailable: "
                                + json.dumps(unavailable, sort_keys=True)
                            )
                        review_bases = workspace_evidence._workspace_review_bases(
                            review_workspace,
                            prepared_review_workspace_snapshot,
                        )
                        review_text = pending_session.path.read_text(encoding="utf-8")
                        refreshed_text = (
                            review_session_body.refreshed_review_repository_base(
                                review_text, review_workspace, review_bases
                            )
                        )
                        if refreshed_text != review_text:
                            pending_session.path.write_text(
                                refreshed_text, encoding="utf-8"
                            )
                            base_refreshed = True
                    if contract_refreshed or rescoped or base_refreshed:
                        readiness_gate._refresh_active_readiness_state(
                            ctx.payload, snapshot.plan_dir, ctx.persistence
                        )
                        snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
                except (plans.PlanError, OSError) as error:
                    ctx.record_final_snapshot(snapshot)
                    ctx.pause(
                        reason="malformed-plan",
                        message="the pending review session's scope could not be "
                        f"re-derived from current Git evidence: {error}",
                        details={
                            **_session_details_payload(pending_session.id),
                            "error": str(error),
                        },
                    )
                    break
            review_session_version = (
                review_session_contract.generated_review_session_version(
                    pending_session.path
                )
                if is_review_session
                else None
            )

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
            # Autonomous turns default to Python-built task packets (packet
            # mode); ASL_WORKER_MODE=skill restores the legacy `session-run`
            # skill invocation as a temporary rollback. Either way the manual
            # `$session-run` path is unchanged.
            worker_mode = task_packet._worker_mode()
            predecessor_id = task_packet._predecessor_session_id(before, session_id)
            has_expansion = task_packet._session_has_expansion(
                before.plan_dir, session_id
            )
            has_progress = (before.plan_dir / plans.PROGRESS_FILE_NAME).is_file()
            repair_references = review_delta._verification_repair_state(
                ctx.event_path, session_id
            )["references"]
            if is_review_session:
                repair_references = repair_references + (
                    _review_verdict_repair_references(ctx.event_path, session_id)
                )
            pending_guidance = run_selection._pending_guidance_snapshot(
                enabled=mailbox_enabled,
                plan_dir=before.plan_dir,
                state_dir_arg=args.state_dir,
            )
            workspace = ctx.verification_context.workspace
            session_scope = (
                run_prompts._session_scope(pending_session, workspace)
                if workspace is not None
                else None
            )
            retained_scope_repair = (
                workspace_evidence._latest_workspace_scope_repair_requirement(
                    ctx.event_path, session_id
                )
                if workspace is not None
                else None
            )
            if (
                retained_scope_repair is not None
                and retained_scope_repair.get("session_id") != session_id
            ):
                # The lookup also matches on the pause envelope's session, so
                # the guard record's own identity is the narrower proof that
                # this is the turn whose changes were retained for this child.
                retained_scope_repair = None
            regenerated_write_scope: dict[str, Any] | None = None
            if is_repair_session:
                if workspace is not None:
                    assert session_scope is not None
                    # The durable child file is the single description of what
                    # this child may write. Widening it here — ahead of commit
                    # preflight, the task packet, the persisted prompt and the
                    # guard — is what keeps all four reading one scope instead
                    # of preflight and the packet describing the pre-repair one.
                    regenerated_write_scope = (
                        workspace_evidence._regenerated_repair_child_write_scope(
                            session=pending_session,
                            plan_dir=before.plan_dir,
                            workspace=workspace,
                            scope=session_scope,
                        )
                    )
                    if regenerated_write_scope is not None:
                        readiness_gate._refresh_active_readiness_state(
                            ctx.payload, before.plan_dir, ctx.persistence
                        )
                        session_scope = run_prompts._session_scope(
                            pending_session, workspace
                        )
                repair_resume = write_guards._pending_repair_child_continuation(
                    snapshot=before,
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    workspace=workspace,
                    max_repair_group_attempts=run_inputs._max_repair_group_attempts(
                        args
                    ),
                    max_review_fix_cycles=max_review_fix_cycles,
                    review_requested=review_requested,
                )
                if repair_resume.action == "run-repairs" and not repair_resume.eligible:
                    # The continuation is the only view that reconciles every
                    # pending child with the artifact they were derived from. A
                    # child it refuses is not launched under the scope that
                    # refusal describes.
                    repair_blocker = repair_resume.blocker or "unknown blocker"
                    ctx.record_final_snapshot(before)
                    ctx.pause(
                        reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                        message="generated repair child "
                        f"{session_label} cannot be run safely: "
                        f"{repair_blocker}.",
                        details={
                            **session_details,
                            "review_resume": repair_resume.to_payload(),
                        },
                    )
                    break
                try:
                    repair_commit_preflight = review_guard._repair_commit_preflight(
                        cwd,
                        verification_context=ctx.verification_context,
                        session_scope=session_scope,
                        workspace_evidence=(
                            workspace_evidence._workspace_write_guard_evidence
                        ),
                        plan_dir=before.plan_dir,
                        retained_scope_repair=retained_scope_repair,
                    )
                except RepairCommitPreflightError as error:
                    ctx.record_final_snapshot(before)
                    ctx.pause(
                        reason="commit-preflight",
                        message=f"repair commit preflight failed before "
                        f"{session_label}: {error}",
                        details={**session_details, **error.details},
                    )
                    break
                with ctx.persistence.lock:
                    ctx.payload.setdefault("repair_commit_preflights", []).append(
                        {
                            **session_identity,
                            **repair_commit_preflight,
                        }
                    )
                    ctx.persistence.append_event(
                        ctx.event_path,
                        events.REPAIR_COMMIT_PREFLIGHT_EVENT,
                        {
                            "run_id": ctx.run_id,
                            **session_identity,
                            **repair_commit_preflight,
                        },
                    )
                    ctx.persistence.write_state(ctx.payload)
            turn_reply = pending_reply
            turn_resolution = pending_resolution
            if turn_reply is not None and turn_resolution is not None:
                raise RuntimeError(
                    "reply and resolution continuations are mutually exclusive"
                )
            use_reply_prompt = turn_reply is not None
            use_resolution_prompt = turn_resolution is not None
            if use_reply_prompt:
                assert turn_reply is not None
                turn_reply_context = turn_reply.context
                turn_reply_text = turn_reply.text
                if worker_mode == task_packet.WORKER_MODE_SKILL:
                    prompt = run_prompts.build_reply_prompt(
                        turn_harness,
                        before.plan_dir,
                        reply=turn_reply_text,
                        context=turn_reply_context,
                        mailbox_enabled=mailbox_enabled,
                        state_dir=args.state_dir,
                        pending_guidance=pending_guidance,
                        workspace=workspace,
                        session_scope=session_scope,
                    )
                else:
                    prompt = task_packet.build_task_packet(
                        turn_harness,
                        before.plan_dir,
                        session_id=session_id,
                        session_kind=session_kind,
                        predecessor_id=predecessor_id,
                        has_expansion=has_expansion,
                        has_progress=has_progress,
                        reply=turn_reply_text,
                        reply_context=turn_reply_context,
                        repair_references=repair_references,
                        mailbox_enabled=mailbox_enabled,
                        state_dir=args.state_dir,
                        pending_guidance=pending_guidance,
                        workspace=workspace,
                        session_scope=session_scope,
                    ).render()
                prompt_kind = "reply"
                reply_source_meta: dict[str, Any] | None = {
                    "run_id": turn_reply_context.run_id,
                    "session_id": turn_reply_context.session_id,
                    "turn_index": turn_reply_context.turn_index,
                    "last_message_path": str(turn_reply_context.last_message_path),
                    "reply_length": len(turn_reply_text),
                    "origin": turn_reply.origin,
                }
                if turn_reply.question_id is not None:
                    reply_source_meta["question_id"] = turn_reply.question_id
                if turn_reply.reply_message_id is not None:
                    reply_source_meta["reply_message_id"] = turn_reply.reply_message_id
                if turn_reply.reply_seq is not None:
                    reply_source_meta["reply_seq"] = turn_reply.reply_seq
                persisted_prompt = _reply_argv_placeholder(len(turn_reply_text))
                resolution_source_meta: dict[str, Any] | None = None
            elif use_resolution_prompt:
                assert turn_resolution is not None
                turn_resolution_context = turn_resolution.context
                if worker_mode == task_packet.WORKER_MODE_SKILL:
                    prompt = run_prompts.build_resolution_prompt(
                        turn_harness,
                        before.plan_dir,
                        action=turn_resolution.action,
                        note=turn_resolution.note,
                        context=turn_resolution_context,
                        mailbox_enabled=mailbox_enabled,
                        state_dir=args.state_dir,
                        pending_guidance=pending_guidance,
                        workspace=workspace,
                        session_scope=session_scope,
                    )
                else:
                    prompt = task_packet.build_task_packet(
                        turn_harness,
                        before.plan_dir,
                        session_id=session_id,
                        session_kind=session_kind,
                        predecessor_id=predecessor_id,
                        has_expansion=has_expansion,
                        has_progress=has_progress,
                        resolution_action=turn_resolution.action,
                        resolution_note=turn_resolution.note,
                        resolution_context=turn_resolution_context,
                        repair_references=repair_references,
                        mailbox_enabled=mailbox_enabled,
                        state_dir=args.state_dir,
                        pending_guidance=pending_guidance,
                        workspace=workspace,
                        session_scope=session_scope,
                    ).render()
                prompt_kind = "resolution"
                reply_source_meta = None
                resolution_source_meta = task_packet._resolution_source_metadata(
                    turn_resolution
                )
                persisted_prompt = _resolution_argv_placeholder(
                    turn_resolution.action,
                    resolution_source_meta["note_length"],
                )
            else:
                if worker_mode == task_packet.WORKER_MODE_SKILL:
                    prompt = run_prompts.build_session_prompt(
                        turn_harness,
                        before.plan_dir,
                        mailbox_enabled=mailbox_enabled,
                        state_dir=args.state_dir,
                        pending_guidance=pending_guidance,
                        workspace=workspace,
                        session_scope=session_scope,
                    )
                else:
                    prompt = task_packet.build_task_packet(
                        turn_harness,
                        before.plan_dir,
                        session_id=session_id,
                        session_kind=session_kind,
                        predecessor_id=predecessor_id,
                        has_expansion=has_expansion,
                        has_progress=has_progress,
                        repair_references=repair_references,
                        mailbox_enabled=mailbox_enabled,
                        state_dir=args.state_dir,
                        pending_guidance=pending_guidance,
                        workspace=workspace,
                        session_scope=session_scope,
                    ).render()
                prompt_kind = "session"
                reply_source_meta = None
                resolution_source_meta = None
                persisted_prompt = prompt
            packet_intent_report = (
                None
                if workspace is not None
                else run_inputs._task_packet_repository_intent_report(
                    prompt,
                    root=cwd,
                    artifact_paths=run_inputs._task_packet_artifact_paths(
                        before.plan_dir, session_id, predecessor_id
                    ),
                    exempt_roots=run_inputs._runner_owned_roots(
                        before.plan_dir, args.state_dir, run_dir=ctx.run_dir
                    ),
                    provenance_roots=preflight.plan_provenance_exempt_roots(
                        before.plan_dir
                    ),
                )
            )
            if packet_intent_report is not None:
                ctx.record_final_snapshot(before)
                ctx.pause(
                    reason=events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
                    message=run_inputs._repository_intent_message(
                        packet_intent_report,
                        subject=f"task packet for {session_label}",
                    ),
                    details={
                        **session_details,
                        "repository_intent": packet_intent_report,
                    },
                )
                break
            turn_started_at = _utc_now()
            turn_started_at_text = _isoformat(turn_started_at)
            idle_seconds_since_previous_turn = (
                0.0
                if ctx.runner_work_finished_at is None
                else _duration_seconds(ctx.runner_work_finished_at, turn_started_at)
            )
            total_sessions = sum(_status_counts(before).values())
            active_turn = {
                "run_id": ctx.run_id,
                "turn_index": turn_index,
                **session_identity,
                "session_kind": session_kind,
                **(
                    {"repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD}
                    if is_repair_session
                    else {}
                ),
                "total_sessions": total_sessions,
                "artifact_dir": str(paths["artifact_dir"]),
                "prompt": persisted_prompt,
                "harness": turn_harness,
                "model": turn_model_effort["model"],
                "model_source": turn_model_effort["model_source"],
                "observed_model": None,
                "observed_model_source": None,
                "effort": turn_model_effort["effort"],
                "effort_source": turn_model_effort["effort_source"],
                "spark_route": spark_route,
                "stdout_path": str(paths["stdout"]),
                "stderr_path": str(paths["stderr"]),
                "last_message_path": str(paths["last_message"]),
                "result_path": str(paths["result"]),
                "started_at": turn_started_at_text,
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": idle_seconds_since_previous_turn,
                "last_activity_at": turn_started_at_text,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
                "prompt_kind": prompt_kind,
                "reply_source": reply_source_meta,
                **(
                    {
                        "cwd": str(cwd),
                        **run_prompts._workspace_payload_fields(
                            ctx.verification_context
                        ),
                    }
                    if ctx.verification_context.workspace is not None
                    else {}
                ),
            }
            if resolution_source_meta is not None:
                active_turn["resolution_source"] = resolution_source_meta
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
            # Captured before the reply is consumed and before `turn_started`:
            # an unusable guard has to stop the run without spending the turn.
            workspace_snapshot_before: repository_evidence.WorkspaceSnapshot | None = (
                None
            )
            legacy_snapshot_before: repository_evidence.WorkspaceSnapshot | None = None
            # The control plane is digested for both modes and unconditionally:
            # the repository snapshot is silent about it by construction, so a
            # permitted mutation there has no other evidence.
            turn_started_ns = time.time_ns()
            external_digests_before = (
                external_write_evidence._capture_external_write_digests(
                    external_write_evidence._control_plane_digest_roots(
                        before.plan_dir, args.state_dir, run_dir=ctx.run_dir
                    ),
                    shallow_roots=(
                        external_write_evidence._control_plane_shallow_digest_roots(
                            args.state_dir
                        )
                    ),
                )
            )
            if workspace is not None:
                assert session_scope is not None
                if is_review_session and prepared_review_workspace_snapshot is not None:
                    workspace_snapshot_before = prepared_review_workspace_snapshot
                    unavailable = workspace_evidence._workspace_snapshot_unavailable(
                        workspace_snapshot_before,
                        required_repository_paths=tuple(
                            repository.repository.path
                            for repository in workspace.repositories
                        ),
                    )
                else:
                    try:
                        workspace_snapshot_before = (
                            handoff_guards._capture_workspace_write_guard_snapshot(
                                workspace=workspace,
                                plan_dir=before.plan_dir,
                                exclude=review_guard_excludes,
                                include_ignored=review_guard_include_ignored,
                            )
                        )
                    except repository_evidence.RepositoryEvidenceError as error:
                        unavailable = {
                            ".": {
                                "reason": "workspace-inventory-failed",
                                "error": str(error),
                            }
                        }
                    else:
                        unavailable = (
                            workspace_evidence._workspace_snapshot_unavailable(
                                workspace_snapshot_before,
                                required_repository_paths=tuple(
                                    repository.repository.path
                                    for repository in workspace.repositories
                                ),
                            )
                        )
                if unavailable:
                    unavailable_summary = (
                        workspace_evidence._workspace_unavailable_summary(
                            unavailable,
                            workspace=workspace,
                        )
                    )
                    guard_record = {
                        "passed": False,
                        "reason": (
                            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
                        ),
                        "phase": "before",
                        "unavailable": unavailable,
                        "read_aliases": list(session_scope.reads),
                        "write_aliases": list(session_scope.writes),
                        "excluded": list(review_guard_excludes),
                        "include_ignored": review_guard_include_ignored,
                    }
                    ctx.record_final_snapshot(before)
                    ctx.pause(
                        reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                        message="workspace write guard unavailable before "
                        f"{session_label}; evidence for a registered "
                        "repository could not be captured safely"
                        f"{unavailable_summary}",
                        details={
                            **session_details,
                            "workspace_write_guard": guard_record,
                        },
                    )
                    break
                if retained_scope_repair is not None:
                    # The durable file was already widened before preflight and
                    # the task packet were built, so the guard below judges the
                    # retained changes against exactly the scope preflight,
                    # expansion, the contract and the prompt already read.
                    prior_snapshot = retained_scope_repair.get("snapshot_before")
                    if isinstance(prior_snapshot, Mapping):
                        repair_comparison = (
                            repository_evidence.compare_workspace_snapshots(
                                prior_snapshot, workspace_snapshot_before
                            )
                        )
                        (
                            repair_comparison,
                            repair_registry_delta,
                        ) = workspace_evidence._workspace_scope_repair_comparison(
                            repair_comparison,
                            frozenset(
                                repository.repository.path
                                for repository in workspace.repositories
                            ),
                        )
                        repair_evidence = (
                            workspace_evidence._workspace_write_guard_evidence(
                                workspace=workspace,
                                scope=session_scope,
                                comparison=repair_comparison,
                                exclude=review_guard_excludes,
                                include_ignored=review_guard_include_ignored,
                            )
                        )
                        repair_evidence["repair_registry_delta"] = repair_registry_delta
                    else:
                        repair_evidence = {
                            "passed": False,
                            "unavailable": {".": {"reason": "repair-baseline-missing"}},
                            "violations": [],
                        }
                    if regenerated_write_scope is not None:
                        repair_evidence["write_scope_regeneration"] = (
                            regenerated_write_scope
                        )
                    current_expansion = workspace_evidence._session_expansion_evidence(
                        before.plan_dir, session_id
                    )
                    prior_expansion = retained_scope_repair.get(
                        "expansion_before_repair"
                    )
                    expansion_fresh = isinstance(
                        prior_expansion, Mapping
                    ) and workspace_evidence._workspace_scope_repair_is_fresh(
                        prior_expansion, current_expansion
                    )
                    if not repair_evidence["passed"] or not expansion_fresh:
                        repair_record = {
                            **retained_scope_repair,
                            "phase": "repair-check",
                            "current_scope_evidence": repair_evidence,
                            "current_expansion": current_expansion,
                            "expansion_fresh": expansion_fresh,
                        }
                        ctx.record_final_snapshot(before)
                        ctx.pause(
                            reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                            message=f"{session_label} still has retained changes "
                            "outside its durable write scope or lacks a "
                            "fresh expansion; repair scope and rerun "
                            "expansion/preflight before continuing",
                            details={
                                **session_details,
                                "workspace_write_guard": repair_record,
                            },
                        )
                        break
                    ctx.persistence.append_event(
                        ctx.event_path,
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": ctx.run_id,
                            "details": {
                                **session_details,
                                "session_id": session_id,
                                "workspace_write_guard": repair_evidence,
                                "expansion": current_expansion,
                            },
                        },
                    )
            elif not is_review_session:
                legacy_snapshot_before = (
                    handoff_guards._capture_legacy_implementation_snapshot(
                        cwd=cwd,
                        plan_dir=before.plan_dir,
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
                    )
                )
                unavailable = workspace_evidence._workspace_snapshot_unavailable(
                    legacy_snapshot_before,
                    required_repository_paths=(".",),
                )
                if unavailable:
                    guard_record = {
                        "passed": False,
                        "scope_mode": "legacy-single-repository",
                        "reason": (
                            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
                        ),
                        "phase": "before",
                        "repository_root": str(cwd),
                        "unavailable": unavailable,
                        "read_aliases": ["legacy"],
                        "write_aliases": ["legacy"],
                        "excluded": list(review_guard_excludes),
                        "include_ignored": review_guard_include_ignored,
                    }
                    ctx.record_final_snapshot(before)
                    ctx.pause(
                        reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                        message="implementation write guard unavailable before "
                        f"{session_label}; the legacy Git repository scope "
                        "could not be established safely",
                        details={
                            **session_details,
                            "workspace_write_guard": guard_record,
                        },
                    )
                    break
            # Captured before the exception-guard context below, because a
            # legacy review turn has no write-guard snapshot and this
            # fingerprint is the only turn-start comparison it will ever hold.
            review_fingerprint_before: dict[str, Any] | None = (
                review_guard._capture_review_repo_fingerprint(
                    cwd,
                    before.plan_dir,
                    exclude=review_guard_excludes,
                    include_ignored=review_guard_include_ignored,
                )
                if is_review_session and workspace is None
                else None
            )
            if review_fingerprint_before is not None:
                unavailable = review_guard._review_guard_unavailable_detail(
                    review_fingerprint_before
                )
                if unavailable is not None:
                    ctx.record_final_snapshot(before)
                    ctx.pause(
                        reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                        message="review mutation guard unavailable before "
                        f"{session_label}: {unavailable['reason']}",
                        details={
                            **session_details,
                            "review_mutation_guard": {
                                "passed": False,
                                "reason": events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                                "phase": "before",
                                "unavailable": unavailable,
                                "excluded": list(review_guard_excludes),
                                "include_ignored": review_guard_include_ignored,
                            },
                        },
                    )
                    break
            if workspace is not None:
                assert session_scope is not None
                assert workspace_snapshot_before is not None
                pending_exception_guard = {
                    "mode": "workspace",
                    "workspace": workspace,
                    "scope": session_scope,
                    "before": before,
                    "after": None,
                    "snapshot_before": workspace_snapshot_before,
                    "session_id": session_id,
                    "session_label": session_label,
                    "session_details": session_details,
                    "turn_payload": None,
                    "active_turn": active_turn,
                    "live_turn": live_turn,
                    "paths": paths,
                    "external_digests_before": external_digests_before,
                    "turn_started_ns": turn_started_ns,
                }
            elif legacy_snapshot_before is not None:
                pending_exception_guard = {
                    "mode": "legacy",
                    "cwd": cwd,
                    "before": before,
                    "after": None,
                    "snapshot_before": legacy_snapshot_before,
                    "session_id": session_id,
                    "session_label": session_label,
                    "session_details": session_details,
                    "turn_payload": None,
                    "active_turn": active_turn,
                    "live_turn": live_turn,
                    "paths": paths,
                    "external_digests_before": external_digests_before,
                    "turn_started_ns": turn_started_ns,
                }
            elif review_fingerprint_before is not None:
                # A legacy review turn captures no write-guard snapshot, so
                # without this branch it enters the harness with no exception
                # context at all and the outer handler skips protected-path
                # adjudication entirely -- in the one moment the run's guards
                # are still alive. The fingerprint is its turn-start
                # comparison, so the crash path can still reach a verdict.
                pending_exception_guard = {
                    "mode": "review",
                    "cwd": cwd,
                    "before": before,
                    "after": None,
                    "review_fingerprint_before": review_fingerprint_before,
                    "session_id": session_id,
                    "session_label": session_label,
                    "session_details": session_details,
                    "turn_payload": None,
                    "active_turn": active_turn,
                    "live_turn": live_turn,
                    "paths": paths,
                    "external_digests_before": external_digests_before,
                    "turn_started_ns": turn_started_ns,
                }
            # Whether this turn has something a protected-path guard can be
            # compared against. The review fingerprint counts: a legacy review
            # turn captures no write-guard snapshot, but its mutation guard
            # proves the whole worktree is unchanged, which clears every path
            # rule at once rather than leaving them unproven.
            protected_path_baseline = (
                workspace_snapshot_before is not None
                or legacy_snapshot_before is not None
                or review_fingerprint_before is not None
            )
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
                            pending_guidance=pending_guidance,
                            harness=turn_harness,
                            run_id=ctx.run_id,
                            session_id=session_id,
                            turn_index=turn_index,
                        )
                        if (
                            pending_guidance
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

            # Ahead of every result branch: a retryable pause (timeout,
            # transport failure) would otherwise relaunch against the turn's
            # own mutation and adopt it as the new baseline.
            #
            # A finalization guard that pauses no longer leaves immediately:
            # it records that it is ending the turn and falls through to the
            # protected-path step below, because the operator's standing
            # instruction is answered against the same comparison the write
            # guard already captured, and a turn that breached both has to
            # report both. The break moves to after that step.
            finalization_paused = False
            if workspace is not None:
                assert session_scope is not None
                assert workspace_snapshot_before is not None
                pending_exception_guard = None
                if write_guards._enforce_workspace_write_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    workspace=workspace,
                    scope=session_scope,
                    before=before,
                    after=after,
                    snapshot_before=workspace_snapshot_before,
                    exclude=review_guard_excludes,
                    include_ignored=review_guard_include_ignored,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                    state_dir_arg=args.state_dir,
                    run_dir=ctx.run_dir,
                    external_digests_before=external_digests_before,
                    turn_started_ns=turn_started_ns,
                ):
                    finalization_paused = True
                elif is_review_session:
                    try:
                        review_bases = workspace_evidence._workspace_review_bases(
                            workspace, workspace_snapshot_before
                        )
                    except RunError as error:
                        with ctx.persistence.lock:
                            ctx.payload["final_snapshot"] = (
                                _snapshot_payload(after)
                                if after is not None
                                else _snapshot_payload(before)
                            )
                        ctx.pause(
                            reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                            message=str(error),
                            details={**session_details, "error": str(error)},
                        )
                        finalization_paused = True
                    else:
                        with ctx.persistence.lock:
                            turn_payload["review_repository_bases"] = review_bases
                            _write_json_file(paths["result"], turn_payload)
            elif legacy_snapshot_before is not None:
                pending_exception_guard = None
                if write_guards._enforce_legacy_implementation_write_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    cwd=cwd,
                    before=before,
                    after=after,
                    snapshot_before=legacy_snapshot_before,
                    exclude=review_guard_excludes,
                    include_ignored=review_guard_include_ignored,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                    state_dir_arg=args.state_dir,
                    run_dir=ctx.run_dir,
                    external_digests_before=external_digests_before,
                    turn_started_ns=turn_started_ns,
                ):
                    finalization_paused = True
            elif review_fingerprint_before is not None:
                # The turn reached its own finalization guards, which adjudicate
                # the protected paths in line below. Disarming the crash-path
                # copy here mirrors the two branches above and keeps one turn
                # from recording the same verdict twice.
                pending_exception_guard = None
            if (
                not finalization_paused
                and is_review_session
                and review_fingerprint_before is not None
                and handoff_guards._enforce_review_mutation_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    cwd=cwd,
                    before=before,
                    after=after,
                    fingerprint_before=review_fingerprint_before,
                    exclude=review_guard_excludes,
                    include_ignored=review_guard_include_ignored,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                )
            ):
                finalization_paused = True

            # After the general write guard and before every result branch, for
            # the same reason the write guard sits there: a retryable pause
            # would otherwise relaunch against a workspace in which the
            # protected path has already moved, and adopt that as the baseline.
            # It is a distinct step because it answers a distinct question —
            # not "did the session stay in scope" but "did the run honour the
            # standing instruction an operator gave it".
            if write_guards._enforce_protected_path_guard(
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                guard_controller=ctx.guard_controller,
                before=before,
                after=after,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                turn_payload=turn_payload,
                paths=paths,
                plan_cache=ctx.plan_cache,
                append_handoff_note=handoff_correction._append_handoff_note,
                reinspect=session_verification._reinspect_after_revert,
                changed_paths=(
                    write_guards._protected_path_changed_paths(
                        turn_payload.get("workspace_write_guard")
                    )
                    if isinstance(turn_payload.get("workspace_write_guard"), Mapping)
                    # A legacy review turn captures no write-guard comparison,
                    # but a mutation guard that passed proves the worktree is
                    # unchanged — a stronger clearance than an empty change
                    # list, not a missing one. One that paused proves the
                    # opposite, and names no paths, so it clears nothing.
                    else ()
                    if review_fingerprint_before is not None and not finalization_paused
                    else None
                ),
                adjudicate_only=finalization_paused,
            ):
                break
            if finalization_paused:
                break

            # Every branch below can end the turn — by pausing, by recording a
            # clean stop, or by looping straight into another turn — and a
            # relaunch that starts from a tampered packet adopts it as the new
            # baseline. So the digests are compared here, before any of them
            # runs, and each of those branches is then selected by the name
            # computed here rather than by re-testing the result: a branch the
            # disposition cannot name is unreachable, so none can be added
            # below without this guard having run for it. A turn that reaches
            # the status guards instead is left to the call site below them,
            # which keeps the sharper wrong-session diagnosis for a tamper that
            # moved a status.
            terminating_disposition = handoff_guards._terminating_result_disposition(
                result, after, turn_payload
            )
            if (
                terminating_disposition is not None
                and handoff_guards._enforce_plan_artifact_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    before=before,
                    after=after,
                    digests_before=plan_artifacts_before,
                    runner_written=plan_artifacts_written_by_runner,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=ctx.plan_cache,
                    result_disposition=terminating_disposition,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                )
            ):
                break

            if terminating_disposition == "interrupted":
                with ctx.persistence.lock:
                    ctx.payload["final_snapshot"] = (
                        _snapshot_payload(after)
                        if after is not None
                        else _snapshot_payload(before)
                    )
                ctx.pause(
                    reason="interrupted",
                    message="run interrupted by user",
                    details=session_details,
                )
                break

            if terminating_disposition == "timeout":
                with ctx.persistence.lock:
                    ctx.payload["final_snapshot"] = (
                        _snapshot_payload(after)
                        if after is not None
                        else _snapshot_payload(before)
                    )
                ctx.pause(
                    reason="timeout",
                    message=f"harness turn timed out for {session_label}",
                    details=session_details,
                )
                break

            if terminating_disposition == "aborted":
                # Beside the timeout branch, but not a pause: the operator asked
                # for this stop, so it is recorded as one.
                with run_control_lock:
                    run_termination._honor_abort_turn(
                        ctx.run_controls,
                        snapshot=after if after is not None else before,
                        session_id=session_id,
                        payload=ctx.payload,
                        event_path=ctx.event_path,
                        control_payload=ctx.control_payload,
                        plan_cache=ctx.plan_cache,
                        revert_session_status=handoff_guards.session_status_reverter(
                            handoff_correction._append_handoff_note
                        ),
                        reinspect=session_verification._reinspect_after_revert,
                        context=turn_payload,
                        persistence=ctx.persistence,
                    )
                break
            if terminating_disposition == "malformed-plan":
                corrected, correction_evidence = (
                    handoff_correction._correct_malformed_handoff(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        plan_cache=ctx.plan_cache,
                        before=before,
                        workspace=workspace,
                        run_id=ctx.run_id,
                        turn_index=turn_index,
                        session_id=session_id,
                        plan_error=plan_error,
                        turn_payload=turn_payload,
                        paths=paths,
                    )
                )
                if corrected is not None:
                    snapshot = corrected
                    continue
                malformed_details: dict[str, Any] = {
                    "error": plan_error,
                    **session_details,
                }
                if correction_evidence is not None:
                    malformed_details["handoff_correction"] = correction_evidence
                reopened_snapshot, reopen_evidence = (
                    handoff_correction._reopen_uninspectable_handoff(
                        plan_cache=ctx.plan_cache,
                        before=before,
                        session_id=session_id,
                        plan_error=plan_error,
                        correction_exhausted=bool(
                            correction_evidence is not None
                            and correction_evidence.get("exhausted")
                        ),
                    )
                )
                if reopen_evidence is not None:
                    malformed_details["handoff_reopen"] = reopen_evidence
                ctx.record_final_snapshot(
                    before if reopened_snapshot is None else reopened_snapshot
                )
                _record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="plan-inspection",
                    reason="malformed-plan",
                    reopened=bool(
                        reopen_evidence is not None and reopen_evidence["reopened"]
                    ),
                    detail={"error": plan_error},
                )
                ctx.pause(
                    reason="malformed-plan",
                    message="plan could not be inspected after the harness turn",
                    details=malformed_details,
                )
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

            runner_pause = turn_payload.get("runner_pause")
            if terminating_disposition == "runner-pause":
                pause_reason = _runner_pause_reason(str(runner_pause))
                ctx.record_final_snapshot(after)
                pause_details: dict[str, Any] = {
                    **session_details,
                    "source": turn_payload.get("runner_pause_source"),
                }
                if paused_status_repair is not None:
                    pause_details["handoff_status_repair"] = paused_status_repair
                worker_pause_resolution = (
                    _worker_pause_resolution_for_turn(
                        plan_dir=after.plan_dir,
                        state_dir_arg=args.state_dir,
                        run_id=ctx.run_id,
                        session_id=session_id,
                        turn_index=turn_index,
                    )
                    if mailbox_enabled
                    else None
                )
                if worker_pause_resolution is not None:
                    pause_details["mailbox_resolution"] = {
                        "run_id": ctx.run_id,
                        "session_id": session_id,
                        "turn_index": turn_index,
                        "waiter_origin": mailbox.WAITER_ORIGIN_WORKER,
                        "question_id": worker_pause_resolution.to,
                        "resolution_message_id": worker_pause_resolution.id,
                        "resolution_seq": worker_pause_resolution.seq,
                        "action": worker_pause_resolution.action,
                        "note_length": (
                            None
                            if worker_pause_resolution.note is None
                            else len(worker_pause_resolution.note)
                        ),
                    }
                    ctx.pause(
                        reason=pause_reason,
                        message=str(runner_pause),
                        details=pause_details,
                    )
                    break
                if _runner_pause_can_wait_for_mailbox_reply(
                    mailbox_enabled=mailbox_enabled,
                    pause_reason=pause_reason,
                    result=result,
                ):
                    try:
                        prepared_wait = _prepare_mailbox_reply_wait(
                            snapshot=after,
                            payload=ctx.payload,
                            plan_state=state_dir,
                            state_path=state_path,
                            state_dir_arg=args.state_dir,
                            run_id=ctx.run_id,
                            session_id=session_id,
                            turn_index=turn_index,
                        )
                    except _MailboxReplyWaitUnavailable as error:
                        _record_mailbox_reply_wait_preparation_failed(
                            ctx.event_path,
                            ctx.payload,
                            session_id=session_id,
                            turn_index=turn_index,
                            reason=error.reason,
                            persistence=ctx.persistence,
                        )
                        prepared_wait = None

                    if prepared_wait is not None:
                        # The wait that follows is genuine idle time: the
                        # runner has no work left for this turn until a human
                        # answers, so the clock starts before the wait. Pinning
                        # it is this path's explicit opt-out from the
                        # structural refresh at the top of the loop.
                        ctx.runner_work_finished_at = _utc_now()
                        ctx.idle_baseline_pinned = True
                        wait_result = run_coordination.MailboxWaitCoordinator(
                            plan_dir=after.plan_dir,
                            state_dir_arg=args.state_dir,
                            prepared=prepared_wait,
                            run_id=ctx.run_id,
                            store=run_coordination.WaitStateStore(
                                event_path=ctx.event_path,
                                payload=ctx.payload,
                                persistence=ctx.persistence,
                            ),
                            persistence=ctx.persistence,
                            notifier=run_persistence._AgentMuxWaitNotifier(
                                ctx.payload, ctx.persistence
                            ),
                            poll_controls=mailbox_poll,
                            # Resolved here rather than inside the coordinator
                            # so this module keeps owning both names: callers
                            # patch them on the runner to assert a wait was or
                            # was not entered.
                            wait_for_reply=_wait_for_mailbox_reply,
                            finish_lifecycle=_finish_mailbox_reply_wait_lifecycle,
                        ).run()

                        if wait_result.reply is not None:
                            reply_message = wait_result.reply
                            assert reply_message.text is not None
                            pending_reply = run_prompts._PendingReply(
                                text=reply_message.text,
                                context=prepared_wait.context,
                                origin=run_prompts.REPLY_ORIGIN_MAILBOX,
                                process_start=False,
                                question_id=prepared_wait.question.id,
                                reply_message_id=reply_message.id,
                                reply_seq=reply_message.seq,
                            )
                            # The session remains pending; start its explicit
                            # reply continuation before honoring any
                            # stop-after-current request.
                            snapshot = after
                            continue
                        if wait_result.resolution is not None:
                            resolution_message = wait_result.resolution
                            assert resolution_message.action is not None
                            if (
                                resolution_message.action
                                == mailbox.RESOLUTION_ACTION_CONTINUE
                            ):
                                pending_resolution = run_prompts._PendingResolution(
                                    action=resolution_message.action,
                                    note=resolution_message.note,
                                    context=prepared_wait.context,
                                    question_id=prepared_wait.question.id,
                                    resolution_message_id=(resolution_message.id),
                                    resolution_seq=resolution_message.seq,
                                )
                                # The session remains pending; schedule its
                                # answerless continuation before honoring any
                                # stop-after-current request.
                                snapshot = after
                                continue
                            assert (
                                resolution_message.action
                                == mailbox.RESOLUTION_ACTION_PAUSE
                            )
                ctx.pause(
                    reason=pause_reason,
                    message=str(runner_pause),
                    details=pause_details,
                )
                break
            if terminating_disposition == "nonzero-exit":
                classification = classify_harness_failure(result)
                spark_failure = spark_route[
                    "decision"
                ] == "spark" and classification.reason in {
                    events.PAUSE_REASON_QUOTA_EXHAUSTED,
                    events.PAUSE_REASON_INVALID_MODEL,
                }
                selected_after = next(
                    (item for item in after.sessions if item.id == session_id),
                    None,
                )
                safely_retryable = (
                    selected_after is not None
                    and selected_after.status == "pending"
                    and after.content_sha256 == before.content_sha256
                )
                if spark_failure and safely_retryable:
                    try:
                        cooldown = model_availability.suppress_until(
                            state.state_root(args.state_dir),
                            run_selection.SPARK_MODEL,
                            reason=classification.reason,
                            retry_hint=classification.retry_at,
                            run_id=ctx.run_id,
                        )
                    except OSError as error:
                        cooldown = None
                        with ctx.persistence.lock:
                            ctx.payload["spark"]["warning"] = (
                                f"could not update model availability ledger: {error}"
                            )
                    fallback_reason = classification.reason
                    if cooldown is None:
                        spark_run_suppression = (
                            f"{classification.reason}-for-current-run"
                        )
                        spark_status_update = {
                            "available": False,
                            "availability": False,
                            "reason": spark_run_suppression,
                            "retry_at": None,
                        }
                    else:
                        spark_status_update = cooldown.to_dict()
                    spark_fallback_reason = fallback_reason
                    with ctx.persistence.lock:
                        spark_route["fallback_reason"] = fallback_reason
                        turn_payload["spark_route"] = dict(spark_route)
                        ctx.payload["spark"].update(spark_status_update)
                        ctx.payload["spark"]["active_route"] = dict(spark_route)
                        ctx.payload["final_snapshot"] = _snapshot_payload(after)
                        _write_json_file(paths["result"], turn_payload)
                        ctx.persistence.append_event(
                            ctx.event_path,
                            "spark_fallback",
                            {
                                "run_id": ctx.run_id,
                                "session_id": session_id,
                                "failed_turn_index": turn_index,
                                "failed_model": run_selection.SPARK_MODEL,
                                "fallback_model": ctx.payload["spark"]["baseline"],
                                "failure_classification": classification.to_dict(),
                                "retry_at": ctx.payload["spark"].get("retry_at"),
                            },
                        )
                        ctx.persistence.write_state(ctx.payload)
                    snapshot = after
                    continue
                if classification.reason == events.PAUSE_REASON_NONZERO_EXIT:
                    pause_message = (
                        f"harness exited with status {result.returncode} "
                        f"for {session_label}"
                    )
                else:
                    pause_message = (
                        f"{session_label} failed: {classification.reason} "
                        f"(harness exit {result.returncode})"
                    )
                ctx.record_final_snapshot(after)
                ctx.pause(
                    reason=classification.reason,
                    message=pause_message,
                    details={
                        **session_details,
                        "returncode": result.returncode,
                        "failure_classification": classification.to_dict(),
                        **(
                            {"spark_fallback_unsafe": True}
                            if spark_failure and not safely_retryable
                            else {}
                        ),
                    },
                )
                break

            expected_change = (
                _session_status_change_payload(
                    session_id,
                    before="pending",
                    after="complete",
                ),
            )
            status_changes = _status_changes(before, after)
            if not status_changes:
                with ctx.persistence.lock:
                    ctx.payload["expected_status_changes"] = list(expected_change)
                    ctx.payload["observed_status_changes"] = []
                    ctx.payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="status-change",
                    reason="no-status-change",
                    detail={
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": [],
                    },
                )
                ctx.pause(
                    reason="no-status-change",
                    message=f"{session_label} did not change status",
                    details={
                        **session_details,
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": [],
                    },
                )
                break
            if len(status_changes) > 1:
                with ctx.persistence.lock:
                    ctx.payload["expected_status_changes"] = list(expected_change)
                    ctx.payload["observed_status_changes"] = list(status_changes)
                    ctx.payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="status-change",
                    reason="multiple-status-changes",
                    detail={
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                )
                ctx.pause(
                    reason="multiple-status-changes",
                    message="more than one session status changed during one harness "
                    "turn",
                    details={
                        **session_details,
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                )
                break
            if status_changes != expected_change:
                with ctx.persistence.lock:
                    ctx.payload["expected_status_changes"] = list(expected_change)
                    ctx.payload["observed_status_changes"] = list(status_changes)
                    ctx.payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="status-change",
                    reason="unexpected-status-change",
                    detail={
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                )
                ctx.pause(
                    reason="unexpected-status-change",
                    message="the changed session status did not match the expected "
                    "pending completion",
                    details={
                        **session_details,
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                )
                break

            # The turn survived every result branch, so nothing terminated it
            # early and the comparison above was deferred to here. Behind the
            # status guards, a turn that completed the wrong session keeps
            # their sharper diagnosis; what is left is the tampering they
            # cannot see, an edit that moves no status at all.
            if handoff_guards._enforce_plan_artifact_guard(
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                before=before,
                after=after,
                digests_before=plan_artifacts_before,
                runner_written=plan_artifacts_written_by_runner,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                turn_payload=turn_payload,
                paths=paths,
                plan_cache=ctx.plan_cache,
                append_handoff_note=handoff_correction._append_handoff_note,
                reinspect=session_verification._reinspect_after_revert,
            ):
                break

            if session_verification._enforce_session_verification(
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                progress=ctx.progress,
                before=before,
                after=after,
                cwd=cwd,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                allow_not_loop_ready=allow_not_loop_ready,
                turn_payload=turn_payload,
                paths=paths,
                plan_cache=ctx.plan_cache,
                append_handoff_note=handoff_correction._append_handoff_note,
                run_id=ctx.run_id,
                turn_index=turn_index,
                total_sessions=total_sessions,
                verification_context=ctx.verification_context,
                review_broad_failure_deferral=is_review_session,
                require_review_artifact=(
                    review_session_version is not None
                    and review_session_version >= STRUCTURED_REVIEW_MIN_VERSION
                ),
                generated_review_session=is_review_session,
            ):
                break

            if is_review_session:
                # A retry short-circuits the loop tail, so the operator's stop
                # request has to be visible *here* or it would be honored a
                # whole review turn late. The queue drain is what makes an
                # `asl control stop` observable at all on a review turn; the
                # read below is non-consuming, like every other checkpoint —
                # only `run_termination._honor_stop_after_current` consumes,
                # at the moment the loop actually stops.
                _drain_external_control_queue(
                    plan_dir=before.plan_dir,
                    state_dir_arg=args.state_dir,
                    control_state=ctx.run_controls.state,
                    seen_ids=ctx.seen_external_command_ids,
                    payload=ctx.payload,
                    event_path=ctx.event_path,
                    control_payload=ctx.control_payload,
                    context=turn_payload,
                    persistence=ctx.persistence,
                    run_control_lock=run_control_lock,
                    wakeup=wakeup,
                    guard_controller=ctx.guard_controller,
                )
                with run_control_lock:
                    stop_after_current_pending = (
                        ctx.run_controls.state.stop_after_current
                    )
                verdict_action, verdict_snapshot = (
                    session_verification._enforce_review_verdict(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        before=before,
                        after=after,
                        session_id=session_id,
                        session_label=session_label,
                        session_details=session_details,
                        turn_payload=turn_payload,
                        paths=paths,
                        plan_cache=ctx.plan_cache,
                        append_handoff_note=handoff_correction._append_handoff_note,
                        require_artifact=(
                            review_session_version is not None
                            and review_session_version >= STRUCTURED_REVIEW_MIN_VERSION
                        ),
                        stop_after_current_pending=stop_after_current_pending,
                    )
                )
                if verdict_action == "pause":
                    break
                if verdict_action == "retry":
                    assert verdict_snapshot is not None
                    snapshot = verdict_snapshot
                    continue

            completed_at = _utc_iso_now()
            progress_error: str | None = None
            try:
                progress_appended = handoff_guards._append_progress_entry(
                    after, session_id, completed_at
                )
            except Exception as error:
                progress_appended = False
                progress_error = str(error)
                ctx.persistence.append_event(
                    ctx.event_path,
                    "progress-journal-append-failed",
                    {
                        "run_id": ctx.payload.get("run_id"),
                        "session": session_id,
                        "session_label": session_label,
                        "completed_at": completed_at,
                        "error": progress_error,
                    },
                )
            with ctx.persistence.lock:
                turn_payload["progress_appended"] = progress_appended
                if progress_error is not None:
                    turn_payload["progress_error"] = progress_error
                _write_json_file(paths["result"], turn_payload)

            try:
                ledger_updates = plans.sync_derived_status_ledgers(after)
            except plans.PlanError as error:
                ctx.record_final_snapshot(after)
                _record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="ledger-sync",
                    reason="malformed-plan",
                    detail={"error": str(error)},
                )
                ctx.pause(
                    reason="malformed-plan",
                    message="derived status ledgers could not be synced",
                    details={
                        **session_details,
                        "error": str(error),
                    },
                )
                break
            if ledger_updates:
                try:
                    after = ctx.plan_cache.get_fresh(before.plan_dir)
                except plans.PlanError as error:
                    ctx.record_final_snapshot(after)
                    _record_turn_rejected(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        turn_payload=turn_payload,
                        session_id=session_id,
                        guard="ledger-sync",
                        reason="malformed-plan",
                        detail={"error": str(error)},
                    )
                    ctx.pause(
                        reason="malformed-plan",
                        message="plan could not be inspected after syncing ledgers",
                        details={
                            **session_details,
                            "error": str(error),
                        },
                    )
                    break
                with ctx.persistence.lock:
                    turn_payload["ledger_updates"] = [
                        str(path) for path in ledger_updates
                    ]
                    turn_payload["after"] = _snapshot_payload(after)
                    _write_json_file(paths["result"], turn_payload)

            readiness_gate._refresh_active_readiness_state(
                ctx.payload, after.plan_dir, ctx.persistence
            )

            repair_owned_commit = is_repair_session and commit_mode is None
            if not is_review_session and (
                commit_mode == run_selection.COMMIT_MODE_PER_SESSION
                or repair_owned_commit
            ):
                session_targets: list[dict[str, Any]] = []
                try:
                    session_targets = (
                        commit_settlement._registered_session_commit_targets(
                            ctx.verification_context,
                            turn_payload.get("workspace_write_guard", {}),
                        )
                        if ctx.verification_context.workspace is not None
                        else commit_settlement._dirty_commit_targets(
                            ctx.verification_context
                        )
                    )
                    if repair_owned_commit:
                        session_targets = [
                            target
                            for target in session_targets
                            if not commit_settlement._commit_target_is_clean(target)
                        ]
                    if session_targets or not repair_owned_commit:
                        commit_group, failed_commit = (
                            commit_settlement._run_commit_group(
                                event_path=ctx.event_path,
                                payload=ctx.payload,
                                progress=ctx.progress,
                                verification_context=ctx.verification_context,
                                run_dir=ctx.run_dir,
                                run_id=ctx.run_id,
                                session_id=session_id,
                                targets=session_targets,
                                generation=ctx.commit_generation,
                                persistence=ctx.persistence,
                            )
                        )
                    else:
                        commit_group = None
                        failed_commit = None
                except RunError as error:
                    commit_group = None
                    failed_commit = None
                    commit_error = str(error)
                else:
                    commit_error = None
                if repair_owned_commit:
                    with ctx.persistence.lock:
                        turn_payload["repair_commit"] = {
                            "mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
                            "result": (
                                "no-changes"
                                if not session_targets
                                else (
                                    "failed"
                                    if failed_commit is not None
                                    or commit_error is not None
                                    else "committed"
                                )
                            ),
                            "commit_group": commit_group,
                        }
                        _write_json_file(paths["result"], turn_payload)
                if failed_commit is not None or commit_error is not None:
                    # The turn itself stands: it executed, it verified, and the
                    # session it completed is still complete. Only the commit
                    # of its changes failed, so the failure is recorded on the
                    # turn and on the run, and the turn goes on to be committed
                    # to the plan. Recording a `turn_rejected` here instead
                    # would say a guard refused a turn the very next call
                    # accepts.
                    with ctx.persistence.lock:
                        turn_payload["session_commit"] = {
                            "guard": (
                                "repair-session-commit"
                                if repair_owned_commit
                                else "session-commit"
                            ),
                            "reason": "commit-failed",
                            "error": commit_error,
                            "commit_group": commit_group,
                            "commit": failed_commit,
                        }
                        _write_json_file(paths["result"], turn_payload)
                    _soft_fail_commit(
                        ctx,
                        scope="session",
                        session_id=session_id,
                        group=commit_group,
                        commit=failed_commit,
                        error=commit_error,
                        message=(
                            f"auto-commit failed after session {session_id} completed"
                        ),
                    )

            try:
                commit = turn_executor.commit(
                    payload=ctx.payload,
                    turn_payload=turn_payload,
                    live_turn=live_turn_payload,
                    accepted=after,
                )
            except _TurnCommitRejected as rejection:
                ctx.record_final_snapshot(after)
                _record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="ledger-sync",
                    reason=rejection.reason,
                    detail=rejection.detail,
                )
                ctx.pause(
                    reason=rejection.reason,
                    message=rejection.message,
                    details={**session_details, **rejection.detail},
                )
                break

            with ctx.persistence.lock:
                ctx.payload["completed_sessions"].append(
                    _session_compat_value(session_id)
                )
                ctx.payload.setdefault("completed_session_ids", []).append(session_id)
                if is_review_session:
                    ctx.payload["review_completed"] = True
                    review_verdict = commit.record.get("review_verdict")
                    if isinstance(review_verdict, dict):
                        ctx.payload["review_verdict"] = {
                            "status_line": review_verdict["status_line"],
                            "outcome": review_verdict["outcome"],
                            "total_findings": review_verdict["total_findings"],
                            "findings_by_severity": review_verdict[
                                "findings_by_severity"
                            ],
                        }
            if not is_review_session:
                ctx.completed_count += 1
            snapshot = after
            ctx.record_final_snapshot(commit.published)

            if is_review_session:
                cycle_action, snapshot = review_cycle._advance_review_cycle(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    args=args,
                    cwd=cwd,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    snapshot=snapshot,
                    turn_payload=commit.record,
                    max_review_fix_cycles=max_review_fix_cycles,
                    plan_cache=ctx.plan_cache,
                    reinspect=session_verification._reinspect_after_plan_mutation,
                )
                review_resume = review_cycle._set_review_resume(
                    snapshot=snapshot,
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    max_repair_group_attempts=run_inputs._max_repair_group_attempts(
                        args
                    ),
                    max_review_fix_cycles=max_review_fix_cycles,
                    review_requested=review_requested,
                )
                if cycle_action == "paused":
                    break
                if cycle_action == "complete":
                    with ctx.persistence.lock:
                        ctx.payload["reason"] = review_cycle._terminal_review_reason(
                            ctx.payload
                        )
                    break
                # Repair children were materialized: clear the appended flag
                # so the loop appends the next review session once the
                # children complete, then fall through to the control tail
                # so operator stop requests are honored between cycles.
                ctx.review_appended = False

            _drain_external_control_queue(
                plan_dir=snapshot.plan_dir,
                state_dir_arg=args.state_dir,
                control_state=ctx.run_controls.state,
                seen_ids=ctx.seen_external_command_ids,
                payload=ctx.payload,
                event_path=ctx.event_path,
                control_payload=ctx.control_payload,
                context=commit.record,
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
                        context=commit.record,
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
