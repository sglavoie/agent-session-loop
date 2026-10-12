"""Repair child scope and cross-session artifact reference tests."""

import unittest

from agent_session_loop import (
    plans,
    review,
    review_repair,
)
from tests.review_materialization_support import MaterializationFixtures


class ReviewRepairChildScopeTests(MaterializationFixtures, unittest.TestCase):
    """Scope repair children and refuse foreign session artifacts."""

    def test_dry_planning_is_complete_and_performs_no_writes(self) -> None:
        findings = [
            self._finding("F1", "runner"),
            self._finding("F2", "runner", severity="med"),
            self._finding("F3", "workbench", severity="low"),
        ]
        self._write_plan(findings)
        before = self._plan_bytes()

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, repair_plan.reason)
        self.assertEqual("2", repair_plan.review_session_id.value)
        self.assertEqual(
            tuple(review.finding_fingerprint(finding) for finding in findings),
            repair_plan.finding_fingerprints,
        )
        self.assertEqual(
            ["runner", "workbench"],
            [group.repair_group for group in repair_plan.repair_groups],
        )
        self.assertEqual(
            ["2.1", "2.2"],
            [session_id.value for session_id in repair_plan.proposed_child_ids],
        )
        self.assertEqual(("F1", "F2"), repair_plan.repair_groups[0].finding_ids)
        self.assertIn(
            "## Completion Contract",
            repair_plan.repair_groups[0].rendered_text,
        )
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self.state_dir.exists())
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_child_scope_states_runner_owned_verification_and_repeat_policy(
        self,
    ) -> None:
        """A repair child gets the packet's verification policy, not its opposite."""
        self._write_plan([self._finding("F1", "runner")])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        rendered = repair_plan.repair_groups[0].rendered_text
        self.assertIn("## Child scope", rendered)
        # "Leave the checks passing" no longer stands alone as a bullet an agent
        # can read as "rerun every check yourself".
        self.assertNotIn(
            "- Leave every targeted check in the completion contract passing.\n",
            rendered,
        )
        self.assertIn("do not rerun the contract solely as a final gate", rendered)
        self.assertIn("ASL runs it authoritatively after this session", rendered)
        self.assertIn("restores the session to pending on failure", rendered)
        # The same three justified-repeat cases the task packet names.
        self.assertIn(
            "Running a check again, or running a broader one, is justified", rendered
        )
        self.assertIn("invalidated it", rendered)
        self.assertIn("a failure needs diagnosis", rendered)
        self.assertIn(
            "concrete correctness concern none of your checks covered", rendered
        )
        self.assertIn(
            "Nothing here caps how many checks you run or how long they take",
            rendered,
        )
        self.assertIn("Record the brief reason in this session's", rendered)
        # The existing scope and commit rules survive.
        self.assertIn("Fix only the issues in the findings above", rendered)
        self.assertIn("Address each demonstrated failure across the workflow", rendered)
        self.assertIn("do not add unrelated scope", rendered)
        self.assertIn("Do not commit the repair yourself", rendered)

    def test_child_scope_defers_another_groups_known_failure_to_that_group(
        self,
    ) -> None:
        self._write_plan(
            [self._finding("F1", "runner"), self._finding("F2", "workbench")]
        )

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        for group in repair_plan.repair_groups:
            with self.subTest(group=group.repair_group):
                rendered = group.rendered_text
                self.assertIn(
                    "attributes a failure to another repair group's findings",
                    rendered,
                )
                self.assertIn(
                    "treat that attribution as the explanation and leave the "
                    "failure to that group",
                    rendered,
                )
                self.assertIn(
                    "only when what you observe contradicts the supplied evidence",
                    rendered,
                )

    def test_evidence_citing_another_session_artifact_is_a_read_only_reference(
        self,
    ) -> None:
        # Evidence explains why the work is owed; it is not a write target, so
        # the group materializes and the citation is labelled read-only
        # (ASI-162).
        artifact = self._plan_artifact_path("session-1.md")
        finding = self._finding(
            "F1",
            "runner",
            evidence=[{"path": artifact, "symbol": "completion check"}],
        )
        self._write_plan([finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertIn(
            f"- `{artifact}` — `completion check` — read-only reference (do not edit)",
            repair_plan.repair_groups[0].rendered_text,
        )

    def test_f3_shaped_finding_citing_a_session_packet_materializes(self) -> None:
        # The ASI-162 finding: real work in the repository, with the session
        # packet cited only as the goal the work was owed against.
        artifact = self._plan_artifact_path("session-1.md")
        finding = self._finding(
            "F3",
            "install-pre-push-hook",
            severity="med",
            evidence=[
                {"path": "justfile", "symbol": "install-hooks (lines 137-140)"},
                {"path": "scripts/hooks/pre-push"},
                {"path": artifact, "symbol": "Goal / Handoff"},
            ],
            done_criteria=[
                "Install the existing hook for this repository so the "
                "effective core.hooksPath is scripts/hooks.",
                "Confirm the resolved scripts/hooks/pre-push exists, is "
                "executable, and has valid shell syntax.",
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": "pre-push-hook-is-installed",
                    "cwd": ".",
                    "argv": [
                        "python3",
                        "-c",
                        "import subprocess\nsubprocess.check_output(['git', "
                        "'config', '--get', 'core.hooksPath'])\n",
                    ],
                }
            ],
        )
        self._write_plan([finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        rendered = repair_plan.repair_groups[0].rendered_text
        self.assertIn(
            f"- `{artifact}` — `Goal / Handoff` — read-only reference (do not edit)",
            rendered,
        )
        self.assertIn("- `justfile` — `install-hooks (lines 137-140)`\n", rendered)
        self.assertIn("- `scripts/hooks/pre-push`\n", rendered)

    def test_done_criteria_demanding_another_session_artifact_is_refused(
        self,
    ) -> None:
        artifact = self._plan_artifact_path("session-1.md")
        finding = self._finding(
            "F1",
            "runner",
            done_criteria=[f"{artifact} records the corrected completion check."],
        )
        self._write_plan([finding])
        before = self._plan_bytes()

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_MALFORMED, repair_plan.reason
        )
        self.assertIn("session-1.md", repair_plan.error)
        self.assertIn("only rewrite its own session file", repair_plan.error)
        self.assertEqual(before, self._plan_bytes())

    def test_a_check_reading_another_session_artifact_is_refused(self) -> None:
        artifact = self._plan_artifact_path("session-1.md")
        finding = self._finding(
            "F1",
            "runner",
            verification_checks=[
                {
                    "type": "command",
                    "name": "verify-F1",
                    "argv": [
                        "python3",
                        "-c",
                        "from pathlib import Path\n"
                        f"raise SystemExit(0 if 'x' in Path({artifact!r})"
                        ".read_text() else 1)",
                    ],
                }
            ],
        )
        self._write_plan([finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_MALFORMED, repair_plan.reason
        )
        self.assertIn("session-1.md", repair_plan.error)

    def test_a_findings_reference_to_the_childs_own_artifact_is_allowed(self) -> None:
        finding = self._finding(
            "F1",
            "runner",
            evidence=[{"path": self._plan_artifact_path("session-2.1.md")}],
        )
        self._write_plan([finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertEqual(
            ["2.1"],
            [session_id.value for session_id in repair_plan.proposed_child_ids],
        )

    def test_a_repository_file_sharing_a_session_artifact_name_is_allowed(self) -> None:
        finding = self._finding(
            "F1",
            "runner",
            evidence=[{"path": "docs/session-1.md"}],
            verification_checks=[
                {
                    "type": "command",
                    "name": "verify-F1",
                    "argv": ["cat", "docs/session-1.md"],
                }
            ],
        )
        self._write_plan([finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)

    def test_registered_groups_read_all_repositories_and_write_finding_aliases(
        self,
    ) -> None:
        findings = [
            self._make_registered_finding("F1", "shared", "OAK"),
            self._make_registered_finding("F2", "shared", "SVC"),
            self._make_registered_finding("F3", "svc-only", "SVC"),
        ]
        self._register_plan(findings)

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        shared, svc_only = repair_plan.repair_groups
        self.assertEqual(("SVC", "OAK"), shared.read_repositories)
        self.assertEqual(("SVC", "OAK"), shared.write_repositories)
        self.assertEqual(("SVC",), svc_only.write_repositories)
        self.assertIn("**Reads repositories**: SVC, OAK", shared.rendered_text)
        self.assertIn("**Writes repositories**: SVC, OAK", shared.rendered_text)
        self.assertIn("**Writes repositories**: SVC", svc_only.rendered_text)
        self.assertIn(
            review_repair.review_repair_session_version_marker(
                review_repair.REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION
            ),
            shared.rendered_text,
        )
        self.assertEqual(
            ["OAK", "SVC"],
            [check["repo"] for check in shared.checks],
        )

        result = self._materialize()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(("SVC", "OAK"), result.children[0].read_repositories)
        self.assertEqual(("SVC", "OAK"), result.children[0].write_repositories)
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        child_scope = plans.parse_session_scope(
            result.children[1].path.read_text(encoding="utf-8"), registry.aliases
        )
        self.assertEqual(plans.SessionScope(("SVC", "OAK"), ("SVC",)), child_scope)

    def test_registered_group_writes_union_evidence_and_check_repositories(
        self,
    ) -> None:
        # Shape of F002 in run 20260820T182939015261Z-25011: attributed to a
        # single alias while its evidence and checks span two.
        widened_finding = self._cross_repo_finding(
            "F1",
            "manifest-contract",
            "OAK",
            evidence_repos=("OAK", "SVC"),
            check_repos=("OAK", "SVC"),
        )
        narrow_finding = self._make_registered_finding("F2", "svc-only", "SVC")
        self._register_plan([widened_finding, narrow_finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        widened, narrow = repair_plan.repair_groups
        self.assertEqual(("SVC", "OAK"), widened.write_repositories)
        self.assertEqual(
            {"SVC": "evidence", "OAK": "repo"},
            widened.write_repository_sources,
        )
        self.assertIn("**Writes repositories**: SVC, OAK", widened.rendered_text)
        self.assertIn(
            "- Write scope: `SVC` (from finding evidence paths), "
            "`OAK` (from finding `repo`).",
            widened.rendered_text,
        )
        self.assertEqual(("SVC",), narrow.write_repositories)
        self.assertEqual({"SVC": "repo"}, narrow.write_repository_sources)
        self.assertIn(
            "- Write scope: `SVC` (from finding `repo`).",
            narrow.rendered_text,
        )

        result = self._materialize()

        self.assertTrue(result.ok, result.error)
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        self.assertEqual(
            plans.SessionScope(("SVC", "OAK"), ("SVC", "OAK")),
            plans.parse_session_scope(
                result.children[0].path.read_text(encoding="utf-8"), registry.aliases
            ),
        )

    def test_registered_group_write_scope_records_check_only_attribution(
        self,
    ) -> None:
        finding = self._cross_repo_finding(
            "F1",
            "shared",
            "SVC",
            evidence_repos=("SVC",),
            check_repos=("SVC", "OAK"),
        )
        self._register_plan([finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        (group,) = repair_plan.repair_groups
        self.assertEqual(("SVC", "OAK"), group.write_repositories)
        self.assertEqual(
            {"SVC": "repo", "OAK": "check"},
            group.write_repository_sources,
        )
        self.assertIn(
            "- Write scope: `SVC` (from finding `repo`), "
            "`OAK` (from finding verification checks).",
            group.rendered_text,
        )


if __name__ == "__main__":
    unittest.main()
