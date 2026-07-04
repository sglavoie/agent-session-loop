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
        args = parser.parse_args(["--command", "asl run /tmp/plan", "--json", "--reply-stdin"])
        self.assertEqual(args.command, "asl run /tmp/plan")
        self.assertTrue(args.json)
        self.assertTrue(args.reply_stdin)

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
            "--model opus --effort high --timeout 600 --max-sessions 3 "
            "--max-pending-sessions 30 --commit-at-end --no-unsafe-bypass "
            "--allow-not-loop-ready"
        )
        self.assertTrue(result["ok"])
        args = result["run_args"]
        self.assertIn("--harness", args)
        self.assertIn("claude", args)
        self.assertIn("--review-harness", args)
        self.assertIn("--commit-at-end", args)
        self.assertIn("--no-unsafe-bypass", args)
        self.assertIn("--allow-not-loop-ready", args)
        self.assertEqual(args[args.index("--timeout") + 1], "600")

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

    def _supervise(self, status_invoker, run_invoker, **kwargs):
        return autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
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
