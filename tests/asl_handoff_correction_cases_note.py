"""Handoff-correction tests: note."""

from __future__ import annotations

import inspect
import unittest

from agent_session_loop import (
    handoff_correction,
)


class AppendHandoffNoteTests(unittest.TestCase):
    """The pure line-list transform all three repairs render their note through."""

    def test_an_existing_notes_bullet_gains_the_note_at_its_end(self) -> None:
        lines = ["## Handoff", "", "- **Notes**:", "  - first", "", "## Next"]

        rewritten = handoff_correction._append_handoff_note(
            lines, handoff_index=0, section_end=5, note="second"
        )

        self.assertEqual(
            ["## Handoff", "", "- **Notes**:", "  - first", "  - second", ""],
            rewritten[:6],
        )

    def test_a_missing_notes_bullet_is_created_inside_the_section(self) -> None:
        lines = ["## Handoff", "", "- **Status**: pending", "", "## Next"]

        rewritten = handoff_correction._append_handoff_note(
            lines, handoff_index=0, section_end=4, note="reopened"
        )

        self.assertEqual(
            [
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Notes**:",
                "  - reopened",
                "",
                "## Next",
            ],
            rewritten,
        )

    def test_the_note_never_lands_past_the_section_end(self) -> None:
        # The bound is the whole point: a bullet list that continues past the
        # handoff heading belongs to the next section, and a note appended to
        # its end would land in the next session's instructions. `section_end`
        # stops the scan at the heading, so the note goes in above it.
        lines = ["## Handoff", "- **Notes**:", "  - first", "## Next", "  - decoy"]

        rewritten = handoff_correction._append_handoff_note(
            lines, handoff_index=0, section_end=3, note="second"
        )

        self.assertEqual(
            [
                "## Handoff",
                "- **Notes**:",
                "  - first",
                "  - second",
                "## Next",
                "  - decoy",
            ],
            rewritten,
        )

    def test_the_transform_touches_no_file(self) -> None:
        source = inspect.getsource(handoff_correction._append_handoff_note)
        for call in ("open(", "write_text(", "Path("):
            with self.subTest(call=call):
                self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
