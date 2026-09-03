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
import contextlib
import functools
import json
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    agent_mux_delivery,
    agent_mux_projection,
    assurance_report,
    clock,
    commit_groups,
    commit_settlement,
    config,
    contracts,
    control_signal,
    durability,
    events,
    external_write_evidence,
    guards,
    guidance_delivery,
    handoff_guards,
    harness_failures,
    harnesses,
    live_verification,
    mailbox,
    mailbox_rendering,
    model_availability,
    pause_policy,
    plans,
    preflight,
    qna,
    readiness_gate,
    repository_evidence,
    repository_registration,
    responses,
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
    run_contention,
    run_control,
    run_coordination,
    run_flags,
    run_inputs,
    run_persistence,
    run_prompts,
    run_reporting,
    run_selection,
    run_termination,
    state,
    supervisor_audit,
    task_packet,
    turn_execution,
    verification,
    verification_transitions,
    workspace_evidence,
    workspaces,
)
from .review_guard import RepairCommitPreflightError
from .run_errors import RunError

NOT_IMPLEMENTED_EXIT = 2
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
AGENT_MUX_PAUSE_REASON = agent_mux_delivery.PAUSE_REASON
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

# Post-turn handoff correction control. A completed handoff whose ``Files
# modified`` line is mechanically malformed — missing, still carrying the
# pending placeholder, or holding an entry that is not ``Alias:path`` — stops
# the whole plan from parsing, which used to end the run in a ``malformed-plan``
# pause even though the defect is one line the worker can rewrite. The runner
# instead reverts that session to ``pending`` once, names the defect in the
# Handoff note, and re-runs it. One attempt is the whole budget: a worker that
# reproduces the same unparsable handoff after being told exactly what is wrong
# is not converging, and every further turn costs a full harness launch.
MAX_HANDOFF_CORRECTION_ATTEMPTS = 1

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


def _not_implemented(command: str) -> int:
    print(f"agent-session-loop {command}: not implemented yet", file=sys.stderr)
    return NOT_IMPLEMENTED_EXIT


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


REPAIR_SCOPE_STALE_BLOCKER = "repair-scope-stale"


def _regenerate_stale_repair_child_scopes(
    *,
    snapshot: plans.PlanSnapshot,
    workspace: workspaces.ResolvedWorkspace,
    gaps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Widen every pending child a scope-stale projection named.

    The projection is plan-wide, so a single stale sibling blocks the whole
    ``run-repairs`` continuation. Regenerating each child the projection names —
    not only the one about to run — is what keeps the gate self-clearing rather
    than a deadlock an operator has to hand-edit their way out of.
    """
    by_id = {session.id: session for session in snapshot.sessions}
    records: list[dict[str, Any]] = []
    for gap in gaps:
        session_id = gap.get("repair_session_id")
        session = by_id.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.status != "pending":
            continue
        try:
            scope = run_prompts._session_scope(session, workspace)
        except plans.PlanError:
            # Fail open exactly like the regeneration helper: a child whose
            # scope cannot be read keeps the blocker rather than being rewritten
            # from a scope nobody could parse.
            continue
        record = workspace_evidence._regenerated_repair_child_write_scope(
            session=session,
            plan_dir=snapshot.plan_dir,
            workspace=workspace,
            scope=scope,
        )
        if record is not None:
            records.append(record)
    return records


def _pending_repair_child_continuation(
    *,
    snapshot: plans.PlanSnapshot,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    workspace: workspaces.ResolvedWorkspace | None,
    max_repair_group_attempts: int,
    max_review_fix_cycles: int,
    review_requested: bool,
) -> review_continuation.ReviewResume:
    """Project the repair continuation, clearing a stale scope once if it can.

    The continuation is the only component that compares every pending child's
    persisted scope against the scope its own findings require today. Projecting
    it before the child is dispatched is what turns ``repair-scope-stale`` from
    an advisory note into a gate: the caller either launches a child the
    projection calls eligible, or stops without spending a turn.
    """

    def project() -> review_continuation.ReviewResume:
        return review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            max_repair_group_attempts=max_repair_group_attempts,
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=review_requested,
        )

    resume = project()
    if (
        workspace is None
        or resume.action != "run-repairs"
        or resume.eligible
        or resume.blocker != REPAIR_SCOPE_STALE_BLOCKER
        or not resume.repair_scope_gaps
    ):
        return resume
    if not _regenerate_stale_repair_child_scopes(
        snapshot=snapshot,
        workspace=workspace,
        gaps=resume.repair_scope_gaps,
    ):
        return resume
    readiness_gate._refresh_active_readiness_state(
        payload, snapshot.plan_dir, persistence
    )
    # Revalidate rather than assume: the rewrite is widening-only and fail-open,
    # so the second projection is the only proof the gap actually closed.
    return project()


def _enforce_workspace_write_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans.SessionScope,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    snapshot_before: repository_evidence.WorkspaceSnapshot,
    exclude: tuple[str, ...],
    include_ignored: bool,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    state_dir_arg: str | None = None,
    run_dir: Path | None = None,
    external_digests_before: Mapping[str, Mapping[str, Any]] | None = None,
    turn_started_ns: int | None = None,
) -> bool:
    """Fail closed when a registered implementation turn exceeds its writes."""
    snapshot_after: repository_evidence.WorkspaceSnapshot | None = None
    try:
        snapshot_after = handoff_guards._capture_workspace_write_guard_snapshot(
            workspace=workspace,
            plan_dir=before.plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    except repository_evidence.RepositoryEvidenceError as error:
        comparison: dict[str, Any] = {
            "changed": {},
            "added": [],
            "removed": [],
            "unavailable": {
                ".": {
                    "after": {
                        "reason": "workspace-inventory-failed",
                        "error": str(error),
                    }
                }
            },
        }
    else:
        comparison = repository_evidence.compare_workspace_snapshots(
            snapshot_before, snapshot_after
        )
        workspace_evidence._annotate_history_transitions(
            comparison,
            repository_roots={
                repository.repository.path: repository.root
                for repository in workspace.repositories
            },
        )

    evidence = workspace_evidence._workspace_write_guard_evidence(
        workspace=workspace,
        scope=scope,
        comparison=comparison,
        exclude=exclude,
        include_ignored=include_ignored,
        external_writes=external_write_evidence._turn_external_write_evidence(
            plan_dir=before.plan_dir,
            artifact_paths=(
                *run_inputs._task_packet_artifact_paths(
                    before.plan_dir, session_id, None
                ),
                paths["last_message"],
            ),
            root=workspace.root,
            workspace_root=workspace.root,
            repository_roots=[repository.root for repository in workspace.repositories],
            state_dir_arg=state_dir_arg,
            run_dir=run_dir,
            digests_before=external_digests_before,
            turn_started_ns=turn_started_ns,
        ),
    )
    if evidence["passed"]:
        with persistence.lock:
            turn_payload["workspace_write_guard"] = evidence
            _write_json_file(paths["result"], turn_payload)
        return False

    if evidence["unavailable"]:
        unavailable_summary = workspace_evidence._workspace_unavailable_summary(
            evidence["unavailable"], workspace=workspace
        )
        reason = events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
        failure_note = (
            "workspace write guard unavailable; evidence for a registered "
            f"repository could not be captured safely{unavailable_summary}"
        )
        message = (
            f"workspace write guard unavailable after {session_label}; "
            "evidence for a registered repository could not be captured "
            f"safely{unavailable_summary}"
        )
    else:
        reason = events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
        summary = workspace_evidence._workspace_guard_violation_summary(evidence)
        failure_note = (
            "workspace write scope violation retained"
            f"{summary}; repair the durable repository scope and rerun "
            "expansion/preflight before continuing"
        )
        message = (
            f"{session_label} changed registered repositories outside its "
            f"write aliases{summary}; changes were retained"
        )

    revert_target = after if after is not None else before
    reverted = handoff_guards._revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=_append_handoff_note,
    )
    inspected = _reinspect_after_revert(
        before.plan_dir, revert_target, reverted, plan_cache
    )
    guard_record = {
        **evidence,
        "session_id": session_id,
        "reason": reason,
        "phase": "after",
        "reverted_to_pending": reverted is not None,
        "repair_required": {
            "durable_scope": True,
            "fresh_expansion": True,
            "fresh_preflight": True,
        },
    }
    if reason == events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION:
        guard_record["snapshot_before"] = snapshot_before
        # The retained changes outlive this process, so what the turn left
        # behind has to outlive it too: the after-vector is the only durable
        # description of the *content* of every retained path. Without it a
        # later resume can prove that a dirty path bears a name the turn wrote
        # but never that it still holds the bytes the turn wrote.
        if snapshot_after is not None:
            guard_record["snapshot_after"] = snapshot_after
        guard_record["expansion_before_repair"] = (
            workspace_evidence._session_expansion_evidence(before.plan_dir, session_id)
        )
    with persistence.lock:
        payload["final_snapshot"] = _snapshot_payload(inspected)
        turn_payload["workspace_write_guard"] = guard_record
        _write_json_file(paths["result"], turn_payload)
    _record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="workspace-write-guard",
        reason=reason,
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=message,
        details={
            **session_details,
            "workspace_write_guard": guard_record,
        },
        persistence=persistence,
    )
    return True


def _enforce_legacy_implementation_write_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    cwd: Path,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    snapshot_before: repository_evidence.WorkspaceSnapshot,
    exclude: tuple[str, ...],
    include_ignored: bool,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    state_dir_arg: str | None,
    run_dir: Path | None = None,
    external_digests_before: Mapping[str, Mapping[str, Any]] | None = None,
    turn_started_ns: int | None = None,
) -> bool:
    """Fail closed around every legacy implementation turn, in every mode."""
    snapshot_after = handoff_guards._capture_legacy_implementation_snapshot(
        cwd=cwd,
        plan_dir=before.plan_dir,
        exclude=exclude,
        include_ignored=include_ignored,
    )
    comparison = repository_evidence.compare_workspace_snapshots(
        snapshot_before, snapshot_after
    )
    workspace_evidence._annotate_history_transitions(
        comparison,
        repository_roots={".": cwd},
    )
    artifact_paths = (
        *run_inputs._task_packet_artifact_paths(before.plan_dir, session_id, None),
        paths["last_message"],
    )
    evidence = workspace_evidence._legacy_implementation_write_guard_evidence(
        cwd=cwd,
        comparison=comparison,
        exclude=exclude,
        include_ignored=include_ignored,
        unobserved=handoff_guards._legacy_unobserved_repositories(
            cwd=cwd,
            artifact_paths=artifact_paths,
            exempt_roots=run_inputs._runner_owned_roots(
                before.plan_dir, state_dir_arg, run_dir=run_dir
            ),
            provenance_roots=preflight.plan_provenance_exempt_roots(before.plan_dir),
        ),
        external_writes=external_write_evidence._turn_external_write_evidence(
            plan_dir=before.plan_dir,
            artifact_paths=artifact_paths,
            root=cwd,
            workspace_root=cwd,
            repository_roots=[cwd],
            state_dir_arg=state_dir_arg,
            run_dir=run_dir,
            digests_before=external_digests_before,
            turn_started_ns=turn_started_ns,
        ),
    )
    if evidence["passed"]:
        with persistence.lock:
            turn_payload["workspace_write_guard"] = evidence
            _write_json_file(paths["result"], turn_payload)
        return False

    reason = events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
    failure_note = (
        "implementation write guard unavailable; legacy repository scope or "
        "history evidence could not be captured safely"
    )
    revert_target = after if after is not None else before
    reverted = handoff_guards._revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=_append_handoff_note,
    )
    inspected = _reinspect_after_revert(
        before.plan_dir, revert_target, reverted, plan_cache
    )
    guard_record = {
        **evidence,
        "session_id": session_id,
        "reason": reason,
        "phase": "after",
        "reverted_to_pending": reverted is not None,
    }
    with persistence.lock:
        payload["final_snapshot"] = _snapshot_payload(inspected)
        turn_payload["workspace_write_guard"] = guard_record
        _write_json_file(paths["result"], turn_payload)
    _record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="legacy-implementation-write-guard",
        reason=reason,
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=(
            f"implementation write guard unavailable after {session_label}; "
            "legacy repository scope or history evidence could not be captured safely"
        ),
        details={
            **session_details,
            "workspace_write_guard": guard_record,
        },
        persistence=persistence,
    )
    return True


def _protected_path_changed_paths(
    evidence: Mapping[str, Any] | None,
) -> tuple[str, ...] | None:
    """Every qualified path the turn changed, as the write guard recorded it.

    Read from the guard's own evidence rather than recaptured: a second
    snapshot would be taken at a different instant, so a path that moved
    between the two comparisons could be convicted by one and cleared by the
    other. One capture, two questions.

    ``None`` means the comparison cannot answer the protected-path question at
    all. That includes a write guard that failed to inventory a repository:
    its ``changed`` map is then a partial view, and reading an empty one as
    "nothing moved" would clear a frozen path the run never actually looked
    at. This only arises on a composed finalization, because an incomplete
    inventory already fails the write guard.
    """
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("unavailable"):
        return None
    changed = evidence.get("changed")
    if not isinstance(changed, Mapping):
        return None
    qualified: list[str] = []
    for entry in changed.values():
        if not isinstance(entry, Mapping):
            continue
        paths = entry.get("qualified_paths")
        if isinstance(paths, list):
            qualified.extend(str(path) for path in paths)
    return tuple(dict.fromkeys(qualified))


def _protected_path_review_changed_paths(
    guard_context: Mapping[str, Any],
    *,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> tuple[str, ...] | None:
    """Every qualified path a crashing legacy review turn changed.

    A legacy review turn captures no write-guard comparison, so the crash path
    has nothing to read out of ``workspace_write_guard``. It recaptures the
    review fingerprint instead and compares it against the one taken at turn
    start -- the same comparison the mutation guard would have made had the
    turn finished -- so the protected-path guard has a baseline to adjudicate
    against.

    ``None`` means the recapture cannot answer the question: it raised, or the
    evidence came back unusable. That is reported as unavailable rather than
    read as "nothing moved", because a guard that cannot see is not a guard
    that saw nothing.
    """
    fingerprint_before = guard_context.get("review_fingerprint_before")
    if not isinstance(fingerprint_before, Mapping):
        return None
    try:
        fingerprint_after = review_guard._capture_review_repo_fingerprint(
            guard_context["cwd"],
            guard_context["before"].plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    except Exception:
        return None
    if review_guard._review_guard_unavailable_detail(fingerprint_after) is not None:
        return None
    if fingerprint_after == fingerprint_before:
        # Equal fingerprints prove the whole worktree is unchanged, which
        # clears every path rule at once -- a stronger clearance than an empty
        # change list, and the same one the finished-turn path grants.
        return ()
    return tuple(
        repository_evidence.qualified_repository_paths(
            guards.LEGACY_PATH_QUALIFIER,
            review_guard._review_guard_changed_paths(
                fingerprint_before, fingerprint_after
            ),
        )
    )


def _enforce_protected_path_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    guard_controller: guards.GuardController | None,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    changed_paths: tuple[str, ...] | None,
    adjudicate_only: bool = False,
) -> bool:
    """Refuse to publish a turn that moved a path an operator declared frozen.

    This runs after the general workspace write guard and before every result
    branch, and it is deliberately a *separate* step: the write guard asks
    whether the session stayed inside its declared scope, which is a question
    about the plan, while this asks whether the run honoured a standing
    instruction an operator gave mid-flight, which is a question about the
    operator. A path can be perfectly in scope and still be one the operator
    said not to touch.

    The baseline is the turn-start snapshot, so a guard added mid-turn convicts
    a change the turn had already made. That is the intended reading of "this
    file must not change": an operator who sees a file they care about and
    freezes it is asking about its state now, not about the moment their
    command was drained.

    Filesystem changes are always retained. The run has no way to know whether
    the change was the valuable part of the turn, and silently reverting an
    agent's work to satisfy a guard would destroy more than it protects.

    ``changed_paths`` of ``None`` means the turn produced no comparison this
    guard can read, which is refused rather than waved through: a guard that
    cannot see is not a guard that saw nothing.

    ``adjudicate_only`` is for a turn that is already ending for another
    reason -- another finalization guard paused it, or the harness raised.
    The verdict is still reached and still recorded, but the outcome that was
    already decided is left standing: no second revert, and no pause that
    would overwrite the first one's reason and remediation with this one's.
    An operator reading the primary pause still finds the protected-path
    verdict beside it, because a combined failure that reports only half of
    itself is the one an operator repairs twice.
    """
    active = () if guard_controller is None else guard_controller.active_paths()
    if not active:
        return False

    evidence = turn_payload.get("workspace_write_guard")
    unavailable_detail: str | None = None
    if changed_paths is None:
        changed_paths = ()
        unavailable_detail = (
            "no turn-start comparison was available for this turn, so the "
            "workspace could not be compared against its baseline"
        )

    coverage_by_id = {
        entry.get("id"): entry.get("coverage")
        for entry in (
            guard_controller.to_payload() if guard_controller is not None else []
        )
    }
    matched = (
        guards.match_protected_paths(active, changed_paths)
        if unavailable_detail is None
        else {}
    )
    if unavailable_detail is None and not matched:
        with persistence.lock:
            turn_payload["protected_path_guard"] = {
                "passed": True,
                "session_id": session_id,
                "guards": [rule.to_payload() for rule in active],
                "observed_paths": list(changed_paths),
                "coverage": {rule.id: coverage_by_id.get(rule.id) for rule in active},
            }
            _write_json_file(paths["result"], turn_payload)
        return False

    if unavailable_detail is not None:
        reason = events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE
        failure_note = (
            "protected-path guard evidence unavailable; "
            f"{unavailable_detail}, so the guard could neither clear nor "
            "convict the turn"
        )
        message = (
            f"protected-path guard unavailable after {session_label}; "
            f"{unavailable_detail}"
        )
    else:
        reason = events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED
        named = ", ".join(
            f"{guard_id} ({', '.join(hits)})" for guard_id, hits in matched.items()
        )
        failure_note = (
            f"protected-path guard violation retained: {named}; restore the "
            "path to its turn-start state or remove the guard before rerunning"
        )
        message = (
            f"{session_label} changed protected paths: {named}; changes were retained"
        )

    matched_repositories = {
        qualified.partition(":")[0] for hits in matched.values() for qualified in hits
    }
    raw_changed = evidence.get("changed") if isinstance(evidence, Mapping) else None
    before_after = (
        {
            repository: dict(entry)
            for repository, entry in raw_changed.items()
            if isinstance(entry, Mapping)
            # A qualified path names a repository by its registered alias when
            # it has one, while the write guard keys its comparison by
            # repository path. Either name identifies the same repository, so
            # either one keeps the evidence attached; matching only the key
            # silently drops the before/after vectors for every aliased
            # workspace repository.
            and (
                repository in matched_repositories
                or entry.get("alias") in matched_repositories
            )
        }
        if isinstance(raw_changed, Mapping)
        else {}
    )
    guard_record: dict[str, Any] = {
        "passed": False,
        "session_id": session_id,
        "reason": reason,
        "phase": "after",
        "guard_ids": list(matched),
        "matched_paths": {guard_id: list(hits) for guard_id, hits in matched.items()},
        "guards": [rule.to_payload() for rule in active],
        "observed_paths": list(changed_paths),
        # The write guard's own before/after vectors for exactly the
        # repositories that hold a matched path: the operator's next decision
        # is "restore or accept", and that needs the content evidence, not just
        # the names.
        "evidence": before_after,
        "coverage": {rule.id: coverage_by_id.get(rule.id) for rule in active},
        "retained": True,
        "detail": unavailable_detail,
    }

    if adjudicate_only:
        guard_record["adjudicated_only"] = True
        # The turn is already ending, and whichever outcome got here first
        # already withdrew the session if it needed withdrawing. Reverting
        # again would append a second failure note for one turn.
        guard_record["reverted_to_pending"] = False
        primary_reason = payload.get("pause_reason")
        if payload.get("paused") and isinstance(primary_reason, str):
            guard_record["deferred_to_pause_reason"] = primary_reason
        with persistence.lock:
            turn_payload["protected_path_guard"] = guard_record
            _write_json_file(paths["result"], turn_payload)
            # The primary pause was published before this verdict existed, so
            # its live details are amended in place -- ``payload["details"]``
            # is the same object -- rather than republished under a reason
            # that would displace the one an operator has to act on.
            pause_details = payload.get("pause_details")
            if payload.get("paused") and isinstance(pause_details, dict):
                pause_details["protected_path_guard"] = guard_record
        _record_turn_rejected(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="protected-path-guard",
            reason=reason,
            reopened=False,
            detail={
                "adjudicated_only": True,
                "guard_ids": list(matched),
                "matched_paths": {
                    guard_id: list(hits) for guard_id, hits in matched.items()
                },
            },
        )
        return False

    revert_target = after if after is not None else before
    reverted = handoff_guards._revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=_append_handoff_note,
    )
    inspected = _reinspect_after_revert(
        before.plan_dir, revert_target, reverted, plan_cache
    )
    guard_record["reverted_to_pending"] = reverted is not None
    with persistence.lock:
        payload["final_snapshot"] = _snapshot_payload(inspected)
        turn_payload["protected_path_guard"] = guard_record
        _write_json_file(paths["result"], turn_payload)
    _record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="protected-path-guard",
        reason=reason,
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=message,
        details={**session_details, "protected_path_guard": guard_record},
        persistence=persistence,
    )
    return True


def _enforce_review_verdict(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    require_artifact: bool = False,
    stop_after_current_pending: bool = False,
) -> tuple[str, plans.PlanSnapshot | None]:
    """Validate and persist the final review verdict.

    ``require_artifact=True`` (review sessions at or above
    :data:`STRUCTURED_REVIEW_MIN_VERSION`) additionally requires a valid,
    consistent ``final-review.json``; v1 review sessions stay
    Markdown-compatible.

    Returns ``("pass", None)`` for a valid verdict. An invalid verdict always
    reverts the review session to ``pending`` with the exact validation error
    as a Handoff note, then either returns ``("retry", snapshot)`` — the caller
    adopts the snapshot and ``continue``s, so the main loop re-selects and
    re-runs the same review session — or ``("pause", snapshot)`` once the
    repair is exhausted or ``stop_after_current_pending`` says an operator
    stop must win over another turn (the caller must ``break``).
    """
    review_path = review_session_contract.final_review_output_path(before.plan_dir)
    verdict = handoff_guards._read_review_verdict(
        before.plan_dir, require_artifact=require_artifact
    )
    cursor = review_transitions.REVIEW_VERDICT_MACHINE.start()
    # Fingerprint this rejection and compare against the retries already
    # recorded for this session in the current review cycle (read before the
    # new retry event is appended). An identical rejection cannot loop, and the
    # attempt cap bounds a reviewer that keeps producing *different* malformed
    # artifacts.
    graded = review_transitions.adjudicate_review_verdict(
        verdict=verdict,
        priors=_read_prior_review_verdict_retries(event_path, session_id),
        stop_after_current_pending=stop_after_current_pending,
    )
    cursor.advance(graded.event)
    if graded.accepted:
        with persistence.lock:
            turn_payload["review_verdict"] = {
                "passed": True,
                **{key: value for key, value in verdict.items() if key != "ok"},
            }
            _write_json_file(paths["result"], turn_payload)
        return ("pass", None)

    error = verdict.get("error", "invalid review verdict")
    failure_note = f"review verdict validation failed: {error}"
    reverted = handoff_guards._revert_session_status_to_pending(
        after,
        session_id,
        failure_note=failure_note,
        append_handoff_note=_append_handoff_note,
    )
    snapshot_after = _reinspect_after_revert(
        before.plan_dir, after, reverted, plan_cache
    )

    repair_payload = graded.to_payload()
    with persistence.lock:
        payload["final_snapshot"] = _snapshot_payload(snapshot_after)
        payload["review_verdict"] = None
        turn_payload["review_verdict"] = {
            "passed": False,
            "reason": "invalid-review-verdict",
            **{
                key: value
                for key, value in verdict.items()
                if key not in ("ok", "reason")
            },
            "failure_reason": verdict.get("reason"),
            **repair_payload,
        }
        _write_json_file(paths["result"], turn_payload)
    _record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="review-verdict",
        reason="invalid-review-verdict",
        reopened=reverted is not None,
        detail={"failure_reason": verdict.get("reason"), "attempt": graded.attempt},
    )

    if graded.action == "retry":
        persistence.append_event(
            event_path,
            events.REVIEW_VERDICT_RETRY_EVENT,
            {
                "run_id": payload.get("run_id"),
                **_session_identity_payload(session_id),
                "attempt": graded.attempt,
                "max_attempts": graded.max_attempts,
                "fingerprint": graded.fingerprint,
                "failure_reason": verdict.get("reason"),
                "error": str(error),
            },
        )
        cursor.advance("resubmit")
        return ("retry", snapshot_after)

    details = {
        **session_details,
        "review_output_path": str(review_path),
        "error": str(error),
        "failure_reason": verdict.get("reason"),
        **repair_payload,
    }
    if "review_artifact_path" in verdict:
        details["review_artifact_path"] = verdict["review_artifact_path"]
    run_termination._pause_run(
        event_path,
        payload,
        reason="verify-failed",
        message=(f"review verdict validation failed for {session_label}: {error}"),
        details=details,
        persistence=persistence,
    )
    return ("pause", snapshot_after)


def _enforce_session_verification(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    progress: run_reporting._ProgressReporter,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
    cwd: Path,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    allow_not_loop_ready: bool,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    run_id: str | None,
    turn_index: int,
    total_sessions: int | None,
    heartbeat_interval: float = (
        live_verification.VERIFICATION_HEARTBEAT_INTERVAL_SECONDS
    ),
    verification_context: verification.VerificationContext | None = None,
    review_broad_failure_deferral: bool = False,
    require_review_artifact: bool = False,
    generated_review_session: bool = False,
) -> bool:
    """Run the session's `Verify` recipe and pause on failure or contract problems.

    The lane's states — scheduled, running, and the settled passed / deferred /
    failed / unenforced / refused — are declared in
    :data:`verification_transitions.VERIFICATION_MACHINE` and driven here beside
    the effects each one earns, so an out-of-order step raises rather than
    writing a durable record describing a lifecycle this session never had.

    Returns ``True`` when the run should pause (the caller must ``break``);
    ``False`` when verification passed and the loop should continue.
    """
    cursor = verification_transitions.VERIFICATION_MACHINE.start()
    live: live_verification._LiveVerification | None = None

    def refuse(
        refusal: verification_transitions.VerificationRefusal,
    ) -> bool:
        """Apply a refusal's effects: close the live record, revert, pause."""

        if live is not None and refusal.live_finish_reason is not None:
            live.finish(
                passed=False,
                passed_count=0,
                reason=refusal.live_finish_reason,
                error=refusal.error,
            )
        reverted = handoff_guards._revert_session_status_to_pending(
            after,
            session_id,
            failure_note=refusal.failure_note,
            append_handoff_note=_append_handoff_note,
        )
        snapshot_after = _reinspect_after_revert(
            before.plan_dir, after, reverted, plan_cache
        )
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot_after)
            turn_payload["verification"] = refusal.turn_verification(session_id)
            _write_json_file(paths["result"], turn_payload)
        _record_turn_rejected(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="session-verification",
            reason=refusal.reason,
            reopened=reverted is not None,
            detail=refusal.guard_detail,
        )
        run_termination._pause_run(
            event_path,
            payload,
            reason=refusal.reason,
            message=refusal.message,
            details=refusal.pause_details(session_details),
            persistence=persistence,
        )
        return True

    contract: contracts.CompletionContract | None
    contract_error: str | None = None
    try:
        contract = contracts.read_completion_contract(before.plan_dir, session_id)
    except plans.PlanError as error:
        contract, contract_error = None, str(error)

    schedule = verification_transitions.schedule_verification(
        contract=contract,
        contract_error=contract_error,
        allow_not_loop_ready=allow_not_loop_ready,
        session_label=session_label,
    )
    cursor.advance(schedule.event)
    if not schedule.runs:
        if schedule.refusal is not None:
            return refuse(schedule.refusal)
        # Waived: a loop-ready run accepted a session with no contract, so the
        # turn records that nothing was enforced and the loop continues.
        with persistence.lock:
            turn_payload["verification"] = schedule.turn_verification(session_id)
            _write_json_file(paths["result"], turn_payload)
        return False

    # `schedule.runs` is `state == "contract-ready"`, which
    # `schedule_verification` returns on exactly one branch: the one guarded by
    # `contract is not None`. The invariant lives across a module boundary, so
    # it is restated here to narrow the contract every call below requires.
    assert contract is not None

    # Persist `final_snapshot` to the post-turn inspected snapshot before
    # verification starts. Completion bookkeeping (progress.md append and
    # `completed_sessions`) still waits until verification passes, but the
    # snapshot itself is the post-turn view either way so operator tools see
    # the in-flight phase as "verifying" rather than "idle".
    with persistence.lock:
        payload["final_snapshot"] = _snapshot_payload(after)

    if generated_review_session:
        review_workspace = (
            verification_context.workspace if verification_context is not None else None
        )
        contract = review_session_contract._authenticated_generated_review_contract(
            contract,
            plan_dir=before.plan_dir,
            repository_aliases=(
                () if review_workspace is None else review_workspace.registry.aliases
            ),
        )

    contract_checks = contract.verify.checks
    check_count = len(contract_checks)
    live = live_verification._LiveVerification(
        event_path=event_path,
        payload=payload,
        progress=progress,
        persistence=persistence,
        run_id=run_id,
        turn_index=turn_index,
        session_id=session_id,
        session_label=session_label,
        total_sessions=total_sessions,
        check_count=check_count,
        check_names=tuple(check.name for check in contract_checks),
        check_kinds=tuple(check.kind for check in contract_checks),
        check_repos=tuple(check.repo for check in contract_checks),
        parallel=contract.verify.parallel,
        heartbeat_interval=heartbeat_interval,
    )

    def _on_check_started(event: verification.CheckStartedEvent) -> None:
        live.check_started(
            check_index=event.check_index,
            name=event.name,
            kind=event.kind,
            argv=event.argv,
            cwd=event.cwd,
            repo=event.repo,
        )

    def _on_check_finished(event: verification.CheckFinishedEvent) -> None:
        live.check_finished(
            check_index=event.check_index,
            name=event.name,
            kind=event.kind,
            role=event.role,
            status=event.result.status,
            duration_seconds=event.result.duration_seconds,
            failure_reason=event.result.failure_reason,
            returncode=event.result.returncode,
            repo=event.repo,
            failure_diagnostic=event.result.failure_diagnostic,
        )

    def _on_check_skipped(event: verification.CheckSkippedEvent) -> None:
        live.check_skipped(
            check_index=event.check_index,
            name=event.name,
            kind=event.kind,
            on_failure_of=event.on_failure_of,
            reason=event.reason,
            repo=event.repo,
        )

    observer = verification.VerificationObserver(
        on_check_started=_on_check_started,
        on_check_finished=_on_check_finished,
        on_check_skipped=_on_check_skipped,
    )

    # Retain failed command stdout/stderr under the turn's artifact directory so
    # large output stays recoverable off to the side while result.json keeps
    # only bounded previews.
    verification_artifact_dir = paths["artifact_dir"] / "verification"

    cursor.advance("start")
    live.start()
    try:
        verification_kwargs: dict[str, Any] = {
            "observer": observer,
            "artifact_dir": verification_artifact_dir,
        }
        if verification_context is None:
            verification_kwargs["repo_root"] = cwd
        else:
            verification_kwargs["context"] = verification_context
        result = verification.run_verification(contract, **verification_kwargs)
    except KeyboardInterrupt:
        cursor.advance("run-refused")
        return refuse(
            verification_transitions.refuse_interrupted_verification(
                session_label=session_label
            )
        )
    except OSError as error:
        cursor.advance("run-refused")
        return refuse(
            verification_transitions.refuse_unavailable_verification(
                session_label=session_label, error=str(error)
            )
        )

    graded = verification_transitions.adjudicate_verification(
        result=result,
        contract=contract,
        deferral=(
            review_delta._review_broad_failure_acceptance(
                result,
                plan_dir=before.plan_dir,
                require_artifact=require_review_artifact,
                read_review_verdict=handoff_guards._read_review_verdict,
            )
            if review_broad_failure_deferral and not result.passed
            else None
        ),
    )
    cursor.advance(graded.event)
    details = graded.details
    live.finish(
        passed=graded.passed,
        passed_count=graded.passed_count,
        skipped_count=graded.skipped_count,
        executed_count=graded.executed_count,
        gate_count=graded.gate_count,
        diagnostic_count=graded.diagnostic_count,
        reason=graded.reason,
        acceptance=graded.acceptance,
    )

    with persistence.lock:
        turn_payload["verification"] = graded.turn_verification(session_id)
        _write_json_file(paths["result"], turn_payload)
        for check in result.checks:
            run_persistence._append_process_sweep_event(
                event_path=event_path,
                persistence=persistence,
                report=check.sweep,
                phase="verification",
                identity={
                    "run_id": run_id,
                    "turn_index": turn_index,
                    **_session_identity_payload(session_id),
                    "check": check.name,
                },
            )

    if graded.continues:
        return False

    summary = str(graded.summary)
    reverted = handoff_guards._revert_session_status_to_pending(
        after,
        session_id,
        failure_note=f"verification failed: {summary}",
        append_handoff_note=_append_handoff_note,
    )
    snapshot_after = _reinspect_after_revert(
        before.plan_dir, after, reverted, plan_cache
    )

    # Fingerprint this failure and compare against prior verify-failed events
    # for the same session (read before this pause is appended). The repair is
    # exhausted when the fingerprint repeats a prior failure (an identical
    # failure cannot loop) or when the total number of verify-failures exceeds
    # the attempt cap. The autopilot honors ``verification_repair_exhausted``
    # and stops relaunching; a changed fingerprint under the cap stays eligible.
    repair = verification_transitions.adjudicate_verification_repair(
        details=details,
        budget=verification_transitions.verification_repair_budget(
            event_path, session_id
        ),
    )
    repair_payload = repair.to_payload()
    with persistence.lock:
        payload["final_snapshot"] = _snapshot_payload(snapshot_after)
        turn_payload["verification"].update(repair_payload)
        _write_json_file(paths["result"], turn_payload)
    _record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="session-verification",
        reason="verify-failed",
        reopened=reverted is not None,
        detail={"summary": summary, "fingerprint": repair.fingerprint},
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason="verify-failed",
        message=(f"verification failed for {session_label}: {summary}"),
        details={**session_details, **details, **repair_payload},
        persistence=persistence,
    )
    return True


def _reinspect_after_revert(
    plan_dir: Path,
    fallback: plans.PlanSnapshot,
    reverted_path: Path | None,
    plan_cache: plans.PlanSnapshotCache,
) -> plans.PlanSnapshot:
    if reverted_path is None:
        return fallback
    return _reinspect_after_plan_mutation(plan_cache, fallback)


def _reinspect_after_plan_mutation(
    plan_cache: plans.PlanSnapshotCache,
    fallback: plans.PlanSnapshot,
) -> plans.PlanSnapshot:
    """Re-read a plan the runner itself just rewrote.

    Reverting a session or materializing repair children changes the durable
    plan, so a projection still built from the pre-mutation snapshot describes
    a plan that no longer exists. The pre-mutation snapshot is kept only when
    the rewritten plan cannot be parsed at all, which is a separate failure the
    caller already reports.
    """
    try:
        return plan_cache.get_fresh(fallback.plan_dir)
    except plans.PlanError:
        return fallback


HANDOFF_DEFECT_MISSING_FILES = "missing-files-modified"
HANDOFF_DEFECT_PENDING_PLACEHOLDER = "pending-files-modified-placeholder"
HANDOFF_DEFECT_UNQUALIFIED_ENTRY = "unqualified-files-modified-entry"


@dataclass(frozen=True)
class _HandoffDefect:
    """A ``Files modified`` defect the worker can repair by rewriting one line."""

    code: str
    detail: str
    instruction: str


def _classify_mechanical_handoff_defect(
    snapshot: plans.PlanSnapshot,
    session_id: str,
    *,
    registry_aliases: Sequence[str],
) -> _HandoffDefect | None:
    """Name the ``Files modified`` defect that left a completed handoff unparsable.

    Reads the handoff with registry validation switched off, because the defect
    being classified is exactly what that validation refuses. Only shapes the
    worker can correct inside its own Handoff block are returned: an entry that
    parses but names a repository outside the session's Writes is a scope
    violation rather than a typo, and keeps the pause it earns.
    """
    session_status = next(
        (item for item in snapshot.sessions if item.id == session_id),
        None,
    )
    if session_status is None:
        return None
    try:
        handoff = plans.read_session_handoff(
            session_status.session, repository_registry=None
        )
    except plans.PlanError:
        return None
    if handoff.status != "complete":
        return None
    aliases = ", ".join(registry_aliases)
    inline = handoff.files_modified[0].strip() if handoff.files_modified else ""
    if not inline:
        return _HandoffDefect(
            code=HANDOFF_DEFECT_MISSING_FILES,
            detail="the completed handoff carries no Files modified entry",
            instruction=(
                "list every file you changed on the Files modified line as "
                f"comma-separated Alias:path entries (aliases: {aliases}), or "
                "write (none) if the session changed no files"
            ),
        )
    if inline == "(none yet)":
        return _HandoffDefect(
            code=HANDOFF_DEFECT_PENDING_PLACEHOLDER,
            detail="the completed handoff still reads (none yet)",
            instruction=(
                "replace (none yet) on the Files modified line with the "
                "Alias:path entries you actually changed "
                f"(aliases: {aliases}), or with (none)"
            ),
        )
    if inline == "(none)":
        return None
    for entry in (part.strip() for part in inline.split(",")):
        try:
            plans.parse_qualified_path(entry, registry_aliases)
        except ValueError as error:
            return _HandoffDefect(
                code=HANDOFF_DEFECT_UNQUALIFIED_ENTRY,
                detail=f"Files modified entry {entry!r} is invalid: {error}",
                instruction=(
                    f"rewrite the Files modified entry {entry!r} as "
                    f"Alias:path using one of the declared aliases ({aliases})"
                ),
            )
    return None


def _preserved_review_artifact_evidence(plan_dir: Path) -> dict[str, Any] | None:
    """Digest the review artifacts a malformed handoff says nothing about.

    ``final-review.md`` and ``final-review.json`` are separate files with their
    own validator, so a handoff that fails to parse is no reason to discard
    them. Recording their digests and verdict before the session is re-run
    makes the correction turn accountable for leaving them intact.
    """
    output_path = review.final_review_output_path(plan_dir)
    artifact_path = review.final_review_artifact_path(plan_dir)
    if not output_path.is_file() and not artifact_path.is_file():
        return None
    evidence: dict[str, Any] = {}
    if output_path.is_file():
        evidence["review_output_path"] = str(output_path)
        with contextlib.suppress(OSError):
            evidence["review_output_sha256"] = review.final_review_output_sha256(
                plan_dir
            )
    if artifact_path.is_file():
        evidence["review_artifact_path"] = str(artifact_path)
        with contextlib.suppress(OSError):
            evidence["review_artifact_sha256"] = review.final_review_artifact_sha256(
                plan_dir
            )
    verdict = review.load_review_verdict(plan_dir)
    evidence["review_verdict_ok"] = bool(verdict.get("ok"))
    if verdict.get("ok"):
        evidence["review_outcome"] = verdict.get("outcome")
        evidence["review_total_findings"] = verdict.get("total_findings")
    else:
        evidence["review_verdict_reason"] = verdict.get("reason")
    return evidence


def _handoff_correction_attempt(event_path: str | Path, session_id: str) -> int:
    """Return the 1-indexed attempt number the next correction would be."""
    try:
        text = Path(event_path).read_text(encoding="utf-8")
    except OSError:
        return 1
    priors = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("name") != events.TURN_HANDOFF_CORRECTION_EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        recorded = payload.get("session_id", payload.get("session"))
        if str(recorded) != str(session_id):
            continue
        priors += 1
    return priors + 1


def _handoff_correction_note(
    defect: _HandoffDefect, *, preserved_review: Mapping[str, Any] | None
) -> str:
    note = (
        f"handoff correction required: {defect.detail}. The plan could not be "
        f"inspected with this handoff in place. To fix it, {defect.instruction}. "
        "Change nothing else: the session's work is already done and its status "
        "was reverted to pending only so this line can be corrected."
    )
    if preserved_review is None:
        return note
    return (
        f"{note} The review artifacts in this plan directory are valid on their "
        "own terms and were preserved; leave them exactly as they are."
    )


def _correct_malformed_handoff(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    plan_cache: plans.PlanSnapshotCache,
    before: plans.PlanSnapshot,
    workspace: workspaces.ResolvedWorkspace | None,
    run_id: str,
    turn_index: int,
    session_id: str,
    plan_error: str | None,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
) -> tuple[plans.PlanSnapshot | None, dict[str, Any] | None]:
    """Give a mechanically malformed handoff one bounded chance to be corrected.

    Returns the re-inspected snapshot when a correction turn was armed, and the
    evidence describing the defect whenever one was recognized — including the
    exhausted case, so the pause that follows says what was tried instead of
    only that the plan would not parse.
    """
    if workspace is None:
        return None, None
    session_path = handoff_guards._session_path_in_snapshot(before, session_id)
    if session_path is None or plan_error is None:
        return None, None
    if session_path.name not in plan_error:
        return None, None
    defect = _classify_mechanical_handoff_defect(
        before, session_id, registry_aliases=workspace.registry.aliases
    )
    if defect is None:
        return None, None
    attempt = _handoff_correction_attempt(event_path, session_id)
    preserved_review = _preserved_review_artifact_evidence(before.plan_dir)
    evidence: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        **_session_identity_payload(session_id),
        "defect": defect.code,
        "detail": defect.detail,
        "plan_error": plan_error,
        "attempt": attempt,
        "max_attempts": MAX_HANDOFF_CORRECTION_ATTEMPTS,
        "session_path": str(session_path),
    }
    if preserved_review is not None:
        evidence["preserved_review_artifacts"] = preserved_review
    if attempt > MAX_HANDOFF_CORRECTION_ATTEMPTS:
        evidence["exhausted"] = True
        return None, evidence
    reverted = handoff_guards._revert_session_status_to_pending(
        before,
        session_id,
        failure_note=_handoff_correction_note(
            defect, preserved_review=preserved_review
        ),
        append_handoff_note=_append_handoff_note,
    )
    if reverted is None:
        evidence["exhausted"] = True
        return None, evidence
    try:
        corrected = plan_cache.get_fresh(before.plan_dir)
    except plans.PlanError as error:
        # The revert did not restore a readable plan, so something other than
        # this handoff is also broken and no correction turn can be launched.
        evidence["exhausted"] = True
        evidence["revert_plan_error"] = str(error)
        return None, evidence
    evidence["exhausted"] = False
    with persistence.lock:
        turn_payload["handoff_correction"] = evidence
        _write_json_file(paths["result"], turn_payload)
        payload["final_snapshot"] = _snapshot_payload(corrected)
        persistence.append_event(
            event_path, events.TURN_HANDOFF_CORRECTION_EVENT, evidence
        )
        persistence.write_state(payload)
    return corrected, evidence


def _uninspectable_handoff_note(plan_error: str) -> str:
    return (
        "the plan could not be inspected with this completed handoff in place: "
        f"{plan_error}. The status was reset to pending so the handoff can be "
        "rewritten; the session's work is already done, so repair the reported "
        "defect and change nothing else."
    )


def _reopen_uninspectable_handoff(
    *,
    plan_cache: plans.PlanSnapshotCache,
    before: plans.PlanSnapshot,
    session_id: str,
    plan_error: str | None,
    correction_exhausted: bool,
) -> tuple[plans.PlanSnapshot | None, dict[str, Any] | None]:
    """Return a completed handoff the plan cannot be read with to pending.

    The bounded correction turn covers the ``Files modified`` shapes a worker
    can be told how to fix. Everything else used to pause on a plan still
    claiming the session is complete, which leaves nothing a relaunch could
    pick up. Resetting the pre-turn session identity to pending and recording
    the parse failure as a repair note leaves a plan that parses again and a
    session a later turn can rewrite, so the pause reports whether the defect
    is repairable rather than only that the plan would not parse.
    """
    session_path = handoff_guards._session_path_in_snapshot(before, session_id)
    if session_path is None or plan_error is None:
        return None, None
    if session_path.name not in plan_error:
        # The plan is unreadable for a reason this session's handoff does not
        # own, so reopening it repairs nothing.
        return None, None
    session_status = next(
        (item for item in before.sessions if item.id == session_id),
        None,
    )
    if session_status is None:
        return None, None
    try:
        handoff = plans.read_session_handoff(
            session_status.session, repository_registry=None
        )
    except plans.PlanError:
        return None, None
    if handoff.status != "complete":
        return None, None
    evidence: dict[str, Any] = {
        **_session_identity_payload(session_id),
        "session_path": str(session_path),
        "plan_error": plan_error,
        "reopened": False,
        "repairable": False,
    }
    reverted = handoff_guards._revert_session_status_to_pending(
        before,
        session_id,
        failure_note=_uninspectable_handoff_note(plan_error),
        append_handoff_note=_append_handoff_note,
    )
    if reverted is None:
        return None, evidence
    evidence["reopened"] = True
    try:
        reinspected = plan_cache.get_fresh(before.plan_dir)
    except plans.PlanError as error:
        evidence["reinspect_error"] = str(error)
        return None, evidence
    # A defect the bounded correction already failed to fix once is reopened
    # for the operator's benefit, not as a promise that a rerun would converge.
    evidence["repairable"] = not correction_exhausted
    return reinspected, evidence


def _paused_handoff_status_note(pause_reason: str) -> str:
    return (
        f"the runner recorded this turn's structured pause ({pause_reason}) and "
        "reset the handoff status to pending, because a session that stopped "
        "without finishing is pending; there is no paused status value. Leave "
        "the status pending and answer the pause instead."
    )


def _repair_paused_handoff_status(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    plan_cache: plans.PlanSnapshotCache,
    after: plans.PlanSnapshot,
    session_id: str,
    turn_index: int,
    runner_pause: object,
) -> tuple[plans.PlanSnapshot, dict[str, Any] | None]:
    """Repair the selected session's handoff when its turn paused on purpose.

    A worker that emits a structured ``RUNNER_PAUSE`` and also writes a status
    the plan vocabulary has no value for (``paused``) is judged by the generic
    invalid-status branch first, which terminates the run as ``unknown-status``
    and discards the reason the worker gave. The status line is the only thing
    wrong, so it is returned to pending here and the structured pause keeps the
    run's cause. ``unknown-status`` stays the fallback: a pause message naming
    no known reason, and any invalid status outside the selected session, are
    both left to it.
    """
    if runner_pause is None:
        return after, None
    pause_reason = _runner_pause_reason(str(runner_pause))
    if pause_reason == "runner-pause":
        return after, None
    invalid = run_inputs._invalid_status_pause(after)
    if invalid is None:
        return after, None
    sessions = invalid[2].get("sessions") or []
    if len(sessions) != 1 or sessions[0].get("id") != session_id:
        return after, None
    from_status = sessions[0].get("raw_value")
    reverted = handoff_guards._revert_session_status_to_pending(
        after,
        session_id,
        failure_note=_paused_handoff_status_note(pause_reason),
        append_handoff_note=_append_handoff_note,
    )
    if reverted is None:
        return after, None
    repaired = _reinspect_after_revert(after.plan_dir, after, reverted, plan_cache)
    evidence: dict[str, Any] = {
        **_session_identity_payload(session_id),
        "session_path": str(reverted),
        "from_status": from_status,
        "to_status": "pending",
        "pause_reason": pause_reason,
    }
    persistence.append_event(
        event_path,
        events.SESSION_REOPENED_EVENT,
        {
            "run_id": payload.get("run_id"),
            "turn_index": turn_index,
            **_session_identity_payload(session_id),
            "from_status": from_status,
            "to_status": "pending",
            "guard": "plan-status-validity",
            "reason": pause_reason,
            **_reopened_snapshot_identity(_snapshot_payload(repaired)),
        },
    )
    return repaired, evidence


def _append_handoff_note(
    lines: list[str],
    *,
    handoff_index: int,
    section_end: int,
    note: str,
) -> list[str]:
    new_bullet = f"  - {note}"
    notes_index: int | None = None
    for index in range(handoff_index + 1, section_end):
        if lines[index].lstrip().startswith("- **Notes**"):
            notes_index = index
            break
    if notes_index is None:
        insert_at = section_end
        while insert_at - 1 > handoff_index and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        return lines[:insert_at] + ["- **Notes**:", new_bullet] + lines[insert_at:]
    insert_at = notes_index + 1
    while insert_at < section_end:
        candidate = lines[insert_at]
        stripped = candidate.lstrip()
        leading = candidate[: len(candidate) - len(stripped)]
        if len(leading) >= 2 and stripped.startswith("-"):
            insert_at += 1
            continue
        break
    return lines[:insert_at] + [new_bullet] + lines[insert_at:]


def _run_config_payload(
    args: argparse.Namespace,
    *,
    cwd: Path,
    commit_mode: str | None,
    verification_context: verification.VerificationContext | None = None,
) -> dict[str, Any]:
    """Snapshot the resolved run flags so ``asl run --resume`` can rehydrate.

    Keys mirror ``RUN_FLAG_SPEC`` dests one-to-one so the resume overlay in
    :func:`_resume_config_overlay` can seed a namespace by attribute name
    without any translation. This is the additive ``run_config`` block persisted
    at the top level of ``state.json``; it captures the flags that were
    previously invisible to a paused-run resume (``timeout``, ``max_sessions``,
    the resolved ``unsafe_bypass``, and the ``state_dir`` override) alongside the
    harness/model/effort selection that a default resume would otherwise reset
    to the ``codex`` defaults.
    """
    context = verification_context or verification.VerificationContext.legacy(cwd)
    return {
        "harness": args.harness,
        "harness_model": getattr(args, "harness_model", None),
        "spark": bool(getattr(args, "spark", False)),
        "review_harness": getattr(args, "review_harness", None),
        "review_harness_model": getattr(args, "review_harness_model", None),
        "effort": getattr(args, "effort", None),
        "timeout": getattr(args, "timeout", None),
        "max_sessions": getattr(args, "max_sessions", None),
        "max_pending_sessions": getattr(args, "max_pending_sessions", None),
        "max_repair_group_attempts": run_inputs._max_repair_group_attempts(args),
        "max_review_fix_cycles": run_inputs._max_review_fix_cycles(args),
        "max_review_fix_cycles_ceiling": run_inputs._max_review_fix_cycles_ceiling(
            args
        ),
        "review_guard_exclude": list(review_guard._review_guard_excludes(args)),
        "review_guard_include_ignored": review_guard._review_guard_include_ignored(
            args
        ),
        "commit_mode": commit_mode,
        "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
        "unsafe_bypass": run_selection._unsafe_bypass(args),
        "no_mailbox": run_selection._no_mailbox(args),
        "allow_not_loop_ready": readiness_gate._allow_not_loop_ready(args),
        "note": getattr(args, "note", None),
        "cwd": str(cwd),
        "state_dir_override": getattr(args, "state_dir", None),
        **supervisor_audit.config_from_namespace(args).to_run_config(),
        **run_prompts._workspace_payload_fields(context),
    }


def _initial_payload(
    *,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
    state_dir: Path,
    run_dir: Path,
    event_path: Path,
    state_path: Path,
    lock_info: state.LockInfo,
    cwd: Path,
    run_id: str,
    commit_mode: str | None,
    mailbox_activation: Mapping[str, Any],
    verification_context: verification.VerificationContext | None = None,
) -> dict[str, Any]:
    context = verification_context or verification.VerificationContext.legacy(cwd)
    model_effort = run_selection._selected_model_effort_payload(args)
    spark = run_selection._safe_initial_spark_payload(args)
    review_harness = getattr(args, "review_harness", None)
    review_requested = review_harness is not None
    review_output_path = (
        str(review_session_contract.final_review_output_path(snapshot.plan_dir))
        if review_requested
        else None
    )
    # A dry run never becomes audit-eligible: eligibility starts with the run ID
    # and run directory of a loop that actually executes.
    assurance = supervisor_audit.initial_summary(
        supervisor_audit.plan_audit(args),
        run_id=run_id,
        eligible=not args.dry_run,
    )
    return responses.success_payload(
        "run",
        paused=False,
        details={},
        dry_run=args.dry_run,
        reason="",
        pause_reason=None,
        pause_message="",
        pause_details={},
        run_id=run_id,
        run_started_at=_isoformat(_utc_now()),
        run_finished_at=None,
        agent_mux_sequence=0,
        # The lifecycle sequence counts notifications; the projection
        # generation counts durable transitions. Seeded separately and side by
        # side so neither is ever read as a stand-in for the other.
        **{agent_mux_projection.GENERATION_KEY: 0},
        duration_seconds=None,
        active_turn=None,
        active_verification=None,
        action_required=None,
        approval_mode=run_selection._approval_mode(args),
        commit_mode=commit_mode,
        repair_commit_mode=run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
        max_pending_sessions=getattr(args, "max_pending_sessions", None),
        note=getattr(args, "note", None),
        run_config=_run_config_payload(
            args,
            cwd=cwd,
            commit_mode=commit_mode,
            verification_context=context,
        ),
        assurance=assurance.to_dict(),
        no_mailbox=run_selection._no_mailbox(args),
        mailbox_enabled=bool(mailbox_activation.get("enabled")),
        mailbox_activation=dict(mailbox_activation),
        plan_dir=str(snapshot.plan_dir),
        cwd=str(cwd),
        kitty_window_id=run_selection._kitty_window_id_from_env(),
        harness=args.harness,
        spark=spark,
        **model_effort,
        **_observed_model_payload(None),
        model_projection=[],
        state_dir=str(state_dir),
        run_dir=str(run_dir),
        event_path=str(event_path),
        events_path=str(event_path),
        state_path=str(state_path),
        lock_path=str(lock_info.path),
        harness_config=run_selection._safe_harness_config_payload(),
        completed_sessions=[],
        completed_session_ids=[],
        commits=[],
        commit_groups=[],
        turns=[],
        control=_initial_control_payload(),
        initial_snapshot=_snapshot_payload(snapshot),
        final_snapshot=_snapshot_payload(snapshot),
        allow_not_loop_ready=readiness_gate._allow_not_loop_ready(args),
        readiness_gate=None,
        readiness_state_initial=None,
        readiness_state_final=None,
        review_requested=review_requested,
        review_harness=review_harness,
        review_session_id=None,
        review_session_executed=None,
        review_output_path=review_output_path,
        review_completed=False,
        review_verdict=None,
        review_skipped_reason=None,
        max_review_fix_cycles=run_inputs._max_review_fix_cycles(args),
        review_cycles=[],
        review_fix_disposition=None,
        review_resume=None,
        review_reopen=None,
        **run_prompts._workspace_payload_fields(context),
    )


def _dry_run_payload(
    *,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
    cwd: Path,
    run_dir: Path,
    mailbox_activation: Mapping[str, Any],
    reply_text: str | None = None,
    reply_context: qna.QnAContext | None = None,
    reply_origin: str = run_prompts.REPLY_ORIGIN_ARGV,
    verification_context: verification.VerificationContext | None = None,
) -> dict[str, Any]:
    context = verification_context or verification.VerificationContext.legacy(cwd)
    review_harness = getattr(args, "review_harness", None)
    review_requested = review_harness is not None
    review_output_path = (
        str(review_session_contract.final_review_output_path(snapshot.plan_dir))
        if review_requested
        else None
    )
    next_pending = snapshot.next_pending
    mailbox_enabled = bool(mailbox_activation.get("enabled"))
    pending_guidance = run_selection._pending_guidance_snapshot(
        enabled=mailbox_enabled,
        plan_dir=snapshot.plan_dir,
        state_dir_arg=getattr(args, "state_dir", None),
    )
    audit = supervisor_audit.plan_audit(args).to_dict()
    if next_pending is None:
        model_effort = run_selection._selected_model_effort_payload(args)
        spark = run_selection._safe_initial_spark_payload(args)
        return {
            "next_pending": None,
            "next_pending_id": None,
            "prompt": None,
            "invocation": None,
            "resolver_prompt": None,
            "prompt_kind": "session",
            "reply_source": None,
            "audit": audit,
            "no_mailbox": run_selection._no_mailbox(args),
            "mailbox_enabled": mailbox_enabled,
            "mailbox_activation": dict(mailbox_activation),
            "review_requested": review_requested,
            "review_harness": review_harness,
            "review_output_path": review_output_path,
            "spark": spark,
            **model_effort,
            **run_prompts._workspace_payload_fields(context),
        }

    is_review_session = review_session_contract.is_generated_review_session_file(
        next_pending.path
    )
    turn_harness = args.review_harness if is_review_session else args.harness
    if turn_harness is None:
        raise RunError("--review-harness is required for a review session")
    spark = run_selection._safe_initial_spark_payload(args)
    requested_model, model_effort, spark_route = run_selection._spark_turn_route(
        args,
        plan_dir=snapshot.plan_dir,
        session_path=next_pending.path,
        session_id=next_pending.id,
        harness=turn_harness,
        is_review_turn=is_review_session,
        implementation_overrides={},
        spark_state=spark,
        run_suppression=None,
    )
    spark["active_route"] = spark_route
    paths = _turn_paths(run_dir, turn_index=1, session_id=next_pending.id)
    workspace = context.workspace
    session_scope = (
        run_prompts._session_scope(next_pending, workspace)
        if workspace is not None
        else None
    )
    use_registered_packet = (
        workspace is not None
        and task_packet._worker_mode() == task_packet.WORKER_MODE_PACKET
    )
    packet_kwargs: dict[str, Any] = {}
    if use_registered_packet:
        dry_run_event_path = (
            state.plan_state_dir(snapshot.plan_dir, getattr(args, "state_dir", None))
            / events.EVENTS_FILE_NAME
        )
        repair_references = review_delta._verification_repair_state(
            dry_run_event_path,
            next_pending.id,
        )["references"]
        if is_review_session:
            repair_references = repair_references + (
                _review_verdict_repair_references(dry_run_event_path, next_pending.id)
            )
        packet_kwargs = {
            "session_id": next_pending.id,
            "session_kind": (
                SESSION_KIND_REVIEW
                if is_review_session
                else SESSION_KIND_IMPLEMENTATION
            ),
            "predecessor_id": task_packet._predecessor_session_id(
                snapshot, next_pending.id
            ),
            "has_expansion": task_packet._session_has_expansion(
                snapshot.plan_dir, next_pending.id
            ),
            "has_progress": (snapshot.plan_dir / plans.PROGRESS_FILE_NAME).is_file(),
            "repair_references": repair_references,
            "mailbox_enabled": mailbox_enabled,
            "state_dir": getattr(args, "state_dir", None),
            "pending_guidance": pending_guidance,
            "workspace": workspace,
            "session_scope": session_scope,
        }
    if reply_context is not None and reply_text is not None:
        if use_registered_packet:
            prompt = task_packet.build_task_packet(
                turn_harness,
                snapshot.plan_dir,
                reply=reply_text,
                reply_context=reply_context,
                **packet_kwargs,
            ).render()
        else:
            prompt = run_prompts.build_reply_prompt(
                turn_harness,
                snapshot.plan_dir,
                reply=reply_text,
                context=reply_context,
                mailbox_enabled=mailbox_enabled,
                state_dir=getattr(args, "state_dir", None),
                pending_guidance=pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            )
        prompt_kind = "reply"
        resolver_prompt: str | None = qna.render_resolver_prompt(reply_context)
        reply_source: dict[str, Any] | None = {
            "run_id": reply_context.run_id,
            "session_id": reply_context.session_id,
            "turn_index": reply_context.turn_index,
            "last_message_path": str(reply_context.last_message_path),
            "reply_length": len(reply_text),
            "origin": reply_origin,
        }
    else:
        if use_registered_packet:
            prompt = task_packet.build_task_packet(
                turn_harness,
                snapshot.plan_dir,
                **packet_kwargs,
            ).render()
        else:
            prompt = run_prompts.build_session_prompt(
                turn_harness,
                snapshot.plan_dir,
                mailbox_enabled=mailbox_enabled,
                state_dir=getattr(args, "state_dir", None),
                pending_guidance=pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            )
        prompt_kind = "session"
        resolver_prompt = None
        reply_source = None
    invocation = run_prompts.build_harness_invocation(
        turn_harness,
        cwd=cwd,
        plan_dir=snapshot.plan_dir,
        unsafe_bypass=run_selection._unsafe_bypass(args),
        last_message_path=paths["last_message"],
        model=requested_model,
        effort=args.effort,
        prompt=prompt,
    )
    return {
        "next_pending": _session_compat_value(next_pending.id),
        "next_pending_id": next_pending.id,
        "prompt": prompt,
        "invocation": run_commit._build_invocation_payload(
            invocation,
            model_effort=model_effort,
            verification_context=context,
        ),
        "resolver_prompt": resolver_prompt,
        "prompt_kind": prompt_kind,
        "reply_source": reply_source,
        "audit": audit,
        "no_mailbox": run_selection._no_mailbox(args),
        "mailbox_enabled": mailbox_enabled,
        "mailbox_activation": dict(mailbox_activation),
        "review_requested": review_requested,
        "review_harness": review_harness,
        "review_output_path": review_output_path,
        "spark": spark,
        "spark_route": spark_route,
        **model_effort,
        **run_prompts._workspace_payload_fields(context),
    }


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
        payload = _error_payload(
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
            payload = _error_payload(
                args,
                reason=error.reason,
                message=error.error,
                paused=False,
                cwd=cwd,
                snapshot=snapshot,
            )
            payload["details"] = dict(error.details)
            return 1, payload

    payload = _initial_payload(
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

    def finish_run_locked(
        exit_code: int,
        result_payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        reply_state_preserver.restore_if_unconsumed()
        return exit_code, result_payload

    persist_run_state = not (args.dry_run and reply_text is not None)
    persistence = run_persistence._RunPersistence(
        write_state=persist_run_state,
        append_events=persist_run_state,
    )
    output = run_reporting._StderrOutputCoordinator(enabled=not args.json)
    # The reporter renders commit failures but owns none of the commit
    # lifecycle's vocabulary, so the run hands it the key terminalization
    # writes owed groups under and the labeller that names one.
    progress = run_reporting._ProgressReporter(
        enabled=not args.json,
        output=output,
        commit_group_label=commit_settlement._unresolved_commit_group_label,
        unresolved_groups_key=commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY,
    )
    inline_tail = run_reporting._InlineTailController(
        plan_dir=snapshot.plan_dir,
        state_dir=args.state_dir,
        output=output,
    )

    def terminalize(
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
        with persistence.lock:
            payload[commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY] = (
                commit_settlement._unresolved_commit_groups(payload)
            )
        return finish_run_locked(
            run_termination._terminalize_run(
                exit_code=exit_code,
                event_path=event_path,
                payload=payload,
                write_readiness_state=readiness_gate.write_readiness_state,
                progress=progress,
                persistence=persistence,
                audit_context=audit_context,
                cancel=cancel,
                poll=poll,
            ),
            payload,
        )

    #: The group this run still owes, carried between commit boundaries. A
    #: commit failure soft-fails, so this is what makes the failure retryable
    #: inside the same run instead of only after a restart.
    owed_commit_group: dict[str, Any] | None = None
    #: Retries already spent per group, so a repository that fails the same way
    #: every time cannot turn the run loop into a commit loop.
    owed_commit_group_attempts: dict[str, int] = {}

    def soft_fail_commit(
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
        nonlocal owed_commit_group
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
            owed_commit_group_attempts.get(group_id, 0)
            if isinstance(group_id, str)
            else commit_settlement.MAX_COMMIT_GROUP_RETRIES
        )
        retrying = (
            group is not None and spent < commit_settlement.MAX_COMMIT_GROUP_RETRIES
        )
        record["retrying"] = retrying
        with persistence.lock:
            commit_settlement._append_commit_failure(payload, record)
            persistence.write_state(payload)
        progress.commit_failed(record)
        if not retrying:
            owed_commit_group = None
            return
        owed_commit_group_attempts[str(group_id)] = spent + 1
        owed_commit_group = dict(group) if group is not None else None

    def settle_owed_commit_group() -> None:
        """Complete the group this run still owes before it commits anything else.

        This is the restart invariant applied inside a single run: an
        unresolved group is finished at the next commit boundary, ahead of the
        next worker and ahead of the end-of-run commit, so a transient failure
        heals without a human. A second failure re-arms the same retry rather
        than escalating.
        """
        nonlocal owed_commit_group
        group = owed_commit_group
        if group is None:
            return
        owed_commit_group = None
        session_id = (
            str(group["session_id"]) if group.get("session_id") is not None else None
        )
        recovered_group: dict[str, Any] = dict(group)
        failed_commit: dict[str, Any] | None = None
        recovery_error: str | None = None
        try:
            recovered_group, failed_commit = commit_settlement._run_commit_group(
                event_path=event_path,
                payload=payload,
                progress=progress,
                verification_context=verification_context,
                run_dir=run_dir,
                run_id=run_id,
                session_id=session_id,
                targets=commit_settlement._resolve_recovered_commit_targets(
                    group, verification_context
                ),
                generation=commit_generation,
                recovered_group=group,
                persistence=persistence,
            )
        except (RunError, commit_groups.CommitGroupError) as error:
            recovery_error = str(error)
        resolved = recovery_error is None and failed_commit is None
        with persistence.lock:
            payload["commit_group_recovery"] = {
                "group_id": group.get("group_id"),
                "success": resolved,
                "error": recovery_error,
                "group": recovered_group,
            }
            persistence.write_state(payload)
        if resolved:
            progress.commit_group_recovered(recovered_group)
            return
        soft_fail_commit(
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

    persistence.append_event(
        event_path,
        events.RUN_STARTED_EVENT,
        {
            "plan_dir": str(snapshot.plan_dir),
            "harness": args.harness,
            "model": payload["model"],
            "model_source": payload["model_source"],
            "effort": payload["effort"],
            "effort_source": payload["effort_source"],
            "spark": payload["spark"],
            "dry_run": args.dry_run,
            "max_sessions": max_sessions,
            "max_pending_sessions": getattr(args, "max_pending_sessions", None),
            "commit_mode": commit_mode,
            "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            "note": payload.get("note"),
            "run_id": run_id,
            "run_dir": str(run_dir),
            "run_started_at": payload["run_started_at"],
            "approval_mode": payload["approval_mode"],
            "no_mailbox": run_selection._no_mailbox(args),
            "mailbox_enabled": mailbox_enabled,
            "counts": payload["initial_snapshot"]["counts"],
            "next_pending": payload["initial_snapshot"]["next_pending"],
            **(
                {
                    "cwd": str(cwd),
                    **run_prompts._workspace_payload_fields(verification_context),
                }
                if verification_context.workspace is not None
                else {}
            ),
        },
    )
    persistence.write_state(payload)
    run_persistence._notify_agent_mux_loop_started(payload, persistence)
    progress.run_started(payload)
    progress.orphaned_state(orphan_candidates)

    invalid_pause = run_inputs._invalid_status_pause(snapshot)
    if invalid_pause is not None:
        reason, message, details = invalid_pause
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        run_termination._pause_run(
            event_path,
            payload,
            reason=reason,
            message=message,
            details=details,
            persistence=persistence,
        )
        return terminalize(1)

    if commit_group_load_error is not None:
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        run_termination._pause_run(
            event_path,
            payload,
            reason="commit-failed",
            message="durable commit-group evidence could not be reconstructed",
            details={"error": commit_group_load_error},
            persistence=persistence,
        )
        return terminalize(1)

    if unresolved_commit_group is not None and args.dry_run:
        with persistence.lock:
            payload["commit_group_recovery"] = {
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
        owed_commit_group = dict(unresolved_commit_group)
        settle_owed_commit_group()

    review_requested = getattr(args, "review_harness", None) is not None
    max_review_fix_cycles = run_inputs._max_review_fix_cycles(args)
    review_resume: review_continuation.ReviewResume | None = None
    if review_requested:
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=True,
        )

    if snapshot.next_pending is None:
        if not run_inputs._all_sessions_complete(snapshot):
            run_termination._pause_run(
                event_path,
                payload,
                reason="no-pending-but-incomplete",
                message="no pending session was found, but not every session is "
                "complete",
                details={"snapshot": _snapshot_payload(snapshot)},
                persistence=persistence,
            )
            return terminalize(1)
        if not review_requested:
            with persistence.lock:
                payload["reason"] = "all-complete"
            return terminalize(0)

    if not review_requested:
        pending_review = review_session_body.find_pending_generated_review_session(
            snapshot.plan_dir
        )
        if pending_review is not None:
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(snapshot)
            run_termination._pause_run(
                event_path,
                payload,
                reason="review-harness-required",
                message=(
                    "a pending generated review session is present; "
                    "rerun with --review-harness to execute it"
                ),
                details={
                    "session_id": pending_review.session_id.value,
                    "session_path": str(pending_review.path),
                },
                persistence=persistence,
            )
            return terminalize(1)

    if args.dry_run:
        with persistence.lock:
            payload["reason"] = "dry-run"
            payload["planned"] = _dry_run_payload(
                args=args,
                snapshot=snapshot,
                cwd=cwd,
                run_dir=run_dir,
                mailbox_activation=mailbox_activation,
                reply_text=reply_text,
                reply_context=reply_context,
                reply_origin=reply_origin,
                verification_context=verification_context,
            )
        return terminalize(0)

    if (
        verification_context.workspace is not None
        and not MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED
    ):
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        run_termination._pause_run(
            event_path,
            payload,
            reason=MULTI_REPO_CAPABILITY_PAUSE_REASON,
            message=(
                "multi-repository harness turns are disabled until the "
                "authoritative workspace write guard is installed"
            ),
            details={
                "workspace_root": str(verification_context.root),
                "registry_digest": verification_context.registry_digest,
            },
            persistence=persistence,
        )
        return terminalize(1)

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
            event_path, review_resume.review_session_id
        )
    ):
        # The atomic child write can win a crash race against event append.
        # Generated children are authoritative.  This also recovers an
        # eventless findings review whose cycle record was lost, preserving
        # its fingerprints before the next review overwrites the artifact.
        recovered_record = review_cycle._review_reopen_record(
            event_path=event_path,
            payload=payload,
            resume=review_resume,
            max_review_fix_cycles=max_review_fix_cycles,
            materialized=True,
            repair_session_ids=review_resume.repair_session_ids,
            result="recovered-existing-children",
            recovered=True,
        )
        review_cycle._record_review_reopen(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            record=recovered_record,
        )
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=event_path,
            payload=payload,
            persistence=persistence,
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
    core_skipped = resume_report is not None
    if core_skipped:
        preflight_report = resume_report
        persistence.append_event(
            event_path,
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
    gate_payload = readiness_gate._readiness_gate_payload(preflight_report, cwd=cwd)
    if core_skipped:
        gate_payload["core_skipped"] = True
    gate_passed = gate_payload["ok"]
    review_only_complete_start = readiness_gate._review_only_complete_start(
        preflight_report,
        review_requested=review_requested,
        snapshot=snapshot,
    )
    with persistence.lock:
        payload["readiness_gate"] = gate_payload
        if preflight_report.readiness is not None:
            payload["readiness_state_initial"] = preflight_report.readiness.state

    initial_workspace_scope_repair = (
        workspace_evidence._latest_workspace_scope_repair_requirement(
            event_path, snapshot.next_pending.id
        )
        if verification_context.workspace is not None
        and snapshot.next_pending is not None
        else None
    )
    if initial_workspace_scope_repair is not None and not gate_passed:
        session_id = snapshot.next_pending.id
        details = {
            **_session_details_payload(session_id),
            "readiness_gate": gate_payload,
            "workspace_write_guard": initial_workspace_scope_repair,
        }
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        run_termination._pause_run(
            event_path,
            payload,
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            message=(
                f"{_session_label(session_id)} has retained changes to "
                "registered repositories outside its write aliases; repair "
                "its durable repository scope and rerun expansion plus "
                "preflight before continuing"
            ),
            details=details,
            persistence=persistence,
        )
        return terminalize(1)

    non_bypassable = readiness_gate._non_bypassable_blockers(preflight_report)
    if non_bypassable:
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        run_termination._pause_run(
            event_path,
            payload,
            reason="not-loop-ready",
            message=readiness_gate._readiness_gate_failure_message(preflight_report),
            details={**gate_payload, "blocker": list(non_bypassable)},
            persistence=persistence,
        )
        return terminalize(1)

    if not gate_passed and not allow_not_loop_ready and not review_only_complete_start:
        preflight_report, repair_payload = readiness_gate._repair_readiness_gate(
            preflight_report,
            plan_dir=snapshot.plan_dir,
            lock_info=lock_info,
            cwd=cwd,
            harness=args.harness,
            commit_mode=commit_mode,
            state_dir=args.state_dir,
            max_pending_sessions=max_pending_sessions,
            event_path=event_path,
            persistence=persistence,
            snapshot=snapshot,
            review_requested=review_requested,
            core_skipped=core_skipped,
        )
        core_skipped = bool(repair_payload["core_skipped"])
        gate_payload = readiness_gate._readiness_gate_payload(preflight_report, cwd=cwd)
        if core_skipped:
            gate_payload["core_skipped"] = True
        gate_passed = gate_payload["ok"]
        review_only_complete_start = readiness_gate._review_only_complete_start(
            preflight_report,
            review_requested=review_requested,
            snapshot=snapshot,
        )
        with persistence.lock:
            payload["readiness_gate"] = gate_payload
            payload["readiness_gate_repair"] = repair_payload

        if not gate_passed and not review_only_complete_start:
            gate_message = readiness_gate._readiness_gate_failure_message(
                preflight_report
            )
            with persistence.lock:
                payload["final_snapshot"] = _snapshot_payload(snapshot)
            run_termination._pause_run(
                event_path,
                payload,
                reason="not-loop-ready",
                message=gate_message,
                details=gate_payload,
                persistence=persistence,
            )
            return terminalize(1)

    try:
        if commit_mode == run_selection.COMMIT_MODE_END:
            review_guard._validate_commit_at_end_scope(
                snapshot=snapshot,
                max_sessions=max_sessions,
            )
        if commit_mode is not None:
            commit_preflight = review_guard._validate_commit_preflight(
                cwd,
                verification_context=verification_context,
            )
            with persistence.lock:
                payload["commit_preflight"] = commit_preflight
    except RunError as error:
        with persistence.lock:
            payload["final_snapshot"] = _snapshot_payload(snapshot)
        run_termination._pause_run(
            event_path,
            payload,
            reason="commit-preflight",
            message=str(error),
            details={
                "commit_mode": commit_mode,
                "max_sessions": max_sessions,
                "pending_sessions": _status_counts(snapshot)["pending"],
            },
            persistence=persistence,
        )
        return terminalize(1)

    should_transition = gate_passed or (
        allow_not_loop_ready and preflight_report.readiness is not None
    )
    if should_transition:
        running_success, running_error = readiness_gate.write_readiness_state(
            snapshot.plan_dir,
            "running",
            expected_sha256=preflight_report.expected_readiness_sha256,
        )
        with persistence.lock:
            if running_success:
                payload["readiness_state_active"] = True
                payload["readiness_state_current"] = "running"
            else:
                payload["readiness_state_active"] = False
                payload["readiness_state_write_error"] = running_error

    run_control_lock = persistence.lock
    run_control_context: dict[str, Any] | None = None
    control_payload = dict(payload["control"])

    def handle_run_control_action(action: run_control.RunControlAction) -> None:
        if action.action in ("consumed", "abort-consumed"):
            # Both consumptions are recorded by the site that acts on them, and
            # an abort's consumption has three distinct outcomes this callback
            # cannot tell apart.
            return
        if action.action == "tail-disabled":
            inline_tail.disable()
        with run_control_lock:
            context = None if run_control_context is None else dict(run_control_context)
            _record_run_control_action(
                event_path=event_path,
                payload=payload,
                control_payload=control_payload,
                action=action,
                context=context,
                persistence=persistence,
            )
        if action.action == "tail-enabled":
            inline_tail.enable()

    def handle_run_control_status(action: run_control.RunControlAction) -> None:
        with run_control_lock:
            context = None if run_control_context is None else dict(run_control_context)
            status_control = dict(control_payload)
            status_control["stop_after_current"] = action.stop_after_current
            status_control["tail_enabled"] = action.tail_enabled
            status_control["next_harness"] = action.next_harness
            status_payload = dict(payload)
            active_turn = payload.get("active_turn")
            if isinstance(active_turn, dict):
                status_payload["active_turn"] = dict(active_turn)
            elif context is not None:
                status_payload["active_turn"] = dict(context)
            turns = payload.get("turns")
            if isinstance(turns, list):
                status_payload["turns"] = [
                    dict(turn) if isinstance(turn, dict) else turn for turn in turns
                ]

        try:
            status_snapshot = plan_cache.get(snapshot.plan_dir)
            plan_error = None
        except plans.PlanError as error:
            status_snapshot = None
            plan_error = str(error)

        progress.status_snapshot(
            run_reporting._compact_status_lines(
                payload=status_payload,
                control=status_control,
                snapshot=status_snapshot,
                plan_error=plan_error,
            )
        )

    run_controls = run_control.RunControlListener.for_run(
        args,
        state=run_control.RunControlState(current_harness=args.harness),
        stdin=sys.stdin,
        stderr=output.stream,
        on_action=handle_run_control_action,
        on_status=handle_run_control_status,
    )
    run_controls.start()

    # Served unconditionally, unlike the keyboard listener: a `--json`
    # supervised run is precisely the case with no interactive control surface,
    # so it is the one that most needs an `asl control` command observed now
    # rather than at the end of the poll interval. The caller owns the endpoint
    # feeding this event; a run reached without one still polls.
    if wakeup is None:
        wakeup = threading.Event()

    completed_count = 0
    # Stamped when the runner stops working on a turn, not when the harness
    # exits: verification and finalization are runner-owned work, so counting
    # them as idle time would report the previous turn's own cost as a gap.
    runner_work_finished_at: datetime | None = None
    first_loop_pass = True
    idle_baseline_pinned = False
    commit_at_end_attempted = False
    review_appended = False
    last_session_details: dict[str, Any] = {}
    seen_external_command_ids: set[str] = set()

    def _expire_run_guards() -> None:
        """Disable this run's command gate on every exit path."""
        run_coordination.expire_run_guards(
            controller=guard_controller,
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            persistence=persistence,
        )

    mailbox_poll = _MailboxPoll(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=args.state_dir,
        control_state=run_controls.state,
        seen_control_ids=seen_external_command_ids,
        payload=payload,
        event_path=event_path,
        control_payload=control_payload,
        persistence=persistence,
        run_control_lock=run_control_lock,
        enabled=mailbox_enabled,
        wakeup=wakeup,
        guard_controller=guard_controller,
    )

    # Drain any commands that were queued before the run started so the
    # first turn already reflects them.
    _drain_external_control_queue(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=args.state_dir,
        control_state=run_controls.state,
        seen_ids=seen_external_command_ids,
        payload=payload,
        event_path=event_path,
        control_payload=control_payload,
        context=None,
        persistence=persistence,
        run_control_lock=run_control_lock,
        wakeup=wakeup,
        guard_controller=guard_controller,
    )
    pending_exception_guard: dict[str, Any] | None = None
    try:
        while True:
            if idle_baseline_pinned:
                # A path that reached here after deliberately establishing its
                # own baseline opts out by pinning it. Refreshing would
                # overwrite that earlier instant with this one and charge the
                # gap it was measuring to the runner.
                idle_baseline_pinned = False
            elif not first_loop_pass:
                # Every path back to the top got here by finishing
                # runner-owned work — a turn and its verification and
                # finalization, or the all-complete branch's final commit,
                # repair materialization, re-gating, readiness writes and
                # review creation. Refreshing the baseline in this one place
                # covers every path, including ones added later.
                runner_work_finished_at = _utc_now()
            entering_first_pass = first_loop_pass
            first_loop_pass = False
            if not entering_first_pass:
                # One place covers both boundaries a still-owed group must be
                # settled at: ahead of the next worker, and ahead of the
                # end-of-run commit the all-complete branch below runs. The
                # first pass is excluded because the recovery above the loop
                # is that pass's boundary and has just run.
                settle_owed_commit_group()
            pending_session = snapshot.next_pending

            if pending_session is None:
                if not run_inputs._all_sessions_complete(snapshot):
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(snapshot)
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason="no-pending-but-incomplete",
                        message=(
                            "no pending session was found, but not every "
                            "session is complete"
                        ),
                        details={"snapshot": _snapshot_payload(snapshot)},
                        persistence=persistence,
                    )
                    break

                if (
                    commit_mode == run_selection.COMMIT_MODE_END
                    and not commit_at_end_attempted
                ):
                    commit_at_end_attempted = True
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(snapshot)
                    try:
                        end_targets = commit_settlement._registered_end_commit_targets(
                            verification_context
                        )
                        commit_group, failed_commit = (
                            commit_settlement._run_commit_group(
                                event_path=event_path,
                                payload=payload,
                                progress=progress,
                                verification_context=verification_context,
                                run_dir=run_dir,
                                run_id=run_id,
                                session_id=None,
                                targets=end_targets,
                                generation=commit_generation,
                                persistence=persistence,
                            )
                        )
                    except RunError as error:
                        commit_group = None
                        failed_commit = None
                        commit_error = str(error)
                    else:
                        commit_error = None
                    if failed_commit is not None or commit_error is not None:
                        soft_fail_commit(
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

                if review_requested and not review_appended:
                    # The review and its repair children are work that starts
                    # *after* the session the operator asked to stop after, so
                    # the request has to be visible before either begins.
                    _drain_external_control_queue(
                        plan_dir=snapshot.plan_dir,
                        state_dir_arg=args.state_dir,
                        control_state=run_controls.state,
                        seen_ids=seen_external_command_ids,
                        payload=payload,
                        event_path=event_path,
                        control_payload=control_payload,
                        context=None,
                        persistence=persistence,
                        run_control_lock=run_control_lock,
                        wakeup=wakeup,
                        guard_controller=guard_controller,
                    )
                    with run_control_lock:
                        stop_before_review = run_controls.state.stop_after_current
                    review_resume = review_cycle._set_review_resume(
                        snapshot=snapshot,
                        event_path=event_path,
                        payload=payload,
                        persistence=persistence,
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
                        with persistence.lock:
                            review_cycle._set_existing_review_verdict(
                                payload,
                                snapshot.plan_dir,
                                read_review_verdict=handoff_guards._read_review_verdict,
                            )
                            payload["review_fix_disposition"] = (
                                review_resume.source_disposition
                                if terminal_disposition
                                else None
                            )
                            payload["final_snapshot"] = _snapshot_payload(snapshot)
                        if terminal_disposition:
                            with persistence.lock:
                                payload["reason"] = (
                                    review_cycle._terminal_review_reason(payload)
                                )
                        else:
                            blocker = review_resume.blocker or "unknown blocker"
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason=(events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR),
                                message=(
                                    "Recorded review repair history cannot be "
                                    f"resumed safely: {blocker}."
                                ),
                                details={"review_resume": review_resume.to_payload()},
                                persistence=persistence,
                            )
                        break

                    if review_resume.action == "materialize-repairs":
                        if not review_resume.eligible:
                            with persistence.lock:
                                review_cycle._set_existing_review_verdict(
                                    payload,
                                    snapshot.plan_dir,
                                    read_review_verdict=handoff_guards._read_review_verdict,
                                )
                                payload["review_fix_disposition"] = (
                                    review_resume.source_disposition
                                )
                                payload["final_snapshot"] = _snapshot_payload(snapshot)
                            if review_resume.blocker == "higher-cap-required":
                                with persistence.lock:
                                    payload["reason"] = (
                                        review_cycle._terminal_review_reason(payload)
                                    )
                                break

                            reopen_error = (
                                review_resume.artifact_error
                                or "review repair artifact is not materializable"
                            )
                            failed_record = review_cycle._review_reopen_record(
                                event_path=event_path,
                                payload=payload,
                                resume=review_resume,
                                max_review_fix_cycles=max_review_fix_cycles,
                                materialized=False,
                                result=(review_resume.blocker or "reopen-ineligible"),
                                error=reopen_error,
                            )
                            review_cycle._record_review_reopen(
                                event_path=event_path,
                                payload=payload,
                                persistence=persistence,
                                record=failed_record,
                            )
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason=(events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR),
                                message=(
                                    "review repair reopening failed for "
                                    f"session {review_resume.review_session_id}: "
                                    f"{reopen_error}"
                                ),
                                details={
                                    "review_resume": review_resume.to_payload(),
                                    "error": reopen_error,
                                },
                                persistence=persistence,
                            )
                            break

                        if stop_before_review:
                            run_termination._stop_before_review_work(
                                run_controls,
                                snapshot=snapshot,
                                payload=payload,
                                event_path=event_path,
                                control_payload=control_payload,
                                record_existing_review_verdict=(
                                    review_cycle.existing_review_verdict_recorder(
                                        handoff_guards._read_review_verdict
                                    )
                                ),
                                persistence=persistence,
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
                            event_path=event_path,
                            payload=payload,
                            resume=review_resume,
                            max_review_fix_cycles=max_review_fix_cycles,
                            materialized=materialized,
                            repair_session_ids=repair_session_ids,
                            result=repair_result.reason,
                            error=repair_result.error,
                        )
                        review_cycle._record_review_reopen(
                            event_path=event_path,
                            payload=payload,
                            persistence=persistence,
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
                            with persistence.lock:
                                payload["final_snapshot"] = _snapshot_payload(snapshot)
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason="not-loop-ready",
                                message=(
                                    "no review repair children survived "
                                    "materialization for session "
                                    f"{repair_result.review_session_id}: "
                                    f"{repair_result.error}"
                                ),
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
                                persistence=persistence,
                            )
                            break

                        if not repair_result.ok or not repair_session_ids:
                            reopen_error = (
                                repair_result.error
                                or "review repair reopening produced no children"
                            )
                            with persistence.lock:
                                payload["final_snapshot"] = _snapshot_payload(snapshot)
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason=(events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR),
                                message=(
                                    "review repair reopening failed for "
                                    f"session {review_resume.review_session_id}: "
                                    f"{reopen_error}"
                                ),
                                details={
                                    "review_resume": review_resume.to_payload(),
                                    "error": reopen_error,
                                    "detail": repair_result.detail,
                                },
                                persistence=persistence,
                            )
                            break

                        try:
                            snapshot = plan_cache.get_fresh(snapshot.plan_dir)
                        except plans.PlanError as error:
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason="malformed-plan",
                                message=(
                                    "plan could not be inspected after "
                                    f"reopening review repairs: {error}"
                                ),
                                details={"error": str(error)},
                                persistence=persistence,
                            )
                            break

                        review_resume = review_cycle._set_review_resume(
                            snapshot=snapshot,
                            event_path=event_path,
                            payload=payload,
                            persistence=persistence,
                            max_repair_group_attempts=(
                                run_inputs._max_repair_group_attempts(args)
                            ),
                            max_review_fix_cycles=max_review_fix_cycles,
                            review_requested=True,
                        )
                        with persistence.lock:
                            payload["review_fix_disposition"] = (
                                review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                            )
                            payload["final_snapshot"] = _snapshot_payload(snapshot)

                        if (
                            repair_result.reason
                            == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY
                            and not allow_not_loop_ready
                        ):
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason="not-loop-ready",
                                message=(
                                    "repair sessions were reopened from review "
                                    f"session {repair_result.review_session_id} "
                                    "but the plan could not be marked loop-ready"
                                ),
                                details={
                                    "review_resume": review_resume.to_payload(),
                                    "blockers": list(repair_result.blockers),
                                    "repair_session_ids": list(repair_session_ids),
                                },
                                persistence=persistence,
                            )
                            break

                        if not readiness_gate._regate_reopened_plan(
                            event_path=event_path,
                            payload=payload,
                            persistence=persistence,
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
                        with persistence.lock:
                            if running_success:
                                payload["readiness_state_active"] = True
                                payload["readiness_state_current"] = "running"
                                payload["readiness_state_refreshed_at"] = _isoformat(
                                    _utc_now()
                                )
                                payload["readiness_state_refresh_error"] = None
                            else:
                                payload["readiness_state_refresh_error"] = running_error
                            persistence.write_state(payload)
                        # Execute the exact children just materialized. Once
                        # they complete, the next all-complete pass evaluates
                        # to ``rereview`` and creates a fresh review.
                        continue

                    if stop_before_review:
                        run_termination._stop_before_review_work(
                            run_controls,
                            snapshot=snapshot,
                            payload=payload,
                            event_path=event_path,
                            control_payload=control_payload,
                            record_existing_review_verdict=(
                                review_cycle.existing_review_verdict_recorder(
                                    handoff_guards._read_review_verdict
                                )
                            ),
                            persistence=persistence,
                        )
                        break

                    review_workspace = verification_context.workspace
                    review_bases: list[dict[str, Any]] = []
                    if review_workspace is None:
                        review_scope_delta, review_scope_fallback = (
                            review_delta._compute_review_delta_context(
                                event_path=event_path,
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
                            with persistence.lock:
                                payload["final_snapshot"] = _snapshot_payload(snapshot)
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason=(events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE),
                                message=(
                                    "combined workspace review base could not "
                                    "be captured safely"
                                    f"{unavailable_summary}"
                                ),
                                details={
                                    "workspace_root": str(review_workspace.root),
                                    "unavailable": unavailable,
                                },
                                persistence=persistence,
                            )
                            break
                        try:
                            review_bases = workspace_evidence._workspace_review_bases(
                                review_workspace, review_base_snapshot
                            )
                        except RunError as error:
                            with persistence.lock:
                                payload["final_snapshot"] = _snapshot_payload(snapshot)
                            run_termination._pause_run(
                                event_path,
                                payload,
                                reason=(events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE),
                                message=str(error),
                                details={"error": str(error)},
                                persistence=persistence,
                            )
                            break
                        review_scope_delta, review_scope_fallback = (
                            review_delta._compute_registered_review_delta_context(
                                event_path=event_path,
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
                        with persistence.lock:
                            payload["final_snapshot"] = _snapshot_payload(snapshot)
                        run_termination._pause_run(
                            event_path,
                            payload,
                            reason="malformed-plan",
                            message=(
                                "could not create or reuse a generated review "
                                f"session: {error}"
                            ),
                            details={"error": str(error)},
                            persistence=persistence,
                        )
                        break
                    review_appended = True
                    with persistence.lock:
                        payload["review_session_id"] = review_file.session_id.value
                    if review_created:
                        persistence.append_event(
                            event_path,
                            events.REVIEW_SCOPE_EVENT,
                            {
                                "run_id": run_id,
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
                        persistence.append_event(
                            event_path,
                            events.REVIEW_SESSION_CREATED_EVENT,
                            {
                                "run_id": run_id,
                                "session_id": review_file.session_id.value,
                                "session": review_file.session_id.value,
                                "review_harness": args.review_harness,
                                "review_session_path": str(review_file.path),
                                "review_output_path": (
                                    payload.get("review_output_path")
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
                        snapshot = plan_cache.get_fresh(snapshot.plan_dir)
                    except plans.PlanError as error:
                        run_termination._pause_run(
                            event_path,
                            payload,
                            reason="malformed-plan",
                            message=(
                                "plan could not be inspected after appending the "
                                f"review session: {error}"
                            ),
                            details={"error": str(error)},
                            persistence=persistence,
                        )
                        break
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(snapshot)
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
                        event_path=event_path,
                        payload=payload,
                        persistence=persistence,
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
                    with persistence.lock:
                        if running_success:
                            payload["readiness_state_active"] = True
                            payload["readiness_state_current"] = "running"
                            payload["readiness_state_refreshed_at"] = _isoformat(
                                _utc_now()
                            )
                            payload["readiness_state_refresh_error"] = None
                        else:
                            payload["readiness_state_refresh_error"] = running_error
                        persistence.write_state(payload)
                    continue

                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(snapshot)
                    payload["reason"] = "all-complete"
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
                    review_workspace = verification_context.workspace
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
                        event_path=event_path,
                        plan_dir=snapshot.plan_dir,
                        cwd=cwd,
                        workspace=verification_context.workspace,
                        run_id=run_id,
                        persistence=persistence,
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
                            payload, snapshot.plan_dir, persistence
                        )
                        snapshot = plan_cache.get_fresh(snapshot.plan_dir)
                except (plans.PlanError, OSError) as error:
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(snapshot)
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason="malformed-plan",
                        message=(
                            "the pending review session's scope could not be "
                            f"re-derived from current Git evidence: {error}"
                        ),
                        details={
                            **_session_details_payload(pending_session.id),
                            "error": str(error),
                        },
                        persistence=persistence,
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
                and completed_count >= max_sessions
            ):
                with persistence.lock:
                    payload["reason"] = "max-sessions"
                break

            before = snapshot
            session_id = pending_session.id
            session_label = _session_label(session_id)
            session_identity = _session_identity_payload(session_id)
            session_details = _session_details_payload(session_id)
            last_session_details = session_details

            if is_review_session:
                with persistence.lock:
                    if payload.get("review_session_id") != session_id:
                        payload["review_session_id"] = session_id

            with persistence.lock:
                turn_index = len(payload["turns"]) + 1
                turns_already_run = bool(payload["turns"])
            paths = _turn_paths(
                run_dir,
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
                control_state=run_controls.state,
                seen_ids=seen_external_command_ids,
                payload=payload,
                event_path=event_path,
                control_payload=control_payload,
                context=pre_turn_control_context,
                persistence=persistence,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                guard_controller=guard_controller,
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
                    and run_controls.state.stop_after_current
                )
                if stop_before_turn:
                    run_termination._honor_stop_after_current(
                        run_controls,
                        snapshot=before,
                        payload=payload,
                        event_path=event_path,
                        control_payload=control_payload,
                        context=pre_turn_control_context,
                        persistence=persistence,
                    )
            if stop_before_turn:
                break
            if is_review_session:
                turn_harness = args.review_harness
            else:
                with run_control_lock:
                    turn_harness = run_selection._selected_future_harness(
                        args, control_payload
                    )
                    implementation_overrides = (
                        run_controls.state.implementation_overrides
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
                with persistence.lock:
                    payload["spark"].update(refreshed_availability.to_dict())
            requested_model, turn_model_effort, spark_route = (
                run_selection._spark_turn_route(
                    args,
                    plan_dir=before.plan_dir,
                    session_path=pending_session.path,
                    session_id=session_id,
                    harness=turn_harness,
                    is_review_turn=is_review_session,
                    implementation_overrides=implementation_overrides,
                    spark_state=payload["spark"],
                    run_suppression=spark_run_suppression,
                    fallback_reason=spark_fallback_reason,
                )
            )
            spark_fallback_reason = None
            with persistence.lock:
                payload["spark"]["active_route"] = spark_route
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
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(before)
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="harness-unavailable",
                    message=(f"harness unavailable before {session_label}: {message}"),
                    details=details,
                    persistence=persistence,
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
                event_path, session_id
            )["references"]
            if is_review_session:
                repair_references = repair_references + (
                    _review_verdict_repair_references(event_path, session_id)
                )
            pending_guidance = run_selection._pending_guidance_snapshot(
                enabled=mailbox_enabled,
                plan_dir=before.plan_dir,
                state_dir_arg=args.state_dir,
            )
            workspace = verification_context.workspace
            session_scope = (
                run_prompts._session_scope(pending_session, workspace)
                if workspace is not None
                else None
            )
            retained_scope_repair = (
                workspace_evidence._latest_workspace_scope_repair_requirement(
                    event_path, session_id
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
                            payload, before.plan_dir, persistence
                        )
                        session_scope = run_prompts._session_scope(
                            pending_session, workspace
                        )
                repair_resume = _pending_repair_child_continuation(
                    snapshot=before,
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
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
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(before)
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                        message=(
                            "generated repair child "
                            f"{session_label} cannot be run safely: "
                            f"{repair_blocker}."
                        ),
                        details={
                            **session_details,
                            "review_resume": repair_resume.to_payload(),
                        },
                        persistence=persistence,
                    )
                    break
                try:
                    repair_commit_preflight = review_guard._repair_commit_preflight(
                        cwd,
                        verification_context=verification_context,
                        session_scope=session_scope,
                        workspace_evidence=(
                            workspace_evidence._workspace_write_guard_evidence
                        ),
                        plan_dir=before.plan_dir,
                        retained_scope_repair=retained_scope_repair,
                    )
                except RepairCommitPreflightError as error:
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(before)
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason="commit-preflight",
                        message=(
                            f"repair commit preflight failed before "
                            f"{session_label}: {error}"
                        ),
                        details={**session_details, **error.details},
                        persistence=persistence,
                    )
                    break
                with persistence.lock:
                    payload.setdefault("repair_commit_preflights", []).append(
                        {
                            **session_identity,
                            **repair_commit_preflight,
                        }
                    )
                    persistence.append_event(
                        event_path,
                        events.REPAIR_COMMIT_PREFLIGHT_EVENT,
                        {
                            "run_id": run_id,
                            **session_identity,
                            **repair_commit_preflight,
                        },
                    )
                    persistence.write_state(payload)
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
                        before.plan_dir, args.state_dir, run_dir=run_dir
                    ),
                    provenance_roots=preflight.plan_provenance_exempt_roots(
                        before.plan_dir
                    ),
                )
            )
            if packet_intent_report is not None:
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(before)
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason=(events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED),
                    message=run_inputs._repository_intent_message(
                        packet_intent_report,
                        subject=f"task packet for {session_label}",
                    ),
                    details={
                        **session_details,
                        "repository_intent": packet_intent_report,
                    },
                    persistence=persistence,
                )
                break
            turn_started_at = _utc_now()
            turn_started_at_text = _isoformat(turn_started_at)
            idle_seconds_since_previous_turn = (
                0.0
                if runner_work_finished_at is None
                else _duration_seconds(runner_work_finished_at, turn_started_at)
            )
            total_sessions = sum(_status_counts(before).values())
            active_turn = {
                "run_id": run_id,
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
                        **run_prompts._workspace_payload_fields(verification_context),
                    }
                    if verification_context.workspace is not None
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
                    run_id=run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                )
            )
            mailbox_poll.set_live_delivery(delivery_endpoint)

            live_turn = run_commit._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=progress,
                active_turn=active_turn,
                persistence=persistence,
                poll_callback=mailbox_poll,
                poll_interval=LIVE_CONTROL_POLL_INTERVAL_SECONDS,
                wakeup=wakeup,
            )
            # One executor per turn: the identity and the artifact paths are
            # what make this turn this turn, so they are bound once here
            # rather than repeated at each of the three transitions below.
            turn_executor = turn_execution.TurnExecutor(
                identity=turn_execution.TurnIdentity(
                    run_id=run_id,
                    turn_index=turn_index,
                    session_id=session_id,
                ),
                paths=paths,
                event_path=event_path,
                persistence=persistence,
                progress=progress,
                plan_cache=plan_cache,
            )

            stream_callback = _live_stream_callback(log_stream_callback, live_turn)

            with run_control_lock:
                run_control_context = dict(active_turn)
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
                        before.plan_dir, args.state_dir, run_dir=run_dir
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
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(before)
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason=(events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE),
                        message=(
                            "workspace write guard unavailable before "
                            f"{session_label}; evidence for a registered "
                            "repository could not be captured safely"
                            f"{unavailable_summary}"
                        ),
                        details={
                            **session_details,
                            "workspace_write_guard": guard_record,
                        },
                        persistence=persistence,
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
                        with persistence.lock:
                            payload["final_snapshot"] = _snapshot_payload(before)
                        run_termination._pause_run(
                            event_path,
                            payload,
                            reason=(
                                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
                            ),
                            message=(
                                f"{session_label} still has retained changes "
                                "outside its durable write scope or lacks a "
                                "fresh expansion; repair scope and rerun "
                                "expansion/preflight before continuing"
                            ),
                            details={
                                **session_details,
                                "workspace_write_guard": repair_record,
                            },
                            persistence=persistence,
                        )
                        break
                    persistence.append_event(
                        event_path,
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": run_id,
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
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(before)
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason=(events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE),
                        message=(
                            "implementation write guard unavailable before "
                            f"{session_label}; the legacy Git repository scope "
                            "could not be established safely"
                        ),
                        details={
                            **session_details,
                            "workspace_write_guard": guard_record,
                        },
                        persistence=persistence,
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
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(before)
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                        message=(
                            "review mutation guard unavailable before "
                            f"{session_label}: {unavailable['reason']}"
                        ),
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
                        persistence=persistence,
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
                    reply_state_preserver.mark_consumed()
                reply_event_payload = {
                    "run_id": run_id,
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
                persistence.append_event(
                    event_path,
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
                persistence.append_event(
                    event_path,
                    events.RESOLUTION_CONSUMED_EVENT,
                    {
                        "run_id": run_id,
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

            def _abandon_turn() -> None:
                run_controls.stop()
                inline_tail.stop()

            # Materialized per turn, not once per run: the guard-bin directory
            # must exist before the child's PATH names it, and a guard added
            # during the previous turn has to be armed for this one. The
            # workspace binding is refreshed here too, because it is what turns
            # the absolute path a tool call carries into the `alias:path` a
            # rule is written in, and a run can change harness mid-flight.
            with run_control_lock:
                guard_controller.set_workspace_aliases(
                    {
                        repository.alias: str(repository.root)
                        for repository in workspace.repositories
                    }
                    if workspace is not None
                    else {guards.LEGACY_PATH_QUALIFIER: str(cwd)}
                )
                guard_controller.mark_turn_snapshot(protected_path_baseline)
                turn_env = guard_controller.prepare_turn(
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
                            event_path=event_path,
                            append_event=persistence.append_event,
                            pending_guidance=pending_guidance,
                            harness=turn_harness,
                            run_id=run_id,
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
                payload=payload,
                after_stop=lambda: run_termination._detach_turn_controls(mailbox_poll),
                on_failure=_abandon_turn,
            )
            result = execution.result
            live_turn_payload = execution.live_turn
            with run_control_lock:
                run_control_context = None
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
                with persistence.lock:
                    payload["review_session_executed"] = session_id
                normalized_after_review = (
                    plans.normalize_generated_review_handoff_artifacts(before.plan_dir)
                )
                if normalized_after_review:
                    normalized_review_handoffs.extend(normalized_after_review)
                    plan_artifacts_written_by_runner = tuple(
                        path.name for path in normalized_after_review
                    )
                    with persistence.lock:
                        payload["normalized_review_handoffs"] = [
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
            with persistence.lock:
                payload["turns"].append(turn_payload)
                # A harness that reports no model leaves the last observation
                # standing rather than erasing it.
                if turn_payload.get("observed_model") is not None:
                    payload.update(
                        _observed_model_payload(turn_payload["observed_model"])
                    )
                payload["model_projection"] = run_commit._merged_model_projection(
                    payload.get("model_projection"), turn_payload
                )
                turn_identity = {
                    "run_id": run_id,
                    "turn_index": turn_index,
                    **_session_identity_payload(session_id),
                }
                run_persistence._append_process_sweep_event(
                    event_path=event_path,
                    persistence=persistence,
                    report=result.process_sweep,
                    phase="turn",
                    identity=turn_identity,
                )
                run_persistence._append_harness_degradation_event(
                    event_path=event_path,
                    persistence=persistence,
                    degradation=turn_payload.get("harness_degradation"),
                    identity=turn_identity,
                )
                persistence.write_state(payload)

            if abort_signal.armed and not result.aborted:
                # The worker ended the turn inside the grace window, so nothing
                # was killed and whatever it recorded stands: the pause or
                # completion path below owns the outcome from here.
                with run_control_lock:
                    run_termination._record_abort_satisfied_by_worker(
                        run_controls,
                        payload=payload,
                        event_path=event_path,
                        control_payload=control_payload,
                        context=turn_payload,
                        persistence=persistence,
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
                if _enforce_workspace_write_guard(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
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
                    plan_cache=plan_cache,
                    state_dir_arg=args.state_dir,
                    run_dir=run_dir,
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
                        with persistence.lock:
                            payload["final_snapshot"] = (
                                _snapshot_payload(after)
                                if after is not None
                                else _snapshot_payload(before)
                            )
                        run_termination._pause_run(
                            event_path,
                            payload,
                            reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                            message=str(error),
                            details={**session_details, "error": str(error)},
                            persistence=persistence,
                        )
                        finalization_paused = True
                    else:
                        with persistence.lock:
                            turn_payload["review_repository_bases"] = review_bases
                            _write_json_file(paths["result"], turn_payload)
            elif legacy_snapshot_before is not None:
                pending_exception_guard = None
                if _enforce_legacy_implementation_write_guard(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
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
                    plan_cache=plan_cache,
                    state_dir_arg=args.state_dir,
                    run_dir=run_dir,
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
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
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
                    plan_cache=plan_cache,
                    append_handoff_note=_append_handoff_note,
                    reinspect=_reinspect_after_revert,
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
            if _enforce_protected_path_guard(
                event_path=event_path,
                payload=payload,
                persistence=persistence,
                guard_controller=guard_controller,
                before=before,
                after=after,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                turn_payload=turn_payload,
                paths=paths,
                plan_cache=plan_cache,
                changed_paths=(
                    _protected_path_changed_paths(
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
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    before=before,
                    after=after,
                    digests_before=plan_artifacts_before,
                    runner_written=plan_artifacts_written_by_runner,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=plan_cache,
                    result_disposition=terminating_disposition,
                    append_handoff_note=_append_handoff_note,
                    reinspect=_reinspect_after_revert,
                )
            ):
                break

            if terminating_disposition == "interrupted":
                with persistence.lock:
                    payload["final_snapshot"] = (
                        _snapshot_payload(after)
                        if after is not None
                        else _snapshot_payload(before)
                    )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="interrupted",
                    message="run interrupted by user",
                    details=session_details,
                    persistence=persistence,
                )
                break

            if terminating_disposition == "timeout":
                with persistence.lock:
                    payload["final_snapshot"] = (
                        _snapshot_payload(after)
                        if after is not None
                        else _snapshot_payload(before)
                    )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="timeout",
                    message=f"harness turn timed out for {session_label}",
                    details=session_details,
                    persistence=persistence,
                )
                break

            if terminating_disposition == "aborted":
                # Beside the timeout branch, but not a pause: the operator asked
                # for this stop, so it is recorded as one.
                with run_control_lock:
                    run_termination._honor_abort_turn(
                        run_controls,
                        snapshot=after if after is not None else before,
                        session_id=session_id,
                        payload=payload,
                        event_path=event_path,
                        control_payload=control_payload,
                        plan_cache=plan_cache,
                        revert_session_status=handoff_guards.session_status_reverter(
                            _append_handoff_note
                        ),
                        reinspect=_reinspect_after_revert,
                        context=turn_payload,
                        persistence=persistence,
                    )
                break
            if terminating_disposition == "malformed-plan":
                corrected, correction_evidence = _correct_malformed_handoff(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    plan_cache=plan_cache,
                    before=before,
                    workspace=workspace,
                    run_id=run_id,
                    turn_index=turn_index,
                    session_id=session_id,
                    plan_error=plan_error,
                    turn_payload=turn_payload,
                    paths=paths,
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
                reopened_snapshot, reopen_evidence = _reopen_uninspectable_handoff(
                    plan_cache=plan_cache,
                    before=before,
                    session_id=session_id,
                    plan_error=plan_error,
                    correction_exhausted=bool(
                        correction_evidence is not None
                        and correction_evidence.get("exhausted")
                    ),
                )
                if reopen_evidence is not None:
                    malformed_details["handoff_reopen"] = reopen_evidence
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(
                        before if reopened_snapshot is None else reopened_snapshot
                    )
                _record_turn_rejected(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="plan-inspection",
                    reason="malformed-plan",
                    reopened=bool(
                        reopen_evidence is not None and reopen_evidence["reopened"]
                    ),
                    detail={"error": plan_error},
                )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="malformed-plan",
                    message="plan could not be inspected after the harness turn",
                    details=malformed_details,
                    persistence=persistence,
                )
                break

            # Every disposition that survives the branches above names a turn
            # the plan could be inspected after, so the malformed-plan branch
            # owns the only `after is None` turn that reaches here.
            assert after is not None
            after, paused_status_repair = _repair_paused_handoff_status(
                event_path=event_path,
                payload=payload,
                persistence=persistence,
                plan_cache=plan_cache,
                after=after,
                session_id=session_id,
                turn_index=turn_index,
                runner_pause=turn_payload.get("runner_pause"),
            )
            invalid_pause = run_inputs._invalid_status_pause(after)
            if invalid_pause is not None:
                reason, message, details = invalid_pause
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="plan-status-validity",
                    reason=reason,
                    detail=details,
                )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason=reason,
                    message=message,
                    details=details | session_details,
                    persistence=persistence,
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
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    after=after,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_index=turn_index,
                    state_dir_arg=args.state_dir,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=plan_cache,
                    append_handoff_note=_append_handoff_note,
                    reinspect=_reinspect_after_revert,
                )
            ):
                break

            runner_pause = turn_payload.get("runner_pause")
            if terminating_disposition == "runner-pause":
                pause_reason = _runner_pause_reason(str(runner_pause))
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(after)
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
                        run_id=run_id,
                        session_id=session_id,
                        turn_index=turn_index,
                    )
                    if mailbox_enabled
                    else None
                )
                if worker_pause_resolution is not None:
                    pause_details["mailbox_resolution"] = {
                        "run_id": run_id,
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
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason=pause_reason,
                        message=str(runner_pause),
                        details=pause_details,
                        persistence=persistence,
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
                            payload=payload,
                            plan_state=state_dir,
                            state_path=state_path,
                            state_dir_arg=args.state_dir,
                            run_id=run_id,
                            session_id=session_id,
                            turn_index=turn_index,
                        )
                    except _MailboxReplyWaitUnavailable as error:
                        _record_mailbox_reply_wait_preparation_failed(
                            event_path,
                            payload,
                            session_id=session_id,
                            turn_index=turn_index,
                            reason=error.reason,
                            persistence=persistence,
                        )
                        prepared_wait = None

                    if prepared_wait is not None:
                        # The wait that follows is genuine idle time: the
                        # runner has no work left for this turn until a human
                        # answers, so the clock starts before the wait. Pinning
                        # it is this path's explicit opt-out from the
                        # structural refresh at the top of the loop.
                        runner_work_finished_at = _utc_now()
                        idle_baseline_pinned = True
                        wait_result = run_coordination.MailboxWaitCoordinator(
                            plan_dir=after.plan_dir,
                            state_dir_arg=args.state_dir,
                            prepared=prepared_wait,
                            run_id=run_id,
                            store=run_coordination.WaitStateStore(
                                event_path=event_path,
                                payload=payload,
                                persistence=persistence,
                            ),
                            persistence=persistence,
                            notifier=run_persistence._AgentMuxWaitNotifier(
                                payload, persistence
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
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason=pause_reason,
                    message=str(runner_pause),
                    details=pause_details,
                    persistence=persistence,
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
                            run_id=run_id,
                        )
                    except OSError as error:
                        cooldown = None
                        with persistence.lock:
                            payload["spark"]["warning"] = (
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
                    with persistence.lock:
                        spark_route["fallback_reason"] = fallback_reason
                        turn_payload["spark_route"] = dict(spark_route)
                        payload["spark"].update(spark_status_update)
                        payload["spark"]["active_route"] = dict(spark_route)
                        payload["final_snapshot"] = _snapshot_payload(after)
                        _write_json_file(paths["result"], turn_payload)
                        persistence.append_event(
                            event_path,
                            "spark_fallback",
                            {
                                "run_id": run_id,
                                "session_id": session_id,
                                "failed_turn_index": turn_index,
                                "failed_model": run_selection.SPARK_MODEL,
                                "fallback_model": payload["spark"]["baseline"],
                                "failure_classification": classification.to_dict(),
                                "retry_at": payload["spark"].get("retry_at"),
                            },
                        )
                        persistence.write_state(payload)
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
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(after)
                run_termination._pause_run(
                    event_path,
                    payload,
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
                    persistence=persistence,
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
                with persistence.lock:
                    payload["expected_status_changes"] = list(expected_change)
                    payload["observed_status_changes"] = []
                    payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="status-change",
                    reason="no-status-change",
                    detail={
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": [],
                    },
                )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="no-status-change",
                    message=f"{session_label} did not change status",
                    details={
                        **session_details,
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": [],
                    },
                    persistence=persistence,
                )
                break
            if len(status_changes) > 1:
                with persistence.lock:
                    payload["expected_status_changes"] = list(expected_change)
                    payload["observed_status_changes"] = list(status_changes)
                    payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="status-change",
                    reason="multiple-status-changes",
                    detail={
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="multiple-status-changes",
                    message="more than one session status changed during one harness "
                    "turn",
                    details={
                        **session_details,
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                    persistence=persistence,
                )
                break
            if status_changes != expected_change:
                with persistence.lock:
                    payload["expected_status_changes"] = list(expected_change)
                    payload["observed_status_changes"] = list(status_changes)
                    payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="status-change",
                    reason="unexpected-status-change",
                    detail={
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="unexpected-status-change",
                    message="the changed session status did not match the expected "
                    "pending completion",
                    details={
                        **session_details,
                        "expected_status_changes": list(expected_change),
                        "observed_status_changes": list(status_changes),
                    },
                    persistence=persistence,
                )
                break

            # The turn survived every result branch, so nothing terminated it
            # early and the comparison above was deferred to here. Behind the
            # status guards, a turn that completed the wrong session keeps
            # their sharper diagnosis; what is left is the tampering they
            # cannot see, an edit that moves no status at all.
            if handoff_guards._enforce_plan_artifact_guard(
                event_path=event_path,
                payload=payload,
                persistence=persistence,
                before=before,
                after=after,
                digests_before=plan_artifacts_before,
                runner_written=plan_artifacts_written_by_runner,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                turn_payload=turn_payload,
                paths=paths,
                plan_cache=plan_cache,
                append_handoff_note=_append_handoff_note,
                reinspect=_reinspect_after_revert,
            ):
                break

            if _enforce_session_verification(
                event_path=event_path,
                payload=payload,
                persistence=persistence,
                progress=progress,
                before=before,
                after=after,
                cwd=cwd,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                allow_not_loop_ready=allow_not_loop_ready,
                turn_payload=turn_payload,
                paths=paths,
                plan_cache=plan_cache,
                run_id=run_id,
                turn_index=turn_index,
                total_sessions=total_sessions,
                verification_context=verification_context,
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
                    control_state=run_controls.state,
                    seen_ids=seen_external_command_ids,
                    payload=payload,
                    event_path=event_path,
                    control_payload=control_payload,
                    context=turn_payload,
                    persistence=persistence,
                    run_control_lock=run_control_lock,
                    wakeup=wakeup,
                    guard_controller=guard_controller,
                )
                with run_control_lock:
                    stop_after_current_pending = run_controls.state.stop_after_current
                verdict_action, verdict_snapshot = _enforce_review_verdict(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    before=before,
                    after=after,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=plan_cache,
                    require_artifact=(
                        review_session_version is not None
                        and review_session_version >= STRUCTURED_REVIEW_MIN_VERSION
                    ),
                    stop_after_current_pending=stop_after_current_pending,
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
                persistence.append_event(
                    event_path,
                    "progress-journal-append-failed",
                    {
                        "run_id": payload.get("run_id"),
                        "session": session_id,
                        "session_label": session_label,
                        "completed_at": completed_at,
                        "error": progress_error,
                    },
                )
            with persistence.lock:
                turn_payload["progress_appended"] = progress_appended
                if progress_error is not None:
                    turn_payload["progress_error"] = progress_error
                _write_json_file(paths["result"], turn_payload)

            try:
                ledger_updates = plans.sync_derived_status_ledgers(after)
            except plans.PlanError as error:
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="ledger-sync",
                    reason="malformed-plan",
                    detail={"error": str(error)},
                )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="malformed-plan",
                    message="derived status ledgers could not be synced",
                    details={
                        **session_details,
                        "error": str(error),
                    },
                    persistence=persistence,
                )
                break
            if ledger_updates:
                try:
                    after = plan_cache.get_fresh(before.plan_dir)
                except plans.PlanError as error:
                    with persistence.lock:
                        payload["final_snapshot"] = _snapshot_payload(after)
                    _record_turn_rejected(
                        event_path=event_path,
                        payload=payload,
                        persistence=persistence,
                        turn_payload=turn_payload,
                        session_id=session_id,
                        guard="ledger-sync",
                        reason="malformed-plan",
                        detail={"error": str(error)},
                    )
                    run_termination._pause_run(
                        event_path,
                        payload,
                        reason="malformed-plan",
                        message="plan could not be inspected after syncing ledgers",
                        details={
                            **session_details,
                            "error": str(error),
                        },
                        persistence=persistence,
                    )
                    break
                with persistence.lock:
                    turn_payload["ledger_updates"] = [
                        str(path) for path in ledger_updates
                    ]
                    turn_payload["after"] = _snapshot_payload(after)
                    _write_json_file(paths["result"], turn_payload)

            readiness_gate._refresh_active_readiness_state(
                payload, after.plan_dir, persistence
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
                            verification_context,
                            turn_payload.get("workspace_write_guard", {}),
                        )
                        if verification_context.workspace is not None
                        else commit_settlement._dirty_commit_targets(
                            verification_context
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
                                event_path=event_path,
                                payload=payload,
                                progress=progress,
                                verification_context=verification_context,
                                run_dir=run_dir,
                                run_id=run_id,
                                session_id=session_id,
                                targets=session_targets,
                                generation=commit_generation,
                                persistence=persistence,
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
                    with persistence.lock:
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
                    with persistence.lock:
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
                    soft_fail_commit(
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
                    payload=payload,
                    turn_payload=turn_payload,
                    live_turn=live_turn_payload,
                    accepted=after,
                )
            except _TurnCommitRejected as rejection:
                with persistence.lock:
                    payload["final_snapshot"] = _snapshot_payload(after)
                _record_turn_rejected(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="ledger-sync",
                    reason=rejection.reason,
                    detail=rejection.detail,
                )
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason=rejection.reason,
                    message=rejection.message,
                    details={**session_details, **rejection.detail},
                    persistence=persistence,
                )
                break

            with persistence.lock:
                payload["completed_sessions"].append(_session_compat_value(session_id))
                payload.setdefault("completed_session_ids", []).append(session_id)
                if is_review_session:
                    payload["review_completed"] = True
                    review_verdict = commit.record.get("review_verdict")
                    if isinstance(review_verdict, dict):
                        payload["review_verdict"] = {
                            "status_line": review_verdict["status_line"],
                            "outcome": review_verdict["outcome"],
                            "total_findings": review_verdict["total_findings"],
                            "findings_by_severity": review_verdict[
                                "findings_by_severity"
                            ],
                        }
            if not is_review_session:
                completed_count += 1
            snapshot = after
            with persistence.lock:
                payload["final_snapshot"] = plans.snapshot_payload(commit.published)

            if is_review_session:
                cycle_action, snapshot = review_cycle._advance_review_cycle(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    args=args,
                    cwd=cwd,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    snapshot=snapshot,
                    turn_payload=commit.record,
                    max_review_fix_cycles=max_review_fix_cycles,
                    plan_cache=plan_cache,
                    reinspect=_reinspect_after_plan_mutation,
                )
                review_resume = review_cycle._set_review_resume(
                    snapshot=snapshot,
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    max_repair_group_attempts=run_inputs._max_repair_group_attempts(
                        args
                    ),
                    max_review_fix_cycles=max_review_fix_cycles,
                    review_requested=review_requested,
                )
                if cycle_action == "paused":
                    break
                if cycle_action == "complete":
                    with persistence.lock:
                        payload["reason"] = review_cycle._terminal_review_reason(
                            payload
                        )
                    break
                # Repair children were materialized: clear the appended flag
                # so the loop appends the next review session once the
                # children complete, then fall through to the control tail
                # so operator stop requests are honored between cycles.
                review_appended = False

            _drain_external_control_queue(
                plan_dir=snapshot.plan_dir,
                state_dir_arg=args.state_dir,
                control_state=run_controls.state,
                seen_ids=seen_external_command_ids,
                payload=payload,
                event_path=event_path,
                control_payload=control_payload,
                context=commit.record,
                persistence=persistence,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                guard_controller=guard_controller,
            )
            with run_control_lock:
                # An unpending request is left set on purpose: the all-complete
                # branch at the top of the loop owns it, so a stop is never
                # swallowed by a run that still has a review to append.
                stop_now = (
                    run_controls.state.stop_after_current
                    and snapshot.next_pending is not None
                )
                if stop_now:
                    run_termination._honor_stop_after_current(
                        run_controls,
                        snapshot=snapshot,
                        payload=payload,
                        event_path=event_path,
                        control_payload=control_payload,
                        context=commit.record,
                        persistence=persistence,
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
                run_control_context = None

            after = guard_context.get("after")
            if after is None:
                try:
                    after = plan_cache.get_fresh(snapshot.plan_dir)
                except Exception:
                    after = None
            turn_payload = guard_context.get("turn_payload")
            if not isinstance(turn_payload, dict):
                turn_payload = {
                    **live_turn_payload,
                    "run_id": run_id,
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
                    _enforce_legacy_implementation_write_guard(
                        event_path=event_path,
                        payload=payload,
                        persistence=persistence,
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
                        plan_cache=plan_cache,
                        state_dir_arg=args.state_dir,
                        run_dir=run_dir,
                        external_digests_before=guard_context.get(
                            "external_digests_before"
                        ),
                        turn_started_ns=guard_context.get("turn_started_ns"),
                    )
                elif guard_mode == "workspace":
                    _enforce_workspace_write_guard(
                        event_path=event_path,
                        payload=payload,
                        persistence=persistence,
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
                        plan_cache=plan_cache,
                        state_dir_arg=args.state_dir,
                        run_dir=run_dir,
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
                _enforce_protected_path_guard(
                    event_path=event_path,
                    payload=payload,
                    persistence=persistence,
                    guard_controller=guard_controller,
                    before=guard_context["before"],
                    after=after,
                    session_id=str(guard_context["session_id"]),
                    session_label=str(guard_context["session_label"]),
                    session_details=guard_context["session_details"],
                    turn_payload=turn_payload,
                    paths=guard_context["paths"],
                    plan_cache=plan_cache,
                    # A write guard that never recorded its comparison — it
                    # raised, or the inventory failed — leaves this guard
                    # blind, and blind is recorded as unavailable rather than
                    # read as clean. A legacy review turn has no write-guard
                    # comparison by construction, so it is answered from its
                    # own turn-start fingerprint instead.
                    changed_paths=(
                        _protected_path_review_changed_paths(
                            guard_context,
                            exclude=review_guard_excludes,
                            include_ignored=review_guard_include_ignored,
                        )
                        if guard_mode == "review"
                        else _protected_path_changed_paths(
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
            if not payload.get("paused"):
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="interrupted",
                    message="run interrupted by user",
                    details=dict(last_session_details),
                    persistence=persistence,
                )
        else:
            _expire_run_guards()
            run_controls.stop()
            inline_tail.stop()
            raise

    if review_requested:
        # An ordinary repair child can change pending/completed membership
        # without traversing a review-cycle branch. Re-inspect here so every
        # loop exit (including max-sessions, stop-after-current, and pauses)
        # publishes the plan's final continuation state.
        try:
            final_review_snapshot = plan_cache.get(snapshot.plan_dir)
        except plans.PlanError:
            # Preserve the run's existing malformed-plan pause and its last
            # safely computed projection when the final plan is unreadable.
            pass
        else:
            review_resume = review_cycle._set_review_resume(
                snapshot=final_review_snapshot,
                event_path=event_path,
                payload=payload,
                persistence=persistence,
                max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=True,
            )

    with persistence.lock:
        if payload["ok"] and payload["reason"] == "":
            payload["reason"] = "stopped"
        review_executed = payload.get("review_session_executed") is not None
        if review_requested and review_executed and not payload.get("review_completed"):
            review_cycle._attest_executed_review(
                payload,
                snapshot.plan_dir,
                require_artifact=executed_review_requires_artifact,
                read_review_verdict=handoff_guards._read_review_verdict,
            )
        if (
            review_requested
            and not review_executed
            and not payload.get("review_completed")
            and payload.get("review_skipped_reason") is None
        ):
            if payload.get("paused"):
                payload["review_skipped_reason"] = (
                    payload.get("pause_reason") or "paused"
                )
            elif payload["reason"] not in (
                "",
                "all-complete",
                REVIEW_FINDINGS_UNRESOLVED_REASON,
            ):
                payload["review_skipped_reason"] = payload["reason"]

    with persistence.lock:
        exit_code = 0 if payload["ok"] else 1
        if payload.get("pause_reason") == "interrupted":
            exit_code = INTERRUPTED_EXIT
    # The controls outlive the loop so a supervisor can still reach a run that
    # is finalizing: the terminal audit is a harness turn of its own, and an
    # abort arriving during it has to find a live listener and a live drain.
    finalization_abort = harnesses.AbortSignal()
    mailbox_poll.set_context(None)
    mailbox_poll.set_abort(finalization_abort, finalization=True)
    try:
        return terminalize(exit_code, cancel=finalization_abort, poll=mailbox_poll)
    finally:
        mailbox_poll.set_abort(None)
        _expire_run_guards()
        run_controls.stop()
        inline_tail.stop()


def _state_error_reason(error: state.StateError) -> tuple[str, bool]:
    message = str(error).lower()
    if "plan is locked" in message or "could not acquire lock" in message:
        return "lock-contention", True
    return "state-error", False


def _error_payload(
    args: argparse.Namespace,
    *,
    reason: str,
    message: str,
    paused: bool,
    cwd: Path | None = None,
    snapshot: plans.PlanSnapshot | None = None,
) -> dict[str, Any]:
    try:
        plan_dir = str(
            snapshot.plan_dir
            if snapshot is not None
            else plans.canonicalize_path(args.plan_dir)
        )
    except OSError:
        plan_dir = str(args.plan_dir)

    review_harness = getattr(args, "review_harness", None)
    review_requested = review_harness is not None
    payload = responses.error_payload(
        "run",
        reason=reason,
        error=message,
        paused=paused,
        details={},
        dry_run=getattr(args, "dry_run", False),
        pause_reason=reason if paused else None,
        pause_message=message if paused else "",
        pause_details={},
        commit_mode=run_selection._commit_mode_payload(args),
        note=getattr(args, "note", None),
        commits=[],
        commit_groups=[],
        plan_dir=plan_dir,
        cwd=str(cwd) if cwd is not None else str(getattr(args, "cwd", "")),
        harness=getattr(args, "harness", None),
        completed_sessions=[],
        completed_session_ids=[],
        turns=[],
        review_requested=review_requested,
        review_harness=review_harness,
        review_session_id=None,
        review_output_path=(
            str(review_session_contract.final_review_output_path(plan_dir))
            if review_requested
            else None
        ),
        review_completed=False,
        review_verdict=None,
        review_skipped_reason=reason if review_requested else None,
    )
    payload.update(run_selection._selected_model_effort_payload(args))
    payload["spark"] = run_selection._safe_initial_spark_payload(args)
    if paused:
        # Match `run_termination.set_pause`: a paused error payload carries the same
        # single-source-of-truth pause classification block.
        payload["pause_policy"] = pause_policy.classify_pause(reason)
    if snapshot is not None:
        state_dir = state.plan_state_dir(snapshot.plan_dir, args.state_dir)
        payload["state_dir"] = str(state_dir)
        payload["state_path"] = str(state_dir / events.STATE_FILE_NAME)
        payload["event_path"] = str(state_dir / events.EVENTS_FILE_NAME)
        payload["events_path"] = str(state_dir / events.EVENTS_FILE_NAME)
        payload["lock_path"] = str(
            state.lock_path_for_plan(snapshot.plan_dir, args.state_dir)
        )
        payload["initial_snapshot"] = _snapshot_payload(snapshot)
        payload["final_snapshot"] = _snapshot_payload(snapshot)
        try:
            registry = plans.read_repository_registry(snapshot.plan_dir)
            metadata = plans.load_plan_metadata(snapshot.plan_dir)
        except plans.PlanError:
            registry = None
            metadata = None
        if registry is not None and isinstance(metadata, plans.WorkspacePlanMetadata):
            workspace_root = plans.canonicalize_path(metadata.workspace_root)
            payload.update(
                {
                    "cwd": str(workspace_root),
                    "workspace_root": str(workspace_root),
                    "registry_digest": metadata.registry_digest,
                    "repositories": [
                        {
                            "alias": repository.alias,
                            "path": repository.path,
                            "origin": repository.origin,
                            "resolved_root": str(
                                plans.canonicalize_path(
                                    workspace_root / repository.path
                                )
                            ),
                        }
                        for repository in registry.repositories
                    ],
                }
            )
    return payload


def _print_model_effort_lines(payload: dict[str, Any]) -> None:
    model = payload.get("model")
    model_source = payload.get("model_source")
    if model is not None and model_source is not None:
        print(f"Model: {model} ({model_source})")

    effort = payload.get("effort")
    if effort is not None:
        effort_source = payload.get("effort_source")
        if effort_source is None:
            print(f"Effort: {effort}")
        else:
            print(f"Effort: {effort} ({effort_source})")


def _commit_scope_label(commit: dict[str, Any]) -> str:
    scope = commit.get("scope")
    session = commit.get("session_id") or commit.get("session")
    if scope == "session" and session is not None:
        label = f"session {session}"
    elif scope == "end":
        label = "end"
    else:
        label = str(scope or "unknown")
    alias = commit.get("repository_alias") or commit.get("repo")
    return f"{label}, repository {alias}" if alias else label


def _print_commit_outcome_lines(payload: dict[str, Any]) -> None:
    commits = payload.get("commits")
    if not isinstance(commits, list):
        return
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        label = _commit_scope_label(commit)
        index = commit.get("commit_index", "?")
        if commit.get("success"):
            sha = commit.get("commit_sha") or "unknown"
            print(f"Created commit {sha} ({label})")
        else:
            status = commit.get("status_result", "unknown")
            print(f"Commit {index} failed ({label}): {status}")


def _last_commit(payload: dict[str, Any]) -> dict[str, Any] | None:
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        return None
    commit = commits[-1]
    return commit if isinstance(commit, dict) else None


def _print_text_outcome(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    workspace_root = payload.get("workspace_root")
    if workspace_root is not None:
        print(f"Workspace root: {workspace_root}")
        repositories = payload.get("repositories")
        if isinstance(repositories, list):
            visible, omitted = workspaces.bounded_repository_rows(repositories)
            aliases = [
                str(row.get("alias"))
                for row in visible
                if isinstance(row, Mapping) and row.get("alias")
            ]
            if aliases:
                suffix = f" (+{omitted} more)" if omitted else ""
                print(f"Repositories: {', '.join(aliases)}{suffix}")
    if payload.get("harness") is not None:
        print(f"Harness: {payload['harness']}")
        _print_model_effort_lines(payload)
    spark = payload.get("spark")
    if isinstance(spark, Mapping):
        route = spark.get("active_route")
        route_decision = route.get("decision") if isinstance(route, Mapping) else None
        print(
            "Spark: "
            f"{'enabled' if spark.get('enabled') else 'disabled'}; "
            f"route {route_decision or 'none'}; {spark.get('reason', 'unknown')}"
        )
        if spark.get("retry_at"):
            print(f"Spark retry: {spark['retry_at']}")
        if isinstance(route, Mapping) and route.get("fallback_reason"):
            print(f"Spark fallback: {route['fallback_reason']}")
        if isinstance(route, Mapping) and route.get("suppression_cause"):
            print(f"Spark suppression: {route['suppression_cause']}")
        if spark.get("warning"):
            print(f"Spark warning: {spark['warning']}")
    if "commit_mode" in payload:
        print(f"Commit mode: {payload.get('commit_mode') or 'none'}")
    if payload.get("run_dir") is not None:
        print(f"Run dir: {payload['run_dir']}")
    if payload.get("state_path") is not None:
        print(f"State file: {payload['state_path']}")
    if payload.get("events_path") is not None:
        print(f"Events: {payload['events_path']}")
    for line in run_reporting._note_lines(payload, prefix="Note"):
        print(line)

    if payload.get("dry_run") and payload["ok"]:
        planned = payload.get("planned", {})
        planned_audit_line = supervisor_audit.status_line(planned.get("audit"))
        if planned_audit_line:
            print(planned_audit_line)
        next_pending = planned.get("next_pending")
        if next_pending is None:
            print("Next pending: none")
            print("Dry run: nothing to invoke")
            return
        print(f"Next pending: session {next_pending}")
        resolver_prompt = planned.get("resolver_prompt")
        if resolver_prompt:
            print("Resolver prompt:")
            print(resolver_prompt)
        print(f"Prompt: {planned['prompt']}")
        invocation = planned["invocation"]
        print(f"Transport: {invocation.get('transport', harnesses.TRANSPORT_EXEC)}")
        print("Argv:")
        for arg in invocation["argv"]:
            print(f"  {arg}")
        turn_plan = invocation.get("turn_plan")
        if turn_plan is not None:
            # Argv no longer states the access profile under this transport, so
            # a dry run that printed only argv would no longer show the
            # operator what the turn is actually allowed to do.
            print("Turn plan:")
            for line in json.dumps(turn_plan, indent=2, sort_keys=True).splitlines():
                print(f"  {line}")
        return

    audit_line = supervisor_audit.status_line(
        payload.get("assurance"), report=payload.get("assurance_report")
    )
    if audit_line:
        print(audit_line)
    for line in assurance_report.report_lines(payload.get("assurance_report")):
        print(line)

    for session_number in payload["completed_sessions"]:
        print(f"Completed session {session_number}")
    _print_commit_outcome_lines(payload)
    commit_groups_payload = payload.get("commit_groups")
    if isinstance(commit_groups_payload, list):
        unresolved = [
            group
            for group in commit_groups_payload
            if isinstance(group, Mapping) and group.get("status") != "complete"
        ]
        for group in unresolved[:3]:
            successful = group.get("successful_repositories")
            partial = (
                [
                    f"{row.get('alias') or row.get('key')}={row.get('commit_sha')}"
                    for row in successful
                    if isinstance(row, Mapping) and row.get("commit_sha")
                ]
                if isinstance(successful, list)
                else []
            )
            print(
                "Pending commit group: "
                f"failed {group.get('failed_alias') or 'none'}; "
                f"remaining "
                f"{', '.join(group.get('remaining_aliases') or []) or 'none'}; "
                f"partial SHAs {', '.join(partial) or 'none'}"
            )
        if len(unresolved) > 3:
            print(f"Pending commit groups: +{len(unresolved) - 3} more")

    if not payload["ok"]:
        if payload.get("paused"):
            print(f"Run paused: {payload['pause_reason']}", file=sys.stderr)
            if payload.get("pause_message"):
                print(f"Pause detail: {payload['pause_message']}", file=sys.stderr)
            details = payload.get("pause_details") or payload.get("details")
            guard = (
                details.get("workspace_write_guard")
                if isinstance(details, Mapping)
                else None
            )
            if isinstance(guard, Mapping):
                violations = guard.get("violations")
                unavailable = guard.get("unavailable")
                if violations:
                    print(
                        "Workspace scope violations retained; edit durable scope and "
                        "rerun expansion/preflight.",
                        file=sys.stderr,
                    )
                if unavailable:
                    print(
                        "Repository evidence unavailable; repair Git evidence before "
                        "retrying.",
                        file=sys.stderr,
                    )
        else:
            print(f"Run failed: {payload['reason']}", file=sys.stderr)
        if payload["turns"]:
            turn = payload["turns"][-1]
            print(f"Stdout: {turn['stdout_path']}", file=sys.stderr)
            print(f"Stderr: {turn['stderr_path']}", file=sys.stderr)
            print(f"Last message: {turn['last_message_path']}", file=sys.stderr)
        interactive_help = payload.get("interactive_help")
        if isinstance(interactive_help, dict):
            questions_command = interactive_help.get("questions_command")
            if questions_command:
                print(f"Questions: {questions_command}", file=sys.stderr)
        commit = _last_commit(payload)
        if commit is not None:
            print(f"Commit stdout: {commit['stdout_path']}", file=sys.stderr)
            print(f"Commit stderr: {commit['stderr_path']}", file=sys.stderr)
            print(f"Commit result: {commit['result_path']}", file=sys.stderr)
        return

    final_snapshot = payload["final_snapshot"]
    next_pending = final_snapshot["next_pending"]
    print(f"Run complete: {len(payload['completed_sessions'])} session(s) completed")
    if next_pending is None:
        print("Next pending: none")
    else:
        print(f"Next pending: session {next_pending}")
    review_output_path = payload.get("review_output_path")
    if payload.get("review_completed") and review_output_path:
        print(f"Review: {review_output_path}")
        review_verdict = payload.get("review_verdict")
        if isinstance(review_verdict, dict):
            status_line = review_verdict.get("status_line")
            if status_line:
                print(status_line)


def _emit_run_payload(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    task_packet._attach_interactive_help(args, payload)
    try:
        if args.json:
            responses.print_json(payload)
        else:
            _print_text_outcome(payload)
    except BrokenPipeError, OSError:
        # A recovered run whose original pipe reader exited (helper
        # early-exit) must not crash on its final emission; the same payload
        # is already durable in state.json / result.json.
        pass


# Flag dests rehydrated from a persisted ``run_config`` by ``asl run --resume``.
# Derived from ``RUN_FLAG_SPEC`` so new ordinary run flags join the overlay
# automatically. Exclusions are flags that are positional, helper-owned, or
# persisted under a different key (``state_dir_override``) / canonical alias.
_RESUME_OVERLAY_EXCLUDED_DESTS = frozenset(
    {
        "dry_run",
        "json",
        "max_review_fix_plans",
        "plan_dir",
        "reply",
        "reply_stdin",
        "resume",
        "state_dir",
    }
)


def _resume_overlay_dests() -> tuple[str, ...]:
    seen: set[str] = set()
    dests: list[str] = []
    for flag in run_flags.RUN_FLAG_SPEC:
        if (
            flag.positional
            or flag.dest in _RESUME_OVERLAY_EXCLUDED_DESTS
            or flag.dest in seen
        ):
            continue
        seen.add(flag.dest)
        dests.append(flag.dest)
    return tuple(dests)


_RESUME_OVERLAY_DESTS = _resume_overlay_dests()


class _ResumeUnavailable(Exception):
    """Raised when ``--resume`` cannot rehydrate flags from persisted state.

    Carries a best-effort ``asl run`` command reconstructed from whatever fields
    the state file does persist, so the operator can re-run with explicit flags.
    """

    def __init__(self, message: str, *, command: str) -> None:
        super().__init__(message)
        self.command = command


def _resume_run_tokens(run_argv: list[str] | None) -> list[str]:
    """Return the ``run`` subcommand's argv tokens, minus the subcommand name."""
    tokens = list(sys.argv[1:] if run_argv is None else run_argv)
    if tokens and tokens[0] == "run":
        tokens = tokens[1:]
    return tokens


def _explicit_resume_dests(run_argv: list[str] | None) -> set[str]:
    """Return the flag dests the operator passed explicitly on this resume.

    Re-parsing with ``mode="resume"`` (``argparse.SUPPRESS`` defaults) leaves
    only operator-supplied flags on the namespace, so ``vars()`` names exactly
    the flags whose values must win over the persisted ``run_config``.
    """
    parser = argparse.ArgumentParser(prog="asl run", add_help=False)
    run_flags.add_run_arguments(parser, mode="resume")
    try:
        namespace, _unknown = parser.parse_known_args(_resume_run_tokens(run_argv))
    except SystemExit:
        return set()
    explicit = set(vars(namespace))
    # The deprecated alias shares intent with the canonical flag; treat passing
    # either as an explicit cap so the persisted value never clobbers it.
    if "max_review_fix_plans" in explicit:
        explicit.add("max_review_fix_cycles")
    return explicit


def _reconstruct_resume_command(
    plan_dir: str,
    state_dir_override: str | None,
    payload: dict[str, Any],
) -> str:
    """Best-effort ``asl run`` command from top-level persisted state fields.

    Used only when ``run_config`` is absent (legacy or missing state): it re-emits
    the flags recoverable from the fields ``_initial_payload`` has always written
    at the top level, so the operator can resume manually with explicit flags.
    """
    argv = ["asl", "run", str(plan_dir)]
    if state_dir_override:
        argv += ["--state-dir", str(state_dir_override)]
    cwd = payload.get("cwd")
    if cwd:
        argv += ["--cwd", str(cwd)]
    harness = payload.get("harness")
    if harness:
        argv += ["--harness", str(harness)]
    review_harness = payload.get("review_harness")
    if review_harness:
        argv += ["--review-harness", str(review_harness)]
    model = payload.get("model")
    if model:
        argv += ["--harness-model", str(model)]
    effort = payload.get("effort")
    if effort:
        argv += ["--effort", str(effort)]
    commit_mode = payload.get("commit_mode")
    if commit_mode == "per-session":
        argv.append("--commit-per-session")
    elif commit_mode == "end":
        argv.append("--commit-at-end")
    max_pending = payload.get("max_pending_sessions")
    if isinstance(max_pending, int):
        argv += ["--max-pending-sessions", str(max_pending)]
    note = payload.get("note")
    if note:
        argv += ["--note", str(note)]
    if payload.get("allow_not_loop_ready") is True:
        argv.append("--allow-not-loop-ready")
    argv += _reconstructed_audit_flags(payload.get("assurance"))
    return shlex.join(argv)


def _reconstructed_audit_flags(assurance: Any) -> list[str]:
    """Recover the audit flags from the assurance object state persisted.

    An inherited role needs no ``--audit-harness``: rebuilding ``--review-harness``
    reproduces it. Its resolved model is re-stated explicitly, which yields the
    same effective role because an explicit audit model wins over an inherited one.
    """
    if not isinstance(assurance, Mapping):
        return []
    argv: list[str] = []
    if assurance.get("state") == "skipped" and (
        assurance.get("reason") == supervisor_audit.REASON_OPTED_OUT
    ):
        return ["--no-supervisor-audit"]
    role = assurance.get("role")
    if not isinstance(role, Mapping):
        return argv
    if role.get("source") == "explicit" and role.get("harness"):
        argv += ["--audit-harness", str(role["harness"])]
    if role.get("model"):
        argv += ["--audit-harness-model", str(role["model"])]
    if role.get("effort"):
        argv += ["--audit-effort", str(role["effort"])]
    timeout = role.get("timeout_seconds")
    if (
        isinstance(timeout, (int, float))
        and int(timeout) != supervisor_audit.DEFAULT_AUDIT_TIMEOUT_SECONDS
    ):
        argv += ["--audit-timeout", str(int(timeout))]
    return argv


def _load_resume_state(
    plan_dir: str,
    state_dir_arg: str | None,
) -> dict[str, Any] | None:
    """Best-effort read of a plan's persisted ``state.json`` payload."""
    plan_state = state.plan_state_dir(plan_dir, state_dir_arg)
    state_path = plan_state / events.STATE_FILE_NAME
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _apply_resume_config(
    args: argparse.Namespace,
    run_argv: list[str] | None,
) -> argparse.Namespace:
    """Overlay a persisted ``run_config`` onto ``args`` for ``asl run --resume``.

    Explicitly-passed flags win; every other flag is restored from the
    ``run_config`` the plan's last run persisted. Raises :class:`_ResumeUnavailable`
    when no usable config exists so the caller can emit a structured
    ``resume-unavailable`` error.
    """
    state_dir_arg = getattr(args, "state_dir", None)
    payload = _load_resume_state(args.plan_dir, state_dir_arg)
    if payload is None:
        raise _ResumeUnavailable(
            "no runner state found for this plan; run it once so its "
            "run_config is persisted before resuming",
            command=_reconstruct_resume_command(args.plan_dir, state_dir_arg, {}),
        )
    run_config = payload.get("run_config")
    if not isinstance(run_config, dict):
        raise _ResumeUnavailable(
            "runner state predates run_config persistence; re-run with "
            "explicit flags instead of --resume",
            command=_reconstruct_resume_command(args.plan_dir, state_dir_arg, payload),
        )
    explicit = _explicit_resume_dests(run_argv)
    args._resume_workspace_config = {
        "workspace_root": run_config.get("workspace_root"),
        "registry_digest": run_config.get("registry_digest"),
        "repositories": run_config.get("repositories"),
    }
    for dest in _RESUME_OVERLAY_DESTS:
        if dest in explicit or dest not in run_config:
            continue
        setattr(args, dest, run_config[dest])
    return args


def run_from_args(
    args: argparse.Namespace,
    *,
    run_argv: list[str] | None = None,
) -> int:
    """Run one or more pending sessions from CLI arguments.

    ``run_argv`` is the raw ``run`` subcommand argv used to detect which flags
    the operator passed explicitly during ``--resume`` rehydration. When it is
    not supplied, the argv the CLI parser recorded on the namespace
    (``args._run_argv``) is used, and only if that is absent does detection fall
    back to the process-global ``sys.argv``. This keeps explicit-flag provenance
    correct through ``cli.main(argv)`` and direct resolved-handler dispatch,
    where ``sys.argv`` may name an unrelated command.
    """
    if run_argv is None:
        run_argv = getattr(args, "_run_argv", None)
    if getattr(args, "resume", False):
        try:
            args = _apply_resume_config(args, run_argv)
        except _ResumeUnavailable as error:
            payload = _error_payload(
                args,
                reason="resume-unavailable",
                message=str(error),
                paused=False,
            )
            payload["resume_command"] = error.command
            payload["details"] = {"resume_command": error.command}
            _emit_run_payload(args, payload)
            return 1

    try:
        cwd, timeout, max_sessions, commit_mode = run_inputs._validate_run_inputs(
            args,
            resolve_review_guard_excludes=review_guard._review_guard_excludes,
        )
    except (plans.PlanError, RunError) as error:
        payload = _error_payload(
            args,
            reason="input-error",
            message=str(error),
            paused=False,
        )
        _emit_run_payload(args, payload)
        return 1

    reply_text = getattr(args, "reply", None)
    reply_via_stdin = bool(getattr(args, "reply_stdin", False))
    reply_origin = run_prompts.REPLY_ORIGIN_ARGV
    if reply_via_stdin:
        if reply_text is not None:
            payload = _error_payload(
                args,
                reason="conflicting-reply",
                message="--reply and --reply-stdin are mutually exclusive",
                paused=False,
                cwd=cwd,
            )
            _emit_run_payload(args, payload)
            return 1
        try:
            reply_text = run_prompts.read_reply_from_stdin()
        except OSError as error:
            payload = _error_payload(
                args,
                reason="reply-read-error",
                message=str(error),
                paused=False,
                cwd=cwd,
            )
            _emit_run_payload(args, payload)
            return 1
        reply_origin = run_prompts.REPLY_ORIGIN_STDIN
    if reply_text is not None and not reply_text.strip():
        flag = "--reply-stdin" if reply_via_stdin else "--reply"
        payload = _error_payload(
            args,
            reason="empty-reply",
            message=f"{flag} requires non-whitespace content",
            paused=False,
            cwd=cwd,
        )
        _emit_run_payload(args, payload)
        return 1

    try:
        initial_snapshot = plans.inspect_plan(args.plan_dir)
    except plans.PlanError as error:
        payload = _error_payload(
            args,
            reason="malformed-plan",
            message=str(error),
            paused=True,
            cwd=cwd,
        )
        _emit_run_payload(args, payload)
        return 1

    try:
        verification_context = run_inputs._resolve_run_verification_context(
            initial_snapshot.plan_dir,
            cwd,
        )
        run_inputs._validate_resume_workspace_binding(args, verification_context)
    except plans.PlanError as error:
        payload = _error_payload(
            args,
            reason="workspace-error",
            message=str(error),
            paused=True,
            cwd=cwd,
            snapshot=initial_snapshot,
        )
        _emit_run_payload(args, payload)
        return 1

    # After root/workspace resolution and before anything this run launches.
    # Deliberately independent of `--allow-not-loop-ready`: the flag waives
    # readiness, and no readiness marking can stand in for the registry that
    # would have bound a second repository in the first place.
    intent_report = run_inputs._run_repository_intent_report(
        initial_snapshot.plan_dir,
        verification_context,
        state_dir_arg=args.state_dir,
    )
    if intent_report is not None:
        registration, registration_error = run_inputs._apply_repository_registry(
            initial_snapshot,
            cwd=verification_context.root,
        )
        payload = _error_payload(
            args,
            reason=events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            message=run_inputs._repository_intent_message(
                intent_report, registration=registration
            ),
            paused=True,
            cwd=verification_context.root,
            snapshot=initial_snapshot,
        )
        details: dict[str, Any] = {"repository_intent": intent_report}
        if registration is not None:
            details["repository_registration"] = repository_registration.result_payload(
                registration
            )
            if registration.status == "applied":
                details["auto_repairs"] = [
                    asdict(
                        preflight.AutoRepair(
                            action="repository-registry",
                            category="repository-registry-required",
                            path=str(initial_snapshot.plan_dir),
                            entries_written=len(registration.files),
                            completed_session_ids=(),
                            journal_replaced=False,
                        )
                    )
                ]
        if registration_error is not None:
            details["repository_registration_error"] = registration_error
        payload["details"] = details
        payload["pause_details"] = dict(details)
        _emit_run_payload(args, payload)
        return 1

    run_id = _utc_run_id()
    claimed_repository_roots = run_prompts._claimed_repository_roots(
        verification_context
    )
    # The endpoint opens before the lock that names it and closes after that
    # lock is gone, so every producer that can find the path finds this
    # process holding the read end. Ordering it this way is what lets the
    # producer skip authenticating an owner: it writes to a descriptor rather
    # than acting on a pid, and an endpoint outliving its reader refuses the
    # write instead of reaching whatever took the pid over.
    wakeup = threading.Event()
    with control_signal.open_wakeup_endpoint(
        state.plan_state_dir(initial_snapshot.plan_dir, args.state_dir),
        wakeup,
    ) as wakeup_endpoint:
        try:
            lock_info = state.acquire_lock(
                initial_snapshot.plan_dir,
                args.state_dir,
                repository_roots=claimed_repository_roots,
                wakeup_endpoint=wakeup_endpoint,
            )
        except state.StateError as error:
            reason, paused = _state_error_reason(error)
            payload = _error_payload(
                args,
                reason=reason,
                message=str(error),
                paused=paused,
                cwd=cwd,
                snapshot=initial_snapshot,
            )
            _emit_run_payload(args, payload)
            return 1

        try:
            try:
                # Checked after acquiring, not before: a check-before-acquire
                # leaves two racing starters blind to each other and both
                # proceed, whereas checking under our own published claim means
                # the worst case is both refusing, which only happens on a
                # genuine overlap.
                if verification_context.workspace is not None:
                    conflicts = run_contention.detect_conflicts(
                        plan_dir=initial_snapshot.plan_dir,
                        repository_roots=claimed_repository_roots,
                        state_dir=args.state_dir,
                    )
                    if conflicts:
                        payload = _error_payload(
                            args,
                            reason=(
                                events.PAUSE_REASON_WORKSPACE_REPOSITORY_CONTENTION
                            ),
                            message=run_contention.conflict_message(
                                initial_snapshot.plan_dir, conflicts
                            ),
                            paused=True,
                            cwd=verification_context.root,
                            snapshot=initial_snapshot,
                        )
                        contention_details = run_contention.conflict_details(conflicts)
                        payload["details"] = contention_details
                        payload["pause_details"] = dict(contention_details)
                        _emit_run_payload(args, payload)
                        return 1
                exit_code, payload = _run_locked(
                    args=args,
                    cwd=verification_context.root,
                    verification_context=verification_context,
                    timeout=timeout,
                    max_sessions=max_sessions,
                    commit_mode=commit_mode,
                    lock_info=lock_info,
                    run_id=run_id,
                    reply_text=reply_text,
                    reply_origin=reply_origin,
                    wakeup=wakeup,
                )
            finally:
                state.release_lock(initial_snapshot.plan_dir, args.state_dir)
        except KeyboardInterrupt:
            payload = _error_payload(
                args,
                reason="interrupted",
                message="run interrupted by user",
                paused=True,
                cwd=cwd,
                snapshot=initial_snapshot,
            )
            _emit_run_payload(args, payload)
            return INTERRUPTED_EXIT
        except (
            config.ConfigError,
            harnesses.HarnessError,
            plans.PlanError,
            RunError,
            state.StateError,
            OSError,
        ) as error:
            payload = _error_payload(
                args,
                reason="run-error",
                message=str(error),
                paused=False,
                cwd=cwd,
                snapshot=initial_snapshot,
            )
            _emit_run_payload(args, payload)
            return 1

    _emit_run_payload(args, payload)
    return exit_code


def _print_doctor_error(
    args: argparse.Namespace,
    *,
    reason: str,
    error: Exception,
    cwd: Path | None = None,
    plan_dir: Path | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    if args.json:
        payload = responses.error_payload(
            "doctor",
            reason=reason,
            error=str(error),
            paused=False,
            details=details,
            harness=args.harness,
            cwd=str(cwd) if cwd is not None else str(args.cwd),
            plan_dir=str(plan_dir) if plan_dir is not None else str(args.plan_dir),
        )
        payload.update(run_selection._selected_model_effort_payload(args))
        responses.print_json(payload)
    else:
        print(f"agent-session-loop doctor: {error}", file=sys.stderr)
    return 1


def doctor_from_args(args: argparse.Namespace) -> int:
    """Check local harness prerequisites from CLI arguments."""
    try:
        cwd = plans.canonicalize_path(args.cwd)
        if not cwd.is_dir():
            raise plans.PlanError(f"cwd does not exist or is not a directory: {cwd}")
    except (plans.PlanError, OSError) as error:
        return _print_doctor_error(args, reason="input-error", error=error)

    try:
        repository_registry = plans.read_repository_registry(args.plan_dir)
        snapshot = plans.inspect_plan(
            args.plan_dir,
            repository_registry=repository_registry,
        )
    except plans.PlanError as error:
        return _print_doctor_error(
            args,
            reason="malformed-plan",
            error=error,
            cwd=cwd,
        )

    verification_context = verification.VerificationContext.legacy(cwd)
    if repository_registry is not None:
        try:
            verification_context = verification.VerificationContext.resolved_workspace(
                workspaces.resolve_workspace(cwd, repository_registry)
            )
        except workspaces.WorkspaceError as error:
            return _print_doctor_error(
                args,
                reason="workspace-error",
                error=error,
                cwd=cwd,
                plan_dir=snapshot.plan_dir,
                details={"registry_digest": repository_registry.digest},
            )
        metadata = plans.load_plan_metadata(snapshot.plan_dir)
        if metadata is not None and (
            not isinstance(metadata, plans.WorkspacePlanMetadata)
            or metadata.workspace_root != str(verification_context.root)
            or metadata.registry_digest != repository_registry.digest
        ):
            return _print_doctor_error(
                args,
                reason="workspace-error",
                error=plans.PlanError(
                    "doctor workspace does not match the prepared workspace binding"
                ),
                cwd=cwd,
                plan_dir=snapshot.plan_dir,
                details={"registry_digest": repository_registry.digest},
            )

    prompt_workspace = (
        verification_context.workspace if snapshot.next_pending is not None else None
    )
    prompt_scope = (
        run_prompts._session_scope(snapshot.next_pending, prompt_workspace)
        if snapshot.next_pending is not None and prompt_workspace is not None
        else None
    )
    if (
        prompt_workspace is not None
        and prompt_scope is not None
        and task_packet._worker_mode() == task_packet.WORKER_MODE_PACKET
    ):
        assert snapshot.next_pending is not None
        prompt = task_packet.build_task_packet(
            args.harness,
            snapshot.plan_dir,
            session_id=snapshot.next_pending.id,
            session_kind=(
                SESSION_KIND_REVIEW
                if review_session_contract.is_generated_review_session_file(
                    snapshot.next_pending.path
                )
                else SESSION_KIND_IMPLEMENTATION
            ),
            predecessor_id=task_packet._predecessor_session_id(
                snapshot, snapshot.next_pending.id
            ),
            has_expansion=task_packet._session_has_expansion(
                snapshot.plan_dir, snapshot.next_pending.id
            ),
            has_progress=(snapshot.plan_dir / plans.PROGRESS_FILE_NAME).is_file(),
            workspace=prompt_workspace,
            session_scope=prompt_scope,
        ).render()
    else:
        prompt = run_prompts.build_session_prompt(
            args.harness,
            snapshot.plan_dir,
            workspace=prompt_workspace,
            session_scope=prompt_scope,
        )

    try:
        harness_config = config.harness_config_payload()
        adapter = harnesses.get_adapter(args.harness)
        executable_path = adapter.resolve_executable()
        if executable_path is None:
            raise harnesses.HarnessError(
                f"{args.harness} executable not found on PATH: {adapter.executable}"
            )

        last_message_path = (
            state.plan_state_dir(snapshot.plan_dir) / "last-message-doctor.txt"
        )
        model_effort = run_selection._selected_model_effort_payload(args)
        invocation = run_prompts.build_harness_invocation(
            args.harness,
            cwd=verification_context.root,
            plan_dir=snapshot.plan_dir,
            unsafe_bypass=run_selection._unsafe_bypass(args),
            last_message_path=last_message_path,
            model=run_selection._requested_model(args, harness=args.harness),
            effort=args.effort,
            prompt=prompt,
        )
    except config.ConfigError as error:
        return _print_doctor_error(
            args,
            reason="config-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )
    except harnesses.HarnessError as error:
        return _print_doctor_error(
            args,
            reason="harness-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )
    except OSError as error:
        return _print_doctor_error(
            args,
            reason="os-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )

    if args.json:
        doctor_fields: dict[str, Any] = {}
        if verification_context.workspace is not None:
            doctor_fields = {
                "workspace_root": str(verification_context.root),
                "registry_digest": verification_context.registry_digest,
                "repositories": [
                    {
                        "alias": repository.alias,
                        "path": repository.repository.path,
                        "origin": repository.origin,
                        "resolved_root": str(repository.root),
                    }
                    for repository in verification_context.workspace.repositories
                ],
            }
        responses.print_json(
            responses.success_payload(
                "doctor",
                reason="ok",
                harness=adapter.name,
                **model_effort,
                harness_config=harness_config,
                executable=str(executable_path),
                resolved_executable=str(executable_path),
                cwd=str(cwd),
                plan_dir=str(snapshot.plan_dir),
                next_pending=(
                    _session_compat_value(snapshot.next_pending.id)
                    if snapshot.next_pending is not None
                    else None
                ),
                next_pending_id=(
                    snapshot.next_pending.id
                    if snapshot.next_pending is not None
                    else None
                ),
                prompt=prompt,
                invocation=run_commit._build_invocation_payload(
                    invocation,
                    model_effort=model_effort,
                    verification_context=verification_context,
                ),
                invocation_argv=invocation.argv,
                **doctor_fields,
            )
        )
        return 0

    print(f"Harness: {adapter.name}")
    _print_model_effort_lines(model_effort)
    print(f"Harness config: {harness_config['path']}")
    configured_harnesses = harness_config["configured_harnesses"]
    if configured_harnesses:
        print(f"Configured harnesses: {', '.join(configured_harnesses)}")
    else:
        print("Configured harnesses: none")
    print(f"Executable: {executable_path}")
    if verification_context.workspace is None:
        print(f"CWD: {cwd}")
    else:
        print(f"Workspace root: {cwd}")
        print("Repositories:")
        visible, omitted = workspaces.bounded_repository_rows(
            verification_context.workspace.repositories
        )
        for repository in visible:
            print(
                f"  {repository.alias}: {repository.repository.path} -> "
                f"{repository.root} ({repository.origin})"
            )
        if omitted:
            print(f"  ... {omitted} more repositories")
    print(f"Plan: {snapshot.plan_dir}")
    if snapshot.next_pending is None:
        print("Next pending: none")
    else:
        print(f"Next pending: session {snapshot.next_pending.id}")
    print(f"Prompt: {prompt}")
    print(f"Transport: {invocation.transport}")
    print("Argv:")
    for arg in invocation.argv:
        print(f"  {arg}")
    if invocation.turn_plan is not None:
        # Argv no longer states the access profile under this transport, so a
        # doctor report that printed only argv would no longer show what the
        # turn is allowed to do.
        print("Turn plan:")
        payload = json.dumps(
            invocation.turn_plan.to_payload(), indent=2, sort_keys=True
        )
        for line in payload.splitlines():
            print(f"  {line}")
    return 0
