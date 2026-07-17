#!/usr/bin/env python3
"""Tests for the asl autopilot helper scaffold."""

from __future__ import annotations

import importlib.util
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
            ]
        )
        self.assertEqual(args.command, "asl run /tmp/plan")
        self.assertTrue(args.json)
        self.assertTrue(args.reply_stdin)

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
            "--allow-not-loop-ready"
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


def _active_lock(plan_dir="/tmp/plan", *, stale=False):
    return {
        "present": True,
        "active": True,
        "stale": stale,
        "plan_dir": plan_dir,
        "reason": "stale" if stale else "active",
    }


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


def _never_run(parsed, chunk_index):  # pragma: no cover - guard for attach paths
    raise AssertionError("run_invoker must not be called")


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

    def _supervise(self, status_invoker, run_invoker, *, parsed=None, **kwargs):
        return autopilot.supervise_run(
            parsed
            or {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status_invoker,
            run_invoker=run_invoker,
            clock=_counter_clock(),
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

    def test_capped_relaunches_stop_at_budget(self) -> None:
        # Pending never decreases, so the loop must stop at max_chunks.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "max-sessions"}])
        result = self._supervise(status, run, max_chunks=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capped")
        self.assertEqual(result["chunks"], 3)

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
        per = state.get("per_chunk", 1)
        took = min(per, state["pending"])
        state["pending"] -= took
        state["complete"] += took
        state["chunks_run"] = state.get("chunks_run", 0) + 1
        with open(state_path, "w") as fh:
            json.dump(state, fh)
        if state["pending"] == 0:
            payload = {"ok": True, "paused": False, "reason": "all-complete"}
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

    def _parsed(self) -> dict:
        parsed = autopilot.parse_command(
            f"FAKE_ASL_STATE={self.state_path} {self.asl} run /tmp/plan"
        )
        self.assertTrue(parsed["ok"], parsed)
        return parsed

    def _supervise(self, **kwargs):
        return autopilot.supervise_run(
            self._parsed(),
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            **kwargs,
        )

    def test_starts_and_completes_single_chunk(self) -> None:
        self._write_state(pending=1, complete=0, per_chunk=1, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(self._read_state()["chunks_run"], 1)

    def test_already_complete_never_runs(self) -> None:
        self._write_state(pending=0, complete=2, per_chunk=1, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(self._read_state()["chunks_run"], 0)

    def test_repeated_capped_chunks_until_complete(self) -> None:
        self._write_state(pending=3, complete=0, per_chunk=1, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 3)
        self.assertEqual(self._read_state()["chunks_run"], 3)

    def test_max_chunks_budget_caps_relaunches(self) -> None:
        self._write_state(pending=5, complete=0, per_chunk=1, chunks_run=0)
        result = self._supervise(max_chunks=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "capped")
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(self._read_state()["chunks_run"], 2)

    def test_heartbeats_stay_off_stdout(self) -> None:
        self._write_state(pending=2, complete=0, per_chunk=1, chunks_run=0)
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


if __name__ == "__main__":
    unittest.main()
