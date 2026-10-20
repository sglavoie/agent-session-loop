"""Identity and disposition metrics over the assurance ledger."""

from __future__ import annotations

import json
import unittest

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models_inbox, assurance_models_primitives
from agent_session_loop import assurance_projection as projection
from tests.asl_assurance_metrics_support import (
    RUN_A,
    RUN_B,
    T1,
    T2,
    AssuranceReportTestCase,
    _finding,
    _publication,
)


class IdentityMetricsTests(AssuranceReportTestCase):
    def observe_unbound(self, **overrides) -> None:
        """Open an issue the way a guard does, which leaves it unnamed."""
        ledger.append_issue_observation(
            assurance_models_inbox.IssueObservation(
                run_id=RUN_B, finding=_finding(**overrides), observed_at=T2
            ),
            state_dir=self.state_dir,
            recorded_at=T2,
        )

    def force_binding(self, issue_id: str, key_of: str) -> None:
        """Write a binding the ledger writer refuses, to reach the anomaly."""
        binding = assurance_models_inbox.IssueIdentityBinding(
            identity_key=assurance_models_primitives.identity_key_for_issue(key_of),
            issue_id=issue_id,
            source="reconcile",
            bound_at=T2,
        )
        line = json.dumps(
            {
                "version": ledger.LEDGER_SCHEMA_VERSION,
                "kind": ledger.KIND_ISSUE_IDENTITY_BOUND,
                "recorded_at": T2,
                "issue_id": None,
                "payload": binding.to_dict(),
            }
        )
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def test_an_empty_ledger_counts_no_identities_and_no_anomalies(self) -> None:
        report = self.metrics()
        self.assertEqual(report["issues"]["bound_total"], 0)
        self.assertEqual(report["issues"]["unbound_total"], 0)
        self.assertEqual(
            report["integrity"],
            {
                "anomalies": 0,
                "rejected_records": 0,
                "identity_key_conflicts": 0,
                "duplicate_identity_bindings": 0,
            },
        )

    def test_bound_and_unbound_split_the_whole_backlog(self) -> None:
        self.publish(_publication())
        self.observe_unbound(invariant_key="scope.no-escape")
        self.observe_unbound(invariant_key="commit.guarded")

        issues = self.metrics()["issues"]
        self.assertEqual(issues["bound_total"], 1)
        self.assertEqual(issues["unbound_total"], 2)

    def test_a_closed_issue_still_counts_as_bound(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=T2
        )

        issues = self.metrics()["issues"]
        self.assertEqual(issues["open_total"], 0)
        self.assertEqual(issues["bound_total"], 1)
        self.assertEqual(issues["unbound_total"], 0)

    def test_a_key_an_issue_absorbed_counts_that_issue_as_bound(self) -> None:
        self.publish(_publication())
        self.observe_unbound(invariant_key="scope.no-escape")
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=T2
        )

        issues = self.metrics()["issues"]
        self.assertEqual(issues["bound_total"], 1)
        self.assertEqual(issues["unbound_total"], 0)

    def test_a_conflict_and_a_duplicate_are_counted_apart(self) -> None:
        self.publish(
            _publication(
                findings=(_finding(), _finding(invariant_key="scope.no-escape"))
            )
        )
        self.force_binding("ASI-2", "ASI-1")
        self.force_binding("ASI-1", "ASI-1")

        integrity = self.metrics()["integrity"]
        self.assertEqual(integrity["identity_key_conflicts"], 1)
        self.assertEqual(integrity["duplicate_identity_bindings"], 1)
        self.assertEqual(integrity["anomalies"], 2)
        self.assertEqual(integrity["rejected_records"], 0)


class DispositionMetricsTests(AssuranceReportTestCase):
    def _open_four(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(invariant_key="scope.no-escape"),
                    _finding(invariant_key="scope.no-unrelated-edits"),
                    _finding(invariant_key="evidence.cited"),
                )
            )
        )

    def test_dismissal_and_accepted_risk_rates(self) -> None:
        self._open_four()
        for issue_id, kind in (
            ("ASI-1", "resolve"),
            ("ASI-2", "dismiss"),
            ("ASI-3", "dismiss"),
            ("ASI-4", "accept-risk"),
        ):
            projection.record_disposition(
                issue_id, kind, state_dir=self.state_dir, recorded_at=T1
            )

        dispositions = self.metrics()["dispositions"]
        self.assertEqual(dispositions["total"], 4)
        self.assertEqual(
            dispositions["by_kind"],
            {"accept-risk": 1, "dismiss": 2, "resolve": 1},
        )
        self.assertEqual(dispositions["dismissal_rate"], 0.5)
        self.assertEqual(dispositions["accepted_risk_rate"], 0.25)

    def test_time_to_disposition_is_measured_from_first_sighting(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=T2
        )

        latency = self.metrics()["dispositions"]["time_to_disposition"]
        self.assertEqual(latency["count"], 1)
        self.assertEqual(latency["mean_hours"], 2.0)
        self.assertEqual(latency["median_hours"], 2.0)
        self.assertEqual(latency["max_hours"], 2.0)

    def test_closeout_outcomes_escapes_and_rework(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        self.publish(
            _publication(run_id=RUN_B, state="all-clear", findings=()), recorded_at=T1
        )
        projection.record_closeout(
            RUN_A,
            "escaped",
            issue_ids=("ASI-1",),
            rework_minutes=45.0,
            state_dir=self.state_dir,
            recorded_at=T1,
        )
        projection.record_closeout(
            RUN_B,
            "accepted",
            rework_minutes=15.0,
            state_dir=self.state_dir,
            recorded_at=T2,
        )

        closeouts = self.metrics()["closeouts"]
        self.assertEqual(closeouts["total"], 2)
        self.assertEqual(closeouts["by_outcome"], {"accepted": 1, "escaped": 1})
        self.assertEqual(closeouts["escaped_issues"], 1)
        self.assertEqual(closeouts["rework_minutes"], 60.0)
        self.assertEqual(closeouts["closeouts_reporting_rework"], 2)

    def test_superseding_closeout_replaces_rather_than_adds(self) -> None:
        self.publish(_publication())
        projection.record_closeout(
            RUN_A,
            "accepted",
            rework_minutes=10.0,
            state_dir=self.state_dir,
            recorded_at=T1,
        )
        projection.record_closeout(
            RUN_A,
            "reworked",
            rework_minutes=30.0,
            replace=True,
            state_dir=self.state_dir,
            recorded_at=T2,
        )

        closeouts = self.metrics()["closeouts"]
        self.assertEqual(closeouts["total"], 1)
        self.assertEqual(closeouts["by_outcome"], {"reworked": 1})
        self.assertEqual(closeouts["rework_minutes"], 30.0)
        self.assertEqual(closeouts["closeouts_reporting_rework"], 1)


if __name__ == "__main__":
    unittest.main()
