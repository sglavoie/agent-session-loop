"""The ratchet that keeps ``agent_session_loop/runner.py`` shrinking.

The 38-session extraction program moves roughly 17,400 lines out of the
runner and into package modules behind the typed-boundary contract. Nothing
in the suite noticed the runner *growing*, so a session could pay the cost of
a move and hand back a file that was no smaller than the one it started from.
This module is that missing gate. It measures the runner's source rather than
its imported namespace, so it stays a pure lint: no package import, no
subprocess, and a failure that names the literal to edit.

Three quantities are ratcheted, each for a different way the target can be
missed:

* **Lines.** The blunt measure the target is stated in. A session that adds a
  line to the runner fails here even if every other rule holds.
* **Public module-level names the runner defines.** Line count alone would let
  a session shed bulk while leaving the re-export surface intact, which is the
  end state the plan explicitly rejects ("a runner that is nothing but a
  re-export table"). Names bound by ``import`` are deliberately excluded: the
  500-line runner keeps its imports, and an extraction that leaves behind
  delegation legitimately adds one.
* **Alias assignments.** ``X = <module>.Y`` is the convention this program
  reverses. The 228 that exist are recorded as a floor set that may only
  shrink; Session 38 empties it. A plain ``from .run_errors import RunError``
  binds a name without creating an alias assignment, which is why the rule is
  written against the assignment's shape rather than against the name.

The per-session step is one appended row in :data:`RUNNER_SHRINK_HISTORY` plus
whatever alias rows the session retired. The history is what makes "may only
fall" structural rather than a matter of review: each row is compared against
the one before it, so lowering a ceiling is an append and raising one is a
visible rewrite of recorded facts.
"""

from __future__ import annotations

import ast
import itertools
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "agent_session_loop" / "runner.py"

#: The end state the program is aiming at: a docstring, imports, and thin
#: delegation. Session 38 removes the skip on the test that asserts it.
RUNNER_LINE_TARGET = 500

#: One row per session that changes the runner: ``(session, lines, public
#: names)``. Oldest first; append, never edit an earlier row. Both counts are
#: compared against their predecessor, so a row that raises either one fails.
#: The last row is the live ceiling and must match the file exactly, which is
#: how a session that shrank the runner is made to record it.
RUNNER_SHRINK_HISTORY: tuple[tuple[str, int, int], ...] = (
    # session, runner lines, public module-level names the runner defines
    ("2", 17906, 197),
    ("3", 17242, 176),
    ("4", 16722, 170),
    ("5", 16206, 162),
    ("6", 15760, 151),
    ("7", 15309, 145),
    ("8", 14972, 142),
    ("9", 14396, 140),
    ("10", 13885, 136),
    ("11", 13229, 134),
    ("12", 12596, 131),
    ("13", 11998, 128),
    ("14", 11517, 124),
    ("15", 10983, 124),
    ("16", 10436, 124),
    ("17", 9987, 124),
    ("18", 9472, 124),
)

#: Every ``X = <module>.Y`` assignment in the runner today, mapped to the
#: module that owns the symbol. A session that moves a symbol deletes its row
#: here along with the assignment; no session may add one.
RUNNER_ALIAS_FLOOR: dict[str, str] = {
    "ABORT_TURN_DEFAULT_REASON": "run_coordination",
    "AGENT_MUX_FINISH_EVENT": "agent_mux_delivery",
    "AGENT_MUX_FINISH_REASON": "agent_mux_delivery",
    "AGENT_MUX_NOTIFY_ENV_VAR": "agent_mux_delivery",
    "AGENT_MUX_NOTIFY_OFF": "agent_mux_delivery",
    "AGENT_MUX_NOTIFY_TIMEOUT_ENV_VAR": "agent_mux_delivery",
    "AGENT_MUX_NOTIFY_TIMEOUT_SECONDS": "agent_mux_delivery",
    "AGENT_MUX_PAUSE_EVENT": "agent_mux_delivery",
    "AGENT_MUX_PAUSE_REASON": "agent_mux_delivery",
    "AGENT_MUX_PHASE_BY_EVENT": "agent_mux_delivery",
    "AGENT_MUX_SOURCE": "agent_mux_delivery",
    "AGENT_MUX_START_EVENT": "agent_mux_delivery",
    "AGENT_MUX_START_REASON": "agent_mux_delivery",
    "AGENT_MUX_TURN_FINISH_EVENT": "agent_mux_delivery",
    "AGENT_MUX_TURN_FINISH_REASON": "agent_mux_delivery",
    "AGENT_MUX_TURN_HANG_CLEARED_EVENT": "agent_mux_delivery",
    "AGENT_MUX_TURN_HANG_CLEARED_REASON": "agent_mux_delivery",
    "AGENT_MUX_TURN_HANG_SUSPECTED_EVENT": "agent_mux_delivery",
    "AGENT_MUX_TURN_HANG_SUSPECTED_REASON": "agent_mux_delivery",
    "AGENT_MUX_TURN_HEARTBEAT_EVENT": "agent_mux_delivery",
    "AGENT_MUX_TURN_HEARTBEAT_REASON": "agent_mux_delivery",
    "AGENT_MUX_TURN_START_EVENT": "agent_mux_delivery",
    "AGENT_MUX_TURN_START_REASON": "agent_mux_delivery",
    "AGENT_MUX_VERIFICATION_FINISH_EVENT": "agent_mux_delivery",
    "AGENT_MUX_VERIFICATION_FINISH_REASON": "agent_mux_delivery",
    "AGENT_MUX_VERIFICATION_HEARTBEAT_EVENT": "agent_mux_delivery",
    "AGENT_MUX_VERIFICATION_HEARTBEAT_REASON": "agent_mux_delivery",
    "AGENT_MUX_VERIFICATION_START_EVENT": "agent_mux_delivery",
    "AGENT_MUX_VERIFICATION_START_REASON": "agent_mux_delivery",
    "AGENT_MUX_WAIT_END_EVENT": "agent_mux_delivery",
    "AGENT_MUX_WAIT_END_REASON": "agent_mux_delivery",
    "AGENT_MUX_WAIT_START_EVENT": "agent_mux_delivery",
    "AGENT_MUX_WAIT_START_REASON": "agent_mux_delivery",
    "APPROVAL_SIGNAL_PATTERNS": "turn_execution",
    "BROAD_REVIEW_CHECK_PREFIX": "verification_transitions",
    "FAILURE_EVIDENCE_MAX_CHARS": "harness_failures",
    "FINAL_REVIEW_ARTIFACT_FILENAME": "review",
    "FINAL_REVIEW_FILENAME": "review",
    "FailureClassification": "harness_failures",
    "GENERATED_REVIEW_SESSION_VERSION": "review",
    "HANG_CONFIRM_SAMPLES": "turn_execution",
    "HANG_CONFIRM_SAMPLES_ENV_VAR": "turn_execution",
    "HANG_CPU_EPSILON_SECONDS": "turn_execution",
    "HANG_REASON_RESULT_RECEIVED": "turn_execution",
    "HANG_REASON_ZERO_CPU": "turn_execution",
    "HANG_RESULT_GRACE_ENV_VAR": "turn_execution",
    "HANG_RESULT_GRACE_SECONDS": "turn_execution",
    "HANG_TRIGGER_IDLE_ENV_VAR": "turn_execution",
    "HANG_TRIGGER_IDLE_SECONDS": "turn_execution",
    "HEARTBEAT_INTERVAL_SECONDS": "turn_execution",
    "INTERRUPTED_EXIT": "turn_execution",
    "LIVE_CONTROL_POLL_INTERVAL_SECONDS": "turn_execution",
    "MAILBOX_QUESTION_TEXT_MAX_CHARS": "run_coordination",
    "MAILBOX_REPLY_WAIT_BASE_ENV_VAR": "run_coordination",
    "MAILBOX_REPLY_WAIT_BASE_SECONDS": "run_coordination",
    "MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR": "run_coordination",
    "MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS": "run_coordination",
    "MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR": "run_coordination",
    "MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS": "run_coordination",
    "MAILBOX_REPLY_WAIT_POLL_ENV_VAR": "run_coordination",
    "MAILBOX_REPLY_WAIT_POLL_SECONDS": "run_coordination",
    "MAILBOX_REPLY_WAIT_PREPARATION_REASONS": "run_coordination",
    "MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST": "run_coordination",
    "MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS": "run_coordination",
    "MAILBOX_REPLY_WAIT_SUSPEND_SKEW_SECONDS": "run_coordination",
    "MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR": "run_coordination",
    "MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS": "run_coordination",
    "MAX_RENDERED_GUIDANCE": "mailbox_rendering",
    "MAX_RENDERED_GUIDANCE_BYTES": "mailbox_rendering",
    "MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS": "review_transitions",
    "MAX_VERIFICATION_REPAIR_ATTEMPTS": "verification_transitions",
    "MULTI_REPO_CAPABILITY_PAUSE_REASON": "events",
    "REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION": "review",
    "REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION": "review",
    "REQUIRED_CHECKS_INCOMPLETE_REASON": "verification_transitions",
    "RESULT_SCHEMA_VERSION": "turn_execution",
    "REVIEW_ARTIFACT_VERSION": "review",
    "REVIEW_BROAD_FAILURES_DEFERRED_REASON": "verification_transitions",
    "REVIEW_CYCLE_DISPOSITIONS": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_ALL_CLEAR": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_MALFORMED": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_REPAIRS": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_REPEATED": "review_continuation",
    "REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED": "review_continuation",
    "REVIEW_FINDINGS_UNRESOLVED_REASON": "review_transitions",
    "REVIEW_NOT_CLEAR_REASON": "verification_transitions",
    "REVIEW_SESSION_MARKER": "review",
    "REVIEW_STATUS_ALL_CLEAR": "review",
    "REVIEW_VERDICT_FINGERPRINT_PREFIX": "review_transitions",
    "RUNNER_STATUS_SOURCE_ABSENT": "turn_execution",
    "RUN_CONTROL_EVENT_ACTIONS": "run_coordination",
    "RUN_CONTROL_EVENT_MESSAGES": "run_coordination",
    "SESSION_KINDS": "plans",
    "SESSION_KIND_IMPLEMENTATION": "plans",
    "SESSION_KIND_REVIEW": "plans",
    "STATE_SCHEMA_VERSION": "agent_mux_projection",
    "STRUCTURED_REVIEW_MIN_VERSION": "review",
    "TRUSTED_RUNNER_SIGNAL_SOURCES": "turn_execution",
    "TurnCommit": "turn_execution",
    "TurnExecution": "turn_execution",
    "TurnExecutor": "turn_execution",
    "TurnFinalization": "turn_execution",
    "TurnIdentity": "turn_execution",
    "TurnLaunch": "turn_execution",
    "VERIFICATION_FINGERPRINT_PREFIX": "verification_transitions",
    "_AUDITABLE_ASSURANCE_STATES": "run_coordination",
    "_DEGRADED_ASSURANCE_STATES": "run_coordination",
    "_HangAssessment": "turn_execution",
    "_HangSentinel": "turn_execution",
    "_LIVE_ACTION_REQUIRED_REASONS": "turn_execution",
    "_MAILBOX_TOKEN_UNSEEN": "run_coordination",
    "_MailboxPoll": "run_coordination",
    "_MailboxReplyWaitResult": "run_coordination",
    "_MailboxReplyWaitSettings": "run_coordination",
    "_MailboxReplyWaitUnavailable": "run_coordination",
    "_PLAN_COMPLETION_REASONS": "verification_transitions",
    "_PreparedMailboxReplyWait": "run_coordination",
    "_REVIEW_STATUS_FINDINGS_RE": "review",
    "_ReplyStatePreserver": "run_coordination",
    "_TerminalAuditContext": "run_coordination",
    "_TerminalBlocker": "verification_transitions",
    "_TurnCommitRejected": "turn_execution",
    "_abort_turn_guidance_text": "run_coordination",
    "_accepted_broad_failure_deferral": "verification_transitions",
    "_append_text": "durability",
    "_apply_external_control_command": "run_coordination",
    "_apply_run_control_action": "run_coordination",
    "_approval_match_is_content": "turn_execution",
    "_arm_requested_abort": "run_coordination",
    "_attach_assurance_report": "run_coordination",
    "_clear_live_action_required": "turn_execution",
    "_clear_waiting_for_reply": "run_coordination",
    "_commit_turn": "turn_execution",
    "_create_turn_logs": "turn_execution",
    "_degrade_abort_to_stop_after_current": "run_coordination",
    "_drain_external_control_queue": "run_coordination",
    "_duration_seconds": "clock",
    "_ensure_mailbox_question": "run_coordination",
    "_extract_prefixed_line": "turn_execution",
    "_final_review_is_clear": "verification_transitions",
    "_finalize_turn_execution": "turn_execution",
    "_finish_mailbox_reply_wait_lifecycle": "run_coordination",
    "_finite_positive_env": "run_coordination",
    "_initial_control_payload": "run_coordination",
    "_is_noop_runner_pause": "turn_execution",
    "_is_review_verdict_repair_exhausted": "review_transitions",
    "_is_verification_repair_exhausted": "verification_transitions",
    "_isoformat": "clock",
    "_lease_evidence": "run_coordination",
    "_live_action_signal": "turn_execution",
    "_live_stream_callback": "turn_execution",
    "_mailbox_question_id": "run_coordination",
    "_mailbox_question_text": "run_coordination",
    "_mailbox_reply_wait_settings": "run_coordination",
    "_observed_model_payload": "turn_execution",
    "_parse_datetime": "clock",
    "_pending_completed": "plans",
    "_persisted_result_argv": "turn_execution",
    "_prepare_mailbox_reply_wait": "run_coordination",
    "_prepare_stream_logs": "turn_execution",
    "_preview_text": "turn_execution",
    "_publication_snapshot": "turn_execution",
    "_read_optional_bytes": "run_coordination",
    "_read_prior_review_verdict_retries": "review_transitions",
    "_read_prior_verification_failures": "verification_transitions",
    "_read_verification_finished_events": "verification_transitions",
    "_rebase_waiting_deadlines": "run_coordination",
    "_record_control_command_observed": "run_coordination",
    "_record_degraded_assurance": "run_coordination",
    "_record_mailbox_reply_wait_preparation_failed": "run_coordination",
    "_record_run_control_action": "run_coordination",
    "_record_turn_rejected": "turn_execution",
    "_record_waiting_lease_activity": "run_coordination",
    "_recorded_review_cycles": "verification_transitions",
    "_reopened_snapshot_identity": "turn_execution",
    "_replace_turn_record": "turn_execution",
    "_reply_argv_placeholder": "turn_execution",
    "_resolution_argv_placeholder": "turn_execution",
    "_restore_optional_bytes": "run_coordination",
    "_review_verdict_fingerprint": "review_transitions",
    "_review_verdict_repair_references": "review_transitions",
    "_runner_pause_can_wait_for_mailbox_reply": "run_coordination",
    "_runner_pause_reason": "turn_execution",
    "_runner_signal_lines": "turn_execution",
    "_runner_signals": "turn_execution",
    "_sanitize_reply_argv": "turn_execution",
    "_sanitize_resolution_argv": "turn_execution",
    "_session_compat_value": "plans",
    "_session_details_payload": "plans",
    "_session_identity_payload": "plans",
    "_session_label": "plans",
    "_session_number_alias": "plans",
    "_session_path_label": "plans",
    "_session_payload": "plans",
    "_session_status_change_payload": "plans",
    "_sessions_with_status": "plans",
    "_set_waiting_for_reply": "run_coordination",
    "_signal_line_candidates": "turn_execution",
    "_signal_source_trusted": "turn_execution",
    "_snapshot_payload": "plans",
    "_status_changes": "plans",
    "_status_counts": "plans",
    "_status_map": "plans",
    "_terminal_blocker": "verification_transitions",
    "_turn_paths": "turn_execution",
    "_turn_status_result": "turn_execution",
    "_unpassed_run_verifications": "verification_transitions",
    "_unresolved_deferred_verifications": "verification_transitions",
    "_utc_iso_now": "clock",
    "_utc_now": "clock",
    "_utc_run_id": "clock",
    "_verification_failure_details": "verification_transitions",
    "_verification_failure_summary": "verification_transitions",
    "_verification_fingerprint": "verification_transitions",
    "_verification_repair_references": "verification_transitions",
    "_wait_for_mailbox_reply": "run_coordination",
    "_waiting_for_reply_payload": "run_coordination",
    "_worker_pause_resolution_for_turn": "run_coordination",
    "_write_bytes_file": "durability",
    "_write_json_file": "durability",
    "_write_text": "durability",
    "_write_turn_artifacts": "turn_execution",
    "classify_harness_failure": "harness_failures",
}


def runner_source() -> str:
    return RUNNER_PATH.read_text(encoding="utf-8")


def runner_line_count(source: str) -> int:
    return len(source.splitlines())


def module_level_public_names(tree: ast.Module) -> frozenset[str]:
    """The public names the runner *defines* at module level.

    Imports are excluded on purpose -- see the module docstring. Only the
    top-level body is walked: a name bound inside a function is not part of
    the module's surface, and the runner has no conditional top-level block.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(name for name in names if not name.startswith("_"))


def module_level_aliases(tree: ast.Module) -> dict[str, str]:
    """Every ``X = <module>.Y`` re-export, mapped to the module it reads.

    The shape is the rule: a module-level assignment whose value is an
    attribute of a bare name that the runner imported as a module. That
    excludes ``CHECKOUT_ROOT = Path(__file__).resolve().parent.parent``, whose
    attribute chain bottoms out in a call, and it never sees a
    ``from .run_errors import RunError``, which is an import rather than an
    assignment.
    """
    modules = imported_module_names(tree)
    aliases: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        if not isinstance(value, ast.Attribute) or not isinstance(
            value.value, ast.Name
        ):
            continue
        if value.value.id not in modules:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = value.value.id
    return aliases


def imported_module_names(tree: ast.Module) -> frozenset[str]:
    """Names bound to a module rather than to a symbol inside one.

    ``import x``/``import x as y`` and the package's own ``from . import (...)``
    block bind modules; ``from .formatting import format_duration_seconds``
    binds a function, and reading an attribute off one of those is not a
    re-export of a moved symbol.
    """
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module is None:
            modules.update(alias.asname or alias.name for alias in node.names)
    return frozenset(modules)


class RunnerShrinkRatchetTests(unittest.TestCase):
    """The runner may only get smaller, and only by really moving symbols."""

    def setUp(self) -> None:
        self.source = runner_source()
        self.tree = ast.parse(self.source)

    def test_the_recorded_history_only_ever_falls(self) -> None:
        self.assertTrue(RUNNER_SHRINK_HISTORY, "the history may never be emptied")
        for previous, row in itertools.pairwise(RUNNER_SHRINK_HISTORY):
            session, lines, names = row
            self.assertLessEqual(
                lines,
                previous[1],
                f"session {session} recorded {lines} runner lines, up from "
                f"{previous[1]}; the ratchet only permits the count to fall",
            )
            self.assertLessEqual(
                names,
                previous[2],
                f"session {session} recorded {names} public runner names, up "
                f"from {previous[2]}; the ratchet only permits it to fall",
            )

    def test_the_runner_is_no_longer_than_the_recorded_ceiling(self) -> None:
        session, ceiling, _names = RUNNER_SHRINK_HISTORY[-1]
        actual = runner_line_count(self.source)

        self.assertEqual(
            actual,
            ceiling,
            f"runner.py is {actual} lines against a ceiling of {ceiling} "
            f"recorded by session {session}. Growing the runner is the "
            f"regression this test exists to catch; a session that shrank it "
            f"appends its own row to RUNNER_SHRINK_HISTORY.",
        )

    def test_the_runners_public_surface_is_no_wider_than_the_ceiling(self) -> None:
        session, _lines, ceiling = RUNNER_SHRINK_HISTORY[-1]
        names = module_level_public_names(self.tree)

        self.assertEqual(
            len(names),
            ceiling,
            f"runner.py defines {len(names)} public module-level names "
            f"against a ceiling of {ceiling} recorded by session {session}. "
            f"An extraction that moved a public symbol appends its own row to "
            f"RUNNER_SHRINK_HISTORY.",
        )

    def test_no_new_alias_assignment_is_added_to_the_runner(self) -> None:
        """The convention this program reverses may not be extended."""
        added = set(module_level_aliases(self.tree).items()) - set(
            RUNNER_ALIAS_FLOOR.items()
        )

        self.assertEqual(
            added,
            set(),
            "these `X = <module>.Y` assignments are new in runner.py; a "
            "session that moves a symbol moves its references too and leaves "
            "no alias behind",
        )

    def test_the_alias_floor_records_only_live_aliases(self) -> None:
        """Retiring an alias means deleting its row, not leaving it recorded."""
        retired = set(RUNNER_ALIAS_FLOOR) - set(module_level_aliases(self.tree))

        self.assertEqual(
            retired,
            set(),
            "these aliases are gone from runner.py but still recorded in "
            "RUNNER_ALIAS_FLOOR; delete their rows so the floor keeps "
            "describing the file",
        )

    @unittest.skip(
        f"the extraction target: runner.py at most {RUNNER_LINE_TARGET} lines. "
        f"Session 38 removes this skip."
    )
    def test_the_runner_is_at_most_the_target_length(self) -> None:
        actual = runner_line_count(self.source)

        self.assertLessEqual(
            actual,
            RUNNER_LINE_TARGET,
            f"runner.py is {actual} lines; the target is {RUNNER_LINE_TARGET}",
        )


if __name__ == "__main__":
    unittest.main()
