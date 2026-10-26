"""Plan document parsing and repository registry tests.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import dataclasses
import json
import unittest

from agent_session_loop import (
    authoring_lint,
    authoring_parse,
    authoring_source_models,
    plans_errors,
)
from tests.asl_authoring_support import PlanAuthoringBase


class PlanParsingTests(PlanAuthoringBase):
    def test_parse_plan_document_splits_context_and_sessions(self) -> None:
        text = (
            "# Plan Title\n\nShared context.\n\n"
            "## Session 1: First\n\nDo one.\n\n"
            "### Session 2.1: Nested\n\nDo two.\n"
        )
        parsed = authoring_parse.parse_plan_document(text)

        self.assertIn("Shared context.", parsed.context_lines)
        self.assertEqual(("1", "2.1"), parsed.session_ids)
        self.assertEqual("First", parsed.sessions[0].title)
        self.assertEqual(2, parsed.sessions[0].heading_level)
        self.assertEqual(3, parsed.sessions[1].heading_level)
        self.assertEqual("Nested", parsed.sessions[1].title)

    def test_parse_plan_document_ignores_headings_in_code_fences(self) -> None:
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                text = (
                    "Context\n\n## Session 1: Real\n\n"
                    f"{fence}\n"
                    "###### Session 009 — fake\n"
                    "### Session nope / malformed\n"
                    f"{fence}\n\nBody.\n"
                )
                parsed = authoring_parse.parse_plan_document(text)

                self.assertEqual(("1",), parsed.session_ids)
                self.assertIn("###### Session 009 — fake", parsed.sessions[0].lines)
                self.assertIn("### Session nope / malformed", parsed.sessions[0].lines)

    def test_parse_plan_document_closes_fence_only_on_matching_run(self) -> None:
        text = (
            "# Plan\n\n## Session 1: Teach\n\n"
            "````markdown\n"
            "```text\n"
            "## Session 9: Example inside an example\n"
            "```\n"
            "## Session 8: Still quoted\n"
            "~~~~\n"
            "```python\n"
            "````\n\n"
            "## Session 2: Real work\n\nDo it.\n"
        )
        parsed = authoring_parse.parse_plan_document(text)

        self.assertEqual(("1", "2"), parsed.session_ids)
        self.assertIn(
            "## Session 9: Example inside an example", parsed.sessions[0].lines
        )
        self.assertIn("## Session 8: Still quoted", parsed.sessions[0].lines)

    def test_parse_plan_document_rejects_duplicate_ids(self) -> None:
        text = "## Session 1: A\n\nx\n\n## Session 01: B\n\ny\n"
        with self.assertRaises(plans_errors.PlanError) as raised:
            authoring_parse.parse_plan_document(text)
        message = str(raised.exception)
        self.assertIn("line 5", message)
        self.assertIn("duplicate canonical session id '1'", message)
        self.assertIn("first defined on line 1", message)
        self.assertIn("## Session 01: B", message)

    def test_parse_plan_document_titleless_heading(self) -> None:
        parsed = authoring_parse.parse_plan_document("## Session 3\n\nBody.\n")
        self.assertEqual(("3",), parsed.session_ids)
        self.assertIsNone(parsed.sessions[0].title)

    def test_parse_plan_document_accepts_title_separators_and_padding(self) -> None:
        cases = (
            ("## Session 01: Colon", "1", "Colon"),
            ("## Session 02-ASCII hyphen", "2", "ASCII hyphen"),
            ("## Session 03 – En dash", "3", "En dash"),
            ("## Session 04—Em dash", "4", "Em dash"),
            ("## Session 005", "5", None),
            ("## Session 006.007: Dotted", "6.7", "Dotted"),
        )
        for source, expected_id, expected_title in cases:
            with self.subTest(source=source):
                parsed = authoring_parse.parse_plan_document(source + "\n")
                self.assertEqual((expected_id,), parsed.session_ids)
                self.assertEqual(expected_title, parsed.sessions[0].title)

    def test_parse_plan_document_accepts_mixed_levels_case_and_formatting(self) -> None:
        headings = (
            "## Session 1: Two",
            " ### session 2 - Three ###   ",
            "  #### SESSION 3 – Four",
            "   ##### SeSsIoN 4—Five\t#####",
            "###### Session\t05\t:\tSix",
        )
        parsed = authoring_parse.parse_plan_document("\n".join(headings) + "\n")

        self.assertEqual(("1", "2", "3", "4", "5"), parsed.session_ids)
        self.assertEqual(
            (2, 3, 4, 5, 6),
            tuple(session.heading_level for session in parsed.sessions),
        )
        self.assertEqual(
            ("Two", "Three", "Four", "Five", "Six"),
            tuple(session.title for session in parsed.sessions),
        )

    def test_parse_plan_document_only_strips_atx_closing_markers(self) -> None:
        parsed = authoring_parse.parse_plan_document(
            "## Session 1: Attached###\n## Session 2: Detached ###\n"
        )
        self.assertEqual("Attached###", parsed.sessions[0].title)
        self.assertEqual("Detached", parsed.sessions[1].title)

    def test_parse_plan_document_rejects_malformed_candidates_with_context(
        self,
    ) -> None:
        cases = (
            ("## Session #1: Bad id", "invalid session id"),
            ("## Session 1..2: Bad id", "invalid session id"),
            ("## Session 0: Zero", "zero-valued session id component"),
            ("## Session 1.0: Zero", "zero-valued session id component"),
            ("## Session 1:", "dangling title separator"),
            ("## Session 1 -   ###", "dangling title separator"),
            ("## Session 1 / Unsupported", "unsupported title separator"),
            ("## Session 1 Missing punctuation", "punctuation-free title suffix"),
        )
        for source, reason in cases:
            with self.subTest(source=source):
                with self.assertRaises(plans_errors.PlanError) as raised:
                    authoring_parse.parse_plan_document("context\n" + source + "\n")
                message = str(raised.exception)
                self.assertIn("line 2", message)
                self.assertIn(reason, message)
                self.assertIn(source, message)

    def test_parse_plan_document_leaves_non_candidates_as_context(self) -> None:
        lines = (
            "# Session 1: H1",
            "####### Session 2: H7",
            "    ## Session 3: indented code",
            "**Session 4: bold**",
            "Session 5: plain",
            "## Phase 6: phase",
        )
        parsed = authoring_parse.parse_plan_document("\n".join(lines) + "\n")
        self.assertEqual((), parsed.sessions)
        self.assertEqual(lines, parsed.context_lines)

    def test_parse_plan_document_extracts_ordered_repository_registry(self) -> None:
        text = (
            "# Plan\n\n"
            "## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            "| SVC | tundra_svc | git@github.com:tundra/SVC.git |\n"
            "| OAK | tundra_OAK | https://github.com/tundra/OAK.git |\n\n"
            "Shared context.\n\n"
            "## Session 1: First\n\n"
            "**Reads repositories**: SVC, OAK\n"
            "**Writes repositories**: SVC\n\n"
            "Do one.\n"
        )

        parsed = authoring_parse.parse_plan_document(text)

        self.assertIsNotNone(parsed.registry)
        self.assertEqual(("SVC", "OAK"), parsed.registry.aliases)
        self.assertEqual(("SVC", "OAK"), parsed.sessions[0].scope.reads)
        self.assertEqual(("SVC",), parsed.sessions[0].scope.writes)
        self.assertEqual(("Do one.",), parsed.sessions[0].instruction_lines)
        self.assertEqual(
            ("github.com/tundra/SVC", "github.com/tundra/OAK"),
            tuple(repository.origin for repository in parsed.registry),
        )
        self.assertNotIn("## Repositories", parsed.context_lines)
        self.assertNotIn("| Alias | Path | Origin |", parsed.context_lines)
        self.assertEqual(
            ("# Plan", "", "", "Shared context.", ""), parsed.context_lines
        )

    def test_repositories_heading_tolerates_trailing_whitespace(self) -> None:
        for trailing in (" ", "\t", "  \t "):
            with self.subTest(trailing=repr(trailing)):
                text = (
                    f"# Plan\n\n## Repositories{trailing}\n\n"
                    "| Alias | Path | Origin |\n"
                    "| --- | --- | --- |\n"
                    "| app | repo | https://example.com/app.git |\n\n"
                    "## Session 1: A\n\n"
                    "**Reads repositories**: app\n"
                    "**Writes repositories**: app\n\nx\n"
                )

                parsed = authoring_parse.parse_plan_document(text)
                lint = authoring_lint.lint_plan_document(text)

                self.assertIsNotNone(parsed.registry)
                self.assertEqual(("app",), parsed.registry.aliases)
                self.assertEqual((), lint.findings)

    def test_parse_plan_document_ignores_registry_shaped_content(
        self,
    ) -> None:
        text = (
            "# Plan\n\n```markdown\n## Repositories\n"
            "| Alias | Path | Origin |\n| --- | --- | --- |\n"
            "| Fake | fake | https://example.com/fake.git |\n```\n\n"
            "## Session 1: First\n\n"
            "| Alias | Path | Origin |\n| --- | --- | --- |\n"
            "| Body | body | https://example.com/body.git |\n"
        )

        parsed = authoring_parse.parse_plan_document(text)

        self.assertIsNone(parsed.registry)
        self.assertIn("## Repositories", parsed.context_lines)
        self.assertIn(
            "| Body | body | https://example.com/body.git |",
            parsed.sessions[0].lines,
        )

    def test_parse_plan_document_reports_registry_errors_with_source_lines(
        self,
    ) -> None:
        cases = (
            (
                "# Plan\n\n## Repositories\n\n| Path | Alias | Origin |\n| --- | --- | "
                "--- |\n",
                5,
                authoring_source_models.LINT_MALFORMED_REPOSITORIES,
                "columns must be exactly",
            ),
            (
                "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC |  | https://example.com/svc.git |\n",
                5,
                authoring_source_models.LINT_INVALID_REPOSITORY,
                "must not be blank",
            ),
            (
                "## Session 1: First\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                3,
                authoring_source_models.LINT_REPOSITORIES_PLACEMENT,
                "before the first session",
            ),
            (
                "### Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                1,
                authoring_source_models.LINT_REPOSITORIES_PLACEMENT,
                "must be exactly",
            ),
            (
                "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n\n"
                "## Repositories\n",
                7,
                authoring_source_models.LINT_DUPLICATE_REPOSITORIES,
                "duplicate repository registry",
            ),
            (
                "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n"
                "| svc | oak | https://example.com/oak.git |\n",
                6,
                authoring_source_models.LINT_INVALID_REPOSITORY,
                "case-insensitively",
            ),
        )
        for text, line, category, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(
                    authoring_source_models.SourceRegistryError
                ) as raised:
                    authoring_parse.parse_plan_document(text)
                self.assertEqual(line, raised.exception.line_number)
                self.assertEqual(category, raised.exception.category)
                self.assertIn(reason, str(raised.exception))

    def test_registry_errors_redact_credential_bearing_origins(self) -> None:
        text = (
            "# Plan\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            "| SVC | ../svc | https://bob:ghp_SECRET123@github.com/t/SVC.git |\n\n"
            "## Session 1: Thing\n\nDo work.\n"
        )

        with self.assertRaises(authoring_source_models.SourceRegistryError) as raised:
            authoring_parse.parse_plan_document(text)
        result = authoring_lint.lint_plan_document(text)
        payload = json.dumps(
            [finding.as_dict() for finding in result.findings], sort_keys=True
        )

        self.assertNotIn("bob", str(raised.exception))
        self.assertNotIn("ghp_SECRET123", str(raised.exception))
        self.assertNotIn("bob", raised.exception.source_line)
        self.assertNotIn("ghp_SECRET123", raised.exception.source_line)
        self.assertNotIn("bob", payload)
        self.assertNotIn("ghp_SECRET123", payload)

    def test_registry_lint_redacts_credentials_from_any_table_cell(self) -> None:
        credential = "https://alice:s3cr3t@github.com/tundra/SVC.git"
        documents = (
            (
                "# Plan\n\n## Repositories\n\n"
                f"| SVC | svc | {credential} |\n"
                "| --- | --- | --- |\n\n"
                "## Session 1: Thing\n\nDo work.\n"
            ),
            (
                "# Plan\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                f"| {credential} | svc | SVC |\n\n"
                "## Session 1: Thing\n\nDo work.\n"
            ),
        )

        for document in documents:
            with self.subTest(document=document):
                result = authoring_lint.lint_plan_document(document)
                payload = json.dumps(dataclasses.asdict(result), default=str)

                self.assertEqual(1, len(result.findings))
                self.assertEqual(
                    "[repository row redacted]",
                    result.findings[0].source_line,
                )
                self.assertNotIn("alice", payload)
                self.assertNotIn("s3cr3t", payload)

    def test_registry_rejects_markdown_delimiter_in_origin(self) -> None:
        text = (
            "# Plan\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            "| SVC | svc | https://github.com/t/pipe|name.git |\n"
        )

        with self.assertRaises(authoring_source_models.SourceRegistryError) as raised:
            authoring_parse.parse_plan_document(text)

        self.assertEqual(
            authoring_source_models.LINT_MALFORMED_REPOSITORIES,
            raised.exception.category,
        )
        self.assertIn("exactly three cells", str(raised.exception))

    def test_repositories_prefixed_prose_heading_is_not_a_registry(self) -> None:
        text = (
            "# Plan\n\n## Repositories consulted\n\n"
            "Prose about repositories.\n\n"
            "## Session 1: Thing\n\nDo work.\n"
        )

        parsed = authoring_parse.parse_plan_document(text)
        lint = authoring_lint.lint_plan_document(text)

        self.assertIsNone(parsed.registry)
        self.assertEqual((), lint.findings)

    def test_malformed_exact_repositories_heading_remains_diagnostic(self) -> None:
        text = (
            "# Plan\n\n## repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            "| SVC | svc | https://example.com/svc.git |\n"
        )

        result = authoring_lint.lint_plan_document(text)

        self.assertEqual(1, len(result.findings))
        self.assertEqual(
            authoring_source_models.LINT_REPOSITORIES_PLACEMENT,
            result.findings[0].category,
        )
        self.assertIn("must be exactly", result.findings[0].message)


if __name__ == "__main__":
    unittest.main()
