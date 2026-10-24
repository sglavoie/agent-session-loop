"""The verification lane as a model, exercised without the run loop.

Verification scheduling, adjudication, the bounded repair budget, and the
terminal required-check policy are decisions about inputs; every effect they
earn stays in ``runner``. These tests drive the decisions directly, which is the
point of the extraction: if they could only be reached through a live run, the
lane would not be a lane.

Two of the tests are model tests rather than case tests. Every triple the
machine declares is walked, and every ``(state, event)`` pair it does *not*
declare is asserted to raise, so a transition added to the table without a
policy — or a policy that quietly accepts an out-of-order step — fails here
rather than in a durable record."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    review,
    verification_cli,
    verification_transitions,
)
from agent_session_loop.transitions import IllegalTransitionError

from .support import assert_no_runner_import
from .support_asl_verification_transitions import (
    DEFERRED_REASON,
    MACHINE,
    _check,
    _contract,
    _result,
)

ASL_TEST_PORTABLE = True


class VerificationMachineModelTests(unittest.TestCase):
    """The declared lifecycle, walked exhaustively in both directions."""

    def test_verification_transitions_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, verification_transitions)

    def test_every_declared_transition_advances_to_its_target(self) -> None:
        for state, event, target in MACHINE.legal_transitions():
            with self.subTest(state=state, event=event):
                self.assertEqual(MACHINE.advance(state, event), target)

    def test_every_undeclared_pair_is_refused(self) -> None:
        for state, event in MACHINE.illegal_transitions():
            with self.subTest(state=state, event=event):
                with self.assertRaises(IllegalTransitionError) as raised:
                    MACHINE.advance(state, event)
                self.assertEqual(raised.exception.state, state)
                self.assertEqual(raised.exception.event, event)

    def test_settled_states_are_exactly_the_lane_endings(self) -> None:
        self.assertEqual(
            MACHINE.terminal_states,
            frozenset({"unenforced", "passed", "deferred", "failed", "refused"}),
        )

    def test_deferred_is_not_a_synonym_for_passed(self) -> None:
        # Both continue the run, but only one claims the gates were met; the
        # terminal adjudicator grades them differently.
        self.assertIn(
            "deferred", verification_transitions.CONTINUING_VERIFICATION_STATES
        )
        self.assertNotIn(
            "failed", verification_transitions.CONTINUING_VERIFICATION_STATES
        )

    def test_cursor_refuses_an_out_of_order_step(self) -> None:
        cursor = MACHINE.start()
        cursor.advance("contract-ready")
        with self.assertRaises(IllegalTransitionError):
            cursor.advance("pass")  # a recipe that never started cannot pass
        self.assertEqual(cursor.state, "contract-ready")

    def test_cursor_records_the_route_it_took(self) -> None:
        cursor = MACHINE.start()
        cursor.advance("contract-ready")
        cursor.advance("start")
        cursor.advance("defer")
        self.assertEqual(
            cursor.history, ("scheduled", "contract-ready", "running", "deferred")
        )
        self.assertTrue(cursor.settled)


class ScheduleVerificationTests(unittest.TestCase):
    def test_a_readable_contract_schedules_the_recipe(self) -> None:
        schedule = verification_transitions.schedule_verification(
            contract=_contract("tests"),
            contract_error=None,
            allow_not_loop_ready=False,
            session_label="session 1",
        )
        self.assertEqual(
            (schedule.event, schedule.state), ("contract-ready", "contract-ready")
        )
        self.assertTrue(schedule.runs)
        self.assertIsNone(schedule.refusal)

    def test_a_malformed_contract_is_refused_even_when_loop_ready_is_waived(
        self,
    ) -> None:
        # An unreadable recipe is a plan defect, not an absent gate, so
        # --allow-not-loop-ready does not waive it.
        schedule = verification_transitions.schedule_verification(
            contract=None,
            contract_error="bad table",
            allow_not_loop_ready=True,
            session_label="session 1",
        )
        self.assertEqual(schedule.state, "refused")
        assert schedule.refusal is not None
        self.assertEqual(
            schedule.refusal.reason,
            verification_transitions.VERIFY_REFUSAL_MALFORMED_CONTRACT,
        )
        self.assertEqual(schedule.refusal.error, "bad table")
        self.assertIn("session 1", schedule.refusal.message)
        self.assertEqual(schedule.refusal.guard_detail, {"error": "bad table"})

    def test_an_absent_contract_is_waived_only_when_the_run_was_told_to(self) -> None:
        waived = verification_transitions.schedule_verification(
            contract=None,
            contract_error=None,
            allow_not_loop_ready=True,
            session_label="session 1",
        )
        self.assertEqual((waived.state, waived.enforced), ("unenforced", False))
        self.assertEqual(
            waived.turn_verification("1"),
            {"session": "1", "reason": "no-contract", "enforced": False},
        )

        refused = verification_transitions.schedule_verification(
            contract=None,
            contract_error=None,
            allow_not_loop_ready=False,
            session_label="session 1",
        )
        self.assertEqual(refused.state, "refused")
        assert refused.refusal is not None
        self.assertEqual(
            refused.refusal.turn_verification("1"),
            {"session": "1", "reason": "no-contract", "enforced": True},
        )
        self.assertIsNone(refused.refusal.guard_detail)

    def test_every_schedule_event_is_declared_by_the_machine(self) -> None:
        cases = [
            (None, "bad table", False),
            (_contract("tests"), None, False),
            (None, None, True),
            (None, None, False),
        ]
        for contract, error, waive in cases:
            with self.subTest(error=error, waive=waive):
                schedule = verification_transitions.schedule_verification(
                    contract=contract,
                    contract_error=error,
                    allow_not_loop_ready=waive,
                    session_label="session 1",
                )
                self.assertEqual(
                    MACHINE.advance("scheduled", schedule.event), schedule.state
                )


class RunRefusalTests(unittest.TestCase):
    def test_an_interrupt_closes_the_live_record_and_pauses(self) -> None:
        refusal = verification_transitions.refuse_interrupted_verification(
            session_label="session 1"
        )
        self.assertEqual(refusal.reason, "interrupted")
        self.assertEqual(refusal.live_finish_reason, "interrupted")
        self.assertIsNone(refusal.error)
        self.assertEqual(
            refusal.turn_verification("1"), {"session": "1", "reason": "interrupted"}
        )
        self.assertEqual(refusal.pause_details({"session": "1"}), {"session": "1"})
        self.assertEqual(MACHINE.advance("running", "run-refused"), "refused")

    def test_an_unexecutable_recipe_reports_the_host_error(self) -> None:
        refusal = verification_transitions.refuse_unavailable_verification(
            session_label="session 1", error="no such file"
        )
        self.assertEqual(refusal.reason, verification_cli.VERIFY_EXIT_REASON_FAILED)
        self.assertEqual(refusal.error, "no such file")
        self.assertEqual(
            refusal.turn_verification("1"),
            {"session": "1", "reason": "verify-failed", "error": "no such file"},
        )
        self.assertEqual(
            refusal.pause_details({"session": "1"}),
            {"session": "1", "error": "no such file"},
        )


class AdjudicateVerificationTests(unittest.TestCase):
    def test_a_clean_recipe_passes(self) -> None:
        graded = verification_transitions.adjudicate_verification(
            result=_result(
                _check("tests", status="pass", failure_reason=None, returncode=0)
            ),
            contract=_contract("tests"),
        )
        self.assertEqual((graded.event, graded.state), ("pass", "passed"))
        self.assertTrue(graded.passed)
        self.assertTrue(graded.continues)
        self.assertIsNone(graded.summary)
        self.assertIsNone(graded.acceptance)
        self.assertEqual(graded.passed_count, 1)

    def test_a_failing_recipe_fails_and_summarizes_every_gate(self) -> None:
        graded = verification_transitions.adjudicate_verification(
            result=_result(_check("tests"), _check("lint")),
            contract=_contract("tests", "lint"),
        )
        self.assertEqual((graded.event, graded.state), ("fail", "failed"))
        self.assertFalse(graded.continues)
        assert graded.summary is not None
        self.assertIn("2 of 2 verification check(s) failed", graded.summary)
        self.assertIn("tests (exit-status)", graded.summary)

    def test_a_deferral_continues_the_run_without_claiming_the_gates_passed(
        self,
    ) -> None:
        failures = (_check("broad:tests"),)
        deferral = verification_transitions.broad_failure_acceptance(
            failures, {"ok": True, "outcome": review.REVIEW_OUTCOME_FINDINGS}
        )
        assert deferral is not None
        graded = verification_transitions.adjudicate_verification(
            result=_result(*failures),
            contract=_contract("broad:tests"),
            deferral=deferral,
        )
        self.assertEqual((graded.event, graded.state), ("defer", "deferred"))
        self.assertTrue(graded.continues)
        self.assertFalse(graded.passed)
        self.assertEqual(graded.reason, verification_cli.VERIFY_EXIT_REASON_FAILED)
        turn = graded.turn_verification("1")
        self.assertIs(turn["passed"], False)
        self.assertEqual(turn["acceptance"]["deferred_failure_names"], ["broad:tests"])
        self.assertEqual(len(turn["deferred_failures"]), 1)

    def test_a_passing_recipe_can_never_record_a_deferral(self) -> None:
        deferral = verification_transitions.BroadFailureDeferral(
            acceptance={"accepted": True}, deferred_failures=[]
        )
        graded = verification_transitions.adjudicate_verification(
            result=_result(
                _check("tests", status="pass", failure_reason=None, returncode=0)
            ),
            contract=_contract("tests"),
            deferral=deferral,
        )
        self.assertEqual(graded.state, "passed")
        self.assertIsNone(graded.deferral)
        self.assertNotIn("acceptance", graded.turn_verification("1"))

    def test_every_adjudication_event_is_declared_by_the_machine(self) -> None:
        for graded_state, graded in (
            (
                "passed",
                verification_transitions.adjudicate_verification(
                    result=_result(
                        _check(
                            "tests", status="pass", failure_reason=None, returncode=0
                        )
                    ),
                    contract=_contract("tests"),
                ),
            ),
            (
                "failed",
                verification_transitions.adjudicate_verification(
                    result=_result(_check("tests")), contract=_contract("tests")
                ),
            ),
        ):
            with self.subTest(state=graded_state):
                self.assertEqual(MACHINE.advance("running", graded.event), graded_state)


class BroadFailureDeferralTests(unittest.TestCase):
    def test_an_artifact_validator_failure_is_never_deferrable(self) -> None:
        self.assertIsNone(
            verification_transitions.broad_failures_only(
                _result(_check("broad:tests"), _check("review-artifact"))
            )
        )

    def test_a_clean_result_has_nothing_to_defer(self) -> None:
        self.assertIsNone(
            verification_transitions.broad_failures_only(
                _result(
                    _check(
                        "broad:tests", status="pass", failure_reason=None, returncode=0
                    )
                )
            )
        )

    def test_only_a_valid_findings_verdict_earns_the_delay(self) -> None:
        failures = (_check("broad:tests"),)
        for verdict in (
            {"ok": False, "outcome": review.REVIEW_OUTCOME_FINDINGS},
            {"ok": True, "outcome": review.REVIEW_OUTCOME_ALL_CLEAR},
            {},
        ):
            with self.subTest(verdict=verdict):
                self.assertIsNone(
                    verification_transitions.broad_failure_acceptance(failures, verdict)
                )

    def test_the_two_projections_differ_in_size(self) -> None:
        deferral = verification_transitions.broad_failure_acceptance(
            (_check("broad:tests"),),
            {"ok": True, "outcome": review.REVIEW_OUTCOME_FINDINGS},
        )
        assert deferral is not None
        self.assertEqual(
            deferral.acceptance,
            {
                "accepted": True,
                "reason": DEFERRED_REASON,
                "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                "deferred_failure_count": 1,
                "deferred_failure_names": ["broad:tests"],
            },
        )
        self.assertEqual(deferral.deferred_failures[0]["name"], "broad:tests")
        self.assertIn("stdout_sha256", deferral.deferred_failures[0])


if __name__ == "__main__":
    unittest.main()
