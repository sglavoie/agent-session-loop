# mypy: ignore-errors
from .asl_write_guards_support import *


class ProtectedPathChangedPathsTests(unittest.TestCase):
    """What the guard reads out of the write guard's own recorded evidence."""

    def test_a_missing_comparison_cannot_answer_the_question(self) -> None:
        self.assertIsNone(write_guards_protected._protected_path_changed_paths(None))

    def test_a_partial_inventory_cannot_answer_the_question(self) -> None:
        # An unavailable lane means the guard did not look everywhere, so an
        # empty `changed` map is not evidence that nothing moved.
        self.assertIsNone(
            write_guards_protected._protected_path_changed_paths(
                {
                    "changed": {},
                    "unavailable": {"svc": {"after": {"reason": "unreadable"}}},
                }
            )
        )

    def test_a_comparison_without_a_changed_map_cannot_answer_it_either(self) -> None:
        self.assertIsNone(write_guards_protected._protected_path_changed_paths({}))
        self.assertIsNone(
            write_guards_protected._protected_path_changed_paths({"changed": ["svc"]})
        )

    def test_a_complete_comparison_with_nothing_changed_clears_every_rule(
        self,
    ) -> None:
        self.assertEqual(
            (),
            write_guards_protected._protected_path_changed_paths(
                {"changed": {}, "unavailable": {}}
            ),
        )

    def test_qualified_paths_are_collected_across_repositories_in_order(self) -> None:
        self.assertEqual(
            ("svc:a.py", "svc:b.py", "ops:c.py"),
            write_guards_protected._protected_path_changed_paths(
                {
                    "changed": {
                        "svc": {"qualified_paths": ["svc:a.py", "svc:b.py"]},
                        "ops": {"qualified_paths": ["ops:c.py"]},
                    }
                }
            ),
        )

    def test_the_same_path_named_twice_is_reported_once(self) -> None:
        self.assertEqual(
            ("svc:a.py",),
            write_guards_protected._protected_path_changed_paths(
                {
                    "changed": {
                        "svc": {"qualified_paths": ["svc:a.py"]},
                        "svc-worktree": {"qualified_paths": ["svc:a.py"]},
                    }
                }
            ),
        )

    def test_an_entry_that_names_no_paths_contributes_nothing(self) -> None:
        self.assertEqual(
            ("svc:a.py",),
            write_guards_protected._protected_path_changed_paths(
                {
                    "changed": {
                        "svc": {"qualified_paths": ["svc:a.py"]},
                        "ops": {"qualified_paths": "ops:c.py"},
                        "docs": ["not a mapping"],
                    }
                }
            ),
        )


class ProtectedPathReviewChangedPathsTests(unittest.TestCase):
    """The recapture a crashing legacy review turn leaves the guard to make."""

    def setUp(self) -> None:
        self.context: dict[str, Any] = {
            "review_fingerprint_before": {"git": True, "head": "abc"},
            "cwd": Path("/tmp/repo"),
            "before": SimpleNamespace(plan_dir=Path("/tmp/plan")),
        }

    def _changed(self, **overrides: Any) -> tuple[str, ...] | None:
        context = {**self.context, **overrides}
        return write_guards_protected._protected_path_review_changed_paths(
            context, exclude=(), include_ignored=False
        )

    def test_no_turn_start_fingerprint_cannot_answer_the_question(self) -> None:
        self.assertIsNone(self._changed(review_fingerprint_before=None))

    def test_a_recapture_that_raised_cannot_answer_it_either(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            side_effect=OSError("boom"),
        ):
            self.assertIsNone(self._changed())

    def test_a_recapture_that_came_back_unusable_is_unavailable(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            return_value={"git": True, "unavailable": {"reason": "unreadable"}},
        ):
            self.assertIsNone(self._changed())

    def test_an_unmoved_fingerprint_clears_every_rule_at_once(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            return_value={"git": True, "head": "abc"},
        ):
            self.assertEqual((), self._changed())

    def test_a_moved_fingerprint_names_its_paths_qualified_as_legacy(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            return_value={
                "git": True,
                "head": "abc",
                "tracked_patches": [["src/app.py", "sha-after"]],
                "untracked": [["notes.md", "file", "sha"]],
            },
        ):
            self.assertEqual(
                (
                    f"{guards.LEGACY_PATH_QUALIFIER}:src/app.py",
                    f"{guards.LEGACY_PATH_QUALIFIER}:notes.md",
                ),
                self._changed(),
            )
