"""Focused tests for the `asl sessions` overview scanners and renderer."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_session_loop import plans, session_overview, state


DEAD_PID = 4_242_424


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _make_plan_state_dir(
    state_root: Path,
    plan_dir: Path,
    *,
    lock: dict[str, object] | None = None,
    state_payload: dict[str, object] | None = None,
) -> Path:
    """Create a `<sha>/` state directory under `state_root` for a given plan dir.

    Writes a `state.json` containing the plan_dir so the recent scanner's
    fallback path resolution can find it. Optionally writes `lock.json`.
    """
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    state_dir = state_root / plans.path_sha256(canonical_plan_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"plan_dir": str(canonical_plan_dir)}
    if state_payload is not None:
        payload.update(state_payload)
    _write_json(state_dir / session_overview.STATE_JSON_NAME, payload)
    if lock is not None:
        lock_with_defaults = {
            "plan_dir": str(canonical_plan_dir),
            "created_at": "2026-06-19T00:00:00+00:00",
            **lock,
        }
        _write_json(state_dir / session_overview.LOCK_JSON_NAME, lock_with_defaults)
    return state_dir


def _make_turn_artifact(
    state_dir: Path,
    *,
    run_id: str,
    turn_label: str,
    payload: dict[str, object],
) -> Path:
    """Write `runs/<run_id>/turn-<turn_label>/result.json`."""
    turn_dir = (
        state_dir / session_overview.RUNS_DIR_NAME / run_id / f"turn-{turn_label}"
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    result_path = turn_dir / session_overview.RESULT_JSON_NAME
    result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return result_path


def _active_turn_payload(
    *,
    run_id: str = "20260619T000000Z-1",
    turn_index: int = 1,
    session_id: str = "1",
    started_at: str = "2026-06-19T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session": int(session_id) if session_id.isdigit() else session_id,
        "harness": "claude",
        "started_at": started_at,
        "elapsed_seconds": 12.5,
        "idle_seconds": 0.0,
        "last_activity_at": started_at,
    }


def _active_verification_payload(
    *,
    run_id: str = "20260619T000000Z-1",
    turn_index: int = 1,
    session_id: str = "1",
    started_at: str = "2026-06-19T00:00:00+00:00",
    check_index: int | None = 0,
    check_count: int = 2,
    check_name: str = "operator-surface-tests",
    check_kind: str = "command",
    check_started_at: str | None = "2026-06-19T00:00:01+00:00",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session": int(session_id) if session_id.isdigit() else session_id,
        "total_sessions": 5,
        "started_at": started_at,
        "elapsed_seconds": 5.5,
        "check_count": check_count,
        "check_names": [check_name, "operator-surface-compile"],
        "check_kinds": [check_kind, "command"],
        "current_check_index": check_index,
        "current_check_name": check_name,
        "current_check_kind": check_kind,
        "current_check_started_at": check_started_at,
        "check_elapsed_seconds": 1.5,
        "heartbeat_count": 0,
    }


def _turn_result_payload(
    *,
    plan_dir: Path,
    run_id: str,
    turn_index: int,
    session_id: str,
    finished_at: str,
    status_result: str = "complete",
    duration_seconds: float = 1.0,
    harness: str = "claude",
    include_plan_dir: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session": int(session_id) if session_id.isdigit() else session_id,
        "harness": harness,
        "status_result": status_result,
        "started_at": "2026-06-19T00:00:00+00:00",
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
    }
    if include_plan_dir:
        payload["after"] = {"plan_dir": str(plan_dir)}
        payload["before"] = {"plan_dir": str(plan_dir)}
    return payload


def _build_args(
    state_dir: Path | None,
    *,
    running: bool = False,
    recent: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    json_output: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        running=running,
        recent=recent,
        limit=(
            session_overview.DEFAULT_RECENT_LIMIT if limit is None else limit
        ),
        state_dir=str(state_dir) if state_dir is not None else None,
        verbose=verbose,
        json=json_output,
    )


def _force_pid_alive(pid: int) -> bool:
    """Treat only the current process as alive so dead PIDs are deterministic."""
    return pid == os.getpid()


class HelpersTests(unittest.TestCase):
    def test_parse_iso_timestamp_accepts_trailing_z(self) -> None:
        parsed = session_overview.parse_iso_timestamp("2026-06-19T01:02:03Z")

        self.assertIsNotNone(parsed)
        self.assertEqual(2026, parsed.year)
        self.assertEqual(0, parsed.utcoffset().total_seconds())

    def test_parse_iso_timestamp_returns_none_for_garbage(self) -> None:
        self.assertIsNone(session_overview.parse_iso_timestamp("not-a-date"))
        self.assertIsNone(session_overview.parse_iso_timestamp(None))
        self.assertIsNone(session_overview.parse_iso_timestamp(""))

    def test_plan_label_basename_by_default(self) -> None:
        self.assertEqual(
            "abc-123",
            session_overview.plan_label("/tmp/agent-plans/abc-123"),
        )

    def test_plan_label_verbose_returns_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            verbose_label = session_overview.plan_label(plan_dir, verbose=True)

            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)),
                verbose_label,
            )


class ScanRunningRowsTests(unittest.TestCase):
    def test_active_lock_emits_running_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "alpha"
            plan_dir.mkdir(parents=True)
            active_turn = _active_turn_payload(
                run_id="20260619T000000Z-1",
                turn_index=2,
                session_id="3",
            )
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={"pid": os.getpid()},
                state_payload={"active_turn": active_turn},
            )

            rows, stale_locks, skipped = session_overview.scan_running_rows(
                state_root
            )

            self.assertEqual((), stale_locks)
            self.assertEqual((), skipped)
            self.assertEqual(1, len(rows))
            row = rows[0]
            self.assertEqual("turn", row.kind)
            self.assertEqual(os.getpid(), row.pid)
            self.assertEqual("3", row.session_id)
            self.assertEqual(2, row.turn_index)
            self.assertEqual("claude", row.harness)
            self.assertTrue(row.lock_active)
            self.assertFalse(row.lock_stale)

    def test_active_verification_emits_verification_running_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "alpha-verify"
            plan_dir.mkdir(parents=True)
            active_verification = _active_verification_payload(
                run_id="20260619T000000Z-2",
                turn_index=3,
                session_id="2",
                check_index=1,
                check_count=2,
                check_name="operator-surface-compile",
            )
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={"pid": os.getpid()},
                state_payload={
                    "run_id": "20260619T000000Z-2",
                    "harness": "claude",
                    "active_turn": None,
                    "active_verification": active_verification,
                },
            )

            rows, stale_locks, skipped = session_overview.scan_running_rows(
                state_root
            )

            self.assertEqual((), stale_locks)
            self.assertEqual((), skipped)
            self.assertEqual(1, len(rows))
            row = rows[0]
            self.assertEqual("verification", row.kind)
            self.assertEqual("2", row.session_id)
            self.assertEqual(3, row.turn_index)
            self.assertEqual(1, row.check_index)
            self.assertEqual(2, row.check_count)
            self.assertEqual("operator-surface-compile", row.check_name)
            self.assertEqual("command", row.check_kind)
            self.assertIsNone(row.idle_seconds)
            self.assertEqual(5.5, row.elapsed_seconds)

    def test_active_verification_takes_priority_over_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "alpha-priority"
            plan_dir.mkdir(parents=True)
            active_turn = _active_turn_payload(session_id="2", turn_index=3)
            active_verification = _active_verification_payload(
                session_id="2", turn_index=3
            )
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={"pid": os.getpid()},
                state_payload={
                    "active_turn": active_turn,
                    "active_verification": active_verification,
                },
            )

            rows, _, _ = session_overview.scan_running_rows(state_root)

            self.assertEqual(1, len(rows))
            self.assertEqual("verification", rows[0].kind)

    def test_stale_lock_excluded_from_running_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "beta"
            plan_dir.mkdir(parents=True)
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={"pid": DEAD_PID},
            )

            with mock.patch.object(
                state, "is_pid_running", side_effect=_force_pid_alive
            ):
                rows, stale_locks, skipped = (
                    session_overview.scan_running_rows(state_root)
                )

            self.assertEqual((), rows)
            self.assertEqual((), skipped)
            self.assertEqual(1, len(stale_locks))
            stale = stale_locks[0]
            self.assertEqual(DEAD_PID, stale.pid)
            self.assertIn(str(DEAD_PID), stale.reason)

    def test_orphan_active_turn_without_lock_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "gamma"
            plan_dir.mkdir(parents=True)
            active_turn = _active_turn_payload(session_id="2")
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock=None,
                state_payload={"active_turn": active_turn},
            )

            rows, stale_locks, skipped = session_overview.scan_running_rows(
                state_root
            )

            self.assertEqual((), rows)
            self.assertEqual((), stale_locks)
            self.assertEqual((), skipped)

    def test_loop_kind_when_no_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "delta"
            plan_dir.mkdir(parents=True)
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={"pid": os.getpid()},
                state_payload={"run_id": "loop-run-id", "harness": "codex"},
            )

            rows, stale_locks, skipped = session_overview.scan_running_rows(
                state_root
            )

            self.assertEqual((), stale_locks)
            self.assertEqual((), skipped)
            self.assertEqual(1, len(rows))
            row = rows[0]
            self.assertEqual("loop", row.kind)
            self.assertEqual("", row.session_id)
            self.assertIsNone(row.turn_index)
            self.assertEqual("codex", row.harness)
            self.assertEqual("loop-run-id", row.run_id)

    def test_live_lock_in_wrong_state_dir_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_dir = state_root / "not-the-plan-hash"
            state_dir.mkdir(parents=True)
            plan_dir = Path(tmp) / "plans" / "wrong-hash"
            plan_dir.mkdir(parents=True)
            _write_json(
                state_dir / session_overview.LOCK_JSON_NAME,
                {
                    "plan_dir": str(plans.canonicalize_path(plan_dir)),
                    "pid": os.getpid(),
                    "created_at": "2026-06-19T00:00:00+00:00",
                },
            )

            rows, stale_locks, skipped = session_overview.scan_running_rows(
                state_root
            )

            self.assertEqual((), rows)
            self.assertEqual((), skipped)
            self.assertEqual(1, len(stale_locks))
            self.assertEqual(os.getpid(), stale_locks[0].pid)
            self.assertIn("does not match plan hash", stale_locks[0].reason)

    def test_malformed_lock_json_reported_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "epsilon"
            plan_dir.mkdir(parents=True)
            state_dir = _make_plan_state_dir(state_root, plan_dir, lock=None)
            (state_dir / session_overview.LOCK_JSON_NAME).write_text(
                "{not valid json",
                encoding="utf-8",
            )

            rows, stale_locks, skipped = session_overview.scan_running_rows(
                state_root
            )

            self.assertEqual((), rows)
            self.assertEqual((), stale_locks)
            self.assertEqual(1, len(skipped))
            self.assertIn("lock unreadable", skipped[0].reason)

    def test_missing_state_root_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "does-not-exist"

            rows, stale_locks, skipped = session_overview.scan_running_rows(
                missing_root
            )

            self.assertEqual((), rows)
            self.assertEqual((), stale_locks)
            self.assertEqual((), skipped)

    def test_existing_file_state_root_raises_scan_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state-file"
            state_root.write_text("", encoding="utf-8")

            with self.assertRaises(NotADirectoryError):
                session_overview.scan_running_rows(state_root)


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
                        plan_dir=plans.canonicalize_path(plan_dir),
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
                        plan_dir=plans.canonicalize_path(plan_dir),
                        run_id="run-1",
                        turn_index=index,
                        session_id=str(index),
                        finished_at=finished_at,
                    ),
                )

            rows, skipped = session_overview.scan_recent_rows(
                state_root, limit=2
            )

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
                    plan_dir=plans.canonicalize_path(plan_dir),
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
                    plan_dir=plans.canonicalize_path(plan_dir),
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
            self.assertEqual(
                plans.canonicalize_path(plan_dir), rows[0].plan_dir
            )

    def test_unresolvable_plan_dir_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            stub_state_dir = state_root / "deadbeef"
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
            self.assertIn(
                "unable to resolve plan directory", skipped[0].reason
            )

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
                    plan_dir=plans.canonicalize_path(plan_dir),
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


class SessionsFromArgsTests(unittest.TestCase):
    def _make_populated_state_root(self, root: Path) -> Path:
        state_root = root / "state"
        plan_alpha = root / "plans" / "alpha-plan-dir"
        plan_beta = root / "plans" / "beta-plan-dir"
        plan_alpha.mkdir(parents=True)
        plan_beta.mkdir(parents=True)

        alpha_active = _active_turn_payload(
            run_id="alpha-run", turn_index=2, session_id="2"
        )
        _make_plan_state_dir(
            state_root,
            plan_alpha,
            lock={"pid": os.getpid()},
            state_payload={"active_turn": alpha_active},
        )
        alpha_state_dir = (
            state_root / plans.path_sha256(plans.canonicalize_path(plan_alpha))
        )
        _make_turn_artifact(
            alpha_state_dir,
            run_id="alpha-run",
            turn_label="0001",
            payload=_turn_result_payload(
                plan_dir=plans.canonicalize_path(plan_alpha),
                run_id="alpha-run",
                turn_index=1,
                session_id="1",
                finished_at="2026-06-19T02:00:00+00:00",
            ),
        )

        _make_plan_state_dir(state_root, plan_beta)
        beta_state_dir = (
            state_root / plans.path_sha256(plans.canonicalize_path(plan_beta))
        )
        _make_turn_artifact(
            beta_state_dir,
            run_id="beta-run",
            turn_label="0001",
            payload=_turn_result_payload(
                plan_dir=plans.canonicalize_path(plan_beta),
                run_id="beta-run",
                turn_index=1,
                session_id="1",
                finished_at="2026-06-19T01:00:00+00:00",
            ),
        )
        return state_root

    def test_json_output_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual("sessions", payload["command"])
            self.assertEqual({"running": True, "recent": True}, payload["filters"])
            self.assertEqual(
                session_overview.DEFAULT_RECENT_LIMIT, payload["limit"]
            )
            self.assertEqual(
                str(plans.canonicalize_path(state_root)),
                payload["state_root"],
            )
            self.assertEqual(1, len(payload["running"]))
            self.assertEqual(2, len(payload["recent"]))
            # Recent rows are sorted newest finish first.
            self.assertEqual(
                "2026-06-19T02:00:00+00:00", payload["recent"][0]["finished_at"]
            )
            self.assertEqual(
                payload["counts"],
                {
                    "running": 1,
                    "recent": 2,
                    "stale_locks": 0,
                    "skipped": 0,
                },
            )

    def test_running_filter_skips_recent_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, running=True, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(
                {"running": True, "recent": False}, payload["filters"]
            )
            self.assertEqual([], payload["recent"])
            self.assertEqual(0, payload["counts"]["recent"])
            self.assertEqual(1, len(payload["running"]))

    def test_recent_filter_skips_running_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, recent=True, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(
                {"running": False, "recent": True}, payload["filters"]
            )
            self.assertEqual([], payload["running"])
            self.assertEqual(0, payload["counts"]["running"])
            self.assertEqual(2, len(payload["recent"]))

    def test_limit_clamps_recent_rows_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, limit=1, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(1, payload["limit"])
            self.assertEqual(1, len(payload["recent"]))
            self.assertEqual(1, len(payload["running"]))

    def test_text_output_uses_basename_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root)
                )

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn(
                f"State root: {plans.canonicalize_path(state_root)}",
                output,
            )
            self.assertIn("Running sessions (1):", output)
            self.assertIn("Recent sessions (2):", output)
            self.assertIn("plan alpha-plan-dir", output)
            self.assertIn("plan beta-plan-dir", output)
            # Default text output omits full plan paths.
            self.assertNotIn(
                str(plans.canonicalize_path(root / "plans" / "alpha-plan-dir")),
                output,
            )

    def test_verbose_text_output_uses_full_canonical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = self._make_populated_state_root(root)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, verbose=True)
                )

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            canonical_alpha = str(
                plans.canonicalize_path(root / "plans" / "alpha-plan-dir")
            )
            canonical_beta = str(
                plans.canonicalize_path(root / "plans" / "beta-plan-dir")
            )
            self.assertIn(canonical_alpha, output)
            self.assertIn(canonical_beta, output)

    def test_text_empty_state_root_renders_none_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-state-root"

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(missing)
                )

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn("Running sessions: none", output)
            self.assertIn("Recent sessions: none", output)

    def test_json_empty_state_root_returns_success_with_empty_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-state-root"

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(missing, json_output=True)
                )

            self.assertEqual(0, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual([], payload["running"])
            self.assertEqual([], payload["recent"])
            self.assertEqual(
                payload["counts"],
                {"running": 0, "recent": 0, "stale_locks": 0, "skipped": 0},
            )

    def test_existing_file_state_root_returns_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state-file"
            state_file.write_text("", encoding="utf-8")

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_file, json_output=True)
                )

            self.assertEqual(1, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertIn("not a directory", payload["error"])

    def test_state_root_iterdir_error_returns_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()

            with mock.patch.object(
                Path, "iterdir", side_effect=PermissionError("denied")
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    exit_code = session_overview.sessions_from_args(
                        _build_args(state_root, json_output=True)
                    )

            self.assertEqual(1, exit_code)
            payload = json.loads(buffer.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertIn("could not scan state root", payload["error"])

    def test_negative_limit_returns_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = session_overview.sessions_from_args(
                    _build_args(state_root, limit=-1, json_output=True)
                )

            self.assertEqual(1, exit_code)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("--limit", payload["error"])

    def test_state_root_error_returns_nonzero_with_error_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "broken"

            with mock.patch.object(
                state, "state_root", side_effect=OSError("no")
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = session_overview.sessions_from_args(
                        _build_args(target, json_output=True)
                    )

            self.assertEqual(1, exit_code)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertIn("no", payload["error"])


if __name__ == "__main__":
    unittest.main()
