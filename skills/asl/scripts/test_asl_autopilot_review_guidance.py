#!/usr/bin/env python3
"""Tests for review retagging, continuation guidance, and human summaries."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from asl_autopilot_test_bases import RunAutopilotReviewBase
from asl_autopilot_test_payloads import _review_resume
from asl_autopilot_test_support import (  # noqa: F401
    autopilot,
    setUpModule,
    tearDownModule,
)


class RunAutopilotReviewGuidanceTest(RunAutopilotReviewBase):
    def test_findings_retag_to_review_findings(self) -> None:
        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (1 high, 0 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 1, "med": 0, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }
        result = autopilot.run_autopilot(
            self._args(),
            supervisor=self._complete_supervisor,
            review_reader=lambda parsed: verdict,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "review_findings")
        self.assertEqual(result["review"]["total_findings"], 1)
        # The runner owns repair cycles now; the helper attaches no follow-up
        # fix-plan guidance.
        self.assertNotIn("review_fix", result)

    def test_resume_only_review_findings_retag_via_persisted_harness(self) -> None:
        # A resume-only command carries no --review-harness of its own; the
        # effective harness lives in the persisted run_config. The completed run
        # must still read the findings verdict and re-tag review_findings rather
        # than returning a bare complete (F1). The supervisor result carries the
        # `state_path` `_effective_review_config` reads to resolve that harness.
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-resume-review-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": {"review_harness": "codex"}}, handle)

        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (1 high, 0 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 1, "med": 0, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }

        def complete_resume(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
                "status": {"ok": True, "state_path": state_path},
            }

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --resume"),
            supervisor=complete_resume,
            review_reader=lambda parsed: verdict,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "review_findings")
        self.assertEqual(result["review"]["total_findings"], 1)

    def test_resume_without_persisted_harness_stays_complete(self) -> None:
        # No effective review harness (resume run_config has none, no explicit
        # flag): the completed run is left complete and the review verdict is not
        # even read, preserving the historical non-review complete behavior.
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-resume-noreview-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": {"harness": "codex"}}, handle)

        reads: list = []

        def reader(parsed):
            reads.append(parsed)
            return {"ok": True, "outcome": "findings", "total_findings": 1}

        def complete_resume(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
                "status": {"ok": True, "state_path": state_path},
            }

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --resume"),
            supervisor=complete_resume,
            review_reader=reader,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(reads, [])

    def test_findings_carry_review_resume_cap_guidance(self) -> None:
        review_resume = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )

        def complete_with_guidance(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 0,
                "status": {"review_resume": review_resume},
            }

        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (0 high, 1 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 0, "med": 1, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }
        result = autopilot.run_autopilot(
            self._args(
                command="asl run /tmp/plan --review-harness codex "
                "--max-review-fix-cycles 2"
            ),
            supervisor=complete_with_guidance,
            review_reader=lambda parsed: verdict,
        )
        self.assertEqual(result["reason"], "review_findings")
        self.assertEqual(result["review_resume"], review_resume)
        summary = autopilot._human_summary(result)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)

    def test_higher_cap_guidance_adds_missing_review_harness(self) -> None:
        review_resume = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )

        def complete_with_guidance(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 0,
                "review_resume": review_resume,
            }

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --max-review-fix-cycles 2"),
            supervisor=complete_with_guidance,
        )
        summary = autopilot._human_summary(result)
        self.assertIn("with a review harness", summary)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)

    def test_resume_only_higher_cap_guidance_uses_persisted_harness(self) -> None:
        # A resume-only command carries no explicit --review-harness; the
        # effective harness lives in the persisted run_config. Human
        # continuation guidance must derive review-harness activity from that
        # same effective config (F8), so it does NOT ask the operator to add a
        # review harness the runner already rehydrates — while still surfacing
        # the higher-cap prerequisite.
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-resume-guidance-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": {"review_harness": "codex"}}, handle)

        review_resume = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )

        def complete_resume(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
                "status": {
                    "ok": True,
                    "state_path": state_path,
                    "review_resume": review_resume,
                },
            }

        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (0 high, 1 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 0, "med": 1, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }
        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --resume"),
            supervisor=complete_resume,
            review_reader=lambda parsed: verdict,
        )
        self.assertEqual(result["reason"], "review_findings")
        summary = autopilot._human_summary(result)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)
        self.assertNotIn("with a review harness", summary)

    def test_eligible_guidance_honors_existing_review_harness(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "command": {"review_harness": "codex"},
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=True,
                minimum_required_cap=3,
                source_disposition="cycle-cap-exhausted",
            ),
        }

        summary = autopilot._human_summary(result)

        self.assertIn("rerun the same review-enabled command", summary)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)
        self.assertNotIn("with a review harness", summary)

    def test_enabled_harness_rereview_guidance_is_cap_free(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "command": {"review_harness": "codex"},
            "review_resume": _review_resume(
                "rereview",
                eligible=True,
                minimum_required_cap=9,
                source_disposition="repairs-materialized",
            ),
        }

        summary = autopilot._human_summary(result)

        self.assertIn("rerun the same review-enabled command", summary)
        self.assertNotIn("--max-review-fix-cycles", summary)

    def test_paused_repair_summaries_omit_continuation_guidance(self) -> None:
        for reason in ("paused", "needs_agent_reply"):
            with self.subTest(reason=reason):
                result = {
                    "ok": False,
                    "reason": reason,
                    "error": "the repair session needs an answer",
                    "review_resume": _review_resume(
                        "run-repairs",
                        eligible=True,
                        source_disposition="repairs-materialized",
                    ),
                }

                summary = autopilot._human_summary(result)

                self.assertIn("the repair session needs an answer", summary)
                self.assertNotIn("resume the same plan", summary)
                self.assertNotIn("review harness", summary)

    def test_foreign_lock_summary_omits_review_continuation_guidance(self) -> None:
        result = {
            "ok": False,
            "reason": "user_required",
            "error": "active lock belongs to another plan",
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="higher-cap-required",
                minimum_required_cap=3,
                source_disposition="cycle-cap-exhausted",
            ),
        }

        summary = autopilot._human_summary(result)

        self.assertIn("active lock belongs to another plan", summary)
        self.assertNotIn("resume the same plan", summary)
        self.assertNotIn("--max-review-fix-cycles", summary)

    def test_human_summary_surfaces_artifact_remediation(self) -> None:
        result = {
            "ok": False,
            "reason": "review_findings",
            "review": {"status_line": "Status: 1 Findings (1 high, 0 med, 0 low)"},
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="artifact-invalid",
                source_disposition="malformed-review-repair",
                artifact_error="repair_group is missing",
            ),
        }
        summary = autopilot._human_summary(result)
        self.assertIn("correct the review artifact", summary)
        self.assertIn("repair_group is missing", summary)

    def test_complete_summary_also_surfaces_artifact_remediation(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "command": {"review_harness": None},
            "review_error": {"reason": "review-malformed"},
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="artifact-invalid",
                source_disposition="malformed-review-repair",
                artifact_error="done_when is empty",
            ),
        }
        summary = autopilot._human_summary(result)
        self.assertIn("ok: complete", summary)
        self.assertIn("correct the review artifact: done_when is empty", summary)
        self.assertIn("resume the same plan with a review harness", summary)

    def test_terminal_blocker_guidance_remains_visible(self) -> None:
        result = {
            "ok": False,
            "reason": "blocked",
            "review_resume": _review_resume(
                "terminal",
                eligible=False,
                blocker="repair-sessions-missing",
                source_disposition="repairs-materialized",
            ),
        }
        self.assertIn(
            "no automatic continuation (repair-sessions-missing)",
            autopilot._human_summary(result),
        )

    def test_all_clear_stays_complete_with_review_attached(self) -> None:
        verdict = {
            "ok": True,
            "outcome": "all-clear",
            "status_line": autopilot.REVIEW_STATUS_ALL_CLEAR,
            "total_findings": 0,
            "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "clean",
        }
        result = autopilot.run_autopilot(
            self._args(),
            supervisor=self._complete_supervisor,
            review_reader=lambda parsed: verdict,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["review"]["outcome"], "all-clear")

    def test_missing_review_records_error_but_stays_complete(self) -> None:
        error = {"ok": False, "reason": "review-missing", "error": "gone"}
        result = autopilot.run_autopilot(
            self._args(),
            supervisor=self._complete_supervisor,
            review_reader=lambda parsed: error,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["review_error"]["reason"], "review-missing")

    def test_no_review_harness_skips_review_read(self) -> None:
        def fail_reader(parsed):  # pragma: no cover - must not run
            raise AssertionError("review reader must not run without --review-harness")

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan"),
            supervisor=self._complete_supervisor,
            review_reader=fail_reader,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertNotIn("review", result)

    def test_human_summary_surfaces_review_status_line(self) -> None:
        result = {
            "ok": False,
            "reason": "review_findings",
            "review": {"status_line": "Status: 2 Findings (0 high, 2 med, 0 low)"},
        }
        summary = autopilot._human_summary(result)
        self.assertIn("review_findings", summary)
        self.assertIn("2 Findings", summary)

    def test_human_summary_reports_max_sessions_progress(self) -> None:
        result = {
            "ok": True,
            "reason": "max-sessions",
            "completed": 1,
            "pending": 30,
        }
        summary = autopilot._human_summary(result)
        self.assertIn("max-sessions", summary)
        self.assertIn("completed 1", summary)
        self.assertIn("30 still pending", summary)


if __name__ == "__main__":
    unittest.main()
