# ruff: noqa: F401, F403, F405, I001
"""Handoff progress append and session-revert tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_handoff_guards_test_support import *


class AppendProgressEntryTests(_PlanCase):
    """The one durable append, and the crash it has to survive twice."""

    def test_a_verified_session_gets_one_journal_row(self) -> None:
        self.assertTrue(
            handoff_guards._append_progress_entry(
                self.snapshot(), "1", "2026-01-01T00:00:00Z"
            )
        )
        entries = plans_progress.read_progress_entries(self.plan_dir)
        self.assertEqual(["1"], [str(entry.session_id) for entry in entries])
        self.assertEqual("2026-01-01T00:00:00Z", entries[0].completed_at)

    def test_repeating_the_same_completion_is_a_no_op(self) -> None:
        # A run that crashed after writing the row and before recording that it
        # had would otherwise publish the session twice, and the journal is what
        # every later session reads as the record of what has been done.
        stamp = "2026-01-01T00:00:00Z"
        self.assertTrue(
            handoff_guards._append_progress_entry(self.snapshot(), "1", stamp)
        )
        self.assertFalse(
            handoff_guards._append_progress_entry(self.snapshot(), "1", stamp)
        )
        self.assertEqual(1, len(plans_progress.read_progress_entries(self.plan_dir)))

    def test_a_second_completion_at_a_different_time_is_a_new_row(self) -> None:
        # Idempotence is keyed on the pair, not the session: a session reopened
        # by a guard and completed again is a genuinely new row.
        self.assertTrue(
            handoff_guards._append_progress_entry(
                self.snapshot(), "1", "2026-01-01T00:00:00Z"
            )
        )
        self.assertTrue(
            handoff_guards._append_progress_entry(
                self.snapshot(), "1", "2026-01-02T00:00:00Z"
            )
        )
        self.assertEqual(2, len(plans_progress.read_progress_entries(self.plan_dir)))

    def test_the_inline_files_modified_bullet_is_split_on_commas(self) -> None:
        path = self.session_path("1")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "- **Files modified**: (none)",
                "- **Files modified**: a.py, b.py ,  c.py",
            ),
            encoding="utf-8",
        )
        handoff_guards._append_progress_entry(
            self.snapshot(), "1", "2026-01-01T00:00:00Z"
        )
        entry = plans_progress.read_progress_entries(self.plan_dir)[0]
        self.assertEqual(("a.py", "b.py", "c.py"), entry.files_modified)

    def test_a_session_absent_from_the_snapshot_raises_rather_than_writes(self) -> None:
        # The caller records the failure as `progress-journal-append-failed` and
        # carries on; a silent no-op would leave the journal short a row with
        # nothing anywhere saying so.
        with self.assertRaises(plans_errors.PlanError):
            handoff_guards._append_progress_entry(
                self.snapshot(), "99", "2026-01-01T00:00:00Z"
            )
        self.assertEqual((), plans_progress.read_progress_entries(self.plan_dir))


class RevertSessionStatusTests(_PlanCase):
    """The one way a ``complete`` claim is taken back."""

    def revert(
        self, session_id: str = "1", **overrides: Any
    ) -> handoff_guards.SessionRevert | None:
        return handoff_guards._revert_session_status_to_pending(
            self.snapshot(),
            session_id,
            failure_note=overrides.pop("failure_note", "guard refused it"),
            append_handoff_note=self.appender,
        )

    def test_the_status_line_goes_back_to_pending(self) -> None:
        reverted = self.revert()
        assert reverted is not None
        self.assertEqual(self.session_path("1"), reverted.path)
        self.assertIn(
            f"{plans_handoff.STATUS_PREFIX} pending",
            reverted.path.read_text(encoding="utf-8"),
        )
        self.assertEqual("pending", self.snapshot().sessions[0].status)

    def test_a_complete_session_is_reported_as_reopened(self) -> None:
        reverted = self.revert("1")
        assert reverted is not None
        self.assertEqual("complete", reverted.from_status)
        self.assertTrue(reverted.reopened)
        self.assertTrue(handoff_guards._reopened(reverted))

    def test_an_already_pending_session_gets_the_note_but_is_not_reopened(
        self,
    ) -> None:
        # ASI-30: the note is still the record of why the guard refused, but a
        # session that never left pending was not reopened, and saying so
        # emits a session_reopened transition that never happened.
        reverted = self.revert("2", failure_note="guard refused a pending turn")
        assert reverted is not None
        self.assertEqual(self.session_path("2"), reverted.path)
        self.assertEqual("pending", reverted.from_status)
        self.assertFalse(reverted.reopened)
        self.assertFalse(handoff_guards._reopened(reverted))
        self.assertFalse(handoff_guards._reopened(None))
        self.assertEqual(
            ["guard refused a pending turn"],
            [call["note"] for call in self.appender.calls],
        )

    def test_the_note_is_offered_the_bounds_of_the_handoff_section(self) -> None:
        self.revert(failure_note="plan artifact guard failed")
        self.assertEqual(1, len(self.appender.calls))
        call = self.appender.calls[0]
        lines = self.session_path("1").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            plans_handoff.HANDOFF_HEADING, lines[call["handoff_index"]].strip()
        )
        self.assertLessEqual(call["section_end"], len(lines))
        self.assertLess(call["handoff_index"], call["section_end"])
        self.assertEqual("plan artifact guard failed", call["note"])

    def test_the_trailing_newline_of_the_session_file_is_preserved(self) -> None:
        # The file is rewritten wholesale, and a lost trailing newline shows up
        # as a diff on every session the run touches afterwards.
        original = self.session_path("1").read_text(encoding="utf-8")
        self.assertTrue(original.endswith("\n"))
        self.revert()
        self.assertTrue(
            self.session_path("1").read_text(encoding="utf-8").endswith("\n")
        )

    def test_a_session_absent_from_the_snapshot_reverts_nothing(self) -> None:
        self.assertIsNone(self.revert("99"))
        self.assertEqual([], self.appender.calls)

    def test_a_session_file_without_a_handoff_heading_reverts_nothing(self) -> None:
        # The caller reads the `None` as "the snapshot you hold is still
        # accurate", so returning a path here would republish an unchanged plan
        # as if it had been reverted.
        path = self.session_path("1")
        path.write_text("# Session 1: Test\n\nInstructions.\n", encoding="utf-8")
        self.assertIsNone(self.revert())
        self.assertEqual([], self.appender.calls)

    def test_the_last_handoff_heading_wins(self) -> None:
        # A session whose body quotes an earlier `## Handoff` -- which the
        # extraction sessions' own files all do -- must still have its real
        # status line rewritten, not the quoted one.
        path = self.session_path("1")
        original = path.read_text(encoding="utf-8")
        path.write_text(
            "## Handoff\n\n- **Status**: complete\n\n## Body\n\n" + original,
            encoding="utf-8",
        )
        self.revert()
        text = path.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(f"{plans_handoff.STATUS_PREFIX} pending"))
        self.assertLess(
            text.index(f"{plans_handoff.STATUS_PREFIX} complete"),
            text.index(f"{plans_handoff.STATUS_PREFIX} pending"),
        )
