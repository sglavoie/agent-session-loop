"""`--reply-stdin` flow tests."""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import cli, events, harnesses, qna, run_prompts, runner, state
from tests.support import (
    REPO_ROOT,
    read_jsonl,
    write_plan,
)

ASL_TEST_LANE = "integration"


class RunReplyStdinFlowTests(unittest.TestCase):
    """`--reply-stdin` should match `--reply TEXT` except for argv exposure."""

    def _build_args(
        self,
        plan_dir: Path,
        state_dir: Path,
        *,
        reply: str | None = None,
        reply_stdin: bool = False,
        dry_run: bool = False,
        json_output: bool = True,
        cwd: Path | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(plan_dir),
            cwd=str(cwd if cwd is not None else REPO_ROOT),
            harness="codex",
            review_harness=None,
            state_dir=str(state_dir),
            json=json_output,
            dry_run=dry_run,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
            reply=reply,
            reply_stdin=reply_stdin,
        )

    def _run_with_stdin(
        self,
        args: argparse.Namespace,
        stdin_text: str,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = io.StringIO(stdin_text)
        with (
            mock.patch.object(sys, "stdin", fake_stdin),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = runner.run_from_args(args)
        return code, out.getvalue(), err.getvalue()

    def test_reply_stdin_and_reply_are_mutually_exclusive_at_cli(self) -> None:
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "run",
                    "/tmp/plan",
                    "--reply",
                    "argv-text",
                    "--reply-stdin",
                ]
            )

    def test_empty_stdin_reply_returns_empty_reply_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeder = rfs.ReplyResumeFlowCase()
            plan_dir, state_dir, _ = seeder._seed_paused_plan(Path(tmp))
            args = self._build_args(plan_dir, state_dir, reply_stdin=True)

            code, out, _err = self._run_with_stdin(args, "   \n  ")

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("empty-reply", payload["reason"])
            self.assertIn("--reply-stdin", payload["error"])

    def test_conflicting_reply_and_reply_stdin_programmatic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeder = rfs.ReplyResumeFlowCase()
            plan_dir, state_dir, _ = seeder._seed_paused_plan(Path(tmp))
            args = self._build_args(
                plan_dir,
                state_dir,
                reply="argv-version",
                reply_stdin=True,
            )

            code, out, _err = self._run_with_stdin(args, "stdin-version")

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("conflicting-reply", payload["reason"])

    def test_reply_stdin_without_paused_run_returns_no_paused_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending", "pending"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            args = self._build_args(plan_dir, state_dir, reply_stdin=True)

            code, out, _err = self._run_with_stdin(args, "continue with option 1a")

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual(qna.REASON_NO_PAUSED_RUN, payload["reason"])

    def test_reply_stdin_context_mismatch_when_paused_session_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeder = rfs.ReplyResumeFlowCase()
            plan_dir, state_dir, _ = seeder._seed_paused_plan(
                Path(tmp),
                statuses=("complete", "pending"),
                paused_session_id="1",
            )
            args = self._build_args(plan_dir, state_dir, reply_stdin=True)

            code, out, _err = self._run_with_stdin(args, "pick 1a")

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual(qna.REASON_REPLY_CONTEXT_MISMATCH, payload["reason"])

    def test_reply_stdin_missing_question_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            seeder = rfs.ReplyResumeFlowCase()
            plan_dir, state_dir, last_message_path = seeder._seed_paused_plan(tmp_dir)
            last_message_path.unlink()
            args = self._build_args(plan_dir, state_dir, reply_stdin=True)

            code, out, _err = self._run_with_stdin(args, "pick 1a")

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, payload["reason"])

    def test_reply_stdin_dry_run_marks_stdin_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            seeder = rfs.ReplyResumeFlowCase()
            plan_dir, state_dir, _ = seeder._seed_paused_plan(tmp_dir)
            args = self._build_args(
                plan_dir,
                state_dir,
                reply_stdin=True,
                dry_run=True,
                cwd=tmp_dir,
            )

            code, out, _err = self._run_with_stdin(args, "use 1a")

            self.assertEqual(0, code, out)
            payload = json.loads(out)
            planned = payload["planned"]
            self.assertEqual("reply", planned["prompt_kind"])
            reply_source = planned["reply_source"]
            self.assertEqual(len("use 1a"), reply_source["reply_length"])
            self.assertEqual("stdin", reply_source["origin"])

    def test_reply_stdin_first_turn_matches_reply_argv_except_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            seeder = rfs.ReplyResumeFlowCase()
            plan_dir, state_dir, last_message_path = seeder._seed_paused_plan(
                tmp_dir,
                last_message_text="Pick 1a or 1b?\n",
            )

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

            reply_text = "use 1a stdin-secret"
            args = self._build_args(
                plan_dir,
                state_dir,
                reply_stdin=True,
                cwd=tmp_dir,
            )

            with mock.patch.object(
                harnesses, "run_harness", side_effect=fake_run_harness
            ) as patched:
                code, out, _err = self._run_with_stdin(args, reply_text)

            self.assertEqual(0, code, out)
            first_prompt = patched.call_args_list[0].kwargs.get("prompt")
            self.assertIn(run_prompts.REPLY_CONTEXT_HEADING, first_prompt)
            self.assertIn(reply_text, first_prompt)

            plan_state = state.plan_state_dir(plan_dir, state_dir)
            events_path = plan_state / events.EVENTS_FILE_NAME
            records = read_jsonl(events_path)
            reply_events = [
                record
                for record in records
                if record.get("name") == events.REPLY_PROVIDED_EVENT
            ]
            self.assertEqual(1, len(reply_events), records)
            reply_payload = reply_events[0]["payload"]
            self.assertEqual(len(reply_text), reply_payload["reply_length"])
            source = reply_payload["source"]
            self.assertEqual("stdin", source["origin"])
            self.assertEqual(len(reply_text), source["reply_length"])
            self.assertEqual(str(last_message_path), source["last_message_path"])
            # Reply text must never leak into any persisted artifact.
            self.assertNotIn(reply_text, json.dumps(reply_events[0]))

            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("reply", first_turn["prompt_kind"])
            self.assertEqual("stdin", first_turn["reply_source"]["origin"])
            for token in first_turn["argv"]:
                self.assertNotIn(reply_text, str(token))


if __name__ == "__main__":
    unittest.main()
