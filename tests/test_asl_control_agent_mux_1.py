"""AgentMuxNotificationIntegrationTests.

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
    agent_mux_projection,
    commit_settlement,
    readiness_gate,
    run_persistence,
    run_selection,
    run_termination,
    turn_execution,
)
from tests.asl_control_agent_mux_support import AgentMuxNotificationIntegrationTestsBase
from tests.support import (
    patched_env,
    read_jsonl,
)


class AgentMuxNotificationIntegrationTests(AgentMuxNotificationIntegrationTestsBase):
    def test_kitty_window_id_from_env_accepts_positive_int(self) -> None:
        with patched_env(KITTY_WINDOW_ID="42"):
            self.assertEqual(42, run_selection._kitty_window_id_from_env())

    def test_kitty_window_id_from_env_ignores_missing_or_invalid_values(self) -> None:
        for raw in (None, "", "0", "-7", "not-an-int"):
            with self.subTest(raw=raw), patched_env(KITTY_WINDOW_ID=raw):
                self.assertIsNone(run_selection._kitty_window_id_from_env())

    def test_wait_lifecycle_delivery_constants_and_phases(self) -> None:
        self.assertEqual(
            "AgentSessionLoopWaitStarted", agent_mux_delivery.WAIT_START_EVENT
        )
        self.assertEqual("AgentSessionLoopWaitEnded", agent_mux_delivery.WAIT_END_EVENT)
        self.assertEqual("wait_started", agent_mux_delivery.WAIT_START_REASON)
        self.assertEqual("wait_ended", agent_mux_delivery.WAIT_END_REASON)
        self.assertEqual(
            "waiting",
            agent_mux_delivery.PHASE_BY_EVENT[agent_mux_delivery.WAIT_START_EVENT],
        )
        self.assertEqual(
            "between",
            agent_mux_delivery.PHASE_BY_EVENT[agent_mux_delivery.WAIT_END_EVENT],
        )
        self.assertEqual(
            agent_mux_delivery.WAIT_START_EVENT,
            agent_mux_delivery.WAIT_START_EVENT,
        )
        self.assertEqual(
            agent_mux_delivery.WAIT_END_EVENT, agent_mux_delivery.WAIT_END_EVENT
        )
        self.assertEqual(
            agent_mux_delivery.WAIT_START_REASON,
            agent_mux_delivery.WAIT_START_REASON,
        )
        self.assertEqual(
            agent_mux_delivery.WAIT_END_REASON, agent_mux_delivery.WAIT_END_REASON
        )

    def test_mailbox_resolution_delivery_constants_are_distinct(self) -> None:
        self.assertEqual(
            "AgentSessionLoopMailboxQuestion",
            agent_mux_delivery.MAILBOX_QUESTION_EVENT,
        )
        self.assertEqual(
            "AgentSessionLoopMailboxReply",
            agent_mux_delivery.MAILBOX_REPLY_EVENT,
        )
        self.assertEqual(
            "AgentSessionLoopMailboxResolution",
            agent_mux_delivery.MAILBOX_RESOLUTION_EVENT,
        )
        self.assertEqual(
            "mailbox_question_surfaced",
            agent_mux_delivery.MAILBOX_QUESTION_REASON,
        )
        self.assertEqual(
            "mailbox_reply_appended",
            agent_mux_delivery.MAILBOX_REPLY_REASON,
        )
        self.assertEqual(
            "mailbox_resolution_appended",
            agent_mux_delivery.MAILBOX_RESOLUTION_REASON,
        )

    def test_agent_mux_notify_timeout_defaults_without_env_override(self) -> None:
        with patched_env(ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS=None):
            self.assertEqual(
                agent_mux_delivery.NOTIFY_TIMEOUT_SECONDS,
                run_persistence._agent_mux_notify_timeout(),
            )

    def test_agent_mux_notify_timeout_honors_env_override(self) -> None:
        for raw, expected in (("30", 30.0), (" 2.5 ", 2.5), ("0.1", 0.1)):
            with (
                self.subTest(raw=raw),
                patched_env(ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS=raw),
            ):
                self.assertEqual(expected, run_persistence._agent_mux_notify_timeout())

    def test_agent_mux_notify_timeout_fails_safe_on_invalid_override(self) -> None:
        for raw in ("", "0", "-1", "not-a-float", "inf", "nan"):
            with (
                self.subTest(raw=raw),
                patched_env(ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS=raw),
            ):
                self.assertEqual(
                    agent_mux_delivery.NOTIFY_TIMEOUT_SECONDS,
                    run_persistence._agent_mux_notify_timeout(),
                )

    def test_notification_subprocess_uses_env_timeout_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            completed = self.accepted_delivery()

            with (
                patched_env(ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS="30"),
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_finished(payload)

            run.assert_called_once()
            self.assertEqual(30.0, run.call_args.kwargs["timeout"])

    def test_loop_payload_uses_active_turn_harness_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            active_turn = {
                "harness": "claude",
                "turn_index": 2,
                "session": "2.1",
                "session_id": "2.1",
                "total_sessions": 4,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": "2026-06-11T12:01:00Z",
            }

            hook_payload = run_persistence._agent_mux_loop_payload(
                payload,
                reason=agent_mux_delivery.TURN_START_REASON,
                active_turn=active_turn,
            )

            self.assertEqual("agent_session_loop", hook_payload["source"])
            self.assertEqual("turn_started", hook_payload["reason"])
            self.assertEqual("claude", hook_payload["harness"])
            self.assertEqual(payload["state_path"], hook_payload["state_path"])
            self.assertEqual(42, hook_payload["kitty_window_id"])
            self.assertEqual(2, hook_payload["turn_index"])
            self.assertEqual("2.1", hook_payload["session"])
            self.assertEqual("2.1", hook_payload["session_id"])
            self.assertEqual(4, hook_payload["total_sessions"])

    def test_start_and_turn_notifications_build_agent_mux_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
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
            }
            completed = self.accepted_delivery()

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_started(payload)
                turn_execution.notify_turn_started(payload, active_turn)

            self.assertEqual(2, run.call_count)
            start_args, start_kwargs = run.call_args_list[0]
            turn_args, turn_kwargs = run.call_args_list[1]
            self.assertEqual("AgentSessionLoopStarted", start_args[0][-1])
            self.assertEqual("AgentSessionLoopTurnStarted", turn_args[0][-1])
            for command in (start_args[0], turn_args[0]):
                self.assertIn("--harness", command)
                self.assertIn("--request-id", command)
                self.assertIn("--json", command)
                self.assertIn("--strict", command)
            self.assertEqual(
                agent_mux_delivery.NOTIFY_TIMEOUT_SECONDS,
                start_kwargs["timeout"],
            )
            self.assertEqual(
                agent_mux_delivery.NOTIFY_TIMEOUT_SECONDS,
                turn_kwargs["timeout"],
            )
            start_payload = json.loads(start_kwargs["input"])
            turn_payload = json.loads(turn_kwargs["input"])
            self.assertEqual("run_started", start_payload["reason"])
            self.assertEqual("codex", start_payload["harness"])
            self.assertEqual(payload["state_path"], start_payload["state_path"])
            self.assertEqual(42, start_payload["kitty_window_id"])
            self.assertEqual("turn_started", turn_payload["reason"])
            self.assertEqual("claude", turn_payload["harness"])
            self.assertEqual(42, turn_payload["kitty_window_id"])
            self.assertEqual(1, turn_payload["turn_index"])
            self.assertEqual(1, turn_payload["session"])

    def test_finish_notification_builds_agent_mux_command_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            completed = self.accepted_delivery()

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil,
                    "which",
                    return_value="/opt/amx",
                ) as which,
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_finished(payload)

            which.assert_called_once_with("amx")
            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual("AgentSessionLoopFinished", args[0][-1])
            self.assertEqual("codex", args[0][args[0].index("--harness") + 1])
            request_id = args[0][args[0].index("--request-id") + 1]
            self.assertEqual(
                agent_mux_delivery.stable_request_id("run-123:1"), request_id
            )
            self.assertIn("--json", args[0])
            self.assertIn("--strict", args[0])
            self.assertTrue(kwargs["text"])
            self.assertEqual(subprocess.PIPE, kwargs["stdout"])
            self.assertEqual(subprocess.PIPE, kwargs["stderr"])
            self.assertEqual(
                run_persistence.AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS,
                kwargs["timeout"],
            )
            self.assertFalse(kwargs["check"])
            hook_payload = json.loads(kwargs["input"])
            emitted_time = hook_payload.pop("emitted_time")
            self.assertTrue(emitted_time.isdigit())
            self.assertEqual(
                {
                    "source": "agent_session_loop",
                    "reason": "run_finished",
                    "run_id": "run-123",
                    "plan_dir": payload["plan_dir"],
                    "cwd": payload["cwd"],
                    "state_path": payload["state_path"],
                    "kitty_window_id": 42,
                    "harness": "codex",
                    "phase": "finished",
                    "run_started_at": "2026-06-11T12:00:00Z",
                    "run_finished_at": "2026-06-11T12:00:03Z",
                    "source_sequence": "1",
                    "source_event_id": "run-123:1",
                    "schema_version": agent_mux_projection.HOOK_SCHEMA_VERSION,
                    "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    "event": "AgentSessionLoopFinished",
                },
                hook_payload,
            )

    def test_finish_notification_skips_when_amx_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value=None
                ),
                mock.patch.object(agent_mux_delivery.subprocess, "run") as run,
            ):
                run_persistence._notify_agent_mux_loop_finished(
                    self.finish_payload(Path(tmp))
                )

            run.assert_not_called()

    def test_finish_notification_swallows_subprocess_failures(self) -> None:
        failures: tuple[Exception, ...] = (
            subprocess.TimeoutExpired(["/opt/amx"], 1),
            OSError("amx failed"),
            subprocess.SubprocessError("amx failed"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            for failure in failures:
                with (
                    self.subTest(failure=type(failure).__name__),
                    mock.patch.object(
                        agent_mux_delivery.shutil,
                        "which",
                        return_value="/opt/amx",
                    ),
                    mock.patch.object(
                        agent_mux_delivery.subprocess,
                        "run",
                        side_effect=failure,
                    ),
                    mock.patch.object(
                        agent_mux_delivery, "retry_sleep", return_value=None
                    ),
                ):
                    run_persistence._notify_agent_mux_loop_finished(payload)

    def test_finish_run_notifies_after_setting_finished_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / "events.jsonl"
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None

            with mock.patch.object(
                run_persistence,
                "_notify_agent_mux_loop_finished",
            ) as notify:
                commit_settlement._finish_run(
                    event_path,
                    payload,
                    finalize_readiness=run_termination.readiness_finalizer(
                        readiness_gate.write_readiness_state
                    ),
                )

            notify.assert_called_once_with(payload, mock.ANY)
            self.assertIsNotNone(payload["run_finished_at"])
            self.assertIsNone(payload["active_turn"])
            self.assertTrue(Path(payload["state_path"]).exists())
            records = read_jsonl(event_path)
            self.assertEqual("run_finished", records[0]["name"])
            self.assertEqual(
                payload["run_finished_at"],
                records[0]["payload"]["run_finished_at"],
            )

    def test_notify_env_off_disables_delivery_before_executable_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            with (
                patched_env(ASL_AGENT_MUX_NOTIFY="off"),
                mock.patch.object(agent_mux_delivery.shutil, "which") as which,
                mock.patch.object(agent_mux_delivery.subprocess, "run") as run,
            ):
                run_persistence._notify_agent_mux_loop_finished(payload)

            which.assert_not_called()
            run.assert_not_called()

    def test_notify_env_unknown_value_behaves_as_auto(self) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            for raw in ("banana", "ON", " Auto ", ""):
                with self.subTest(raw=raw):
                    with (
                        patched_env(ASL_AGENT_MUX_NOTIFY=raw),
                        mock.patch.object(
                            agent_mux_delivery.shutil,
                            "which",
                            return_value="/opt/amx",
                        ),
                        mock.patch.object(
                            agent_mux_delivery.subprocess, "run", return_value=completed
                        ) as run,
                    ):
                        run_persistence._notify_agent_mux_loop_finished(payload)
                    run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
