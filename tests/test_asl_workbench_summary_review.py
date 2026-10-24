"""Test asl workbench summary review."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    plans_files,
    plans_models,
    plans_progress,
    plans_session_ids,
    review,
    review_repair,
    workbench_recovery_build,
    workbench_summary,
    workbench_summary_model,
)
from tests import support_asl_workbench_summary as fixtures
from tests.support import (
    _discovered,
    _structured_review_finding,
    _write_state_payload,
    write_completed_review_plan,
    write_plan,
    write_review_cycle_event,
)

ASL_TEST_PORTABLE = True


class WorkbenchSummaryTests(fixtures.WorkbenchSummaryCase):
    def test_corrected_malformed_review_becomes_rerunnable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings = [
                _structured_review_finding(
                    finding_id=f"F{index}", repair_group=f"g{index}"
                )
                for index in range(9)
            ]
            plan_dir = write_completed_review_plan(
                tmp_path / "review-malformed", findings
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "malformed-review-repair",
                    "fingerprints": [],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 1,
                },
            )

            blocked = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            self.assertEqual("complete", blocked.primary_state)
            self.assertEqual("artifact-invalid", blocked.review_resume["blocker"])
            self.assertIn("Correct", blocked.next_action_hint)

            findings[-1]["repair_group"] = "g7"
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
                encoding="utf-8",
            )
            ready = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            self.assertEqual("recent", ready.primary_state)
            self.assertEqual("run", ready.recommended_action)
            self.assertTrue(ready.review_resume["eligible"])
            self.assertIn("Resume review repairs", ready.next_action_hint)

    def test_malformed_review_pause_without_turn_uses_recovery_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            findings = [
                _structured_review_finding(
                    finding_id=f"F{index}", repair_group=f"g{index}"
                )
                for index in range(9)
            ]
            plan_dir = write_completed_review_plan(
                tmp_path / "review-malformed-paused", findings
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "malformed-review-repair",
                    "fingerprints": [],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 1,
                },
            )
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                    "pause_reason": (events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR),
                    "pause_message": "Review artifact validation failed",
                    "run_id": "run-malformed-review",
                    "run_started_at": "2026-06-19T14:00:00+00:00",
                    "run_finished_at": "2026-06-19T14:01:00+00:00",
                    "active_turn": None,
                    "turns": [],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )
            recovery = workbench_recovery_build.build_workbench_recovery(
                discovered, state_dir=state_root
            )

            self.assertEqual("failed", summary.primary_state)
            self.assertEqual("inspect-failure", summary.recommended_action)
            self.assertEqual("review-continuation", recovery.kind)
            self.assertEqual("artifact-invalid", summary.review_resume["blocker"])
            self.assertEqual(recovery.title, summary.next_action_hint)
            self.assertEqual(recovery.summary, summary.detail)
            self.assertEqual(recovery.review_resume, summary.review_resume)
            self.assertEqual(summary.review_resume, summary.recovery["review_resume"])
            self.assertEqual(
                events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                summary.pause_reason,
            )
            self.assertEqual((), summary.warnings)

            findings[-1]["repair_group"] = "g7"
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
                encoding="utf-8",
            )

            resumed = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )
            ready_recovery = workbench_recovery_build.build_workbench_recovery(
                discovered, state_dir=state_root
            )

            self.assertEqual("recent", resumed.primary_state)
            self.assertEqual("run", resumed.recommended_action)
            self.assertEqual(
                workbench_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL,
                resumed.activity_label,
            )
            self.assertNotIn("sessions remain", resumed.activity_label)
            self.assertEqual("Review continuation ready", resumed.next_action_hint)
            self.assertTrue(resumed.review_resume["eligible"])
            self.assertEqual("materialize-repairs", resumed.review_resume["action"])
            self.assertEqual("review-continuation", ready_recovery.kind)
            self.assertEqual("info", ready_recovery.severity)
            self.assertEqual("review-profile", ready_recovery.actions[0].id)
            self.assertEqual("review-profile", resumed.recovery["primary_action"]["id"])
            self.assertEqual(ready_recovery.title, resumed.next_action_hint)
            self.assertEqual(ready_recovery.summary, resumed.detail)
            self.assertEqual(ready_recovery.review_resume, resumed.review_resume)
            self.assertEqual(resumed.review_resume, resumed.recovery["review_resume"])

    def test_completed_review_repairs_use_zero_pending_continuation_badge(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "rereview-badge",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            materialized = review_repair.materialize_review_repair_sessions(
                plan_dir, state_dir=state_root
            )
            self.assertTrue(materialized.ok, materialized.error)
            child = materialized.children[0].path
            child.write_text(
                child.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete"
                ),
                encoding="utf-8",
            )

            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("recent", summary.primary_state)
            self.assertEqual("rereview", summary.review_resume["action"])
            self.assertEqual(0, summary.counts["pending"])
            self.assertEqual(
                workbench_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL,
                summary.activity_label,
            )
            self.assertNotIn("sessions remain", summary.activity_label)

    def test_completed_at_from_progress_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-progress"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            plans_progress.append_progress_entry(
                canonical,
                plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id("2"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                ),
            )
            discovered = _discovered(canonical)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("2026-06-19T14:22:00Z", summary.completed_at)
            self.assertEqual("progress-journal", summary.last_activity_source)
            self.assertEqual(
                "2026-06-19T14:22:00Z", summary.to_payload()["completed_at"]
            )


if __name__ == "__main__":
    unittest.main()
