"""Preflight repository footprint and repository intent target tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans,
    preflight,
    preflight_auto_repair,
    preflight_cli,
    preflight_footprint,
    preflight_hint_resolution,
    preflight_models,
    preflight_output,
    preflight_repair_hints,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    _write_loop_ready_plan,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)
from tests.support_asl_preflight import RepositoryIntentFixtures


class RepositoryFootprintPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = self.repositories["SVC"]
        self.second = self.repositories["OAK"]
        for repository in (self.first, self.second):
            (repository / "README.md").write_text("x\n", encoding="utf-8")
        self.plan_dir = self.root / "plan"

    def _write_plan(self, *paths: Path) -> None:
        contract = _contract_block(
            ", ".join(
                json.dumps(
                    {"type": "file_exists", "path": str(path), "name": f"check-{index}"}
                )
                for index, path in enumerate(paths)
            ),
            session="1",
        )
        _write_loop_ready_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )

    def _footprint_findings(
        self, report: preflight_models.PreflightReport
    ) -> list[preflight_models.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "repository-footprint"
        ]

    def test_out_of_root_check_is_red_using_the_stored_plan_metadata(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._footprint_findings(report)
        self.assertEqual(["red", "red"], [finding.severity for finding in findings])
        self.assertIn("escapes the bound root", findings[0].message)
        self.assertIn("2 git repositories", findings[1].message)
        self.assertEqual(
            ["repository-footprint.out-of-root", "repository-footprint.multi-repo"],
            [
                preflight_hint_resolution.repair_hint_for_finding(f).check_id
                for f in findings
            ],
        )

    def test_absolute_check_inside_the_bound_root_stays_green(self) -> None:
        self._write_plan(self.first / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._footprint_findings(report))
        self.assertFalse(report.repository_footprint.registry_required)

    def test_non_git_target_outside_the_bound_root_stays_green(self) -> None:
        loose = self.root / "loose.txt"
        loose.write_text("x\n", encoding="utf-8")
        self._write_plan(self.first / "README.md", loose)
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._footprint_findings(report))
        self.assertFalse(report.repository_footprint.registry_required)

    def test_without_plan_metadata_the_explicit_cwd_supplies_the_root(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")

        core_report = preflight.run_core_preflight(self.plan_dir)
        self.assertEqual([], self._footprint_findings(core_report))

        report = preflight.run_preflight(self.plan_dir, cwd=self.first)

        self.assertTrue(self._footprint_findings(report))

    def test_footprint_blocks_marking_the_plan_loop_ready(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        blockers = preflight_cli._mark_loop_ready_blockers(report)
        self.assertIn(
            "repository-footprint", {blocker.category for blocker in blockers}
        )

    def test_payload_carries_a_registry_table_derived_from_disk(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)

        payload = preflight_output._preflight_payload_fields(
            report,
            plan_dir_arg=str(self.plan_dir),
            cwd=None,
            harness=None,
            commit_mode=None,
            state_dir=None,
            max_pending_sessions=None,
            mark_loop_ready=False,
        )["repository_footprint"]

        self.assertEqual(str(self.workspace.resolve()), payload["workspace_root"])
        self.assertFalse(payload["registry_present"])
        self.assertTrue(payload["registry_required"])
        self.assertEqual(
            ["| Alias | Path | Origin |", "| --- | --- | --- |"],
            payload["suggested_registry_table"][:2],
        )
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(entry["alias"] for entry in payload["detected"]),
        )
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(payload["suggested_scopes"]["1"]["writes"]),
        )

    def test_registered_plans_are_governed_by_the_registry_instead(self) -> None:
        write_multi_repo_plan(
            self.plan_dir, ("pending",), specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._footprint_findings(report))
        self.assertIsNone(report.repository_footprint)

    def test_footprint_blockers_are_cleared_by_the_registry_steps(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)
        self.assertEqual(2, len(self._footprint_findings(report)))

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(
            ["register-repositories", "prepare"],
            [step["argv"][1] for step in steps],
        )
        self.assertIsNone(
            preflight_auto_repair.build_suggested_pause(
                report, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )
        )


class RepositoryIntentTargetTests(RepositoryIntentFixtures):
    def test_target_a_session_has_yet_to_create_still_names_its_repository(
        self,
    ) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertIn("2 git repositories", findings[0].message)
        self.assertIn("`## Repositories`", findings[0].message)
        self.assertEqual(
            "repository-registry-required.declare",
            preflight_hint_resolution.repair_hint_for_finding(findings[0]).check_id,
        )
        # The footprint check cannot see a path that does not exist yet, which
        # is exactly the gap the intent check closes.
        self.assertFalse(report.repository_footprint.registry_required)

    def test_repair_guidance_names_the_migration_command(self) -> None:
        hint = preflight_repair_hints.REPAIR_HINTS["repository-registry-required"]
        self.assertIn(
            "asl register-repositories <plan-dir> --cwd <repo-root>",
            hint.message,
        )
        self.assertIn("`## Repositories` table", hint.message)
        self.assertIn("Reads repositories**:", hint.message)
        self.assertIn("Writes repositories**:", hint.message)
        self.assertIn("`repo` alias", hint.message)
        self.assertIn("asl register-repositories", hint.question_template)

    def test_every_repository_hint_names_the_migration_command(self) -> None:
        hints = (
            preflight_repair_hints.REPAIR_HINTS["repository-footprint"],
            preflight_repair_hints.REPAIR_HINTS["repository-registry-required"],
            preflight_hint_resolution._CATEGORY_HINT_OVERRIDES[
                "repository-footprint.multi-repo"
            ],
            preflight_hint_resolution._CATEGORY_HINT_OVERRIDES[
                "repository-footprint.out-of-root"
            ],
        )
        for hint in hints:
            with self.subTest(check_id=hint.check_id):
                self.assertIn("asl register-repositories", hint.message)
                self.assertIn("asl register-repositories", hint.question_template)
                # The hand-edit recipe is gone: no numbered step list survives.
                self.assertNotIn("(1) add", hint.message)

    def test_single_repository_legacy_plan_is_unchanged(self) -> None:
        self._write_plan(self.first / "README.md", self.first / "src" / "new.py")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertFalse(report.repository_intent.registry_required)

    def test_git_target_outside_non_git_bound_root_requires_registry(self) -> None:
        non_git_root = self.root / "operator-workspace"
        non_git_root.mkdir()
        self._write_plan(self.first / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(non_git_root))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertTrue(report.repository_intent.registry_required)
        self.assertIsNone(report.repository_intent.bound_git_root)
        self.assertEqual(
            (workspaces.canonicalize_filesystem_path(self.first),),
            report.repository_intent.git_roots,
        )

    def test_source_plan_in_another_repository_is_not_a_second_target(
        self,
    ) -> None:
        self._write_plan(self.first / "README.md", self.first / "src" / "new.py")
        manifest = self.plan_dir / "manifest.md"
        source = self.second / "plans" / "source.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"\n**Split from**: `{source}`\n",
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertFalse(report.repository_intent.registry_required)

    def test_source_plan_cited_in_session_prose_is_not_a_second_target(
        self,
    ) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(self.first / "README.md", self.first / "src" / "new.py")
        self._declare_source(str(source), cited_in_session=f"{source}:209")

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertEqual([], self._footprint_findings(report))
        self.assertFalse(report.repository_intent.registry_required)

    def test_sibling_of_the_source_plan_is_not_a_second_target(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        sibling = source.parent / "companion.md"
        sibling.write_text("# Companion\n", encoding="utf-8")
        self._write_plan(self.first / "README.md")
        self._declare_source(str(source), cited_in_session=str(sibling))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertEqual([], self._footprint_findings(report))

    def test_a_write_elsewhere_in_the_source_repository_still_needs_a_registry(
        self,
    ) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        self._declare_source(str(source), cited_in_session=str(source))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertIn("2 git repositories", findings[0].message)

    def test_a_relative_split_from_leaves_the_gate_intact(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(self.first / "README.md")
        self._declare_source("plans/source.md", cited_in_session=str(source))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertIn("2 git repositories", findings[0].message)

    def test_footprint_gate_does_not_count_the_cited_source_plan(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(self.first / "README.md")
        self._declare_source(str(source), cited_in_session=str(source))

        footprint, payload = preflight_footprint.plan_repository_footprint(
            self.plan_dir, self.first
        )

        self.assertEqual((), footprint.out_of_root)
        self.assertEqual(
            (workspaces.canonicalize_filesystem_path(self.first),),
            footprint.repo_roots,
        )
        # ``None`` is the payload's way of saying there is nothing to report.
        self.assertIsNone(payload)
