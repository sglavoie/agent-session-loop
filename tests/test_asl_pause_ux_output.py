"""Test asl pause ux output."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    run_outcome,
    run_prompts,
    runner,
    state,
    turn_execution_argv,
)
from tests import support_asl_pause_ux as fixtures
from tests.support import (
    init_git_repo,
    read_jsonl,
    write_plan,
)


class ArtifactSafetyAndPauseUxTests(fixtures.ArtifactSafetyAndPauseUxCase):
    def test_paused_text_output_prints_questions_hint_after_last_message(
        self,
    ) -> None:
        payload = {
            "ok": False,
            "paused": True,
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "plan_dir": "/tmp/plan",
            "harness": None,
            "completed_sessions": [],
            "commits": [],
            "turns": [
                {
                    "stdout_path": "/tmp/plan/.state/stdout.log",
                    "stderr_path": "/tmp/plan/.state/stderr.log",
                    "last_message_path": "/tmp/plan/.state/last-message.txt",
                }
            ],
            "interactive_help": {
                "questions_command": ("asl questions /tmp/plan --state-dir /tmp/state"),
            },
        }
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            run_outcome._print_text_outcome(payload)

        err_text = err.getvalue()
        self.assertIn("Last message: /tmp/plan/.state/last-message.txt", err_text)
        self.assertIn(
            "Questions: asl questions /tmp/plan --state-dir /tmp/state",
            err_text,
        )
        last_message_position = err_text.index("Last message:")
        questions_position = err_text.index("Questions:")
        self.assertLess(last_message_position, questions_position)

    def test_paused_text_output_omits_questions_hint_without_help(self) -> None:
        payload = {
            "ok": False,
            "paused": True,
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "plan_dir": "/tmp/plan",
            "harness": None,
            "completed_sessions": [],
            "commits": [],
            "turns": [
                {
                    "stdout_path": "/tmp/plan/.state/stdout.log",
                    "stderr_path": "/tmp/plan/.state/stderr.log",
                    "last_message_path": "/tmp/plan/.state/last-message.txt",
                }
            ],
        }
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            run_outcome._print_text_outcome(payload)

        self.assertNotIn("Questions:", err.getvalue())

    def test_reply_turn_redacts_full_prompt_in_persisted_argv_and_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = ArtifactSafetyAndPauseUxTests._seed_paused_plan(
                self,
                tmp_dir,
                last_message_text="Pick 1a or 1b?\n",
            )
            reply_text = "use 1a verbatim secret"

            def fake_run_harness(name, **kwargs):
                plan = Path(kwargs["plan_dir"])
                for session_file in sorted(plan.glob("session-*.md")):
                    text = session_file.read_text(encoding="utf-8")
                    if "- **Status**: pending" in text:
                        session_file.write_text(
                            text.replace(
                                "- **Status**: pending",
                                "- **Status**: complete",
                                1,
                            ),
                            encoding="utf-8",
                        )
                        break
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, "--prompt", str(kwargs.get("prompt", ""))],
                    returncode=0,
                    stdout="ok\n",
                    stderr="",
                )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(tmp_dir),
                harness="codex",
                review_harness=None,
                state_dir=str(state_dir),
                json=True,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
                reply=reply_text,
            )

            with mock.patch.object(
                harnesses, "run_harness", side_effect=fake_run_harness
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = runner.run_from_args(args)
            self.assertEqual(0, code, out.getvalue())

            placeholder = turn_execution_argv.reply_argv_placeholder(len(reply_text))

            plan_state = state.plan_state_dir(plan_dir, state_dir)
            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("reply", first_turn["prompt_kind"])
            for token in first_turn["argv"]:
                self.assertNotIn(reply_text, str(token))
                self.assertNotIn(run_prompts.REPLY_CONTEXT_HEADING, str(token))
            self.assertIn(placeholder, first_turn["argv"])

            result_path = Path(first_turn["result_path"])
            per_turn = json.loads(result_path.read_text(encoding="utf-8"))
            for token in per_turn["argv"]:
                self.assertNotIn(reply_text, str(token))
            self.assertIn(placeholder, per_turn["argv"])

            events_path = plan_state / events.EVENTS_FILE_NAME
            records = read_jsonl(events_path)
            turn_started = [
                record for record in records if record.get("name") == "turn_started"
            ]
            self.assertTrue(turn_started)
            first_turn_started = turn_started[0]["payload"]
            self.assertEqual(placeholder, first_turn_started["prompt"])
            self.assertNotIn(reply_text, first_turn_started["prompt"])

            turn_finished = [
                record for record in records if record.get("name") == "turn_finished"
            ]
            self.assertTrue(turn_finished)
            first_turn_finished = turn_finished[0]["payload"]
            for token in first_turn_finished["argv"]:
                self.assertNotIn(reply_text, str(token))

            reply_events = [
                record
                for record in records
                if record.get("name") == events.REPLY_PROVIDED_EVENT
            ]
            self.assertEqual(1, len(reply_events))
            self.assertEqual(
                len(reply_text),
                reply_events[0]["payload"]["reply_length"],
            )
            self.assertNotIn(reply_text, json.dumps(reply_events[0]))

    def test_non_reply_turn_argv_is_not_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)

            captured_prompts: list[str] = []

            def fake_run_harness(name, **kwargs):
                captured_prompts.append(str(kwargs.get("prompt", "")))
                plan = Path(kwargs["plan_dir"])
                for session_file in sorted(plan.glob("session-*.md")):
                    text = session_file.read_text(encoding="utf-8")
                    if "- **Status**: pending" in text:
                        session_file.write_text(
                            text.replace(
                                "- **Status**: pending",
                                "- **Status**: complete",
                                1,
                            ),
                            encoding="utf-8",
                        )
                        break
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, "--prompt", str(kwargs.get("prompt", ""))],
                    returncode=0,
                    stdout="ok\n",
                    stderr="",
                )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(tmp_dir),
                harness="codex",
                review_harness=None,
                state_dir=str(state_dir),
                json=True,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
                reply=None,
            )

            with mock.patch.object(
                harnesses, "run_harness", side_effect=fake_run_harness
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = runner.run_from_args(args)

            self.assertEqual(0, code, out.getvalue())
            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("session", first_turn["prompt_kind"])
            self.assertEqual(captured_prompts[0], first_turn["argv"][-1])
            self.assertNotIn(
                "<asl reply prompt redacted",
                json.dumps(first_turn["argv"]),
            )


if __name__ == "__main__":
    unittest.main()
