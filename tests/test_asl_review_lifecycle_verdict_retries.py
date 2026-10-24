"""Review verdict retries and ``stop after current`` handling."""

from __future__ import annotations

from agent_session_loop import (
    control_queue,
    plans_content_hash,
    plans_handoff,
    plans_models,
    plans_payloads,
    plans_readiness,
    plans_session_ids,
    review_transitions,
)
from tests.review_lifecycle_mutating_claude import MutatingFakeClaudeMixin
from tests.support import (
    _structured_review_finding,
)

ASL_TEST_LANE = "integration"


class RunReviewVerdictRetryTests(MutatingFakeClaudeMixin):
    """Review verdict retries and ``stop after current`` handling."""

    def test_invalid_review_verdict_retries_in_run_then_completes(self) -> None:
        review_session = self._setup_complete_plan_with_pending_review()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-artifact,all-clear"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("all-clear", payload["review_verdict"]["outcome"])
        retries = self._review_verdict_retries()
        self.assertEqual(1, len(retries))
        self.assertEqual(1, retries[0]["attempt"])
        self.assertEqual(
            review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS,
            retries[0]["max_attempts"],
        )
        self.assertEqual("malformed-review-artifact", retries[0]["failure_reason"])
        self.assertTrue(str(retries[0]["fingerprint"]).startswith("review-verdict:"))
        self.assertIn("bogus", retries[0]["error"])
        # The retry costs a turn, not a human relaunch.
        self.assertEqual(2, len(payload["turns"]))
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", review_text)
        self.assertIn("review verdict validation failed", review_text)

    def test_persistently_invalid_review_verdict_pauses_when_exhausted(
        self,
    ) -> None:
        review_session = self._setup_complete_plan_with_pending_review()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                "FAKE_CODEX_REVIEW_OUTCOMES": (
                    "invalid-artifact,invalid-artifact-2,invalid-artifact-3"
                )
            },
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertTrue(details["review_verdict_repair_exhausted"])
        self.assertEqual(3, details["review_verdict_repair_attempt"])
        self.assertEqual(
            review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS,
            details["review_verdict_repair_max_attempts"],
        )
        self.assertEqual("malformed-review-artifact", details["failure_reason"])
        retries = self._review_verdict_retries()
        self.assertEqual([1, 2], [record["attempt"] for record in retries])
        # Distinct validation errors must not collapse into one fingerprint.
        self.assertEqual(2, len({record["fingerprint"] for record in retries}))
        self.assertEqual(3, len(payload["turns"]))
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_repeated_review_verdict_error_pauses_on_second_failure(
        self,
    ) -> None:
        # A clamped outcome list replays the same defect, so the second
        # rejection carries a fingerprint the runner has already seen.
        self._setup_complete_plan_with_pending_review()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-artifact"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertTrue(details["review_verdict_repair_exhausted"])
        self.assertEqual(2, details["review_verdict_repair_attempt"])
        retries = self._review_verdict_retries()
        self.assertEqual(1, len(retries))
        self.assertEqual(
            retries[0]["fingerprint"], details["review_verdict_fingerprint"]
        )
        self.assertEqual(2, len(payload["turns"]))

    def test_review_verdict_retry_still_trips_mutation_guard(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_session = self._write_pending_review_session()
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude(invalid_first_attempt=True)

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(1, len(self._review_verdict_retries()))
        guard = payload["turns"][1]["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])
        self.assertEqual(
            "pending", plans_handoff.read_session_status(review_session).status
        )

    def test_stop_after_current_converts_review_verdict_retry_to_pause(
        self,
    ) -> None:
        self._setup_complete_plan_with_pending_review()
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-artifact,all-clear"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertFalse(details["review_verdict_repair_exhausted"])
        self.assertEqual(
            "stop-after-current", details["review_verdict_repair_declined"]
        )
        self.assertEqual([], self._review_verdict_retries())
        self.assertEqual(1, len(payload["turns"]))

    def test_stop_after_current_prevents_the_generated_review(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual("stopped", payload["review_skipped_reason"])
        self.assertEqual("initial-review", payload["review_resume"]["action"])
        self.assertEqual(
            [plans_payloads.SESSION_KIND_IMPLEMENTATION],
            [turn["session_kind"] for turn in payload["turns"]],
        )
        self.assertEqual([], self._generated_review_sessions())

        resumed_completed, resumed = self._run(review_harness="claude")

        self.assertEqual(0, resumed_completed.returncode, resumed_completed.stderr)
        self.assertTrue(resumed["review_completed"])
        self.assertEqual("2", resumed["review_session_id"])

    def test_stop_after_current_suppresses_repair_materialization(
        self,
    ) -> None:
        self._write_reviewed_plan([_structured_review_finding()])
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([], payload["turns"])
        self.assertFalse((self.plan_dir / "session-2.1.md").is_file())

    def test_stop_after_current_during_review_still_materializes_repairs(
        self,
    ) -> None:
        # Accepted asymmetry: a stop landing *during* the review turn does not
        # unwind the cycle the turn just completed, so the repair children are
        # written; the loop then stops before executing any of them.
        self._setup_complete_plan_with_pending_review()
        self._queue_stop_after_current()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(1, len(payload["turns"]))
        repair = self.plan_dir / "session-2.1.md"
        self.assertTrue(repair.is_file())
        self.assertEqual(
            "pending",
            plans_handoff.read_session_status(
                plans_models.SessionFile(
                    plans_session_ids.parse_session_id("2.1"), repair
                )
            ).status,
        )

    def test_stop_after_current_keeps_a_terminal_plan_all_complete(
        self,
    ) -> None:
        self._write_reviewed_plan([])
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual([], payload["turns"])

    def test_stop_after_current_reports_a_capped_plan_unresolved(self) -> None:
        self._write_reviewed_plan([_structured_review_finding()])
        self._queue_stop_after_current()

        completed, payload = self._run(review_harness="claude", max_review_fix_cycles=0)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        self.assertEqual("higher-cap-required", payload["review_resume"]["blocker"])
        self.assertEqual([], payload["turns"])
        self.assertFalse((self.plan_dir / "session-2.1.md").is_file())
