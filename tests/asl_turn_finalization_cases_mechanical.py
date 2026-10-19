from .asl_turn_finalization_support import *


class MechanicalHandoffDefectTests(unittest.TestCase):
    def classify(
        self, *, status: str, files_modified: str | None
    ) -> handoff_correction._HandoffDefect | None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("pending",))
            snapshot = plans_snapshot.inspect_plan(plan_dir)
            set_handoff(
                plan_dir / "session-1.md",
                status=status,
                files_modified=files_modified,
            )
            return handoff_correction._classify_mechanical_handoff_defect(
                snapshot, "1", registry_aliases=ALIASES
            )

    def test_missing_files_modified_line_is_correctable(self) -> None:
        defect = self.classify(status="complete", files_modified=None)
        assert defect is not None
        self.assertEqual(handoff_correction.HANDOFF_DEFECT_MISSING_FILES, defect.code)
        self.assertIn("SVC", defect.instruction)

    def test_pending_placeholder_is_correctable(self) -> None:
        defect = self.classify(status="complete", files_modified="(none yet)")
        assert defect is not None
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_PENDING_PLACEHOLDER, defect.code
        )

    def test_unqualified_entry_is_correctable(self) -> None:
        defect = self.classify(status="complete", files_modified="README.md")
        assert defect is not None
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, defect.code
        )
        self.assertIn("README.md", defect.detail)

    def test_mixed_entries_report_the_first_invalid_one(self) -> None:
        defect = self.classify(
            status="complete", files_modified="SVC:README.md, README.md"
        )
        assert defect is not None
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, defect.code
        )
        self.assertIn("'README.md'", defect.detail)

    def test_qualified_entries_are_not_a_defect(self) -> None:
        self.assertIsNone(
            self.classify(status="complete", files_modified="SVC:README.md")
        )

    def test_no_files_marker_is_not_a_defect(self) -> None:
        self.assertIsNone(self.classify(status="complete", files_modified="(none)"))

    def test_unknown_alias_is_not_a_mechanical_defect(self) -> None:
        # ``Nope`` parses as a qualified path but names no registered
        # repository, which is a scope question rather than a typo.
        defect = self.classify(status="complete", files_modified="Nope:README.md")
        assert defect is not None
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, defect.code
        )

    def test_incomplete_session_is_not_corrected(self) -> None:
        self.assertIsNone(self.classify(status="pending", files_modified="README.md"))

    def test_unknown_session_is_not_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("pending",))
            snapshot = plans_snapshot.inspect_plan(plan_dir)
            self.assertIsNone(
                handoff_correction._classify_mechanical_handoff_defect(
                    snapshot, "9", registry_aliases=ALIASES
                )
            )
