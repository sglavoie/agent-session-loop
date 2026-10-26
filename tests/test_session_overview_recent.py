"""ScanRecentRowsTests, BuildSessionsPayloadTests coverage."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import plans_files, session_overview
from tests.session_overview_support import (
    DEAD_PID,
    _make_plan_state_dir,
    _make_turn_artifact,
    _turn_result_payload,
)


class ScanRecentRowsTests(unittest.TestCase):
    def test_recent_rows_sorted_newest_finish_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "alpha"
            plan_dir.mkdir(parents=True)
            state_dir = _make_plan_state_dir(state_root, plan_dir)

            for index, finished_at in enumerate(
                [
                    "2026-06-19T03:00:00+00:00",
                    "2026-06-19T01:00:00+00:00",
                    "2026-06-19T02:00:00+00:00",
                ],
                start=1,
            ):
                _make_turn_artifact(
                    state_dir,
                    run_id="run-1",
                    turn_label=f"{index:04d}",
                    payload=_turn_result_payload(
                        plan_dir=plans_files.canonicalize_path(plan_dir),
                        run_id="run-1",
                        turn_index=index,
                        session_id=str(index),
                        finished_at=finished_at,
                    ),
                )

            rows, skipped = session_overview.scan_recent_rows(state_root)

            self.assertEqual((), skipped)
            self.assertEqual(3, len(rows))
            self.assertEqual(
                [
                    "2026-06-19T03:00:00+00:00",
                    "2026-06-19T02:00:00+00:00",
                    "2026-06-19T01:00:00+00:00",
                ],
                [row.finished_at for row in rows],
            )

    def test_recent_rows_limit_truncates_after_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "alpha"
            plan_dir.mkdir(parents=True)
            state_dir = _make_plan_state_dir(state_root, plan_dir)

            finished_times = [
                "2026-06-19T05:00:00+00:00",
                "2026-06-19T04:00:00+00:00",
                "2026-06-19T03:00:00+00:00",
                "2026-06-19T02:00:00+00:00",
                "2026-06-19T01:00:00+00:00",
            ]
            for index, finished_at in enumerate(finished_times, start=1):
                _make_turn_artifact(
                    state_dir,
                    run_id="run-1",
                    turn_label=f"{index:04d}",
                    payload=_turn_result_payload(
                        plan_dir=plans_files.canonicalize_path(plan_dir),
                        run_id="run-1",
                        turn_index=index,
                        session_id=str(index),
                        finished_at=finished_at,
                    ),
                )

            rows, skipped = session_overview.scan_recent_rows(state_root, limit=2)

            self.assertEqual((), skipped)
            self.assertEqual(2, len(rows))
            self.assertEqual(
                ["2026-06-19T05:00:00+00:00", "2026-06-19T04:00:00+00:00"],
                [row.finished_at for row in rows],
            )

    def test_recent_rows_limit_zero_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "alpha"
            plan_dir.mkdir(parents=True)
            state_dir = _make_plan_state_dir(state_root, plan_dir)
            _make_turn_artifact(
                state_dir,
                run_id="run-1",
                turn_label="0001",
                payload=_turn_result_payload(
                    plan_dir=plans_files.canonicalize_path(plan_dir),
                    run_id="run-1",
                    turn_index=1,
                    session_id="1",
                    finished_at="2026-06-19T00:00:00+00:00",
                ),
            )

            rows, skipped = session_overview.scan_recent_rows(state_root, limit=0)

            self.assertEqual((), rows)
            self.assertEqual((), skipped)

    def test_recent_rows_negative_limit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            with self.assertRaisesRegex(ValueError, "limit must be non-negative"):
                session_overview.scan_recent_rows(state_root, limit=-1)

    def test_plan_path_fallback_from_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "fallback"
            plan_dir.mkdir(parents=True)
            state_dir = _make_plan_state_dir(state_root, plan_dir)

            _make_turn_artifact(
                state_dir,
                run_id="run-1",
                turn_label="0001",
                payload=_turn_result_payload(
                    plan_dir=plans_files.canonicalize_path(plan_dir),
                    run_id="run-1",
                    turn_index=1,
                    session_id="1",
                    finished_at="2026-06-19T00:00:00+00:00",
                    include_plan_dir=False,
                ),
            )

            rows, skipped = session_overview.scan_recent_rows(state_root)

            self.assertEqual((), skipped)
            self.assertEqual(1, len(rows))
            self.assertEqual(plans_files.canonicalize_path(plan_dir), rows[0].plan_dir)

    def test_unresolvable_plan_dir_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            stub_state_dir = state_root / ("deadbeef" * 8)
            stub_state_dir.mkdir(parents=True)

            _make_turn_artifact(
                stub_state_dir,
                run_id="run-1",
                turn_label="0001",
                payload={
                    "run_id": "run-1",
                    "turn_index": 1,
                    "session_id": "1",
                    "harness": "claude",
                    "finished_at": "2026-06-19T00:00:00+00:00",
                    "status_result": "complete",
                },
            )

            rows, skipped = session_overview.scan_recent_rows(state_root)

            self.assertEqual((), rows)
            self.assertEqual(1, len(skipped))
            self.assertIn("unable to resolve plan directory", skipped[0].reason)

    def test_malformed_artifact_reported_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "alpha"
            plan_dir.mkdir(parents=True)
            state_dir = _make_plan_state_dir(state_root, plan_dir)
            run_dir = state_dir / session_overview.RUNS_DIR_NAME / "run-1"
            turn_dir = run_dir / "turn-0001"
            turn_dir.mkdir(parents=True)
            (turn_dir / session_overview.RESULT_JSON_NAME).write_text(
                "not json",
                encoding="utf-8",
            )
            _make_turn_artifact(
                state_dir,
                run_id="run-1",
                turn_label="0002",
                payload=_turn_result_payload(
                    plan_dir=plans_files.canonicalize_path(plan_dir),
                    run_id="run-1",
                    turn_index=2,
                    session_id="2",
                    finished_at="2026-06-19T01:00:00+00:00",
                ),
            )

            rows, skipped = session_overview.scan_recent_rows(state_root)

            self.assertEqual(1, len(rows))
            self.assertEqual(2, rows[0].turn_index)
            self.assertEqual(1, len(skipped))
            self.assertTrue(str(skipped[0].path).endswith("/result.json"))


class BuildSessionsPayloadTests(unittest.TestCase):
    def test_payload_counts_match_emitted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            running_row = session_overview.RunningRow(
                kind="turn",
                plan_dir=Path("/plans/alpha"),
                state_dir=Path("/state/alpha"),
                run_id="run-1",
                harness="claude",
                session_id="2",
                turn_index=3,
                pid=os.getpid(),
                started_at="2026-06-19T00:00:00+00:00",
                elapsed_seconds=5.0,
                idle_seconds=0.0,
                last_activity_at="2026-06-19T00:00:00+00:00",
                lock_active=True,
                lock_stale=False,
            )
            recent_row = session_overview.RecentRow(
                plan_dir=Path("/plans/alpha"),
                state_dir=Path("/state/alpha"),
                run_id="run-1",
                harness="claude",
                session_id="2",
                turn_index=3,
                status="complete",
                finished_at="2026-06-19T00:00:00+00:00",
                duration_seconds=1.0,
                artifact_dir=Path("/state/alpha/runs/run-1/turn-0003"),
            )
            stale_lock = session_overview.StaleLock(
                plan_dir="/plans/beta",
                lock_path=Path("/state/beta/lock.json"),
                pid=DEAD_PID,
                reason=f"pid {DEAD_PID} is not running",
                created_at="2026-06-19T00:00:00+00:00",
            )
            skipped = session_overview.SkippedArtifact(
                path=Path("/state/gamma/runs/run-2/turn-0001/result.json"),
                reason="not json",
            )

            payload = session_overview.build_sessions_payload(
                state_root_path=state_root,
                show_running=True,
                show_recent=True,
                limit=20,
                running_rows=(running_row,),
                recent_rows=(recent_row,),
                stale_locks=(stale_lock,),
                skipped=(skipped,),
            )

            self.assertTrue(payload["ok"])
            self.assertEqual("sessions", payload["command"])
            self.assertEqual(str(state_root), payload["state_root"])
            self.assertEqual({"running": True, "recent": True}, payload["filters"])
            self.assertEqual(20, payload["limit"])
            self.assertEqual(
                {"running": 1, "recent": 1, "stale_locks": 1, "skipped": 1},
                payload["counts"],
            )
            self.assertEqual(1, len(payload["running"]))
            self.assertEqual(1, len(payload["recent"]))
            self.assertEqual(
                {
                    "kind",
                    "plan_dir",
                    "state_dir",
                    "run_id",
                    "harness",
                    "session_id",
                    "turn_index",
                    "pid",
                    "started_at",
                    "elapsed_seconds",
                    "idle_seconds",
                    "last_activity_at",
                    "lock_active",
                    "lock_stale",
                    "check_index",
                    "check_count",
                    "check_name",
                    "check_kind",
                    "cwd",
                    "workspace_root",
                    "registry_digest",
                    "repositories",
                },
                set(payload["running"][0].keys()),
            )
            self.assertEqual(
                {
                    "plan_dir",
                    "state_dir",
                    "run_id",
                    "harness",
                    "session_id",
                    "turn_index",
                    "status",
                    "finished_at",
                    "duration_seconds",
                    "artifact_dir",
                    "session_kind",
                    "cwd",
                    "workspace_root",
                    "registry_digest",
                    "repositories",
                },
                set(payload["recent"][0].keys()),
            )
            self.assertEqual(
                {"plan_dir", "lock_path", "pid", "reason", "created_at"},
                set(payload["stale_locks"][0].keys()),
            )
            self.assertEqual(
                {"path", "reason"},
                set(payload["skipped"][0].keys()),
            )

    def test_verification_running_row_payload_includes_check_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            running_row = session_overview.RunningRow(
                kind="verification",
                plan_dir=Path("/plans/alpha"),
                state_dir=Path("/state/alpha"),
                run_id="run-1",
                harness="claude",
                session_id="2",
                turn_index=3,
                pid=os.getpid(),
                started_at="2026-06-19T00:00:00+00:00",
                elapsed_seconds=5.0,
                idle_seconds=None,
                last_activity_at="2026-06-19T00:00:01+00:00",
                lock_active=True,
                lock_stale=False,
                check_index=0,
                check_count=2,
                check_name="operator-surface-tests",
                check_kind="command",
            )

            payload = session_overview.build_sessions_payload(
                state_root_path=state_root,
                show_running=True,
                show_recent=False,
                limit=20,
                running_rows=(running_row,),
                recent_rows=(),
                stale_locks=(),
                skipped=(),
            )

            self.assertEqual(1, len(payload["running"]))
            row = payload["running"][0]
            self.assertEqual("verification", row["kind"])
            self.assertEqual(0, row["check_index"])
            self.assertEqual(2, row["check_count"])
            self.assertEqual("operator-surface-tests", row["check_name"])
            self.assertEqual("command", row["check_kind"])

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                session_overview._print_text_sessions(payload, verbose=False)
            text = buffer.getvalue()
            self.assertIn("verification", text)
            self.assertIn("check 1/2 operator-surface-tests", text)


if __name__ == "__main__":
    unittest.main()
