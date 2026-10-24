"""Test asl codex app server fallback."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    harness_codex,
    harness_process,
    harnesses,
)
from tests import support_asl_codex_app_server as fixtures
from tests.support import install_fake_codex

ASL_TEST_LANE = "integration"


class AdapterFallbackTests(unittest.TestCase):
    """One turn runs once: before acceptance it may be retried, after it may not."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        self.launch_log = self.tmp / "launches.log"
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)

    def _dual_mode_codex(self, mode: str) -> Path:
        """One binary that answers both call shapes and logs every launch.

        The ``app-server`` half runs the real fixture under ``mode``; the
        ``exec`` half does nothing at all, so a turn that was run twice shows up
        as a second line in the log rather than as a difference in the output.
        """
        executable = self.tmp / "codex"
        executable.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import os, sys",
                    "from pathlib import Path",
                    f"log = Path({str(self.launch_log)!r})",
                    "with log.open('a', encoding='utf-8') as handle:",
                    "    handle.write(sys.argv[1] + '\\n')",
                    "if sys.argv[1] != 'app-server':",
                    "    raise SystemExit(0)",
                    f"os.environ['FAKE_APP_SERVER_MODE'] = {mode!r}",
                    f"exec(open({str(fixtures.FAKE_APP_SERVER_FIXTURE)!r}).read())",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable

    def test_a_harness_without_an_app_server_runs_the_legacy_turn(self) -> None:
        executable = install_fake_codex(self.tmp)
        adapter = harness_codex.CodexAdapter(executable=str(executable))
        chunks: list[tuple[str, str]] = []

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
            stream_callback=lambda name, text: chunks.append((name, text)),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertIn("exec", result.argv)
        self.assertIn("Runner status: complete", result.last_message)
        self.assertIn("codex app-server unavailable", result.stderr)
        # The declined attempt's own output is not the turn's output.
        streamed = "".join(text for _, text in chunks)
        self.assertNotIn("does not implement app-server", streamed)

    def test_the_declined_attempt_leaves_the_stream_accounting_honest(self) -> None:
        executable = install_fake_codex(self.tmp)
        adapter = harness_codex.CodexAdapter(executable=str(executable))

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
        )

        assert result.stream_io is not None
        self.assertEqual(len(result.stderr), result.stream_io["stderr_chars"])

    def test_a_turn_that_died_after_acceptance_is_never_run_again(self) -> None:
        # The app-server dies after accepting the turn, and `exec` would run it
        # a second time if the adapter let it.
        executable = self._dual_mode_codex("die-after-accept")
        adapter = harness_codex.CodexAdapter(executable=str(executable))

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
        )

        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertNotEqual(0, result.returncode)

    def test_a_turn_whose_answer_was_lost_is_never_run_again(self) -> None:
        """The same promise as above, for the death that leaves no acceptance.

        The app-server here reads the prompt and dies without answering, so
        nothing on this side was ever told the turn started. A fallback written
        from acceptance alone hands the identical prompt to ``codex exec``,
        which is a second execution of work the provider may already be doing.
        """
        executable = self._dual_mode_codex("die-before-turn-response")
        adapter = harness_codex.CodexAdapter(executable=str(executable))

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
        )

        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertNotEqual(0, result.returncode)

    def test_a_fallback_reruns_the_legacy_argv_exactly_once(self) -> None:
        executable = install_fake_codex(self.tmp)
        adapter = harness_codex.CodexAdapter(executable=str(executable))
        legacy = adapter.build_exec_argv(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
        )

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
        )

        self.assertEqual(legacy, result.argv)


class LegacyContinuityTests(unittest.TestCase):
    """What continuity a ``codex exec`` turn can and cannot honestly report."""

    SESSION_ID = "0199b0ce-2f5e-7c2a-9f11-4a1d0f0c8b31"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        self.adapter = harness_codex.CodexAdapter(
            executable=str(install_fake_codex(self.tmp))
        )

    def _say(self, text: str) -> None:
        env = mock.patch.dict("os.environ", {"FAKE_CODEX_STDERR_TEXT": text})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, **kwargs: Any) -> Any:
        return self.adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=fixtures.PROMPT,
            last_message_path=self.last_message,
            timeout=fixtures.WAIT_SECONDS,
            **kwargs,
        )

    def test_a_legacy_turn_reports_the_session_its_output_names(self) -> None:
        self._say(f"session id: {self.SESSION_ID}\n")
        captured: list[str] = []

        result = self._run(on_conversation=captured.append)

        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertEqual([self.SESSION_ID], captured)
        self.assertEqual(self.SESSION_ID, result.conversation_id)
        self.assertIsNone(result.continuity_unavailable)

    def test_a_legacy_turn_that_names_nothing_says_so(self) -> None:
        result = self._run(on_conversation=lambda native_id: None)

        self.assertIsNone(result.conversation_id)
        # Stated rather than left empty: a caller that wanted to continue this
        # turn would otherwise read "no id" as "nobody has looked yet".
        self.assertEqual(
            "codex exec output named no session id", result.continuity_unavailable
        )

    def test_a_turn_nobody_asked_about_is_missing_nothing(self) -> None:
        result = self._run()

        self.assertIsNone(result.continuity_unavailable)
        self.assertEqual(0, result.returncode)

    def test_a_capture_that_fails_after_the_fact_does_not_lose_the_turn(self) -> None:
        self._say(f"session id: {self.SESSION_ID}\n")

        def refuse(native_id: str) -> None:
            raise RuntimeError("the ledger is unwritable")

        result = self._run(on_conversation=refuse)

        # `codex exec` submits its prompt on argv, so the work is already done
        # by the time its output names a session: there is nothing to retract,
        # and the only honest outcome is a turn without continuity.
        self.assertEqual(0, result.returncode)
        self.assertIn("Runner status: complete", result.last_message)
        self.assertIsNone(result.conversation_id)
        self.assertIn("the ledger is unwritable", str(result.continuity_unavailable))

    def test_a_legacy_invocation_cannot_be_built_for_a_resume(self) -> None:
        with self.assertRaises(harnesses.HarnessError) as caught:
            self.adapter.build_exec_invocation(
                cwd=self.tmp,
                plan_dir=self.plan_dir,
                prompt=fixtures.PROMPT,
                last_message_path=self.last_message,
                resume_conversation_id="thread-77",
            )

        self.assertIn("fresh session", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
