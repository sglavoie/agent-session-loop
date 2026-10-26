"""Test asl assurance cli closeout."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from agent_session_loop import (
    assurance_cli,
)
from agent_session_loop import assurance_projection as projection
from tests import support_asl_assurance_cli as fixtures


class AssuranceMergeTests(fixtures.AssuranceCliTestCase):
    def test_merge_subcommand_takes_a_source_and_a_target(self) -> None:
        args = self.parser.parse_args(["assurance", "merge", "9", "7"])

        self.assertEqual("assurance_cli:assurance_merge_from_args", args.handler_ref)
        self.assertEqual("9", args.issue)
        self.assertEqual("7", args.target)
        self.assertFalse(args.yes)

    def test_merge_moves_the_whole_history_into_the_target(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(
            ["assurance", "merge", "2", "1", "--note", "same defect"]
        )

        self.assertEqual(0, code)
        self.assertEqual("ASI-2", payload["source_issue_id"])
        self.assertEqual("ASI-1", payload["target_issue_id"])
        inbox = self.inbox()
        self.assertEqual(["ASI-1", "ASI-3"], [item.issue_id for item in inbox.issues])
        self.assertEqual(2, len(inbox.issue("ASI-1").occurrences))

    def test_merging_is_irreversible(self) -> None:
        self.open_three_issues()
        self.run_cli(["assurance", "merge", "2", "1", "--yes"])

        code, payload, _ = self.run_json(["assurance", "merge", "2", "3", "--yes"])

        self.assertEqual(1, code)
        self.assertEqual(projection.MERGE_SOURCE_ALREADY_MERGED, payload["reason"])
        self.assertEqual("ASI-1", self.inbox().resolve("ASI-2"))

    def test_every_merge_refusal_reaches_the_operator_by_its_own_reason(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(["assurance", "merge", "1", "1", "--yes"])

        self.assertEqual(1, code)
        self.assertEqual(projection.MERGE_SELF, payload["reason"])
        self.assertIn(payload["reason"], projection.MERGE_REJECTIONS)

    def test_a_person_at_a_terminal_is_asked_before_the_irreversible_write(
        self,
    ) -> None:
        self.open_three_issues()

        with (
            mock.patch.object(assurance_cli, "_is_tty", return_value=True),
            mock.patch("builtins.input", return_value="n"),
        ):
            code, out, err = self.run_cli(["assurance", "merge", "2", "1"])

        self.assertEqual(1, code)
        self.assertIn("cancelled", err)
        self.assertEqual(3, len(self.inbox().issues))

    def test_confirming_the_prompt_performs_the_merge(self) -> None:
        self.open_three_issues()

        with (
            mock.patch.object(assurance_cli, "_is_tty", return_value=True),
            mock.patch("builtins.input", return_value="y"),
        ):
            code, _, _ = self.run_cli(["assurance", "merge", "2", "1"])

        self.assertEqual(0, code)
        self.assertEqual("ASI-1", self.inbox().resolve("ASI-2"))

    def test_a_scripted_invocation_is_never_prompted(self) -> None:
        self.open_three_issues()

        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            code, _, _ = self.run_cli(["assurance", "merge", "2", "1"])

        self.assertEqual(0, code)


class AssuranceCloseoutTests(fixtures.AssuranceCliTestCase):
    def test_closeout_implements_the_documented_interface(self) -> None:
        args = self.parser.parse_args(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "reworked",
                "--rework-minutes",
                "25",
                "--issue",
                "1",
                "--issue",
                "ASI-2",
                "--note",
                "worth it",
                "--plan",
                "some-plan",
                "--replace",
            ]
        )

        self.assertEqual("assurance_cli:assurance_closeout_from_args", args.handler_ref)
        self.assertEqual(fixtures.RUN_ID, args.run_id)
        self.assertEqual("reworked", args.outcome)
        self.assertEqual(25.0, args.rework_minutes)
        self.assertEqual(["1", "ASI-2"], args.issue)
        self.assertEqual("worth it", args.note)
        self.assertEqual("some-plan", args.plan)
        self.assertTrue(args.replace)

    def test_outcome_is_required(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.parser.parse_args(["assurance", "closeout", fixtures.RUN_ID])

    def test_a_closeout_records_the_outcome_rework_and_linked_issues(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "reworked",
                "--rework-minutes",
                "25",
                "--issue",
                "1",
                "--issue",
                "ASI-1",
                "--note",
                "clamped the counter",
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual("reworked", payload["outcome"])
        self.assertEqual(25.0, payload["rework_minutes"])
        self.assertEqual(["ASI-1"], payload["issue_ids"])
        self.assertIsNone(payload["replaces"])
        self.assertEqual(
            "reworked",
            self.inbox().run(fixtures.RUN_ID).closeout.outcome,
        )

    def test_one_minute_of_rework_reads_in_the_singular(self) -> None:
        self.open_three_issues()

        code, out, err = self.run_cli(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "reworked",
                "--rework-minutes",
                "1",
            ]
        )

        self.assertEqual(0, code, err)
        self.assertIn("Rework: 1 minute\n", out)

    def test_a_bare_run_id_resolves_through_the_state_corpus(self) -> None:
        self.open_three_issues()
        plan_dir = self.seed_run_directory("alpha-plan", fixtures.RUN_ID)

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual("alpha-plan", payload["plan_id"])
        self.assertEqual(str(plan_dir.resolve()), payload["plan_dir"])

    def test_plan_is_required_only_when_the_run_id_is_ambiguous(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha-plan", fixtures.RUN_ID)
        self.seed_run_directory("beta-plan", fixtures.RUN_ID)

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("ambiguous-run", payload["reason"])
        self.assertEqual(2, len(payload["candidates"]))
        self.assertIsNone(self.inbox().run(fixtures.RUN_ID).closeout)

    def test_plan_disambiguates_a_shared_run_id(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha-plan", fixtures.RUN_ID)
        self.seed_run_directory("beta-plan", fixtures.RUN_ID)

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
                "--plan",
                "beta-plan",
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual("beta-plan", payload["plan_id"])

    def test_a_plan_that_never_produced_the_run_is_refused(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha-plan", fixtures.RUN_ID)
        self.seed_run_directory("beta-plan", fixtures.OTHER_RUN_ID)

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
                "--plan",
                "beta-plan",
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-run", payload["reason"])

    def test_a_plan_label_that_names_no_plan_is_refused(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha-plan", fixtures.RUN_ID)

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
                "--plan",
                "no-such-plan",
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("plan-not-found", payload["reason"])

    def test_a_run_the_inbox_never_audited_is_refused(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                "20260101T000000000000Z-1",
                "--outcome",
                "accepted",
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-run", payload["reason"])

    def test_a_missing_linked_issue_is_refused(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
                "--issue",
                "99",
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-issue", payload["reason"])
        self.assertIsNone(self.inbox().run(fixtures.RUN_ID).closeout)

    def test_unusable_outcomes_and_minutes_are_refused(self) -> None:
        self.open_three_issues()
        for argv in (
            ["--outcome", "partly"],
            ["--outcome", "accepted", "--rework-minutes", "-1"],
            ["--outcome", "accepted", "--rework-minutes", "nan"],
            ["--outcome", "accepted", "--rework-minutes", "inf"],
        ):
            with self.subTest(argv=argv):
                code, payload, _ = self.run_json(
                    ["assurance", "closeout", fixtures.RUN_ID, *argv]
                )

                self.assertEqual(1, code)
                self.assertEqual("input-error", payload["reason"])
        self.assertIsNone(self.inbox().run(fixtures.RUN_ID).closeout)

    def test_a_second_closeout_needs_replace(self) -> None:
        self.open_three_issues()
        self.run_cli(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
            ]
        )

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "escaped",
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual(projection.DUPLICATE_CLOSEOUT, payload["reason"])
        self.assertEqual(
            "accepted",
            self.inbox().run(fixtures.RUN_ID).closeout.outcome,
        )

    def test_a_replacement_names_the_closeout_it_supersedes(self) -> None:
        self.open_three_issues()
        _, first, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
            ]
        )

        code, second, _ = self.run_json(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "escaped",
                "--replace",
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual(
            projection.closeout_reference(first["seq"]), second["replaces"]
        )
        recorded = self.inbox().run(fixtures.RUN_ID)
        self.assertEqual(
            ["accepted", "escaped"], [item.outcome for item in recorded.closeouts]
        )

    def test_the_text_form_reports_what_the_replacement_superseded(self) -> None:
        self.open_three_issues()
        self.run_cli(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "accepted",
            ]
        )

        code, out, _ = self.run_cli(
            [
                "assurance",
                "closeout",
                fixtures.RUN_ID,
                "--outcome",
                "escaped",
                "--replace",
            ]
        )

        self.assertEqual(0, code)
        self.assertIn("Supersedes the closeout on ledger line", out)


if __name__ == "__main__":
    unittest.main()
