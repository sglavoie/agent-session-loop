"""Input mistakes read as sentences and keep a stable JSON ``reason``."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import cli
from tests.support import isolated_home, write_plan

_UNKNOWN_QUESTION = "00000000-0000-4000-8000-000000000000"


class InputErrorWordingTests(unittest.TestCase):
    """A refusal names the mistake in prose; ``reason`` keeps the code."""

    def setUp(self) -> None:
        isolated_home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        write_plan(self.plan_dir, ("pending",))

    def _run(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def _json_refusal(self, *argv: str) -> dict:
        code, stdout, stderr = self._run(*argv, "--json")
        self.assertEqual(1, code, stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        return payload

    def test_set_model_refuses_an_empty_model_or_effort_in_prose(self) -> None:
        for flag, reason in (
            ("--harness-model", "invalid-model"),
            ("--effort", "invalid-effort"),
        ):
            with self.subTest(flag=flag):
                payload = self._json_refusal(
                    "control",
                    "set-model",
                    str(self.plan_dir),
                    "--state-dir",
                    str(self.state_dir),
                    flag,
                    "",
                )
                self.assertEqual(reason, payload["reason"])
                self.assertIn(f"{flag} needs a non-empty value", payload["error"])

    def test_mailbox_refusals_describe_the_question_id(self) -> None:
        base = ("--state-dir", str(self.state_dir))
        cases = (
            (
                ("inbox", "reply", str(self.plan_dir), "--to", "nope", "--text", "hi"),
                "invalid-target",
                "the question id must be a UUID",
            ),
            (
                (
                    "inbox",
                    "reply",
                    str(self.plan_dir),
                    "--to",
                    _UNKNOWN_QUESTION,
                    "--text",
                    "hi",
                ),
                "question-not-found",
                f"no question with id '{_UNKNOWN_QUESTION}'",
            ),
            (
                ("ask", str(self.plan_dir), "--question-id", "nope"),
                "question-not-found",
                "no question with id 'nope'",
            ),
        )
        for argv, reason, message in cases:
            with self.subTest(argv=argv):
                payload = self._json_refusal(*argv, *base)
                self.assertEqual(reason, payload["reason"])
                self.assertIn(message, payload["error"])
                self.assertNotEqual(reason, payload["error"])
                code, _stdout, stderr = self._run(*argv, *base)
                self.assertEqual(1, code)
                self.assertIn(message, stderr)

    def test_ask_without_a_source_names_all_three(self) -> None:
        payload = self._json_refusal(
            "ask", str(self.plan_dir), "--state-dir", str(self.state_dir)
        )
        self.assertEqual("invalid-question-source", payload["reason"])
        self.assertIn("TEXT, --text-stdin, or --question-id", payload["error"])


if __name__ == "__main__":
    unittest.main()
