"""Completion contract and session file rendering tests.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    authoring_parse,
    authoring_render,
    contracts,
    plans_errors,
    plans_handoff,
    plans_models,
    plans_progress,
    plans_session_ids,
    plans_session_scope,
    workspaces,
)
from tests.asl_authoring_support import PlanAuthoringBase


class PlanRenderingTests(PlanAuthoringBase):
    def test_render_completion_contract_round_trips(self) -> None:
        contract = self._command_contract("2")
        rendered = authoring_render.render_completion_contract(contract)
        parsed = contracts.parse_completion_contract(
            rendered + "\n" + plans_handoff.HANDOFF_HEADING + "\n",
            session_id=plans_session_ids.parse_session_id("2"),
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(("Tests pass.",), parsed.done_when)
        self.assertEqual("c1", parsed.verify.checks[0].name)
        self.assertEqual(
            ("python3", "-m", "unittest", "-q"), parsed.verify.checks[0].argv
        )

    def test_render_completion_contract_emits_nondefault_baseline(self) -> None:
        contract = contracts.CompletionContract(
            session_id=plans_session_ids.parse_session_id("2"),
            done_when=("done",),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.FileExistsCheck(
                        name="f1",
                        path="agent_session_loop/authoring_render.py",
                        baseline_expectation="pass",
                    ),
                )
            ),
        )
        rendered = authoring_render.render_completion_contract(contract)
        self.assertIn('"baseline_expectation": "pass"', rendered)
        parsed = contracts.parse_completion_contract(
            rendered + "\n" + plans_handoff.HANDOFF_HEADING + "\n",
            session_id=plans_session_ids.parse_session_id("2"),
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
            session_id=plans_session_ids.parse_session_id("2"),
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

        with self.assertRaisesRegex(
            plans_errors.PlanError, "without a repository registry"
        ):
            authoring_render.render_completion_contract(contract)

        rendered = authoring_render.render_completion_contract(
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

    def test_rendered_checks_emit_only_keys_the_parser_allows(self) -> None:
        # The parser fails closed on an unknown key, so anything this renderer
        # emits must already be inside the allowlist or the tool would author
        # contracts it cannot read back.
        registry = workspaces.RepositoryRegistry(
            (workspaces.Repository("SVC", "svc", "https://example.com/svc.git"),)
        )
        contract = contracts.CompletionContract(
            session_id=plans_session_ids.parse_session_id("2"),
            done_when=("done",),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c1",
                        argv=("python3", "-V"),
                        timeout_seconds=10,
                        cwd="sub",
                        baseline_expectation="fail",
                        repo="SVC",
                        review_replay=True,
                    ),
                    contracts.FileExistsCheck(
                        name="f1",
                        path="README.md",
                        baseline_expectation="pass",
                        on_failure_of=("c1",),
                        repo="SVC",
                    ),
                    contracts.RegexInFileCheck(
                        name="r1",
                        path="main.py",
                        pattern="main",
                        baseline_expectation="pass",
                        on_failure_of=("c1",),
                        repo="SVC",
                    ),
                )
            ),
        )

        rendered = authoring_render.render_completion_contract(
            contract, repository_registry=registry
        )
        block = rendered.split("```json\n", 1)[1].split("```", 1)[0]
        for check in json.loads(block)["checks"]:
            with self.subTest(check=check["name"]):
                self.assertEqual(
                    set(),
                    set(check) - contracts._ALLOWED_CHECK_KEYS[check["type"]],
                )

        reparsed = contracts.parse_completion_contract(
            rendered, session_id="2", repository_registry=registry
        )
        self.assertEqual(
            ["c1", "f1", "r1"], [check.name for check in reparsed.verify.checks]
        )

    def test_render_session_file_requires_contract_by_default(self) -> None:
        with self.assertRaises(plans_errors.PlanError):
            authoring_render.render_session_file(
                session_id="1", title="First", instruction_lines=["body"]
            )

    def test_render_session_file_rejects_mismatched_contract(self) -> None:
        with self.assertRaises(plans_errors.PlanError):
            authoring_render.render_session_file(
                session_id="1",
                title="First",
                instruction_lines=["body"],
                contract=self._command_contract("2"),
            )

    def test_render_session_file_replaces_inline_completion_contract(self) -> None:
        instruction_lines = [
            "Do the first thing.",
            "",
            *self._inline_contract_lines("inline-gate"),
            "",
            "---",
        ]
        rendered = authoring_render.render_session_file(
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
        rendered = authoring_render.render_session_file(
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
                plans_handoff.HANDOFF_HEADING,
                "",
            ]
        )
        with self.assertRaisesRegex(
            plans_errors.PlanError, r"has 2 '## Completion Contract' sections"
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
                plans_handoff.HANDOFF_HEADING,
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
        rendered = authoring_render.render_session_file(
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
        rendered = authoring_render.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=["Do the first thing."],
            is_first=True,
            contract=self._command_contract("1"),
        )
        self.assertIn("# Session 1: First", rendered)
        self.assertIn(authoring_render.CONTEXT_REFERENCE_LINE, rendered)
        self.assertNotIn("Previous session", rendered)
        self.assertIn("## Completion Contract", rendered)
        # The handoff ends with the optional-headings documentation comment that
        # follows the empty `- **Notes**:` line.
        self.assertIn("- **Notes**:\n", rendered)
        self.assertTrue(rendered.rstrip().endswith("-->"))
        self.assertIn(plans_handoff.DISCOVERED_FACTS_HEADING, rendered)
        self.assertIn(plans_handoff.OPEN_QUESTIONS_HEADING, rendered)

    def test_render_session_file_places_scope_before_instruction_separator(
        self,
    ) -> None:
        scope = plans_session_scope.SessionScope(reads=("SVC", "OAK"), writes=("SVC",))
        rendered = authoring_render.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=["Do the first thing."],
            is_first=True,
            contract=self._command_contract("1"),
            scope=scope,
        )

        self.assertIn(
            authoring_render.CONTEXT_REFERENCE_LINE
            + "\n\n**Reads repositories**: SVC, OAK\n"
            + "**Writes repositories**: SVC\n\n---",
            rendered,
        )
        self.assertEqual(
            scope,
            plans_session_scope.parse_session_scope(rendered, ("SVC", "OAK")),
        )

    def test_handoff_template_documentation_comment_is_ignored_by_parser(
        self,
    ) -> None:
        rendered = authoring_render.render_session_file(
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
            session_file = plans_models.SessionFile(
                session_id=plans_session_ids.parse_session_id("1"), path=session_path
            )

            # The fresh template's documentation comment names the optional
            # headings and a `- ` bullet example; none of it may leak into the
            # parsed handoff.
            fresh = plans_handoff.read_session_handoff(session_file)
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
                    plans_handoff.DISCOVERED_FACTS_HEADING,
                    "- a verified fact",
                    "",
                    plans_handoff.OPEN_QUESTIONS_HEADING,
                    "- an open question",
                    "",
                ]
            )
            session_path.write_text(completed, encoding="utf-8")
            handoff = plans_handoff.read_session_handoff(session_file)
            self.assertEqual("complete", handoff.status)
            self.assertEqual(("a verified fact",), handoff.discovered_facts)
            self.assertEqual(("an open question",), handoff.open_questions)

    def test_render_session_file_includes_previous_reference(self) -> None:
        rendered = authoring_render.render_session_file(
            session_id="2",
            title="Second",
            instruction_lines=["Do the second thing."],
            previous_session_id="1",
            contract=self._command_contract("2"),
        )
        self.assertIn("bottom of `session-1.md`.", rendered)

    def test_render_session_file_normalizes_deeper_accepted_heading(self) -> None:
        parsed = authoring_parse.parse_plan_document(
            "##### Session 004 — Deep ###\n\nIntro.\n\n###### Changes\n\n- item\n"
        )
        session = parsed.sessions[0]
        rendered = authoring_render.render_session_file(
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
        rendered = authoring_render.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=body,
            is_first=True,
            contract=self._command_contract("1"),
        )
        self.assertIn("x = 1  # not a heading", rendered)

    def test_render_context_uses_template_when_empty(self) -> None:
        rendered = authoring_render.render_context([], "/plans/PLAN.md")
        self.assertIn("_No shared context section found", rendered)
        self.assertIn("`/plans/PLAN.md`", rendered)

    def test_render_context_preserves_lines(self) -> None:
        rendered = authoring_render.render_context(
            ["", "# Plan", "", "Shared context.", ""], "/plans/PLAN.md"
        )
        self.assertEqual("# Plan\n\nShared context.\n", rendered)

    def test_render_progress_skeleton_is_header_only(self) -> None:
        self.assertEqual(
            plans_progress.PROGRESS_HEADER, authoring_render.render_progress_skeleton()
        )
        self.assertNotIn("## Session", authoring_render.render_progress_skeleton())


if __name__ == "__main__":
    unittest.main()
