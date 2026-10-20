"""Shared fixtures for test_asl_acceptance."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agent_session_loop import (
    assurance_models_findings,
    assurance_models_vocab,
    harnesses,
    supervisor_audit_agent,
)
from agent_session_loop.assurance_models_vocab import RUBRIC_LANES

#: Every acceptance criterion of the assurance work, mapped to the regressions
#: that execute it. A reference is ``tests.module:Class.method`` for a package
#: module, or ``path/to/suite.py:Class.method`` for a standalone script suite.
ACCEPTANCE_MATRIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "The Kiln wrong-clone incident is prevented at split, "
        "prepare/preflight, and run, including same-origin sibling clones "
        "and --allow-not-loop-ready.",
        (
            "tests.test_asl_repository_intent:"
            "ResolveIntentTests.test_same_origin_clones_stay_distinct_repositories",
            "tests.test_asl_authoring_split_footprint:"
            "SplitRepositoryFootprintTests."
            "test_multi_repository_plan_without_a_registry_is_refused",
            "tests.test_asl_authoring_split_footprint:"
            "SplitRepositoryFootprintTests."
            "test_write_path_refuses_contracts_that_span_repositories",
            "tests.test_asl_preflight_repository_registry:"
            "RepositoryIntentRegistryRepairTests."
            "test_payload_reports_the_repositories_the_plan_intends_to_reach",
            "tests.test_asl_prepare_legacy_repository_scope:"
            "LegacyRepositoryIntentPrepareTests."
            "test_build_validation_packet_refuses_a_multi_repo_intent",
            "tests.test_asl_run_readiness_hard_gate:"
            "RunReadinessHardGateTests."
            "test_allow_not_loop_ready_does_not_bypass_repository_intent",
            "tests.test_asl_e2e_kiln_twin_clone:"
            "KilnTwinCloneRegressionTests."
            "test_legacy_plan_declaring_the_sibling_clone_launches_no_harness",
            "tests.test_asl_e2e_kiln_twin_clone:"
            "KilnTwinCloneRegressionTests."
            "test_expansion_note_redirect_to_the_sibling_clone_launches_no_harness",
            "tests.test_asl_e2e_kiln_twin_clone:"
            "KilnTwinCloneRegressionTests."
            "test_registered_workspace_targeting_the_testing_clone_succeeds",
        ),
    ),
    (
        "Every eligible persisted loop ends with exactly one truthful assurance state.",
        (
            "tests.test_asl_assurance_lifecycle_terminal_outcomes:"
            "TerminalBranchRoutingTests."
            "test_a_completed_run_is_audited_once_and_keeps_its_exit_code",
            "tests.test_asl_assurance_lifecycle_terminal_outcomes:"
            "TerminalBranchRoutingTests."
            "test_a_paused_run_is_audited_once_and_keeps_its_pause_reason",
            "tests.test_asl_assurance_lifecycle_coordinator:"
            "TerminalizationCoordinatorTests."
            "test_finalizing_is_reachable_only_through_the_coordinator",
            "tests.test_asl_assurance_lifecycle_terminal_audit:"
            "TerminalAuditTests.test_an_all_clear_audit_writes_every_canonical_artifact",
            "tests.test_asl_assurance_lifecycle_terminal_audit:"
            "TerminalAuditTests.test_republishing_the_same_audit_leaves_one_occurrence",
            "tests.test_asl_acceptance_audit:"
            "AdvisoryVerdictTests."
            "test_a_deterministic_failure_ends_the_run_findings_and_opens_one_issue",
        ),
    ),
    (
        "Skipped, unavailable, and pending are never presented as all-clear.",
        (
            "tests.test_asl_assurance_models:AllClearTests.test_all_clear_needs_every_lane",
            "tests.test_asl_assurance_models:"
            "AllClearTests.test_all_clear_needs_every_lane_to_pass",
            "tests.test_asl_assurance_models:AllClearTests.test_all_clear_cannot_carry_findings",
            "tests.test_asl_assurance_models_summary:SummaryInvariantTests.test_all_clear_creates_no_issue",
            "tests.test_asl_assurance_lifecycle_terminal_audit:"
            "TerminalAuditTests.test_an_agent_that_never_answers_still_publishes_the_checks",
            "tests.test_asl_assurance_lifecycle_terminal_audit:"
            "TerminalAuditTests.test_a_failed_publication_leaves_no_report_behind",
            "tests.test_asl_supervisor_audit_run_config:"
            "AuditPlanTests.test_opting_out_is_persisted_as_skipped_immediately",
        ),
    ),
    (
        "The auditor evaluates operational behavior only and runs under an "
        "enforceable read-only profile.",
        (
            "tests.test_asl_supervisor_audit_evidence_determinism:"
            "EvidenceBundleTests.test_application_source_and_diffs_are_excluded",
            "tests.test_asl_supervisor_audit_evidence_determinism:"
            "EvidenceBundleTests.test_credentials_never_reach_the_bundle",
            "tests.test_asl_supervisor_audit_agent_engine_answers:"
            "AgentAuditEngineTests.test_the_prompt_orders_evidence_before_any_source_reading",
            "tests.test_asl_supervisor_audit_restricted:"
            "RestrictedAuditTests.test_the_auditor_is_launched_read_only_without_the_plan_directory",
            "tests.test_asl_assurance_lifecycle_terminal_audit:"
            "TerminalAuditTests.test_a_harness_that_cannot_enforce_the_profile_never_runs",
            "tests.test_asl_assurance_lifecycle_terminal_audit:"
            "TerminalAuditTests.test_an_auditor_that_touched_the_workspace_is_unavailable",
            "tests.test_asl_supervisor_audit_agent_engine_delivery:"
            "AgentAuditEngineTests.test_a_malformed_answer_is_retried_in_a_fresh_invocation",
            "tests.test_asl_supervisor_audit_agent_engine_delivery:"
            "AgentAuditEngineTests.test_two_malformed_answers_keep_the_deterministic_findings",
            "tests.test_asl_acceptance:"
            "AuditAccessProfileTests.test_no_audit_argv_carries_a_worker_write_grant",
        ),
    ),
    (
        "The ledger provides stable ASI-N references, concurrency-safe "
        "allocation, deterministic deduplication, recurrence history, "
        "append-only dispositions, irreversible merge lineage, and quick "
        "human closeouts.",
        (
            "tests.test_asl_assurance_models_identity:IssueIdTests.test_canonical_and_shorthand_forms_agree",
            "tests.test_asl_assurance_ledger_identity:IssueIdTests.test_concurrent_threads_never_mint_the_same_id",
            "tests.test_asl_assurance_ledger_identity:"
            "IssueIdTests.test_concurrent_processes_never_mint_the_same_id",
            "tests.test_asl_assurance_ledger_storage:"
            "CrashRecoveryTests.test_a_partial_final_line_is_readable_history_not_a_read_failure",
            "tests.test_asl_assurance_projection_publication:"
            "DeduplicationTests.test_a_matching_fingerprint_routes_to_the_existing_issue",
            "tests.test_asl_assurance_projection_lifecycle:"
            "LifecycleTests.test_a_recurrence_after_resolution_reopens_the_issue",
            "tests.test_asl_assurance_projection_lifecycle:"
            "LifecycleTests.test_a_recurrence_after_dismissal_stays_closed",
            "tests.test_asl_assurance_projection_lifecycle:MergeTests.test_merge_is_irreversible",
            "tests.test_asl_assurance_projection_lifecycle:MergeTests.test_merge_rejects_a_cycle",
            "tests.test_asl_assurance_cli_closeout:AssuranceCloseoutTests.test_a_second_closeout_needs_replace",
            "tests.test_asl_assurance_cli_closeout:AssuranceCloseoutTests.test_a_replacement_names_the_closeout_it_supersedes",
            "tests.test_asl_acceptance_ledger:"
            "AppendOnlyLedgerTests.test_every_canonical_operation_only_appends",
        ),
    ),
    (
        "Run, status, Autopilot, Workbench, and metrics expose assurance "
        "without driving implementation or changing results.",
        (
            "tests.test_asl_supervisor_audit_run_output_state:"
            "AuditStateProjectionTests.test_status_projects_the_audit_without_launching_one",
            "tests.test_asl_assurance_lifecycle_coordinator:"
            "TerminalizationCoordinatorTests.test_the_exit_code_survives_the_coordinator_unchanged",
            "tests.test_asl_assurance_lifecycle_terminal_outcomes:"
            "TerminalBranchRoutingTests.test_an_unavailable_audit_changes_nothing_it_graded",
            "tests.test_asl_workbench_summary_completion:WorkbenchSummaryTests.test_assurance_summary_carries_no_audit_controls",
            "tests.test_asl_assurance_metrics:"
            "CoverageMetricsTests.test_coverage_separates_graded_runs_from_merely_recorded_ones",
            "skills/asl/scripts/test_asl_autopilot_review_verdict.py:"
            "RunAutopilotReviewAuditTest.test_run_result_omits_assurance_when_no_audit_ran",
            "tests.test_asl_acceptance:"
            "ObservationalStatusTests.test_the_operator_read_path_cannot_reach_a_harness",
            "tests.test_asl_acceptance_audit:AdvisoryVerdictTests.test_auditing_rewrites_nothing_the_run_recorded",
        ),
    ),
    (
        "Findings become editable improvement-plan drafts only through an "
        "explicit human command.",
        (
            "tests.test_asl_assurance_cli_plan:AssurancePlanTests.test_a_valid_reply_becomes_the_proposal_and_then_a_record",
            "tests.test_asl_assurance_cli_plan_safety:AssurancePlanTests.test_drafting_cannot_reach_the_commands_that_change_a_repository",
            "tests.test_asl_assurance_cli_plan_safety:AssurancePlanTests.test_a_mutation_is_reported_ahead_of_the_exit_code",
        ),
    ),
    (
        "Existing registered multi-repository behavior, legacy "
        "single-repository compatibility, state/event readers, retention, "
        "and current tests remain intact.",
        (
            "tests.test_asl_e2e_multi_repo_lifecycle:"
            "MultiRepositoryEndToEndQATests.test_four_repository_public_core_lifecycle",
            "tests.test_asl_e2e_lifecycle:"
            "EndToEndQATests.test_full_lifecycle_repairs_legacy_plan_and_runs_to_completion",
            "tests.test_asl_supervisor_audit_run_config:"
            "AuditRunConfigPersistenceTests.test_resume_restores_persisted_audit_settings",
            "tests.test_asl_assurance_lifecycle_catchup:"
            "PendingAuditCatchupTests.test_an_abandoned_run_is_audited_after_the_fact",
            "tests.test_asl_assurance_lifecycle_catchup:"
            "PendingAuditCatchupTests.test_processing_twice_publishes_one_audit",
            "tests.test_asl_assurance_lifecycle_catchup:"
            "PendingAuditCatchupTests.test_a_run_that_predates_auditing_is_reported_never_audited",
            "tests.test_asl_retention:"
            "AssurancePinningTests.test_open_issue_pins_an_otherwise_reclaimable_run",
            "tests.test_asl_acceptance_ledger:"
            "PreFeatureCompatibilityTests."
            "test_a_state_root_without_a_ledger_reads_as_an_empty_inbox",
        ),
    ),
)


def _split_reference(reference: str) -> tuple[str, str, str]:
    location, _, qualified = reference.partition(":")
    class_name, _, method_name = qualified.partition(".")
    return location, class_name, method_name


RUN_ID = "20260810T101112123456Z-4242"


FIXED_CLOCK = datetime(2026, 8, 10, 10, 11, 12, tzinfo=UTC)


MANIFEST = (
    "# Manifest\n\n## Repositories\n\n"
    "| Alias | Path | Origin |\n"
    "| --- | --- | --- |\n"
    "| app | app | https://example.invalid/app.git |\n"
)


def _agent_reply(locator: str) -> str:
    body = json.dumps(
        {
            "schema_version": supervisor_audit_agent.AGENT_OUTPUT_SCHEMA_VERSION,
            "lanes": [
                {
                    "lane": lane,
                    "status": "pass",
                    "summary": f"{lane} judged from the recorded evidence",
                    "evidence": [locator],
                }
                for lane in RUBRIC_LANES
            ],
            "findings": [],
        }
    )
    return f"```json\n{body}\n```\n"


class _Auditor:
    """Stand-in auditor that answers all-clear from the recorded evidence."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        self.calls += 1
        return harnesses.HarnessResult(
            argv=[name],
            returncode=0,
            stdout="",
            stderr="",
            last_message=self.reply,
            name=name,
        )


INPUT_DIGEST = "a" * 64


OTHER_DIGEST = "b" * 64


RUN_A = "20260810T100000000000Z-4241"


RUN_B = "20260810T110000000000Z-4242"


T0 = "2026-08-10T10:00:00+00:00"


#: A metrics window wide enough that nothing these tests write falls outside it.
WHOLE_HISTORY = datetime(2000, 1, 1, tzinfo=UTC)


def _artifacts(digest: str) -> tuple[assurance_models_findings.EvidenceRef, ...]:
    return (
        assurance_models_findings.EvidenceRef(
            kind="artifact",
            locator=f"run/{assurance_models_vocab.AUDIT_INPUT_FILENAME}",
            digest=digest,
        ),
        assurance_models_findings.EvidenceRef(
            kind="artifact",
            locator=f"run/{assurance_models_vocab.AUDIT_REPORT_FILENAME}",
        ),
    )


def _finding(invariant_key: str) -> assurance_models_findings.Finding:
    return assurance_models_findings.Finding(
        category="lifecycle",
        component="agent_session_loop/runner.py",
        invariant_key=invariant_key,
        lane="lifecycle-retry-resume",
        severity="high",
        summary="A retry ran after the budget was exhausted.",
        recommendation="Clamp the retry counter before relaunching.",
        evidence=("events.jsonl",),
    )
