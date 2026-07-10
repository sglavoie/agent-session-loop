"""Artifact safety and pause UX tests.

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
    write_plan,
    read_jsonl,
)


class ArtifactSafetyAndPauseUxTests(unittest.TestCase):
    def _seed_paused_plan(
        self,
        tmp_dir: Path,
        *,
        last_message_text: str = "Should we use 1a or 1b?\n",
        statuses: tuple[str, ...] = ("pending", "pending"),
        paused_session_id: str = "1",
    ) -> tuple[Path, Path, Path]:
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, statuses)
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        state_path = plan_state / events.STATE_FILE_NAME
        last_message_path = tmp_dir / "last-message.txt"
        last_message_path.write_text(last_message_text, encoding="utf-8")
        turn = {
            "run_id": "run-prev",
            "turn_index": 2,
            "session_id": paused_session_id,
            "session": int(paused_session_id),
            "session_number": int(paused_session_id),
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": "high",
            "effort_source": "flag",
            "artifact_dir": str(plan_state / "runs" / "run-prev" / "turn-0002"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        payload = {
            "ok": False,
            "paused": True,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": int(paused_session_id)},
            "details": {"session": int(paused_session_id)},
            "run_id": "run-prev",
            "run_dir": str(plan_state / "runs" / "run-prev"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": "high",
            "turns": [turn],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_dir, state_dir, last_message_path

    def _paused_args(
        self,
        plan_dir: Path,
        state_dir: Path | None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(plan_dir),
            state_dir=str(state_dir) if state_dir is not None else None,
        )

    def test_questions_command_text_includes_state_dir_when_provided(self) -> None:
        self.assertEqual(
            "asl questions /tmp/plan",
            runner._questions_command_text("/tmp/plan", None),
        )
        self.assertEqual(
            "asl questions /tmp/plan --state-dir /tmp/state",
            runner._questions_command_text("/tmp/plan", "/tmp/state"),
        )

    def test_reply_command_text_includes_state_dir_when_provided(self) -> None:
        self.assertEqual(
            "asl run /tmp/plan --reply TEXT",
            runner._reply_command_text("/tmp/plan", None),
        )
        self.assertEqual(
            "asl run /tmp/plan --state-dir /tmp/state --reply TEXT",
            runner._reply_command_text("/tmp/plan", "/tmp/state"),
        )

    def test_interactive_help_commands_shell_quote_dynamic_paths(self) -> None:
        questions = runner._questions_command_text(
            "/tmp/plan dir",
            "/tmp/state dir",
        )
        reply = runner._reply_command_text(
            "/tmp/plan dir",
            "/tmp/state dir",
        )

        self.assertEqual(
            "asl questions '/tmp/plan dir' --state-dir '/tmp/state dir'",
            questions,
        )
        self.assertEqual(
            "asl run '/tmp/plan dir' --state-dir '/tmp/state dir' --reply TEXT",
            reply,
        )
        self.assertEqual(
            [
                "asl",
                "questions",
                "/tmp/plan dir",
                "--state-dir",
                "/tmp/state dir",
            ],
            shlex.split(questions),
        )
        self.assertEqual(
            [
                "asl",
                "run",
                "/tmp/plan dir",
                "--state-dir",
                "/tmp/state dir",
                "--reply",
                "TEXT",
            ],
            shlex.split(reply),
        )

    def test_attach_interactive_help_populates_payload_when_qna_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
                Path(tmp)
            )
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": True,
                "plan_dir": str(plan_dir),
            }

            runner._attach_interactive_help(args, payload)

            help_payload = payload["interactive_help"]
            self.assertIsInstance(help_payload, dict)
            self.assertEqual(
                runner._questions_command_text(str(plan_dir), str(state_dir)),
                help_payload["questions_command"],
            )
            self.assertEqual(
                runner._reply_command_text(str(plan_dir), str(state_dir)),
                help_payload["reply_command"],
            )
            self.assertEqual("1", help_payload["session_id"])
            self.assertEqual("1", help_payload["next_pending_id"])
            self.assertEqual(2, help_payload["turn_index"])
            self.assertEqual(
                str(last_message_path),
                help_payload["artifact_paths"]["last_message_path"],
            )
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                help_payload["rendered_prompt"],
            )

    def test_attach_interactive_help_omits_when_no_paused_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": True,
                "plan_dir": str(plan_dir),
            }

            runner._attach_interactive_help(args, payload)

            self.assertNotIn("interactive_help", payload)

    def test_attach_interactive_help_skips_non_paused_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(Path(tmp))
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": False,
                "plan_dir": str(plan_dir),
            }

            runner._attach_interactive_help(args, payload)

            self.assertNotIn("interactive_help", payload)

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
                "questions_command": (
                    "asl questions /tmp/plan --state-dir /tmp/state"
                ),
            },
        }
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            runner._print_text_outcome(payload)

        err_text = err.getvalue()
        self.assertIn(
            "Last message: /tmp/plan/.state/last-message.txt", err_text
        )
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
            runner._print_text_outcome(payload)

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
                runner.harnesses, "run_harness", side_effect=fake_run_harness
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = runner.run_from_args(args)
            self.assertEqual(0, code, out.getvalue())

            placeholder = runner._reply_argv_placeholder(len(reply_text))

            plan_state = state.plan_state_dir(plan_dir, state_dir)
            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("reply", first_turn["prompt_kind"])
            for token in first_turn["argv"]:
                self.assertNotIn(reply_text, str(token))
                self.assertNotIn(runner.REPLY_CONTEXT_HEADING, str(token))
            self.assertIn(placeholder, first_turn["argv"])

            result_path = Path(first_turn["result_path"])
            per_turn = json.loads(result_path.read_text(encoding="utf-8"))
            for token in per_turn["argv"]:
                self.assertNotIn(reply_text, str(token))
            self.assertIn(placeholder, per_turn["argv"])

            events_path = plan_state / events.EVENTS_FILE_NAME
            records = read_jsonl(events_path)
            turn_started = [
                record
                for record in records
                if record.get("name") == "turn_started"
            ]
            self.assertTrue(turn_started)
            first_turn_started = turn_started[0]["payload"]
            self.assertEqual(placeholder, first_turn_started["prompt"])
            self.assertNotIn(reply_text, first_turn_started["prompt"])

            turn_finished = [
                record
                for record in records
                if record.get("name") == "turn_finished"
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
                runner.harnesses, "run_harness", side_effect=fake_run_harness
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
