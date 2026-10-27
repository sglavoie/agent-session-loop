"""Tests for test asl commit message format."""

from __future__ import annotations

import unittest

from agent_session_loop import commit_message


class ApiErrorGuardTests(unittest.TestCase):
    def test_api_error_prefix_is_rejected(self) -> None:
        self.assertTrue(commit_message.looks_like_api_error("API Error: 500"))
        self.assertTrue(
            commit_message.looks_like_api_error(
                '  API Error: 500 {"type":"error","error":{"type":"api_error"}}\n'
            )
        )

    def test_bare_error_object_is_rejected(self) -> None:
        self.assertTrue(commit_message.looks_like_api_error('{"type": "error"}'))
        self.assertTrue(
            commit_message.looks_like_api_error(
                'Request failed: {"type": "error", "error": {"message": "overloaded"}}'
            )
        )

    def test_a_normal_message_containing_braces_is_accepted(self) -> None:
        message = (
            "fix: escape the {placeholder} braces in the template\n"
            "\n"
            'The renderer choked on {"type": "config"} literals in the body.\n'
        )

        self.assertFalse(commit_message.looks_like_api_error(message))

    def test_a_conventional_message_whose_body_ends_in_an_error_object_is_accepted(
        self,
    ) -> None:
        message = (
            "fix: Surface upstream failures as error envelopes\n"
            "\n"
            "Wrap every upstream failure in the error envelope clients parse:\n"
            '{"type": "error", "error": {"message": "upstream unavailable"}}'
        )

        self.assertFalse(commit_message.looks_like_api_error(message))

    def test_an_error_object_behind_a_title_shaped_prefix_is_rejected(self) -> None:
        self.assertTrue(
            commit_message.looks_like_api_error(
                'error: {"type": "error", "error": {"message": "overloaded"}}'
            )
        )
        self.assertTrue(
            commit_message.looks_like_api_error(
                'error: {\n  "type": "error",\n  "error": {"message": "overloaded"}\n}'
            )
        )

    def test_non_error_json_and_unparseable_braces_are_accepted(self) -> None:
        self.assertFalse(commit_message.looks_like_api_error('{"type": "result"}'))
        self.assertFalse(commit_message.looks_like_api_error("feat: add {"))
        self.assertFalse(commit_message.looks_like_api_error(""))


class TrailerStrippingTests(unittest.TestCase):
    def test_attribution_trailers_are_dropped_with_their_blank_lines(self) -> None:
        text = (
            "fix: correct the retry ceiling\n"
            "\n"
            "body\n"
            "\n"
            "Co-authored-by: Someone <someone@example.com>\n"
            "Signed-off-by: Someone Else <else@example.com>\n"
        )

        self.assertEqual(
            commit_message.strip_disallowed_trailers(text),
            "fix: correct the retry ceiling\n\nbody",
        )

    def test_trailers_match_case_insensitively_and_through_indentation(self) -> None:
        text = "fix: x\n\n  co-authored-by: A <a@example.com>\n\tSIGNED-OFF-BY: B\n"

        self.assertEqual(commit_message.strip_disallowed_trailers(text), "fix: x")

    def test_a_body_line_merely_mentioning_a_trailer_survives(self) -> None:
        text = "fix: x\n\nDrop the Co-authored-by handling from the formatter path.\n"

        self.assertEqual(
            commit_message.strip_disallowed_trailers(text),
            "fix: x\n\nDrop the Co-authored-by handling from the formatter path.",
        )


class ExtractCommitMessageTests(unittest.TestCase):
    def test_preamble_before_the_first_title_is_stripped(self) -> None:
        text = (
            "I've read the diff and here is the message.\n"
            "\n"
            "Here goes:\n"
            "\n"
            "feat: add the native commit generator\n"
            "\n"
            "body\n"
        )

        self.assertEqual(
            commit_message.extract_commit_message(text),
            "feat: add the native commit generator\n\nbody",
        )

    def test_scoped_and_breaking_titles_are_recognized(self) -> None:
        for title in ("fix(cli): x", "feat!: x", "refactor(a/b)!: x"):
            with self.subTest(title=title):
                self.assertEqual(
                    commit_message.extract_commit_message(f"preamble\n{title}\n"),
                    title,
                )

    def test_text_without_a_title_falls_back_to_the_whole_response(self) -> None:
        text = "no conventional title anywhere\nsecond line\n"

        self.assertEqual(
            commit_message.extract_commit_message(text),
            "no conventional title anywhere\nsecond line",
        )

    def test_the_fallback_still_strips_trailers(self) -> None:
        text = "no title here\n\nCo-authored-by: A <a@example.com>\n"

        self.assertEqual(commit_message.extract_commit_message(text), "no title here")


class LineFormattingTests(unittest.TestCase):
    def test_detect_line_type_classifies_each_shape(self) -> None:
        self.assertEqual(commit_message.detect_line_type("   "), ("blank", "   "))
        self.assertEqual(
            commit_message.detect_line_type("plain prose"), ("paragraph", "plain prose")
        )
        for line, expected in (
            ("- dash", ("", "-", "dash")),
            ("  * star", ("  ", "*", "star")),
            ("• unicode", ("", "•", "unicode")),
            ("  10. numbered", ("  ", "10.", "numbered")),
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    commit_message.detect_line_type(line), ("bullet", expected)
                )

    def test_a_marker_without_trailing_space_is_not_a_bullet(self) -> None:
        self.assertEqual(
            commit_message.detect_line_type("-nospace"), ("paragraph", "-nospace")
        )

    def test_format_bullet_hangs_continuations_under_the_text_column(self) -> None:
        formatted = commit_message.format_bullet(
            "  ", "-", "alpha " * 30, width=40
        ).split("\n")

        self.assertTrue(formatted[0].startswith("  - alpha"))
        # "  - " is four columns wide, so every continuation starts at column 4.
        for line in formatted[1:]:
            self.assertTrue(line.startswith("    alpha"), line)
        self.assertTrue(all(len(line) <= 40 for line in formatted))

    def test_format_bullet_aligns_wider_markers_to_their_own_width(self) -> None:
        formatted = commit_message.format_bullet(
            "", "10.", "beta " * 20, width=40
        ).split("\n")

        self.assertTrue(formatted[0].startswith("10. beta"))
        for line in formatted[1:]:
            self.assertTrue(line.startswith("    beta"), line)

    def test_format_paragraph_inherits_its_own_leading_indent(self) -> None:
        formatted = commit_message.format_paragraph("    gamma " * 12, width=40)

        for line in formatted.split("\n"):
            self.assertTrue(line.startswith("    gamma"), line)

    def test_format_paragraph_honours_an_explicit_indent(self) -> None:
        formatted = commit_message.format_paragraph(
            "delta " * 12, width=40, initial_indent=">> ", subsequent_indent="   "
        ).split("\n")

        self.assertTrue(formatted[0].startswith(">> delta"))
        for line in formatted[1:]:
            self.assertTrue(line.startswith("   delta"), line)

    def test_wrapping_never_breaks_a_long_word_or_a_hyphenated_one(self) -> None:
        url = "https://example.com/a/very/long/path/that/exceeds/the/wrap/width"

        formatted = commit_message.format_paragraph(f"see {url} ok", width=40)

        self.assertIn(url, formatted.split("\n")[1])


class FormatCommitMessageTests(unittest.TestCase):
    # A message exercising every branch at once: preamble, an over-long title,
    # a paragraph needing a re-wrap, mixed bullet markers, a bullet
    # continuation line, preserved blank lines, and an attribution trailer.
    REPRESENTATIVE = (
        "I've read the diff and here is the commit message you asked for.\n"
        "\n"
        "feat(commit): add a native commit message generator that no longer "
        "shells out to the retired spec binary\n"
        "\n"
        "Replace the external spec commit dependency with an in-tree "
        "implementation so a\n"
        "run no longer dies on a bare exit 127 when the binary disappears. "
        "The runner and the gitsummary skill now share one code path.\n"
        "\n"
        "Collapse the three copies of the logic:\n"
        "\n"
        "  - Port the diff budgeting from the skill so truncation stays "
        "byte-identical\n"
        "    across both callers.\n"
        "- Port the formatter and add the API-error guard the skill copy "
        "never had.\n"
        "  1. Keep the Conventional Commits rules and the type table intact.\n"
        "\n"
        "Commit failures now soft-fail instead of pausing the run.\n"
        "\n"
        "Co-authored-by: Someone Else <someone@example.com>\n"
    )

    EXPECTED = (
        "feat(commit): add a native commit message generator that no longer "
        "shells out to the retired spec binary\n"
        "\n"
        "Replace the external spec commit dependency with an in-tree "
        "implementation so a\n"
        "run no longer dies on a bare exit 127 when the binary disappears. "
        "The runner and\n"
        "the gitsummary skill now share one code path.\n"
        "\n"
        "Collapse the three copies of the logic:\n"
        "\n"
        "  - Port the diff budgeting from the skill so truncation stays "
        "byte-identical\n"
        "    across both callers.\n"
        "- Port the formatter and add the API-error guard the skill copy "
        "never had.\n"
        "  1. Keep the Conventional Commits rules and the type table intact.\n"
        "\n"
        "Commit failures now soft-fail instead of pausing the run."
    )

    def test_representative_message_matches_the_ported_output(self) -> None:
        self.assertEqual(
            commit_message.format_commit_message(self.REPRESENTATIVE), self.EXPECTED
        )

    def test_title_is_kept_verbatim_while_the_body_wraps_at_eighty(self) -> None:
        lines = commit_message.format_commit_message(self.REPRESENTATIVE).split("\n")

        title = lines[0]
        self.assertEqual(
            title,
            "feat(commit): add a native commit message generator that no "
            "longer shells out to the retired spec binary",
        )
        self.assertGreater(len(title), 80)
        for line in lines[1:]:
            self.assertLessEqual(len(line), 80, line)

    def test_bullet_hanging_indents_align_to_the_text_column(self) -> None:
        lines = commit_message.format_commit_message(self.REPRESENTATIVE).split("\n")

        self.assertIn(
            "  - Port the diff budgeting from the skill so truncation stays "
            "byte-identical",
            lines,
        )
        # The continuation was a separate source line; it is re-joined and
        # hung under the bullet's text column rather than left as written.
        self.assertIn("    across both callers.", lines)

    def test_blank_lines_between_sections_are_preserved(self) -> None:
        lines = commit_message.format_commit_message(self.REPRESENTATIVE).split("\n")

        # One after the title, one after the re-wrapped paragraph, one before
        # the bullet list, and one before the closing paragraph.
        self.assertEqual(
            [index for index, line in enumerate(lines) if not line], [1, 5, 7, 12]
        )

    def test_trailing_blank_lines_are_dropped(self) -> None:
        self.assertEqual(
            commit_message.format_commit_message("docs: update\n\nbody\n\n\n\n"),
            "docs: update\n\nbody",
        )

    def test_the_width_is_configurable(self) -> None:
        message = "fix: x\n\n" + "word " * 30

        wrapped = commit_message.format_commit_message(message, width=40)

        for line in wrapped.split("\n")[1:]:
            self.assertLessEqual(len(line), 40, line)

    def test_extract_false_keeps_everything_above_a_later_title_shaped_line(
        self,
    ) -> None:
        text = (
            "Bump the pinned dependencies\n"
            "\n"
            "The lockfile drifted.\n"
            "\n"
            "note: pins lxml below 6 until upstream ships a wheel.\n"
            "\n"
            "Signed-off-by: Someone <a@b.c>\n"
        )

        self.assertEqual(
            commit_message.format_commit_message(text, extract=False),
            "Bump the pinned dependencies\n"
            "\n"
            "The lockfile drifted.\n"
            "\n"
            "note: pins lxml below 6 until upstream ships a wheel.",
        )


if __name__ == "__main__":
    unittest.main()
