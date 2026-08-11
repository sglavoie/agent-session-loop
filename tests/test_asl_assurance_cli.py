"""Assurance CLI tests: browsing, dispositions, merges, and human closeouts."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_session_loop import assurance_cli
from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
from agent_session_loop import cli
from agent_session_loop import state
from agent_session_loop.assurance_projection import AuditPublication


RUN_ID = "20260810T101112123456Z-4242"
OTHER_RUN_ID = "20260810T101112999999Z-4243"
THIRD_RUN_ID = "20260810T101113111111Z-4244"
TIMESTAMP = "2026-08-10T10:11:12+00:00"
LATER_TIMESTAMP = "2026-08-10T11:11:12+00:00"
LAST_TIMESTAMP = "2026-08-10T12:11:12+00:00"
INPUT_DIGEST = "a" * 64
OTHER_INPUT_DIGEST = "b" * 64


def _finding(**overrides) -> models.Finding:
    payload = {
        "category": "lifecycle",
        "component": "agent_session_loop/runner.py",
        "invariant_key": "retry.budget-respected",
        "lane": "lifecycle-retry-resume",
        "severity": "high",
        "summary": "A retry ran after the budget was exhausted.",
        "recommendation": "Clamp the retry counter before relaunching.",
        "evidence": ("events.jsonl",),
    }
    payload.update(overrides)
    return models.Finding(**payload)


def _artifacts(digest: str | None = INPUT_DIGEST) -> tuple[models.EvidenceRef, ...]:
    return (
        models.EvidenceRef(
            kind="artifact", locator="supervisor-audit-input.json", digest=digest
        ),
        models.EvidenceRef(kind="artifact", locator="supervisor-audit.json"),
    )


def _publication(**overrides) -> AuditPublication:
    payload = {
        "run_id": RUN_ID,
        "state": "findings",
        "eligible": True,
        "findings": (_finding(),),
        "artifacts": _artifacts(),
        "completed_at": TIMESTAMP,
    }
    payload.update(overrides)
    return AuditPublication(**payload)


class AssuranceCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)
        self.parser = cli.build_parser(prog="asl")

    # -- fixtures ---------------------------------------------------------

    def publish(self, publication: AuditPublication, **kwargs):
        kwargs.setdefault("recorded_at", TIMESTAMP)
        return projection.publish_audit(
            publication, state_dir=self.state_dir, **kwargs
        )

    def open_three_issues(self) -> None:
        """ASI-1 seen first, then ASI-2, then ASI-3 as the newest sighting."""
        self.publish(_publication())
        self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(invariant_key="scope.no-escape"),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(
                    _finding(
                        invariant_key="mailbox.no-stale-read",
                        severity="low",
                        component="agent_session_loop/mailbox.py",
                    ),
                ),
                artifacts=(),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )

    def seed_run_directory(self, plan_name: str, run_id: str) -> Path:
        """Record one plan in the state corpus as having produced ``run_id``."""
        plan_dir = Path(self._tmp.name) / "plans" / plan_name
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_state = state.plan_state_dir(plan_dir, self.state_dir)
        (plan_state / state.RUNS_DIR_NAME / run_id).mkdir(parents=True)
        (plan_state / "state.json").write_text(
            json.dumps({"plan_dir": str(plan_dir)}), encoding="utf-8"
        )
        return plan_dir

    def inbox(self) -> projection.AssuranceProjection:
        return projection.read_projection(state_dir=self.state_dir)

    # -- invocation -------------------------------------------------------

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        args = self.parser.parse_args(
            [*argv, "--state-dir", str(self.state_dir)]
        )
        handler = cli.resolve_handler(args)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handler(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, argv: list[str]) -> tuple[int, dict, str]:
        code, out, err = self.run_cli([*argv, "--json"])
        return code, json.loads(out), err

    def list_rows(self, out: str) -> list[str]:
        """Return the issue IDs an operator can copy out of the text rows."""
        return [
            line.split()[0]
            for line in out.splitlines()
            if line.startswith("  ASI-")
        ]


class AssuranceWiringTests(AssuranceCliTestCase):
    def test_list_subcommand_is_wired_with_documented_defaults(self) -> None:
        args = self.parser.parse_args(["assurance", "list"])
        self.assertEqual(
            "assurance_cli:assurance_list_from_args", args.handler_ref
        )
        self.assertIs(
            cli.resolve_handler(args), assurance_cli.assurance_list_from_args
        )
        self.assertIsNone(args.limit)
        self.assertIsNone(args.state)
        self.assertIsNone(args.severity)
        self.assertFalse(args.all)
        self.assertFalse(args.json)

    def test_show_subcommand_is_wired_and_takes_one_issue(self) -> None:
        args = self.parser.parse_args(["assurance", "show", "ASI-3"])
        self.assertEqual(
            "assurance_cli:assurance_show_from_args", args.handler_ref
        )
        self.assertIs(
            cli.resolve_handler(args), assurance_cli.assurance_show_from_args
        )
        self.assertEqual("ASI-3", args.issue)

    def test_assurance_requires_a_subcommand(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.parser.parse_args(["assurance"])
        self.assertEqual(2, raised.exception.code)


class AssuranceListTests(AssuranceCliTestCase):
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
        row = next(
            line for line in out.splitlines() if line.startswith("  ASI-3")
        )
        self.assertIn("low", row)
        self.assertIn("open", row)
        self.assertIn("x1", row)
        self.assertIn(LAST_TIMESTAMP, row)
        self.assertIn("agent_session_loop/mailbox.py", row)
        self.assertIn("A retry ran after the budget was exhausted.", row)

    def test_default_view_hides_issues_that_are_no_longer_open(self) -> None:
        self.open_three_issues()
        projection.record_disposition(
            "ASI-1", "dismiss", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        code, out, err = self.run_cli(["assurance", "list"])
        self.assertEqual(0, code, err)
        self.assertEqual(["ASI-3", "ASI-2"], self.list_rows(out))

    def test_state_filter_selects_one_lifecycle_state(self) -> None:
        self.open_three_issues()
        projection.record_disposition(
            "ASI-1", "dismiss", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        code, payload, err = self.run_json(
            ["assurance", "list", "--state", "dismissed"]
        )
        self.assertEqual(0, code, err)
        self.assertEqual(
            ["ASI-1"], [row["issue_id"] for row in payload["issues"]]
        )

    def test_severity_filter_narrows_within_the_default_state(self) -> None:
        self.open_three_issues()
        code, payload, err = self.run_json(
            ["assurance", "list", "--severity", "low"]
        )
        self.assertEqual(0, code, err)
        self.assertEqual(
            ["ASI-3"], [row["issue_id"] for row in payload["issues"]]
        )
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
            "ASI-1", "dismiss", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
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
                code, payload, _ = self.run_json(
                    ["assurance", "list", "--all", *extra]
                )
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


class AssuranceShowTests(AssuranceCliTestCase):
    def test_show_reports_state_fingerprint_history_runs_and_artifacts(
        self,
    ) -> None:
        self.publish(_publication())
        code, out, err = self.run_cli(["assurance", "show", "ASI-1"])
        self.assertEqual(0, code, err)
        self.assertIn("Issue ASI-1", out)
        self.assertIn("state open", out)
        self.assertIn("retry.budget-respected", out)
        self.assertIn("lifecycle-retry-resume", out)
        self.assertIn("1 occurrence(s)", out)
        self.assertIn(RUN_ID, out)
        self.assertIn("supervisor-audit-input.json", out)
        self.assertIn(INPUT_DIGEST, out)

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
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        _, listed, _ = self.run_json(["assurance", "list", "--all"])
        printed = [row["issue_id"] for row in listed["issues"]]
        self.assertEqual(3, len(printed))
        for issue_id in printed:
            with self.subTest(issue_id=issue_id):
                code, shown, err = self.run_json(
                    ["assurance", "show", issue_id]
                )
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
            recorded_at=LAST_TIMESTAMP,
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
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1",
            "resolve",
            note="Fixed by the retry clamp.",
            state_dir=self.state_dir,
            recorded_at=LATER_TIMESTAMP,
        )
        self.publish(
            _publication(run_id=OTHER_RUN_ID, completed_at=LAST_TIMESTAMP),
            recorded_at=LAST_TIMESTAMP,
        )
        code, out, err = self.run_cli(["assurance", "show", "ASI-1"])
        self.assertEqual(0, code, err)
        self.assertIn("resolve -> resolved", out)
        self.assertIn("Fixed by the retry clamp.", out)
        self.assertIn("reopened 1", out)

    def test_unknown_issue_is_an_error_with_a_way_forward(self) -> None:
        self.publish(_publication())
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


class AssuranceDamagedLedgerTests(AssuranceCliTestCase):
    def corrupt_between_records(self) -> None:
        """Write a line no parser can read, then keep appending valid records."""
        self.publish(_publication())
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(invariant_key="scope.no-escape"),),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
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
        self.assertEqual(
            ledger.REJECT_INVALID_JSON, payload["warnings"][0]["reason"]
        )

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


class AssuranceReadOnlyTests(AssuranceCliTestCase):
    def state_digests(self) -> dict[str, str]:
        root = self.ledger_path.parent
        return {
            str(path.relative_to(root)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
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


class AssuranceDispositionTests(AssuranceCliTestCase):
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


class AssuranceMergeTests(AssuranceCliTestCase):
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

        with mock.patch.object(assurance_cli, "_is_tty", return_value=True), (
            mock.patch("builtins.input", return_value="n")
        ):
            code, out, err = self.run_cli(["assurance", "merge", "2", "1"])

        self.assertEqual(1, code)
        self.assertIn("cancelled", err)
        self.assertEqual(3, len(self.inbox().issues))

    def test_confirming_the_prompt_performs_the_merge(self) -> None:
        self.open_three_issues()

        with mock.patch.object(assurance_cli, "_is_tty", return_value=True), (
            mock.patch("builtins.input", return_value="y")
        ):
            code, _, _ = self.run_cli(["assurance", "merge", "2", "1"])

        self.assertEqual(0, code)
        self.assertEqual("ASI-1", self.inbox().resolve("ASI-2"))

    def test_a_scripted_invocation_is_never_prompted(self) -> None:
        self.open_three_issues()

        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            code, _, _ = self.run_cli(["assurance", "merge", "2", "1"])

        self.assertEqual(0, code)


class AssuranceCloseoutTests(AssuranceCliTestCase):
    def test_closeout_implements_the_documented_interface(self) -> None:
        args = self.parser.parse_args(
            [
                "assurance",
                "closeout",
                RUN_ID,
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
        self.assertEqual(RUN_ID, args.run_id)
        self.assertEqual("reworked", args.outcome)
        self.assertEqual(25.0, args.rework_minutes)
        self.assertEqual(["1", "ASI-2"], args.issue)
        self.assertEqual("worth it", args.note)
        self.assertEqual("some-plan", args.plan)
        self.assertTrue(args.replace)

    def test_outcome_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                self.parser.parse_args(["assurance", "closeout", RUN_ID])

    def test_a_closeout_records_the_outcome_rework_and_linked_issues(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                RUN_ID,
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
        self.assertEqual("reworked", self.inbox().run(RUN_ID).closeout.outcome)

    def test_a_bare_run_id_resolves_through_the_state_corpus(self) -> None:
        self.open_three_issues()
        plan_dir = self.seed_run_directory("alpha-plan", RUN_ID)

        code, payload, _ = self.run_json(
            ["assurance", "closeout", RUN_ID, "--outcome", "accepted"]
        )

        self.assertEqual(0, code)
        self.assertEqual("alpha-plan", payload["plan_id"])
        self.assertEqual(str(plan_dir.resolve()), payload["plan_dir"])

    def test_plan_is_required_only_when_the_run_id_is_ambiguous(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha-plan", RUN_ID)
        self.seed_run_directory("beta-plan", RUN_ID)

        code, payload, _ = self.run_json(
            ["assurance", "closeout", RUN_ID, "--outcome", "accepted"]
        )

        self.assertEqual(1, code)
        self.assertEqual("ambiguous-run", payload["reason"])
        self.assertEqual(2, len(payload["candidates"]))
        self.assertIsNone(self.inbox().run(RUN_ID).closeout)

    def test_plan_disambiguates_a_shared_run_id(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha-plan", RUN_ID)
        self.seed_run_directory("beta-plan", RUN_ID)

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                RUN_ID,
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
        self.seed_run_directory("alpha-plan", RUN_ID)
        self.seed_run_directory("beta-plan", OTHER_RUN_ID)

        code, payload, _ = self.run_json(
            [
                "assurance",
                "closeout",
                RUN_ID,
                "--outcome",
                "accepted",
                "--plan",
                "beta-plan",
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-run", payload["reason"])

    def test_a_run_the_inbox_never_audited_is_refused(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(
            ["assurance", "closeout", "20260101T000000000000Z-1", "--outcome", "accepted"]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-run", payload["reason"])

    def test_a_missing_linked_issue_is_refused(self) -> None:
        self.open_three_issues()

        code, payload, _ = self.run_json(
            ["assurance", "closeout", RUN_ID, "--outcome", "accepted", "--issue", "99"]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-issue", payload["reason"])
        self.assertIsNone(self.inbox().run(RUN_ID).closeout)

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
                    ["assurance", "closeout", RUN_ID, *argv]
                )

                self.assertEqual(1, code)
                self.assertEqual("input-error", payload["reason"])
        self.assertIsNone(self.inbox().run(RUN_ID).closeout)

    def test_a_second_closeout_needs_replace(self) -> None:
        self.open_three_issues()
        self.run_cli(["assurance", "closeout", RUN_ID, "--outcome", "accepted"])

        code, payload, _ = self.run_json(
            ["assurance", "closeout", RUN_ID, "--outcome", "escaped"]
        )

        self.assertEqual(1, code)
        self.assertEqual(projection.DUPLICATE_CLOSEOUT, payload["reason"])
        self.assertEqual("accepted", self.inbox().run(RUN_ID).closeout.outcome)

    def test_a_replacement_names_the_closeout_it_supersedes(self) -> None:
        self.open_three_issues()
        _, first, _ = self.run_json(
            ["assurance", "closeout", RUN_ID, "--outcome", "accepted"]
        )

        code, second, _ = self.run_json(
            ["assurance", "closeout", RUN_ID, "--outcome", "escaped", "--replace"]
        )

        self.assertEqual(0, code)
        self.assertEqual(
            projection.closeout_reference(first["seq"]), second["replaces"]
        )
        recorded = self.inbox().run(RUN_ID)
        self.assertEqual(
            ["accepted", "escaped"], [item.outcome for item in recorded.closeouts]
        )

    def test_the_text_form_reports_what_the_replacement_superseded(self) -> None:
        self.open_three_issues()
        self.run_cli(["assurance", "closeout", RUN_ID, "--outcome", "accepted"])

        code, out, _ = self.run_cli(
            ["assurance", "closeout", RUN_ID, "--outcome", "escaped", "--replace"]
        )

        self.assertEqual(0, code)
        self.assertIn("Supersedes the closeout on ledger line", out)


if __name__ == "__main__":
    unittest.main()
