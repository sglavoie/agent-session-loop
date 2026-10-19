"""Test asl assurance cli disposition."""

from __future__ import annotations

import hashlib
import unittest

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_inbox,
)
from agent_session_loop import assurance_projection as projection
from tests import support_asl_assurance_cli as fixtures


class AssuranceIdentityTests(fixtures.AssuranceCliTestCase):
    #: The fields list and show published before issues carried an identity.
    LIST_ROW_FIELDS = frozenset(
        {
            "issue_id",
            "severity",
            "state",
            "occurrence_count",
            "first_seen_at",
            "last_seen_at",
            "component",
            "summary",
        }
    )
    SHOW_ISSUE_FIELDS = frozenset(
        {
            "issue_id",
            "state",
            "severity",
            "reopened_count",
            "occurrence_count",
            "first_seen_at",
            "last_seen_at",
            "fingerprint",
            "fingerprints",
            "schema_version",
            "category",
            "component",
            "invariant_key",
            "lane",
            "summary",
            "recommendation",
            "evidence",
            "merged_from",
            "occurrences",
            "dispositions",
            "plans",
        }
    )

    def observe_unbound(self, **overrides) -> None:
        """Open an issue the way a guard does, which leaves it unnamed."""
        ledger.append_issue_observation(
            assurance_models_inbox.IssueObservation(
                run_id=fixtures.THIRD_RUN_ID,
                finding=fixtures._finding(**overrides),
                observed_at=fixtures.LAST_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=fixtures.LAST_TIMESTAMP,
        )

    def test_a_list_row_names_the_identity_or_says_there_is_none(self) -> None:
        self.publish(fixtures._publication())
        self.observe_unbound(invariant_key="scope.no-escape")

        code, payload, err = self.run_json(["assurance", "list"])

        self.assertEqual(0, code, err)
        self.assertEqual(
            {"ASI-1": "issue.asi-1", "ASI-2": None},
            {row["issue_id"]: row["primary_identity_key"] for row in payload["issues"]},
        )

    def test_show_reports_the_identity_and_the_keys_it_absorbed(self) -> None:
        self.open_three_issues()
        projection.record_merge(
            "ASI-1",
            "ASI-2",
            state_dir=self.state_dir,
            recorded_at=fixtures.LAST_TIMESTAMP,
        )

        code, payload, err = self.run_json(["assurance", "show", "ASI-2"])

        self.assertEqual(0, code, err)
        self.assertEqual("issue.asi-2", payload["issue"]["primary_identity_key"])
        self.assertEqual(
            ["issue.asi-1", "issue.asi-2"], payload["issue"]["identity_keys"]
        )
        _, out, _ = self.run_cli(["assurance", "show", "ASI-2"])
        self.assertIn("identity key    issue.asi-2", out)
        self.assertIn("also named by   issue.asi-1", out)

    def test_show_calls_an_unbound_issue_unbound(self) -> None:
        self.observe_unbound()

        code, out, err = self.run_cli(["assurance", "show", "ASI-1"])

        self.assertEqual(0, code, err)
        self.assertIn("identity key    unbound", out)
        self.assertNotIn("also named by", out)

    def test_the_fields_list_and_show_already_published_are_still_there(self) -> None:
        self.publish(fixtures._publication())
        _, listed, _ = self.run_json(["assurance", "list"])
        _, shown, _ = self.run_json(["assurance", "show", "ASI-1"])
        self.assertLessEqual(self.LIST_ROW_FIELDS, set(listed["issues"][0]))
        self.assertLessEqual(self.SHOW_ISSUE_FIELDS, set(shown["issue"]))


class AssuranceDamagedLedgerTests(fixtures.AssuranceCliTestCase):
    def corrupt_between_records(self) -> None:
        """Write a line no parser can read, then keep appending valid records."""
        self.publish(fixtures._publication())
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self.publish(
            fixtures._publication(
                run_id=fixtures.OTHER_RUN_ID,
                findings=(fixtures._finding(invariant_key="scope.no-escape"),),
                completed_at=fixtures.LATER_TIMESTAMP,
            ),
            recorded_at=fixtures.LATER_TIMESTAMP,
        )

    def test_list_warns_about_rejected_lines_and_keeps_going(self) -> None:
        self.corrupt_between_records()
        code, out, err = self.run_cli(["assurance", "list"])
        self.assertEqual(0, code, err)
        self.assertIn("warning", err)
        self.assertIn("rejected", err)
        self.assertEqual({"ASI-1", "ASI-2"}, set(self.list_rows(out)))

    def test_json_carries_the_same_warnings_the_text_form_prints(self) -> None:
        self.corrupt_between_records()
        code, payload, err = self.run_json(["assurance", "list"])
        self.assertEqual(0, code, err)
        self.assertEqual("", err)
        self.assertEqual(1, payload["rejected_count"])
        self.assertEqual(1, payload["warning_count"])
        self.assertEqual(0, payload["warnings_omitted"])
        self.assertEqual("rejected-line", payload["warnings"][0]["kind"])
        self.assertEqual(ledger.REJECT_INVALID_JSON, payload["warnings"][0]["reason"])

    def test_show_warns_before_reporting_an_id_it_cannot_resolve(self) -> None:
        self.corrupt_between_records()
        code, out, err = self.run_cli(["assurance", "show", "ASI-9"])
        self.assertEqual(1, code)
        self.assertEqual("", out)
        warning_line = err.index("warning")
        self.assertLess(warning_line, err.index("names no issue"))

    def test_show_warns_while_still_printing_a_readable_issue(self) -> None:
        self.corrupt_between_records()
        code, out, err = self.run_cli(["assurance", "show", "ASI-2"])
        self.assertEqual(0, code, err)
        self.assertIn("warning", err)
        self.assertIn("Issue ASI-2", out)


class AssuranceReadOnlyTests(fixtures.AssuranceCliTestCase):
    def state_digests(self) -> dict[str, str]:
        root = self.ledger_path.parent
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_browsing_never_writes_to_the_state_directory(self) -> None:
        self.open_three_issues()
        before = self.state_digests()
        self.assertIn(self.ledger_path.name, before)
        for argv in (
            ["assurance", "list"],
            ["assurance", "list", "--all"],
            ["assurance", "show", "ASI-2"],
            ["assurance", "show", "ASI-99"],
        ):
            self.run_cli(argv)
            self.run_cli([*argv, "--json"])
        self.assertEqual(before, self.state_digests())


class AssuranceDispositionTests(fixtures.AssuranceCliTestCase):
    def test_every_disposition_is_wired_to_the_shared_handler(self) -> None:
        for kind in ("resolve", "dismiss", "accept-risk"):
            with self.subTest(kind=kind):
                args = self.parser.parse_args(["assurance", kind, "42"])
                self.assertEqual(
                    "assurance_cli:assurance_disposition_from_args", args.handler_ref
                )
                self.assertEqual(kind, args.disposition_kind)
                self.assertIsNone(args.note)

    def test_each_disposition_moves_the_issue_and_stores_its_note(self) -> None:
        for kind, expected in (
            ("resolve", "resolved"),
            ("dismiss", "dismissed"),
            ("accept-risk", "accepted-risk"),
        ):
            with self.subTest(kind=kind):
                self.setUp()
                self.open_three_issues()

                code, payload, _ = self.run_json(
                    ["assurance", kind, "1", "--note", "operator said so"]
                )

                self.assertEqual(0, code)
                self.assertEqual(expected, payload["result_state"])
                self.assertEqual("operator said so", payload["note"])
                issue = self.inbox().issue("ASI-1")
                self.assertEqual(expected, issue.state)
                self.assertEqual("operator said so", issue.dispositions[-1].note)

    def test_the_text_form_says_what_the_state_claims_about_the_future(self) -> None:
        self.open_three_issues()

        _, resolved, _ = self.run_cli(["assurance", "resolve", "1"])
        self.setUp()
        self.open_three_issues()
        _, dismissed, _ = self.run_cli(["assurance", "dismiss", "1"])

        self.assertIn("reopen", resolved)
        self.assertIn("without reopening", dismissed)

    def test_revising_a_decision_appends_rather_than_rewrites(self) -> None:
        self.open_three_issues()

        self.run_cli(["assurance", "dismiss", "1", "--note", "noise"])
        self.run_cli(["assurance", "resolve", "1", "--note", "fixed after all"])

        issue = self.inbox().issue("ASI-1")
        self.assertEqual(
            ["dismiss", "resolve"], [item.kind for item in issue.dispositions]
        )
        self.assertEqual("resolved", issue.state)

    def test_a_disposition_on_a_merged_id_reaches_the_issue_that_owns_it(self) -> None:
        self.open_three_issues()
        self.run_cli(["assurance", "merge", "2", "1", "--yes"])

        code, payload, _ = self.run_json(["assurance", "resolve", "2"])

        self.assertEqual(0, code)
        self.assertEqual("ASI-2", payload["requested_issue_id"])
        self.assertEqual("ASI-1", payload["issue_id"])
        self.assertTrue(payload["merged"])
        self.assertEqual("resolved", self.inbox().issue("ASI-1").state)

    def test_an_unknown_issue_is_refused_with_a_way_forward(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(["assurance", "resolve", "99"])

        self.assertEqual(1, code)
        self.assertEqual("unknown-issue", payload["reason"])
        self.assertEqual((), self.inbox().issue("ASI-1").dispositions)

    def test_an_unusable_issue_id_is_refused_before_the_ledger_is_touched(self) -> None:
        code, payload, _ = self.run_json(["assurance", "dismiss", "0"])

        self.assertEqual(1, code)
        self.assertEqual("invalid-issue-id", payload["reason"])
        self.assertFalse(self.ledger_path.exists())


if __name__ == "__main__":
    unittest.main()
