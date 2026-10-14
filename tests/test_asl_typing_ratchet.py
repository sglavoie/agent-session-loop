"""The typing ratchet: repository-wide suppressions may only shrink.

The repository still hides nine mypy error codes globally, because clearing
them needs container and generic annotations across a 125-file untyped tree
rather than defect fixes. That baseline is a debt, and debt without a ratchet
grows: a new module lands untyped, a tenth code joins the list to keep the
gate green, and the extraction program's typed boundaries stop being a
boundary at all.

These tests are that ratchet. ``pyproject.toml`` carries *two*
``enable_error_code`` override tables, under two contracts:

* **Boundary** -- mypy codes *and* the linter below: annotated public
  signatures, parameterized containers, no ``type: ignore``, no import of the
  runner. That is extraction discipline, which is meaningful for a package
  boundary and meaningless for a test module or for a package module whose job
  is to drive the runner.
* **Checked** -- the same three mypy codes and nothing else. Where a module
  lands when it is clean under mypy but cannot meet the extraction rules.

Neither table can name itself: mypy rejects an extra key in an overrides table
as an unrecognized option, so each is looked up by an anchor module only that
table may list (see ``TABLE_ANCHORS``).

Together the tests assert five things a future session cannot quietly undo:

* the repository-wide ``disable_error_code`` list may only *shrink*, and no
  per-module override -- in either table -- may add a suppression of its own;
* both tables re-enable all three codes, list only modules that exist, and stay
  sorted and disjoint;
* each table's module list may only *grow*, measured against its own floor set
  (``REQUIRED_RATCHETED_MODULES``, ``REQUIRED_CHECKED_MODULES``);
* every module in the package appears on exactly one of the two tables, so a
  module added tomorrow cannot sit under the global suppressions unnoticed --
  the floors only stop listed modules from leaving;
* every module on the **Boundary** table keeps its durable-boundary payloads
  typed under the linter rules above.

That last check is a linter, not a mypy run: it fails on the *shape* mypy
would have to trust, so a public entry point that takes a bare ``dict`` is
caught even though ``dict`` is a perfectly valid annotation. The bare-container
rule reads the whole annotation rather than its root, because ``dict | None``
and ``list[dict]`` restore the untyped payload just as surely. It applies to
the Boundary table only.
"""

from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
#: The package trees every module of which must be on one of the two override
#: tables. ``asl_workbench`` joins the walk because it is a package this
#: repository maintains and ships, so a portable module added tomorrow has to
#: pick a table rather than inherit the untyped baseline.
PACKAGE_ROOTS = (
    REPO_ROOT / "agent_session_loop",
    REPO_ROOT / "asl_workbench",
)

#: The repository-wide suppressions as of the session that added this ratchet.
#: This set is a ceiling: a code may leave it once the tree is clean enough to
#: drop the suppression, but nothing may join it. A new module that would need
#: a tenth code is a module that needs annotations instead.
BASELINE_DISABLED_CODES = frozenset(
    {
        "arg-type",
        "union-attr",
        "index",
        "assignment",
        "var-annotated",
        "misc",
        "call-overload",
        "operator",
        "override",
        "annotation-unchecked",
    }
)

#: The codes a ratcheted module opts back in to. All three are ways a wrong
#: value reaches a durable record instead of raising at the call site.
RATCHETED_CODES = frozenset({"arg-type", "union-attr", "index"})

#: The two override tables. An overrides table has no name key -- mypy rejects
#: one as an unrecognized option -- so each is named by an anchor module that
#: only that table may list. The anchors are load-bearing: swap a table's
#: contract and the lookup below stops finding it.
BOUNDARY_TABLE = "boundary"
CHECKED_TABLE = "checked"
TABLE_ANCHORS = {
    BOUNDARY_TABLE: "agent_session_loop.artifacts",
    CHECKED_TABLE: "agent_session_loop.compact_state",
}

#: The package modules that meet the boundary contract: clean under all three
#: codes and clean under the linter below. This set is a floor: every module
#: that becomes clean adds to it, and nothing may leave it. A module removed
#: from ``pyproject.toml`` fails here rather than silently rejoining the
#: untyped baseline.
REQUIRED_RATCHETED_MODULES = frozenset(
    {
        "agent_session_loop",
        "agent_session_loop.__main__",
        "agent_session_loop.agent_mux_contract",
        "agent_session_loop.agent_mux_delivery",
        "agent_session_loop.agent_mux_pairing",
        "agent_session_loop.agent_mux_projection",
        "agent_session_loop.artifacts",
        "agent_session_loop.assurance_catchup",
        "agent_session_loop.assurance_cli",
        "agent_session_loop.assurance_draft",
        "agent_session_loop.assurance_ledger",
        "agent_session_loop.assurance_models_artifacts",
        "agent_session_loop.assurance_models_audit",
        "agent_session_loop.assurance_models_findings",
        "agent_session_loop.assurance_models_inbox",
        "agent_session_loop.assurance_models_primitives",
        "agent_session_loop.assurance_models_reconcile",
        "agent_session_loop.assurance_models_summary",
        "agent_session_loop.assurance_models_vocab",
        "agent_session_loop.assurance_projection",
        "agent_session_loop.assurance_reconcile",
        "agent_session_loop.assurance_report",
        "agent_session_loop.audit_replay",
        "agent_session_loop.authoring_lint",
        "agent_session_loop.authoring_manifest",
        "agent_session_loop.authoring_parse",
        "agent_session_loop.authoring_paths",
        "agent_session_loop.authoring_render",
        "agent_session_loop.authoring_source_models",
        "agent_session_loop.authoring_split",
        "agent_session_loop.authoring_split_cli",
        "agent_session_loop.authoring_transaction",
        "agent_session_loop.baseline",
        "agent_session_loop.baseline_cache",
        "agent_session_loop.bridge_errors",
        "agent_session_loop.bridge_handlers",
        "agent_session_loop.bridge_ledger",
        "agent_session_loop.bridge_projection",
        "agent_session_loop.bridge_protocol",
        "agent_session_loop.bridge_redirect",
        "agent_session_loop.bridge_server",
        "agent_session_loop.bridge_session",
        "agent_session_loop.bridge_session_state",
        "agent_session_loop.bridge_workers",
        "agent_session_loop.claude_guidance_hook",
        "agent_session_loop.claude_stream",
        "agent_session_loop.cli",
        "agent_session_loop.cli_common",
        "agent_session_loop.cli_defaults",
        "agent_session_loop.cli_parser_assurance",
        "agent_session_loop.cli_parser_control",
        "agent_session_loop.cli_parser_integrations",
        "agent_session_loop.cli_parser_mailbox",
        "agent_session_loop.cli_parser_maintenance",
        "agent_session_loop.cli_parser_overseer",
        "agent_session_loop.cli_parser_plan_authoring",
        "agent_session_loop.cli_parser_recovery",
        "agent_session_loop.cli_parser_run",
        "agent_session_loop.cli_parser_support",
        "agent_session_loop.cli_parser_workbench",
        "agent_session_loop.cli_trace",
        "agent_session_loop.clock",
        "agent_session_loop.codex_app_server",
        "agent_session_loop.command_guard",
        "agent_session_loop.commit_groups",
        "agent_session_loop.commit_message",
        "agent_session_loop.commit_settlement",
        "agent_session_loop.config",
        "agent_session_loop.contracts",
        "agent_session_loop.control_cli",
        "agent_session_loop.control_queue",
        "agent_session_loop.control_signal",
        "agent_session_loop.diagnostics_export",
        "agent_session_loop.durability",
        "agent_session_loop.events",
        "agent_session_loop.execution_timing",
        "agent_session_loop.external_write_evidence",
        "agent_session_loop.formatting",
        "agent_session_loop.git_subprocess",
        "agent_session_loop.guard_pretool_hook",
        "agent_session_loop.guards",
        "agent_session_loop.guidance_delivery",
        "agent_session_loop.handoff_correction",
        "agent_session_loop.handoff_guards",
        "agent_session_loop.harness_claude",
        "agent_session_loop.harness_codex",
        "agent_session_loop.harness_compatibility",
        "agent_session_loop.harness_copilot",
        "agent_session_loop.harness_failures",
        "agent_session_loop.harnesses",
        "agent_session_loop.install_verify",
        "agent_session_loop.intervention",
        "agent_session_loop.literals",
        "agent_session_loop.live_verification",
        "agent_session_loop.mailbox",
        "agent_session_loop.mailbox_append_record",
        "agent_session_loop.mailbox_appenders",
        "agent_session_loop.mailbox_cli_agent_mux",
        "agent_session_loop.mailbox_cli_args",
        "agent_session_loop.mailbox_cli_ask",
        "agent_session_loop.mailbox_cli_constants",
        "agent_session_loop.mailbox_cli_guidance",
        "agent_session_loop.mailbox_cli_inbox",
        "agent_session_loop.mailbox_cli_models",
        "agent_session_loop.mailbox_cli_notify",
        "agent_session_loop.mailbox_cli_outbox",
        "agent_session_loop.mailbox_cli_output",
        "agent_session_loop.mailbox_cli_projections",
        "agent_session_loop.mailbox_cli_receipts",
        "agent_session_loop.mailbox_cli_replies",
        "agent_session_loop.mailbox_cli_status",
        "agent_session_loop.mailbox_cli_waits",
        "agent_session_loop.mailbox_constants",
        "agent_session_loop.mailbox_cursors",
        "agent_session_loop.mailbox_leases",
        "agent_session_loop.mailbox_models",
        "agent_session_loop.mailbox_parsing",
        "agent_session_loop.mailbox_rendering",
        "agent_session_loop.mailbox_snapshot",
        "agent_session_loop.mailbox_validation",
        "agent_session_loop.mailbox_vocabulary",
        "agent_session_loop.mailbox_waits",
        "agent_session_loop.maintenance_bodies",
        "agent_session_loop.maintenance_cli",
        "agent_session_loop.maintenance_job",
        "agent_session_loop.maintenance_models",
        "agent_session_loop.maintenance_mutator",
        "agent_session_loop.maintenance_plan",
        "agent_session_loop.maintenance_policy",
        "agent_session_loop.maintenance_run",
        "agent_session_loop.metrics",
        "agent_session_loop.model_availability",
        "agent_session_loop.overseer_checkpoint",
        "agent_session_loop.overseer_checkpoint_groups",
        "agent_session_loop.overseer_cli",
        "agent_session_loop.overseer_context",
        "agent_session_loop.overseer_decisions",
        "agent_session_loop.overseer_discovery",
        "agent_session_loop.overseer_dispatch",
        "agent_session_loop.overseer_history",
        "agent_session_loop.overseer_isolation",
        "agent_session_loop.overseer_mailbox",
        "agent_session_loop.overseer_policy_client",
        "agent_session_loop.overseer_resume",
        "agent_session_loop.overseer_store_actions",
        "agent_session_loop.overseer_store_core",
        "agent_session_loop.overseer_store_groups",
        "agent_session_loop.overseer_store_incidents",
        "agent_session_loop.overseer_store_ledger",
        "agent_session_loop.overseer_store_models",
        "agent_session_loop.overseer_store_rows",
        "agent_session_loop.overseer_store_schema",
        "agent_session_loop.overseer_store_watch",
        "agent_session_loop.overseer_verify",
        "agent_session_loop.overseer_watch",
        "agent_session_loop.pause_policy",
        "agent_session_loop.plans_content_hash",
        "agent_session_loop.plans_discovery",
        "agent_session_loop.plans_errors",
        "agent_session_loop.plans_files",
        "agent_session_loop.plans_handoff",
        "agent_session_loop.plans_metadata",
        "agent_session_loop.plans_models",
        "agent_session_loop.plans_payloads",
        "agent_session_loop.plans_progress",
        "agent_session_loop.plans_readiness",
        "agent_session_loop.plans_registry",
        "agent_session_loop.plans_session_ids",
        "agent_session_loop.plans_session_scope",
        "agent_session_loop.plans_snapshot",
        "agent_session_loop.plans_status_tables",
        "agent_session_loop.plans_transaction",
        "agent_session_loop.preflight",
        "agent_session_loop.preflight_auto_repair",
        "agent_session_loop.preflight_baseline_checks",
        "agent_session_loop.preflight_cli",
        "agent_session_loop.preflight_context",
        "agent_session_loop.preflight_environment",
        "agent_session_loop.preflight_footprint",
        "agent_session_loop.preflight_hint_resolution",
        "agent_session_loop.preflight_models",
        "agent_session_loop.preflight_output",
        "agent_session_loop.preflight_repair_hints",
        "agent_session_loop.preflight_runtime_hints",
        "agent_session_loop.preflight_session_checks",
        "agent_session_loop.preflight_verification_checks",
        "agent_session_loop.prepare",
        "agent_session_loop.prepare_baseline",
        "agent_session_loop.prepare_cli",
        "agent_session_loop.prepare_commands",
        "agent_session_loop.prepare_corrections",
        "agent_session_loop.prepare_expand_ingest",
        "agent_session_loop.prepare_expansion",
        "agent_session_loop.prepare_models",
        "agent_session_loop.prepare_packet",
        "agent_session_loop.prepare_parsing",
        "agent_session_loop.prepare_rebind",
        "agent_session_loop.prepare_results",
        "agent_session_loop.prepare_structure",
        "agent_session_loop.prepare_validation_cache",
        "agent_session_loop.prepare_validation_stage",
        "agent_session_loop.prepare_validation_store",
        "agent_session_loop.prepare_validation_support",
        "agent_session_loop.prepare_validator",
        "agent_session_loop.process_tree",
        "agent_session_loop.progress_cli",
        "agent_session_loop.progress_repair",
        "agent_session_loop.qna",
        "agent_session_loop.readiness_gate",
        "agent_session_loop.repository_evidence",
        "agent_session_loop.repository_intent",
        "agent_session_loop.repository_registration",
        "agent_session_loop.repository_scope",
        "agent_session_loop.responses",
        "agent_session_loop.retention",
        "agent_session_loop.retry_hints",
        "agent_session_loop.review",
        "agent_session_loop.review_continuation",
        "agent_session_loop.review_cycle",
        "agent_session_loop.review_delta",
        "agent_session_loop.review_guard",
        "agent_session_loop.review_repair",
        "agent_session_loop.review_session_body",
        "agent_session_loop.review_session_contract",
        "agent_session_loop.review_transitions",
        "agent_session_loop.run_commit",
        "agent_session_loop.run_completion",
        "agent_session_loop.run_contention",
        "agent_session_loop.run_control",
        "agent_session_loop.run_coordination_constants",
        "agent_session_loop.run_coordination_control",
        "agent_session_loop.run_coordination_control_drain",
        "agent_session_loop.run_coordination_mailbox_poll",
        "agent_session_loop.run_coordination_mailbox_question",
        "agent_session_loop.run_coordination_mailbox_wait",
        "agent_session_loop.run_coordination_reply_state",
        "agent_session_loop.run_coordination_seams",
        "agent_session_loop.run_coordination_terminal",
        "agent_session_loop.run_coordination_wait_models",
        "agent_session_loop.run_coordination_wait_record",
        "agent_session_loop.run_coordination_wait_state",
        "agent_session_loop.run_doctor",
        "agent_session_loop.run_entry",
        "agent_session_loop.run_errors",
        "agent_session_loop.run_flags",
        "agent_session_loop.run_inputs",
        "agent_session_loop.run_loop",
        "agent_session_loop.run_loop_carried_corrections",
        "agent_session_loop.run_loop_commit_settlement",
        "agent_session_loop.run_loop_context",
        "agent_session_loop.run_loop_control_redirects",
        "agent_session_loop.run_loop_finalization",
        "agent_session_loop.run_loop_frame",
        "agent_session_loop.run_loop_setup_gates",
        "agent_session_loop.run_loop_setup_state",
        "agent_session_loop.run_loop_terminal",
        "agent_session_loop.run_loop_turn_execution",
        "agent_session_loop.run_loop_turn_outcome",
        "agent_session_loop.run_loop_turn_preparation",
        "agent_session_loop.run_loop_turn_selection",
        "agent_session_loop.run_outcome",
        "agent_session_loop.run_payloads",
        "agent_session_loop.run_persistence",
        "agent_session_loop.run_prompts",
        "agent_session_loop.run_reporting",
        "agent_session_loop.run_selection",
        "agent_session_loop.run_termination",
        "agent_session_loop.runner",
        "agent_session_loop.session_overview",
        "agent_session_loop.session_verification",
        "agent_session_loop.spark_status",
        "agent_session_loop.startup_context",
        "agent_session_loop.state",
        "agent_session_loop.state_abandonment",
        "agent_session_loop.state_assurance_pending",
        "agent_session_loop.state_cli_handlers",
        "agent_session_loop.state_locks",
        "agent_session_loop.state_mailbox_status",
        "agent_session_loop.state_migration",
        "agent_session_loop.state_migration_notes",
        "agent_session_loop.state_payloads",
        "agent_session_loop.state_plan_refs",
        "agent_session_loop.state_schema_upgrade",
        "agent_session_loop.state_status",
        "agent_session_loop.state_status_text",
        "agent_session_loop.supervision_liveness",
        "agent_session_loop.supervisor_audit",
        "agent_session_loop.supervisor_audit_agent",
        "agent_session_loop.supervisor_audit_checks",
        "agent_session_loop.supervisor_audit_checks_anomalies",
        "agent_session_loop.supervisor_audit_checks_commits",
        "agent_session_loop.supervisor_audit_checks_failures",
        "agent_session_loop.supervisor_audit_checks_intent",
        "agent_session_loop.supervisor_audit_checks_paused_work",
        "agent_session_loop.supervisor_audit_checks_success",
        "agent_session_loop.supervisor_audit_checks_terminal",
        "agent_session_loop.supervisor_audit_checks_verification",
        "agent_session_loop.supervisor_audit_commit_records",
        "agent_session_loop.supervisor_audit_evidence_collector",
        "agent_session_loop.supervisor_audit_evidence_commits",
        "agent_session_loop.supervisor_audit_evidence_constants",
        "agent_session_loop.supervisor_audit_evidence_failures",
        "agent_session_loop.supervisor_audit_evidence_helpers",
        "agent_session_loop.supervisor_audit_evidence_input",
        "agent_session_loop.supervisor_audit_evidence_models",
        "agent_session_loop.supervisor_audit_evidence_redaction",
        "agent_session_loop.supervisor_audit_evidence_sections",
        "agent_session_loop.supervisor_audit_evidence_shedding",
        "agent_session_loop.supervisor_audit_evidence_status",
        "agent_session_loop.supervisor_audit_evidence_streams",
        "agent_session_loop.supervisor_audit_evidence_turns",
        "agent_session_loop.supervisor_audit_models",
        "agent_session_loop.supervisor_audit_vocabulary",
        "agent_session_loop.supervisor_audit_write_scope",
        "agent_session_loop.supervisor_guide",
        "agent_session_loop.tail",
        "agent_session_loop.task_packet",
        "agent_session_loop.transitions",
        "agent_session_loop.turn_dispositions",
        "agent_session_loop.turn_execution_argv",
        "agent_session_loop.turn_execution_artifacts",
        "agent_session_loop.turn_execution_commit",
        "agent_session_loop.turn_execution_constants",
        "agent_session_loop.turn_execution_executor",
        "agent_session_loop.turn_execution_hang_sentinel",
        "agent_session_loop.turn_execution_hang_settings",
        "agent_session_loop.turn_execution_live",
        "agent_session_loop.turn_execution_protocols",
        "agent_session_loop.turn_execution_signals",
        "agent_session_loop.turn_postflight",
        "agent_session_loop.turn_preflight",
        "agent_session_loop.turn_preparation",
        "agent_session_loop.turn_settlement",
        "agent_session_loop.verification_checks",
        "agent_session_loop.verification_cli",
        "agent_session_loop.verification_command_diagnosis",
        "agent_session_loop.verification_contract_check",
        "agent_session_loop.verification_diagnostics",
        "agent_session_loop.verification_models",
        "agent_session_loop.verification_process",
        "agent_session_loop.verification_run",
        "agent_session_loop.verification_transitions",
        "agent_session_loop.workbench_actions",
        "agent_session_loop.workbench_archive",
        "agent_session_loop.workbench_catalog",
        "agent_session_loop.workbench_connector",
        "agent_session_loop.workbench_controls",
        "agent_session_loop.workbench_details",
        "agent_session_loop.workbench_history",
        "agent_session_loop.workbench_local",
        "agent_session_loop.workbench_operations",
        "agent_session_loop.workbench_remote_actions",
        "agent_session_loop.workbench_remote_reads",
        "agent_session_loop.workbench_server",
        "agent_session_loop.workbench_workspace",
        "agent_session_loop.workspace_evidence",
        "agent_session_loop.workspaces",
        "agent_session_loop.write_guards",
        # The portable Workbench package. It cannot leave this table: the rule
        # that keeps it loadable on the Pi is stricter than the one this table
        # enforces, so a module that could not meet the boundary contract could
        # not be in the package at all.
        "asl_workbench",
        "asl_workbench.__main__",
        "asl_workbench.actions",
        "asl_workbench.archive",
        "asl_workbench.capabilities",
        "asl_workbench.catalog",
        "asl_workbench.changes",
        "asl_workbench.compatibility",
        "asl_workbench.connector_lanes",
        "asl_workbench.connector_mutations",
        "asl_workbench.connector_payloads",
        "asl_workbench.connector_protocols",
        "asl_workbench.connector_reads",
        "asl_workbench.connector_runtime",
        "asl_workbench.connector_state",
        "asl_workbench.connector_support",
        "asl_workbench.controls",
        "asl_workbench.credentials",
        "asl_workbench.details",
        "asl_workbench.freshness",
        "asl_workbench.history",
        "asl_workbench.http",
        "asl_workbench.incidents",
        "asl_workbench.installation",
        "asl_workbench.integrations",
        "asl_workbench.notifications",
        "asl_workbench.observation",
        "asl_workbench.operations",
        "asl_workbench.overview",
        "asl_workbench.remote_actions",
        "asl_workbench.remote_reads",
        "asl_workbench.service",
        "asl_workbench.store_base",
        "asl_workbench.store_columns",
        "asl_workbench.store_ingest_catalog",
        "asl_workbench.store_ingest_runs",
        "asl_workbench.store_journal",
        "asl_workbench.store_migrations",
        "asl_workbench.store_reads_catalog",
        "asl_workbench.store_reads_runs",
        "asl_workbench.store_retention",
        "asl_workbench.store_types",
        "asl_workbench.store_values",
        "asl_workbench.store_workbench",
        "asl_workbench.transport",
        "asl_workbench.transport_checks",
        "asl_workbench.transport_compat",
        "asl_workbench.transport_envelopes",
        "asl_workbench.transport_payloads",
        "asl_workbench.transport_shapes",
        "asl_workbench.transport_vocabulary",
    }
)

#: The floor for the Checked table, same rule as the set above: clean under all
#: three codes, but held off the Boundary table by the linter rather than by
#: mypy. Seeded with the two package modules that import the runner, which is a
#: rule neither of them could ever satisfy.
REQUIRED_CHECKED_MODULES = frozenset(
    {
        "agent_session_loop.compact_state",
        "agent_session_loop.workbench_cli",
        "agent_session_loop.workbench_discovery",
        "agent_session_loop.workbench_launch",
        "agent_session_loop.workbench_recovery_actions",
        "agent_session_loop.workbench_recovery_build",
        "agent_session_loop.workbench_recovery_builders_run",
        "agent_session_loop.workbench_recovery_builders_state",
        "agent_session_loop.workbench_recovery_conversation",
        "agent_session_loop.workbench_recovery_evidence_checks",
        "agent_session_loop.workbench_recovery_evidence_commit",
        "agent_session_loop.workbench_recovery_models",
        "agent_session_loop.workbench_recovery_payload",
        "agent_session_loop.workbench_recovery_review",
        "agent_session_loop.workbench_recovery_state",
        "agent_session_loop.workbench_registry",
        "agent_session_loop.workbench_registry_coerce",
        "agent_session_loop.workbench_registry_models",
        "agent_session_loop.workbench_summary",
        "agent_session_loop.workbench_summary_helpers",
        "agent_session_loop.workbench_summary_model",
    }
)

#: Each table's floor, keyed by table name.
REQUIRED_MODULES = {
    BOUNDARY_TABLE: REQUIRED_RATCHETED_MODULES,
    CHECKED_TABLE: REQUIRED_CHECKED_MODULES,
}

#: Container annotations that say nothing about what they hold. A durable
#: payload crossing a boundary as one of these is exactly the case the
#: repository-wide ``arg-type`` suppression was hiding. ``Any`` is excluded
#: deliberately: it is an explicit statement that the value is unconstrained,
#: which a JSON payload slot legitimately is.
UNPARAMETERIZED_CONTAINERS = frozenset(
    {
        "dict",
        "list",
        "tuple",
        "set",
        "frozenset",
        "Mapping",
        "MutableMapping",
        "Sequence",
        "Iterable",
        "Iterator",
        "Callable",
    }
)


def load_mypy_config() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["mypy"]


def enabling_overrides() -> list[dict[str, Any]]:
    """Every override table that re-enables codes, in file order."""
    overrides = load_mypy_config()["overrides"]
    return [override for override in overrides if "enable_error_code" in override]


def ratcheted_override(table: str) -> dict[str, Any]:
    """The named override table that re-enables codes under ``table``'s contract.

    The tables are unnamed in TOML, so each is resolved by the anchor module
    only it may list. Resolving by name rather than by position keeps the
    guarantee the old "exactly one table" assertion gave: a floor set and the
    linter each address one unambiguous table.
    """
    anchor = TABLE_ANCHORS[table]
    matching = [
        override
        for override in enabling_overrides()
        if anchor in override.get("module", [])
    ]
    if len(matching) != 1:
        raise AssertionError(
            f"expected exactly one enable_error_code override table listing "
            f"{anchor} (the {table} table), found {len(matching)}"
        )
    return matching[0]


def module_path(dotted: str) -> Path:
    """Resolve a dotted name to its file.

    A package name such as ``agent_session_loop`` is the name mypy matches for
    that package's ``__init__.py``, so it resolves to the ``__init__`` rather
    than to a sibling module file that never existed.
    """
    base = REPO_ROOT / Path(*dotted.split("."))
    if base.is_dir():
        return base / "__init__.py"
    return base.with_suffix(".py")


def dotted_name(path: Path) -> str:
    """The name mypy matches for a module file, relative to the repository root.

    The inverse of :func:`module_path`. An ``__init__.py`` is named by its
    package -- mypy has no ``agent_session_loop.__init__`` to match -- so the
    file name is dropped rather than turned into a component.
    """
    relative = path.relative_to(REPO_ROOT)
    if path.name == "__init__.py":
        return ".".join(relative.parent.parts)
    return ".".join((*relative.parent.parts, relative.stem))


def package_modules() -> frozenset[str]:
    """Every module in the maintained packages, by the name mypy would match.

    Walked recursively so a subpackage added later is discovered rather than
    silently exempted, and filtered on ``__pycache__`` because a stale ``.pyc``
    directory holds no source.
    """
    return frozenset(
        dotted_name(path)
        for root in PACKAGE_ROOTS
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def subscript_base(node: ast.Subscript) -> str | None:
    """Name the generic being parameterized, or ``None`` when it is an expression."""
    if isinstance(node.value, ast.Name):
        return node.value.id
    if isinstance(node.value, ast.Attribute):
        return node.value.attr
    return None


def unparameterized_container(annotation: ast.expr) -> str | None:
    """Name the first bare container in this annotation, or ``None`` when typed.

    The search recurses through unions and generic arguments, because
    ``dict | None`` and ``list[dict]`` restore the ``Any``-valued payload the
    root-only check existed to reject just as surely as a bare ``dict`` does. A
    fully parameterized nesting such as ``list[dict[str, object]]`` is accepted:
    every container in it names what it holds.

    ``Literal[...]`` arguments are skipped and string annotations are never
    parsed -- both hold values rather than types, so ``Literal["dict"]`` is not
    a bare container.
    """
    if isinstance(annotation, ast.Name) and annotation.id in UNPARAMETERIZED_CONTAINERS:
        return annotation.id
    if (
        isinstance(annotation, ast.Attribute)
        and annotation.attr in UNPARAMETERIZED_CONTAINERS
    ):
        return annotation.attr
    if isinstance(annotation, ast.Subscript):
        if subscript_base(annotation) == "Literal":
            return None
        return unparameterized_container(annotation.slice)
    if isinstance(annotation, ast.Tuple | ast.List):
        for element in annotation.elts:
            bare = unparameterized_container(element)
            if bare is not None:
                return bare
        return None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return unparameterized_container(annotation.left) or unparameterized_container(
            annotation.right
        )
    return None


def public_definitions(
    tree: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every module-level and class-level definition that is part of the API.

    Nested helpers are excluded: a closure inside a private function is not a
    boundary anything durable crosses, and typing it is a style question rather
    than a correctness one.
    """
    definitions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def collect(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                if node.name.startswith("_"):
                    continue
                collect(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("_") and node.name != "__init__":
                    continue
                definitions.append((f"{prefix}{node.name}", node))

    collect(tree.body, "")
    return definitions


def annotated_arguments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = node.args
    every = [
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
        args.vararg,
        args.kwarg,
    ]
    return [
        argument
        for argument in every
        if argument is not None and argument.arg not in ("self", "cls")
    ]


class GlobalSuppressionRatchetTests(unittest.TestCase):
    """The repository-wide baseline may only shrink."""

    def test_no_code_joins_the_repository_wide_baseline(self) -> None:
        disabled = set(load_mypy_config()["disable_error_code"])

        added = disabled - BASELINE_DISABLED_CODES
        self.assertEqual(
            added,
            set(),
            "new repository-wide mypy suppressions are not allowed; annotate the "
            f"code instead of disabling {sorted(added)}",
        )

    def test_the_baseline_pin_is_kept_honest(self) -> None:
        """A code dropped from ``pyproject.toml`` must be dropped here too.

        Without this the ceiling would drift upward for free: a session could
        clear a code, leave the pin behind, and a later session could re-add it
        without tripping the ratchet above.
        """
        disabled = set(load_mypy_config()["disable_error_code"])

        self.assertEqual(disabled, set(BASELINE_DISABLED_CODES))

    def test_no_override_table_adds_a_suppression(self) -> None:
        """Per-module ``disable_error_code`` is unioned with the global list.

        That makes it a repository-wide suppression wearing a narrower name, so
        the ratchet refuses it outright.
        """
        for override in load_mypy_config()["overrides"]:
            with self.subTest(module=override.get("module")):
                self.assertNotIn("disable_error_code", override)

    def test_the_typed_boundaries_are_still_checked(self) -> None:
        config = load_mypy_config()

        self.assertEqual(
            config["files"],
            [
                "agent_session_loop",
                "asl_workbench",
                "tests",
                "skills/asl/scripts",
                "skills/session-resplit/scripts",
            ],
        )


class RatchetedModuleListTests(unittest.TestCase):
    """Both ratcheted module lists may only grow."""

    def test_both_named_tables_resolve_and_nothing_else_enables_codes(self) -> None:
        """Named lookup only stays unambiguous while there are no other tables.

        A third ``enable_error_code`` table would be a contract nobody wrote
        down: the linter would skip it and no floor set would guard it.
        """
        resolved = [ratcheted_override(table) for table in REQUIRED_MODULES]

        self.assertEqual(len(enabling_overrides()), len(resolved))

    def test_the_two_tables_are_disjoint(self) -> None:
        """A module on both would be linted and not linted at once."""
        boundary = set(ratcheted_override(BOUNDARY_TABLE)["module"])
        checked = set(ratcheted_override(CHECKED_TABLE)["module"])

        self.assertEqual(boundary & checked, set())

    def test_every_ratcheted_module_is_still_on_its_list(self) -> None:
        for table, required in REQUIRED_MODULES.items():
            listed = set(ratcheted_override(table)["module"])
            missing = required - listed
            with self.subTest(table=table):
                self.assertEqual(
                    missing,
                    set(),
                    f"a module left the {table} enable_error_code list; "
                    f"restore {sorted(missing)}",
                )

    def test_every_package_module_is_ratcheted(self) -> None:
        """A new package module must join a table rather than inherit the debt.

        The floor sets above only stop a *listed* module from leaving. Nothing
        made a module that never appeared on either table an error, so a module
        added tomorrow would stay under the globally disabled ``arg-type``,
        ``union-attr`` and ``index`` codes for as long as nobody noticed. This
        derives the tree instead of trusting a list, and requires exactly one
        table per module: neither is a home a module can be missing from, and
        two would mean it is linted and not linted at once.
        """
        tables_by_module: dict[str, list[str]] = {
            dotted: [] for dotted in package_modules()
        }
        for table in REQUIRED_MODULES:
            for dotted in ratcheted_override(table)["module"]:
                if dotted in tables_by_module:
                    tables_by_module[dotted].append(table)

        for dotted, tables in sorted(tables_by_module.items()):
            with self.subTest(module=dotted):
                self.assertEqual(
                    len(tables),
                    1,
                    f"{dotted} is on {len(tables)} enable_error_code tables; add "
                    f"it to {BOUNDARY_TABLE} if it meets the extraction rules, "
                    f"otherwise to {CHECKED_TABLE}",
                )

    def test_every_listed_module_re_enables_all_three_codes(self) -> None:
        for table in REQUIRED_MODULES:
            override = ratcheted_override(table)
            with self.subTest(table=table):
                self.assertEqual(
                    set(override["enable_error_code"]), set(RATCHETED_CODES)
                )

    def test_every_listed_module_exists(self) -> None:
        for table in REQUIRED_MODULES:
            for dotted in ratcheted_override(table)["module"]:
                with self.subTest(table=table, module=dotted):
                    self.assertTrue(module_path(dotted).is_file())

    def test_the_lists_stay_sorted_so_additions_do_not_collide(self) -> None:
        for table in REQUIRED_MODULES:
            listed = list(ratcheted_override(table)["module"])
            with self.subTest(table=table):
                self.assertEqual(listed, sorted(listed))

    def test_the_required_sets_name_only_real_modules(self) -> None:
        for table, required in REQUIRED_MODULES.items():
            for dotted in sorted(required):
                with self.subTest(table=table, module=dotted):
                    self.assertTrue(module_path(dotted).is_file())

    def test_each_anchor_is_on_the_floor_of_the_table_it_names(self) -> None:
        """An anchor that could be moved off its table would rename it."""
        for table, anchor in TABLE_ANCHORS.items():
            with self.subTest(table=table):
                self.assertIn(anchor, REQUIRED_MODULES[table])


class UnparameterizedContainerTests(unittest.TestCase):
    """The bare-container rule reads the whole annotation, not just its root.

    Before this, only the root node was inspected, so ``dict | None`` and
    ``list[dict]`` passed the linter while restoring exactly the ``Any``-valued
    payload it exists to reject.
    """

    def bare(self, source: str) -> str | None:
        return unparameterized_container(ast.parse(source, mode="eval").body)

    def test_a_root_container_is_still_named(self) -> None:
        self.assertEqual(self.bare("dict"), "dict")
        self.assertEqual(self.bare("collections.abc.Mapping"), "Mapping")

    def test_a_union_member_is_reached(self) -> None:
        self.assertEqual(self.bare("dict | None"), "dict")
        self.assertEqual(self.bare("str | list"), "list")

    def test_a_generic_argument_is_reached(self) -> None:
        self.assertEqual(self.bare("list[dict]"), "dict")
        self.assertEqual(self.bare("dict[str, list]"), "list")
        self.assertEqual(self.bare("Callable[[int], dict]"), "dict")
        self.assertEqual(self.bare("Callable[..., dict]"), "dict")

    def test_a_fully_parameterized_nesting_is_accepted(self) -> None:
        self.assertIsNone(self.bare("list[dict[str, object]]"))
        self.assertIsNone(self.bare("dict[str, list[str]] | None"))
        self.assertIsNone(
            self.bare("Callable[[Mapping[str, Any]], dict[str, Any]] | None")
        )

    def test_literal_arguments_are_values_rather_than_types(self) -> None:
        """``Literal["dict"]`` names a string, not a container."""
        self.assertIsNone(self.bare('Literal["dict"]'))
        self.assertIsNone(self.bare('Literal["dict", "list"] | None'))

    def test_a_string_annotation_is_left_alone(self) -> None:
        """Forward references are not parsed, so they cannot be misread."""
        self.assertIsNone(self.bare('"dict"'))


class DurableBoundaryTypingTests(unittest.TestCase):
    """Boundary modules keep their durable-boundary payloads typed.

    Scoped to the Boundary table. These four rules are extraction discipline,
    which the Checked table's members are on that table precisely because they
    cannot meet.
    """

    def boundary_modules(self) -> list[str]:
        return list(ratcheted_override(BOUNDARY_TABLE)["module"])

    def modules(self) -> list[tuple[str, ast.Module]]:
        parsed: list[tuple[str, ast.Module]] = []
        for dotted in self.boundary_modules():
            path = module_path(dotted)
            parsed.append((dotted, ast.parse(path.read_text(encoding="utf-8"))))
        return parsed

    def test_public_signatures_are_fully_annotated(self) -> None:
        for dotted, tree in self.modules():
            for name, node in public_definitions(tree):
                with self.subTest(module=dotted, definition=name):
                    if node.name != "__init__":
                        self.assertIsNotNone(
                            node.returns,
                            f"{dotted}.{name} has no return annotation",
                        )
                    for argument in annotated_arguments(node):
                        self.assertIsNotNone(
                            argument.annotation,
                            f"{dotted}.{name} takes an unannotated {argument.arg}",
                        )

    def test_public_signatures_carry_no_bare_containers(self) -> None:
        for dotted, tree in self.modules():
            for name, node in public_definitions(tree):
                with self.subTest(module=dotted, definition=name):
                    annotations = [
                        argument.annotation
                        for argument in annotated_arguments(node)
                        if argument.annotation is not None
                    ]
                    if node.returns is not None:
                        annotations.append(node.returns)
                    for annotation in annotations:
                        bare = unparameterized_container(annotation)
                        self.assertIsNone(
                            bare,
                            f"{dotted}.{name} crosses the boundary with a bare "
                            f"{bare}; name what it holds",
                        )

    def test_declared_attributes_carry_no_bare_containers(self) -> None:
        """Dataclass fields and module constants are durable payload slots too."""
        for dotted, tree in self.modules():
            for node in ast.walk(tree):
                if not isinstance(node, ast.AnnAssign):
                    continue
                bare = unparameterized_container(node.annotation)
                with self.subTest(module=dotted, line=node.lineno):
                    self.assertIsNone(
                        bare,
                        f"{dotted}:{node.lineno} declares a bare {bare}",
                    )

    def test_no_ratcheted_module_suppresses_an_error_inline(self) -> None:
        """``# type: ignore`` would reopen locally what the module opted in to."""
        for dotted in self.boundary_modules():
            source = module_path(dotted).read_text(encoding="utf-8")
            with self.subTest(module=dotted):
                self.assertNotIn("type: ignore", source)
                self.assertNotIn("mypy: disable-error-code", source)

    def test_no_ratcheted_module_imports_the_runner(self) -> None:
        """The extraction only holds while the dependency runs one way."""
        for dotted, tree in self.modules():
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [alias.name for alias in node.names]
                    if node.module is not None:
                        imported.append(node.module)
                with self.subTest(module=dotted, line=getattr(node, "lineno", 0)):
                    self.assertNotIn(
                        "runner", [name.split(".")[-1] for name in imported]
                    )


if __name__ == "__main__":
    unittest.main()
