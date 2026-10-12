"""External control-queue actions and harness selection."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    config,
    control_queue,
    events,
    harnesses,
    run_control,
    run_loop,
    state,
    task_packet,
    turn_execution,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    patched_env,
    read_jsonl,
    wait_until,
)


class ExternalQueueAndSelectionTests(FakeHarnessIntegrationBase):
    def test_external_queue_select_future_harness_switches_next_turn(self) -> None:
        listener = FakeRunControlListener(current_harness="codex")
        calls: list[dict[str, Any]] = []
        plan_dir_box: list[Path] = []
        state_dir_box: list[Path] = []

        def complete_next_pending(plan_dir: Path) -> None:
            for session_file in sorted(plan_dir.glob("session-*.md")):
                text = session_file.read_text(encoding="utf-8")
                if "- **Status**: pending" not in text:
                    continue
                session_file.write_text(
                    text.replace(
                        "- **Status**: pending",
                        "- **Status**: complete",
                        1,
                    ),
                    encoding="utf-8",
                )
                return

        def fake_run_harness(
            name: str,
            **kwargs: object,
        ) -> harnesses.HarnessResult:
            calls.append({"harness": name, **kwargs})
            complete_next_pending(Path(kwargs["plan_dir"]))
            if len(calls) == 1:
                control_queue.append_command(
                    plan_dir_box[0],
                    control_queue.COMMAND_SELECT_FUTURE_HARNESS,
                    state_dir=state_dir_box[0],
                    harness="claude",
                    command_id="ext-sel-1",
                )
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending"),
            harness_config_overrides={"claude": sys.executable},
        )
        plan_dir_box.append(plan_dir)
        state_dir_box.append(state_dir)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=fake_run_harness,
        ):
            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(REPO_ROOT),
                harness="codex",
                state_dir=str(state_dir),
                json=False,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                harness_model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
            )
            real_state_dir = state.plan_state_dir(plan_dir, state_dir)
            listener.state_path = real_state_dir / events.STATE_FILE_NAME
            listener.events_path = real_state_dir / events.EVENTS_FILE_NAME

            def fake_for_run(*_args, **kwargs):
                listener.on_action = kwargs.get("on_action")
                listener.on_status = kwargs.get("on_status")
                return listener

            with (
                patched_env(
                    XDG_CONFIG_HOME=str(config_home),
                    FAKE_CODEX_MODE="success",
                    FAKE_CODEX_SLEEP="5",
                    PYTHONPATH=str(REPO_ROOT)
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                ),
                mock.patch.object(
                    run_control.RunControlListener,
                    "for_run",
                    side_effect=fake_for_run,
                ),
            ):
                lock_info = state.acquire_lock(plan_dir, state_dir)
                try:
                    exit_code, payload = run_loop.run_locked(
                        args=args,
                        cwd=REPO_ROOT,
                        timeout=None,
                        max_sessions=None,
                        commit_mode=None,
                        lock_info=lock_info,
                        run_id="test-run",
                    )
                finally:
                    state.release_lock(plan_dir, state_dir)

        self.assertEqual(0, exit_code, payload)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        self.assertEqual("claude", payload["control"]["next_harness"])

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        actions = [event["action"] for event in control_events]
        self.assertIn("harness-selected", actions)

    def test_external_queue_applies_while_harness_subprocess_is_active(self) -> None:
        """A queue command written mid-turn is applied by periodic polling."""

        listener = FakeRunControlListener(current_harness="codex")
        calls: list[dict[str, Any]] = []
        plan_dir_box: list[Path] = []
        state_dir_box: list[Path] = []
        stop_state_during_call: list[bool] = []

        def complete_next_pending(plan_dir: Path) -> None:
            for session_file in sorted(plan_dir.glob("session-*.md")):
                text = session_file.read_text(encoding="utf-8")
                if "- **Status**: pending" not in text:
                    continue
                session_file.write_text(
                    text.replace(
                        "- **Status**: pending",
                        "- **Status**: complete",
                        1,
                    ),
                    encoding="utf-8",
                )
                return

        def fake_run_harness(
            name: str,
            **kwargs: object,
        ) -> harnesses.HarnessResult:
            calls.append({"harness": name, **kwargs})
            plan_dir = Path(kwargs["plan_dir"])
            # Queue arrives while the harness is "running". Observe the
            # listener state before completing the session.
            control_queue.append_command(
                plan_dir_box[0],
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                state_dir=state_dir_box[0],
                command_id=f"midflight-{len(calls)}",
            )
            wait_until(lambda: listener.state.stop_after_current)
            stop_state_during_call.append(listener.state.stop_after_current)
            complete_next_pending(plan_dir)
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending"),
        )
        plan_dir_box.append(plan_dir)
        state_dir_box.append(state_dir)

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=fake_run_harness,
        ):
            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(REPO_ROOT),
                harness="codex",
                state_dir=str(state_dir),
                json=False,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                harness_model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
            )
            real_state_dir = state.plan_state_dir(plan_dir, state_dir)
            listener.state_path = real_state_dir / events.STATE_FILE_NAME
            listener.events_path = real_state_dir / events.EVENTS_FILE_NAME

            def fake_for_run(*_args, **kwargs):
                listener.on_action = kwargs.get("on_action")
                listener.on_status = kwargs.get("on_status")
                return listener

            with (
                patched_env(
                    XDG_CONFIG_HOME=str(config_home),
                    FAKE_CODEX_MODE="success",
                    FAKE_CODEX_SLEEP="5",
                    PYTHONPATH=str(REPO_ROOT)
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                ),
                mock.patch.object(
                    run_control.RunControlListener,
                    "for_run",
                    side_effect=fake_for_run,
                ),
                mock.patch.object(
                    turn_execution,
                    "LIVE_CONTROL_POLL_INTERVAL_SECONDS",
                    0.01,
                ),
            ):
                lock_info = state.acquire_lock(plan_dir, state_dir)
                try:
                    exit_code, _payload = run_loop.run_locked(
                        args=args,
                        cwd=REPO_ROOT,
                        timeout=None,
                        max_sessions=None,
                        commit_mode=None,
                        lock_info=lock_info,
                        run_id="test-run",
                    )
                finally:
                    state.release_lock(plan_dir, state_dir)

        self.assertEqual(0, exit_code)
        self.assertTrue(stop_state_during_call[0])
        # Exactly one harness call happened because the queued stop was
        # consumed after the current session and ended the run.
        self.assertEqual(1, len(calls))

    def test_harness_selection_applies_to_next_session_and_recomputes_native_model(
        self,
    ) -> None:
        home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(home_tmp.cleanup)
        home = Path(home_tmp.name)
        codex_config = home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")

        exit_code, payload, calls, _plan_dir, _state_dir = (
            self.run_with_mocked_harness_switch(extra_env={"HOME": str(home)})
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        # Packet mode is the default: both harnesses receive the harness-neutral
        # task packet rather than a `session-run` skill invocation.
        self.assertTrue(
            str(calls[0]["prompt"]).startswith(task_packet.TASK_PACKET_HEADING_PREFIX)
        )
        self.assertTrue(
            str(calls[1]["prompt"]).startswith(task_packet.TASK_PACKET_HEADING_PREFIX)
        )
        self.assertNotIn("session-run", str(calls[0]["prompt"]))
        self.assertNotIn("session-run", str(calls[1]["prompt"]))
        self.assertIsNone(calls[0]["model"])
        self.assertIsNone(calls[1]["model"])

        turns = payload["turns"]
        self.assertEqual(["codex", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual("gpt-5-codex", turns[0]["model"])
        self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, turns[0]["model_source"])
        self.assertEqual(config.NATIVE_MODEL_DEFAULT, turns[1]["model"])
        self.assertEqual(config.MODEL_SOURCE_UNRESOLVED, turns[1]["model_source"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
                "tail_enabled": False,
                "next_harness": "claude",
                "target_harness": None,
                "changed_fields": [],
                "implementation_overrides": {},
                "guards": [],
                "consumed": False,
                "last_action": "harness-selected",
            },
            payload["control"],
        )

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["harness-selected"],
            [event["action"] for event in control_events],
        )
        self.assertEqual("codex", control_events[0]["current_harness"])
        self.assertEqual("claude", control_events[0]["selected_harness"])
        self.assertEqual("claude", control_events[0]["next_harness"])
        self.assertEqual(1, control_events[0]["session"])
        self.assertEqual(1, control_events[0]["turn_index"])
