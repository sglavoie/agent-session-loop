"""Model control overrides and missing switched harnesses."""

from __future__ import annotations

import json
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
    plans_payloads,
    review_session_body,
    run_control,
    state,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    FakeRunControlListener,
    read_jsonl,
)

ASL_TEST_LANE = "integration"


class ModelControlTests(FakeHarnessIntegrationBase):
    def test_harness_selection_does_not_carry_explicit_model_and_effort(self) -> None:
        exit_code, payload, calls, _plan_dir, _state_dir = (
            self.run_with_mocked_harness_switch(harness_model="gpt-5", effort="high")
        )

        self.assertEqual(0, exit_code)
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        self.assertEqual(["gpt-5", None], [call["model"] for call in calls])
        self.assertEqual(["high", "high"], [call["effort"] for call in calls])

        turns = payload["turns"]
        self.assertEqual(["codex", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual(
            ["gpt-5", config.NATIVE_MODEL_DEFAULT],
            [turn["model"] for turn in turns],
        )
        self.assertEqual(
            [config.MODEL_SOURCE_FLAG, config.MODEL_SOURCE_UNRESOLVED],
            [turn["model_source"] for turn in turns],
        )
        self.assertEqual(["high", "high"], [turn["effort"] for turn in turns])
        self.assertEqual(
            [config.EFFORT_SOURCE_FLAG, config.EFFORT_SOURCE_FLAG],
            [turn["effort_source"] for turn in turns],
        )

    def test_model_control_changes_next_invocation_and_clear_restores_defaults(
        self,
    ) -> None:
        listener = FakeRunControlListener()
        calls: list[dict[str, Any]] = []
        paths: dict[str, Path] = {}

        def remember_paths(plan_dir: Path, state_dir: Path) -> None:
            paths["plan_dir"] = plan_dir
            paths["state_dir"] = state_dir

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

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            calls.append({"harness": name, **kwargs})
            plan_dir = Path(kwargs["plan_dir"])
            complete_next_pending(plan_dir)
            if len(calls) == 1:
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_SELECT_FUTURE_HARNESS,
                    state_dir=paths["state_dir"],
                    harness="claude",
                    command_id="select-claude",
                )
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
                    state_dir=paths["state_dir"],
                    harness_model="claude-opus-control",
                    effort="high",
                    command_id="set-claude",
                )
            elif len(calls) == 2:
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
                    state_dir=paths["state_dir"],
                    clear_harness_model=True,
                    clear_effort=True,
                    command_id="clear-claude",
                )
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["model"]), str(kwargs["effort"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=fake_run_harness,
        ):
            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending", "pending", "pending"),
                    listener=listener,
                    harness_model="gpt-5-flag",
                    effort="medium",
                    harness_config_overrides={"claude": sys.executable},
                    before_run=remember_paths,
                )
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            ["codex", "claude", "claude"],
            [call["harness"] for call in calls],
        )
        self.assertEqual(
            ["gpt-5-flag", "claude-opus-control", None],
            [call["model"] for call in calls],
        )
        self.assertEqual(
            ["medium", "high", "medium"],
            [call["effort"] for call in calls],
        )
        turns = payload["turns"]
        self.assertEqual(
            [
                config.MODEL_SOURCE_FLAG,
                config.MODEL_SOURCE_CONTROL,
                config.MODEL_SOURCE_UNRESOLVED,
            ],
            [turn["model_source"] for turn in turns],
        )
        self.assertEqual(
            [
                config.EFFORT_SOURCE_FLAG,
                config.EFFORT_SOURCE_CONTROL,
                config.EFFORT_SOURCE_FLAG,
            ],
            [turn["effort_source"] for turn in turns],
        )
        self.assertEqual({}, payload["control"]["implementation_overrides"])
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_HARNESS_SELECTED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
            ],
            [event["action"] for event in control_events],
        )
        self.assertEqual("claude", control_events[3]["target_harness"])

    def test_model_control_expires_when_a_new_runner_process_starts(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        control_queue.append_command(
            plan_dir,
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            state_dir=state_dir,
            harness="codex",
            harness_model="gpt-5-control",
            effort="high",
        )

        first_completed, first_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
            harness_model="gpt-5-flag",
            effort="medium",
        )
        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
            harness_model="gpt-5-flag",
            effort="medium",
        )

        self.assertEqual(0, first_completed.returncode, first_completed.stderr)
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        first_turn = first_payload["turns"][0]
        second_turn = second_payload["turns"][0]
        self.assertEqual("gpt-5-control", first_turn["model"])
        self.assertEqual(config.MODEL_SOURCE_CONTROL, first_turn["model_source"])
        self.assertEqual("high", first_turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_CONTROL, first_turn["effort_source"])
        self.assertEqual("gpt-5-flag", second_turn["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, second_turn["model_source"])
        self.assertEqual("medium", second_turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, second_turn["effort_source"])

    def test_model_control_does_not_change_generated_review_invocation(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("complete",)
        )
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        configured = json.loads(config_path.read_text(encoding="utf-8"))
        configured["claude"] = {"executable": str(fake_codex)}
        config_path.write_text(json.dumps(configured), encoding="utf-8")
        review_session_body.ensure_generated_review_session(
            plan_dir, review_harness="claude"
        )
        control_queue.append_command(
            plan_dir,
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            state_dir=state_dir,
            harness="claude",
            harness_model="claude-control",
            effort="high",
        )

        completed, payload = self.run_loop_command(
            mode="success-with-review",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            review_harness="claude",
            review_harness_model="claude-review",
            effort="medium",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        turn = payload["turns"][0]
        self.assertEqual(plans_payloads.SESSION_KIND_REVIEW, turn["session_kind"])
        self.assertEqual("claude", turn["harness"])
        self.assertEqual("claude-review", turn["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turn["model_source"])
        self.assertEqual("medium", turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, turn["effort_source"])
        self.assertEqual(
            "claude-review", turn["argv"][turn["argv"].index("--model") + 1]
        )
        self.assertEqual("medium", turn["argv"][turn["argv"].index("--effort") + 1])

    def test_missing_switched_harness_pauses_before_next_turn(self) -> None:
        listener = FakeRunControlListener()
        calls: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory() as tmp:
            missing_claude = Path(tmp) / "missing-claude"

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
                if listener.on_action is not None:
                    listener.on_action(
                        run_control.RunControlAction(
                            action="harness-selected",
                            stop_after_current=False,
                            next_harness="claude",
                        )
                    )
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, str(kwargs["prompt"])],
                    returncode=0,
                    stdout=f"{name} completed\n",
                    stderr="",
                )

            with mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=fake_run_harness,
            ):
                exit_code, payload, plan_dir, state_dir, _fake_codex = (
                    self.run_locked_with_listener(
                        mode="success",
                        statuses=("pending", "pending"),
                        listener=listener,
                        harness_config_overrides={
                            "claude": str(missing_claude),
                        },
                    )
                )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("harness-unavailable", payload["reason"])
        self.assertEqual("harness-unavailable", payload["pause_reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(["1"], payload["completed_session_ids"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual("complete", payload["turns"][0]["status_result"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual("2", payload["final_snapshot"]["next_pending_id"])
        self.assertEqual(["codex"], [call["harness"] for call in calls])
        self.assertFalse(state.lock_path_for_plan(plan_dir, state_dir).exists())

        details = payload["pause_details"]
        self.assertEqual(2, details["session"])
        self.assertEqual("2", details["session_id"])
        self.assertEqual("session-2.md", details["session_file"])
        self.assertEqual("claude", details["requested_harness"])
        self.assertEqual(str(missing_claude), details["executable"])
        self.assertIsNone(details["resolved_executable"])
        self.assertEqual(payload["harness_config"]["path"], details["config_path"])
        self.assertIn("claude executable not found", details["error"])

        event_records = read_jsonl(Path(payload["events_path"]))
        self.assertEqual(
            1,
            len(
                [record for record in event_records if record["name"] == "turn_started"]
            ),
        )
        paused_event = event_records[
            self.event_index(
                event_records,
                events.RUN_PAUSED_EVENT,
                reason="harness-unavailable",
            )
        ]
        paused_details = paused_event["payload"]["details"]
        self.assertEqual("claude", paused_details["requested_harness"])
        self.assertEqual(str(missing_claude), paused_details["executable"])
