"""Progress journal, runner entry helper, and progress CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    clock,
    handoff_guards,
    plans,
    progress_cli,
    progress_repair,
)
from tests.support import (
    write_plan,
)


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
    ) -> plans.ProgressEntry:
        return plans.ProgressEntry(
            session_id=plans.parse_session_id(session_id),
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

            path = plans.append_progress_entry(plan_dir, self._entry())

            self.assertEqual(plans.progress_path(plan_dir), path)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(plans.PROGRESS_HEADER))
            self.assertIn(plans.PROGRESS_SCHEMA_COMMENT, text)
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

            plans.append_progress_entry(plan_dir, self._entry())
            second = self._entry(
                session_id="2",
                completed_at="2026-06-19T15:00:00Z",
                files_modified=("runner.py",),
                key_decisions=None,
                discovered_facts=(),
                open_questions=("Should we cache?",),
                blockers="needs review",
            )
            plans.append_progress_entry(plan_dir, second)

            text = plans.progress_path(plan_dir).read_text(encoding="utf-8")
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

            plans.append_progress_entry(plan_dir, self._entry())
            path = plans.progress_path(plan_dir)
            first_bytes = path.read_bytes()

            # Duplicate by (session_id, completed_at) — even with different fields.
            duplicate = self._entry(
                files_modified=("totally", "different"),
                key_decisions="something else entirely",
            )
            plans.append_progress_entry(plan_dir, duplicate)

            self.assertEqual(first_bytes, path.read_bytes())

    def test_append_after_different_timestamp_is_not_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            plans.append_progress_entry(plan_dir, self._entry())
            plans.append_progress_entry(
                plan_dir,
                self._entry(completed_at="2026-06-19T14:23:00Z"),
            )

            entries = plans.read_progress_entries(plan_dir)
            self.assertEqual(2, len(entries))

    def test_append_raises_on_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            path = plans.progress_path(plan_dir)
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

            with self.assertRaises(plans.PlanError) as ctx:
                plans.append_progress_entry(plan_dir, self._entry())
            self.assertIn("v2", str(ctx.exception))
            self.assertIn("v1", str(ctx.exception))

    def test_read_progress_entries_raises_on_missing_version_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            path = plans.progress_path(plan_dir)
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

            with self.assertRaises(plans.PlanError):
                plans.read_progress_entries(plan_dir)

    def test_read_progress_entries_returns_empty_when_file_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            self.assertEqual((), plans.read_progress_entries(plan_dir))

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
                plans.append_progress_entry(plan_dir, entry)

            recovered = plans.read_progress_entries(plan_dir)
            self.assertEqual(entries, recovered)

    def test_render_progress_entry_returns_newline_terminated_block(self) -> None:
        rendered = plans.render_progress_entry(self._entry())
        self.assertTrue(rendered.endswith("\n"))
        self.assertTrue(
            rendered.startswith("## Session 1 — complete (2026-06-19T14:22:00Z)")
        )

    def test_plan_content_sha256_unchanged_when_progress_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans.plan_content_sha256(plan_dir)
            plans.append_progress_entry(plan_dir, self._entry())
            self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))
            plans.append_progress_entry(
                plan_dir,
                self._entry(
                    session_id="2",
                    completed_at="2026-06-19T15:00:00Z",
                ),
            )
            self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))
            # Manual mutation (e.g., user edit) of progress.md still ignored.
            path = plans.progress_path(plan_dir)
            path.write_text(
                path.read_text(encoding="utf-8") + "\n<!-- noise -->\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans.plan_content_sha256(plan_dir))

    def test_stable_readiness_sha256_unchanged_when_progress_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans.stable_readiness_sha256(plan_dir)
            plans.append_progress_entry(plan_dir, self._entry())
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))
            plans.append_progress_entry(
                plan_dir,
                self._entry(
                    session_id="2",
                    completed_at="2026-06-19T15:00:00Z",
                ),
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))
            path = plans.progress_path(plan_dir)
            path.write_text(
                path.read_text(encoding="utf-8") + "\n<!-- noise -->\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_atomic_write_failure_leaves_original_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            plans.append_progress_entry(plan_dir, self._entry())
            path = plans.progress_path(plan_dir)
            original_bytes = path.read_bytes()

            with (
                mock.patch(
                    "agent_session_loop.durability.os.replace",
                    side_effect=OSError("boom"),
                ),
                self.assertRaises(plans.PlanError),
            ):
                plans.append_progress_entry(
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


class RunnerProgressEntryHelperTests(unittest.TestCase):
    """Unit coverage for ``handoff_guards._append_progress_entry``."""

    def _write_complete_plan(self, plan_dir: Path) -> plans.PlanSnapshot:
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
        return plans.inspect_plan(plan_dir)

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

            entries = plans.read_progress_entries(plan_dir)
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

            entries = plans.read_progress_entries(plan_dir)
            self.assertEqual(2, len(entries))
            self.assertEqual(
                ["2026-06-19T15:00:00Z", "2026-06-19T16:00:00Z"],
                [entry.completed_at for entry in entries],
            )

    def test_append_raises_when_session_missing_from_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            snapshot = self._write_complete_plan(plan_dir)

            with self.assertRaises(plans.PlanError):
                handoff_guards._append_progress_entry(
                    snapshot, "99", "2026-06-19T15:00:00Z"
                )

    def test_utc_iso_now_returns_iso_string_with_timezone(self) -> None:
        value = clock.utc_iso_now()

        parsed = clock.parse_datetime(value)
        self.assertIsNotNone(parsed)
        self.assertEqual("UTC", parsed.tzname())


class ProgressCliTests(unittest.TestCase):
    """Coverage for ``asl backfill-progress`` and ``asl progress``."""

    def _write_completed_plan(
        self,
        plan_dir: Path,
        session_ids: tuple[str, ...] = ("1", "2", "3"),
    ) -> None:
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        for session_id in session_ids:
            filename = plans.session_id_to_filename(session_id)
            label = plans.session_id_display_label(session_id)
            (plan_dir / filename).write_text(
                "\n".join(
                    [
                        f"# {label}: Test",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
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

    def _run_backfill(
        self,
        plan_dir: Path,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> tuple[int, str, str]:
        args = argparse.Namespace(
            plan_dir=str(plan_dir),
            force=force,
            dry_run=dry_run,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = progress_cli.backfill_progress_from_args(args)
        return code, out.getvalue(), err.getvalue()

    def _run_progress(
        self,
        plan_dir: Path,
        *,
        tail: bool = False,
        json_output: bool = False,
    ) -> tuple[int, str, str]:
        args = argparse.Namespace(
            plan_dir=str(plan_dir),
            tail=tail,
            json=json_output,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = progress_cli.progress_from_args(args)
        return code, out.getvalue(), err.getvalue()

    def test_backfill_writes_entries_in_dotted_numeric_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            # Intentionally write filenames in non-sorted alphabetical order
            # to confirm the backfill uses snapshot order (dotted numeric).
            self._write_completed_plan(plan_dir, ("1", "1.1", "2", "10"))

            code, out, err = self._run_backfill(plan_dir)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            self.assertIn("wrote 4 entries", out)

            entries = plans.read_progress_entries(plan_dir)
            self.assertEqual(
                ["1", "1.1", "2", "10"],
                [entry.session_id.value for entry in entries],
            )
            for entry in entries:
                self.assertEqual("complete", entry.status)
                self.assertEqual(
                    (f"file-{entry.session_id.value}.py",),
                    entry.files_modified,
                )
                self.assertEqual(
                    f"decision for {entry.session_id.value}",
                    entry.key_decisions,
                )
                self.assertEqual(
                    (f"fact about {entry.session_id.value}",),
                    entry.discovered_facts,
                )
                self.assertEqual(
                    (f"question about {entry.session_id.value}",),
                    entry.open_questions,
                )

    def test_backfill_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1",))
            progress_file = plans.progress_path(plan_dir)
            progress_file.write_text("preexisting\n", encoding="utf-8")

            code, out, err = self._run_backfill(plan_dir)

            self.assertEqual(1, code)
            self.assertIn("already exists", err)
            self.assertIn("--force", err)
            self.assertEqual(
                "preexisting\n",
                progress_file.read_text(encoding="utf-8"),
            )

    def test_backfill_force_overwrites_existing_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            progress_file = plans.progress_path(plan_dir)
            progress_file.write_text(
                "stale and definitely not v1 schema\n", encoding="utf-8"
            )

            code, out, err = self._run_backfill(plan_dir, force=True)

            self.assertEqual(0, code, err)
            entries = plans.read_progress_entries(plan_dir)
            self.assertEqual(2, len(entries))

    def test_backfill_force_preserves_existing_journal_when_replace_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            progress_file = plans.progress_path(plan_dir)
            progress_file.write_text("old journal contents\n", encoding="utf-8")

            with mock.patch(
                "agent_session_loop.durability.os.replace",
                side_effect=OSError("replace failed"),
            ):
                code, out, err = self._run_backfill(plan_dir, force=True)

            self.assertEqual(1, code)
            self.assertEqual("", out)
            self.assertIn("replace failed", err)
            self.assertEqual(
                "old journal contents\n",
                progress_file.read_text(encoding="utf-8"),
            )
            self.assertEqual([], list(plan_dir.glob(".progress.md.*")))

    def test_backfill_dry_run_prints_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))

            code, out, err = self._run_backfill(plan_dir, dry_run=True)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            self.assertIn(plans.PROGRESS_SCHEMA_COMMENT, out)
            self.assertIn("## Session 1 — complete", out)
            self.assertIn("## Session 2 — complete", out)
            self.assertFalse(plans.progress_path(plan_dir).exists())

    def test_backfill_with_no_completed_sessions_writes_header_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            with mock.patch(
                "agent_session_loop.durability.os.replace",
                wraps=os.replace,
            ) as replace:
                code, out, err = self._run_backfill(plan_dir)

            self.assertEqual(0, code, err)
            self.assertEqual(1, replace.call_count)
            self.assertIn("wrote 0 entries", out)
            journal = plans.progress_path(plan_dir).read_text(encoding="utf-8")
            self.assertEqual(plans.PROGRESS_HEADER, journal)
            self.assertEqual((), plans.read_progress_entries(plan_dir))

    def test_progress_prints_existing_journal_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1",))
            self._run_backfill(plan_dir)

            code, out, err = self._run_progress(plan_dir)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            self.assertEqual(
                plans.progress_path(plan_dir).read_text(encoding="utf-8"),
                out,
            )

    def test_progress_errors_when_journal_missing_without_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            code, out, err = self._run_progress(plan_dir)

            self.assertEqual(1, code)
            self.assertIn("does not exist", err)
            self.assertIn("asl backfill-progress", err)

    def test_progress_json_returns_entries_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            self._run_backfill(plan_dir)

            code, out, err = self._run_progress(plan_dir, json_output=True)

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual("progress", payload["command"])
            self.assertEqual(2, payload["entry_count"])
            self.assertEqual(2, len(payload["entries"]))
            first = payload["entries"][0]
            self.assertEqual(
                {
                    "session_id",
                    "status",
                    "completed_at",
                    "files_modified",
                    "key_decisions",
                    "discovered_facts",
                    "open_questions",
                    "blockers",
                },
                set(first.keys()),
            )
            self.assertEqual("1", first["session_id"])
            self.assertEqual("complete", first["status"])
            self.assertEqual(["file-1.py"], first["files_modified"])
            self.assertEqual("decision for 1", first["key_decisions"])
            self.assertEqual(["fact about 1"], first["discovered_facts"])
            self.assertEqual(["question about 1"], first["open_questions"])

    def test_progress_json_empty_entries_when_journal_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            code, out, err = self._run_progress(plan_dir, json_output=True)

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual([], payload["entries"])
            self.assertEqual(0, payload["entry_count"])

    def test_progress_json_errors_on_malformed_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            plans.progress_path(plan_dir).write_text(
                "# Progress Journal\n"
                "<!-- agent-session-loop progress-journal v999 -->\n",
                encoding="utf-8",
            )

            code, out, err = self._run_progress(plan_dir, json_output=True)

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("progress", payload["command"])
            self.assertEqual("malformed-progress-journal", payload["reason"])
            self.assertIn("v999", payload["error"])

    def test_follow_progress_streams_newly_appended_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            initial_entry = plans.ProgressEntry(
                session_id=plans.parse_session_id("1"),
                status="complete",
                completed_at="2026-06-19T15:00:00Z",
                files_modified=("first.py",),
                key_decisions=None,
                discovered_facts=(),
                open_questions=(),
                blockers=None,
            )
            plans.append_progress_entry(plan_dir, initial_entry)

            buffer = io.StringIO()
            stop = threading.Event()
            seen_new_entry = threading.Event()

            class WaitableStringIO(io.StringIO):
                def write(self, value: str) -> int:
                    written = super().write(value)
                    if "Session 2" in self.getvalue():
                        seen_new_entry.set()
                    return written

            buffer = WaitableStringIO()
            thread = threading.Thread(
                target=progress_cli.follow_progress,
                args=(plan_dir,),
                kwargs={
                    "out": buffer,
                    "poll_interval": 0.01,
                    "stop_event": stop,
                },
                daemon=True,
            )
            thread.start()
            try:
                second_entry = plans.ProgressEntry(
                    session_id=plans.parse_session_id("2"),
                    status="complete",
                    completed_at="2026-06-19T15:30:00Z",
                    files_modified=("second.py",),
                    key_decisions="kept inline",
                    discovered_facts=(),
                    open_questions=(),
                    blockers=None,
                )
                plans.append_progress_entry(plan_dir, second_entry)
                self.assertTrue(
                    seen_new_entry.wait(timeout=2.0),
                    "follow_progress did not pick up the new entry in time",
                )
            finally:
                stop.set()
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())

            output = buffer.getvalue()
            self.assertIn("## Session 2 — complete", output)
            self.assertIn("- Files touched: second.py", output)
            # The pre-existing Session 1 entry was already in the file when
            # the follower started, so it should not appear in the streamed
            # output.
            self.assertNotIn("## Session 1 — complete", output)

    def test_backfill_progress_cli_registered_in_parser(self) -> None:
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1",))
            args = parser.parse_args(["backfill-progress", str(plan_dir)])
            self.assertEqual(
                progress_cli.backfill_progress_from_args,
                cli.resolve_handler(args),
            )
            self.assertEqual(str(plan_dir), args.plan_dir)
            self.assertFalse(args.force)
            self.assertFalse(args.dry_run)

    def test_progress_cli_registered_in_parser(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["progress", "/tmp/example", "--tail", "--json"])
        self.assertEqual(progress_cli.progress_from_args, cli.resolve_handler(args))
        self.assertEqual("/tmp/example", args.plan_dir)
        self.assertTrue(args.tail)
        self.assertTrue(args.json)


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
            filename = plans.session_id_to_filename(session_id)
            label = plans.session_id_display_label(session_id)
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
            progress_file = plans.progress_path(plan_dir)
            self.assertFalse(progress_file.exists())

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertEqual("rewrite", result.action)
            self.assertEqual(progress_file, result.path)
            self.assertEqual(2, result.entries_written)
            self.assertEqual(("1", "2"), result.completed_session_ids)
            self.assertFalse(result.journal_replaced)
            entries = plans.read_progress_entries(plan_dir)
            self.assertEqual(
                ["1", "2"],
                [entry.session_id.value for entry in entries],
            )
            self.assertEqual(("file-1.py",), entries[0].files_modified)

    def test_repair_replaces_header_only_journal_with_full_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            plans.write_progress_journal(plan_dir, ())
            self.assertEqual(
                plans.PROGRESS_HEADER,
                plans.progress_path(plan_dir).read_text(encoding="utf-8"),
            )

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertTrue(result.journal_replaced)
            self.assertEqual(2, result.entries_written)
            self.assertEqual(("1", "2"), result.completed_session_ids)
            self.assertEqual(
                ["1", "2"],
                [
                    entry.session_id.value
                    for entry in plans.read_progress_entries(plan_dir)
                ],
            )

    def test_repair_overwrites_stale_journal_with_authoritative_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            stale_only = (
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("1"),
                    status="complete",
                    completed_at="2020-01-01T00:00:00Z",
                    files_modified=("stale.py",),
                    key_decisions="stale decision",
                    discovered_facts=(),
                    open_questions=(),
                    blockers=None,
                ),
            )
            plans.write_progress_journal(plan_dir, stale_only)

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertTrue(result.journal_replaced)
            self.assertEqual(2, result.entries_written)
            self.assertEqual(("1", "2"), result.completed_session_ids)
            entries = plans.read_progress_entries(plan_dir)
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
            progress_file = plans.progress_path(plan_dir)
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
                plans.PROGRESS_SCHEMA_COMMENT,
                progress_file.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                ("1",),
                tuple(
                    entry.session_id.value
                    for entry in plans.read_progress_entries(plan_dir)
                ),
            )

    def test_repair_writes_header_only_when_no_completed_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"), complete_ids=())
            progress_file = plans.progress_path(plan_dir)
            self.assertFalse(progress_file.exists())

            result = progress_repair.repair_progress_journal(plan_dir)

            self.assertEqual(0, result.entries_written)
            self.assertEqual((), result.completed_session_ids)
            self.assertFalse(result.journal_replaced)
            self.assertEqual(
                plans.PROGRESS_HEADER,
                progress_file.read_text(encoding="utf-8"),
            )
            self.assertEqual((), plans.read_progress_entries(plan_dir))

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
            snapshot = plans.inspect_plan(plan_dir)

            with mock.patch(
                "agent_session_loop.progress_repair.plans.inspect_plan"
            ) as inspect:
                result = progress_repair.repair_progress_journal(
                    plan_dir, snapshot=snapshot
                )

            inspect.assert_not_called()
            self.assertEqual(1, result.entries_written)
            self.assertEqual(("1",), result.completed_session_ids)


if __name__ == "__main__":
    unittest.main()
