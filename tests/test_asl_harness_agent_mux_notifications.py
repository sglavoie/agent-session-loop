"""Agent-mux notifications emitted around fake-harness turns."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    config,
    plans_files,
    run_persistence,
    turn_execution,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.harness_integration_support import LOOP_RUN_TIMEOUT_SECONDS
from tests.support import (
    REPO_ROOT,
    FakeRunControlListener,
    init_git_repo,
    install_fake_amx,
    install_fake_codex,
    install_fake_harness,
    read_jsonl,
    write_plan,
)


class AgentMuxNotificationTests(FakeHarnessIntegrationBase):
    def test_loop_lifecycle_notifications_run_after_state_writes(self) -> None:
        listener = FakeRunControlListener()
        observed: list[tuple[str, object]] = []

        def read_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
            return json.loads(
                Path(str(payload["state_path"])).read_text(encoding="utf-8")
            )

        def notify_started(
            payload: dict[str, Any],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("started", state_payload["active_turn"]))
            self.assertEqual(payload["run_id"], state_payload["run_id"])
            self.assertEqual("codex", state_payload["harness"])
            self.assertIsNone(state_payload["active_turn"])

        def notify_turn_started(
            payload: dict[str, Any],
            active_turn: dict[str, Any],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            state_turn = state_payload["active_turn"]
            self.assertIsInstance(state_turn, dict)
            assert isinstance(state_turn, dict)
            observed.append(("turn_started", state_turn["harness"]))
            self.assertEqual(active_turn["turn_index"], state_turn["turn_index"])
            self.assertEqual("codex", state_turn["harness"])

        def notify_finished(
            payload: dict[str, Any],
            persistence: object = None,
        ) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("finished", state_payload["run_finished_at"]))
            self.assertEqual(
                payload["run_finished_at"], state_payload["run_finished_at"]
            )
            self.assertIsNone(state_payload["active_turn"])

        with (
            mock.patch.object(
                run_persistence,
                "_notify_agent_mux_loop_started",
                side_effect=notify_started,
            ),
            mock.patch.object(
                turn_execution,
                "notify_turn_started",
                side_effect=notify_turn_started,
            ),
            mock.patch.object(
                run_persistence,
                "_notify_agent_mux_loop_finished",
                side_effect=notify_finished,
            ),
        ):
            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending",),
                    listener=listener,
                )
            )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            ["started", "turn_started", "finished"],
            [name for name, _value in observed],
        )

    def test_copilot_run_from_other_cwd_notifies_agent_mux_with_project_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            fake_copilot = install_fake_harness(tmp_dir, "copilot")
            _fake_amx, amx_log = install_fake_amx(tmp_dir)
            caller_dir = tmp_dir / "caller"
            project_dir = tmp_dir / "project"
            caller_dir.mkdir()
            init_git_repo(project_dir)
            write_plan(plan_dir, ("pending",))
            config_path.write_text(
                json.dumps(
                    {
                        "codex": {"executable": str(fake_codex)},
                        "copilot": {"executable": str(fake_copilot)},
                    }
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "FAKE_CODEX_MODE": "success",
                    "FAKE_CODEX_SLEEP": "5",
                    "FAKE_AMX_LOG": str(amx_log),
                    # The suite environment forces notification off; this
                    # test exercises real delivery against the fake amx.
                    agent_mux_delivery.NOTIFY_ENV_VAR: "auto",
                    # Under parallel-shard load the fake amx interpreter can
                    # take longer than the 1s production timeout to start,
                    # which silently drops the notification record.
                    agent_mux_delivery.NOTIFY_TIMEOUT_ENV_VAR: "30",
                    "PATH": str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
                    "PYTHONPATH": str(REPO_ROOT)
                    + os.pathsep
                    + env.get("PYTHONPATH", ""),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(project_dir),
                    "--harness",
                    "copilot",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "--allow-not-loop-ready",
                ],
                cwd=caller_dir,
                env=env,
                text=True,
                capture_output=True,
                timeout=LOOP_RUN_TIMEOUT_SECONDS,
                check=False,
            )
            payload = json.loads(completed.stdout)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("copilot", payload["harness"])
            expected_cwd = str(plans_files.canonicalize_path(project_dir))
            self.assertEqual(expected_cwd, payload["cwd"])

            amx_records = read_jsonl(amx_log)
            self.assertEqual(
                [
                    agent_mux_delivery.START_EVENT,
                    agent_mux_delivery.TURN_START_EVENT,
                    agent_mux_delivery.TURN_FINISH_EVENT,
                    agent_mux_delivery.FINISH_EVENT,
                ],
                [record["argv"][-1] for record in amx_records],
            )
            self.assertEqual(
                {str(plans_files.canonicalize_path(caller_dir))},
                {record["cwd"] for record in amx_records},
            )

            hook_payloads = [json.loads(str(record["stdin"])) for record in amx_records]
            self.assertEqual(
                [
                    agent_mux_delivery.START_REASON,
                    agent_mux_delivery.TURN_START_REASON,
                    agent_mux_delivery.TURN_FINISH_REASON,
                    agent_mux_delivery.FINISH_REASON,
                ],
                [hook_payload["reason"] for hook_payload in hook_payloads],
            )
            self.assertEqual(
                ["between", "turn", "between", "finished"],
                [hook_payload["phase"] for hook_payload in hook_payloads],
            )
            # A nested ASL subprocess allocates and persists the per-run
            # sequence: strings, starting at 1, monotonic across the run.
            self.assertEqual(
                ["1", "2", "3", "4"],
                [hook_payload["source_sequence"] for hook_payload in hook_payloads],
            )
            for hook_payload in hook_payloads:
                self.assertEqual("agent_session_loop", hook_payload["source"])
                self.assertEqual("copilot", hook_payload["harness"])
                self.assertEqual(expected_cwd, hook_payload["cwd"])
                self.assertEqual(payload["plan_dir"], hook_payload["plan_dir"])
                self.assertEqual(payload["state_path"], hook_payload["state_path"])
                self.assertEqual(payload["run_id"], hook_payload["run_id"])
                self.assertEqual(
                    f"{payload['run_id']}:{hook_payload['source_sequence']}",
                    hook_payload["source_event_id"],
                )
                self.assertTrue(hook_payload["emitted_time"].isdigit())
                self.assertTrue(Path(str(hook_payload["state_path"])).exists())

            turn_payload = hook_payloads[1]
            self.assertEqual(1, turn_payload["turn_index"])
            self.assertEqual(1, turn_payload["session"])
            self.assertEqual("1", turn_payload["session_id"])
            self.assertEqual(
                str(payload["turns"][0]["artifact_dir"]),
                turn_payload["artifact_dir"],
            )
            final_state = json.loads(
                Path(payload["state_path"]).read_text(encoding="utf-8")
            )
            self.assertIsNone(final_state["active_turn"])
            self.assertEqual(4, final_state["agent_mux_sequence"])

    def test_harness_switch_updates_agent_mux_turn_notification_harness(self) -> None:
        hook_payloads: list[dict[str, Any]] = []
        finish_payloads: list[dict[str, Any]] = []

        def notify_turn_started(
            payload: dict[str, Any],
            active_turn: dict[str, Any],
            persistence: object = None,
        ) -> None:
            hook_payloads.append(
                run_persistence._agent_mux_loop_payload(
                    payload,
                    reason=agent_mux_delivery.TURN_START_REASON,
                    active_turn=active_turn,
                )
            )

        def notify_finished(
            payload: dict[str, Any],
            persistence: object = None,
        ) -> None:
            finish_payloads.append(
                run_persistence._agent_mux_loop_payload(
                    payload,
                    reason=agent_mux_delivery.FINISH_REASON,
                )
            )

        with (
            mock.patch.object(
                turn_execution,
                "notify_turn_started",
                side_effect=notify_turn_started,
            ),
            mock.patch.object(
                run_persistence,
                "_notify_agent_mux_loop_finished",
                side_effect=notify_finished,
            ),
        ):
            exit_code, payload, _calls, _plan_dir, _state_dir = (
                self.run_with_mocked_harness_switch()
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            ["codex", "claude"],
            [turn["harness"] for turn in payload["turns"]],
        )
        self.assertEqual(
            ["codex", "claude"],
            [hook_payload["harness"] for hook_payload in hook_payloads],
        )
        self.assertEqual(
            [1, 2],
            [hook_payload["turn_index"] for hook_payload in hook_payloads],
        )
        self.assertEqual(
            [1, 2],
            [hook_payload["session"] for hook_payload in hook_payloads],
        )
        self.assertEqual(1, len(finish_payloads))
        self.assertEqual("claude", finish_payloads[0]["harness"])
