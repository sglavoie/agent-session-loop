"""Preflight resume-readiness and unhashed-state tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    plans_content_hash,
    plans_models,
    plans_readiness,
    plans_snapshot,
    preflight,
    preflight_models,
)
from tests.support import (
    _command_contract,
    _write_loop_ready_plan,
    init_git_repo,
    write_plan,
)


class PreflightResumeTests(unittest.TestCase):
    _SKIPPED_CHECKS = (
        "_check_session_contracts",
        "_check_ambiguity_sentinels",
        "_check_path_references",
        "_check_verification_scope",
        "_check_command_runnability",
        "_check_progress_journal",
        "_check_baselines",
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.xdg_state = self.tmp_path / "xdg-state"
        self.xdg_state.mkdir()
        self.plan_dir = self.tmp_path / "plan"
        self.repo_dir = self.tmp_path / "repo"
        init_git_repo(self.repo_dir)
        _write_loop_ready_plan(self.plan_dir, ("pending",))

    def _rewrite_readiness(
        self,
        *,
        state: plans_models.ReadinessState = "loop-ready",
        sha: str | None = None,
    ) -> None:
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state=state,
                readiness_sha256=sha
                or plans_content_hash.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _resume(self, **kwargs: Any) -> preflight_models.PreflightReport | None:
        kwargs.setdefault("snapshot", plans_snapshot.inspect_plan(self.plan_dir))
        kwargs.setdefault("cwd", self.repo_dir)
        kwargs.setdefault("state_dir", self.xdg_state)
        return preflight.run_resume_preflight(self.plan_dir, **kwargs)

    def _unhashed(self, **kwargs: Any) -> tuple[preflight_models.PreflightFinding, ...]:
        kwargs.setdefault("snapshot", plans_snapshot.inspect_plan(self.plan_dir))
        kwargs.setdefault("cwd", self.repo_dir)
        return preflight.run_unhashed_state_checks(self.plan_dir, **kwargs)

    def test_returns_none_without_snapshot(self) -> None:
        self.assertIsNone(self._resume(snapshot=None))

    def test_returns_none_for_non_resumable_readiness_states(self) -> None:
        for readiness_state in ("draft", "reviewed", "complete"):
            with self.subTest(state=readiness_state):
                self._rewrite_readiness(state=readiness_state)
                self.assertIsNone(self._resume())

    def test_returns_none_when_recorded_sha_does_not_match(self) -> None:
        self._rewrite_readiness(sha="00" * 32)

        self.assertIsNone(self._resume())

    def test_returns_none_when_readiness_metadata_is_missing(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        self.assertIsNone(self._resume())

    def test_returns_none_when_readiness_metadata_is_malformed(self) -> None:
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "**Readiness**: loop-ready", "**Readiness**: bogus"
            ),
            encoding="utf-8",
        )

        self.assertIsNone(self._resume())

    def test_eligible_report_skips_plan_content_checks(self) -> None:
        with mock.patch.multiple(
            preflight, **{name: mock.DEFAULT for name in self._SKIPPED_CHECKS}
        ) as mocks:
            report = self._resume()

        self.assertIsNotNone(report)
        for name, patched in mocks.items():
            self.assertFalse(patched.called, msg=name)
        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertEqual({}, report.contracts_by_session)
        self.assertEqual(
            plans_content_hash.stable_readiness_sha256(self.plan_dir),
            report.expected_readiness_sha256,
        )
        self.assertEqual("loop-ready", report.readiness.state)
        self.assertIsNotNone(report.snapshot)
        categories = {f.category for f in report.findings}
        self.assertEqual({"env-amx", "env-turn-budget"}, categories)

    def test_clean_plan_has_no_unhashed_state_findings(self) -> None:
        self.assertEqual((), self._unhashed())

    def test_stale_progress_journal_surfaces_from_unhashed_state_checks(
        self,
    ) -> None:
        _write_loop_ready_plan(self.plan_dir, ("complete", "pending"))

        report = self._resume()
        findings = self._unhashed()

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        journal = [f for f in findings if f.category == "progress-journal-missing"]
        self.assertEqual(1, len(journal))
        self.assertEqual("yellow", journal[0].severity)

    def test_missing_baseline_evidence_surfaces_from_unhashed_state_checks(
        self,
    ) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _command_contract('["true"]', session="1", expectation="pass")
            },
        )

        report = self._resume()
        findings = self._unhashed()

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        baselines = [f for f in findings if f.category == "baseline-missing"]
        self.assertEqual(1, len(baselines))
        self.assertEqual("red", baselines[0].severity)

    def test_malformed_contract_surfaces_from_unhashed_state_checks(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "## Completion Contract", "## Notes"
            ),
            encoding="utf-8",
        )
        self._rewrite_readiness()

        report = self._resume()
        findings = self._unhashed()

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertEqual(
            ["missing-contract"],
            [f.category for f in findings if f.severity == "red"],
        )

    def test_running_readiness_state_is_eligible(self) -> None:
        self._rewrite_readiness(state="running")

        report = self._resume()

        self.assertIsNotNone(report)
        self.assertEqual("running", report.readiness.state)

    def test_red_environment_finding_surfaces(self) -> None:
        report = self._resume(harness="bogus")

        self.assertIsNotNone(report)
        self.assertFalse(report.ok)
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
