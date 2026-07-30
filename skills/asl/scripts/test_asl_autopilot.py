#!/usr/bin/env python3
"""Tests for the asl autopilot helper scaffold."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
import io
import itertools
import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout

MODULE_PATH = os.path.join(os.path.dirname(__file__), "asl_autopilot.py")
SPEC = importlib.util.spec_from_file_location("asl_autopilot", MODULE_PATH)
autopilot = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(autopilot)


class ArgParserTest(unittest.TestCase):
    def test_parser_exposes_stable_option_surface(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(
            [
                "--command",
                "asl run /tmp/plan",
                "--json",
                "--reply-stdin",
                "--wait-quota-reset",
            ]
        )
        self.assertEqual(args.command, "asl run /tmp/plan")
        self.assertTrue(args.json)
        self.assertTrue(args.reply_stdin)
        self.assertTrue(args.wait_quota_reset)

    def test_helper_no_longer_accepts_review_fix_plan_flag(self) -> None:
        # The runner owns repair cycles (Session 18); the helper-only
        # --max-review-fix-plans option is gone. It survives only inside the
        # `asl run ...` command string as a deprecated alias.
        parser = autopilot.build_arg_parser()
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(
                    [
                        "--command",
                        "asl run /tmp/plan",
                        "--max-review-fix-plans",
                        "4",
                    ]
                )

    def test_command_is_required(self) -> None:
        parser = autopilot.build_arg_parser()
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args([])

    def test_json_and_reply_stdin_default_false(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(["--command", "asl run /tmp/plan"])
        self.assertFalse(args.json)
        self.assertFalse(args.reply_stdin)
        self.assertFalse(args.wait_quota_reset)


class MainTest(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                autopilot.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_json_output_is_valid_parse_failure_payload(self) -> None:
        # A parse failure never reaches the supervisor, so main() stays
        # hermetic (no subprocess) while still exercising the JSON + exit path.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = autopilot.main(
                ["--command", "asl run /tmp/plan | cat", "--json"]
            )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["stage"], autopilot.PARSE_STAGE)
        self.assertEqual(payload["reason"], "shell-syntax")
        self.assertFalse(payload["ok"])
        self.assertEqual(exit_code, 1)


class ParseCommandTest(unittest.TestCase):
    def test_parses_minimal_run_and_appends_json(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], autopilot.PARSE_STAGE)
        self.assertEqual(result["executable"], "asl")
        self.assertEqual(result["env"], {})
        self.assertEqual(result["run_args"], ["run", "/tmp/plan", "--json"])
        self.assertEqual(result["argv"], ["asl", "run", "/tmp/plan", "--json"])
        self.assertTrue(result["json_appended"])
        self.assertEqual(
            result["max_review_fix_cycles"],
            autopilot.DEFAULT_MAX_REVIEW_FIX_CYCLES,
        )

    def test_existing_json_is_preserved_not_duplicated(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --json")
        self.assertTrue(result["ok"])
        self.assertFalse(result["json_appended"])
        self.assertEqual(result["run_args"].count("--json"), 1)

    def test_quoted_args_are_preserved(self) -> None:
        result = autopilot.parse_command(
            'asl run /tmp/plan --note "fix the parser bug"'
        )
        self.assertTrue(result["ok"])
        self.assertIn("--note", result["run_args"])
        note_index = result["run_args"].index("--note")
        self.assertEqual(result["run_args"][note_index + 1], "fix the parser bug")

    def test_env_assignments_are_captured_and_tilde_expanded(self) -> None:
        home = os.path.expanduser("~")
        result = autopilot.parse_command(
            "CLAUDE_CONFIG_DIR=~/.claude-personal asl run /tmp/plan"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["env"], {"CLAUDE_CONFIG_DIR": os.path.join(home, ".claude-personal")}
        )
        # The executable and subcommand are found after the env assignment.
        self.assertEqual(result["executable"], "asl")

    def test_env_value_does_not_shell_expand_variables(self) -> None:
        result = autopilot.parse_command("FOO=$BAR asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertEqual(result["env"], {"FOO": "$BAR"})

    def test_local_asl_path_is_supported(self) -> None:
        local = "/Users/dev/scripts-asl-interactive/bin/.local/bin/asl"
        result = autopilot.parse_command(f"{local} run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertEqual(result["executable"], local)
        self.assertEqual(result["argv"][0], local)

    def test_plan_dir_tilde_is_rewritten(self) -> None:
        home = os.path.expanduser("~")
        result = autopilot.parse_command("asl run ~/plans/demo")
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan_dir"], os.path.join(home, "plans", "demo"))
        self.assertEqual(result["run_args"][1], os.path.join(home, "plans", "demo"))

    def test_cwd_and_state_dir_tilde_rewritten(self) -> None:
        home = os.path.expanduser("~")
        result = autopilot.parse_command(
            "asl run /tmp/plan --cwd ~/repo --state-dir ~/state"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["cwd"], os.path.join(home, "repo"))
        self.assertEqual(result["state_dir"], os.path.join(home, "state"))

    def test_supported_flags_are_preserved(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --harness claude --review-harness codex "
            "--harness-model opus --review-harness-model gpt-5 "
            "--effort high --timeout 600 --max-sessions 3 "
            "--max-pending-sessions 30 --commit-at-end --no-unsafe-bypass "
            "--no-mailbox --allow-not-loop-ready"
        )
        self.assertTrue(result["ok"])
        args = result["run_args"]
        self.assertIn("--harness", args)
        self.assertIn("claude", args)
        self.assertIn("--review-harness", args)
        self.assertEqual(args[args.index("--harness-model") + 1], "opus")
        self.assertEqual(args[args.index("--review-harness-model") + 1], "gpt-5")
        self.assertIn("--commit-at-end", args)
        self.assertIn("--no-unsafe-bypass", args)
        self.assertIn("--no-mailbox", args)
        self.assertTrue(result["no_mailbox"])
        self.assertIn("--allow-not-loop-ready", args)
        self.assertEqual(args[args.index("--timeout") + 1], "600")

    def test_rejects_legacy_model_flag(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --model opus")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_review_harness_model_requires_review_harness(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness-model gpt-5"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")
        self.assertEqual(
            result["error"],
            "--review-harness-model requires --review-harness",
        )

    def test_resume_defers_review_harness_model_cross_field(self) -> None:
        # On a --resume command the review harness may be rehydrated from the
        # persisted run_config, so a model-only override must not be rejected at
        # parse time; the cross-field check is deferred to the child, which the
        # runner re-runs after applying the resume overlay. This mirrors runner
        # behavior (`_apply_resume_config` runs before `_validate_run_inputs`).
        result = autopilot.parse_command(
            "asl run /tmp/plan --resume --review-harness-model gpt-5"
        )
        self.assertTrue(result["ok"], result)
        args = result["run_args"]
        self.assertIn("--resume", args)
        self.assertEqual(
            args[args.index("--review-harness-model") + 1], "gpt-5"
        )

    def test_non_resume_still_rejects_model_only_override(self) -> None:
        # The deferral is scoped to --resume: without it, a model-only override
        # is still a parse-time invalid-argument.
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness-model gpt-5"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")
        self.assertEqual(
            result["error"], "--review-harness-model requires --review-harness"
        )

    def test_review_fix_cycles_forwarded_to_child(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness codex --max-review-fix-cycles 3"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_review_fix_cycles"], 3)
        args = result["run_args"]
        self.assertEqual(args[args.index("--max-review-fix-cycles") + 1], "3")

    def test_deprecated_review_fix_plans_alias_translates_to_cycles(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness codex --max-review-fix-plans 5"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_review_fix_cycles"], 5)
        args = result["run_args"]
        self.assertNotIn("--max-review-fix-plans", args)
        self.assertEqual(args[args.index("--max-review-fix-cycles") + 1], "5")

    def test_zero_review_fix_cycles_is_accepted(self) -> None:
        # 0 is the review-only mode: the runner reviews but creates no repairs.
        result = autopilot.parse_command(
            "asl run /tmp/plan --max-review-fix-cycles 0"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_review_fix_cycles"], 0)

    def test_rejects_negative_review_fix_cycles(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --max-review-fix-cycles -1"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_both_review_fix_flags_together(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --max-review-fix-cycles 2 "
            "--max-review-fix-plans 2"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")
        self.assertEqual(result["reason"], "invalid-argument")

    def test_review_guard_exclude_round_trips_repeated_values(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-guard-exclude claude/.claude-personal "
            "--review-guard-exclude notes"
        )
        self.assertTrue(result["ok"], result)
        args = result["run_args"]
        self.assertEqual(
            ["claude/.claude-personal", "notes"],
            [
                args[index + 1]
                for index, token in enumerate(args)
                if token == "--review-guard-exclude"
            ],
        )

    def test_review_guard_exclude_absent_by_default(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertNotIn("--review-guard-exclude", result["run_args"])

    def test_review_guard_include_ignored_round_trips(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-guard-include-ignored"
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("--review-guard-include-ignored", result["run_args"])

    def test_review_guard_include_ignored_absent_by_default(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertNotIn("--review-guard-include-ignored", result["run_args"])

    def test_rejects_shell_pipe(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan | tee log")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "shell-syntax")

    def test_rejects_redirect(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan > out.txt")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "shell-syntax")

    def test_rejects_command_substitution(self) -> None:
        result = autopilot.parse_command("asl run $(echo /tmp/plan)")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "shell-syntax")

    def test_resume_parsed_and_forwarded_to_child(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --resume")
        self.assertTrue(result["ok"])
        self.assertIn("--resume", result["run_args"])

    def test_resume_absent_by_default(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertNotIn("--resume", result["run_args"])

    def test_rejects_dry_run(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --dry-run")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-mode")

    def test_rejects_user_supplied_reply(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --reply hello")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-mode")

    def test_rejects_user_supplied_reply_stdin(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --reply-stdin")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-mode")

    def test_rejects_non_asl_executable(self) -> None:
        result = autopilot.parse_command("rm run /tmp/plan")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not-asl")

    def test_rejects_non_run_subcommand(self) -> None:
        result = autopilot.parse_command("asl status /tmp/plan")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-subcommand")

    def test_rejects_unknown_flag(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --bogus")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_non_positive_timeout(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --timeout 0")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_review_harness_equal_to_harness(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --harness claude --review-harness claude"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_empty_command(self) -> None:
        result = autopilot.parse_command("   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "empty-command")


class RunAutopilotParseIntegrationTest(unittest.TestCase):
    def test_valid_command_supervises_and_attaches_parsed_model(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(["--command", "asl run /tmp/plan"])
        captured = {}

        def fake_supervisor(parsed):
            captured["parsed"] = parsed
            return {"ok": True, "stage": autopilot.RUN_STAGE, "reason": "complete", "chunks": 1}

        result = autopilot.run_autopilot(args, supervisor=fake_supervisor)
        self.assertEqual(result["stage"], autopilot.RUN_STAGE)
        self.assertTrue(result["ok"])
        # The parsed command model is threaded through to the supervisor and
        # attached to the result for callers/reply handling.
        self.assertEqual(captured["parsed"]["run_args"], ["run", "/tmp/plan", "--json"])
        self.assertTrue(result["command"]["ok"])
        self.assertEqual(result["command"]["run_args"], ["run", "/tmp/plan", "--json"])

    def test_wait_quota_reset_reaches_the_supervisor_only_when_requested(self) -> None:
        captured: list = []

        def fake_supervisor(parsed, **kwargs):
            captured.append(kwargs)
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
            }

        parser = autopilot.build_arg_parser()
        autopilot.run_autopilot(
            parser.parse_args(["--command", "asl run /tmp/plan"]),
            supervisor=fake_supervisor,
        )
        autopilot.run_autopilot(
            parser.parse_args(
                ["--command", "asl run /tmp/plan", "--wait-quota-reset"]
            ),
            supervisor=fake_supervisor,
        )
        self.assertEqual(captured, [{}, {"wait_quota_reset": True}])

    def test_invalid_command_surfaces_parse_error_without_supervising(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(["--command", "asl run /tmp/plan | cat"])

        def fail_supervisor(parsed):  # pragma: no cover - must not be reached
            raise AssertionError("supervisor must not run for a parse failure")

        result = autopilot.run_autopilot(args, supervisor=fail_supervisor)
        self.assertEqual(result["stage"], autopilot.PARSE_STAGE)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "shell-syntax")

    def test_command_review_fix_cycles_reach_supervisor(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(
            [
                "--command",
                "asl run /tmp/plan --review-harness codex "
                "--max-review-fix-cycles 1",
            ]
        )
        captured = {}

        def fake_supervisor(parsed):
            captured["parsed"] = parsed
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
            }

        result = autopilot.run_autopilot(
            args,
            supervisor=fake_supervisor,
            review_reader=lambda parsed: {
                "ok": True,
                "outcome": "all-clear",
                "status_line": autopilot.REVIEW_STATUS_ALL_CLEAR,
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
                "review_output_path": "/tmp/plan/final-review.md",
                "summary": "clean",
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(captured["parsed"]["max_review_fix_cycles"], 1)
        self.assertEqual(result["command"]["max_review_fix_cycles"], 1)
        run_args = captured["parsed"]["run_args"]
        self.assertEqual(
            run_args[run_args.index("--max-review-fix-cycles") + 1], "1"
        )


class RunAutopilotPauseReplyTest(unittest.TestCase):
    """Cover pause -> needs_agent_reply enrichment and stdin resume."""

    def _args(self, command="asl run /tmp/plan", *, reply_stdin=False):
        parser = autopilot.build_arg_parser()
        argv = ["--command", command]
        if reply_stdin:
            argv.append("--reply-stdin")
        return parser.parse_args(argv)

    def test_paused_run_is_enriched_with_question_context(self) -> None:
        def paused_supervisor(parsed):
            return {
                "ok": False,
                "stage": autopilot.RUN_STAGE,
                "reason": "paused",
                "chunks": 1,
                "run": {"paused": True, "pause_reason": "input-error"},
            }

        questions = {
            "ok": True,
            "command": "questions",
            "session_id": "4",
            "rendered_prompt": "Answer the paused question.",
            "last_message": "Which option should I pick?",
            "artifact_paths": {"last_message_path": "/tmp/last.txt"},
        }

        result = autopilot.run_autopilot(
            self._args(),
            supervisor=paused_supervisor,
            questioner=lambda parsed: questions,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "needs_agent_reply")
        self.assertEqual(result["questions"]["session_id"], "4")
        self.assertEqual(result["command"]["run_args"], ["run", "/tmp/plan", "--json"])

    def test_paused_run_records_questions_error_when_context_missing(self) -> None:
        def paused_supervisor(parsed):
            return {"ok": False, "stage": autopilot.RUN_STAGE, "reason": "paused", "chunks": 1}

        error = {"ok": False, "reason": "no-paused-run", "error": "not paused"}
        result = autopilot.run_autopilot(
            self._args(),
            supervisor=paused_supervisor,
            questioner=lambda parsed: error,
        )
        # Without loadable context the result stays paused but records why.
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(result["questions_error"]["reason"], "no-paused-run")

    def test_user_required_harness_pause_skips_question_context(self) -> None:
        # A quota/auth/invalid-model user_required stop must not fetch Q&A
        # context; the result passes through with its reset info intact.
        def user_required_supervisor(parsed):
            return {
                "ok": False,
                "stage": autopilot.RUN_STAGE,
                "reason": "user_required",
                "chunks": 1,
                "run": {"paused": True, "pause_reason": "quota-exhausted"},
                "retries": {
                    "reason": "quota-exhausted",
                    "classification": {"retry_at": "2026-07-10T06:00:00Z"},
                },
            }

        def fail_questioner(parsed):  # pragma: no cover - must not run
            raise AssertionError("asl questions must not be fetched for user_required")

        result = autopilot.run_autopilot(
            self._args(),
            supervisor=user_required_supervisor,
            questioner=fail_questioner,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "user_required")
        self.assertNotIn("questions", result)
        self.assertNotIn("questions_error", result)
        self.assertEqual(
            result["retries"]["classification"]["retry_at"],
            "2026-07-10T06:00:00Z",
        )

    def test_stopped_run_is_terminal_and_skips_question_context(self) -> None:
        # A honored `asl control stop-after-current` is a clean early stop: it
        # must pass through as a terminal `stopped` report — no `asl questions`
        # fetch, no review verdict read, still `ok: true` with its counts.
        def stopped_supervisor(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "stopped",
                "chunks": 1,
                "completed": 2,
                "pending": 5,
            }

        def fail_questioner(parsed):  # pragma: no cover - must not run
            raise AssertionError("asl questions must not be fetched for a stop")

        def fail_review_reader(parsed):  # pragma: no cover - must not run
            raise AssertionError("no review verdict exists for an early stop")

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --review-harness codex"),
            supervisor=stopped_supervisor,
            questioner=fail_questioner,
            review_reader=fail_review_reader,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["pending"], 5)
        self.assertNotIn("questions", result)
        self.assertNotIn("questions_error", result)
        self.assertNotIn("review", result)

    def test_reply_stdin_streams_reply_into_supervisor(self) -> None:
        captured = {}

        def reply_supervisor(parsed, *, reply_text=None):
            captured["reply_text"] = reply_text
            return {"ok": True, "stage": autopilot.RUN_STAGE, "reason": "complete", "chunks": 1}

        result = autopilot.run_autopilot(
            self._args(reply_stdin=True),
            supervisor=reply_supervisor,
            stdin=io.StringIO("use the cached parser\n"),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(captured["reply_text"], "use the cached parser\n")

    def test_empty_reply_stops_without_supervising(self) -> None:
        def fail_supervisor(parsed, *, reply_text=None):  # pragma: no cover
            raise AssertionError("supervisor must not run for an empty reply")

        result = autopilot.run_autopilot(
            self._args(reply_stdin=True),
            supervisor=fail_supervisor,
            stdin=io.StringIO("   \n"),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "empty-reply")
        self.assertEqual(result["command"]["run_args"], ["run", "/tmp/plan", "--json"])


def _counter_clock():
    """Monotonic-ish clock that advances one unit per call (no real time)."""
    ticks = itertools.count()
    return lambda: float(next(ticks))


def _status_payload(pending, *, lock=None, ok=True, next_pending_id="1"):
    return {
        "ok": ok,
        "counts": {"pending": pending, "complete": 0},
        "next_pending_id": None if pending == 0 else next_pending_id,
        "lock": lock or {"present": False, "active": False, "stale": False},
        "sessions": [],
        "plan_dir": "/tmp/plan",
    }


def _review_resume(
    action,
    *,
    eligible,
    blocker=None,
    minimum_required_cap=1,
    source_disposition=None,
    artifact_error=None,
):
    return {
        "source_cycle": 1 if source_disposition is not None else None,
        "review_session_id": "2" if source_disposition is not None else None,
        "source_disposition": source_disposition,
        "action": action,
        "eligible": eligible,
        "blocker": blocker,
        "historical_materializations": max(0, minimum_required_cap - 1),
        "minimum_required_cap": minimum_required_cap,
        "selected_cap": max(0, minimum_required_cap - 1),
        "artifact_valid": blocker != "artifact-invalid",
        "artifact_error": artifact_error,
    }


LOCK_CREATED_AT = "2026-07-24T18:59:00+00:00"
RUN_STARTED_AT = "2026-07-24T19:00:00+00:00"
ATTACHED_RUN_ID = "run-attached"
OLDER_RUN_STARTED_AT = "2026-07-24T18:00:00+00:00"


def _active_lock(plan_dir="/tmp/plan", *, stale=False, created_at=LOCK_CREATED_AT):
    lock = {
        "present": True,
        "active": True,
        "stale": stale,
        "plan_dir": plan_dir,
        "reason": "stale" if stale else "active",
    }
    if created_at is not None:
        lock["created_at"] = created_at
    return lock


def _attach_status(idle, *, session_id="tidy-db-writes", number=3, total=8):
    """Attach-disposition status: active matching lock + a live running turn."""
    status = _status_payload(1, lock=_active_lock())
    status["runner_state"] = {
        "paused": False,
        "active_turn": {
            "session_id": session_id,
            "session_number": number,
            "total_sessions": total,
            "idle_seconds": idle,
            "elapsed_seconds": 120,
        },
    }
    return status


def _attach_waiting_status(*, pending_outbox=1, session_id="12"):
    """Attach-disposition status whose runner is in a bounded reply wait."""
    status = _status_payload(1, lock=_active_lock())
    waiting = {
        "question_id": "00000000-0000-0000-0000-000000000012",
        "session_id": session_id,
        "session_number": int(session_id),
        "turn_index": 2,
        "base_deadline": "2099-07-24T19:05:00+00:00",
        "hard_deadline": "2099-07-24T19:15:00+00:00",
    }
    status["runner_state"] = {
        "paused": False,
        "active_turn": None,
        "active_verification": None,
        "waiting_for_reply": waiting,
    }
    status["waiting_for_reply"] = waiting
    status["mailbox"] = {"open_questions": pending_outbox}
    return status


def _attached_run_status(pending, *, run_id="run-a", lock_created_at=LOCK_CREATED_AT):
    """Attach-disposition status naming the `run_id` that holds the plan."""
    status = _status_payload(pending, lock=_active_lock(created_at=lock_created_at))
    status["runner_state"] = {
        "present": True,
        "error": None,
        "run_id": run_id,
        "ok": True,
        "paused": False,
        "reason": "",
        "run_started_at": RUN_STARTED_AT,
        "run_finished_at": None,
        "active_turn": {
            "session_id": "tidy-db-writes",
            "idle_seconds": 4,
            "elapsed_seconds": 90,
        },
        "stale_unfinished": False,
    }
    return status


def _startup_attach_status(pending, *, lock_created_at=LOCK_CREATED_AT):
    """Attach poll inside the runner's lock-before-state startup window.

    The holder has taken the plan lock but has not persisted the `state.json`
    carrying its `run_id` yet, so `asl status` reports an active lock over an
    absent runner state.
    """
    status = _status_payload(pending, lock=_active_lock(created_at=lock_created_at))
    status["runner_state"] = {
        "present": False,
        "error": None,
        "active_turn": None,
        "active_verification": None,
    }
    return status


def _finished_run_status(
    pending,
    *,
    run_id="run-a",
    reason="stopped",
    ok=True,
    finished=True,
    paused=False,
    run_started_at=RUN_STARTED_AT,
):
    """Unlocked status whose persisted runner_state describes a finished run."""
    status = _status_payload(pending)
    status["runner_state"] = {
        "present": True,
        "error": None,
        "run_id": run_id,
        "ok": ok,
        "paused": paused,
        "reason": reason,
        "run_started_at": run_started_at,
        "run_finished_at": "2026-07-24T19:20:00+00:00" if finished else None,
        "active_turn": None,
        "stale_unfinished": not finished,
    }
    return status


def _leftover_state_attach_status(
    pending, *, run_id="run-old", run_started_at=OLDER_RUN_STARTED_AT,
    lock_created_at=LOCK_CREATED_AT,
):
    """Startup-window attach poll with the *previous* run's state still on disk.

    A new run holds the lock, but it has not overwritten `state.json` yet, so
    the visible `run_id` names the run before it.
    """
    status = _finished_run_status(
        pending, run_id=run_id, reason="stopped", run_started_at=run_started_at
    )
    status["lock"] = _active_lock(created_at=lock_created_at)
    return status


class _StatusScript:
    """Return scripted status payloads in order, repeating the last one."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self, parsed):
        index = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return self.statuses[index]


class _RunScript:
    """Return scripted run payloads and record the chunk indices requested."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.chunks = []

    def __call__(self, parsed, chunk_index):
        self.chunks.append(chunk_index)
        index = min(len(self.chunks) - 1, len(self.payloads) - 1)
        return self.payloads[index]


class _AttachClock:
    """Clock advanced by status probes, not by `clock()` calls.

    Attach-deadline tests would otherwise depend on how many times the loop
    calls `clock()` per iteration, an internal detail of the events-wait slicing.
    """

    def __init__(self, statuses, *, step):
        self._script = _StatusScript(statuses)
        self._step = step
        self.now = 0.0

    def status(self, parsed):
        payload = self._script(parsed)
        self.now += self._step
        return payload

    def __call__(self):
        return self.now

    @property
    def calls(self):
        return self._script.calls


def _never_run(parsed, chunk_index):  # pragma: no cover - guard for attach paths
    raise AssertionError("run_invoker must not be called")


def _max_sessions_of(argv):
    """Return the ``--max-sessions`` value in an argv list, or None if absent."""
    for index, token in enumerate(argv):
        if token == "--max-sessions" and index + 1 < len(argv):
            return argv[index + 1]
    return None


class ClassifyLockTest(unittest.TestCase):
    def _classify(self, parsed_extra, status):
        parsed = {"plan_dir": "/tmp/plan"}
        parsed.update(parsed_extra)
        return autopilot._classify_lock(parsed, status)

    def test_no_lock_is_clear(self) -> None:
        self.assertEqual(self._classify({}, _status_payload(1)), "clear")

    def test_present_but_inactive_is_clear(self) -> None:
        status = _status_payload(1, lock={"present": True, "active": False, "stale": False})
        self.assertEqual(self._classify({}, status), "clear")

    def test_active_stale_lock_is_stale(self) -> None:
        status = _status_payload(1, lock=_active_lock(stale=True))
        self.assertEqual(self._classify({}, status), "stale")

    def test_active_matching_lock_is_attach(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        self.assertEqual(self._classify({}, status), "attach")

    def test_active_different_plan_is_mismatch(self) -> None:
        status = _status_payload(1, lock=_active_lock(plan_dir="/other/plan"))
        self.assertEqual(self._classify({}, status), "mismatch")

    def test_harness_mismatch_is_mismatch(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        status["runner_state"] = {"active_turn": {"harness": "claude"}}
        self.assertEqual(self._classify({"harness": "codex"}, status), "mismatch")

    def test_harness_match_is_attach(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        status["runner_state"] = {"active_turn": {"harness": "claude"}}
        self.assertEqual(self._classify({"harness": "claude"}, status), "attach")

    def test_unobservable_harness_is_attach(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        self.assertEqual(self._classify({"harness": "claude"}, status), "attach")


class SuperviseRunInjectedTest(unittest.TestCase):
    """Drive the supervision loop with injected status/run invokers."""

    def _supervise(self, status_invoker, run_invoker, *, parsed=None, clock=None, **kwargs):
        return autopilot.supervise_run(
            parsed
            or {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status_invoker,
            run_invoker=run_invoker,
            clock=clock or _counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            **kwargs,
        )

    def test_already_complete_returns_without_running(self) -> None:
        status = _StatusScript([_status_payload(0)])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 0)

    def test_stale_lock_starts_a_run(self) -> None:
        status = _StatusScript([
            _status_payload(1, lock=_active_lock(stale=True)),
            _status_payload(0),
        ])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_mismatched_lock_is_user_required(self) -> None:
        status = _StatusScript([_status_payload(1, lock=_active_lock(plan_dir="/other"))])
        result = self._supervise(status, _never_run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "user_required")

    def test_attach_waits_then_completes(self) -> None:
        status = _StatusScript([
            _status_payload(1, lock=_active_lock()),
            _status_payload(1, lock=_active_lock()),
            _status_payload(0),
        ])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        # Polled three times: two attach waits then the completed state.
        self.assertEqual(status.calls, 3)

    def test_active_lock_with_zero_pending_attaches_before_completing(self) -> None:
        status = _StatusScript([
            _status_payload(0, lock=_active_lock()),
            _status_payload(0),
        ])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(status.calls, 2)

    def test_all_complete_initial_review_launches_with_review_harness(self) -> None:
        initial = _status_payload(0)
        initial["review_resume"] = _review_resume(
            "initial-review", eligible=False, blocker="review-harness-required"
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            source_disposition="all-clear",
        )
        status = _StatusScript([initial, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["review_resume"]["blocker"], "all-clear")
        self.assertNotIn("no automatic continuation", autopilot._human_summary(result))

    def test_corrected_malformed_review_launches_materialization(self) -> None:
        corrected = _status_payload(0)
        corrected["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=True,
            minimum_required_cap=1,
            source_disposition="malformed-review-repair",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            minimum_required_cap=2,
            source_disposition="findings-repeated",
        )
        status = _StatusScript([corrected, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 1,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])
        # The terminal projection prevents another launch after the reopened
        # chunk, even though its reported next-cap minimum is now higher.
        self.assertEqual(status.calls, 2)

    def test_raised_command_cap_overrides_status_historical_cap(self) -> None:
        resumable = _status_payload(0)
        resumable["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            minimum_required_cap=4,
            source_disposition="findings-repeated",
        )
        status = _StatusScript([resumable, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--max-review-fix-cycles", "3", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 3,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_unchanged_insufficient_cap_returns_guidance_without_launch(self) -> None:
        blocked = _status_payload(0)
        blocked["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--max-review-fix-cycles", "2", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(_StatusScript([blocked]), _never_run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["review_resume"]["minimum_required_cap"], 3)

    def test_command_cap_lower_than_status_selected_cap_does_not_launch(self) -> None:
        status_payload = _status_payload(0)
        status_payload["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=True,
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        status_payload["review_resume"]["selected_cap"] = 5
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--max-review-fix-cycles", "2", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(
            _StatusScript([status_payload]), _never_run, parsed=parsed
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)
        summary = autopilot._human_summary(result)
        self.assertIn("with a review harness", summary)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)

    def test_missing_rereview_launches_when_command_cap_is_sufficient(self) -> None:
        rereview = _status_payload(0)
        rereview["review_resume"] = _review_resume(
            "rereview",
            eligible=True,
            minimum_required_cap=2,
            source_disposition="repairs-materialized",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            minimum_required_cap=2,
            source_disposition="all-clear",
        )
        status = _StatusScript([rereview, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_missing_rereview_launches_below_reported_cap(self) -> None:
        rereview = _status_payload(0)
        rereview["review_resume"] = _review_resume(
            "rereview",
            eligible=True,
            minimum_required_cap=3,
            source_disposition="repairs-materialized",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            minimum_required_cap=3,
            source_disposition="all-clear",
        )
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--max-review-fix-cycles", "2", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        status = _StatusScript([rereview, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["review_resume"]["blocker"], "all-clear")

    def test_artifact_invalid_and_terminal_dispositions_do_not_launch(self) -> None:
        cases = (
            _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="artifact-invalid",
                source_disposition="malformed-review-repair",
                artifact_error="repair_group is missing",
            ),
            _review_resume(
                "terminal",
                eligible=False,
                blocker="findings-repeated",
                source_disposition="findings-repeated",
            ),
            _review_resume(
                "terminal",
                eligible=False,
                blocker="unstructured-findings",
                source_disposition="unstructured-findings",
            ),
            _review_resume(
                "terminal",
                eligible=False,
                blocker="all-clear",
                source_disposition="all-clear",
            ),
        )
        for review_resume in cases:
            with self.subTest(blocker=review_resume["blocker"]):
                status_payload = _status_payload(0)
                status_payload["review_resume"] = review_resume
                parsed = {
                    "plan_dir": "/tmp/plan",
                    "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
                    "review_harness": "codex",
                    "max_review_fix_cycles": 9,
                }
                result = self._supervise(
                    _StatusScript([status_payload]), _never_run, parsed=parsed
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["chunks"], 0)

    def test_run_repairs_and_unknown_actions_do_not_launch(self) -> None:
        for action in ("run-repairs", "future-action"):
            with self.subTest(action=action):
                status_payload = _status_payload(0)
                status_payload["review_resume"] = _review_resume(
                    action,
                    eligible=True,
                    source_disposition="repairs-materialized",
                )
                parsed = {
                    "plan_dir": "/tmp/plan",
                    "argv": [
                        "asl",
                        "run",
                        "/tmp/plan",
                        "--review-harness",
                        "codex",
                        "--json",
                    ],
                    "review_harness": "codex",
                    "max_review_fix_cycles": 9,
                }
                result = self._supervise(
                    _StatusScript([status_payload]), _never_run, parsed=parsed
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["chunks"], 0)

    def test_review_resume_does_not_launch_without_review_harness(self) -> None:
        status_payload = _status_payload(0)
        status_payload["review_resume"] = _review_resume(
            "materialize-repairs", eligible=True
        )
        result = self._supervise(_StatusScript([status_payload]), _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)
        summary = autopilot._human_summary(result)
        self.assertIn("with a review harness", summary)
        self.assertIn("eligible materialize-repairs continuation", summary)

    def test_older_status_without_review_resume_keeps_complete_behavior(self) -> None:
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 4,
        }
        result = self._supervise(
            _StatusScript([_status_payload(0)]), _never_run, parsed=parsed
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)

    def _seed_run_config(self, run_config):
        """Write a persisted ``state.json`` and return its path.

        Mirrors the on-disk shape the runner persists so a ``--resume``
        supervised command can resolve the effective review harness/cap from it.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": run_config}, handle)
        return state_path

    def _resume_parsed(self, state_path):
        """Parsed model for a resume-only command (no review harness/cap)."""
        return {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--resume", "--json"],
            "review_harness": None,
            "max_review_fix_cycles": autopilot.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            "max_review_fix_cycles_explicit": False,
            "resume": True,
        }

    def test_resume_command_honors_persisted_review_harness_for_initial_review(
        self,
    ) -> None:
        # A resume-only command has no --review-harness of its own; the runner
        # would rehydrate it from run_config. The supervisor must resolve the
        # same persisted harness so it launches the initial review instead of
        # stopping complete.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 2}
        )
        initial = _status_payload(0)
        initial["state_path"] = state_path
        initial["review_resume"] = _review_resume(
            "initial-review", eligible=False, blocker="review-harness-required"
        )
        terminal = _status_payload(0)
        terminal["state_path"] = state_path
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            source_disposition="all-clear",
        )
        status = _StatusScript([initial, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(
            status, run, parsed=self._resume_parsed(state_path)
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_resume_command_honors_persisted_higher_cap_for_materialization(
        self,
    ) -> None:
        # The persisted cap (3) is high enough for a materialize-repairs
        # continuation whose minimum required cap is 3; the resume-only
        # command's default cap of 2 would otherwise wrongly stop it.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 3}
        )
        resumable = _status_payload(0)
        resumable["state_path"] = state_path
        resumable["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        terminal = _status_payload(0)
        terminal["state_path"] = state_path
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            minimum_required_cap=4,
            source_disposition="findings-repeated",
        )
        status = _StatusScript([resumable, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(
            status, run, parsed=self._resume_parsed(state_path)
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_resume_command_stops_when_persisted_cap_too_low(self) -> None:
        # Persisted cap (2) is below the minimum required cap (3): the resume
        # command must not launch, matching what the resumed runner (which keeps
        # the persisted cap) would decide.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 2}
        )
        blocked = _status_payload(0)
        blocked["state_path"] = state_path
        blocked["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        result = self._supervise(
            _StatusScript([blocked]), _never_run, parsed=self._resume_parsed(state_path)
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)

    def test_resume_explicit_cap_overrides_persisted_lower_cap(self) -> None:
        # An explicit --max-review-fix-cycles on the resume command wins over the
        # lower persisted cap, mirroring the runner's resume overlay.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 1}
        )
        resumable = _status_payload(0)
        resumable["state_path"] = state_path
        resumable["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        terminal = _status_payload(0)
        terminal["state_path"] = state_path
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            source_disposition="findings-repeated",
        )
        parsed = self._resume_parsed(state_path)
        parsed["max_review_fix_cycles"] = 3
        parsed["max_review_fix_cycles_explicit"] = True
        status = _StatusScript([resumable, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_paused_run_stops_with_paused(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "input-error",
                "pause_message": "needs an answer",
            }
        ])
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(result["error"], "needs an answer")

    def test_non_paused_failure_is_blocked(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([{"ok": False, "paused": False, "reason": "verify-failed"}])
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "blocked")

    def test_status_probe_failure_stops(self) -> None:
        status = _StatusScript([
            {"ok": False, "stage": autopilot.RUN_STAGE, "reason": "status-error", "error": "boom"}
        ])
        result = self._supervise(status, _never_run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "status-error")

    def test_runaway_relaunch_caps_at_max_chunks(self) -> None:
        # Safety net: with no session budget, a run that keeps returning a clean
        # stop while pending never decreases would relaunch forever; max_chunks
        # bounds it and surfaces `capped`.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, max_chunks=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capped")
        self.assertEqual(result["chunks"], 3)

    def test_attach_capped_by_elapsed_time(self) -> None:
        # An attached run that never releases its lock would poll forever;
        # attach_max_seconds bounds it in wall clock and surfaces `capped`.
        driver = _AttachClock([_status_payload(1, lock=_active_lock())], step=30.0)
        result = self._supervise(
            driver.status, _never_run, clock=driver,
            events_stat=lambda _path: None, attach_max_seconds=90.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capped")
        self.assertIn("limit 90s", result["error"])
        # The self-launch counter is untouched by attach polls.
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(driver.calls, 4)

    def test_attach_is_not_bounded_by_max_chunks(self) -> None:
        # Regression guard: the two budgets are independent, so a tiny relaunch
        # cap must not evict the supervisor from a healthy attach loop.
        attached = _status_payload(1, lock=_active_lock())
        driver = _AttachClock([attached] * 6 + [_status_payload(0)], step=1.0)
        result = self._supervise(
            driver.status, _never_run, clock=driver,
            events_stat=lambda _path: None, max_chunks=1, attach_max_seconds=3600.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(driver.calls, 7)

    def test_attach_deadline_is_per_episode_not_cumulative(self) -> None:
        # Cumulative elapsed at the final attach poll is 160s; a cumulative
        # bound would cap. Per-episode elapsed never exceeds 40s.
        attached = _status_payload(1, lock=_active_lock())
        driver = _AttachClock(
            [attached, attached, _status_payload(1),
             attached, attached, _status_payload(0)],
            step=40.0,
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(
            driver.status, run, clock=driver,
            events_stat=lambda _path: None, attach_max_seconds=100.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def _budget_parsed(self, max_sessions):
        return {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--max-sessions", str(max_sessions), "--json"],
            "max_sessions": max_sessions,
        }

    def test_max_sessions_budget_stops_cleanly(self) -> None:
        # Global budget of 1 on a 3-session plan: after one completed session
        # (pending 3 -> 2) the supervisor stops cleanly without chunking through
        # the remaining sessions.
        status = _StatusScript([_status_payload(3), _status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "max-sessions"}])
        result = self._supervise(status, run, parsed=self._budget_parsed(1))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "max-sessions")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(run.chunks, [1])

    def test_budget_equal_to_pending_completes(self) -> None:
        # Budget covering every pending session must finish as `complete`, not
        # pre-empt with `max-sessions`, when pending reaches 0.
        status = _StatusScript([_status_payload(2), _status_payload(0)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=self._budget_parsed(2))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_no_budget_completes_in_one_chunk(self) -> None:
        # Without `--max-sessions`, the runner drives all pending sessions in a
        # single chunk and the supervisor reports `complete`.
        status = _StatusScript([_status_payload(4), _status_payload(0)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_budget_injection_caps_relaunch_after_pause(self) -> None:
        # Budget 3: chunk 1 completes 2 sessions (pending 3 -> 1) then pauses on
        # a recoverable verify-failed; the retry must be capped at the 1 session
        # still in budget so total completions never exceed 3.
        status = _StatusScript([
            _status_payload(3),
            _status_payload(1),
            _status_payload(0),
        ])
        payloads = [
            {"ok": False, "paused": True, "reason": "verify-failed"},
            {"ok": True, "paused": False, "reason": "all-complete"},
        ]
        seen = []

        def run(parsed, chunk_index):
            seen.append(list(parsed["argv"]))
            return payloads[min(chunk_index - 1, len(payloads) - 1)]

        result = self._supervise(status, run, parsed=self._budget_parsed(3))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # Chunk 1 launched with the full budget; the retry capped at the remainder.
        self.assertEqual(_max_sessions_of(seen[0]), "3")
        self.assertEqual(_max_sessions_of(seen[1]), "1")

    def test_budget_injection_passes_full_budget_without_pause(self) -> None:
        # No pause: a single chunk carries the full budget and completes.
        status = _StatusScript([_status_payload(2), _status_payload(0)])
        seen = []

        def run(parsed, chunk_index):
            seen.append(list(parsed["argv"]))
            return {"ok": True, "paused": False, "reason": "all-complete"}

        result = self._supervise(status, run, parsed=self._budget_parsed(5))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(len(seen), 1)
        self.assertEqual(_max_sessions_of(seen[0]), "5")

    def test_honored_stop_after_current_stops_cleanly(self) -> None:
        # The runner honored an out-of-band `asl control stop-after-current`:
        # it exits ok/non-paused with `reason: "stopped"` after finishing the
        # current session (pending 3 -> 2). The supervisor must report that as a
        # clean early stop instead of relaunching the remaining sessions.
        status = _StatusScript([_status_payload(3), _status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)
        # No further chunk relaunched.
        self.assertEqual(run.chunks, [1])

    def test_stop_reported_with_nothing_pending_completes(self) -> None:
        # A stop that lands exactly as pending reaches 0 must not pre-empt the
        # normal complete/review path.
        status = _StatusScript([_status_payload(1), _status_payload(0)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_stop_takes_precedence_over_a_relaunch_without_progress(self) -> None:
        # Even when the stopped chunk completed no session (pending unchanged),
        # the request is honored rather than relaunched, and the reported counts
        # stay consistent with the budget accounting.
        status = _StatusScript([_status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "stopped"}])
        result = self._supervise(status, run, max_chunks=5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(run.chunks, [1])

    def test_attached_run_that_honored_stop_is_terminal(self) -> None:
        # The supervisor attaches to a foreign run (run-a) that then honors an
        # out-of-band `asl control stop-after-current`: it releases the lock
        # having persisted `ok`/`reason: "stopped"` with sessions still pending.
        # The helper must report that clean early stop rather than launching a
        # chunk that would undo the operator's request.
        status = _StatusScript([
            _attached_run_status(3),
            _finished_run_status(2, reason="stopped"),
        ])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        # One session finished while attached (pending 3 -> 2).
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)

    def test_attached_stop_is_terminal_before_any_run_id_is_observable(self) -> None:
        # The runner takes the plan lock before it persists the state carrying
        # its `run_id`. Our only attached poll lands in that window and the run
        # then finishes fast, honoring the stop — so no poll ever saw a
        # `run_id`. The lock we attached to still identifies the run that wrote
        # the state (it started after the lock was created), so the stop is
        # honored rather than relaunched over.
        status = _StatusScript([
            _startup_attach_status(3),
            _finished_run_status(2, reason="stopped"),
        ])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)

    def test_attached_stop_ignores_the_previous_runs_leftover_run_id(self) -> None:
        # Same startup window, but `state.json` still holds the *previous* run's
        # outcome. Latching that `run_id` would make the new run's own stopped
        # state look like a mismatch; it is rejected as older than the lock, so
        # the fresh stop is still honored.
        status = _StatusScript([
            _leftover_state_attach_status(3, run_id="run-old"),
            _finished_run_status(2, run_id="run-new", reason="stopped"),
        ])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)

    def test_attached_run_that_never_persisted_state_still_launches(self) -> None:
        # Startup window again, but the run we attached to died without writing
        # anything: the `stopped` state left behind started before our lock
        # existed, so it belongs to an earlier run and must not block a launch.
        status = _StatusScript([
            _startup_attach_status(2),
            _finished_run_status(
                2, run_id="run-old", reason="stopped",
                run_started_at=OLDER_RUN_STARTED_AT,
            ),
            _status_payload(0),
        ])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_stop_without_a_readable_lock_timestamp_still_launches(self) -> None:
        # No `run_id` observed and no lock `created_at` to correlate against
        # (an older `asl status` payload): there is no trustworthy identity, so
        # the conservative pre-existing launch behavior stands.
        status = _StatusScript([
            _startup_attach_status(2, lock_created_at=None),
            _finished_run_status(2, reason="stopped"),
            _status_payload(0),
        ])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_stop_follows_a_lock_taken_over_mid_attach(self) -> None:
        # The run we first attached to (run-a) releases and another takes the
        # lock while we watch. The new holder's identity is not observable yet,
        # but its lock is: run-a's id must not be held against the run that
        # actually honored the stop.
        status = _StatusScript([
            _attached_run_status(3, run_id="run-a"),
            _startup_attach_status(2, lock_created_at="2026-07-24T19:05:00+00:00"),
            _finished_run_status(
                1, run_id="run-b", reason="stopped",
                run_started_at="2026-07-24T19:06:00+00:00",
            ),
        ])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["pending"], 1)

    def test_attached_stop_counts_every_session_finished_while_attached(self) -> None:
        # Progress made across several attach polls is attributed to `completed`,
        # measured from the pending count at the first poll (4 -> 1).
        status = _StatusScript([
            _attached_run_status(4),
            _attached_run_status(3),
            _attached_run_status(2),
            _finished_run_status(1, reason="stopped"),
        ])
        result = self._supervise(status, _never_run)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["pending"], 1)

    def test_attached_stop_with_nothing_pending_completes(self) -> None:
        # As with a spent budget, a stop that lands exactly as pending reaches 0
        # must not pre-empt the normal complete/review path.
        status = _StatusScript([
            _attached_run_status(1),
            _finished_run_status(0, reason="stopped"),
        ])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")

    def test_stale_stopped_state_from_an_older_run_still_launches(self) -> None:
        # Never attached: the plan is unlocked and the `stopped` state on disk
        # belongs to some earlier run. It must not block a new run.
        status = _StatusScript([
            _finished_run_status(2, run_id="run-old", reason="stopped"),
            _status_payload(0),
        ])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_stopped_state_from_a_different_run_id_still_launches(self) -> None:
        # We attached to run-b, but the state left behind was written by run-old
        # (run-b died before persisting its own outcome). The identity mismatch
        # means the `stopped` reason says nothing about the run we watched.
        status = _StatusScript([
            _attached_run_status(2, run_id="run-b"),
            _finished_run_status(2, run_id="run-old", reason="stopped"),
            _status_payload(0),
        ])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_run_stopped_for_another_reason_still_launches(self) -> None:
        # The attached run ended on its own `--max-sessions` budget, not on a
        # control request: the supervisor owns the remaining sessions and its own
        # (absent) budget, so it launches.
        status = _StatusScript([
            _attached_run_status(3, run_id="run-a"),
            _finished_run_status(2, run_id="run-a", reason="max-sessions"),
            _status_payload(0),
        ])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_unfinished_attached_stop_state_still_launches(self) -> None:
        # An orphaned state (no `run_finished_at`, flagged stale_unfinished) says
        # nothing about how the run ended, even when a `stopped` reason was
        # persisted mid-flight before the process died.
        status = _StatusScript([
            _attached_run_status(3, run_id="run-a"),
            _finished_run_status(2, run_id="run-a", reason="stopped", finished=False),
            _status_payload(0),
        ])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_attached_stop_respects_a_spent_budget_first(self) -> None:
        # Sessions the attached run finished count against the global budget, so
        # a budget already spent while attached reports `max-sessions` (its own
        # clean early stop) rather than the control-request stop.
        status = _StatusScript([
            _attached_run_status(3, run_id="run-a"),
            _finished_run_status(2, run_id="run-a", reason="stopped"),
        ])
        result = self._supervise(
            status, _never_run, parsed=self._budget_parsed(1)
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "max-sessions")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)

    def test_run_invoke_error_stops_with_run_error(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {"ok": False, "reason": "run-error", "error": "no JSON", "_invoke_error": True}
        ])
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "run-error")

    def test_recoverable_pause_retries_then_completes(self) -> None:
        # First chunk hits a recoverable verify-failed pause; the supervisor
        # relaunches the same session and the retry completes the plan.
        status = _StatusScript([
            _status_payload(1),
            _status_payload(1),
            _status_payload(0),
        ])
        run = _RunScript([
            {"ok": False, "paused": True, "reason": "verify-failed"},
            {"ok": True, "paused": False, "reason": "all-complete"},
        ])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # Chunk 1 paused, chunk 2 (the retry) completed.
        self.assertEqual(run.chunks, [1, 2])

    def test_recoverable_pause_retry_budget_is_exhausted(self) -> None:
        # The pause never clears, so after max_retries relaunches the supervisor
        # surfaces the pause instead of retrying forever.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "verify-failed",
                "pause_reason": "verify-failed",
                "pause_message": "checks failed",
            }
        ])
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(result["error"], "checks failed")
        # Two retries after the initial attempt -> three chunks total.
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(result["retries"]["reason"], "verify-failed")
        self.assertEqual(result["retries"]["count"], 2)
        self.assertTrue(result["retries"]["exhausted"])

    def test_non_recoverable_pause_is_not_retried(self) -> None:
        # A question/blocked pause needs a reply; it must not consume retries.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "input-error",
                "pause_reason": "input-error",
                "pause_message": "needs an answer",
            }
        ])
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)
        self.assertFalse(result["retries"]["exhausted"])

    def test_payload_pause_policy_overrides_recoverable_constants(self) -> None:
        # verify-failed is recoverable by the imported constants, but a payload
        # that carries pause_policy={recoverable: false} is the single source of
        # truth and wins: the supervisor surfaces the pause without relaunching.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "verify-failed",
                "pause_reason": "verify-failed",
                "pause_message": "checks failed",
                "pause_policy": {
                    "reason": "verify-failed",
                    "recoverable": False,
                    "requires_user": False,
                    "max_retries": 0,
                    "backoff_seconds": [],
                },
            }
        ])
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        # No retry consumed even though the constant set marks it recoverable.
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)
        self.assertFalse(result["retries"]["exhausted"])

    def test_payload_pause_policy_requires_user_overrides_constants(self) -> None:
        # A reason absent from the user-required constant set still surfaces as
        # user_required when the payload's pause_policy says so.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "runner-pause",
                "pause_reason": "runner-pause",
                "pause_message": "needs a human",
                "pause_policy": {
                    "reason": "runner-pause",
                    "recoverable": False,
                    "requires_user": True,
                    "max_retries": 0,
                    "backoff_seconds": [],
                },
            }
        ])
        result = self._supervise(status, run)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1])

    def test_verify_failed_exhausted_flag_stops_without_retry(self) -> None:
        # The runner marked the verification repair exhausted (an identical
        # failure fingerprint repeated), so the supervisor must not relaunch.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "verify-failed",
                "pause_reason": "verify-failed",
                "pause_message": "identical checks failed",
                "pause_details": {"verification_repair_exhausted": True},
            }
        ])
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        # Only the initial chunk ran; no retry consumed.
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)

    def test_review_verdict_exhausted_flag_stops_without_retry(self) -> None:
        # The runner burned its in-run review-verdict retries and reused the
        # verify-failed pause, so relaunching would just replay the same turn.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "verify-failed",
                "pause_reason": "verify-failed",
                "pause_message": "invalid review verdict",
                "pause_details": {"review_verdict_repair_exhausted": True},
            }
        ])
        result = self._supervise(status, run, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["retries"]["count"], 0)

    def test_verify_failed_changed_fingerprint_still_retries(self) -> None:
        # A changed verification failure (exhausted flag clear) stays eligible
        # for another bounded retry, which then completes the plan.
        status = _StatusScript([
            _status_payload(1),
            _status_payload(1),
            _status_payload(0),
        ])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "verify-failed",
                "pause_reason": "verify-failed",
                "pause_details": {"verification_repair_exhausted": False},
            },
            {"ok": True, "paused": False, "reason": "all-complete"},
        ])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1, 2])

    def test_default_run_invoker_forwards_status_probe_and_stall_threshold(self) -> None:
        # With no injected run_invoker, the default closure must thread the
        # status probe (reusing the status_invoker seam) and stall_threshold
        # into `_invoke_run`.
        calls = {"status": 0}

        def status_invoker(parsed):
            calls["status"] += 1
            # First poll -> pending (start a chunk); afterwards -> complete.
            return _status_payload(0) if calls["status"] > 1 else _status_payload(1)

        captured: dict = {}

        def fake_invoke_run(target, **kwargs):
            captured["target"] = target
            captured["kwargs"] = kwargs
            return {"ok": True, "paused": False, "reason": "all-complete"}

        original = autopilot._invoke_run
        autopilot._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
                status_invoker=status_invoker,
                clock=_counter_clock(),
                sleep=lambda *_: None,
                heartbeat=lambda *_: None,
                stall_threshold=42.0,
            )
        finally:
            autopilot._invoke_run = original

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        kwargs = captured["kwargs"]
        self.assertEqual(kwargs["stall_threshold"], 42.0)
        self.assertTrue(callable(kwargs["status_probe"]))
        # The probe reuses the status_invoker seam: calling it re-polls status.
        before = calls["status"]
        probe_payload = kwargs["status_probe"]()
        self.assertEqual(calls["status"], before + 1)
        self.assertTrue(probe_payload["ok"])

    def test_default_run_establishes_mailbox_lease_before_child_launch(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-prelaunch-lease-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plan_dir = os.path.join(tmp, "plan")
        state_root = os.path.join(tmp, "state")
        os.makedirs(plan_dir)
        mailbox_path = autopilot._mailbox.mailbox_path_for_plan(
            plan_dir, state_root
        )
        lease_path = autopilot._mailbox.lease_path_for_plan(plan_dir, state_root)

        initial = _status_payload(1)
        initial.update(
            {
                "plan_dir": plan_dir,
                "state_dir": str(lease_path.parent),
                "mailbox": {
                    "path": str(mailbox_path),
                    "lease_path": str(lease_path),
                    "last_seq": 0,
                },
            }
        )
        terminal = dict(initial)
        terminal["counts"] = {"pending": 0, "complete": 1}
        terminal["next_pending_id"] = None
        status = _StatusScript([initial, terminal])
        observed = {}

        def fake_invoke_run(target, **kwargs):
            observed["lease_present_at_launch"] = lease_path.exists()
            observed["watcher"] = kwargs["mailbox_watcher"]
            observed["watcher_enabled_at_launch"] = kwargs[
                "mailbox_watcher"
            ].enabled
            return {"ok": True, "paused": False, "reason": "all-complete"}

        original = autopilot._invoke_run
        autopilot._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {
                    "plan_dir": plan_dir,
                    "state_dir": state_root,
                    "argv": [
                        "asl",
                        "run",
                        plan_dir,
                        "--state-dir",
                        state_root,
                        "--json",
                    ],
                },
                status_invoker=status,
                clock=_counter_clock(),
                heartbeat=lambda *_: None,
            )
        finally:
            autopilot._invoke_run = original

        self.assertTrue(result["ok"], result)
        self.assertTrue(observed["lease_present_at_launch"])
        self.assertTrue(observed["watcher_enabled_at_launch"])
        self.assertFalse(observed["watcher"].enabled)
        self.assertEqual(result["outbox"]["questions"], [])

    def test_relaunch_after_expired_lease_refreshes_before_next_chunk(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-relaunch-lease-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plan_dir = os.path.join(tmp, "plan")
        state_root = os.path.join(tmp, "state")
        os.makedirs(plan_dir)
        mailbox_path = autopilot._mailbox.mailbox_path_for_plan(
            plan_dir, state_root
        )
        lease_path = autopilot._mailbox.lease_path_for_plan(plan_dir, state_root)
        clock = _ManualClock()
        wall_start = datetime(2026, 7, 24, tzinfo=timezone.utc)
        lease_writes = []
        launch_times = []
        activation_at_launch = []

        initial = _status_payload(1)
        initial.update(
            {
                "plan_dir": plan_dir,
                "state_dir": str(lease_path.parent),
                "mailbox": {
                    "path": str(mailbox_path),
                    "lease_path": str(lease_path),
                    "last_seq": 0,
                },
            }
        )
        terminal = dict(initial)
        terminal["counts"] = {"pending": 0, "complete": 1}
        terminal["next_pending_id"] = None
        status = _StatusScript([initial, initial, terminal])

        def write_lease(plan, supervisor_id, *, state_dir=None):
            lease_writes.append(clock())
            updated_at = wall_start + timedelta(seconds=clock())
            return autopilot._mailbox.write_lease(
                plan,
                supervisor_id,
                state_dir=state_dir,
                updated_at=updated_at.isoformat(),
            )

        def watcher_factory(status_payload, **kwargs):
            return autopilot._MailboxWatcher.from_status(
                status_payload,
                **kwargs,
                write_lease=write_lease,
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )

        def run_invoker(_parsed, chunk_index):
            launch_times.append(clock())
            now = wall_start + timedelta(seconds=clock())
            activation_at_launch.append(
                autopilot._mailbox.read_lease(
                    plan_dir,
                    state_dir=state_root,
                    now=now,
                ).fresh
            )
            if chunk_index == 1:
                clock.advance(
                    autopilot._mailbox.MAILBOX_LEASE_FRESH_SECONDS + 1.0
                )
                return {
                    "ok": False,
                    "paused": True,
                    "reason": "transport-failed",
                    "pause_reason": "transport-failed",
                }
            return {"ok": True, "paused": False, "reason": "all-complete"}

        result = autopilot.supervise_run(
            {
                "plan_dir": plan_dir,
                "state_dir": state_root,
                "argv": [
                    "asl",
                    "run",
                    plan_dir,
                    "--state-dir",
                    state_root,
                    "--json",
                ],
            },
            status_invoker=status,
            run_invoker=run_invoker,
            clock=clock,
            sleep=clock.advance,
            heartbeat=lambda *_: None,
            mailbox_watcher_factory=watcher_factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertFalse(mailbox_path.exists())
        self.assertEqual(activation_at_launch, [True, True])
        self.assertEqual(lease_writes, launch_times)
        self.assertGreater(
            launch_times[1] - launch_times[0],
            autopilot._mailbox.MAILBOX_LEASE_FRESH_SECONDS,
        )

    def test_no_mailbox_skips_watcher_and_lease_write(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-no-mailbox-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plan_dir = os.path.join(tmp, "plan")
        state_root = os.path.join(tmp, "state")
        os.makedirs(plan_dir)
        mailbox_path = autopilot._mailbox.mailbox_path_for_plan(
            plan_dir, state_root
        )
        lease_path = autopilot._mailbox.lease_path_for_plan(plan_dir, state_root)
        initial = _status_payload(1)
        initial.update(
            {
                "plan_dir": plan_dir,
                "state_dir": str(lease_path.parent),
                "mailbox": {
                    "path": str(mailbox_path),
                    "lease_path": str(lease_path),
                    "last_seq": 0,
                },
            }
        )
        terminal = dict(initial)
        terminal["counts"] = {"pending": 0, "complete": 1}
        terminal["next_pending_id"] = None
        status = _StatusScript([initial, terminal])
        observed = {}

        def fake_invoke_run(target, **kwargs):
            observed["watcher"] = kwargs["mailbox_watcher"]
            observed["lease_present_at_launch"] = lease_path.exists()
            return {"ok": True, "paused": False, "reason": "all-complete"}

        original = autopilot._invoke_run
        autopilot._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {
                    "plan_dir": plan_dir,
                    "state_dir": state_root,
                    "argv": [
                        "asl",
                        "run",
                        plan_dir,
                        "--state-dir",
                        state_root,
                        "--no-mailbox",
                        "--json",
                    ],
                    "no_mailbox": True,
                },
                status_invoker=status,
                clock=_counter_clock(),
                heartbeat=lambda *_: None,
            )
        finally:
            autopilot._invoke_run = original

        self.assertTrue(result["ok"], result)
        self.assertIsNone(observed["watcher"])
        self.assertFalse(observed["lease_present_at_launch"])
        self.assertNotIn("outbox", result)


class SuperviseRunTransportBackoffTest(unittest.TestCase):
    """Transient transport failures back off on the bounded 5s/30s schedule."""

    def _supervise(self, status_invoker, run_invoker, sleeps, **kwargs):
        return autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status_invoker,
            run_invoker=run_invoker,
            clock=_counter_clock(),
            sleep=lambda seconds: sleeps.append(seconds),
            heartbeat=lambda *_: None,
            **kwargs,
        )

    def _transport_pause(self, *, retry_at=None):
        return {
            "ok": False,
            "paused": True,
            "reason": "transport-failed",
            "pause_reason": "transport-failed",
            "pause_message": "connection reset by peer",
            "pause_details": {
                "failure_classification": {
                    "reason": "transport-failed",
                    "retryable": True,
                    "fingerprint": "transport-failed:connection-reset",
                    "retry_at": retry_at,
                }
            },
        }

    def test_transport_retry_clears_after_first_backoff(self) -> None:
        # First chunk hits a transport pause; after a single 5s backoff the
        # relaunch completes the plan.
        status = _StatusScript([
            _status_payload(1),
            _status_payload(1),
            _status_payload(0),
        ])
        run = _RunScript([
            self._transport_pause(),
            {"ok": True, "paused": False, "reason": "all-complete"},
        ])
        sleeps = []
        result = self._supervise(status, run, sleeps)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1, 2])
        # Exactly one 5s backoff slept before the successful relaunch.
        self.assertEqual(sleeps, [5.0])

    def test_transport_retries_follow_five_then_thirty(self) -> None:
        # The pause never clears: two retries at 5s and 30s, launching three
        # times total, then the exhausted pause is surfaced with recorded delay
        # and classification.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([self._transport_pause(retry_at="2026-07-10T06:00:00Z")])
        sleeps = []
        result = self._supervise(status, run, sleeps, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(sleeps, [5.0, 30.0])
        retries = result["retries"]
        self.assertEqual(retries["reason"], "transport-failed")
        self.assertEqual(retries["count"], 2)
        self.assertTrue(retries["exhausted"])
        self.assertEqual(retries["delayed_seconds"], 35.0)
        self.assertEqual(retries["classification"]["retry_at"], "2026-07-10T06:00:00Z")

    def test_verify_failed_retry_does_not_back_off(self) -> None:
        # Verification repair retries relaunch immediately (Session 10 revisits
        # them); no backoff sleep is scheduled.
        status = _StatusScript([
            _status_payload(1),
            _status_payload(1),
            _status_payload(0),
        ])
        run = _RunScript([
            {
                "ok": False,
                "paused": True,
                "reason": "verify-failed",
                "pause_reason": "verify-failed",
            },
            {"ok": True, "paused": False, "reason": "all-complete"},
        ])
        sleeps = []
        result = self._supervise(status, run, sleeps)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(sleeps, [])

    def test_retry_accounting_survives_interleaved_attach(self) -> None:
        # A transport retry, an attach poll (someone else briefly holds the
        # lock), then the same pause again: the retry budget keeps counting from
        # where it left off rather than resetting after the attach.
        status = _StatusScript([
            _status_payload(1),                        # chunk 1 -> transport pause
            _status_payload(1, lock=_active_lock()),   # attach wait
            _status_payload(1),                        # chunk 2 -> transport pause
            _status_payload(1),                        # chunk 3 -> transport pause
        ])
        run = _RunScript([self._transport_pause()])
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status,
            run_invoker=run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            max_retries=2,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        # Three launches total; the attach poll did not reset the budget.
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(result["retries"]["count"], 2)
        self.assertTrue(result["retries"]["exhausted"])
        self.assertEqual(result["retries"]["delayed_seconds"], 35.0)


class SuperviseRunUserRequiredHarnessTest(unittest.TestCase):
    """Quota/auth/invalid-model pauses surface as user_required, launch once."""

    def _supervise(self, run_payload, *, sleeps=None, **kwargs):
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([run_payload])
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status,
            run_invoker=run,
            clock=_counter_clock(),
            sleep=(sleeps.append if sleeps is not None else (lambda *_: None)),
            heartbeat=lambda *_: None,
            **kwargs,
        )
        return result, run

    def _harness_pause(self, reason, *, retry_at=None):
        return {
            "ok": False,
            "paused": True,
            "reason": reason,
            "pause_reason": reason,
            "pause_message": f"session paused: {reason}",
            "pause_details": {
                "failure_classification": {
                    "reason": reason,
                    "retryable": False,
                    "retry_at": retry_at,
                }
            },
        }

    def test_quota_exhaustion_is_user_required_and_launches_once(self) -> None:
        sleeps = []
        result, run = self._supervise(
            self._harness_pause("quota-exhausted", retry_at="2026-07-10T06:00:00Z"),
            sleeps=sleeps,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "user_required")
        # Launched exactly once, never retried, never backed off.
        self.assertEqual(run.chunks, [1])
        self.assertEqual(sleeps, [])
        retries = result["retries"]
        self.assertEqual(retries["reason"], "quota-exhausted")
        self.assertFalse(retries["exhausted"])
        self.assertEqual(retries["delayed_seconds"], 0.0)
        # Reset information travels with the result for the operator.
        self.assertEqual(retries["classification"]["retry_at"], "2026-07-10T06:00:00Z")

    def test_auth_and_invalid_model_are_user_required(self) -> None:
        for reason in ("auth-failed", "invalid-model"):
            result, run = self._supervise(self._harness_pause(reason))
            self.assertEqual(result["reason"], "user_required", reason)
            self.assertEqual(run.chunks, [1], reason)

    def test_quota_pause_without_flag_carries_no_quota_wait_block(self) -> None:
        # Default-off: the surfaced payload is exactly what it was before the
        # opt-in existed.
        result, run = self._supervise(
            self._harness_pause("quota-exhausted", retry_at="3600")
        )
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1])
        self.assertNotIn("quota_wait", result)


QUOTA_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


class _WallClock:
    """Injectable wall clock advanced by its own `sleep`, with optional suspend.

    The quota wait recomputes remaining time from wall clock rather than
    accumulating slept durations, so a clock that jumps forward mid-wait is the
    faithful model of a machine suspend.
    """

    def __init__(self, start=QUOTA_NOW, *, suspend_after=None, suspend_seconds=0.0):
        self.now = start
        self.sleeps: list = []
        self._suspend_after = suspend_after
        self._suspend_seconds = suspend_seconds

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, duration) -> None:
        self.sleeps.append(duration)
        self.now += timedelta(seconds=duration)
        if self._suspend_after is not None and len(self.sleeps) == self._suspend_after:
            self.now += timedelta(seconds=self._suspend_seconds)


class ParseQuotaResetAtTest(unittest.TestCase):
    """Every `retry_at` shape `runner._parse_retry_at` can hand the supervisor."""

    def _parse(self, value, *, now=QUOTA_NOW):
        return autopilot._parse_quota_reset_at(value, now=now)

    def test_bare_digits_are_seconds_from_now(self) -> None:
        self.assertEqual(self._parse("3600"), QUOTA_NOW + timedelta(hours=1))
        self.assertEqual(self._parse(" 120 "), QUOTA_NOW + timedelta(minutes=2))

    def test_iso_timestamp_tolerates_trailing_z(self) -> None:
        expected = datetime(2026, 7, 31, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(self._parse("2026-07-31T06:00:00Z"), expected)
        self.assertEqual(self._parse("2026-07-31T06:00:00+00:00"), expected)

    def test_naive_iso_timestamp_is_read_as_local_time(self) -> None:
        parsed = self._parse("2026-07-31T06:00:00")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNotNone(parsed.tzinfo)
        # Local time, so the instant depends on the host zone; the wall-clock
        # reading is what must survive.
        self.assertEqual(parsed.replace(tzinfo=None), datetime(2026, 7, 31, 6, 0))

    def test_relative_hours_and_minutes(self) -> None:
        self.assertEqual(self._parse("3 hours"), QUOTA_NOW + timedelta(hours=3))
        self.assertEqual(self._parse("1 hour"), QUOTA_NOW + timedelta(hours=1))
        self.assertEqual(self._parse("45 minutes"), QUOTA_NOW + timedelta(minutes=45))
        self.assertEqual(self._parse("1 minute"), QUOTA_NOW + timedelta(minutes=1))

    def test_time_of_day_with_zone_resolves_to_next_occurrence(self) -> None:
        # 12:00Z is 08:00 in New York, so 10:30pm is later the same day.
        parsed = self._parse("10:30pm (America/New_York)")
        self.assertEqual(parsed, datetime(2026, 7, 31, 2, 30, tzinfo=timezone.utc))
        # A time already past today rolls to tomorrow.
        parsed = self._parse("6:00am (America/New_York)")
        self.assertEqual(parsed, datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc))

    def test_time_of_day_without_zone_uses_local_wall_clock(self) -> None:
        local_now = QUOTA_NOW.astimezone()
        target_hour = (local_now.hour + 2) % 24
        parsed = self._parse(f"{target_hour:02d}:15")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.astimezone().hour, target_hour)
        self.assertEqual(parsed.astimezone().minute, 15)
        self.assertGreater(parsed, QUOTA_NOW)

    def test_unparseable_hints_return_none(self) -> None:
        for value in (
            None,
            "",
            "   ",
            "soon",
            "when the quota resets",
            "99:99",
            "13:00pm (America/New_York)",
            "2026-13-45T99:00:00Z",
            "10:30pm (Not/AZone)",
        ):
            self.assertIsNone(self._parse(value), value)

    def test_out_of_range_hints_decline_instead_of_raising(self) -> None:
        # `runner._parse_retry_at` bounds the hint to 60 characters and not at
        # all in magnitude, so syntactically accepted provider text can name an
        # instant off the end of the calendar. Both overflow shapes appear: a
        # span no `timedelta` can hold, and one no `datetime` can reach.
        for value in (
            "9" * 60,
            "9999999999999",
            "999999999999999 hours",
            "99999999999 minutes",
            # A naive timestamp is read as local time, and converting the last
            # day of year 9999 overflows in every zone.
            "9999-12-31T23:59:59",
        ):
            self.assertIsNone(self._parse(value), value)


class WaitForQuotaResetTest(unittest.TestCase):
    """The wait sleeps in bounded slices measured against the wall clock."""

    def _wait(self, deadline, clock, **kwargs):
        beats: list = []
        slept = autopilot._wait_for_quota_reset(
            deadline,
            now_fn=clock,
            sleep=clock.sleep,
            heartbeat=beats.append,
            **kwargs,
        )
        return slept, beats

    def test_sleeps_in_slices_summing_to_the_remaining_time(self) -> None:
        clock = _WallClock()
        slept, beats = self._wait(QUOTA_NOW + timedelta(seconds=95), clock)
        self.assertEqual(clock.sleeps, [30.0, 30.0, 30.0, 5.0])
        self.assertEqual(slept, 95.0)
        self.assertEqual(clock.now, QUOTA_NOW + timedelta(seconds=95))
        # Every slice heartbeats the remaining time.
        self.assertEqual(len(beats), 4)
        self.assertIn("95s remaining", beats[0])
        self.assertIn("5s remaining", beats[-1])

    def test_past_deadline_returns_without_sleeping(self) -> None:
        clock = _WallClock()
        slept, beats = self._wait(QUOTA_NOW - timedelta(seconds=1), clock)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(slept, 0.0)
        self.assertEqual(beats, [])

    def test_suspend_past_the_deadline_returns_after_one_slice(self) -> None:
        # The machine sleeps through the reset: the next slice reads a past
        # deadline off the wall clock and returns instead of serving out the
        # remaining hour.
        clock = _WallClock(suspend_after=1, suspend_seconds=7200.0)
        slept, _ = self._wait(QUOTA_NOW + timedelta(hours=1), clock)
        self.assertEqual(clock.sleeps, [30.0])
        self.assertEqual(slept, 30.0)

    def test_slice_length_is_configurable(self) -> None:
        clock = _WallClock()
        self._wait(QUOTA_NOW + timedelta(seconds=25), clock, slice_seconds=10.0)
        self.assertEqual(clock.sleeps, [10.0, 10.0, 5.0])


class SuperviseRunQuotaWaitTest(unittest.TestCase):
    """`--wait-quota-reset` sleeps a quota pause out and relaunches."""

    def _quota_pause(self, *, retry_at="3600", reason="quota-exhausted"):
        return {
            "ok": False,
            "paused": True,
            "reason": reason,
            "pause_reason": reason,
            "pause_message": f"session paused: {reason}",
            "pause_details": {
                "failure_classification": {
                    "reason": reason,
                    "retryable": False,
                    "retry_at": retry_at,
                }
            },
        }

    def _supervise(self, statuses, payloads, *, clock=None, beats=None, **kwargs):
        wall = clock or _WallClock()
        run = _RunScript(payloads)
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=_StatusScript(statuses),
            run_invoker=run,
            clock=_counter_clock(),
            sleep=wall.sleep,
            heartbeat=(beats.append if beats is not None else (lambda *_: None)),
            wait_quota_reset=True,
            wall_clock=wall,
            **kwargs,
        )
        return result, run, wall

    def test_waits_reset_plus_buffer_then_relaunches_and_completes(self) -> None:
        beats: list = []
        result, run, wall = self._supervise(
            [_status_payload(1), _status_payload(1), _status_payload(0)],
            [self._quota_pause(retry_at="3600"), {"ok": True, "reason": "complete"}],
            beats=beats,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # Two chunks: the quota pause slept, then the same plan relaunched.
        self.assertEqual(run.chunks, [1, 2])
        # Sleeps total the parsed reset plus the buffer, in 30s slices.
        expected = 3600.0 + autopilot.DEFAULT_QUOTA_RESET_BUFFER_SECONDS
        self.assertEqual(sum(wall.sleeps), expected)
        self.assertEqual(set(wall.sleeps[:-1]), {autopilot.QUOTA_WAIT_SLICE_SECONDS})
        self.assertEqual(wall.now, QUOTA_NOW + timedelta(seconds=expected))
        joined = "\n".join(beats)
        self.assertIn("quota exhausted; waiting 3900s for reset (resume 1/", joined)
        self.assertIn("quota reset wait: 3900s remaining", joined)

    def test_each_retry_at_form_relaunches(self) -> None:
        for retry_at, wait_seconds in (
            ("3600", 3600.0),
            ("2026-07-30T13:00:00Z", 3600.0),
            ("2 hours", 7200.0),
            ("30 minutes", 1800.0),
            ("9:00am (America/New_York)", 3600.0),
        ):
            with self.subTest(retry_at=retry_at):
                result, run, wall = self._supervise(
                    [_status_payload(1), _status_payload(1), _status_payload(0)],
                    [
                        self._quota_pause(retry_at=retry_at),
                        {"ok": True, "reason": "complete"},
                    ],
                )
                self.assertEqual(result["reason"], "complete", result)
                self.assertEqual(run.chunks, [1, 2])
                self.assertEqual(
                    sum(wall.sleeps),
                    wait_seconds + autopilot.DEFAULT_QUOTA_RESET_BUFFER_SECONDS,
                )

    def test_reset_already_past_waits_only_the_buffer(self) -> None:
        result, run, wall = self._supervise(
            [_status_payload(1), _status_payload(1), _status_payload(0)],
            [
                self._quota_pause(retry_at="2026-07-30T11:00:00Z"),
                {"ok": True, "reason": "complete"},
            ],
        )
        self.assertEqual(result["reason"], "complete", result)
        self.assertEqual(run.chunks, [1, 2])
        self.assertEqual(
            sum(wall.sleeps), autopilot.DEFAULT_QUOTA_RESET_BUFFER_SECONDS
        )

    def test_suspend_past_the_reset_relaunches_promptly(self) -> None:
        clock = _WallClock(suspend_after=1, suspend_seconds=86400.0)
        result, run, wall = self._supervise(
            [_status_payload(1), _status_payload(1), _status_payload(0)],
            [self._quota_pause(retry_at="3600"), {"ok": True, "reason": "complete"}],
            clock=clock,
        )
        self.assertEqual(result["reason"], "complete", result)
        self.assertEqual(run.chunks, [1, 2])
        self.assertEqual(wall.sleeps, [autopilot.QUOTA_WAIT_SLICE_SECONDS])

    def test_missing_and_unparseable_reset_hints_skip_the_wait(self) -> None:
        for retry_at, skipped in (
            (None, "no-reset-hint"),
            ("some time later today", "unparseable-reset-hint"),
        ):
            with self.subTest(retry_at=retry_at):
                result, run, wall = self._supervise(
                    [_status_payload(1)], [self._quota_pause(retry_at=retry_at)]
                )
                self.assertEqual(result["reason"], "user_required")
                self.assertEqual(run.chunks, [1])
                self.assertEqual(wall.sleeps, [])
                self.assertEqual(result["quota_wait"]["skipped"], skipped)

    def test_out_of_range_reset_hints_fail_closed(self) -> None:
        # Extreme but syntactically accepted hints stop `user_required` with a
        # named guard rather than raising out of `supervise_run`. A hint no
        # `timedelta` or `datetime` can represent is unparseable; one that
        # resolves to a real instant eight millennia out is declined by the
        # ceiling, which reports the span it refused.
        for retry_at, skipped in (
            ("9" * 60, "unparseable-reset-hint"),
            ("999999999999999 hours", "unparseable-reset-hint"),
            ("9999-12-31T23:59:59Z", "wait-exceeds-ceiling"),
        ):
            with self.subTest(retry_at=retry_at):
                result, run, wall = self._supervise(
                    [_status_payload(1)], [self._quota_pause(retry_at=retry_at)]
                )
                self.assertEqual(result["reason"], "user_required")
                self.assertEqual(run.chunks, [1])
                self.assertEqual(wall.sleeps, [])
                self.assertEqual(result["quota_wait"]["skipped"], skipped)

    def test_wait_beyond_the_ceiling_is_skipped(self) -> None:
        result, run, wall = self._supervise(
            [_status_payload(1)],
            [self._quota_pause(retry_at="2027-07-30T12:00:00Z")],
        )
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1])
        self.assertEqual(wall.sleeps, [])
        quota_wait = result["quota_wait"]
        self.assertEqual(quota_wait["skipped"], "wait-exceeds-ceiling")
        self.assertGreater(
            quota_wait["wait_seconds"], autopilot.DEFAULT_QUOTA_WAIT_CEILING_SECONDS
        )
        self.assertEqual(
            quota_wait["ceiling_seconds"],
            autopilot.DEFAULT_QUOTA_WAIT_CEILING_SECONDS,
        )

    def test_resume_budget_converges_a_permanently_exhausted_provider(self) -> None:
        # Every relaunch re-fails with quota, so each attempt spends a resume
        # and the loop stops at the cap instead of sleeping forever.
        result, run, wall = self._supervise(
            [_status_payload(1)],
            [self._quota_pause(retry_at="600")],
            max_quota_resumes=2,
        )
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(sum(wall.sleeps), 2 * (600.0 + 300.0))
        quota_wait = result["quota_wait"]
        self.assertEqual(quota_wait["skipped"], "max-resumes-spent")
        self.assertEqual(quota_wait["resumes"], 2)
        self.assertEqual(quota_wait["max_resumes"], 2)

    def test_chunk_budget_bounds_the_wait_independently(self) -> None:
        result, run, _ = self._supervise(
            [_status_payload(1)],
            [self._quota_pause(retry_at="600")],
            max_chunks=1,
            max_quota_resumes=8,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "capped")
        self.assertEqual(run.chunks, [1])

    def test_auth_and_invalid_model_never_wait(self) -> None:
        for reason in ("auth-failed", "invalid-model"):
            with self.subTest(reason=reason):
                result, run, wall = self._supervise(
                    [_status_payload(1)],
                    [self._quota_pause(retry_at="3600", reason=reason)],
                )
                self.assertEqual(result["reason"], "user_required")
                self.assertEqual(run.chunks, [1])
                self.assertEqual(wall.sleeps, [])
                self.assertEqual(
                    result["quota_wait"]["skipped"], "reason-not-quota"
                )

    def test_relaunch_after_the_wait_carries_no_operator_reply(self) -> None:
        # Reply injection is chunk-1 only, so the chunk resuming after a quota
        # wait must not re-deliver the operator's reply. Exercised through the
        # default run invoker, which is where that rule lives.
        replies: list = []
        payloads = iter(
            [
                self._quota_pause(retry_at="600"),
                {"ok": True, "reason": "complete"},
            ]
        )

        def fake_invoke_run(target, **kwargs):
            replies.append(kwargs.get("reply_text"))
            return next(payloads)

        wall = _WallClock()
        original = autopilot._invoke_run
        autopilot._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {
                    "plan_dir": "/tmp/plan",
                    "argv": ["asl", "run", "/tmp/plan", "--json"],
                    "no_mailbox": True,
                },
                status_invoker=_StatusScript(
                    [_status_payload(1), _status_payload(1), _status_payload(0)]
                ),
                reply_text="use the cached parser",
                clock=_counter_clock(),
                sleep=wall.sleep,
                heartbeat=lambda *_: None,
                wait_quota_reset=True,
                wall_clock=wall,
            )
        finally:
            autopilot._invoke_run = original
        self.assertEqual(result["reason"], "complete", result)
        self.assertEqual(replies, ["use the cached parser", None])


class SuperviseRunAttachHeartbeatTest(unittest.TestCase):
    """The attach-wait heartbeat surfaces live session/idle detail + stall WARN."""

    def _supervise(self, status_invoker, **kwargs):
        beats: list = []
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status_invoker,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=beats.append,
            **kwargs,
        )
        return result, beats

    def test_attach_heartbeat_carries_session_and_idle(self) -> None:
        status = _StatusScript([_attach_status(34), _status_payload(0)])
        result, beats = self._supervise(status)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        joined = "\n".join(beats)
        self.assertIn("attached to active run", joined)
        self.assertIn("session tidy-db-writes", joined)
        self.assertIn("(3/8)", joined)
        self.assertIn("idle 34s", joined)
        self.assertNotIn("WARN", joined)

    def test_attach_heartbeat_warns_on_high_idle(self) -> None:
        status = _StatusScript([_attach_status(96), _status_payload(0)])
        result, beats = self._supervise(status, stall_threshold=90.0)
        self.assertTrue(result["ok"], result)
        joined = "\n".join(beats)
        self.assertIn("WARN possibly stalled", joined)
        self.assertIn("idle 96s (no output)", joined)
        # The pending/elapsed context is preserved alongside the warning.
        self.assertIn("pending 1", joined)

    def test_waiting_for_reply_is_healthy_and_reports_pending_outbox(self) -> None:
        # Older ``asl status`` payloads do not carry
        # ``waiting_for_reply_status``; retain their deadline-only behavior.
        waiting = _attach_waiting_status(pending_outbox=1)
        snapshot = autopilot._status_snapshot(waiting)
        self.assertEqual(snapshot["state"], "waiting_for_reply")
        self.assertEqual(snapshot["pending_outbox"], 1)
        self.assertNotIn("waiting_for_reply_status", snapshot)

        status = _StatusScript([waiting, _status_payload(0)])
        result, beats = self._supervise(status, stall_threshold=0.0)
        self.assertTrue(result["ok"], result)
        joined = "\n".join(beats)
        self.assertIn("waiting for reply for session 12", joined)
        self.assertIn("pending outbox 1", joined)
        self.assertNotIn("WARN possibly stalled", joined)

    def test_expired_wait_status_overrides_future_deadline_and_warns(self) -> None:
        waiting = _attach_waiting_status()
        waiting["runner_state"]["waiting_for_reply_status"] = "expired"
        waiting["waiting_for_reply_status"] = "expired"

        snapshot = autopilot._status_snapshot(waiting)
        self.assertEqual(snapshot["waiting_for_reply_status"], "expired")
        is_stall, detail = autopilot._heartbeat_detail(
            snapshot,
            90.0,
            events_stale=False,
            now=datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(is_stall)
        self.assertIn("wait status expired", detail)
        heartbeat = autopilot._format_heartbeat(
            1,
            10.0,
            snapshot,
            90.0,
            events_stale=False,
            now=datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc),
        )
        self.assertIn("WARN possibly stalled", heartbeat)

    def test_terminal_pause_takes_precedence_over_stale_wait_record(self) -> None:
        paused = _attach_waiting_status()
        paused["runner_state"]["paused"] = True
        paused["runner_state"]["reason"] = "needs-agent-reply"
        snapshot = autopilot._status_snapshot(paused)
        self.assertEqual(snapshot["state"], "paused")
        self.assertEqual(snapshot["reason"], "needs-agent-reply")

    def test_wait_fallback_keeps_the_existing_terminal_pause_reason(self) -> None:
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "blocked",
                    "pause_reason": "blocked",
                    "pause_message": "reply wait expired",
                }
            ]
        )
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=_StatusScript(
                [_attach_waiting_status(), _status_payload(1)]
            ),
            run_invoker=run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(result["run"]["pause_reason"], "blocked")
        self.assertNotEqual(result["reason"], "needs_supervisor_answer")


def _sequence_clock(values):
    """Clock returning each value in turn, repeating the final value."""
    seq = list(values)

    def clock():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return clock


class AttachEventsWaitTest(unittest.TestCase):
    """`_attach_events_wait` watches events.jsonl by os.stat between probes."""

    def test_no_events_path_degrades_to_single_poll(self) -> None:
        sleeps: list = []
        autopilot._attach_events_wait(
            None,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            clock=lambda: 0.0,
            sleep=sleeps.append,
            events_stat=lambda _p: None,
        )
        # No file to watch -> exactly one legacy poll_interval sleep, no clock use.
        self.assertEqual(sleeps, [5.0])

    def test_returns_early_when_events_file_grows(self) -> None:
        sizes = iter([100, 140])
        sleeps: list = []
        autopilot._attach_events_wait(
            "events.jsonl",
            poll_interval=1.0,
            heartbeat_interval=30.0,
            clock=lambda: 0.0,
            sleep=sleeps.append,
            events_stat=lambda _p: next(sizes),
        )
        # Growth (140 != baseline 100) breaks the wait after a single slice.
        self.assertEqual(sleeps, [1.0])

    def test_returns_when_heartbeat_interval_elapses(self) -> None:
        clock = _sequence_clock([0.0, 31.0])
        sleeps: list = []
        autopilot._attach_events_wait(
            "events.jsonl",
            poll_interval=1.0,
            heartbeat_interval=30.0,
            clock=clock,
            sleep=sleeps.append,
            events_stat=lambda _p: 100,
        )
        # Size never changes; the wait ends once the heartbeat interval elapses.
        self.assertEqual(sleeps, [1.0])

    def test_quiet_events_file_still_services_mailbox_deadlines(self) -> None:
        clock = _ManualClock()
        polls = []

        class Watcher:
            def __init__(self):
                self.next_at = 5.0

            def next_deadline(self):
                return self.next_at

            def poll(self, *, now=None):
                polls.append(now)
                self.next_at = now + 5.0
                return ()

        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)

        autopilot._attach_events_wait(
            "events.jsonl",
            poll_interval=10.0,
            heartbeat_interval=30.0,
            clock=clock,
            sleep=advance,
            events_stat=lambda _p: 100,
            mailbox_watcher=Watcher(),
            heartbeat=lambda *_: None,
        )

        self.assertEqual(sleeps, [5.0] * 6)
        self.assertEqual(polls, [5.0, 10.0, 15.0, 20.0, 25.0, 30.0])


class SuperviseRunAttachEventsStatTest(unittest.TestCase):
    """Attach mode consults the events_stat seam on the payload's events_path."""

    def test_attach_wait_stats_the_events_path(self) -> None:
        attach = _attach_status(20)
        attach["events_path"] = "/tmp/plan/events.jsonl"
        status = _StatusScript([attach, attach, _status_payload(0)])
        stat_paths: list = []

        def events_stat(path):
            stat_paths.append(path)
            return 100

        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            events_stat=events_stat,
            heartbeat_interval=3.0,
            poll_interval=1.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # The attach waits watched the advertised events path by os.stat.
        self.assertTrue(stat_paths)
        self.assertTrue(all(p == "/tmp/plan/events.jsonl" for p in stat_paths))


class FakeTimeoutPopen:
    """Fake ``subprocess.Popen`` whose ``communicate`` times out N times.

    Constructing the instance yields a callable that stands in for the ``popen``
    factory: calling it (as ``_invoke_run`` does) records the argv and returns
    ``self`` so the same object services the ``communicate()`` wait loop. The
    first ``timeout_count`` ``communicate()`` calls raise
    ``subprocess.TimeoutExpired`` (driving one heartbeat tick each), then the
    final call returns the configured ``(stdout, stderr)``. Delivered ``input``
    values are recorded so tests can assert the reply is streamed once. No real
    subprocess is spawned.
    """

    def __init__(self, *, timeout_count, stdout, stderr="", returncode=0):
        self._remaining_timeouts = timeout_count
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.inputs = []
        self.communicate_calls = 0
        self.argv = None
        self.kwargs = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return self

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        self.inputs.append(input)
        if self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="asl run", timeout=timeout)
        return self._stdout, self._stderr


class _ManualClock:
    """Monotonic test clock advanced explicitly by a fake subprocess."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _DeadlinePopen:
    """Timeout once after advancing the clock, then return final JSON."""

    def __init__(self, clock, stdout):
        self.clock = clock
        self.stdout = stdout
        self.returncode = 0
        self.timeouts = []
        self.inputs = []

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return self

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            self.clock.advance(timeout)
            raise subprocess.TimeoutExpired(cmd="asl run", timeout=timeout)
        return self.stdout, ""


class MailboxWatcherTest(unittest.TestCase):
    """Self-launched supervision uses one status-derived durable cursor."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-mailbox-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan_dir = os.path.join(self.tmp, "plan")
        self.state_root = os.path.join(self.tmp, "state")
        self.state_path = os.path.join(self.state_root, "plan-hash")
        os.makedirs(self.plan_dir)
        self.mailbox_path = os.path.join(self.state_path, "mailbox.jsonl")
        self.lease_path = os.path.join(self.state_path, "mailbox-lease.json")

    def _status(self, *, last_seq=1):
        return {
            "ok": True,
            "plan_dir": self.plan_dir,
            "state_dir": self.state_path,
            "mailbox": {
                "path": self.mailbox_path,
                "lease_path": self.lease_path,
                "last_seq": last_seq,
            },
        }

    def _question(self, *, seq=2, text="Need a decision", run_id=None):
        return autopilot._mailbox.MailboxMessage(
            seq=seq,
            id="00000000-0000-0000-0000-000000000011",
            kind=autopilot._mailbox.KIND_QUESTION,
            created_at="2026-07-24T19:00:00+00:00",
            text=text,
            run_id=run_id,
        )

    def test_resolves_paths_renews_lease_and_returns_questions_once(self) -> None:
        writes = []
        reads = []
        question = self._question(text="Full question body\nwith a second line")
        guidance = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=autopilot._mailbox.KIND_GUIDANCE,
            created_at="2026-07-24T19:00:01+00:00",
            text="guidance advances the physical cursor",
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question, guidance),
            last_seq=3,
        )

        def write_lease(
            plan_dir, supervisor_id, *, state_dir=None, **projection
        ):
            writes.append(
                (plan_dir, supervisor_id, state_dir, projection)
            )

        def read_mailbox(plan_dir, *, state_dir=None):
            reads.append((plan_dir, state_dir))
            return view

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(),
            clock=lambda: 0.0,
            read_mailbox=read_mailbox,
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        self.assertTrue(watcher.establish(now=0.0))
        self.assertEqual(watcher.next_deadline(), 5.0)
        self.assertEqual(writes[0][0], os.path.realpath(self.plan_dir))
        self.assertEqual(writes[0][2], os.path.realpath(self.state_root))

        self.assertEqual(watcher.poll(now=5.0), (question,))
        self.assertEqual(watcher.poll(now=10.0), ())
        watcher.poll(now=30.0)
        self.assertEqual(len(writes), 4)
        self.assertEqual(len(reads), 3)

        outbox = watcher.outbox_payload()
        self.assertEqual(outbox["since"], 1)
        self.assertEqual(outbox["next_since"], 3)
        self.assertEqual(len(outbox["questions"]), 1)
        self.assertEqual(outbox["questions"][0]["text"], question.text)

    def test_lease_transient_failure_recovers_and_delivers_question(self) -> None:
        writes = []
        reads = []
        question = self._question(seq=1, text="question after lease recovery")
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )
        question_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question,),
            last_seq=1,
        )
        views = iter((empty_view, question_view))

        def write_lease(
            plan_dir, supervisor_id, *, state_dir=None, **projection
        ):
            writes.append(
                (plan_dir, supervisor_id, state_dir, projection)
            )
            if len(writes) == 2:
                raise OSError("transient lease write failure")

        def read_mailbox(plan_dir, *, state_dir=None):
            reads.append((plan_dir, state_dir))
            return next(views)

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=read_mailbox,
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        self.assertTrue(watcher.establish(now=0.0))
        progress = []

        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=30.0,
                heartbeat=progress.append,
            ),
            (),
        )
        self.assertTrue(watcher.enabled)
        self.assertEqual(watcher.next_deadline(), 35.0)
        self.assertIn(
            "MAILBOX WARN lease renewal failed; retry scheduled",
            progress,
        )

        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=60.0,
                heartbeat=progress.append,
            ),
            (question,),
        )
        self.assertTrue(watcher.enabled)
        self.assertIn("MAILBOX lease renewal recovered", progress)
        self.assertTrue(
            any(
                record.startswith(f"QUESTION id={question.id} ")
                for record in progress
            )
        )
        self.assertEqual(len(writes), 3)
        self.assertEqual(len(reads), 2)
        self.assertEqual(
            watcher.outbox_payload()["lease_supervision"],
            {
                "degraded": False,
                "renewal_failures": 1,
                "renewal_recoveries": 1,
            },
        )

    def test_initial_lease_failure_stays_armed_and_recovers(self) -> None:
        writes = []
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )

        def write_lease(
            plan_dir, supervisor_id, *, state_dir=None, **projection
        ):
            writes.append(
                (plan_dir, supervisor_id, state_dir, projection)
            )
            if len(writes) == 1:
                raise autopilot._mailbox.MailboxError(
                    autopilot._mailbox.ERROR_LEASE_WRITE,
                    "transient establishment failure",
                )

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: empty_view,
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        progress = []

        self.assertTrue(watcher.establish(now=0.0))
        self.assertTrue(watcher.enabled)
        self.assertEqual(watcher.next_deadline(), 5.0)
        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=5.0,
                heartbeat=progress.append,
            ),
            (),
        )
        self.assertIn(
            "MAILBOX WARN lease renewal failed; retry scheduled",
            progress,
        )

        self.assertEqual(
            autopilot._poll_mailbox_watcher(
                watcher,
                now=30.0,
                heartbeat=progress.append,
            ),
            (),
        )
        self.assertTrue(watcher.enabled)
        self.assertIn("MAILBOX lease renewal recovered", progress)
        self.assertEqual(len(writes), 3)
        self.assertEqual(
            watcher.outbox_payload()["lease_supervision"],
            {
                "degraded": False,
                "renewal_failures": 1,
                "renewal_recoveries": 1,
            },
        )

    def test_initial_lease_persistent_failure_remains_armed(self) -> None:
        writes = []
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )

        def write_lease(*_args, **_kwargs):
            writes.append(True)
            raise OSError("persistent lease write failure")

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: empty_view,
            write_lease=write_lease,
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        progress = []

        self.assertTrue(watcher.establish(now=0.0))
        for now in (5.0, 30.0, 60.0):
            self.assertEqual(
                autopilot._poll_mailbox_watcher(
                    watcher,
                    now=now,
                    heartbeat=progress.append,
                ),
                (),
            )

        self.assertTrue(watcher.enabled)
        self.assertEqual(len(writes), 4)
        self.assertEqual(
            progress.count(
                "MAILBOX WARN lease renewal failed; retry scheduled"
            ),
            1,
        )
        self.assertEqual(
            watcher.outbox_payload()["lease_supervision"],
            {
                "degraded": True,
                "renewal_failures": 4,
                "renewal_recoveries": 0,
            },
        )

    def test_projects_only_observed_unanswered_questions_for_exact_run(
        self,
    ) -> None:
        writes = []
        question = self._question(
            seq=1, text="private question body", run_id="run-current"
        )
        reply = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=autopilot._mailbox.KIND_REPLY,
            created_at="2026-07-24T19:00:02+00:00",
            text="handled",
            to=question.id,
        )
        guidance = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000013",
            kind=autopilot._mailbox.KIND_GUIDANCE,
            created_at="2026-07-24T19:00:01+00:00",
            text="unrelated guidance",
        )
        views = iter(
            (
                autopilot._mailbox.MailboxView(
                    mailbox_path=autopilot.Path(self.mailbox_path),
                    messages=(question,),
                    last_seq=1,
                ),
                autopilot._mailbox.MailboxView(
                    mailbox_path=autopilot.Path(self.mailbox_path),
                    messages=(question, guidance),
                    last_seq=2,
                ),
                autopilot._mailbox.MailboxView(
                    mailbox_path=autopilot.Path(self.mailbox_path),
                    messages=(question, guidance, reply),
                    last_seq=3,
                ),
            )
        )

        def write_lease(
            plan_dir, supervisor_id, *, state_dir=None, **projection
        ):
            writes.append(projection)

        watcher = autopilot._MailboxWatcher.from_status(
            self._status(last_seq=0),
            clock=lambda: 0.0,
            read_mailbox=lambda *_args, **_kwargs: next(views),
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertIsNotNone(watcher)
        assert watcher is not None
        watcher.set_run_id("run-current")
        watcher.establish(now=0.0)
        watcher.poll(now=5.0)
        watcher.poll(now=10.0)
        watcher.poll(now=15.0)

        self.assertEqual(
            writes,
            [
                {
                    "projection_seq": 0,
                    "run_id": "run-current",
                    "unanswered_questions": 0,
                },
                {
                    "latest_unanswered_question_created_at": (
                        question.created_at
                    ),
                    "latest_unanswered_question_id": question.id,
                    "projection_seq": 1,
                    "run_id": "run-current",
                    "unanswered_questions": 1,
                },
                {
                    "latest_unanswered_question_created_at": (
                        question.created_at
                    ),
                    "latest_unanswered_question_id": question.id,
                    "projection_seq": 1,
                    "run_id": "run-current",
                    "unanswered_questions": 1,
                },
                {
                    "projection_seq": 3,
                    "run_id": "run-current",
                    "unanswered_questions": 0,
                },
            ],
        )
        self.assertNotIn(question.text, repr(writes))

    def test_relaunch_drops_old_projection_and_rebinds_from_current_lock(
        self,
    ) -> None:
        clock = _ManualClock()
        writes: list[dict] = []
        old_question = self._question(
            seq=1, text="old run question", run_id="run-old"
        )
        current_question = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000022",
            kind=autopilot._mailbox.KIND_QUESTION,
            created_at="2026-07-24T19:01:00+00:00",
            text="current run question",
            run_id="run-current",
        )
        old_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(old_question,),
            last_seq=1,
        )
        current_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(old_question, current_question),
            last_seq=2,
        )
        views = iter((old_view, current_view, current_view))

        def write_lease(
            _plan_dir, _supervisor_id, *, state_dir=None, **projection
        ):
            writes.append(projection)

        watcher = autopilot._MailboxWatcher(
            plan_dir=self.plan_dir,
            state_root=self.state_root,
            state_path=self.state_path,
            mailbox_path=self.mailbox_path,
            lease_path=self.lease_path,
            since=0,
            clock=clock,
            read_mailbox=lambda *_args, **_kwargs: next(views),
            write_lease=write_lease,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        watcher.set_run_id("run-old")
        watcher.establish(now=0.0)
        clock.advance(5.0)
        watcher.poll(now=clock())
        self.assertEqual(watcher.last_seq, 1)
        self.assertEqual(writes[-1]["run_id"], "run-old")
        self.assertEqual(
            writes[-1]["latest_unanswered_question_id"],
            old_question.id,
        )

        watcher.prepare_for_launch(now=clock())
        unbound_write = len(writes) - 1
        self.assertEqual(writes[unbound_write], {})
        self.assertIsNone(watcher.run_id)
        self.assertEqual(watcher.last_seq, 1)

        stale_status = _leftover_state_attach_status(
            1,
            run_id="run-old",
        )
        current_status = _attached_run_status(
            1,
            run_id="run-current",
        )
        statuses = iter((stale_status, current_status, current_status))
        status_calls = 0

        def status_probe():
            nonlocal status_calls
            status_calls += 1
            return next(statuses, current_status)

        final = '{"ok": true, "paused": false, "reason": "all-complete"}'
        payload = autopilot._invoke_run(
            {
                "argv": ["asl", "run", self.plan_dir, "--json"],
                "plan_dir": self.plan_dir,
            },
            chunk_index=2,
            clock=clock,
            heartbeat=lambda *_: None,
            popen=_DeadlinePopen(clock, final),
            status_probe=status_probe,
            mailbox_watcher=watcher,
        )

        self.assertEqual(payload["reason"], "all-complete")
        self.assertGreaterEqual(status_calls, 2)
        self.assertEqual(watcher.run_id, "run-current")
        self.assertEqual(watcher.last_seq, 2)
        successor_writes = writes[unbound_write + 1 :]
        self.assertTrue(successor_writes)
        self.assertFalse(
            any(write.get("run_id") == "run-old" for write in successor_writes)
        )
        self.assertTrue(
            any(
                write.get("run_id") == "run-current"
                and write.get("latest_unanswered_question_id")
                == current_question.id
                for write in successor_writes
            )
        )
        self.assertFalse(
            any(
                write.get("latest_unanswered_question_id") == old_question.id
                for write in successor_writes
            )
        )

    def test_invoke_run_polls_at_five_seconds_and_streams_reply_once(self) -> None:
        clock = _ManualClock()
        full_text = 'Line one\n"quoted" ' + ("x" * 500)
        question = self._question(seq=1, text=full_text)
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question,),
            last_seq=1,
        )
        watcher = autopilot._MailboxWatcher(
            plan_dir=self.plan_dir,
            state_root=self.state_root,
            state_path=self.state_path,
            mailbox_path=self.mailbox_path,
            lease_path=self.lease_path,
            since=0,
            clock=clock,
            read_mailbox=lambda *_args, **_kwargs: view,
            write_lease=lambda *_args, **_kwargs: None,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertTrue(watcher.establish(now=0.0))
        final = '{"ok": true, "paused": false, "reason": "all-complete"}'
        popen = _DeadlinePopen(clock, final)
        progress = []

        payload = autopilot._invoke_run(
            {
                "argv": ["asl", "run", self.plan_dir, "--json"],
                "plan_dir": self.plan_dir,
            },
            chunk_index=1,
            reply_text="operator reply",
            clock=clock,
            heartbeat=progress.append,
            popen=popen,
            mailbox_watcher=watcher,
        )

        self.assertEqual(popen.timeouts, [5.0, 5.0])
        self.assertEqual(popen.inputs, ["operator reply", None])
        question_records = [line for line in progress if line.startswith("QUESTION ")]
        self.assertEqual(len(question_records), 1)
        self.assertNotIn("\n", question_records[0])
        preview = question_records[0].split("preview=", 1)[1]
        self.assertLessEqual(len(preview), autopilot.QUESTION_PREVIEW_MAX_CHARS)
        self.assertEqual(payload["outbox"]["questions"][0]["text"], full_text)

    def test_final_mailbox_drain_after_child_exit(self) -> None:
        clock = _ManualClock()
        question = self._question(
            seq=1,
            text="question appended immediately before child exit",
        )
        empty_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(),
            last_seq=0,
        )
        final_view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question,),
            last_seq=1,
        )
        child_exited = False
        reads = []

        def read_mailbox(*_args, **_kwargs):
            reads.append(child_exited)
            return final_view if child_exited else empty_view

        class ExitAppendingPopen:
            returncode = 0

            def __call__(self, argv, **kwargs):
                self.argv = argv
                self.kwargs = kwargs
                return self

            def communicate(self, input=None, timeout=None):
                nonlocal child_exited
                child_exited = True
                return (
                    '{"ok": true, "paused": false, "reason": "all-complete"}',
                    "",
                )

        watcher = autopilot._MailboxWatcher(
            plan_dir=self.plan_dir,
            state_root=self.state_root,
            state_path=self.state_path,
            mailbox_path=self.mailbox_path,
            lease_path=self.lease_path,
            since=0,
            clock=clock,
            read_mailbox=read_mailbox,
            write_lease=lambda *_args, **_kwargs: None,
            supervisor_id="00000000-0000-0000-0000-000000000099",
        )
        self.assertTrue(watcher.establish(now=0.0))
        progress = []

        payload = autopilot._invoke_run(
            {
                "argv": ["asl", "run", self.plan_dir, "--json"],
                "plan_dir": self.plan_dir,
            },
            chunk_index=1,
            clock=clock,
            heartbeat=progress.append,
            popen=ExitAppendingPopen(),
            mailbox_watcher=watcher,
        )

        self.assertEqual(reads, [True])
        self.assertEqual(
            [item["id"] for item in payload["outbox"]["questions"]],
            [question.id],
        )
        question_records = [
            record for record in progress if record.startswith("QUESTION ")
        ]
        self.assertEqual(len(question_records), 1)
        self.assertIn(question.id, question_records[0])


class AttachedMailboxWatcherTest(unittest.TestCase):
    """Attach supervision shares the durable watcher without replaying history."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-attached-mailbox-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_path = os.path.join(self.tmp, "state", "plan-hash")
        self.mailbox_path = os.path.join(self.state_path, "mailbox.jsonl")
        self.lease_path = os.path.join(self.state_path, "mailbox-lease.json")

    def _attach_status(self, *, run_id=ATTACHED_RUN_ID):
        status = _attach_status(10)
        status["runner_state"].update(
            {
                "present": True,
                "error": None,
                "run_id": run_id,
                "run_started_at": RUN_STARTED_AT,
                "run_finished_at": None,
            }
        )
        status.update(
            {
                "state_dir": self.state_path,
                "mailbox": {
                    "path": self.mailbox_path,
                    "lease_path": self.lease_path,
                    "last_seq": 3,
                    "unanswered_questions": 1,
                    "open_questions": 1,
                },
            }
        )
        return status

    def test_attach_delivers_preexisting_unanswered_question_once(self) -> None:
        clock = _ManualClock()
        answered = autopilot._mailbox.MailboxMessage(
            seq=1,
            id="00000000-0000-0000-0000-000000000010",
            kind=autopilot._mailbox.KIND_QUESTION,
            created_at="2026-07-24T18:59:00+00:00",
            text="answered before this supervisor attached",
            run_id=ATTACHED_RUN_ID,
        )
        reply = autopilot._mailbox.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000011",
            kind=autopilot._mailbox.KIND_REPLY,
            created_at="2026-07-24T18:59:01+00:00",
            text="already handled",
            to=answered.id,
        )
        unanswered = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=autopilot._mailbox.KIND_QUESTION,
            created_at="2026-07-24T19:00:00+00:00",
            text="unanswered before this supervisor attached",
            run_id=ATTACHED_RUN_ID,
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(answered, reply, unanswered),
            last_seq=3,
        )
        lease_writes = []
        created = []

        def factory(status, **kwargs):
            watcher = autopilot._MailboxWatcher.from_status(
                status,
                **kwargs,
                read_mailbox=lambda *_args, **_kwargs: view,
                write_lease=lambda *args, **kw: lease_writes.append((args, kw)),
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )
            created.append(watcher)
            return watcher

        attach = self._attach_status()
        status = _StatusScript([attach] * 7 + [_status_payload(0)])
        beats = []
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status,
            run_invoker=_never_run,
            clock=clock,
            sleep=clock.advance,
            heartbeat=beats.append,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            mailbox_watcher_factory=factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(len(created), 1)
        watcher = created[0]
        self.assertIsNotNone(watcher)
        assert watcher is not None
        self.assertFalse(watcher.enabled)
        self.assertGreaterEqual(len(lease_writes), 2)
        question_beats = [beat for beat in beats if beat.startswith("QUESTION ")]
        self.assertEqual(len(question_beats), 1)
        self.assertIn(unanswered.id, question_beats[0])
        self.assertNotIn(answered.id, question_beats[0])
        self.assertEqual(result["outbox"]["since"], 3)
        self.assertEqual(result["outbox"]["next_since"], 3)
        self.assertEqual(
            [question["id"] for question in result["outbox"]["questions"]],
            [unanswered.id],
        )
        self.assertIn("pending outbox 1", "\n".join(beats))

    def test_attach_never_resurfaces_a_question_an_earlier_run_abandoned(
        self,
    ) -> None:
        clock = _ManualClock()
        orphan = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=autopilot._mailbox.KIND_QUESTION,
            created_at="2026-07-24T05:00:00+00:00",
            text="wrangler auth expired",
            run_id="run-that-paused-14-hours-ago",
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(orphan,),
            last_seq=3,
        )
        lease_writes: list[dict] = []
        created = []

        def factory(status, **kwargs):
            watcher = autopilot._MailboxWatcher.from_status(
                status,
                **kwargs,
                read_mailbox=lambda *_args, **_kwargs: view,
                write_lease=(
                    lambda *_args, **projection: lease_writes.append(projection)
                ),
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )
            created.append(watcher)
            return watcher

        attach = self._attach_status()
        status = _StatusScript([attach] * 7 + [_status_payload(0)])
        beats: list[str] = []
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status,
            run_invoker=_never_run,
            clock=clock,
            sleep=clock.advance,
            heartbeat=beats.append,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            mailbox_watcher_factory=factory,
        )

        self.assertTrue(result["ok"], result)
        watcher = created[0]
        assert watcher is not None
        self.assertEqual(watcher.run_id, ATTACHED_RUN_ID)
        self.assertEqual(
            [], [beat for beat in beats if beat.startswith("QUESTION ")]
        )
        self.assertEqual([], result["outbox"]["questions"])
        projections = [
            write for write in lease_writes if "run_id" in write
        ]
        self.assertTrue(projections)
        # The reducer generation stays at the attach cursor: a question the
        # previous run abandoned is never a pending-set delta.
        self.assertTrue(
            all(
                write["unanswered_questions"] == 0
                and write["projection_seq"] == 3
                and "latest_unanswered_question_id" not in write
                for write in projections
            ),
            projections,
        )

    def test_attach_adopts_the_backlog_once_run_id_binds_late(self) -> None:
        clock = _ManualClock()
        question = autopilot._mailbox.MailboxMessage(
            seq=3,
            id="00000000-0000-0000-0000-000000000012",
            kind=autopilot._mailbox.KIND_QUESTION,
            created_at="2026-07-24T19:00:00+00:00",
            text="unanswered before this supervisor attached",
            run_id=ATTACHED_RUN_ID,
        )
        view = autopilot._mailbox.MailboxView(
            mailbox_path=autopilot.Path(self.mailbox_path),
            messages=(question,),
            last_seq=3,
        )
        created = []

        def factory(status, **kwargs):
            watcher = autopilot._MailboxWatcher.from_status(
                status,
                **kwargs,
                read_mailbox=lambda *_args, **_kwargs: view,
                write_lease=lambda *_args, **_kw: None,
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )
            created.append(watcher)
            return watcher

        # The first probe carries no run id, so the backlog must stay armed.
        unbound = self._attach_status()
        unbound["runner_state"].pop("run_id")
        bound = self._attach_status()
        status = _StatusScript(
            [unbound] + [bound] * 6 + [_status_payload(0)]
        )
        beats: list[str] = []
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status,
            run_invoker=_never_run,
            clock=clock,
            sleep=clock.advance,
            heartbeat=beats.append,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            mailbox_watcher_factory=factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(created[0].run_id, ATTACHED_RUN_ID)
        self.assertEqual(
            [item["id"] for item in result["outbox"]["questions"]],
            [question.id],
        )
        self.assertEqual(
            1, len([beat for beat in beats if beat.startswith("QUESTION ")])
        )

    def test_no_mailbox_attach_never_constructs_a_watcher(self) -> None:
        def fail_factory(*_args, **_kwargs):  # pragma: no cover - guard
            raise AssertionError("disabled mailbox must not construct a watcher")

        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": [
                    "asl",
                    "run",
                    "/tmp/plan",
                    "--no-mailbox",
                    "--json",
                ],
                "no_mailbox": True,
            },
            status_invoker=_StatusScript(
                [self._attach_status(), _status_payload(0)]
            ),
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            mailbox_watcher_factory=fail_factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertNotIn("outbox", result)


def _running_status(idle, *, session_id="tidy-db-writes", number=3, total=8):
    """Status payload whose live turn is running with the given idle seconds."""
    return {
        "ok": True,
        "runner_state": {
            "paused": False,
            "active_turn": {
                "session_id": session_id,
                "session_number": number,
                "total_sessions": total,
                "idle_seconds": idle,
                "elapsed_seconds": 120,
            },
        },
    }


class InvokeRunHeartbeatTest(unittest.TestCase):
    """Exercise the per-tick status probe / stall detection in `_invoke_run`."""

    FINAL_JSON = '{"ok": true, "paused": false, "reason": "all-complete"}'

    def _parsed(self) -> dict:
        return {"argv": ["asl", "run", "/tmp/plan", "--json"], "plan_dir": "/tmp/plan"}

    def _run(self, *, popen, status_probe=None, stall_threshold=None):
        beats: list = []
        kwargs = {}
        if stall_threshold is not None:
            kwargs["stall_threshold"] = stall_threshold
        payload = autopilot._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=beats.append,
            heartbeat_interval=0.01,
            popen=popen,
            status_probe=status_probe,
            **kwargs,
        )
        return payload, beats

    def test_enriched_running_line_has_session_label_and_idle(self) -> None:
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(
            popen=popen,
            status_probe=lambda: _running_status(34),
        )
        self.assertEqual(payload["reason"], "all-complete")
        # First beat is the "started" line; the tick beat is the enriched one.
        tick = beats[1]
        self.assertIn("session tidy-db-writes", tick)
        self.assertIn("(3/8)", tick)
        self.assertIn("idle 34s", tick)
        self.assertNotIn("running, elapsed", tick)

    def test_idle_over_threshold_emits_stall_warning(self) -> None:
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(
            popen=popen,
            status_probe=lambda: _running_status(96),
            stall_threshold=90.0,
        )
        tick = beats[1]
        self.assertIn("WARN possibly stalled", tick)
        self.assertIn("idle 96s (no output)", tick)

    def test_running_line_includes_pending_outbox_count(self) -> None:
        status = _running_status(34)
        status["mailbox"] = {"open_questions": 3}
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, status_probe=lambda: status)
        self.assertIn("pending outbox 3", beats[1])

    def test_probe_that_raises_falls_back_to_plain_line(self) -> None:
        def boom():
            raise RuntimeError("status blew up")

        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, status_probe=boom)
        # Run still returns its final payload despite the probe failure.
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("running, elapsed", tick)
        self.assertNotIn("session", tick)

    def test_status_error_payload_falls_back_to_plain_line(self) -> None:
        error = {"ok": False, "stage": autopilot.RUN_STAGE, "reason": "status-error"}
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, status_probe=lambda: error)
        tick = beats[1]
        self.assertIn("running, elapsed", tick)
        self.assertNotIn("session", tick)

    def test_review_phase_snapshot_renders_verifying(self) -> None:
        verifying = {
            "ok": True,
            "runner_state": {
                "paused": False,
                "active_turn": None,
                "active_verification": {"session_id": "1", "phase": "verify"},
            },
        }
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, status_probe=lambda: verifying)
        tick = beats[1]
        self.assertIn("verifying", tick)
        self.assertNotIn("session tidy-db-writes", tick)

    def test_no_probe_reproduces_legacy_plain_line(self) -> None:
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, status_probe=None)
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("running, elapsed", tick)
        self.assertNotIn("session", tick)
        self.assertNotIn("|", tick)


def _batch_follower(batches):
    """A `follow_events` stand-in yielding each batch, then empty batches."""

    def follow(_path):
        for batch in batches:
            yield batch
        while True:
            yield []

    return follow


def _clock_from(values):
    """Deterministic clock returning each value in turn (last value repeats)."""
    seq = list(values)

    def clock():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return clock


def _turn_heartbeat_record(idle, *, session_id="tidy-db-writes", number=3, total=8):
    return {
        "name": "turn_heartbeat",
        "payload": {
            "session_id": session_id,
            "session_number": number,
            "total_sessions": total,
            "idle_seconds": idle,
            "elapsed_seconds": 120,
        },
    }


class EventsTailerTest(unittest.TestCase):
    """`_EventsTailer` folds appended events into per-tick heartbeat snapshots."""

    def test_fresh_events_suppress_stall_despite_high_idle(self) -> None:
        # Construction reads the clock once, then one snapshot per call.
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(96)]]),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["idle_seconds"], 96)
        # A just-seen event keeps the stream fresh even though idle >= threshold.
        self.assertFalse(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", message)
        self.assertIn("idle 96s", message)

    def test_event_silence_past_threshold_triggers_stall(self) -> None:
        clock = _clock_from([0.0, 1.0, 200.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(96)]]),
        )
        # First snapshot folds the heartbeat and marks the stream fresh (t=1).
        tailer.snapshot()
        # Second snapshot at t=200 sees no new events -> silent for 199s.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["idle_seconds"], 96)
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("WARN possibly stalled", message)
        self.assertIn("idle 96s (no output)", message)

    def test_below_threshold_idle_then_silence_triggers_stall(self) -> None:
        # Regression for F4: a heartbeat sampled with idle *below* the threshold
        # must still surface a stall once the event stream goes silent past the
        # threshold. The frozen sub-threshold idle value must not suppress it.
        clock = _clock_from([0.0, 1.0, 200.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(30)]]),
        )
        # First snapshot folds the heartbeat (idle 30, below threshold) at t=1.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["idle_seconds"], 30)
        self.assertFalse(events_stale)
        fresh = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", fresh)
        # Second snapshot at t=200 sees no new events -> silent for 199s. The
        # idle sample is still frozen at 30 (< threshold), yet event silence
        # alone must now drive the stall verdict.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["idle_seconds"], 30)
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("WARN possibly stalled", message)
        self.assertIn("no new events for 90s+ (no output)", message)

    def test_fresh_events_below_threshold_idle_stay_quiet(self) -> None:
        # A fresh stream with a below-threshold idle sample is a healthy running
        # tick — no stall, enriched line carries the idle fragment.
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(30)]]),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertFalse(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", message)
        self.assertIn("idle 30s", message)

    def test_verification_and_pause_records_change_state(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower(
                [
                    [{"name": "verification_heartbeat", "payload": {}}],
                    [{"name": "run_paused", "payload": {"reason": "quota-exhausted"}}],
                ]
            ),
        )
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "verifying")
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "paused")
        self.assertEqual(snapshot["reason"], "quota-exhausted")

    def test_wait_started_is_healthy_even_when_event_stream_goes_silent(self) -> None:
        clock = _clock_from([0.0, 1.0, 200.0])
        wait_started = {
            "name": autopilot._events.MAILBOX_EVENT,
            "payload": {
                "action": autopilot._events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-12",
                "session_id": "12",
                "session_number": 12,
                "turn_index": 2,
                "pending_outbox": 3,
                "base_deadline": "2099-07-24T19:05:00+00:00",
                "hard_deadline": "2099-07-24T19:15:00+00:00",
            },
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[wait_started]]),
        )

        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "waiting_for_reply")
        self.assertEqual(snapshot["pending_outbox"], 3)
        self.assertFalse(events_stale)
        snapshot, events_stale = tailer.snapshot()
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 200.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("waiting for reply for session 12", message)
        self.assertIn("pending outbox 3", message)
        self.assertNotIn("WARN possibly stalled", message)
        self.assertNotEqual(snapshot["state"], "paused")

    def test_wait_cleared_ends_wait_and_preserves_actual_outbox_count(self) -> None:
        wait_started = {
            "name": autopilot._events.MAILBOX_EVENT,
            "payload": {
                "action": autopilot._events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-12",
                "session_id": "12",
                "turn_index": 2,
                "pending_outbox": 3,
                "hard_deadline": "2099-07-24T19:15:00+00:00",
            },
        }
        wait_cleared = {
            "name": autopilot._events.MAILBOX_EVENT,
            "payload": {
                "action": autopilot._events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-12",
                "session_id": "12",
                "turn_index": 2,
                "reason": "reply",
                "pending_outbox": 2,
            },
        }
        turn_started = {
            "name": "turn_started",
            "payload": {
                "session_id": "12",
                "session_number": 12,
                "total_sessions": 14,
            },
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=_clock_from([0.0, 1.0, 2.0, 3.0]),
            follow=_batch_follower(
                [[wait_started], [wait_cleared], [turn_started]]
            ),
        )

        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "waiting_for_reply")
        self.assertEqual(snapshot["pending_outbox"], 3)
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "unknown")
        self.assertEqual(snapshot["pending_outbox"], 2)
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["pending_outbox"], 2)

    def test_elapsed_wait_hard_deadline_triggers_stall_warning(self) -> None:
        snapshot = {
            "state": "waiting_for_reply",
            "session_id": "12",
            "pending_outbox": 2,
            "hard_deadline": "2026-07-24T19:15:00+00:00",
        }

        message = autopilot._format_heartbeat(
            1,
            900.0,
            snapshot,
            90.0,
            events_stale=False,
            now=datetime(2026, 7, 24, 19, 15, tzinfo=timezone.utc),
        )

        self.assertIn("WARN possibly stalled", message)
        self.assertIn("hard deadline elapsed", message)
        self.assertIn("pending outbox 2", message)

    def test_fresh_verification_stream_stays_quiet(self) -> None:
        # Regression for F4: a verifying snapshot from a fresh event stream keeps
        # the normal verifying heartbeat (no stall warning).
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower(
                [[{"name": "verification_heartbeat", "payload": {}}]]
            ),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "verifying")
        self.assertFalse(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", message)
        self.assertIn("verifying", message)

    def test_silent_verification_stream_triggers_stall(self) -> None:
        # Regression for F4: once a verification heartbeat is folded and the
        # stream goes silent past the threshold, the verifying snapshot must
        # surface a stall warning instead of a healthy verifying heartbeat.
        clock = _clock_from([0.0, 1.0, 200.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower(
                [[{"name": "verification_heartbeat", "payload": {}}]]
            ),
        )
        # First snapshot folds the verification heartbeat and marks it fresh.
        tailer.snapshot()
        # Second snapshot at t=200 sees no new events -> silent for 199s.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "verifying")
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("WARN possibly stalled", message)
        self.assertIn("no new events for 90s+ (no output)", message)

    def test_starts_unknown_before_any_folded_event(self) -> None:
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=_clock_from([0.0, 1.0]),
            follow=_batch_follower([[]]),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "unknown")
        self.assertFalse(events_stale)


class InvokeRunEventsTailerTest(unittest.TestCase):
    """`_invoke_run` prefers the events tailer and falls back to the probe."""

    FINAL_JSON = '{"ok": true, "paused": false, "reason": "all-complete"}'

    def _parsed(self) -> dict:
        return {"argv": ["asl", "run", "/tmp/plan", "--json"], "plan_dir": "/tmp/plan"}

    def _run(self, *, popen, tailer=None, status_probe=None, stall_threshold=90.0):
        beats: list = []
        payload = autopilot._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=beats.append,
            heartbeat_interval=0.01,
            popen=popen,
            status_probe=status_probe,
            events_tailer=tailer,
            stall_threshold=stall_threshold,
        )
        return payload, beats

    def test_tailer_snapshot_drives_the_tick_line(self) -> None:
        tailer = _FakeTailer([({"state": "running", "session_id": "s7",
                                "session_number": 2, "total_sessions": 5,
                                "idle_seconds": 96}, False)])
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, tailer=tailer)
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("session s7", tick)
        self.assertIn("(2/5)", tick)
        self.assertIn("idle 96s", tick)
        # Fresh stream (events_stale False) suppresses the WARN despite high idle.
        self.assertNotIn("WARN", tick)

    def test_tailer_stall_flag_renders_warning(self) -> None:
        tailer = _FakeTailer([({"state": "running", "session_id": "s7",
                                "idle_seconds": 96}, True)])
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, tailer=tailer)
        self.assertIn("WARN possibly stalled", beats[1])
        self.assertIn("idle 96s (no output)", beats[1])

    def test_tailer_failure_falls_back_to_status_probe(self) -> None:
        class _BoomTailer:
            def snapshot(self):
                raise RuntimeError("tailer blew up")

        popen = FakeTimeoutPopen(timeout_count=2, stdout=self.FINAL_JSON)
        payload, beats = self._run(
            popen=popen,
            tailer=_BoomTailer(),
            status_probe=lambda: _running_status(30),
        )
        self.assertEqual(payload["reason"], "all-complete")
        # Both ticks fell back to the status probe (idle 30, enriched line).
        self.assertIn("session tidy-db-writes", beats[1])
        self.assertIn("idle 30s", beats[1])
        self.assertIn("idle 30s", beats[2])

    def test_unknown_tailer_snapshot_falls_back_to_status_probe(self) -> None:
        # A missing/unreadable events file makes `follow_events` yield empty
        # batches (never raising), so the tailer snapshot stays "unknown". The
        # tick must then fall back to the authoritative status probe instead of
        # emitting a plain line, so enriched progress and stall detection are
        # not permanently suppressed. The tailer is not disabled, so it resumes
        # on later ticks once the events file appears.
        tailer = _FakeTailer([({"state": "unknown"}, False)])
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(
            popen=popen,
            tailer=tailer,
            status_probe=lambda: _running_status(41),
        )
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("session tidy-db-writes", tick)
        self.assertIn("idle 41s", tick)
        self.assertNotIn("running, elapsed", tick)

    def test_unknown_tailer_snapshot_with_stalled_probe_warns(self) -> None:
        # The recovered fallback still surfaces a stall: an unknown tailer
        # snapshot with a status probe reporting idle past the threshold must
        # render the WARN line rather than a suppressed plain line.
        tailer = _FakeTailer([({"state": "unknown"}, False)])
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(
            popen=popen,
            tailer=tailer,
            status_probe=lambda: _running_status(96),
        )
        self.assertIn("WARN possibly stalled", beats[1])
        self.assertIn("idle 96s (no output)", beats[1])

    def test_builds_tailer_from_status_events_path(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-tailer-wire-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        events_path = os.path.join(tmp, "events.jsonl")
        with open(events_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(_turn_heartbeat_record(42, session_id="wired")) + "\n")
        status = {"ok": True, "events_path": events_path}
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, status_probe=lambda: status)
        self.assertEqual(payload["reason"], "all-complete")
        # The tick was derived from the events file, not a per-tick status probe.
        self.assertIn("session wired", beats[1])
        self.assertIn("idle 42s", beats[1])


class _FakeTailer:
    """Return scripted ``(snapshot, events_stale)`` pairs (last pair repeats)."""

    def __init__(self, results):
        self._results = list(results)
        self._index = 0

    def snapshot(self):
        pair = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        return pair


def _hang_block(
    reason=autopilot.HANG_CLASS_RESULT_RECEIVED, *, idle=162.0, process_tree=None
):
    """Runner-shaped ``hang_suspected`` block (session-3 sentinel contract)."""
    return {
        "reason": reason,
        "detected_at": "2026-07-24T19:02:00+00:00",
        "idle_seconds": idle,
        "harness_pid": 4242,
        "result_seen_at": (
            "2026-07-24T19:01:00+00:00"
            if reason == autopilot.HANG_CLASS_RESULT_RECEIVED
            else None
        ),
        "confirm_samples": 2,
        "cpu_delta_seconds": 0.0,
        "process_tree": process_tree,
        "last_checked_at": "2026-07-24T19:03:00+00:00",
    }


def _hung_running_snapshot(idle=162.0, *, reason=autopilot.HANG_CLASS_RESULT_RECEIVED):
    """Tailer-shaped running snapshot carrying a hang_suspected block."""
    return {
        "state": "running",
        "session_id": "tidy-db-writes",
        "session_number": 3,
        "total_sessions": 8,
        "idle_seconds": idle,
        "elapsed_seconds": 300,
        "hang_suspected": _hang_block(reason, idle=idle),
    }


def _hung_run_status(idle=162.0, *, reason=autopilot.HANG_CLASS_RESULT_RECEIVED):
    """Full ``asl status`` payload with paths + a hang-suspected live turn."""
    return {
        "ok": True,
        "plan_dir": "/tmp/plan",
        "state_path": "/tmp/plan/.asl/state.json",
        "events_path": "/tmp/plan/.asl/events.jsonl",
        "active_turn": {
            "session_id": "tidy-db-writes",
            "session_number": 3,
            "total_sessions": 8,
            "idle_seconds": idle,
            "elapsed_seconds": 300,
            "stdout_path": "/tmp/plan/.asl/turn-stdout.log",
            "stderr_path": "/tmp/plan/.asl/turn-stderr.log",
            "last_message_path": "/tmp/plan/.asl/last-message.txt",
            "result_path": "/tmp/plan/.asl/result.json",
            "hang_suspected": _hang_block(reason, idle=idle),
        },
    }


def _hung_attach_status(idle=162.0, *, reason=autopilot.HANG_CLASS_RESULT_RECEIVED):
    """Attach-disposition status whose live turn carries a hang block."""
    status = _attach_status(idle)
    status["runner_state"]["active_turn"]["hang_suspected"] = _hang_block(
        reason, idle=idle
    )
    return status


class _HangingPopen:
    """``communicate`` always times out; killing or waiting is forbidden.

    Models a wedged ``asl run`` child: the escalation path must return the
    ``_worker_hung`` marker while this process object stays untouched.
    """

    pid = 4242

    def __init__(self):
        self.returncode = None
        self.argv = None
        self.kwargs = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return self

    def communicate(self, input=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd="asl run", timeout=timeout)

    def kill(self):  # pragma: no cover - guard
        raise AssertionError("worker_hung must not kill the `asl run` child")

    def terminate(self):  # pragma: no cover - guard
        raise AssertionError("worker_hung must not terminate the `asl run` child")

    def wait(self, timeout=None):  # pragma: no cover - guard
        raise AssertionError("worker_hung must not wait on the `asl run` child")


class HangEscalationPolicyTest(unittest.TestCase):
    """`_HangEscalation` hard bars above the soft stall WARN."""

    def test_result_received_needs_consecutive_probes(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        verdict = policy.observe(_hung_running_snapshot())
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(verdict["probes"], 2)
        self.assertEqual(verdict["idle_seconds"], 162.0)
        self.assertEqual(
            verdict["hang_suspected"]["reason"],
            autopilot.HANG_CLASS_RESULT_RECEIVED,
        )

    def test_retraction_resets_the_probe_count(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        # A tick without the block (turn_hang_cleared, or a heartbeat that
        # dropped it) resets the consecutive count.
        healthy = {"state": "running", "idle_seconds": 3}
        self.assertIsNone(policy.observe(healthy))
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        verdict = policy.observe(_hung_running_snapshot())
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["probes"], 2)

    def test_rearmed_new_episode_resets_the_probe_count(self) -> None:
        # F-001: two result-received blocks with different detected_at values
        # are different suspicion episodes; the second must count as probe 1,
        # not inherit the prior episode's streak.
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        rearmed = _hung_running_snapshot()
        rearmed["hang_suspected"]["detected_at"] = "2026-07-24T19:05:30+00:00"
        self.assertIsNone(policy.observe(rearmed))
        verdict = policy.observe(rearmed)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(verdict["probes"], 2)

    def test_idle_ceiling_without_block_is_idle_ceiling_exceeded(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        verdict = policy.observe({"state": "running", "idle_seconds": 950})
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertEqual(verdict["probes"], 0)
        self.assertIsNone(verdict["hang_suspected"])
        self.assertEqual(verdict["idle_seconds"], 950.0)

    def test_idle_ceiling_with_zero_cpu_block_keeps_the_runner_class(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot = _hung_running_snapshot(950, reason=autopilot.HANG_CLASS_ZERO_CPU)
        verdict = policy.observe(snapshot)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_ZERO_CPU)

    def test_idle_falls_back_to_the_hang_block_reading(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot = {
            "state": "running",
            "hang_suspected": _hang_block(
                autopilot.HANG_CLASS_ZERO_CPU, idle=950.0
            ),
        }
        verdict = policy.observe(snapshot)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_ZERO_CPU)
        self.assertEqual(verdict["idle_seconds"], 950.0)

    def test_below_the_bars_never_escalates(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        # Zero-cpu suspicion below the ceiling is deliberately conservative:
        # the runner already surfaced it; the helper keeps waiting.
        snapshot = _hung_running_snapshot(300, reason=autopilot.HANG_CLASS_ZERO_CPU)
        for _ in range(5):
            self.assertIsNone(policy.observe(snapshot))


class EventsTailerHangTest(unittest.TestCase):
    """The tailer folds hang blocks from heartbeats and one-shot hang events."""

    def test_heartbeat_folds_and_drops_the_hang_block(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0, 3.0])
        with_block = _turn_heartbeat_record(162)
        with_block["payload"]["hang_suspected"] = _hang_block()
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[with_block], [_turn_heartbeat_record(4)]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertEqual(
            snapshot["hang_suspected"]["reason"],
            autopilot.HANG_CLASS_RESULT_RECEIVED,
        )
        # A heartbeat without the block is an implicit retraction.
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)

    def test_one_shot_hang_events_set_and_clear_suspicion(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0, 3.0, 4.0])
        suspected = {
            "name": autopilot._events.TURN_HANG_SUSPECTED_EVENT,
            "payload": {"hang_suspected": _hang_block()},
        }
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {},
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower(
                [[_turn_heartbeat_record(30)], [suspected], [cleared]]
            ),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        snapshot, _ = tailer.snapshot()
        # The one-shot event folds onto the running snapshot in place.
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["session_id"], "tidy-db-writes")
        self.assertIn("hang_suspected", snapshot)
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)

    def test_same_batch_ceiling_heartbeat_then_clear_resets_stale_idle(self) -> None:
        # F-003: a 900s heartbeat and its retraction folded in one batch must
        # not leave the stale idle reading behind for the ceiling check.
        clock = _clock_from([0.0, 1.0, 2.0])
        hung = _turn_heartbeat_record(900)
        hung["payload"]["hang_suspected"] = _hang_block(
            autopilot.HANG_CLASS_ZERO_CPU, idle=900.0
        )
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {
                "idle_seconds": 0.0,
                "hang_suspected": _hang_block(
                    autopilot.HANG_CLASS_ZERO_CPU, idle=900.0
                ),
            },
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[hung, cleared]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        self.assertEqual(snapshot["idle_seconds"], 0.0)
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(snapshot))

    def test_same_batch_clear_then_rearm_starts_a_new_episode(self) -> None:
        # F-001: a clear and a re-armed suspicion folded between two polls
        # leave only the new block visible; its different detected_at must
        # reset the probe count instead of inheriting the old episode's.
        clock = _clock_from([0.0, 1.0, 2.0, 3.0])
        first = _turn_heartbeat_record(162)
        first["payload"]["hang_suspected"] = _hang_block()
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {"idle_seconds": 0.0, "hang_suspected": _hang_block()},
        }
        rearmed_block = _hang_block(idle=45.0)
        rearmed_block["detected_at"] = "2026-07-24T19:05:30+00:00"
        rearmed = {
            "name": autopilot._events.TURN_HANG_SUSPECTED_EVENT,
            "payload": {"hang_suspected": dict(rearmed_block)},
        }
        followup = _turn_heartbeat_record(75)
        followup["payload"]["hang_suspected"] = dict(rearmed_block)
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[first], [cleared, rearmed], [followup]]),
        )
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot, _ = tailer.snapshot()
        self.assertIsNone(policy.observe(snapshot))
        snapshot, _ = tailer.snapshot()
        self.assertEqual(
            snapshot["hang_suspected"]["detected_at"],
            "2026-07-24T19:05:30+00:00",
        )
        # One observation of the re-armed episode is probe 1, never probe 2.
        self.assertIsNone(policy.observe(snapshot))
        snapshot, _ = tailer.snapshot()
        verdict = policy.observe(snapshot)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(verdict["probes"], 2)

    def test_clear_without_idle_drops_the_stale_reading(self) -> None:
        # A cleared payload without the runner's fresh idle (older runners)
        # still invalidates the pre-clear value instead of retaining it.
        clock = _clock_from([0.0, 1.0, 2.0])
        hung = _turn_heartbeat_record(900)
        hung["payload"]["hang_suspected"] = _hang_block(
            autopilot.HANG_CLASS_ZERO_CPU, idle=900.0
        )
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {},
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[hung, cleared]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        self.assertNotIn("idle_seconds", snapshot)
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(snapshot))


class InvokeRunHangEscalationTest(unittest.TestCase):
    """`_invoke_run` returns the `_worker_hung` marker without touching the child."""

    def _parsed(self) -> dict:
        return {"argv": ["asl", "run", "/tmp/plan", "--json"], "plan_dir": "/tmp/plan"}

    def _run(self, *, tailer, status_probe=None, **kwargs):
        beats: list = []
        popen = _HangingPopen()
        payload = autopilot._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=beats.append,
            heartbeat_interval=0.01,
            popen=popen,
            status_probe=status_probe,
            events_tailer=tailer,
            **kwargs,
        )
        return payload, beats, popen

    def test_result_received_probes_escalate_without_killing(self) -> None:
        tailer = _FakeTailer([(_hung_running_snapshot(), False)])
        payload, beats, popen = self._run(
            tailer=tailer,
            status_probe=lambda: _hung_run_status(),
        )
        # _HangingPopen raises from kill/terminate/wait, so reaching here at
        # all proves the child was left alone.
        self.assertTrue(payload["_worker_hung"])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual(payload["reason"], "worker_hung")
        hang = payload["hang"]
        self.assertEqual(hang["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(hang["probes"], 2)
        self.assertEqual(hang["supervised_child_pid"], 4242)
        self.assertTrue(hang["detached"])
        self.assertEqual(hang["plan_dir"], "/tmp/plan")
        self.assertEqual(hang["state_path"], "/tmp/plan/.asl/state.json")
        self.assertEqual(hang["events_path"], "/tmp/plan/.asl/events.jsonl")
        self.assertEqual(
            hang["artifact_paths"],
            {
                "stdout": "/tmp/plan/.asl/turn-stdout.log",
                "stderr": "/tmp/plan/.asl/turn-stderr.log",
                "last_message": "/tmp/plan/.asl/last-message.txt",
                "result": "/tmp/plan/.asl/result.json",
            },
        )
        self.assertEqual(len(hang["next_steps"]), 4)
        # The freshly probed status rides on the marker for supervise_run.
        self.assertEqual(payload["status"]["plan_dir"], "/tmp/plan")
        joined = "\n".join(beats)
        self.assertIn("escalating worker_hung", joined)
        self.assertIn("leaving the `asl run` child alive", joined)

    def test_first_result_received_probe_does_not_escalate(self) -> None:
        # One result-received tick then a healthy tick, then the ordinary
        # timeout loop would continue — cap it with a final clean payload.
        tailer = _FakeTailer(
            [
                (_hung_running_snapshot(), False),
                ({"state": "running", "idle_seconds": 3}, False),
            ]
        )
        popen = FakeTimeoutPopen(
            timeout_count=2,
            stdout='{"ok": true, "paused": false, "reason": "all-complete"}',
        )
        payload = autopilot._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=lambda *_: None,
            heartbeat_interval=0.01,
            popen=popen,
            events_tailer=tailer,
        )
        self.assertEqual(payload["reason"], "all-complete")
        self.assertNotIn("_worker_hung", payload)

    def test_idle_ceiling_escalates_without_status_probe(self) -> None:
        tailer = _FakeTailer(
            [({"state": "running", "session_id": "s7", "idle_seconds": 950}, False)]
        )
        payload, _beats, _popen = self._run(tailer=tailer, status_probe=None)
        self.assertTrue(payload["_worker_hung"])
        hang = payload["hang"]
        self.assertEqual(hang["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertEqual(hang["probes"], 0)
        self.assertIsNone(hang["hang_suspected"])
        # No status payload: plan_dir falls back to the parsed command; the
        # path fields degrade to None rather than being omitted.
        self.assertEqual(hang["plan_dir"], "/tmp/plan")
        self.assertIsNone(hang["state_path"])
        self.assertEqual(
            hang["artifact_paths"],
            {"stdout": None, "stderr": None, "last_message": None, "result": None},
        )
        self.assertNotIn("status", payload)


class SuperviseRunWorkerHungTest(unittest.TestCase):
    """`supervise_run` maps hang escalations to `worker_hung` results."""

    PARSED = {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]}

    def test_marker_from_chunk_maps_to_worker_hung_result(self) -> None:
        hang = {"class": autopilot.HANG_CLASS_RESULT_RECEIVED, "probes": 2}
        marker_status = {"ok": True, "plan_dir": "/tmp/plan", "marker": True}
        marker = {
            "ok": False,
            "paused": False,
            "reason": "worker_hung",
            "_worker_hung": True,
            "hang": hang,
            "status": marker_status,
        }
        run = _RunScript([marker])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=_StatusScript([_status_payload(1)]),
            run_invoker=run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"], hang)
        # The marker's escalation-time status is fresher than the launch probe.
        self.assertTrue(result["status"]["marker"])
        self.assertEqual(result["chunks"], 1)

    def test_marker_without_status_falls_back_to_the_loop_probe(self) -> None:
        marker = {
            "ok": False,
            "paused": False,
            "reason": "worker_hung",
            "_worker_hung": True,
            "hang": {"class": autopilot.HANG_CLASS_IDLE_CEILING},
        }
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=_StatusScript([_status_payload(1)]),
            run_invoker=_RunScript([marker]),
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["status"]["counts"]["pending"], 1)

    def test_attach_path_escalates_after_consecutive_hung_probes(self) -> None:
        beats: list = []
        status = _StatusScript([_hung_attach_status(), _hung_attach_status()])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=beats.append,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "worker_hung")
        hang = result["hang"]
        self.assertEqual(hang["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(hang["probes"], 2)
        # Attach mode launched nothing, so there is no child pid to report.
        self.assertNotIn("supervised_child_pid", hang)
        self.assertNotIn("detached", hang)
        self.assertIn("escalating worker_hung", "\n".join(beats))

    def test_attach_probe_from_a_different_episode_resets_the_count(self) -> None:
        # The attach-path policy instance keys on episode identity too: a
        # second probe whose block carries a new detected_at is probe 1 of
        # the re-armed episode, so escalation waits for the third probe.
        first = _hung_attach_status()
        rearmed = _hung_attach_status()
        rearmed["runner_state"]["active_turn"]["hang_suspected"][
            "detected_at"
        ] = "2026-07-24T19:05:30+00:00"
        status = _StatusScript([first, rearmed, rearmed])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"]["probes"], 2)
        self.assertEqual(status.calls, 3)

    def test_attach_idle_ceiling_escalates_without_a_hang_block(self) -> None:
        status = _StatusScript([_attach_status(950)])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"]["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertIsNone(result["hang"]["hang_suspected"])

    def test_attach_soft_stall_below_the_bars_keeps_waiting(self) -> None:
        # idle 96 crosses the 90s soft WARN but no hard bar: supervision must
        # keep polling and complete normally (soft-stall behavior unchanged).
        beats: list = []
        status = _StatusScript([_attach_status(96), _status_payload(0)])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=beats.append,
            stall_threshold=90.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        joined = "\n".join(beats)
        self.assertIn("WARN possibly stalled", joined)
        self.assertNotIn("escalating worker_hung", joined)


class RunAutopilotWorkerHungTest(unittest.TestCase):
    """`run_autopilot` passes a `worker_hung` stop through untouched."""

    def _args(self, command="asl run /tmp/plan --review-harness codex"):
        parser = autopilot.build_arg_parser()
        return parser.parse_args(["--command", command])

    def test_worker_hung_skips_questions_and_review(self) -> None:
        hang = {
            "class": autopilot.HANG_CLASS_ZERO_CPU,
            "idle_seconds": 8412.0,
            "next_steps": list(autopilot.HANG_NEXT_STEPS),
        }

        def hung_supervisor(parsed):
            return {
                "ok": False,
                "stage": autopilot.RUN_STAGE,
                "reason": "worker_hung",
                "chunks": 1,
                "hang": hang,
            }

        def fail_questioner(parsed):  # pragma: no cover - must not run
            raise AssertionError("asl questions must not be fetched for worker_hung")

        def fail_review_reader(parsed):  # pragma: no cover - must not run
            raise AssertionError("no review verdict is read for worker_hung")

        result = autopilot.run_autopilot(
            self._args(),
            supervisor=hung_supervisor,
            questioner=fail_questioner,
            review_reader=fail_review_reader,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"], hang)
        self.assertNotIn("questions", result)
        self.assertNotIn("questions_error", result)
        self.assertNotIn("review", result)


class HumanSummaryWorkerHungTest(unittest.TestCase):
    """The one-line summary names the class, idle time, and hung leaf."""

    def test_summary_names_class_idle_and_leaf_command(self) -> None:
        tree = [
            {
                "pid": 4243,
                "ppid": 4242,
                "state": "S",
                "cpu_seconds": 12.0,
                "command": "node /usr/local/bin/harness",
            },
            {
                "pid": 4244,
                "ppid": 4243,
                "state": "S",
                "cpu_seconds": 0.2,
                "command": "/bin/zsh -c git diff | diff-so-fancy",
            },
            {
                "pid": 4245,
                "ppid": 4244,
                "state": "S+",
                "cpu_seconds": 0.0,
                "command": "perl /usr/local/bin/diff-so-fancy",
            },
        ]
        result = {
            "ok": False,
            "reason": "worker_hung",
            "hang": {
                "class": autopilot.HANG_CLASS_ZERO_CPU,
                "idle_seconds": 8412.0,
                "hang_suspected": _hang_block(
                    autopilot.HANG_CLASS_ZERO_CPU, idle=8412.0, process_tree=tree
                ),
            },
        }
        summary = autopilot._human_summary(result)
        self.assertIn("worker_hung", summary)
        self.assertIn(autopilot.HANG_CLASS_ZERO_CPU, summary)
        self.assertIn("idle 8412s", summary)
        self.assertIn("perl /usr/local/bin/diff-so-fancy", summary)
        self.assertIn("inspect before killing", summary)
        self.assertIn("re-run the same command to re-attach", summary)

    def test_summary_survives_a_missing_hang_payload(self) -> None:
        summary = autopilot._human_summary({"ok": False, "reason": "worker_hung"})
        self.assertIn("worker_hung", summary)
        self.assertIn("idle unknown", summary)
        self.assertNotIn("deepest descendant", summary)


_FAKE_ASL = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    state_path = os.environ["FAKE_ASL_STATE"]

    with open(state_path) as fh:
        state = json.load(fh)

    argv = sys.argv[1:]
    sub = argv[0] if argv else ""

    if sub == "status":
        payload = {
            "ok": True,
            "counts": {"pending": state["pending"], "complete": state["complete"]},
            "next_pending_id": None if state["pending"] == 0 else str(state["complete"] + 1),
            "lock": {"present": False, "active": False, "stale": False},
            "sessions": [],
            "plan_dir": "/tmp/plan",
        }
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    if sub == "run":
        # Model the real runner: drain every pending session in one process
        # unless `--max-sessions N` caps it, in which case take at most N and
        # report the runner's own `max-sessions` stop.
        max_sessions = None
        for i, tok in enumerate(argv):
            if tok == "--max-sessions" and i + 1 < len(argv):
                max_sessions = int(argv[i + 1])
        took = state["pending"] if max_sessions is None else min(max_sessions, state["pending"])
        # A queued `stop-after-current` request: the runner drains it after the
        # current session, so it takes exactly one session and then exits ok
        # with `reason: "stopped"`. Draining consumes the request (one-shot),
        # and a stop with nothing left pending is a no-op that reports the
        # ordinary all-complete exit.
        stop_after_current = bool(state.get("stop_after_current"))
        if stop_after_current:
            took = min(took, 1)
            state["stop_after_current"] = False
        state["pending"] -= took
        state["complete"] += took
        state["chunks_run"] = state.get("chunks_run", 0) + 1
        with open(state_path, "w") as fh:
            json.dump(state, fh)
        if state["pending"] == 0:
            payload = {"ok": True, "paused": False, "reason": "all-complete"}
        elif stop_after_current:
            payload = {"ok": True, "paused": False, "reason": "stopped"}
        else:
            payload = {"ok": True, "paused": False, "reason": "max-sessions"}
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    sys.stderr.write("unknown subcommand\\n")
    sys.exit(2)
    '''
)


class SuperviseRunFakeAslTest(unittest.TestCase):
    """Exercise the real subprocess plumbing against a fake `asl` executable."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.asl = os.path.join(self.tmp, "asl")
        with open(self.asl, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_ASL)
        os.chmod(self.asl, os.stat(self.asl).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self.state_path = os.path.join(self.tmp, "state.json")

    def _write_state(self, **state) -> None:
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def _read_state(self) -> dict:
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _parsed(self, extra: str = "") -> dict:
        command = f"FAKE_ASL_STATE={self.state_path} {self.asl} run /tmp/plan {extra}".strip()
        parsed = autopilot.parse_command(command)
        self.assertTrue(parsed["ok"], parsed)
        return parsed

    def _supervise(self, *, command_extra: str = "", **kwargs):
        return autopilot.supervise_run(
            self._parsed(command_extra),
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            **kwargs,
        )

    def test_starts_and_completes_single_chunk(self) -> None:
        self._write_state(pending=1, complete=0, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(self._read_state()["chunks_run"], 1)

    def test_already_complete_never_runs(self) -> None:
        self._write_state(pending=0, complete=2, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(self._read_state()["chunks_run"], 0)

    def test_uncapped_drains_all_pending_in_one_chunk(self) -> None:
        # Without `--max-sessions`, the runner completes every pending session in
        # a single process — the supervisor does not chunk through them.
        self._write_state(pending=3, complete=0, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(self._read_state()["chunks_run"], 1)
        self.assertEqual(self._read_state()["complete"], 3)

    def test_max_sessions_is_a_global_budget(self) -> None:
        # End-to-end through parse -> argv -> child: `--max-sessions 2` on a
        # 5-session plan completes exactly 2 and stops cleanly with the remaining
        # sessions still pending — a single chunk, no relaunching.
        self._write_state(pending=5, complete=0, chunks_run=0)
        result = self._supervise(command_extra="--max-sessions 2")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "max-sessions")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["pending"], 3)
        state = self._read_state()
        self.assertEqual(state["complete"], 2)
        self.assertEqual(state["pending"], 3)
        self.assertEqual(state["chunks_run"], 1)

    def test_budget_stop_is_a_resumable_checkpoint(self) -> None:
        # A budget stop leaves a clean checkpoint: re-running the same command
        # continues from the remaining sessions rather than a dead end.
        self._write_state(pending=5, complete=0, chunks_run=0)
        first = self._supervise(command_extra="--max-sessions 2")
        self.assertEqual(first["reason"], "max-sessions")
        self.assertEqual(self._read_state()["complete"], 2)

        second = self._supervise(command_extra="--max-sessions 2")
        self.assertEqual(second["reason"], "max-sessions")
        self.assertEqual(second["completed"], 2)
        self.assertEqual(self._read_state()["complete"], 4)
        self.assertEqual(self._read_state()["pending"], 1)

    def test_honored_stop_after_current_is_a_resumable_checkpoint(self) -> None:
        # End-to-end: a queued `asl control stop-after-current` makes the child
        # finish one session and exit ok with `reason: "stopped"`. The supervisor
        # must stop there — a single chunk, no relaunch — and re-running the same
        # command continues from the remaining sessions.
        self._write_state(
            pending=4, complete=0, chunks_run=0, stop_after_current=True
        )
        first = self._supervise()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["reason"], "stopped")
        self.assertEqual(first["chunks"], 1)
        self.assertEqual(first["completed"], 1)
        self.assertEqual(first["pending"], 3)
        state = self._read_state()
        self.assertEqual(state["complete"], 1)
        self.assertEqual(state["chunks_run"], 1)

        second = self._supervise()
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["reason"], "complete")
        self.assertEqual(self._read_state()["pending"], 0)

    def test_heartbeats_stay_off_stdout(self) -> None:
        self._write_state(pending=2, complete=0, chunks_run=0)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = autopilot.supervise_run(
                self._parsed(),
                clock=_counter_clock(),
                sleep=lambda *_: None,
            )
        self.assertTrue(result["ok"], result)
        # stdout is reserved for the final JSON payload; heartbeats go to stderr.
        self.assertEqual(out.getvalue(), "")
        self.assertIn("started `asl run`", err.getvalue())


_FAKE_ASL_REPLY = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    capture = os.environ["FAKE_ASL_CAPTURE"]
    argv = sys.argv[1:]
    sub = argv[0] if argv else ""

    if sub == "status":
        done = os.path.exists(capture)
        payload = {
            "ok": True,
            "counts": {"pending": 0 if done else 1, "complete": 1 if done else 0},
            "next_pending_id": None if done else "4",
            "lock": {"present": False, "active": False, "stale": False},
            "sessions": [],
            "plan_dir": "/tmp/plan",
        }
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    if sub == "run":
        reply = sys.stdin.read() if "--reply-stdin" in argv else ""
        with open(capture, "w") as fh:
            json.dump({"argv": argv, "stdin": reply}, fh)
        sys.stdout.write(json.dumps({"ok": True, "paused": False, "reason": "all-complete"}))
        sys.exit(0)

    sys.stderr.write("unknown subcommand\\n")
    sys.exit(2)
    '''
)


class ReplyResumeFakeAslTest(unittest.TestCase):
    """End-to-end resume: reply streams on stdin, never into argv."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-reply-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.asl = os.path.join(self.tmp, "asl")
        with open(self.asl, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_ASL_REPLY)
        os.chmod(self.asl, os.stat(self.asl).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self.capture_path = os.path.join(self.tmp, "capture.json")

    def _read_capture(self) -> dict:
        with open(self.capture_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_reply_reaches_stdin_and_never_leaks_into_argv(self) -> None:
        reply = "reuse the cached parser output"
        parser = autopilot.build_arg_parser()
        args = parser.parse_args([
            "--command",
            f"FAKE_ASL_CAPTURE={self.capture_path} {self.asl} run /tmp/plan",
            "--reply-stdin",
        ])
        err = io.StringIO()
        with redirect_stderr(err):
            result = autopilot.run_autopilot(args, stdin=io.StringIO(reply))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")

        capture = self._read_capture()
        # The reply is delivered on stdin verbatim...
        self.assertEqual(capture["stdin"], reply)
        # ...and `--reply-stdin` is the flag that carried it...
        self.assertIn("--reply-stdin", capture["argv"])
        # ...but the reply text itself must never appear in argv.
        self.assertNotIn(reply, capture["argv"])
        self.assertFalse(any(reply in token for token in capture["argv"]))


_FAKE_ASL_QUESTIONS = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import json
    import sys

    argv = sys.argv[1:]
    sub = argv[0] if argv else ""

    if sub == "questions":
        payload = {
            "ok": True,
            "command": "questions",
            "session_id": "4",
            "next_pending_id": "4",
            "pause_reason": "input-error",
            "rendered_prompt": "Resolve the paused question and reply.",
            "last_message": "Should I cache the parser output?",
            "artifact_paths": {"last_message_path": "/tmp/last.txt"},
        }
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    sys.stderr.write("unknown subcommand\\n")
    sys.exit(2)
    '''
)


class InvokeQuestionsFakeAslTest(unittest.TestCase):
    """`asl questions --json` transport is parsed into the question payload."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-q-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.asl = os.path.join(self.tmp, "asl")
        with open(self.asl, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_ASL_QUESTIONS)
        os.chmod(self.asl, os.stat(self.asl).st_mode | stat.S_IEXEC | stat.S_IXGRP)

    def test_questions_payload_is_returned(self) -> None:
        parsed = autopilot.parse_command(f"{self.asl} run /tmp/plan")
        self.assertTrue(parsed["ok"], parsed)
        payload = autopilot._invoke_questions(parsed)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["session_id"], "4")
        self.assertEqual(payload["last_message"], "Should I cache the parser output?")


class ParseReviewVerdictTest(unittest.TestCase):
    """Classify `final-review.md` status lines (mirrors the ASL runner)."""

    def test_all_clear_is_parsed(self) -> None:
        text = "# Review\n\nNo issues found.\n\nStatus: All clear, no findings\n"
        verdict = autopilot.parse_review_verdict(text)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["outcome"], "all-clear")
        self.assertEqual(verdict["total_findings"], 0)
        self.assertEqual(
            verdict["findings_by_severity"], {"high": 0, "med": 0, "low": 0}
        )

    def test_findings_status_is_parsed(self) -> None:
        text = (
            "# Review\n\nFound some things.\n\n"
            "Status: 3 Findings (1 high, 1 med, 1 low)\n"
        )
        verdict = autopilot.parse_review_verdict(text)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["outcome"], "findings")
        self.assertEqual(verdict["total_findings"], 3)
        self.assertEqual(
            verdict["findings_by_severity"], {"high": 1, "med": 1, "low": 1}
        )
        self.assertEqual(
            verdict["status_line"], "Status: 3 Findings (1 high, 1 med, 1 low)"
        )

    def test_empty_review_is_malformed(self) -> None:
        verdict = autopilot.parse_review_verdict("\n\n   \n")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_missing_status_line_is_malformed(self) -> None:
        verdict = autopilot.parse_review_verdict("# Review\n\nAll good here.\n")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_multiple_status_lines_is_malformed(self) -> None:
        text = (
            "Status: All clear, no findings\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n"
        )
        verdict = autopilot.parse_review_verdict(text)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_status_not_final_line_is_malformed(self) -> None:
        text = "Status: All clear, no findings\n\nTrailing prose here.\n"
        verdict = autopilot.parse_review_verdict(text)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_malformed_findings_grammar_is_rejected(self) -> None:
        verdict = autopilot.parse_review_verdict("Status: 3 issues found\n")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_findings_counts_must_sum_to_total(self) -> None:
        verdict = autopilot.parse_review_verdict(
            "Status: 3 Findings (1 high, 1 med, 0 low)\n"
        )
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")

    def test_zero_total_findings_is_rejected(self) -> None:
        verdict = autopilot.parse_review_verdict(
            "Status: 0 Findings (0 high, 0 med, 0 low)\n"
        )
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "malformed-review")


class ReadReviewVerdictTest(unittest.TestCase):
    """`_read_review_verdict` reads `final-review.md` from the plan dir."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-review-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.parsed = {"plan_dir": self.tmp}

    def _write_review(self, text: str) -> None:
        path = os.path.join(self.tmp, autopilot.FINAL_REVIEW_FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_findings_verdict_carries_path_and_summary(self) -> None:
        body = "# Review\n\nDetails.\n\nStatus: 2 Findings (2 high, 0 med, 0 low)\n"
        self._write_review(body)
        verdict = autopilot._read_review_verdict(self.parsed)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["outcome"], "findings")
        self.assertEqual(
            verdict["review_output_path"],
            os.path.join(self.tmp, autopilot.FINAL_REVIEW_FILENAME),
        )
        self.assertIn("Details.", verdict["summary"])
        # A short review is not truncated, but still names the full-review
        # escape hatch so callers can always reach the untruncated verdict.
        self.assertFalse(verdict["summary_truncated"])
        self.assertIn("asl review-verdict", verdict["full_review"])
        self.assertIn(self.tmp, verdict["full_review"])

    def test_bounded_review_summary_returns_text_and_flag(self) -> None:
        short, truncated = autopilot._bounded_review_summary("body\n")
        self.assertEqual("body", short)
        self.assertFalse(truncated)

        big = "x" * (autopilot.REVIEW_SUMMARY_MAX_CHARS + 500)
        clipped, truncated = autopilot._bounded_review_summary(big)
        self.assertTrue(truncated)
        self.assertTrue(clipped.endswith("… (truncated)"))

    def test_summary_is_bounded(self) -> None:
        big = "x" * (autopilot.REVIEW_SUMMARY_MAX_CHARS + 500)
        self._write_review(f"{big}\n\nStatus: All clear, no findings\n")
        verdict = autopilot._read_review_verdict(self.parsed)
        self.assertTrue(verdict["ok"], verdict)
        self.assertLessEqual(
            len(verdict["summary"]),
            autopilot.REVIEW_SUMMARY_MAX_CHARS + len("\n… (truncated)"),
        )
        self.assertTrue(verdict["summary"].endswith("… (truncated)"))
        # Truncation is now flagged explicitly instead of silently dropping
        # the tail, and the full-review escape hatch is named.
        self.assertTrue(verdict["summary_truncated"])
        self.assertIn("asl review-verdict", verdict["full_review"])

    def test_missing_file_reports_review_missing(self) -> None:
        verdict = autopilot._read_review_verdict(self.parsed)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "review-missing")


class RunAutopilotReviewTest(unittest.TestCase):
    """`run_autopilot` re-tags a completed review run by its verdict."""

    def _args(self, command="asl run /tmp/plan --review-harness codex"):
        parser = autopilot.build_arg_parser()
        return parser.parse_args(["--command", command])

    def _complete_supervisor(self, parsed):
        return {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "chunks": 1,
        }

    def test_findings_retag_to_review_findings(self) -> None:
        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (1 high, 0 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 1, "med": 0, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }
        result = autopilot.run_autopilot(
            self._args(),
            supervisor=self._complete_supervisor,
            review_reader=lambda parsed: verdict,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "review_findings")
        self.assertEqual(result["review"]["total_findings"], 1)
        # The runner owns repair cycles now; the helper attaches no follow-up
        # fix-plan guidance.
        self.assertNotIn("review_fix", result)

    def test_resume_only_review_findings_retag_via_persisted_harness(self) -> None:
        # A resume-only command carries no --review-harness of its own; the
        # effective harness lives in the persisted run_config. The completed run
        # must still read the findings verdict and re-tag review_findings rather
        # than returning a bare complete (F1). The supervisor result carries the
        # `state_path` `_effective_review_config` reads to resolve that harness.
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-resume-review-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": {"review_harness": "codex"}}, handle)

        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (1 high, 0 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 1, "med": 0, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }

        def complete_resume(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
                "status": {"ok": True, "state_path": state_path},
            }

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --resume"),
            supervisor=complete_resume,
            review_reader=lambda parsed: verdict,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "review_findings")
        self.assertEqual(result["review"]["total_findings"], 1)

    def test_resume_without_persisted_harness_stays_complete(self) -> None:
        # No effective review harness (resume run_config has none, no explicit
        # flag): the completed run is left complete and the review verdict is not
        # even read, preserving the historical non-review complete behavior.
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-resume-noreview-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": {"harness": "codex"}}, handle)

        reads: list = []

        def reader(parsed):
            reads.append(parsed)
            return {"ok": True, "outcome": "findings", "total_findings": 1}

        def complete_resume(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
                "status": {"ok": True, "state_path": state_path},
            }

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --resume"),
            supervisor=complete_resume,
            review_reader=reader,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(reads, [])

    def test_findings_carry_review_resume_cap_guidance(self) -> None:
        review_resume = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )

        def complete_with_guidance(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 0,
                "status": {"review_resume": review_resume},
            }

        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (0 high, 1 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 0, "med": 1, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }
        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --review-harness codex --max-review-fix-cycles 2"),
            supervisor=complete_with_guidance,
            review_reader=lambda parsed: verdict,
        )
        self.assertEqual(result["reason"], "review_findings")
        self.assertEqual(result["review_resume"], review_resume)
        summary = autopilot._human_summary(result)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)

    def test_higher_cap_guidance_adds_missing_review_harness(self) -> None:
        review_resume = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )

        def complete_with_guidance(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 0,
                "review_resume": review_resume,
            }

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --max-review-fix-cycles 2"),
            supervisor=complete_with_guidance,
        )
        summary = autopilot._human_summary(result)
        self.assertIn("with a review harness", summary)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)

    def test_resume_only_higher_cap_guidance_uses_persisted_harness(self) -> None:
        # A resume-only command carries no explicit --review-harness; the
        # effective harness lives in the persisted run_config. Human
        # continuation guidance must derive review-harness activity from that
        # same effective config (F8), so it does NOT ask the operator to add a
        # review harness the runner already rehydrates — while still surfacing
        # the higher-cap prerequisite.
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-resume-guidance-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": {"review_harness": "codex"}}, handle)

        review_resume = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )

        def complete_resume(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
                "status": {
                    "ok": True,
                    "state_path": state_path,
                    "review_resume": review_resume,
                },
            }

        verdict = {
            "ok": True,
            "outcome": "findings",
            "status_line": "Status: 1 Findings (0 high, 1 med, 0 low)",
            "total_findings": 1,
            "findings_by_severity": {"high": 0, "med": 1, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "…",
        }
        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --resume"),
            supervisor=complete_resume,
            review_reader=lambda parsed: verdict,
        )
        self.assertEqual(result["reason"], "review_findings")
        summary = autopilot._human_summary(result)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)
        self.assertNotIn("with a review harness", summary)

    def test_eligible_guidance_honors_existing_review_harness(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "command": {"review_harness": "codex"},
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=True,
                minimum_required_cap=3,
                source_disposition="cycle-cap-exhausted",
            ),
        }

        summary = autopilot._human_summary(result)

        self.assertIn("rerun the same review-enabled command", summary)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)
        self.assertNotIn("with a review harness", summary)

    def test_enabled_harness_rereview_guidance_is_cap_free(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "command": {"review_harness": "codex"},
            "review_resume": _review_resume(
                "rereview",
                eligible=True,
                minimum_required_cap=9,
                source_disposition="repairs-materialized",
            ),
        }

        summary = autopilot._human_summary(result)

        self.assertIn("rerun the same review-enabled command", summary)
        self.assertNotIn("--max-review-fix-cycles", summary)

    def test_paused_repair_summaries_omit_continuation_guidance(self) -> None:
        for reason in ("paused", "needs_agent_reply"):
            with self.subTest(reason=reason):
                result = {
                    "ok": False,
                    "reason": reason,
                    "error": "the repair session needs an answer",
                    "review_resume": _review_resume(
                        "run-repairs",
                        eligible=True,
                        source_disposition="repairs-materialized",
                    ),
                }

                summary = autopilot._human_summary(result)

                self.assertIn("the repair session needs an answer", summary)
                self.assertNotIn("resume the same plan", summary)
                self.assertNotIn("review harness", summary)

    def test_foreign_lock_summary_omits_review_continuation_guidance(self) -> None:
        result = {
            "ok": False,
            "reason": "user_required",
            "error": "active lock belongs to another plan",
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="higher-cap-required",
                minimum_required_cap=3,
                source_disposition="cycle-cap-exhausted",
            ),
        }

        summary = autopilot._human_summary(result)

        self.assertIn("active lock belongs to another plan", summary)
        self.assertNotIn("resume the same plan", summary)
        self.assertNotIn("--max-review-fix-cycles", summary)

    def test_human_summary_surfaces_artifact_remediation(self) -> None:
        result = {
            "ok": False,
            "reason": "review_findings",
            "review": {"status_line": "Status: 1 Findings (1 high, 0 med, 0 low)"},
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="artifact-invalid",
                source_disposition="malformed-review-repair",
                artifact_error="repair_group is missing",
            ),
        }
        summary = autopilot._human_summary(result)
        self.assertIn("correct the review artifact", summary)
        self.assertIn("repair_group is missing", summary)

    def test_complete_summary_also_surfaces_artifact_remediation(self) -> None:
        result = {
            "ok": True,
            "reason": "complete",
            "command": {"review_harness": None},
            "review_error": {"reason": "review-malformed"},
            "review_resume": _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="artifact-invalid",
                source_disposition="malformed-review-repair",
                artifact_error="done_when is empty",
            ),
        }
        summary = autopilot._human_summary(result)
        self.assertIn("ok: complete", summary)
        self.assertIn("correct the review artifact: done_when is empty", summary)
        self.assertIn("resume the same plan with a review harness", summary)

    def test_terminal_blocker_guidance_remains_visible(self) -> None:
        result = {
            "ok": False,
            "reason": "blocked",
            "review_resume": _review_resume(
                "terminal",
                eligible=False,
                blocker="repair-sessions-missing",
                source_disposition="repairs-materialized",
            ),
        }
        self.assertIn(
            "no automatic continuation (repair-sessions-missing)",
            autopilot._human_summary(result),
        )

    def test_all_clear_stays_complete_with_review_attached(self) -> None:
        verdict = {
            "ok": True,
            "outcome": "all-clear",
            "status_line": autopilot.REVIEW_STATUS_ALL_CLEAR,
            "total_findings": 0,
            "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            "review_output_path": "/tmp/plan/final-review.md",
            "summary": "clean",
        }
        result = autopilot.run_autopilot(
            self._args(),
            supervisor=self._complete_supervisor,
            review_reader=lambda parsed: verdict,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["review"]["outcome"], "all-clear")

    def test_missing_review_records_error_but_stays_complete(self) -> None:
        error = {"ok": False, "reason": "review-missing", "error": "gone"}
        result = autopilot.run_autopilot(
            self._args(),
            supervisor=self._complete_supervisor,
            review_reader=lambda parsed: error,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["review_error"]["reason"], "review-missing")

    def test_no_review_harness_skips_review_read(self) -> None:
        def fail_reader(parsed):  # pragma: no cover - must not run
            raise AssertionError("review reader must not run without --review-harness")

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan"),
            supervisor=self._complete_supervisor,
            review_reader=fail_reader,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertNotIn("review", result)

    def test_human_summary_surfaces_review_status_line(self) -> None:
        result = {
            "ok": False,
            "reason": "review_findings",
            "review": {"status_line": "Status: 2 Findings (0 high, 2 med, 0 low)"},
        }
        summary = autopilot._human_summary(result)
        self.assertIn("review_findings", summary)
        self.assertIn("2 Findings", summary)

    def test_human_summary_reports_max_sessions_progress(self) -> None:
        result = {
            "ok": True,
            "reason": "max-sessions",
            "completed": 1,
            "pending": 30,
        }
        summary = autopilot._human_summary(result)
        self.assertIn("max-sessions", summary)
        self.assertIn("completed 1", summary)
        self.assertIn("30 still pending", summary)

    def test_human_summary_reports_stopped_progress(self) -> None:
        result = {
            "ok": True,
            "reason": "stopped",
            "completed": 3,
            "pending": 4,
        }
        summary = autopilot._human_summary(result)
        self.assertTrue(summary.startswith("ok: stopped"), summary)
        self.assertIn("completed 3", summary)
        self.assertIn("4 still pending", summary)
        # Never rendered as a pause, a failure, or `blocked`.
        self.assertNotIn("stopped:", summary)
        self.assertNotIn("blocked", summary)
        self.assertNotIn("paused", summary)


if __name__ == "__main__":
    unittest.main()
