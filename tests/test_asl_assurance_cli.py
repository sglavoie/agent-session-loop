"""Assurance CLI tests: browsing, dispositions, merges, closeouts, drafting."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import assurance_cli, cli, harnesses, state
from agent_session_loop import assurance_draft as draft
from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
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
        return projection.publish_audit(publication, state_dir=self.state_dir, **kwargs)

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
        args = self.parser.parse_args([*argv, "--state-dir", str(self.state_dir)])
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
            line.split()[0] for line in out.splitlines() if line.startswith("  ASI-")
        ]


class AssuranceWiringTests(AssuranceCliTestCase):
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
        row = next(line for line in out.splitlines() if line.startswith("  ASI-3"))
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


class AssuranceIdentityTests(AssuranceCliTestCase):
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
            models.IssueObservation(
                run_id=THIRD_RUN_ID,
                finding=_finding(**overrides),
                observed_at=LAST_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=LAST_TIMESTAMP,
        )

    def test_a_list_row_names_the_identity_or_says_there_is_none(self) -> None:
        self.publish(_publication())
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
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
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
        self.publish(_publication())
        _, listed, _ = self.run_json(["assurance", "list"])
        _, shown, _ = self.run_json(["assurance", "show", "ASI-1"])
        self.assertLessEqual(self.LIST_ROW_FIELDS, set(listed["issues"][0]))
        self.assertLessEqual(self.SHOW_ISSUE_FIELDS, set(shown["issue"]))


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


class AssuranceReadOnlyTests(AssuranceCliTestCase):
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
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
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


class AssuranceProcessPendingTests(AssuranceCliTestCase):
    """The one command here that runs a harness, and what it refuses to run."""

    def seed_pending_run(
        self,
        plan_name: str,
        run_id: str,
        *,
        skip: str | None = state.ASSURANCE_SKIP_PREDATES,
    ) -> Path:
        """Record that one run of ``plan_name`` died still owing an audit."""
        plan_dir = self.seed_run_directory(plan_name, run_id)
        run_dir = (
            state.plan_state_dir(plan_dir, self.state_dir)
            / state.RUNS_DIR_NAME
            / run_id
        )
        (run_dir / state.ASSURANCE_PENDING_FILE_NAME).write_text(
            json.dumps(
                {
                    "schema_version": state.ASSURANCE_PENDING_SCHEMA_VERSION,
                    "run_id": run_id,
                    "recorded_at": TIMESTAMP,
                    "trigger": "test",
                    "skip": skip,
                    "plan_dir": str(plan_dir),
                    "workspace_root": str(plan_dir),
                    "run_config": {"harness": "codex", "audit_harness": "claude"},
                }
            ),
            encoding="utf-8",
        )
        return plan_dir

    def test_process_pending_is_wired_with_documented_defaults(self) -> None:
        args = self.parser.parse_args(["assurance", "process-pending"])

        self.assertEqual(
            "assurance_cli:assurance_process_pending_from_args", args.handler_ref
        )
        self.assertIs(
            cli.resolve_handler(args),
            assurance_cli.assurance_process_pending_from_args,
        )
        self.assertIsNone(args.limit)
        self.assertIsNone(args.run_id)
        self.assertIsNone(args.plan)
        self.assertIsNone(args.audit_harness)
        self.assertIsNone(args.audit_timeout)
        self.assertFalse(args.json)

    def test_an_empty_corpus_has_no_backlog_rather_than_an_error(self) -> None:
        code, out, err = self.run_cli(["assurance", "process-pending"])

        self.assertEqual(0, code, err)
        self.assertIn("No run is recorded as owing an audit.", out)
        self.assertFalse(self.ledger_path.exists())

    def test_history_that_predates_auditing_is_reported_never_audited(self) -> None:
        self.seed_pending_run("alpha", RUN_ID)

        code, payload, err = self.run_json(["assurance", "process-pending"])

        self.assertEqual(0, code, err)
        self.assertEqual(1, payload["candidate_count"])
        self.assertEqual(0, payload["audited_count"])
        self.assertEqual(20, payload["limit"])
        self.assertEqual(
            [
                {
                    "run_id": RUN_ID,
                    "outcome": "skipped",
                    "plan_id": "alpha",
                    "state": None,
                    "reason": state.ASSURANCE_SKIP_PREDATES,
                    "issue_ids": [],
                    "archived_artifacts": [],
                }
            ],
            payload["results"],
        )
        self.assertFalse(self.ledger_path.exists())

    def test_the_text_form_names_the_run_and_what_became_of_it(self) -> None:
        self.seed_pending_run("alpha", RUN_ID)

        code, out, err = self.run_cli(["assurance", "process-pending"])

        self.assertEqual(0, code, err)
        self.assertIn("1 recorded, 0 audited (limit 20)", out)
        self.assertIn(RUN_ID, out)
        self.assertIn(state.ASSURANCE_SKIP_PREDATES, out)

    def test_a_non_positive_limit_is_refused_before_anything_runs(self) -> None:
        self.seed_pending_run("alpha", RUN_ID)

        code, out, err = self.run_cli(["assurance", "process-pending", "--limit", "0"])

        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("--limit must be a positive integer; got 0", err)

    def test_a_non_positive_audit_timeout_is_refused(self) -> None:
        code, payload, _ = self.run_json(
            ["assurance", "process-pending", "--audit-timeout", "0"]
        )

        self.assertEqual(1, code)
        self.assertEqual("input-error", payload["reason"])

    def test_a_run_with_no_recorded_debt_is_refused(self) -> None:
        code, payload, _ = self.run_json(
            ["assurance", "process-pending", "--run-id", RUN_ID]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-run", payload["reason"])
        self.assertIn("has no recorded pending audit", payload["error"])

    def test_one_run_id_under_two_plans_is_refused_rather_than_guessed(self) -> None:
        self.seed_pending_run("alpha", RUN_ID)
        self.seed_pending_run("beta", RUN_ID)

        code, out, err = self.run_cli(
            ["assurance", "process-pending", "--run-id", RUN_ID]
        )

        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("names a pending audit under 2 plans", err)
        self.assertIn("pass --plan PLAN_DIR_OR_ID", err)

    def test_a_plan_narrows_the_pass_to_its_own_runs(self) -> None:
        plan_dir = self.seed_pending_run("alpha", RUN_ID)
        self.seed_pending_run("beta", OTHER_RUN_ID)

        code, payload, err = self.run_json(
            ["assurance", "process-pending", "--plan", str(plan_dir)]
        )

        self.assertEqual(0, code, err)
        self.assertEqual([RUN_ID], [row["run_id"] for row in payload["results"]])


DRAFT_SECTIONS: dict[str, str] = {
    "Summary": "Clamp the retry counter before the relaunch decision is made.",
    "Problem and Evidence": (
        "supervisor-audit.md records a fourth attempt under a budget of three."
    ),
    "Proposed Changes": (
        "Move the budget check above the relaunch branch in runner.py."
    ),
    "Test Plan": "Assert the fourth attempt never starts once the budget is spent.",
    "Assumptions": "The recorded budget is the one the run actually started with.",
}


def _draft_document(sections: dict[str, str] | None = None) -> str:
    body = "\n\n".join(
        f"## {name}\n\n{text}"
        for name, text in (DRAFT_SECTIONS if sections is None else sections).items()
    )
    return f"# Proposal for ASI-1\n\n{body}\n"


class AssurancePlanTests(AssuranceCliTestCase):
    """Drafting a proposal, and everything drafting refuses to do."""

    def setUp(self) -> None:
        super().setUp()
        self.output = Path(self._tmp.name) / "proposal.md"

    # -- fixtures ---------------------------------------------------------

    @contextmanager
    def drafting(
        self,
        *,
        unavailable: str | None = None,
        last_message: str | None = None,
        returncode: int = 0,
        timed_out: bool = False,
        during: object = None,
    ):
        """Run drafting against a harness that answers without being installed."""
        calls: list[dict] = []
        text = _draft_document() if last_message is None else last_message

        def fake_run_harness(name, **kwargs):
            calls.append({"name": name, **kwargs})
            if during is not None:
                during()
            return harnesses.HarnessResult(
                argv=[name],
                returncode=returncode,
                stdout="",
                stderr="",
                last_message=text,
                name=name,
                timed_out=timed_out,
            )

        with (
            mock.patch.object(harnesses, "run_harness", fake_run_harness),
            (
                mock.patch.object(
                    harnesses,
                    "audit_read_only_unavailable_reason",
                    return_value=unavailable,
                )
            ),
        ):
            yield calls

    def plan_argv(self, issue: str = "ASI-1", **flags: str) -> list[str]:
        argv = [
            "assurance",
            "plan",
            issue,
            "--harness",
            "claude",
            "--output",
            str(self.output),
        ]
        for name, value in flags.items():
            argv.extend([f"--{name.replace('_', '-')}", value])
        return argv

    def drafted_plans(self, issue_id: str) -> tuple[models.ImprovementPlanDraft, ...]:
        issue = self.inbox().issue(issue_id)
        assert issue is not None
        return issue.plans

    # -- wiring -----------------------------------------------------------

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

    # -- the drafted proposal ---------------------------------------------

    def test_a_valid_reply_becomes_the_proposal_and_then_a_record(self) -> None:
        self.open_three_issues()

        with self.drafting():
            code, payload, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        document = self.output.read_bytes()
        self.assertEqual(_draft_document(), document.decode("utf-8"))
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

    # -- what drafting refuses --------------------------------------------

    def test_an_unknown_issue_is_refused_before_a_harness_runs(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv("ASI-99"))

        self.assertEqual(1, code)
        self.assertEqual("unknown-issue", payload["reason"])
        self.assertEqual([], calls)
        self.assertFalse(self.output.exists())

    def test_an_unusable_issue_id_is_refused_before_the_ledger_is_read(self) -> None:
        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv("not-an-id"))

        self.assertEqual(1, code)
        self.assertEqual("invalid-issue-id", payload["reason"])
        self.assertEqual([], calls)

    def test_an_existing_output_path_is_never_overwritten(self) -> None:
        self.open_three_issues()
        self.output.write_text("a proposal a human is reading\n", encoding="utf-8")

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("output-exists", payload["reason"])
        self.assertEqual(
            "a proposal a human is reading\n",
            self.output.read_text(encoding="utf-8"),
        )
        self.assertEqual([], calls)
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_an_output_path_with_no_directory_is_refused(self) -> None:
        self.open_three_issues()
        self.output = Path(self._tmp.name) / "nowhere" / "proposal.md"

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("output-unwritable", payload["reason"])
        self.assertEqual([], calls)

    def test_an_unknown_harness_is_refused(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            code, payload, _ = self.run_json(
                [
                    "assurance",
                    "plan",
                    "ASI-1",
                    "--harness",
                    "nope",
                    "--output",
                    str(self.output),
                ]
            )

        self.assertEqual(1, code)
        self.assertEqual("input-error", payload["reason"])
        self.assertEqual([], calls)

    def test_a_harness_that_cannot_run_read_only_is_refused(self) -> None:
        self.open_three_issues()

        with self.drafting(unavailable="harness-not-installed") as calls:
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("harness-unavailable", payload["reason"])
        self.assertIn("harness-not-installed", payload["error"])
        self.assertEqual([], calls)
        self.assertFalse(self.output.exists())

    def test_a_non_positive_timeout_is_refused_before_anything_runs(self) -> None:
        self.open_three_issues()

        with self.drafting() as calls:
            code, payload, _ = self.run_json(self.plan_argv(timeout="0"))

        self.assertEqual(1, code)
        self.assertEqual("input-error", payload["reason"])
        self.assertEqual([], calls)

    def test_a_timed_out_turn_writes_nothing(self) -> None:
        self.open_three_issues()

        with self.drafting(timed_out=True, returncode=124):
            code, payload, _ = self.run_json(self.plan_argv(timeout="45"))

        self.assertEqual(1, code)
        self.assertEqual("timeout", payload["reason"])
        self.assertIn("45 seconds", payload["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_a_failed_turn_writes_nothing(self) -> None:
        self.open_three_issues()

        with self.drafting(returncode=3):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("harness-failed", payload["reason"])
        self.assertFalse(self.output.exists())

    def test_a_reply_missing_a_required_section_is_not_a_proposal(self) -> None:
        self.open_three_issues()
        sections = dict(DRAFT_SECTIONS)
        del sections["Test Plan"]

        with self.drafting(last_message=_draft_document(sections)):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("malformed-output", payload["reason"])
        self.assertIn("Test Plan", payload["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual((), self.drafted_plans("ASI-1"))

    def test_an_empty_reply_is_not_a_proposal(self) -> None:
        self.open_three_issues()

        with self.drafting(last_message="   \n"):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("malformed-output", payload["reason"])
        self.assertFalse(self.output.exists())

    def test_a_section_named_only_inside_a_code_fence_does_not_count(self) -> None:
        self.open_three_issues()
        sections = dict(DRAFT_SECTIONS)
        sections["Proposed Changes"] = (
            "```markdown\n## Test Plan\nnot a real section\n```"
        )
        del sections["Test Plan"]

        with self.drafting(last_message=_draft_document(sections)):
            code, payload, _ = self.run_json(self.plan_argv())

        self.assertEqual(1, code)
        self.assertEqual("malformed-output", payload["reason"])
        self.assertIn("Test Plan", payload["error"])

    def test_a_proposal_wrapped_in_one_fence_is_still_a_proposal(self) -> None:
        self.open_three_issues()
        fenced = f"```markdown\n{_draft_document()}```\n"

        with self.drafting(last_message=fenced):
            code, payload, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        self.assertEqual(_draft_document(), self.output.read_text(encoding="utf-8"))

    # -- the boundary the draft rests on ----------------------------------

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
        self.assertIn(RUN_ID, prompt)
        self.assertIn("asl run", prompt)
        self.assertIn("any change refuses the draft outright", prompt)
        for section in draft.REQUIRED_SECTIONS:
            self.assertIn(f"- ## {section}", prompt)

    def test_the_drafter_reads_the_run_directories_holding_its_evidence(self) -> None:
        self.open_three_issues()
        self.seed_run_directory("alpha", RUN_ID)

        with self.drafting() as calls:
            code, payload, err = self.run_json(self.plan_argv())

        self.assertEqual(0, code, err)
        self.assertEqual([RUN_ID], payload["evidence_run_ids"])
        self.assertIn(RUN_ID, {Path(root).name for root in payload["read_roots"]})
        self.assertEqual(
            [Path(root) for root in payload["read_roots"]],
            list(calls[0]["access"].read_roots),
        )

    def test_a_drafter_that_changed_a_read_only_root_is_disbelieved(self) -> None:
        self.open_three_issues()
        plan_dir = self.seed_run_directory("alpha", RUN_ID)
        run_dir = (
            state.plan_state_dir(plan_dir, self.state_dir)
            / state.RUNS_DIR_NAME
            / RUN_ID
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
                "assurance_models",
                "assurance_projection",
                "harnesses",
                "state",
            },
            siblings,
        )
        source = Path(draft.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "git_ops", "runner", "plans", "authoring"):
            self.assertNotIn(f"import {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
