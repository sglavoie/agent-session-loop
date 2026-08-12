"""Command/config and JSON CLI surface tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timedelta, timezone
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
import socket
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
    mailbox,
    mailbox_cli,
    metrics,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    repository_registration,
    responses,
    retention,
    review,
    review_repair,
    run_control,
    run_flags,
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
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _write_state_payload,
    git_stdout,
    init_multi_repo_workspace,
    patched_env,
    write_plan,
    write_plan_with_ids,
    install_fake_codex,
    install_fake_spec,
    write_multi_repo_plan,
    _contract_block,
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
        self.assertIn("--verbose", claude)
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
        self.assertIn("--verbose", result.argv)
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

    def test_run_mailbox_defaults_active_capable_with_explicit_opt_out(self) -> None:
        parser = cli.build_parser()

        default_args = parser.parse_args(["run", "/tmp/plan"])
        disabled_args = parser.parse_args(
            ["run", "/tmp/plan", "--no-mailbox"]
        )

        self.assertFalse(default_args.no_mailbox)
        self.assertTrue(disabled_args.no_mailbox)
        self.assertIn("no_mailbox", runner._RESUME_OVERLAY_DESTS)

    def test_notify_and_outbox_include_parser_contract(self) -> None:
        parser = cli.build_parser()

        notify_args = parser.parse_args(
            [
                "notify",
                "/tmp/plan",
                "checkpoint",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )
        self.assertEqual("checkpoint", notify_args.text)
        self.assertFalse(notify_args.text_stdin)
        self.assertEqual(
            "mailbox_cli:notify_from_args",
            notify_args.handler_ref,
        )
        self.assertIs(
            mailbox_cli.notify_from_args,
            cli.resolve_handler(notify_args),
        )

        stdin_args = parser.parse_args(
            ["notify", "/tmp/plan", "--text-stdin"]
        )
        self.assertIsNone(stdin_args.text)
        self.assertTrue(stdin_args.text_stdin)

        default_wait = parser.parse_args(["outbox", "wait", "/tmp/plan"])
        self.assertEqual("questions", default_wait.include)
        for include in ("questions", "notices", "all"):
            with self.subTest(include=include):
                wait_args = parser.parse_args(
                    [
                        "outbox",
                        "wait",
                        "/tmp/plan",
                        "--include",
                        include,
                    ]
                )
                self.assertEqual(include, wait_args.include)

    def test_mailbox_activation_accepts_fresh_lease_and_honors_opt_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            plan_dir.mkdir()
            mailbox.write_lease(
                plan_dir,
                "11111111-1111-4111-8111-111111111111",
                state_dir=state_dir,
            )

            enabled = runner._mailbox_activation_payload(
                argparse.Namespace(state_dir=str(state_dir), no_mailbox=False),
                plan_dir,
            )
            disabled = runner._mailbox_activation_payload(
                argparse.Namespace(state_dir=str(state_dir), no_mailbox=True),
                plan_dir,
            )

            self.assertTrue(enabled["enabled"])
            self.assertEqual("fresh-lease", enabled["reason"])
            self.assertTrue(enabled["lease_fresh"])
            self.assertFalse(disabled["enabled"])
            self.assertEqual("disabled-by-flag", disabled["reason"])
            self.assertFalse(disabled["lease_fresh"])

    def test_review_guard_exclude_is_repeatable_and_defaults_unset(self) -> None:
        parser = cli.build_parser()

        default_args = parser.parse_args(["run", "/tmp/plan"])
        repeated_args = parser.parse_args(
            [
                "run",
                "/tmp/plan",
                "--review-guard-exclude",
                "claude/.claude-personal",
                "--review-guard-exclude",
                "notes",
            ]
        )

        self.assertIsNone(default_args.review_guard_exclude)
        self.assertEqual(
            ["claude/.claude-personal", "notes"],
            repeated_args.review_guard_exclude,
        )
        self.assertIn("review_guard_exclude", runner._RESUME_OVERLAY_DESTS)

    def test_review_guard_include_ignored_defaults_off_and_round_trips(
        self,
    ) -> None:
        parser = cli.build_parser()

        default_args = parser.parse_args(["run", "/tmp/plan"])
        enabled_args = parser.parse_args(
            ["run", "/tmp/plan", "--review-guard-include-ignored"]
        )

        self.assertFalse(default_args.review_guard_include_ignored)
        self.assertFalse(runner._review_guard_include_ignored(default_args))
        self.assertTrue(enabled_args.review_guard_include_ignored)
        self.assertTrue(runner._review_guard_include_ignored(enabled_args))
        self.assertIn(
            "review_guard_include_ignored", runner._RESUME_OVERLAY_DESTS
        )

    def test_review_guard_exclude_merges_env_and_flags(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "run",
                "/tmp/plan",
                "--review-guard-exclude",
                "claude/",
                "--review-guard-exclude",
                "./nested/dir",
            ]
        )

        with mock.patch.dict(
            os.environ,
            {runner.REVIEW_GUARD_EXCLUDE_ENV_VAR: os.pathsep.join(["notes", "claude"])},
        ):
            resolved = runner._review_guard_excludes(args)

        self.assertEqual(("claude", "nested/dir", "notes"), resolved)

    def test_review_guard_exclude_rejects_absolute_and_parent_paths(self) -> None:
        parser = cli.build_parser()

        for value in ("/etc", "../outside", "a/../../b"):
            with self.subTest(value=value):
                args = parser.parse_args(
                    ["run", "/tmp/plan", "--review-guard-exclude", value]
                )
                with self.assertRaises(runner.RunError):
                    runner._review_guard_excludes(args)

    def test_review_guard_exclude_invalid_value_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(Path(tmp) / "plan"),
                    "--cwd",
                    tmp,
                    "--review-guard-exclude",
                    "../escape",
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("--review-guard-exclude", payload["error"])

    def test_resume_overlay_rehydrates_review_guard_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            plan_dir.mkdir()
            plan_state_dir = state.plan_state_dir(plan_dir, state_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / events.STATE_FILE_NAME).write_text(
                json.dumps({"run_config": {"review_guard_exclude": ["claude"]}}),
                encoding="utf-8",
            )
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(state_dir),
            ]
            args = cli.build_parser().parse_args(tokens)

            overlaid = runner._apply_resume_config(args, tokens)

            self.assertEqual(["claude"], overlaid.review_guard_exclude)

    def test_resume_overlay_rehydrates_no_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            plan_dir.mkdir()
            plan_state_dir = state.plan_state_dir(plan_dir, state_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / events.STATE_FILE_NAME).write_text(
                json.dumps({"run_config": {"no_mailbox": True}}),
                encoding="utf-8",
            )
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(state_dir),
            ]
            args = cli.build_parser().parse_args(tokens)

            overlaid = runner._apply_resume_config(args, tokens)

            self.assertTrue(overlaid.no_mailbox)

    def test_parser_uses_custom_program_name_in_help(self) -> None:
        parser = cli.build_parser(prog="asl")
        stdout = io.StringIO()

        self.assertTrue(parser.format_help().startswith("usage: asl "))
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["run", "--help"])

        self.assertEqual(0, raised.exception.code)
        self.assertIn("usage: asl run", stdout.getvalue())

    def test_review_verdict_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "review-verdict",
                "/tmp/plan",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )

        self.assertEqual("review-verdict", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertTrue(args.json)
        self.assertEqual("review:review_verdict_from_args", args.handler_ref)
        self.assertIs(
            cli.resolve_handler(args), review.review_verdict_from_args
        )

    def test_contract_check_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            ["contract-check", "/tmp/plan", "--session", "3", "--cwd", "/repo"]
        )

        self.assertEqual("contract-check", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("3", args.session)
        self.assertEqual("/repo", args.cwd)
        self.assertEqual("verification:contract_check_from_args", args.handler_ref)
        self.assertIs(
            cli.resolve_handler(args), verification.contract_check_from_args
        )

    def test_register_repositories_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "register-repositories",
                "/tmp/plan",
                "--cwd",
                "/repo",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )

        self.assertEqual("register-repositories", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/repo", args.cwd)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertTrue(args.json)
        self.assertEqual(
            "repository_registration:register_repositories_from_args",
            args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(args),
            repository_registration.register_repositories_from_args,
        )

    def test_plans_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "plans",
                "--state-dir",
                "/tmp/state",
                "--stale-days",
                "14",
                "--never-run",
                "--readiness",
                "reviewed",
                "--json",
            ]
        )

        self.assertEqual("plans", args.command)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(14.0, args.stale_days)
        self.assertTrue(args.never_run)
        self.assertEqual("reviewed", args.readiness)
        self.assertTrue(args.json)
        self.assertEqual("workbench:plans_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), workbench.plans_from_args)

    def test_expand_ingest_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            ["expand-ingest", "/tmp/plan", "--batch-file", "/tmp/batch.json"]
        )

        self.assertEqual("expand-ingest", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/tmp/batch.json", args.batch_file)
        self.assertEqual("prepare:expand_ingest_from_args", args.handler_ref)
        self.assertIs(
            cli.resolve_handler(args), prepare.expand_ingest_from_args
        )

    def test_importing_cli_does_not_import_heavy_handler_modules(self) -> None:
        # Guard: `import agent_session_loop.cli` must not eagerly pull in the
        # heavy handler subsystems. This keeps light probes like `asl status`
        # fast — they only import their own handler module, resolved lazily
        # from `handler_ref`. Run in a clean interpreter because sys.modules is
        # process-global and other tests import these modules.
        probe = (
            "import sys\n"
            "import agent_session_loop.cli\n"
            "heavy = [\n"
            "    name\n"
            "    for name in (\n"
            "        'workbench_server',\n"
            "        'workbench',\n"
            "        'runner',\n"
            "        'review',\n"
            "        'metrics',\n"
            "        'prepare',\n"
            "        'preflight',\n"
            "    )\n"
            "    if 'agent_session_loop.' + name in sys.modules\n"
            "]\n"
            "print(','.join(heavy))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual("", completed.stdout.strip(), completed.stdout)

    def test_cli_build_time_default_constants_match_source_modules(self) -> None:
        # cli.py mirrors these defaults locally so building the parser does not
        # import the heavy `workbench_server`/`metrics` modules. Keep the copies
        # honest against their authoritative definitions.
        self.assertEqual(
            workbench_server.DEFAULT_HOST, cli._WORKBENCH_DEFAULT_HOST
        )
        self.assertEqual(
            workbench_server.DEFAULT_PORT, cli._WORKBENCH_DEFAULT_PORT
        )
        self.assertEqual(
            metrics.DEFAULT_SINCE_DAYS, cli._METRICS_DEFAULT_SINCE_DAYS
        )
        self.assertEqual(
            retention.DEFAULT_MAX_AGE_DAYS, cli._RETENTION_DEFAULT_MAX_AGE_DAYS
        )

    def test_expand_ingest_requires_batch_file(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["expand-ingest", "/tmp/plan"])

        self.assertEqual(2, raised.exception.code)

    def test_review_verdict_requires_plan_dir(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["review-verdict"])

        self.assertEqual(2, raised.exception.code)

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

    def test_run_subparser_actions_match_run_flag_spec(self) -> None:
        # Tripwire: the real `asl run` subparser surface must equal the set the
        # declarative RUN_FLAG_SPEC describes, so a flag can never be added to
        # the CLI (or the helper's supervisor parser) without going through the
        # shared spec.
        parser = cli.build_parser()
        subparsers_action = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        run_subparser = subparsers_action.choices["run"]

        real = {
            (tuple(action.option_strings), action.dest)
            for action in run_subparser._actions
            if action.dest not in ("help", "handler")
        }
        expected = {
            (tuple(flag.option_strings), flag.dest)
            for flag in run_flags.RUN_FLAG_SPEC
        }

        self.assertEqual(expected, real)

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
        self.assertEqual(
            "review-guard-unavailable",
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
        )
        self.assertIn(
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, events.PAUSE_REASONS
        )

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
            # The check runs at the bound root; the plan artifacts it reads are
            # named by absolute path inside the command source.
            self.assertEqual(".", check.cwd)
            self.assertIn(
                str(plans.canonicalize_path(plan_dir) / review.FINAL_REVIEW_FILENAME),
                check.argv[2],
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
    def _write_readiness(
        self,
        plan_dir: Path,
        *,
        state_value: str,
        checked_at: str,
    ) -> None:
        plans.write_readiness_metadata(
            plan_dir,
            plans.ReadinessMetadata(
                state=state_value,
                readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                checked_at=checked_at,
            ),
        )

    def _run_plans(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["plans", *argv])
        return code, stdout.getvalue(), stderr.getvalue()

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

    def test_plans_cli_text_and_json_shapes_with_readiness_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            data_home = tmp_dir / "data"
            share_root = data_home / "agent-plans"
            share_root.mkdir(parents=True)
            (tmp_dir / "config").mkdir()
            (tmp_dir / "tmp").mkdir()
            (tmp_dir / "home").mkdir()

            reviewed = share_root / "reviewed-plan"
            draft = share_root / "draft-plan"
            write_plan(reviewed, ("pending",))
            write_plan(draft, ("pending",))
            self._write_readiness(
                reviewed,
                state_value="reviewed",
                checked_at="2026-07-01T00:00:00+00:00",
            )
            self._write_readiness(
                draft,
                state_value="draft",
                checked_at="2026-07-02T00:00:00+00:00",
            )

            with patched_env(
                HOME=str(tmp_dir / "home"),
                XDG_CONFIG_HOME=str(tmp_dir / "config"),
                XDG_DATA_HOME=str(data_home),
                TMPDIR=str(tmp_dir / "tmp"),
            ):
                text_code, text_stdout, text_stderr = self._run_plans(
                    "--readiness",
                    "reviewed",
                )
                json_code, json_stdout, json_stderr = self._run_plans(
                    "--readiness",
                    "reviewed",
                    "--json",
                )

            self.assertEqual(0, text_code, text_stderr)
            self.assertEqual("", text_stderr)
            self.assertIn(f"- {plans.canonicalize_path(reviewed)}", text_stdout)
            self.assertIn("  state: ready", text_stdout)
            self.assertIn("  run state: absent", text_stdout)
            self.assertIn("  readiness: reviewed", text_stdout)
            self.assertNotIn(str(plans.canonicalize_path(draft)), text_stdout)

            self.assertEqual(0, json_code, json_stderr)
            self.assertEqual("", json_stderr)
            payload = json.loads(json_stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("plans", payload["command"])
            self.assertEqual(
                {
                    "never_run": False,
                    "readiness": "reviewed",
                    "stale_days": None,
                },
                payload["filters"],
            )
            self.assertEqual(1, payload["plan_count"])
            self.assertEqual(
                str(plans.canonicalize_path(reviewed)),
                payload["plans"][0]["plan_dir"],
            )
            self.assertEqual("ready", payload["plans"][0]["primary_state"])
            self.assertEqual("reviewed", payload["plans"][0]["readiness"]["state"])

    def test_plans_cli_stale_and_never_run_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            data_home = tmp_dir / "data"
            share_root = data_home / "agent-plans"
            share_root.mkdir(parents=True)
            state_root = tmp_dir / "state"
            (tmp_dir / "config").mkdir()
            (tmp_dir / "tmp").mkdir()
            (tmp_dir / "home").mkdir()

            abandoned = share_root / "abandoned-plan"
            recent = share_root / "recent-plan"
            write_plan(abandoned, ("pending",))
            write_plan(recent, ("pending",))
            self._write_readiness(
                abandoned,
                state_value="reviewed",
                checked_at="2026-07-01T00:00:00+00:00",
            )
            _write_state_payload(
                plans.canonicalize_path(recent),
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260810T120000Z-9",
                    "run_started_at": "2026-08-10T12:00:00+00:00",
                    "run_finished_at": "2026-08-11T12:45:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )

            with patched_env(
                HOME=str(tmp_dir / "home"),
                XDG_CONFIG_HOME=str(tmp_dir / "config"),
                XDG_DATA_HOME=str(data_home),
                TMPDIR=str(tmp_dir / "tmp"),
            ):
                stale_code, stale_stdout, stale_stderr = self._run_plans(
                    "--state-dir",
                    str(state_root),
                    "--stale-days",
                    "14",
                    "--json",
                )
                never_run_code, never_run_stdout, never_run_stderr = self._run_plans(
                    "--state-dir",
                    str(state_root),
                    "--never-run",
                    "--json",
                )

            self.assertEqual(0, stale_code, stale_stderr)
            self.assertEqual("", stale_stderr)
            stale_payload = json.loads(stale_stdout)
            self.assertEqual(1, stale_payload["plan_count"])
            self.assertEqual(
                [str(plans.canonicalize_path(abandoned))],
                [entry["plan_dir"] for entry in stale_payload["plans"]],
            )

            self.assertEqual(0, never_run_code, never_run_stderr)
            self.assertEqual("", never_run_stderr)
            never_run_payload = json.loads(never_run_stdout)
            self.assertEqual(1, never_run_payload["plan_count"])
            self.assertEqual(
                [str(plans.canonicalize_path(abandoned))],
                [entry["plan_dir"] for entry in never_run_payload["plans"]],
            )

    def test_dry_run_reports_mailbox_activation_and_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan with spaces"
            state_dir = tmp_dir / "state with spaces"
            write_plan(plan_dir, ("pending",))
            mailbox.append_guidance(
                plan_dir,
                "Prefer the smaller implementation.",
                state_dir=state_dir,
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = (
                str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            )
            base_argv = [
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
            ]

            active_result = subprocess.run(
                base_argv,
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            disabled_result = subprocess.run(
                [*base_argv, "--no-mailbox"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            active = json.loads(active_result.stdout)
            disabled = json.loads(disabled_result.stdout)
            self.assertTrue(active["planned"]["mailbox_enabled"])
            self.assertEqual(
                "mailbox-present",
                active["planned"]["mailbox_activation"]["reason"],
            )
            self.assertFalse(active["run_config"]["no_mailbox"])
            self.assertFalse(active["planned"]["no_mailbox"])
            self.assertIn(
                runner.MAILBOX_PROTOCOL_HEADING,
                active["planned"]["prompt"],
            )
            self.assertFalse(disabled["planned"]["mailbox_enabled"])
            self.assertEqual(
                "disabled-by-flag",
                disabled["planned"]["mailbox_activation"]["reason"],
            )
            self.assertTrue(disabled["run_config"]["no_mailbox"])
            self.assertTrue(disabled["planned"]["no_mailbox"])
            self.assertEqual(
                runner.build_session_prompt("codex", plan_dir),
                disabled["planned"]["prompt"],
            )

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

    def _write_gc_run(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        run_id: str,
        days_ago: float,
        log_bytes: int,
    ) -> Path:
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / "state.json").write_text(
            json.dumps(
                {"plan_dir": str(plans.canonicalize_path(plan_dir))}, sort_keys=True
            ),
            encoding="utf-8",
        )
        run_dir = plan_state / state.RUNS_DIR_NAME / run_id
        turn_dir = run_dir / "turn-0001-session-1"
        turn_dir.mkdir(parents=True)
        (turn_dir / "stderr.log").write_bytes(b"x" * log_bytes)
        finished_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        (turn_dir / "result.json").write_text(
            json.dumps(
                {"finished_at": finished_at.isoformat(), "status_result": "ok"},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return run_dir

    def test_gc_cli_dry_run_then_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            old = self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260401T000000000000Z-11",
                days_ago=90,
                log_bytes=4096,
            )
            newest = self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260803T000000000000Z-22",
                days_ago=1,
                log_bytes=16,
            )

            def _run_gc(*extra: str) -> dict:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "gc",
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

            dry = _run_gc("--dry-run")
            self.assertTrue(dry["ok"])
            self.assertEqual("gc", dry["command"])
            self.assertEqual("plan", dry["scope"])
            self.assertFalse(dry["applied"])
            self.assertEqual(1, dry["reclaimable_run_count"])
            self.assertGreaterEqual(dry["reclaimable_bytes"], 4096)
            self.assertEqual(0, dry["removed_run_count"])
            self.assertEqual(0, dry["reclaimed_bytes"])
            self.assertEqual(
                1, dry["retained_by_reason"][retention.RETAIN_NEWEST_RUN]["run_count"]
            )
            self.assertEqual(
                dry["reclaimable_bytes"], dry["plans"][0]["reclaimable_bytes"]
            )
            self.assertTrue(old.is_dir())

            applied = _run_gc("--apply")
            self.assertTrue(applied["applied"])
            self.assertEqual(1, applied["removed_run_count"])
            self.assertEqual(dry["reclaimable_bytes"], applied["reclaimed_bytes"])
            self.assertEqual(0, applied["failed_removal_count"])
            self.assertFalse(old.exists())
            self.assertTrue(newest.is_dir())

    def test_gc_cli_corpus_scope_aggregates_and_refuses_locked_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            sweepable = tmp_dir / "sweepable"
            locked = tmp_dir / "locked"
            write_plan(sweepable, ("pending",))
            write_plan(locked, ("pending",))
            for plan_dir in (sweepable, locked):
                self._write_gc_run(
                    plan_dir=plan_dir,
                    state_dir=state_dir,
                    run_id="20260401T000000000000Z-11",
                    days_ago=90,
                    log_bytes=2048,
                )
                self._write_gc_run(
                    plan_dir=plan_dir,
                    state_dir=state_dir,
                    run_id="20260803T000000000000Z-22",
                    days_ago=1,
                    log_bytes=16,
                )
            locked_state = state.plan_state_dir(locked, state_dir)
            (locked_state / state.LOCK_FILE_NAME).write_text(
                json.dumps(
                    {
                        "plan_dir": str(plans.canonicalize_path(locked)),
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "pid": 4_242_424,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            args = cli.build_parser().parse_args(
                ["gc", "--state-dir", str(state_dir), "--json"]
            )
            handler = cli.resolve_handler(args)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = handler(args)

            self.assertIs(retention.gc_from_args, handler)
            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())
            self.assertEqual("corpus", payload["scope"])
            self.assertFalse(payload["applied"])
            self.assertEqual(2, payload["plan_count"])
            self.assertEqual(1, payload["reclaimable_run_count"])
            self.assertEqual(
                2,
                payload["retained_by_reason"][retention.RETAIN_PLAN_LOCKED][
                    "run_count"
                ],
            )
            by_plan = {
                entry["plan_dir"]: entry["reclaimable_bytes"]
                for entry in payload["plans"]
            }
            self.assertEqual(
                0, by_plan[str(plans.canonicalize_path(locked))]
            )
            self.assertEqual(
                payload["reclaimable_bytes"],
                by_plan[str(plans.canonicalize_path(sweepable))],
            )
            self.assertTrue(
                (
                    locked_state / state.RUNS_DIR_NAME / "20260401T000000000000Z-11"
                ).is_dir()
            )

    def test_gc_cli_preserves_a_dead_local_lock_in_both_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            plan_dir = tmp_dir / "dead-locked"
            write_plan(plan_dir, ("pending",))
            old = self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260401T000000000000Z-11",
                days_ago=90,
                log_bytes=2048,
            )
            self._write_gc_run(
                plan_dir=plan_dir,
                state_dir=state_dir,
                run_id="20260803T000000000000Z-22",
                days_ago=1,
                log_bytes=16,
            )
            # A pid this host can prove dead: the lock every other command
            # reclaims on sight, and the one `gc` has promised not to touch.
            lock_path = (
                state.plan_state_dir(plan_dir, state_dir) / state.LOCK_FILE_NAME
            )
            lock_path.write_text(
                json.dumps(
                    {
                        "plan_dir": str(plans.canonicalize_path(plan_dir)),
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "pid": 4_242_424,
                        "hostname": socket.gethostname(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            lock_bytes = lock_path.read_bytes()

            def _gc(*argv: str) -> dict:
                args = cli.build_parser().parse_args(
                    ["gc", *argv, "--state-dir", str(state_dir), "--json"]
                )
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(0, cli.resolve_handler(args)(args))
                return json.loads(stdout.getvalue())

            def _decisions(payload: dict) -> list:
                entry = next(
                    plan
                    for plan in payload["plans"]
                    if plan["plan_dir"] == str(plans.canonicalize_path(plan_dir))
                )
                return [
                    (run["run_id"], run["reclaimable"], run["reason"])
                    for run in entry["runs"]
                ]

            corpus = _gc()
            self.assertEqual(lock_bytes, lock_path.read_bytes())
            plan = _gc(str(plan_dir))
            self.assertEqual(lock_bytes, lock_path.read_bytes())

            self.assertEqual(_decisions(corpus), _decisions(plan))
            self.assertEqual(
                {reason for _id, _reclaimable, reason in _decisions(plan)},
                {retention.RETAIN_PLAN_LOCKED},
            )
            self.assertEqual(0, plan["reclaimable_run_count"])

            applied = _gc(str(plan_dir), "--apply")
            self.assertEqual(0, applied["removed_run_count"])
            self.assertEqual(lock_bytes, lock_path.read_bytes())
            self.assertTrue(old.is_dir())

    def test_gc_rejects_apply_together_with_dry_run(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["gc", "--apply", "--dry-run"])

        self.assertEqual(2, raised.exception.code)

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

    def test_status_next_command_pending_shows_per_harness_invocations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("complete", "pending"))

            payload = state.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )
            next_command = payload["next_command"]
            self.assertFalse(next_command["complete"])
            self.assertEqual("2", next_command["next_pending_id"])
            invocations = next_command["next_steps"][0]["invocations"]
            resolved_plan_dir = payload["plan_dir"]
            self.assertEqual(
                f"$session-run {resolved_plan_dir}/", invocations["codex"]
            )
            self.assertEqual(
                f"/session-run {resolved_plan_dir}/", invocations["claude"]
            )
            self.assertEqual(
                f"/session-run {resolved_plan_dir}/", invocations["copilot"]
            )

    def test_status_next_command_complete_uses_exact_session_run_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("complete", "complete"))

            payload = state.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )
            self.assertEqual(
                {
                    "complete": True,
                    "message": "All sessions implemented - nothing left to run.",
                },
                payload["next_command"],
            )
            self.assertEqual(
                responses.ALL_SESSIONS_COMPLETE_MESSAGE,
                payload["next_command"]["message"],
            )

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

    def test_doctor_json_reports_ordered_registered_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root = tmp_dir / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_dir / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get(
                "PYTHONPATH", ""
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--cwd",
                    str(workspace_root),
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
            self.assertEqual(
                str(workspace_root.resolve()),
                payload["workspace_root"],
            )
            self.assertEqual(
                ["SVC", "OAK"],
                [repository["alias"] for repository in payload["repositories"]],
            )
            self.assertEqual(
                str(repositories["SVC"].resolve()),
                payload["repositories"][0]["resolved_root"],
            )
            self.assertTrue(
                payload["prompt"].startswith(runner.TASK_PACKET_HEADING_PREFIX)
            )
            self.assertEqual(
                str(workspace_root.resolve()),
                payload["invocation"]["cwd"],
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


def _register_plan_in_state(state_dir: Path, plan_dir: Path) -> None:
    """Record ``plan_dir`` in a per-plan ``state.json`` under ``state_dir``.

    Mirrors the persisted metadata that ``asl sessions`` scans so a bare
    compact label routed through ``asl status`` resolves back to this plan.
    """
    canonical = plans.canonicalize_path(plan_dir)
    entry = state_dir / plans.path_sha256(canonical)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / events.STATE_FILE_NAME).write_text(
        json.dumps({"plan_dir": str(canonical)}, sort_keys=True),
        encoding="utf-8",
    )


class SplitCliTests(unittest.TestCase):
    """`asl split`: authoring chain, JSON payload, and stable failure reasons.

    Also carries the CLI-surface coverage migrated from the retired
    ``capture-inline-plan.py`` / ``resolve-plan-output.py`` script tests.
    """

    _TWO_SESSION_PLAN = (
        "# My Plan\n\nShared context.\n\n"
        "## Session 1: First\n\nDo one.\n\n"
        "## Session 2: Second\n\nDo two.\n"
    )
    _MULTI_REPO_PLAN = (
        "# My Plan\n\n"
        "## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        "| SVC | tundra_svc | git@github.com:tundra/SVC.git |\n"
        "| OAK | tundra_OAK | https://github.com/tundra/OAK.git |\n\n"
        "Shared context.\n\n"
        "## Session 1: First\n\n"
        "**Reads repositories**: SVC, OAK\n"
        "**Writes repositories**: SVC\n\nDo one.\n\n"
        "## Session 2: Second\n\n"
        "**Reads repositories**: OAK\n"
        "**Writes repositories**: (none)\n\nDo two.\n"
    )

    def _split_args(self, **overrides: object) -> argparse.Namespace:
        args = argparse.Namespace(
            plan_file=None,
            inline_stdin=False,
            base_dir=None,
            check=False,
            contracts_file=None,
            skill_prefix="codex",
            analysis_file=None,
            json=True,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _write_contracts(self, path: Path, ids: tuple[str, ...]) -> None:
        payload = {
            sid: {
                "done_when": [f"Session {sid} lands."],
                "verify": {
                    "session": sid,
                    "checks": [
                        {
                            "type": "command",
                            "argv": ["python3", "-m", "unittest", "-q"],
                            "timeout_seconds": 60,
                        }
                    ],
                },
            }
            for sid in ids
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _run_split(
        self, args: argparse.Namespace, *, stdin: str | None = None
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = authoring.split_from_args(args)
            else:
                code = authoring.split_from_args(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_split_subparser_is_wired_with_defaults(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(
            ["split", "--plan-file", "P.md", "--contracts-file", "c.json"]
        )
        self.assertEqual("authoring:split_from_args", parsed.handler_ref)
        self.assertIs(cli.resolve_handler(parsed), authoring.split_from_args)
        self.assertEqual("auto", parsed.skill_prefix)
        self.assertFalse(parsed.inline_stdin)
        # A source is required and the two sources are mutually exclusive.
        with self.assertRaises(SystemExit):
            parser.parse_args(["split", "--contracts-file", "c.json"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "split",
                    "--plan-file",
                    "P.md",
                    "--inline-stdin",
                    "--contracts-file",
                    "c.json",
                ]
            )

    def test_split_happy_path_produces_preflight_clean_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            base = tmp_dir / "out"

            code, out, err = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                )
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual("split", payload["command"])
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertEqual("draft", payload["readiness"]["state"])
            self.assertEqual("$", payload["skill_prefix"])
            self.assertIn(
                "session-expand", payload["next_steps"][0]["invocations"]["codex"]
            )

            output_dir = Path(payload["output_dir"])
            self.assertTrue(output_dir.is_dir())
            self.assertTrue((output_dir / "session-1.md").is_file())
            self.assertTrue((output_dir / "session-2.md").is_file())
            self.assertTrue((output_dir / "manifest.md").is_file())
            self.assertTrue((output_dir / "progress.md").is_file())
            self.assertEqual(
                sorted(str(output_dir / name) for name in (
                    "context.md",
                    "manifest.md",
                    "progress.md",
                    "session-1.md",
                    "session-2.md",
                )),
                sorted(payload["files"]),
            )

            report = preflight.run_core_preflight(output_dir)
            self.assertEqual((), report.red_findings)

    def test_split_single_session_reports_not_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text("# Plan\n\n## Session 1: Only\n\nDo.\n", encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1",))

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("single-session", payload["reason"])
            self.assertIn("splitting is not needed", payload["error"])

    def test_split_malformed_plan_passes_through_line_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text("# Plan\n\n## Session foo\n\nBody\n", encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1",))

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("line", payload["error"])
            self.assertIn("invalid session id", payload["error"])

    def test_split_missing_contract_reason_and_no_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1",))
            base = tmp_dir / "out"

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("missing-contract:2", payload["reason"])
            # Contracts are validated before the output dir is created.
            self.assertFalse(base.exists())

    def test_split_malformed_contract_reason_and_no_dir(self) -> None:
        # A malformed contract entry (a non-object, or one missing a required
        # field) must fail closed with a stable structured reason and no
        # traceback — never as an uncaught TypeError/KeyError — and must not
        # strand an output directory.
        valid_entry = {
            "done_when": ["Session 1 lands."],
            "verify": {
                "session": "1",
                "checks": [
                    {
                        "type": "command",
                        "argv": ["python3", "-m", "unittest", "-q"],
                        "timeout_seconds": 60,
                    }
                ],
            },
        }
        cases = (
            # A non-object entry for the first session (subscripting it would
            # otherwise raise TypeError).
            ({"1": "not-an-object", "2": "also-not"}, "invalid-contract:1"),
            # A present-but-incomplete entry (missing done_when) reached only
            # after the first session's valid contract builds cleanly.
            (
                {"1": valid_entry, "2": {"verify": {"checks": []}}},
                "invalid-contract:2",
            ),
        )
        for index, (contract_payload, expected_reason) in enumerate(cases):
            with self.subTest(reason=expected_reason):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_dir = Path(tmp)
                    plan = tmp_dir / "PLAN.md"
                    plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
                    contracts_file = tmp_dir / "contracts.json"
                    contracts_file.write_text(
                        json.dumps(contract_payload), encoding="utf-8"
                    )
                    base = tmp_dir / f"out{index}"

                    code, out, _ = self._run_split(
                        self._split_args(
                            plan_file=str(plan),
                            contracts_file=str(contracts_file),
                            base_dir=str(base),
                        )
                    )

                    self.assertEqual(1, code)
                    payload = json.loads(out)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(expected_reason, payload["reason"])
                    self.assertIn("invalid contract", payload["error"])
                    self.assertFalse(base.exists())

    def test_split_inline_stdin_captures_durable_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            base = tmp_dir / "out"

            code, out, err = self._run_split(
                self._split_args(
                    inline_stdin=True,
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                ),
                stdin=self._TWO_SESSION_PLAN,
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertTrue(payload["source_path"].endswith(".md"))
            self.assertIn("sources", payload["source_path"])
            self.assertTrue(Path(payload["source_path"]).is_file())

    def test_split_output_naming_is_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            base = tmp_dir / "out"
            args = self._split_args(
                plan_file=str(plan),
                contracts_file=str(contracts_file),
                base_dir=str(base),
            )

            _, out1, _ = self._run_split(args)
            _, out2, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(base),
                    force_new=True,
                )
            )

            first = Path(json.loads(out1)["output_dir"])
            second = Path(json.loads(out2)["output_dir"])
            self.assertNotEqual(first, second)
            self.assertEqual(f"{first.name}-2", second.name)

    def test_split_skill_prefix_auto_uses_detected_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))

            with mock.patch.object(
                harnesses, "detect_invoking_harness", return_value="claude"
            ):
                code, out, err = self._run_split(
                    self._split_args(
                        plan_file=str(plan),
                        contracts_file=str(contracts_file),
                        base_dir=str(tmp_dir / "out"),
                        skill_prefix="auto",
                    )
                )

            self.assertEqual(0, code, err)
            self.assertEqual("/", json.loads(out)["skill_prefix"])

    def test_split_skill_prefix_auto_undetectable_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))

            with mock.patch.object(
                harnesses, "detect_invoking_harness", return_value=None
            ):
                code, out, _ = self._run_split(
                    self._split_args(
                        plan_file=str(plan),
                        contracts_file=str(contracts_file),
                        base_dir=str(tmp_dir / "out"),
                        skill_prefix="auto",
                    )
                )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("auto-detect", payload["error"])

    def test_split_analysis_file_appends_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            analysis = tmp_dir / "analysis.md"
            analysis.write_text("## Token Budget\n\nSaved 42%.\n", encoding="utf-8")

            code, out, err = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                    analysis_file=str(analysis),
                )
            )

            self.assertEqual(0, code, err)
            output_dir = Path(json.loads(out)["output_dir"])
            manifest = (output_dir / "manifest.md").read_text(encoding="utf-8")
            self.assertIn("Saved 42%.", manifest)

    def test_split_missing_contracts_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(tmp_dir / "does-not-exist.json"),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("input-error", payload["reason"])

    # -- `asl split --check` -------------------------------------------------

    _BROKEN_PLAN = (
        "# Broken Plan\n\nShared context.\n\n"
        "## Session 1 No colon\n\nDo one.\n\n"
        "## Session 2 / Bad sep\n\nDo two.\n\n"
        "## Session 3a: Glued\n\nDo three.\n"
    )

    def test_check_subparser_makes_contracts_optional(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(["split", "--check", "--plan-file", "P.md"])
        self.assertTrue(parsed.check)
        self.assertIsNone(parsed.contracts_file)
        self.assertFalse(
            parser.parse_args(
                ["split", "--plan-file", "P.md", "--contracts-file", "c.json"]
            ).check
        )

    def test_split_without_contracts_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")

            code, out, _ = self._run_split(
                self._split_args(plan_file=str(plan), base_dir=str(Path(tmp) / "out"))
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("--contracts-file is required", payload["error"])

    def test_check_reports_every_finding_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._BROKEN_PLAN, encoding="utf-8")
            base = tmp_dir / "out"

            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(plan), check=True, base_dir=str(base)
                )
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertTrue(payload["checked"])
            self.assertEqual(str(plan), payload["source_path"])
            self.assertEqual(
                [
                    (5, "punctuation-free-title"),
                    (9, "unsupported-title-separator"),
                    (13, "session-id-suffix"),
                ],
                [
                    (finding["line"], finding["category"])
                    for finding in payload["findings"]
                ],
            )
            self.assertEqual(
                "## Session 1: No colon", payload["findings"][0]["suggested_line"]
            )
            self.assertIsNone(payload["findings"][2]["suggested_line"])
            self.assertEqual(payload["findings"][0]["message"], payload["error"])
            self.assertFalse(base.exists())
            self.assertEqual(
                self._BROKEN_PLAN, plan.read_text(encoding="utf-8")
            )

    def test_check_clean_plan_reports_canonical_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._TWO_SESSION_PLAN, encoding="utf-8")

            code, out, err = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["checked"])
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertEqual(2, payload["session_count"])
            self.assertNotIn("repositories", payload)
            self.assertNotIn("registry_digest", payload)

    def test_check_multi_repo_plan_reports_ordered_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._MULTI_REPO_PLAN, encoding="utf-8")

            code, out, err = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertEqual(
                ["SVC", "OAK"],
                [repo["alias"] for repo in payload["repositories"]],
            )
            self.assertEqual(64, len(payload["registry_digest"]))

    def test_split_multi_repo_plan_authors_registry_only_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(self._MULTI_REPO_PLAN, encoding="utf-8")
            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            contract_payload = json.loads(contracts_file.read_text(encoding="utf-8"))
            contract_payload["1"]["verify"]["checks"][0]["repo"] = "SVC"
            contract_payload["2"]["verify"]["checks"][0]["repo"] = "OAK"
            contracts_file.write_text(
                json.dumps(contract_payload), encoding="utf-8"
            )

            code, out, err = self._run_split(
                self._split_args(
                    plan_file=str(plan),
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                )
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertEqual(
                ["SVC", "OAK"],
                [repo["alias"] for repo in payload["repositories"]],
            )
            output_dir = Path(payload["output_dir"])
            manifest = (output_dir / "manifest.md").read_text(encoding="utf-8")
            context = (output_dir / "context.md").read_text(encoding="utf-8")
            first_session = (output_dir / "session-1.md").read_text(encoding="utf-8")
            second_session = (output_dir / "session-2.md").read_text(encoding="utf-8")
            self.assertIn("## Repositories", manifest)
            self.assertIn("| SVC | tundra_svc | github.com/tundra/SVC |", manifest)
            self.assertNotIn("## Repositories", context)
            self.assertNotIn("| Alias | Path | Origin |", context)
            self.assertEqual(
                1, first_session.count("**Reads repositories**: SVC, OAK")
            )
            self.assertEqual(1, first_session.count("**Writes repositories**: SVC"))
            self.assertEqual(1, second_session.count("**Reads repositories**: OAK"))
            self.assertEqual(
                1, second_session.count("**Writes repositories**: (none)")
            )
            self.assertEqual(
                plans.stable_readiness_sha256(output_dir),
                payload["readiness"]["readiness_sha256"],
            )

    def test_check_single_session_reports_not_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text("# Plan\n\n## Session 1: Only\n\nDo.\n", encoding="utf-8")

            code, out, _ = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("single-session", payload["reason"])
            self.assertEqual(["1"], payload["session_ids"])

    def test_check_missing_plan_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self._run_split(
                self._split_args(
                    plan_file=str(Path(tmp) / "nope.md"), check=True
                )
            )

            self.assertEqual(1, code)
            self.assertEqual("input-error", json.loads(out)["reason"])

    def test_check_inline_stdin_never_persists_a_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"

            code, out, _ = self._run_split(
                self._split_args(
                    inline_stdin=True, check=True, base_dir=str(base)
                ),
                stdin=self._BROKEN_PLAN,
            )

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertEqual("<stdin>", payload["source_path"])
            self.assertEqual(3, len(payload["findings"]))
            self.assertFalse(base.exists())

    def test_check_text_mode_prints_findings_and_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "PLAN.md"
            plan.write_text(self._BROKEN_PLAN, encoding="utf-8")

            code, _, err = self._run_split(
                self._split_args(plan_file=str(plan), check=True, json=False)
            )

            self.assertEqual(1, code)
            self.assertIn("3 malformed session heading(s)", err)
            self.assertIn("suggested: ## Session 1: No colon", err)

    def test_check_repair_round_trip_then_splits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = tmp_dir / "PLAN.md"
            plan.write_text(
                "# Plan\n\nContext.\n\n"
                "## Session 1 No colon\n\nDo one.\n\n"
                "## Session 2 / Bad sep\n\nDo two.\n",
                encoding="utf-8",
            )

            _, out, _ = self._run_split(
                self._split_args(plan_file=str(plan), check=True)
            )
            lines = plan.read_text(encoding="utf-8").splitlines()
            for finding in json.loads(out)["findings"]:
                lines[finding["line"] - 1] = finding["suggested_line"]
            repaired = "\n".join(lines) + "\n"

            contracts_file = tmp_dir / "contracts.json"
            self._write_contracts(contracts_file, ("1", "2"))
            code, out, err = self._run_split(
                self._split_args(
                    inline_stdin=True,
                    contracts_file=str(contracts_file),
                    base_dir=str(tmp_dir / "out"),
                ),
                stdin=repaired,
            )

            self.assertEqual(0, code, err)
            payload = json.loads(out)
            self.assertEqual(["1", "2"], payload["session_ids"])
            self.assertTrue(Path(payload["output_dir"]).is_dir())


class StatusLabelResolutionCliTests(unittest.TestCase):
    """End-to-end coverage for `asl status PLAN_DIR_OR_ID` label resolution."""

    def _run_status(self, args: list[str], *, check: bool) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent_session_loop", "status", *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=check,
        )

    def test_compact_label_from_sessions_resolves_to_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plans" / "04f2f9095c86-14c7dd6b7f18"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            _register_plan_in_state(state_dir, plan_dir)

            completed = self._run_status(
                [plan_dir.name, "--state-dir", str(state_dir), "--json"],
                check=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("status", payload["command"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)), payload["plan_dir"]
            )

    def test_compact_label_text_output_reports_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plans" / "text-label-plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            _register_plan_in_state(state_dir, plan_dir)

            completed = self._run_status(
                [plan_dir.name, "--state-dir", str(state_dir)],
                check=True,
            )
            self.assertIn(str(plans.canonicalize_path(plan_dir)), completed.stdout)

    def test_full_path_takes_precedence_over_state_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plans" / "precedence-plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            # Deliberately do NOT register in state: a full path must resolve
            # directly without needing any corpus entry.

            completed = self._run_status(
                [str(plan_dir), "--state-dir", str(state_dir), "--json"],
                check=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)), payload["plan_dir"]
            )

    def test_relative_path_with_directory_component_bypasses_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "sub" / "relative-plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    os.path.join("sub", "relative-plan"),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=tmp_dir,
                text=True,
                capture_output=True,
                check=True,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)), payload["plan_dir"]
            )

    def test_unknown_label_error_uses_plan_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()

            completed = self._run_status(
                ["no-such-label-xyz", "--state-dir", str(state_dir), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertEqual("no-such-label-xyz", payload["plan_reference"])
            self.assertNotIn("plan_dir", payload)

    def test_existing_malformed_plan_dir_retains_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "broken-plan"
            plan_dir.mkdir()

            completed = self._run_status(
                [str(plan_dir), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertEqual(str(plan_dir), payload["plan_reference"])

    def test_ambiguous_label_json_lists_sorted_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            first = tmp_dir / "a" / "dup-label"
            second = tmp_dir / "b" / "dup-label"
            write_plan(first, ("pending",))
            write_plan(second, ("pending",))
            _register_plan_in_state(state_dir, first)
            _register_plan_in_state(state_dir, second)

            completed = self._run_status(
                ["dup-label", "--state-dir", str(state_dir), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("ambiguous-plan", payload["reason"])
            self.assertEqual("dup-label", payload["plan_reference"])
            expected_candidates = sorted(
                [
                    str(plans.canonicalize_path(first)),
                    str(plans.canonicalize_path(second)),
                ]
            )
            self.assertEqual(
                expected_candidates, payload["details"]["candidates"]
            )
            self.assertEqual(
                str(plans.canonicalize_path(state_dir)),
                payload["details"]["state_root"],
            )

    def test_ambiguous_label_text_instructs_full_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            first = tmp_dir / "a" / "dup-text"
            second = tmp_dir / "b" / "dup-text"
            write_plan(first, ("pending",))
            write_plan(second, ("pending",))
            _register_plan_in_state(state_dir, first)
            _register_plan_in_state(state_dir, second)

            completed = self._run_status(
                ["dup-text", "--state-dir", str(state_dir)],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertIn("ambiguous", completed.stderr)
            self.assertIn("full plan directory path", completed.stderr)

    def test_non_directory_state_root_reports_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state-file"
            state_file.write_text("not a directory", encoding="utf-8")

            completed = self._run_status(
                ["some-label", "--state-dir", str(state_file), "--json"],
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("state-error", payload["reason"])
            self.assertEqual("some-label", payload["plan_reference"])

    def test_status_help_advertises_plan_dir_or_id(self) -> None:
        parser = cli.build_parser(prog="asl")
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["status", "--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("PLAN_DIR_OR_ID", help_text)
        self.assertIn("compact plan label printed after 'plan' by 'asl sessions'", help_text)


class MultiRepositoryRunnerFoundationTests(unittest.TestCase):
    def _workspace_plan(self, tmp_dir: Path) -> tuple[Path, Path, dict[str, Path]]:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace_root = tmp_dir / "workspace"
        repositories = init_multi_repo_workspace(workspace_root, specs)
        plan_dir = tmp_dir / "plan"
        write_multi_repo_plan(
            plan_dir,
            ("pending",),
            specs=specs,
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session="1",
                )
            },
        )
        session_path = plan_dir / "session-1.md"
        session_text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            session_text.replace(
                "**Reads repositories**: SVC, OAK\n"
                "**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n"
                "**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans.read_repository_registry(plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(
            plan_dir,
            workspace_root,
            registry.digest,
        )
        return workspace_root, plan_dir, repositories

    def _run(
        self,
        plan_dir: Path,
        workspace_root: Path,
        state_dir: Path,
        *extra: str,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env.update(env_overrides or {})
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "run",
                str(plan_dir),
                "--cwd",
                str(workspace_root),
                "--state-dir",
                str(state_dir),
                "--json",
                *extra,
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_multi_repo_dry_run_persists_workspace_and_scoped_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
            )
            metadata_path = plan_dir / plans.PLAN_METADATA_FILE_NAME
            legacy = json.loads(metadata_path.read_text(encoding="utf-8"))
            legacy["workspace_inventory_excludes"] = ["Library/Protected.dv"]
            metadata_path.write_text(json.dumps(legacy), encoding="utf-8")
            metadata_before = metadata_path.read_bytes()
            completed = self._run(
                plan_dir, workspace_root, tmp_dir / "state", "--dry-run"
            )

            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            root = str(workspace_root.resolve())
            self.assertEqual(root, payload["cwd"])
            self.assertEqual(root, payload["workspace_root"])
            self.assertEqual(root, payload["run_config"]["workspace_root"])
            self.assertEqual(root, payload["planned"]["workspace_root"])
            self.assertNotIn("workspace_inventory_excludes", payload)
            self.assertNotIn(
                "workspace_inventory_excludes", payload["run_config"]
            )
            invocation = payload["planned"]["invocation"]
            self.assertEqual(root, invocation["cwd"])
            self.assertEqual(root, invocation["workspace_root"])
            prompt = payload["planned"]["prompt"]
            self.assertTrue(prompt.startswith(runner.TASK_PACKET_HEADING_PREFIX))
            self.assertIn("Expected read aliases: SVC", prompt)
            self.assertIn("Writable aliases: SVC", prompt)
            self.assertIn("Alias:path/to/file", prompt)
            self.assertIn("OAK: path=tundra_OAK", prompt)
            self.assertIn(str(repositories["SVC"].resolve()), prompt)
            self.assertNotIn(str(repositories["OAK"].resolve()), prompt)
            self.assertEqual(
                metadata_before,
                (plan_dir / plans.PLAN_METADATA_FILE_NAME).read_bytes(),
            )

    def _fake_codex_env(self, tmp_dir: Path, **extra: str) -> dict[str, str]:
        fake_codex = install_fake_codex(tmp_dir)
        config_home = tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )
        return {
            "XDG_CONFIG_HOME": str(config_home),
            "FAKE_CODEX_MODE": "success",
            **extra,
        }

    def _fake_review_env(self, tmp_dir: Path, **extra: str) -> dict[str, str]:
        env = self._fake_codex_env(tmp_dir, **extra)
        config_path = (
            Path(env["XDG_CONFIG_HOME"])
            / config.APP_NAME
            / config.HARNESS_CONFIG_NAME
        )
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["claude"] = dict(payload["codex"])
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return env

    def test_multi_repo_review_combines_bases_and_uses_read_only_guard(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            dirty = repositories["OAK"] / "scratch.txt"
            dirty.write_text("pre-existing\n", encoding="utf-8")

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                env_overrides=self._fake_review_env(
                    tmp_dir, FAKE_CODEX_MODE="success-with-review"
                ),
            )

            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            review_turn = payload["turns"][-1]
            guard = review_turn["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual(["SVC", "OAK"], guard["read_aliases"])
            self.assertEqual([], guard["write_aliases"])
            bases = review_turn["review_repository_bases"]
            self.assertEqual(["SVC", "OAK"], [row["alias"] for row in bases])
            self.assertEqual(["OAK:scratch.txt"], bases[1]["dirty_paths"])
            review_path = plan_dir / f"session-{payload['review_session_id']}.md"
            body = review_path.read_text(encoding="utf-8")
            self.assertIn(
                review.review_session_version_marker(
                    review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
                ),
                body,
            )
            self.assertIn("**Reads repositories**: SVC, OAK", body)
            self.assertIn("**Writes repositories**: (none)", body)
            self.assertIn("## Ordered repository review base", body)
            artifact = json.loads(
                (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_text()
            )
            self.assertEqual(
                review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                artifact["version"],
            )
            cycle = payload["review_cycles"][-1]
            self.assertEqual(
                ["SVC", "OAK"],
                [row["alias"] for row in cycle["repositories"]],
            )

    def test_multi_repo_review_retains_qualified_repository_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            target = repositories["OAK"] / "README.md"

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                env_overrides=self._fake_review_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="success-with-review",
                    FAKE_CODEX_REVIEW_MUTATION_PATH=str(target),
                    FAKE_CODEX_REVIEW_MUTATION_TEXT="retained review edit\n",
                ),
            )

            self.assertEqual(
                1, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                payload["pause_reason"],
            )
            guard = payload["turns"][-1]["workspace_write_guard"]
            self.assertEqual([], guard["write_aliases"])
            self.assertEqual(
                ["OAK:README.md"], guard["violations"][0]["qualified_paths"]
            )
            self.assertTrue(guard["reverted_to_pending"])
            self.assertIn("retained review edit", target.read_text())
            review_path = plan_dir / f"session-{payload['review_session_id']}.md"
            self.assertIn(
                "- **Status**: pending", review_path.read_text(encoding="utf-8")
            )

    def test_multi_repo_review_accepts_repository_attributed_finding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                "--max-review-fix-cycles",
                "0",
                env_overrides=self._fake_review_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="success-with-review",
                    FAKE_CODEX_REVIEW_OUTCOMES="findings",
                ),
            )

            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            finding = payload["turns"][-1]["review_verdict"]["findings"][0]
            self.assertEqual("SVC", finding["repo"])
            self.assertTrue(finding["evidence"][0]["path"].startswith("SVC:"))
            self.assertEqual(
                "SVC", finding["verification_checks"][0]["repo"]
            )

    def test_multi_repo_review_repairs_persist_scopes_and_qualified_lineage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-harness",
                "claude",
                "--max-review-fix-cycles",
                "1",
                env_overrides=self._fake_review_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="success-with-review",
                    FAKE_CODEX_REVIEW_OUTCOMES="findings,all-clear",
                ),
            )

            self.assertEqual(
                0, completed.returncode, completed.stderr or completed.stdout
            )
            payload = json.loads(completed.stdout)
            first_cycle = payload["review_cycles"][0]
            self.assertEqual(
                runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                first_cycle["disposition"],
            )
            self.assertEqual("SVC", first_cycle["findings"][0]["repo"])
            self.assertTrue(
                first_cycle["findings"][0]["evidence"][0]["path"].startswith(
                    "SVC:"
                )
            )
            group = first_cycle["repair_groups"][0]
            self.assertEqual(["SVC", "OAK"], group["read_repositories"])
            self.assertEqual(["SVC"], group["write_repositories"])
            child = plan_dir / f"session-{group['repair_session_id']}.md"
            child_text = child.read_text(encoding="utf-8")
            self.assertIn("**Reads repositories**: SVC, OAK", child_text)
            self.assertIn("**Writes repositories**: SVC", child_text)
            self.assertIn("`SVC:", child_text)
            repair_turn = next(
                turn
                for turn in payload["turns"]
                if turn["session_id"] == group["repair_session_id"]
            )
            self.assertEqual(
                ["SVC"],
                repair_turn["workspace_write_guard"]["write_aliases"],
            )

    def test_multi_repo_rereview_delta_keeps_repository_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            context = verification.VerificationContext.from_plan(
                plan_dir, workspace_root
            )
            assert context.workspace is not None
            snapshot = runner._capture_workspace_write_guard_snapshot(
                workspace=context.workspace,
                plan_dir=plan_dir,
                exclude=(),
                include_ignored=False,
            )
            bases = runner._workspace_review_bases(context.workspace, snapshot)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "## Findings\nStatus: 1 Findings (0 high, 1 med, 0 low)\n",
                encoding="utf-8",
            )
            artifact = {
                "version": review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                "outcome": "findings",
                "findings": [
                    {
                        "id": "cross-repo",
                        "repo": "SVC",
                        "severity": "med",
                        "title": "Cross repository mismatch",
                        "evidence": [{"path": "OAK:README.md"}],
                        "explanation": "The repositories disagree.",
                        "repair_group": "cross-repo",
                        "done_criteria": ["Both repositories agree."],
                        "verification_checks": [
                            {
                                "type": "command",
                                "name": "cross-check",
                                "repo": "SVC",
                                "argv": ["python3", "-c", "pass"],
                            }
                        ],
                    }
                ],
            }
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(artifact), encoding="utf-8"
            )
            event_path = tmp_dir / "events.jsonl"
            event_path.write_text(
                json.dumps(
                    {
                        "name": events.REVIEW_CYCLE_EVENT,
                        "payload": {
                            "disposition": runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                            "repositories": bases,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            for alias in ("SVC", "OAK"):
                (repositories[alias] / "shared.txt").write_text(
                    f"{alias}\n", encoding="utf-8"
                )

            delta, fallback = runner._compute_registered_review_delta_context(
                event_path=event_path,
                plan_dir=plan_dir,
                workspace=context.workspace,
            )

            self.assertIsNone(fallback)
            assert delta is not None
            self.assertEqual(runner.REVIEW_SCOPE_DELTA, delta["mode"])
            self.assertEqual(
                ["SVC:shared.txt", "OAK:shared.txt"],
                delta["changed_files"],
            )
            rendered = runner._review_scope_detail_lines(delta)
            text = "\n".join(rendered)
            self.assertIn("git -C tundra_svc diff", text)
            self.assertIn("git -C tundra_OAK diff", text)

            event_path.write_text(
                json.dumps(
                    {
                        "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                        "payload": {
                            "materialized": True,
                            "current_repositories": bases,
                            "current_findings": artifact["findings"],
                            "repair_session_ids": ["2.1"],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            recovered_delta, recovered_fallback = (
                runner._compute_registered_review_delta_context(
                    event_path=event_path,
                    plan_dir=plan_dir,
                    workspace=context.workspace,
                )
            )

            self.assertIsNone(recovered_fallback)
            assert recovered_delta is not None
            self.assertEqual(delta["repositories"], recovered_delta["repositories"])
            self.assertEqual(delta["changed_files"], recovered_delta["changed_files"])

    def test_multi_repo_real_turn_allows_only_write_alias_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            target = repositories["SVC"] / "authorized.txt"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual(["SVC"], guard["authorized_aliases"])
            self.assertEqual(
                ["SVC:authorized.txt"],
                guard["changed"]["tundra_svc"]["qualified_paths"],
            )
            self.assertEqual(
                "complete",
                plans.read_session_status(
                    plans.SessionFile(
                        session_id=plans.SessionId.parse("1"),
                        path=plan_dir / "session-1.md",
                    )
                ).status,
            )

    def test_multi_repo_per_session_commit_uses_manifest_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            install_fake_spec(tmp_dir)
            spec_log = tmp_dir / "fake-spec.jsonl"
            mutations = [
                {"path": str(repositories[alias] / "changed.txt"), "text": alias}
                for alias in ("SVC", "OAK")
            ]
            env = self._fake_codex_env(
                tmp_dir,
                PATH=str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
                FAKE_SPEC_LOG=str(spec_log),
                FAKE_CODEX_MUTATIONS_JSON=json.dumps(mutations),
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                ["SVC", "OAK"],
                [commit["repository_alias"] for commit in payload["commits"]],
            )
            group = payload["commit_groups"][0]
            self.assertEqual("complete", group["status"])
            self.assertEqual(["SVC", "OAK"], group["ordered_aliases"])
            self.assertEqual(
                ["SVC", "OAK"],
                [row["alias"] for row in group["successful_repositories"]],
            )
            records = [json.loads(line) for line in spec_log.read_text().splitlines()]
            self.assertEqual(
                [
                    str(repositories["SVC"].resolve()),
                    str(repositories["OAK"].resolve()),
                ],
                [record["cwd"] for record in records],
            )

    def test_multi_repo_end_commit_selects_all_changed_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            install_fake_spec(tmp_dir)
            mutations = [
                {"path": str(repositories[alias] / "end.txt"), "text": alias}
                for alias in ("SVC", "OAK")
            ]
            env = self._fake_codex_env(
                tmp_dir,
                PATH=str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
                FAKE_CODEX_MUTATIONS_JSON=json.dumps(mutations),
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-at-end",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                ["SVC", "OAK"],
                [commit["repository_alias"] for commit in payload["commits"]],
            )
            group = payload["commit_groups"][0]
            self.assertEqual("end", group["scope"])
            self.assertIsNone(group["session_id"])
            self.assertEqual(["SVC", "OAK"], group["ordered_aliases"])

    def test_multi_repo_per_session_commit_skips_unchanged_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            install_fake_spec(tmp_dir)
            spec_log = tmp_dir / "fake-spec.jsonl"
            env = self._fake_codex_env(
                tmp_dir,
                PATH=str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
                FAKE_SPEC_LOG=str(spec_log),
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual([], payload["commits"])
            self.assertEqual([], payload["commit_groups"][0]["ordered_aliases"])
            self.assertEqual("complete", payload["commit_groups"][0]["status"])
            self.assertFalse(spec_log.exists())

    def test_multi_repo_per_session_commit_skips_harness_committed_head(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            install_fake_spec(tmp_dir)
            spec_log = tmp_dir / "fake-spec.jsonl"
            head_before = git_stdout(repositories["SVC"], "rev-parse", "HEAD")
            env = self._fake_codex_env(
                tmp_dir,
                PATH=str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
                FAKE_SPEC_LOG=str(spec_log),
                FAKE_CODEX_MUTATION_PATH=str(repositories["SVC"]),
                FAKE_CODEX_MUTATION_ACTION="commit-head",
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertNotEqual(
                head_before,
                git_stdout(repositories["SVC"], "rev-parse", "HEAD"),
            )
            self.assertEqual([], payload["commits"])
            self.assertEqual([], payload["commit_groups"][0]["ordered_aliases"])
            self.assertEqual("complete", payload["commit_groups"][0]["status"])
            self.assertFalse(spec_log.exists())

    def test_multi_repo_commit_preflight_requires_every_repository_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            install_fake_spec(tmp_dir)
            (repositories["OAK"] / "dirty.txt").write_text(
                "dirty\n", encoding="utf-8"
            )
            env = self._fake_codex_env(
                tmp_dir,
                PATH=str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertIn(
                payload["pause_reason"], {"not-loop-ready", "commit-preflight"}
            )
            self.assertIn("OAK", json.dumps(payload["pause_details"]))
            self.assertEqual([], payload["turns"])

    def test_multi_repo_partial_commit_recovers_before_next_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            workspace_root = tmp_dir / "workspace"
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_dir / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session="1",
            )
            contract_2 = _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session="2",
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending", "pending"),
                specs=specs,
                contracts_by_id={"1": contract, "2": contract_2},
            )
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(
                plan_dir, workspace_root, registry.digest
            )
            install_fake_spec(tmp_dir)
            spec_log = tmp_dir / "fake-spec.jsonl"
            state_dir = tmp_dir / "state"
            base_env = self._fake_codex_env(
                tmp_dir,
                PATH=str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
                FAKE_SPEC_LOG=str(spec_log),
            )
            mutations = [
                {"path": str(repositories[alias] / "changed.txt"), "text": alias}
                for alias in ("SVC", "OAK")
            ]
            first = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                "--commit-per-session",
                env_overrides={
                    **base_env,
                    "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                    "FAKE_SPEC_FAIL_CWD_BASENAME": repositories["OAK"].name,
                },
            )

            self.assertEqual(1, first.returncode, first.stderr)
            first_payload = json.loads(first.stdout)
            self.assertEqual("commit-failed", first_payload["pause_reason"])
            first_group = first_payload["commit_groups"][0]
            self.assertEqual("failed", first_group["status"])
            self.assertEqual("OAK", first_group["failed_alias"])
            self.assertEqual(["OAK"], first_group["remaining_aliases"])

            second = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                "--resume",
                env_overrides=base_env,
            )

            self.assertEqual(0, second.returncode, second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertTrue(second_payload["commit_group_recovery"]["success"])
            recovered = second_payload["commit_group_recovery"]["group"]
            self.assertEqual("complete", recovered["status"])
            records = [json.loads(line) for line in spec_log.read_text().splitlines()]
            self.assertEqual(
                [
                    repositories["SVC"].name,
                    repositories["OAK"].name,
                    repositories["OAK"].name,
                ],
                [Path(record["cwd"]).name for record in records],
            )

    def test_multi_repo_real_turn_retains_and_reports_read_only_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            target = repositories["OAK"] / "README.md"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                    FAKE_CODEX_MUTATION_TEXT="unauthorized but retained\n",
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["paused"])
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                payload["pause_reason"],
            )
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertFalse(guard["passed"])
            self.assertEqual("outside-read-scope", guard["violations"][0]["kind"])
            self.assertEqual(
                ["OAK:README.md"],
                guard["violations"][0]["qualified_paths"],
            )
            self.assertTrue(guard["reverted_to_pending"])
            self.assertNotIn("verification", payload["turns"][0])
            self.assertIn(
                "unauthorized but retained",
                target.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_multi_repo_scope_violation_requires_scope_expansion_and_preflight(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            first = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(
                        repositories["OAK"] / "README.md"
                    ),
                ),
            )
            self.assertEqual(1, first.returncode, first.stderr)

            unrepaired = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(tmp_dir),
            )
            self.assertEqual(1, unrepaired.returncode, unrepaired.stderr)
            unrepaired_payload = json.loads(unrepaired.stdout)
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                unrepaired_payload["pause_reason"],
            )
            self.assertEqual([], unrepaired_payload["turns"])

            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            (plan_dir / "session-1.expand.md").write_text(
                "# Refreshed expansion\n", encoding="utf-8"
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-08-08T00:00:00Z",
                ),
            )
            repaired = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(tmp_dir),
            )

            self.assertEqual(0, repaired.returncode, repaired.stderr)
            repaired_payload = json.loads(repaired.stdout)
            self.assertTrue(
                repaired_payload["turns"][0]["workspace_write_guard"]["passed"]
            )
            records = [
                json.loads(line)
                for line in Path(repaired_payload["events_path"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(
                any(
                    record["name"]
                    == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT
                    for record in records
                )
            )

    def test_multi_repo_scope_violation_repair_may_register_a_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            newly_registered = DEFAULT_MULTI_REPO_SPECS[2]
            init_multi_repo_workspace(workspace_root, (newly_registered,))
            first = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(
                        repositories["OAK"] / "README.md"
                    ),
                ),
            )
            self.assertEqual(1, first.returncode, first.stderr)
            first_guard = json.loads(first.stdout)["turns"][0][
                "workspace_write_guard"
            ]
            self.assertEqual(
                "outside-read-scope",
                first_guard["violations"][0]["kind"],
            )

            manifest_path = plan_dir / "manifest.md"
            alias, relative_path, origin = newly_registered
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + f"| {alias} | {relative_path} | {origin} |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            (plan_dir / "session-1.expand.md").write_text(
                "# Refreshed expansion after registering a repository\n",
                encoding="utf-8",
            )
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-08-08T00:00:00Z",
                ),
            )

            repaired = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(tmp_dir),
            )

            self.assertEqual(0, repaired.returncode, repaired.stderr)
            repaired_payload = json.loads(repaired.stdout)
            self.assertTrue(
                repaired_payload["turns"][0]["workspace_write_guard"]["passed"]
            )
            records = [
                json.loads(line)
                for line in Path(repaired_payload["events_path"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            repair_records = [
                record
                for record in records
                if record["name"] == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT
            ]
            self.assertEqual(1, len(repair_records))
            repair_guard = repair_records[0]["payload"]["details"][
                "workspace_write_guard"
            ]
            self.assertEqual(
                [relative_path],
                repair_guard["repair_registry_delta"],
            )
            self.assertEqual([], repair_guard["added"])

    def test_multi_repo_read_only_session_has_empty_write_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: (none)",
                ),
                encoding="utf-8",
            )
            target = repositories["SVC"] / "README.md"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0][
                "workspace_write_guard"
            ]
            self.assertEqual([], guard["write_aliases"])
            self.assertEqual(
                "read-only-repository",
                guard["violations"][0]["kind"],
            )

    def test_multi_repo_read_only_session_passes_without_repository_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: (none)",
                ),
                encoding="utf-8",
            )
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(tmp_dir),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0][
                "workspace_write_guard"
            ]
            self.assertTrue(guard["passed"])
            self.assertEqual([], guard["write_aliases"])
            self.assertEqual({}, guard["changed"])

    def test_multi_repo_real_turn_ignores_new_unregistered_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            added = workspace_root / "surprise-repository"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(added),
                    FAKE_CODEX_MUTATION_ACTION="git-init",
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual([], guard["added"])
            self.assertTrue((added / ".git").is_dir())

    def test_multi_repo_real_turn_rejects_removed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            removed = repositories["OAK"]
            retained_destination = tmp_dir / "retained-oak"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(removed),
                    FAKE_CODEX_MUTATION_ACTION="rename",
                    FAKE_CODEX_MUTATION_DESTINATION=str(retained_destination),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertEqual(["tundra_OAK"], guard["removed"])
            self.assertEqual("repository-removed", guard["violations"][0]["kind"])
            self.assertTrue((retained_destination / ".git").is_dir())

    def test_multi_repo_scope_guard_precedes_timeout_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--timeout",
                # Leave enough startup budget for this shard to schedule the
                # fake harness and publish its mutation under full-suite
                # parallel load.  The sleep remains comfortably beyond the
                # runner deadline, so this still exercises the timeout path.
                "10",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="timeout",
                    FAKE_CODEX_SLEEP="30",
                    FAKE_CODEX_MUTATION_PATH=str(
                        repositories["OAK"] / "README.md"
                    ),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                payload["pause_reason"],
            )
            self.assertTrue(payload["turns"][0]["timed_out"])

    def test_multi_repo_guard_compares_against_dirty_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            (repositories["OAK"] / "README.md").write_text(
                "pre-existing dirty state\n", encoding="utf-8"
            )
            target = repositories["SVC"] / "authorized.txt"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0][
                "workspace_write_guard"
            ]
            self.assertEqual(["tundra_svc"], list(guard["changed"]))

    def test_multi_repo_guard_ignores_git_ignored_build_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            ignore_file = repositories["OAK"] / ".gitignore"
            ignore_file.write_text("cache/\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "ignore cache"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            target = repositories["OAK"] / "cache" / "build.bin"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0][
                "workspace_write_guard"
            ]
            self.assertTrue(guard["passed"])
            self.assertEqual({}, guard["changed"])
            self.assertTrue(target.is_file())

    def test_multi_repo_guard_can_include_git_ignored_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            ignore_file = repositories["OAK"] / ".gitignore"
            ignore_file.write_text("cache/\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "ignore cache"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            target = repositories["OAK"] / "cache" / "build.bin"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-guard-include-ignored",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0][
                "workspace_write_guard"
            ]
            self.assertTrue(guard["include_ignored"])
            self.assertEqual(
                ["OAK:cache/build.bin"],
                guard["violations"][0]["qualified_paths"],
            )

    def test_multi_repo_run_rejects_binding_mismatch_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            metadata_path = plan_dir / plans.PLAN_METADATA_FILE_NAME
            before = metadata_path.read_bytes()
            moved_root = tmp_dir / "moved-workspace"
            shutil.copytree(workspace_root, moved_root)
            completed = self._run(
                plan_dir, moved_root, tmp_dir / "state", "--dry-run"
            )

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("workspace-error", payload["reason"])
            self.assertEqual(before, metadata_path.read_bytes())

    def test_status_projects_persisted_workspace_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
            )
            metadata_path = plan_dir / plans.PLAN_METADATA_FILE_NAME
            legacy = json.loads(metadata_path.read_text(encoding="utf-8"))
            legacy["workspace_inventory_excludes"] = ["Library/Protected.dv"]
            metadata_path.write_text(json.dumps(legacy), encoding="utf-8")
            before_run = state.status_payload(
                plans.inspect_plan(plan_dir),
                state_dir,
            )
            self.assertEqual(
                str(workspace_root.resolve()), before_run["workspace_root"]
            )
            self.assertEqual(before_run["workspace_root"], before_run["cwd"])
            self.assertNotIn("workspace_inventory_excludes", before_run)
            run_completed = self._run(
                plan_dir, workspace_root, state_dir, "--dry-run"
            )
            self.assertEqual(0, run_completed.returncode, run_completed.stderr)

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
            payload = json.loads(status_completed.stdout)
            self.assertEqual(str(workspace_root.resolve()), payload["workspace_root"])
            self.assertEqual(
                payload["workspace_root"],
                payload["runner_state"]["workspace_root"],
            )
            self.assertNotIn("workspace_inventory_excludes", payload)
            self.assertEqual(
                ["SVC", "OAK"],
                [repository["alias"] for repository in payload["repositories"]],
            )

    def test_resume_rejects_workspace_changed_since_persisted_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            first = self._run(plan_dir, workspace_root, state_dir, "--dry-run")
            self.assertEqual(0, first.returncode, first.stderr)

            moved_root = tmp_dir / "moved-workspace"
            shutil.copytree(workspace_root, moved_root)
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(plan_dir, moved_root, registry.digest)
            resumed = self._run(
                plan_dir,
                moved_root,
                state_dir,
                "--resume",
                "--dry-run",
            )

            self.assertEqual(1, resumed.returncode)
            payload = json.loads(resumed.stdout)
            self.assertEqual("workspace-error", payload["reason"])
            self.assertIn("persisted run", payload["error"])


class RegisterRepositoriesCommandTests(unittest.TestCase):
    """`asl register-repositories` over a legacy two-repository plan."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        tmp = Path(os.path.realpath(self._temp.name))
        self.workspace = tmp / "workspace"
        self.plan_dir = tmp / "plan"
        self.state_dir = tmp / "state"
        self.outside = tmp / "artifacts"
        repositories = init_multi_repo_workspace(
            self.workspace, specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.svc = repositories["SVC"]
        self.oak = repositories["OAK"]

    def _write_legacy_plan(self, *, stray_contract_path: bool = False) -> None:
        checks = [
            {"name": "svc-file", "type": "file_exists", "path": "src/new.py"},
            {
                "name": "oak-suite",
                "type": "command",
                "argv": ["true"],
                "cwd": str(self.oak),
            },
        ]
        if stray_contract_path:
            self.outside.mkdir(parents=True, exist_ok=True)
            checks.append(
                {
                    "name": "stray",
                    "type": "file_exists",
                    "path": f"{self.outside}/report.txt",
                }
            )
        write_plan(
            self.plan_dir,
            ("pending", "pending"),
            contracts_by_id={
                "1": _contract_block(
                    ", ".join(json.dumps(check) for check in checks), session="1"
                ),
                "2": _contract_block(
                    json.dumps(
                        {
                            "name": "oak-regex",
                            "type": "regex_in_file",
                            "path": f"{self.oak}/README.md",
                            "pattern": "test repo",
                        }
                    ),
                    session="2",
                ),
            },
        )

    def _register(self, *extra: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(
                [
                    "register-repositories",
                    str(self.plan_dir),
                    "--cwd",
                    str(self.svc),
                    "--state-dir",
                    str(self.state_dir),
                    *extra,
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_text_mode_prints_the_table_the_files_and_the_follow_ups(
        self,
    ) -> None:
        self._write_legacy_plan()

        code, stdout, stderr = self._register()

        self.assertEqual(0, code, stderr)
        self.assertIn("Status: applied", stdout)
        self.assertIn("| Alias | Path | Origin |", stdout)
        self.assertIn(
            "| tundra_svc | tundra_svc | github.com/tundra/SVC |",
            stdout,
        )
        for name in ("manifest.md", "session-1.md", "session-2.md"):
            self.assertIn(str(self.plan_dir / name), stdout)
        rebind = (
            f"asl prepare {self.plan_dir} --resume --rebind-workspace "
            f"--cwd {self.workspace}"
        )
        rerun = f"asl run {self.plan_dir} --cwd {self.workspace}"
        self.assertIn(rebind, stdout)
        self.assertIn(rerun, stdout)
        self.assertLess(stdout.index(rebind), stdout.index(rerun))

        registry = plans.read_repository_registry(self.plan_dir)
        self.assertEqual(("tundra_OAK", "tundra_svc"), registry.aliases)

    def test_json_output_serializes_the_whole_registration_result(self) -> None:
        self._write_legacy_plan()

        code, stdout, stderr = self._register("--json")

        self.assertEqual(0, code, stderr)
        payload = json.loads(stdout)
        for field in dataclasses.fields(
            repository_registration.RegistrationResult
        ):
            self.assertIn(field.name, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual("register-repositories", payload["command"])
        self.assertEqual("ok", payload["reason"])
        self.assertEqual("applied", payload["status"])
        self.assertIsNone(payload["missing_fact"])
        self.assertEqual(str(self.workspace), payload["workspace_root"])
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            [repository["alias"] for repository in payload["repositories"]],
        )
        self.assertEqual(["1", "2"], payload["sessions_changed"])
        self.assertIn(str(self.plan_dir / "manifest.md"), payload["files"])
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir), payload["readiness"]
        )
        self.assertEqual(
            [
                f"asl prepare {self.plan_dir} --resume --rebind-workspace "
                f"--cwd {self.workspace}",
                f"asl run {self.plan_dir} --cwd {self.workspace}",
            ],
            payload["next_commands"],
        )

    def test_refusal_exits_one_and_names_the_missing_fact_on_both_surfaces(
        self,
    ) -> None:
        self._write_legacy_plan(stray_contract_path=True)

        text_code, _text_stdout, text_stderr = self._register()
        json_code, json_stdout, _json_stderr = self._register("--json")

        self.assertEqual(1, text_code)
        self.assertIn("missing fact: contract-path-outside-registry", text_stderr)
        self.assertIn("'stray'", text_stderr)

        self.assertEqual(1, json_code)
        payload = json.loads(json_stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("refused", payload["reason"])
        self.assertEqual("refused", payload["status"])
        self.assertEqual(
            "contract-path-outside-registry", payload["missing_fact"]
        )
        self.assertIn("'stray'", payload["detail"])
        self.assertIn(
            "missing fact: contract-path-outside-registry", payload["error"]
        )
        self.assertEqual([], payload["files"])
        self.assertIsNone(plans.read_repository_registry(self.plan_dir))

    def test_active_lock_refuses_before_any_session_file_is_rewritten(
        self,
    ) -> None:
        self._write_legacy_plan()
        state.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)

        code, stdout, _stderr = self._register("--json")

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("lock-contention", payload["reason"])
        self.assertIsNone(plans.read_repository_registry(self.plan_dir))


class RegisterRepositoriesSourcePlanTests(unittest.TestCase):
    """`asl register-repositories` over a source plan, before any split."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(os.path.realpath(self._temp.name))
        self.workspace = self.tmp / "workspace"
        repositories = init_multi_repo_workspace(
            self.workspace, specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.svc = repositories["SVC"]
        self.oak = repositories["OAK"]

    def _source_plan(self, *, stray_contract_path: bool = False) -> str:
        checks = [
            {"name": "svc-file", "type": "file_exists", "path": "src/new.py"},
            {
                "name": "oak-suite",
                "type": "command",
                "argv": ["true"],
                "cwd": str(self.oak),
            },
        ]
        if stray_contract_path:
            outside = self.tmp / "artifacts"
            outside.mkdir(exist_ok=True)
            checks.append(
                {
                    "name": "stray",
                    "type": "file_exists",
                    "path": f"{outside}/report.txt",
                }
            )
        return "\n".join(
            [
                "# Test Plan",
                "",
                "## Context",
                "",
                "Shared context.",
                "",
                "## Session 1: First",
                "",
                "Add the module.",
                "",
                _contract_block(
                    ", ".join(json.dumps(check) for check in checks),
                    session="1",
                ),
                "",
                "---",
                "",
                "## Session 2: Second",
                "",
                f"Read {self.oak}/README.md before editing.",
                "",
                _contract_block(
                    json.dumps(
                        {
                            "name": "oak-regex",
                            "type": "regex_in_file",
                            "path": f"{self.oak}/README.md",
                            "pattern": "test repo",
                        }
                    ),
                    session="2",
                ),
                "",
            ]
        )

    def _write_source_plan(self, **kwargs: bool) -> Path:
        path = self.tmp / "PLAN.md"
        path.write_text(self._source_plan(**kwargs), encoding="utf-8")
        return path

    def _register(
        self, *extra: str, stdin: str | None = None
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [
            "register-repositories",
            "--cwd",
            str(self.svc),
            *extra,
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is None:
                code = cli.main(argv)
            else:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _split_check(self, path: Path) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            cli.main(
                [
                    "split",
                    "--check",
                    "--plan-file",
                    str(path),
                    "--cwd",
                    str(self.svc),
                    "--json",
                ]
            )
        return json.loads(stdout.getvalue() or stderr.getvalue())

    def test_migrated_plan_file_passes_split_check_and_spares_its_input(
        self,
    ) -> None:
        source = self._write_source_plan()
        original = source.read_bytes()
        output = self.tmp / "migrated.md"

        code, stdout, stderr = self._register(
            "--plan-file", str(source), "--output", str(output)
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual(original, source.read_bytes())
        self.assertIn("Status: applied", stdout)
        self.assertIn(str(output), stdout)

        migrated = output.read_text(encoding="utf-8")
        self.assertIn("## Repositories", migrated)
        self.assertIn(
            "| tundra_svc | tundra_svc "
            "| https://github.com/tundra/SVC |",
            migrated,
        )
        parsed = authoring.parse_plan_document(migrated)
        self.assertEqual(
            [["tundra_OAK", "tundra_svc"], ["tundra_OAK"]],
            [list(session.scope.reads) for session in parsed.sessions],
        )
        self.assertEqual(
            [["tundra_OAK", "tundra_svc"], ["tundra_OAK"]],
            [list(session.scope.writes) for session in parsed.sessions],
        )
        self.assertTrue(self._split_check(output)["ok"])

    def test_inline_stdin_puts_the_whole_migrated_plan_on_stdout(self) -> None:
        code, stdout, stderr = self._register(
            "--inline-stdin", stdin=self._source_plan()
        )

        self.assertEqual(0, code, stderr)
        piped = self.tmp / "piped.md"
        piped.write_text(stdout, encoding="utf-8")
        self.assertTrue(self._split_check(piped)["ok"])
        self.assertEqual(
            stdout,
            repository_registration.register_source_plan(
                self._source_plan(), cwd=self.svc
            ).markdown,
        )

    def test_json_output_carries_the_markdown_and_both_paths(self) -> None:
        source = self._write_source_plan()
        output = self.tmp / "migrated.md"

        code, stdout, stderr = self._register(
            "--plan-file", str(source), "--output", str(output), "--json"
        )

        self.assertEqual(0, code, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("applied", payload["status"])
        self.assertEqual(str(source), payload["source_path"])
        self.assertEqual(str(output), payload["output_path"])
        self.assertEqual(
            output.read_text(encoding="utf-8"), payload["markdown"]
        )
        self.assertEqual(["1", "2"], payload["sessions_changed"])

    def test_refusal_emits_no_markdown_and_writes_no_output_file(self) -> None:
        source = self._write_source_plan(stray_contract_path=True)
        output = self.tmp / "migrated.md"

        code, stdout, _stderr = self._register(
            "--plan-file", str(source), "--output", str(output), "--json"
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("refused", payload["reason"])
        self.assertEqual(
            "contract-path-outside-registry", payload["missing_fact"]
        )
        self.assertIsNone(payload["markdown"])
        self.assertFalse(output.exists())

    def test_output_naming_the_input_is_refused_before_anything_is_read(
        self,
    ) -> None:
        source = self._write_source_plan()
        original = source.read_bytes()

        code, stdout, _stderr = self._register(
            "--plan-file", str(source), "--output", str(source), "--json"
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("never replaces its source", payload["error"])
        self.assertEqual(original, source.read_bytes())

    def test_exactly_one_source_is_required(self) -> None:
        source = self._write_source_plan()

        neither_code, neither_stdout, _err = self._register("--json")
        both_code, both_stdout, _both_err = self._register(
            str(self.tmp / "plan"), "--plan-file", str(source), "--json"
        )

        for code, stdout in (
            (neither_code, neither_stdout),
            (both_code, both_stdout),
        ):
            self.assertEqual(1, code)
            payload = json.loads(stdout)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("exactly one", payload["error"])


if __name__ == "__main__":
    unittest.main()
