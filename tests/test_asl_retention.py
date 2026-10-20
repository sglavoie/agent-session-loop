"""Tests for the run-retention projection in `agent_session_loop.retention`.

Covers the age cutoff boundary and every predicate that forbids selecting a
run: a live run, a locked plan directory, the newest run of a plan, unfinished
runs, and artifacts whose timestamps are malformed or missing. Also pins the
no-write contract of the projection itself, which the ordinary lock read would
otherwise break by reclaiming a lock it proved dead.
"""

from __future__ import annotations

import json
import os
import socket
import unittest
from datetime import timedelta

from agent_session_loop import (
    plans_files,
    retention_projection,
    state,
)
from tests.asl_retention_support import (
    DEAD_PID,
    NOW,
    RUN_NEW,
    RUN_OLD,
    RetentionTestBase,
    _stamp,
)


class RetentionSelectionTests(RetentionTestBase):
    """Age cutoff, byte accounting, and the projection's read-only contract."""

    def test_old_run_is_selected_and_newest_run_is_kept(self) -> None:
        old = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1), log_bytes=10)

        report = self._report()
        runs = self._runs_by_id(report)

        self.assertTrue(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, "")
        self.assertFalse(runs[RUN_NEW].reclaimable)
        self.assertEqual(runs[RUN_NEW].reason, retention_projection.RETAIN_NEWEST_RUN)
        self.assertEqual(report.reclaimable_runs, (runs[RUN_OLD],))
        self.assertEqual(report.reclaimable_bytes, runs[RUN_OLD].size_bytes)
        self.assertEqual(
            report.total_bytes, report.reclaimable_bytes + report.retained_bytes
        )
        self.assertTrue(old.is_dir(), "projection must not delete anything")

    def test_reclaimable_bytes_count_the_logs_inside_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=4096)
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertGreater(run.size_bytes, 4096)
        self.assertLess(run.size_bytes, 4096 + 2048)

    def test_age_cutoff_boundary_is_inclusive(self) -> None:
        self._write_run("20260705T120000000000Z-1", finished_at=_stamp(30))
        self._write_run("20260705T120001000000Z-2", finished_at=_stamp(30, seconds=-1))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        runs = self._runs_by_id(self._report())

        self.assertTrue(runs["20260705T120000000000Z-1"].reclaimable)
        self.assertFalse(runs["20260705T120001000000Z-2"].reclaimable)
        self.assertEqual(
            runs["20260705T120001000000Z-2"].reason,
            retention_projection.RETAIN_WITHIN_CUTOFF,
        )

    def test_max_age_days_moves_the_cutoff(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        strict = self._runs_by_id(self._report(max_age_days=0.5))
        relaxed = self._runs_by_id(self._report(max_age_days=90.0))

        self.assertTrue(strict[RUN_OLD].reclaimable)
        self.assertFalse(relaxed[RUN_OLD].reclaimable)
        self.assertEqual(
            relaxed[RUN_OLD].reason, retention_projection.RETAIN_WITHIN_CUTOFF
        )

    def test_run_finish_time_is_the_latest_turn(self) -> None:
        run_dir = self._write_run(RUN_OLD, finished_at=_stamp(45))
        late_turn = run_dir / "turn-0002-session-2"
        late_turn.mkdir()
        (late_turn / "result.json").write_text(
            json.dumps({"finished_at": _stamp(2)}), encoding="utf-8"
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention_projection.RETAIN_WITHIN_CUTOFF)
        self.assertEqual(run.finished_at, _stamp(2))

    def test_newest_run_is_kept_even_when_every_run_is_ancient(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(120))
        self._write_run(RUN_NEW, finished_at=_stamp(90))

        runs = self._runs_by_id(self._report())

        self.assertTrue(runs[RUN_OLD].reclaimable)
        self.assertFalse(runs[RUN_NEW].reclaimable)
        self.assertEqual(runs[RUN_NEW].reason, retention_projection.RETAIN_NEWEST_RUN)

    def test_plan_without_state_directory_reports_no_runs(self) -> None:
        report = retention_projection.plan_retention(
            self.tmp_path / "never-run",
            state_dir=self.state_root,
            now=NOW,
            live_runs=frozenset(),
        )

        self.assertEqual(report.runs, ())
        self.assertEqual(report.reclaimable_bytes, 0)

    def test_negative_max_age_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._report(max_age_days=-1.0)
        with self.assertRaises(ValueError):
            retention_projection.corpus_retention(self.state_root, max_age_days=-1.0)

    def test_report_serializes_for_the_cli(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        payload = self._report().to_dict()

        self.assertEqual(
            payload["plan_dir"], str(plans_files.canonicalize_path(self.plan_dir))
        )
        self.assertEqual(payload["state_dir"], str(self.plan_state))
        self.assertEqual(payload["run_count"], 2)
        self.assertEqual(payload["reclaimable_run_count"], 1)
        self.assertEqual(
            payload["max_age_days"], retention_projection.DEFAULT_MAX_AGE_DAYS
        )
        self.assertEqual(
            payload["cutoff_at"],
            (
                NOW - timedelta(days=retention_projection.DEFAULT_MAX_AGE_DAYS)
            ).isoformat(),
        )
        self.assertEqual([run["run_id"] for run in payload["runs"]], [RUN_OLD, RUN_NEW])
        json.dumps(payload)


class RetentionSafetyTests(RetentionTestBase):
    """Live runs, locked plan dirs, and runs that cannot be dated."""

    def test_live_run_is_never_selected(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        runs = self._runs_by_id(
            self._report(live_runs=frozenset({(str(self.plan_state), RUN_OLD)}))
        )

        self.assertFalse(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, retention_projection.RETAIN_LIVE_RUN)

    def test_live_run_keys_come_from_the_running_scan(self) -> None:
        plan_dir = self._make_plan("running-plan", run_id=RUN_NEW)
        self._write_lock(plan_dir=plan_dir, pid=os.getpid())
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=plan_dir)
        plan_state = state.plan_state_dir(plan_dir, self.state_root)

        keys = retention_projection.live_run_keys(self.state_root)

        self.assertIn((str(plan_state), RUN_NEW), keys)
        self.assertNotIn((str(self.plan_state), RUN_OLD), keys)

    def test_active_lock_retains_every_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_lock(pid=os.getpid())

        report = self._report()

        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(
            [run.reason for run in report.runs],
            [retention_projection.RETAIN_PLAN_LOCKED],
        )
        self.assertIn("active", report.runs[0].detail)

    def test_unreadable_lock_retains_every_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        lock_path = self._write_lock(pid=DEAD_PID)
        lock_path.write_text("{ not json", encoding="utf-8")

        report = self._report()

        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(report.runs[0].reason, retention_projection.RETAIN_PLAN_LOCKED)

    def test_stale_lock_retains_every_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        lock_path = self._write_lock(pid=DEAD_PID, hostname=socket.gethostname())

        report = self._report()

        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(report.runs[0].reason, retention_projection.RETAIN_PLAN_LOCKED)
        self.assertIn("stale", report.runs[0].detail)
        self.assertTrue(lock_path.exists(), "projection must not remove the lock")

    def test_run_without_turn_artifacts_is_unfinished(self) -> None:
        self._write_run(RUN_OLD, finished_at=None, write_result=False)
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention_projection.RETAIN_UNFINISHED)
        self.assertIsNone(run.finished_at)
        self.assertIsNone(run.age_seconds)

    def test_turn_without_finished_at_is_unfinished(self) -> None:
        self._write_run(RUN_OLD, finished_at=None)
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention_projection.RETAIN_UNFINISHED)

    def test_one_undated_turn_retains_the_whole_run(self) -> None:
        run_dir = self._write_run(RUN_OLD, finished_at=_stamp(45))
        undated_turn = run_dir / "turn-0002-session-2"
        undated_turn.mkdir()
        (undated_turn / "result.json").write_text(
            json.dumps({"finished_at": "not a timestamp"}), encoding="utf-8"
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention_projection.RETAIN_UNDATED)

    def test_unreadable_turn_artifact_retains_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=None, result_text="{ not json")
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention_projection.RETAIN_UNDATED)

    def test_non_object_turn_artifact_retains_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=None, result_text="[]")
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention_projection.RETAIN_UNDATED)


if __name__ == "__main__":
    unittest.main()
