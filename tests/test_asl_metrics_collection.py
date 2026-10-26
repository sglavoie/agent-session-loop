"""Workflow metrics aggregation tests: turns, pauses, reviews, assurance."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import assurance_projection as assurance_projection
from agent_session_loop import (
    events,
    metrics,
    metrics_models,
)
from tests.asl_metrics_support import (
    MetricsCollectionBase,
    _append_event,
    _iso,
    _make_state_dir,
    _publish_audit,
    _write_turn_result,
)

ASL_TEST_PORTABLE = True


class MetricsCollectionTests(MetricsCollectionBase):
    def test_empty_state_root_reports_zero_counts(self) -> None:
        report = self._collect()
        self.assertEqual(0, report.state_dirs_scanned)
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])
        self.assertEqual(0, report.agent_mux_delivery["total"])
        self.assertIsNone(report.first_pass["first_pass_rate"])
        self.assertIsNone(report.verification_repair["repair_success_rate"])

    def test_missing_state_root_reports_zero_counts(self) -> None:
        report = metrics.collect_metrics(
            state_dir=self.state_root / "absent", since_days=7, now=self.now
        )
        self.assertEqual(0, report.state_dirs_scanned)

    def test_rejects_non_positive_since_days(self) -> None:
        with self.assertRaises(ValueError):
            self._collect(since_days=0)

    def test_first_pass_completion_counts_first_turn_success(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-a")
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=5),
            finished_at=_iso(self.now, hours_ago=4.9),
        )
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=2,
            session_id="2",
            started_at=_iso(self.now, hours_ago=4),
            finished_at=_iso(self.now, hours_ago=3.9),
            status_result="verify-failed",
        )
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=3,
            session_id="2",
            started_at=_iso(self.now, hours_ago=3),
            finished_at=_iso(self.now, hours_ago=2.9),
        )
        report = self._collect()
        self.assertEqual(2, report.first_pass["sessions_attempted"])
        self.assertEqual(2, report.first_pass["sessions_completed"])
        self.assertEqual(1, report.first_pass["sessions_completed_first_pass"])
        self.assertEqual(0.5, report.first_pass["first_pass_rate"])
        self.assertEqual(3, report.turns["total"])
        self.assertEqual(1, report.turns["unsuccessful"])
        self.assertEqual(60.0, report.turns["unsuccessful_runtime_seconds"])

    def test_a_failure_before_the_window_still_spoils_first_pass(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-d")
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=24 * 10),
            finished_at=_iso(self.now, hours_ago=24 * 10 - 0.1),
            status_result="verify-failed",
        )
        _write_turn_result(
            state_dir,
            run_id="run-2",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=24 * 2),
            finished_at=_iso(self.now, hours_ago=24 * 2 - 0.1),
        )
        report = self._collect(since_days=7)
        self.assertEqual(1, report.first_pass["sessions_attempted"])
        self.assertEqual(1, report.first_pass["sessions_completed"])
        self.assertEqual(0, report.first_pass["sessions_completed_first_pass"])
        self.assertEqual(0.0, report.first_pass["first_pass_rate"])

    def test_non_finite_or_boolean_durations_add_no_runtime(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-e")
        for index, duration in enumerate((float("inf"), float("nan"), True), 1):
            _write_turn_result(
                state_dir,
                run_id="run-1",
                turn_index=index,
                session_id=str(index),
                started_at=_iso(self.now, hours_ago=3),
                finished_at=_iso(self.now, hours_ago=2.9),
                duration_seconds=duration,
            )
        report = self._collect()
        self.assertEqual(3, report.turns["total"])
        self.assertEqual(0.0, report.turns["successful_runtime_seconds"])
        json.dumps(report.turns, allow_nan=False)

    def test_review_turns_do_not_count_toward_first_pass(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-b")
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="3",
            session_kind="review",
            started_at=_iso(self.now, hours_ago=2),
            finished_at=_iso(self.now, hours_ago=1.9),
        )
        report = self._collect()
        self.assertEqual(0, report.first_pass["sessions_attempted"])
        self.assertEqual(1, report.turns["total"])

    def test_turns_outside_window_are_excluded(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-c")
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=24 * 30),
            finished_at=_iso(self.now, hours_ago=24 * 30 - 0.1),
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=24 * 30),
            name="run_paused",
            payload={"reason": "verify-failed", "details": {}},
        )
        report = self._collect()
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])
        self.assertEqual(0, report.state_dirs_active)

    def test_pause_classification_counts_by_reason_and_retryability(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-d")
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=3),
            name="run_paused",
            payload={
                "reason": "quota-exhausted",
                "details": {
                    "failure_classification": {
                        "reason": "quota-exhausted",
                        "retryable": False,
                    }
                },
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name="run_paused",
            payload={
                "reason": "transport-failed",
                "details": {
                    "failure_classification": {
                        "reason": "transport-failed",
                        "retryable": True,
                    }
                },
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=1),
            name="run_paused",
            payload={"reason": "blocked", "details": {}},
        )
        report = self._collect()
        self.assertEqual(3, report.pauses["total"])
        self.assertEqual(
            {"blocked": 1, "quota-exhausted": 1, "transport-failed": 1},
            report.pauses["by_reason"],
        )
        self.assertEqual(1, report.pauses["classified_never_retry"])
        self.assertEqual(1, report.pauses["classified_retryable"])

    def test_answered_mailbox_wait_does_not_count_as_a_pause(self) -> None:
        state_dir = _make_state_dir(
            self.state_root, self.tmp_path / "plan-mailbox-reply"
        )
        question_id = "00000000-0000-0000-0000-000000000010"
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name=events.MAILBOX_EVENT,
            payload={
                "action": events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": question_id,
                "session_id": "1",
                "turn_index": 1,
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=1),
            name=events.REPLY_PROVIDED_EVENT,
            payload={
                "session_id": "1",
                "turn_index": 2,
                "source": {
                    "origin": "mailbox",
                    "question_id": question_id,
                },
            },
        )

        report = self._collect()
        self.assertEqual(0, report.pauses["total"])
        self.assertEqual({}, report.pauses["by_reason"])

    def test_agent_mux_failures_are_visible_without_changing_lifecycle_metrics(
        self,
    ) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-delivery")
        for hours_ago, status, acknowledged, classification in (
            (3, "accepted", True, "none"),
            (2, "retryable_exhausted", False, "retryable_outcome"),
            (1, "legacy_unacknowledged", False, "legacy_binary"),
        ):
            _append_event(
                state_dir,
                created_at=_iso(self.now, hours_ago=hours_ago),
                name="agent_mux_delivery",
                payload={
                    "run_id": "run-1",
                    "final_status": status,
                    "acknowledged": acknowledged,
                    "error_classification": classification,
                },
            )

        report = self._collect()
        delivery = report.agent_mux_delivery
        self.assertEqual(3, delivery["total"])
        self.assertEqual(1, delivery["acknowledged"])
        self.assertEqual(1, delivery["failed"])
        self.assertEqual(1, delivery["legacy_unacknowledged"])
        self.assertEqual(
            {
                "accepted": 1,
                "legacy_unacknowledged": 1,
                "retryable_exhausted": 1,
            },
            delivery["by_status"],
        )
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])

    def test_verification_repair_success_tracks_later_completion(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-e")
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=6),
            name="run_paused",
            payload={
                "reason": "verify-failed",
                "details": {"session": "2", "verification_repair_exhausted": False},
            },
        )
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=2,
            session_id="2",
            started_at=_iso(self.now, hours_ago=5),
            finished_at=_iso(self.now, hours_ago=4.9),
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=4),
            name="run_paused",
            payload={
                "reason": "verify-failed",
                "details": {"session": "3", "verification_repair_exhausted": True},
            },
        )
        report = self._collect()
        repair = report.verification_repair
        self.assertEqual(2, repair["verify_failed_pauses"])
        self.assertEqual(1, repair["repair_exhausted_pauses"])
        self.assertEqual(2, repair["sessions_with_verify_failures"])
        self.assertEqual(1, repair["sessions_repaired"])
        self.assertEqual(0.5, repair["repair_success_rate"])

    def test_paused_work_commits_are_counted(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-settled")
        for hours_ago, result in ((6, "committed"), (5, "no-changes"), (4, "failed")):
            _append_event(
                state_dir,
                created_at=_iso(self.now, hours_ago=hours_ago),
                name=events.PAUSED_WORK_COMMITTED_EVENT,
                payload={
                    "run_id": "run-1",
                    "session_id": "2",
                    "guard": "paused-work-commit",
                    "pause_reason": "verify-failed",
                    "commit_mode": "per-session",
                    "result": result,
                },
            )
        # Outside the window: a settlement older than the window is not this
        # window's evidence, exactly like every other counted event.
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=24 * 9),
            name=events.PAUSED_WORK_COMMITTED_EVENT,
            payload={"run_id": "run-0", "result": "committed"},
        )
        # A settlement that recorded no result still counts as one, under the
        # same "unknown" bucket every other grouped counter uses.
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=3),
            name=events.PAUSED_WORK_COMMITTED_EVENT,
            payload={"run_id": "run-1"},
        )

        report = self._collect()
        repair = report.verification_repair
        self.assertEqual(4, repair["paused_work_commits"])
        self.assertEqual(
            {"committed": 1, "failed": 1, "no-changes": 1, "unknown": 1},
            repair["paused_work_commits_by_result"],
        )
        # The settlement is not a pause and not a verification failure; it must
        # not move the counters that answer those questions.
        self.assertEqual(0, report.pauses["total"])
        self.assertEqual(0, repair["verify_failed_pauses"])
        self.assertEqual(0, repair["repair_exhausted_pauses"])
        # The event is activity, so the directory is not skipped as dormant.
        self.assertEqual(1, report.state_dirs_active)

    def test_no_settlement_reports_an_empty_breakdown_not_a_missing_key(
        self,
    ) -> None:
        report = self._collect()
        self.assertEqual(0, report.verification_repair["paused_work_commits"])
        self.assertEqual(
            {}, report.verification_repair["paused_work_commits_by_result"]
        )

    def test_review_cycles_aggregate_dispositions_and_findings(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-f")
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=4),
            name="review_cycle",
            payload={
                "disposition": "repairs-materialized",
                "introduced_findings": ["finding:aa", "finding:bb"],
                "closed_findings": [],
                "repeated_findings": [],
                "repair_session_ids": ["3.1"],
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name="review_cycle",
            payload={
                "disposition": "all-clear",
                "introduced_findings": [],
                "closed_findings": ["finding:aa", "finding:bb"],
                "repeated_findings": [],
                "repair_session_ids": [],
            },
        )
        report = self._collect()
        cycles = report.review_cycles
        self.assertEqual(2, cycles["cycles_recorded"])
        self.assertEqual(
            {"all-clear": 1, "repairs-materialized": 1}, cycles["by_disposition"]
        )
        self.assertEqual(1, cycles["repairs_materialized"])
        self.assertEqual(2, cycles["findings_introduced"])
        self.assertEqual(2, cycles["findings_closed"])
        self.assertEqual(0, cycles["findings_repeated"])


class AssuranceMetricsSectionTests(unittest.TestCase):
    """The assurance section is aggregated once from the corpus-wide ledger."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()
        self.now = datetime.now(UTC)

    def _collect(self) -> metrics.MetricsReport:
        return metrics.collect_metrics(
            state_dir=self.state_root, since_days=7, now=self.now
        )

    def test_absent_ledger_reports_an_empty_but_available_section(self) -> None:
        report = self._collect()
        self.assertTrue(report.assurance["available"])
        self.assertEqual(report.assurance["runs_recorded"], 0)
        self.assertEqual(report.assurance["issues"]["open_total"], 0)
        self.assertIn("assurance", report.to_dict())

    def test_section_reports_coverage_findings_and_dispositions(self) -> None:
        _publish_audit(self.state_root, recorded_at=_iso(self.now, hours_ago=3))
        assurance_projection.record_disposition(
            "ASI-1",
            "dismiss",
            state_dir=self.state_root,
            recorded_at=_iso(self.now, hours_ago=1),
        )

        section = self._collect().assurance

        self.assertEqual(section["runs_recorded"], 1)
        self.assertEqual(section["coverage"]["coverage_rate"], 1.0)
        self.assertEqual(section["audit_states"]["findings"], 1)
        self.assertEqual(section["issues"]["new"], 1)
        self.assertEqual(section["issues"]["by_severity"], {"high": 1})
        self.assertEqual(section["issues"]["by_category"], {"lifecycle": 1})
        self.assertEqual(section["issues"]["open_total"], 0)
        self.assertEqual(section["dispositions"]["dismissal_rate"], 1.0)
        self.assertEqual(section["dispositions"]["time_to_disposition"]["count"], 1)

    def test_records_outside_the_window_are_not_counted(self) -> None:
        _publish_audit(self.state_root, recorded_at=_iso(self.now, hours_ago=24 * 30))

        section = self._collect().assurance

        self.assertEqual(section["runs_recorded"], 0)
        self.assertEqual(section["issues"]["observations"], 0)
        self.assertEqual(section["issues"]["open_total"], 1)


class UndecodableRecordTests(unittest.TestCase):
    # Bytes that are not UTF-8 used to escape as UnicodeDecodeError and end the
    # whole metrics scan instead of reading like any other corrupt record.

    def test_a_non_utf8_event_line_is_skipped_and_its_neighbors_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(b'{"name": "a"}\n{"name": "\xff\n{"name": "b"}\n')

            records = metrics_models._read_event_records(path)

        self.assertEqual(["a", "b"], [record["name"] for record in records])

    def test_a_non_utf8_result_payload_reads_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_bytes(b'{"status": "\xff\xfe"}')

            self.assertIsNone(metrics_models._read_result_payload(path))


if __name__ == "__main__":
    unittest.main()
