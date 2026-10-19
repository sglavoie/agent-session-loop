from .asl_turn_finalization_support import *


class TerminalAdjudicationTests(unittest.TestCase):
    """The one gate every terminal path is closed through (ASI-133).

    Each test states an ending a run could reach and asks the adjudicator
    whether the success that ending claimed was earned. The evidence lives in
    the append-only event log, so the same questions are answered identically by
    the run that opened a blocker and by any later run resumed over it.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = Path(tmp.name) / "events.jsonl"
        self.persistence = run_persistence._RunPersistence(
            write_state=False, append_events=False
        )

    def append(self, name: str, payload: dict[str, Any]) -> None:
        events.append_event(self.event_path, events.Event(name=name, payload=payload))

    def record_deferral(
        self,
        *,
        session_id: str = "2",
        run_id: str = "run-1",
        checks: tuple[str, ...] = ("broad:1:suite",),
        acceptance: dict[str, Any] | None = None,
    ) -> None:
        """Record a review turn that failed its broad gates and deferred them."""
        self.append(
            events.VERIFICATION_FINISHED_EVENT,
            {
                "run_id": run_id,
                "turn_index": 1,
                "session": session_id,
                "session_id": session_id,
                "passed": False,
                "reason": "checks-failed",
                "acceptance": (
                    {
                        "accepted": True,
                        "reason": DEFERRED_REASON,
                        "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                        "deferred_failure_count": len(checks),
                        "deferred_failure_names": list(checks),
                    }
                    if acceptance is None
                    else acceptance
                ),
            },
        )

    def record_cycle(
        self,
        *,
        cycle: int,
        outcome: str,
        disposition: str,
        session_id: str = "2",
    ) -> None:
        self.append(
            events.REVIEW_CYCLE_EVENT,
            {
                "cycle": cycle,
                "outcome": outcome,
                "disposition": disposition,
                "review_session_id": session_id,
                "repair_session_ids": [],
                "fingerprints": [],
            },
        )

    def clear_review(self) -> dict[str, Any]:
        return {
            "review_requested": True,
            "review_completed": True,
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            ),
            "review_verdict": {
                "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                "total_findings": 0,
            },
        }

    def state(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "paused": False,
            "reason": "all-complete",
            "turns": [],
            "review_requested": False,
        }
        payload.update(overrides)
        return payload

    def adjudicate(
        self, payload: dict[str, Any]
    ) -> run_termination._TerminalAdjudication:
        return run_termination._adjudicate_terminal_run(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
        )

    def test_an_unredeemed_deferral_blocks_the_success_it_promised(self) -> None:
        # ASI-133: a findings review may finish with the plan's own gates
        # failing, but only against a later all-clear. Without one, the run
        # holds a recorded required-check failure and may not report success —
        # not even behind a clear review, which is the narrower claim.
        self.record_deferral(checks=("broad:1:suite", "broad:1:readme"))
        payload = self.state(**self.clear_review())

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            adjudication.blocker.reason,
        )
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        unresolved = payload["pause_details"]["deferred_unresolved"]
        self.assertEqual(["broad:1:suite", "broad:1:readme"], unresolved[0]["checks"])
        self.assertEqual("2", unresolved[0]["session_id"])
        self.assertEqual("run-1", unresolved[0]["run_id"])
        # The claim the run arrived with is preserved beside the refusal.
        self.assertEqual(
            {"claimed_ok": True, "claimed_reason": "all-complete", "blocked": True},
            {
                key: payload["terminal_adjudication"][key]
                for key in ("claimed_ok", "claimed_reason", "blocked")
            },
        )

    def test_a_later_all_clear_cycle_redeems_the_deferral(self) -> None:
        self.record_deferral()
        self.record_cycle(
            cycle=1,
            outcome=review.REVIEW_OUTCOME_FINDINGS,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        )
        self.record_cycle(
            cycle=2,
            outcome=review.REVIEW_OUTCOME_ALL_CLEAR,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            session_id="3",
        )
        payload = self.state(**self.clear_review())

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_a_deferral_that_never_bought_repairs_is_never_redeemed(self) -> None:
        # A cycle the cap exhausted materialized no repairs, so its broad
        # failures were never owed to anything; a later all-clear over a
        # different review cannot retroactively discharge them.
        self.record_deferral()
        self.record_cycle(
            cycle=1,
            outcome=review.REVIEW_OUTCOME_FINDINGS,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
        )
        self.record_cycle(
            cycle=2,
            outcome=review.REVIEW_OUTCOME_ALL_CLEAR,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            session_id="3",
        )

        payload = self.state(**self.clear_review())
        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            adjudication.blocker.reason,
        )

    def test_an_operator_requested_stop_cannot_strand_a_deferral_green(self) -> None:
        # The exact ASI-133 residual: the loop stops for a reason nobody
        # disputes, and the stop is what keeps the redeeming review from ever
        # running. The ending is legitimate; the success is not.
        self.record_deferral()

        payload = self.state(reason="stopped")
        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual("stopped", adjudication.claimed_reason)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )

    def test_an_unverifiable_acceptance_is_not_evidence_of_a_deferral(self) -> None:
        # A run resumed over an older event log reads acceptances a previous
        # build wrote. One whose shape cannot be checked proves nothing, so the
        # failure under it is an ordinary unpassed check rather than a deferral.
        payload = self.state(
            turns=[
                {
                    "session_id": "2",
                    "turn_index": 1,
                    "verification": {
                        "session": "2",
                        "passed": False,
                        "reason": "checks-failed",
                        "acceptance": {
                            "accepted": True,
                            "reason": DEFERRED_REASON,
                            "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                            "deferred_failure_count": 2,
                            "deferred_failure_names": ["broad:1:suite"],
                        },
                    },
                }
            ],
        )

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            adjudication.blocker.reason,
        )
        self.assertEqual(
            [{"session_id": "2", "turn_index": 1, "reason": "checks-failed"}],
            payload["pause_details"]["unpassed_verifications"],
        )

    def test_a_plan_called_complete_needs_a_review_that_came_back_clear(self) -> None:
        payload = self.state(
            review_requested=True,
            review_completed=False,
            review_skipped_reason="harness-unavailable",
        )

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(
            verification_transitions.REVIEW_NOT_CLEAR_REASON,
            adjudication.blocker.reason,
        )
        self.assertEqual(
            verification_transitions.REVIEW_NOT_CLEAR_REASON, payload["pause_reason"]
        )
        self.assertEqual(
            "harness-unavailable", payload["pause_details"]["review_skipped_reason"]
        )

    def test_open_findings_are_not_a_clear_review(self) -> None:
        payload = self.state(
            review_requested=True,
            review_completed=True,
            review_fix_disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            review_verdict={
                "outcome": review.REVIEW_OUTCOME_FINDINGS,
                "total_findings": 3,
                "findings_by_severity": {"high": 1, "med": 2, "low": 0},
            },
        )

        adjudication = self.adjudicate(payload)

        self.assertEqual(
            verification_transitions.REVIEW_NOT_CLEAR_REASON,
            adjudication.blocker.reason,
        )
        self.assertEqual(3, payload["pause_details"]["unresolved_findings"])
        self.assertEqual(
            review.REVIEW_OUTCOME_FINDINGS, payload["pause_details"]["review_outcome"]
        )

    def test_an_early_exit_is_never_asked_for_a_final_review(self) -> None:
        # ``stopped`` and ``max-sessions`` report the exit an operator asked
        # for. Neither claims the plan is finished, so neither owes a review.
        for reason in ("stopped", "max-sessions"):
            with self.subTest(reason=reason):
                payload = self.state(
                    reason=reason, review_requested=True, review_completed=False
                )

                adjudication = self.adjudicate(payload)

                self.assertFalse(adjudication.blocked)
                self.assertTrue(payload["ok"])
                self.assertEqual(reason, payload["reason"])

    def test_a_plan_that_asked_for_no_review_completes_without_one(self) -> None:
        payload = self.state(review_requested=False)

        self.assertFalse(self.adjudicate(payload).blocked)
        self.assertTrue(payload["ok"])

    def test_a_run_that_already_paused_keeps_its_own_blocker(self) -> None:
        # The specific pause is what an operator needs; restating it as a
        # generic adjudication verdict would lose the evidence attached to it.
        self.record_deferral()
        payload = self.state(
            ok=False,
            paused=True,
            reason="verify-failed",
            pause_reason="verify-failed",
        )

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertFalse(adjudication.claimed_ok)
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertEqual("verify-failed", payload["reason"])

    def test_a_failed_run_still_cannot_strand_an_unredeemed_deferral(self) -> None:
        # Failing is not the same claim as pausing. A run that reports
        # unresolved findings has a repair loop that never converged, and a
        # resume can converge it — so the deferral it still owes belongs in a
        # durable blocker rather than behind a plain failure nobody resumes.
        self.record_deferral()
        payload = self.state(
            ok=False,
            paused=False,
            reason=review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            action_required={
                "reason": review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON
            },
        )

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertFalse(adjudication.claimed_ok)
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            adjudication.claimed_reason,
        )
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        # The narrower reason the review path recorded is not lost to the pause.
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            payload["action_required"]["reason"],
        )

    def test_a_failed_run_owing_nothing_keeps_the_ending_it_reached(self) -> None:
        # The review clause is scoped to endings that assert the plan finished,
        # and this one asserts the opposite, so nothing here is adjudicable.
        payload = self.state(
            ok=False,
            paused=False,
            reason=review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            review_requested=True,
            review_completed=True,
        )

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertFalse(payload["paused"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )

    def test_a_dry_run_claimed_nothing_and_is_not_adjudicated(self) -> None:
        self.record_deferral()
        payload = self.state(reason="dry-run", review_requested=True)

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertTrue(payload["ok"])
        self.assertEqual("dry-run", payload["reason"])

    def test_an_earned_success_records_the_verdict_that_tested_it(self) -> None:
        payload = self.state(**self.clear_review())

        self.adjudicate(payload)

        self.assertEqual(
            {
                "claimed_ok": True,
                "claimed_reason": "all-complete",
                "blocked": False,
                "reason": None,
                "details": {},
            },
            payload["terminal_adjudication"],
        )

    def test_an_adjudicated_blocker_routes_the_operator_to_an_inspection(self) -> None:
        # No mailbox answer makes a failing gate pass, so both reasons have to
        # classify as inspections rather than burning a reply lease first.
        for reason in (
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            verification_transitions.REVIEW_NOT_CLEAR_REASON,
        ):
            with self.subTest(reason=reason):
                policy = pause_policy.classify_pause(reason)

                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT, policy["user_action"]
                )
                self.assertEqual(0, policy["max_retries"])

    def test_the_blocker_reaches_the_payload_with_an_action_and_a_policy(self) -> None:
        self.record_deferral()
        payload = self.state()

        self.adjudicate(payload)

        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_policy"]["reason"],
        )
        action = payload["action_required"]
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON, action["reason"]
        )
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, action["user_action"])
