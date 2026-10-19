"""A native conversation the provider will not restore, end to end.

A confirmed ledger entry can outlive the history it names. The provider is the
only one who knows, and the only moment it says so is the handshake of a turn
that asked to continue that conversation. Everything here is about what that
one sentence has to become:

* a *structural* fact on the result rather than a nonzero exit with a message
  in it, because a message cannot be routed;
* a durable block on the ledger entry, so every later ordinary launch stops for
  recovery instead of asking for the same dead conversation again;
* something the operator's one-use ``--fresh-conversation`` authorization can
  get past, keeping the session's retained files and outstanding feedback and
  losing only the context that was already lost.

The three harnesses that can continue a conversation each refuse in their own
protocol, so each is driven through its real refusal here: codex over the
app-server's ``thread/resume``, copilot over ACP's ``session/load``, and claude
-- which has no restore handshake at all -- out of the CLI's own complaint about
the identifier on ``--resume``. Beside each refusal stands the near miss it must
not be confused with, because the cost of a false positive is a conversation
retired while the provider still holds it.

The largest near miss is the provider answering at all. A rejected restore
request is the provider *stating* something, but what it states is usually not
about the history: an internal error, a service that is down, an expired
credential, a workspace this profile may not read. Those are transient by
nature, so each is driven through the same real adapters here and each has to
leave the saved conversation exactly where it was -- ending in the launch that
matters, an ordinary one after the outage is over, which continues the very
conversation the failure interrupted and spends no fresh-start authorization.

The fixtures are the real ones: a spawned app-server, a spawned ACP agent, and
for the run-loop story the whole-loop fixture from
:mod:`tests.asl_redirect_run_support`. The result the run loop is driven
by is produced by the real adapter against the real refusing app-server rather
than assembled by hand, so the branch cannot keep passing against a result shape
no transport produces.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    harness_codex,
    harness_copilot,
    harness_process,
)
from tests import support_asl_native_restore_recovery as fixtures
from tests.support_asl_codex_app_server import (
    FAKE_APP_SERVER_FIXTURE,
    PROMPT,
    WAIT_SECONDS,
)
from tests.support_asl_native_restore_recovery import NOT_A_MISSING_HISTORY, THREAD_ID
from tests.test_asl_copilot_acp import FAKE_ACP_FIXTURE
from tests.test_asl_copilot_acp import SESSION_ID as ACP_SESSION_ID


class CodexRestoreRefusalTests(unittest.TestCase):
    """A real app-server answering ``thread/resume`` with an error."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        self.record = self.tmp / "record.json"
        # The real sweep shells out to ``ps``; nothing here is about the
        # process table.
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        env = mock.patch.dict(
            "os.environ", {"FAKE_APP_SERVER_RECORD": str(self.record)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.spawned: list[int] = []
        self.submitted: list[int] = []

    def _run(
        self,
        mode: str,
        *,
        resume: str | None = THREAD_ID,
    ) -> Any:
        os.environ["FAKE_APP_SERVER_MODE"] = mode
        executable = fixtures._install(
            FAKE_APP_SERVER_FIXTURE, self.tmp / "codex-app-server"
        )
        adapter = harness_codex.CodexAdapter(executable=str(executable))
        options: dict[str, Any] = {}
        if resume is not None:
            options["invocation"] = adapter.build_invocation(
                cwd=self.tmp,
                plan_dir=self.plan_dir,
                prompt=PROMPT,
                last_message_path=self.last_message,
                resume_conversation_id=resume,
            )
        return adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
            on_spawn=self.spawned.append,
            # Recorded against the spawns so far, so a submission reported from
            # the wrong side of the handshake cannot look like this one.
            on_prompt_submitted=lambda: self.submitted.append(len(self.spawned)),
            **options,
        )

    def test_a_refused_resume_is_a_field_and_not_only_a_message(self) -> None:
        result = self._run("resume-unknown")

        # The exit code and the stderr were already there, and neither can be
        # branched on: the field is what makes this routable.
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no such thread", result.stderr)
        refused = result.conversation_restore_refused
        self.assertIsNotNone(refused)
        assert refused is not None
        self.assertIn(THREAD_ID, refused)
        # The provider's own words survive into the field, because the operator
        # reading the pause needs to know why the history is gone.
        self.assertIn("no such thread", refused)

    def _reject_resume(self, error: dict[str, Any]) -> Any:
        os.environ["FAKE_APP_SERVER_RESUME_ERROR"] = json.dumps(error)
        return self._run("resume-rejected")

    def test_an_error_that_is_not_about_the_history_is_not_a_refused_restore(
        self,
    ) -> None:
        for error in NOT_A_MISSING_HISTORY:
            with self.subTest(error=error["message"]):
                result = self._reject_resume(error)

                # The app-server answered, so the turn is over -- but what it
                # answered is about itself, its credentials, or this moment.
                # Retiring the thread here would discard a history the server is
                # still holding, on the strength of an outage.
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(result.conversation_restore_refused)
                # Still reported: an operator has to be able to see what the
                # provider said, or a transient failure becomes a silent one.
                self.assertIn(error["message"], result.stderr)

    def test_a_thread_that_came_back_different_is_not_a_refused_restore(self) -> None:
        result = self._run("resume-other-thread")

        # The app-server had a thread and offered the wrong one. Retiring the
        # requested conversation here would discard a history that still exists.
        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(result.conversation_restore_refused)

    def test_settings_drift_on_a_restore_is_not_a_refused_restore(self) -> None:
        result = self._run("resume-approvals-drifted")

        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(result.conversation_restore_refused)

    def test_a_turn_that_asked_for_nothing_can_report_no_refusal(self) -> None:
        result = self._run("success", resume=None)

        self.assertEqual(0, result.returncode)
        self.assertIsNone(result.conversation_restore_refused)

    def test_a_refused_resume_never_submits_the_prompt_it_was_holding(self) -> None:
        self._run("resume-unknown")

        # ``thread/resume`` is answered before ``turn/start``, so this process
        # was spawned, told the server which thread it wanted, and refused --
        # all before anything could read the prompt. Whatever that prompt was
        # carrying is still the run's to account for.
        self.assertEqual(1, len(self.spawned))
        self.assertEqual([], self.submitted)

    def test_an_accepted_turn_reports_the_prompt_it_handed_over(self) -> None:
        self._run("success", resume=None)

        # The far side of the same handshake: a turn the server answered with,
        # which is the only point at which the words have gone out.
        self.assertEqual([1], self.submitted)


class CopilotRestoreRefusalTests(unittest.TestCase):
    """A real ACP agent answering ``session/load`` with an error."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.record = self.tmp / "record.json"
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        env = mock.patch.dict("os.environ", {"FAKE_ACP_RECORD": str(self.record)})
        env.start()
        self.addCleanup(env.stop)
        self.spawned: list[int] = []
        self.submitted: list[int] = []

    def _run(self, mode: str) -> Any:
        os.environ["FAKE_ACP_MODE"] = mode
        executable = fixtures._install(FAKE_ACP_FIXTURE, self.tmp / "copilot")
        adapter = harness_copilot.CopilotAdapter(executable=str(executable))
        return adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            timeout=WAIT_SECONDS,
            unsafe_bypass=True,
            on_spawn=self.spawned.append,
            on_prompt_submitted=lambda: self.submitted.append(len(self.spawned)),
            invocation=adapter.build_invocation(
                cwd=self.tmp,
                plan_dir=self.plan_dir,
                prompt=PROMPT,
                unsafe_bypass=True,
                resume_conversation_id=ACP_SESSION_ID,
            ),
        )

    def test_a_session_the_agent_no_longer_has_is_a_refused_restore(self) -> None:
        result = self._run("load-missing")

        self.assertNotEqual(0, result.returncode)
        refused = result.conversation_restore_refused
        self.assertIsNotNone(refused)
        assert refused is not None
        self.assertIn(ACP_SESSION_ID, refused)

    def _reject_load(self, error: dict[str, Any]) -> Any:
        os.environ["FAKE_ACP_LOAD_ERROR"] = json.dumps(error)
        return self._run("load-rejected")

    def test_an_error_that_is_not_about_the_history_is_not_a_refused_restore(
        self,
    ) -> None:
        for error in NOT_A_MISSING_HISTORY:
            with self.subTest(error=error["message"]):
                result = self._reject_load(error)

                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(result.conversation_restore_refused)
                self.assertIn(error["message"], result.stderr)

    def test_an_agent_that_cannot_load_at_all_is_not_a_refused_restore(self) -> None:
        result = self._run("no-load-capability")

        # This agent never said the conversation is gone; it said it cannot be
        # asked. The history may be sitting there for a client that can ask.
        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(result.conversation_restore_refused)

    def test_a_restore_answered_with_another_session_is_not_a_refusal(self) -> None:
        result = self._run("load-drift")

        self.assertNotEqual(0, result.returncode)
        self.assertIsNone(result.conversation_restore_refused)

    def test_a_refused_load_never_submits_the_prompt_it_was_holding(self) -> None:
        self._run("load-missing")

        # ``session/load`` is answered before ``session/prompt``: an agent that
        # says it no longer has the session is one that never read the words.
        self.assertEqual(1, len(self.spawned))
        self.assertEqual([], self.submitted)

    def test_a_loaded_session_reports_the_prompt_it_handed_over(self) -> None:
        result = self._run("success")

        self.assertEqual(0, result.returncode)
        self.assertEqual([1], self.submitted)


if __name__ == "__main__":
    unittest.main()
