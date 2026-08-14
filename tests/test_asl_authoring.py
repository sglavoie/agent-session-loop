"""Plan authoring and resplit-script migration tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from unittest import mock

from agent_session_loop import (
    authoring,
    cli,
    contracts,
    harnesses,
    plans,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _load_script_module,
    init_multi_repo_workspace,
)


class PlanAuthoringTests(unittest.TestCase):
    """Central plan authoring and transaction API (`agent_session_loop.authoring`)."""

    def _command_contract(
        self, session_id: str, *, done: str = "Tests pass."
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=plans.parse_session_id(session_id),
            done_when=(done,),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c1",
                        argv=("python3", "-m", "unittest", "-q"),
                        timeout_seconds=60.0,
                    ),
                )
            ),
        )

    # -- inline capture / output resolution parity --------------------------

    def test_capture_inline_plan_persists_source_and_dedups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = authoring.capture_inline_plan("# My PLAN!! v2\n\nBody\n", tmp)
            second = authoring.capture_inline_plan("# My PLAN!! v2\n\nBody\n", tmp)

            self.assertEqual(os.path.realpath(tmp), first["base_dir"])
            self.assertEqual("my-plan-v2", first["slug"])
            self.assertTrue(first["source_path"].endswith(".md"))
            self.assertTrue(os.path.exists(first["source_path"]))
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["source_path"], second["source_path"])

    def test_extract_inline_plan_uses_last_proposed_block(self) -> None:
        raw = (
            "Intro\n<proposed_plan>\n# Old\n</proposed_plan>\n"
            "<proposed_plan>\n# New\n\n## Session 1\n</proposed_plan>\n"
        )
        self.assertEqual("# New\n\n## Session 1\n", authoring.extract_inline_plan(raw))

    def test_extract_inline_plan_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            authoring.extract_inline_plan("<proposed_plan>   </proposed_plan>")

    def test_split_plan_refuses_duplicate_plan_id_without_force_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out")
            plan = os.path.join(tmp, "PLAN.md")
            with open(plan, "w", encoding="utf-8") as handle:
                handle.write(
                    "# Plan\n\n"
                    "## Session 1: First\n\nDo one.\n\n"
                    "## Session 2: Second\n\nDo two.\n"
                )
            contracts_by_id = {
                "1": {
                    "done_when": ["Session 1 lands."],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "type": "command",
                                "argv": ["python3", "-m", "unittest", "-q"],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["Session 2 lands."],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "type": "command",
                                "argv": ["python3", "-m", "unittest", "-q"],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                },
            }

            first = authoring.split_plan(
                plan_file=plan,
                contracts_by_id=contracts_by_id,
                base_dir=base,
            )

            with self.assertRaises(authoring.DuplicateSplitError) as raised:
                authoring.split_plan(
                    plan_file=plan, contracts_by_id=contracts_by_id, base_dir=base
                )

            self.assertIn(first.plan_id, str(raised.exception))
            self.assertIn(str(first.output_dir), str(raised.exception))
            self.assertIn("[draft]", str(raised.exception))
            self.assertEqual(
                [first.output_dir.name],
                sorted(path.name for path in Path(base).iterdir()),
            )

    def test_resolve_plan_output_force_new_keeps_collision_safe_suffixing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out")
            plan = os.path.join(tmp, "PLAN.md")
            with open(plan, "w", encoding="utf-8") as handle:
                handle.write("# Plan\n")

            first = authoring.resolve_plan_output(plan, base)
            second = authoring.resolve_plan_output(plan, base, force_new=True)

            self.assertEqual(1, first["collision_index"])
            self.assertEqual(2, second["collision_index"])
            self.assertEqual(first["hash_prefix"], first["fingerprint_prefix"])
            self.assertEqual(
                f"{first['content_hash_prefix']}-{first['fingerprint_prefix']}",
                first["plan_id"],
            )
            self.assertEqual(first["plan_id"], os.path.basename(first["output_dir"]))
            self.assertEqual(
                f"{first['plan_id']}-2", os.path.basename(second["output_dir"])
            )

    def test_split_and_prepare_parsers_accept_force_new(self) -> None:
        parser = cli.build_parser()

        split_args = parser.parse_args(
            [
                "split",
                "--plan-file",
                "P.md",
                "--contracts-file",
                "c.json",
                "--force-new",
            ]
        )
        prepare_args = parser.parse_args(["prepare", "/tmp/plan.md", "--force-new"])

        self.assertTrue(split_args.force_new)
        self.assertTrue(prepare_args.force_new)

    def test_resolve_plan_output_fingerprints_content_and_path(self) -> None:
        # Migrated from the retired resolve-plan-output.py script tests.
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out")
            left_dir = os.path.join(tmp, "left")
            right_dir = os.path.join(tmp, "right")
            os.mkdir(left_dir)
            os.mkdir(right_dir)
            for directory in (left_dir, right_dir):
                with open(
                    os.path.join(directory, "PLAN.md"), "w", encoding="utf-8"
                ) as h:
                    h.write("# Shared Name\n")

            left = authoring.resolve_plan_output(
                os.path.join(left_dir, "PLAN.md"), base
            )
            right = authoring.resolve_plan_output(
                os.path.join(right_dir, "PLAN.md"), base
            )

            # Same content => identical content prefix; distinct paths => distinct id.
            self.assertEqual(left["content_hash_prefix"], right["content_hash_prefix"])
            self.assertNotEqual(left["fingerprint_prefix"], right["fingerprint_prefix"])
            self.assertNotEqual(left["plan_id"], right["plan_id"])

    def test_slug_from_source_path_sanitizes_unsafe_filenames(self) -> None:
        # Migrated from the retired resolve-plan-output.py script tests.
        cases = {
            "My PLAN!! v2.md": "my-plan-v2",
            "___###.md": "plan",
            "Spacing_AND.symbols.md": "spacing-and-symbols",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(expected, authoring.slug_from_source_path(filename))

    def test_default_base_dir_resolution_precedence(self) -> None:
        # Migrated from the retired capture/resolve script tests.
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = os.path.join(tmp, "agent")
            xdg_dir = os.path.join(tmp, "xdg")
            with mock.patch.dict(
                os.environ,
                {"AGENT_PLANS_DIR": agent_dir, "XDG_DATA_HOME": xdg_dir},
            ):
                self.assertEqual(
                    os.path.realpath(agent_dir), authoring.default_base_dir()
                )
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": xdg_dir}, clear=False):
                os.environ.pop("AGENT_PLANS_DIR", None)
                self.assertEqual(
                    os.path.realpath(os.path.join(xdg_dir, "agent-plans")),
                    authoring.default_base_dir(),
                )
            with mock.patch.dict(os.environ, {"TMPDIR": tmp}, clear=False):
                os.environ.pop("AGENT_PLANS_DIR", None)
                os.environ.pop("XDG_DATA_HOME", None)
                result = authoring.default_base_dir()
                self.assertTrue(
                    result.endswith(os.path.join(".local", "share", "agent-plans"))
                )
                self.assertFalse(result.startswith(os.path.realpath(tmp)))

    # -- fenced-code-aware parsing ------------------------------------------

    def test_parse_plan_document_splits_context_and_sessions(self) -> None:
        text = (
            "# Plan Title\n\nShared context.\n\n"
            "## Session 1: First\n\nDo one.\n\n"
            "### Session 2.1: Nested\n\nDo two.\n"
        )
        parsed = authoring.parse_plan_document(text)

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
                parsed = authoring.parse_plan_document(text)

                self.assertEqual(("1",), parsed.session_ids)
                self.assertIn("###### Session 009 — fake", parsed.sessions[0].lines)
                self.assertIn("### Session nope / malformed", parsed.sessions[0].lines)

    def test_parse_plan_document_rejects_duplicate_ids(self) -> None:
        text = "## Session 1: A\n\nx\n\n## Session 01: B\n\ny\n"
        with self.assertRaises(plans.PlanError) as raised:
            authoring.parse_plan_document(text)
        message = str(raised.exception)
        self.assertIn("line 5", message)
        self.assertIn("duplicate canonical session id '1'", message)
        self.assertIn("first defined on line 1", message)
        self.assertIn("## Session 01: B", message)

    def test_parse_plan_document_titleless_heading(self) -> None:
        parsed = authoring.parse_plan_document("## Session 3\n\nBody.\n")
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
                parsed = authoring.parse_plan_document(source + "\n")
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
        parsed = authoring.parse_plan_document("\n".join(headings) + "\n")

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
        parsed = authoring.parse_plan_document(
            "## Session 1: Attached###\n## Session 2: Detached ###\n"
        )
        self.assertEqual("Attached###", parsed.sessions[0].title)
        self.assertEqual("Detached", parsed.sessions[1].title)

    def test_parse_plan_document_rejects_malformed_candidates_with_context(
        self,
    ) -> None:
        cases = (
            ("## Session nope: Bad id", "invalid session id"),
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
                with self.assertRaises(plans.PlanError) as raised:
                    authoring.parse_plan_document("context\n" + source + "\n")
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
        parsed = authoring.parse_plan_document("\n".join(lines) + "\n")
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

        parsed = authoring.parse_plan_document(text)

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

        parsed = authoring.parse_plan_document(text)

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
                authoring.LINT_MALFORMED_REPOSITORIES,
                "columns must be exactly",
            ),
            (
                "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC |  | https://example.com/svc.git |\n",
                5,
                authoring.LINT_INVALID_REPOSITORY,
                "must not be blank",
            ),
            (
                "## Session 1: First\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                3,
                authoring.LINT_REPOSITORIES_PLACEMENT,
                "before the first session",
            ),
            (
                "### Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                1,
                authoring.LINT_REPOSITORIES_PLACEMENT,
                "must be exactly",
            ),
            (
                "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n\n"
                "## Repositories\n",
                7,
                authoring.LINT_DUPLICATE_REPOSITORIES,
                "duplicate repository registry",
            ),
            (
                "## Repositories\n\n| Alias | Path | Origin |\n| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n"
                "| svc | oak | https://example.com/oak.git |\n",
                6,
                authoring.LINT_INVALID_REPOSITORY,
                "case-insensitively",
            ),
        )
        for text, line, category, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(authoring.SourceRegistryError) as raised:
                    authoring.parse_plan_document(text)
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

        with self.assertRaises(authoring.SourceRegistryError) as raised:
            authoring.parse_plan_document(text)
        result = authoring.lint_plan_document(text)
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
                result = authoring.lint_plan_document(document)
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

        with self.assertRaises(authoring.SourceRegistryError) as raised:
            authoring.parse_plan_document(text)

        self.assertEqual(
            authoring.LINT_MALFORMED_REPOSITORIES,
            raised.exception.category,
        )
        self.assertIn("exactly three cells", str(raised.exception))

    def test_repositories_prefixed_prose_heading_is_not_a_registry(self) -> None:
        text = (
            "# Plan\n\n## Repositories consulted\n\n"
            "Prose about repositories.\n\n"
            "## Session 1: Thing\n\nDo work.\n"
        )

        parsed = authoring.parse_plan_document(text)
        lint = authoring.lint_plan_document(text)

        self.assertIsNone(parsed.registry)
        self.assertEqual((), lint.findings)

    def test_malformed_exact_repositories_heading_remains_diagnostic(self) -> None:
        text = (
            "# Plan\n\n## repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            "| SVC | svc | https://example.com/svc.git |\n"
        )

        result = authoring.lint_plan_document(text)

        self.assertEqual(1, len(result.findings))
        self.assertEqual(
            authoring.LINT_REPOSITORIES_PLACEMENT,
            result.findings[0].category,
        )
        self.assertIn("must be exactly", result.findings[0].message)

    # -- source-plan linting -------------------------------------------------

    def test_lint_plan_document_accepts_a_clean_plan(self) -> None:
        result = authoring.lint_plan_document(
            "# Plan\n\n## Session 1: A\n\nx\n\n## Session 2: B\n\ny\n"
        )
        self.assertEqual((), result.findings)
        self.assertEqual(("1", "2"), result.session_ids)

    def test_lint_plan_document_carries_valid_registry(self) -> None:
        result = authoring.lint_plan_document(
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
                with self.assertRaises(authoring.SourceScopeError) as raised:
                    authoring.parse_plan_document(prefix + body)
                self.assertEqual(
                    authoring.LINT_INVALID_SESSION_SCOPE,
                    raised.exception.category,
                )
                self.assertIn(message, str(raised.exception))

    def test_lint_plan_document_categorizes_every_malformed_heading(self) -> None:
        cases = (
            ("## Session nope: Bad id", authoring.LINT_NON_NUMERIC_SESSION_ID),
            ("## Session 1..2: Bad id", authoring.LINT_INVALID_SESSION_ID),
            ("## Session 0: Zero", authoring.LINT_ZERO_ID_COMPONENT),
            ("## Session 1.0: Zero", authoring.LINT_ZERO_ID_COMPONENT),
            ("## Session 1a: Glued", authoring.LINT_SESSION_ID_SUFFIX),
            ("## Session: No id", authoring.LINT_MISSING_ID_WHITESPACE),
            ("## Session ", authoring.LINT_MISSING_SESSION_ID),
            ("## Session 1:", authoring.LINT_DANGLING_TITLE_SEPARATOR),
            ("## Session 1 -   ###", authoring.LINT_DANGLING_TITLE_SEPARATOR),
            (
                "## Session 1 / Unsupported",
                authoring.LINT_UNSUPPORTED_TITLE_SEPARATOR,
            ),
            (
                "## Session 1 Missing punctuation",
                authoring.LINT_PUNCTUATION_FREE_TITLE,
            ),
        )
        for source, category in cases:
            with self.subTest(source=source):
                result = authoring.lint_plan_document("context\n" + source + "\n")
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
        with self.assertRaises(plans.PlanError):
            authoring.parse_plan_document(text)

        result = authoring.lint_plan_document(text)
        self.assertEqual(
            [
                (3, authoring.LINT_PUNCTUATION_FREE_TITLE),
                (7, authoring.LINT_UNSUPPORTED_TITLE_SEPARATOR),
                (11, authoring.LINT_SESSION_ID_SUFFIX),
            ],
            [(finding.line, finding.category) for finding in result.findings],
        )
        self.assertEqual((), result.session_ids)

    def test_lint_plan_document_reports_duplicate_ids_as_findings(self) -> None:
        result = authoring.lint_plan_document(
            "## Session 1: First\n\nx\n\n## Session 01: Padded dup\n\ny\n"
        )
        self.assertEqual(1, len(result.findings))
        finding = result.findings[0]
        self.assertEqual(authoring.LINT_DUPLICATE_SESSION_ID, finding.category)
        self.assertEqual(5, finding.line)
        self.assertIn("first defined on line 1", finding.message)
        self.assertIsNone(finding.suggested_line)
        self.assertEqual(("1",), result.session_ids)

    def test_lint_plan_document_ignores_headings_in_code_fences(self) -> None:
        result = authoring.lint_plan_document(
            "## Session 1: A\n\n```\n## Session 2 Broken\n```\n\n## Session 2: B\n\ny\n"
        )
        self.assertEqual((), result.findings)
        self.assertEqual(("1", "2"), result.session_ids)

    def test_lint_suggestions_only_cover_unambiguous_repairs(self) -> None:
        suggestable = {
            "## Session 1 Missing punctuation": "## Session 1: Missing punctuation",
            "## Session 2 / Unsupported": "## Session 2: Unsupported",
            # A prose heading, not a session with the ID "plan". Original
            # spacing and any ATX closing marker are preserved.
            "## Session plan ##": "## Plan overview ##",
            # Original spacing and any ATX closing marker are preserved.
            "##  Session   3 No colon ##": "##  Session   3: No colon ##",
        }
        for source, expected in suggestable.items():
            with self.subTest(source=source):
                finding = authoring.lint_plan_document(source + "\n").findings[0]
                self.assertEqual(expected, finding.suggested_line)

        # A repair that would invent an id, renumber, or drop a title is left
        # to a human decision, so no suggestion is offered.
        for source in (
            "## Session 1a: Glued",
            "## Session 1.0: Zero",
            "## Session 1:",
            "## Session nope: Bad id",
            "## Session roadmap",
            "## Session 1 /",
        ):
            with self.subTest(source=source):
                finding = authoring.lint_plan_document(source + "\n").findings[0]
                self.assertIsNone(finding.suggested_line)

    def test_lint_suggests_a_safe_rename_for_a_stray_session_plan_heading(
        self,
    ) -> None:
        text = (
            "# Plan\n\n## Session plan\n\nOverview body stays intact.\n\n"
            "## Session 1: First\n\nDo one.\n\n"
            "## Session 2: Second\n\nDo two.\n"
        )
        finding = authoring.lint_plan_document(text).findings[0]
        self.assertEqual(authoring.LINT_NON_NUMERIC_SESSION_ID, finding.category)
        self.assertEqual("## Plan overview", finding.suggested_line)

        lines = text.splitlines()
        lines[finding.line - 1] = str(finding.suggested_line)
        repaired = "\n".join(lines) + "\n"

        self.assertEqual((), authoring.lint_plan_document(repaired).findings)
        parsed = authoring.parse_plan_document(repaired)
        self.assertEqual(("1", "2"), parsed.session_ids)
        self.assertIn("## Plan overview", parsed.context_lines)
        self.assertIn("Overview body stays intact.", parsed.context_lines)

    def test_lint_suggestions_parse_when_applied(self) -> None:
        text = "## Session 1 No colon\n\none\n\n### Session 2 | Bad sep\n\ntwo\n"
        lines = text.splitlines()
        for finding in authoring.lint_plan_document(text).findings:
            self.assertIsNotNone(finding.suggested_line)
            lines[finding.line - 1] = str(finding.suggested_line)

        repaired = "\n".join(lines) + "\n"
        self.assertEqual((), authoring.lint_plan_document(repaired).findings)
        self.assertEqual(
            ("1", "2"), authoring.parse_plan_document(repaired).session_ids
        )

    def test_lint_reveals_duplicates_masked_by_an_earlier_malformed_heading(
        self,
    ) -> None:
        # A duplicate id is invisible while the first heading fails to parse, so
        # a repair round can legitimately surface a new finding.
        text = "## Session 1 No colon\n\none\n\n## Session 1: Dup\n\ntwo\n"
        first = authoring.lint_plan_document(text)
        self.assertEqual(
            [authoring.LINT_PUNCTUATION_FREE_TITLE],
            [finding.category for finding in first.findings],
        )

        lines = text.splitlines()
        lines[0] = str(first.findings[0].suggested_line)
        second = authoring.lint_plan_document("\n".join(lines) + "\n")
        self.assertEqual(
            [authoring.LINT_DUPLICATE_SESSION_ID],
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
                result = authoring.lint_plan_document(text)
                try:
                    parsed = authoring.parse_plan_document(text)
                except plans.PlanError as error:
                    self.assertTrue(result.findings)
                    self.assertIn(f"line {result.findings[0].line}:", str(error))
                    self.assertEqual(result.findings[0].message, str(error))
                else:
                    self.assertEqual((), result.findings)
                    self.assertEqual(parsed.session_ids, result.session_ids)

    # -- heading normalization ----------------------------------------------

    def test_normalize_heading_levels_promotes_level_three(self) -> None:
        body = ["#### Changes", "prose", "##### Deep"]
        self.assertEqual(
            ["## Changes", "prose", "### Deep"],
            authoring.normalize_heading_levels(body, 3),
        )

    def test_normalize_heading_levels_preserves_level_two(self) -> None:
        body = ["### Changes", "prose"]
        self.assertEqual(body, authoring.normalize_heading_levels(body, 2))

    def test_normalize_heading_levels_skips_code_fences(self) -> None:
        body = ["#### Real", "```", "#### fenced", "```"]
        self.assertEqual(
            ["## Real", "```", "#### fenced", "```"],
            authoring.normalize_heading_levels(body, 3),
        )

    # -- contract rendering --------------------------------------------------

    def test_render_completion_contract_round_trips(self) -> None:
        contract = self._command_contract("2")
        rendered = authoring.render_completion_contract(contract)
        parsed = contracts.parse_completion_contract(
            rendered + "\n" + plans.HANDOFF_HEADING + "\n",
            session_id=plans.parse_session_id("2"),
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(("Tests pass.",), parsed.done_when)
        self.assertEqual("c1", parsed.verify.checks[0].name)
        self.assertEqual(
            ("python3", "-m", "unittest", "-q"), parsed.verify.checks[0].argv
        )

    def test_render_completion_contract_emits_nondefault_baseline(self) -> None:
        contract = contracts.CompletionContract(
            session_id=plans.parse_session_id("2"),
            done_when=("done",),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.FileExistsCheck(
                        name="f1",
                        path="agent_session_loop/authoring.py",
                        baseline_expectation="pass",
                    ),
                )
            ),
        )
        rendered = authoring.render_completion_contract(contract)
        self.assertIn('"baseline_expectation": "pass"', rendered)
        parsed = contracts.parse_completion_contract(
            rendered + "\n" + plans.HANDOFF_HEADING + "\n",
            session_id=plans.parse_session_id("2"),
        )
        self.assertEqual("pass", parsed.verify.checks[0].baseline_expectation)

    def test_render_completion_contract_round_trips_repository_alias(self) -> None:
        registry = workspaces.RepositoryRegistry(
            (
                workspaces.Repository("SVC", "svc", "https://example.com/svc.git"),
                workspaces.Repository("OAK", "oak", "https://example.com/oak.git"),
            )
        )
        contract = contracts.CompletionContract(
            session_id=plans.parse_session_id("2"),
            done_when=("done",),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c1",
                        argv=("python3", "-V"),
                        timeout_seconds=10,
                        repo="SVC",
                    ),
                    contracts.FileExistsCheck(name="f1", path="README.md", repo="OAK"),
                    contracts.RegexInFileCheck(
                        name="r1", path="main.py", pattern="main", repo="SVC"
                    ),
                )
            ),
        )

        with self.assertRaisesRegex(plans.PlanError, "without a repository registry"):
            authoring.render_completion_contract(contract)

        rendered = authoring.render_completion_contract(
            contract, repository_registry=registry
        )
        reparsed = contracts.parse_completion_contract(
            rendered, session_id="2", repository_registry=registry
        )

        self.assertEqual(
            ["SVC", "OAK", "SVC"],
            [check.repo for check in reparsed.verify.checks],
        )
        self.assertEqual(3, rendered.count('"repo"'))

    # -- session-file rendering ----------------------------------------------

    def test_render_session_file_requires_contract_by_default(self) -> None:
        with self.assertRaises(plans.PlanError):
            authoring.render_session_file(
                session_id="1", title="First", instruction_lines=["body"]
            )

    def test_render_session_file_rejects_mismatched_contract(self) -> None:
        with self.assertRaises(plans.PlanError):
            authoring.render_session_file(
                session_id="1",
                title="First",
                instruction_lines=["body"],
                contract=self._command_contract("2"),
            )

    @staticmethod
    def _inline_contract_lines(check_name: str) -> list[str]:
        return [
            "## Completion Contract",
            "",
            "### Done when",
            "",
            "- Inline criterion.",
            "",
            "### Verify",
            "",
            "```json",
            "{",
            '  "session": "1",',
            '  "checks": [',
            "    {",
            f'      "name": "{check_name}",',
            '      "type": "command",',
            '      "argv": ["python3", "-c", "pass"]',
            "    }",
            "  ]",
            "}",
            "```",
        ]

    def test_render_session_file_replaces_inline_completion_contract(self) -> None:
        instruction_lines = [
            "Do the first thing.",
            "",
            *self._inline_contract_lines("inline-gate"),
            "",
            "---",
        ]
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=instruction_lines,
            is_first=True,
            contract=self._command_contract("1"),
        )

        self.assertEqual(1, rendered.count("\n## Completion Contract\n"))
        self.assertNotIn("inline-gate", rendered)
        self.assertNotIn("Inline criterion.", rendered)
        self.assertIn("Do the first thing.", rendered)

        parsed = contracts.parse_completion_contract(rendered, session_id="1")
        self.assertEqual(("Tests pass.",), parsed.done_when)
        self.assertEqual(("c1",), tuple(check.name for check in parsed.verify.checks))

    def test_render_session_file_keeps_inline_contract_without_authored_one(
        self,
    ) -> None:
        instruction_lines = [
            "Do the first thing.",
            "",
            *self._inline_contract_lines("inline-gate"),
        ]
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=instruction_lines,
            is_first=True,
            require_contract=False,
        )

        parsed = contracts.parse_completion_contract(rendered, session_id="1")
        self.assertEqual(("Inline criterion.",), parsed.done_when)
        self.assertEqual(
            ("inline-gate",), tuple(check.name for check in parsed.verify.checks)
        )

    def test_parse_completion_contract_rejects_duplicate_sections(self) -> None:
        text = "\n".join(
            [
                "# Session 1",
                "",
                *self._inline_contract_lines("inline-gate"),
                "",
                *self._inline_contract_lines("authored-gate"),
                "",
                plans.HANDOFF_HEADING,
                "",
            ]
        )
        with self.assertRaisesRegex(
            plans.PlanError, r"has 2 '## Completion Contract' sections"
        ):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_ignores_fenced_heading_mention(self) -> None:
        text = "\n".join(
            [
                "# Session 1",
                "",
                "Quoting the heading below must not create a second contract:",
                "",
                "````markdown",
                "## Completion Contract",
                "````",
                "",
                *self._inline_contract_lines("inline-gate"),
                "",
                plans.HANDOFF_HEADING,
                "",
            ]
        )
        parsed = contracts.parse_completion_contract(text, session_id="1")
        self.assertEqual(
            ("inline-gate",), tuple(check.name for check in parsed.verify.checks)
        )

    def test_render_session_file_preserves_nested_fenced_contract_example(self) -> None:
        quoted_example = [
            "````markdown",
            "```markdown",
            "## Completion Contract",
            "",
            "### Done when",
            "",
            "- Quoted criterion.",
            "```",
            "````",
        ]
        instruction_lines = [
            "Do the first thing.",
            "",
            "A session file looks like this:",
            "",
            *quoted_example,
            "",
            "Keep this trailing instruction.",
        ]
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=instruction_lines,
            is_first=True,
            contract=self._command_contract("1"),
        )

        for line in quoted_example:
            self.assertIn(f"\n{line}\n", rendered)
        self.assertIn("Keep this trailing instruction.", rendered)
        self.assertIn("- Quoted criterion.", rendered)

        self.assertEqual(
            1,
            len(
                contracts.completion_contract_heading_indices(
                    rendered.splitlines(keepends=False)
                )
            ),
        )
        parsed = contracts.parse_completion_contract(rendered, session_id="1")
        self.assertEqual(("Tests pass.",), parsed.done_when)
        self.assertEqual(("c1",), tuple(check.name for check in parsed.verify.checks))

    def test_render_session_file_first_omits_previous_reference(self) -> None:
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=["Do the first thing."],
            is_first=True,
            contract=self._command_contract("1"),
        )
        self.assertIn("# Session 1: First", rendered)
        self.assertIn(authoring.CONTEXT_REFERENCE_LINE, rendered)
        self.assertNotIn("Previous session", rendered)
        self.assertIn("## Completion Contract", rendered)
        # The handoff ends with the optional-headings documentation comment that
        # follows the empty `- **Notes**:` line.
        self.assertIn("- **Notes**:\n", rendered)
        self.assertTrue(rendered.rstrip().endswith("-->"))
        self.assertIn(plans.DISCOVERED_FACTS_HEADING, rendered)
        self.assertIn(plans.OPEN_QUESTIONS_HEADING, rendered)

    def test_render_session_file_places_scope_before_instruction_separator(
        self,
    ) -> None:
        scope = plans.SessionScope(reads=("SVC", "OAK"), writes=("SVC",))
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=["Do the first thing."],
            is_first=True,
            contract=self._command_contract("1"),
            scope=scope,
        )

        self.assertIn(
            authoring.CONTEXT_REFERENCE_LINE
            + "\n\n**Reads repositories**: SVC, OAK\n"
            + "**Writes repositories**: SVC\n\n---",
            rendered,
        )
        self.assertEqual(
            scope,
            plans.parse_session_scope(rendered, ("SVC", "OAK")),
        )

    def test_handoff_template_documentation_comment_is_ignored_by_parser(
        self,
    ) -> None:
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=["Do the first thing."],
            is_first=True,
            contract=self._command_contract("1"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(rendered, encoding="utf-8")
            session_file = plans.SessionFile(
                session_id=plans.parse_session_id("1"), path=session_path
            )

            # The fresh template's documentation comment names the optional
            # headings and a `- ` bullet example; none of it may leak into the
            # parsed handoff.
            fresh = plans.read_session_handoff(session_file)
            self.assertEqual("pending", fresh.status)
            self.assertEqual((), fresh.discovered_facts)
            self.assertEqual((), fresh.open_questions)

            # Completing the session below the comment round-trips cleanly: the
            # real headings/bullets are captured and the comment is still skipped.
            completed = rendered.replace(
                "- **Status**: pending", "- **Status**: complete"
            ) + "\n".join(
                [
                    "",
                    plans.DISCOVERED_FACTS_HEADING,
                    "- a verified fact",
                    "",
                    plans.OPEN_QUESTIONS_HEADING,
                    "- an open question",
                    "",
                ]
            )
            session_path.write_text(completed, encoding="utf-8")
            handoff = plans.read_session_handoff(session_file)
            self.assertEqual("complete", handoff.status)
            self.assertEqual(("a verified fact",), handoff.discovered_facts)
            self.assertEqual(("an open question",), handoff.open_questions)

    def test_render_session_file_includes_previous_reference(self) -> None:
        rendered = authoring.render_session_file(
            session_id="2",
            title="Second",
            instruction_lines=["Do the second thing."],
            previous_session_id="1",
            contract=self._command_contract("2"),
        )
        self.assertIn("bottom of `session-1.md`.", rendered)

    def test_render_session_file_normalizes_deeper_accepted_heading(self) -> None:
        parsed = authoring.parse_plan_document(
            "##### Session 004 — Deep ###\n\nIntro.\n\n###### Changes\n\n- item\n"
        )
        session = parsed.sessions[0]
        rendered = authoring.render_session_file(
            session_id=session.session_id,
            title=session.title,
            instruction_lines=session.instruction_lines,
            heading_level=session.heading_level,
            is_first=True,
            contract=self._command_contract("4"),
        )
        self.assertIn("# Session 4: Deep", rendered)
        self.assertIn("## Changes", rendered)
        self.assertNotIn("###### Changes", rendered)

    def test_render_session_file_preserves_fenced_content(self) -> None:
        body = ["Run:", "```python", "x = 1  # not a heading", "```"]
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=body,
            is_first=True,
            contract=self._command_contract("1"),
        )
        self.assertIn("x = 1  # not a heading", rendered)

    # -- context / progress rendering ---------------------------------------

    def test_render_context_uses_template_when_empty(self) -> None:
        rendered = authoring.render_context([], "/plans/PLAN.md")
        self.assertIn("_No shared context section found", rendered)
        self.assertIn("`/plans/PLAN.md`", rendered)

    def test_render_context_preserves_lines(self) -> None:
        rendered = authoring.render_context(
            ["", "# Plan", "", "Shared context.", ""], "/plans/PLAN.md"
        )
        self.assertEqual("# Plan\n\nShared context.\n", rendered)

    def test_render_progress_skeleton_is_header_only(self) -> None:
        self.assertEqual(plans.PROGRESS_HEADER, authoring.render_progress_skeleton())
        self.assertNotIn("## Session", authoring.render_progress_skeleton())

    # -- manifest rendering and table updates -------------------------------

    def _manifest_spec(self) -> authoring.ManifestSpec:
        sessions = (
            authoring.ManifestSession(
                session_id=plans.parse_session_id("1"), title="First"
            ),
            authoring.ManifestSession(
                session_id=plans.parse_session_id("2"), title="Second"
            ),
        )
        return authoring.ManifestSpec(
            plan_title="My Plan",
            source_path="/plans/PLAN.md",
            output_dir="/plans/out",
            plan_id="aaaa-bbbb",
            source_fingerprint="f" * 64,
            content_sha256="c" * 64,
            sessions=sessions,
            split_date="2026-07-10",
            readiness=plans.ReadinessMetadata(
                state="draft",
                readiness_sha256="d" * 64,
                checked_at="2026-07-10T00:00:00+00:00",
            ),
            skill_prefix="/",
        )

    def test_render_manifest_includes_readiness_and_table(self) -> None:
        rendered = authoring.render_manifest(self._manifest_spec())
        self.assertIn("**Readiness**: draft", rendered)
        self.assertIn("**Readiness SHA-256**: `" + "d" * 64 + "`", rendered)
        self.assertIn("**Sessions**: 2", rendered)
        self.assertIn("| 1 | First | pending | session-1.md |", rendered)
        self.assertIn("/session-run /plans/out/", rendered)
        readiness = plans.parse_readiness_metadata(rendered)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_render_manifest_includes_registry_only_when_declared(self) -> None:
        registry = workspaces.RepositoryRegistry(
            (
                workspaces.Repository(
                    "SVC", "tundra_svc", "git@github.com:tundra/SVC.git"
                ),
                workspaces.Repository(
                    "OAK", "tundra_OAK", "https://github.com/tundra/OAK.git"
                ),
            )
        )
        rendered = authoring.render_manifest(
            dataclasses.replace(self._manifest_spec(), registry=registry)
        )

        self.assertIn("## Repositories", rendered)
        self.assertIn("| Alias | Path | Origin |", rendered)
        self.assertLess(rendered.index("| SVC |"), rendered.index("| OAK |"))
        self.assertIn("| SVC | tundra_svc | github.com/tundra/SVC |", rendered)
        self.assertNotIn(
            "## Repositories", authoring.render_manifest(self._manifest_spec())
        )

    def test_add_manifest_session_row_appends(self) -> None:
        rendered = authoring.render_manifest(self._manifest_spec())
        updated = authoring.add_manifest_session_row(
            rendered,
            authoring.ManifestSession(
                session_id=plans.parse_session_id("3"), title="Third"
            ),
        )
        self.assertIn("| 3 | Third | pending | session-3.md |", updated)
        self.assertLess(updated.index("| 2 | Second"), updated.index("| 3 | Third"))

    def test_add_manifest_session_row_without_table_raises(self) -> None:
        with self.assertRaises(plans.PlanError):
            authoring.add_manifest_session_row(
                "# Plan\n\nNo table here.\n",
                authoring.ManifestSession(
                    session_id=plans.parse_session_id("1"), title="First"
                ),
            )

    # -- archive paths -------------------------------------------------------

    def test_archive_dir_name_is_deterministic(self) -> None:
        now = datetime(2026, 7, 10, 12, 30, 45, tzinfo=UTC)
        self.assertEqual(
            "session-2.1-20260710T123045Z",
            authoring.archive_dir_name("2.1", now),
        )

    def test_create_archive_dir_is_collision_safe(self) -> None:
        now = datetime(2026, 7, 10, 12, 30, 45, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            first = authoring.create_archive_dir(tmp, "2", now)
            second = authoring.create_archive_dir(tmp, "2", now)
            self.assertEqual(authoring.ARCHIVE_ROOT_NAME, first.parent.name)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    # -- atomic multi-file transaction --------------------------------------

    def test_transaction_commits_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            tx = authoring.PlanTransaction(plan_dir)
            tx.stage("context.md", "ctx")
            tx.stage("manifest.md", "man")
            committed = tx.commit()

            self.assertEqual(2, len(committed))
            self.assertEqual("ctx", (plan_dir / "context.md").read_text())
            self.assertEqual("man", (plan_dir / "manifest.md").read_text())
            leftovers = [p.name for p in plan_dir.iterdir() if p.name.startswith(".")]
            self.assertEqual([], leftovers)

    def test_transaction_failed_write_leaves_no_partial_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "blocker").write_text("i am a file")
            tx = authoring.PlanTransaction(plan_dir)
            tx.stage("context.md", "ctx")
            tx.stage("manifest.md", "man")
            # A staged path whose parent is a regular file forces a write failure.
            tx.stage("blocker/session-1.md", "nope")

            with self.assertRaises(plans.PlanError):
                tx.commit()

            self.assertFalse((plan_dir / "context.md").exists())
            self.assertFalse((plan_dir / "manifest.md").exists())
            self.assertEqual(["blocker"], sorted(p.name for p in plan_dir.iterdir()))

    def test_transaction_overwrite_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "context.md").write_text("original")
            tx = authoring.PlanTransaction(plan_dir)
            tx.stage("context.md", "updated")
            tx.commit()
            self.assertEqual("updated", (plan_dir / "context.md").read_text())

    def test_transaction_recovery_waits_for_in_flight_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            names = ("a.md", "b.md", "c.md", "d.md", "e.md")
            for name in names:
                (plan_dir / name).write_text("OLD\n", encoding="utf-8")

            started = threading.Event()
            reader_results: list[bool] = []
            reader_errors: list[BaseException] = []
            original_fsync_directory = plans._fsync_directory
            calls = 0

            def slow_fsync_directory(path: Path) -> None:
                nonlocal calls
                original_fsync_directory(path)
                calls += 1
                if calls == 3:
                    started.set()
                    time.sleep(0.1)

            def recover() -> None:
                if not started.wait(5):
                    reader_errors.append(AssertionError("commit did not start"))
                    return
                try:
                    reader_results.append(
                        plans.recover_pending_plan_transaction(plan_dir)
                    )
                except BaseException as error:
                    reader_errors.append(error)

            transaction = authoring.PlanTransaction(plan_dir)
            for name in names:
                transaction.stage(name, "NEW\n")
            reader = threading.Thread(target=recover)
            with mock.patch.object(
                plans, "_fsync_directory", side_effect=slow_fsync_directory
            ):
                reader.start()
                committed = transaction.commit()
                reader.join(timeout=5)

            self.assertFalse(reader.is_alive())
            self.assertEqual([], reader_errors)
            self.assertEqual([False], reader_results)
            self.assertEqual(len(names), len(committed))
            self.assertEqual(
                {"NEW"},
                {
                    (plan_dir / name).read_text(encoding="utf-8").strip()
                    for name in names
                },
            )

    def test_transaction_recovery_times_out_with_holder_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            transaction_dir = plan_dir / plans.PLAN_TRANSACTION_DIR_NAME
            transaction_dir.mkdir()
            (transaction_dir / plans.PLAN_TRANSACTION_JOURNAL_NAME).write_text(
                '{"version":1,"entries":[]}\n', encoding="utf-8"
            )
            holder_ready = threading.Event()
            release_holder = threading.Event()

            def hold_transaction_lock() -> None:
                with plans._plan_transaction_mutex(transaction_dir):
                    holder_ready.set()
                    release_holder.wait(5)

            holder = threading.Thread(
                target=hold_transaction_lock, name="transaction-test-holder"
            )
            holder.start()
            self.assertTrue(holder_ready.wait(5))
            started_at = time.monotonic()
            try:
                with (
                    mock.patch.object(plans, "PLAN_TRANSACTION_LOCK_WAIT_SECONDS", 0.1),
                    self.assertRaisesRegex(
                        plans.PlanError,
                        r"timed out after 0\.1s waiting for plan transaction lock "
                        r"at .*transaction\.lock; holder pid \d+ on .* "
                        r"thread 'transaction-test-holder'",
                    ),
                ):
                    plans.load_plan_metadata(plan_dir)
            finally:
                release_holder.set()
                holder.join(timeout=5)

            self.assertFalse(holder.is_alive())
            self.assertLess(time.monotonic() - started_at, 1.0)

    # -- readiness refresh ---------------------------------------------------

    def test_refresh_readiness_matches_recompute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = self._author_plan(Path(tmp))
            metadata = authoring.refresh_readiness_metadata(
                plan_dir, checked_at="2026-07-10T12:00:00+00:00"
            )
            self.assertEqual("draft", metadata.state)
            self.assertEqual(
                plans.stable_readiness_sha256(plan_dir), metadata.readiness_sha256
            )
            on_disk = plans.read_readiness_metadata(plan_dir)
            self.assertEqual(metadata, on_disk)

    # -- golden end-to-end determinism --------------------------------------

    def _author_plan(self, plan_dir: Path) -> Path:
        """Author a two-session plan directory deterministically; return its path."""
        spec = authoring.ManifestSpec(
            plan_title="My Plan",
            source_path="/plans/PLAN.md",
            output_dir="/plans/out",
            plan_id="aaaa-bbbb",
            source_fingerprint="f" * 64,
            content_sha256="c" * 64,
            sessions=(
                authoring.ManifestSession(
                    session_id=plans.parse_session_id("1"), title="First"
                ),
                authoring.ManifestSession(
                    session_id=plans.parse_session_id("2"), title="Second"
                ),
            ),
            split_date="2026-07-10",
            readiness=plans.ReadinessMetadata(
                state="draft", readiness_sha256="0" * 64, checked_at="pending"
            ),
            skill_prefix="/",
        )
        tx = authoring.PlanTransaction(plan_dir)
        tx.stage("manifest.md", authoring.render_manifest(spec))
        tx.stage(
            "context.md",
            authoring.render_context(["# My Plan", "", "ctx"], "/plans/PLAN.md"),
        )
        tx.stage("progress.md", authoring.render_progress_skeleton())
        for sid, title in (("1", "First"), ("2", "Second")):
            tx.stage(
                f"session-{sid}.md",
                authoring.render_session_file(
                    session_id=sid,
                    title=title,
                    instruction_lines=["Do the work."],
                    is_first=(sid == "1"),
                    previous_session_id=("1" if sid == "2" else None),
                    contract=self._command_contract(sid),
                ),
            )
        tx.commit()
        return plan_dir

    def test_authored_plan_is_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = self._author_plan(Path(tmp))
            authoring.refresh_readiness_metadata(
                plan_dir, checked_at="2026-07-10T12:00:00+00:00"
            )
            snapshot = plans.inspect_plan(plan_dir)
            self.assertEqual(("1", "2"), snapshot.session_ids)
            self.assertEqual({"1": "pending", "2": "pending"}, snapshot.status_by_id)
            for session in snapshot.sessions:
                contract = contracts.read_completion_contract(
                    plan_dir, session.session_id
                )
                self.assertIsNotNone(contract)

    def test_registry_order_changes_content_and_readiness_hashes(self) -> None:
        repositories = (
            workspaces.Repository("SVC", "svc", "https://example.com/svc.git"),
            workspaces.Repository("OAK", "oak", "https://example.com/oak.git"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            left = self._author_plan(Path(tmp) / "left")
            right = self._author_plan(Path(tmp) / "right")
            left_spec = dataclasses.replace(
                self._manifest_spec(),
                registry=workspaces.RepositoryRegistry(repositories),
            )
            right_spec = dataclasses.replace(
                self._manifest_spec(),
                registry=workspaces.RepositoryRegistry(tuple(reversed(repositories))),
            )
            (left / "manifest.md").write_text(
                authoring.render_manifest(left_spec), encoding="utf-8"
            )
            (right / "manifest.md").write_text(
                authoring.render_manifest(right_spec), encoding="utf-8"
            )

            self.assertNotEqual(
                plans.plan_content_sha256(left), plans.plan_content_sha256(right)
            )
            self.assertNotEqual(
                plans.stable_readiness_sha256(left),
                plans.stable_readiness_sha256(right),
            )

    def test_authoring_is_deterministic(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first_tmp,
            tempfile.TemporaryDirectory() as second_tmp,
        ):
            first_dir = self._author_plan(Path(first_tmp))
            second_dir = self._author_plan(Path(second_tmp))
            for name in (
                "manifest.md",
                "context.md",
                "progress.md",
                "session-1.md",
                "session-2.md",
            ):
                self.assertEqual(
                    (first_dir / name).read_text(),
                    (second_dir / name).read_text(),
                    name,
                )


class SplitRepositoryFootprintTests(unittest.TestCase):
    """`asl split` refuses to write a multi-repository plan without a registry."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = self.repositories["SVC"]
        self.second = self.repositories["OAK"]
        for repository in (self.first, self.second):
            (repository / "README.md").write_text("x\n", encoding="utf-8")

    def _write_plan(self, body: str) -> Path:
        plan_file = self.root / "plan.md"
        plan_file.write_text(body, encoding="utf-8")
        return plan_file

    def _canonical_roots(self) -> list[str]:
        return sorted(
            str(workspaces.canonicalize_filesystem_path(repository))
            for repository in (self.first, self.second)
        )

    def _check(self, plan_file: Path, cwd: Path) -> dict:
        args = argparse.Namespace(
            json=True, inline_stdin=False, plan_file=str(plan_file), cwd=str(cwd)
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            authoring.check_from_args(args)
        return json.loads(buffer.getvalue())

    def _write_contracts(self, contracts_by_id: dict) -> Path:
        contracts_file = self.root / "contracts.json"
        contracts_file.write_text(json.dumps(contracts_by_id), encoding="utf-8")
        return contracts_file

    def _split(
        self, plan_file: Path, contracts_file: Path, cwd: Path, *, out: str = "out"
    ) -> tuple[int, dict, Path]:
        output_base = self.root / out
        args = argparse.Namespace(
            json=True,
            inline_stdin=False,
            plan_file=str(plan_file),
            base_dir=str(output_base),
            check=False,
            contracts_file=str(contracts_file),
            skill_prefix="claude",
            analysis_file=None,
            cwd=str(cwd),
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = authoring.split_from_args(args)
        return exit_code, json.loads(buffer.getvalue()), output_base

    def test_multi_repository_plan_without_a_registry_is_refused(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Then update {self.second}/README.md too.\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertEqual(authoring.SPLIT_REASON_REGISTRY_REQUIRED, payload["reason"])
        self.assertEqual(
            [authoring.LINT_MISSING_REPOSITORY_REGISTRY],
            [finding["category"] for finding in payload["findings"]],
        )
        intent = payload["repository_intent"]
        self.assertTrue(intent["registry_required"])
        self.assertEqual(self._canonical_roots(), sorted(intent["git_roots"]))
        footprint = payload["repository_footprint"]
        self.assertFalse(footprint["registry_present"])
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(entry["alias"] for entry in footprint["detected"]),
        )
        self.assertEqual(
            ["| Alias | Path | Origin |", "| --- | --- | --- |"],
            footprint["suggested_registry_table"][:2],
        )

    def test_single_repository_plan_still_checks_clean(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Revisit {self.first}/README.md once more.\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertTrue(payload["ok"])
        self.assertNotIn("repository_footprint", payload)

    def test_a_non_git_path_outside_the_root_still_checks_clean(self) -> None:
        loose = self.root / "loose.txt"
        loose.write_text("x\n", encoding="utf-8")
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Then read {loose} for reference.\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertTrue(payload["ok"])
        self.assertNotIn("repository_footprint", payload)

    def test_paths_inside_a_fenced_block_are_not_evidence(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            "```bash\n"
            f"cd {self.second} && make\n"
            "```\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertTrue(payload["ok"])

    def test_write_path_refuses_contracts_that_span_repositories(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\nDo the first thing.\n\n"
            "## Session 2: Second\n\nDo the second thing.\n"
        )
        contracts_file = self._write_contracts(
            {
                "1": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "path": str(self.first / "README.md"),
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "name": "b",
                                "type": "file_exists",
                                "path": str(self.second / "README.md"),
                            }
                        ],
                    },
                },
            }
        )

        # The prose alone names no repository, so `--check` has nothing
        # conclusive to report and defers to the contract-aware write path.
        self.assertTrue(self._check(plan_file, self.first)["ok"])

        exit_code, payload, output_base = self._split(
            plan_file, contracts_file, self.first
        )

        self.assertEqual(1, exit_code)
        self.assertEqual(authoring.SPLIT_REASON_REGISTRY_REQUIRED, payload["reason"])
        self.assertEqual(
            self._canonical_roots(),
            sorted(payload["repository_intent"]["git_roots"]),
        )
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(
                entry["alias"] for entry in payload["repository_footprint"]["detected"]
            ),
        )
        self.assertFalse(output_base.exists())

    def test_contract_target_that_does_not_exist_yet_still_names_its_repository(
        self,
    ) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Work in {self.first}/README.md.\n\n"
            "## Session 2: Second\n\nDo the second thing.\n"
        )
        contracts_file = self._write_contracts(
            {
                "1": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "path": str(self.first / "README.md"),
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "name": "b",
                                "type": "file_exists",
                                "path": str(self.second / "src" / "new.py"),
                            }
                        ],
                    },
                },
            }
        )

        exit_code, payload, output_base = self._split(
            plan_file, contracts_file, self.first
        )

        self.assertEqual(1, exit_code)
        self.assertEqual(authoring.SPLIT_REASON_REGISTRY_REQUIRED, payload["reason"])
        self.assertEqual(
            self._canonical_roots(),
            sorted(payload["repository_intent"]["git_roots"]),
        )
        self.assertFalse(output_base.exists())

    def test_single_repository_legacy_plan_still_splits(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Then add {self.first}/src/new.py.\n"
        )
        contracts_file = self._write_contracts(
            {
                session_id: {
                    "done_when": ["done"],
                    "verify": {
                        "session": session_id,
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "path": str(self.first / "README.md"),
                            }
                        ],
                    },
                }
                for session_id in ("1", "2")
            }
        )

        exit_code, payload, _ = self._split(plan_file, contracts_file, self.first)

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertNotIn("repository_intent", payload)

    def test_registered_multi_repository_plan_still_splits(self) -> None:
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |"
            for alias, path, origin in DEFAULT_MULTI_REPO_SPECS[:2]
        )
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n\n"
            "## Session 1: First\n\n"
            "**Reads repositories**: SVC, OAK\n"
            "**Writes repositories**: SVC\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            "**Reads repositories**: SVC, OAK\n"
            "**Writes repositories**: OAK\n\n"
            f"Then update {self.second}/README.md too.\n"
        )
        contracts_file = self._write_contracts(
            {
                "1": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "repo": "SVC",
                                "path": "README.md",
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "name": "b",
                                "type": "file_exists",
                                "repo": "OAK",
                                "path": "README.md",
                            }
                        ],
                    },
                },
            }
        )

        exit_code, payload, _ = self._split(plan_file, contracts_file, self.workspace)

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            ["SVC", "OAK"],
            [repository["alias"] for repository in payload["repositories"]],
        )


class PlanAuthoringResplitTests(unittest.TestCase):
    """Split/resplit migration onto `agent_session_loop.authoring` (Session 12)."""

    resplit: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.resplit = _load_script_module(
            "resplit_session_module",
            "skills",
            "session-resplit",
            "scripts",
            "resplit-session.py",
        )

    # -- shared structural operations ----------------------------------------

    def test_replace_previous_session_reference_repoints_line(self) -> None:
        text = (
            "# Session 3: Wrap\n\n"
            + authoring.previous_session_reference_line("2")
            + "\n\nBody.\n"
        )
        updated, changed = authoring.replace_previous_session_reference(
            text, "2", "2.3"
        )
        self.assertTrue(changed)
        self.assertIn(authoring.previous_session_reference_line("2.3"), updated)
        self.assertNotIn(authoring.previous_session_reference_line("2"), updated)

        unchanged, changed_again = authoring.replace_previous_session_reference(
            updated, "2", "2.3"
        )
        self.assertFalse(changed_again)
        self.assertEqual(updated, unchanged)

    def test_replace_session_status_rows_updates_only_status_tables(self) -> None:
        text = "\n".join(
            [
                "| # | Title | Status | File |",
                "| --- | --- | --- | --- |",
                "| 1 | Setup | complete | session-1.md |",
                "| 2 | Parent | pending | session-2.md |",
                "",
                "| Session | Title | Status |",
                "|---|---|---|",
                "| S2 | Parent | pending |",
                "",
                "| Wave | Sessions | Notes |",
                "|---|---|---|",
                "| 2 | 2 | untouched |",
                "",
            ]
        )
        replacements = (
            authoring.ManifestSession(
                session_id=plans.parse_session_id("2.1"), title="Parent: A"
            ),
            authoring.ManifestSession(
                session_id=plans.parse_session_id("2.2"), title="Parent: B"
            ),
        )
        updated, changed = authoring.replace_session_status_rows(
            text, "2", replacements
        )
        self.assertTrue(changed)
        self.assertIn("| 2.1 | Parent: A | pending | session-2.1.md |", updated)
        self.assertIn("| 2.2 | Parent: B | pending | session-2.2.md |", updated)
        self.assertNotIn("| 2 | Parent | pending | session-2.md |", updated)
        self.assertIn("| 2.1 | Parent: A | pending |", updated)
        # Tables without a Status column are never rewritten.
        self.assertIn("| 2 | 2 | untouched |", updated)

    def test_replace_manifest_sessions_count_rewrites_metadata_line(self) -> None:
        text = "# Manifest\n\n**Sessions**: 3\n"
        self.assertIn(
            "**Sessions**: 5",
            authoring.replace_manifest_sessions_count(text, 5),
        )

    def test_contract_from_payload_round_trips_and_fails_closed(self) -> None:
        contract = authoring.contract_from_payload(
            "2.1",
            ["Child lands."],
            {
                "session": "2.1",
                "checks": [
                    {
                        "name": "suite",
                        "type": "command",
                        "argv": ["python3", "-m", "unittest", "-q"],
                        "timeout_seconds": 60,
                    }
                ],
            },
        )
        rendered = authoring.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="2.1")
        self.assertEqual(contract, reparsed)

        with self.assertRaises(plans.PlanError):
            authoring.contract_from_payload(
                "2.1", [], {"checks": [{"type": "command"}]}
            )
        with self.assertRaises(plans.PlanError):
            authoring.contract_from_payload("2.1", ["Done."], {"checks": []})
        with self.assertRaises(plans.PlanError):
            authoring.contract_from_payload(
                "2.1",
                ["Done."],
                {"session": "3", "checks": [{"type": "file_exists", "path": "x"}]},
            )
        # A non-array done_when fails closed as PlanError rather than raising a
        # TypeError when the payload builder tries to iterate it.
        with self.assertRaises(plans.PlanError):
            authoring.contract_from_payload(
                "2.1",
                5,
                {"checks": [{"type": "command"}]},  # type: ignore[arg-type]
            )

    # -- resplit fixture ------------------------------------------------------

    def _write_resplit_plan(
        self,
        plan_dir: Path,
        *,
        parent_contract: bool = True,
    ) -> None:
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(
            "\n".join(
                [
                    "# Manifest",
                    "",
                    "**Sessions**: 3",
                    "",
                    "| # | Title | Status | File |",
                    "|---|---|---|---|",
                    "| 1 | Setup | complete | session-1.md |",
                    "| 2 | Parent | pending | session-2.md |",
                    "| 3 | Wrap | pending | session-3.md |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        contract_lines: list[str] = []
        if parent_contract:
            verify_json = json.dumps(
                {
                    "session": "2",
                    "checks": [
                        {
                            "name": "parent-suite",
                            "type": "command",
                            "argv": ["python3", "-m", "unittest", "-q"],
                            "timeout_seconds": 90,
                        }
                    ],
                },
                indent=2,
            )
            contract_lines = [
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- Parent goal is met.",
                "",
                "### Verify",
                "",
                "```json",
                *verify_json.splitlines(),
                "```",
            ]
        sessions = {
            "1": ("Setup", "complete", ["Setup instructions."]),
            "2": (
                "Parent",
                "pending",
                ["Implement these deliverables: alpha, beta."] + contract_lines,
            ),
            "3": (
                "Wrap",
                "pending",
                [
                    authoring.previous_session_reference_line("2"),
                    "",
                    "Wrap instructions.",
                ],
            ),
        }
        for sid, (title, status, body_lines) in sessions.items():
            (plan_dir / f"session-{sid}.md").write_text(
                "\n".join(
                    [
                        f"# Session {sid}: {title}",
                        "",
                        *body_lines,
                        "",
                        "## Handoff",
                        "",
                        f"- **Status**: {status}",
                        "- **Files modified**: (none)",
                        "- **Key decisions**: (none)",
                        "- **Blockers for next session**: (none)",
                        "- **Notes**:",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

    # -- resplit behavior on shared authoring seams ---------------------------

    def test_resplit_success_emits_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            result = self.resplit.resplit_session(
                plan_dir, session_id="2", parts=2, state_dir=state_dir
            )

            self.assertIn("next_steps", result)
            steps = result["next_steps"]
            self.assertEqual(1, len(steps))
            invocations = steps[0]["invocations"]
            self.assertEqual(set(harnesses.HARNESS_NAMES), set(invocations))
            for command in invocations.values():
                self.assertIn("session-expand", command)
                self.assertTrue(command.rstrip().endswith("/"))

    def test_resplit_pause_emits_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir, parent_contract=False)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = self.resplit.main(
                    [
                        str(plan_dir),
                        "--session",
                        "2",
                        "--parts",
                        "2",
                        "--state-dir",
                        str(state_dir),
                        "--json",
                    ]
                )

            self.assertEqual(1, code)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("no-contract", payload["reason"])
            self.assertEqual(
                self.resplit.PAUSE_REMEDIATION["no-contract"],
                payload["remediation"],
            )

    def test_pause_remediation_covers_every_resplit_reason(self) -> None:
        # Every reason the script raises must have a one-line remediation.
        raised_reasons = {
            "ambiguous-input",
            "blocked",
            "unknown-status",
            "malformed-plan",
            "collision",
            "cannot-split",
            "no-contract",
            "ambiguous-contract",
            "lock-contention",
        }
        self.assertTrue(raised_reasons.issubset(set(self.resplit.PAUSE_REMEDIATION)))

    def test_resplit_derives_child_contracts_and_refreshes_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            result = self.resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=2,
                state_dir=state_dir,
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(["2.1", "2.2"], result["children"])
            archive_name = authoring.archive_dir_name(
                "2", datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
            )
            self.assertEqual(
                f"{authoring.ARCHIVE_ROOT_NAME}/{archive_name}",
                result["archive_dir"],
            )

            for child_id in ("2.1", "2.2"):
                child_text = (
                    plan_dir / plans.session_id_to_filename(child_id)
                ).read_text(encoding="utf-8")
                self.assertEqual(1, child_text.count("## Completion Contract"))
                contract = contracts.parse_completion_contract(
                    child_text, session_id=child_id
                )
                self.assertEqual(
                    ["parent-suite"],
                    [check.name for check in contract.verify.checks],
                )

            manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
            self.assertIn("**Sessions**: 4", manifest)
            metadata = plans.read_readiness_metadata(plan_dir)
            self.assertEqual("draft", metadata.state)
            self.assertEqual(
                plans.stable_readiness_sha256(plan_dir), metadata.readiness_sha256
            )
            self.assertEqual(
                metadata.readiness_sha256, result["readiness"]["readiness_sha256"]
            )

            next_session = (plan_dir / "session-3.md").read_text(encoding="utf-8")
            self.assertIn(
                authoring.previous_session_reference_line("2.2"), next_session
            )

    def test_resplit_without_parent_contract_rejects_no_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir, parent_contract=False)

            with self.assertRaises(self.resplit.ResplitError) as raised:
                self.resplit.resplit_session(
                    plan_dir, session_id="2", parts=2, state_dir=state_dir
                )

            self.assertEqual("no-contract", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / authoring.ARCHIVE_ROOT_NAME).exists())

    def test_resplit_rejects_ambiguous_check_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            with self.assertRaises(self.resplit.ResplitError) as raised:
                self.resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    child_specs=[
                        {
                            "title": "Alpha",
                            "instructions": "Do alpha.",
                            "done_when": ["Alpha lands."],
                            "inherit_checks": ["missing-check"],
                        },
                        {
                            "title": "Beta",
                            "instructions": "Do beta.",
                            "done_when": ["Beta lands."],
                            "inherit_checks": ["parent-suite"],
                        },
                    ],
                    state_dir=state_dir,
                )

            self.assertEqual("ambiguous-contract", raised.exception.reason)
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / authoring.ARCHIVE_ROOT_NAME).exists())

    def test_resplit_rolls_back_archive_when_transaction_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)
            parent_text = (plan_dir / "session-2.md").read_text(encoding="utf-8")

            with (
                mock.patch.object(
                    authoring.PlanTransaction,
                    "commit",
                    side_effect=plans.PlanError("simulated commit failure"),
                ),
                self.assertRaises(plans.PlanError),
            ):
                self.resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    parts=2,
                    state_dir=state_dir,
                    now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
                )

            self.assertEqual(
                parent_text,
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / authoring.ARCHIVE_ROOT_NAME).exists())
            # The plan is still resplittable after rollback (lock released,
            # parent restored), so a retry succeeds.
            retry = self.resplit.resplit_session(
                plan_dir, session_id="2", parts=2, state_dir=state_dir
            )
            self.assertTrue(retry["ok"])


if __name__ == "__main__":
    unittest.main()
