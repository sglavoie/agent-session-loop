# mypy: ignore-errors
from .asl_review_transitions_support import *
from .asl_review_transitions_support import _delta, _Group


class ClassifyReviewCycleTests(unittest.TestCase):
    def _classify(
        self,
        *,
        outcome: Any = review.REVIEW_OUTCOME_FINDINGS,
        structured: bool = True,
        delta: review_transitions.FindingDelta | None = None,
        max_review_fix_cycles: int = 3,
        repairs_materialized: int = 0,
    ) -> review_transitions.ReviewCycleTransition:
        return review_transitions.classify_review_cycle(
            outcome=outcome,
            structured=structured,
            delta=_delta(("a",)) if delta is None else delta,
            max_review_fix_cycles=max_review_fix_cycles,
            repairs_materialized=repairs_materialized,
        )

    def test_every_disposition_the_table_can_settle_on(self) -> None:
        cases = [
            (
                "all-clear",
                {"outcome": review.REVIEW_OUTCOME_ALL_CLEAR},
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            ),
            (
                "markdown-only",
                {"structured": False},
                review_continuation.REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
            ),
            (
                "repeat",
                {"delta": _delta(("a",), ("a",))},
                review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
            ),
            (
                "cycles-disabled",
                {"max_review_fix_cycles": 0},
                review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
            ),
            (
                "cap-spent",
                {"max_review_fix_cycles": 2, "repairs_materialized": 2},
                review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            ),
        ]
        for label, kwargs, disposition in cases:
            with self.subTest(label=label):
                transition = self._classify(**kwargs)
                self.assertTrue(transition.settles)
                self.assertEqual(transition.action, "complete")
                self.assertEqual(transition.disposition, disposition)
                self.assertEqual(
                    CYCLE_MACHINE.advance("reviewed", transition.event),
                    transition.state,
                )

    def test_open_findings_under_budget_earn_repairs(self) -> None:
        transition = self._classify()
        self.assertEqual(
            (transition.event, transition.state, transition.action),
            ("materialize", "materializing", "materialize"),
        )
        self.assertIsNone(transition.disposition)

    def test_a_repeat_is_reported_as_a_repeat_not_as_a_spent_cap(self) -> None:
        transition = self._classify(
            delta=_delta(("a",), ("a",)),
            max_review_fix_cycles=1,
            repairs_materialized=1,
        )
        self.assertEqual(
            transition.disposition,
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
        )

    def test_an_all_clear_verdict_settles_before_any_budget_is_consulted(self) -> None:
        transition = self._classify(
            outcome=review.REVIEW_OUTCOME_ALL_CLEAR,
            structured=False,
            delta=_delta(("a",), ("a",)),
            max_review_fix_cycles=0,
        )
        self.assertEqual(
            transition.disposition,
            review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
        )

    def test_every_settling_disposition_is_declared(self) -> None:
        self.assertEqual(
            set(review_transitions.SETTLING_DISPOSITIONS),
            {
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
                review_continuation.REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
                review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
                review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
                review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
                review_continuation.REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
            },
        )


class RepairGroupBudgetTests(unittest.TestCase):
    def test_the_first_spent_group_stops_the_cycle(self) -> None:
        group = review_transitions.first_exhausted_repair_group(
            repair_groups=[_Group("alpha"), _Group("beta")],
            historical_attempts={"beta": 2},
            max_repair_group_attempts=2,
        )
        assert group is not None
        self.assertEqual(group.repair_group, "beta")

    def test_groups_with_budget_left_do_not_stop_the_cycle(self) -> None:
        self.assertIsNone(
            review_transitions.first_exhausted_repair_group(
                repair_groups=[_Group("alpha")],
                historical_attempts={"alpha": 1},
                max_repair_group_attempts=2,
            )
        )

    def test_an_unseen_group_has_spent_nothing(self) -> None:
        self.assertIsNone(
            review_transitions.first_exhausted_repair_group(
                repair_groups=[_Group("alpha")],
                historical_attempts={},
                max_repair_group_attempts=1,
            )
        )

    def test_the_settlement_it_earns_is_declared_by_the_machine(self) -> None:
        transition = review_transitions.settle_group_exhausted()
        self.assertEqual(
            transition.disposition,
            review_continuation.REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
        )
        self.assertEqual(
            CYCLE_MACHINE.advance("reviewed", transition.event), transition.state
        )


class ClassifyMaterializationTests(unittest.TestCase):
    def _classify(
        self,
        *,
        ok: bool = True,
        reason: str | None = review_repair.REVIEW_REPAIR_REASON_OK,
        non_bypassable_blockers: tuple[str, ...] = (),
        allow_not_loop_ready: bool = False,
    ) -> review_transitions.ReviewCycleTransition:
        return review_transitions.classify_materialization(
            ok=ok,
            reason=reason,
            non_bypassable_blockers=non_bypassable_blockers,
            allow_not_loop_ready=allow_not_loop_ready,
        )

    def test_authored_children_reopen_the_cycle(self) -> None:
        transition = self._classify()
        self.assertEqual(
            (transition.event, transition.state, transition.action),
            ("reopen", "reopened", "continue"),
        )
        self.assertEqual(
            transition.disposition,
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        )

    def test_malformed_repair_output_refuses_and_pauses(self) -> None:
        transition = self._classify(
            ok=False, reason=review_repair.REVIEW_REPAIR_REASON_MALFORMED
        )
        self.assertEqual((transition.state, transition.action), ("refused", "paused"))
        self.assertEqual(
            transition.pause_reason, events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR
        )

    def test_an_artifact_reread_with_no_findings_is_an_all_clear(self) -> None:
        transition = self._classify(
            reason=review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS
        )
        self.assertTrue(transition.settles)
        self.assertEqual(
            transition.disposition,
            review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
        )

    def test_not_loop_ready_refuses_only_when_the_run_did_not_accept_it(self) -> None:
        cases = [
            ((), False, "paused"),
            ((), True, "continue"),
            (("dirty-worktree",), True, "paused"),
            (("dirty-worktree",), False, "paused"),
        ]
        for blockers, allowed, action in cases:
            with self.subTest(blockers=blockers, allowed=allowed):
                transition = self._classify(
                    reason=review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY,
                    non_bypassable_blockers=blockers,
                    allow_not_loop_ready=allowed,
                )
                self.assertEqual(transition.action, action)
                if action == "paused":
                    self.assertEqual(
                        transition.pause_reason,
                        review_continuation.REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
                    )

    def test_every_materialization_event_is_declared_by_the_machine(self) -> None:
        cases = [
            {"ok": False},
            {"reason": review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS},
            {"reason": review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY},
            {},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                transition = self._classify(**kwargs)
                self.assertEqual(
                    CYCLE_MACHINE.advance("materializing", transition.event),
                    transition.state,
                )


class AdjudicateTerminalReviewTests(unittest.TestCase):
    def _cleared(self) -> dict[str, Any]:
        return {
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            ),
            "review_verdict": {
                "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                "total_findings": 0,
            },
        }

    def test_a_cleared_review_earns_the_completion_reason(self) -> None:
        outcome = review_transitions.adjudicate_terminal_review(self._cleared())
        self.assertTrue(outcome.cleared)
        self.assertEqual(outcome.reason, review_transitions.REVIEW_ALL_COMPLETE_REASON)
        self.assertIsNone(outcome.action_required)

    def test_an_exhausted_budget_reports_the_findings_it_failed_to_close(self) -> None:
        outcome = review_transitions.adjudicate_terminal_review(
            {
                "review_fix_disposition": (
                    review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED
                ),
                "review_verdict": {
                    "outcome": review.REVIEW_OUTCOME_FINDINGS,
                    "total_findings": 3,
                    "findings_by_severity": {"major": 3},
                },
            }
        )
        self.assertFalse(outcome.cleared)
        self.assertEqual(
            outcome.reason, review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON
        )
        self.assertEqual(
            outcome.action_required,
            {
                "reason": review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
                "review_fix_disposition": (
                    review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED
                ),
                "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                "unresolved_findings": 3,
                "findings_by_severity": {"major": 3},
            },
        )

    def test_a_missing_verdict_is_not_a_cleared_review(self) -> None:
        outcome = review_transitions.adjudicate_terminal_review(
            {
                "review_fix_disposition": (
                    review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
                )
            }
        )
        self.assertFalse(outcome.cleared)
        self.assertIsNone(outcome.action_required["review_outcome"])

    def test_it_agrees_with_the_terminal_blocker_on_every_payload(self) -> None:
        # One success policy, not two: the review lane and the required-check
        # adjudicator read the same predicate.
        from agent_session_loop import verification_transitions

        payloads = [
            self._cleared(),
            {**self._cleared(), "review_fix_disposition": "findings"},
            {
                **self._cleared(),
                "review_verdict": {
                    "outcome": review.REVIEW_OUTCOME_FINDINGS,
                    "total_findings": 1,
                },
            },
            {},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    review_transitions.adjudicate_terminal_review(payload).cleared,
                    verification_transitions.final_review_is_clear(payload),
                )
