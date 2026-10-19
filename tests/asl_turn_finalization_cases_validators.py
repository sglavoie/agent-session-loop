from .asl_turn_finalization_support import *


class GeneratedReviewValidatorDiagnosticTests(unittest.TestCase):
    def run_checks(self, plan_dir: Path) -> dict[str, subprocess.CompletedProcess[str]]:
        payload = review_session_contract._generated_review_contract_payload(
            plans_session_ids.parse_session_id("2"), plan_dir=plan_dir
        )
        results: dict[str, subprocess.CompletedProcess[str]] = {}
        for check in payload["checks"]:
            argv = [
                sys.executable if part == "python3" else part for part in check["argv"]
            ]
            results[check["name"]] = subprocess.run(
                argv, cwd=plan_dir, text=True, capture_output=True, check=False
            )
        return results

    def diagnostic(
        self, completed: subprocess.CompletedProcess[str]
    ) -> dict[str, object]:
        self.assertEqual(1, completed.returncode)
        lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertTrue(lines, "validator failed without a diagnostic line")
        return json.loads(lines[-1])

    def test_valid_review_passes_both_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\n## Findings\n\n"
                "Read agent_session_loop/runner.py, agent_session_loop/plans.py "
                "and agent_session_loop/review.py.\n\n"
                f"{review.REVIEW_STATUS_ALL_CLEAR}\n",
                encoding="utf-8",
            )
            for name, completed in self.run_checks(plan_dir).items():
                self.assertEqual(0, completed.returncode, f"{name}: {completed.stderr}")

    def test_output_gate_reports_its_rejecting_predicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\nNo heading of interest.\n", encoding="utf-8"
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-output-evidence-based"]
            )
            self.assertEqual("final-review-output-evidence-based", payload["check"])
            self.assertEqual("missing-required-heading", payload["reason"])

    def test_output_gate_reports_thin_path_evidence_with_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\n## Findings\n\nSaw agent_session_loop/runner.py.\n\n"
                f"{review.REVIEW_STATUS_ALL_CLEAR}\n",
                encoding="utf-8",
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-output-evidence-based"]
            )
            self.assertEqual("insufficient-path-evidence", payload["reason"])
            self.assertEqual(1, payload["path_reference_count"])
            self.assertEqual(3, payload["required"])

    def test_artifact_gate_reports_an_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_artifact_path(plan_dir).write_text(
                json.dumps({"version": 1, "outcome": "all-clear", "findings": []}),
                encoding="utf-8",
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )
            self.assertEqual("final-review-artifact-consistent", payload["check"])
            self.assertEqual("artifact-version-unsupported", payload["reason"])
            self.assertEqual(1, payload["version"])
            self.assertEqual(review.REVIEW_ARTIFACT_VERSION, payload["expected"])

    def test_artifact_gate_reports_the_offending_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_artifact_path(plan_dir).write_text(
                json.dumps(
                    {
                        "version": review.REVIEW_ARTIFACT_VERSION,
                        "outcome": "findings",
                        "findings": [{"id": "F1", "severity": "high"}],
                    }
                ),
                encoding="utf-8",
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )
            self.assertEqual("finding-missing-keys", payload["reason"])
            self.assertEqual(0, payload["index"])
            self.assertEqual("F1", payload["finding_id"])
            self.assertIn("title", payload["missing"])

    def test_artifact_gate_reports_unparsable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_artifact_path(plan_dir).write_text(
                "{not json", encoding="utf-8"
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )
            self.assertEqual("artifact-unparsable", payload["reason"])
            self.assertTrue(payload["error"])

    def finding_artifact(self, plan_dir: Path, checks: list[dict[str, object]]) -> None:
        review.final_review_output_path(plan_dir).write_text(
            "# Final review\n\n## Findings\n\nSee agent_session_loop/runner.py.\n\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        review.final_review_artifact_path(plan_dir).write_text(
            json.dumps(
                {
                    "version": review.REVIEW_ARTIFACT_VERSION,
                    "outcome": "findings",
                    "findings": [
                        {
                            "id": "F1",
                            "severity": "high",
                            "title": "Absence gate",
                            "evidence": [{"path": "agent_session_loop/runner.py"}],
                            "explanation": "why",
                            "repair_group": "g1",
                            "done_criteria": ["fixed"],
                            "verification_checks": checks,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_artifact_gate_rejects_an_absence_gate_the_review_authored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            self.finding_artifact(
                plan_dir,
                [
                    {
                        "type": "regex_in_file",
                        "name": "drops-the-claim",
                        "path": "docs/evidence.md",
                        "pattern": "75-pixel",
                        "expect_absent": True,
                    }
                ],
            )

            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )

            self.assertEqual("verification-check-unknown-keys", payload["reason"])
            self.assertEqual("F1", payload["finding_id"])
            self.assertEqual("drops-the-claim", payload["check_name"])
            self.assertEqual(["expect_absent"], payload["unknown"])
            self.assertEqual(
                contracts.CHECK_PRESENCE_SEMANTICS["regex_in_file"],
                payload["semantics"],
            )

    def test_artifact_gate_leaves_an_unknown_check_type_to_deep_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            self.finding_artifact(
                plan_dir, [{"type": "bogus", "name": "not-a-check", "nonsense": 1}]
            )

            completed = self.run_checks(plan_dir)["final-review-artifact-consistent"]

            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_repository_aware_gate_keeps_the_severity_predicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            payload = review_session_contract._generated_review_contract_payload(
                plans_session_ids.parse_session_id("2"),
                plan_dir=plan_dir,
                repository_aliases=("SVC", "OAK"),
            )
            source = payload["checks"][1]["argv"][2]
            self.assertIn(
                f"EXPECTED_VERSION = {review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION}",
                source,
            )
            self.assertIn("finding-severity-invalid", source)
            self.assertIn("finding-repo-unknown", source)
            # The reported expectation reads the same anchor the widening
            # rewrote, so it cannot drift back to the unaware version.
            self.assertIn("expected=EXPECTED_VERSION", source)
            self.assertNotIn(
                f"EXPECTED_VERSION = {review.REVIEW_ARTIFACT_VERSION}", source
            )
