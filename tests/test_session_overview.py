"""HelpersTests, ScanRunningRowsTests coverage."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    plans_files,
    run_reporting_status,
    session_overview,
    state,
    tail_control_messages,
)
from tests.session_overview_support import (
    DEAD_PID,
    _active_turn_payload,
    _active_verification_payload,
    _make_plan_state_dir,
    _write_json,
)
from tests.support_base import force_pid_alive as _force_pid_alive


class HelpersTests(unittest.TestCase):
    def test_parse_iso_timestamp_accepts_trailing_z(self) -> None:
        parsed = session_overview.parse_iso_timestamp("2026-06-19T01:02:03Z")

        self.assertIsNotNone(parsed)
        self.assertEqual(2026, parsed.year)
        self.assertEqual(0, parsed.utcoffset().total_seconds())

    def test_format_local_time_carries_its_utc_offset(self) -> None:
        # The rest of the CLI prints UTC; a zone-less local time could not be
        # told apart from it.
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            self.addCleanup(time.tzset)
            self.assertEqual(
                "2024-01-01 22:04:05-05:00",
                session_overview.format_local_time("2024-01-02T03:04:05Z"),
            )

    def test_a_stamp_the_local_zone_cannot_hold_keeps_its_offset(self) -> None:
        # Converting year 1 west of UTC, or year 9999 east of it, overflows
        # datetime; the stored offset is printed instead of crashing a reader.
        cases = (
            ("America/Denver", "0001-01-01T00:00:00Z", "0001-01-01 00:00:00+00:00"),
            ("Pacific/Kiritimati", "9999-12-31T23:59:59Z", "9999-12-31 23:59:59+00:00"),
        )
        self.addCleanup(time.tzset)
        for zone, stamp, expected in cases:
            with self.subTest(zone=zone), mock.patch.dict(os.environ, {"TZ": zone}):
                time.tzset()
                self.assertEqual(expected, session_overview.format_local_time(stamp))
                self.assertEqual(
                    expected, tail_control_messages._format_local_datetime(stamp)
                )
                self.assertEqual(
                    expected[:19], run_reporting_status._format_local_datetime(stamp)
                )

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
                str(plans_files.canonicalize_path(plan_dir)),
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

            rows, stale_locks, skipped = session_overview.scan_running_rows(state_root)

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

            rows, stale_locks, skipped = session_overview.scan_running_rows(state_root)

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
                rows, stale_locks, skipped = session_overview.scan_running_rows(
                    state_root
                )

            self.assertEqual((), rows)
            self.assertEqual((), skipped)
            self.assertEqual(1, len(stale_locks))
            stale = stale_locks[0]
            self.assertEqual(DEAD_PID, stale.pid)
            self.assertIn(str(DEAD_PID), stale.reason)

    def test_running_row_carries_the_locks_repository_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "claimed"
            plan_dir.mkdir(parents=True)
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={
                    "version": 2,
                    "pid": os.getpid(),
                    "repositories": ["/w/apple", "/w/zebra"],
                },
                state_payload={"active_turn": _active_turn_payload()},
            )

            rows, _, _ = session_overview.scan_running_rows(state_root)

            self.assertEqual(1, len(rows))
            self.assertEqual(("/w/apple", "/w/zebra"), rows[0].repository_roots)

    def test_running_row_from_a_v1_lock_claims_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "legacy-claim"
            plan_dir.mkdir(parents=True)
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={"version": 1, "pid": os.getpid()},
                state_payload={"active_turn": _active_turn_payload()},
            )

            rows, _, _ = session_overview.scan_running_rows(state_root)

            self.assertEqual(1, len(rows))
            self.assertEqual((), rows[0].repository_roots)

    def test_foreign_host_stale_lock_keeps_its_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "elsewhere"
            plan_dir.mkdir(parents=True)
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={
                    "version": 2,
                    "pid": os.getpid(),
                    "hostname": "some-other-host",
                    "repositories": ["/w/apple"],
                },
            )

            rows, stale_locks, _ = session_overview.scan_running_rows(state_root)

            self.assertEqual((), rows)
            self.assertEqual(1, len(stale_locks))
            stale = stale_locks[0]
            self.assertTrue(stale.foreign_host)
            self.assertEqual(("/w/apple",), stale.repository_roots)

    def test_dead_local_lock_is_stale_without_being_foreign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            plan_dir = Path(tmp) / "plans" / "dead-local"
            plan_dir.mkdir(parents=True)
            _make_plan_state_dir(
                state_root,
                plan_dir,
                lock={
                    "version": 2,
                    "pid": DEAD_PID,
                    "repositories": ["/w/apple"],
                },
            )

            with mock.patch.object(
                state, "is_pid_running", side_effect=_force_pid_alive
            ):
                _, stale_locks, _ = session_overview.scan_running_rows(state_root)

            self.assertEqual(1, len(stale_locks))
            stale = stale_locks[0]
            self.assertFalse(stale.foreign_host)
            self.assertEqual(("/w/apple",), stale.repository_roots)

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

            rows, stale_locks, skipped = session_overview.scan_running_rows(state_root)

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

            rows, stale_locks, skipped = session_overview.scan_running_rows(state_root)

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
            state_dir = state_root / ("0" * 64)  # a digest, but not this plan's
            state_dir.mkdir(parents=True)
            plan_dir = Path(tmp) / "plans" / "wrong-hash"
            plan_dir.mkdir(parents=True)
            _write_json(
                state_dir / session_overview.LOCK_JSON_NAME,
                {
                    "plan_dir": str(plans_files.canonicalize_path(plan_dir)),
                    "pid": os.getpid(),
                    "created_at": "2026-06-19T00:00:00+00:00",
                },
            )

            rows, stale_locks, skipped = session_overview.scan_running_rows(state_root)

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

            rows, stale_locks, skipped = session_overview.scan_running_rows(state_root)

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


if __name__ == "__main__":
    unittest.main()
