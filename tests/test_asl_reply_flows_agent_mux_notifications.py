"""Agent-mux wait and turn-hang notification payload tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    events,
    run_coordination_constants,
    run_persistence,
    turn_execution_protocols,
)
from tests.support import (
    agent_mux_notify_enabled,
    read_jsonl,
)

ASL_TEST_LANE = "integration"


class AgentMuxWaitNotificationTests(unittest.TestCase):
    """Wait lifecycle delivery payloads, preview bounds, and fail-open behavior."""

    def setUp(self) -> None:
        self.enterContext(agent_mux_notify_enabled())
        agent_mux_delivery.reset_legacy_cache()
        self.addCleanup(agent_mux_delivery.reset_legacy_cache)

    @staticmethod
    def _accepted_delivery() -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            ["/opt/amx"],
            0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "status": "accepted",
                    "retryable": False,
                    "reason": "accepted",
                    "trace_id": "trace-wait",
                }
            ),
            stderr="",
        )

    @staticmethod
    def _wait_payload(tmp_dir: Path) -> dict[str, Any]:
        return {
            "run_id": "run-wait",
            "plan_dir": str(tmp_dir / "plan"),
            "cwd": str(tmp_dir / "repo"),
            "harness": "codex",
            "state_path": str(tmp_dir / "state.json"),
            "events_path": str(tmp_dir / "events.jsonl"),
        }

    def test_question_preview_collapses_whitespace_and_caps_length(self) -> None:
        self.assertEqual(
            "Should we use 1a or 1b?",
            run_persistence._question_preview("  Should we use\n\t1a  or 1b?  \n"),
        )
        self.assertIsNone(run_persistence._question_preview(None))
        self.assertIsNone(run_persistence._question_preview("  \n\t "))

        capped = run_persistence._question_preview("x" * 500)
        self.assertEqual(run_persistence.AGENT_MUX_QUESTION_PREVIEW_CHARS, len(capped))
        self.assertEqual("x" * run_persistence.AGENT_MUX_QUESTION_PREVIEW_CHARS, capped)

        multiline = run_persistence._question_preview("line\n" * 400)
        self.assertLessEqual(
            len(multiline), run_persistence.AGENT_MUX_QUESTION_PREVIEW_CHARS
        )
        self.assertNotIn("\n", multiline)

    def test_wait_started_delivers_bounded_question_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._wait_payload(Path(tmp))

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=self._accepted_delivery(),
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_wait_started(
                    payload,
                    question_id="question-1",
                    question_text="Should we use\n\n1a or 1b? " + "detail " * 100,
                )

            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual("AgentSessionLoopWaitStarted", args[0][-1])
            hook_payload = json.loads(kwargs["input"])
            self.assertEqual("wait_started", hook_payload["reason"])
            self.assertEqual("waiting", hook_payload["phase"])
            self.assertEqual("question-1", hook_payload["question_id"])
            preview = hook_payload["question_preview"]
            self.assertTrue(preview.startswith("Should we use 1a or 1b? detail"))
            self.assertLessEqual(
                len(preview), run_persistence.AGENT_MUX_QUESTION_PREVIEW_CHARS
            )
            self.assertNotIn("wait_reason", hook_payload)

    def test_wait_started_omits_preview_for_blank_question_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._wait_payload(Path(tmp))

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=self._accepted_delivery(),
                ) as run,
            ):
                run_persistence._notify_agent_mux_loop_wait_started(
                    payload,
                    question_id="question-1",
                    question_text=None,
                )

            hook_payload = json.loads(run.call_args.kwargs["input"])
            self.assertEqual("question-1", hook_payload["question_id"])
            self.assertNotIn("question_preview", hook_payload)

    def test_wait_ended_passes_every_wait_reason_through(self) -> None:
        for wait_reason in (
            "reply",
            "timeout",
            run_coordination_constants.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            "error",
        ):
            with (
                self.subTest(wait_reason=wait_reason),
                tempfile.TemporaryDirectory() as tmp,
            ):
                payload = self._wait_payload(Path(tmp))

                with (
                    mock.patch.object(
                        agent_mux_delivery.shutil,
                        "which",
                        return_value="/opt/amx",
                    ),
                    mock.patch.object(
                        agent_mux_delivery.subprocess,
                        "run",
                        return_value=self._accepted_delivery(),
                    ) as run,
                ):
                    run_persistence._notify_agent_mux_loop_wait_ended(
                        payload,
                        question_id="question-1",
                        wait_reason=wait_reason,
                    )

                args, kwargs = run.call_args
                self.assertEqual("AgentSessionLoopWaitEnded", args[0][-1])
                hook_payload = json.loads(kwargs["input"])
                self.assertEqual("wait_ended", hook_payload["reason"])
                self.assertEqual("between", hook_payload["phase"])
                self.assertEqual("question-1", hook_payload["question_id"])
                self.assertEqual(wait_reason, hook_payload["wait_reason"])
                self.assertNotIn("question_preview", hook_payload)

    def test_wait_notifications_stay_fail_open_when_amx_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self._wait_payload(tmp_dir)
            events_path = Path(str(payload["events_path"]))

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value=None
                ),
                mock.patch.object(agent_mux_delivery.subprocess, "run") as run,
            ):
                run_persistence._notify_agent_mux_loop_wait_started(
                    payload,
                    question_id="question-1",
                    question_text="Should we use 1a or 1b?",
                )
                run_persistence._notify_agent_mux_loop_wait_ended(
                    payload,
                    question_id="question-1",
                    wait_reason="timeout",
                )

            run.assert_not_called()
            outcomes = [
                record["payload"]
                for record in read_jsonl(events_path)
                if record.get("name") == events.AGENT_MUX_DELIVERY_EVENT
            ]
            self.assertEqual(
                [
                    agent_mux_delivery.WAIT_START_EVENT,
                    agent_mux_delivery.WAIT_END_EVENT,
                ],
                [outcome["agent_mux_event"] for outcome in outcomes],
            )
            for outcome in outcomes:
                self.assertEqual("unavailable", outcome["final_status"])
                self.assertEqual("agent_mux_missing", outcome["reason"])
                self.assertFalse(outcome["acknowledged"])


class AgentMuxTurnHangNotificationTests(unittest.TestCase):
    """Hang lifecycle delivery payload bounds, phase mapping, and fail-open."""

    def setUp(self) -> None:
        self.enterContext(agent_mux_notify_enabled())
        agent_mux_delivery.reset_legacy_cache()
        self.addCleanup(agent_mux_delivery.reset_legacy_cache)

    @staticmethod
    def _accepted_delivery() -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            ["/opt/amx"],
            0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "status": "accepted",
                    "retryable": False,
                    "reason": "accepted",
                    "trace_id": "trace-hang",
                }
            ),
            stderr="",
        )

    @staticmethod
    def _hang_payload(tmp_dir: Path) -> dict[str, Any]:
        return {
            "run_id": "run-hang",
            "plan_dir": str(tmp_dir / "plan"),
            "cwd": str(tmp_dir / "repo"),
            "harness": "claude",
            "state_path": str(tmp_dir / "state.json"),
            "events_path": str(tmp_dir / "events.jsonl"),
        }

    @staticmethod
    def _active_turn() -> dict[str, Any]:
        return {
            "turn_index": 3,
            "session": "2",
            "harness": "claude",
            "heartbeat_count": 7,
        }

    def test_turn_hang_suspected_delivers_bounded_scalars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._hang_payload(Path(tmp))

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=self._accepted_delivery(),
                ) as run,
            ):
                turn_execution_protocols.notify_turn_hang_suspected(
                    payload,
                    self._active_turn(),
                    hang_reason="result_received_process_alive",
                    hang_idle_seconds=187.5,
                    harness_pid=4242,
                )

            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual("AgentSessionLoopTurnHangSuspected", args[0][-1])
            hook_payload = json.loads(kwargs["input"])
            self.assertEqual("turn_hang_suspected", hook_payload["reason"])
            self.assertEqual("turn", hook_payload["phase"])
            self.assertEqual(
                "result_received_process_alive", hook_payload["hang_reason"]
            )
            self.assertEqual(187.5, hook_payload["hang_idle_seconds"])
            self.assertEqual(4242, hook_payload["harness_pid"])
            self.assertEqual(3, hook_payload["turn_index"])
            self.assertEqual("2", hook_payload["session"])
            self.assertNotIn("process_tree", hook_payload)

    def test_turn_hang_cleared_maps_to_turn_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._hang_payload(Path(tmp))

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=self._accepted_delivery(),
                ) as run,
            ):
                turn_execution_protocols.notify_turn_hang_cleared(
                    payload,
                    self._active_turn(),
                    hang_reason="output_resumed",
                    hang_idle_seconds=201.0,
                    harness_pid=4242,
                )

            args, kwargs = run.call_args
            self.assertEqual("AgentSessionLoopTurnHangCleared", args[0][-1])
            hook_payload = json.loads(kwargs["input"])
            self.assertEqual("turn_hang_cleared", hook_payload["reason"])
            self.assertEqual("turn", hook_payload["phase"])
            self.assertEqual("output_resumed", hook_payload["hang_reason"])
            self.assertEqual(201.0, hook_payload["hang_idle_seconds"])
            self.assertEqual(4242, hook_payload["harness_pid"])
            self.assertNotIn("process_tree", hook_payload)

    def test_hang_notifications_omit_unknown_scalars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._hang_payload(Path(tmp))

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value="/opt/amx"
                ),
                mock.patch.object(
                    agent_mux_delivery.subprocess,
                    "run",
                    return_value=self._accepted_delivery(),
                ) as run,
            ):
                turn_execution_protocols.notify_turn_hang_suspected(
                    payload,
                    self._active_turn(),
                    hang_reason="zero_cpu_ceiling",
                    hang_idle_seconds=None,
                    harness_pid=None,
                )

            hook_payload = json.loads(run.call_args.kwargs["input"])
            self.assertEqual("zero_cpu_ceiling", hook_payload["hang_reason"])
            self.assertNotIn("hang_idle_seconds", hook_payload)
            self.assertNotIn("harness_pid", hook_payload)

    def test_hang_notifications_stay_fail_open_when_amx_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self._hang_payload(tmp_dir)
            events_path = Path(str(payload["events_path"]))

            with (
                mock.patch.object(
                    agent_mux_delivery.shutil, "which", return_value=None
                ),
                mock.patch.object(agent_mux_delivery.subprocess, "run") as run,
            ):
                turn_execution_protocols.notify_turn_hang_suspected(
                    payload,
                    self._active_turn(),
                    hang_reason="result_received_process_alive",
                    hang_idle_seconds=90.0,
                    harness_pid=4242,
                )
                turn_execution_protocols.notify_turn_hang_cleared(
                    payload,
                    self._active_turn(),
                    hang_reason="output_resumed",
                    hang_idle_seconds=95.0,
                    harness_pid=4242,
                )

            run.assert_not_called()
            outcomes = [
                record["payload"]
                for record in read_jsonl(events_path)
                if record.get("name") == events.AGENT_MUX_DELIVERY_EVENT
            ]
            self.assertEqual(
                [
                    agent_mux_delivery.TURN_HANG_SUSPECTED_EVENT,
                    agent_mux_delivery.TURN_HANG_CLEARED_EVENT,
                ],
                [outcome["agent_mux_event"] for outcome in outcomes],
            )
            for outcome in outcomes:
                self.assertEqual("unavailable", outcome["final_status"])
                self.assertEqual("agent_mux_missing", outcome["reason"])
                self.assertFalse(outcome["acknowledged"])


if __name__ == "__main__":
    unittest.main()
