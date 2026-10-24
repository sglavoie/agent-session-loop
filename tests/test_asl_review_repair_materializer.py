"""Repair materializer staging, merging and manifest tests."""

import json
import unittest
from unittest import mock

from agent_session_loop import (
    authoring_render,
    contracts,
    plans_errors,
    plans_handoff,
    plans_models,
    review,
    review_repair,
    review_repair_planning,
    review_repair_render,
    workspaces,
)
from tests.review_materialization_support import MaterializationFixtures
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
)

ASL_TEST_LANE = "integration"


class ReviewRepairMaterializerTests(MaterializationFixtures, unittest.TestCase):
    """Stage and merge repair children from review findings."""

    def test_revised_grouping_changes_json_digest_not_fingerprints(self) -> None:
        original = self._finding("F1", "runner")
        self._write_plan([original])
        first = review_repair_planning.plan_review_repair_sessions(self.plan_dir)
        revised = self._finding(
            "F1",
            "workbench",
            done_criteria=["F1 is fixed through a revised criterion."],
            verification_checks=[
                {
                    "type": "command",
                    "name": "verify-revised-F1",
                    "argv": ["python3", "-c", "print('revised')"],
                }
            ],
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [revised]}),
            encoding="utf-8",
        )

        second = review_repair_planning.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(first.finding_fingerprints, second.finding_fingerprints)
        self.assertEqual(first.review_output_sha256, second.review_output_sha256)
        self.assertNotEqual(first.review_artifact_sha256, second.review_artifact_sha256)
        self.assertEqual("runner", first.repair_groups[0].repair_group)
        self.assertEqual("workbench", second.repair_groups[0].repair_group)

    def test_materializer_stages_prevalidated_planner_output(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        repair_plan = review_repair_planning.plan_review_repair_sessions(self.plan_dir)
        self.assertTrue(repair_plan.ok, repair_plan.error)

        with (
            mock.patch.object(
                review_repair_planning,
                "plan_review_repair_sessions",
                return_value=repair_plan,
            ),
            mock.patch.object(
                authoring_render,
                "render_session_file",
                side_effect=AssertionError("writer re-rendered a planned child"),
            ),
        ):
            result = self._materialize()

        self.assertTrue(result.ok, result.error)
        child_text = result.children[0].path.read_text(encoding="utf-8")
        self.assertEqual(repair_plan.repair_groups[0].rendered_text, child_text)

    def test_materializes_one_child_per_repair_group(self) -> None:
        self._write_plan(
            [
                self._finding("F1", "runner"),
                self._finding("F2", "runner", severity="med"),
                self._finding("F3", "workbench", severity="low"),
            ]
        )
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, result.reason)
        self.assertEqual("2", result.review_session_id.value)
        self.assertEqual(
            ["2.1", "2.2"],
            [child.session_id.value for child in result.children],
        )
        self.assertEqual(
            ["runner", "workbench"],
            [child.repair_group for child in result.children],
        )
        self.assertEqual(("F1", "F2"), result.children[0].finding_ids)
        self.assertEqual(("F3",), result.children[1].finding_ids)
        for child in result.children:
            self.assertTrue(child.path.is_file())
            status = plans_handoff.read_session_status(
                plans_models.SessionFile(child.session_id, child.path)
            )
            self.assertEqual("pending", status.status)

    def test_child_contract_merges_checks_and_done_criteria(self) -> None:
        self._write_plan(
            [
                self._finding("F1", "runner"),
                self._finding("F2", "runner", severity="med"),
            ]
        )
        self._materialize()

        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        self.assertIsNotNone(contract)
        self.assertEqual(
            ["verify-F1"],
            [check.name for check in contract.verify.checks],
        )
        self.assertEqual(
            ("F1 is fixed and tested.", "F2 is fixed and tested."),
            contract.done_when,
        )

    def test_merged_diagnostic_retargets_deduplicated_gate_name(self) -> None:
        gate_one = {
            "type": "command",
            "name": "gate-one",
            "argv": ["python3", "-c", "pass"],
        }
        gate_two = {
            "type": "command",
            "name": "gate-two",
            "argv": ["python3", "-c", "pass"],
        }
        diagnostic = {
            "type": "file_exists",
            "name": "focused-evidence",
            "path": "focused.txt",
            "on_failure_of": ["gate-two"],
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[gate_one]),
                self._finding(
                    "F2",
                    "runner",
                    verification_checks=[gate_two, diagnostic],
                ),
            ]
        )

        result = self._materialize()

        self.assertTrue(result.ok, result.detail)
        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        self.assertIsNotNone(contract)
        self.assertEqual(
            ["gate-one", "focused-evidence"],
            [check.name for check in contract.verify.checks],
        )
        self.assertEqual(("gate-one",), contract.verify.checks[1].on_failure_of)

    def test_duplicate_commands_with_conflicting_settings_pause_malformed(
        self,
    ) -> None:
        first = {
            "type": "command",
            "name": "first",
            "argv": ["python3", "-c", "pass"],
            "timeout_seconds": 30,
        }
        second = {
            "type": "command",
            "name": "second",
            "argv": ["python3", "-c", "pass"],
            "timeout_seconds": 60,
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[first]),
                self._finding("F2", "runner", verification_checks=[second]),
            ]
        )

        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("conflicting settings", result.error)

    def test_duplicate_commands_with_conflicting_review_replay_fail_closed(
        self,
    ) -> None:
        first = {
            "type": "command",
            "name": "first",
            "argv": ["python3", "-c", "pass"],
            "review_replay": True,
        }
        second = {
            "type": "command",
            "name": "second",
            "argv": ["python3", "-c", "pass"],
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[first]),
                self._finding("F2", "runner", verification_checks=[second]),
            ]
        )

        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("conflicting settings", result.error)

    def test_merge_keeps_identical_commands_for_distinct_repositories(self) -> None:
        findings = [
            {
                "id": "F1",
                "verification_checks": [
                    {
                        "type": "command",
                        "name": "svc-check",
                        "repo": "SVC",
                        "argv": ["python3", "-m", "unittest"],
                    }
                ],
            },
            {
                "id": "F2",
                "verification_checks": [
                    {
                        "type": "command",
                        "name": "oak-check",
                        "repo": "OAK",
                        "argv": ["python3", "-m", "unittest"],
                    }
                ],
            },
        ]

        registry = workspaces.RepositoryRegistry(
            tuple(
                workspaces.Repository(alias, path, origin)
                for alias, path, origin in DEFAULT_MULTI_REPO_SPECS[:2]
            )
        )
        merged = review_repair_render._merge_verification_checks(
            "runner", findings, repository_registry=registry
        )

        self.assertEqual(["SVC", "OAK"], [check["repo"] for check in merged])
        self.assertEqual(2, len(merged))

    def test_merge_refuses_to_carry_an_absence_gate_into_a_repair_child(self) -> None:
        findings = [
            {
                "id": "F1",
                "verification_checks": [
                    {
                        "type": "regex_in_file",
                        "name": "drops-the-claim",
                        "path": "docs/evidence.md",
                        "pattern": "75-pixel",
                        "expect_absent": True,
                    }
                ],
            }
        ]

        with self.assertRaisesRegex(plans_errors.PlanError, "no absence semantics"):
            review_repair_render._merge_verification_checks("runner", findings)

    def test_no_finding_is_omitted_or_assigned_twice(self) -> None:
        findings = [
            self._finding("F1", "a"),
            self._finding("F2", "b"),
            self._finding("F3", "a"),
            self._finding("F4", "c"),
        ]
        self._write_plan(findings)
        result = self._materialize()

        assigned = [fid for child in result.children for fid in child.finding_ids]
        self.assertEqual(sorted(assigned), ["F1", "F2", "F3", "F4"])
        self.assertEqual(len(assigned), len(set(assigned)))
        # First-appearance group ordering: a, b, c.
        self.assertEqual(
            ["a", "b", "c"],
            [child.repair_group for child in result.children],
        )
        self.assertEqual(("F1", "F3"), result.children[0].finding_ids)

    def test_children_carry_review_repair_marker(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        result = self._materialize()

        body = result.children[0].path.read_text(encoding="utf-8")
        self.assertTrue(review_repair.contains_review_repair_session_marker(body))
        self.assertEqual(1, review_repair.review_repair_session_marker_version(body))
        self.assertTrue(
            review_repair.is_generated_review_repair_session_file(
                result.children[0].path
            )
        )
        # A repair child must not masquerade as a generated review session.
        self.assertFalse(review.contains_review_session_marker(body))

    def test_children_chain_previous_session_references(self) -> None:
        self._write_plan([self._finding("F1", "a"), self._finding("F2", "b")])
        result = self._materialize()

        first = result.children[0].path.read_text(encoding="utf-8")
        second = result.children[1].path.read_text(encoding="utf-8")
        self.assertIn("`session-2.md`", first)
        self.assertIn("`session-2.1.md`", second)

    def test_manifest_table_and_count_refreshed(self) -> None:
        self._write_plan([self._finding("F1", "a"), self._finding("F2", "b")])
        self._materialize()

        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("| 2.1 | Review repair: a | pending | session-2.1.md |", manifest)
        self.assertIn("| 2.2 | Review repair: b | pending | session-2.2.md |", manifest)
        self.assertIn("**Sessions**: 4", manifest)

    def test_missing_manifest_table_still_materializes(self) -> None:
        self._write_plan([self._finding("F1", "a")], with_manifest_table=False)
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertTrue(result.children[0].path.is_file())
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Sessions**: 3", manifest)

    def test_auto_detects_generated_review_session(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        result = self._materialize()
        self.assertEqual("2", result.review_session_id.value)

    def test_explicit_review_session_id(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        result = self._materialize(review_session_id="2")
        self.assertTrue(result.ok)
        self.assertEqual("2", result.review_session_id.value)

    def test_over_eight_groups_pauses_malformed(self) -> None:
        findings = [self._finding(f"F{i}", f"g{i}") for i in range(9)]
        self._write_plan(findings)
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("exceed the maximum of 8", result.error)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_existing_children_pause_malformed(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        first = self._materialize()
        self.assertTrue(first.ok)
        original = first.children[0].path.read_text(encoding="utf-8")

        second = self._materialize()
        self.assertFalse(second.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, second.reason)
        self.assertIn("already exist", second.error)
        # Existing child left untouched.
        self.assertEqual(original, first.children[0].path.read_text(encoding="utf-8"))

    def test_missing_artifact_pauses_malformed(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        (self.plan_dir / "final-review.json").unlink()
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, result.detail)

    def test_conflicting_merged_check_names_pause_malformed(self) -> None:
        clash = {
            "type": "command",
            "name": "shared",
            "argv": ["python3", "-c", "pass"],
        }
        clash_other = {
            "type": "command",
            "name": "shared",
            "argv": ["python3", "-c", "print(1)"],
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[clash]),
                self._finding("F2", "runner", verification_checks=[clash_other]),
            ]
        )
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("conflicting", result.error)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_all_clear_is_noop(self) -> None:
        self._write_plan([], outcome="all-clear")
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS, result.reason)
        self.assertEqual((), result.children)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))


if __name__ == "__main__":
    unittest.main()
