"""AgentMuxNotificationIntegrationTestsPart2.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    commit_settlement,
    events,
    readiness_gate,
    run_persistence,
    run_termination,
    turn_execution,
)
from tests.asl_control_agent_mux_support import AgentMuxNotificationIntegrationTestsBase
from tests.support import (
    read_jsonl,
)


class AgentMuxNotificationIntegrationTestsPart2(
    AgentMuxNotificationIntegrationTestsBase
):
    def test_lifecycle_notifications_carry_phase_sequence_and_identity(
        self,
    ) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            payload["agent_mux_sequence"] = 0
            payload["pause_reason"] = "verify-failed"
            payload["pause_message"] = "check exploded"
            persistence = run_persistence._RunPersistence()
            active_turn = {
                "harness": "claude",
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": "2026-06-11T12:01:00Z",
                "heartbeat_count": 3,
                "last_activity_at": "2026-06-11T12:01:30Z",
            }

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess, "run", return_value=completed
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_started(payload, persistence)
                turn_execution.notify_turn_started(payload, active_turn, persistence)
                turn_execution.notify_turn_heartbeat(payload, active_turn, persistence)
                turn_execution.notify_turn_finished(payload, active_turn, persistence)
                run_persistence._notify_agent_mux_loop_paused(payload, persistence)
                run_persistence._notify_agent_mux_loop_finished(payload, persistence)

            events_seen = [call.args[0][-1] for call in run.call_args_list]
            self.assertEqual(
                [
                    "AgentSessionLoopStarted",
                    "AgentSessionLoopTurnStarted",
                    "AgentSessionLoopTurnHeartbeat",
                    "AgentSessionLoopTurnFinished",
                    "AgentSessionLoopPaused",
                    "AgentSessionLoopFinished",
                ],
                events_seen,
            )
            hook_payloads = [
                json.loads(call.kwargs["input"]) for call in run.call_args_list
            ]
            self.assertEqual(
                ["between", "turn", "turn", "between", "paused", "finished"],
                [hook_payload["phase"] for hook_payload in hook_payloads],
            )
            # The persisted per-run counter is monotonic across every event
            # and both ordering fields are string-encoded for the daemon.
            self.assertEqual(
                [str(index) for index in range(1, 7)],
                [hook_payload["source_sequence"] for hook_payload in hook_payloads],
            )
            self.assertEqual(
                [f"run-123:{index}" for index in range(1, 7)],
                [hook_payload["source_event_id"] for hook_payload in hook_payloads],
            )
            for hook_payload in hook_payloads:
                self.assertEqual("run-123", hook_payload["run_id"])
                self.assertTrue(hook_payload["emitted_time"].isdigit())
            # Turn-scoped events carry the turn identity, heartbeat count,
            # and activity recency the daemon consumes.
            heartbeat_payload = hook_payloads[2]
            self.assertEqual(1, heartbeat_payload["turn_index"])
            self.assertEqual("1", heartbeat_payload["session_id"])
            self.assertEqual(3, heartbeat_payload["heartbeat_count"])
            self.assertEqual(
                "2026-06-11T12:01:30Z", heartbeat_payload["last_output_at"]
            )
            # Paused carries the attention-mapping reason for the daemon.
            paused_payload = hook_payloads[4]
            self.assertEqual("verify-failed", paused_payload["pause_reason"])
            self.assertEqual("check exploded", paused_payload["pause_message"])
            # The allocated counter is durable in state.json.
            state_payload = json.loads(
                Path(str(payload["state_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual(6, state_payload["agent_mux_sequence"])
            self.assertEqual(6, payload["agent_mux_sequence"])

    def test_retryable_delivery_reuses_request_id_and_records_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            payload["agent_mux_sequence"] = 0
            persistence = run_persistence._RunPersistence()
            retryable = subprocess.CompletedProcess(
                ["/opt/amx"],
                75,
                stdout=json.dumps(
                    {
                        "status": "retryable_failure",
                        "retryable": True,
                        "reason": "daemon_unavailable",
                        "trace_id": "trace-attempt-1",
                    }
                ),
                stderr="",
            )
            accepted = self.accepted_delivery("trace-attempt-3")

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    side_effect=[
                        retryable,
                        subprocess.TimeoutExpired(["/opt/amx"], 1),
                        accepted,
                    ],
                ) as run,
                mock.patch.object(
                    agent_mux_delivery, "retry_sleep", return_value=None
                ) as retry_sleep,
            ):
                run_persistence._notify_agent_mux_loop_finished(payload, persistence)

            self.assertEqual(3, run.call_count)
            retry_sleep.assert_has_calls([mock.call(0.1), mock.call(0.3)])
            commands = [call.args[0] for call in run.call_args_list]
            request_ids = [
                command[command.index("--request-id") + 1] for command in commands
            ]
            self.assertEqual(1, len(set(request_ids)))
            self.assertEqual(
                agent_mux_delivery.stable_request_id("run-123:1"),
                request_ids[0],
            )
            hook_payloads = [
                json.loads(call.kwargs["input"]) for call in run.call_args_list
            ]
            self.assertEqual(
                {"run-123:1"},
                {hook_payload["source_event_id"] for hook_payload in hook_payloads},
            )
            records = read_jsonl(Path(str(payload["events_path"])))
            self.assertEqual(1, len(records))
            outcome = records[0]["payload"]
            self.assertEqual("accepted", outcome["final_status"])
            self.assertEqual("trace-attempt-3", outcome["trace_id"])
            self.assertEqual(3, outcome["attempt_count"])
            self.assertEqual(
                [
                    "retryable_failure",
                    "retryable_failure",
                    "accepted",
                ],
                [attempt["status"] for attempt in outcome["attempts"]],
            )

    def test_duplicate_acknowledgement_is_success_without_retry(self) -> None:
        duplicate = subprocess.CompletedProcess(
            ["/opt/amx"],
            0,
            stdout=json.dumps(
                {
                    "status": "duplicate",
                    "retryable": False,
                    "reason": "duplicate",
                    "trace_id": "trace-duplicate",
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess, "run", return_value=duplicate
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_finished(
                    payload, run_persistence._RunPersistence()
                )
            run.assert_called_once()
            outcome = read_jsonl(Path(str(payload["events_path"])))[0]["payload"]
            self.assertTrue(outcome["acknowledged"])
            self.assertEqual("duplicate", outcome["final_status"])

    def test_permanent_delivery_failure_is_sanitized_and_not_retried(self) -> None:
        permanent = subprocess.CompletedProcess(
            ["/opt/amx"],
            2,
            stdout=json.dumps(
                {
                    "status": "permanent_failure",
                    "retryable": False,
                    "reason": "secret /Users/example/private",
                    "trace_id": "trace-permanent",
                }
            ),
            stderr="sensitive diagnostic detail",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess, "run", return_value=permanent
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_finished(
                    payload, run_persistence._RunPersistence()
                )
            run.assert_called_once()
            event_text = Path(str(payload["events_path"])).read_text(encoding="utf-8")
            self.assertNotIn("/Users/example", event_text)
            self.assertNotIn("sensitive diagnostic", event_text)
            outcome = json.loads(event_text)["payload"]
            self.assertEqual("permanent_failure", outcome["final_status"])
            self.assertEqual("unspecified", outcome["reason"])
            self.assertEqual("permanent_outcome", outcome["error_classification"])

    def test_legacy_binary_falls_back_once_and_caches_capability(self) -> None:
        unsupported = subprocess.CompletedProcess(
            ["/opt/amx"],
            1,
            stdout="",
            stderr="unknown flag: --request-id",
        )
        legacy = subprocess.CompletedProcess(["/opt/amx"], 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            persistence = run_persistence._RunPersistence()
            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    side_effect=[unsupported, legacy, legacy],
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_started(payload, persistence)
                run_persistence._notify_agent_mux_loop_finished(payload, persistence)

            self.assertEqual(3, run.call_count)
            self.assertIn("--strict", run.call_args_list[0].args[0])
            self.assertEqual(
                ["/opt/amx", "hook-indicator", agent_mux_delivery.START_EVENT],
                run.call_args_list[1].args[0],
            )
            self.assertEqual(
                ["/opt/amx", "hook-indicator", agent_mux_delivery.FINISH_EVENT],
                run.call_args_list[2].args[0],
            )
            outcomes = [
                record["payload"]
                for record in read_jsonl(Path(str(payload["events_path"])))
            ]
            self.assertEqual(2, len(outcomes))
            self.assertEqual(
                ["legacy_unacknowledged", "legacy_unacknowledged"],
                [outcome["final_status"] for outcome in outcomes],
            )
            self.assertEqual([2, 1], [outcome["attempt_count"] for outcome in outcomes])

    def test_exhausted_delivery_is_recorded_without_raising(self) -> None:
        retryable = subprocess.CompletedProcess(
            ["/opt/amx"],
            75,
            stdout=json.dumps(
                {
                    "status": "retryable_failure",
                    "retryable": True,
                    "reason": "daemon_unavailable",
                    "trace_id": "trace-retryable",
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess, "run", return_value=retryable
                ) as run,
                mock.patch.object(agent_mux_delivery, "retry_sleep", return_value=None),
            ):
                run_persistence._notify_agent_mux_loop_finished(
                    payload, run_persistence._RunPersistence()
                )
            self.assertEqual(3, run.call_count)
            outcome = read_jsonl(Path(str(payload["events_path"])))[0]["payload"]
            self.assertEqual("retryable_exhausted", outcome["final_status"])
            self.assertFalse(outcome["acknowledged"])

    def test_pause_run_emits_paused_and_finish_run_skips_finished(self) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / "events.jsonl"
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            payload["agent_mux_sequence"] = 0
            persistence = run_persistence._RunPersistence()

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess, "run", return_value=completed
                ) as run,
            ):
                run_termination._pause_run(
                    event_path,
                    payload,
                    reason="question",
                    message="need input",
                    persistence=persistence,
                )
                commit_settlement._finish_run(
                    event_path,
                    payload,
                    persistence=persistence,
                    finalize_readiness=run_termination.readiness_finalizer(
                        readiness_gate.write_readiness_state
                    ),
                )

            events_seen = [call.args[0][-1] for call in run.call_args_list]
            self.assertEqual(["AgentSessionLoopPaused"], events_seen)
            hook_payload = json.loads(run.call_args_list[0].kwargs["input"])
            self.assertEqual("paused", hook_payload["phase"])
            self.assertEqual("question", hook_payload["pause_reason"])
            self.assertEqual("need input", hook_payload["pause_message"])
            # The run still finalized normally on disk.
            self.assertIsNotNone(payload["run_finished_at"])
            self.assertTrue(payload["paused"])

    def test_unpaused_finish_run_emits_finished(self) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / "events.jsonl"
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            payload["agent_mux_sequence"] = 0
            persistence = run_persistence._RunPersistence()

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess, "run", return_value=completed
                ) as run,
            ):
                commit_settlement._finish_run(
                    event_path,
                    payload,
                    persistence=persistence,
                    finalize_readiness=run_termination.readiness_finalizer(
                        readiness_gate.write_readiness_state
                    ),
                )

            events_seen = [call.args[0][-1] for call in run.call_args_list]
            self.assertEqual(["AgentSessionLoopFinished"], events_seen)
            hook_payload = json.loads(run.call_args_list[0].kwargs["input"])
            self.assertEqual("finished", hook_payload["phase"])
            self.assertEqual("1", hook_payload["source_sequence"])

    def test_cancelled_recheck_skips_emission_without_allocating_sequence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            payload["agent_mux_sequence"] = 0
            persistence = run_persistence._RunPersistence()

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(agent_mux_delivery.subprocess, "run") as run,
            ):
                run_persistence._notify_agent_mux_loop(
                    agent_mux_delivery.TURN_HEARTBEAT_EVENT,
                    payload,
                    reason=agent_mux_delivery.TURN_HEARTBEAT_REASON,
                    persistence=persistence,
                    cancelled=lambda: True,
                )

            run.assert_not_called()
            self.assertEqual(0, payload["agent_mux_sequence"])

    def test_missing_amx_records_outcome_and_preserves_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            payload["agent_mux_sequence"] = 0
            persistence = run_persistence._RunPersistence()

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value=None
                ),
                mock.patch.object(agent_mux_delivery.subprocess, "run") as run,
            ):
                run_persistence._notify_agent_mux_loop_finished(payload, persistence)

            run.assert_not_called()
            self.assertEqual(1, payload["agent_mux_sequence"])
            records = read_jsonl(Path(str(payload["events_path"])))
            self.assertEqual(1, len(records))
            self.assertEqual(events.AGENT_MUX_DELIVERY_EVENT, records[0]["name"])
            outcome = records[0]["payload"]
            self.assertEqual("run-123:1", outcome["source_event_id"])
            self.assertEqual("unavailable", outcome["final_status"])
            self.assertEqual("missing_binary", outcome["error_classification"])


if __name__ == "__main__":
    unittest.main()
