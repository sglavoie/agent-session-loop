"""Test asl assurance cli plan safety."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agent_session_loop import assurance_draft as draft
from agent_session_loop import (
    state,
)
from tests import support_asl_assurance_cli as fixtures


class AssurancePlanTests(fixtures.AssurancePlanCase):
    def test_the_turn_runs_read_only_from_a_throwaway_directory(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            code, _, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        self.assertEqual(1, len(calls))
        call = calls[0]
        self.assertEqual("claude", call["name"])
        self.assertIsNone(call["plan_dir"])
        self.assertFalse(call["unsafe_bypass"])
        self.assertEqual("audit-read-only", call["access"].name)
        self.assertEqual(draft.DEFAULT_DRAFT_TIMEOUT_SECONDS, call["timeout"])
        self.assertNotIn(self.state_dir, call["cwd"].parents)
        self.assertEqual(
            tuple(draft.draft_read_roots([])), call["access"].read_roots[-2:]
        )

    def test_the_prompt_carries_the_issue_and_forbids_running_anything(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            self.run_json(self.plan_argv())

        prompt = calls[0]["prompt"]
        self.assertIn("ASI-1", prompt)
        self.assertIn("retry.budget-respected", prompt)
        self.assertIn(fixtures.RUN_ID, prompt)
        self.assertIn("asl run", prompt)
        self.assertIn("any change refuses the draft outright", prompt)
        for section in draft.REQUIRED_SECTIONS:
            self.assertIn(f"- ## {section}", prompt)

    def test_the_drafter_reads_the_run_directories_holding_its_evidence(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha", fixtures.RUN_ID)

        with self.drafting() as calls:
            code, payload, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        self.assertEqual([fixtures.RUN_ID], payload["evidence_run_ids"])
        self.assertIn(
            fixtures.RUN_ID,
            {Path(root).name for root in payload["read_roots"]},
        )
        self.assertEqual(
            [Path(root) for root in payload["read_roots"]],
            list(calls[0]["access"].read_roots),
        )

    def test_a_drafter_that_changed_a_read_only_root_is_disbelieved(self) -> None:
        self.open_three_issues()
        plan_dir = self.seed_run_directory("alpha", fixtures.RUN_ID)
        run_dir = (
            state.plan_state_dir(plan_dir, self.state_dir)
            / state.RUNS_DIR_NAME
            / fixtures.RUN_ID
        )

        def write_under_evidence() -> None:
            (run_dir / "edited.txt").write_text("changed\n", encoding="utf-8")

        with self.drafting(during=write_under_evidence):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("mutation-detected", payload["reason"])
        self.assertIn(str(run_dir), payload["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_a_drafter_that_wrote_the_output_itself_is_disbelieved(self) -> None:
        self.open_three_issues()

        def write_the_output() -> None:
            self.output.write_text("written by the agent\n", encoding="utf-8")

        with self.drafting(during=write_the_output):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("mutation-detected", payload["reason"])
        self.assertIn(str(self.output), payload["error"])
        self.assertEqual(
            "written by the agent\n", self.output.read_text(encoding="utf-8")
        )
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_a_mutation_is_reported_ahead_of_the_exit_code(self) -> None:
        self.open_three_issues()

        with self.drafting(
            returncode=1, during=lambda: self.output.write_text("x", encoding="utf-8")
        ):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("mutation-detected", payload["reason"])

    def test_drafting_cannot_reach_the_commands_that_change_a_repository(self) -> None:
        """The refusal to self-modify is enforced by the import graph, not a rule."""
        tree = ast.parse(Path(draft.__file__).read_text(encoding="utf-8"))
        siblings: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.module is None:
                    siblings.update(alias.name for alias in node.names)
                else:
                    siblings.add(node.module.split(".")[0])

        self.assertEqual(
            {
                "assurance_ledger",
                "assurance_models_artifacts",
                "assurance_models_inbox",
                "assurance_models_primitives",
                "assurance_models_vocab",
                "assurance_projection",
                # Refusal wording only; it imports nothing beyond the stdlib.
                "cli_parser_support",
                # The plan parser's fence rule; it imports only session ids.
                "contract_models",
                "harnesses",
                "state",
                "state_plan_refs",
            },
            siblings,
        )
        source = Path(draft.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "git_ops", "runner", "plans", "authoring"):
            self.assertNotIn(f"import {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
