"""Coverage, issue and window metrics over the assurance ledger."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from agent_session_loop import assurance_models_summary, assurance_models_vocab
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import AuditPublication
from tests.asl_assurance_metrics_support import (
    OLD,
    OTHER_INPUT_DIGEST,
    RUN_A,
    RUN_B,
    RUN_C,
    RUN_D,
    RUN_E,
    RUN_F,
    T1,
    T2,
    AssuranceReportTestCase,
    _artifacts,
    _finding,
    _publication,
)

ASL_TEST_PORTABLE = True


class CoverageMetricsTests(AssuranceReportTestCase):
    def test_empty_ledger_reports_zeroes_and_stays_available(self) -> None:
        report = self.metrics()
        self.assertTrue(report["available"])
        self.assertIsNone(report["error"])
        self.assertEqual(report["ledger_path"], str(self.ledger_path))
        self.assertEqual(report["runs_recorded"], 0)
        self.assertEqual(
            report["coverage"],
            {
                "eligible_runs": 0,
                "ineligible_runs": 0,
                "audited_runs": 0,
                "coverage_rate": None,
            },
        )
        self.assertEqual(
            report["audit_states"],
            {audit_state: 0 for audit_state in assurance_models_vocab.AUDIT_STATES},
        )

    def test_coverage_separates_graded_runs_from_merely_recorded_ones(self) -> None:
        self.publish(_publication(run_id=RUN_A, state="all-clear", findings=()))
        self.publish(_publication(run_id=RUN_B))
        self.publish(
            AuditPublication(
                run_id=RUN_C, state="skipped", eligible=False, reason="opted-out"
            )
        )
        self.publish(
            AuditPublication(
                run_id=RUN_D,
                state="unavailable",
                eligible=True,
                reason="timeout",
                attempts=(
                    assurance_models_summary.AuditAttempt(
                        number=1,
                        outcome="timeout",
                        artifact_dir="supervisor-audit-attempt-1",
                    ),
                ),
            )
        )
        self.publish(AuditPublication(run_id=RUN_E, state="pending", eligible=True))
        # A partial audit reached a verdict on the lanes it could judge, so it
        # counts as audited: the gap it reports is coverage of the rubric, not
        # of the run.
        self.publish(
            _publication(
                run_id=RUN_F,
                state="partial",
                findings=(),
                reason="lane-evidence-absent",
            )
        )

        report = self.metrics()
        self.assertEqual(report["runs_recorded"], 6)
        self.assertEqual(
            report["coverage"],
            {
                "eligible_runs": 5,
                "ineligible_runs": 1,
                "audited_runs": 3,
                "coverage_rate": 0.6,
            },
        )
        self.assertEqual(
            report["audit_states"],
            {
                "all-clear": 1,
                "findings": 1,
                "partial": 1,
                "unavailable": 1,
                "skipped": 1,
                "pending": 1,
            },
        )

    def test_superseded_state_counts_once_under_its_latest_verdict(self) -> None:
        self.publish(AuditPublication(run_id=RUN_A, state="pending", eligible=True))
        self.publish(_publication(run_id=RUN_A), recorded_at=T1)

        report = self.metrics()
        self.assertEqual(report["runs_recorded"], 1)
        self.assertEqual(report["audit_states"]["pending"], 0)
        self.assertEqual(report["audit_states"]["findings"], 1)


class IssueMetricsTests(AssuranceReportTestCase):
    def test_findings_are_bucketed_by_severity_and_category(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(
                        category="scope",
                        invariant_key="scope.no-escape",
                        severity="low",
                    ),
                    _finding(
                        category="scope",
                        invariant_key="scope.no-unrelated-edits",
                        severity="low",
                    ),
                )
            )
        )

        issues = self.metrics()["issues"]
        self.assertEqual(issues["observations"], 3)
        self.assertEqual(issues["by_severity"], {"high": 1, "low": 2})
        self.assertEqual(issues["by_category"], {"lifecycle": 1, "scope": 2})
        self.assertEqual(issues["open_total"], 3)

    def test_new_and_recurring_are_counted_separately(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        self.publish(
            _publication(
                run_id=RUN_B, artifacts=_artifacts(OTHER_INPUT_DIGEST), completed_at=T1
            ),
            recorded_at=T1,
        )

        issues = self.metrics()["issues"]
        self.assertEqual(issues["observations"], 2)
        self.assertEqual(issues["new"], 1)
        self.assertEqual(issues["recurring"], 1)
        self.assertEqual(issues["open_total"], 1)

    def test_recurrence_after_resolution_is_windowed(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=T1
        )
        self.publish(
            _publication(
                run_id=RUN_B, artifacts=_artifacts(OTHER_INPUT_DIGEST), completed_at=T2
            ),
            recorded_at=T2,
        )

        issues = self.metrics()["issues"]
        self.assertEqual(issues["recurrence_after_resolution"], 1)
        self.assertEqual(issues["issues_recurring_after_resolution"], 1)
        self.assertEqual(issues["open_total"], 1)

        later = self.metrics(since=datetime(2026, 8, 11, tzinfo=UTC))["issues"]
        self.assertEqual(later["recurrence_after_resolution"], 0)
        self.assertEqual(later["issues_recurring_after_resolution"], 0)

    def test_recurrence_after_resolution_is_split_by_finding_category(self) -> None:
        findings = (
            _finding(),
            _finding(invariant_key="retry.no-double-launch"),
            _finding(category="scope", invariant_key="scope.no-escape", severity="low"),
        )
        self.publish(_publication(run_id=RUN_A, findings=findings))
        for issue_id in ("ASI-1", "ASI-2", "ASI-3"):
            projection.record_disposition(
                issue_id, "resolve", state_dir=self.state_dir, recorded_at=T1
            )
        self.publish(
            _publication(
                run_id=RUN_B,
                findings=findings,
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=T2,
            ),
            recorded_at=T2,
        )

        issues = self.metrics()["issues"]
        self.assertEqual(
            issues["recurrence_after_resolution_by_category"],
            {"lifecycle": 2, "scope": 1},
        )
        self.assertEqual(issues["recurrence_after_resolution"], 3)
        self.assertEqual(issues["issues_recurring_after_resolution"], 3)

        later = self.metrics(since=datetime(2026, 8, 11, tzinfo=UTC))["issues"]
        self.assertEqual(later["recurrence_after_resolution_by_category"], {})

    def test_recurrence_after_dismissal_is_not_a_reopen(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        projection.record_disposition(
            "ASI-1", "dismiss", state_dir=self.state_dir, recorded_at=T1
        )
        self.publish(
            _publication(
                run_id=RUN_B, artifacts=_artifacts(OTHER_INPUT_DIGEST), completed_at=T2
            ),
            recorded_at=T2,
        )

        issues = self.metrics()["issues"]
        self.assertEqual(issues["recurrence_after_resolution"], 0)
        self.assertEqual(issues["open_total"], 0)


class MetricsWindowTests(AssuranceReportTestCase):
    def test_records_before_the_window_are_excluded_but_open_count_is_now(
        self,
    ) -> None:
        self.publish(_publication(completed_at=OLD), recorded_at=OLD)
        projection.record_closeout(
            RUN_A, "accepted", state_dir=self.state_dir, recorded_at=OLD
        )

        report = self.metrics()
        self.assertEqual(report["runs_recorded"], 0)
        self.assertEqual(report["coverage"]["audited_runs"], 0)
        self.assertEqual(report["issues"]["observations"], 0)
        self.assertEqual(report["closeouts"]["total"], 0)
        # The open backlog is a fact about now, not about the window.
        self.assertEqual(report["issues"]["open_total"], 1)

    def test_unreadable_ledger_is_reported_not_raised(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.mkdir()

        report = self.metrics()
        self.assertFalse(report["available"])
        self.assertTrue(report["error"])
        self.assertEqual(report["ledger_path"], str(self.ledger_path))
        self.assertEqual(report["runs_recorded"], 0)
        self.assertEqual(report["issues"]["open_total"], 0)
        self.assertIsNone(report["coverage"]["coverage_rate"])


if __name__ == "__main__":
    unittest.main()
