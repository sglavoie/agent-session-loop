# ruff: noqa: F401, F403, F405, I001
"""Progress journal persistence tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_progress_test_support import *


class ProgressJournalTests(unittest.TestCase):
    def _entry(
        self,
        *,
        session_id: str = "1",
        status: str = "complete",
        completed_at: str = "2026-06-19T14:22:00Z",
        files_modified: tuple[str, ...] = ("cli.py", "plans.py"),
        key_decisions: str | None = "chose option A over B (perf)",
        discovered_facts: tuple[str, ...] = ("Lockfile X uses Y format.",),
        open_questions: tuple[str, ...] = (),
        blockers: str | None = None,
    ) -> plans_models.ProgressEntry:
        return plans_models.ProgressEntry(
            session_id=plans_session_ids.parse_session_id(session_id),
            status=status,  # type: ignore[arg-type]
            completed_at=completed_at,
            files_modified=files_modified,
            key_decisions=key_decisions,
            discovered_facts=discovered_facts,
            open_questions=open_questions,
            blockers=blockers,
        )

    def test_first_append_creates_file_with_header_and_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            path = plans_progress.append_progress_entry(plan_dir, self._entry())

            self.assertEqual(plans_progress.progress_path(plan_dir), path)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(plans_progress.PROGRESS_HEADER))
            self.assertIn(plans_progress.PROGRESS_SCHEMA_COMMENT, text)
            self.assertIn("## Session 1 — complete (2026-06-19T14:22:00Z)", text)
            self.assertIn("- Files touched: cli.py, plans.py", text)
            self.assertIn("- Decisions: chose option A over B (perf)", text)
            self.assertIn("- Discovered facts:\n  - Lockfile X uses Y format.", text)
            self.assertIn("- Open questions: none", text)
            self.assertIn("- Blockers: none", text)

    def test_second_append_preserves_prior_entry_and_appends_new_one_in_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            plans_progress.append_progress_entry(plan_dir, self._entry())
            second = self._entry(
                session_id="2",
                completed_at="2026-06-19T15:00:00Z",
                files_modified=("runner.py",),
                key_decisions=None,
                discovered_facts=(),
                open_questions=("Should we cache?",),
                blockers="needs review",
            )
            plans_progress.append_progress_entry(plan_dir, second)

            text = plans_progress.progress_path(plan_dir).read_text(encoding="utf-8")
            self.assertLess(
                text.index("## Session 1 — complete"),
                text.index("## Session 2 — complete"),
            )
            self.assertIn("- Files touched: runner.py", text)
            self.assertIn("- Decisions: none", text)
            self.assertIn("- Open questions:\n  - Should we cache?", text)
            self.assertIn("- Blockers: needs review", text)
            # Exactly one blank line between the two session headings.
            session1_index = text.index("## Session 1")
            session2_index = text.index("## Session 2")
            between = text[session1_index:session2_index]
            self.assertTrue(between.endswith("\n\n"))
            self.assertFalse(between.endswith("\n\n\n"))

    def test_append_is_idempotent_for_duplicate_session_id_and_timestamp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            plans_progress.append_progress_entry(plan_dir, self._entry())
            path = plans_progress.progress_path(plan_dir)
            first_bytes = path.read_bytes()

            # Duplicate by (session_id, completed_at) — even with different fields.
            duplicate = self._entry(
                files_modified=("totally", "different"),
                key_decisions="something else entirely",
            )
            plans_progress.append_progress_entry(plan_dir, duplicate)

            self.assertEqual(first_bytes, path.read_bytes())

    def test_append_after_different_timestamp_is_not_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            plans_progress.append_progress_entry(plan_dir, self._entry())
            plans_progress.append_progress_entry(
                plan_dir,
                self._entry(completed_at="2026-06-19T14:23:00Z"),
            )

            entries = plans_progress.read_progress_entries(plan_dir)
            self.assertEqual(2, len(entries))

    def test_append_raises_on_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            path = plans_progress.progress_path(plan_dir)
            path.write_text(
                "# Progress Journal\n"
                "<!-- agent-session-loop progress-journal v2 -->\n"
                "\n"
                "## Session 1 — complete (2026-06-19T14:22:00Z)\n"
                "- Files touched: none\n"
                "- Decisions: none\n"
                "- Discovered facts: none\n"
                "- Open questions: none\n"
                "- Blockers: none\n",
                encoding="utf-8",
            )

            with self.assertRaises(plans_errors.PlanError) as ctx:
                plans_progress.append_progress_entry(plan_dir, self._entry())
            self.assertIn("v2", str(ctx.exception))
            self.assertIn("v1", str(ctx.exception))

    def test_read_progress_entries_raises_on_missing_version_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            path = plans_progress.progress_path(plan_dir)
            path.write_text(
                "# Progress Journal\n\n"
                "## Session 1 — complete (2026-06-19T14:22:00Z)\n"
                "- Files touched: none\n"
                "- Decisions: none\n"
                "- Discovered facts: none\n"
                "- Open questions: none\n"
                "- Blockers: none\n",
                encoding="utf-8",
            )

            with self.assertRaises(plans_errors.PlanError):
                plans_progress.read_progress_entries(plan_dir)

    def test_read_progress_entries_returns_empty_when_file_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            self.assertEqual((), plans_progress.read_progress_entries(plan_dir))

    def test_round_trip_render_then_read_recovers_identical_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            entries = (
                self._entry(),
                self._entry(
                    session_id="1.1",
                    completed_at="2026-06-19T14:30:00Z",
                    files_modified=(),
                    key_decisions=None,
                    discovered_facts=("Fact A.", "Fact B."),
                    open_questions=("Q1?", "Q2?"),
                    blockers="db migration pending",
                ),
                self._entry(
                    session_id="3",
                    completed_at="2026-06-19T15:00:00Z",
                    files_modified=("runner.py",),
                    key_decisions="kept inline",
                    discovered_facts=(),
                    open_questions=(),
                    blockers=None,
                ),
            )
            for entry in entries:
                plans_progress.append_progress_entry(plan_dir, entry)

            recovered = plans_progress.read_progress_entries(plan_dir)
            self.assertEqual(entries, recovered)

    def test_render_progress_entry_returns_newline_terminated_block(self) -> None:
        rendered = plans_progress.render_progress_entry(self._entry())
        self.assertTrue(rendered.endswith("\n"))
        self.assertTrue(
            rendered.startswith("## Session 1 — complete (2026-06-19T14:22:00Z)")
        )

    def test_plan_content_sha256_unchanged_when_progress_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans_content_hash.plan_content_sha256(plan_dir)
            plans_progress.append_progress_entry(plan_dir, self._entry())
            self.assertEqual(baseline, plans_content_hash.plan_content_sha256(plan_dir))
            plans_progress.append_progress_entry(
                plan_dir,
                self._entry(
                    session_id="2",
                    completed_at="2026-06-19T15:00:00Z",
                ),
            )
            self.assertEqual(baseline, plans_content_hash.plan_content_sha256(plan_dir))
            # Manual mutation (e.g., user edit) of progress.md still ignored.
            path = plans_progress.progress_path(plan_dir)
            path.write_text(
                path.read_text(encoding="utf-8") + "\n<!-- noise -->\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans_content_hash.plan_content_sha256(plan_dir))

    def test_stable_readiness_sha256_unchanged_when_progress_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)
            plans_progress.append_progress_entry(plan_dir, self._entry())
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )
            plans_progress.append_progress_entry(
                plan_dir,
                self._entry(
                    session_id="2",
                    completed_at="2026-06-19T15:00:00Z",
                ),
            )
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )
            path = plans_progress.progress_path(plan_dir)
            path.write_text(
                path.read_text(encoding="utf-8") + "\n<!-- noise -->\n",
                encoding="utf-8",
            )
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_atomic_write_failure_leaves_original_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            plans_progress.append_progress_entry(plan_dir, self._entry())
            path = plans_progress.progress_path(plan_dir)
            original_bytes = path.read_bytes()

            with (
                mock.patch(
                    "agent_session_loop.durability.os.replace",
                    side_effect=OSError("boom"),
                ),
                self.assertRaises(plans_errors.PlanError),
            ):
                plans_progress.append_progress_entry(
                    plan_dir,
                    self._entry(
                        session_id="2",
                        completed_at="2026-06-19T15:00:00Z",
                    ),
                )

            self.assertEqual(original_bytes, path.read_bytes())
            self.assertEqual(
                [],
                list(plan_dir.glob(".progress.md.*")),
                "atomic-write temp file should be cleaned up on failure",
            )
