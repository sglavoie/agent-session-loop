# ruff: noqa: F401, F403, F405, I001
"""Progress CLI and backfill tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_progress_test_support import *


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
            filename = plans_session_ids.session_id_to_filename(session_id)
            label = plans_session_ids.session_id_display_label(session_id)
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

            entries = plans_progress.read_progress_entries(plan_dir)
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
            progress_file = plans_progress.progress_path(plan_dir)
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
            progress_file = plans_progress.progress_path(plan_dir)
            progress_file.write_text(
                "stale and definitely not v1 schema\n", encoding="utf-8"
            )

            code, out, err = self._run_backfill(plan_dir, force=True)

            self.assertEqual(0, code, err)
            entries = plans_progress.read_progress_entries(plan_dir)
            self.assertEqual(2, len(entries))

    def test_backfill_force_preserves_existing_journal_when_replace_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1", "2"))
            progress_file = plans_progress.progress_path(plan_dir)
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
            self.assertIn(plans_progress.PROGRESS_SCHEMA_COMMENT, out)
            self.assertIn("## Session 1 — complete", out)
            self.assertIn("## Session 2 — complete", out)
            self.assertFalse(plans_progress.progress_path(plan_dir).exists())

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
            journal = plans_progress.progress_path(plan_dir).read_text(encoding="utf-8")
            self.assertEqual(plans_progress.PROGRESS_HEADER, journal)
            self.assertEqual((), plans_progress.read_progress_entries(plan_dir))

    def test_progress_prints_existing_journal_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            self._write_completed_plan(plan_dir, ("1",))
            self._run_backfill(plan_dir)

            code, out, err = self._run_progress(plan_dir)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            self.assertEqual(
                plans_progress.progress_path(plan_dir).read_text(encoding="utf-8"),
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
            plans_progress.progress_path(plan_dir).write_text(
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
            initial_entry = plans_models.ProgressEntry(
                session_id=plans_session_ids.parse_session_id("1"),
                status="complete",
                completed_at="2026-06-19T15:00:00Z",
                files_modified=("first.py",),
                key_decisions=None,
                discovered_facts=(),
                open_questions=(),
                blockers=None,
            )
            plans_progress.append_progress_entry(plan_dir, initial_entry)

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
                second_entry = plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id("2"),
                    status="complete",
                    completed_at="2026-06-19T15:30:00Z",
                    files_modified=("second.py",),
                    key_decisions="kept inline",
                    discovered_facts=(),
                    open_questions=(),
                    blockers=None,
                )
                plans_progress.append_progress_entry(plan_dir, second_entry)
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
