from .asl_execution_timing_support import *


class ReconciliationTests(unittest.TestCase):
    def test_three_records_about_one_attempt_are_one_attempt(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100)],
        )
        self.assertEqual(len(run.attempts), 1)
        attempt = run.attempts[0]
        self.assertEqual(attempt.duration_seconds, 100.0)
        self.assertEqual(
            attempt.duration_source, execution_timing.SOURCE_EXECUTION_FINISHED
        )
        self.assertEqual(
            sorted(attempt.sources),
            sorted(
                [
                    execution_timing.SOURCE_EXECUTION_FINISHED,
                    execution_timing.SOURCE_TURN_FINISHED,
                    execution_timing.SOURCE_RESULT_FILE,
                ]
            ),
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )

    def test_turn_finished_is_the_fallback_when_execution_finished_is_absent(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100)],
        )
        self.assertEqual(
            run.attempts[0].duration_source, execution_timing.SOURCE_TURN_FINISHED
        )
        self.assertEqual(run.attempts[0].duration_seconds, 100.0)

    def test_the_result_file_is_the_last_fallback(self) -> None:
        run = _one(
            [_run_started(), _turn_rejected(turn_index=1), _run_finished()],
            [_result(turn_index=1, started=0, finished=100)],
        )
        self.assertEqual(
            run.attempts[0].duration_source, execution_timing.SOURCE_RESULT_FILE
        )
        self.assertEqual(run.attempts[0].outcome, execution_timing.OUTCOME_REJECTED)
        self.assertEqual(run.attempts[0].duration_seconds, 100.0)

    def test_conflicting_evidence_stays_visible_rather_than_being_resolved(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100, duration=500.0)],
        )
        self.assertIn(
            execution_timing.ANOMALY_CONFLICTING_EVIDENCE, _codes(run.anomalies)
        )
        conflict = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_CONFLICTING_EVIDENCE
        )
        # Both readings are named, so a reader can see which was preferred and
        # what it was preferred over.
        self.assertEqual(conflict.detail["preferred"]["value"], 100.0)
        self.assertEqual(conflict.detail["conflicting"]["value"], 500.0)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )

    def test_agreeing_evidence_within_tolerance_is_not_a_conflict(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100, duration=100.4)],
        )
        self.assertEqual(_codes(run.anomalies), [])

    def test_duplicate_execution_evidence_is_flagged_and_counted_once(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ]
        )
        self.assertEqual(len(run.attempts), 1)
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_duplicate_verification_finishes_do_not_double_count(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=100, finished=140),
                *_verification(
                    turn_index=1, started=100, finished=140, with_start=False
                ),
                _run_finished(),
            ]
        )
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_VERIFICATION], 40.0)
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_a_replayed_check_finish_is_one_check_not_two(self) -> None:
        check = _check_finished(turn_index=1, check_index=0, duration=12.0)
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        *_verification(turn_index=1, started=10, finished=30),
                        check,
                        check,
                        _run_finished(),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.verification_check_count, 1)
        self.assertEqual(run.verification_check_seconds, 12.0)
        work = report.verification_check_work()
        self.assertEqual(work["checks"], 1)
        self.assertEqual(work["seconds"], 12.0)
        # Replay is still visible: the run states one execution twice.
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_conflicting_check_durations_leave_the_run_untrusted(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=10, finished=30),
                _check_finished(turn_index=1, check_index=0, duration=12.0),
                _check_finished(turn_index=1, check_index=0, duration=15.0),
                _run_finished(),
            ]
        )
        self.assertEqual(run.verification_check_count, 1)
        self.assertEqual(run.verification_check_seconds, 12.0)
        conflict = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_CONFLICTING_EVIDENCE
        )
        self.assertEqual(conflict.detail["check_index"], 0)
        self.assertEqual(conflict.detail["values"], [12.0, 15.0])
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )
        self.assertFalse(run.trustworthy)

    def test_checks_of_different_identity_are_separate_executions(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=10, finished=30),
                _check_finished(turn_index=1, check_index=0, duration=12.0),
                _check_finished(turn_index=1, check_index=1, duration=12.0),
                _check_finished(turn_index=2, check_index=0, duration=12.0),
                _run_finished(),
            ]
        )
        self.assertEqual(run.verification_check_count, 3)
        self.assertEqual(run.verification_check_seconds, 36.0)
        self.assertEqual(_codes(run.anomalies), [])

    def test_a_check_without_a_check_index_is_marked_rather_than_multiplied(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(turn_index=1, started=10, finished=30),
                _check_finished(
                    turn_index=1, check_index=0, duration=12.0, omit_check_index=True
                ),
                _check_finished(
                    turn_index=1, check_index=1, duration=12.0, omit_check_index=True
                ),
                _run_finished(),
            ]
        )
        # Two records nothing distinguishes reconcile into one execution, and
        # the gap says so rather than the count claiming certainty.
        self.assertEqual(run.verification_check_count, 1)
        self.assertEqual(run.verification_check_seconds, 12.0)
        self.assertIn(execution_timing.GAP_MISSING_IDENTITY, _codes(run.gaps))
        self.assertNotEqual(
            run.classification, execution_timing.CLASSIFICATION_COMPLETE
        )

    def test_a_retried_turn_counts_both_attempts(self) -> None:
        run = _one(
            [
                _run_started(),
                _turn_started(turn_index=1, started=0),
                _execution_finished(turn_index=1, started=0, finished=30),
                _turn_rejected(turn_index=1),
                _turn_started(turn_index=2, started=30),
                _execution_finished(turn_index=2, started=30, finished=120),
                _turn_finished(turn_index=2, started=30, finished=120),
                _run_finished(),
            ],
            [
                _result(turn_index=1, started=0, finished=30),
                _result(turn_index=2, started=30, finished=120),
            ],
        )
        self.assertEqual([a.turn_index for a in run.attempts], [1, 2])
        self.assertEqual(
            [a.outcome for a in run.attempts],
            [execution_timing.OUTCOME_REJECTED, execution_timing.OUTCOME_ACCEPTED],
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 120.0
        )

    def test_the_same_run_id_under_two_plans_stays_two_runs(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    "plan-a", event_records=_clean_run_events()
                ),
                execution_timing.RunEvidence(
                    "plan-b", event_records=_clean_run_events()
                ),
            ]
        )
        self.assertEqual(len(report.runs), 2)
        self.assertEqual({run.plan_key for run in report.runs}, {"plan-a", "plan-b"})
        self.assertEqual(report.known_durations()["wall_seconds"], 500.0)
