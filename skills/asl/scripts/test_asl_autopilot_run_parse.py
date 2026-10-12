#!/usr/bin/env python3
"""Tests for the autopilot child environment, parse integration, and pause replies."""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from asl_autopilot_test_support import (  # noqa: F401
    autopilot,
    setUpModule,
    tearDownModule,
)


class ChildEnvTest(unittest.TestCase):
    def test_marks_every_child_as_autopilot(self) -> None:
        env = autopilot._child_env({})
        self.assertEqual(env["ASL_INVOCATION_ORIGIN"], "autopilot")
        self.assertEqual(autopilot.INVOCATION_ORIGIN, "autopilot")

    def test_parsed_assignments_are_overlaid_on_the_environment(self) -> None:
        env = autopilot._child_env({"env": {"CLAUDE_CONFIG_DIR": "/tmp/config"}})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/tmp/config")
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_supervised_command_cannot_relabel_the_origin(self) -> None:
        env = autopilot._child_env(
            {"env": {"ASL_INVOCATION_ORIGIN": "direct", "FOO": "bar"}}
        )
        self.assertEqual(env["ASL_INVOCATION_ORIGIN"], "autopilot")
        self.assertEqual(env["FOO"], "bar")

    def test_an_inherited_origin_is_overridden(self) -> None:
        with mock.patch.dict(os.environ, {"ASL_INVOCATION_ORIGIN": "workbench"}):
            env = autopilot._child_env({})
        self.assertEqual(env["ASL_INVOCATION_ORIGIN"], "autopilot")


class RunAutopilotParseIntegrationTest(unittest.TestCase):
    def test_valid_command_supervises_and_attaches_parsed_model(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(["--command", "asl run /tmp/plan"])
        captured = {}

        def fake_supervisor(parsed):
            captured["parsed"] = parsed
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
            }

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
            parser.parse_args(["--command", "asl run /tmp/plan", "--wait-quota-reset"]),
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
                "asl run /tmp/plan --review-harness codex --max-review-fix-cycles 1",
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
        self.assertEqual(run_args[run_args.index("--max-review-fix-cycles") + 1], "1")


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
            return {
                "ok": False,
                "stage": autopilot.RUN_STAGE,
                "reason": "paused",
                "chunks": 1,
            }

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

    def test_aborted_run_is_terminal_and_skips_question_context(self) -> None:
        # An `asl control abort` stop takes the same terminal pass-through: it is
        # a clean early stop, not a pause, so nothing fetches questions or a
        # review verdict, and `stop_kind` survives to the caller.
        def aborted_supervisor(parsed):
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "stopped",
                "stop_kind": autopilot.STOP_KIND_ABORT,
                "chunks": 1,
                "completed": 2,
                "pending": 5,
            }

        def fail_questioner(parsed):  # pragma: no cover - must not run
            raise AssertionError("asl questions must not be fetched for an abort")

        def fail_review_reader(parsed):  # pragma: no cover - must not run
            raise AssertionError("no review verdict exists for an early stop")

        result = autopilot.run_autopilot(
            self._args(command="asl run /tmp/plan --review-harness codex"),
            supervisor=aborted_supervisor,
            questioner=fail_questioner,
            review_reader=fail_review_reader,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["stop_kind"], autopilot.STOP_KIND_ABORT)
        self.assertNotIn("questions", result)
        self.assertNotIn("review", result)

    def test_reply_stdin_streams_reply_into_supervisor(self) -> None:
        captured = {}

        def reply_supervisor(parsed, *, reply_text=None):
            captured["reply_text"] = reply_text
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "chunks": 1,
            }

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


if __name__ == "__main__":
    unittest.main()
