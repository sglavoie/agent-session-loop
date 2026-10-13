"""Interactive PTY run keys: harness selection, inline tail and commit timing."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agent_session_loop import config, events, run_selection, state, task_packet
from tests.harness_integration_pty import PtyRunHarnessBase
from tests.harness_integration_support import STREAM_GATE_TIMEOUT_SECONDS
from tests.support import (
    init_git_repo,
    read_jsonl,
    wait_until,
)


class PtyHarnessSelectAndTailTests(PtyRunHarnessBase):
    def test_text_run_pty_s_and_number_keys_select_future_harness(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(fake_codex)},
                    "claude": {"executable": str(fake_codex)},
                    "copilot": {"executable": str(fake_codex)},
                }
            ),
            encoding="utf-8",
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            note="operator reference ABC-123",
            fake_step_sleep="2.0",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"s")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "status: active session 1/2 turn 1" in text
                and "total " in text
                and "status note: operator reference ABC-123" in text
                and "status: harness active codex; future unchanged" in text
                and "status: controls stop-after-current off; live tail off" in text
            ),
            process=process,
        )

        os.write(master_fd, b"1")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness already codex" in text,
            process=process,
        )
        os.write(master_fd, b"2")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness selected: claude" in text,
            process=process,
        )
        os.write(master_fd, b"3")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness selected: copilot" in text,
            process=process,
        )
        os.write(master_fd, b"s")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "status: harness active codex; future copilot" in text,
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "session 2/2 started" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": "copilot",
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": False,
                "last_action": "harness-selected",
            },
            payload["control"],
        )
        turns = payload["turns"]
        self.assertEqual(["codex", "copilot"], [turn["harness"] for turn in turns])
        # Autonomous turns default to packet mode, so the prompt is the
        # harness-neutral task packet, not the `session-run` skill invocation.
        codex_prompt = turns[0]["argv"][-1]
        self.assertTrue(codex_prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX))
        self.assertNotIn("session-run", codex_prompt)
        # The copilot turn submits its prompt over ACP, so a process listing is
        # precisely where the packet must not appear. What the packet says is
        # asserted on the codex turn above: an autonomous prompt is
        # harness-neutral, so both turns were handed the same text.
        self.assertIn("--acp", turns[1]["argv"])
        self.assertNotIn("-p", turns[1]["argv"])
        for value in turns[1]["argv"]:
            self.assertNotIn(task_packet.TASK_PACKET_HEADING_PREFIX, value)

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["harness-selected", "harness-selected"],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            ["claude", "copilot"],
            [event["selected_harness"] for event in control_events],
        )
        self.assertEqual(
            ["claude", "copilot"],
            [event["next_harness"] for event in control_events],
        )

    def test_text_run_pty_t_toggles_inline_tail_for_new_log_lines(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        gate_dir = state_dir / "stream-gates"

        def ready_path(session_id: str) -> Path:
            return gate_dir / f"session-{session_id}.ready"

        def continue_path(session_id: str) -> Path:
            return gate_dir / f"session-{session_id}.continue"

        def release_session(session_id: str) -> None:
            gate_dir.mkdir(parents=True, exist_ok=True)
            continue_path(session_id).write_text("continue\n", encoding="utf-8")

        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            fake_step_sleep="0",
            extra_env={
                "FAKE_CODEX_STREAM_GATE_DIR": str(gate_dir),
                "FAKE_CODEX_STREAM_GATE_TIMEOUT": str(STREAM_GATE_TIMEOUT_SECONDS),
            },
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )

        def first_turn_logs_ready():
            if not ready_path("1").exists():
                return None
            if not state_path.exists():
                return None
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            active_turn = payload.get("active_turn")
            if not isinstance(active_turn, dict):
                return None
            stdout_path = Path(active_turn["stdout_path"])
            stderr_path = Path(active_turn["stderr_path"])
            if not stdout_path.exists() or not stderr_path.exists():
                return None
            if "session 1 stdout before completion" in stdout_path.read_text(
                encoding="utf-8"
            ) and "session 1 stderr before completion" in stderr_path.read_text(
                encoding="utf-8"
            ):
                return active_turn
            return None

        wait_until(first_turn_logs_ready, timeout=STREAM_GATE_TIMEOUT_SECONDS)
        os.write(master_fd, b"t")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: live tail enabled" in text,
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=1 turn=1 | session 1 stdout before completion" in text
            ),
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stderr session=1 turn=1 | session 1 stderr before completion" in text
            ),
            process=process,
        )
        release_session("1")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=1 turn=1 | session 1 stdout after completion" in text
            ),
            process=process,
        )

        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=2 turn=2 | session 2 stdout before completion" in text
            ),
            process=process,
        )
        os.write(master_fd, b"t")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: live tail disabled" in text,
            process=process,
        )
        release_session("2")
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertIn(
            "stdout session=2 turn=2 | session 2 stdout before completion",
            output,
        )
        self.assertNotIn(
            "stdout session=2 turn=2 | session 2 stdout after completion",
            output,
        )
        self.assertNotIn(
            "stderr session=2 turn=2 | session 2 stderr after completion",
            output,
        )
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
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
                "last_action": "tail-disabled",
            },
            payload["control"],
        )
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["tail-enabled", "tail-disabled"],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            [True, False],
            [event["tail_enabled"] for event in control_events],
        )

    def test_text_run_pty_commit_per_session_finishes_before_stop_consumed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
                statuses=("pending", "pending")
            )
            process, master_fd = self.start_run_pty(
                plan_dir=plan_dir,
                state_dir=state_dir,
                config_home=config_home,
                mode="streaming-success",
                cwd=repo_dir,
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
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
            payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
            commit_records = read_jsonl(commit_log)

        self.assertEqual(0, process.returncode, output)
        self.assertIn("commit 1 finished (session 1, status success,", output)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertTrue(payload["commits"][0]["success"])
        self.assertEqual(1, len(commit_records))
        event_records = read_jsonl(Path(payload["events_path"]))
        commit_finished = self.event_index(
            event_records,
            events.COMMIT_FINISHED_EVENT,
            session=1,
            scope="session",
        )
        consumed = self.event_index(
            event_records,
            events.RUN_CONTROL_EVENT,
            action="stop-after-current-consumed",
        )
        self.assertLess(commit_finished, consumed)
