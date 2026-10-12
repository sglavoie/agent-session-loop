"""Repair materialization across registered repositories and dry planning."""

import unittest

from agent_session_loop import (
    contracts,
    events,
    plans,
    review,
    review_continuation,
    review_cycle,
    review_delta,
    review_repair,
)
from tests.review_materialization_support import MaterializationFixtures


class ReviewRepairRegisteredRepositoryTests(MaterializationFixtures, unittest.TestCase):
    """Materialize repairs over registered repositories and dry-plan groups."""

    def test_write_repository_sources_helper_is_bounded_by_group_findings(
        self,
    ) -> None:
        self._register_plan([self._make_registered_finding("F1", "svc-only", "SVC")])
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        findings = [
            self._cross_repo_finding(
                "F1",
                "shared",
                "OAK",
                evidence_repos=("OAK",),
                check_repos=("OAK",),
            ),
            self._cross_repo_finding(
                "F2",
                "shared",
                "SVC",
                evidence_repos=("SVC",),
                check_repos=("SVC",),
            ),
        ]

        self.assertEqual(
            {"OAK": "repo"},
            review_repair.repair_group_write_repository_sources(
                "shared", findings[:1], repository_registry=registry
            ),
        )
        self.assertEqual(
            ["SVC", "OAK"],
            list(
                review_repair.repair_group_write_repository_sources(
                    "shared", findings, repository_registry=registry
                )
            ),
        )

    def test_registered_repair_fails_closed_for_unattributed_legacy_finding(
        self,
    ) -> None:
        finding = self._finding("F1", "runner")
        self._register_plan([finding], artifact_version=review.REVIEW_ARTIFACT_VERSION)
        before = self._plan_bytes()

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("without an explicit repository alias", repair_plan.error)
        self.assertEqual(before, self._plan_bytes())

    def test_registered_repair_qualifies_same_check_name_across_aliases(self) -> None:
        first = self._make_registered_finding("F1", "shared", "SVC")
        second = self._make_registered_finding("F2", "shared", "OAK")
        first["verification_checks"][0]["name"] = "shared-check"
        second["verification_checks"][0]["name"] = "shared-check"
        self._register_plan([first, second])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertEqual(
            ["SVC:shared-check", "OAK:shared-check"],
            [check["name"] for check in repair_plan.repair_groups[0].checks],
        )
        result = self._materialize()
        self.assertTrue(result.ok, result.error)
        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        assert contract is not None
        self.assertEqual(
            ["SVC:shared-check", "OAK:shared-check"],
            [check.name for check in contract.verify.checks],
        )

    def test_registered_repair_avoids_literal_qualified_name_collision(self) -> None:
        first = self._make_registered_finding("F1", "shared", "SVC")
        second = self._make_registered_finding("F2", "shared", "OAK")
        literal = self._make_registered_finding("F3", "shared", "SVC")
        first["verification_checks"][0].update(
            name="shared-check", argv=["python3", "-c", "pass # generated"]
        )
        second["verification_checks"][0]["name"] = "shared-check"
        literal["verification_checks"][0].update(
            name="SVC:shared-check",
            argv=["python3", "-c", "pass # literal"],
            on_failure_of=["shared-check"],
        )
        self._register_plan([first, second, literal])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        expected_names = [
            "SVC:shared-check#2",
            "OAK:shared-check",
            "SVC:shared-check",
        ]
        self.assertEqual(
            expected_names,
            [check["name"] for check in repair_plan.repair_groups[0].checks],
        )
        result = self._materialize()
        self.assertTrue(result.ok, result.error)
        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        assert contract is not None
        self.assertEqual(
            expected_names,
            [check.name for check in contract.verify.checks],
        )
        self.assertEqual(
            ("SVC:shared-check#2",),
            contract.verify.checks[2].on_failure_of,
        )

    def test_registered_repair_rejects_same_repo_check_name_conflict(self) -> None:
        first = self._make_registered_finding("F1", "shared", "SVC")
        second = self._make_registered_finding("F2", "shared", "SVC")
        first["verification_checks"][0]["name"] = "shared-check"
        second["verification_checks"][0]["name"] = "shared-check"
        second["verification_checks"][0]["argv"] = [
            "python3",
            "-c",
            "raise SystemExit(1)",
        ]
        self._register_plan([first, second])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("conflicting definitions", repair_plan.error)

    def test_registered_eventless_recovery_reads_bases_and_scopes_from_sessions(
        self,
    ) -> None:
        finding = self._make_registered_finding("F1", "runner", "SVC")
        self._register_plan([finding])
        review_path = self.plan_dir / "session-2.md"
        review_text = review_path.read_text(encoding="utf-8").replace(
            review.review_session_version_marker(),
            review.review_session_version_marker(
                review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
            ),
        )
        base_section = (
            "## Ordered repository review base\n\n"
            "- `SVC`: path `tundra_svc`; origin `github.com/tundra/SVC`; "
            "HEAD `1111111111111111111111111111111111111111`.\n"
            "  - Pre-existing dirty paths: (none)\n"
            "- `OAK`: path `tundra_OAK`; origin `github.com/tundra/OAK`; "
            "HEAD `2222222222222222222222222222222222222222`.\n"
            "  - Pre-existing dirty paths:\n"
            "    - `OAK:staged.txt`\n\n"
        )
        review_path.write_text(
            review_text.replace(
                "## Instructions\n", base_section + "## Instructions\n"
            ),
            encoding="utf-8",
        )

        planned = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(planned.ok, planned.error)
        self.assertEqual(
            ["SVC", "OAK"],
            [row["alias"] for row in planned.repository_bases],
        )
        self.assertEqual(["OAK:staged.txt"], planned.repository_bases[1]["dirty_paths"])
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            self.tmp_path / "missing-events.jsonl",
            max_review_fix_cycles=1,
        )

        self.assertEqual("run-repairs", resume.action)
        self.assertEqual(
            ["SVC", "OAK"],
            [row["alias"] for row in resume.current_repositories],
        )
        assert resume.repair_groups is not None
        self.assertEqual(["SVC"], resume.repair_groups[0]["write_repositories"])
        payload = resume.to_payload()
        self.assertEqual("SVC", payload["current_findings"][0]["repo"])
        self.assertEqual(
            ["OAK:staged.txt"],
            payload["current_repositories"][1]["dirty_paths"],
        )
        event_path = self.tmp_path / "events.jsonl"
        reopen = review_cycle._review_reopen_record(
            event_path=event_path,
            payload={"run_id": "eventless"},
            resume=resume,
            max_review_fix_cycles=1,
            materialized=True,
            repair_session_ids=("2.1",),
            result=review_repair.REVIEW_REPAIR_REASON_OK,
            recovered=True,
        )
        events.append_event(
            event_path,
            events.Event(events.REVIEW_REPAIR_REOPENED_EVENT, reopen),
        )
        recovered = review_delta._last_repairs_materialized_cycle(event_path)
        assert recovered is not None
        self.assertEqual("SVC", recovered["findings"][0]["repo"])
        self.assertEqual(
            ["OAK:staged.txt"], recovered["repositories"][1]["dirty_paths"]
        )

    def test_narrowed_pending_repair_child_blocks_until_scope_is_regenerated(
        self,
    ) -> None:
        self._register_plan(
            [
                self._cross_repo_finding(
                    "F1",
                    "manifest-contract",
                    "OAK",
                    evidence_repos=("OAK", "SVC"),
                    check_repos=("OAK",),
                )
            ]
        )
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)
        child_path = materialized.children[0].path
        regenerated = child_path.read_text(encoding="utf-8")

        def resume() -> review_continuation.ReviewResume:
            return review_continuation.evaluate_review_continuation(
                plans.inspect_plan(self.plan_dir),
                self.tmp_path / "missing-events.jsonl",
                max_review_fix_cycles=1,
            )

        ready = resume()

        self.assertEqual("run-repairs", ready.action)
        self.assertTrue(ready.eligible)
        self.assertIsNone(ready.blocker)
        self.assertIsNone(ready.repair_scope_gaps)

        child_path.write_text(
            regenerated.replace(
                "**Writes repositories**: SVC, OAK",
                "**Writes repositories**: OAK",
            ),
            encoding="utf-8",
        )
        narrowed = resume()

        self.assertEqual("run-repairs", narrowed.action)
        self.assertFalse(narrowed.eligible)
        self.assertEqual("repair-scope-stale", narrowed.blocker)
        assert narrowed.repair_scope_gaps is not None
        (gap,) = narrowed.repair_scope_gaps
        self.assertEqual(
            {
                "repair_session_id": "2.1",
                "repair_group": "manifest-contract",
                "durable_write_repositories": ["OAK"],
                "required_write_repositories": ["SVC", "OAK"],
                "missing_write_repositories": ["SVC"],
                "write_repository_sources": {"SVC": "evidence", "OAK": "repo"},
            },
            dict(gap),
        )
        self.assertEqual(
            "widen **Writes repositories** in the pending repair children, "
            "then resume the same plan: 2.1 needs SVC",
            review_continuation.review_resume_guidance(
                narrowed.to_payload(), review_harness_active=True
            ),
        )

        child_path.write_text(regenerated, encoding="utf-8")
        cleared = resume()

        self.assertEqual("run-repairs", cleared.action)
        self.assertTrue(cleared.eligible)
        self.assertIsNone(cleared.blocker)
        self.assertIsNone(cleared.repair_scope_gaps)

    def test_dry_planning_accepts_exactly_eight_groups(self) -> None:
        self._write_plan([self._finding(f"F{i}", f"g{i}") for i in range(8)])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertEqual(8, len(repair_plan.repair_groups))
        self.assertEqual(
            [f"2.{index}" for index in range(1, 9)],
            [session_id.value for session_id in repair_plan.proposed_child_ids],
        )

    def test_dry_planning_rejects_nine_groups_without_writes(self) -> None:
        self._write_plan([self._finding(f"F{i}", f"g{i}") for i in range(9)])
        before = self._plan_bytes()

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_MALFORMED,
            repair_plan.reason,
        )
        self.assertIn("exceed the maximum of 8", repair_plan.error)
        self.assertEqual(9, len(repair_plan.proposed_child_ids))
        self.assertEqual(before, self._plan_bytes())

    def test_dry_planning_reports_conflicting_checks(self) -> None:
        self._write_plan(
            [
                self._finding(
                    "F1",
                    "runner",
                    verification_checks=[
                        {
                            "type": "command",
                            "name": "shared",
                            "argv": ["python3", "-c", "pass"],
                        }
                    ],
                ),
                self._finding(
                    "F2",
                    "runner",
                    verification_checks=[
                        {
                            "type": "command",
                            "name": "shared",
                            "argv": ["python3", "-c", "print(1)"],
                        }
                    ],
                ),
            ]
        )

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("conflicting", repair_plan.error)
        self.assertEqual(2, len(repair_plan.validated_findings))
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_dry_planning_reports_missing_and_inconsistent_artifacts(
        self,
    ) -> None:
        self._write_plan([self._finding("F1", "runner")])
        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_body = artifact_path.read_text(encoding="utf-8")
        artifact_path.unlink()

        missing = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(missing.ok)
        self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, missing.detail)
        self.assertIsNone(missing.review_artifact_sha256)
        artifact_path.write_text(artifact_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n",
            encoding="utf-8",
        )

        inconsistent = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(inconsistent.ok)
        self.assertEqual(
            review.REVIEW_REASON_CONTRADICTORY_ARTIFACT,
            inconsistent.detail,
        )
        self.assertIsNotNone(inconsistent.review_output_sha256)
        self.assertIsNotNone(inconsistent.review_artifact_sha256)

    def test_dry_planning_reports_existing_child_conflicts(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        materialized = self._materialize()
        self.assertTrue(materialized.ok)

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("already exist", repair_plan.error)
        self.assertEqual(
            ("2.1",),
            tuple(session_id.value for session_id in repair_plan.existing_child_ids),
        )
        self.assertEqual(
            ("2.1",),
            tuple(session_id.value for session_id in repair_plan.proposed_child_ids),
        )


if __name__ == "__main__":
    unittest.main()
