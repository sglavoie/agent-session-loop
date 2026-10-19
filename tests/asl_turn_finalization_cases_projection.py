from .asl_turn_finalization_support import *


class ReviewArtifactProjectionTests(unittest.TestCase):
    def review_session_text(self, plan_dir: Path, files_modified: str) -> str:
        body = review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("2"),
            review_harness="codex",
            plan_dir=plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        return re.sub(
            r"- \*\*Files modified\*\*: .*",
            f"- **Files modified**: {files_modified}",
            body,
            count=1,
        )

    def project(self, files_modified: str) -> plans_handoff._ReviewArtifactProjection:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir(parents=True)
            text = self.review_session_text(plan_dir, files_modified)
            return plans_handoff._project_generated_review_handoff_artifacts(text)

    def files_line(self, text: str) -> str:
        for line in text.splitlines():
            if line.startswith(plans_handoff.HANDOFF_FILES_PREFIX):
                return line[len(plans_handoff.HANDOFF_FILES_PREFIX) :].strip()
        raise AssertionError("no Files modified line")

    def test_review_only_entries_project_to_none_and_are_exclusive(self) -> None:
        projection = self.project(
            f"{review.FINAL_REVIEW_FILENAME}, {review.FINAL_REVIEW_ARTIFACT_FILENAME}"
        )
        self.assertTrue(projection.exclusive)
        self.assertEqual("(none)", self.files_line(projection.text))

    def test_mixed_entries_keep_the_repository_path_and_are_not_exclusive(self) -> None:
        projection = self.project(f"{review.FINAL_REVIEW_FILENAME}, README.md")
        self.assertFalse(projection.exclusive)
        self.assertEqual("README.md", self.files_line(projection.text))

    def test_repository_only_entries_are_left_untouched(self) -> None:
        projection = self.project("README.md")
        self.assertFalse(projection.exclusive)
        self.assertEqual("README.md", self.files_line(projection.text))

    def test_dot_prefixed_review_entries_are_recognized(self) -> None:
        projection = self.project(f"./{review.FINAL_REVIEW_FILENAME}, README.md")
        self.assertEqual("README.md", self.files_line(projection.text))

    def test_mixed_handoff_is_never_rewritten_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("complete", "complete"))
            session_path = plan_dir / "session-2.md"
            session_path.write_text(
                self.review_session_text(
                    plan_dir, f"{review.FINAL_REVIEW_FILENAME}, README.md"
                ),
                encoding="utf-8",
            )
            before = session_path.read_text(encoding="utf-8")
            changed = plans_handoff.normalize_generated_review_handoff_artifacts(
                plan_dir
            )
            self.assertEqual((), changed)
            self.assertEqual(before, session_path.read_text(encoding="utf-8"))

    def test_review_only_handoff_is_normalized_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("complete", "complete"))
            session_path = plan_dir / "session-2.md"
            session_path.write_text(
                self.review_session_text(plan_dir, review.FINAL_REVIEW_FILENAME),
                encoding="utf-8",
            )
            changed = plans_handoff.normalize_generated_review_handoff_artifacts(
                plan_dir
            )
            self.assertEqual(["session-2.md"], [path.name for path in changed])
            self.assertEqual(
                "(none)", self.files_line(session_path.read_text(encoding="utf-8"))
            )
