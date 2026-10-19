from .asl_turn_finalization_support import *


class PreservedReviewArtifactTests(unittest.TestCase):
    def test_valid_artifacts_are_digested_with_their_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            evidence = handoff_correction._preserved_review_artifact_evidence(plan_dir)
            assert evidence is not None
            self.assertEqual(
                str(review.final_review_output_path(plan_dir)),
                evidence["review_output_path"],
            )
            self.assertEqual(
                review.final_review_output_sha256(plan_dir),
                evidence["review_output_sha256"],
            )
            self.assertEqual(
                review.final_review_artifact_sha256(plan_dir),
                evidence["review_artifact_sha256"],
            )
            self.assertTrue(evidence["review_verdict_ok"])
            self.assertEqual("all-clear", evidence["review_outcome"])
            self.assertEqual(0, evidence["review_total_findings"])

    def test_unparsable_verdict_still_preserves_the_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\nNo verdict here.\n", encoding="utf-8"
            )
            evidence = handoff_correction._preserved_review_artifact_evidence(plan_dir)
            assert evidence is not None
            self.assertFalse(evidence["review_verdict_ok"])
            self.assertTrue(evidence["review_verdict_reason"])
            self.assertIn("review_output_sha256", evidence)

    def test_plan_without_review_artifacts_yields_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("pending",))
            self.assertIsNone(
                handoff_correction._preserved_review_artifact_evidence(plan_dir)
            )
