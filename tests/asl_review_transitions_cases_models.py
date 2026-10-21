# mypy: ignore-errors
from .asl_review_transitions_support import *
from .asl_review_transitions_support import _delta


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
