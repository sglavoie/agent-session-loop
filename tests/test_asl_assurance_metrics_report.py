"""Run report, bounded summary, open-issue and observer projections."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agent_session_loop import assurance_models_vocab, assurance_report
from agent_session_loop import assurance_projection as projection
from tests.asl_assurance_metrics_support import (
    OTHER_INPUT_DIGEST,
    RUN_A,
    RUN_B,
    T1,
    AssuranceReportTestCase,
    _artifacts,
    _assurance,
    _finding,
    _publication,
)

ASL_TEST_PORTABLE = True

REPO_ROOT = Path(__file__).resolve().parents[1]


class RunReportTests(AssuranceReportTestCase):
    def test_report_carries_ids_counts_path_and_open_total(self) -> None:
        self.publish(_publication())
        report = assurance_report.build_run_report(
            _assurance(),
            run_dir=Path("/runs") / RUN_A,
            state_dir=self.state_dir,
        )

        self.assertEqual(report["run_id"], RUN_A)
        self.assertEqual(report["state"], "findings")
        self.assertEqual(report["issue_ids"], ["ASI-1", "ASI-2"])
        self.assertEqual(report["issue_count"], 2)
        self.assertEqual(report["new_issue_count"], 1)
        self.assertEqual(report["recurring_issue_count"], 1)
        self.assertFalse(report["partial"])
        self.assertEqual(
            report["report_path"],
            str(Path("/runs") / RUN_A / assurance_models_vocab.AUDIT_REPORT_FILENAME),
        )
        self.assertEqual(report["open_issue_count"], 1)
        self.assertIsNone(report["open_issue_error"])
        self.assertEqual(report["list_command"], assurance_report.LIST_COMMAND)

    def test_missing_state_yields_no_report_and_no_lines(self) -> None:
        self.assertIsNone(assurance_report.build_run_report(None))
        self.assertIsNone(assurance_report.build_run_report({}))
        self.assertEqual(assurance_report.report_lines(None), [])

    def test_unavailable_findings_are_labelled_partial(self) -> None:
        report = assurance_report.build_run_report(
            _assurance(state="unavailable", reason="timeout"),
            state_dir=self.state_dir,
        )
        self.assertTrue(report["partial"])
        self.assertIn(
            "Assurance issues (partial)", assurance_report.report_lines(report)[0]
        )

    def test_unavailable_without_findings_is_not_partial(self) -> None:
        report = assurance_report.build_run_report(
            _assurance(state="unavailable", reason="timeout", issue_ids=[]),
            state_dir=self.state_dir,
        )
        self.assertFalse(report["partial"])

    def test_lines_print_counts_ids_open_total_and_the_list_hint(self) -> None:
        self.publish(_publication())
        lines = assurance_report.report_lines(
            assurance_report.build_run_report(
                _assurance(), run_dir="/runs/a", state_dir=self.state_dir
            )
        )
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Assurance issues: 1 new, 1 recurring; ASI-1, ASI-2")
        self.assertEqual(lines[1], "Assurance inbox: 1 open; `asl assurance list`")
        self.assertEqual(
            lines[2],
            f"Assurance report: /runs/a/{assurance_models_vocab.AUDIT_REPORT_FILENAME}",
        )

    def test_unread_open_count_says_so_instead_of_guessing(self) -> None:
        report = assurance_report.build_run_report(_assurance(), open_issues=False)
        self.assertIsNone(report["open_issue_count"])
        self.assertIn(
            "open count unavailable", assurance_report.report_lines(report)[1]
        )

    def test_status_line_names_the_harness_or_the_reason(self) -> None:
        self.assertEqual(
            assurance_report.status_line(
                {
                    "state": "findings",
                    "role": {
                        "harness": "codex",
                        "source": "config",
                        "timeout_seconds": 600,
                    },
                }
            ),
            "Supervisor audit: findings (codex via config role, 600s per attempt)",
        )
        self.assertEqual(
            assurance_report.status_line({"state": "skipped", "reason": "opted-out"}),
            "Supervisor audit: skipped (opted-out)",
        )
        self.assertIsNone(assurance_report.status_line(None))
        self.assertIsNone(assurance_report.status_line({}))

    def test_status_line_survives_a_timeout_too_large_for_a_float(self) -> None:
        huge = 10**400
        self.assertEqual(
            f"Supervisor audit: all-clear (codex via config role, {huge}s per attempt)",
            assurance_report.status_line(
                {
                    "state": "all-clear",
                    "role": {
                        "harness": "codex",
                        "source": "config",
                        "timeout_seconds": huge,
                    },
                }
            ),
        )

    def test_an_over_deep_audit_report_degrades_like_a_malformed_one(self) -> None:
        run_dir = Path(self.state_dir) / "run"
        run_dir.mkdir(parents=True)
        report_path = run_dir / assurance_models_vocab.AUDIT_REPORT_FILENAME
        for text in ("{not json", "[" * 200_000):
            with self.subTest(text=text[:10]):
                report_path.write_text(text, encoding="utf-8")
                report = assurance_report.build_run_report(
                    _assurance(), run_dir=run_dir, open_issues=False
                )
                self.assertIsNone(report["lanes_total"])


class BoundedSummaryTests(AssuranceReportTestCase):
    def test_summary_truncates_and_flags_it(self) -> None:
        issue_ids = [f"ASI-{index}" for index in range(1, 21)]
        summary = assurance_report.bounded_summary(_assurance(issue_ids=issue_ids))

        self.assertEqual(
            len(summary["issue_ids"]), assurance_report.MAX_BOUNDED_ISSUE_IDS
        )
        self.assertEqual(summary["issue_count"], 20)
        self.assertTrue(summary["issue_ids_truncated"])
        self.assertEqual(summary["list_command"], assurance_report.LIST_COMMAND)

    def test_short_lists_are_not_flagged_as_truncated(self) -> None:
        summary = assurance_report.bounded_summary(_assurance())
        self.assertEqual(summary["issue_ids"], ["ASI-1", "ASI-2"])
        self.assertFalse(summary["issue_ids_truncated"])
        self.assertEqual(summary["status_line"], "Supervisor audit: findings")

    def test_summary_is_empty_without_a_state(self) -> None:
        self.assertEqual(assurance_report.bounded_summary(None), {})
        self.assertEqual(assurance_report.bounded_summary({"run_id": RUN_A}), {})

    def test_summary_carries_no_control_surface(self) -> None:
        summary = assurance_report.bounded_summary(_assurance())
        for key in summary:
            self.assertNotIn("timeout", key)
            self.assertNotIn("command", key.replace("list_command", ""))
            self.assertNotIn("harness", key)
            self.assertNotIn("retry", key)


class OpenIssueRunsTests(AssuranceReportTestCase):
    def test_open_issues_pin_the_runs_that_found_them(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        self.publish(
            _publication(
                run_id=RUN_B,
                findings=(_finding(invariant_key="scope.no-escape"),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=T1,
            ),
            recorded_at=T1,
        )

        pinned = assurance_report.open_issue_run_ids(self.state_dir)
        self.assertTrue(pinned.available)
        self.assertEqual(pinned.open_issue_count, 2)
        self.assertEqual(pinned.run_ids, frozenset({RUN_A, RUN_B}))

    def test_closing_every_issue_releases_the_pin(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=T1
        )

        pinned = assurance_report.open_issue_run_ids(self.state_dir)
        self.assertTrue(pinned.available)
        self.assertEqual(pinned.open_issue_count, 0)
        self.assertEqual(pinned.run_ids, frozenset())

    def test_unreadable_ledger_pins_nothing_and_says_so(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.mkdir()

        pinned = assurance_report.open_issue_run_ids(self.state_dir)
        self.assertFalse(pinned.available)
        self.assertTrue(pinned.error)
        self.assertEqual(pinned.run_ids, frozenset())


class ObserverStructureTests(unittest.TestCase):
    def test_report_module_imports_no_module_that_can_launch_an_audit(self) -> None:
        """``asl status`` renders assurance without being able to produce it.

        The guarantee is structural: this module's only siblings are the
        ledger, its models (the ``assurance_models_*`` modules), and its projection,
        so nothing reachable from a status or metrics command can start a harness.
        """
        source = (REPO_ROOT / "agent_session_loop" / "assurance_report.py").read_text()
        siblings = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.level == 1
            for alias in node.names
        }
        self.assertEqual(
            siblings,
            {
                "assurance_models_primitives",
                "assurance_models_vocab",
                "assurance_projection",
                "collect_assurance_metrics",
            },
        )


if __name__ == "__main__":
    unittest.main()
