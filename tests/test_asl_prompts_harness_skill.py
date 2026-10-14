"""Harness invocation prompt override and session-run skill reply tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_session_loop import (
    claude_stream,
    copilot_acp,
    harnesses,
    plans_files,
    run_prompts,
)
from tests.support import (
    FAKE_CODEX_FIXTURE,
    REPO_ROOT,
    write_plan,
)


class HarnessInvocationPromptOverrideTests(unittest.TestCase):
    """The prompt an invocation is built from reaches the harness.

    Codex runs over the app-server, where the prompt travels the JSON-RPC
    conversation instead of argv, so for that harness the assertion is made
    against the legacy one-shot invocation built from the same prompt -- and
    the app-server invocation is checked to carry the prompt nowhere at all.
    """

    def _codex_exec_argv(self, **kwargs: object) -> list[str]:
        adapter = harnesses.get_adapter("codex")
        return adapter.build_exec_invocation(**kwargs).argv  # type: ignore[attr-defined]

    def _claude_stdin_prompt(self, prompt: str) -> str:
        """Return the prompt text a claude session actually writes to stdin."""
        stdin = io.StringIO()
        process = SimpleNamespace(stdin=stdin)
        session = claude_stream.ClaudeStreamSession(process=process, prompt=prompt)
        self.assertTrue(session.send_prompt())
        record = json.loads(stdin.getvalue().splitlines()[0])
        self.assertEqual("user", record["type"])
        return record["message"]["content"][0]["text"]

    def _copilot_acp_prompt(self, prompt: str) -> str:
        """Return the prompt text a copilot session puts on ``session/prompt``."""
        process = SimpleNamespace(stdin=io.StringIO())
        session = copilot_acp.CopilotAcpSession(
            process=process,  # type: ignore[arg-type]
            prompt=prompt,
            cwd="/tmp/repo",
            permissions=copilot_acp.PermissionPolicy.for_turn(
                is_audit=False, unsafe_bypass=False
            ),
        )
        blocks = session.prompt_params("acp-session-1")["prompt"]
        return "".join(str(block["text"]) for block in blocks)

    def _assert_prompt_free(
        self, invocation: harnesses.HarnessInvocation, prompt: str
    ) -> None:
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, invocation.transport)
        self.assertNotIn(prompt, invocation.argv)
        self.assertIsNotNone(invocation.turn_plan)
        assert invocation.turn_plan is not None
        self.assertNotIn(prompt, json.dumps(invocation.turn_plan.to_payload()))

    def test_default_invocation_uses_build_session_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            plan_dir.mkdir()
            cwd = tmp_path / "repo"
            cwd.mkdir()
            last_message = tmp_path / "last-message.txt"

            invocation = run_prompts.build_harness_invocation(
                "codex",
                cwd=cwd,
                plan_dir=plan_dir,
                last_message_path=last_message,
            )
            expected_prompt = run_prompts.build_session_prompt(
                "codex", plans_files.canonicalize_path(plan_dir)
            )
            self._assert_prompt_free(invocation, expected_prompt)
            self.assertIn(
                expected_prompt,
                self._codex_exec_argv(
                    cwd=plans_files.canonicalize_path(cwd),
                    plan_dir=plans_files.canonicalize_path(plan_dir),
                    prompt=expected_prompt,
                    last_message_path=last_message,
                ),
            )

    def test_prompt_override_is_passed_to_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            plan_dir.mkdir()
            cwd = tmp_path / "repo"
            cwd.mkdir()
            last_message = tmp_path / "last-message.txt"
            override_prompt = "$session-run /custom/ --runner-managed\n\nextra"

            invocation = run_prompts.build_harness_invocation(
                "codex",
                cwd=cwd,
                plan_dir=plan_dir,
                last_message_path=last_message,
                prompt=override_prompt,
            )
            self._assert_prompt_free(invocation, override_prompt)
            argv = self._codex_exec_argv(
                cwd=plans_files.canonicalize_path(cwd),
                plan_dir=plans_files.canonicalize_path(plan_dir),
                prompt=override_prompt,
                last_message_path=last_message,
            )
            self.assertIn(override_prompt, argv)
            self.assertNotIn(
                run_prompts.build_session_prompt("codex", plan_dir),
                argv,
            )

    def test_prompt_override_consistent_across_harnesses(self) -> None:
        override_prompt = "/session-run /plan/ --runner-managed\n\nbody"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            plan_dir.mkdir()
            cwd = tmp_path / "repo"
            cwd.mkdir()
            last_message = tmp_path / "last-message.txt"

            for harness in ("codex", "claude", "copilot"):
                with self.subTest(harness=harness):
                    invocation = run_prompts.build_harness_invocation(
                        harness,
                        cwd=cwd,
                        plan_dir=plan_dir,
                        last_message_path=last_message,
                        prompt=override_prompt,
                    )
                    if harness == "codex":
                        self._assert_prompt_free(invocation, override_prompt)
                        self.assertIn(
                            override_prompt,
                            self._codex_exec_argv(
                                cwd=plans_files.canonicalize_path(cwd),
                                plan_dir=plans_files.canonicalize_path(plan_dir),
                                prompt=override_prompt,
                                last_message_path=last_message,
                            ),
                        )
                        continue
                    if harness == "claude":
                        # Claude's prompt is the stream-JSON conversation's
                        # first stdin message, so argv carries it nowhere. The
                        # override still has to arrive verbatim, so this
                        # asserts against the channel that actually carries it.
                        self.assertEqual(
                            harnesses.TRANSPORT_STREAM_JSON,
                            invocation.transport,
                        )
                        self.assertNotIn(override_prompt, invocation.argv)
                        self.assertEqual(
                            override_prompt,
                            self._claude_stdin_prompt(override_prompt),
                        )
                        continue
                    # Copilot's worker turn speaks ACP, so the prompt is the
                    # ``session/prompt`` payload rather than a trailing argv
                    # word -- and argv must not repeat it, since the launch
                    # line is what gets logged.
                    self.assertEqual(
                        harnesses.TRANSPORT_ACP,
                        invocation.transport,
                    )
                    self.assertNotIn(override_prompt, invocation.argv)
                    self.assertEqual(
                        override_prompt,
                        self._copilot_acp_prompt(override_prompt),
                    )


class SessionRunSkillReplyCompatibilityTests(unittest.TestCase):
    SKILL_PATH = REPO_ROOT / "skills/session-run/SKILL.md"

    def _skill_text(self) -> str:
        return self.SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_documents_asl_reply_context_block(self) -> None:
        text = self._skill_text()

        self.assertIn("## ASL reply context", text)
        self.assertIn("### Operator reply", text)
        self.assertIn("### Previous pause", text)
        self.assertIn("BEGIN ASL OPERATOR REPLY JSONL", text)
        self.assertIn("use only its `text` value as the answer", text)

    def test_skill_documents_first_line_authority(self) -> None:
        text = self._skill_text()

        self.assertIn(
            "The first line is the only CLI invocation",
            text,
        )

    def test_skill_documents_blocked_pause_for_incomplete_reply(self) -> None:
        text = self._skill_text()

        self.assertIn(
            "RUNNER_PAUSE: blocked - interactive decision gates require user answers",
            text,
        )
        self.assertIn("Runner status: paused", text)

    def test_skill_directs_skill_to_use_operator_reply_first(self) -> None:
        text = self._skill_text()

        self.assertIn("use only its `text` value as the answer", text)
        self.assertIn("Do not ask the user again", text)

    def test_skill_documents_resolution_context_independently(self) -> None:
        text = self._skill_text()

        self.assertIn("## ASL resolution context", text)
        self.assertIn("### Supervisor resolution", text)
        self.assertIn("BEGIN ASL SUPERVISOR RESOLUTION JSONL", text)
        self.assertIn("END ASL SUPERVISOR RESOLUTION JSONL", text)
        self.assertIn("without claiming an answer was supplied", text)
        self.assertIn("pending mailbox guidance normally", text)
        self.assertIn("Never reuse it in a later turn or session", text)
        self.assertIn(
            "never reinterpret it as the `## ASL reply context` block",
            text,
        )

    def _run_fake(
        self,
        *,
        mode: str,
        prompt_argv: str,
        plan_dir: Path,
        last_message_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["FAKE_CODEX_MODE"] = mode
        env["FAKE_CODEX_STEP_SLEEP"] = "0"
        return subprocess.run(
            [
                sys.executable,
                str(FAKE_CODEX_FIXTURE),
                "--add-dir",
                str(plan_dir),
                "-o",
                str(last_message_path),
                prompt_argv,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_fake_reply_blocked_mode_emits_blocked_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            write_plan(plan_dir, ("pending",))
            last_message_path = tmp_dir / "last-message.txt"

            result = self._run_fake(
                mode="reply-blocked",
                prompt_argv="/session-run " + str(plan_dir) + "/ --runner-managed",
                plan_dir=plan_dir,
                last_message_path=last_message_path,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(
                "RUNNER_PAUSE: blocked - interactive decision gates require user "
                "answers",
                last_message_path.read_text(encoding="utf-8"),
            )
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn("- **Status**: pending", session_text)

    def test_fake_reply_aware_pauses_when_no_reply_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            write_plan(plan_dir, ("pending",))
            last_message_path = tmp_dir / "last-message.txt"

            result = self._run_fake(
                mode="reply-aware",
                prompt_argv="/session-run " + str(plan_dir) + "/ --runner-managed",
                plan_dir=plan_dir,
                last_message_path=last_message_path,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(
                "RUNNER_PAUSE: blocked - interactive decision gates require user "
                "answers",
                last_message_path.read_text(encoding="utf-8"),
            )
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn("- **Status**: pending", session_text)

    def test_fake_reply_aware_completes_when_reply_context_present(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            write_plan(plan_dir, ("pending",))
            last_message_path = tmp_dir / "last-message.txt"
            prompt = "\n".join(
                [
                    "/session-run " + str(plan_dir) + "/ --runner-managed",
                    "",
                    "## ASL reply context",
                    "",
                    "### Operator reply",
                    "",
                    "1a, 2a, 3a",
                    "",
                ]
            )

            result = self._run_fake(
                mode="reply-aware",
                prompt_argv=prompt,
                plan_dir=plan_dir,
                last_message_path=last_message_path,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            last_message = last_message_path.read_text(encoding="utf-8")
            self.assertIn("Runner status: complete", last_message)
            self.assertNotIn(
                "RUNNER_PAUSE: blocked - interactive decision gates require user "
                "answers",
                last_message,
            )
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn("- **Status**: complete", session_text)
