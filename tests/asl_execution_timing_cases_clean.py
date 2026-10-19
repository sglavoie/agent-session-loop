from .asl_execution_timing_support import *


class CleanRunAccountingTests(unittest.TestCase):
    def test_a_reconciling_run_attributes_every_phase_and_is_trustworthy(self) -> None:
        run = _one(_clean_run_events())
        self.assertEqual(run.classification, execution_timing.CLASSIFICATION_COMPLETE)
        self.assertTrue(run.included)
        self.assertTrue(run.trustworthy)
        self.assertEqual(run.anomalies, ())
        self.assertEqual(run.gaps, ())
        self.assertEqual(run.wall_seconds, 250.0)
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
        self.assertEqual(run.attributed_seconds, 220.0)
        self.assertEqual(run.unattributed_seconds, 30.0)

    def test_phase_shares_sum_to_the_whole_run(self) -> None:
        report = _collect(
            [execution_timing.RunEvidence(PLAN, event_records=_clean_run_events())]
        )
        shares = report.phase_shares()
        self.assertEqual(shares["basis_runs"], 1)
        self.assertEqual(shares["basis_seconds"], 250.0)
        assert shares["shares"] is not None
        self.assertAlmostEqual(sum(shares["shares"].values()), 1.0, places=6)
        self.assertAlmostEqual(
            shares["shares"][execution_timing.PHASE_WORKER_EXECUTION], 0.4
        )
        self.assertAlmostEqual(
            shares["shares"][execution_timing.PHASE_UNATTRIBUTED], 0.12
        )

    def test_the_report_round_trips_through_json(self) -> None:
        report = _collect(
            [execution_timing.RunEvidence(PLAN, event_records=_clean_run_events())]
        )
        encoded = json.dumps(report.to_dict())
        self.assertEqual(json.loads(encoded)["runs_included"], 1)
