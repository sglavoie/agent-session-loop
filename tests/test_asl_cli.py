"""Command/config and JSON CLI surface tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    REPO_ROOT,
    patched_env,
    write_plan,
    write_plan_with_ids,
    install_fake_codex,
)


class CommandAndConfigTests(unittest.TestCase):
    def test_codex_command_uses_workspace_write_or_unsafe_bypass(self) -> None:
        adapter = harnesses.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")
        last_message = Path("/tmp/state/last-message-1.txt")

        safe = adapter.build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            last_message_path=last_message,
        )
        self.assertIn("-s", safe)
        self.assertIn("workspace-write", safe)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", safe)
        self.assertNotIn("-a", safe)

        unsafe = adapter.build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            unsafe_bypass=True,
            last_message_path=last_message,
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", unsafe)
        self.assertNotIn("workspace-write", unsafe)

    def test_codex_command_includes_model_and_reasoning_effort_config(self) -> None:
        adapter = harnesses.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")
        prompt = "$session-run /tmp/plan/"
        last_message = Path("/tmp/state/last-message-1.txt")

        argv = adapter.build_argv(
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

        claude = harnesses.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
        )
        self.assertEqual("dontAsk", claude[claude.index("--permission-mode") + 1])
        self.assertEqual(
            "stream-json",
            claude[claude.index("--output-format") + 1],
        )
        self.assertIn("--include-partial-messages", claude)
        self.assertIn("--add-dir", claude)

        claude_unsafe = harnesses.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            unsafe_bypass=True,
        )
        self.assertEqual(
            "bypassPermissions",
            claude_unsafe[claude_unsafe.index("--permission-mode") + 1],
        )

        copilot_safe = harnesses.CopilotAdapter(executable="copilot").build_argv(
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
        self.assertEqual(
            "/session-run /tmp/plan/",
            copilot_safe[copilot_safe.index("-p") + 1],
        )

        copilot_unsafe = harnesses.CopilotAdapter(executable="copilot").build_argv(
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

        claude = harnesses.ClaudeAdapter(executable="claude").build_argv(
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
        self.assertEqual("/session-run /tmp/plan/", claude[-1])

        copilot = harnesses.CopilotAdapter(executable="copilot").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            model="gpt-5",
            effort="low",
        )
        prompt_index = copilot.index("-p")
        self.assertEqual("gpt-5", copilot[copilot.index("--model") + 1])
        self.assertEqual("low", copilot[copilot.index("--effort") + 1])
        self.assertLess(copilot.index("--model"), prompt_index)
        self.assertLess(copilot.index("--effort"), prompt_index)
        self.assertEqual("/session-run /tmp/plan/", copilot[prompt_index + 1])

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
                        "    {'type': 'system', 'subtype': 'status', 'status': 'requesting'},",
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
                        "            'delta': {'type': 'text_delta', 'text': 'Runner status: '},",
                        "        },",
                        "    },",
                        "    {",
                        "        'type': 'stream_event',",
                        "        'event': {",
                        "            'type': 'content_block_delta',",
                        "            'delta': {",
                        "                'type': 'text_delta',",
                        "                'text': 'complete\\nNext command: /session-run plan/\\n',",
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
                        "                        'Next command: /session-run plan/\\n',",
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

            result = harnesses.ClaudeAdapter(executable=str(fake_claude)).run(
                cwd=tmp_dir,
                plan_dir=tmp_dir,
                prompt="/session-run plan/",
                stream_callback=lambda stream, text: callbacks.append(
                    (stream, text)
                ),
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--output-format", result.argv)
        self.assertIn("--include-partial-messages", result.argv)
        self.assertIn(
            "claude init; model claude-test; session session-1",
            result.stdout,
        )
        self.assertIn("claude status: requesting", result.stdout)
        self.assertIn("claude response: model claude-test", result.stdout)
        self.assertIn("claude text: Runner status: complete", result.stdout)
        self.assertIn("claude text: Next command: /session-run plan/", result.stdout)
        self.assertIn("claude result: success (1 turn, 1.2s)", result.stdout)
        self.assertNotIn('"type": "system"', result.stdout)
        self.assertEqual(
            "complete",
            runner._extract_prefixed_line(
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
            runner._runner_signals(result)["runner_status"],
        )
        self.assertEqual(
            {
                "reason": "runner-pause",
                "message": "unsafe - wait",
                "source": "stdout",
                "matched": events.RUNNER_PAUSE_PREFIX,
            },
            runner._live_action_signal(
                "stdout",
                "claude text: RUNNER_PAUSE: unsafe - wait\n",
            ),
        )

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

        signals = runner._runner_signals(result)
        self.assertEqual("complete", signals["runner_status"])
        self.assertEqual("last_message", signals["runner_status_source"])
        self.assertNotIn("runner_pause", signals)
        self.assertEqual(
            "complete",
            runner._turn_status_result(
                result=result,
                status_changes=(
                    runner._session_status_change_payload(
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

        signals = runner._runner_signals(result)
        self.assertEqual(
            {"runner_status": "complete", "runner_status_source": "stdout"},
            signals,
        )

    def test_live_action_signal_ignores_stderr_runner_pause_text(self) -> None:
        self.assertIsNone(
            runner._live_action_signal(
                "stderr",
                "RUNNER_PAUSE: unsafe - quoted skill instructions\n",
            )
        )
        self.assertIsNotNone(
            runner._live_action_signal(
                "stderr",
                "approval required before continuing\n",
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

        signals = runner._runner_signals(result)
        self.assertEqual("complete", signals["runner_status"])
        self.assertEqual("last_message", signals["runner_status_source"])
        self.assertNotIn("runner_pause", signals)
        self.assertNotIn("runner_pause_source", signals)
        self.assertEqual(
            "complete",
            runner._turn_status_result(
                result=result,
                status_changes=(
                    runner._session_status_change_payload(
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

        signals = runner._runner_signals(result)
        self.assertEqual(
            "blocked - interactive decision gates",
            signals["runner_pause"],
        )
        self.assertEqual("last_message", signals["runner_pause_source"])

    def test_live_action_signal_ignores_noop_runner_pause(self) -> None:
        self.assertIsNone(
            runner._live_action_signal("stdout", "RUNNER_PAUSE: none\n")
        )
        self.assertIsNotNone(
            runner._live_action_signal(
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
            runner.build_session_prompt("codex", "/tmp/plan").startswith(
                "$session-run "
            )
        )
        self.assertIn(
            runner.RUNNER_MANAGED_SESSION_ARG,
            runner.build_session_prompt("codex", "/tmp/plan"),
        )
        self.assertTrue(
            runner.build_session_prompt("claude", "/tmp/plan").startswith(
                "/session-run "
            )
        )

    def test_build_session_prompt_omits_explicit_session_selector(self) -> None:
        for harness in ("codex", "claude", "copilot"):
            with self.subTest(harness=harness):
                prompt = runner.build_session_prompt(harness, "/tmp/plan")
                self.assertNotIn("--session", prompt)
                self.assertNotIn("session-1.md", prompt)
                self.assertNotIn("session-2.md", prompt)

    def test_commit_flags_are_mutually_exclusive(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                [
                    "run",
                    "/tmp/plan",
                    "--cwd",
                    "/tmp/repo",
                    "--commit-per-session",
                    "--commit-at-end",
                ]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_run_and_doctor_default_cwd_to_current_directory(self) -> None:
        parser = cli.build_parser()

        run_args = parser.parse_args(["run", "/tmp/plan"])
        self.assertEqual(".", run_args.cwd)

        doctor_args = parser.parse_args(
            ["doctor", "--harness", "codex", "--plan-dir", "/tmp/plan"]
        )
        self.assertEqual(".", doctor_args.cwd)

    def test_run_and_doctor_default_to_unsafe_bypass_with_opt_out(self) -> None:
        parser = cli.build_parser()

        run_args = parser.parse_args(["run", "/tmp/plan"])
        self.assertTrue(run_args.unsafe_bypass)

        run_explicit_args = parser.parse_args(["run", "/tmp/plan", "--unsafe-bypass"])
        self.assertTrue(run_explicit_args.unsafe_bypass)

        run_safe_args = parser.parse_args(
            ["run", "/tmp/plan", "--no-unsafe-bypass"]
        )
        self.assertFalse(run_safe_args.unsafe_bypass)

        doctor_args = parser.parse_args(
            ["doctor", "--harness", "codex", "--plan-dir", "/tmp/plan"]
        )
        self.assertTrue(doctor_args.unsafe_bypass)

        doctor_safe_args = parser.parse_args(
            [
                "doctor",
                "--harness",
                "codex",
                "--plan-dir",
                "/tmp/plan",
                "--no-unsafe-bypass",
            ]
        )
        self.assertFalse(doctor_safe_args.unsafe_bypass)

    def test_parser_uses_custom_program_name_in_help(self) -> None:
        parser = cli.build_parser(prog="asl")
        stdout = io.StringIO()

        self.assertTrue(parser.format_help().startswith("usage: asl "))
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["run", "--help"])

        self.assertEqual(0, raised.exception.code)
        self.assertIn("usage: asl run", stdout.getvalue())

    def test_tail_interactive_flag_is_rejected(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["tail", "--interactive", "/tmp/plan"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments: --interactive", stderr.getvalue())

    def test_preflight_mark_loop_ready_short_flag(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(["preflight", "/tmp/plan", "-m"])

        self.assertTrue(args.mark_loop_ready)

    def test_run_parser_review_harness_defaults_to_none(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(["run", "/tmp/plan"])

        self.assertIsNone(args.review_harness)

    def test_run_parser_uses_role_specific_model_attributes(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "run",
                "/tmp/plan",
                "--harness-model",
                "gpt-5",
                "--review-harness-model",
                "claude-opus-4",
            ]
        )

        self.assertEqual("gpt-5", args.harness_model)
        self.assertEqual("claude-opus-4", args.review_harness_model)
        self.assertFalse(hasattr(args, "model"))

    def test_run_parser_rejects_legacy_model_flag(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["run", "/tmp/plan", "--model", "gpt-5"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments: --model", stderr.getvalue())

    def test_prepare_parser_reserves_role_specific_model_forwarding(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "prepare",
                "/tmp/plan.md",
                "--harness-model",
                "gpt-5",
                "--review-harness-model",
                "claude-opus-4",
            ]
        )

        self.assertEqual("gpt-5", args.harness_model)
        self.assertEqual("claude-opus-4", args.review_harness_model)

    def test_doctor_parser_rejects_legacy_model_flag(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                [
                    "doctor",
                    "--harness",
                    "codex",
                    "--plan-dir",
                    "/tmp/plan",
                    "--model",
                    "gpt-5",
                ]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments: --model", stderr.getvalue())

    def test_run_parser_accepts_review_harness_choices(self) -> None:
        parser = cli.build_parser()

        for harness_name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=harness_name):
                args = parser.parse_args(
                    ["run", "/tmp/plan", "--review-harness", harness_name]
                )
                self.assertEqual(harness_name, args.review_harness)

    def test_run_parser_rejects_unknown_review_harness(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                ["run", "/tmp/plan", "--review-harness", "not-a-harness"]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("--review-harness", stderr.getvalue())

    def test_validate_run_inputs_rejects_review_harness_equal_to_harness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                review_harness="codex",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )
            with self.assertRaises(runner.RunError) as raised:
                runner._validate_run_inputs(args)

            message = str(raised.exception)
            self.assertIn("--review-harness must differ from --harness", message)
            self.assertIn("'codex'", message)

    def test_validate_run_inputs_accepts_distinct_review_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                review_harness="claude",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )

            cwd, timeout, max_sessions, commit_mode = runner._validate_run_inputs(
                args
            )

            self.assertEqual(Path(tmp).resolve(), cwd)
            self.assertIsNone(timeout)
            self.assertIsNone(max_sessions)
            self.assertIsNone(commit_mode)

    def test_validate_run_inputs_requires_review_harness_for_review_model(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                review_harness=None,
                review_harness_model="claude-opus-4",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )

            with self.assertRaises(runner.RunError) as raised:
                runner._validate_run_inputs(args)

        self.assertEqual(
            "--review-harness-model requires --review-harness",
            str(raised.exception),
        )

    def test_validate_run_inputs_allows_missing_review_harness_attribute(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )

            runner._validate_run_inputs(args)

    def test_review_session_kind_constants(self) -> None:
        self.assertEqual("implementation", runner.SESSION_KIND_IMPLEMENTATION)
        self.assertEqual("review", runner.SESSION_KIND_REVIEW)
        self.assertEqual(
            (
                runner.SESSION_KIND_IMPLEMENTATION,
                runner.SESSION_KIND_REVIEW,
            ),
            runner.SESSION_KINDS,
        )
        self.assertEqual("final-review.md", runner.FINAL_REVIEW_FILENAME)
        self.assertIn("asl:generated-review-session", runner.REVIEW_SESSION_MARKER)

    def test_review_pause_reasons_are_registered(self) -> None:
        self.assertIn("review-harness-required", events.PAUSE_REASONS)
        self.assertIn("review-mutated-worktree", events.PAUSE_REASONS)

    def test_contains_review_session_marker_detects_marker(self) -> None:
        body_with_marker = "\n".join(
            [
                "# Session 7: Final Review",
                "",
                runner.REVIEW_SESSION_MARKER,
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
            ]
        )
        body_without_marker = "\n".join(
            [
                "# Session 1",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
            ]
        )

        self.assertTrue(runner.contains_review_session_marker(body_with_marker))
        self.assertFalse(
            runner.contains_review_session_marker(body_without_marker)
        )

    def test_is_generated_review_session_file_reads_marker_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            review_path = plan_dir / "session-7.md"
            other_path = plan_dir / "session-1.md"
            review_path.write_text(
                f"# Review\n\n{runner.REVIEW_SESSION_MARKER}\n",
                encoding="utf-8",
            )
            other_path.write_text("# Session 1\n", encoding="utf-8")

            self.assertTrue(
                runner.is_generated_review_session_file(review_path)
            )
            self.assertFalse(
                runner.is_generated_review_session_file(other_path)
            )
            self.assertFalse(
                runner.is_generated_review_session_file(plan_dir / "missing.md")
            )

    def test_final_review_output_path_joins_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)

            self.assertEqual(
                plan_dir / runner.FINAL_REVIEW_FILENAME,
                runner.final_review_output_path(plan_dir),
            )

    def test_parse_review_verdict_accepts_all_clear_status(self) -> None:
        verdict = runner.parse_review_verdict(
            "## Findings\n"
            "- Reviewed agent_session_loop/runner.py.\n"
            "Status: All clear, no findings\n"
        )

        self.assertEqual("all-clear", verdict["outcome"])
        self.assertEqual(0, verdict["total_findings"])
        self.assertEqual(
            {"high": 0, "med": 0, "low": 0},
            verdict["findings_by_severity"],
        )

    def test_parse_review_verdict_accepts_counted_findings_status(self) -> None:
        verdict = runner.parse_review_verdict(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "Status: 5 Findings (2 high, 2 med, 1 low)\n"
        )

        self.assertEqual("findings", verdict["outcome"])
        self.assertEqual(5, verdict["total_findings"])
        self.assertEqual(
            {"high": 2, "med": 2, "low": 1},
            verdict["findings_by_severity"],
        )

    def test_parse_review_verdict_rejects_missing_status(self) -> None:
        with self.assertRaisesRegex(
            runner.ReviewVerdictError, "missing required final Status line"
        ):
            runner.parse_review_verdict("## Findings\n- Nothing to report.\n")

    def test_parse_review_verdict_rejects_multiple_status_lines(self) -> None:
        with self.assertRaisesRegex(
            runner.ReviewVerdictError, "expected exactly one Status line"
        ):
            runner.parse_review_verdict(
                "Status: All clear, no findings\n"
                "## Findings\n"
                "Status: All clear, no findings\n"
            )

    def test_parse_review_verdict_rejects_non_final_status(self) -> None:
        with self.assertRaisesRegex(
            runner.ReviewVerdictError,
            "Status line must be the final non-empty line",
        ):
            runner.parse_review_verdict(
                "## Findings\n"
                "Status: All clear, no findings\n"
                "- trailing note\n"
            )

    def test_parse_review_verdict_rejects_malformed_status(self) -> None:
        with self.assertRaisesRegex(
            runner.ReviewVerdictError, "malformed Status line"
        ):
            runner.parse_review_verdict(
                "## Findings\n"
                "Status: 1 Finding (1 high, 0 med, 0 low)\n"
            )

    def test_parse_review_verdict_rejects_count_mismatch(self) -> None:
        with self.assertRaisesRegex(
            runner.ReviewVerdictError, "severity counts do not add up"
        ):
            runner.parse_review_verdict(
                "## Findings\n"
                "Status: 5 Findings (2 high, 2 med, 0 low)\n"
            )

    def test_next_top_level_session_id_returns_one_for_empty_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")

            self.assertEqual("1", plans.next_top_level_session_id(plan_dir).value)

    def test_next_top_level_session_id_uses_max_top_level_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan_with_ids(
                plan_dir,
                (
                    ("1", "complete"),
                    ("2.1", "complete"),
                    ("3", "pending"),
                ),
            )

            self.assertEqual("4", plans.next_top_level_session_id(plan_dir).value)

    def test_next_top_level_session_id_ignores_expansion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))
            (plan_dir / "session-9.expand.md").write_text(
                "# Expansion\n", encoding="utf-8"
            )

            self.assertEqual("2", plans.next_top_level_session_id(plan_dir).value)

    def test_build_generated_review_session_body_is_valid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans.SessionId((7,))

            body = runner.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
            )

            self.assertIn(runner.REVIEW_SESSION_MARKER, body)
            self.assertIn("claude", body)
            self.assertIn(runner.FINAL_REVIEW_FILENAME, body)
            self.assertIn("three distinct source, documentation, or test", body)
            self.assertIn("review-oriented section heading", body)
            self.assertIn("valid `Status:` verdict line", body)
            self.assertIn("Do not modify", body)
            self.assertIn("- **Status**: pending", body)

            contract = contracts.parse_completion_contract(body, session_id=session_id)
            self.assertIsNotNone(contract)
            self.assertEqual(session_id, contract.session_id)
            self.assertEqual(2, len(contract.verify.checks))
            check = contract.verify.checks[0]
            self.assertIsInstance(check, contracts.CommandCheck)
            self.assertEqual("final-review-output-evidence-based", check.name)
            self.assertEqual("python3", check.argv[0])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)), check.cwd
            )

    def test_find_pending_generated_review_session_returns_none_when_no_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("pending", "complete"))

            self.assertIsNone(
                runner.find_pending_generated_review_session(plan_dir)
            )

    def test_find_pending_generated_review_session_returns_pending_marked_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete", "complete"))
            review_id = plans.next_top_level_session_id(plan_dir)
            review_path = plan_dir / review_id.filename
            review_path.write_text(
                runner.build_generated_review_session_body(
                    review_id,
                    review_harness="claude",
                    plan_dir=plan_dir,
                ),
                encoding="utf-8",
            )

            found = runner.find_pending_generated_review_session(plan_dir)

            self.assertIsNotNone(found)
            self.assertEqual(review_id, found.session_id)
            self.assertEqual(plans.canonicalize_path(review_path), found.path)

    def test_find_pending_generated_review_session_skips_completed_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))
            review_id = plans.next_top_level_session_id(plan_dir)
            review_path = plan_dir / review_id.filename
            body = runner.build_generated_review_session_body(
                review_id,
                review_harness="claude",
                plan_dir=plan_dir,
            ).replace("- **Status**: pending", "- **Status**: complete")
            review_path.write_text(body, encoding="utf-8")

            self.assertIsNone(
                runner.find_pending_generated_review_session(plan_dir)
            )

    def test_ensure_generated_review_session_creates_new_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete", "pending"))

            session_file, created = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )

            self.assertTrue(created)
            self.assertEqual("3", session_file.session_id.value)
            self.assertEqual(
                plans.canonicalize_path(plan_dir / "session-3.md"),
                session_file.path,
            )
            self.assertTrue(
                runner.is_generated_review_session_file(session_file.path)
            )
            status = plans.read_session_status(session_file)
            self.assertEqual("pending", status.status)

    def test_ensure_generated_review_session_reuses_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            first_file, first_created = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            second_file, second_created = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )

            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_file.session_id, second_file.session_id)
            self.assertEqual(first_file.path, second_file.path)

    def test_ensure_generated_review_session_appends_after_completed_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))
            first_file, _ = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            completed_body = first_file.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            )
            first_file.path.write_text(completed_body, encoding="utf-8")

            second_file, created = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )

            self.assertTrue(created)
            self.assertNotEqual(first_file.session_id, second_file.session_id)
            self.assertEqual(
                first_file.session_id.components[0] + 1,
                second_file.session_id.components[0],
            )

    def test_ensure_generated_review_session_contract_passes_with_review_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
                (
                    "## Findings\n"
                    "- Reviewed agent_session_loop/runner.py for generated review "
                    "contracts.\n"
                    "- Checked tests/test_agent_session_loop.py for lifecycle "
                    "coverage.\n"
                    "- Compared docs/agent-session-loop.md for operator-facing "
                    "documentation impact.\n"
                    "Status: All clear, no findings\n"
                ),
                encoding="utf-8",
            )
            (plan_dir / runner.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(
                    {"version": 2, "outcome": "all-clear", "findings": []}
                ),
                encoding="utf-8",
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification.run_verification(contract, repo_root=plan_dir)
            self.assertTrue(result.passed, result.failures)

    def test_ensure_generated_review_session_contract_fails_without_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
                "   \n\n  \t\n", encoding="utf-8"
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification.run_verification(contract, repo_root=plan_dir)
            self.assertFalse(result.passed)

    def test_ensure_generated_review_session_contract_fails_without_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
                (
                    "## Findings\n"
                    "- Reviewed agent_session_loop/runner.py for generated review "
                    "contracts.\n"
                    "- Checked tests/test_agent_session_loop.py for lifecycle "
                    "coverage.\n"
                    "- Compared docs/agent-session-loop.md for operator-facing "
                    "documentation impact.\n"
                ),
                encoding="utf-8",
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification.run_verification(contract, repo_root=plan_dir)
            self.assertFalse(result.passed)

    def test_ensure_generated_review_session_contract_fails_low_effort_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = runner.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / runner.FINAL_REVIEW_FILENAME).write_text(
                "Looks good overall.\nNo issues found.\n", encoding="utf-8"
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification.run_verification(contract, repo_root=plan_dir)
            self.assertFalse(result.passed)


class JsonCliTests(unittest.TestCase):
    def test_status_and_dry_run_json_outputs_include_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            status_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            status_payload = json.loads(status_completed.stdout)
            self.assertTrue(status_payload["ok"])
            self.assertEqual("status", status_payload["command"])
            self.assertEqual(1, status_payload["next_pending"])
            self.assertEqual("1", status_payload["next_pending_id"])
            self.assertEqual("1", status_payload["sessions"][0]["id"])
            self.assertEqual(1, status_payload["sessions"][0]["number"])
            self.assertTrue(status_payload["state_path"].endswith("/state.json"))
            self.assertTrue(status_payload["events_path"].endswith("/events.jsonl"))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            run_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--harness-model",
                    "gpt-5",
                    "--effort",
                    "high",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            run_payload = json.loads(run_completed.stdout)
            self.assertTrue(run_payload["ok"])
            self.assertEqual("run", run_payload["command"])
            self.assertEqual({}, run_payload["details"])
            self.assertEqual("dry-run", run_payload["reason"])
            self.assertIn("harness_config", run_payload)
            self.assertEqual("gpt-5", run_payload["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, run_payload["model_source"])
            self.assertEqual("high", run_payload["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, run_payload["effort_source"])
            self.assertEqual(1, run_payload["planned"]["next_pending"])
            self.assertEqual("1", run_payload["planned"]["next_pending_id"])
            self.assertEqual("1", run_payload["initial_snapshot"]["next_pending_id"])
            planned = run_payload["planned"]
            invocation = planned["invocation"]
            self.assertEqual("gpt-5", planned["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, planned["model_source"])
            self.assertEqual("high", planned["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, planned["effort_source"])
            self.assertEqual("gpt-5", invocation["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, invocation["model_source"])
            self.assertEqual("high", invocation["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, invocation["effort_source"])
            self.assertIn(
                "--dangerously-bypass-approvals-and-sandbox",
                invocation["argv"],
            )
            self.assertNotIn("workspace-write", invocation["argv"])
            self.assertEqual(
                "gpt-5", invocation["argv"][invocation["argv"].index("--model") + 1]
            )
            self.assertEqual(
                'model_reasoning_effort="high"',
                invocation["argv"][invocation["argv"].index("-c") + 1],
            )
            self.assertTrue(run_payload["state_path"].endswith("/state.json"))
            self.assertTrue(run_payload["events_path"].endswith("/events.jsonl"))

    def test_compact_state_cli_dry_run_then_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True, exist_ok=True)
            big = "X" * 40000
            turn = {
                "session_id": "1",
                "turn_index": 1,
                "before": {"counts": {"pending": 1}, "sessions": [{"id": "1"}]},
                "verification": {
                    "checks": [
                        {"name": "c1", "status": "fail", "stdout": big, "stderr": ""}
                    ]
                },
            }
            state_path = plan_state / "state.json"
            state_path.write_text(
                json.dumps({"run_id": "r", "turns": [turn]}, sort_keys=True),
                encoding="utf-8",
            )
            before = state_path.read_bytes()

            def _run_compact(*extra: str) -> dict:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "compact-state",
                        str(plan_dir),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                        *extra,
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                return json.loads(completed.stdout)

            dry = _run_compact()
            self.assertTrue(dry["ok"])
            self.assertEqual("compact-state", dry["command"])
            self.assertFalse(dry["applied"])
            self.assertGreater(dry["projected_bytes_reclaimed"], 0)
            self.assertEqual(0, dry["actual_bytes_reclaimed"])
            self.assertEqual(before, state_path.read_bytes())

            applied = _run_compact("--apply")
            self.assertTrue(applied["applied"])
            self.assertGreater(applied["actual_bytes_reclaimed"], 0)
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["version"])
            self.assertNotIn("stdout", payload["turns"][0]["verification"]["checks"][0])

    def test_status_and_dry_run_json_outputs_nested_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan_with_ids(plan_dir, (("1", "complete"), ("1.1", "pending")))

            status_payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            self.assertEqual("1.1", status_payload["next_pending"])
            self.assertEqual("1.1", status_payload["next_pending_id"])
            integer_session = status_payload["sessions"][0]
            nested_session = status_payload["sessions"][1]
            self.assertEqual("1", integer_session["id"])
            self.assertEqual(1, integer_session["number"])
            self.assertEqual("1.1", nested_session["id"])
            self.assertNotIn("number", nested_session)

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("1.1", payload["initial_snapshot"]["next_pending"])
            self.assertEqual("1.1", payload["initial_snapshot"]["next_pending_id"])
            self.assertEqual("1.1", payload["planned"]["next_pending"])
            self.assertEqual("1.1", payload["planned"]["next_pending_id"])
            nested = payload["initial_snapshot"]["sessions"][1]
            self.assertEqual("1.1", nested["id"])
            self.assertNotIn("number", nested)

    def test_dry_run_json_no_unsafe_bypass_uses_harness_default_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--no-unsafe-bypass",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            argv = payload["planned"]["invocation"]["argv"]
            self.assertIn("-s", argv)
            self.assertIn("workspace-write", argv)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

            copilot_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(tmp_dir / "copilot-state"),
                    "--dry-run",
                    "--json",
                    "--harness",
                    "copilot",
                    "--no-unsafe-bypass",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            copilot_payload = json.loads(copilot_completed.stdout)
            copilot_argv = copilot_payload["planned"]["invocation"]["argv"]
            self.assertIn("--no-ask-user", copilot_argv)
            self.assertIn("--silent", copilot_argv)
            self.assertIn("-C", copilot_argv)
            self.assertIn("--add-dir", copilot_argv)
            self.assertNotIn("--allow-all-tools", copilot_argv)
            self.assertNotIn("--allow-all-paths", copilot_argv)

    def test_dry_run_json_copilot_default_unsafe_bypass_allows_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--harness",
                    "copilot",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            argv = payload["planned"]["invocation"]["argv"]
            self.assertIn("--allow-all-tools", argv)
            self.assertIn("--allow-all-paths", argv)
            self.assertIn("--no-ask-user", argv)
            self.assertIn("--silent", argv)

    def test_run_json_uses_current_directory_when_cwd_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            invocation = payload["planned"]["invocation"]
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(REPO_ROOT), invocation["cwd"])
            self.assertEqual(
                str(REPO_ROOT), invocation["argv"][invocation["argv"].index("-C") + 1]
            )

    def test_dry_run_json_reports_native_model_metadata_without_argv_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            home = tmp_dir / "home"
            codex_config = home / ".codex" / "config.toml"
            codex_config.parent.mkdir(parents=True)
            codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("gpt-5-codex", payload["model"])
            self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, payload["model_source"])
            self.assertIsNone(payload["effort"])
            self.assertIsNone(payload["effort_source"])
            planned = payload["planned"]
            invocation = planned["invocation"]
            self.assertEqual("gpt-5-codex", planned["model"])
            self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, planned["model_source"])
            self.assertIsNone(planned["effort"])
            self.assertIsNone(planned["effort_source"])
            self.assertEqual("gpt-5-codex", invocation["model"])
            self.assertEqual(
                config.MODEL_SOURCE_CODEX_CONFIG, invocation["model_source"]
            )
            self.assertIsNone(invocation["effort"])
            self.assertIsNone(invocation["effort_source"])
            self.assertNotIn("--model", invocation["argv"])
            self.assertNotIn("-c", invocation["argv"])

    def test_status_json_error_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("missing context.md", payload["error"])
            self.assertEqual({}, payload["details"])

    def test_doctor_json_outputs_harness_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--harness-model",
                    "gpt-5",
                    "--effort",
                    "high",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("doctor", payload["command"])
            self.assertEqual("codex", payload["harness"])
            self.assertEqual(str(fake_codex), payload["resolved_executable"])
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)), payload["plan_dir"]
            )
            self.assertEqual(1, payload["next_pending"])
            self.assertEqual("gpt-5", payload["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, payload["model_source"])
            self.assertEqual("high", payload["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, payload["effort_source"])
            self.assertTrue(payload["prompt"].startswith("$session-run "))
            self.assertEqual(
                ["codex"], payload["harness_config"]["configured_harnesses"]
            )
            self.assertEqual(str(fake_codex), payload["invocation"]["argv"][0])
            self.assertIn(
                "--dangerously-bypass-approvals-and-sandbox",
                payload["invocation"]["argv"],
            )
            self.assertNotIn("workspace-write", payload["invocation"]["argv"])
            self.assertEqual(payload["invocation"]["argv"], payload["invocation_argv"])
            self.assertEqual("gpt-5", payload["invocation"]["model"])
            self.assertEqual(
                config.MODEL_SOURCE_FLAG, payload["invocation"]["model_source"]
            )
            self.assertEqual("high", payload["invocation"]["effort"])
            self.assertEqual(
                config.EFFORT_SOURCE_FLAG, payload["invocation"]["effort_source"]
            )
            self.assertEqual(
                "gpt-5",
                payload["invocation_argv"][
                    payload["invocation_argv"].index("--model") + 1
                ],
            )
            self.assertEqual(
                'model_reasoning_effort="high"',
                payload["invocation_argv"][payload["invocation_argv"].index("-c") + 1],
            )

    def test_doctor_json_uses_current_directory_when_cwd_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--plan-dir",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            invocation = payload["invocation"]
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(REPO_ROOT), invocation["cwd"])
            self.assertEqual(
                str(REPO_ROOT), invocation["argv"][invocation["argv"].index("-C") + 1]
            )

    def test_doctor_text_outputs_model_effort_and_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--harness-model",
                    "gpt-5",
                    "--effort",
                    "high",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            lines = completed.stdout.splitlines()
            self.assertIn("Model: gpt-5 (flag)", lines)
            self.assertIn("Effort: high (flag)", lines)
            self.assertIn("Argv:", lines)
            model_index = lines.index("  --model")
            effort_index = lines.index("  -c")
            self.assertEqual("  gpt-5", lines[model_index + 1])
            self.assertEqual('  model_reasoning_effort="high"', lines[effort_index + 1])

    def test_doctor_json_error_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("doctor", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("missing context.md", payload["error"])
            self.assertEqual({}, payload["details"])
            self.assertEqual("codex", payload["harness"])
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(plan_dir), payload["plan_dir"])

    def test_unlock_json_success_reports_lock_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "unlock",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("unlock", payload["command"])
            self.assertFalse(payload["had_lock"])
            self.assertFalse(payload["removed"])
            self.assertFalse(payload["active"])
            self.assertFalse(payload["stale"])
            self.assertTrue(payload["lock_path"].endswith("/lock.json"))
            self.assertIn("no lock present", payload["message"])

    def test_unlock_json_active_lock_failure_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            lock_info = state.acquire_lock(plan_dir, state_dir)

            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "unlock",
                        str(plan_dir),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("unlock", payload["command"])
            self.assertEqual("active-lock", payload["reason"])
            self.assertIn("active lock", payload["error"])
            self.assertEqual(str(lock_info.path), payload["lock_path"])
            self.assertEqual(str(lock_info.path), payload["details"]["lock_path"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)),
                payload["details"]["plan_dir"],
            )

    def test_sessions_help_lists_overview_flags(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "sessions",
                "--help",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertIn("--running", completed.stdout)
        self.assertIn("--recent", completed.stdout)
        self.assertIn("--limit", completed.stdout)
        self.assertIn("--state-dir", completed.stdout)
        self.assertIn("--verbose", completed.stdout)
        self.assertIn("--json", completed.stdout)

    def test_sessions_json_empty_state_root_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "empty-state"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "sessions",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("sessions", payload["command"])
            self.assertEqual([], payload["running"])
            self.assertEqual([], payload["recent"])
            self.assertEqual(
                {"running": 0, "recent": 0, "stale_locks": 0, "skipped": 0},
                payload["counts"],
            )
            self.assertEqual(
                {"running": True, "recent": True}, payload["filters"]
            )

    def test_sessions_json_negative_limit_returns_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "empty-state"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "sessions",
                    "--state-dir",
                    str(state_dir),
                    "--limit",
                    "-1",
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("sessions", payload["command"])
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("--limit", payload["error"])

    def test_sessions_text_empty_state_root_renders_none_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "empty-state"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "sessions",
                    "--state-dir",
                    str(state_dir),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )

            self.assertIn("Running sessions: none", completed.stdout)
            self.assertIn("Recent sessions: none", completed.stdout)


if __name__ == "__main__":
    unittest.main()
