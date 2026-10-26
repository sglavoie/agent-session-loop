"""Plan document lint and heading normalization tests.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    authoring_lint,
    authoring_parse,
    authoring_render,
    authoring_source_models,
    plans_errors,
)
from tests.asl_authoring_support import PlanAuthoringBase


class PlanLintTests(PlanAuthoringBase):
    def test_lint_plan_document_accepts_a_clean_plan(self) -> None:
        result = authoring_lint.lint_plan_document(
            "# Plan\n\n## Session 1: A\n\nx\n\n## Session 2: B\n\ny\n"
        )
        self.assertEqual((), result.findings)
        self.assertEqual(("1", "2"), result.session_ids)

    def test_lint_plan_document_carries_valid_registry(self) -> None:
        result = authoring_lint.lint_plan_document(
            "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
            "| SVC | svc | https://example.com/svc.git |\n\n"
            "## Session 1: A\n\n**Reads repositories**: SVC\n"
            "**Writes repositories**: SVC\n\nx\n\n"
            "## Session 2: B\n\n**Reads repositories**: SVC\n"
            "**Writes repositories**: (none)\n\ny\n"
        )
        self.assertEqual((), result.findings)
        self.assertIsNotNone(result.registry)
        self.assertEqual(("SVC",), result.registry.aliases)

    def test_multi_repo_session_scope_errors_are_line_specific(self) -> None:
        prefix = (
            "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
            "| SVC | svc | https://example.com/svc.git |\n"
            "| OAK | oak | https://example.com/oak.git |\n\n"
            "## Session 1: A\n\n"
        )
        cases = (
            ("**Writes repositories**: SVC\n", "missing exact 'Reads repositories'"),
            (
                "**Reads repositories**: SVC\n**Reads repositories**: OAK\n"
                "**Writes repositories**: SVC\n",
                "duplicate exact 'Reads repositories'",
            ),
            (
                "**Reads repositories**: (none)\n**Writes repositories**: (none)\n",
                "allowed only by itself for writes",
            ),
            (
                "**Reads repositories**: SVC\n**Writes repositories**: OAK\n",
                "must be a subset",
            ),
            (
                "**Reads repositories**: Missing\n**Writes repositories**: (none)\n",
                "unknown repository aliases",
            ),
        )
        for body, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(
                    authoring_source_models.SourceScopeError
                ) as raised:
                    authoring_parse.parse_plan_document(prefix + body)
                self.assertEqual(
                    authoring_source_models.LINT_INVALID_SESSION_SCOPE,
                    raised.exception.category,
                )
                self.assertIn(message, str(raised.exception))

    def test_lint_plan_document_categorizes_every_malformed_heading(self) -> None:
        cases = (
            (
                "## Session #1: Bad id",
                authoring_source_models.LINT_NON_NUMERIC_SESSION_ID,
            ),
            (
                "## Session 1..2: Bad id",
                authoring_source_models.LINT_INVALID_SESSION_ID,
            ),
            ("## Session 0: Zero", authoring_source_models.LINT_ZERO_ID_COMPONENT),
            ("## Session 1.0: Zero", authoring_source_models.LINT_ZERO_ID_COMPONENT),
            ("## Session 1a: Glued", authoring_source_models.LINT_SESSION_ID_SUFFIX),
            ("## Session1: Glued", authoring_source_models.LINT_MISSING_ID_WHITESPACE),
            ("## Session:2: Colon", authoring_source_models.LINT_MISSING_ID_WHITESPACE),
            ("## Session 1:", authoring_source_models.LINT_DANGLING_TITLE_SEPARATOR),
            (
                "## Session 1 -   ###",
                authoring_source_models.LINT_DANGLING_TITLE_SEPARATOR,
            ),
            (
                "## Session 1 / Unsupported",
                authoring_source_models.LINT_UNSUPPORTED_TITLE_SEPARATOR,
            ),
            (
                "## Session 1 Missing punctuation",
                authoring_source_models.LINT_PUNCTUATION_FREE_TITLE,
            ),
        )
        for source, category in cases:
            with self.subTest(source=source):
                result = authoring_lint.lint_plan_document("context\n" + source + "\n")
                self.assertEqual(1, len(result.findings))
                finding = result.findings[0]
                self.assertEqual(category, finding.category)
                self.assertEqual(2, finding.line)
                self.assertEqual(source, finding.source_line)
                self.assertIn("line 2", finding.message)

    def test_lint_plan_document_accumulates_instead_of_failing_fast(self) -> None:
        text = (
            "# Plan\n\n"
            "## Session 1 No colon\n\none\n\n"
            "### Session 2 / Bad sep\n\ntwo\n\n"
            "## Session 3a: Glued\n\nthree\n"
        )
        with self.assertRaises(plans_errors.PlanError):
            authoring_parse.parse_plan_document(text)

        result = authoring_lint.lint_plan_document(text)
        self.assertEqual(
            [
                (3, authoring_source_models.LINT_PUNCTUATION_FREE_TITLE),
                (7, authoring_source_models.LINT_UNSUPPORTED_TITLE_SEPARATOR),
                (11, authoring_source_models.LINT_SESSION_ID_SUFFIX),
            ],
            [(finding.line, finding.category) for finding in result.findings],
        )
        self.assertEqual((), result.session_ids)

    def test_lint_plan_document_reports_duplicate_ids_as_findings(self) -> None:
        result = authoring_lint.lint_plan_document(
            "## Session 1: First\n\nx\n\n## Session 01: Padded dup\n\ny\n"
        )
        self.assertEqual(1, len(result.findings))
        finding = result.findings[0]
        self.assertEqual(
            authoring_source_models.LINT_DUPLICATE_SESSION_ID, finding.category
        )
        self.assertEqual(5, finding.line)
        self.assertIn("first defined on line 1", finding.message)
        self.assertIsNone(finding.suggested_line)
        self.assertEqual(("1",), result.session_ids)

    def test_out_of_order_sessions_are_refused_not_sorted(self) -> None:
        text = (
            "# Order\n\n## Session 2: Second\n\nDo b.\n\n"
            "## Session 1: First\n\nDo a.\n\n"
            "## Session 3: Third\n\nDo c.\n\n"
            "### Session 2.1: Late child\n\nDo d.\n"
        )

        result = authoring_lint.lint_plan_document(text)

        self.assertEqual(
            [
                (7, authoring_source_models.LINT_OUT_OF_ORDER_SESSION),
                (15, authoring_source_models.LINT_OUT_OF_ORDER_SESSION),
            ],
            [(finding.line, finding.category) for finding in result.findings],
        )
        self.assertIn("follows session '2' (line 3)", result.findings[0].message)
        self.assertIsNone(result.findings[0].suggested_line)
        with self.assertRaises(authoring_source_models.SourceHeadingError) as raised:
            authoring_parse.parse_plan_document(text)
        self.assertEqual(
            authoring_source_models.LINT_OUT_OF_ORDER_SESSION,
            raised.exception.category,
        )
        self.assertEqual(7, raised.exception.line_number)

    def test_lint_plan_document_ignores_headings_in_code_fences(self) -> None:
        result = authoring_lint.lint_plan_document(
            "## Session 1: A\n\n```\n## Session 2 Broken\n```\n\n## Session 2: B\n\ny\n"
        )
        self.assertEqual((), result.findings)
        self.assertEqual(("1", "2"), result.session_ids)

    def test_lint_suggestions_only_cover_unambiguous_repairs(self) -> None:
        suggestable = {
            "## Session 1 Missing punctuation": "## Session 1: Missing punctuation",
            "## Session 2 / Unsupported": "## Session 2: Unsupported",
            # Original spacing and any ATX closing marker are preserved.
            "##  Session   3 No colon ##": "##  Session   3: No colon ##",
        }
        for source, expected in suggestable.items():
            with self.subTest(source=source):
                finding = authoring_lint.lint_plan_document(source + "\n").findings[0]
                self.assertEqual(expected, finding.suggested_line)

        # A repair that would invent an id, renumber, or drop a title is left
        # to a human decision, so no suggestion is offered.
        for source in (
            "## Session 1a: Glued",
            "## Session 1.0: Zero",
            "## Session 1:",
            "## Session #1: Bad id",
            "## Session1: Glued",
            "## Session 1 /",
        ):
            with self.subTest(source=source):
                finding = authoring_lint.lint_plan_document(source + "\n").findings[0]
                self.assertIsNone(finding.suggested_line)

    def test_headings_whose_next_word_is_not_an_id_are_prose(self) -> None:
        prose = (
            "## Session plan",
            "### Session cookies",
            "#### Session-based auth notes",
            "## Session",
            "## Session: overview",
            "## Sessions 1 and 2",
            "## Session - 2024 recap",
        )
        text = (
            "# Plan\n\n"
            + "\n\n".join(prose[:2])
            + "\n\n## Session 1: Login\n\n"
            + "\n\n".join(prose[2:])
            + "\n\n## Session 2: Logout\n\nDo b.\n"
        )

        result = authoring_lint.lint_plan_document(text)
        parsed = authoring_parse.parse_plan_document(text)

        self.assertEqual((), result.findings)
        self.assertEqual(("1", "2"), parsed.session_ids)
        self.assertIn("### Session cookies", parsed.context_lines)
        for heading in prose[2:]:
            self.assertIn(heading, parsed.sessions[0].lines)

    def test_lint_suggestions_parse_when_applied(self) -> None:
        text = "## Session 1 No colon\n\none\n\n### Session 2 | Bad sep\n\ntwo\n"
        lines = text.splitlines()
        for finding in authoring_lint.lint_plan_document(text).findings:
            self.assertIsNotNone(finding.suggested_line)
            lines[finding.line - 1] = str(finding.suggested_line)

        repaired = "\n".join(lines) + "\n"
        self.assertEqual((), authoring_lint.lint_plan_document(repaired).findings)
        self.assertEqual(
            ("1", "2"), authoring_parse.parse_plan_document(repaired).session_ids
        )

    def test_lint_reveals_duplicates_masked_by_an_earlier_malformed_heading(
        self,
    ) -> None:
        # A duplicate id is invisible while the first heading fails to parse, so
        # a repair round can legitimately surface a new finding.
        text = "## Session 1 No colon\n\none\n\n## Session 1: Dup\n\ntwo\n"
        first = authoring_lint.lint_plan_document(text)
        self.assertEqual(
            [authoring_source_models.LINT_PUNCTUATION_FREE_TITLE],
            [finding.category for finding in first.findings],
        )

        lines = text.splitlines()
        lines[0] = str(first.findings[0].suggested_line)
        second = authoring_lint.lint_plan_document("\n".join(lines) + "\n")
        self.assertEqual(
            [authoring_source_models.LINT_DUPLICATE_SESSION_ID],
            [finding.category for finding in second.findings],
        )

    def test_lint_and_parse_agree_on_every_corpus_document(self) -> None:
        corpus = (
            "## Session 1: A\n\nx\n\n## Session 2: B\n\ny\n",
            "no sessions at all\n",
            "## Session 1: Only\n\nx\n",
            "## Session 1 No colon\n\nx\n\n## Session 2: B\n\ny\n",
            "## Session 1: A\n\nx\n\n## Session 1: Dup\n\ny\n",
            "## Session 1: A\n\n```\n## Session 2 Broken\n```\n\n"
            "## Session 2: B\n\ny\n",
            "## Session 0: Zero\n\nx\n",
        )
        for text in corpus:
            with self.subTest(text=text):
                result = authoring_lint.lint_plan_document(text)
                try:
                    parsed = authoring_parse.parse_plan_document(text)
                except plans_errors.PlanError as error:
                    self.assertTrue(result.findings)
                    self.assertIn(f"line {result.findings[0].line}:", str(error))
                    self.assertEqual(result.findings[0].message, str(error))
                else:
                    self.assertEqual((), result.findings)
                    self.assertEqual(parsed.session_ids, result.session_ids)

    def test_normalize_heading_levels_promotes_level_three(self) -> None:
        body = ["#### Changes", "prose", "##### Deep"]
        self.assertEqual(
            ["## Changes", "prose", "### Deep"],
            authoring_render.normalize_heading_levels(body, 3),
        )

    def test_normalize_heading_levels_preserves_level_two(self) -> None:
        body = ["### Changes", "prose"]
        self.assertEqual(body, authoring_render.normalize_heading_levels(body, 2))

    def test_normalize_heading_levels_skips_code_fences(self) -> None:
        body = ["#### Real", "```", "#### fenced", "```"]
        self.assertEqual(
            ["## Real", "```", "#### fenced", "```"],
            authoring_render.normalize_heading_levels(body, 3),
        )

    def test_normalize_heading_levels_keeps_long_fence_open_past_short_run(
        self,
    ) -> None:
        body = ["````", "```", "#### fenced", "```", "````", "#### Real"]
        self.assertEqual(
            ["````", "```", "#### fenced", "```", "````", "## Real"],
            authoring_render.normalize_heading_levels(body, 3),
        )


if __name__ == "__main__":
    unittest.main()
