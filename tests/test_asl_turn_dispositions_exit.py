"""Asl turn dispositions exit regressions."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harness_failures,
    model_availability,
    plans_models,
    plans_payloads,
    plans_snapshot,
    turn_dispositions,
)

from .support import write_plan
from .support_asl_turn_dispositions import (
    SESSION_ID,
    SESSION_LABEL,
    STATE_DIR_ARG,
    DispositionTestCase,
)

ASL_TEST_PORTABLE = True


class NonzeroExitTests(DispositionTestCase):
    """A failed harness exit, and the one retry it can earn."""

    def classification(self, reason: str) -> harness_failures.FailureClassification:
        return harness_failures.FailureClassification(
            reason=reason,
            retryable=True,
            fingerprint="fp",
            evidence="",
            returncode=1,
            retry_at="2026-01-01T01:00:00Z",
        )

    def settle(
        self,
        *,
        reason: str = events.PAUSE_REASON_QUOTA_EXHAUSTED,
        decision: str = "spark",
        after: plans_models.PlanSnapshot | None = None,
        cooldown: Any = None,
        suppress_error: Exception | None = None,
        spark_run_suppression: str | None = None,
    ) -> Any:
        self.spark_route: dict[str, Any] = {"decision": decision}
        self.turn_payload: dict[str, Any] = {}
        suppress = mock.Mock(return_value=cooldown, side_effect=suppress_error)
        with (
            mock.patch.object(
                harness_failures,
                "classify_harness_failure",
                return_value=self.classification(reason),
            ),
            mock.patch.object(model_availability, "suppress_until", suppress),
        ):
            self.suppress = suppress
            return turn_dispositions.settle_nonzero_exit(
                self.ctx,
                before=self.before,
                after=self.after if after is None else after,
                result=self.result(returncode=2),
                spark_route=self.spark_route,
                session_id=SESSION_ID,
                session_label=SESSION_LABEL,
                turn_index=1,
                turn_payload=self.turn_payload,
                session_details=self.session_details,
                paths=self.paths(),
                state_dir_arg=STATE_DIR_ARG,
                spark_run_suppression=spark_run_suppression,
            )

    def cooldown(self) -> Any:
        record = mock.Mock()
        record.to_dict.return_value = {"available": False, "retry_at": "later"}
        return record

    def test_a_retryable_spark_failure_falls_back_and_retries(self) -> None:
        outcome = self.settle(cooldown=self.cooldown())
        assert outcome is not None
        self.assertIs(self.after, outcome.snapshot)
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, outcome.fallback_reason)
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual(
            events.PAUSE_REASON_QUOTA_EXHAUSTED,
            self.spark_route["fallback_reason"],
        )
        self.assertEqual(self.spark_route, self.turn_payload["spark_route"])
        self.assertEqual(
            ["spark_fallback"], [name for name, _ in self.ctx.persistence.events]
        )
        self.assertEqual(1, len(self.ctx.persistence.states))
        self.assertTrue(self.paths()["result"].exists())

    def test_a_written_ledger_leaves_the_run_scoped_suppression_alone(self) -> None:
        outcome = self.settle(
            cooldown=self.cooldown(), spark_run_suppression="already-set"
        )
        assert outcome is not None
        self.assertEqual("already-set", outcome.run_suppression)
        self.assertIs(False, self.ctx.payload["spark"]["available"])

    def test_an_unwritable_ledger_suppresses_spark_for_this_run_only(self) -> None:
        outcome = self.settle(suppress_error=OSError("read-only"))
        assert outcome is not None
        self.assertEqual(
            f"{events.PAUSE_REASON_QUOTA_EXHAUSTED}-for-current-run",
            outcome.run_suppression,
        )
        self.assertEqual(outcome.run_suppression, self.ctx.payload["spark"]["reason"])
        self.assertIsNone(self.ctx.payload["spark"]["retry_at"])
        self.assertIn("read-only", self.ctx.payload["spark"]["warning"])

    def test_a_spark_failure_that_left_a_trace_is_not_retried(self) -> None:
        write_plan(self.plan_dir, ("complete",))
        moved = plans_snapshot.inspect_plan(self.plan_dir)
        outcome = self.settle(after=moved)
        self.assertIsNone(outcome)
        self.suppress.assert_not_called()
        self.assertIs(True, self.ctx.pauses[0]["details"]["spark_fallback_unsafe"])

    def test_a_non_spark_failure_pauses_with_its_classification(self) -> None:
        outcome = self.settle(decision="baseline")
        self.assertIsNone(outcome)
        self.suppress.assert_not_called()
        pause = self.ctx.pauses[0]
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, pause["reason"])
        self.assertEqual(
            f"{SESSION_LABEL} failed: {events.PAUSE_REASON_QUOTA_EXHAUSTED} "
            "(harness exit 2)",
            pause["message"],
        )
        self.assertEqual(2, pause["details"]["returncode"])
        self.assertNotIn("spark_fallback_unsafe", pause["details"])
        self.assertEqual([self.after], self.ctx.final_snapshots)

    def test_a_plain_nonzero_exit_reports_the_status_rather_than_a_reason(
        self,
    ) -> None:
        self.settle(reason=events.PAUSE_REASON_NONZERO_EXIT, decision="baseline")
        self.assertEqual(
            f"harness exited with status 2 for {SESSION_LABEL}",
            self.ctx.pauses[0]["message"],
        )

    def claimed_complete(self) -> plans_models.PlanSnapshot:
        """The after-image of a turn that claimed the session and then died."""
        write_plan(self.plan_dir, ("complete",))
        return plans_snapshot.inspect_plan(self.plan_dir)

    def test_an_unsettled_completion_is_taken_back_before_the_run_pauses(
        self,
    ) -> None:
        self.settle(after=self.claimed_complete(), decision="baseline")
        reverted = plans_snapshot.inspect_plan(self.plan_dir)
        self.assertEqual("pending", reverted.sessions[0].status)
        record = self.turn_payload["completion_reconciliation"]
        self.assertIs(True, record["reverted"])
        self.assertEqual("complete", record["claimed_status"])
        self.assertEqual("pending", record["restored_status"])
        self.assertEqual(2, record["returncode"])
        self.assertEqual(
            record, self.ctx.pauses[0]["details"]["completion_reconciliation"]
        )

    def test_the_published_snapshot_is_the_reconciled_one(self) -> None:
        self.settle(after=self.claimed_complete(), decision="baseline")
        published = self.ctx.final_snapshots[0]
        self.assertEqual("pending", published.sessions[0].status)
        self.assertEqual(
            plans_payloads.snapshot_payload(published),
            self.ctx.payload["final_snapshot"],
        )

    def test_the_reversion_is_recorded_as_a_reopen(self) -> None:
        self.settle(after=self.claimed_complete(), decision="baseline")
        reopens = [
            payload
            for name, payload in self.ctx.persistence.events
            if name == events.SESSION_REOPENED_EVENT
        ]
        self.assertEqual(1, len(reopens))
        self.assertEqual("complete", reopens[0]["from_status"])
        self.assertEqual("pending", reopens[0]["to_status"])
        self.assertEqual("turn-completion-reconciliation", reopens[0]["guard"])
        self.assertEqual(
            plans_snapshot.inspect_plan(self.plan_dir).content_sha256,
            reopens[0]["content_sha256"],
        )

    def test_a_session_that_claimed_nothing_is_left_alone(self) -> None:
        self.settle(decision="baseline")
        self.assertNotIn("completion_reconciliation", self.turn_payload)
        self.assertNotIn("completion_reconciliation", self.ctx.pauses[0]["details"])
        self.assertEqual([], [name for name, _ in self.ctx.persistence.events])
        self.assertEqual([self.after], self.ctx.final_snapshots)

    def test_an_unwritable_session_file_still_reports_the_claim_it_kept(
        self,
    ) -> None:
        after = self.claimed_complete()
        with mock.patch.object(
            turn_dispositions.handoff_guards,
            "_revert_session_status_to_pending",
            return_value=None,
        ):
            self.settle(after=after, decision="baseline")
        record = self.turn_payload["completion_reconciliation"]
        self.assertIs(False, record["reverted"])
        self.assertEqual("complete", record["restored_status"])
        self.assertIsNone(record["session_path"])
        self.assertEqual([], [name for name, _ in self.ctx.persistence.events])
        self.assertEqual([after], self.ctx.final_snapshots)

    def test_a_retryable_spark_failure_reconciles_nothing(self) -> None:
        outcome = self.settle(cooldown=self.cooldown())
        self.assertIsNotNone(outcome)
        self.assertNotIn("completion_reconciliation", self.turn_payload)


if __name__ == "__main__":
    unittest.main()
