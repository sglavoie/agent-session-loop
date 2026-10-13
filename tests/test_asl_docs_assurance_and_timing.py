"""Assurance and timing-comparison docs regression tests."""

from __future__ import annotations

import argparse
import unittest

from agent_session_loop import (
    assurance_ledger,
    assurance_models,
    cli,
    mailbox_constants,
    state,
    supervisor_audit_agent,
)
from tests.asl_docs_support import ASL_GUIDE, read_asl_reference
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class AssuranceDocsRegressionTests(unittest.TestCase):
    """The assurance vocabulary, surface, and storage as documented."""

    docs: str
    guide: str
    package_readme: str
    asl_skill: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = read_asl_reference()
        cls.guide = (REPO_ROOT / ASL_GUIDE).read_text(encoding="utf-8")
        cls.package_readme = (REPO_ROOT / "agent_session_loop" / "README.md").read_text(
            encoding="utf-8"
        )
        cls.asl_skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(
            encoding="utf-8"
        )

    def test_every_assurance_state_is_documented_by_name(self) -> None:
        for text in (self.guide, self.docs, self.package_readme):
            for audit_state in assurance_models.AUDIT_STATES:
                self.assertIn(
                    f"`{audit_state}`",
                    text,
                    f"docs omit assurance state {audit_state!r}",
                )

    def test_every_assurance_subcommand_is_documented(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        assurance = subparsers.choices["assurance"]
        nested = next(
            action
            for action in assurance._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for name in nested.choices:
            for text in (self.guide, self.docs):
                self.assertIn(
                    f"asl assurance {name}",
                    text,
                    f"docs never mention `asl assurance {name}`",
                )

    def test_every_audit_run_flag_is_documented(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        run_help = subparsers.choices["run"].format_help()
        flags = (
            "--supervisor-audit",
            "--no-supervisor-audit",
            "--audit-harness",
            "--audit-harness-model",
            "--audit-effort",
            "--audit-timeout",
        )
        for flag in flags:
            self.assertIn(flag, run_help, f"`asl run` no longer offers {flag}")
            for text in (self.guide, self.docs):
                self.assertIn(flag, text, f"docs omit run flag {flag}")

    def test_opting_out_is_documented_as_the_only_way_to_run_no_checks(
        self,
    ) -> None:
        for text in (self.guide, self.docs):
            self.assertIn("--no-supervisor-audit", text)
        self.assertIn("skipped` and creates no issue", self.guide)
        self.assertIn("Opting out is the only way to run no checks at all", self.docs)

    def test_issue_references_are_documented_in_the_canonical_form(self) -> None:
        self.assertEqual("ASI-", assurance_models.ISSUE_ID_PREFIX)
        for text in (self.guide, self.docs, self.package_readme):
            self.assertIn("ASI-", text, "docs never show a canonical issue id")

    def test_storage_filenames_match_the_authoritative_constants(self) -> None:
        for name in (
            assurance_ledger.LEDGER_FILE_NAME,
            assurance_ledger.LEDGER_LOCK_FILE_NAME,
            assurance_ledger.LEDGER_INDEX_FILE_NAME,
        ):
            self.assertIn(name, self.docs, f"docs omit ledger file {name}")
        self.assertIn(state.ASSURANCE_PENDING_FILE_NAME, self.docs)
        for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS:
            self.assertIn(name, self.docs, f"docs omit audit artifact {name}")

    def test_the_mailbox_and_the_ledger_are_documented_as_separate_systems(
        self,
    ) -> None:
        """Conflating them is the misreading the documentation must prevent.

        Both are append-only JSONL logs an operator can be told to read, so the
        docs have to say outright that they are different files with different
        scopes, locks, and lifetimes rather than leaving it to be inferred.
        """
        for text in (self.guide, self.docs, self.package_readme):
            self.assertIn(assurance_ledger.LEDGER_FILE_NAME, text)
            self.assertIn(mailbox_constants.MAILBOX_FILE_NAME, text)
            self.assertIn("separate system", text)

    def test_lifecycle_ordering_puts_the_audit_before_the_terminal_record(
        self,
    ) -> None:
        self.assertIn("run_finished", self.guide)
        self.assertIn(
            "before `run_finished` is appended",
            self.docs,
            "docs no longer state that auditing precedes the terminal record",
        )

    def test_pending_recovery_is_documented_as_operator_driven(self) -> None:
        for text in (self.guide, self.docs):
            self.assertIn(state.ASSURANCE_PENDING_FILE_NAME, text)
            self.assertIn("asl assurance process-pending", text)
        self.assertIn("idempotent", self.guide)

    def test_the_improvement_workflow_is_documented_as_human_gated(self) -> None:
        for text in (self.guide, self.docs):
            self.assertIn("asl assurance plan ISSUE --harness NAME --output PATH", text)
        self.assertIn("no commit, and no repository change", self.guide)

    def test_the_autopilot_skill_reports_assurance_without_driving_it(
        self,
    ) -> None:
        self.assertIn("assurance", self.asl_skill)
        self.assertIn("asl assurance list", self.asl_skill)
        self.assertIn("reports assurance and never drives it", self.asl_skill)
        for flag in ("--supervisor-audit", "--audit-harness", "--audit-timeout"):
            self.assertIn(flag, self.asl_skill)

    def test_assurance_docs_define_the_repaired_evidence_semantics(self) -> None:
        for phrase in (
            "`findings-defer-broad-failures`",
            "`resolved_root`",
            "`source: explicit` or `source: review`",
            "`requested_model`",
            "`requested_model_source`",
            "`model_projection` is authoritative",
        ):
            self.assertIn(phrase, self.docs)
        self.assertIn("containment checks use only its canonical", self.docs)
        self.assertIn("canonical `resolved_root`", self.package_readme)
        self.assertIn(
            "per-role `model_projection` is authoritative", self.package_readme
        )

    def test_the_auditor_is_told_a_null_failure_diagnostic_is_the_boundary(
        self,
    ) -> None:
        # The auditor kept refiling a null `failure_diagnostic` on an authored
        # `ci` gate as a defect. The observation is true and the fix it asked
        # for would reverse the fence that keeps application output out of
        # control-plane text, so the prompt has to explain the fence instead.
        prompt = supervisor_audit_agent.AUDIT_PROMPT_TEMPLATE

        self.assertIn("`failure_diagnostic` of `null`", prompt)
        self.assertIn("check ASL did not generate", prompt)
        self.assertIn("retained stderr artifact", prompt)
        self.assertIn("only if a", prompt)

    def test_docs_state_why_an_authored_gate_reports_no_failure_diagnostic(
        self,
    ) -> None:
        for phrase in (
            "`failure_diagnostic`",
            "`null` for\nevery check ASL did not generate itself",
            "never a generated gate",
            "`stderr_artifact_path` retains the full text",
        ):
            self.assertIn(phrase, self.docs)


class TimingComparisonDocsRegressionTests(unittest.TestCase):
    """The procedure for comparing two timing reports, and the rollback."""

    docs: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = read_asl_reference()

    def test_docs_name_the_json_paths_a_comparison_reads(self) -> None:
        # A comparison procedure that names a path the report does not emit is
        # worse than none, so every path it tells an operator to keep is
        # checked against the section the collector's consumer actually builds.
        for path in (
            "`execution_timing.window_start`",
            "`execution_timing.coverage`",
            "`execution_timing.phases`",
            "`execution_timing.review_scope`",
            "`execution_timing.plans`",
            "`verification_checks.cumulative_seconds`",
        ):
            self.assertIn(path, self.docs)

    def test_docs_warn_that_two_rolling_windows_are_not_the_same_interval(
        self,
    ) -> None:
        for phrase in (
            "`window_end` is `null` for a rolling window",
            "two readings never\ncover the same interval",
            "a run enters a window by its *finish*",
            "compare `phases.shares`",
            "not corpus totals",
        ):
            self.assertIn(phrase, self.docs)

    def test_docs_state_the_coverage_check_that_qualifies_a_moved_share(
        self,
    ) -> None:
        for phrase in (
            "`runs_outside_share_basis`",
            "share basis gained or lost",
            "is not the workflow moving",
        ):
            self.assertIn(phrase, self.docs)

    def test_docs_name_the_expected_reconciliation_differences(self) -> None:
        for phrase in (
            "evidence predates a field",
            "only a supervisor audit\nand never emitted `run_started`",
            "pre-window portion of a run that\nstraddles `window_start`",
        ):
            self.assertIn(phrase, self.docs)

    def test_docs_document_rollback_as_a_plain_revert(self) -> None:
        for phrase in (
            "Rollback is a plain revert",
            "without rewriting any\nhistorical state",
            "older readers ignore",
            "stops emitting",
            "never edits an event",
            "`just sync-skills`",
            "`just verify-asl-install`",
        ):
            self.assertIn(phrase, self.docs)


if __name__ == "__main__":
    unittest.main()
