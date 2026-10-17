"""Tests for test asl commit message prompt."""

from __future__ import annotations

import unittest

from agent_session_loop import commit_message
from tests import support_asl_commit_message as fixtures


class TruncationNoteTests(unittest.TestCase):
    def test_an_untruncated_diff_gets_no_note(self) -> None:
        result = commit_message.DiffResult(
            diff="x", files_included=["a.py"], is_truncated=False
        )

        self.assertEqual(commit_message.build_truncation_note(result), "")

    def test_the_note_counts_each_reduction_kind(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            binary_files=["a.png", "b.png"],
            truncated_files=["big.py", "bigger.py", "biggest.py"],
            omitted_files=["c.py", "d.py"],
            is_truncated=True,
        )

        note = commit_message.build_truncation_note(result)

        self.assertIn("2 binary file(s)", note)
        self.assertIn("3 file(s) partially shown", note)
        self.assertIn("2 file(s) omitted: c.py, d.py.", note)
        self.assertIn("focus on what IS visible", note)

    def test_the_omitted_list_caps_at_five_names_plus_a_remainder(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            omitted_files=[f"src/mod_{index}.py" for index in range(8)],
            is_truncated=True,
        )

        note = commit_message.build_truncation_note(result)

        self.assertIn(
            "8 file(s) omitted: src/mod_0.py, src/mod_1.py, src/mod_2.py, "
            "src/mod_3.py, src/mod_4.py, ... (+3 more)",
            note,
        )
        self.assertNotIn("src/mod_5.py", note)

    def test_the_note_reports_only_the_kinds_that_apply(self) -> None:
        result = commit_message.DiffResult(
            diff="x", binary_files=["a.png"], is_truncated=True
        )

        note = commit_message.build_truncation_note(result)

        self.assertIn("size limits. 1 binary file(s).", note)
        self.assertNotIn("partially shown", note)
        self.assertNotIn("file(s) omitted", note)

    def test_the_prompt_note_caps_looser_than_the_header_notice(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            omitted_files=[f"m{index}.py" for index in range(5)],
            is_truncated=True,
        )

        # Five names fit the prompt note whole; the header notice keeps three.
        self.assertIn("m4.py", commit_message.build_truncation_note(result))
        self.assertIn("(+2 more)", commit_message.build_truncation_notice(result))


class CommitPromptTests(unittest.TestCase):
    def build_result(self, **overrides: object) -> commit_message.DiffResult:
        fields: dict[str, object] = {
            "diff": fixtures.build_file_diff("src/alpha.py", hunks=1, lines=2),
            "files_included": ["src/alpha.py"],
        }
        fields.update(overrides)
        return commit_message.DiffResult(**fields)  # type: ignore[arg-type]

    def test_the_prompt_carries_the_rules_the_type_table_and_the_diff(self) -> None:
        result = self.build_result()

        prompt = commit_message.build_commit_prompt(result)

        self.assertTrue(prompt.startswith("You are a commit message generator."))
        self.assertIn("Conventional Commits specification", prompt)
        self.assertIn("## Format Requirements", prompt)
        self.assertIn("## Content Rules", prompt)
        for commit_type in (
            "feat",
            "fix",
            "docs",
            "refactor",
            "test",
            "chore",
            "perf",
            "style",
        ):
            with self.subTest(commit_type=commit_type):
                self.assertIn(f"\n- {commit_type}: ", prompt)
        self.assertIn("Keep title under 72 characters", prompt)
        self.assertIn("Wrap body at 80 characters", prompt)
        self.assertIn('No author attribution (no "Co-authored-by:"', prompt)
        self.assertIn("No markdown syntax", prompt)
        self.assertIn("Use PRESENT TENSE", prompt)
        self.assertIn("Focus on WHAT changed and WHY it matters", prompt)
        self.assertIn("Do NOT describe HOW the code works", prompt)
        self.assertTrue(prompt.endswith("Generate the commit message:"))

    def test_the_diff_is_inlined_under_its_own_heading(self) -> None:
        result = self.build_result()

        prompt = commit_message.build_commit_prompt(result)

        self.assertIn(f"## Git Diff\n\n{result.diff}\n", prompt)
        self.assertNotIn("Read tool", prompt)

    def test_guidance_appears_in_its_own_section_when_supplied(self) -> None:
        prompt = commit_message.build_commit_prompt(
            self.build_result(), guidance="Mention the ticket number ABC-123."
        )

        self.assertIn(
            "\n## User Guidance\n\nMention the ticket number ABC-123.\n", prompt
        )

    def test_no_guidance_section_without_guidance(self) -> None:
        for guidance in (None, ""):
            with self.subTest(guidance=guidance):
                prompt = commit_message.build_commit_prompt(
                    self.build_result(), guidance=guidance
                )

                self.assertNotIn("User Guidance", prompt)

    def test_the_truncation_note_is_embedded_only_when_truncated(self) -> None:
        clean = commit_message.build_commit_prompt(self.build_result())
        self.assertNotIn("truncated to fit within size limits", clean)

        truncated_result = self.build_result(
            truncated_files=["src/alpha.py"], is_truncated=True
        )
        truncated = commit_message.build_commit_prompt(truncated_result)
        self.assertIn(commit_message.build_truncation_note(truncated_result), truncated)
        self.assertIn("1 file(s) partially shown", truncated)

    def test_the_dropped_spec_couplings_are_absent(self) -> None:
        prompt = commit_message.build_commit_prompt(self.build_result())

        # The feature-name context note and the spec-dir large-content branch
        # are the two couplings the port drops; only their type-table and
        # content-rule neighbours survive.
        self.assertNotIn("This change is part of the", prompt)
        self.assertNotIn("do NOT mention the feature name", prompt)
        self.assertNotIn(".spec", prompt)


if __name__ == "__main__":
    unittest.main()
