# ruff: noqa: F401, F403, F405, I001
"""Progress journal repair tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_progress_test_support import *


class ProgressRepairHelperTests(unittest.TestCase):
    """Coverage for the shared `progress_repair.repair_progress_journal`."""

    def _write_completed_plan(
        self,
        plan_dir: Path,
        session_ids: tuple[str, ...] = ("1", "2", "3"),
        *,
        complete_ids: tuple[str, ...] | None = None,
    ) -> None:
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        complete = set(complete_ids) if complete_ids is not None else set(session_ids)
        for session_id in session_ids:
            filename = plans_session_ids.session_id_to_filename(session_id)
            label = plans_session_ids.session_id_display_label(session_id)
            status = "complete" if session_id in complete else "pending"
            (plan_dir / filename).write_text(
                "\n".join(
                    [
                        f"# {label}: Test",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        f"- **Status**: {status}",
                        f"- **Files modified**: file-{session_id}.py",
                        f"- **Key decisions**: decision for {session_id}",
                        "- **Blockers for next session**: (none)",
                        f"- **Notes**: notes for {session_id}",
                        "",
                        "### Discovered Facts",
                        "",
                        f"- fact about {session_id}",
                        "",
                        "### Open Questions",
                        "",
                        f"- question about {session_id}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

    def test_repair_writes_fresh_journal_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            progress_file = plans_progress.progress_path(plan_dir)
            self.assertFalse(progress_file.exists())

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertEqual("rewrite", result.action)
            self.assertEqual(progress_file, result.path)
            self.assertEqual(2, result.entries_written)
            self.assertEqual(("1", "2"), result.completed_session_ids)
            self.assertFalse(result.journal_replaced)
            entries = plans_progress.read_progress_entries(plan_dir)
            self.assertEqual(
                ["1", "2"],
                [entry.session_id.value for entry in entries],
            )
            self.assertEqual(("file-1.py",), entries[0].files_modified)

    def test_repair_replaces_header_only_journal_with_full_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            plans_progress.write_progress_journal(plan_dir, ())
            self.assertEqual(
                plans_progress.PROGRESS_HEADER,
                plans_progress.progress_path(plan_dir).read_text(encoding="utf-8"),
            )

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertTrue(result.journal_replaced)
            self.assertEqual(2, result.entries_written)
            self.assertEqual(("1", "2"), result.completed_session_ids)
            self.assertEqual(
                ["1", "2"],
                [
                    entry.session_id.value
                    for entry in plans_progress.read_progress_entries(plan_dir)
                ],
            )

    def test_repair_overwrites_stale_journal_with_authoritative_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            stale_only = (
                plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id("1"),
                    status="complete",
                    completed_at="2020-01-01T00:00:00Z",
                    files_modified=("stale.py",),
                    key_decisions="stale decision",
                    discovered_facts=(),
                    open_questions=(),
                    blockers=None,
                ),
            )
            plans_progress.write_progress_journal(plan_dir, stale_only)

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertTrue(result.journal_replaced)
            self.assertEqual(2, result.entries_written)
            self.assertEqual(("1", "2"), result.completed_session_ids)
            entries = plans_progress.read_progress_entries(plan_dir)
            self.assertEqual(
                ["1", "2"],
                [entry.session_id.value for entry in entries],
            )
            for entry in entries:
                self.assertEqual(
                    f"decision for {entry.session_id.value}",
                    entry.key_decisions,
                )
                self.assertNotEqual("stale decision", entry.key_decisions)

    def test_repair_force_replaces_malformed_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1",))
            progress_file = plans_progress.progress_path(plan_dir)
            progress_file.write_text(
                "# Progress Journal\n"
                "<!-- agent-session-loop progress-journal v999 -->\n"
                "garbage that the parser would reject\n",
                encoding="utf-8",
            )

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertTrue(result.journal_replaced)
            self.assertEqual(1, result.entries_written)
            self.assertIn(
                plans_progress.PROGRESS_SCHEMA_COMMENT,
                progress_file.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                ("1",),
                tuple(
                    entry.session_id.value
                    for entry in plans_progress.read_progress_entries(plan_dir)
                ),
            )

    def test_repair_writes_header_only_when_no_completed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"), complete_ids=())
            progress_file = plans_progress.progress_path(plan_dir)
            self.assertFalse(progress_file.exists())

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertEqual(0, result.entries_written)
            self.assertEqual((), result.completed_session_ids)
            self.assertFalse(result.journal_replaced)
            self.assertEqual(
                plans_progress.PROGRESS_HEADER,
                progress_file.read_text(encoding="utf-8"),
            )
            self.assertEqual((), plans_progress.read_progress_entries(plan_dir))

    def test_repair_uses_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))

            with mock.patch(
                "agent_session_loop.durability.os.replace",
                wraps=os.replace,
            ) as replace:
                progress_repair.repair_progress_journal(plan_dir)

            self.assertEqual(1, replace.call_count)

    def test_repair_uses_provided_snapshot_without_reinspecting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1",))
            snapshot = plans_snapshot.inspect_plan(plan_dir)

            with mock.patch(
                "agent_session_loop.progress_repair.plans_snapshot.inspect_plan"
            ) as inspect:
                result = progress_repair.repair_progress_journal(
                    plan_dir, snapshot=snapshot
                )

            inspect.assert_not_called()
            self.assertEqual(1, result.entries_written)
            self.assertEqual(("1",), result.completed_session_ids)
