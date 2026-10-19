# ruff: noqa: F401, F403, F405, I001
"""Runner progress-entry helper tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_progress_test_support import *


class RunnerProgressEntryHelperTests(unittest.TestCase):
    """Unit coverage for ``handoff_guards._append_progress_entry``."""

    def _write_complete_plan(self, plan_dir: Path) -> plans_models.PlanSnapshot:
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        (plan_dir / "session-1.md").write_text(
            "\n".join(
                [
                    "# Session 1: Test",
                    "",
                    "Instructions.",
                    "",
                    "## Handoff",
                    "",
                    "- **Status**: complete",
                    "- **Files modified**: cli.py, plans.py",
                    "- **Key decisions**: chose A over B",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**: extra notes",
                    "",
                    "### Discovered Facts",
                    "",
                    "- fact one",
                    "- fact two",
                    "",
                    "### Open Questions",
                    "",
                    "- question one",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return plans_snapshot.inspect_plan(plan_dir)

    def test_append_returns_true_then_false_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            snapshot = self._write_complete_plan(plan_dir)
            completed_at = "2026-06-19T15:00:00Z"

            self.assertTrue(
                handoff_guards._append_progress_entry(snapshot, "1", completed_at)
            )
            self.assertFalse(
                handoff_guards._append_progress_entry(snapshot, "1", completed_at)
            )

            entries = plans_progress.read_progress_entries(plan_dir)
            self.assertEqual(1, len(entries))
            entry = entries[0]
            self.assertEqual("1", entry.session_id.value)
            self.assertEqual("complete", entry.status)
            self.assertEqual(("cli.py", "plans.py"), entry.files_modified)
            self.assertEqual("chose A over B", entry.key_decisions)
            self.assertEqual(("fact one", "fact two"), entry.discovered_facts)
            self.assertEqual(("question one",), entry.open_questions)
            self.assertEqual("(none)", entry.blockers)

    def test_append_with_distinct_timestamp_writes_second_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            snapshot = self._write_complete_plan(plan_dir)

            self.assertTrue(
                handoff_guards._append_progress_entry(
                    snapshot, "1", "2026-06-19T15:00:00Z"
                )
            )
            self.assertTrue(
                handoff_guards._append_progress_entry(
                    snapshot, "1", "2026-06-19T16:00:00Z"
                )
            )

            entries = plans_progress.read_progress_entries(plan_dir)
            self.assertEqual(2, len(entries))
            self.assertEqual(
                ["2026-06-19T15:00:00Z", "2026-06-19T16:00:00Z"],
                [entry.completed_at for entry in entries],
            )

    def test_append_raises_when_session_missing_from_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            snapshot = self._write_complete_plan(plan_dir)

            with self.assertRaises(plans_errors.PlanError):
                handoff_guards._append_progress_entry(
                    snapshot, "99", "2026-06-19T15:00:00Z"
                )

    def test_utc_iso_now_returns_iso_string_with_timezone(self) -> None:
        value = clock.utc_iso_now()

        parsed = clock.parse_datetime(value)
        self.assertIsNotNone(parsed)
        self.assertEqual("UTC", parsed.tzname())
