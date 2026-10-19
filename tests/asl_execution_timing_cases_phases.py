from .asl_execution_timing_support import *


class PhaseAttributionTests(unittest.TestCase):
    def test_verification_uses_phase_elapsed_not_the_sum_of_parallel_checks(
        self,
    ) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        *_verification(turn_index=1, started=100, finished=140),
                        _check_finished(turn_index=1, check_index=0, duration=30.0),
                        _check_finished(turn_index=1, check_index=1, duration=35.0),
                        _check_finished(turn_index=1, check_index=2, duration=38.0),
                        _run_finished(),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_VERIFICATION], 40.0)
        self.assertEqual(run.verification_check_seconds, 103.0)
        self.assertEqual(run.verification_check_count, 3)
        work = report.verification_check_work()
        self.assertEqual(work["seconds"], 103.0)
        self.assertEqual(work["phase_seconds"], 40.0)
        self.assertGreater(work["seconds"], work["phase_seconds"])

    def test_a_turn_of_unknown_kind_is_not_filed_as_implementation(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(
                    turn_index=1, started=0, finished=100, session_kind=None
                ),
                _run_finished(),
            ],
            [_result(turn_index=1, started=0, finished=100, session_kind=None)],
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 0.0
        )
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_TURN_UNCLASSIFIED], 100.0
        )
        self.assertIn(execution_timing.GAP_TURN_KIND_UNKNOWN, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)

    def test_review_turns_are_attributed_to_review(self) -> None:
        run = _one(_clean_run_events())
        review_attempt = next(a for a in run.attempts if a.turn_index == 2)
        self.assertEqual(review_attempt.phase, execution_timing.PHASE_REVIEW)

    def test_commits_across_repositories_are_separate_occurrences(self) -> None:
        run = _one(
            [
                _run_started(),
                *_commit(commit_index=0, started=140, finished=150),
                *_commit(
                    commit_index=0,
                    started=150,
                    finished=157,
                    repository_key="submodule",
                ),
                _run_finished(),
            ]
        )
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_COMMITS], 17.0)
        self.assertEqual(run.anomalies, ())


class EligibilityTests(unittest.TestCase):
    def test_dry_runs_are_excluded(self) -> None:
        run = _one([_run_started(dry_run=True), _run_finished()])
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_DRY_RUN)
        self.assertTrue(run.dry_run)

    def test_a_paused_run_is_included(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(paused=True),
            ]
        )
        self.assertTrue(run.included)
        self.assertTrue(run.paused)
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_COMPLETE)

    def test_an_active_run_is_excluded_and_never_scored(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        _turn_started(turn_index=1, started=0),
                        _execution_finished(turn_index=1, started=0, finished=100),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_ACTIVE)
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_ACTIVE)
        self.assertIsNone(report.phase_shares()["shares"])
        self.assertEqual(report.known_durations()["seconds"]["worker_execution"], 0.0)

    def test_a_run_whose_start_record_is_absent_is_kept_and_marked(self) -> None:
        run = _one([_run_finished()])
        self.assertTrue(run.included)
        self.assertIsNone(run.dry_run)
        self.assertIn(execution_timing.GAP_RUN_START_ABSENT, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)
