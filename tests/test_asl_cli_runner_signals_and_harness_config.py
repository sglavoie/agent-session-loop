"""Runner pause signals, custom harness config and session prompt tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    config,
    harnesses,
    plans_payloads,
    run_prompts,
    turn_execution_artifacts,
    turn_execution_constants,
    turn_execution_signals,
)
from tests.support import (
    patched_env,
)


class RunnerSignalAndHarnessConfigTests(unittest.TestCase):
    """Runner pause signals, custom harness config and session prompt tests."""

    def test_runner_signals_ignore_stderr_pause_when_final_status_complete(
        self,
    ) -> None:
        result = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=0,
            stdout="Runner status: complete\n",
            stderr=(
                "```text\n"
                "RUNNER_PAUSE: unsafe - plan is not loop-ready; "
                "run `asl preflight <plan-dir> --mark-loop-ready` before "
                "continuing\n"
                "```\n"
            ),
            last_message="Runner status: complete\n",
        )

        signals = turn_execution_signals.runner_signals(result)
        self.assertEqual("complete", signals["runner_status"])
        self.assertEqual("last_message", signals["runner_status_source"])
        self.assertNotIn("runner_pause", signals)
        self.assertEqual(
            "complete",
            turn_execution_artifacts.turn_status_result(
                result=result,
                status_changes=(
                    plans_payloads.session_status_change_payload(
                        "9",
                        before="pending",
                        after="complete",
                    ),
                ),
                pending_completed=("9",),
                session_id="9",
                runner_signals=signals,
                plan_error=None,
            ),
        )

    def test_runner_signals_use_last_signal_within_selected_source(self) -> None:
        result = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=0,
            stdout=(
                "RUNNER_PAUSE: unsafe - quoted skill instructions\n"
                "Runner status: complete\n"
            ),
            stderr="RUNNER_PAUSE: unsafe - stale stderr\n",
            last_message="",
        )

        signals = turn_execution_signals.runner_signals(result)
        self.assertEqual(
            {"runner_status": "complete", "runner_status_source": "stdout"},
            signals,
        )

    def test_runner_status_source_absent_when_marker_only_in_stderr(self) -> None:
        stderr = "```text\nRunner status: complete\n```\n"
        result = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=0,
            stdout="working\n",
            stderr=stderr,
            last_message="",
        )

        signals = turn_execution_signals.runner_signals(result)
        self.assertNotIn("runner_status", signals)
        self.assertNotIn("runner_pause", signals)
        self.assertEqual(
            turn_execution_constants.RUNNER_STATUS_SOURCE_ABSENT,
            signals["runner_status_source"],
        )
        self.assertEqual(
            {
                "source": "stderr",
                "kind": "status",
                "preview": "Runner status: complete",
            },
            signals["runner_status_degraded"],
        )
        self.assertEqual(
            "complete",
            turn_execution_artifacts.turn_status_result(
                result=result,
                status_changes=(
                    plans_payloads.session_status_change_payload(
                        "9",
                        before="pending",
                        after="complete",
                    ),
                ),
                pending_completed=("9",),
                session_id="9",
                runner_signals=signals,
                plan_error=None,
            ),
        )

        trusted = turn_execution_signals.runner_signals(
            harnesses.HarnessResult(
                name="codex",
                argv=["codex"],
                returncode=0,
                stdout="working\n",
                stderr=stderr,
                last_message="Runner status: complete\n",
            )
        )
        self.assertEqual(
            {"runner_status": "complete", "runner_status_source": "last_message"},
            trusted,
        )

    def test_live_action_signal_ignores_stderr_runner_pause_text(self) -> None:
        self.assertIsNone(
            turn_execution_signals.live_action_signal(
                "stderr",
                "RUNNER_PAUSE: unsafe - quoted skill instructions\n",
            )
        )
        self.assertIsNotNone(
            turn_execution_signals.live_action_signal(
                "stderr",
                "approval required before continuing\n",
            )
        )

    def test_approval_phrase_the_agent_wrote_is_not_a_live_ask(self) -> None:
        # The literal line that opened ASI-24: a session editing
        # approval-handling code streamed its own diff, and the substring match
        # read it as the run stopping for a human.
        self.assertIsNone(
            turn_execution_signals.live_action_signal(
                "stderr",
                '+    "message": "approval required before continuing",\n',
            )
        )
        self.assertIsNone(
            turn_execution_signals.live_action_signal(
                "stderr",
                '-    "message": "needs approval",\n',
            )
        )
        self.assertIsNone(
            turn_execution_signals.live_action_signal(
                "stderr",
                '     "detail": "requires approval"\n',
            )
        )

    def test_a_prompt_on_its_own_line_still_asks(self) -> None:
        self.assertIsNotNone(
            turn_execution_signals.live_action_signal(
                "stderr",
                "Approval required: run npm install?\n",
            )
        )
        self.assertIsNotNone(
            turn_execution_signals.live_action_signal(
                "stderr",
                "building the bundle\nwaiting for approval\nstill waiting\n",
            )
        )

    def test_runner_signals_ignore_noop_pause_when_final_status_complete(
        self,
    ) -> None:
        result = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=0,
            stdout="",
            stderr="",
            last_message="Runner status: complete\nRUNNER_PAUSE: none\n",
        )

        signals = turn_execution_signals.runner_signals(result)
        self.assertEqual("complete", signals["runner_status"])
        self.assertEqual("last_message", signals["runner_status_source"])
        self.assertNotIn("runner_pause", signals)
        self.assertNotIn("runner_pause_source", signals)
        self.assertEqual(
            "complete",
            turn_execution_artifacts.turn_status_result(
                result=result,
                status_changes=(
                    plans_payloads.session_status_change_payload(
                        "9",
                        before="pending",
                        after="complete",
                    ),
                ),
                pending_completed=("9",),
                session_id="9",
                runner_signals=signals,
                plan_error=None,
            ),
        )

    def test_runner_signals_preserve_real_pause_alongside_noop(self) -> None:
        result = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=0,
            stdout="",
            stderr="",
            last_message=(
                "RUNNER_PAUSE: none\n"
                "RUNNER_PAUSE: blocked - interactive decision gates\n"
            ),
        )

        signals = turn_execution_signals.runner_signals(result)
        self.assertEqual(
            "blocked - interactive decision gates",
            signals["runner_pause"],
        )
        self.assertEqual("last_message", signals["runner_pause_source"])

    def test_live_action_signal_ignores_noop_runner_pause(self) -> None:
        self.assertIsNone(
            turn_execution_signals.live_action_signal("stdout", "RUNNER_PAUSE: none\n")
        )
        self.assertIsNotNone(
            turn_execution_signals.live_action_signal(
                "stdout",
                "RUNNER_PAUSE: blocked - needs input\n",
            )
        )

    def test_custom_harness_config_accepts_supported_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "harnesses.json"
            cases = [
                ({"codex": {"executable": "/bin/codex"}}, "/bin/codex"),
                ({"codex": "/bin/codex-short"}, "/bin/codex-short"),
                (
                    {"harnesses": {"codex": {"executable": "/bin/codex-nested"}}},
                    "/bin/codex-nested",
                ),
            ]

            for raw, expected in cases:
                with self.subTest(raw=raw):
                    config_path.write_text(json.dumps(raw), encoding="utf-8")
                    self.assertEqual(
                        {"codex": {"executable": expected}},
                        config.load_harness_config(config_path),
                    )
                    self.assertEqual(
                        expected,
                        config.configured_harness_executable("codex", config_path),
                    )

    def test_native_model_payload_reads_codex_toml_and_copilot_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_config = home / ".codex" / "config.toml"
            copilot_settings = home / ".copilot" / "settings.json"
            codex_config.parent.mkdir(parents=True)
            copilot_settings.parent.mkdir(parents=True)
            codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")
            copilot_settings.write_text(
                json.dumps({"model": "gpt-5-copilot"}),
                encoding="utf-8",
            )

            with patched_env(HOME=str(home)):
                self.assertEqual(
                    {
                        "model": "gpt-5-codex",
                        "model_source": config.MODEL_SOURCE_CODEX_CONFIG,
                    },
                    config.native_model_payload("codex"),
                )
                self.assertEqual(
                    {
                        "model": "gpt-5-copilot",
                        "model_source": config.MODEL_SOURCE_COPILOT_SETTINGS,
                    },
                    config.native_model_payload("copilot"),
                )

    def test_native_model_payload_falls_back_when_missing_or_unreadable(self) -> None:
        expected = {
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
        }
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patched_env(HOME=str(home)):
                self.assertEqual(expected, config.native_model_payload("codex"))
                self.assertEqual(expected, config.native_model_payload("copilot"))
                self.assertEqual(expected, config.native_model_payload("claude"))

                with mock.patch.object(
                    Path, "read_text", side_effect=OSError("denied")
                ):
                    self.assertEqual(expected, config.native_model_payload("codex"))
                    self.assertEqual(expected, config.native_model_payload("copilot"))

    def test_build_session_prompt_uses_harness_skill_prefix(self) -> None:
        self.assertTrue(
            run_prompts.build_session_prompt("codex", "/tmp/plan").startswith(
                "$session-run "
            )
        )
        self.assertIn(
            run_prompts.RUNNER_MANAGED_SESSION_ARG,
            run_prompts.build_session_prompt("codex", "/tmp/plan"),
        )
        self.assertTrue(
            run_prompts.build_session_prompt("claude", "/tmp/plan").startswith(
                "/session-run "
            )
        )

    def test_build_session_prompt_omits_explicit_session_selector(self) -> None:
        for harness in ("codex", "claude", "copilot"):
            with self.subTest(harness=harness):
                prompt = run_prompts.build_session_prompt(harness, "/tmp/plan")
                self.assertNotIn("--session", prompt)
                self.assertNotIn("session-1.md", prompt)
                self.assertNotIn("session-2.md", prompt)


if __name__ == "__main__":
    unittest.main()
