"""Harness adapter command and turn-plan construction tests."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    harness_claude,
    harness_codex,
    harness_copilot,
    harnesses,
    turn_execution,
)


class HarnessAdapterCommandTests(unittest.TestCase):
    """Harness adapter command and turn-plan construction tests."""

    def test_codex_turn_plan_uses_workspace_write_or_unsafe_bypass(self) -> None:
        """The sandbox choice moved from argv into the app-server turn plan."""
        adapter = harness_codex.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")

        safe = adapter.build_turn_plan(cwd=cwd, plan_dir=plan_dir)
        self.assertEqual("workspace-write", safe.thread_params["sandbox"])
        self.assertEqual("never", safe.thread_params["approvalPolicy"])
        self.assertEqual(
            [str(plan_dir)],
            safe.thread_params["config"]["sandbox_workspace_write"]["writable_roots"],
        )

        unsafe = adapter.build_turn_plan(cwd=cwd, plan_dir=plan_dir, unsafe_bypass=True)
        self.assertEqual("danger-full-access", unsafe.thread_params["sandbox"])
        self.assertNotIn("config", unsafe.thread_params)

    def test_codex_fallback_command_uses_workspace_write_or_unsafe_bypass(
        self,
    ) -> None:
        """The one-shot argv kept for the pre-acceptance fallback is unchanged."""
        adapter = harness_codex.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")
        last_message = Path("/tmp/state/last-message-1.txt")

        safe = adapter.build_exec_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            last_message_path=last_message,
        )
        self.assertIn("-s", safe)
        self.assertIn("workspace-write", safe)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", safe)
        self.assertNotIn("-a", safe)

        unsafe = adapter.build_exec_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            unsafe_bypass=True,
            last_message_path=last_message,
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", unsafe)
        self.assertNotIn("workspace-write", unsafe)

    def test_codex_command_includes_model_and_reasoning_effort_config(self) -> None:
        adapter = harness_codex.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")
        prompt = "$session-run /tmp/plan/"
        last_message = Path("/tmp/state/last-message-1.txt")

        plan = adapter.build_turn_plan(
            cwd=cwd,
            plan_dir=plan_dir,
            model="gpt-5",
            effort="high",
        )
        self.assertEqual("gpt-5", plan.thread_params["model"])
        self.assertEqual("high", plan.turn_params["effort"])

        argv = adapter.build_exec_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            last_message_path=last_message,
            model="gpt-5",
            effort="high",
        )

        output_index = argv.index("-o")
        self.assertEqual("gpt-5", argv[argv.index("--model") + 1])
        self.assertEqual('model_reasoning_effort="high"', argv[argv.index("-c") + 1])
        self.assertLess(argv.index("--model"), output_index)
        self.assertLess(argv.index("-c"), output_index)
        self.assertEqual(str(last_message), argv[output_index + 1])
        self.assertEqual(prompt, argv[-1])

    def test_adapter_command_shapes_for_claude_and_copilot(self) -> None:
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")

        claude = harness_claude.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
        )
        self.assertEqual("dontAsk", claude[claude.index("--permission-mode") + 1])
        self.assertEqual(
            "stream-json",
            claude[claude.index("--output-format") + 1],
        )
        self.assertIn("--verbose", claude)
        self.assertIn("--include-partial-messages", claude)
        self.assertIn("--add-dir", claude)

        claude_unsafe = harness_claude.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            unsafe_bypass=True,
        )
        self.assertEqual(
            "bypassPermissions",
            claude_unsafe[claude_unsafe.index("--permission-mode") + 1],
        )

        copilot_safe = harness_copilot.CopilotAdapter(executable="copilot").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            unsafe_bypass=False,
        )
        self.assertNotIn("--allow-all-tools", copilot_safe)
        self.assertNotIn("--allow-all-paths", copilot_safe)
        self.assertIn("--no-ask-user", copilot_safe)
        self.assertIn("--silent", copilot_safe)
        self.assertEqual(str(cwd), copilot_safe[copilot_safe.index("-C") + 1])
        self.assertEqual(
            str(plan_dir),
            copilot_safe[copilot_safe.index("--add-dir") + 1],
        )
        # The worker form ends at ``--acp``: the prompt travels the protocol,
        # so argv neither ends with it nor carries the one-shot ``-p``.
        self.assertEqual("--acp", copilot_safe[-1])
        self.assertNotIn("-p", copilot_safe)
        self.assertNotIn("/session-run /tmp/plan/", copilot_safe)

        copilot_unsafe = harness_copilot.CopilotAdapter(
            executable="copilot"
        ).build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            unsafe_bypass=True,
        )
        self.assertIn("--allow-all-tools", copilot_unsafe)
        self.assertIn("--allow-all-paths", copilot_unsafe)
        self.assertIn("--no-ask-user", copilot_unsafe)
        self.assertIn("--silent", copilot_unsafe)
        self.assertEqual(str(cwd), copilot_unsafe[copilot_unsafe.index("-C") + 1])

    def test_adapter_model_and_effort_flags_for_claude_and_copilot(self) -> None:
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")

        claude = harness_claude.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            model="claude-opus-4",
            effort="medium",
        )
        self.assertEqual("claude-opus-4", claude[claude.index("--model") + 1])
        self.assertEqual("medium", claude[claude.index("--effort") + 1])
        self.assertLess(claude.index("--model"), len(claude) - 1)
        self.assertLess(claude.index("--effort"), len(claude) - 1)
        # The prompt is a stdin message under streaming input, so it is not the
        # trailing argument any more -- and is nowhere on argv at all.
        self.assertNotIn("/session-run /tmp/plan/", claude)

        copilot = harness_copilot.CopilotAdapter(executable="copilot").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            model="gpt-5",
            effort="low",
        )
        # ``--acp`` closes the worker form, so the flags that configure the
        # session still have to precede it -- and the prompt is not on argv at
        # all any more, since the protocol carries it.
        acp_index = copilot.index("--acp")
        self.assertEqual("gpt-5", copilot[copilot.index("--model") + 1])
        self.assertEqual("low", copilot[copilot.index("--effort") + 1])
        self.assertLess(copilot.index("--model"), acp_index)
        self.assertLess(copilot.index("--effort"), acp_index)
        self.assertNotIn("/session-run /tmp/plan/", copilot)

    def test_claude_stream_json_output_is_rendered_and_preserves_last_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            fake_claude = tmp_dir / "claude"
            fake_claude.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json",
                        "records = [",
                        "    {",
                        "        'type': 'system',",
                        "        'subtype': 'init',",
                        "        'model': 'claude-test',",
                        "        'session_id': 'session-1',",
                        "    },",
                        "    {'type': 'system', 'subtype': 'status', 'status': "
                        "'requesting'},",
                        "    {",
                        "        'type': 'stream_event',",
                        "        'event': {",
                        "            'type': 'message_start',",
                        "            'message': {'model': 'claude-test'},",
                        "        },",
                        "    },",
                        "    {",
                        "        'type': 'stream_event',",
                        "        'event': {",
                        "            'type': 'content_block_delta',",
                        "            'delta': {'type': 'text_delta', 'text': 'Runner "
                        "status: '},",
                        "        },",
                        "    },",
                        "    {",
                        "        'type': 'stream_event',",
                        "        'event': {",
                        "            'type': 'content_block_delta',",
                        "            'delta': {",
                        "                'type': 'text_delta',",
                        "                'text': 'complete\\nNext command: "
                        "/session-run plan/\\n',",
                        "            },",
                        "        },",
                        "    },",
                        "    {",
                        "        'type': 'stream_event',",
                        "        'event': {'type': 'content_block_stop', 'index': 0},",
                        "    },",
                        "    {",
                        "        'type': 'assistant',",
                        "        'message': {",
                        "            'content': [",
                        "                {",
                        "                    'type': 'text',",
                        "                    'text': 'Runner status: complete\\n'",
                        "                        'Next command: /session-run "
                        "plan/\\n',",
                        "                },",
                        "            ],",
                        "        },",
                        "    },",
                        "    {",
                        "        'type': 'result',",
                        "        'subtype': 'success',",
                        "        'duration_ms': 1234,",
                        "        'num_turns': 1,",
                        "        'result': 'Runner status: complete\\n'",
                        "            'Next command: /session-run plan/\\n',",
                        "    },",
                        "]",
                        "for record in records:",
                        "    print(json.dumps(record), flush=True)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
            callbacks: list[tuple[harnesses.StreamName, str]] = []

            result = harness_claude.ClaudeAdapter(executable=str(fake_claude)).run(
                cwd=tmp_dir,
                plan_dir=tmp_dir,
                prompt="/session-run plan/",
                stream_callback=lambda stream, text: callbacks.append((stream, text)),
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--verbose", result.argv)
        self.assertIn("--output-format", result.argv)
        self.assertIn("--include-partial-messages", result.argv)
        self.assertIn(
            "claude init; model claude-test; session session-1",
            result.stdout,
        )
        self.assertEqual("claude-test", result.observed_model)
        self.assertIn("claude status: requesting", result.stdout)
        self.assertIn("claude response: model claude-test", result.stdout)
        self.assertIn("claude text: Runner status: complete", result.stdout)
        self.assertIn("claude text: Next command: /session-run plan/", result.stdout)
        self.assertIn("claude result: success (1 turn, 1.2s)", result.stdout)
        self.assertNotIn('"type": "system"', result.stdout)
        self.assertEqual(
            "complete",
            turn_execution.extract_prefixed_line(
                result.stdout,
                events.RUNNER_STATUS_PREFIX,
            ),
        )
        self.assertEqual(
            "Runner status: complete\nNext command: /session-run plan/\n",
            result.last_message,
        )
        self.assertTrue(
            any("claude status: requesting" in text for _stream, text in callbacks)
        )
        self.assertEqual(
            "complete",
            turn_execution.runner_signals(result)["runner_status"],
        )
        self.assertEqual(
            {
                "reason": "runner-pause",
                "message": "unsafe - wait",
                "source": "stdout",
                "matched": events.RUNNER_PAUSE_PREFIX,
            },
            turn_execution.live_action_signal(
                "stdout",
                "claude text: RUNNER_PAUSE: unsafe - wait\n",
            ),
        )


if __name__ == "__main__":
    unittest.main()
