from .asl_execution_timing_support import *


class WindowTests(unittest.TestCase):
    def _windowed(
        self, *, window_start: datetime | None, window_end: datetime | None
    ) -> execution_timing.RunTiming:
        return _one(
            _clean_run_events(), window_start=window_start, window_end=window_end
        )

    def test_the_window_start_is_inclusive(self) -> None:
        run = self._windowed(window_start=_at(250), window_end=None)
        self.assertTrue(run.included)

    def test_the_window_end_is_exclusive(self) -> None:
        run = self._windowed(window_start=None, window_end=_at(250))
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_OUTSIDE_WINDOW)

    def test_a_run_finishing_before_the_window_is_excluded(self) -> None:
        run = self._windowed(window_start=_at(251), window_end=None)
        self.assertFalse(run.included)
        self.assertEqual(run.excluded_reason, execution_timing.EXCLUDED_OUTSIDE_WINDOW)

    def test_a_run_that_began_before_the_window_contributes_its_whole_duration(
        self,
    ) -> None:
        # The run spans [0, 250]; the window opens at 200, well after it began.
        run = self._windowed(window_start=_at(200), window_end=_at(300))
        self.assertTrue(run.included)
        self.assertEqual(run.wall_seconds, 250.0)
        self.assertEqual(run.attributed_seconds, 220.0)

    def test_gaps_between_invocations_are_not_accounted(self) -> None:
        # Two 100-second runs an hour apart: the report totals 200 seconds, not
        # the 3800-second span from the first start to the last finish.
        second = [
            _run_started(run_id=OTHER_RUN_ID),
            _execution_finished(
                turn_index=1, started=3700, finished=3800, run_id=OTHER_RUN_ID
            ),
            _turn_finished(
                turn_index=1, started=3700, finished=3800, run_id=OTHER_RUN_ID
            ),
            _run_finished(
                run_id=OTHER_RUN_ID, started=3700, finished=3800, duration=100.0
            ),
        ]
        first = [
            _run_started(),
            _execution_finished(turn_index=1, started=0, finished=100),
            _turn_finished(turn_index=1, started=0, finished=100),
            _run_finished(started=0, finished=100, duration=100.0),
        ]
        report = _collect(
            [execution_timing.RunEvidence(PLAN, event_records=[*first, *second])]
        )
        self.assertEqual(len(report.included_runs), 2)
        self.assertEqual(report.known_durations()["wall_seconds"], 200.0)
        self.assertEqual(report.phase_shares()["basis_seconds"], 200.0)


class MissingEvidenceTests(unittest.TestCase):
    def test_an_interrupted_turn_leaves_an_unmatched_start(self) -> None:
        run = _one(
            [
                _run_started(),
                _turn_started(turn_index=1, started=0),
                _run_finished(paused=True),
            ]
        )
        self.assertIn(execution_timing.GAP_UNMATCHED_START, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)
        self.assertTrue(run.included)
        self.assertEqual(run.attempts[0].outcome, execution_timing.OUTCOME_STARTED)
        self.assertIsNone(run.attempts[0].duration_seconds)
        # The turn's time is unknown, not zero: it lands in unattributed rather
        # than being counted as no worker time at all.
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 0.0
        )
        self.assertEqual(run.unattributed_seconds, 250.0)

    def test_a_verification_start_without_a_finish_is_a_gap(self) -> None:
        run = _one(
            [
                _run_started(),
                _event(
                    events.VERIFICATION_STARTED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "turn_index": 1,
                        "session": "1",
                        "started_at": _iso(100),
                    },
                    created_at=100,
                ),
                _run_finished(),
            ]
        )
        self.assertIn(execution_timing.GAP_UNMATCHED_START, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)

    def test_a_verification_finish_without_a_start_is_an_anomaly(self) -> None:
        run = _one(
            [
                _run_started(),
                *_verification(
                    turn_index=1, started=100, finished=140, with_start=False
                ),
                _run_finished(),
            ]
        )
        self.assertIn(execution_timing.ANOMALY_UNMATCHED_FINISH, _codes(run.anomalies))
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )
        # The measured duration survives the anomaly.
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_VERIFICATION], 40.0)

    def test_a_finished_phase_nothing_timed_is_a_gap_rather_than_zero(self) -> None:
        for phase, start_name, finish_name, identity in UNTIMED_PHASE_CASES:
            with self.subTest(phase=phase):
                run = _one(
                    [
                        _run_started(),
                        *_untimed_phase(start_name, finish_name, identity),
                        _run_finished(),
                    ]
                )
                gap = next(
                    item
                    for item in run.gaps
                    if item.code == execution_timing.GAP_PHASE_TIMING_UNKNOWN
                )
                self.assertEqual(gap.detail["phase"], phase)
                self.assertEqual(_codes(run.anomalies), [])
                self.assertEqual(
                    run.classification, execution_timing.CLASSIFICATION_PARTIAL
                )
                self.assertFalse(run.trustworthy)
                # No seconds are invented for the phase, and the run that would
                # have carried a fabricated zero into the shares is out of them.
                self.assertEqual(run.phase_seconds[phase], 0.0)

    def test_an_untimed_phase_keeps_a_run_out_of_the_share_basis(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        *_clean_run_events(),
                        *_untimed_phase(
                            events.COMMIT_STARTED_EVENT,
                            events.COMMIT_FINISHED_EVENT,
                            {"commit_index": 9, "repository_key": "root"},
                        ),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertIn(
            execution_timing.GAP_PHASE_TIMING_UNKNOWN,
            _codes(run.gaps),
        )
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)
        self.assertEqual(report.phase_shares()["basis_runs"], 0)
        self.assertIsNone(report.phase_shares()["shares"])
        # Every duration the run does know survives the exclusion.
        self.assertTrue(run.included)
        self.assertEqual(
            dict(run.phase_seconds),
            {
                execution_timing.PHASE_WORKER_EXECUTION: 100.0,
                execution_timing.PHASE_REVIEW: 50.0,
                execution_timing.PHASE_TURN_UNCLASSIFIED: 0.0,
                execution_timing.PHASE_VERIFICATION: 40.0,
                execution_timing.PHASE_COMMITS: 10.0,
                execution_timing.PHASE_ASSURANCE: 20.0,
            },
        )
        self.assertEqual(report.known_durations()["seconds"]["commits"], 10.0)
        self.assertEqual(run.unattributed_seconds, 30.0)

    def test_a_phase_timed_only_by_its_timestamps_is_not_a_gap(self) -> None:
        run = _one(
            [
                _run_started(),
                _event(
                    events.COMMIT_STARTED_EVENT,
                    {"run_id": RUN_ID, "commit_index": 1, "started_at": _iso(140)},
                    created_at=140,
                ),
                _event(
                    events.COMMIT_FINISHED_EVENT,
                    {"run_id": RUN_ID, "commit_index": 1, "finished_at": _iso(150)},
                    created_at=150,
                ),
                _run_finished(),
            ]
        )
        self.assertEqual(_codes(run.gaps), [])
        self.assertEqual(run.phase_seconds[execution_timing.PHASE_COMMITS], 10.0)

    def test_a_record_without_a_turn_index_is_a_missing_identity_gap(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(
                    turn_index=1, started=0, finished=100, omit_turn_index=True
                ),
                _run_finished(),
            ]
        )
        self.assertIn(execution_timing.GAP_MISSING_IDENTITY, _codes(run.gaps))
        self.assertIsNone(run.attempts[0].turn_index)
        self.assertEqual(run.attempts[0].duration_seconds, 100.0)

    def test_an_unknown_wall_duration_is_never_read_as_zero(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _event(
                    events.RUN_FINISHED_EVENT,
                    {"run_id": RUN_ID, "ok": True, "paused": False},
                    created_at=250,
                ),
            ]
        )
        self.assertIsNone(run.wall_seconds)
        self.assertIsNone(run.unattributed_seconds)
        self.assertIn(execution_timing.GAP_WALL_DURATION_UNKNOWN, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)

    def test_an_abandoned_run_is_included_but_marked_reconstructed(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(
                    extra={
                        "synthetic": True,
                        "outcome": events.RUN_OUTCOME_ABANDONED,
                        "abandoned": {"detected_at": _iso(250)},
                    }
                ),
            ]
        )
        self.assertTrue(run.included)
        self.assertEqual(run.outcome, events.RUN_OUTCOME_ABANDONED)
        self.assertIn(execution_timing.GAP_RECONSTRUCTED_TERMINAL, _codes(run.gaps))
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_PARTIAL)
        self.assertFalse(run.trustworthy)
        # Its measured worker time is still real evidence.
        self.assertEqual(
            run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 100.0
        )

    def test_records_the_collector_cannot_place_are_counted(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        {"name": events.RUN_STARTED_EVENT, "payload": {}},
                        {"name": events.RUN_STARTED_EVENT},
                        "not-a-record",  # type: ignore[list-item]
                    ],
                    turn_results=[{"turn_index": 1}],
                )
            ]
        )
        self.assertEqual(report.unattributable_records, 3)
        self.assertEqual(report.unattributable_turn_results, 1)
        self.assertEqual(report.runs, ())
