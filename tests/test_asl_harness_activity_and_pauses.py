"""Activity coalescing, retries and failure pause reasons."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    durability,
    events,
    run_commit,
    run_control,
    run_coordination_control,
    run_persistence,
    run_reporting,
    turn_execution_signals,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    read_jsonl,
)

ASL_TEST_LANE = "integration"


class ActivityAndFailurePauseTests(FakeHarnessIntegrationBase):
    def test_a_new_turn_supersedes_the_previous_turns_action_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload: dict[str, Any] = {
                "state_path": str(state_path),
                "active_turn": None,
                "action_required": {
                    "run_id": "run-1",
                    "turn_index": 1,
                    "reason": "approval-required",
                    "message": "approval required before continuing",
                    "source": "stderr",
                    "matched": "approval required",
                },
            }
            active_turn = {
                "run_id": "run-1",
                "turn_index": 2,
                "session": 2,
                "total_sessions": 3,
                "harness": "codex",
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": clock.isoformat(clock.utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            live_turn = run_commit._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=run_reporting._ProgressReporter(enabled=False),
                active_turn=active_turn,
                heartbeat_interval=60,
            )

            live_turn.start()
            live_turn.stop()
            live_turn.finish(clock.utc_now())

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertIsNone(payload["action_required"])
        self.assertIsNone(state_payload["action_required"])

    def test_burst_activity_is_coalesced_and_control_persists_latest_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "run_id": "run-1",
                "harness": "codex",
                "active_turn": None,
                "control": run_coordination_control.initial_control_payload(),
            }
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 2,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": clock.isoformat(clock.utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            persistence = run_persistence._RunPersistence()
            live_turn = run_commit._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=run_reporting._ProgressReporter(enabled=False),
                active_turn=active_turn,
                persistence=persistence,
                heartbeat_interval=60,
            )
            control_write_started = threading.Event()
            release_control_write = threading.Event()
            preview_started = threading.Event()
            release_preview = threading.Event()
            delayed_once = False
            thread_errors: list[BaseException] = []
            original_write_json = durability.write_json_file
            original_preview_text = turn_execution_signals.preview_text
            state_write_count = 0

            def delayed_write_json(
                path: Path,
                payload_arg: dict[str, Any],
                *,
                durable: bool = False,
            ) -> None:
                nonlocal delayed_once, state_write_count
                path = Path(path)
                active = payload_arg.get("active_turn")
                control = payload_arg.get("control")
                if path == state_path:
                    state_write_count += 1
                if (
                    path == state_path
                    and not delayed_once
                    and isinstance(active, dict)
                    and active.get("last_activity_stream") == "stdout"
                    and isinstance(control, dict)
                    and control.get("next_harness") == "claude"
                ):
                    delayed_once = True
                    serialized = (
                        json.dumps(payload_arg, sort_keys=True, indent=2) + "\n"
                    )
                    control_write_started.set()
                    release_control_write.wait(timeout=2)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary_path = path.with_name(f".{path.name}.blocked.tmp")
                    temporary_path.write_text(serialized, encoding="utf-8")
                    os.replace(temporary_path, path)
                    return
                original_write_json(path, payload_arg, durable=durable)

            def record_control() -> None:
                try:
                    run_coordination_control.record_run_control_action(
                        event_path=event_path,
                        payload=payload,
                        control_payload=dict(payload["control"]),
                        action=run_control.RunControlAction(
                            action="harness-selected",
                            stop_after_current=False,
                            next_harness="claude",
                        ),
                        context=active_turn,
                        persistence=persistence,
                    )
                except BaseException as error:
                    thread_errors.append(error)

            def record_latest_activity() -> None:
                try:
                    live_turn.record_activity("stdout", "latest output\n")
                except BaseException as error:
                    thread_errors.append(error)

            def delayed_preview(text: str, *, limit: int = 160) -> str:
                preview_started.set()
                release_preview.wait(timeout=2)
                return original_preview_text(text, limit=limit)

            with mock.patch.object(
                durability,
                "write_json_file",
                side_effect=delayed_write_json,
            ):
                for index in range(5_000):
                    live_turn.record_activity("stdout", f"output {index}\n")

                self.assertEqual(0, state_write_count)
                self.assertFalse(state_path.exists())
                self.assertEqual(
                    sum(len(f"output {index}\n") for index in range(5_000)),
                    payload["active_turn"]["stdout_chars"],
                )
                self.assertEqual(
                    "output 4999", payload["active_turn"]["last_activity_preview"]
                )

                with mock.patch.object(
                    turn_execution_signals, "preview_text", side_effect=delayed_preview
                ):
                    activity_thread = threading.Thread(target=record_latest_activity)
                    activity_thread.start()
                    self.assertTrue(preview_started.wait(timeout=2))

                    control_thread = threading.Thread(target=record_control)
                    control_thread.start()
                    time.sleep(0.05)
                    self.assertTrue(control_thread.is_alive())
                    self.assertFalse(control_write_started.is_set())

                    release_preview.set()
                    activity_thread.join(timeout=2)
                self.assertTrue(control_write_started.wait(timeout=2))

                release_control_write.set()
                control_thread.join(timeout=2)

            self.assertFalse(control_thread.is_alive())
            self.assertFalse(activity_thread.is_alive())
            if thread_errors:
                raise thread_errors[0]
            self.assertEqual(1, state_write_count)

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                "stdout", state_payload["active_turn"]["last_activity_stream"]
            )
            self.assertEqual(
                "latest output",
                state_payload["active_turn"]["last_activity_preview"],
            )
            self.assertEqual("claude", state_payload["control"]["next_harness"])
            self.assertEqual(
                "harness-selected", state_payload["control"]["last_action"]
            )
            control_events = [
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.RUN_CONTROL_EVENT
            ]
            self.assertEqual(
                ["harness-selected"], [event["action"] for event in control_events]
            )

    def test_heartbeat_persists_accumulated_activity_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {"state_path": str(state_path), "active_turn": None}
            started_at = clock.isoformat(clock.utc_now())
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 1,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": started_at,
                "last_activity_at": started_at,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
            }
            persistence = run_persistence._RunPersistence()
            live_turn = run_commit._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=run_reporting._ProgressReporter(enabled=False),
                active_turn=active_turn,
                persistence=persistence,
                heartbeat_interval=60,
            )
            write_state = mock.Mock(wraps=persistence.write_state)

            with mock.patch.object(persistence, "write_state", write_state):
                live_turn.record_activity("stdout", "first\n")
                live_turn.record_activity("stderr", "second\n")
                self.assertEqual(0, write_state.call_count)

                heartbeat = live_turn._record_heartbeat()

            self.assertEqual(1, write_state.call_count)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(6, persisted["active_turn"]["stdout_chars"])
            self.assertEqual(7, persisted["active_turn"]["stderr_chars"])
            self.assertEqual("stderr", persisted["active_turn"]["last_activity_stream"])
            self.assertEqual(
                "second", persisted["active_turn"]["last_activity_preview"]
            )
            self.assertEqual(1, heartbeat["heartbeat_count"])

    def test_retry_after_failed_turn_keeps_first_run_artifacts(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        first_completed, first_payload = self.run_loop_command(
            mode="failure",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        self.assertEqual(1, first_completed.returncode)
        self.assertFalse(first_payload["ok"])
        first_turn = first_payload["turns"][0]
        first_stderr = Path(first_turn["stderr_path"])
        first_result = Path(first_turn["result_path"])
        first_artifact_dir = Path(first_turn["artifact_dir"])
        self.assertEqual(
            Path(first_payload["run_dir"]) / "turn-0001-session-1",
            first_artifact_dir,
        )
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        original_stderr = first_stderr.read_text(encoding="utf-8")
        original_result = json.loads(first_result.read_text(encoding="utf-8"))
        self.assertIn("fake codex failure", original_stderr)
        self.assertEqual(7, original_result["returncode"])
        self.assertEqual(first_payload["run_id"], original_result["run_id"])

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
        )

        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertNotEqual(first_payload["run_id"], second_payload["run_id"])
        second_turn = second_payload["turns"][0]
        self.assertNotEqual(first_artifact_dir, Path(second_turn["artifact_dir"]))
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        self.assertEqual(original_stderr, first_stderr.read_text(encoding="utf-8"))
        self.assertEqual(
            original_result,
            json.loads(first_result.read_text(encoding="utf-8")),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_nonzero_fake_harness_pauses_without_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="failure",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertIn(payload["pause_reason"], events.PAUSE_REASONS)

    def test_timed_out_fake_harness_reports_timeout_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="timeout",
            statuses=("pending",),
            timeout=1,
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertTrue(payload["turns"][0]["timed_out"])

    def test_bad_fake_harness_progress_reports_multiple_status_changes(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="multiple-progress",
            statuses=("pending", "pending"),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("multiple-status-changes", payload["pause_reason"])
        self.assertEqual(2, len(payload["observed_status_changes"]))

    def test_no_progress_fake_harness_reports_no_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="no-progress",
            statuses=("pending",),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertEqual("no-status-change", payload["reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual([], payload["observed_status_changes"])
        self.assertEqual(payload["pause_details"], payload["details"])
        self.assertEqual(expected, payload["details"]["expected_status_changes"])
        self.assertEqual([], payload["details"]["observed_status_changes"])

    def test_unexpected_progress_reports_unexpected_status_change(self) -> None:
        completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="unexpected-progress",
            statuses=("pending", "pending"),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        observed = [{"number": 2, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual(observed, payload["observed_status_changes"])
        self.assertEqual(observed, payload["details"]["observed_status_changes"])
        self.assertEqual(1, payload["final_snapshot"]["next_pending"])
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

    def test_runner_pause_signal_reports_runner_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="runner-pause",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("runner-pause", payload["pause_reason"])
        self.assertEqual("operator requested pause", payload["pause_message"])
        self.assertEqual({"session": 1, "source": "last_message"}, payload["details"])
        turn = payload["turns"][0]
        self.assertEqual("operator requested pause", turn["runner_pause"])
        self.assertEqual("last_message", turn["runner_pause_source"])

    def test_structured_runner_pause_signal_uses_embedded_reason(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="runner-pause-unsafe",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unsafe", payload["pause_reason"])
        self.assertEqual("unsafe", payload["reason"])
        self.assertIn("plan readiness is not current", payload["pause_message"])
        self.assertIn(payload["pause_reason"], events.PAUSE_REASONS)
        self.assertEqual("unsafe", payload["turns"][0]["status_result"])

    def test_noop_runner_pause_signal_completes_without_pausing(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success-runner-pause-none",
            statuses=("pending",),
        )

        self.assertEqual(0, completed.returncode)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        turn = payload["turns"][0]
        self.assertEqual("complete", turn["status_result"])
        self.assertNotIn("runner_pause", turn)

    def test_malformed_after_turn_reports_malformed_plan_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="malformed-after-turn",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        self.assertEqual(
            "plan could not be inspected after the harness turn",
            payload["pause_message"],
        )
        self.assertEqual(1, payload["details"]["session"])
        self.assertIn("missing context.md", payload["details"]["error"])
        turn = payload["turns"][0]
        self.assertIsNone(turn["after"])
        self.assertIn("missing context.md", turn["plan_error"])
