"""Reply resume and stdin reply flow tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import contextlib
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
    write_plan,
    install_fake_codex,
    init_git_repo,
    read_jsonl,
    _contract_block,
)


class RunReplyResumeFlowTests(unittest.TestCase):
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

    def _build_args(
        self,
        plan_dir: Path,
        state_dir: Path,
        *,
        reply: str | None,
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
        )

    def _run_capture(
        self,
        args: argparse.Namespace,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = runner.run_from_args(args)
        return code, out.getvalue(), err.getvalue()

    def test_empty_reply_returns_empty_reply_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(Path(tmp))
            args = self._build_args(plan_dir, state_dir, reply="   \n  ")

            code, out, _err = self._run_capture(args)

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("run", payload["command"])
            self.assertEqual("empty-reply", payload["reason"])
            self.assertFalse(payload["paused"])

    def test_reply_without_paused_run_returns_no_paused_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending", "pending"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            args = self._build_args(
                plan_dir, state_dir, reply="continue with option 1a"
            )

            code, out, _err = self._run_capture(args)

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("run", payload["command"])
            self.assertEqual(qna.REASON_NO_PAUSED_RUN, payload["reason"])

    def test_reply_context_mismatch_when_paused_session_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(
                Path(tmp),
                statuses=("complete", "pending"),
                paused_session_id="1",
            )
            args = self._build_args(plan_dir, state_dir, reply="pick 1a")

            code, out, _err = self._run_capture(args)

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                qna.REASON_REPLY_CONTEXT_MISMATCH, payload["reason"]
            )
            self.assertEqual("1", payload["details"]["paused_session_id"])
            self.assertEqual("2", payload["details"]["next_pending_id"])

    def test_dry_run_with_reply_emits_resolver_prompt_and_reply_invocation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
                tmp_dir,
                last_message_text="Pick 1a or 1b?\n",
            )
            state_path = (
                state.plan_state_dir(plan_dir, state_dir)
                / events.STATE_FILE_NAME
            )
            paused_state = state_path.read_text(encoding="utf-8")
            args = self._build_args(
                plan_dir,
                state_dir,
                reply="use 1a",
                dry_run=True,
                cwd=tmp_dir,
            )

            code, out, _err = self._run_capture(args)

            self.assertEqual(0, code, out)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            planned = payload["planned"]
            self.assertEqual("reply", planned["prompt_kind"])
            self.assertIn(runner.REPLY_CONTEXT_HEADING, planned["prompt"])
            self.assertIn("use 1a", planned["prompt"])
            self.assertIsNotNone(planned["resolver_prompt"])
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                planned["resolver_prompt"],
            )
            reply_source = planned["reply_source"]
            self.assertEqual("run-prev", reply_source["run_id"])
            self.assertEqual("1", reply_source["session_id"])
            self.assertEqual(2, reply_source["turn_index"])
            self.assertEqual(
                str(last_message_path),
                reply_source["last_message_path"],
            )
            self.assertEqual(len("use 1a"), reply_source["reply_length"])
            invocation_argv = planned["invocation"]["argv"]
            self.assertTrue(
                any(
                    runner.REPLY_CONTEXT_HEADING in str(token)
                    for token in invocation_argv
                ),
                invocation_argv,
            )
            self.assertEqual(
                paused_state,
                state_path.read_text(encoding="utf-8"),
            )
            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            self.assertEqual("1", context.session_id)

    def test_dry_run_text_output_includes_resolver_prompt_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = self._seed_paused_plan(tmp_dir)
            args = self._build_args(
                plan_dir,
                state_dir,
                reply="use 1a",
                dry_run=True,
                json_output=False,
                cwd=tmp_dir,
            )

            code, out, _err = self._run_capture(args)

            self.assertEqual(0, code, out)
            self.assertIn("Resolver prompt:", out)
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt", out
            )
            self.assertIn(runner.REPLY_CONTEXT_HEADING, out)

    def test_dry_run_without_reply_keeps_session_prompt_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending", "pending"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            args = self._build_args(
                plan_dir,
                state_dir,
                reply=None,
                dry_run=True,
                cwd=tmp_dir,
            )

            code, out, _err = self._run_capture(args)

            self.assertEqual(0, code, out)
            payload = json.loads(out)
            planned = payload["planned"]
            self.assertEqual("session", planned["prompt_kind"])
            self.assertIsNone(planned["resolver_prompt"])
            self.assertIsNone(planned["reply_source"])
            self.assertNotIn(runner.REPLY_CONTEXT_HEADING, planned["prompt"])

    def test_first_turn_uses_reply_prompt_and_emits_reply_provided_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
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

            args = self._build_args(
                plan_dir, state_dir, reply="use 1a", cwd=tmp_dir
            )

            with mock.patch.object(
                runner.harnesses, "run_harness", side_effect=fake_run_harness
            ) as patched:
                code, out, _err = self._run_capture(args)

            self.assertEqual(0, code, out)
            calls = patched.call_args_list
            self.assertGreaterEqual(len(calls), 1)
            first_prompt = calls[0].kwargs.get("prompt")
            self.assertIsNotNone(first_prompt)
            self.assertIn(runner.REPLY_CONTEXT_HEADING, first_prompt)
            self.assertIn("use 1a", first_prompt)
            if len(calls) >= 2:
                second_prompt = calls[1].kwargs.get("prompt")
                self.assertNotIn(
                    runner.REPLY_CONTEXT_HEADING, second_prompt
                )

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
            self.assertEqual(1, reply_payload["turn_index"])
            self.assertEqual("1", reply_payload["session_id"])
            self.assertEqual(len("use 1a"), reply_payload["reply_length"])
            source = reply_payload["source"]
            self.assertEqual("run-prev", source["run_id"])
            self.assertEqual("1", source["session_id"])
            self.assertEqual(2, source["turn_index"])
            self.assertEqual(
                str(last_message_path), source["last_message_path"]
            )
            self.assertEqual(len("use 1a"), source["reply_length"])

            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("reply", first_turn["prompt_kind"])
            self.assertEqual("run-prev", first_turn["reply_source"]["run_id"])
            if len(state_payload["turns"]) >= 2:
                self.assertEqual(
                    "session", state_payload["turns"][1]["prompt_kind"]
                )
                self.assertNotIn(
                    "reply_source", state_payload["turns"][1]
                )


class RunResumeConfigTests(unittest.TestCase):
    """`asl run --resume` rehydrates flags from the persisted run_config."""

    def _full_run_config(self, tmp_dir: Path, **overrides: object) -> dict:
        config = {
            "harness": "claude",
            "harness_model": "claude-opus-4",
            "review_harness": None,
            "review_harness_model": None,
            "effort": "high",
            "timeout": None,
            "max_sessions": None,
            "max_pending_sessions": None,
            "max_review_fix_cycles": 2,
            "commit_mode": None,
            "unsafe_bypass": True,
            "allow_not_loop_ready": True,
            "note": None,
            "cwd": str(tmp_dir),
            "state_dir_override": None,
        }
        config.update(overrides)
        return config

    def _seed(
        self,
        tmp_dir: Path,
        *,
        run_config: dict | None,
    ) -> tuple[Path, Path]:
        plan_dir, state_dir, _ = RunReplyResumeFlowTests()._seed_paused_plan(
            tmp_dir, last_message_text="Pick 1a or 1b?\n"
        )
        state_path = (
            state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        )
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if run_config is not None:
            payload["run_config"] = run_config
        else:
            payload.pop("run_config", None)
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_dir, state_dir

    def _run(
        self,
        tokens: list[str],
        *,
        stdin_text: str | None = None,
    ) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(tokens)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            if stdin_text is not None:
                stack.enter_context(
                    mock.patch.object(runner.sys, "stdin", io.StringIO(stdin_text))
                )
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = runner.run_from_args(args, run_argv=tokens)
        return code, out.getvalue(), err.getvalue()

    def test_resume_rehydrates_harness_model_and_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir = self._seed(
                tmp_dir,
                run_config=self._full_run_config(
                    tmp_dir, state_dir_override=str(tmp_dir / "state")
                ),
            )
            # state_dir_override is informational; the explicit --state-dir is
            # what locates the paused state. cwd is left to rehydrate.
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(state_dir),
                "--reply",
                "use 1a",
                "--dry-run",
                "--json",
            ]

            code, out, err = self._run(tokens)

            self.assertEqual(0, code, err or out)
            planned = json.loads(out)["planned"]
            # Harness rehydrated from run_config, not reset to the codex default.
            self.assertEqual("claude", planned["invocation"]["argv"][0])
            self.assertEqual("high", planned["effort"])
            self.assertEqual("claude-opus-4", planned["model"])

    def test_resume_explicit_flag_overrides_run_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, _ = self._seed(
                tmp_dir, run_config=self._full_run_config(tmp_dir)
            )
            state_dir_arg = str(tmp_dir / "state")
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--harness",
                "codex",
                "--state-dir",
                state_dir_arg,
                "--cwd",
                str(tmp_dir),
                "--reply",
                "use 1a",
                "--dry-run",
                "--json",
            ]

            code, out, err = self._run(tokens)

            self.assertEqual(0, code, err or out)
            planned = json.loads(out)["planned"]
            # Explicit --harness wins over the persisted claude harness, while
            # the un-passed effort still rehydrates.
            self.assertEqual("codex", planned["invocation"]["argv"][0])
            self.assertEqual("high", planned["effort"])

    def test_resume_explicit_flag_wins_through_cli_main(self) -> None:
        # `cli.main(argv)` parses the supplied argv but dispatches lazily; the
        # explicit-flag provenance must ride on the namespace so an explicit
        # --harness beats the persisted claude harness without depending on the
        # process-global sys.argv (which, under the test runner, carries no such
        # flag and would otherwise let the persisted value clobber it).
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, _ = self._seed(
                tmp_dir, run_config=self._full_run_config(tmp_dir)
            )
            state_dir_arg = str(tmp_dir / "state")
            argv = [
                "run",
                str(plan_dir),
                "--resume",
                "--harness",
                "codex",
                "--state-dir",
                state_dir_arg,
                "--cwd",
                str(tmp_dir),
                "--reply",
                "use 1a",
                "--dry-run",
                "--json",
            ]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)

            self.assertEqual(0, code, err.getvalue() or out.getvalue())
            planned = json.loads(out.getvalue())["planned"]
            # Explicit --harness wins over the persisted claude harness; the
            # un-passed effort still rehydrates from run_config.
            self.assertEqual("codex", planned["invocation"]["argv"][0])
            self.assertEqual("high", planned["effort"])

    def test_resume_review_harness_model_override_succeeds_with_persisted_harness(
        self,
    ) -> None:
        # A model-only resume override (--review-harness-model with no
        # --review-harness) succeeds because the runner overlays the persisted
        # review harness before validating: `_apply_resume_config` runs ahead of
        # `_validate_run_inputs`, so the cross-field check sees the effective
        # config. This is the runner side of the supervisor parity the helper's
        # deferred-validation path preserves for the same command.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, _ = self._seed(
                tmp_dir,
                run_config=self._full_run_config(
                    tmp_dir, review_harness="codex"
                ),
            )
            state_dir_arg = str(tmp_dir / "state")
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--review-harness-model",
                "gpt-5-codex",
                "--state-dir",
                state_dir_arg,
                "--cwd",
                str(tmp_dir),
                "--reply",
                "use 1a",
                "--dry-run",
                "--json",
            ]

            code, out, err = self._run(tokens)

            # No input-error: the review harness rehydrates from run_config and
            # the explicit model-only override is accepted post-overlay.
            self.assertEqual(0, code, err or out)
            planned = json.loads(out)["planned"]
            self.assertEqual("claude", planned["invocation"]["argv"][0])

    def test_resume_unavailable_on_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, _ = self._seed(tmp_dir, run_config=None)
            state_dir_arg = str(tmp_dir / "state")
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                state_dir_arg,
                "--reply",
                "use 1a",
                "--json",
            ]

            code, out, _err = self._run(tokens)

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("resume-unavailable", payload["reason"])
            command = payload["resume_command"]
            self.assertTrue(command.startswith("asl run "))
            # Reconstructed best-effort from the fields legacy state persists.
            self.assertIn("--harness codex", command)
            self.assertIn("--effort high", command)
            self.assertIn(state_dir_arg, command)
            self.assertEqual(command, payload["details"]["resume_command"])

    def test_resume_unavailable_when_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            write_plan(plan_dir, ("pending", "pending"))
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(tmp_dir / "state"),
                "--json",
            ]

            code, out, _err = self._run(tokens)

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual("resume-unavailable", payload["reason"])
            self.assertTrue(payload["resume_command"].startswith("asl run "))

    def test_resume_with_reply_stdin_combined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, _ = self._seed(
                tmp_dir, run_config=self._full_run_config(tmp_dir)
            )
            state_dir_arg = str(tmp_dir / "state")
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                state_dir_arg,
                "--cwd",
                str(tmp_dir),
                "--reply-stdin",
                "--dry-run",
                "--json",
            ]

            code, out, err = self._run(tokens, stdin_text="use 1a")

            self.assertEqual(0, code, err or out)
            planned = json.loads(out)["planned"]
            # Rehydration and stdin reply coexist: the harness is restored and
            # the answer is read from stdin (never argv).
            self.assertEqual("claude", planned["invocation"]["argv"][0])
            self.assertEqual("reply", planned["prompt_kind"])
            self.assertEqual("stdin", planned["reply_source"]["origin"])
            self.assertIn("use 1a", planned["prompt"])

    def test_run_persists_run_config_in_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = RunReplyResumeFlowTests()._seed_paused_plan(
                tmp_dir
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
                    argv=[name],
                    returncode=0,
                    stdout="ok\n",
                    stderr="",
                )

            tokens = [
                "run",
                str(plan_dir),
                "--harness",
                "claude",
                "--effort",
                "high",
                "--harness-model",
                "claude-opus-4",
                "--note",
                "ref-1",
                "--max-pending-sessions",
                "5",
                "--allow-not-loop-ready",
                "--state-dir",
                str(state_dir),
                "--cwd",
                str(tmp_dir),
                "--reply",
                "use 1a",
                "--json",
            ]
            args = cli.build_parser().parse_args(tokens)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(
                runner.harnesses, "run_harness", side_effect=fake_run_harness
            ):
                with redirect_stdout(out), redirect_stderr(err):
                    code = runner.run_from_args(args)

            self.assertEqual(0, code, err.getvalue() or out.getvalue())
            state_path = (
                state.plan_state_dir(plan_dir, state_dir)
                / events.STATE_FILE_NAME
            )
            run_config = json.loads(state_path.read_text(encoding="utf-8"))[
                "run_config"
            ]
            self.assertEqual("claude", run_config["harness"])
            self.assertEqual("claude-opus-4", run_config["harness_model"])
            self.assertEqual("high", run_config["effort"])
            self.assertEqual("ref-1", run_config["note"])
            self.assertEqual(5, run_config["max_pending_sessions"])
            self.assertTrue(run_config["allow_not_loop_ready"])
            self.assertTrue(run_config["unsafe_bypass"])
            self.assertEqual(
                str(plans.canonicalize_path(tmp_dir)), run_config["cwd"]
            )
            self.assertEqual(str(state_dir), run_config["state_dir_override"])


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
        with mock.patch.object(runner.sys, "stdin", fake_stdin):
            with redirect_stdout(out), redirect_stderr(err):
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
            seeder = RunReplyResumeFlowTests()
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
            seeder = RunReplyResumeFlowTests()
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
            seeder = RunReplyResumeFlowTests()
            plan_dir, state_dir, _ = seeder._seed_paused_plan(
                Path(tmp),
                statuses=("complete", "pending"),
                paused_session_id="1",
            )
            args = self._build_args(plan_dir, state_dir, reply_stdin=True)

            code, out, _err = self._run_with_stdin(args, "pick 1a")

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual(
                qna.REASON_REPLY_CONTEXT_MISMATCH, payload["reason"]
            )

    def test_reply_stdin_missing_question_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            seeder = RunReplyResumeFlowTests()
            plan_dir, state_dir, last_message_path = seeder._seed_paused_plan(
                tmp_dir
            )
            last_message_path.unlink()
            args = self._build_args(plan_dir, state_dir, reply_stdin=True)

            code, out, _err = self._run_with_stdin(args, "pick 1a")

            self.assertEqual(1, code)
            payload = json.loads(out)
            self.assertEqual(
                qna.REASON_MISSING_QUESTION_CONTEXT, payload["reason"]
            )

    def test_reply_stdin_dry_run_marks_stdin_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            seeder = RunReplyResumeFlowTests()
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
            seeder = RunReplyResumeFlowTests()
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
                runner.harnesses, "run_harness", side_effect=fake_run_harness
            ) as patched:
                code, out, _err = self._run_with_stdin(args, reply_text)

            self.assertEqual(0, code, out)
            first_prompt = patched.call_args_list[0].kwargs.get("prompt")
            self.assertIn(runner.REPLY_CONTEXT_HEADING, first_prompt)
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
            self.assertEqual(
                str(last_message_path), source["last_message_path"]
            )
            # Reply text must never leak into any persisted artifact.
            self.assertNotIn(reply_text, json.dumps(reply_events[0]))

            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("reply", first_turn["prompt_kind"])
            self.assertEqual("stdin", first_turn["reply_source"]["origin"])
            for token in first_turn["argv"]:
                self.assertNotIn(reply_text, str(token))


class EndToEndReplyResumeFlowTests(unittest.TestCase):
    """Subprocess-level coverage for the pause → questions → reply → resume flow.

    Unit tests already cover empty-reply, no-paused-run, reply-context-mismatch,
    and the dry-run shape via ``runner.run_from_args`` with a mocked harness.
    These tests exercise the same flow through ``python -m agent_session_loop``
    so the CLI wiring, fake harness, paused JSON help, and resume payload all
    compose correctly at the process boundary the docs describe.
    """

    REPLY_TEXT = "1a, 2b, 3a"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...] = ("pending",),
        *,
        contracts_by_id: dict[str, str] | None = None,
    ) -> None:
        if contracts_by_id is None:
            contracts_by_id = {
                str(index): _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    f'"name": "readme-{index}"}}',
                    session=str(index),
                )
                for index in range(1, len(statuses) + 1)
            }
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)

    def _base_env(self, mode: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_STEP_SLEEP": "0",
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
            }
        )
        return env

    def _run_run(
        self,
        *,
        mode: str,
        reply: str | None = None,
        dry_run: bool = False,
        json_output: bool = True,
        allow_not_loop_ready: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if json_output:
            argv.append("--json")
        if dry_run:
            argv.append("--dry-run")
        if reply is not None:
            argv.extend(["--reply", reply])
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self._base_env(mode),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        payload = json.loads(completed.stdout) if json_output else {}
        return completed, payload

    def _run_questions(
        self,
        *,
        json_output: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "questions",
            str(self.plan_dir),
            "--state-dir",
            str(self.state_dir),
        ]
        if json_output:
            argv.append("--json")
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self._base_env("reply-aware"),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if json_output:
            return completed, json.loads(completed.stdout)
        return completed, completed.stdout

    def test_run_pauses_with_blocked_reason_and_interactive_help_payload(
        self,
    ) -> None:
        self._write_plan()

        completed, payload = self._run_run(mode="reply-aware")

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("blocked", payload["pause_reason"])
        help_payload = payload["interactive_help"]
        self.assertIsInstance(help_payload, dict)
        self.assertEqual(
            f"asl questions {self.plan_dir} --state-dir {self.state_dir}",
            help_payload["questions_command"],
        )
        self.assertEqual(
            f"asl run {self.plan_dir} --resume "
            f"--state-dir {self.state_dir} --reply TEXT",
            help_payload["reply_command"],
        )
        self.assertEqual("1", help_payload["session_id"])
        self.assertEqual("1", help_payload["next_pending_id"])
        rendered = help_payload["rendered_prompt"]
        self.assertIn(
            "# Paused Agent Session - Resolver Prompt", rendered
        )
        self.assertIn("cursor-based pagination", rendered)
        session_text = (self.plan_dir / "session-1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- **Status**: pending", session_text)

    def test_questions_command_renders_resolver_prompt_with_questions(
        self,
    ) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])

        questions_completed, questions_payload = self._run_questions()
        assert isinstance(questions_payload, dict)

        self.assertEqual(
            0, questions_completed.returncode, questions_completed.stderr
        )
        self.assertTrue(questions_payload["ok"])
        self.assertEqual("questions", questions_payload["command"])
        self.assertEqual("1", questions_payload["session_id"])
        self.assertEqual("1", questions_payload["next_pending_id"])
        rendered = questions_payload["rendered_prompt"]
        self.assertIn("# Paused Agent Session - Resolver Prompt", rendered)
        self.assertIn("## Previous Agent Message", rendered)
        self.assertIn("cursor-based pagination", rendered)
        self.assertIn("(e.g., `1a, 2a, 3b`)", rendered)
        last_message = questions_payload["last_message"]
        self.assertIn("cursor-based pagination", last_message)
        self.assertIn(
            "RUNNER_PAUSE: blocked - interactive decision gates require user answers",
            last_message,
        )

    def test_questions_text_output_prints_resolver_prompt(self) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])

        text_completed, text_output = self._run_questions(json_output=False)
        assert isinstance(text_output, str)

        self.assertEqual(0, text_completed.returncode, text_completed.stderr)
        self.assertIn("# Paused Agent Session - Resolver Prompt", text_output)
        self.assertIn("cursor-based pagination", text_output)
        self.assertIn("`asl run --resume --reply`", text_output)

    def test_run_with_reply_resumes_and_completes_paused_session(self) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware", reply=self.REPLY_TEXT
        )

        self.assertEqual(
            0, resume_completed.returncode, resume_completed.stderr
        )
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])
        session_text = (self.plan_dir / "session-1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- **Status**: complete", session_text)
        # The first persisted turn should record the reply prompt kind, while
        # the operator reply text is redacted at the persistence boundary.
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_payload = json.loads(
            (plan_state / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )
        first_turn = state_payload["turns"][0]
        self.assertEqual("reply", first_turn["prompt_kind"])
        for token in first_turn["argv"]:
            self.assertNotIn(self.REPLY_TEXT, str(token))
            self.assertNotIn(runner.REPLY_CONTEXT_HEADING, str(token))
        # The structured reply event is emitted with only metadata, not the
        # operator answer text.
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        reply_events = [
            record
            for record in records
            if record.get("name") == events.REPLY_PROVIDED_EVENT
        ]
        self.assertEqual(1, len(reply_events))
        self.assertEqual(
            len(self.REPLY_TEXT), reply_events[0]["payload"]["reply_length"]
        )
        self.assertNotIn(self.REPLY_TEXT, json.dumps(reply_events[0]))

    def test_dry_run_reply_preview_preserves_paused_state_for_resume(self) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_path = plan_state / events.STATE_FILE_NAME
        events_path = plan_state / events.EVENTS_FILE_NAME
        paused_state = state_path.read_text(encoding="utf-8")
        paused_events = events_path.read_text(encoding="utf-8")

        dry_completed, dry_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
            dry_run=True,
        )

        self.assertEqual(0, dry_completed.returncode, dry_completed.stderr)
        self.assertTrue(dry_payload["ok"])
        self.assertEqual("dry-run", dry_payload["reason"])
        self.assertEqual("reply", dry_payload["planned"]["prompt_kind"])
        self.assertEqual(
            paused_state,
            state_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            paused_events,
            events_path.read_text(encoding="utf-8"),
        )
        context = qna.load_qna_context(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("1", context.session_id)

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
        )

        self.assertEqual(
            0, resume_completed.returncode, resume_completed.stderr
        )
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])

    def test_reply_resume_after_auto_repair_fixes_readiness_and_succeeds(
        self,
    ) -> None:
        # The first run is allow-not-loop-ready so the missing readiness
        # metadata does not block the initial pause on the operator
        # question. The reply attempt must run with the gate enforced
        # so the runner-side auto-repair fires before consuming the
        # paused reply context.
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])
        self.assertIsNone(plans.read_readiness_metadata(self.plan_dir))

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
            allow_not_loop_ready=False,
        )

        self.assertEqual(
            0, resume_completed.returncode, resume_completed.stderr
        )
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])
        repair_payload = resume_payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        self.assertEqual(
            "loop-ready", repair_payload["auto_repairs"][0]["target_state"]
        )
        self.assertEqual("reply", resume_payload["turns"][0]["prompt_kind"])
        session_text = (self.plan_dir / "session-1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- **Status**: complete", session_text)
        # The reply was actually consumed: only metadata, not the operator
        # text, leaks into the persisted reply event.
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        reply_events = [
            record
            for record in records
            if record.get("name") == events.REPLY_PROVIDED_EVENT
        ]
        self.assertEqual(1, len(reply_events))
        self.assertNotIn(self.REPLY_TEXT, json.dumps(reply_events[0]))

    def test_failed_reply_readiness_gate_preserves_paused_context_for_retry(
        self,
    ) -> None:
        self._write_plan()
        # Inject a yellow ambiguity sentinel so the readiness gate retains a
        # non-repairable blocker; otherwise the runner-side auto-repair would
        # rebuild the missing readiness metadata and let the reply through.
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.", "Instructions. TODO: revisit later"
            ),
            encoding="utf-8",
        )
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_path = plan_state / events.STATE_FILE_NAME
        events_path = plan_state / events.EVENTS_FILE_NAME
        paused_state = state_path.read_bytes()
        paused_events = events_path.read_bytes()

        failed_completed, failed_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
            allow_not_loop_ready=False,
        )

        self.assertEqual(1, failed_completed.returncode, failed_completed.stderr)
        self.assertFalse(failed_payload["ok"])
        self.assertTrue(failed_payload["paused"])
        self.assertEqual("not-loop-ready", failed_payload["pause_reason"])
        self.assertEqual(paused_state, state_path.read_bytes())
        self.assertEqual(paused_events, events_path.read_bytes())
        context = qna.load_qna_context(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("1", context.session_id)

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
        )

        self.assertEqual(
            0, resume_completed.returncode, resume_completed.stderr
        )
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])

    def test_run_with_reply_rejects_stale_paused_context(self) -> None:
        # Plan session 1 is already complete, but the paused state.json still
        # points at session 1 - the reply must be rejected with the stable
        # reply-context-mismatch reason, not silently replayed.
        self._write_plan(statuses=("complete", "pending"))
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        last_message_path = self.tmp_path / "last-message.txt"
        last_message_path.write_text(
            "Should we ship now? RUNNER_PAUSE: blocked - need answer\n",
            encoding="utf-8",
        )
        turn = {
            "run_id": "run-stale",
            "turn_index": 1,
            "session_id": "1",
            "session": 1,
            "session_number": 1,
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": None,
            "effort_source": "unresolved",
            "artifact_dir": str(plan_state / "runs" / "run-stale" / "turn-0001"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        state_payload = {
            "ok": False,
            "paused": True,
            "reason": "runner-pause",
            "pause_reason": "blocked",
            "pause_message": "blocked on operator answers",
            "pause_details": {"session": 1},
            "details": {"session": 1},
            "run_id": "run-stale",
            "run_dir": str(plan_state / "runs" / "run-stale"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": None,
            "turns": [turn],
        }
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(state_payload), encoding="utf-8"
        )

        completed, payload = self._run_run(
            mode="reply-aware", reply=self.REPLY_TEXT
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("run", payload["command"])
        self.assertEqual(
            qna.REASON_REPLY_CONTEXT_MISMATCH, payload["reason"]
        )
        self.assertEqual("1", payload["details"]["paused_session_id"])
        self.assertEqual("2", payload["details"]["next_pending_id"])
        # The pending session is not advanced when the reply is rejected.
        session_two = (self.plan_dir / "session-2.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- **Status**: pending", session_two)

    def test_run_with_empty_reply_returns_stable_empty_reply_reason(
        self,
    ) -> None:
        self._write_plan()
        first_completed, _first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)

        completed, payload = self._run_run(
            mode="reply-aware", reply="   \n  "
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("run", payload["command"])
        self.assertEqual("empty-reply", payload["reason"])
        self.assertFalse(payload["paused"])

    def test_paused_json_payload_exposes_stable_scriptable_keys(self) -> None:
        self._write_plan()

        completed, payload = self._run_run(mode="reply-aware")

        self.assertEqual(1, completed.returncode, completed.stderr)
        # Stable top-level keys the docs publish for scripted consumers.
        for key in (
            "ok",
            "command",
            "paused",
            "pause_reason",
            "pause_message",
            "interactive_help",
            "turns",
            "state_path",
            "events_path",
        ):
            self.assertIn(key, payload)
        self.assertEqual("run", payload["command"])
        self.assertEqual("blocked", payload["pause_reason"])
        # interactive_help is the stable shape that operators script against.
        help_payload = payload["interactive_help"]
        for key in (
            "questions_command",
            "reply_command",
            "session_id",
            "next_pending_id",
            "turn_index",
            "artifact_paths",
            "rendered_prompt",
        ):
            self.assertIn(key, help_payload)
        # Artifact paths reuse qna.artifact_paths_payload so consumers can
        # share the shape across asl questions and asl run pause output.
        artifact_keys = {
            "run_dir",
            "artifact_dir",
            "stdout_path",
            "stderr_path",
            "last_message_path",
            "result_path",
        }
        self.assertEqual(
            artifact_keys,
            set(help_payload["artifact_paths"].keys()),
        )


if __name__ == "__main__":
    unittest.main()
