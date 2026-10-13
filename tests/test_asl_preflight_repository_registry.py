"""Preflight repository registry migration and repair tests."""

from __future__ import annotations

import dataclasses
import subprocess

from agent_session_loop import (
    plans_metadata,
    preflight,
    preflight_auto_repair,
    preflight_cli,
    preflight_models,
    preflight_output,
    repository_registration,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    write_multi_repo_plan,
)
from tests.support_asl_preflight import RepositoryIntentFixtures, _finding


class RepositoryIntentRegistryRepairTests(RepositoryIntentFixtures):
    def test_registered_multi_repository_plan_passes(self) -> None:
        write_multi_repo_plan(
            self.plan_dir, ("pending",), specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertIsNone(report.repository_intent)

    def test_explicit_cwd_supplies_the_root_without_plan_metadata(self) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )

        core_report = preflight.run_core_preflight(self.plan_dir)
        self.assertEqual([], self._intent_findings(core_report))

        report = preflight.run_preflight(self.plan_dir, cwd=self.first)

        self.assertTrue(self._intent_findings(report))

    def test_payload_reports_the_repositories_the_plan_intends_to_reach(
        self,
    ) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans_metadata.save_plan_metadata(self.plan_dir, str(self.first))
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
        )["repository_intent"]

        self.assertTrue(payload["registry_required"])
        self.assertEqual("repository-registry-required", payload["reason"])
        self.assertEqual(2, payload["git_root_count"])
        self.assertEqual(
            sorted(
                str(workspaces.canonicalize_filesystem_path(path))
                for path in (self.first, self.second)
            ),
            sorted(payload["git_roots"]),
        )

    def test_mark_loop_ready_cannot_silence_the_invariant(self) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans_metadata.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)

        blockers = preflight_cli._mark_loop_ready_blockers(report)
        self.assertIn(
            "repository-registry-required",
            {blocker.category for blocker in blockers},
        )

        marked, error = preflight_cli._mark_loop_ready(report)

        self.assertFalse(marked)
        self.assertIn("non-bypassable", error)
        self.assertIn("repository-registry-required", error)

    def test_auto_repair_migrates_the_registry_and_clears_the_pause(self) -> None:
        report = self._registry_required_report()

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        workspace = workspaces.canonicalize_filesystem_path(self.workspace)
        self.assertEqual(
            [
                [
                    "asl",
                    "register-repositories",
                    str(self.plan_dir),
                    "--cwd",
                    str(self.first),
                    "--json",
                ],
                [
                    "asl",
                    "prepare",
                    str(self.plan_dir),
                    "--resume",
                    "--rebind-workspace",
                    "--cwd",
                    str(workspace),
                ],
            ],
            [step["argv"] for step in steps],
        )
        for step in steps:
            self.assertEqual(1, step["max_attempts"])
            self.assertEqual("repository-registry-required", step["category"])
        self.assertIsNone(
            preflight_auto_repair.build_suggested_pause(
                report, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )
        )

    def test_registry_steps_forward_a_state_directory_override(self) -> None:
        report = self._registry_required_report()

        steps = preflight_auto_repair.build_auto_repair_plan(
            report,
            plan_dir=str(self.plan_dir),
            cwd=str(self.first),
            state_dir="/state/dir",
        )

        for step in steps:
            self.assertIn("--state-dir", step["argv"])
            self.assertEqual(
                "/state/dir",
                step["argv"][step["argv"].index("--state-dir") + 1],
            )

    def test_completed_handoff_blocks_automatic_registry_steps(self) -> None:
        """Preflight never advertises a migration the command would refuse."""
        self._write_plan(
            self.first / "README.md",
            self.second / "src" / "new_module.py",
            statuses=("complete", "pending"),
        )
        plans_metadata.save_plan_metadata(self.plan_dir, str(self.first))
        self._set_handoff("1", status="complete", files_modified="src/new.py")
        report = preflight.run_core_preflight(self.plan_dir)

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(
            [],
            [step for step in steps if "register-repositories" in step["argv"]],
        )
        pause = preflight_auto_repair.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])

        # The rule is the entry, not the status: a completed handoff that
        # modified nothing survives the registry, so the steps come back.
        self._set_handoff("1", status="complete", files_modified="(none)")
        report = preflight.run_core_preflight(self.plan_dir)

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(
            [
                "asl",
                "register-repositories",
                str(self.plan_dir),
                "--cwd",
                str(self.first),
                "--json",
            ],
            steps[0]["argv"],
        )
        self.assertIsNone(
            preflight_auto_repair.build_suggested_pause(
                report, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )
        )

    def test_missing_origin_keeps_registry_blocker_and_suggested_pause(
        self,
    ) -> None:
        """Topology alone cannot see this refusal; the derivation can."""
        subprocess.run(
            ["git", "remote", "remove", "origin"],
            cwd=self.second,
            check=True,
            capture_output=True,
        )
        report = self._registry_required_report()
        self.assertTrue(self._intent_findings(report))
        self.assertEqual(
            "repository-origin",
            repository_registration.derive_registration(
                self.plan_dir, cwd=self.first
            ).missing_fact,
        )

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual([], list(steps))
        pause = preflight_auto_repair.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])
        self.assertIn("repository-origin", pause["detail"])

    def test_unattributable_contract_keeps_registry_blocker_and_suggested_pause(
        self,
    ) -> None:
        """A check under no repository refuses, so neither step is offered."""
        self._write_plan(
            self.first / "README.md",
            self.second / "src" / "new_module.py",
            self.root / "outside" / "artifact.txt",
        )
        plans_metadata.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)
        self.assertTrue(self._intent_findings(report))
        self.assertEqual(
            "contract-path-outside-registry",
            repository_registration.derive_registration(
                self.plan_dir, cwd=self.first
            ).missing_fact,
        )

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual([], list(steps))
        pause = preflight_auto_repair.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])
        self.assertIn("contract-path-outside-registry", pause["detail"])

    def test_without_a_cwd_no_registry_step_is_emitted(self) -> None:
        report = self._registry_required_report()

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=None
        )

        self.assertEqual([], list(steps))
        pause = preflight_auto_repair.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=None
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])

    def test_an_active_lock_still_wins_over_the_registry_steps(self) -> None:
        report = self._registry_required_report()
        locked = dataclasses.replace(
            report,
            findings=report.findings
            + (_finding("red", "env-lock", "active lock held by pid 9 at /p/lock"),),
        )

        plan = preflight_auto_repair.build_auto_repair_plan(
            locked, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(1, len(plan))
        self.assertTrue(plan[0]["blocked"])
        self.assertEqual("lock-contention", plan[0]["pause_reason"])
        self.assertEqual(
            "lock-contention",
            preflight_auto_repair.build_suggested_pause(
                locked, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )["reason"],
        )

    def test_the_rebind_step_matches_the_command_the_migration_prints(
        self,
    ) -> None:
        # The two surfaces emit the same follow-up command, so neither can
        # drift into telling an operator to run something the other does not.
        report = self._registry_required_report()
        plan_dir = str(workspaces.canonicalize_filesystem_path(self.plan_dir))

        steps = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=plan_dir, cwd=str(self.first)
        )
        applied = repository_registration.register_repositories(
            self.plan_dir, cwd=self.first
        )

        self.assertEqual("applied", applied.status)
        self.assertEqual(applied.next_commands[0], steps[1]["command"])

    def test_category_is_not_bypassable(self) -> None:
        self.assertIn(
            "repository-registry-required",
            preflight_models.NON_BYPASSABLE_PREFLIGHT_CATEGORIES,
        )
