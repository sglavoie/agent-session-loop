"""The runner's remaining surface: two argv entry points and the public names.

Nothing runs here any more. The locked run this module used to hold moved to
:mod:`agent_session_loop.run_loop`, and what is left is the boundary the rest
of the tree still addresses by name: ``run_from_args`` and ``doctor_from_args``,
which ``cli.py`` reaches through ``handler_ref`` strings, and the re-exports
below that keep long-standing ``runner.X`` spellings resolving at their owning
modules.

The re-exports are deliberate, not residue. Each one is a name that predates
the extraction and is read by in-tree callers, skills, or tests; each is
assigned from the module that now owns the behaviour, so there is exactly one
definition and the runner spelling is an alias rather than a copy. They retire
in the final session of this extraction, once their callers are repointed.
"""

from __future__ import annotations

import argparse

from . import (
    agent_mux_delivery,
    agent_mux_projection,
    clock,
    durability,
    events,
    harness_failures,
    # Re-exported, not used here: the last call site moved to
    # `turn_dispositions` in Session 35, but in-tree callers patch
    # `runner.mailbox` to stub a mailbox read, and this session moves code
    # rather than names.
    mailbox,  # noqa: F401
    mailbox_rendering,
    plans,
    review,
    review_continuation,
    review_transitions,
    run_coordination,
    run_doctor,
    run_entry,
    run_loop,
    turn_execution,
    verification_transitions,
)

# Re-exported, not used here: `run_errors.py` owns the run's fatal-error type
# and this import is what keeps `runner.RunError` the same class object, so
# every `except runner.RunError` in the tree is unchanged.
from .run_errors import RunError  # noqa: F401

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
    that module may not import, the locked run in
    :mod:`agent_session_loop.run_loop`.
    """
    return run_entry.run_from_args(
        args, run_argv=run_argv, run_locked=run_loop.run_locked
    )


def doctor_from_args(args: argparse.Namespace) -> int:
    """Check local harness prerequisites from CLI arguments.

    Reached by ``cli.py`` through the string
    ``handler_ref="runner:doctor_from_args"``, so the name is a contract. The
    staged resolution and every refusal it prints live in
    :mod:`agent_session_loop.run_doctor`; this boundary keeps only the name.
    """
    return run_doctor.doctor_from_args(args)
