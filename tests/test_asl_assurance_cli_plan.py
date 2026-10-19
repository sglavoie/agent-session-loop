"""Test asl assurance cli plan."""

from __future__ import annotations

import hashlib
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    assurance_cli,
    cli,
)
from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_projection as projection
from tests import support_asl_assurance_cli as fixtures


class AssurancePlanTests(fixtures.AssurancePlanCase):
    def test_plan_subcommand_is_wired_with_documented_defaults(self) -> None:
        args = self.parser.parse_args(
            ["assurance", "plan", "ASI-1", "--harness", "claude", "--output", "p.md"]
        )

        self.assertEqual("assurance_cli:assurance_plan_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), assurance_cli.assurance_plan_from_args)
        self.assertEqual("ASI-1", args.issue)
        self.assertEqual("claude", args.harness)
        self.assertEqual("p.md", args.output)
        self.assertIsNone(args.harness_model)
        self.assertIsNone(args.effort)
        self.assertIsNone(args.timeout)
        self.assertFalse(args.json)

    def test_a_proposal_needs_a_harness_and_somewhere_to_put_it(self) -> None:
        for argv in (
            ["assurance", "plan", "ASI-1", "--output", "p.md"],
            ["assurance", "plan", "ASI-1", "--harness", "claude"],
            ["assurance", "plan", "--harness", "claude", "--output", "p.md"],
        ):
            with self.subTest(argv=argv):
                with (
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    self.parser.parse_args(argv)
                self.assertEqual(2, raised.exception.code)

    def test_a_valid_reply_becomes_the_proposal_and_then_a_record(self) -> None:
        self.open_three_issues()

        with self.drafting():
            code, payload, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        document = self.output.read_bytes()
        self.assertEqual(fixtures._draft_document(), document.decode("utf-8"))
        self.assertEqual(hashlib.sha256(document).hexdigest(), payload["output_sha256"])
        self.assertEqual(len(document), payload["byte_count"])
        self.assertEqual("ASI-1", payload["issue_id"])
        self.assertFalse(payload["merged"])

        plans = self.drafted_plans("ASI-1")
        self.assertEqual(1, len(plans))
        self.assertEqual(str(self.output), plans[0].output_path)
        self.assertEqual(payload["output_sha256"], plans[0].output_sha256)
        self.assertEqual("claude", plans[0].harness)

    def test_the_record_is_appended_only_after_the_file_is_durable(self) -> None:
        self.open_three_issues()
        observed: dict[str, object] = {}
        real = projection.record_improvement_plan_draft

        def spy(plan, **kwargs):
            path = Path(plan.output_path)
            observed["exists"] = path.exists()
            observed["digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
            return real(plan, **kwargs)

        with (
            self.drafting(),
            mock.patch.object(projection, "record_improvement_plan_draft", spy),
        ):
            code, payload, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        self.assertTrue(observed["exists"])
        self.assertEqual(payload["output_sha256"], observed["digest"])

    def test_a_failed_append_keeps_the_proposal_and_names_where_it_landed(
        self,
    ) -> None:
        self.open_three_issues()
        failure = ledger.LedgerError(
            ledger.ERROR_LEDGER_WRITE, "the ledger is read-only"
        )

        with (
            self.drafting(),
            mock.patch.object(
                projection, "record_improvement_plan_draft", side_effect=failure
            ),
        ):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("ledger-write-failed", payload["reason"])
        self.assertIn(str(self.output), payload["error"])
        self.assertTrue(self.output.exists())
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_a_proposal_for_a_merged_id_is_recorded_against_its_owner(self) -> None:
        self.open_three_issues()
        self.run_cli(["assurance", "merge", "ASI-1", "ASI-2"])

        with self.drafting():
            code, payload, err = self.run_json(self.plan_argv("ASI-1"))

        self.assertEqual(0, code, err)
        self.assertEqual("ASI-1", payload["requested_issue_id"])
        self.assertEqual("ASI-2", payload["issue_id"])
        self.assertTrue(payload["merged"])
        self.assertEqual(1, len(self.drafted_plans("ASI-2")))

    def test_show_reports_the_proposal_that_was_drafted(self) -> None:
        self.open_three_issues()

        with self.drafting():
            self.run_cli(self.plan_argv())
        code, out, err = self.run_cli(["assurance", "show", "ASI-1"])

        self.assertEqual(0, code, err)
        self.assertIn("Improvement plans", out)
        self.assertIn(str(self.output), out)

    def test_text_and_json_agree_about_the_proposal(self) -> None:
        self.open_three_issues()

        with self.drafting():
            code, out, err = self.run_cli(self.plan_argv())
        self.assertEqual(0, code, err)

        plan = self.drafted_plans("ASI-1")[0]
        self.assertIn("Issue: ASI-1", out)
        self.assertIn(str(self.output), out)
        self.assertIn(plan.output_sha256, out)
        self.assertIn("claude", out)
        self.assertIn("Nothing was implemented", out)


if __name__ == "__main__":
    unittest.main()
