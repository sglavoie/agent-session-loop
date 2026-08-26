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
rather than in a durable record.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    contracts,
    events,
    plans,
    review,
    review_continuation,
    verification,
    verification_transitions,
)
from agent_session_loop.transitions import IllegalTransitionError

MACHINE = verification_transitions.VERIFICATION_MACHINE
DEFERRED_REASON = verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON


def _check(
    name: str,
    *,
    status: str = "fail",
    kind: str = "command",
    failure_reason: str | None = "exit-status",
    returncode: int | None = 1,
    stdout_sha256: str | None = "aa",
    stderr_sha256: str | None = "bb",
) -> verification.CheckResult:
    return verification.CheckResult(
        name=name,
        kind=kind,
        status=status,
        returncode=returncode,
        duration_seconds=0.0,
        failure_reason=failure_reason,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
    )


def _result(
    *checks: verification.CheckResult,
    session_id: str = "1",
) -> verification.VerificationResult:
    return verification.VerificationResult(
        session_id=plans.SessionId.parse(session_id),
        checks=list(checks),
        skipped_checks=[],
    )


def _contract(*names: str) -> contracts.CompletionContract:
    return contracts.CompletionContract(
        session_id=plans.SessionId.parse("1"),
        done_when=("done",),
        verify=contracts.VerifyRecipe(
            checks=tuple(
                contracts.CommandCheck(name=name, argv=("true",), timeout_seconds=1)
                for name in names
            ),
        ),
    )


class VerificationMachineModelTests(unittest.TestCase):
    """The declared lifecycle, walked exhaustively in both directions."""

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
        self.assertEqual(refusal.reason, verification.VERIFY_EXIT_REASON_FAILED)
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
        self.assertEqual(graded.reason, verification.VERIFY_EXIT_REASON_FAILED)
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


class VerificationFingerprintTests(unittest.TestCase):
    def _details(self, **overrides: Any) -> dict[str, Any]:
        failure = {
            "name": "tests",
            "kind": "command",
            "failure_reason": "exit-status",
            "returncode": 1,
            "stdout_sha256": "aa",
            "stderr_sha256": "bb",
        }
        failure.update(overrides)
        return {"failures": [failure]}

    def test_an_identical_failure_folds_to_the_same_slug(self) -> None:
        first = verification_transitions.verification_fingerprint(self._details())
        second = verification_transitions.verification_fingerprint(self._details())
        assert first is not None
        self.assertEqual(first, second)
        self.assertTrue(
            first.startswith(verification_transitions.VERIFICATION_FINGERPRINT_PREFIX)
        )

    def test_changed_output_yields_a_different_slug(self) -> None:
        self.assertNotEqual(
            verification_transitions.verification_fingerprint(self._details()),
            verification_transitions.verification_fingerprint(
                self._details(stdout_sha256="cc")
            ),
        )

    def test_failure_order_does_not_change_the_slug(self) -> None:
        one = {"failures": [self._details()["failures"][0], {"name": "lint"}]}
        other = {"failures": [{"name": "lint"}, self._details()["failures"][0]]}
        self.assertEqual(
            verification_transitions.verification_fingerprint(one),
            verification_transitions.verification_fingerprint(other),
        )

    def test_evidence_free_input_has_no_slug(self) -> None:
        for details in (None, {}, {"failures": "nope"}, {"failures": []}):
            with self.subTest(details=details):
                self.assertIsNone(
                    verification_transitions.verification_fingerprint(details)
                )


class VerificationRepairBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def _pause(self, session: str, *, stdout: str = "aa") -> None:
        self._append(
            events.RUN_PAUSED_EVENT,
            {
                "reason": verification.VERIFY_EXIT_REASON_FAILED,
                "details": {
                    "session": session,
                    "failures": [
                        {
                            "name": "tests",
                            "kind": "command",
                            "failure_reason": "exit-status",
                            "returncode": 1,
                            "stdout_sha256": stdout,
                            "stderr_sha256": "bb",
                            "stdout_artifact_path": "/tmp/out.txt",
                        }
                    ],
                },
            },
        )

    def test_a_missing_log_leaves_the_first_attempt_unbudgeted(self) -> None:
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        self.assertEqual(budget.attempt, 1)
        self.assertEqual(budget.references, ())
        self.assertEqual(budget.prior_fingerprints, ())

    def test_only_this_session_s_verify_failures_are_counted(self) -> None:
        self._pause("1")
        self._pause("2")
        self._append(
            events.RUN_PAUSED_EVENT,
            {"reason": "no-contract", "details": {"session": "1"}},
        )
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        self.assertEqual(budget.attempt, 2)
        self.assertEqual(len(budget.prior_fingerprints), 1)

    def test_references_name_the_failure_the_next_turn_must_fix(self) -> None:
        self._pause("1")
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        joined = "\n".join(budget.references)
        self.assertIn("Prior verification attempt 1 failed", joined)
        self.assertIn("Failed check `tests` (command): exit-status.", joined)
        self.assertIn("Full stdout for `tests` retained at /tmp/out.txt.", joined)
        self.assertIn("do not re-submit the same failing result unchanged", joined)

    def test_an_identical_failure_exhausts_the_loop_under_the_cap(self) -> None:
        self._pause("1")
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        repair = verification_transitions.adjudicate_verification_repair(
            details={
                "failures": [
                    {
                        "name": "tests",
                        "kind": "command",
                        "failure_reason": "exit-status",
                        "returncode": 1,
                        "stdout_sha256": "aa",
                        "stderr_sha256": "bb",
                    }
                ]
            },
            budget=budget,
        )
        self.assertEqual(repair.attempt, 2)
        self.assertTrue(repair.exhausted)

    def test_a_changed_failure_under_the_cap_stays_eligible(self) -> None:
        self._pause("1", stdout="aa")
        repair = verification_transitions.adjudicate_verification_repair(
            details={
                "failures": [
                    {
                        "name": "tests",
                        "kind": "command",
                        "failure_reason": "exit-status",
                        "returncode": 1,
                        "stdout_sha256": "cc",
                        "stderr_sha256": "bb",
                    }
                ]
            },
            budget=verification_transitions.verification_repair_budget(
                self.event_path, "1"
            ),
        )
        self.assertFalse(repair.exhausted)
        self.assertEqual(
            repair.to_payload(),
            {
                "verification_fingerprint": repair.fingerprint,
                "verification_repair_attempt": 2,
                "verification_repair_exhausted": False,
                "verification_repair_max_attempts": (
                    verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS
                ),
            },
        )

    def test_the_attempt_cap_stops_a_reviewer_producing_new_failures(self) -> None:
        self._pause("1", stdout="aa")
        self._pause("1", stdout="cc")
        repair = verification_transitions.adjudicate_verification_repair(
            details={
                "failures": [
                    {
                        "name": "tests",
                        "kind": "command",
                        "failure_reason": "exit-status",
                        "returncode": 1,
                        "stdout_sha256": "dd",
                        "stderr_sha256": "bb",
                    }
                ]
            },
            budget=verification_transitions.verification_repair_budget(
                self.event_path, "1"
            ),
        )
        self.assertEqual(repair.attempt, 3)
        self.assertTrue(repair.exhausted)

    def test_a_failure_without_evidence_is_bounded_by_the_cap_alone(self) -> None:
        self.assertFalse(
            verification_transitions.is_verification_repair_exhausted(None, ("x",), 1)
        )
        self.assertTrue(
            verification_transitions.is_verification_repair_exhausted(
                None,
                (),
                verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS + 1,
            )
        )


class TerminalRequiredCheckTests(unittest.TestCase):
    """Session 2's success policy, asked of payloads rather than of runs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def _acceptance(self, *names: str) -> dict[str, Any]:
        return {
            "accepted": True,
            "reason": DEFERRED_REASON,
            "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
            "deferred_failure_count": len(names),
            "deferred_failure_names": list(names),
        }

    def _defer(self, session: str, *names: str) -> None:
        self._append(
            events.VERIFICATION_FINISHED_EVENT,
            {
                "run_id": "r1",
                "session_id": session,
                "turn_index": 1,
                "passed": False,
                "acceptance": self._acceptance(*names),
            },
        )

    def test_a_clean_run_is_not_blocked(self) -> None:
        self.assertIsNone(
            verification_transitions.terminal_blocker({"turns": []}, self.event_path)
        )

    def test_an_unredeemed_deferral_blocks_a_terminal_ending(self) -> None:
        self._defer("1", "broad:tests")
        blocker = verification_transitions.terminal_blocker(
            {"reason": "all-complete", "turns": []}, self.event_path
        )
        assert blocker is not None
        self.assertEqual(
            blocker.reason, verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON
        )
        self.assertIn("broad:tests", blocker.message)
        self.assertEqual(len(blocker.details["deferred_unresolved"]), 1)

    def test_a_later_all_clear_review_redeems_the_deferral(self) -> None:
        self._defer("1", "broad:tests")
        self._append(
            events.REVIEW_CYCLE_EVENT,
            {
                "cycle": 1,
                "review_session_id": "1",
                "outcome": review.REVIEW_OUTCOME_FINDINGS,
                "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            },
        )
        self._append(
            events.REVIEW_CYCLE_EVENT,
            {
                "cycle": 2,
                "review_session_id": "2",
                "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                "total_findings": 0,
                "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            },
        )
        self.assertEqual(
            verification_transitions.unresolved_deferred_verifications(
                {"turns": []}, self.event_path
            ),
            [],
        )

    def test_a_malformed_acceptance_is_not_evidence_of_a_deferral(self) -> None:
        for acceptance in (
            "nope",
            {},
            {"accepted": True, "deferred_failure_names": []},
            {
                "accepted": True,
                "reason": DEFERRED_REASON,
                "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                "deferred_failure_count": 2,
                "deferred_failure_names": ["broad:tests"],
            },
            {
                "accepted": True,
                "reason": DEFERRED_REASON,
                "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                "deferred_failure_count": 1,
                "deferred_failure_names": ["tests"],
            },
        ):
            with self.subTest(acceptance=acceptance):
                self.assertIsNone(
                    verification_transitions.accepted_broad_failure_deferral(
                        {"acceptance": acceptance}
                    )
                )

    def test_an_unpassed_turn_verification_blocks_even_without_a_pause(self) -> None:
        blocker = verification_transitions.terminal_blocker(
            {
                "reason": "all-complete",
                "turns": [
                    {
                        "turn_index": 1,
                        "verification": {
                            "session": "1",
                            "passed": False,
                            "reason": "verify-failed",
                        },
                    }
                ],
            },
            self.event_path,
        )
        assert blocker is not None
        self.assertEqual(
            blocker.reason, verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON
        )
        self.assertEqual(
            blocker.details["unpassed_verifications"],
            [{"session_id": "1", "turn_index": 1, "reason": "verify-failed"}],
        )

    def test_a_deferred_turn_verification_is_not_an_unpassed_one(self) -> None:
        self.assertEqual(
            verification_transitions.unpassed_run_verifications(
                {
                    "turns": [
                        {
                            "turn_index": 1,
                            "verification": {
                                "session": "1",
                                "passed": False,
                                "acceptance": self._acceptance("broad:tests"),
                            },
                        }
                    ]
                }
            ),
            [],
        )

    def test_required_checks_are_asked_about_before_the_review(self) -> None:
        # A run whose gates never passed is not made honest by a clear review,
        # so the broader claim is the one reported.
        self._defer("1", "broad:tests")
        blocker = verification_transitions.terminal_blocker(
            {
                "reason": "all-complete",
                "review_requested": True,
                "review_fix_disposition": "findings",
                "turns": [],
            },
            self.event_path,
        )
        assert blocker is not None
        self.assertEqual(
            blocker.reason, verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON
        )

    def test_the_review_clause_is_scoped_to_plan_completion_endings(self) -> None:
        payload = {
            "review_requested": True,
            "review_fix_disposition": "findings",
            "review_verdict": {
                "outcome": review.REVIEW_OUTCOME_FINDINGS,
                "total_findings": 2,
            },
            "turns": [],
        }
        for reason, blocked in (
            ("all-complete", True),
            ("stopped", False),
            ("max-sessions", False),
            ("", False),
        ):
            with self.subTest(reason=reason):
                blocker = verification_transitions.terminal_blocker(
                    {**payload, "reason": reason}, self.event_path
                )
                self.assertEqual(blocker is not None, blocked)
                if blocker is not None:
                    self.assertEqual(
                        blocker.reason, verification_transitions.REVIEW_NOT_CLEAR_REASON
                    )
                    self.assertEqual(blocker.details["unresolved_findings"], 2)

    def test_a_cleared_review_needs_all_three_facts(self) -> None:
        clear = {
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            ),
            "review_verdict": {
                "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                "total_findings": 0,
            },
        }
        self.assertTrue(verification_transitions.final_review_is_clear(clear))
        for override in (
            {"review_fix_disposition": "findings"},
            {"review_verdict": {"outcome": "findings", "total_findings": 0}},
            {
                "review_verdict": {
                    "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                    "total_findings": 1,
                }
            },
            {"review_verdict": None},
        ):
            with self.subTest(override=override):
                self.assertFalse(
                    verification_transitions.final_review_is_clear(
                        {**clear, **override}
                    )
                )

    def test_a_run_that_never_asked_for_a_review_is_not_blocked_by_one(self) -> None:
        self.assertIsNone(
            verification_transitions.terminal_blocker(
                {"reason": "all-complete", "turns": []}, self.event_path
            )
        )

    def test_recorded_cycles_fall_back_to_the_live_projection(self) -> None:
        self.assertEqual(
            verification_transitions.recorded_review_cycles(
                {"review_cycles": [{"cycle": 1}, "junk"]}, self.event_path
            ),
            [{"cycle": 1}],
        )


class EventLogReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def test_an_absent_log_reads_as_empty(self) -> None:
        self.assertEqual(
            verification_transitions.read_event_records(self.event_path), []
        )

    def test_malformed_lines_are_skipped_not_fatal(self) -> None:
        self.event_path.write_text(
            "\n".join(
                [
                    "",
                    "{not json",
                    "[1, 2]",
                    json.dumps(
                        {
                            "name": events.VERIFICATION_FINISHED_EVENT,
                            "payload": {"a": 1},
                        }
                    ),
                    json.dumps(
                        {"name": events.VERIFICATION_FINISHED_EVENT, "payload": "no"}
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            verification_transitions.read_verification_finished_events(self.event_path),
            [{"a": 1}],
        )


if __name__ == "__main__":
    unittest.main()
