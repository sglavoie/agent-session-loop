"""Asl turn settlement completion regressions."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    commit_settlement,
    review_cycle,
    run_selection,
    session_verification,
    turn_settlement,
)

from .support_asl_turn_settlement import SESSION_ID, SettlementTestCase

ASL_TEST_PORTABLE = True


class ReviewVerdictTests(SettlementTestCase):
    """The verdict gate, and the stop request that must be seen before it."""

    def test_the_control_queue_is_drained_before_the_verdict_is_taken(self) -> None:
        self.settle(is_review_session=True)
        self.assertLess(self.trace.index("drain"), self.trace.index("verdict"))

    def test_the_verdict_runs_after_verification(self) -> None:
        self.settle(is_review_session=True)
        self.assertLess(self.trace.index("verification"), self.trace.index("verdict"))

    def test_a_retry_verdict_hands_back_the_verdict_s_own_snapshot(self) -> None:
        self.patch(
            session_verification,
            "_enforce_review_verdict",
            self.recorder("verdict", ("retry", self.before)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_RETRY, settlement.action)
        self.assertIs(self.before, settlement.snapshot)
        self.assertIsNone(settlement.turn_record)
        self.assertNotIn("progress", self.trace)

    def test_a_pausing_verdict_stops_the_pass(self) -> None:
        self.patch(
            session_verification,
            "_enforce_review_verdict",
            self.recorder("verdict", ("pause", self.after)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual([], self.turn_executor.calls)

    def test_a_non_review_turn_never_reaches_the_verdict(self) -> None:
        self.settle(is_review_session=False)
        self.assertNotIn("verdict", self.trace)
        self.assertNotIn("drain", self.trace)


class SessionCommitTests(SettlementTestCase):
    """The per-session commit, and the failure the run survives."""

    def test_no_commit_is_attempted_outside_the_per_session_mode(self) -> None:
        self.settle()
        self.assertNotIn("commit-group", self.trace)

    def test_the_per_session_mode_commits_before_the_turn_is_published(self) -> None:
        self.settle(commit_mode=run_selection.COMMIT_MODE_PER_SESSION)
        self.assertLess(
            self.trace.index("commit-group"), self.trace.index("turn-commit")
        )

    def test_a_failed_commit_is_soft_failed_and_the_turn_still_commits(self) -> None:
        self.patch(
            commit_settlement,
            "_run_commit_group",
            self.recorder("commit-group", ({"id": "g1"}, {"target": "."})),
        )
        settlement = self.settle(commit_mode=run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual(1, len(self.soft_failures))
        self.assertEqual("session", self.soft_failures[0]["scope"])
        self.assertEqual("commit-failed", self.turn_payload["session_commit"]["reason"])
        self.assertEqual(1, len(self.turn_executor.calls))

    def test_a_repair_child_owns_its_own_commit_and_records_the_mode(self) -> None:
        self.patch(
            commit_settlement,
            "_dirty_commit_targets",
            self.recorder("targets", [{"target": "."}]),
        )
        self.settle(is_repair_session=True, commit_mode=None)
        self.assertEqual(
            run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            self.turn_payload["repair_commit"]["mode"],
        )
        self.assertEqual("committed", self.turn_payload["repair_commit"]["result"])

    def test_a_repair_child_with_nothing_dirty_records_no_changes(self) -> None:
        self.settle(is_repair_session=True, commit_mode=None)
        self.assertEqual("no-changes", self.turn_payload["repair_commit"]["result"])
        self.assertNotIn("commit-group", self.trace)


class PublicationTests(SettlementTestCase):
    """What a settled turn leaves on the run."""

    def test_a_completed_turn_hands_back_the_published_turn_record(self) -> None:
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertIs(self.turn_record, settlement.turn_record)
        self.assertIs(self.after, settlement.snapshot)
        self.assertIs(self.published, self.ctx.final_snapshots[-1])

    def test_a_completed_session_advances_the_run_s_own_counter(self) -> None:
        self.settle()
        self.assertEqual(1, self.ctx.completed_count)
        self.assertEqual([SESSION_ID], self.ctx.payload["completed_session_ids"])

    def test_a_review_session_is_recorded_without_advancing_the_counter(self) -> None:
        self.turn_record["review_verdict"] = {
            "status_line": "ASL-REVIEW: pass",
            "outcome": "pass",
            "total_findings": 0,
            "findings_by_severity": {},
        }
        self.settle(is_review_session=True)
        self.assertEqual(0, self.ctx.completed_count)
        self.assertTrue(self.ctx.payload["review_completed"])
        self.assertEqual("pass", self.ctx.payload["review_verdict"]["outcome"])


class ReviewCycleTests(SettlementTestCase):
    """The last decision on the review lane, and the flag only it clears."""

    def test_a_completed_cycle_stops_the_run_with_a_terminal_reason(self) -> None:
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual("review-complete", self.ctx.payload["reason"])
        self.assertTrue(self.ctx.review_appended)

    def test_a_paused_cycle_stops_without_a_terminal_reason(self) -> None:
        self.patch(
            review_cycle,
            "_advance_review_cycle",
            self.recorder("advance", ("paused", self.after)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertNotIn("reason", self.ctx.payload)

    def test_repair_children_clear_the_flag_and_let_the_loop_go_round(self) -> None:
        self.patch(
            review_cycle,
            "_advance_review_cycle",
            self.recorder("advance", ("continue", self.after)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertFalse(self.ctx.review_appended)
        self.assertIs(self.turn_record, settlement.turn_record)

    def test_the_review_resume_state_is_written_on_every_cycle_outcome(self) -> None:
        for action in ("complete", "paused", "continue"):
            with self.subTest(cycle=action):
                self.trace.clear()
                self.patch(
                    review_cycle,
                    "_advance_review_cycle",
                    self.recorder("advance", (action, self.after)),
                )
                self.settle(is_review_session=True)
                self.assertIn("review-resume", self.trace)

    def test_a_non_review_turn_never_advances_the_cycle(self) -> None:
        self.settle()
        self.assertNotIn("advance", self.trace)


if __name__ == "__main__":
    unittest.main()
