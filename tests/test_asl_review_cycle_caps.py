"""Review cycle cap and reopen tests."""

import unittest

from agent_session_loop import (
    events,
    plans_snapshot,
    review,
    review_continuation,
    review_session_body,
    review_transitions,
    workbench_recovery,
)
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _discovered,
    read_jsonl,
)


class ReviewCycleCapTests(RepairCycleFixtures, unittest.TestCase):
    """Stop review cycles at caps and reopen completed reviews."""

    def test_eventless_repeated_rereview_stops_before_rematerialization(
        self,
    ) -> None:
        self._write_plan()

        # Stop after the first findings review materializes its child. Then
        # model the crash window after the child completes and the rereview
        # completes, but before _advance_review_cycle appends that rereview's
        # findings-repeated cycle record.
        first, first_payload = self._run_cycles(
            review_outcomes="findings",
            extra_args=["--max-sessions", "1"],
        )
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("max-sessions", first_payload["reason"])
        first_cycle = first_payload["review_cycles"][0]
        fingerprint = first_cycle["fingerprints"][0]
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            first_cycle["disposition"],
        )

        repair_path = self.plan_dir / "session-2.1.md"
        repair_path.write_text(
            repair_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        rereview, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        self.assertTrue(created)
        self.assertEqual("3", rereview.id)
        rereview.path.write_text(
            rereview.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            self._events_path(),
            max_review_fix_cycles=2,
        )
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
            projected.blocker,
        )
        self.assertEqual((fingerprint,), projected.current_fingerprints)

        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,findings-drift",
            extra_args=["--max-review-fix-cycles", "2"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual([], resumed_payload["turns"])
        self.assertEqual("terminal", resumed_payload["review_resume"]["action"])
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
            resumed_payload["review_resume"]["blocker"],
        )
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())
        self.assertEqual(1, len(self._review_cycle_events()))

    def test_zero_cycles_reviews_but_creates_no_repairs(self) -> None:
        self._write_plan()

        completed, payload = self._run_cycles(
            review_outcomes="findings",
            extra_args=["--max-review-fix-cycles", "0"],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        self.assertEqual(0, payload["max_review_fix_cycles"])
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
            payload["review_fix_disposition"],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(1, len(cycles))
        self.assertEqual([], cycles[0]["repair_session_ids"])
        self.assertEqual(["1", "2"], payload["completed_session_ids"])
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        # The findings verdict is still recorded for the operator.
        self.assertEqual("findings", payload["review_verdict"]["outcome"])

    def test_cycle_cap_stops_after_configured_cycles(self) -> None:
        self._write_plan()

        # Cycle 1 repairs; the rereview finds a *different* defect, but the
        # cap of 1 materialization is already spent.
        completed, payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            payload["review_fix_disposition"],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(2, len(cycles))
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            cycles[0]["disposition"],
        )
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            cycles[1]["disposition"],
        )
        # The new finding is introduced, not repeated.
        self.assertEqual([], cycles[1]["repeated_findings"])
        self.assertEqual(cycles[1]["fingerprints"], cycles[1]["introduced_findings"])
        self.assertTrue((self.plan_dir / "session-2.1.md").is_file())
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

    def test_deprecated_alias_flag_maps_to_cycles(self) -> None:
        self._write_plan()

        completed, payload = self._run_cycles(
            review_outcomes="findings",
            extra_args=["--max-review-fix-plans", "0"],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(0, payload["max_review_fix_cycles"])
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
            payload["review_fix_disposition"],
        )
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

    def test_completed_review_reopens_only_when_absolute_cap_increases(
        self,
    ) -> None:
        self._write_plan()
        scripted = "findings,findings-new,all-clear"

        first, first_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "0"],
        )

        self.assertEqual(1, first.returncode, first.stderr)
        self.assertEqual("cycles-disabled", first_payload["review_fix_disposition"])
        self.assertEqual(
            "higher-cap-required", first_payload["review_resume"]["blocker"]
        )
        first_cycle = self._review_cycle_events()[0]
        self.assertIsNotNone(first_cycle["review_output_sha256"])
        self.assertIsNotNone(first_cycle["review_artifact_sha256"])
        artifact_before_noop = (
            self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        ).read_bytes()

        same, same_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "0"],
        )

        self.assertEqual(1, same.returncode, same.stderr)
        self.assertEqual([], same_payload["turns"])
        self.assertEqual(
            "higher-cap-required", same_payload["review_resume"]["blocker"]
        )
        self.assertEqual(
            artifact_before_noop,
            (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_bytes(),
        )
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertEqual([], self._review_reopen_events())

        second, second_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, second.returncode, second.stderr)
        self.assertEqual(
            ["2.1", "3"],
            [turn["session_id"] for turn in second_payload["turns"]],
        )
        self.assertEqual(
            "cycle-cap-exhausted", second_payload["review_fix_disposition"]
        )
        self.assertEqual(
            "higher-cap-required", second_payload["review_resume"]["blocker"]
        )
        first_reopen = self._review_reopen_events()[0]
        self.assertTrue(first_reopen["materialized"])
        self.assertEqual("2", first_reopen["source_review_session_id"])
        self.assertEqual(["2.1"], first_reopen["repair_session_ids"])
        self.assertEqual(0, first_reopen["historical_materializations_before"])
        self.assertEqual(1, first_reopen["historical_materializations_after"])
        self.assertEqual(0, first_reopen["before_max_review_fix_cycles"])
        self.assertEqual(1, first_reopen["after_max_review_fix_cycles"])
        self.assertIsNotNone(first_reopen["current_review_output_sha256"])
        self.assertIsNotNone(first_reopen["current_review_artifact_sha256"])

        artifact_before_second_noop = (
            self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        ).read_bytes()
        lower, lower_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "1"],
        )
        self.assertEqual(1, lower.returncode, lower.stderr)
        self.assertEqual([], lower_payload["turns"])
        self.assertEqual(
            artifact_before_second_noop,
            (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_bytes(),
        )
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

        third, third_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "2"],
        )

        self.assertEqual(0, third.returncode, third.stderr)
        self.assertEqual(
            ["3.1", "4"],
            [turn["session_id"] for turn in third_payload["turns"]],
        )
        self.assertEqual("all-clear", third_payload["review_fix_disposition"])
        self.assertEqual("terminal", third_payload["review_resume"]["action"])
        reopen_events = self._review_reopen_events()
        self.assertEqual(2, len(reopen_events))
        self.assertEqual("3", reopen_events[1]["source_review_session_id"])
        self.assertEqual(["3.1"], reopen_events[1]["repair_session_ids"])
        self.assertEqual(1, reopen_events[1]["historical_materializations_before"])
        self.assertEqual(2, reopen_events[1]["historical_materializations_after"])
        self.assertEqual(1, reopen_events[1]["before_max_review_fix_cycles"])
        self.assertEqual(2, reopen_events[1]["after_max_review_fix_cycles"])

    def test_repair_sessions_missing_blocker_is_not_reported_all_complete(
        self,
    ) -> None:
        self._write_plan()
        first, first_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=[
                "--max-review-fix-cycles",
                "1",
                "--max-sessions",
                "1",
            ],
        )
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("max-sessions", first_payload["reason"])
        child_path = self.plan_dir / "session-2.1.md"
        self.assertTrue(child_path.is_file())

        child_path.unlink()
        resumed, payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertNotEqual("all-complete", payload["reason"])
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            payload["pause_reason"],
        )
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("repair-sessions-missing", payload["review_resume"]["blocker"])
        self.assertIsNone(payload["review_fix_disposition"])

        finished_events = [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == "run_finished"
            and record["payload"]["run_id"] == payload["run_id"]
        ]
        self.assertEqual(1, len(finished_events))
        self.assertIsNone(finished_events[0]["review_fix_disposition"])

        recovery = workbench_recovery.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("review-continuation", recovery.kind)
        self.assertEqual("Review continuation is inconsistent", recovery.title)
        self.assertEqual("error", recovery.severity)
        self.assertEqual(payload["pause_message"], recovery.summary)
        self.assertEqual(payload["review_resume"], recovery.review_resume)

    def test_partial_repair_child_deletion_pauses_as_missing_lineage(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1", "2.2"),
        )
        missing_child = self.plan_dir / "session-2.2.md"
        self.assertTrue(missing_child.is_file())
        missing_child.unlink()
        event_path = self._events_path()
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "repairs-materialized",
                    "repair_session_ids": ["2.1", "2.2"],
                    "max_review_fix_cycles": 1,
                },
            ),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )

        self.assertEqual("2", projected.review_session_id)
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("repair-sessions-missing", projected.blocker)
        self.assertEqual(("2.1", "2.2"), projected.repair_session_ids)
        self.assertEqual(("2.1",), projected.completed_repair_session_ids)

        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=[
                "--allow-not-loop-ready",
                "--max-review-fix-cycles",
                "1",
            ],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            payload["pause_reason"],
        )
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual(
            "repair-sessions-missing",
            payload["review_resume"]["blocker"],
        )
        self.assertIsNone(payload["review_fix_disposition"])

        recovery = workbench_recovery.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("review-continuation", recovery.kind)
        self.assertEqual("Review continuation is inconsistent", recovery.title)
        self.assertEqual("error", recovery.severity)
        self.assertEqual(payload["pause_message"], recovery.summary)
        self.assertEqual(payload["review_resume"], recovery.review_resume)


if __name__ == "__main__":
    unittest.main()
