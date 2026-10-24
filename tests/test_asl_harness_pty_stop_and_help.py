"""Interactive PTY run keys: stop, cancel, interrupt and help."""

from __future__ import annotations

import os
import signal
from pathlib import Path

from agent_session_loop import (
    events,
    state,
    turn_execution_constants,
)
from tests.harness_integration_pty import PtyRunHarnessBase
from tests.harness_integration_support import STREAM_GATE_TIMEOUT_SECONDS
from tests.support import (
    read_jsonl,
    wait_until,
)

ASL_TEST_LANE = "integration"


class PtyStopAndHelpTests(PtyRunHarnessBase):
    def test_text_run_pty_p_stops_after_current_session(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current requested" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertIn("Run complete: 1 session(s) completed", output)
        self.assertIn("Next pending: session 2", output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": None,
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": True,
                "last_action": "stop-after-current-consumed",
            },
            payload["control"],
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-consumed",
            ],
            [event["action"] for event in control_events],
        )

    def test_text_run_pty_second_p_cancels_stop_after_current(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current requested" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current canceled" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": None,
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": False,
                "last_action": "stop-after-current-canceled",
            },
            payload["control"],
        )
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-canceled",
            ],
            [event["action"] for event in control_events],
        )

    def test_text_run_pty_ctrl_c_pauses_with_note(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        note = "operator reference ABC-123"
        gate_dir = state_dir / "ctrl-c-stream-gates"
        # The turn must still be in flight when SIGINT lands: hold the harness at
        # the stream gate instead of racing a step sleep, which a loaded machine
        # can outlast.
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            note=note,
            fake_step_sleep="0",
            extra_env={
                "FAKE_CODEX_STREAM_GATE_DIR": str(gate_dir),
                "FAKE_CODEX_STREAM_GATE_TIMEOUT": str(STREAM_GATE_TIMEOUT_SECONDS),
            },
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/1 started" in text,
            process=process,
        )

        def harness_started():
            if not (gate_dir / "session-1.ready").exists():
                return None
            payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
            active_turn = payload.get("active_turn")
            if not isinstance(active_turn, dict):
                return None
            stdout_path = Path(active_turn["stdout_path"])
            if not stdout_path.exists():
                return None
            if "stdout before completion" in stdout_path.read_text(encoding="utf-8"):
                return active_turn
            return None

        wait_until(harness_started, timeout=STREAM_GATE_TIMEOUT_SECONDS)
        process.send_signal(signal.SIGINT)
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(
            turn_execution_constants.INTERRUPTED_EXIT, process.returncode, output
        )
        self.assertIn(f"Note: {note}", output)
        self.assertIn("Run paused: interrupted", output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("interrupted", payload["pause_reason"])
        self.assertEqual(note, payload["note"])
        self.assertIsNone(payload["active_turn"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertTrue(payload["turns"][0]["interrupted"])
        self.assertEqual("interrupted", payload["turns"][0]["status_result"])
        self.assertFalse(state.lock_path_for_plan(plan_dir, state_dir).exists())

    def test_text_run_pty_h_prints_one_help_line(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/1 started" in text,
            process=process,
        )
        os.write(master_fd, b"h")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "controls: p toggle stop-after-current" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertRegex(
            output,
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"controls: p toggle stop-after-current "
            r"\(currently off\); s show status; "
            r"t toggle live tail \(currently off\); "
            r"1 codex, 2 claude, 3 copilot select next harness "
            r"\(currently unchanged\); h show help",
        )
        self.assertEqual(1, output.count("controls: p toggle stop-after-current"))
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        control_events = [
            record
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual([], control_events)
