"""Preflight tests for registered multi-repository workspaces."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    baseline,
    plans_content_hash,
    plans_models,
    plans_readiness,
    preflight,
    preflight_hint_resolution,
    preflight_output,
    repository_evidence,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)


class MultiRepositoryPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace_root = self.root / "workspace"
        self.specs = DEFAULT_MULTI_REPO_SPECS[:2]
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            self.specs,
        )
        self.plan_dir = self.root / "plan"
        self.contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", '
            '"name": "readme", "repo": "SVC"}'
        )
        self._write_plan()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_plan(self) -> None:
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={"1": self.contract},
            specs=self.specs,
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_non_git_workspace_with_registered_children_passes(self) -> None:
        self.assertFalse((self.workspace_root / ".git").exists())

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertIsNotNone(report.verification_context)
        self.assertEqual(
            ["SVC", "OAK"],
            [repository.alias for repository in report.repositories],
        )
        self.assertTrue(
            all(repository.resolved_root for repository in report.repositories)
        )

    def test_workspace_is_resolved_once_per_full_preflight(self) -> None:
        original = workspaces.resolve_workspace
        with mock.patch.object(
            workspaces,
            "resolve_workspace",
            wraps=original,
        ) as resolve:
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        self.assertTrue(report.ok)
        resolve.assert_called_once()

    def test_wrong_origin_is_stable_red_finding(self) -> None:
        subprocess.run(
            ["git", "remote", "set-url", "origin", "git@example.com:wrong/repo.git"],
            cwd=self.repositories["OAK"],
            text=True,
            capture_output=True,
            check=True,
        )

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-origin"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertEqual(
            "repository-origin.correct-checkout",
            preflight_hint_resolution.repair_hint_for_finding(findings[0]).check_id,
        )

    def test_missing_registered_child_is_repository_root_failure(self) -> None:
        shutil.rmtree(self.repositories["OAK"])

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-root"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertIn("OAK", findings[0].message)

    def test_overlapping_registry_paths_have_stable_category(self) -> None:
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                f"| OAK | {self.specs[1][1]} |",
                f"| OAK | {self.specs[0][1]} |",
            ),
            encoding="utf-8",
        )

        report = preflight.run_core_preflight(self.plan_dir)

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-overlap"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual(
            "repository-overlap.separate",
            preflight_hint_resolution.repair_hint_for_finding(findings[0]).check_id,
        )

    def test_unavailable_workspace_evidence_fails_closed(self) -> None:
        unavailable = {
            "git": True,
            "unavailable": {"reason": "git-command-failed"},
        }
        with mock.patch.object(
            repository_evidence,
            "capture_repository_fingerprint",
            side_effect=[unavailable, {"git": True}],
        ):
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        findings = [
            finding
            for finding in report.findings
            if "evidence is unavailable" in finding.message
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("repository-root", findings[0].category)
        self.assertIn("SVC", findings[0].message)

    def test_uninspectable_undeclared_repository_produces_no_finding(
        self,
    ) -> None:
        sibling = self.workspace_root / "undeclared-broken"
        (sibling / ".git").mkdir(parents=True)

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertEqual(
            [],
            [
                (finding.category, finding.severity, finding.message)
                for finding in report.findings
                if "undeclared-broken"
                in f"{finding.message}\n{finding.path}\n{finding.detail}"
            ],
        )

    def test_undeclared_breakage_is_ignored_while_registered_still_fails(
        self,
    ) -> None:
        sibling = self.workspace_root / "undeclared-broken"
        (sibling / ".git").mkdir(parents=True)
        unavailable = {
            "git": True,
            "unavailable": {"reason": "git-command-failed"},
        }
        with mock.patch.object(
            repository_evidence,
            "capture_repository_fingerprint",
            side_effect=[unavailable, {"git": True}],
        ):
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        findings = [
            finding
            for finding in report.findings
            if "evidence is unavailable" in finding.message
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertEqual("repository-root", findings[0].category)
        self.assertIn("SVC", findings[0].message)
        self.assertEqual(
            [],
            [
                (finding.category, finding.severity, finding.message)
                for finding in report.findings
                if "undeclared-broken"
                in f"{finding.message}\n{finding.path}\n{finding.detail}"
            ],
        )

    def test_commit_mode_requires_every_registered_tree_clean(self) -> None:
        (self.repositories["OAK"] / "dirty.txt").write_text(
            "dirty\n",
            encoding="utf-8",
        )

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
            commit_mode="per-session",
        )

        dirty = [
            finding
            for finding in report.findings
            if finding.category == "env-git" and "OAK" in finding.message
        ]
        self.assertEqual(1, len(dirty))
        self.assertEqual("yellow", dirty[0].severity)

    def test_baseline_staleness_uses_all_registered_repositories(self) -> None:
        self.contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", '
            '"name": "baseline", "repo": "SVC", '
            '"baseline_expectation": "pass"}'
        )
        self._write_plan()
        baseline.run_baseline(self.plan_dir, self.workspace_root)

        fresh = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )
        self.assertNotIn(
            "baseline-stale",
            {finding.category for finding in fresh.findings},
        )

        (self.repositories["OAK"] / "changed.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )
        stale = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        self.assertIn(
            "baseline-stale",
            {finding.category for finding in stale.findings},
        )

    def test_registered_baseline_fingerprint_is_captured_once_per_preflight(
        self,
    ) -> None:
        self.contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", '
            '"name": "svc-baseline", "repo": "SVC", '
            '"baseline_expectation": "pass"}, '
            '{"type": "file_exists", "path": "README.md", '
            '"name": "oak-baseline", "repo": "OAK", '
            '"baseline_expectation": "pass"}'
        )
        self._write_plan()
        baseline.run_baseline(self.plan_dir, self.workspace_root)
        original = baseline.capture_baseline_repository_evidence

        with mock.patch.object(
            baseline,
            "capture_baseline_repository_evidence",
            wraps=original,
        ) as capture:
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        self.assertNotIn(
            "baseline-stale",
            {finding.category for finding in report.findings},
        )
        capture.assert_called_once()

    def test_invalid_scope_has_repository_scope_category(self) -> None:
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "**Writes repositories**: SVC, OAK",
                "**Writes repositories**: Unknown",
            ),
            encoding="utf-8",
        )

        report = preflight.run_core_preflight(self.plan_dir)

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-scope"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("1", findings[0].session_id)
        self.assertEqual(str(session.resolve()), findings[0].path)

    def test_unknown_contract_alias_has_repository_contract_category(self) -> None:
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                '"repo": "SVC"',
                '"repo": "Unknown"',
            ),
            encoding="utf-8",
        )

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertIn(
            "repository-contract",
            {finding.category for finding in report.findings},
        )

    def test_registered_json_typo_is_a_malformed_contract_at_its_line(
        self,
    ) -> None:
        # A plain JSON syntax error is not a repository defect just because the
        # plan is registered, and its line is the session file's, not the
        # fenced block's.
        session = self.plan_dir / "session-1.md"
        lines = session.read_text(encoding="utf-8").splitlines(keepends=True)
        opener = next(
            index for index, line in enumerate(lines) if line.strip() == "```json"
        )
        lines.insert(opener + 1, "[,\n")
        session.write_text("".join(lines), encoding="utf-8")

        report = preflight.run_core_preflight(self.plan_dir)

        findings = [
            finding
            for finding in report.findings
            if finding.category in {"malformed-contract", "repository-contract"}
        ]
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("malformed-contract", finding.category)
        self.assertEqual(str(session.resolve()), finding.path)
        self.assertEqual(opener + 2, finding.line)
        self.assertIn(f"at line {opener + 2} column", finding.message)

    def test_registered_payload_has_ordered_repository_summaries(self) -> None:
        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        payload = preflight_output._preflight_payload_fields(
            report,
            plan_dir_arg=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness=None,
            commit_mode=None,
            state_dir=None,
            max_pending_sessions=None,
            mark_loop_ready=False,
        )

        self.assertEqual(str(self.workspace_root.resolve()), payload["workspace_root"])
        self.assertEqual(
            ["SVC", "OAK"],
            [repository["alias"] for repository in payload["repositories"]],
        )
        self.assertEqual(
            self.specs[0][1],
            payload["repositories"][0]["path"],
        )
