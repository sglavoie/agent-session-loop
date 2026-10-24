"""Artifact safety and pause UX tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from agent_session_loop import (
    events,
    harnesses,
    pause_policy,
    run_coordination_mailbox_question,
    run_coordination_wait_record,
    run_persistence,
    run_termination,
)
from tests import support_asl_pause_ux as fixtures

ASL_TEST_LANE = "integration"


class ArtifactSafetyAndPauseUxTests(fixtures.ArtifactSafetyAndPauseUxCase):
    def test_verify_failed_pause_keeps_the_validator_reason(self) -> None:
        """ASI-105: the pause record dropped every account of the rejection.

        A generated review gate writes its reason to stderr, which
        ``_strip_check_output`` removes from pause details, and puts a ~2 KB
        argv ahead of it in ``detail``. The structured field is what survives
        both, so a verify-failed pause names the requirement that failed.
        """
        diagnostic = {
            "check": "final-review-output-evidence-based",
            "reason": "missing-required-heading",
        }
        details = {
            "session": 6,
            "failures": [
                {
                    "name": "final-review-output-evidence-based",
                    "kind": "command",
                    "status": "fail",
                    "failure_reason": "command-failed",
                    "returncode": 1,
                    "stdout": "",
                    "stderr": json.dumps(diagnostic, sort_keys=True),
                    "stderr_sha256": "a" * 64,
                    "failure_diagnostic": diagnostic,
                }
            ],
        }

        bounded = run_persistence._bounded_verification_details(details)

        failure = bounded["failures"][0]
        self.assertNotIn("stdout", failure)
        self.assertNotIn("stderr", failure)
        self.assertEqual(diagnostic, failure["failure_diagnostic"])
        self.assertEqual("a" * 64, failure["stderr_sha256"])

    def test_set_pause_attaches_pause_policy_block(self) -> None:
        payload: dict[str, object] = {}
        run_termination.set_pause(
            payload, reason="verify-failed", message="checks failed"
        )
        policy = payload["pause_policy"]
        self.assertEqual(pause_policy.classify_pause("verify-failed"), policy)
        self.assertTrue(policy["recoverable"])
        self.assertFalse(policy["requires_user"])
        self.assertIsNone(policy["user_action"])
        self.assertEqual(2, policy["max_retries"])
        self.assertEqual([], policy["backoff_seconds"])

    def test_set_pause_pause_policy_marks_user_required_harness_failure(
        self,
    ) -> None:
        payload: dict[str, object] = {}
        run_termination.set_pause(
            payload, reason="quota-exhausted", message="out of quota"
        )
        policy = payload["pause_policy"]
        self.assertFalse(policy["recoverable"])
        self.assertTrue(policy["requires_user"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, policy["user_action"])
        self.assertEqual(0, policy["max_retries"])

    def test_set_pause_pause_policy_names_the_reply_resolvable_state(self) -> None:
        # Neither machine-retryable nor a harness failure a reply cannot clear:
        # the pause needs a human, and what the human does is answer.
        # All three are the worker declaring its own stop through
        # `RUNNER_PAUSE`, which is the one path that opens a mailbox wait.
        for reason in ("blocked", "unsafe", "runner-pause"):
            with self.subTest(reason=reason):
                payload: dict[str, object] = {}
                run_termination.set_pause(payload, reason=reason, message="stopped")
                policy = payload["pause_policy"]
                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual(pause_policy.USER_ACTION_REPLY, policy["user_action"])
                self.assertEqual(0, policy["max_retries"])

    def test_workspace_blocked_pauses_are_inspect_not_reply(self) -> None:
        """ASI-114: a pause no reply can clear stopped calling itself `reply`.

        `commit-preflight` names 30+ dirty repositories in its details; no
        mailbox answer makes them clean. Classifying it `reply` sent the runner
        into a mailbox wait that could only ever time out.
        """
        for reason in pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, events.PAUSE_REASONS)
                payload: dict[str, object] = {}
                run_termination.set_pause(payload, reason=reason, message="blocked")
                policy = payload["pause_policy"]
                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT, policy["user_action"]
                )
                self.assertEqual(0, policy["max_retries"])
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT,
                    payload["action_required"]["user_action"],
                )

    def test_a_guard_verdict_is_inspect_not_reply(self) -> None:
        """ASI-114: the reply channel these advertised was never opened.

        A mailbox wait is entered from the runner-pause disposition alone, so a
        run that ends on a guard verdict or an unclassifiable turn outcome
        projected `user_action: reply` with nothing anywhere to reply to.
        """
        for reason in pause_policy.UNANSWERABLE_OUTCOME_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, events.PAUSE_REASONS)
                payload: dict[str, object] = {}
                run_termination.set_pause(payload, reason=reason, message="refused")
                policy = payload["pause_policy"]
                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT, policy["user_action"]
                )
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT,
                    payload["action_required"]["user_action"],
                )

    def test_a_guard_verdict_never_waits_for_a_mailbox_reply(self) -> None:
        # A worker may declare any pause reason through `RUNNER_PAUSE`, so the
        # classification is what keeps a self-declared guard verdict from
        # holding the turn open for an answer that clears nothing.
        result = harnesses.HarnessResult(
            argv=["codex"], returncode=0, stdout="", stderr=""
        )
        for reason in pause_policy.UNANSWERABLE_OUTCOME_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.assertFalse(
                    run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
                        mailbox_enabled=True,
                        pause_reason=reason,
                        result=result,
                    )
                )

    def test_policy_version_marks_the_changed_user_action_meaning(self) -> None:
        # `user_action` changed meaning for reasons that already ship, so a
        # consumer diffing a persisted v3 block must not read it as agreement.
        self.assertEqual(5, pause_policy.POLICY_VERSION)
        self.assertEqual(
            pause_policy.POLICY_VERSION,
            pause_policy.classify_pause("commit-preflight")["policy_version"],
        )

    def test_a_blocked_pause_carries_remediation_from_its_reason(self) -> None:
        """ASI-114: `remediation: []` told an operator to inspect, not where.

        A runner-side refusal carries findings but no repair hints in its
        details, so the stable reason has to resolve one instead.
        """
        for reason in pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS:
            with self.subTest(reason=reason):
                payload: dict[str, object] = {
                    "plan_dir": "/plans/demo",
                    "cwd": "/repo",
                }
                run_termination.set_pause(payload, reason=reason, message="blocked")
                remediation = payload["action_required"]["remediation"]
                self.assertEqual(1, len(remediation))
                entry = remediation[0]
                self.assertEqual(f"runtime.{reason}", entry["check_id"])
                self.assertEqual(reason, entry["category"])
                self.assertTrue(entry["message"])
                self.assertNotIn("<plan-dir>", entry["message"])
                self.assertNotIn("<repo-root>", entry["message"])

    def test_details_repair_hints_still_win_over_the_reason_fallback(self) -> None:
        # The gate that raised the pause resolved its own findings, so its
        # hints are more specific than anything the bare reason can say.
        carried = [
            {
                "check_id": "manifest-readiness.state-not-loop-ready",
                "category": "manifest-readiness",
                "fix_category": "manifest-update",
                "message": "mark it loop-ready",
            }
        ]
        payload: dict[str, object] = {"plan_dir": "/plans/demo", "cwd": "/repo"}
        run_termination.set_pause(
            payload,
            reason="not-loop-ready",
            message="gate refused",
            details={"readiness_gate": {"repair_hints": carried}},
        )

        self.assertEqual(carried, payload["action_required"]["remediation"])

    def test_a_pause_reason_with_no_hint_keeps_an_empty_remediation(self) -> None:
        payload: dict[str, object] = {"plan_dir": "/plans/demo", "cwd": "/repo"}
        run_termination.set_pause(payload, reason="blocked", message="needs an answer")

        self.assertEqual([], payload["action_required"]["remediation"])
        self.assertEqual(
            pause_policy.USER_ACTION_REPLY,
            payload["action_required"]["user_action"],
        )

    def test_a_blocked_pause_never_waits_for_a_mailbox_reply(self) -> None:
        # The gate the runner uses to decide whether to hold the turn open for
        # an answer reads the same classification, so moving the reason moves
        # the wait with it.
        result = harnesses.HarnessResult(
            argv=["codex"], returncode=0, stdout="", stderr=""
        )
        for reason in pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.assertFalse(
                    run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
                        mailbox_enabled=True,
                        pause_reason=reason,
                        result=result,
                    )
                )
        self.assertTrue(
            run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=True,
                pause_reason="blocked",
                result=result,
            )
        )

    def test_new_waits_emit_the_rolling_ceiling_and_not_a_hard_deadline(
        self,
    ) -> None:
        started_at = datetime(2026, 7, 24, 12, tzinfo=UTC)
        waiting = run_coordination_wait_record.waiting_for_reply_payload(
            question_id="question-1",
            session_id="9",
            turn_index=3,
            started_at=started_at,
            base_deadline=started_at + timedelta(minutes=5),
            unsupervised_deadline_at=started_at + timedelta(minutes=30),
            unsupervised_ceiling_seconds=1800.0,
            last_lease_activity_at=None,
        )

        self.assertNotIn("hard_deadline", waiting)
        self.assertEqual(
            (started_at + timedelta(minutes=30)).isoformat(),
            waiting["unsupervised_deadline_at"],
        )
        self.assertEqual(1800.0, waiting["unsupervised_ceiling_seconds"])

        for bad_ceiling in (0, -1.0, float("nan"), float("inf"), "1800", True):
            with self.assertRaisesRegex(ValueError, "unsupervised_ceiling_seconds"):
                run_coordination_wait_record.waiting_for_reply_payload(
                    question_id="question-1",
                    session_id="9",
                    turn_index=3,
                    started_at=started_at,
                    base_deadline=started_at + timedelta(minutes=5),
                    unsupervised_deadline_at=started_at + timedelta(minutes=30),
                    unsupervised_ceiling_seconds=bad_ceiling,
                    last_lease_activity_at=None,
                )


if __name__ == "__main__":
    unittest.main()
