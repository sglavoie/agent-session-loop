#!/usr/bin/env python3
"""Tests for supervise_run recoverable pause retries and pause policy."""

from __future__ import annotations

import unittest

from asl_autopilot_test_bases import SuperviseRunInjectedBase
from asl_autopilot_test_payloads import (
    LOCK_CREATED_AT,
    OLDER_RUN_STARTED_AT,
    RUN_STARTED_AT,
    _commit_preflight_pause,
    _finished_run_status,
    _settled_verify_failed_pause,
    _status_payload,
)
from asl_autopilot_test_support import (  # noqa: F401
    _RunScript,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)


class SuperviseRunPauseRetryTest(SuperviseRunInjectedBase):
    def test_run_invoke_error_stops_with_run_error(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "reason": "run-error",
                    "error": "no JSON",
                    "_invoke_error": True,
                }
            ]
        )
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "run-error")

    def test_a_killed_child_that_stopped_on_request_is_not_an_error(self) -> None:
        # The runner was killed while it finalized an abort the operator asked
        # for, so the launch reports nothing. The state it persisted before
        # dying says the run ended on that request, which is a clean stop.
        self._launch_before(LOCK_CREATED_AT)
        status = _StatusScript(
            [
                _status_payload(1),
                _finished_run_status(
                    1, reason="stopped", last_action="finalization-abort-honored"
                ),
            ]
        )
        run = _RunScript([{"_invoke_error": True, "error": "no JSON"}])

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["stop_kind"], "abort")

    def test_a_killed_child_reclaimed_as_abandoned_reports_its_prior_stop(self) -> None:
        # The kill left the state unfinished, so a later probe reclaimed it as
        # `abandoned`; the reason it overwrote is the one that explains the run.
        self._launch_before(LOCK_CREATED_AT)
        reclaimed = _finished_run_status(
            1, reason="abandoned", finished=False, last_action="abort-turn-honored"
        )
        reclaimed["runner_state"]["abandoned"] = {
            "prior_reason": "stopped",
            "reason": "process-gone",
        }
        status = _StatusScript([_status_payload(1), reclaimed])
        run = _RunScript([{"_invoke_error": True, "error": "no JSON"}])

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["stop_kind"], "abort")

    def test_a_killed_child_with_an_older_stopped_state_stays_an_error(self) -> None:
        # The `stopped` on disk was written before this chunk launched, so it
        # belongs to an earlier run and explains nothing about this death.
        self._launch_before(RUN_STARTED_AT)
        status = _StatusScript(
            [
                _status_payload(1),
                _finished_run_status(
                    1,
                    reason="stopped",
                    run_started_at=OLDER_RUN_STARTED_AT,
                    last_action="abort-turn-honored",
                ),
            ]
        )
        run = _RunScript([{"_invoke_error": True, "error": "no JSON"}])

        result = self._supervise(status, run)

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "run-error")

    def test_recoverable_pause_retries_then_completes(self) -> None:
        # First chunk hits a recoverable verify-failed pause; the supervisor
        # relaunches the same session and the retry completes the plan.
        status = _StatusScript(
            [
                _status_payload(1),
                _status_payload(1),
                _status_payload(0),
            ]
        )
        run = _RunScript(
            [
                {"ok": False, "paused": True, "reason": "verify-failed"},
                {"ok": True, "paused": False, "reason": "all-complete"},
            ]
        )
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # Chunk 1 paused, chunk 2 (the retry) completed.
        self.assertEqual(run.chunks, [1, 2])

    def test_a_verify_failed_pause_with_settled_work_is_retried_to_completion(
        self,
    ) -> None:
        # The whole plan lives between these two chunks. Chunk 1 pauses
        # `verify-failed` and the run commits what its turn retained, so chunk
        # 2 clears its own startup commit gate and finishes the session. No
        # human is asked anything at any point.
        status = _StatusScript(
            [_status_payload(1), _status_payload(1), _status_payload(0)]
        )
        run = _RunScript(
            [
                _settled_verify_failed_pause(),
                {"ok": True, "paused": False, "reason": "all-complete"},
            ]
        )

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual("complete", result["reason"])
        self.assertEqual([1, 2], run.chunks)
        self.assertIsNone(result.get("error"))

    def test_an_unsettled_relaunch_still_dead_ends_on_the_commit_gate(self) -> None:
        # The contrast that gives the test above its meaning. Left in the
        # worktree, the retained work becomes the relaunch's problem, and the
        # supervisor has no move: `commit-preflight` is non-recoverable and
        # asks a human to inspect, so the run stops one chunk after the pause
        # it was supposed to repair.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_policy": autopilot.classify_pause("verify-failed"),
                },
                _commit_preflight_pause(),
            ]
        )

        result = self._supervise(status, run)

        self.assertFalse(result["ok"], result)
        self.assertEqual("user_required", result["reason"])
        self.assertEqual("commit-preflight", result["run"]["pause_reason"])
        self.assertEqual("inspect", result["run"]["pause_policy"]["user_action"])
        # Two chunks, not three: the gate is not retried, so the relaunch
        # budget the verify-failed pause still had is unreachable.
        self.assertEqual([1, 2], run.chunks)

    def test_the_settlement_record_is_not_a_second_repair_verdict(self) -> None:
        # The record carries the repair flag the runner adjudicated, so a
        # settlement made after the budget was spent embeds
        # `verification_repair_exhausted: true`. It is nested under
        # `paused_work_commit`, and the supervisor's gate reads the run's own
        # flag at the top of the details — a copy one level down must not stop
        # a retry the runner did not stop.
        payload = _settled_verify_failed_pause(repair_exhausted=True)
        self.assertTrue(
            payload["pause_details"]["paused_work_commit"][
                "verification_repair_exhausted"
            ]
        )
        self.assertFalse(autopilot._verification_repair_exhausted(payload))

        status = _StatusScript(
            [_status_payload(1), _status_payload(1), _status_payload(0)]
        )
        run = _RunScript(
            [payload, {"ok": True, "paused": False, "reason": "all-complete"}]
        )

        result = self._supervise(status, run)

        self.assertTrue(result["ok"], result)
        self.assertEqual([1, 2], run.chunks)

    def test_recoverable_pause_retry_budget_is_exhausted(self) -> None:
        # The pause never clears, so after max_retries relaunches the supervisor
        # surfaces the pause instead of retrying forever.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "checks failed",
                }
            ]
        )
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(result["error"], "checks failed")
        # Two retries after the initial attempt -> three chunks total.
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(result["retries"]["reason"], "verify-failed")
        self.assertEqual(result["retries"]["count"], 2)
        self.assertTrue(result["retries"]["exhausted"])

    def test_non_recoverable_pause_is_not_retried(self) -> None:
        # A question/blocked pause needs a reply; it must not consume retries.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "input-error",
                    "pause_reason": "input-error",
                    "pause_message": "needs an answer",
                }
            ]
        )
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)
        self.assertFalse(result["retries"]["exhausted"])

    def test_payload_pause_policy_overrides_recoverable_constants(self) -> None:
        # verify-failed is recoverable by the imported constants, but a payload
        # that carries pause_policy={recoverable: false} is the single source of
        # truth and wins: the supervisor surfaces the pause without relaunching.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "checks failed",
                    "pause_policy": {
                        "reason": "verify-failed",
                        "recoverable": False,
                        "requires_user": False,
                        "max_retries": 0,
                        "backoff_seconds": [],
                    },
                }
            ]
        )
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        # No retry consumed even though the constant set marks it recoverable.
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)
        self.assertFalse(result["retries"]["exhausted"])

    def test_payload_pause_policy_requires_user_overrides_constants(self) -> None:
        # A reason absent from the user-required constant set still surfaces as
        # user_required when the payload's pause_policy says so.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "runner-pause",
                    "pause_reason": "runner-pause",
                    "pause_message": "needs a human",
                    "pause_policy": {
                        "reason": "runner-pause",
                        "recoverable": False,
                        "requires_user": True,
                        "max_retries": 0,
                        "backoff_seconds": [],
                    },
                }
            ]
        )
        result = self._supervise(status, run)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1])

    def test_verify_failed_exhausted_flag_stops_without_retry(self) -> None:
        # The runner marked the verification repair exhausted (an identical
        # failure fingerprint repeated), so the supervisor must not relaunch.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "identical checks failed",
                    "pause_details": {"verification_repair_exhausted": True},
                }
            ]
        )
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "user_required")
        # Only the initial chunk ran; no retry consumed.
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)

    def test_review_verdict_exhausted_flag_stops_without_retry(self) -> None:
        # The runner burned its in-run review-verdict retries and reused the
        # verify-failed pause, so relaunching would just replay the same turn.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "invalid review verdict",
                    "pause_details": {"review_verdict_repair_exhausted": True},
                }
            ]
        )
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)

    def test_verify_failed_changed_fingerprint_still_retries(self) -> None:
        # A changed verification failure (exhausted flag clear) stays eligible
        # for another bounded retry, which then completes the plan.
        status = _StatusScript(
            [
                _status_payload(1),
                _status_payload(1),
                _status_payload(0),
            ]
        )
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_details": {"verification_repair_exhausted": False},
                },
                {"ok": True, "paused": False, "reason": "all-complete"},
            ]
        )
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1, 2])


if __name__ == "__main__":
    unittest.main()
