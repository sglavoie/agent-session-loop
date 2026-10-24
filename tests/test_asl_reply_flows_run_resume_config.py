"""`asl run --resume` run_config rehydration tests."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import cli, events, harnesses, plans_files, runner, state
from tests.support import (
    write_plan,
)

ASL_TEST_LANE = "integration"


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
        plan_dir, state_dir, _ = rfs.ReplyResumeFlowCase()._seed_paused_plan(
            tmp_dir, last_message_text="Pick 1a or 1b?\n"
        )
        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
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
                    mock.patch.object(sys, "stdin", io.StringIO(stdin_text))
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
            plan_dir, _ = self._seed(tmp_dir, run_config=self._full_run_config(tmp_dir))
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
            plan_dir, _ = self._seed(tmp_dir, run_config=self._full_run_config(tmp_dir))
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
                run_config=self._full_run_config(tmp_dir, review_harness="codex"),
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
            plan_dir, _ = self._seed(tmp_dir, run_config=self._full_run_config(tmp_dir))
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
            plan_dir, state_dir, _ = rfs.ReplyResumeFlowCase()._seed_paused_plan(
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
            with (
                mock.patch.object(
                    harnesses, "run_harness", side_effect=fake_run_harness
                ),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                code = runner.run_from_args(args)

            self.assertEqual(0, code, err.getvalue() or out.getvalue())
            state_path = (
                state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
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
                str(plans_files.canonicalize_path(tmp_dir)), run_config["cwd"]
            )
            self.assertEqual(str(state_dir), run_config["state_dir_override"])


if __name__ == "__main__":
    unittest.main()
