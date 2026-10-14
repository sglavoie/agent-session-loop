"""Parallel shard driver for the agent_session_loop test suite.

``just test-fast`` runs the unit/schema/parser modules; ``just test-all`` runs
every test module plus the standalone helper-script suites, one isolated
subprocess per shard. Shards are ordered longest-first so the slowest module
starts immediately and bounds wall-clock time.

``just test-portable`` runs the subset that needs nothing but the Python
interpreter, which is what a non-macOS CI lane can honestly gate on.

Usage:
    python3 -m tests.run_suite --fast
    python3 -m tests.run_suite --portable
    python3 -m tests.run_suite --all [--jobs N]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# Unit/schema/parser modules: no fake-harness runs, no HTTP servers, no
# multi-second subprocess orchestration. Ordered longest-first by measured
# shard duration: the pool starts shards in list order, so the slowest ones
# must not be left to start last and stretch wall-clock time.
FAST_MODULES = (
    "tests.test_asl_cli_check_command",
    "tests.test_asl_cli_commit",
    "tests.test_asl_cli_harness_adapter_commands",
    "tests.test_asl_cli_json_doctor_unlock_sessions",
    "tests.test_asl_cli_json_plans_listing",
    "tests.test_asl_cli_json_state_maintenance",
    "tests.test_asl_cli_json_status_and_dry_run",
    "tests.test_asl_cli_multi_repo_commit_recovery",
    "tests.test_asl_cli_multi_repo_read_only_and_guard",
    "tests.test_asl_cli_multi_repo_scope_violation",
    "tests.test_asl_cli_multi_repo_workspace_and_review",
    "tests.test_asl_cli_parser_defaults_and_supervisor_guide",
    "tests.test_asl_cli_parser_review_guard_and_resume_overlay",
    "tests.test_asl_cli_parser_subcommand_registration",
    "tests.test_asl_cli_register_repositories",
    "tests.test_asl_cli_register_repositories_source_plan",
    "tests.test_asl_cli_review_session_helpers",
    "tests.test_asl_cli_run_parser_validation",
    "tests.test_asl_cli_runner_signals_and_harness_config",
    "tests.test_asl_cli_split_command",
    "tests.test_asl_cli_status_label_resolution",
    "tests.test_asl_turn_commit_boundary",
    "tests.test_asl_turn_commit_paused_message",
    "tests.test_asl_turn_commit_paused_settlement",
    "tests.test_asl_turn_commit_paused_record",
    "tests.test_asl_turn_commit_run_acceptance",
    "tests.test_asl_turn_commit_run_subprocess",
    "tests.test_asl_turn_commit_run_rejection",
    "tests.test_asl_turn_commit_failure",
    "tests.test_asl_turn_commit_targets",
    "tests.test_asl_turn_commit_snapshots",
    "tests.test_asl_supervisor_audit_agent_engine_answers",
    "tests.test_asl_supervisor_audit_agent_engine_delivery",
    "tests.test_asl_supervisor_audit_agent_identity",
    "tests.test_asl_supervisor_audit_catalog_reports",
    "tests.test_asl_supervisor_audit_checks_catch_attribution",
    "tests.test_asl_supervisor_audit_checks_commit_failures",
    "tests.test_asl_supervisor_audit_checks_restore_and_aborts",
    "tests.test_asl_supervisor_audit_checks_settlement",
    "tests.test_asl_supervisor_audit_checks_verification",
    "tests.test_asl_supervisor_audit_checks_write_scope",
    "tests.test_asl_supervisor_audit_commit_observability",
    "tests.test_asl_supervisor_audit_evidence_determinism",
    "tests.test_asl_supervisor_audit_evidence_guidance",
    "tests.test_asl_supervisor_audit_evidence_index",
    "tests.test_asl_supervisor_audit_history_projections",
    "tests.test_asl_supervisor_audit_restricted",
    "tests.test_asl_supervisor_audit_run_config",
    "tests.test_asl_supervisor_audit_run_output_state",
    "tests.test_asl_prepare",
    "tests.test_asl_prepare_cli",
    "tests.test_asl_prepare_expand_ingest_cli",
    "tests.test_asl_prepare_expansion_renderer",
    "tests.test_asl_prepare_legacy_repository_scope",
    "tests.test_asl_prepare_rebind_and_corrections",
    "tests.test_asl_prepare_registered_workspace",
    "tests.test_asl_prepare_resume_and_cache",
    "tests.test_asl_prepare_validation_packet_and_cache",
    "tests.test_asl_prepare_validation_waves",
    "tests.test_asl_scope_recovery",
    "tests.test_asl_control_queue",
    "tests.test_asl_control_mailbox_1",
    "tests.test_asl_control_mailbox_2",
    "tests.test_asl_control_mailbox_3",
    "tests.test_asl_control_mailbox_4",
    "tests.test_asl_control_mailbox_5",
    "tests.test_asl_control_drain",
    "tests.test_asl_control_stop_checkpoint",
    "tests.test_asl_control_abort_turn",
    "tests.test_asl_control_mailbox_poll_live_turn",
    "tests.test_asl_control_signal",
    "tests.test_asl_control_hang_sentinel_1",
    "tests.test_asl_control_hang_sentinel_2",
    "tests.test_asl_control_recovery_sentinel",
    "tests.test_asl_control_cli_1",
    "tests.test_asl_control_cli_2",
    "tests.test_asl_control_cli_wait",
    "tests.test_asl_control_mailbox_cli_1",
    "tests.test_asl_control_mailbox_cli_2",
    "tests.test_asl_control_mailbox_cli_3",
    "tests.test_asl_control_mailbox_cli_4",
    "tests.test_asl_control_mailbox_cli_5",
    "tests.test_asl_control_mailbox_cli_6",
    "tests.test_asl_control_mailbox_cli_7",
    "tests.test_asl_control_mailbox_cli_8",
    "tests.test_asl_control_mailbox_cli_9",
    "tests.test_asl_control_mailbox_cli_10",
    "tests.test_asl_control_run_control",
    "tests.test_asl_control_agent_mux_1",
    "tests.test_asl_control_agent_mux_2",
    "tests.test_asl_control_agent_mux_stale_lock",
    "tests.test_asl_overseer_checkpoint_groups",
    "tests.test_asl_redirect_continuation_journal",
    "tests.test_asl_redirect_continuation_disposition",
    "tests.test_asl_redirect_continuation_settle",
    "tests.test_asl_redirect_continuation_run",
    "tests.test_asl_redirect_continuation_delivery",
    "tests.test_asl_commit_message",
    "tests.test_asl_overseer_checkpoint",
    "tests.test_asl_repository_evidence",
    "tests.test_asl_preflight",
    "tests.test_asl_preflight_auto_repair",
    "tests.test_asl_preflight_baselines",
    "tests.test_asl_preflight_cli_mark_loop_ready",
    "tests.test_asl_preflight_cli_reports",
    "tests.test_asl_preflight_command_runnability",
    "tests.test_asl_preflight_environment",
    "tests.test_asl_preflight_multi_repository",
    "tests.test_asl_preflight_path_scope_and_progress",
    "tests.test_asl_preflight_repair_hints",
    "tests.test_asl_preflight_repository_footprint",
    "tests.test_asl_preflight_repository_registry",
    "tests.test_asl_preflight_resume",
    "tests.test_asl_assurance_lifecycle_terminal_outcomes",
    "tests.test_asl_assurance_lifecycle_terminal_audit",
    "tests.test_asl_assurance_lifecycle_coordinator",
    "tests.test_asl_assurance_lifecycle_catchup",
    "tests.test_asl_verification",
    "tests.test_asl_verification_artifact_storage",
    "tests.test_asl_verification_cli",
    "tests.test_asl_verification_command_diagnosis",
    "tests.test_asl_verification_contract_check_cli",
    "tests.test_asl_verification_failure_diagnostics",
    "tests.test_asl_verification_multi_repository",
    "tests.test_asl_verification_output_capture",
    "tests.test_asl_verification_parallel",
    "tests.test_asl_verification_run_and_observers",
    "tests.test_asl_overseer_cli_invocation",
    "tests.test_asl_overseer_cli_evidence",
    "tests.test_asl_overseer_cli_human_dependency",
    "tests.test_asl_overseer_cli_project_scope",
    "tests.test_asl_overseer_cli_freshness",
    "tests.test_asl_overseer_cli_verification",
    "tests.test_asl_overseer_resume",
    "tests.test_asl_overseer_watch",
    "tests.test_asl_overseer_mailbox",
    "tests.test_forgejo_sync",
    "tests.test_asl_core_locks_plan_refs",
    "tests.test_asl_core_mark_run_abandoned",
    "tests.test_asl_core_parser_state_1",
    "tests.test_asl_core_parser_state_10",
    "tests.test_asl_core_parser_state_11",
    "tests.test_asl_core_parser_state_12",
    "tests.test_asl_core_parser_state_13",
    "tests.test_asl_core_parser_state_2",
    "tests.test_asl_core_parser_state_3",
    "tests.test_asl_core_parser_state_4",
    "tests.test_asl_core_parser_state_5",
    "tests.test_asl_core_parser_state_6",
    "tests.test_asl_core_parser_state_7",
    "tests.test_asl_core_parser_state_8",
    "tests.test_asl_core_parser_state_9",
    "tests.test_asl_core_plan_events",
    "tests.test_asl_core_snapshots_reviews",
    "tests.test_asl_redirect_store",
    "tests.test_asl_redirect_adversarial",
    "tests.test_asl_redirect_control",
    "tests.test_asl_overseer_dispatch",
    "tests.test_asl_overseer_context",
    "tests.test_asl_review_delta",
    "tests.test_asl_continuation_guards",
    "tests.test_asl_repository_registration",
    "tests.test_asl_authoring_plan_rendering",
    "tests.test_asl_authoring_resplit",
    "tests.test_asl_authoring_split_footprint",
    "tests.test_asl_authoring_plan_parsing",
    "tests.test_asl_authoring_plan_manifest",
    "tests.test_asl_authoring_plan_lint",
    "tests.test_asl_authoring_plan_resolution",
    "tests.test_workbench_connector_command",
    "tests.test_workbench_connector_corpus",
    "tests.test_workbench_connector_delivery",
    "tests.test_workbench_connector_payloads",
    "tests.test_workbench_connector_privacy",
    "tests.test_asl_repository_intent",
    "tests.test_asl_repository_scope",
    "tests.test_asl_overseer_store",
    "tests.test_asl_typing_ratchet",
    "tests.test_asl_bridge_redirect",
    "tests.test_asl_assurance_cli",
    "tests.test_asl_baseline",
    "tests.test_asl_overseer_discovery",
    "tests.test_workbench_integrations",
    "tests.test_asl_startup_context",
    "tests.test_asl_agent_mux_projection",
    "tests.test_asl_docs_assurance_and_timing",
    "tests.test_asl_docs_authoritative_cli",
    "tests.test_asl_docs_authoritative_lanes",
    "tests.test_asl_docs_authoritative_pause_metrics",
    "tests.test_asl_docs_control_and_abort",
    "tests.test_asl_docs_guidance_and_errors",
    "tests.test_asl_docs_live_contract",
    "tests.test_asl_docs_mailbox_protocol",
    "tests.test_asl_docs_runner_facade",
    "tests.test_asl_docs_session_skills",
    "tests.test_asl_docs_skill_contracts",
    "tests.test_asl_docs_supervision",
    "tests.test_asl_docs_workbench_cli",
    "tests.test_asl_docs_workbench_triage",
    "tests.test_asl_docs_workflow_and_install",
    "tests.test_asl_agent_mux_contract",
    "tests.test_asl_state_migration",
    "tests.test_asl_assurance_projection_publication",
    "tests.test_asl_assurance_projection_identity",
    "tests.test_asl_assurance_projection_lifecycle",
    "tests.test_asl_assurance_projection_catalog",
    "tests.test_asl_assurance_projection_replay",
    "tests.test_asl_maintenance_compaction",
    "tests.test_asl_maintenance_job",
    "tests.test_asl_maintenance_policy",
    "tests.test_asl_maintenance_revalidation",
    "tests.test_asl_maintenance_run",
    "tests.test_asl_conversations",
    "tests.test_asl_redirect_ack",
    "tests.test_workbench_remote_actions_submission_queue",
    "tests.test_workbench_remote_actions_lane",
    "tests.test_workbench_remote_actions_restart",
    "tests.test_workbench_remote_actions_performer",
    "tests.test_asl_redirect_recovery",
    "tests.test_asl_conversation_resume",
    "tests.test_asl_retention",
    "tests.test_asl_overseer_isolation",
    "tests.test_workbench_http",
    "tests.test_asl_agent_mux_pairing",
    "tests.test_workbench_history",
    "tests.test_workbench_store_migration_schema",
    "tests.test_workbench_store_ingestion",
    "tests.test_workbench_store_catalog_retention",
    "tests.test_workbench_store_incidents",
    "tests.test_workbench_store_incident_delivery",
    "tests.test_asl_assurance_reconcile",
    "tests.test_asl_redirect_cli",
    "tests.test_asl_workbench_summary",
    "tests.test_asl_workspaces",
    "tests.test_asl_diagnostics_export",
    "tests.test_asl_review_guard",
    "tests.test_asl_acceptance",
    "tests.test_asl_claude_stream",
    "tests.test_asl_harness_compatibility",
    "tests.test_asl_assurance_ledger",
    "tests.test_asl_turn_finalization",
    "tests.test_asl_metrics_collection",
    "tests.test_asl_metrics_materialization",
    "tests.test_asl_metrics_cli",
    "tests.test_asl_metrics_cli_invocations",
    "tests.test_asl_metrics_execution_timing",
    "tests.test_asl_metrics_execution_timing_reconciliation",
    "tests.test_workbench_remote_reads",
    "tests.test_asl_bridge_projection",
    "tests.test_asl_tail",
    "tests.test_asl_guidance_delivery",
    "tests.test_asl_qna",
    "tests.test_skill_sync",
    "tests.test_asl_workbench_recovery",
    "tests.test_asl_workbench_archive",
    "tests.test_asl_prompts_task_packet",
    "tests.test_asl_prompts_pending_guidance",
    "tests.test_asl_prompts_reply_resolution",
    "tests.test_asl_prompts_harness_skill",
    "tests.test_asl_prompts_corrections",
    "tests.test_asl_run_termination",
    "tests.test_asl_external_write_evidence",
    "tests.test_session_overview",
    "tests.test_asl_handoff_guards",
    "tests.test_asl_turn_settlement",
    "tests.test_asl_redirect_admission",
    "tests.test_asl_assurance_metrics",
    "tests.test_asl_turn_execution",
    "tests.test_workbench_uplink",
    "tests.test_asl_turn_postflight",
    "tests.test_asl_progress",
    "tests.test_asl_compaction",
    "tests.test_asl_supervision_liveness",
    "tests.test_asl_turn_dispositions",
    "tests.test_asl_run_coordination",
    "tests.test_asl_turn_preparation",
    "tests.test_asl_run_entry",
    "tests.test_asl_run_outcome",
    "tests.test_asl_run_payloads",
    "tests.test_asl_run_contention",
    "tests.test_asl_turn_preflight",
    "tests.test_asl_live_verification",
    "tests.test_asl_review_cycle",
    "tests.test_asl_session_verification",
    "tests.test_asl_handoff_correction",
    "tests.test_asl_cli_trace",
    "tests.test_asl_run_loop",
    "tests.test_asl_run_doctor",
    "tests.test_asl_harness_lifecycle",
    "tests.test_asl_audit_replay",
    "tests.test_asl_readiness_gate",
    "tests.test_asl_write_guards",
    "tests.test_daily_remind",
    "tests.test_asl_run_completion",
    "tests.test_asl_workspace_evidence",
    "tests.test_asl_verification_transitions",
    "tests.test_asl_run_selection",
    "tests.test_asl_run_inputs",
    "tests.test_asl_run_persistence",
    "tests.test_asl_run_reporting",
    "tests.test_asl_commit_groups",
    "tests.test_asl_bridge_fixtures",
    "tests.test_asl_overseer_decisions",
    "tests.test_asl_review_session_body",
    "tests.test_asl_review_session_contract",
    "tests.test_asl_redirect_supervision",
    "tests.test_asl_overseer_history",
    "tests.test_asl_overseer_policy_client",
    "tests.test_asl_assurance_models",
    "tests.test_asl_review_transitions",
    "tests.test_workbench_transport_contract",
    "tests.test_asl_literals",
    "tests.test_asl_assurance_baseline",
    "tests.test_asl_transitions",
    "tests.test_asl_runner_shrink",
    "tests.test_asl_execution_timing",
    "tests.test_asl_process_tree",
)

# Modules that need nothing outside the Python interpreter: no `git`, no shell
# script, no launchd domain, no harness executable, no Kitty. This is what the
# Linux CI lane gates on, and the membership rule is mechanical rather than a
# judgement call -- a module belongs here when it spawns no external process at
# all. Everything else is left to the macOS lane, which has the real tools the
# product is developed and released against, so the portable lane never reports
# a pass it did not earn on a machine that has none of them.
#
# This is a strict subset of FAST_MODULES; `tests/test_asl_docs_authoritative_lanes.py`
# enforces that, so a module deleted or renamed cannot survive here as a silent skip.
PORTABLE_MODULES = (
    "tests.test_asl_cli_trace",
    "tests.test_asl_tail",
    "tests.test_asl_assurance_baseline",
    "tests.test_asl_assurance_reconcile",
    "tests.test_asl_assurance_metrics",
    "tests.test_asl_turn_execution",
    "tests.test_asl_guidance_delivery",
    "tests.test_asl_harness_lifecycle",
    "tests.test_asl_verification_transitions",
    "tests.test_asl_review_transitions",
    "tests.test_asl_review_session_contract",
    "tests.test_asl_review_session_body",
    "tests.test_asl_run_completion",
    "tests.test_asl_turn_dispositions",
    "tests.test_asl_turn_postflight",
    "tests.test_asl_turn_preflight",
    "tests.test_asl_turn_preparation",
    "tests.test_asl_turn_settlement",
    "tests.test_asl_run_selection",
    "tests.test_asl_run_inputs",
    "tests.test_asl_run_loop",
    "tests.test_asl_run_doctor",
    "tests.test_asl_run_entry",
    "tests.test_asl_run_outcome",
    "tests.test_asl_run_payloads",
    "tests.test_asl_run_persistence",
    "tests.test_asl_run_reporting",
    "tests.test_asl_live_verification",
    "tests.test_asl_run_termination",
    "tests.test_asl_readiness_gate",
    "tests.test_asl_redirect_store",
    "tests.test_asl_redirect_admission",
    "tests.test_asl_redirect_cli",
    "tests.test_asl_redirect_continuation_journal",
    "tests.test_asl_redirect_continuation_disposition",
    "tests.test_asl_redirect_continuation_settle",
    "tests.test_asl_redirect_continuation_run",
    "tests.test_asl_redirect_continuation_delivery",
    "tests.test_asl_redirect_ack",
    "tests.test_asl_redirect_recovery",
    "tests.test_asl_redirect_supervision",
    "tests.test_asl_external_write_evidence",
    "tests.test_asl_workspace_evidence",
    "tests.test_asl_write_guards",
    "tests.test_asl_session_verification",
    "tests.test_asl_handoff_correction",
    "tests.test_asl_transitions",
    "tests.test_asl_typing_ratchet",
    "tests.test_asl_runner_shrink",
    "tests.test_asl_commit_groups",
    "tests.test_asl_bridge_projection",
    "tests.test_asl_bridge_redirect",
    "tests.test_asl_bridge_fixtures",
    "tests.test_asl_agent_mux_contract",
    "tests.test_asl_agent_mux_projection",
    "tests.test_asl_agent_mux_pairing",
    "tests.test_asl_workbench_archive",
    "tests.test_asl_workbench_summary",
    "tests.test_asl_workbench_recovery",
    "tests.test_asl_progress",
    "tests.test_asl_metrics_collection",
    "tests.test_asl_metrics_materialization",
    "tests.test_asl_metrics_cli",
    "tests.test_asl_metrics_cli_invocations",
    "tests.test_asl_metrics_execution_timing",
    "tests.test_asl_metrics_execution_timing_reconciliation",
    "tests.test_asl_execution_timing",
    "tests.test_asl_state_migration",
    "tests.test_asl_conversations",
    "tests.test_asl_conversation_resume",
    "tests.test_asl_retention",
    "tests.test_asl_harness_compatibility",
    "tests.test_asl_startup_context",
    "tests.test_asl_overseer_cli_invocation",
    "tests.test_asl_overseer_cli_evidence",
    "tests.test_asl_overseer_cli_human_dependency",
    "tests.test_asl_overseer_cli_project_scope",
    "tests.test_asl_overseer_cli_freshness",
    "tests.test_asl_overseer_cli_verification",
    "tests.test_asl_overseer_context",
    "tests.test_asl_overseer_decisions",
    "tests.test_asl_overseer_discovery",
    "tests.test_asl_overseer_dispatch",
    "tests.test_asl_overseer_history",
    "tests.test_asl_overseer_isolation",
    "tests.test_asl_overseer_mailbox",
    "tests.test_asl_overseer_store",
    "tests.test_asl_overseer_policy_client",
    "tests.test_asl_overseer_resume",
    "tests.test_asl_overseer_watch",
    "tests.test_workbench_connector_command",
    "tests.test_workbench_connector_corpus",
    "tests.test_workbench_connector_delivery",
    "tests.test_workbench_connector_payloads",
    "tests.test_workbench_connector_privacy",
    "tests.test_workbench_history",
    "tests.test_workbench_http",
    "tests.test_workbench_integrations",
    "tests.test_workbench_remote_actions_submission_queue",
    "tests.test_workbench_remote_actions_lane",
    "tests.test_workbench_remote_actions_restart",
    "tests.test_workbench_remote_actions_performer",
    "tests.test_workbench_remote_reads",
    "tests.test_workbench_store_migration_schema",
    "tests.test_workbench_store_ingestion",
    "tests.test_workbench_store_catalog_retention",
    "tests.test_workbench_store_incidents",
    "tests.test_workbench_store_incident_delivery",
    "tests.test_workbench_transport_contract",
    "tests.test_workbench_uplink",
    "tests.test_asl_literals",
)

# Integration modules: fake-harness end-to-end runs, workbench HTTP servers,
# and subprocess-heavy flows. Ordered longest-first.
INTEGRATION_MODULES = (
    "tests.test_asl_redirect_restart",
    "tests.test_asl_spark",
    "tests.test_asl_harness_activity_and_pauses",
    "tests.test_asl_harness_agent_mux_notifications",
    "tests.test_asl_harness_audit_access_profile",
    "tests.test_asl_harness_commit_gated_retries",
    "tests.test_asl_harness_commit_group_recovery",
    "tests.test_asl_harness_commit_modes",
    "tests.test_asl_harness_external_queue_selection",
    "tests.test_asl_harness_failure_classification",
    "tests.test_asl_harness_fake_turns",
    "tests.test_asl_harness_hang_sentinel",
    "tests.test_asl_harness_idle_baseline",
    "tests.test_asl_harness_live_verification_finish",
    "tests.test_asl_harness_live_verification_heartbeat",
    "tests.test_asl_harness_model_control",
    "tests.test_asl_harness_process_group_abort",
    "tests.test_asl_harness_process_sweep_recovery",
    "tests.test_asl_harness_progress_heartbeat",
    "tests.test_asl_harness_pty_select_and_tail",
    "tests.test_asl_harness_pty_stop_and_help",
    "tests.test_asl_harness_reader_join_hooks",
    "tests.test_asl_harness_run_control",
    "tests.test_asl_harness_status_snapshots",
    "tests.test_asl_codex_app_server",
    "tests.test_asl_copilot_acp",
    "tests.test_asl_copilot_resume",
    "tests.test_asl_native_restore_recovery",
    "tests.test_asl_lost_submission_response",
    "tests.test_asl_workbench_server_http",
    "tests.test_asl_workbench_server_queue",
    "tests.test_asl_workbench_server_queue_sort",
    "tests.test_asl_workbench_server_recovery",
    "tests.test_asl_workbench_server_registry",
    "tests.test_asl_workbench_server_archive",
    "tests.test_asl_workbench_server_launch_reply",
    "tests.test_asl_workbench_server_control",
    "tests.test_asl_workbench_server_questions",
    "tests.test_asl_workbench_server_run_state",
    "tests.test_asl_workbench_server_run_results",
    "tests.test_asl_workbench_redirect",
    "tests.test_asl_review_delta_rescoping",
    "tests.test_asl_review_delta_scope_computation",
    "tests.test_asl_review_fingerprint_diff_evidence",
    "tests.test_asl_review_fingerprint_excludes_file",
    "tests.test_asl_review_fingerprint_exclusions",
    "tests.test_asl_review_fingerprint_ignore_sources",
    "tests.test_asl_review_fingerprint_index_and_git_failures",
    "tests.test_asl_review_guard_failed_turn_outcome",
    "tests.test_asl_review_guard_unavailable_run",
    "tests.test_asl_review_lifecycle_broad_gate_deferral",
    "tests.test_asl_review_lifecycle_final_review_pauses",
    "tests.test_asl_review_lifecycle_harness_pairs",
    "tests.test_asl_review_lifecycle_mutation_guard",
    "tests.test_asl_review_lifecycle_rescope_and_options",
    "tests.test_asl_review_lifecycle_session_creation",
    "tests.test_asl_review_lifecycle_session_events",
    "tests.test_asl_review_lifecycle_verdict_retries",
    "tests.test_asl_review_repair_group_attempt_history",
    "tests.test_asl_review_revert_snapshot_cache",
    "tests.test_asl_release_integration",
    "tests.test_asl_workbench_static_assets",
    "tests.test_asl_workbench_static_shell",
    "tests.test_asl_workbench_static_queue_state",
    "tests.test_asl_workbench_static_queue_views",
    "tests.test_asl_workbench_static_layout",
    "tests.test_asl_workbench_static_interactions",
    "tests.test_asl_workbench_static_redirect",
    "tests.test_asl_workbench_hardening_cleanup",
    "tests.test_asl_workbench_hardening_runtime",
    "tests.test_asl_workbench_cli_commands",
    "tests.test_asl_repair_dispatch",
    "tests.test_asl_repair_regeneration_integration",
    "tests.test_asl_repair_scope_regeneration",
    "tests.test_asl_run_disposition_source",
    "tests.test_asl_run_handoff_status",
    "tests.test_asl_run_interrupt_and_journal",
    "tests.test_asl_run_launch_gates",
    "tests.test_asl_run_legacy_implementation_guard",
    "tests.test_asl_run_protected_path_guard",
    "tests.test_asl_run_readiness_auto_repair",
    "tests.test_asl_run_readiness_hard_gate",
    "tests.test_asl_run_readiness_lifecycle",
    "tests.test_asl_run_readiness_preflight",
    "tests.test_asl_run_verification_enforcement",
    "tests.test_asl_run_verification_heartbeat",
    "tests.test_asl_run_workspace_guard_exceptions",
    "tests.test_asl_settled_pause_commit_gate",
    "tests.test_asl_guards",
    "tests.test_asl_reply_flows_agent_mux_notifications",
    "tests.test_asl_reply_flows_e2e_live_resolution",
    "tests.test_asl_reply_flows_e2e_reply_resume",
    "tests.test_asl_reply_flows_exhausted_mailbox",
    "tests.test_asl_reply_flows_mailbox_wait_notifications",
    "tests.test_asl_reply_flows_prompts",
    "tests.test_asl_reply_flows_question_bounds",
    "tests.test_asl_reply_flows_run_resume_config",
    "tests.test_asl_reply_flows_runner_resolution",
    "tests.test_asl_reply_flows_stdin",
    "tests.test_asl_reply_flows_suspend_grace",
    "tests.test_asl_reply_flows_wait_evidence",
    "tests.test_asl_reply_flows_wait_extension",
    "tests.test_asl_e2e_kiln_twin_clone",
    "tests.test_asl_e2e_lifecycle",
    "tests.test_asl_e2e_multi_repo_commits_review",
    "tests.test_asl_e2e_multi_repo_lifecycle",
    "tests.test_asl_e2e_review_reopen",
    "tests.test_asl_e2e_verification_repair",
    "tests.test_asl_worker_verification",
    "tests.test_asl_review_artifact_schema",
    "tests.test_asl_review_artifact_verdicts",
    "tests.test_asl_review_cycle_advance",
    "tests.test_asl_review_cycle_baselines",
    "tests.test_asl_review_cycle_caps",
    "tests.test_asl_review_cycle_crash_recovery",
    "tests.test_asl_review_cycle_fingerprints",
    "tests.test_asl_review_cycle_lineage",
    "tests.test_asl_review_cycle_reopen",
    "tests.test_asl_review_repair_child_scope",
    "tests.test_asl_review_repair_contract_gate",
    "tests.test_asl_review_repair_gate_and_continuation",
    "tests.test_asl_review_repair_materializer",
    "tests.test_asl_review_repair_pause_blockers",
    "tests.test_asl_review_repair_publication_window",
    "tests.test_asl_review_repair_registered_repos",
    "tests.test_asl_review_replay_and_verdict_command",
    "tests.test_asl_workbench_launch",
    # Integration rather than portable: it binds a loopback server and spawns
    # a fresh interpreter to prove the portable package loads without ASL.
    "tests.test_workbench_foundations",
    # Final cross-repository ownership audit: it reads the registered Pi
    # checkout to prove the retired implementation is gone and its historical
    # evidence remains, so it cannot run in a scripts-only portable checkout.
    "tests.test_workbench_consolidation",
    # Same shape, one step further out: the production artifact is unpacked and
    # the packaged application is started as a release, so this lane needs a
    # socket and child interpreters. It is also the one module the Pi's own
    # interpreter runs -- `python3.13 -m unittest -q tests.test_workbench_portable`
    # -- which is a separate lane from either of the two lists above.
    "tests.test_workbench_portable",
    # Integration for the plainest reason: git, `plutil`, the installed `asl`
    # executable, and a service release that is genuinely started and stopped.
    "tests.test_workbench_lifecycle",
    # Half of it is pure translation, but the rollback half installs two real
    # releases and starts them, because "the data a release is handed is the
    # data it ran on" cannot be proven against a switch that was faked.
    "tests.test_workbench_migration",
    # Same reason: most of it is pure catalog algebra, but the reads it proves
    # are observational are proven against a live workbench server.
    "tests.test_workbench_catalog",
    # Same again for the detail reads: containment and "a read changes
    # nothing" are only worth asserting against the routes that serve them.
    "tests.test_workbench_details",
    # Integration for a third reason: the guarantee is cross-process, so
    # proving it takes real child interpreters racing a real flock, and the
    # route half needs a live server.
    "tests.test_workbench_operations",
    # Integration because the claim is that two surfaces are one path: half of
    # it drives the shared action directly and half drives a live workbench
    # server, and the point is that both land in the same journal.
    "tests.test_workbench_launch_reply",
    # And for the same reason once more, for the controls: the run controls and
    # the redirect journal are driven here through the shared service and
    # through both surfaces that call it, so a divergence shows up as a failure
    # rather than as two passing suites.
    "tests.test_workbench_controls",
    # And once more for the archive: "browsing never deletes" is a claim about
    # what a real GET does to a real archive tree, so it is asserted against a
    # live server reading real entries rather than against a stub listing.
    "tests.test_workbench_archive",
    # Integration because there is no other way to ask the question: the claim
    # is that one front end serves two deployments, so both providers are bound
    # to real loopback sockets over one plan corpus and asked the same things.
    "tests.test_workbench_shell",
    # And once more for the overview: the vocabulary halves and the selection
    # algebra would run anywhere, but "both modes present one body of work" is
    # only a claim about two providers answering the same questions, so both
    # are bound to loopback sockets over one corpus here too.
    "tests.test_workbench_overview",
    # And once more for the mutations: what a mode will carry out, whether its
    # advance warning matches its refusal, and what a recorded-but-unconfirmed
    # operation is allowed to claim are all questions about two live providers
    # answering the same request, so both are bound here too.
    "tests.test_workbench_interactions",
    # Integration because the gate it proves is enforced by real git history
    # and real packed artifacts: synthetic repositories are built and their
    # commits packed, because a fake digest would agree with a fake packer.
    "tests.test_workbench_acceptance",
    # The harness on the other side of the same document. Integration for the
    # same reason: it builds the release's own native definitions and restages
    # them, so it exercises the packaging code rather than a description of it.
    "tests.test_workbench_staging",
    # The harness that drives the cutover the rehearsal above was a rehearsal
    # for. Integration for the same reason again, and one of its own: it writes
    # real prefixes and takes real SQLite backups of them, because a backup
    # proved against a fake database is a claim about the fake.
    "tests.test_workbench_cutover",
    # Integration in the fullest sense the repository has: one plan corpus, the
    # real observation source, a connector, a link that can be made to fail, the
    # Pi's store, and the remote provider behind a loopback socket, with the
    # launch children replaced by an inert interpreter rather than the launch
    # path. Nothing here would tell the truth without all of it running.
    "tests.test_workbench_fullstack_corpus_link",
    "tests.test_workbench_fullstack_mutations",
    "tests.test_workbench_fullstack_robustness",
    "tests.test_workbench_fullstack_resources",
    # The one hop the module above supplies for itself. Integration because it
    # is nothing but real moving parts: a listening service, a connector loop
    # in its own thread, a real clock, and bounded waits for frames to cross a
    # socket. The lane that runs on a frozen clock cannot hold it.
    "tests.test_workbench_wire",
    "tests.test_asl_pause_ux",
    "tests.test_asl_bridge_server_capabilities_rows",
    "tests.test_asl_bridge_server_replies",
    "tests.test_asl_bridge_server_control_resume",
    "tests.test_asl_bridge_server_projection_cadence",
    "tests.test_asl_bridge_server_lifecycle",
    "tests.test_asl_overseer_e2e",
    "tests.test_asl_overseer_paired_smoke",
    "tests.test_brainnotes_vault_guard",
    "tests.test_launchagents_setup",
)

# Standalone helper-script suites (not part of the tests package).
SCRIPT_SUITES = (
    "skills/asl/scripts/test_asl_autopilot_attached_mailbox_watcher.py",
    "skills/asl/scripts/test_asl_autopilot_cli.py",
    "skills/asl/scripts/test_asl_autopilot_detach.py",
    "skills/asl/scripts/test_asl_autopilot_events_tailer_hang.py",
    "skills/asl/scripts/test_asl_autopilot_fake_asl.py",
    "skills/asl/scripts/test_asl_autopilot_hang_escalation.py",
    "skills/asl/scripts/test_asl_autopilot_heartbeat_and_tailer.py",
    "skills/asl/scripts/test_asl_autopilot_mailbox_watcher_lease.py",
    "skills/asl/scripts/test_asl_autopilot_mailbox_watcher_projection.py",
    "skills/asl/scripts/test_asl_autopilot_quota_wait.py",
    "skills/asl/scripts/test_asl_autopilot_review_guidance.py",
    "skills/asl/scripts/test_asl_autopilot_review_verdict.py",
    "skills/asl/scripts/test_asl_autopilot_run_parse.py",
    "skills/asl/scripts/test_asl_autopilot_supervise_mailbox_lease.py",
    "skills/asl/scripts/test_asl_autopilot_supervise_pause_retry.py",
    "skills/asl/scripts/test_asl_autopilot_supervise_resume_budget.py",
    "skills/asl/scripts/test_asl_autopilot_supervise_review_launch.py",
    "skills/asl/scripts/test_asl_autopilot_supervise_stop_control.py",
    "skills/asl/scripts/test_asl_autopilot_supervise_transport_and_attach.py",
    "skills/asl/scripts/test_asl_autopilot_supervision_end_records.py",
    "skills/asl/scripts/test_asl_autopilot_supervision_end_signals.py",
    "skills/asl/scripts/test_asl_autopilot_supervision_end_terminal_write.py",
    "skills/asl/scripts/test_asl_autopilot_worker_hung_reporting.py",
    "skills/session-resplit/scripts/test_resplit_session.py",
)

_RAN_PATTERN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)


def _modules_for(mode: str) -> tuple[str, ...]:
    if mode == "portable":
        return PORTABLE_MODULES
    if mode == "fast":
        return FAST_MODULES
    return FAST_MODULES + INTEGRATION_MODULES


def _shard_commands(mode: str) -> list[tuple[str, list[str]]]:
    fast_only = mode != "all"
    modules = _modules_for(mode)
    shards = [
        (module, [sys.executable, "-m", "unittest", module]) for module in modules
    ]
    if not fast_only:
        shards.extend(
            (path, [sys.executable, str(REPO_ROOT / path)]) for path in SCRIPT_SUITES
        )
    if not fast_only:
        # Longest-first across the combined list keeps the slowest shards at
        # the front of the queue.
        integration_first = [
            shard
            for module in INTEGRATION_MODULES
            for shard in shards
            if shard[0] == module
        ]
        rest = [shard for shard in shards if shard not in integration_first]
        shards = integration_first + rest
    return shards


def _run_shard(name: str, command: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    ran_match = _RAN_PATTERN.search(output)
    return {
        "name": name,
        "returncode": completed.returncode,
        "duration": time.monotonic() - started,
        "tests": int(ran_match.group(1)) if ran_match else 0,
        "output": output,
    }


def _discovered_package_modules() -> set[str]:
    """Every module broad discovery finds at least one test in.

    The registration ratchet used to compare test *counts* -- discovery's total
    against the sum of what the shards reported running. A count is only a proxy
    for the question being asked, and it answers a different one as soon as a
    module declines to run: a module-level ``SkipTest`` reports ``Ran 0 tests``,
    which the count read as a module nobody had registered. Two modules in this
    suite skip that way by design, both because the thing they judge is an
    installed program rather than this source tree, so the proxy failed the
    suite for a legitimately unavailable dependency.

    Module names answer the actual question directly.
    """
    loader = unittest.defaultTestLoader
    suite = loader.discover(str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    if loader.errors:
        raise SystemExit("test discovery failed:\n" + "\n".join(loader.errors))
    modules: set[str] = set()
    pending: list[Any] = [suite]
    while pending:
        case = pending.pop()
        if isinstance(case, unittest.TestSuite):
            pending.extend(case)
            continue
        modules.add(type(case).__module__)
    return modules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--fast",
        action="store_true",
        help="run only the unit/schema/parser modules",
    )
    group.add_argument(
        "--portable",
        action="store_true",
        help="run only the modules that need no external process",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="run every test module plus helper-script suites (default)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        # Half the cores, not all of them: a shard is not a single process. The
        # loop tests each spawn `agent_session_loop run`, which spawns a fake
        # harness of its own, so one shard per core oversubscribes the machine
        # several times over. Under that load the loop tests race their own
        # subprocess timeouts and process-group enumeration, which reads as
        # flaky tests rather than as the overload it is.
        default=max(1, (os.cpu_count() or 4) // 2),
        help="maximum parallel shard processes (default: half the CPU count)",
    )
    args = parser.parse_args(argv)
    mode = "portable" if args.portable else "fast" if args.fast else "all"
    fast_only = mode != "all"

    shards = _shard_commands(mode)
    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(_run_shard, name, command): name for name, command in shards
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            verdict = "ok" if result["returncode"] == 0 else "FAIL"
            print(
                f"{verdict:>4}  {result['name']:<44} "
                f"{result['duration']:6.1f}s  {result['tests']:4d} tests",
                flush=True,
            )
            results.append(result)

    failures = [result for result in results if result["returncode"] != 0]
    total_tests = sum(result["tests"] for result in results)
    wall = time.monotonic() - started
    for result in failures:
        print(f"\n=== output from failed shard {result['name']} ===")
        print(result["output"].rstrip())

    if not fast_only:
        registered = set(FAST_MODULES) | set(INTEGRATION_MODULES)
        discovered = _discovered_package_modules()
        unregistered = sorted(discovered - registered)
        if unregistered:
            print(
                "\nFAIL: discovery finds tests in "
                f"{', '.join(unregistered)}; a test module is missing from "
                "run_suite lists"
            )
            return 1
        undiscovered = sorted(registered - discovered)
        if undiscovered:
            print(
                f"\nFAIL: run_suite lists {', '.join(undiscovered)} but broad "
                "discovery finds no test there; the list names a module that "
                "moved, was renamed, or no longer holds tests"
            )
            return 1
        # A module that ran nothing is not a failure -- the two installed-pair
        # gates skip when the program they judge is not installed -- but it is
        # not a success either, and a suite that printed only `ok` for it would
        # report coverage it did not have.
        for result in results:
            if result["name"] in registered and result["tests"] == 0:
                print(f"\nnote: {result['name']} ran no tests (skipped)")

    label = {"portable": "portable suite", "fast": "fast suite"}.get(
        mode, "complete suite"
    )
    verdict = "FAILED" if failures else "passed"
    print(
        f"\n{label} {verdict}: {total_tests} tests across {len(shards)} shards "
        f"in {wall:.1f}s ({len(failures)} failed shard(s))"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
