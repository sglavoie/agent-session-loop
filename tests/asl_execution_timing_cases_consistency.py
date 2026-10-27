from .asl_execution_timing_support import *


class InconsistencyTests(unittest.TestCase):
    def test_invalid_numbers_are_reported_rather_than_absorbed(self) -> None:
        for label, value in (
            ("string", "twelve"),
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("bool", True),
            ("negative", -5.0),
        ):
            with self.subTest(label):
                run = _one(
                    [
                        _run_started(),
                        _event(
                            events.TURN_EXECUTION_FINISHED_EVENT,
                            {
                                "run_id": RUN_ID,
                                "turn_index": 1,
                                "session_id": "1",
                                "duration_seconds": value,
                            },
                        ),
                        _run_finished(),
                    ]
                )
                self.assertIn(
                    execution_timing.ANOMALY_INVALID_NUMBER, _codes(run.anomalies)
                )
                self.assertIsNone(run.attempts[0].duration_seconds)
                self.assertEqual(
                    run.phase_seconds[execution_timing.PHASE_WORKER_EXECUTION], 0.0
                )

    def test_backwards_timestamp_spans_are_reported_rather_than_absorbed(
        self,
    ) -> None:
        records = [
            record
            for record in _clean_run_events()
            if record["name"]
            not in (
                events.VERIFICATION_STARTED_EVENT,
                events.VERIFICATION_FINISHED_EVENT,
            )
        ]
        verification = _verification(turn_index=1, started=140, finished=100)
        del verification[1]["payload"]["duration_seconds"]
        records[4:4] = verification
        phase_run = _one(records)
        self.assertIn(
            execution_timing.ANOMALY_INVALID_NUMBER, _codes(phase_run.anomalies)
        )
        self.assertEqual(
            0.0, phase_run.phase_seconds[execution_timing.PHASE_VERIFICATION]
        )
        self.assertFalse(phase_run.trustworthy)

        turn = _execution_finished(turn_index=1, started=100, finished=0)
        del turn["payload"]["duration_seconds"]
        turn_run = _one([_run_started(), turn, _run_finished()])
        self.assertIn(
            execution_timing.ANOMALY_INVALID_NUMBER, _codes(turn_run.anomalies)
        )
        self.assertIsNone(turn_run.attempts[0].duration_seconds)

        wall_run = _one(
            [_run_started(), _run_finished(started=500, finished=300, duration=None)]
        )
        self.assertIn(
            execution_timing.ANOMALY_INVALID_NUMBER, _codes(wall_run.anomalies)
        )
        self.assertIsNone(wall_run.wall_seconds)

    def test_a_duration_contradicting_its_own_timestamps_is_flagged(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(
                    turn_index=1, started=0, finished=100, duration=900.0
                ),
                _turn_finished(turn_index=1, started=0, finished=100, duration=900.0),
                _run_finished(),
            ]
        )
        codes = _codes(run.anomalies)
        self.assertIn(execution_timing.ANOMALY_CONTRADICTORY_DURATION, codes)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )

    def test_a_run_wall_contradicting_its_own_timestamps_is_flagged(self) -> None:
        run = _one([_run_started(), _run_finished(duration=9000.0)])
        self.assertIn(
            execution_timing.ANOMALY_CONTRADICTORY_DURATION, _codes(run.anomalies)
        )

    def test_overlapping_phases_are_detected(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                # Verification claims to have started while the harness was
                # still running.
                *_verification(turn_index=1, started=50, finished=140),
                _run_finished(),
            ]
        )
        overlap = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_OVERLAPPING_PHASES
        )
        self.assertEqual(overlap.detail["overlap_seconds"], 50.0)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )

    def test_a_long_phase_overlapping_several_short_ones_is_caught_each_time(
        self,
    ) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=200),
                _turn_finished(turn_index=1, started=0, finished=200),
                *_verification(turn_index=1, started=20, finished=40),
                *_commit(commit_index=0, started=60, finished=80),
                _run_finished(),
            ]
        )
        overlaps = [
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_OVERLAPPING_PHASES
        ]
        self.assertEqual(len(overlaps), 2)

    def test_attributed_time_exceeding_the_run_wall_is_never_clamped(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=500),
                _turn_finished(turn_index=1, started=0, finished=500),
                _run_finished(started=0, finished=100, duration=100.0),
            ]
        )
        self.assertIn(
            execution_timing.ANOMALY_ATTRIBUTED_EXCEEDS_WALL, _codes(run.anomalies)
        )
        self.assertEqual(run.unattributed_seconds, -400.0)
        self.assertEqual(
            run.classification, execution_timing.CLASSIFICATION_INCONSISTENT
        )

    def test_sub_tolerance_rounding_does_not_become_a_negative_remainder(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=250),
                _turn_finished(turn_index=1, started=0, finished=250),
                _run_finished(duration=249.6),
            ]
        )
        self.assertEqual(run.unattributed_seconds, 0.0)
        self.assertEqual(_codes(run.anomalies), [])

    def test_a_second_run_finished_record_is_flagged(self) -> None:
        run = _one([_run_started(), _run_finished(), _run_finished()])
        self.assertIn(
            execution_timing.ANOMALY_DUPLICATE_EVIDENCE, _codes(run.anomalies)
        )

    def test_disagreeing_session_kinds_are_a_conflict(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=100),
                _turn_finished(turn_index=1, started=0, finished=100),
                _run_finished(),
            ],
            [
                _result(
                    turn_index=1,
                    started=0,
                    finished=100,
                    session_kind=plans_payloads.SESSION_KIND_REVIEW,
                )
            ],
        )
        conflict = next(
            item
            for item in run.anomalies
            if item.code == execution_timing.ANOMALY_CONFLICTING_EVIDENCE
        )
        self.assertEqual(conflict.detail["field"], "session_kind")

    def test_unhashable_phase_discriminators_do_not_crash_collection(self) -> None:
        # A list or object where a turn index or phase name belongs is a bad
        # payload value, not a reason to crash `asl metrics`.
        for name, field in (
            (events.VERIFICATION_STARTED_EVENT, "turn_index"),
            (events.VERIFICATION_FINISHED_EVENT, "turn_index"),
            (events.FINALIZATION_STARTED_EVENT, "phase"),
            (events.FINALIZATION_FINISHED_EVENT, "phase"),
        ):
            for value in ([1], {"x": 1}):
                with self.subTest(name=name, value=value):
                    run = _one(
                        [
                            _run_started(),
                            _event(name, {"run_id": RUN_ID, field: value}),
                            _run_finished(),
                        ]
                    )
                    self.assertEqual(run.run_id, RUN_ID)


class ZeroAndEmptyTests(unittest.TestCase):
    def test_a_zero_duration_run_reports_no_shares_instead_of_dividing_by_zero(
        self,
    ) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        _run_finished(started=0, finished=0, duration=0.0),
                    ],
                )
            ]
        )
        run = report.runs[0]
        self.assertEqual(run.wall_seconds, 0.0)
        self.assertEqual(run.unattributed_seconds, 0.0)
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_COMPLETE)
        shares = report.phase_shares()
        self.assertEqual(shares["basis_runs"], 1)
        self.assertEqual(shares["basis_seconds"], 0.0)
        self.assertIsNone(shares["shares"])

    def test_a_zero_duration_turn_is_still_a_reconciled_attempt(self) -> None:
        run = _one(
            [
                _run_started(),
                _execution_finished(turn_index=1, started=0, finished=0),
                _turn_finished(turn_index=1, started=0, finished=0),
                _run_finished(),
            ]
        )
        self.assertEqual(run.attempts[0].duration_seconds, 0.0)
        self.assertEqual(_codes(run.anomalies), [])
        self.assertEqual(_codes(run.gaps), [])

    def test_an_explicit_zero_length_phase_is_not_a_missing_timing_gap(self) -> None:
        """A recorded ``0.0`` is a measurement; an absent duration is not."""
        for phase, records in (
            (
                execution_timing.PHASE_VERIFICATION,
                _verification(turn_index=1, started=100, finished=100),
            ),
            (
                execution_timing.PHASE_COMMITS,
                _commit(commit_index=0, started=140, finished=140),
            ),
            (
                execution_timing.PHASE_ASSURANCE,
                _finalization(started=200, finished=200),
            ),
        ):
            with self.subTest(phase=phase):
                run = _one([_run_started(), *records, _run_finished()])
                self.assertEqual(_codes(run.gaps), [])
                self.assertEqual(_codes(run.anomalies), [])
                self.assertEqual(
                    run.classification, execution_timing.CLASSIFICATION_COMPLETE
                )
                self.assertEqual(run.phase_seconds[phase], 0.0)

    def test_no_evidence_at_all_produces_an_empty_report(self) -> None:
        report = _collect([])
        self.assertEqual(report.runs, ())
        self.assertEqual(report.known_durations()["wall_seconds"], 0.0)
        self.assertIsNone(report.phase_shares()["shares"])
