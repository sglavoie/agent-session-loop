from .asl_execution_timing_support import *


class TrustSeparationTests(unittest.TestCase):
    def _mixed_report(self) -> execution_timing.ExecutionTimingReport:
        partial = [
            _run_started(run_id=OTHER_RUN_ID),
            _execution_finished(
                turn_index=1, started=300, finished=400, run_id=OTHER_RUN_ID
            ),
            _run_finished(
                run_id=OTHER_RUN_ID, started=300, finished=400, duration=100.0
            ),
        ]
        return _collect(
            [
                execution_timing.RunEvidence(
                    PLAN, event_records=[*_clean_run_events(), *partial]
                )
            ]
        )

    def test_known_durations_keep_seconds_that_shares_must_drop(self) -> None:
        report = self._mixed_report()
        self.assertEqual(len(report.included_runs), 2)
        self.assertEqual(len(report.trustworthy_runs), 1)
        known = report.known_durations()
        shares = report.phase_shares()
        # The partial run's unclassified 100 seconds are preserved as a known
        # duration but excluded from the phase-share basis.
        self.assertEqual(known["seconds"]["turn_unclassified"], 100.0)
        self.assertEqual(shares["seconds"]["turn_unclassified"], 0.0)
        self.assertEqual(known["wall_seconds"], 350.0)
        self.assertEqual(shares["basis_seconds"], 250.0)

    def test_classification_counts_name_every_run(self) -> None:
        report = self._mixed_report()
        counts = report.counts_by_classification()
        self.assertEqual(sum(counts.values()), len(report.runs))
        self.assertEqual(counts[execution_timing.CLASSIFICATION_COMPLETE], 1)
        self.assertEqual(counts[execution_timing.CLASSIFICATION_PARTIAL], 1)

    def test_exclusion_reasons_are_reported(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[_run_started(dry_run=True), _run_finished()],
                ),
                execution_timing.RunEvidence(
                    "plan-b",
                    event_records=[_run_started(run_id=OTHER_RUN_ID)],
                ),
            ]
        )
        self.assertEqual(
            report.exclusions(),
            {
                execution_timing.EXCLUDED_ACTIVE: 1,
                execution_timing.EXCLUDED_DRY_RUN: 1,
            },
        )

    def test_an_inconsistent_run_never_backs_a_phase_share(self) -> None:
        report = _collect(
            [
                execution_timing.RunEvidence(
                    PLAN,
                    event_records=[
                        _run_started(),
                        _execution_finished(turn_index=1, started=0, finished=500),
                        _turn_finished(turn_index=1, started=0, finished=500),
                        _run_finished(started=0, finished=100, duration=100.0),
                    ],
                )
            ]
        )
        self.assertEqual(len(report.included_runs), 1)
        self.assertEqual(report.trustworthy_runs, ())
        self.assertIsNone(report.phase_shares()["shares"])
        self.assertEqual(report.known_durations()["seconds"]["worker_execution"], 500.0)


class PurityTests(unittest.TestCase):
    def test_the_collector_does_no_input_or_output(self) -> None:
        source = Path(execution_timing.__file__).read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]
        for forbidden in ("open(", "Path(", "print(", "import os", "json."):
            self.assertNotIn(forbidden, body, f"collector must not use {forbidden}")

    def test_every_observation_code_is_declared(self) -> None:
        run = _one(_clean_run_events())
        declared = set(execution_timing.ANOMALY_CODES) | set(execution_timing.GAP_CODES)
        self.assertEqual(run.anomalies, ())
        self.assertTrue(declared)
        # Codes are stable identifiers a renderer can switch on.
        self.assertEqual(len(declared), len(set(declared)))


if __name__ == "__main__":
    unittest.main()
