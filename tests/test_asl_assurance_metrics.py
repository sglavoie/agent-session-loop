"""Operator, metrics, and retention projections over the assurance ledger."""

from __future__ import annotations

import ast
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
from agent_session_loop import assurance_report
from agent_session_loop.assurance_projection import AuditPublication

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_A = "20260810T100000000000Z-4241"
RUN_B = "20260810T110000000000Z-4242"
RUN_C = "20260810T120000000000Z-4243"
RUN_D = "20260810T130000000000Z-4244"
RUN_E = "20260810T140000000000Z-4245"

OLD = "2026-08-01T10:00:00+00:00"
T0 = "2026-08-10T10:00:00+00:00"
T1 = "2026-08-10T11:00:00+00:00"
T2 = "2026-08-10T12:00:00+00:00"

SINCE = datetime(2026, 8, 10, tzinfo=UTC)

INPUT_DIGEST = "a" * 64
OTHER_INPUT_DIGEST = "b" * 64


def _finding(**overrides) -> models.Finding:
    payload = {
        "category": "lifecycle",
        "component": "agent_session_loop/runner.py",
        "invariant_key": "retry.budget-respected",
        "lane": "lifecycle-retry-resume",
        "severity": "high",
        "summary": "A retry ran after the budget was exhausted.",
        "recommendation": "Clamp the retry counter before relaunching.",
        "evidence": ("events.jsonl",),
    }
    payload.update(overrides)
    return models.Finding(**payload)


def _artifacts(digest: str | None = INPUT_DIGEST) -> tuple[models.EvidenceRef, ...]:
    return (
        models.EvidenceRef(
            kind="artifact",
            locator=f"run/{models.AUDIT_INPUT_FILENAME}",
            digest=digest,
        ),
        models.EvidenceRef(
            kind="artifact", locator=f"run/{models.AUDIT_REPORT_FILENAME}"
        ),
    )


def _publication(**overrides) -> AuditPublication:
    payload = {
        "run_id": RUN_A,
        "state": "findings",
        "eligible": True,
        "findings": (_finding(),),
        "artifacts": _artifacts(),
        "completed_at": T0,
    }
    payload.update(overrides)
    return AuditPublication(**payload)


def _assurance(**overrides) -> dict:
    payload = {
        "run_id": RUN_A,
        "state": "findings",
        "eligible": True,
        "issue_ids": ["ASI-1", "ASI-2"],
        "new_issue_count": 1,
        "recurring_issue_count": 1,
        "artifacts": [
            {"kind": "artifact", "locator": f"run/{models.AUDIT_REPORT_FILENAME}"}
        ],
    }
    payload.update(overrides)
    return payload


class AssuranceReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)

    def publish(self, publication: AuditPublication, **kwargs):
        kwargs.setdefault("recorded_at", T0)
        return projection.publish_audit(publication, state_dir=self.state_dir, **kwargs)

    def metrics(self, since: datetime = SINCE) -> dict:
        return assurance_report.collect_assurance_metrics(
            state_dir=self.state_dir, since=since
        )


# ---------------------------------------------------------------------------
# Coverage and audit states
# ---------------------------------------------------------------------------


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
            {audit_state: 0 for audit_state in models.AUDIT_STATES},
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
                    models.AuditAttempt(
                        number=1,
                        outcome="timeout",
                        artifact_dir="supervisor-audit-attempt-1",
                    ),
                ),
            )
        )
        self.publish(AuditPublication(run_id=RUN_E, state="pending", eligible=True))

        report = self.metrics()
        self.assertEqual(report["runs_recorded"], 5)
        self.assertEqual(
            report["coverage"],
            {
                "eligible_runs": 4,
                "ineligible_runs": 1,
                "audited_runs": 2,
                "coverage_rate": 0.5,
            },
        )
        self.assertEqual(
            report["audit_states"],
            {
                "all-clear": 1,
                "findings": 1,
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


# ---------------------------------------------------------------------------
# Issue counts
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dispositions and closeouts
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Windowing and degradation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The per-run operator block
# ---------------------------------------------------------------------------


class RunReportTests(AssuranceReportTestCase):
    def test_report_carries_ids_counts_path_and_open_total(self) -> None:
        self.publish(_publication())
        report = assurance_report.build_run_report(
            _assurance(),
            run_dir=Path("/runs") / RUN_A,
            state_dir=self.state_dir,
        )

        self.assertEqual(report["run_id"], RUN_A)
        self.assertEqual(report["state"], "findings")
        self.assertEqual(report["issue_ids"], ["ASI-1", "ASI-2"])
        self.assertEqual(report["issue_count"], 2)
        self.assertEqual(report["new_issue_count"], 1)
        self.assertEqual(report["recurring_issue_count"], 1)
        self.assertFalse(report["partial"])
        self.assertEqual(
            report["report_path"],
            str(Path("/runs") / RUN_A / models.AUDIT_REPORT_FILENAME),
        )
        self.assertEqual(report["open_issue_count"], 1)
        self.assertIsNone(report["open_issue_error"])
        self.assertEqual(report["list_command"], assurance_report.LIST_COMMAND)

    def test_missing_state_yields_no_report_and_no_lines(self) -> None:
        self.assertIsNone(assurance_report.build_run_report(None))
        self.assertIsNone(assurance_report.build_run_report({}))
        self.assertEqual(assurance_report.report_lines(None), [])

    def test_unavailable_findings_are_labelled_partial(self) -> None:
        report = assurance_report.build_run_report(
            _assurance(state="unavailable", reason="timeout"),
            state_dir=self.state_dir,
        )
        self.assertTrue(report["partial"])
        self.assertIn(
            "Assurance issues (partial)", assurance_report.report_lines(report)[0]
        )

    def test_unavailable_without_findings_is_not_partial(self) -> None:
        report = assurance_report.build_run_report(
            _assurance(state="unavailable", reason="timeout", issue_ids=[]),
            state_dir=self.state_dir,
        )
        self.assertFalse(report["partial"])

    def test_lines_print_counts_ids_open_total_and_the_list_hint(self) -> None:
        self.publish(_publication())
        lines = assurance_report.report_lines(
            assurance_report.build_run_report(
                _assurance(), run_dir="/runs/a", state_dir=self.state_dir
            )
        )
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "Assurance issues: 1 new, 1 recurring; ASI-1, ASI-2")
        self.assertEqual(lines[1], "Assurance inbox: 1 open; `asl assurance list`")
        self.assertEqual(
            lines[2],
            f"Assurance report: /runs/a/{models.AUDIT_REPORT_FILENAME}",
        )

    def test_unread_open_count_says_so_instead_of_guessing(self) -> None:
        report = assurance_report.build_run_report(_assurance(), open_issues=False)
        self.assertIsNone(report["open_issue_count"])
        self.assertIn(
            "open count unavailable", assurance_report.report_lines(report)[1]
        )

    def test_status_line_names_the_harness_or_the_reason(self) -> None:
        self.assertEqual(
            assurance_report.status_line(
                {
                    "state": "findings",
                    "role": {
                        "harness": "codex",
                        "source": "config",
                        "timeout_seconds": 600,
                    },
                }
            ),
            "Supervisor audit: findings (codex via config role, 600s per attempt)",
        )
        self.assertEqual(
            assurance_report.status_line({"state": "skipped", "reason": "opted-out"}),
            "Supervisor audit: skipped (opted-out)",
        )
        self.assertIsNone(assurance_report.status_line(None))
        self.assertIsNone(assurance_report.status_line({}))


class BoundedSummaryTests(AssuranceReportTestCase):
    def test_summary_truncates_and_flags_it(self) -> None:
        issue_ids = [f"ASI-{index}" for index in range(1, 21)]
        summary = assurance_report.bounded_summary(_assurance(issue_ids=issue_ids))

        self.assertEqual(
            len(summary["issue_ids"]), assurance_report.MAX_BOUNDED_ISSUE_IDS
        )
        self.assertEqual(summary["issue_count"], 20)
        self.assertTrue(summary["issue_ids_truncated"])
        self.assertEqual(summary["list_command"], assurance_report.LIST_COMMAND)

    def test_short_lists_are_not_flagged_as_truncated(self) -> None:
        summary = assurance_report.bounded_summary(_assurance())
        self.assertEqual(summary["issue_ids"], ["ASI-1", "ASI-2"])
        self.assertFalse(summary["issue_ids_truncated"])
        self.assertEqual(summary["status_line"], "Supervisor audit: findings")

    def test_summary_is_empty_without_a_state(self) -> None:
        self.assertEqual(assurance_report.bounded_summary(None), {})
        self.assertEqual(assurance_report.bounded_summary({"run_id": RUN_A}), {})

    def test_summary_carries_no_control_surface(self) -> None:
        summary = assurance_report.bounded_summary(_assurance())
        for key in summary:
            self.assertNotIn("timeout", key)
            self.assertNotIn("command", key.replace("list_command", ""))
            self.assertNotIn("harness", key)
            self.assertNotIn("retry", key)


class OpenIssueRunsTests(AssuranceReportTestCase):
    def test_open_issues_pin_the_runs_that_found_them(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        self.publish(
            _publication(
                run_id=RUN_B,
                findings=(_finding(invariant_key="scope.no-escape"),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=T1,
            ),
            recorded_at=T1,
        )

        pinned = assurance_report.open_issue_run_ids(self.state_dir)
        self.assertTrue(pinned.available)
        self.assertEqual(pinned.open_issue_count, 2)
        self.assertEqual(pinned.run_ids, frozenset({RUN_A, RUN_B}))

    def test_closing_every_issue_releases_the_pin(self) -> None:
        self.publish(_publication(run_id=RUN_A))
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=T1
        )

        pinned = assurance_report.open_issue_run_ids(self.state_dir)
        self.assertTrue(pinned.available)
        self.assertEqual(pinned.open_issue_count, 0)
        self.assertEqual(pinned.run_ids, frozenset())

    def test_unreadable_ledger_pins_nothing_and_says_so(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.mkdir()

        pinned = assurance_report.open_issue_run_ids(self.state_dir)
        self.assertFalse(pinned.available)
        self.assertTrue(pinned.error)
        self.assertEqual(pinned.run_ids, frozenset())


class ObserverStructureTests(unittest.TestCase):
    def test_report_module_imports_no_module_that_can_launch_an_audit(self) -> None:
        """``asl status`` renders assurance without being able to produce it.

        The guarantee is structural: this module's only siblings are the
        ledger, its models, and its projection, so nothing reachable from a
        status or metrics command can start a harness.
        """
        source = (REPO_ROOT / "agent_session_loop" / "assurance_report.py").read_text()
        siblings = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.level == 1
            for alias in node.names
        }
        self.assertEqual(
            siblings,
            {"assurance_ledger", "assurance_models", "assurance_projection"},
        )


if __name__ == "__main__":
    unittest.main()
