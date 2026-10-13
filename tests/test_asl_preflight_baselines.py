"""Preflight baseline evidence tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_session_loop import (
    preflight,
)
from tests.support import (
    _command_contract,
    _write_loop_ready_plan,
)
from tests.support_asl_preflight import PreflightCoreFixtures


class PreflightBaselineEvidenceTests(PreflightCoreFixtures):
    def test_baseline_missing_evidence_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-missing", findings[0].category)
            self.assertEqual("red", findings[0].severity)
            self.assertIn("asl baseline", findings[0].detail)
            self.assertFalse(report.ok)

    def test_baseline_recorded_pass_emits_no_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(plan_dir, repo_root, self._fresh_result(repo_root))

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            self.assertEqual([], self._baseline_findings(report))

    def test_baseline_unexpected_failure_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(repo_root, status="fail", satisfied=False),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-unexpected", findings[0].category)
            self.assertEqual("red", findings[0].severity)

    def test_baseline_stale_evidence_emits_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(
                    repo_root, repository_fingerprint="stale-fingerprint"
                ),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-stale", findings[0].category)
            self.assertEqual("yellow", findings[0].severity)

    def test_baseline_timeout_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(repo_root, status="timeout", satisfied=False),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-timeout", findings[0].category)
            self.assertEqual("red", findings[0].severity)

    def test_baseline_total_budget_overrun_not_accepted_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            # Satisfied on its own terms, but produced by a run that crossed
            # the total baseline budget: must not count as loop-ready evidence.
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(repo_root, total_budget_exceeded=True),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-timeout", findings[0].category)
            self.assertEqual("red", findings[0].severity)
            self.assertIn("total baseline budget", findings[0].message)
            self.assertFalse(report.ok)

    def test_baseline_near_timeout_emits_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(
                    repo_root, near_timeout=True, duration_seconds=110.0
                ),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-near-timeout", findings[0].category)
            self.assertEqual("yellow", findings[0].severity)

    def test_baseline_findings_suppressed_after_a_session_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("complete", "pending"),
                contracts_by_id={
                    "1": _command_contract('["true"]', session="1"),
                    "2": _command_contract('["false"]', session="2"),
                },
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            # Implementation has started; no baseline gating even with no evidence.
            self.assertEqual([], self._baseline_findings(report))

    def test_baseline_skip_expectation_is_never_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "README.md").write_text("x\n", encoding="utf-8")
            _write_loop_ready_plan(plan_dir, ("pending",))

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            self.assertEqual([], self._baseline_findings(report))

    def test_baseline_not_evaluated_without_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )

            report = preflight.run_preflight(plan_dir)

            self.assertEqual([], self._baseline_findings(report))
