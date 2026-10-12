#!/usr/bin/env python3
"""Tests for review verdict parsing, reading, and audit results."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from asl_autopilot_test_bases import RunAutopilotReviewBase
from asl_autopilot_test_support import (  # noqa: F401
    autopilot,
    setUpModule,
    tearDownModule,
)


class ParseReviewVerdictTest(unittest.TestCase):
    """Classify `final-review.md` status lines (mirrors the ASL runner)."""

    def test_all_clear_is_parsed(self) -> None:
        text = "# Review\n\nNo issues found.\n\nStatus: All clear, no findings\n"
        verdict = autopilot.parse_review_verdict(text)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["outcome"], "all-clear")
        self.assertEqual(verdict["total_findings"], 0)
        self.assertEqual(
            verdict["findings_by_severity"], {"high": 0, "med": 0, "low": 0}
        )

    def test_findings_status_is_parsed(self) -> None:
        text = (
            "# Review\n\nFound some things.\n\n"
            "Status: 3 Findings (1 high, 1 med, 1 low)\n"
        )
        verdict = autopilot.parse_review_verdict(text)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["outcome"], "findings")
        self.assertEqual(verdict["total_findings"], 3)
        self.assertEqual(
            verdict["findings_by_severity"], {"high": 1, "med": 1, "low": 1}
        )
        self.assertEqual(
            verdict["status_line"], "Status: 3 Findings (1 high, 1 med, 1 low)"
        )

    def test_empty_review_is_malformed(self) -> None:
        verdict = autopilot.parse_review_verdict("\n\n   \n")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_missing_status_line_is_malformed(self) -> None:
        verdict = autopilot.parse_review_verdict("# Review\n\nAll good here.\n")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_multiple_status_lines_is_malformed(self) -> None:
        text = (
            "Status: All clear, no findings\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n"
        )
        verdict = autopilot.parse_review_verdict(text)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_status_not_final_line_is_malformed(self) -> None:
        text = "Status: All clear, no findings\n\nTrailing prose here.\n"
        verdict = autopilot.parse_review_verdict(text)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_malformed_findings_grammar_is_rejected(self) -> None:
        verdict = autopilot.parse_review_verdict("Status: 3 issues found\n")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_findings_counts_must_sum_to_total(self) -> None:
        verdict = autopilot.parse_review_verdict(
            "Status: 3 Findings (1 high, 1 med, 0 low)\n"
        )
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_zero_total_findings_is_rejected(self) -> None:
        verdict = autopilot.parse_review_verdict(
            "Status: 0 Findings (0 high, 0 med, 0 low)\n"
        )
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")


class ReadReviewVerdictTest(unittest.TestCase):
    """`_read_review_verdict` reads `final-review.md` from the plan dir."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-review-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.parsed = {"plan_dir": self.tmp}

    def _write_review(self, text: str) -> None:
        path = os.path.join(self.tmp, autopilot.FINAL_REVIEW_FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_findings_verdict_carries_path_and_summary(self) -> None:
        body = "# Review\n\nDetails.\n\nStatus: 2 Findings (2 high, 0 med, 0 low)\n"
        self._write_review(body)
        verdict = autopilot._read_review_verdict(self.parsed)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["outcome"], "findings")
        self.assertEqual(
            verdict["review_output_path"],
            os.path.join(self.tmp, autopilot.FINAL_REVIEW_FILENAME),
        )
        self.assertIn("Details.", verdict["summary"])
        # A short review is not truncated, but still names the full-review
        # escape hatch so callers can always reach the untruncated verdict.
        self.assertFalse(verdict["summary_truncated"])
        self.assertIn("asl review-verdict", verdict["full_review"])
        self.assertIn(self.tmp, verdict["full_review"])

    def test_bounded_review_summary_returns_text_and_flag(self) -> None:
        short, truncated = autopilot._bounded_review_summary("body\n")
        self.assertEqual("body", short)
        self.assertFalse(truncated)

        big = "x" * (autopilot.REVIEW_SUMMARY_MAX_CHARS + 500)
        clipped, truncated = autopilot._bounded_review_summary(big)
        self.assertTrue(truncated)
        self.assertTrue(clipped.endswith("… (truncated)"))

    def test_summary_is_bounded(self) -> None:
        big = "x" * (autopilot.REVIEW_SUMMARY_MAX_CHARS + 500)
        self._write_review(f"{big}\n\nStatus: All clear, no findings\n")
        verdict = autopilot._read_review_verdict(self.parsed)
        self.assertTrue(verdict["ok"], verdict)
        self.assertLessEqual(
            len(verdict["summary"]),
            autopilot.REVIEW_SUMMARY_MAX_CHARS + len("\n… (truncated)"),
        )
        self.assertTrue(verdict["summary"].endswith("… (truncated)"))
        # Truncation is now flagged explicitly instead of silently dropping
        # the tail, and the full-review escape hatch is named.
        self.assertTrue(verdict["summary_truncated"])
        self.assertIn("asl review-verdict", verdict["full_review"])

    def test_missing_file_reports_review_missing(self) -> None:
        verdict = autopilot._read_review_verdict(self.parsed)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "review-missing")


class RunAutopilotReviewAuditTest(RunAutopilotReviewBase):
    def test_run_result_carries_the_bounded_audit_verdict(self) -> None:
        result = autopilot._run_result(
            True,
            "complete",
            run={
                "assurance": {
                    "run_id": "20260619T120000Z-9",
                    "state": "findings",
                    "eligible": True,
                    "issue_ids": ["ASI-1", "ASI-2"],
                    "new_issue_count": 2,
                }
            },
        )

        self.assertEqual("findings", result["assurance"]["state"])
        self.assertEqual(["ASI-1", "ASI-2"], result["assurance"]["issue_ids"])
        self.assertEqual("asl assurance list", result["assurance"]["list_command"])
        # Display only: nothing here can configure, launch, or retry an audit.
        self.assertNotIn("role", result["assurance"])
        self.assertNotIn("attempts", result["assurance"])

    def test_run_result_falls_back_to_status_runner_state(self) -> None:
        result = autopilot._run_result(
            True,
            "complete",
            status={
                "runner_state": {
                    "assurance": {
                        "run_id": "20260619T120000Z-9",
                        "state": "all-clear",
                        "eligible": True,
                    }
                }
            },
        )

        self.assertEqual("all-clear", result["assurance"]["state"])
        self.assertEqual([], result["assurance"]["issue_ids"])

    def test_run_result_omits_assurance_when_no_audit_ran(self) -> None:
        result = autopilot._run_result(True, "complete", run={"ok": True})

        self.assertNotIn("assurance", result)

    def test_human_summary_names_the_issues_the_audit_found(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "assurance": {
                "state": "findings",
                "issue_ids": ["ASI-1", "ASI-2"],
                "issue_count": 2,
                "issue_ids_truncated": False,
                "partial": False,
                "list_command": "asl assurance list",
            },
        }

        summary = autopilot._human_summary(result)

        self.assertIn("supervisor audit found 2 issue(s)", summary)
        self.assertIn("ASI-1, ASI-2", summary)
        self.assertIn("asl assurance list", summary)

    def test_human_summary_flags_a_partial_audit_verdict(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "assurance": {
                "state": "unavailable",
                "issue_ids": ["ASI-1"],
                "issue_count": 9,
                "issue_ids_truncated": True,
                "partial": True,
                "list_command": "asl assurance list",
            },
        }

        summary = autopilot._human_summary(result)

        self.assertIn("partial: the audit could not complete", summary)
        self.assertIn("ASI-1, …", summary)

    def test_human_summary_stays_unchanged_without_findings(self) -> None:
        result = {"ok": True, "reason": "complete", "completed": 3, "pending": 0}

        self.assertNotIn("supervisor audit", autopilot._human_summary(result))

    def test_human_summary_reports_stopped_progress(self) -> None:
        result = {
            "ok": True,
            "reason": "stopped",
            "completed": 3,
            "pending": 4,
        }
        summary = autopilot._human_summary(result)
        self.assertTrue(summary.startswith("ok: stopped"), summary)
        self.assertIn("completed 3", summary)
        self.assertIn("4 still pending", summary)
        # Never rendered as a pause, a failure, or `blocked`.
        self.assertNotIn("stopped:", summary)
        self.assertNotIn("blocked", summary)
        self.assertNotIn("paused", summary)


if __name__ == "__main__":
    unittest.main()
