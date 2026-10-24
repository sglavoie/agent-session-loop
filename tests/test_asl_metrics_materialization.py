"""Workflow metrics tests for review materializations and compaction."""

from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta

from agent_session_loop import assurance_projection as assurance_projection
from agent_session_loop import (
    events,
    plans_snapshot,
    review_continuation,
)
from tests.asl_metrics_support import (
    MetricsCollectionBase,
    _append_event,
    _iso,
    _make_state_dir,
    _write_generated_review_repair_plan,
    _write_turn_result,
)

ASL_TEST_PORTABLE = True


class MetricsMaterializationTests(MetricsCollectionBase):
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
            plans_snapshot.inspect_plan(plan_dir), event_path
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
            plans_snapshot.inspect_plan(plan_dir), event_path
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
            plans_snapshot.inspect_plan(plan_dir), event_path
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


if __name__ == "__main__":
    unittest.main()
