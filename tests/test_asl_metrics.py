"""Workflow metrics aggregation and `asl metrics` CLI tests."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_session_loop import assurance_models as assurance
from agent_session_loop import assurance_projection as assurance_projection
from agent_session_loop import (
    cli,
    cli_trace,
    events,
    metrics,
    plans,
    review,
    review_continuation,
    review_repair,
)
from tests.support import write_plan_with_ids

AUDIT_RUN_ID = "20260810T101112123456Z-4242"


def _iso(now: datetime, *, hours_ago: float) -> str:
    return (now - timedelta(hours=hours_ago)).isoformat()


def _make_state_dir(state_root: Path, plan_dir: Path) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    state_dir = state_root / plans.path_sha256(plan_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps({"plan_dir": str(plan_dir), "version": 2}, sort_keys=True),
        encoding="utf-8",
    )
    return state_dir


def _write_turn_result(
    state_dir: Path,
    *,
    run_id: str,
    turn_index: int,
    session_id: str,
    started_at: str,
    finished_at: str,
    status_result: str = "complete",
    session_kind: str = "implementation",
    duration_seconds: float = 60.0,
    interrupted: bool = False,
    extra: dict | None = None,
) -> Path:
    turn_dir = (
        state_dir / "runs" / run_id / f"turn-{turn_index:04d}-session-{session_id}"
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session_kind": session_kind,
        "started_at": started_at,
        "finished_at": finished_at,
        "status_result": status_result,
        "duration_seconds": duration_seconds,
        "interrupted": interrupted,
    }
    payload.update(extra or {})
    result_path = turn_dir / "result.json"
    result_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result_path


def _append_event(
    state_dir: Path, *, created_at: str, name: str, payload: dict
) -> None:
    record = {
        "created_at": created_at,
        "name": name,
        "payload": payload,
        "version": 2,
    }
    with (state_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _publish_audit(state_root: Path, *, recorded_at: str, run_id: str = AUDIT_RUN_ID):
    finding = assurance.Finding(
        category="lifecycle",
        component="agent_session_loop/runner.py",
        invariant_key="retry.budget-respected",
        lane="lifecycle-retry-resume",
        severity="high",
        summary="A retry ran after the budget was exhausted.",
        recommendation="Clamp the retry counter before relaunching.",
        evidence=("events.jsonl",),
    )
    return assurance_projection.publish_audit(
        assurance_projection.AuditPublication(
            run_id=run_id,
            state="findings",
            eligible=True,
            findings=(finding,),
            completed_at=recorded_at,
        ),
        state_dir=state_root,
        recorded_at=recorded_at,
    )


def _write_generated_review_repair_plan(
    plan_dir: Path,
    *,
    review_id: str = "2",
    child_status: str = "pending",
) -> None:
    child_id = f"{review_id}.1"
    write_plan_with_ids(
        plan_dir,
        (("1", "complete"), (review_id, "complete"), (child_id, child_status)),
    )
    review_path = plan_dir / plans.session_id_to_filename(review_id)
    review_path.write_text(
        "\n".join(
            (
                review.REVIEW_SESSION_MARKER,
                review.review_session_version_marker(),
                review_path.read_text(encoding="utf-8"),
            )
        ),
        encoding="utf-8",
    )
    child_path = plan_dir / plans.session_id_to_filename(child_id)
    child_path.write_text(
        "\n".join(
            (
                review_repair.REVIEW_REPAIR_SESSION_MARKER,
                review_repair.review_repair_session_version_marker(),
                child_path.read_text(encoding="utf-8"),
            )
        ),
        encoding="utf-8",
    )


class MetricsCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()
        self.now = datetime.now(UTC)

    def _collect(self, since_days: int = 7) -> metrics.MetricsReport:
        return metrics.collect_metrics(
            state_dir=self.state_root, since_days=since_days, now=self.now
        )

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

    def test_review_reopens_report_attempts_and_dedupe_materializations(
        self,
    ) -> None:
        state_dir = _make_state_dir(
            self.state_root, self.tmp_path / "plan-review-reopen"
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=6),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=5),
            name=events.REVIEW_REPAIR_REOPENED_EVENT,
            payload={
                "source_cycle": 1,
                "source_review_session_id": "2",
                "source_disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
                "materialized": True,
                "result": "ok",
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=4),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "cycle": 2,
                "review_session_id": "3",
                "disposition": "repairs-materialized",
                "repair_session_ids": ["3.1"],
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=3),
            name=events.REVIEW_REPAIR_REOPENED_EVENT,
            payload={
                "source_cycle": 2,
                "source_review_session_id": "3",
                "source_disposition": "cycle-cap-exhausted",
                "repair_session_ids": ["3.1"],
                "materialized": True,
                "result": "ok",
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name=events.REVIEW_REPAIR_REOPENED_EVENT,
            payload={
                "source_cycle": 3,
                "source_review_session_id": "4",
                "source_disposition": "malformed-review-repair",
                "repair_session_ids": [],
                "materialized": False,
                "result": "malformed-review-repair",
                "error": "repair groups exceed the supported maximum",
            },
        )

        cycles = self._collect().review_cycles
        self.assertEqual(2, cycles["cycles_recorded"])
        self.assertEqual(2, cycles["repairs_materialized"])
        self.assertEqual(2, cycles["unique_materializations"])
        self.assertEqual(3, cycles["reopen_attempts"])
        self.assertEqual(2, cycles["successful_reopen_materializations"])
        self.assertEqual(1, cycles["reopen_failures"])
        self.assertEqual(
            {
                "cycle-cap-exhausted": 1,
                "malformed-review-repair": 1,
                "not-loop-ready": 1,
            },
            cycles["reopen_attempts_by_source_disposition"],
        )
        self.assertEqual(
            {"malformed-review-repair": 1},
            cycles["reopen_failures_by_result"],
        )

    def test_child_only_crash_materialization_is_undatable_in_any_window(
        self,
    ) -> None:
        plan_dir = self.tmp_path / "plan-child-only-materialization"
        _write_generated_review_repair_plan(plan_dir)
        state_dir = _make_state_dir(self.state_root, plan_dir)
        event_path = state_dir / events.EVENTS_FILE_NAME

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(plan_dir), event_path
        )

        # The evaluator stays the whole-history cap authority and still
        # recovers the child whose event append was lost. Metrics cannot: the
        # lost append is what carried the timestamp, so no window can place it.
        self.assertEqual(1, projected.historical_materializations)
        for since_days in (7, 3650):
            cycles = self._collect(since_days=since_days).review_cycles
            self.assertEqual(0, cycles["unique_materializations"])
            self.assertEqual(0, cycles["repairs_materialized"])
            self.assertEqual(0, cycles["cycles_recorded"])

    def test_materializations_reconcile_legacy_cycle_and_child_once(
        self,
    ) -> None:
        plan_dir = self.tmp_path / "plan-legacy-materialization"
        _write_generated_review_repair_plan(plan_dir)
        state_dir = _make_state_dir(self.state_root, plan_dir)
        event_path = state_dir / events.EVENTS_FILE_NAME
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=24 * 30),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "cycle": 1,
                "disposition": "repairs-materialized",
                "fingerprints": ["finding:legacy"],
            },
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(plan_dir), event_path
        )
        wide = self._collect(since_days=60).review_cycles
        narrow = self._collect().review_cycles

        self.assertEqual(1, projected.historical_materializations)
        self.assertEqual(
            projected.historical_materializations,
            wide["unique_materializations"],
        )
        self.assertEqual(1, wide["repairs_materialized"])
        # One cycle event plus its durable child still reconcile to a single
        # materialization; a window that excludes the event excludes both.
        self.assertEqual(1, wide["cycles_recorded"])
        self.assertEqual(0, narrow["unique_materializations"])
        self.assertEqual(0, narrow["cycles_recorded"])
        self.assertEqual({}, narrow["by_disposition"])

    def test_materializations_shrink_with_a_narrower_window(self) -> None:
        recent_dir = _make_state_dir(
            self.state_root, self.tmp_path / "plan-window-recent"
        )
        older_dir = _make_state_dir(
            self.state_root, self.tmp_path / "plan-window-older"
        )
        _append_event(
            recent_dir,
            created_at=_iso(self.now, hours_ago=2),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "repairs-materialized",
                "repair_session_ids": ["2.1"],
            },
        )
        _append_event(
            older_dir,
            created_at=_iso(self.now, hours_ago=24 * 10),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "repairs-materialized",
                "repair_session_ids": ["2.1"],
            },
        )
        _append_event(
            older_dir,
            created_at=_iso(self.now, hours_ago=24 * 30),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "cycle": 2,
                "review_session_id": "3",
                "disposition": "repairs-materialized",
                "repair_session_ids": ["3.1"],
            },
        )

        narrow = self._collect(since_days=3).review_cycles
        wide = self._collect(since_days=45).review_cycles

        self.assertEqual(1, narrow["unique_materializations"])
        self.assertEqual(3, wide["unique_materializations"])
        self.assertLess(
            narrow["unique_materializations"], wide["unique_materializations"]
        )
        self.assertEqual(
            narrow["unique_materializations"], narrow["repairs_materialized"]
        )
        self.assertEqual(wide["unique_materializations"], wide["repairs_materialized"])

    def test_state_dirs_untouched_in_window_are_skipped_before_events(
        self,
    ) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-stale")
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=1),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "repairs-materialized",
                "repair_session_ids": ["2.1"],
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=1),
            name="run_paused",
            payload={"reason": "blocked", "details": {}},
        )
        stale = (self.now - timedelta(days=30)).timestamp()
        total_bytes = 0
        for path in state_dir.rglob("*"):
            if path.is_file():
                total_bytes += path.stat().st_size
            os.utime(path, (stale, stale))

        report = self._collect()

        # File mtimes are the cheap in-window probe: nothing here was written
        # inside the window, so the events file is never read.
        self.assertEqual(1, report.state_dirs_scanned)
        self.assertEqual(0, report.state_dirs_active)
        self.assertEqual(0, report.pauses["total"])
        self.assertEqual(0, report.review_cycles["cycles_recorded"])
        self.assertEqual(0, report.review_cycles["unique_materializations"])
        self.assertEqual(total_bytes, report.artifacts["total_bytes"])
        self.assertEqual(0, report.artifacts["grown_bytes"])
        self.assertEqual(0, report.artifacts["grown_files"])

    def test_recovered_reopen_supplies_lineage_not_attempt_activity(
        self,
    ) -> None:
        plan_dir = self.tmp_path / "plan-recovered-reopen"
        _write_generated_review_repair_plan(plan_dir)
        state_dir = _make_state_dir(self.state_root, plan_dir)
        event_path = state_dir / events.EVENTS_FILE_NAME
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name=events.REVIEW_REPAIR_REOPENED_EVENT,
            payload={
                "source_cycle": 1,
                "source_review_session_id": "2",
                "source_disposition": "cycle-cap-exhausted",
                "repair_session_ids": ["2.1"],
                "materialized": True,
                "recovered": True,
                "result": "recovered-existing-children",
            },
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(plan_dir), event_path
        )
        cycles = self._collect().review_cycles

        self.assertEqual(1, projected.historical_materializations)
        self.assertEqual(
            projected.historical_materializations,
            cycles["unique_materializations"],
        )
        self.assertEqual(1, cycles["repairs_materialized"])
        self.assertEqual(0, cycles["reopen_attempts"])
        self.assertEqual(0, cycles["successful_reopen_materializations"])
        self.assertEqual(0, cycles["reopen_failures"])
        self.assertEqual({}, cycles["reopen_attempts_by_source_disposition"])
        self.assertEqual({}, cycles["reopen_failures_by_result"])

    def test_compaction_projection_covers_only_active_dirs(self) -> None:
        active_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-g")
        fat_checks = [
            {
                "name": "check-1",
                "kind": "command",
                "passed": False,
                "stdout": "x" * (64 * 1024),
                "stderr": "y" * (64 * 1024),
                "stdout_sha256": "",
                "stderr_sha256": "",
            }
        ]
        _write_turn_result(
            active_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=3),
            finished_at=_iso(self.now, hours_ago=2.9),
            extra={"verification": {"checks": fat_checks, "failures": fat_checks}},
        )
        idle_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-h")
        _write_turn_result(
            idle_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=24 * 60),
            finished_at=_iso(self.now, hours_ago=24 * 60 - 0.1),
        )
        report = self._collect()
        self.assertEqual(2, report.state_dirs_scanned)
        self.assertEqual(1, report.state_dirs_active)
        compaction = report.compaction
        self.assertEqual(1, compaction["dirs_projected"])
        self.assertEqual([], compaction["dirs_skipped"])
        self.assertGreater(compaction["projected_bytes_reclaimed"], 0)
        # Projected reclaim is net of the gzip bytes the failed-check
        # artifacts would add, and the artifact figure is surfaced.
        self.assertGreater(compaction["artifact_bytes_added"], 0)
        self.assertEqual(
            compaction["projected_bytes_reclaimed"],
            compaction["bytes_before"]
            - compaction["bytes_after"]
            - compaction["artifact_bytes_added"],
        )
        self.assertGreater(report.artifacts["total_bytes"], 0)
        self.assertGreater(report.artifacts["grown_bytes"], 0)

    def test_active_dir_without_plan_dir_is_skipped_for_compaction(self) -> None:
        state_dir = self.state_root / "orphaned"
        state_dir.mkdir()
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=1),
            finished_at=_iso(self.now, hours_ago=0.9),
        )
        report = self._collect()
        self.assertEqual(["orphaned"], report.compaction["dirs_skipped"])
        self.assertEqual(0, report.compaction["dirs_projected"])

    def test_malformed_event_lines_and_results_are_tolerated(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-i")
        (state_dir / "events.jsonl").write_text(
            "not-json\n" + json.dumps([1, 2]) + "\n", encoding="utf-8"
        )
        turn_dir = state_dir / "runs" / "run-1" / "turn-0001-session-1"
        turn_dir.mkdir(parents=True)
        (turn_dir / "result.json").write_text("{broken", encoding="utf-8")
        report = self._collect()
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])


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


class MetricsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_metrics_json_defaults_to_seven_days(self) -> None:
        exit_code, stdout, _ = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root), "--json"]
        )
        self.assertEqual(0, exit_code)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("metrics", payload["command"])
        self.assertEqual(metrics.DEFAULT_SINCE_DAYS, payload["since_days"])
        self.assertEqual(7, metrics.DEFAULT_SINCE_DAYS)
        for section in (
            "turns",
            "first_pass",
            "pauses",
            "verification_repair",
            "review_cycles",
            "artifacts",
            "compaction",
            "assurance",
        ):
            self.assertIn(section, payload)

    def test_metrics_text_output_reports_sections(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-a")
        now = datetime.now(UTC)
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(now, hours_ago=2),
            finished_at=_iso(now, hours_ago=1.9),
        )
        exit_code, stdout, _ = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root)]
        )
        self.assertEqual(0, exit_code)
        self.assertIn("First-pass completion", stdout)
        self.assertIn("Verification repair", stdout)
        self.assertIn("Review cycles", stdout)
        self.assertIn("unique materializations", stdout)
        self.assertIn("reopen attempts", stdout)
        self.assertIn("Compaction", stdout)
        self.assertIn("Supervisor audit:", stdout)
        self.assertIn("recurrence after resolution", stdout)

    def test_metrics_text_reports_what_the_audit_found(self) -> None:
        now = datetime.now(UTC)
        _publish_audit(self.state_root, recorded_at=_iso(now, hours_ago=2))

        exit_code, stdout, _ = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root)]
        )

        self.assertEqual(0, exit_code)
        self.assertIn("states all-clear 0, findings 1", stdout)
        self.assertIn("1 sighting(s), 1 new, 0 recurring, 1 open now", stdout)

    def test_metrics_rejects_invalid_since_days(self) -> None:
        exit_code, stdout, _ = self._run_cli(
            [
                "metrics",
                "--state-dir",
                str(self.state_root),
                "--since-days",
                "0",
                "--json",
            ]
        )
        self.assertEqual(1, exit_code)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("since-days", payload["error"])


def _trace_record(
    *,
    event: str,
    invocation_id: str,
    timestamp: str,
    command_path: list[str] | None = None,
    options: list[str] | None = None,
    help_depth: str | None = None,
    harness: str | None = None,
    origin: str = cli_trace.ORIGIN_DIRECT,
    **end_fields: object,
) -> dict:
    record = {
        "schema_version": cli_trace.SCHEMA_VERSION,
        "event": event,
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "pid": 4242,
        "command_path": command_path or [],
        "options": options or [],
        "help_depth": help_depth,
        "harness": harness,
        "origin": origin,
    }
    record.update(end_fields)
    return record


class CliInvocationMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.now = datetime.now(UTC)

    def _write(self, *, days_ago: int, lines: list[str]) -> Path:
        day = (self.now - timedelta(days=days_ago)).date()
        path = self.root / cli_trace.trace_file_name(day)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _collect(self, *, since_days: int = 7, exclude: str | None = None) -> dict:
        return metrics.collect_cli_invocations(
            self.root,
            cutoff=self.now - timedelta(days=since_days),
            exclude_invocation_id=exclude,
        )

    def _pair(
        self,
        invocation_id: str,
        *,
        hours_ago: float,
        outcome: str = cli_trace.OUTCOME_OK,
        exit_code: int | None = 0,
        duration_ms: float = 100.0,
        **shape: object,
    ) -> list[str]:
        start = _trace_record(
            event=cli_trace.EVENT_START,
            invocation_id=invocation_id,
            timestamp=_iso(self.now, hours_ago=hours_ago),
            **shape,
        )
        end = dict(start)
        end.update(
            {
                "event": cli_trace.EVENT_END,
                "timestamp": _iso(self.now, hours_ago=hours_ago - 0.01),
                "exit_code": exit_code,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "exception_type": None,
            }
        )
        return [json.dumps(start, sort_keys=True), json.dumps(end, sort_keys=True)]

    def test_an_empty_trace_still_reports_root_and_retention(self) -> None:
        section = self._collect()

        self.assertEqual(str(self.root), section["trace_root"])
        self.assertEqual(cli_trace.RETENTION_DAYS, section["retention_days"])
        self.assertEqual(0, section["starts"])
        self.assertEqual(0, section["completions"])
        self.assertIsNone(section["durations_ms"]["median"])
        self.assertEqual([], section["recent_failures"])

    def test_lifecycle_outcomes_are_counted_separately(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair("ok", hours_ago=5, command_path=["status"]),
                *self._pair(
                    "bad",
                    hours_ago=4,
                    command_path=["run"],
                    outcome=cli_trace.OUTCOME_FAILED,
                    exit_code=1,
                ),
                *self._pair(
                    "usage",
                    hours_ago=3,
                    outcome=cli_trace.OUTCOME_USAGE,
                    exit_code=2,
                ),
                *self._pair(
                    "help",
                    hours_ago=2,
                    outcome=cli_trace.OUTCOME_HELP,
                    help_depth=cli_trace.HELP_ROOT,
                ),
                *self._pair(
                    "sigint",
                    hours_ago=1,
                    outcome=cli_trace.OUTCOME_INTERRUPTED,
                    exit_code=130,
                ),
                *self._pair(
                    "boom",
                    hours_ago=0.5,
                    outcome=cli_trace.OUTCOME_EXCEPTION,
                    exit_code=None,
                ),
            ],
        )

        section = self._collect()

        self.assertEqual(6, section["starts"])
        self.assertEqual(6, section["completions"])
        self.assertEqual(1, section["successes"])
        self.assertEqual(1, section["nonzero_exits"])
        self.assertEqual(1, section["usage_errors"])
        self.assertEqual(1, section["help_calls"])
        self.assertEqual(1, section["interruptions"])
        self.assertEqual(1, section["exceptions"])
        self.assertEqual(0, section["unmatched_starts"])

    def test_grouped_counts_cover_every_documented_dimension(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair(
                    "a",
                    hours_ago=3,
                    command_path=["workbench", "serve"],
                    harness="claude",
                    origin=cli_trace.ORIGIN_WORKBENCH,
                ),
                *self._pair(
                    "b",
                    hours_ago=2,
                    command_path=["run"],
                    origin=cli_trace.ORIGIN_AUTOPILOT,
                    outcome=cli_trace.OUTCOME_FAILED,
                    exit_code=3,
                ),
                *self._pair(
                    "c",
                    hours_ago=1,
                    outcome=cli_trace.OUTCOME_HELP,
                    help_depth=cli_trace.HELP_LEAF,
                ),
            ],
        )

        section = self._collect()

        self.assertEqual(
            {"(root)": 1, "run": 1, "workbench serve": 1}, section["by_command"]
        )
        self.assertEqual({cli_trace.HELP_LEAF: 1}, section["by_help_depth"])
        self.assertEqual({"0": 2, "3": 1}, section["by_exit_code"])
        self.assertEqual({"claude": 1, "unknown": 2}, section["by_harness"])
        self.assertEqual(
            {
                cli_trace.ORIGIN_AUTOPILOT: 1,
                cli_trace.ORIGIN_DIRECT: 1,
                cli_trace.ORIGIN_WORKBENCH: 1,
            },
            section["by_origin"],
        )

    def test_a_null_exit_code_is_grouped_without_being_dropped(self) -> None:
        self._write(
            days_ago=0,
            lines=self._pair(
                "boom",
                hours_ago=1,
                outcome=cli_trace.OUTCOME_EXCEPTION,
                exit_code=None,
            ),
        )

        self.assertEqual({"none": 1}, self._collect()["by_exit_code"])

    def test_durations_summarize_only_completed_invocations(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair("a", hours_ago=4, duration_ms=10.0),
                *self._pair("b", hours_ago=3, duration_ms=30.0),
                *self._pair("c", hours_ago=2, duration_ms=90.0),
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_START,
                        invocation_id="killed",
                        timestamp=_iso(self.now, hours_ago=1),
                    ),
                    sort_keys=True,
                ),
            ],
        )

        durations = self._collect()["durations_ms"]

        self.assertEqual(3, durations["count"])
        self.assertEqual(30.0, durations["median"])
        self.assertEqual(90.0, durations["p95"])
        self.assertEqual(90.0, durations["max"])
        self.assertEqual(130.0, durations["total"])

    def test_a_killed_process_is_reported_as_an_unmatched_start(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_START,
                        invocation_id="killed",
                        timestamp=_iso(self.now, hours_ago=1),
                        command_path=["run"],
                    ),
                    sort_keys=True,
                )
            ],
        )

        section = self._collect()

        self.assertEqual(1, section["starts"])
        self.assertEqual(0, section["completions"])
        self.assertEqual(1, section["unmatched_starts"])
        self.assertEqual({"run": 1}, section["by_command"])

    def test_an_end_whose_start_was_pruned_is_still_summarized(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_END,
                        invocation_id="orphan",
                        timestamp=_iso(self.now, hours_ago=1),
                        command_path=["gc"],
                        exit_code=1,
                        outcome=cli_trace.OUTCOME_FAILED,
                        duration_ms=25.0,
                        exception_type=None,
                    ),
                    sort_keys=True,
                )
            ],
        )

        section = self._collect()

        self.assertEqual(0, section["starts"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(1, section["nonzero_exits"])
        self.assertEqual({"gc": 1}, section["by_command"])

    def test_a_pair_split_across_midnight_is_counted_once(self) -> None:
        yesterday = _trace_record(
            event=cli_trace.EVENT_START,
            invocation_id="overnight",
            timestamp=_iso(self.now, hours_ago=25),
            command_path=["run"],
        )
        today = dict(yesterday)
        today.update(
            {
                "event": cli_trace.EVENT_END,
                "timestamp": _iso(self.now, hours_ago=23),
                "exit_code": 0,
                "outcome": cli_trace.OUTCOME_OK,
                "duration_ms": 7200000.0,
                "exception_type": None,
            }
        )
        self._write(days_ago=1, lines=[json.dumps(yesterday, sort_keys=True)])
        self._write(days_ago=0, lines=[json.dumps(today, sort_keys=True)])

        section = self._collect()

        self.assertEqual(1, section["starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual({"run": 1}, section["by_command"])

    def test_duplicate_records_never_inflate_the_totals(self) -> None:
        pair = self._pair("dup", hours_ago=1, command_path=["status"])
        self._write(days_ago=0, lines=[*pair, *pair])

        section = self._collect()

        self.assertEqual(4, section["records_read"])
        self.assertEqual(1, section["starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(1, section["successes"])
        self.assertEqual({"status": 1}, section["by_command"])

    def test_malformed_and_unusable_records_degrade_the_sample_only(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                "not json at all",
                "[]",
                json.dumps({"event": "start", "timestamp": "now"}),
                json.dumps({"event": "sideways", "invocation_id": "x"}),
                json.dumps(
                    {
                        "event": cli_trace.EVENT_START,
                        "invocation_id": "undated",
                        "timestamp": "not-a-timestamp",
                    }
                ),
                *self._pair("good", hours_ago=1, command_path=["status"]),
            ],
        )

        section = self._collect()

        # Seven non-blank lines: one malformed, one non-object, three unusable
        # dictionaries, and the two halves of the only usable invocation.
        self.assertEqual(7, section["records_read"])
        self.assertEqual(5, section["records_ignored"])
        self.assertEqual(1, section["starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(1, section["successes"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual({"status": 1}, section["by_command"])

    def test_an_invocation_is_windowed_by_its_start(self) -> None:
        self._write(days_ago=0, lines=self._pair("recent", hours_ago=1))
        self._write(days_ago=3, lines=self._pair("older", hours_ago=72))

        wide = self._collect(since_days=7)
        narrow = self._collect(since_days=1)

        self.assertEqual(2, wide["starts"])
        self.assertEqual(1, narrow["starts"])
        self.assertLessEqual(narrow["completions"], wide["completions"])

    def test_recent_failures_are_bounded_newest_first_and_content_free(self) -> None:
        lines: list[str] = []
        for index in range(metrics.RECENT_FAILURE_LIMIT + 4):
            lines.extend(
                self._pair(
                    f"fail-{index:02d}",
                    hours_ago=float(index + 1),
                    command_path=["run"],
                    options=["--json"],
                    origin=cli_trace.ORIGIN_AUTOPILOT,
                    outcome=cli_trace.OUTCOME_FAILED,
                    exit_code=1,
                )
            )
        lines.extend(self._pair("fine", hours_ago=0.5, command_path=["status"]))
        self._write(days_ago=0, lines=lines)

        failures = self._collect()["recent_failures"]

        self.assertEqual(metrics.RECENT_FAILURE_LIMIT, len(failures))
        self.assertEqual("fail-00", failures[0]["invocation_id"])
        self.assertNotIn("fine", {entry["invocation_id"] for entry in failures})
        for entry in failures:
            self.assertEqual(set(metrics.RECENT_FAILURE_FIELDS), set(entry))
            self.assertEqual("run", entry["command"])
            self.assertEqual(["--json"], entry["options"])
            self.assertEqual(cli_trace.ORIGIN_AUTOPILOT, entry["origin"])

    def test_every_failing_outcome_reaches_recent_failures(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair("ok", hours_ago=5),
                *self._pair(
                    "bad", hours_ago=4, outcome=cli_trace.OUTCOME_FAILED, exit_code=1
                ),
                *self._pair(
                    "usage", hours_ago=3, outcome=cli_trace.OUTCOME_USAGE, exit_code=2
                ),
                *self._pair(
                    "sigint",
                    hours_ago=2,
                    outcome=cli_trace.OUTCOME_INTERRUPTED,
                    exit_code=130,
                ),
                *self._pair(
                    "boom",
                    hours_ago=1,
                    outcome=cli_trace.OUTCOME_EXCEPTION,
                    exit_code=None,
                ),
                *self._pair(
                    "help", hours_ago=0.5, outcome=cli_trace.OUTCOME_HELP, exit_code=0
                ),
            ],
        )

        failures = self._collect()["recent_failures"]
        reported = {entry["invocation_id"] for entry in failures}

        self.assertEqual({"bad", "usage", "sigint", "boom"}, reported)

    def test_the_excluded_invocation_is_dropped_entirely(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_START,
                        invocation_id="self",
                        timestamp=_iso(self.now, hours_ago=0.1),
                        command_path=["metrics"],
                    ),
                    sort_keys=True,
                ),
                *self._pair("other", hours_ago=1, command_path=["status"]),
            ],
        )

        section = self._collect(exclude="self")

        self.assertEqual(1, section["starts"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual({"status": 1}, section["by_command"])


class CliInvocationsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = cli.main(argv)
        return exit_code, stdout.getvalue()

    def _seed_failure(self) -> None:
        now = datetime.now(UTC)
        start = _trace_record(
            event=cli_trace.EVENT_START,
            invocation_id="seeded",
            timestamp=_iso(now, hours_ago=1),
            command_path=["run"],
            origin=cli_trace.ORIGIN_WORKBENCH,
        )
        end = dict(start)
        end.update(
            {
                "event": cli_trace.EVENT_END,
                "timestamp": _iso(now, hours_ago=0.9),
                "exit_code": 1,
                "outcome": cli_trace.OUTCOME_FAILED,
                "duration_ms": 1500.0,
                "exception_type": None,
            }
        )
        path = self.state_root / cli_trace.trace_file_name(now.date())
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in (start, end))
            + "\n",
            encoding="utf-8",
        )

    def test_json_carries_cli_invocations_beside_the_existing_sections(self) -> None:
        self._seed_failure()

        exit_code, stdout = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root), "--json"]
        )

        self.assertEqual(0, exit_code)
        payload = json.loads(stdout)
        for section in (
            "turns",
            "first_pass",
            "pauses",
            "verification_repair",
            "review_cycles",
            "agent_mux_delivery",
            "artifacts",
            "compaction",
            "assurance",
            "cli_invocations",
        ):
            self.assertIn(section, payload)
        invocations = payload["cli_invocations"]
        self.assertEqual(payload["state_root"], invocations["trace_root"])
        self.assertEqual(cli_trace.RETENTION_DAYS, invocations["retention_days"])
        self.assertEqual(1, invocations["nonzero_exits"])
        self.assertEqual({cli_trace.ORIGIN_WORKBENCH: 1}, invocations["by_origin"])
        self.assertEqual(
            ["seeded"],
            [entry["invocation_id"] for entry in invocations["recent_failures"]],
        )

    def test_text_output_adds_a_cli_invocations_section(self) -> None:
        self._seed_failure()

        exit_code, stdout = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root)]
        )

        self.assertEqual(0, exit_code)
        self.assertIn("CLI invocations:", stdout)
        self.assertIn(f"retention {cli_trace.RETENTION_DAYS} day(s)", stdout)
        self.assertIn("unmatched starts 0", stdout)
        self.assertIn("nonzero exits 1", stdout)
        self.assertIn("by origin: workbench 1", stdout)
        self.assertIn("recent failures (1):", stdout)
        # Existing sections stay put: the block is additive, not a replacement.
        self.assertIn("First-pass completion", stdout)
        self.assertIn("Supervisor audit:", stdout)

    def test_metrics_excludes_its_own_traced_invocation(self) -> None:
        self._seed_failure()

        with mock.patch.dict(os.environ, {cli_trace.TRACE_ENV_VAR: "1"}):
            exit_code, stdout = self._run_cli(
                ["metrics", "--state-dir", str(self.state_root), "--json"]
            )

        self.assertEqual(0, exit_code)
        invocations = json.loads(stdout)["cli_invocations"]
        self.assertEqual(0, invocations["unmatched_starts"])
        self.assertEqual({"run": 1}, invocations["by_command"])
        written = cli_trace.read_corpus(self.state_root).records
        self.assertIn(
            ["metrics"],
            [record["command_path"] for record in written],
        )


if __name__ == "__main__":
    unittest.main()
