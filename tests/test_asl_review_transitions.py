"""The review verdict and repair/rereview lanes as models.

Three decisions used to be ordered branches inside the run loop: whether a
submitted verdict is accepted, retried, or has spent its budget; what one
completed review cycle settles on; and what repair materialization produced.
Each is now a function of its inputs, so each is tested as one — the runner's
effects (revert, pause, durable append) are not reached from here at all.

The clause *order* inside :func:`classify_review_cycle` is itself policy, so it
is asserted directly: a repeat that arrives on the same cycle as an exhausted
cap must be reported as a repeat, and the table-driven model tests below make an
undeclared step raise rather than silently record a lifecycle the plan never
had.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    review,
    review_continuation,
    review_repair,
    review_transitions,
)
from agent_session_loop.transitions import IllegalTransitionError

VERDICT_MACHINE = review_transitions.REVIEW_VERDICT_MACHINE
CYCLE_MACHINE = review_transitions.REVIEW_CYCLE_MACHINE


def _delta(
    current: tuple[str, ...] | None = (),
    previous: tuple[str, ...] = (),
    *,
    current_keys: tuple[str, ...] | None = None,
    previous_keys: tuple[str, ...] | None = None,
) -> review_transitions.FindingDelta:
    return review_transitions.diff_review_findings(
        fingerprints=current,
        repeat_keys=current if current_keys is None else current_keys,
        previous_fingerprints=previous,
        previous_repeat_keys=previous if previous_keys is None else previous_keys,
    )


@dataclass(frozen=True)
class _Group:
    """The one attribute the exhaustion check reads off a planned group."""

    repair_group: str


class ReviewVerdictMachineModelTests(unittest.TestCase):
    def test_every_declared_transition_advances_to_its_target(self) -> None:
        for state, event, target in VERDICT_MACHINE.legal_transitions():
            with self.subTest(state=state, event=event):
                self.assertEqual(VERDICT_MACHINE.advance(state, event), target)

    def test_every_undeclared_pair_is_refused(self) -> None:
        for state, event in VERDICT_MACHINE.illegal_transitions():
            with (
                self.subTest(state=state, event=event),
                self.assertRaises(IllegalTransitionError),
            ):
                VERDICT_MACHINE.advance(state, event)

    def test_a_retry_returns_to_submitted_for_the_same_grading(self) -> None:
        cursor = VERDICT_MACHINE.start()
        cursor.advance("retry")
        cursor.advance("resubmit")
        self.assertEqual(cursor.state, "submitted")
        self.assertEqual(cursor.history, ("submitted", "retrying", "submitted"))
        self.assertFalse(cursor.settled)

    def test_an_accepted_verdict_cannot_be_retried(self) -> None:
        cursor = VERDICT_MACHINE.start()
        cursor.advance("accept")
        self.assertTrue(cursor.settled)
        with self.assertRaises(IllegalTransitionError):
            cursor.advance("retry")


class ReviewCycleMachineModelTests(unittest.TestCase):
    def test_every_declared_transition_advances_to_its_target(self) -> None:
        for state, event, target in CYCLE_MACHINE.legal_transitions():
            with self.subTest(state=state, event=event):
                self.assertEqual(CYCLE_MACHINE.advance(state, event), target)

    def test_every_undeclared_pair_is_refused(self) -> None:
        for state, event in CYCLE_MACHINE.illegal_transitions():
            with (
                self.subTest(state=state, event=event),
                self.assertRaises(IllegalTransitionError),
            ):
                CYCLE_MACHINE.advance(state, event)

    def test_settling_is_reachable_from_materializing(self) -> None:
        # An artifact re-read that finds nothing left to repair is an all-clear,
        # not a refusal.
        cursor = CYCLE_MACHINE.start()
        cursor.advance("materialize")
        cursor.advance("settle")
        self.assertEqual(cursor.state, "settled")

    def test_a_settled_cycle_cannot_materialize_repairs(self) -> None:
        cursor = CYCLE_MACHINE.start()
        cursor.advance("settle")
        with self.assertRaises(IllegalTransitionError) as raised:
            cursor.advance("materialize")
        self.assertEqual(raised.exception.machine, "review-cycle")


class ReviewVerdictFingerprintTests(unittest.TestCase):
    def test_the_same_rejection_folds_to_the_same_slug(self) -> None:
        verdict = {"reason": "invalid-artifact", "error": "missing field"}
        slug = review_transitions.review_verdict_fingerprint(verdict)
        assert slug is not None
        self.assertTrue(
            slug.startswith(review_transitions.REVIEW_VERDICT_FINGERPRINT_PREFIX)
        )
        self.assertEqual(
            slug, review_transitions.review_verdict_fingerprint(dict(verdict))
        )

    def test_a_different_validation_error_stays_eligible(self) -> None:
        self.assertNotEqual(
            review_transitions.review_verdict_fingerprint(
                {"reason": "invalid-artifact", "error": "missing field"}
            ),
            review_transitions.review_verdict_fingerprint(
                {"reason": "invalid-artifact", "error": "bad severity"}
            ),
        )

    def test_a_verdict_with_neither_reason_nor_error_has_no_slug(self) -> None:
        self.assertIsNone(review_transitions.review_verdict_fingerprint({}))
        self.assertIsNone(
            review_transitions.review_verdict_fingerprint({"reason": "", "error": None})
        )


class AdjudicateReviewVerdictTests(unittest.TestCase):
    def _rejection(self, error: str = "missing field") -> dict[str, Any]:
        return {"ok": False, "reason": "invalid-artifact", "error": error}

    def test_a_valid_verdict_is_accepted_with_no_repair_bookkeeping(self) -> None:
        graded = review_transitions.adjudicate_review_verdict(
            verdict={"ok": True, "outcome": review.REVIEW_OUTCOME_ALL_CLEAR},
            priors=[],
        )
        self.assertEqual(
            (graded.event, graded.state, graded.action), ("accept", "accepted", "pass")
        )
        self.assertTrue(graded.accepted)
        self.assertIsNone(graded.fingerprint)

    def test_a_first_rejection_earns_one_retry(self) -> None:
        graded = review_transitions.adjudicate_review_verdict(
            verdict=self._rejection(), priors=[]
        )
        self.assertEqual(
            (graded.event, graded.state, graded.action), ("retry", "retrying", "retry")
        )
        self.assertEqual(graded.attempt, 1)
        self.assertFalse(graded.exhausted)
        self.assertEqual(
            graded.to_payload(),
            {
                "review_verdict_fingerprint": graded.fingerprint,
                "review_verdict_repair_attempt": 1,
                "review_verdict_repair_exhausted": False,
                "review_verdict_repair_max_attempts": (
                    review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS
                ),
            },
        )

    def test_an_identical_rejection_exhausts_the_budget(self) -> None:
        first = review_transitions.adjudicate_review_verdict(
            verdict=self._rejection(), priors=[]
        )
        graded = review_transitions.adjudicate_review_verdict(
            verdict=self._rejection(),
            priors=[{"fingerprint": first.fingerprint}],
        )
        self.assertEqual((graded.event, graded.action), ("exhaust", "pause"))
        self.assertTrue(graded.exhausted)
        self.assertEqual(graded.attempt, 2)

    def test_the_attempt_cap_bounds_a_reviewer_producing_new_errors(self) -> None:
        graded = review_transitions.adjudicate_review_verdict(
            verdict=self._rejection("third"),
            priors=[{"fingerprint": "a"}, {"fingerprint": "b"}],
        )
        self.assertEqual(graded.attempt, 3)
        self.assertTrue(graded.exhausted)
        self.assertEqual(graded.state, "exhausted")

    def test_an_operator_stop_declines_an_otherwise_eligible_retry(self) -> None:
        graded = review_transitions.adjudicate_review_verdict(
            verdict=self._rejection(), priors=[], stop_after_current_pending=True
        )
        self.assertEqual(
            (graded.event, graded.state, graded.action),
            ("decline", "declined", "pause"),
        )
        self.assertFalse(graded.exhausted)
        self.assertEqual(
            graded.to_payload()["review_verdict_repair_declined"],
            review_transitions.REVIEW_VERDICT_DECLINED_STOP_AFTER_CURRENT,
        )

    def test_exhaustion_wins_over_a_pending_stop(self) -> None:
        # Both pause, but the durable record must say which limit was reached.
        graded = review_transitions.adjudicate_review_verdict(
            verdict=self._rejection(),
            priors=[{"fingerprint": "a"}, {"fingerprint": "b"}],
            stop_after_current_pending=True,
        )
        self.assertEqual(graded.state, "exhausted")
        self.assertNotIn("review_verdict_repair_declined", graded.to_payload())

    def test_every_grading_event_is_declared_by_the_machine(self) -> None:
        cases = [
            ({"ok": True}, [], False),
            (self._rejection(), [], False),
            (self._rejection(), [], True),
            (self._rejection(), [{"fingerprint": "a"}, {"fingerprint": "b"}], False),
        ]
        for verdict, priors, stopping in cases:
            with self.subTest(priors=len(priors), stopping=stopping):
                graded = review_transitions.adjudicate_review_verdict(
                    verdict=verdict,
                    priors=priors,
                    stop_after_current_pending=stopping,
                )
                self.assertEqual(
                    VERDICT_MACHINE.advance("submitted", graded.event), graded.state
                )


class ReviewVerdictRetryHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def _append(self, name: str, payload: Any) -> None:
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def _retry(self, session: str, attempt: int, **extra: Any) -> None:
        self._append(
            events.REVIEW_VERDICT_RETRY_EVENT,
            {
                "session": session,
                "attempt": attempt,
                "fingerprint": f"fp-{attempt}",
                **extra,
            },
        )

    def test_an_absent_log_reads_as_no_history(self) -> None:
        self.assertEqual(
            review_transitions.read_prior_review_verdict_retries(self.event_path, "1"),
            [],
        )

    def test_a_reopened_cycle_starts_with_a_fresh_budget(self) -> None:
        self._retry("1", 1)
        self._retry("1", 2)
        self._append(events.REVIEW_CYCLE_EVENT, {"cycle": 1})
        self._retry("1", 1)
        priors = review_transitions.read_prior_review_verdict_retries(
            self.event_path, "1"
        )
        self.assertEqual([prior["attempt"] for prior in priors], [1])

    def test_only_this_session_s_retries_are_counted(self) -> None:
        self._retry("1", 1)
        self._retry("2", 1)
        self.assertEqual(
            len(
                review_transitions.read_prior_review_verdict_retries(
                    self.event_path, "1"
                )
            ),
            1,
        )

    def test_references_hand_the_next_turn_the_exact_error(self) -> None:
        self._retry("1", 1, failure_reason="invalid-artifact", error="missing severity")
        references = review_transitions.review_verdict_repair_references(
            self.event_path, "1"
        )
        joined = "\n".join(references)
        self.assertIn("Prior review verdict attempt 1 was rejected", joined)
        self.assertIn("Rejection reason: invalid-artifact.", joined)
        self.assertIn("Validation error: missing severity", joined)
        self.assertIn(review.FINAL_REVIEW_FILENAME, joined)
        self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, joined)

    def test_no_rejection_in_this_cycle_yields_no_references(self) -> None:
        self.assertEqual(
            review_transitions.review_verdict_repair_references(self.event_path, "1"),
            (),
        )


class FindingDeltaTests(unittest.TestCase):
    def test_a_first_cycle_introduces_everything_and_closes_nothing(self) -> None:
        delta = _delta(("a", "b"))
        self.assertEqual(delta.introduced, ("a", "b"))
        self.assertEqual(delta.repeated, ())
        self.assertEqual(delta.closed, ())

    def test_a_surviving_fingerprint_is_repeated_not_introduced(self) -> None:
        delta = _delta(("a", "c"), ("a", "b"))
        self.assertEqual(delta.repeated, ("a",))
        self.assertEqual(delta.introduced, ("c",))
        self.assertEqual(delta.closed, ("b",))

    def test_a_moved_evidence_path_is_still_the_same_open_finding(self) -> None:
        # The fingerprint changed but the drift-tolerant repeat key did not, so
        # the defect has not been closed by relocating the file it cites.
        delta = _delta(
            ("a2",),
            ("a1",),
            current_keys=("k",),
            previous_keys=("k",),
        )
        self.assertEqual(delta.repeated, ("a2",))
        self.assertEqual(delta.repeated_keys, ("k",))
        self.assertEqual(delta.closed, ())

    def test_a_legacy_markdown_verdict_compares_as_empty(self) -> None:
        delta = _delta(None, ("a",))
        self.assertEqual(delta.current, ())
        self.assertEqual(delta.closed, ("a",))


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


if __name__ == "__main__":
    unittest.main()
