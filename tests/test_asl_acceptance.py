"""Final acceptance: the criteria matrix and the architectural invariants.

The matrix names, for every acceptance criterion of the assurance work, the
tests that prove it. Resolving those names is itself a test, so renaming or
deleting a regression fails here instead of silently uncovering a criterion.

The remaining classes assert the four whole-system properties no single
session owns: no adapter drives implementation tools under the audit profile,
an audit never changes what the run it graded recorded, the operator read path
cannot launch an auditor, and no canonical ledger operation deletes history.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
from agent_session_loop import (
    assurance_report,
    events,
    harnesses,
    plans,
    state,
    supervisor_audit,
    supervisor_audit_agent,
    supervisor_audit_evidence,
)
from agent_session_loop.assurance_models import RUBRIC_LANES, AuditRole
from agent_session_loop.assurance_projection import AuditPublication
from tests import run_suite
from tests.support import REPO_ROOT, init_git_repo

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
            "tests.test_asl_authoring:"
            "SplitRepositoryFootprintTests."
            "test_multi_repository_plan_without_a_registry_is_refused",
            "tests.test_asl_authoring:"
            "SplitRepositoryFootprintTests."
            "test_write_path_refuses_contracts_that_span_repositories",
            "tests.test_asl_preflight:"
            "RepositoryIntentPreflightTests."
            "test_payload_reports_the_repositories_the_plan_intends_to_reach",
            "tests.test_asl_prepare:"
            "LegacyRepositoryIntentPrepareTests."
            "test_build_validation_packet_refuses_a_multi_repo_intent",
            "tests.test_asl_run_gates:"
            "RunReadinessGateTests."
            "test_allow_not_loop_ready_does_not_bypass_repository_intent",
            "tests.test_asl_e2e:"
            "KilnTwinCloneRegressionTests."
            "test_legacy_plan_declaring_the_sibling_clone_launches_no_harness",
            "tests.test_asl_e2e:"
            "KilnTwinCloneRegressionTests."
            "test_expansion_note_redirect_to_the_sibling_clone_launches_no_harness",
            "tests.test_asl_e2e:"
            "KilnTwinCloneRegressionTests."
            "test_registered_workspace_targeting_the_testing_clone_succeeds",
        ),
    ),
    (
        "Every eligible persisted loop ends with exactly one truthful assurance state.",
        (
            "tests.test_asl_assurance_lifecycle:"
            "TerminalBranchRoutingTests."
            "test_a_completed_run_is_audited_once_and_keeps_its_exit_code",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalBranchRoutingTests."
            "test_a_paused_run_is_audited_once_and_keeps_its_pause_reason",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalizationCoordinatorTests."
            "test_finalizing_is_reachable_only_through_the_coordinator",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalAuditTests.test_an_all_clear_audit_writes_every_canonical_artifact",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalAuditTests.test_republishing_the_same_audit_leaves_one_occurrence",
            "tests.test_asl_acceptance:"
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
            "tests.test_asl_assurance_models:SummaryInvariantTests.test_all_clear_creates_no_issue",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalAuditTests.test_an_agent_that_never_answers_still_publishes_the_checks",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalAuditTests.test_a_failed_publication_leaves_no_report_behind",
            "tests.test_asl_supervisor_audit:"
            "AuditPlanTests.test_opting_out_is_persisted_as_skipped_immediately",
        ),
    ),
    (
        "The auditor evaluates operational behavior only and runs under an "
        "enforceable read-only profile.",
        (
            "tests.test_asl_supervisor_audit:"
            "EvidenceBundleTests.test_application_source_and_diffs_are_excluded",
            "tests.test_asl_supervisor_audit:"
            "EvidenceBundleTests.test_credentials_never_reach_the_bundle",
            "tests.test_asl_supervisor_audit:"
            "AgentAuditEngineTests.test_the_prompt_orders_evidence_before_any_source_reading",
            "tests.test_asl_supervisor_audit:"
            "RestrictedAuditTests.test_the_auditor_is_launched_read_only_without_the_plan_directory",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalAuditTests.test_a_harness_that_cannot_enforce_the_profile_never_runs",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalAuditTests.test_an_auditor_that_touched_the_workspace_is_unavailable",
            "tests.test_asl_supervisor_audit:"
            "AgentAuditEngineTests.test_a_malformed_answer_is_retried_in_a_fresh_invocation",
            "tests.test_asl_supervisor_audit:"
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
            "tests.test_asl_assurance_models:IssueIdTests.test_canonical_and_shorthand_forms_agree",
            "tests.test_asl_assurance_ledger:IssueIdTests.test_concurrent_threads_never_mint_the_same_id",
            "tests.test_asl_assurance_ledger:"
            "IssueIdTests.test_concurrent_processes_never_mint_the_same_id",
            "tests.test_asl_assurance_ledger:"
            "CrashRecoveryTests.test_a_partial_final_line_is_readable_history_not_a_read_failure",
            "tests.test_asl_assurance_projection:"
            "DeduplicationTests.test_a_matching_fingerprint_routes_to_the_existing_issue",
            "tests.test_asl_assurance_projection:"
            "LifecycleTests.test_a_recurrence_after_resolution_reopens_the_issue",
            "tests.test_asl_assurance_projection:"
            "LifecycleTests.test_a_recurrence_after_dismissal_stays_closed",
            "tests.test_asl_assurance_projection:MergeTests.test_merge_is_irreversible",
            "tests.test_asl_assurance_projection:MergeTests.test_merge_rejects_a_cycle",
            "tests.test_asl_assurance_cli:AssuranceCloseoutTests.test_a_second_closeout_needs_replace",
            "tests.test_asl_assurance_cli:"
            "AssuranceCloseoutTests.test_a_replacement_names_the_closeout_it_supersedes",
            "tests.test_asl_acceptance:"
            "AppendOnlyLedgerTests.test_every_canonical_operation_only_appends",
        ),
    ),
    (
        "Run, status, Autopilot, Workbench, and metrics expose assurance "
        "without driving implementation or changing results.",
        (
            "tests.test_asl_supervisor_audit:"
            "AuditStateProjectionTests.test_status_projects_the_audit_without_launching_one",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalizationCoordinatorTests.test_the_exit_code_survives_the_coordinator_unchanged",
            "tests.test_asl_assurance_lifecycle:"
            "TerminalBranchRoutingTests.test_an_unavailable_audit_changes_nothing_it_graded",
            "tests.test_asl_workbench_summary:"
            "WorkbenchSummaryTests.test_assurance_summary_carries_no_audit_controls",
            "tests.test_asl_assurance_metrics:"
            "CoverageMetricsTests.test_coverage_separates_graded_runs_from_merely_recorded_ones",
            "skills/asl/scripts/test_asl_autopilot.py:"
            "RunAutopilotReviewTest.test_run_result_omits_assurance_when_no_audit_ran",
            "tests.test_asl_acceptance:"
            "ObservationalStatusTests.test_the_operator_read_path_cannot_reach_a_harness",
            "tests.test_asl_acceptance:AdvisoryVerdictTests.test_auditing_rewrites_nothing_the_run_recorded",
        ),
    ),
    (
        "Findings become editable improvement-plan drafts only through an "
        "explicit human command.",
        (
            "tests.test_asl_assurance_cli:"
            "AssurancePlanTests.test_a_valid_reply_becomes_the_proposal_and_then_a_record",
            "tests.test_asl_assurance_cli:"
            "AssurancePlanTests.test_drafting_cannot_reach_the_commands_that_change_a_repository",
            "tests.test_asl_assurance_cli:"
            "AssurancePlanTests.test_a_mutation_is_reported_ahead_of_the_exit_code",
        ),
    ),
    (
        "Existing registered multi-repository behavior, legacy "
        "single-repository compatibility, state/event readers, retention, "
        "and current tests remain intact.",
        (
            "tests.test_asl_e2e:"
            "MultiRepositoryEndToEndQATests.test_four_repository_public_core_lifecycle",
            "tests.test_asl_e2e:"
            "EndToEndQATests.test_full_lifecycle_repairs_legacy_plan_and_runs_to_completion",
            "tests.test_asl_supervisor_audit:"
            "AuditRunConfigPersistenceTests.test_resume_restores_persisted_audit_settings",
            "tests.test_asl_assurance_lifecycle:"
            "PendingAuditCatchupTests.test_an_abandoned_run_is_audited_after_the_fact",
            "tests.test_asl_assurance_lifecycle:"
            "PendingAuditCatchupTests.test_processing_twice_publishes_one_audit",
            "tests.test_asl_assurance_lifecycle:"
            "PendingAuditCatchupTests.test_a_run_that_predates_auditing_is_reported_never_audited",
            "tests.test_asl_retention:"
            "AssurancePinningTests.test_open_issue_pins_an_otherwise_reclaimable_run",
            "tests.test_asl_acceptance:"
            "PreFeatureCompatibilityTests."
            "test_a_state_root_without_a_ledger_reads_as_an_empty_inbox",
        ),
    ),
)


def _split_reference(reference: str) -> tuple[str, str, str]:
    location, _, qualified = reference.partition(":")
    class_name, _, method_name = qualified.partition(".")
    return location, class_name, method_name


class AcceptanceMatrixTests(unittest.TestCase):
    """Every acceptance criterion names regressions that actually exist."""

    def test_every_acceptance_criterion_resolves_to_an_executable_regression(
        self,
    ) -> None:
        for criterion, references in ACCEPTANCE_MATRIX:
            with self.subTest(criterion=criterion):
                self.assertTrue(references, "criterion names no regression")
            for reference in references:
                with self.subTest(reference=reference):
                    location, class_name, method_name = _split_reference(reference)
                    self.assertTrue(
                        method_name.startswith("test"),
                        f"{reference} does not name a test method",
                    )
                    if location.endswith(".py"):
                        self._assert_script_suite_defines(
                            location, class_name, method_name
                        )
                    else:
                        self._assert_module_defines(location, class_name, method_name)

    def _assert_module_defines(
        self, module_name: str, class_name: str, method_name: str
    ) -> None:
        module = importlib.import_module(module_name)
        case = getattr(module, class_name, None)
        self.assertIsNotNone(case, f"{module_name} defines no {class_name}")
        self.assertTrue(
            issubclass(case, unittest.TestCase),
            f"{module_name}:{class_name} is not a TestCase",
        )
        self.assertTrue(
            callable(getattr(case, method_name, None)),
            f"{module_name}:{class_name} defines no {method_name}",
        )

    def _assert_script_suite_defines(
        self, relative_path: str, class_name: str, method_name: str
    ) -> None:
        """Standalone suites are read rather than imported.

        They live outside the ``tests`` package and expect to run as scripts,
        so parsing proves the regression exists without adopting their import
        assumptions.
        """
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                names = {
                    child.name
                    for child in node.body
                    if isinstance(child, ast.FunctionDef)
                }
                self.assertIn(method_name, names, f"{relative_path}:{class_name}")
                return
        self.fail(f"{relative_path} defines no {class_name}")

    def test_every_named_module_runs_in_a_suite_shard(self) -> None:
        """A regression the suite driver never launches proves nothing."""
        shard_modules = set(run_suite.FAST_MODULES) | set(run_suite.INTEGRATION_MODULES)
        script_suites = set(run_suite.SCRIPT_SUITES)
        for _, references in ACCEPTANCE_MATRIX:
            for reference in references:
                location = _split_reference(reference)[0]
                with self.subTest(location=location):
                    if location.endswith(".py"):
                        self.assertIn(location, script_suites)
                    else:
                        self.assertIn(location, shard_modules)


class AuditAccessProfileTests(unittest.TestCase):
    """No adapter drives implementation tools while auditing."""

    #: Argv fragments that grant a worker's write access. None may appear in an
    #: audit invocation for any harness, whatever that harness calls them.
    WORKER_GRANTS = (
        "--dangerously-bypass-approvals-and-sandbox",
        "workspace-write",
        "bypassPermissions",
        "dontAsk",
        "--allow-all-tools",
        "--allow-all-paths",
    )

    def _audit_argv(self, name: str, read_root: Path, cwd: Path) -> list[str]:
        adapter = harnesses.get_adapter(name)
        # Codex states its audit profile over the app-server protocol, so its
        # *argv* boundary is the fallback transport's argv; the protocol side
        # is asserted separately against the turn plan.
        builder = getattr(adapter, "build_exec_argv", adapter.build_argv)
        return builder(
            cwd=cwd,
            plan_dir=None,
            prompt="audit this run",
            last_message_path=cwd / "last-message.txt",
            access=harnesses.audit_read_only_access((read_root,)),
        )

    def _audit_access_text(self, name: str, read_root: Path, cwd: Path) -> str:
        """Everything describing one audit turn's access, on either transport.

        Argv alone would pass vacuously for a harness driven over a protocol,
        so this renders argv *and* the turn plan, which is where the sandbox,
        the approval policy, and the writable roots now live.
        """
        adapter = harnesses.get_adapter(name)
        invocation = adapter.build_invocation(
            cwd=cwd,
            plan_dir=None,
            prompt="audit this run",
            last_message_path=cwd / "last-message.txt",
            access=harnesses.audit_read_only_access((read_root,)),
        )
        rendered = "\n".join(invocation.argv)
        if invocation.turn_plan is not None:
            rendered += "\n" + json.dumps(
                invocation.turn_plan.to_payload(), sort_keys=True
            )
        return rendered

    def test_no_audit_argv_carries_a_worker_write_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in harnesses.HARNESS_NAMES:
                argv = self._audit_argv(name, root / "evidence", root / "workdir")
                described = self._audit_access_text(
                    name, root / "evidence", root / "workdir"
                )
                for grant in self.WORKER_GRANTS:
                    with self.subTest(harness=name, grant=grant):
                        self.assertNotIn(grant, argv)
                        self.assertNotIn(grant, described)

    def test_no_audit_argv_enables_a_mutating_tool(self) -> None:
        """Each adapter denies mutation in the vocabulary its own CLI enforces."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = self._audit_argv("codex", root / "evidence", root / "workdir")
            self.assertNotIn("--add-dir", codex)
            self.assertEqual(["read-only"], codex[codex.index("-s") + 1 :][:1])
            codex_plan = harnesses.get_adapter("codex").build_turn_plan(  # type: ignore[attr-defined]
                cwd=root / "workdir",
                plan_dir=None,
                access=harnesses.audit_read_only_access((root / "evidence",)),
            )
            self.assertEqual("read-only", codex_plan.thread_params.get("sandbox"))
            self.assertNotIn("config", codex_plan.thread_params)

            claude = self._audit_argv("claude", root / "evidence", root / "workdir")
            self.assertEqual(
                ["plan"], claude[claude.index("--permission-mode") + 1 :][:1]
            )
            self.assertIn(harnesses.CLAUDE_AUDIT_DENIED_TOOLS, claude)
            for tool in harnesses.CLAUDE_AUDIT_TOOLS.split(","):
                self.assertNotIn(tool, ("Bash", "Edit", "Write"))

            copilot = self._audit_argv("copilot", root / "evidence", root / "workdir")
            for denied in harnesses.COPILOT_AUDIT_DENIED_TOOLS:
                self.assertIn(f"--deny-tool={denied}", copilot)

    def test_no_adapter_accepts_a_writable_plan_dir_for_an_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            for name in harnesses.HARNESS_NAMES:
                with (
                    self.subTest(harness=name),
                    self.assertRaises(harnesses.HarnessError),
                ):
                    harnesses.get_adapter(name).check_access(
                        harnesses.audit_read_only_access((Path(tmp),)),
                        plan_dir=plan_dir,
                        unsafe_bypass=False,
                    )

    def test_no_adapter_lets_an_audit_inherit_unsafe_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in harnesses.HARNESS_NAMES:
                with (
                    self.subTest(harness=name),
                    self.assertRaises(harnesses.HarnessError),
                ):
                    harnesses.get_adapter(name).check_access(
                        harnesses.audit_read_only_access((Path(tmp),)),
                        plan_dir=None,
                        unsafe_bypass=True,
                    )


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


class AdvisoryVerdictTests(unittest.TestCase):
    """A finding is reported, never enforced: the run's own record is final."""

    def _history(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state" / "planhash"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(MANIFEST, encoding="utf-8")
        (plan_dir / "session-01.md").write_text(
            "# Session 1\n\n"
            f"{plans.READS_REPOSITORIES_PREFIX} app\n"
            f"{plans.WRITES_REPOSITORIES_PREFIX} app\n",
            encoding="utf-8",
        )
        (plan_dir / plans.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "workspace_root": str(workspace)}),
            encoding="utf-8",
        )

        turn_dir = run_dir / "turn-0001-01"
        turn_dir.mkdir()
        (turn_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(
                {
                    "ok": True,
                    "returncode": 0,
                    "timed_out": False,
                    "interrupted": False,
                    "session_kind": "implementation",
                    "artifact_dir": str(turn_dir),
                    "stdout_path": str(turn_dir / "stdout.log"),
                    "stderr_path": str(turn_dir / "stderr.log"),
                    "last_message_path": str(turn_dir / "last-message.txt"),
                    "result_path": str(turn_dir / state.RESULT_FILE_NAME),
                    "invocation": {"name": "codex", "cwd": str(workspace / "app")},
                    # No workspace_write_guard: a completed turn that recorded
                    # no guard result is the gap the audit is supposed to find.
                    "verification": {
                        "passed": True,
                        "checks": [{"name": "tests", "passed": True, "returncode": 0}],
                        "skipped_checks": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        (turn_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
        (turn_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
        (turn_dir / "last-message.txt").write_text("Done.\n", encoding="utf-8")

        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "harness": "codex",
                    "ok": True,
                    "paused": False,
                    "workspace_root": str(workspace),
                    "commit_mode": "session",
                    "review_completed": True,
                    "review_harness": "claude",
                    "review_verdict": "approved",
                    "repositories": [
                        {
                            "alias": "app",
                            "path": "app",
                            "origin": "https://example.invalid/app.git",
                            "resolved_root": str(workspace / "app"),
                        }
                    ],
                    "run_config": {"cwd": str(workspace)},
                }
            ),
            encoding="utf-8",
        )
        (state_dir / events.EVENTS_FILE_NAME).write_text(
            json.dumps(
                {
                    "name": "run_started",
                    "version": events.EVENT_SCHEMA_VERSION,
                    "created_at": "2026-08-10T10:11:12Z",
                    "payload": {"run_id": RUN_ID},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return workspace, plan_dir, state_dir, run_dir

    def _plan(self) -> supervisor_audit.AuditPlan:
        return supervisor_audit.AuditPlan(
            config=supervisor_audit.AuditConfig(
                enabled=True, harness="claude", timeout_seconds=30
            ),
            role=AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=30.0,
            ),
            state="pending",
            reason=None,
        )

    def _audit(self, history: tuple[Path, Path, Path, Path]) -> models.AssuranceSummary:
        workspace, plan_dir, state_dir, run_dir = history
        bundle = supervisor_audit_evidence.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            terminal_pending=True,
        )
        return supervisor_audit.audit_terminal_run(
            self._plan(),
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            repositories=("app",),
            ledger_state_dir=state_dir.parent,
            durable=False,
            now=lambda: FIXED_CLOCK,
            _run_harness_fn=_Auditor(_agent_reply(bundle.evidence[0].locator)),
        )

    def test_a_deterministic_failure_ends_the_run_findings_and_opens_one_issue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            summary = self._audit(history)

            self.assertEqual("findings", summary.state, summary.reason)
            self.assertEqual(1, len(summary.issue_ids), summary.issue_ids)
            self.assertTrue(summary.issue_ids[0].startswith("ASI-"))
            inbox = projection.read_projection(state_dir=history[2].parent)
            issue = inbox.issue(summary.issue_ids[0])
            self.assertIsNotNone(issue)
            self.assertEqual("open", issue.state)

    def test_auditing_rewrites_nothing_the_run_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_dir = history[2]
            recorded = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(state_dir.rglob("*"))
                if path.is_file()
            }

            summary = self._audit(history)

            self.assertEqual("findings", summary.state, summary.reason)
            for path, digest in recorded.items():
                with self.subTest(path=path.name):
                    self.assertEqual(
                        digest, hashlib.sha256(path.read_bytes()).hexdigest()
                    )
            written = {
                path
                for path in state_dir.rglob("*")
                if path.is_file() and path not in recorded
            }
            run_dir = history[3]
            for path in written:
                with self.subTest(path=path.name):
                    self.assertTrue(path.is_relative_to(run_dir))


class ObservationalStatusTests(unittest.TestCase):
    """Nothing an operator reads assurance through can start a harness."""

    #: Every module `asl status` reaches to render assurance.
    READ_PATH = (
        assurance_report,
        projection,
        ledger,
        models,
    )

    #: Modules that start an agent turn. The read path may borrow a name from
    #: ``harnesses`` — the harness-name tuple is a vocabulary, not a launcher —
    #: so importing it is allowed and calling into it is not.
    FORBIDDEN_IMPORTS = (
        "subprocess",
        "supervisor_audit",
        "assurance_catchup",
        "runner",
    )

    #: The call names that actually start something. Generic names such as
    #: ``run`` are left out on purpose: the read path calls ``inbox.run(...)``
    #: to look a run up, and banning the import of ``subprocess`` already
    #: settles the only ambiguous case.
    FORBIDDEN_CALLS = (
        "run_harness",
        "build_invocation",
        "build_argv",
        "audit_terminal_run",
        "Popen",
    )

    def test_the_operator_read_path_cannot_reach_a_harness(self) -> None:
        for module in self.READ_PATH:
            source = Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported.add(node.module.split(".")[0])
                    if node.level:
                        imported.update(alias.name for alias in node.names)
            for forbidden in self.FORBIDDEN_IMPORTS:
                with self.subTest(module=module.__name__, imports=forbidden):
                    self.assertNotIn(forbidden, imported)

            called = {
                node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Attribute, ast.Name))
            }
            for forbidden in self.FORBIDDEN_CALLS:
                with self.subTest(module=module.__name__, calls=forbidden):
                    self.assertNotIn(forbidden, called)


INPUT_DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
RUN_A = "20260810T100000000000Z-4241"
RUN_B = "20260810T110000000000Z-4242"
T0 = "2026-08-10T10:00:00+00:00"
#: A metrics window wide enough that nothing these tests write falls outside it.
WHOLE_HISTORY = datetime(2000, 1, 1, tzinfo=UTC)


def _artifacts(digest: str) -> tuple[models.EvidenceRef, ...]:
    return (
        models.EvidenceRef(
            kind="artifact",
            locator=f"run/{models.AUDIT_INPUT_FILENAME}",
            digest=digest,
        ),
        models.EvidenceRef(
            kind="artifact", locator=f"run/{models.AUDIT_REPORT_FILENAME}"
        ),
    )


def _finding(invariant_key: str) -> models.Finding:
    return models.Finding(
        category="lifecycle",
        component="agent_session_loop/runner.py",
        invariant_key=invariant_key,
        lane="lifecycle-retry-resume",
        severity="high",
        summary="A retry ran after the budget was exhausted.",
        recommendation="Clamp the retry counter before relaunching.",
        evidence=("events.jsonl",),
    )


class AppendOnlyLedgerTests(unittest.TestCase):
    """No canonical operation shortens the ledger or rewrites earlier bytes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name) / "state"
        self.path = ledger.ledger_path(self.state_dir)

    def _bytes(self) -> bytes:
        return self.path.read_bytes() if self.path.exists() else b""

    def _publish(self, run_id: str, digest: str, invariant_key: str) -> None:
        projection.publish_audit(
            AuditPublication(
                run_id=run_id,
                state="findings",
                eligible=True,
                findings=(_finding(invariant_key),),
                artifacts=_artifacts(digest),
                completed_at=T0,
            ),
            state_dir=self.state_dir,
            recorded_at=T0,
        )

    def test_every_canonical_operation_only_appends(self) -> None:
        before = self._bytes()
        steps = (
            ("publish an audit", lambda: self._publish(RUN_A, INPUT_DIGEST, "a.key")),
            (
                "publish a second audit",
                lambda: self._publish(RUN_B, OTHER_DIGEST, "b.key"),
            ),
            (
                "republish the first",
                lambda: self._publish(RUN_A, INPUT_DIGEST, "a.key"),
            ),
            (
                "resolve an issue",
                lambda: projection.record_disposition(
                    "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=T0
                ),
            ),
            (
                "merge one issue into another",
                lambda: projection.record_merge(
                    "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=T0
                ),
            ),
            (
                "close out a run",
                lambda: projection.record_closeout(
                    RUN_A,
                    "accepted",
                    issue_ids=("ASI-2",),
                    state_dir=self.state_dir,
                    recorded_at=T0,
                ),
            ),
            (
                "replace that closeout",
                lambda: projection.record_closeout(
                    RUN_A,
                    "reworked",
                    rework_minutes=15,
                    replace=True,
                    state_dir=self.state_dir,
                    recorded_at=T0,
                ),
            ),
            (
                "record a drafted proposal",
                lambda: projection.record_improvement_plan_draft(
                    models.ImprovementPlanDraft(
                        issue_id="ASI-2",
                        harness="claude",
                        output_path="/tmp/asi-2.md",
                        output_sha256="c" * 64,
                        drafted_at=T0,
                    ),
                    state_dir=self.state_dir,
                    recorded_at=T0,
                ),
            ),
        )

        records = 0
        for label, operation in steps:
            operation()
            after = self._bytes()
            with self.subTest(operation=label):
                self.assertTrue(
                    after.startswith(before), f"{label} rewrote earlier bytes"
                )
                view = ledger.read_ledger(state_dir=self.state_dir)
                self.assertGreaterEqual(len(view.records), records)
                records = len(view.records)
            before = after

        final = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual(records, len(final.records))

    def test_reading_the_inbox_never_writes_to_it(self) -> None:
        self._publish(RUN_A, INPUT_DIGEST, "a.key")
        before = self._bytes()

        ledger.read_ledger(state_dir=self.state_dir)
        projection.read_projection(state_dir=self.state_dir)
        assurance_report.collect_assurance_metrics(
            state_dir=self.state_dir, since=WHOLE_HISTORY
        )

        self.assertEqual(before, self._bytes())


class PreFeatureCompatibilityTests(unittest.TestCase):
    """A state root written before assurance existed is an empty inbox."""

    def test_a_state_root_without_a_ledger_reads_as_an_empty_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "planhash").mkdir()

            inbox = projection.read_projection(state_dir=state_dir)
            metrics = assurance_report.collect_assurance_metrics(
                state_dir=state_dir, since=WHOLE_HISTORY
            )

            self.assertEqual((), inbox.issues)
            self.assertTrue(metrics["available"])
            self.assertFalse(ledger.ledger_path(state_dir).exists())


if __name__ == "__main__":
    unittest.main()
