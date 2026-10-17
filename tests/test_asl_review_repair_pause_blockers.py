"""Repair eligibility, standing pause and withdrawn cycle tests."""

import json
import unittest
from unittest import mock

from agent_session_loop import (
    authoring_transaction,
    events,
    pause_policy,
    plans_content_hash,
    plans_models,
    plans_readiness,
    plans_session_ids,
    plans_snapshot,
    review,
    review_continuation,
    review_delta,
    review_repair_planning,
    review_session_body,
)
from tests.review_materialization_support import MaterializationFixtures


class ReviewRepairPauseBlockerTests(MaterializationFixtures, unittest.TestCase):
    """Keep pending repairs eligible or blocked around pauses and refusals."""

    def test_continuation_reconciles_not_loop_ready_and_crash_lineage(
        self,
    ) -> None:
        self._write_plan([self._finding("F1", "runner")])
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)
        child_path = materialized.children[0].path
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "fingerprints": ["finding:legacy"],
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )

        pending = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual("run-repairs", pending.action)
        # The children exist and look runnable, but the gate that refused them
        # has not been cleared, so they are not eligible yet.
        self.assertFalse(pending.eligible)
        self.assertEqual("not-loop-ready", pending.blocker)
        self.assertEqual(1, pending.historical_materializations)
        self.assertEqual(("2.1",), pending.pending_repair_session_ids)
        self.assertIsNone(pending.artifact_valid)
        self.assertIsNone(pending.artifact_error)
        self.assertEqual(
            1, review_delta._review_cycle_state(event_path)["repairs_materialized"]
        )

        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_cycle": 1,
                    "source_review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                    "materialized": True,
                },
            ),
        )
        deduped = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual(1, deduped.historical_materializations)
        self.assertEqual(
            1, review_delta._review_cycle_state(event_path)["repairs_materialized"]
        )

        event_path.unlink()
        crashed = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual("run-repairs", crashed.action)
        self.assertEqual(1, crashed.historical_materializations)

        child_path.write_text(
            child_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        rereview = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual("rereview", rereview.action)
        self.assertTrue(rereview.eligible)
        self.assertIsNone(rereview.artifact_valid)
        self.assertIsNone(rereview.artifact_error)

    def test_clearing_the_refusal_makes_the_repairs_eligible_again(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-08-17T00:00:00Z",
            ),
        )

        resume = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("run-repairs", resume.action)
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def test_a_standing_workspace_pause_blocks_the_pending_repairs(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        eligible_log = event_path.read_text(encoding="utf-8")
        for reason in pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS:
            with self.subTest(reason=reason):
                event_path.write_text(eligible_log, encoding="utf-8")
                self._append_run(event_path, "run-1", paused=reason)

                resume = review_continuation.evaluate_review_continuation(
                    plans_snapshot.inspect_plan(self.plan_dir), event_path
                )

                # The children are on disk and runnable, but the precondition the
                # run stopped on is still true, so the same state.json cannot
                # honestly call them eligible while it reports the pause.
                self.assertEqual("run-repairs", resume.action)
                self.assertFalse(resume.eligible)
                self.assertEqual(reason, resume.blocker)
                self.assertEqual(("2.1",), resume.pending_repair_session_ids)

    def test_a_later_run_start_clears_the_standing_pause_blocker(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        self._append_run(event_path, "run-1", paused="commit-preflight")
        self._append_run(event_path, "run-2", paused=None)

        resume = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        # Rerunning the plan after fixing the workspace is the whole remedy, and
        # it is also what keeps the blocker inert for a run still in flight.
        self.assertEqual("run-repairs", resume.action)
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def test_finishing_the_paused_run_leaves_the_blocker_standing(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        self._append_run(event_path, "run-1", paused="commit-preflight")
        events.append_event(
            event_path,
            events.Event(events.RUN_FINISHED_EVENT, {"run_id": "run-1", "ok": False}),
        )

        resume = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        # The runner closes paused runs with run_finished too, so treating that
        # record as the all-clear would make the blocker unreachable in practice.
        self.assertFalse(resume.eligible)
        self.assertEqual("commit-preflight", resume.blocker)

    def test_a_pause_an_answer_can_clear_does_not_block_the_repairs(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        self._append_run(event_path, "run-1", paused="needs-decision")
        self.assertEqual(
            pause_policy.USER_ACTION_REPLY,
            pause_policy.classify_pause("needs-decision")["user_action"],
        )

        resume = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        # A pause waiting on a mailbox answer says nothing about whether the
        # workspace can host the next run, so it is not this blocker's business.
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def test_a_stale_loop_ready_stamp_does_not_clear_the_refusal(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="0" * 64,
                checked_at="2026-08-17T00:00:00Z",
            ),
        )

        resume = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        self.assertFalse(resume.eligible)
        self.assertEqual("not-loop-ready", resume.blocker)

    def test_materialized_repairs_stay_eligible_without_a_refusal(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "repairs-materialized",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )

        resume = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("run-repairs", resume.action)
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def test_continuation_projects_group_exhausted_from_run_config(
        self,
    ) -> None:
        first = self._finding("F1", "runner")
        second = self._finding(
            "F2",
            "runner",
            title="Fix the rerouted runner failure",
            evidence=[{"path": "pkg/F2.py", "symbol": "retry_loop"}],
            explanation="F2 is still broken in a different way.",
            done_criteria=["F2 is fixed and tested."],
        )
        self._write_plan([first])
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)
        child_path = materialized.children[0].path
        child_path.write_text(
            child_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        review_body = review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-3.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "# Review\n\n## Findings\n\n"
            "See pkg/F2.py and README.md.\n\n"
            f"{self._status_line([second])}\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [second]}),
            encoding="utf-8",
        )
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                "fingerprints": [review.finding_fingerprint(first)],
                "repair_session_ids": ["2.1"],
                "repair_groups": [
                    {
                        "repair_group": "runner",
                        "repair_session_id": "2.1",
                        "finding_ids": ["F1"],
                    }
                ],
                "max_review_fix_cycles": 4,
            }
        )
        event_path.with_name(events.STATE_FILE_NAME).write_text(
            json.dumps({"run_config": {"max_repair_group_attempts": 1}}),
            encoding="utf-8",
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
            projected.blocker,
        )
        self.assertEqual(1, projected.max_repair_group_attempts)
        self.assertEqual({"runner": 1}, projected.repair_group_attempts)
        self.assertEqual(
            (review.finding_fingerprint(second),),
            projected.current_fingerprints,
        )
        self.assertEqual((), projected.repair_session_ids)
        payload = projected.to_payload()
        self.assertEqual(1, payload["max_repair_group_attempts"])
        self.assertEqual({"runner": 1}, payload["repair_group_attempts"])
        self.assertEqual(
            (
                'the recurring repair group "runner" already hit 1 attempt; '
                "fix it manually or re-split instead of raising "
                "--max-review-fix-cycles"
            ),
            review_continuation.review_resume_guidance(
                payload, review_harness_active=False
            ),
        )

    def test_review_resume_guidance_avoids_higher_cap_for_exhausted_group(
        self,
    ) -> None:
        resume = review_continuation.ReviewResume(
            source_cycle=2,
            review_session_id="2",
            source_disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            action="materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            historical_materializations=2,
            minimum_required_cap=3,
            selected_cap=2,
            max_repair_group_attempts=2,
            repair_group_attempts={"runner": 2},
            repair_groups=(
                {
                    "repair_group": "runner",
                    "repair_session_id": "2.1",
                    "finding_ids": ["F1"],
                },
            ),
        )

        guidance = review_continuation.review_resume_guidance(
            resume.to_payload(), review_harness_active=False
        )

        self.assertIn('repair group "runner"', guidance or "")
        self.assertIn("2 attempts", guidance or "")
        self.assertIn("fix it manually or re-split", guidance or "")
        self.assertNotIn("or higher", guidance or "")

    def test_continuation_skips_repair_planning_for_terminal_states(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        for disposition, repair_ids, blocker, materializations in (
            ("all-clear", [], "all-clear", 0),
            ("findings-repeated", [], "findings-repeated", 0),
            ("unstructured-findings", [], "unstructured-findings", 0),
            ("repairs-materialized", [], "repair-sessions-missing", 1),
            (
                "repairs-materialized",
                ["2.1"],
                "repair-sessions-missing",
                1,
            ),
            ("future-disposition", [], "unknown-disposition", 0),
        ):
            with self.subTest(disposition=disposition, repair_ids=repair_ids):
                event_path = self._write_review_events(
                    {
                        "cycle": 1,
                        "review_session_id": "2",
                        "disposition": disposition,
                        "fingerprints": [],
                        "repair_session_ids": repair_ids,
                        "max_review_fix_cycles": 8,
                    }
                )
                with mock.patch.object(
                    review_repair_planning,
                    "plan_review_repair_sessions",
                    side_effect=AssertionError(
                        "terminal continuation must not plan repair children"
                    ),
                ):
                    terminal = review_continuation.evaluate_review_continuation(
                        plans_snapshot.inspect_plan(self.plan_dir), event_path
                    )
                self.assertEqual("terminal", terminal.action)
                self.assertFalse(terminal.eligible)
                self.assertEqual(blocker, terminal.blocker)
                self.assertEqual(disposition, terminal.source_disposition)
                self.assertEqual(materializations, terminal.historical_materializations)
                self.assertEqual(materializations + 1, terminal.minimum_required_cap)
                self.assertIsNone(terminal.current_review_output_sha256)
                self.assertIsNone(terminal.current_review_artifact_sha256)

    def test_a_withdrawn_cycle_waits_for_the_refusal_to_be_cleared(self) -> None:
        """ASI-6: withdrawn children are not a deleted child set."""
        self._write_plan([self._finding("F1", "runner")])

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), self._withdrawn_cycle_events()
        )

        self.assertEqual("materialize-repairs", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("not-loop-ready", projected.blocker)
        self.assertEqual("not-loop-ready", projected.source_disposition)
        self.assertEqual(0, projected.historical_materializations)

    def test_a_withdrawn_cycle_resumes_once_the_plan_is_loop_ready(self) -> None:
        """Clearing the blockers is what the operator was told to do."""
        self._write_plan([self._finding("F1", "runner")])
        authoring_transaction.refresh_readiness_metadata(
            self.plan_dir, state="loop-ready"
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), self._withdrawn_cycle_events()
        )

        self.assertEqual("materialize-repairs", projected.action)
        self.assertTrue(projected.eligible)
        self.assertIsNone(projected.blocker)

    def test_a_cycle_that_did_materialize_still_fails_closed(self) -> None:
        """A child set deleted from a real materialization is still missing."""
        self._write_plan([self._finding("F1", "runner")])
        authoring_transaction.refresh_readiness_metadata(
            self.plan_dir, state="loop-ready"
        )
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "not-loop-ready",
                "fingerprints": [],
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 8,
            }
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("terminal", projected.action)
        self.assertEqual("repair-sessions-missing", projected.blocker)


if __name__ == "__main__":
    unittest.main()
