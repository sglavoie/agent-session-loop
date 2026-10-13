"""ParserAndStateTests (part 12).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import (
    agent_mux_projection,
    events,
    plans_models,
    plans_progress,
    plans_session_ids,
    plans_snapshot,
    run_persistence,
    state,
    state_abandonment,
    state_locks,
    state_status,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    read_jsonl,
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_status_from_args_text_output_renders_journal_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                state_dir=str(state_dir),
                json=False,
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = state_status.status_from_args(args)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn("Progress journal: not initialized", output)

            plans_progress.append_progress_entry(
                plan_dir,
                plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                ),
            )

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = state_status.status_from_args(args)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn(
                "Progress journal: 1 entry, last appended 2026-06-19T14:22:00Z",
                output,
            )

    def test_status_payload_backfills_old_no_lock_runner_state_as_abandoned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(
                plan_dir,
                ("complete", "complete", "complete", "complete", "complete"),
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-1",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "final_snapshot": {
                            "counts": {
                                "pending": 0,
                                "complete": 5,
                                "empty": 0,
                                "unknown": 0,
                            },
                            "next_pending_id": "5",
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            self.assertIsNone(payload["next_pending"])
            self.assertIsNone(payload["next_pending_id"])
            self.assertEqual(0, payload["counts"]["pending"])
            self.assertEqual(5, payload["counts"]["complete"])

            # The same call that found the orphan reports the terminal state
            # it wrote, so no second `asl status` is needed to see it.
            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["present"])
            self.assertFalse(runner_state["stale_unfinished"])
            self.assertIsNone(runner_state["stale_reason"])
            self.assertFalse(runner_state["ok"])
            self.assertEqual(state_abandonment.ABANDONED_REASON, runner_state["reason"])
            self.assertIsNotNone(runner_state["run_finished_at"])
            abandoned = runner_state["abandoned"]
            self.assertEqual(
                state_abandonment.ABANDONED_TRIGGER_STATUS, abandoned["trigger"]
            )
            self.assertEqual(os.getpid(), abandoned["detected_by_pid"])
            self.assertIsNone(abandoned["lock_reason"])

            records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
            self.assertEqual(1, len(records))
            self.assertEqual(events.RUN_FINISHED_EVENT, records[0]["name"])
            self.assertEqual(events.EVENT_SCHEMA_VERSION, records[0]["version"])
            self.assertTrue(records[0]["payload"]["synthetic"])
            self.assertEqual(
                events.RUN_OUTCOME_ABANDONED, records[0]["payload"]["outcome"]
            )
            self.assertEqual("20260619T120000Z-1", records[0]["payload"]["run_id"])
            self.assertEqual(
                {"pending": 0, "complete": 5, "empty": 0, "unknown": 0},
                records[0]["payload"]["counts"],
            )

    def test_status_payload_leaves_fresh_no_lock_runner_state_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            # A runner that just started writes state.json before its lock, so
            # the missing lock inside the grace window proves nothing.
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-9",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": datetime.now(UTC).isoformat(),
                        "run_finished_at": None,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["stale_unfinished"])
            self.assertIn("no lock file is present", runner_state["stale_reason"])
            self.assertIsNone(runner_state["abandoned"])
            self.assertIsNone(runner_state["run_finished_at"])
            self.assertFalse((plan_state / events.EVENTS_FILE_NAME).exists())

    def test_status_payload_does_not_flag_stale_when_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete", "complete"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-2",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": "2026-06-19T12:30:00+00:00",
                        "duration_seconds": 1800.0,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["present"])
            self.assertFalse(runner_state["stale_unfinished"])
            self.assertIsNone(runner_state["stale_reason"])

    def test_status_payload_does_not_flag_stale_when_active_lock_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending", "pending"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-3",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )
            lock_info = state_locks.acquire_lock(plan_dir, state_dir)
            try:
                payload = state_status.status_payload(
                    plans_snapshot.inspect_plan(plan_dir), state_dir
                )
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertTrue(payload["lock"]["active"])
            self.assertEqual(lock_info.pid, payload["lock"]["pid"])
            runner_state = payload["runner_state"]
            self.assertTrue(runner_state["present"])
            self.assertFalse(runner_state["stale_unfinished"])
            self.assertIsNone(runner_state["stale_reason"])

    def test_status_payload_exposes_active_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            active_verification = {
                "run_id": "20260620T100000Z-1",
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "check_count": 2,
                "check_names": ["operator-surface-tests", "operator-surface-compile"],
                "check_kinds": ["command", "command"],
                "started_at": "2026-06-20T10:00:00+00:00",
                "current_check_index": 0,
                "current_check_name": "operator-surface-tests",
                "current_check_kind": "command",
                "current_check_started_at": "2026-06-20T10:00:01+00:00",
                "elapsed_seconds": 2.0,
                "check_elapsed_seconds": 1.0,
                "heartbeat_count": 0,
            }
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260620T100000Z-1",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "active_verification": active_verification,
                    }
                ),
                encoding="utf-8",
            )

            # A run with a check in flight holds its lock; without one the
            # status backfill would (correctly) declare the run abandoned.
            state_locks.acquire_lock(plan_dir, state_dir)
            try:
                payload = state_status.status_payload(
                    plans_snapshot.inspect_plan(plan_dir), state_dir
                )
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual(active_verification, payload["active_verification"])
            self.assertEqual(
                active_verification,
                payload["runner_state"]["active_verification"],
            )

    def test_status_payload_active_verification_null_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260620T100000Z-2",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "active_verification": None,
                    }
                ),
                encoding="utf-8",
            )

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )

            self.assertIsNone(payload["active_verification"])
            self.assertIsNone(payload["runner_state"]["active_verification"])

    def test_status_from_args_text_output_renders_active_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260620T100000Z-3",
                        "harness": "claude",
                        "reason": "",
                        "run_started_at": "2026-06-20T10:00:00+00:00",
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                        "active_verification": {
                            "run_id": "20260620T100000Z-3",
                            "turn_index": 1,
                            "session": 1,
                            "session_id": "1",
                            "total_sessions": 1,
                            "check_count": 2,
                            "check_names": [
                                "operator-surface-tests",
                                "operator-surface-compile",
                            ],
                            "check_kinds": ["command", "command"],
                            "started_at": "2026-06-20T10:00:00+00:00",
                            "current_check_index": 0,
                            "current_check_name": "operator-surface-tests",
                            "current_check_kind": "command",
                            "current_check_started_at": ("2026-06-20T10:00:01+00:00"),
                            "elapsed_seconds": 2.0,
                            "check_elapsed_seconds": 1.0,
                            "heartbeat_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                state_dir=str(state_dir),
                json=False,
            )
            buffer = io.StringIO()
            # A run with a check in flight holds its lock; without one the
            # status backfill would (correctly) declare the run abandoned.
            state_locks.acquire_lock(plan_dir, state_dir)
            try:
                with redirect_stdout(buffer):
                    exit_code = state_status.status_from_args(args)
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn(
                "Active verification: session 1 turn 1 check 1/2 "
                "operator-surface-tests",
                output,
            )
            self.assertIn("Check kind: command", output)

    def test_status_from_args_text_output_flags_stale_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(
                plan_dir,
                ("complete", "complete", "complete", "complete", "complete"),
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "20260619T120000Z-4",
                        "harness": "claude",
                        "reason": "",
                        # Inside the no-lock grace window, so the orphan is
                        # reported without being declared dead.
                        "run_started_at": datetime.now(UTC).isoformat(),
                        "run_finished_at": None,
                        "duration_seconds": None,
                        "active_turn": None,
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                state_dir=str(state_dir),
                json=False,
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = state_status.status_from_args(args)

            self.assertEqual(0, exit_code)
            output = buffer.getvalue()
            self.assertIn("Run state: stale/orphaned", output)
            self.assertNotIn("Run state: abandoned", output)
            self.assertIn("Next pending: none", output)

    # --- Schema v2 state/result/event compaction -----------------------------

    def test_write_state_payload_compacts_turns_and_stamps_the_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            payload = self._base_state_payload(state_path)
            run_persistence._write_state_payload(payload)

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            agent_mux_projection.STATE_SCHEMA_VERSION, persisted["version"]
        )
        # The projection rides in the same write the version names.
        self.assertEqual(
            "turn",
            persisted[agent_mux_projection.PROJECTION_KEY]["phase"],
        )
        turn = persisted["turns"][0]
        # Scalar/path fields readers depend on are preserved.
        self.assertEqual("/artifacts/stdout.log", turn["stdout_path"])
        self.assertEqual("codex", turn["harness"])
        self.assertEqual("complete", turn["status_result"])
        # Heavy snapshot session lists are dropped; counts survive.
        self.assertNotIn("sessions", turn["before"])
        self.assertNotIn("sessions", turn["after"])
        self.assertEqual(
            {"pending": 1, "complete": 1, "empty": 0, "unknown": 0},
            turn["after"]["counts"],
        )
        # No raw verification output survives, but artifact references do.
        check = turn["verification"]["checks"][0]
        self.assertNotIn("stdout", check)
        self.assertNotIn("stderr", check)
        self.assertEqual("/artifacts/tests-stdout.gz", check["stdout_artifact_path"])
        self.assertEqual(8192, check["stdout_byte_count"])
        # The in-memory payload is untouched (no aliasing of the compact copy).
        self.assertIn("sessions", payload["turns"][0]["after"])
        self.assertIn("stdout", payload["turns"][0]["verification"]["checks"][0])
        self.assertNotIn("version", payload)
