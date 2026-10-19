"""Assurance CLI tests: browsing, dispositions, merges, closeouts, drafting."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from agent_session_loop import (
    assurance_cli,
    cli,
)
from agent_session_loop import assurance_projection as projection
from tests import support_asl_assurance_cli as fixtures


class AssuranceWiringTests(fixtures.AssuranceCliTestCase):
    def test_list_subcommand_is_wired_with_documented_defaults(self) -> None:
        args = self.parser.parse_args(["assurance", "list"])
        self.assertEqual("assurance_cli:assurance_list_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), assurance_cli.assurance_list_from_args)
        self.assertIsNone(args.limit)
        self.assertIsNone(args.state)
        self.assertIsNone(args.severity)
        self.assertFalse(args.all)
        self.assertFalse(args.json)

    def test_show_subcommand_is_wired_and_takes_one_issue(self) -> None:
        args = self.parser.parse_args(["assurance", "show", "ASI-3"])
        self.assertEqual("assurance_cli:assurance_show_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), assurance_cli.assurance_show_from_args)
        self.assertEqual("ASI-3", args.issue)

    def test_assurance_requires_a_subcommand(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.parser.parse_args(["assurance"])
        self.assertEqual(2, raised.exception.code)


class AssuranceListTests(fixtures.AssuranceCliTestCase):
    def test_missing_ledger_lists_nothing_rather_than_failing(self) -> None:
        code, out, err = self.run_cli(["assurance", "list"])
        self.assertEqual(0, code, err)
        self.assertIn("No matching issues.", out)
        self.assertFalse(self.ledger_path.exists())

    def test_rows_are_ordered_by_most_recent_sighting(self) -> None:
        self.open_three_issues()
        code, out, err = self.run_cli(["assurance", "list"])
        self.assertEqual(0, code, err)
        self.assertEqual(["ASI-3", "ASI-2", "ASI-1"], self.list_rows(out))

    def test_rows_carry_the_columns_an_operator_triages_by(self) -> None:
        self.open_three_issues()
        code, out, err = self.run_cli(["assurance", "list"])
        self.assertEqual(0, code, err)
        row = next(line for line in out.splitlines() if line.startswith("  ASI-3"))
        self.assertIn("low", row)
        self.assertIn("open", row)
        self.assertIn("x1", row)
        self.assertIn(fixtures.LAST_TIMESTAMP, row)
        self.assertIn("agent_session_loop/mailbox.py", row)
        self.assertIn("A retry ran after the budget was exhausted.", row)

    def test_default_view_hides_issues_that_are_no_longer_open(self) -> None:
        self.open_three_issues()
        projection.record_disposition(
            "ASI-1",
            "dismiss",
            state_dir=self.state_dir,
            recorded_at=fixtures.LAST_TIMESTAMP,
        )
        code, out, err = self.run_cli(["assurance", "list"])
        self.assertEqual(0, code, err)
        self.assertEqual(["ASI-3", "ASI-2"], self.list_rows(out))

    def test_state_filter_selects_one_lifecycle_state(self) -> None:
        self.open_three_issues()
        projection.record_disposition(
            "ASI-1",
            "dismiss",
            state_dir=self.state_dir,
            recorded_at=fixtures.LAST_TIMESTAMP,
        )
        code, payload, err = self.run_json(
            ["assurance", "list", "--state", "dismissed"]
        )
        self.assertEqual(0, code, err)
        self.assertEqual(["ASI-1"], [row["issue_id"] for row in payload["issues"]])

    def test_severity_filter_narrows_within_the_default_state(self) -> None:
        self.open_three_issues()
        code, payload, err = self.run_json(["assurance", "list", "--severity", "low"])
        self.assertEqual(0, code, err)
        self.assertEqual(["ASI-3"], [row["issue_id"] for row in payload["issues"]])
        self.assertEqual("open", payload["filters"]["state"])

    def test_limit_caps_rows_and_reports_what_it_hid(self) -> None:
        self.open_three_issues()
        code, out, err = self.run_cli(["assurance", "list", "--limit", "2"])
        self.assertEqual(0, code, err)
        self.assertEqual(["ASI-3", "ASI-2"], self.list_rows(out))
        self.assertIn("1 more matching issue(s) not shown", out)

    def test_default_limit_is_fifty_rows(self) -> None:
        self.open_three_issues()
        code, payload, err = self.run_json(["assurance", "list"])
        self.assertEqual(0, code, err)
        self.assertEqual(50, payload["filters"]["limit"])

    def test_all_removes_both_the_state_filter_and_the_row_cap(self) -> None:
        self.open_three_issues()
        projection.record_disposition(
            "ASI-1",
            "dismiss",
            state_dir=self.state_dir,
            recorded_at=fixtures.LAST_TIMESTAMP,
        )
        code, payload, err = self.run_json(["assurance", "list", "--all"])
        self.assertEqual(0, code, err)
        self.assertIsNone(payload["filters"]["state"])
        self.assertIsNone(payload["filters"]["limit"])
        self.assertEqual(
            {"ASI-1", "ASI-2", "ASI-3"},
            {row["issue_id"] for row in payload["issues"]},
        )
        self.assertEqual(0, payload["omitted_count"])

    def test_all_refuses_to_guess_against_limit_or_state(self) -> None:
        for extra in (["--limit", "2"], ["--state", "open"]):
            with self.subTest(extra=extra):
                code, payload, _ = self.run_json(["assurance", "list", "--all", *extra])
                self.assertEqual(1, code)
                self.assertEqual("input-error", payload["reason"])

    def test_unusable_filter_values_are_refused_before_the_ledger_is_read(
        self,
    ) -> None:
        cases = {
            "limit": ["--limit", "0"],
            "state": ["--state", "closed"],
            "severity": ["--severity", "urgent"],
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                code, payload, _ = self.run_json(["assurance", "list", *extra])
                self.assertEqual(1, code)
                self.assertEqual("input-error", payload["reason"])
                self.assertFalse(payload["ok"])

    def test_text_and_json_agree_about_which_issues_matched(self) -> None:
        self.open_three_issues()
        _, out, _ = self.run_cli(["assurance", "list"])
        _, payload, _ = self.run_json(["assurance", "list"])
        self.assertEqual(
            self.list_rows(out), [row["issue_id"] for row in payload["issues"]]
        )


class AssuranceShowTests(fixtures.AssuranceCliTestCase):
    def test_show_reports_state_fingerprint_history_runs_and_artifacts(
        self,
    ) -> None:
        self.publish(fixtures._publication())
        code, out, err = self.run_cli(["assurance", "show", "ASI-1"])
        self.assertEqual(0, code, err)
        self.assertIn("Issue ASI-1", out)
        self.assertIn("state open", out)
        self.assertIn("retry.budget-respected", out)
        self.assertIn("lifecycle-retry-resume", out)
        self.assertIn("1 occurrence(s)", out)
        self.assertIn(fixtures.RUN_ID, out)
        self.assertIn("supervisor-audit-input.json", out)
        self.assertIn(fixtures.INPUT_DIGEST, out)

    def test_canonical_and_shorthand_ids_name_the_same_issue(self) -> None:
        self.open_three_issues()
        _, canonical, _ = self.run_json(["assurance", "show", "ASI-2"])
        _, shorthand, _ = self.run_json(["assurance", "show", "2"])
        self.assertEqual(canonical, shorthand)
        self.assertEqual("ASI-2", shorthand["issue_id"])
        self.assertEqual("ASI-2", shorthand["requested_issue_id"])

    def test_every_id_printed_by_list_pastes_into_show(self) -> None:
        self.open_three_issues()
        projection.record_disposition(
            "ASI-1",
            "resolve",
            state_dir=self.state_dir,
            recorded_at=fixtures.LAST_TIMESTAMP,
        )
        _, listed, _ = self.run_json(["assurance", "list", "--all"])
        printed = [row["issue_id"] for row in listed["issues"]]
        self.assertEqual(3, len(printed))
        for issue_id in printed:
            with self.subTest(issue_id=issue_id):
                code, shown, err = self.run_json(["assurance", "show", issue_id])
                self.assertEqual(0, code, err)
                self.assertEqual(issue_id, shown["issue_id"])

    def test_text_and_json_show_the_same_issue_facts(self) -> None:
        self.open_three_issues()
        _, out, _ = self.run_cli(["assurance", "show", "ASI-1"])
        _, payload, _ = self.run_json(["assurance", "show", "ASI-1"])
        issue = payload["issue"]
        self.assertIn(f"Issue {issue['issue_id']}", out)
        self.assertIn(f"state {issue['state']}", out)
        self.assertIn(f"severity {issue['severity']}", out)
        self.assertIn(issue["fingerprint"], out)
        self.assertIn(issue["summary"], out)
        self.assertIn(issue["recommendation"], out)
        for occurrence in issue["occurrences"]:
            self.assertIn(occurrence["run_id"], out)

    def test_show_follows_a_merge_and_names_the_id_it_landed_on(self) -> None:
        self.open_three_issues()
        projection.record_merge(
            "ASI-1",
            "ASI-2",
            state_dir=self.state_dir,
            recorded_at=fixtures.LAST_TIMESTAMP,
        )
        code, payload, err = self.run_json(["assurance", "show", "ASI-1"])
        self.assertEqual(0, code, err)
        self.assertEqual("ASI-1", payload["requested_issue_id"])
        self.assertEqual("ASI-2", payload["issue_id"])
        self.assertTrue(payload["merged"])
        self.assertIn("ASI-1", payload["issue"]["merged_from"])
        _, out, _ = self.run_cli(["assurance", "show", "ASI-1"])
        self.assertIn("ASI-1 was merged into ASI-2.", out)

    def test_show_surfaces_dispositions_and_reopen_counts(self) -> None:
        self.publish(fixtures._publication())
        projection.record_disposition(
            "ASI-1",
            "resolve",
            note="Fixed by the retry clamp.",
            state_dir=self.state_dir,
            recorded_at=fixtures.LATER_TIMESTAMP,
        )
        self.publish(
            fixtures._publication(
                run_id=fixtures.OTHER_RUN_ID,
                completed_at=fixtures.LAST_TIMESTAMP,
            ),
            recorded_at=fixtures.LAST_TIMESTAMP,
        )
        code, out, err = self.run_cli(["assurance", "show", "ASI-1"])
        self.assertEqual(0, code, err)
        self.assertIn("resolve -> resolved", out)
        self.assertIn("Fixed by the retry clamp.", out)
        self.assertIn("reopened 1", out)

    def test_unknown_issue_is_an_error_with_a_way_forward(self) -> None:
        self.publish(fixtures._publication())
        code, out, err = self.run_cli(["assurance", "show", "ASI-99"])
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("ASI-99", err)
        self.assertIn("asl assurance list --all", err)
        code, payload, _ = self.run_json(["assurance", "show", "ASI-99"])
        self.assertEqual(1, code)
        self.assertEqual("unknown-issue", payload["reason"])
        self.assertEqual("ASI-99", payload["details"]["issue_id"])

    def test_unusable_issue_ids_are_refused_before_the_ledger_is_read(
        self,
    ) -> None:
        for issue in ("ASI-0", "-3", "ASI-007", "banana", ""):
            with self.subTest(issue=issue):
                code, payload, _ = self.run_json(["assurance", "show", issue])
                self.assertEqual(1, code)
                self.assertEqual("invalid-issue-id", payload["reason"])


if __name__ == "__main__":
    unittest.main()
