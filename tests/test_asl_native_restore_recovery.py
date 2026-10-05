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
:mod:`tests.test_asl_redirect_continuation`. The result the run loop is driven
by is produced by the real adapter against the real refusing app-server rather
than assembled by hand, so the branch cannot keep passing against a result shape
no transport produces.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    conversation_resume,
    conversations,
    events,
    handoff_guards,
    harness_codex,
    harnesses,
    redirect_ack,
    redirect_recovery,
    redirect_store,
    restore_diagnosis,
    state,
    turn_execution,
)
from tests.test_asl_codex_app_server import (
    FAKE_APP_SERVER_FIXTURE,
    PROMPT,
    WAIT_SECONDS,
)
from tests.test_asl_copilot_acp import FAKE_ACP_FIXTURE
from tests.test_asl_copilot_acp import SESSION_ID as ACP_SESSION_ID
from tests.test_asl_harness_lifecycle import _FakePipe, _FakeProcess, _TurnTestCase
from tests.test_asl_redirect_continuation import (
    FEEDBACK,
    SESSION_ID,
    WORKER_PID,
    _RedirectedRun,
    target,
)

#: The native conversation the adapter-level refusals ask for.
THREAD_ID = "thread-77"

#: The conversation the whole-loop run opens on its first turn and is refused on
#: its second. Fixed by the fixture's worker, not chosen here.
RUN_CONVERSATION_ID = "conv-1"


#: A name lookup that failed on the way to the provider. It is the shape of
#: refusal a blacklist of transient words can never catch: nothing in it is
#: temporary, an operator repairs it in a minute, and the only reason it was
#: ever read as a disowned history is that it happens to contain "no such".
UNRELATED_NO_SUCH_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "no such host: api.provider.example",
}


#: A misconfigured provider install, reported in the one shape that reads most
#: like a disowned history and is not one: the noun "session" followed by "not
#: found". What is missing is a file on disk that an operator puts back, and the
#: conversation the restore asked for is untouched by its absence.
SESSION_FILE_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "session file not found: /etc/provider/session-config.json",
}


#: The same compound trap written the way code writes it. A provider that did
#: not recognize the call names the method, and the method is spelled with a dot
#: -- which is also how a sentence ends, so the classifier once read "no such
#: session" out of it and stopped there. What is missing is an RPC method on an
#: out-of-date provider, and an operator who upgrades it gets the conversation
#: back untouched.
METHOD_NAME_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "no such session.load method",
}


#: The other half of that trap: the noun, a *filename*, and "not found". The
#: dot in "config.json" is what makes the word look like an identifier a provider
#: minted rather than the ordinary compound it is, and the sentence is about a
#: configuration file the provider could not read.
CONFIG_FILE_NAME_ERROR: dict[str, Any] = {
    "code": -32000,
    "message": "session config.json not found",
}


#: Error responses a provider can answer a restore request with that say
#: nothing about whether the history exists. The internal-error code is the one
#: that matters most -- it is what a server wraps any unhandled failure in, so
#: every transient backend fault arrives wearing it -- and the rest are the
#: refusals an operator repairs rather than starts over from: a service that is
#: down, a credential that expired, and a workspace this launch may not read.
#: The middle three are the ones no vocabulary of transient words could have
#: rescued -- a name lookup, a model, and a workspace that are absent say
#: nothing temporary and nothing at all about whether the history exists.
#: The last six go one step further: each one *does* contain a history noun,
#: and in each one that noun is an adjective in front of the thing the provider
#: is really talking about -- a configuration file, a worker pool, an RPC
#: method, a storage backend. An operator repairs every one of them in a minute,
#: and the conversation was never in question. The final two write that compound
#: with punctuation rather than a space, which is the form that reads most like a
#: finished sentence and a minted identifier: a method spelled "session.load",
#: and a filename spelled "config.json".
NOT_A_MISSING_HISTORY: tuple[dict[str, Any], ...] = (
    {"code": -32603, "message": "internal error"},
    {"code": -32603, "message": "temporary service unavailable; retry later"},
    {"code": -32000, "message": "authentication required: the token has expired"},
    {"code": -32000, "message": "permission denied for this workspace"},
    {"code": -32000, "message": UNRELATED_NO_SUCH_ERROR["message"]},
    {"code": -32000, "message": "no such model: missing-model-id"},
    {"code": -32000, "message": "no such workspace: /workspace"},
    SESSION_FILE_ERROR,
    {"code": -32000, "message": "thread pool not found"},
    {"code": -32000, "message": "no such session/load method"},
    {"code": -32000, "message": "unknown session storage backend"},
    METHOD_NAME_ERROR,
    CONFIG_FILE_NAME_ERROR,
)


def _install(fixture: Path, destination: Path) -> Path:
    shutil.copyfile(fixture, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    return destination


def codex_resume_result(
    tmp: Path,
    *,
    conversation: str,
    mode: str = "resume-unknown",
    resume_error: dict[str, Any] | None = None,
) -> harnesses.HarnessResult:
    """One real codex resume against an app-server that answers it with an error.

    A module-level function rather than a test method because two very different
    cases need the same result: the adapter claims below, and the whole-loop runs
    that have to be driven by results the transport actually produces. Which
    error it is answered with is the parameter, because the two whole-loop
    stories differ in nothing else.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    executable = _install(FAKE_APP_SERVER_FIXTURE, tmp / "codex-app-server")
    adapter = harness_codex.CodexAdapter(executable=str(executable))
    plan_dir = tmp / "plan"
    plan_dir.mkdir(exist_ok=True)
    last_message = tmp / "last-message.txt"
    with (
        mock.patch.object(harnesses, "sweep_enabled", return_value=False),
        mock.patch.dict(
            "os.environ",
            {
                "FAKE_APP_SERVER_MODE": mode,
                "FAKE_APP_SERVER_RECORD": str(tmp / "record.json"),
                "FAKE_APP_SERVER_RESUME_ERROR": json.dumps(resume_error or {}),
            },
        ),
    ):
        return adapter.run(
            cwd=tmp,
            plan_dir=plan_dir,
            prompt=PROMPT,
            last_message_path=last_message,
            timeout=WAIT_SECONDS,
            invocation=adapter.build_invocation(
                cwd=tmp,
                plan_dir=plan_dir,
                prompt=PROMPT,
                last_message_path=last_message,
                resume_conversation_id=conversation,
            ),
        )


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
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        env = mock.patch.dict(
            "os.environ", {"FAKE_APP_SERVER_RECORD": str(self.record)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.spawned: list[int] = []
        self.submitted: list[int] = []

    def _run(self, mode: str, *, resume: str | None = THREAD_ID) -> Any:
        os.environ["FAKE_APP_SERVER_MODE"] = mode
        executable = _install(FAKE_APP_SERVER_FIXTURE, self.tmp / "codex-app-server")
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
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        env = mock.patch.dict("os.environ", {"FAKE_ACP_RECORD": str(self.record)})
        env.start()
        self.addCleanup(env.stop)
        self.spawned: list[int] = []
        self.submitted: list[int] = []

    def _run(self, mode: str) -> Any:
        os.environ["FAKE_ACP_MODE"] = mode
        executable = _install(FAKE_ACP_FIXTURE, self.tmp / "copilot")
        adapter = harnesses.CopilotAdapter(executable=str(executable))
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


class _BrokenPipe(_FakePipe):
    """A stdin whose reader is gone, like a CLI that exited at startup."""

    def write(self, text: str) -> int:  # type: ignore[override]
        raise BrokenPipeError(32, "Broken pipe")


class ClaudeRestoreRefusalTests(_TurnTestCase):
    """A claude turn whose ``--resume`` the CLI would not honour.

    Claude has no restore handshake to fail, so the refusal has to be read out
    of the CLI's own output after the fact. Each case here is one of the gates
    that reading passes through, because a wrong reading retires a live
    conversation.
    """

    def _resume(
        self,
        *,
        stderr: str,
        stdout: str = "",
        returncode: int = 1,
        resume: str = THREAD_ID,
        stdin: _FakePipe | None = None,
        **hooks: Any,
    ) -> Any:
        adapter = harnesses.ClaudeAdapter(executable="claude")
        process = _FakeProcess(running=False)
        process.returncode = returncode
        process.stdout = _FakePipe(stdout)
        process.stderr = _FakePipe(stderr)
        if stdin is not None:
            process.stdin = stdin
        invocation = adapter.build_invocation(
            cwd=self.tmp,
            plan_dir=self.tmp,
            prompt="do the work",
            resume_conversation_id=resume,
        )
        return self.run_turn(
            adapter,
            process,
            invocation=invocation,
            on_conversation=lambda native_id: None,
            **hooks,
        )

    def _init_record(self, session_id: str) -> str:
        return (
            json.dumps({"type": "system", "subtype": "init", "session_id": session_id})
            + "\n"
        )

    def test_a_cli_that_says_the_conversation_is_gone_is_a_refused_restore(
        self,
    ) -> None:
        result = self._resume(
            stderr=f"Error: No conversation found with session ID: {THREAD_ID}\n"
        )

        refused = result.conversation_restore_refused
        self.assertIsNotNone(refused)
        assert refused is not None
        self.assertIn(THREAD_ID, refused)

    def test_a_resumed_turn_that_announced_its_session_is_never_a_refusal(self) -> None:
        # The CLI restored the conversation, ran inside it, and failed there.
        # The history exists; only this turn did not work out.
        result = self._resume(
            stdout=self._init_record(THREAD_ID),
            stderr=f"No conversation found with session ID: {THREAD_ID}\n",
        )

        self.assertIsNone(result.conversation_restore_refused)

    def test_a_complaint_about_another_conversation_is_not_a_refusal(self) -> None:
        result = self._resume(
            stderr="Error: No conversation found with session ID: some-other-id\n"
        )

        self.assertIsNone(result.conversation_restore_refused)

    def test_an_unrelated_startup_failure_is_not_a_refusal(self) -> None:
        # Names the conversation, but says nothing about whether it still exists.
        result = self._resume(
            stderr=f"Error: could not reach the API while resuming {THREAD_ID}\n"
        )

        self.assertIsNone(result.conversation_restore_refused)

    def test_a_complaint_that_explains_itself_as_temporary_is_not_a_refusal(
        self,
    ) -> None:
        # Names the conversation and says it is missing, and then says why: the
        # service is down. A history that cannot be reached right now is not a
        # history that is gone, whichever half of the sentence is read first.
        result = self._resume(
            stderr=(
                "Error: No conversation found with session ID: "
                f"{THREAD_ID} (service temporarily unavailable, retry later)\n"
            )
        )

        self.assertIsNone(result.conversation_restore_refused)

    def test_a_turn_that_ran_and_succeeded_is_not_a_refusal(self) -> None:
        result = self._resume(
            stdout=self._init_record(THREAD_ID), stderr="", returncode=0
        )

        self.assertIsNone(result.conversation_restore_refused)

    def test_a_cli_that_exited_before_the_write_submitted_nothing(self) -> None:
        """Claude's refusal has no handshake, so the pipe is what reports it.

        The CLI rejects the identifier and exits at startup, which leaves the
        run writing the prompt into a pipe with no reader. Nothing read the
        words, and the turn says so rather than counting the spawn.
        """
        submitted: list[str] = []

        result = self._resume(
            stderr=f"Error: No conversation found with session ID: {THREAD_ID}\n",
            stdin=_BrokenPipe(),
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        self.assertIsNotNone(result.conversation_restore_refused)
        self.assertEqual([], submitted)
        self.assertIs(False, result.prompt_submitted)
        self.assertTrue(harnesses.prompt_provably_unsubmitted(result))

    def test_a_cli_that_refused_after_taking_the_write_submitted_nothing(self) -> None:
        """The same refusal, from the side of it the pipe does not report.

        A pipe holds what is written to it whether or not anybody is reading,
        so a CLI that rejects ``--resume`` without ever touching its stdin
        leaves the write succeeding and the words unread. The write is not the
        submission here: this turn ends having announced no session and having
        said, in its own output, that it would not take the conversation up --
        which is the whole of what "nobody got these words" can be made of.
        """
        submitted: list[str] = []

        result = self._resume(
            stderr=f"Error: No conversation found with session ID: {THREAD_ID}\n",
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        self.assertIsNotNone(result.conversation_restore_refused)
        self.assertEqual([], submitted)
        self.assertIs(False, result.prompt_submitted)
        self.assertTrue(harnesses.prompt_provably_unsubmitted(result))

    def test_a_turn_that_explains_nothing_is_never_proof_of_non_submission(
        self,
    ) -> None:
        """The fail-closed half: silence is not the refusal it resembles.

        This CLI also announced no session and also exited nonzero, and the
        write also succeeded -- and it says nothing about the conversation. It
        may have read the prompt and died, so the words may be spent. A turn
        that cannot show otherwise reports the submission it made, because the
        alternative is handing the same correction to a second worker.
        """
        submitted: list[str] = []

        result = self._resume(
            stderr="Error: the sandbox denied a required capability\n",
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        self.assertIsNone(result.conversation_restore_refused)
        self.assertEqual(["submitted"], submitted)
        self.assertIs(True, result.prompt_submitted)
        self.assertFalse(harnesses.prompt_provably_unsubmitted(result))

    def test_a_prompt_that_reached_the_stream_is_submitted(self) -> None:
        submitted: list[str] = []

        result = self._resume(
            stdout=self._init_record(THREAD_ID),
            stderr="",
            returncode=0,
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        self.assertEqual(["submitted"], submitted)
        self.assertIs(True, result.prompt_submitted)

    def test_a_turn_the_cli_took_up_and_then_failed_in_is_still_submitted(self) -> None:
        """The turn the deferral must not cost anything: a started one.

        The CLI announced its session, so it got past the ``--resume`` it could
        have refused and the prompt in its stdin is its. What it then did with
        the turn is a separate failure, and none of it may return the
        correction to the journal for a second worker to receive.
        """
        submitted: list[str] = []

        result = self._resume(
            stdout=self._init_record(THREAD_ID),
            stderr="Error: the model call failed after the turn started\n",
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        self.assertEqual(["submitted"], submitted)
        self.assertIs(True, result.prompt_submitted)
        self.assertFalse(harnesses.prompt_provably_unsubmitted(result))


class RestoreDiagnosisTests(unittest.TestCase):
    """The one vocabulary all three harnesses weigh a refusal against.

    The adapter claims above prove each transport asks the question; these prove
    what the answer is, on the inputs a transport test cannot reach -- a code
    and a message that disagree, a reason buried in ``data``, a provider that
    sent no structure at all.
    """

    def _rejection(self, **error: Any) -> restore_diagnosis.ProviderRejection:
        return restore_diagnosis.ProviderRejection.from_payload(error)

    def test_the_answers_both_providers_actually_give_are_diagnoses(self) -> None:
        for message in ("no such thread", "no such session"):
            with self.subTest(message=message):
                rejection = self._rejection(code=-32602, message=message)

                self.assertTrue(rejection.diagnoses_missing_history())

    def test_a_protocol_fixed_code_is_never_a_diagnosis(self) -> None:
        # Even saying the words. These codes are spent on faults in the request
        # or in the server, and the internal-error one is what a server wraps
        # every unhandled failure in -- a code that can mean anything may not
        # mean a conversation is gone.
        for code in sorted(restore_diagnosis.NON_DIAGNOSTIC_ERROR_CODES):
            with self.subTest(code=code):
                rejection = self._rejection(code=code, message="no such session")

                self.assertFalse(rejection.diagnoses_missing_history())

    def test_a_message_that_explains_itself_as_transient_is_not_a_diagnosis(
        self,
    ) -> None:
        rejection = self._rejection(
            code=-32000,
            message="no such session: authentication expired, retry after sign in",
        )

        self.assertFalse(rejection.diagnoses_missing_history())

    def test_the_reason_is_read_out_of_the_data_blob_as_well(self) -> None:
        rejection = self._rejection(
            code=-32001,
            message="session not found",
            data={"reason": "rate limit exceeded"},
        )

        self.assertFalse(rejection.diagnoses_missing_history())

    def test_the_data_blob_is_never_positive_evidence(self) -> None:
        # A provider that echoes the request into ``data`` would otherwise quote
        # the identifier in question back as proof that it is gone.
        rejection = self._rejection(
            code=-32001,
            message="the request failed",
            data={"detail": "no such session"},
        )

        self.assertFalse(rejection.diagnoses_missing_history())

    def test_an_unstructured_error_is_judged_on_the_words_it_is(self) -> None:
        rejection = restore_diagnosis.ProviderRejection.from_payload("no such session")

        self.assertIsNone(rejection.numeric_code)
        self.assertTrue(rejection.diagnoses_missing_history())

    def test_a_code_that_is_not_a_number_decides_nothing(self) -> None:
        # ``True == 1`` in Python, so a forged boolean would otherwise arrive as
        # a code, and a string code is a code this vocabulary cannot weigh.
        for code in (True, "SESSION_NOT_FOUND"):
            with self.subTest(code=code):
                rejection = self._rejection(code=code, message="the request failed")

                self.assertIsNone(rejection.numeric_code)
                self.assertFalse(rejection.diagnoses_missing_history())

    def test_the_rendering_keeps_everything_the_provider_sent(self) -> None:
        rejection = self._rejection(
            code=-32602, message="no such thread", data={"threadId": "thread-77"}
        )

        self.assertEqual(
            '[-32602] no such thread {"threadId":"thread-77"}', rejection.rendered
        )

    def test_the_prose_form_is_the_same_judgement(self) -> None:
        for line, expected in (
            (f"Error: No conversation found with session ID: {THREAD_ID}", True),
            (f"Error: could not reach the API while resuming {THREAD_ID}", False),
            (f"Error: permission denied for conversation {THREAD_ID}", False),
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    expected, restore_diagnosis.text_diagnoses_missing_history(line)
                )

    def test_something_else_being_absent_is_never_a_missing_history(self) -> None:
        # The words "no such" are a sentence about *something* being gone, and
        # almost everything a provider reports gone is not a conversation. None
        # of these is transient either, so nothing in the veto vocabulary could
        # have caught them: the subject has to be required, not excused.
        for line in (
            UNRELATED_NO_SUCH_ERROR["message"],
            "no such model: missing-model-id",
            "no such workspace: /workspace",
            "open /tmp/plan/state.json: no such file or directory",
            "no such tool: shell",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_a_verdict_belongs_to_the_subject_it_was_passed_about(self) -> None:
        # The same trap one clause further along: a message may name a session
        # and then report something else missing. The history noun appearing
        # somewhere in the sentence is not the history being disowned.
        for line in (
            "session/load failed: the settings file does not exist",
            f"while resuming thread {THREAD_ID}: the model endpoint was not found",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_a_history_noun_used_as_an_adjective_is_not_the_history(self) -> None:
        # The same trap with no clause boundary to give it away. English builds
        # compounds by stacking nouns, so every one of these puts "session" or
        # "thread" directly in front of the thing it is actually about -- a
        # config file, a worker pool, an RPC method, a storage backend. Naming
        # the history noun cannot be enough; it has to hold the subject position.
        for line in (
            SESSION_FILE_ERROR["message"],
            "thread pool not found",
            "no such session/load method",
            "unknown session storage backend",
            "session cache directory does not exist",
            "no such thread scheduler",
            "invalid conversation log format",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_punctuation_inside_a_name_is_not_the_end_of_the_sentence(self) -> None:
        # The same compound trap, written the way code writes compounds. A dot
        # or a colon joins two halves of one token in "session.load",
        # "session:load" and "session.json" -- and a dot is also how a sentence
        # ends, so reading one as a boundary reads "no such session" out of a
        # complaint about an RPC method. The clause a history noun owns has to
        # end where a word stops continuing it.
        for line in (
            METHOD_NAME_ERROR["message"],
            "unknown session.load method",
            "unknown session:load method",
            "no such session.json file",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_a_separator_alone_does_not_make_a_word_an_identifier(self) -> None:
        # The trap one layer in from the clause boundary: here the noun really is
        # followed by "not found", and the only question is whether the word
        # between them is this session's name. "config.json" and
        # "storage-backend" are compounds of ordinary words that happen to carry
        # the punctuation an identifier carries -- so a name has to look like a
        # minted one, which is to say it has to carry a digit.
        for line in (
            CONFIG_FILE_NAME_ERROR["message"],
            "session storage-backend not found",
            "thread work-queue does not exist",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_infrastructure_identifiers_are_not_native_history(self) -> None:
        for line in (
            "session config-v2.json not found",
            'session "config.json" not found',
            "session v2-backend does not exist",
            "no such session::load method",
            "no such session() method",
            "no such session[load] method",
            "pthread not found",
            "subsession not found",
            "session config99.toml not found",
            'session "v3-cache" was deleted',
        ):
            with self.subTest(line=line):
                self.assertFalse(
                    restore_diagnosis.ProviderRejection(
                        code=-32000, message=line
                    ).diagnoses_missing_history()
                )

    def test_the_ways_a_provider_says_the_history_itself_is_gone(self) -> None:
        # The other direction of the same rule: insisting on the subject may not
        # cost a genuine diagnosis. Each of these names the history and then
        # says it is gone, with and without the identifier in between.
        for line in (
            "no such thread",
            "no such session",
            "no conversation",
            "session not found",
            f"thread {THREAD_ID} was not found",
            "conversation does not exist",
            f'thread "{THREAD_ID}" was deleted',
            "conversation id=c-3 no longer exists",
            "unknown session",
            f"invalid thread {THREAD_ID}",
            "expired conversation",
            # The subject requirement is about what follows the noun, so these
            # pin the endings it has to keep accepting: punctuation that closes
            # the clause, a parenthesized identifier, and a predicate.
            "no such session.",
            f"no such conversation ({THREAD_ID})",
            "no such thread is available",
            # And the identifier requirement is about what a minted name looks
            # like, so these pin the ones it may not refuse along with the
            # compounds: a hyphenated name, a colon-introduced one, and a dotted
            # one, each carrying the digit an ordinary word does not.
            "session s-1 does not exist",
            f"no such session: {THREAD_ID}",
            "conversation v2.thread-9 was deleted",
        ):
            with self.subTest(line=line):
                self.assertTrue(restore_diagnosis.text_diagnoses_missing_history(line))


class RefusedRestoreDispositionTests(unittest.TestCase):
    """Where a refused restore lands among the turn's terminating branches."""

    def _result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "name": "codex",
            "argv": ["codex"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "conversation_restore_refused": f"could not restore {THREAD_ID}",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def test_a_refused_restore_is_named_rather_than_filed_as_a_failed_harness(
        self,
    ) -> None:
        disposition = handoff_guards._terminating_result_disposition(
            self._result(), object(), {}
        )

        self.assertEqual("restore-refused", disposition)

    def test_it_outranks_the_uninspectable_plan_that_would_hide_it(self) -> None:
        # An unreadable plan is a condition that predates this turn, and the
        # ledger still has to be told the conversation is gone.
        disposition = handoff_guards._terminating_result_disposition(
            self._result(), None, {}
        )

        self.assertEqual("restore-refused", disposition)

    def test_a_stopped_turn_still_belongs_to_whatever_stopped_it(self) -> None:
        # None of these say the history is gone: the turn never got far enough
        # to find out, so the conversation keeps its standing.
        for field, expected in (
            ("interrupted", "interrupted"),
            ("timed_out", "timeout"),
            ("aborted", "aborted"),
        ):
            with self.subTest(field=field):
                disposition = handoff_guards._terminating_result_disposition(
                    self._result(**{field: True}), object(), {}
                )

                self.assertEqual(expected, disposition)

    def test_the_turn_record_names_the_refusal_the_run_pauses_under(self) -> None:
        status = turn_execution.turn_status_result(
            result=self._result(),
            status_changes=(),
            pending_completed=(),
            session_id="1",
            runner_signals={},
            plan_error=None,
        )

        self.assertEqual(events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED, status)


class RefusedRestoreLedgerTests(unittest.TestCase):
    """What the refusal writes, and what every later launch makes of it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.ledger = self.tmp / "ledger"
        self.ledger.mkdir()
        self.binding = conversation_resume.turn_binding(
            plan_dir=self.plan_dir, session_id="1", harness="codex", cwd=self.tmp
        )
        reference = conversations.ConversationReference(
            binding=self.binding, native_id=THREAD_ID
        )
        conversations.open_conversation(self.ledger, reference)
        conversations.confirm_conversation(self.ledger, reference)

    def _refuse(self) -> conversation_resume.RestoreRefusal:
        return conversation_resume.refuse_unrestorable_conversation(
            self.ledger,
            binding=self.binding,
            conversation=THREAD_ID,
            detail=f"could not restore conversation {THREAD_ID}: no such thread",
        )

    def _continuation(self, *, granted: bool) -> tuple[Any, Any]:
        authorization = conversation_resume.FreshConversationAuthorization(granted)
        continuation = conversation_resume.plan_turn_continuation(
            self.ledger, binding=self.binding, authorization=authorization
        )
        return continuation, authorization

    def test_a_confirmed_conversation_resumes_until_it_is_refused(self) -> None:
        continuation, authorization = self._continuation(granted=False)

        self.assertEqual(
            conversation_resume.CONTINUATION_RESUMED, continuation.disposition
        )
        self.assertEqual(THREAD_ID, continuation.resume_conversation_id)
        self.assertFalse(authorization.consumed)

    def test_the_refusal_retires_the_entry_as_unusable(self) -> None:
        refusal = self._refuse()

        self.assertTrue(refusal.retired)
        self.assertIsNone(refusal.error)
        # Named by locator, the way every other continuation record names a
        # conversation, so one reader can follow both.
        self.assertEqual(f"codex:{THREAD_ID}", refusal.conversation)
        selection = conversations.select_conversation(self.ledger, self.binding)
        self.assertEqual(conversations.SELECTION_RETIRED, selection.state)
        self.assertIn(
            conversation_resume.RESTORE_REFUSED_RETIREMENT_REASON, selection.detail
        )

    def test_an_ordinary_launch_afterwards_stops_for_recovery(self) -> None:
        self._refuse()

        continuation, authorization = self._continuation(granted=False)

        self.assertTrue(continuation.paused)
        self.assertEqual(
            conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
            continuation.disposition,
        )
        # No latest, no fresh: an unauthorized launch starts nothing at all.
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertFalse(authorization.consumed)

    def test_the_block_survives_every_later_ordinary_launch(self) -> None:
        self._refuse()

        for attempt in range(3):
            with self.subTest(attempt=attempt):
                continuation, _ = self._continuation(granted=False)

                self.assertTrue(continuation.paused)

    def test_one_authorized_launch_supersedes_it_and_opens_fresh(self) -> None:
        self._refuse()

        continuation, authorization = self._continuation(granted=True)

        self.assertEqual(
            conversation_resume.CONTINUATION_FRESH, continuation.disposition
        )
        # Fresh means fresh: the dead conversation is not offered to the turn
        # under another name, and nothing picks "the latest" in its place.
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertTrue(continuation.captures)
        self.assertTrue(authorization.consumed)

    def test_the_authorization_is_still_one_use(self) -> None:
        self._refuse()
        authorization = conversation_resume.FreshConversationAuthorization(True)

        first = conversation_resume.plan_turn_continuation(
            self.ledger, binding=self.binding, authorization=authorization
        )
        second = conversation_resume.plan_turn_continuation(
            self.ledger, binding=self.binding, authorization=authorization
        )

        self.assertEqual(conversation_resume.CONTINUATION_FRESH, first.disposition)
        self.assertTrue(second.paused)

    def test_a_refusal_that_names_no_conversation_writes_nothing(self) -> None:
        refusal = conversation_resume.refuse_unrestorable_conversation(
            self.ledger,
            binding=self.binding,
            conversation=None,
            detail="the conversation was not restored",
        )

        # Reported rather than guessed at: retiring "whatever this binding
        # points at" would discard a record on no evidence.
        self.assertFalse(refusal.retired)
        self.assertIsNotNone(refusal.error)
        selection = conversations.select_conversation(self.ledger, self.binding)
        self.assertEqual(conversations.SELECTION_RESUMABLE, selection.state)

    def test_the_names_it_uses_are_in_the_vocabularies_readers_share(self) -> None:
        self.assertIn(
            conversation_resume.CONTINUATION_RESTORE_REFUSED,
            conversation_resume.CONTINUATION_DISPOSITIONS,
        )
        self.assertIn(
            conversation_resume.RESTORE_REFUSED_RETIREMENT_REASON,
            conversations.RETIREMENT_REASONS,
        )


class _NativeRestoreRun(_RedirectedRun):
    """What both whole-loop stories share: a turn that never got its prompt out.

    Each story runs the same loop and differs only in the sentence the
    app-server answers ``thread/resume`` with, so the hooks that say how far the
    refused turn got, and the two lookups its consequences are read through,
    belong to neither of them.
    """

    def _spawned(self, **kwargs: Any) -> None:
        """Report the spawn, and nothing beyond it.

        A spawn is as far as a refused restore ever gets. The app-server process
        exists and this run is still holding the prompt: ``thread/resume`` is
        answered before ``turn/start``, so nothing was submitted and no worker
        ever read the correction the prompt was carrying.
        """
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(WORKER_PID)

    def _submitted(self, **kwargs: Any) -> None:
        """Report a spawn whose handshake went through and whose prompt went out.

        The pair of hooks is the distinction this fixture exists to exercise: a
        worker that reached here holds the operator's words, and one that
        stopped at :meth:`_spawned` does not.
        """
        self._spawned(**kwargs)
        on_prompt_submitted = kwargs.get("on_prompt_submitted")
        if on_prompt_submitted is not None:
            on_prompt_submitted()

    def _selection(self) -> conversations.ConversationSelection:
        """What a later launch's lookup now finds for this session."""
        return conversations.select_conversation(
            state.plan_state_dir(self.plan_dir, self.state_dir),
            conversation_resume.turn_binding(
                plan_dir=self.plan_dir,
                session_id=SESSION_ID,
                harness="codex",
                cwd=self.repo_root,
            ),
        )

    def _request(self) -> redirect_store.RedirectRequest:
        request = redirect_store.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request


class RefusedRestoreRunTests(_NativeRestoreRun):
    """The whole loop: a conversation opened, corrected, and then refused.

    The fixture's own story supplies the second turn: a worker corrected
    mid-turn, resuming the conversation it was corrected in. Here that resume is
    the one the provider refuses, so the run reaches the refusal the way a real
    run does -- with a confirmed ledger entry, an outstanding correction, and a
    turn that genuinely asked to continue something.

    The refusal itself is a real ``thread/resume`` rejection, produced once in
    :meth:`setUp` by running the real codex adapter against the real refusing
    app-server fixture.
    """

    #: The relaunch mints its own id, exactly as a second ``asl run`` would.
    RECOVERY_RUN_ID = "run-restore-2"

    def setUp(self) -> None:
        super().setUp()
        self.resumed: list[str | None] = []
        self.prompts: list[str] = []
        self.refused = False
        self.refusal = codex_resume_result(
            self.tmp / "app-server", conversation=RUN_CONVERSATION_ID
        )

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.resumed.append(kwargs.get("resume_conversation_id"))
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.harness_calls:
            # The fixture's first turn: it opens the conversation, is corrected
            # mid-turn, and leaves a successor to resume.
            return super()._fake_worker(name, **kwargs)
        if self.refused:
            return self._recovered_turn(name, **kwargs)
        self.refused = True
        self.harness_calls.append(name)
        self._spawned(**kwargs)
        kwargs["abort"].release()
        return dataclasses.replace(self.refusal, name=name)

    def _recovered_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """The authorized relaunch: a new conversation, and the work done in it."""
        self.harness_calls.append(name)
        self._submitted(**kwargs)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation("conv-2")
        # The stale token is the refused attempt's: a receipt for the turn that
        # asked for the dead conversation. It is the one a worker would forge or
        # a crashed successor would leave behind, and it stays refused.
        self._receipt_from_prompt(
            str(kwargs.get("prompt") or ""), harness=name, stale=target(2)
        )
        (self.repo_root / "worked.txt").write_text("done\n", encoding="utf-8")
        self._complete_session()
        kwargs["abort"].release()
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=0,
            stdout="codex completed\n",
            stderr="",
            conversation_id="conv-2",
        )

    def _pause(self) -> dict[str, Any]:
        payload = self.run_loop_once(expect_exit_code=1)
        # Two launches: the corrected turn, and the successor that asked for the
        # conversation and was told it is gone.
        self.assertEqual(["codex", "codex"], self.harness_calls)
        self.assertEqual([None, RUN_CONVERSATION_ID], self.resumed)
        return payload

    def test_the_refused_turn_pauses_for_conversation_recovery(self) -> None:
        payload = self._pause()

        self.assertTrue(payload["paused"])
        self.assertEqual("conversation-recovery-required", payload["reason"])
        refused = payload["details"]["conversation_restore_refused"]
        self.assertEqual(
            conversation_resume.CONTINUATION_RESTORE_REFUSED, refused["disposition"]
        )
        self.assertEqual(f"codex:{RUN_CONVERSATION_ID}", refused["conversation"])
        self.assertTrue(refused["retired"])
        # The app-server's own words, carried the whole way from the transport.
        self.assertIn("no such thread", refused["detail"])

    def test_the_pause_says_how_to_recover_rather_than_only_what_failed(self) -> None:
        payload = self._pause()

        message = payload["pause_message"]
        self.assertIn("no longer holds this history", message)
        self.assertIn("--fresh-conversation", message)

    def test_the_run_records_the_refusal_on_the_continuation_event(self) -> None:
        self._pause()

        refusals = [
            record
            for record in self.events_named(events.CONVERSATION_CONTINUATION_EVENT)
            if record.get("disposition")
            == conversation_resume.CONTINUATION_RESTORE_REFUSED
        ]
        self.assertEqual(1, len(refusals))
        self.assertEqual(f"codex:{RUN_CONVERSATION_ID}", refusals[0]["conversation"])

    def test_the_session_is_left_pending_for_the_launch_that_recovers_it(self) -> None:
        self._pause()

        self.assertEqual("pending", self._session_status())

    def test_the_run_leaves_the_block_on_the_ledger_and_not_only_in_the_pause(
        self,
    ) -> None:
        self._pause()

        selection = self._selection()

        # The pause payload is this run's account of itself and goes away with
        # it. The block has to be where the *next* launch looks.
        self.assertEqual(conversations.SELECTION_RETIRED, selection.state)
        self.assertIn(
            conversation_resume.RESTORE_REFUSED_RETIREMENT_REASON, selection.detail
        )

    def test_a_correction_the_refused_worker_never_received_is_handed_back(
        self,
    ) -> None:
        self._pause()

        request = self._request()
        # The refusal happens in the provider's handshake: a process existed,
        # but it was never given the turn, so the operator's words are still
        # this run's to account for. Calling that delivery -- which a spawn
        # would -- is what left the next launch with nothing to hand over.
        self.assertFalse(request.delivered)
        self.assertFalse(request.reached_a_worker)
        self.assertTrue(request.released)

    def test_the_release_is_recorded_where_the_next_run_reads_it(self) -> None:
        self._pause()

        released = [
            record
            for record in self.events_named(events.REDIRECT_CONTINUATION_EVENT)
            if record.get("outcome") == "released"
        ]
        self.assertEqual(1, len(released))
        self.assertEqual(["op-1"], released[0]["operation_ids"])

    def test_the_next_ordinary_launch_never_reaches_the_harness(self) -> None:
        self._pause()

        payload = self.run_loop_once(
            run_id=self.RECOVERY_RUN_ID, prepare_plan=False, expect_exit_code=1
        )

        # Still two calls, both from the first run. The second stopped before
        # launching anything, which is what "blocked until repaired or
        # explicitly superseded" means.
        self.assertEqual(["codex", "codex"], self.harness_calls)
        self.assertEqual("conversation-recovery-required", payload["reason"])

    def test_the_outstanding_correction_is_offered_to_the_next_run(self) -> None:
        self._pause()

        # What the relaunch sees before it launches anything. Uncertain is where
        # a correction goes to die: nothing may be handed to a second attempt
        # once the journal says a first one was given it, and a spawn counted as
        # delivery is what used to put it there.
        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id=self.RECOVERY_RUN_ID,
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )
        self.assertEqual(("op-1",), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.uncertain_for(SESSION_ID))

    def test_an_authorized_launch_starts_over_and_finishes_the_session(self) -> None:
        payload = self._recover_after_pause()

        # The recovering turn asked for nothing: fresh, not "the latest".
        self.assertEqual([None, RUN_CONVERSATION_ID, None], self.resumed)
        self.assertFalse(payload["paused"])
        self.assertEqual("complete", self._session_status())

    def test_the_fresh_conversation_is_handed_the_correction_it_lost(self) -> None:
        self._recover_after_pause()

        # The point of the whole story. The conversation is gone and cannot be
        # recovered; the operator's words were never spoken to anything, so they
        # are still owed, and this is the turn that owes them.
        self.assertEqual(["codex", "codex", "codex"], self.harness_calls)
        self.assertIn(FEEDBACK, self.prompts[2])
        request = self._request()
        self.assertTrue(request.delivered)
        self.assertTrue(request.recovered)
        self.assertEqual(
            f"{self.RECOVERY_RUN_ID}/{SESSION_ID}/1",
            redirect_ack.attempt_token(request.continuation),
        )

    def test_the_recovering_attempt_acknowledges_on_its_own_attempt(self) -> None:
        self._recover_after_pause()

        assert self.receipt is not None
        self.assertEqual("op-1", self.receipt.operation_id)
        self.assertEqual(f"{self.RECOVERY_RUN_ID}/{SESSION_ID}/1", self.receipt.attempt)
        self.assertFalse(self.receipt.repeated)
        self.assertTrue(self._request().acknowledged)

    def test_a_receipt_naming_the_refused_attempt_is_still_refused(self) -> None:
        self._recover_after_pause()

        # Handing the words to a later attempt does not make the refused one a
        # carrier. The surface will not verify a token against a live attempt it
        # does not name, and the journal will not take a receipt from an attempt
        # it never recorded as the carrier -- the refusal that survives a forged
        # live attempt.
        assert self.stale_refusal is not None
        self.assertEqual(redirect_ack.ERROR_ATTEMPT_MISMATCH, self.stale_refusal.reason)
        with self.assertRaises(redirect_store.RedirectStoreError) as raised:
            redirect_store.acknowledge_request(
                self.plan_dir,
                "op-1",
                attempt=target(2),
                state_dir=self.state_dir,
            )
        self.assertEqual(
            redirect_store.ERROR_CONTINUATION_MISMATCH, raised.exception.reason
        )

    def _recover_after_pause(self) -> dict[str, Any]:
        self._pause()
        return self.run_loop_once(
            run_id=self.RECOVERY_RUN_ID, fresh_conversation=True, prepare_plan=False
        )


class ClaudeRefusedRestoreRunTests(_NativeRestoreRun):
    """The same whole loop, refused by the harness that has no handshake.

    Claude puts ``--resume`` on argv and its prompt on a pipe, so the refusal
    arrives after the write rather than before it -- and a pipe accepts what is
    written to it whether or not the process on the other end will ever read.
    That is the case this class exists for: the write *succeeded*, and the
    correction it carried still reached nobody.

    The turn is driven by the real adapter against a stand-in CLI, so what
    decides whether this run counts the correction as delivered is the adapter's
    own submission seam rather than a hook the fixture chose to call.
    :class:`ClaudeRefusedRestoreBrokenPipeRunTests` runs every claim here again
    against the other half of the same story, where the CLI was gone before the
    write and the pipe said so.
    """

    #: The relaunch mints its own id, exactly as a second ``asl run`` would.
    RECOVERY_RUN_ID = "run-restore-claude-2"

    #: Whether the CLI's stdin still had a reader when the prompt was written.
    #: ``True`` is the case a broken pipe cannot stand in for: nothing failed,
    #: and nothing read the words either.
    PIPE_TOOK_THE_WRITE = True

    def setUp(self) -> None:
        super().setUp()
        self.resumed: list[str | None] = []
        self.prompts: list[str] = []
        #: How many times the loop was told a prompt went out to a worker.
        self.submissions: list[str] = []
        self.refused = False

    # -- the refusing CLI ----------------------------------------------------

    def _refusing_process(self) -> _FakeProcess:
        """A CLI that answers ``--resume`` with the one sentence that retires it."""
        process = _FakeProcess(running=False)
        process.returncode = 1
        process.stderr = _FakePipe(
            f"Error: No conversation found with session ID: {RUN_CONVERSATION_ID}\n"
        )
        if not self.PIPE_TOOK_THE_WRITE:
            process.stdin = _BrokenPipe()
        return process

    def _refused_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """One real claude turn against that CLI, wired to this run's callbacks."""
        adapter = harnesses.ClaudeAdapter(executable="claude")

        def submitted() -> None:
            self.submissions.append(name)
            sink = kwargs.get("on_prompt_submitted")
            if sink is not None:
                sink()

        with (
            mock.patch.object(harnesses, "sweep_enabled", return_value=False),
            mock.patch.object(
                harnesses.subprocess, "Popen", return_value=self._refusing_process()
            ),
        ):
            result = adapter.run(
                cwd=self.repo_root,
                plan_dir=self.plan_dir,
                prompt=str(kwargs.get("prompt") or ""),
                last_message_path=self.tmp / "last-message.txt",
                resume_conversation_id=kwargs.get("resume_conversation_id"),
                # The fixture's process tools are built around one pid, and a
                # fake process's own is not it.
                on_spawn=lambda _pid: self._spawned(**kwargs),
                on_conversation=kwargs.get("on_conversation"),
                on_prompt_submitted=submitted,
                abort=kwargs["abort"],
            )
        return dataclasses.replace(result, name=name)

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.resumed.append(kwargs.get("resume_conversation_id"))
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.harness_calls:
            # The fixture's first turn: it opens the conversation, is corrected
            # mid-turn, and leaves a successor to resume.
            return super()._fake_worker(name, **kwargs)
        if self.refused:
            return self._recovered_turn(name, **kwargs)
        self.refused = True
        self.harness_calls.append(name)
        return self._refused_turn(name, **kwargs)

    def _recovered_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """The authorized relaunch: a new conversation, and the work done in it."""
        self.harness_calls.append(name)
        self._submitted(**kwargs)
        self.submissions.append(name)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation("conv-2")
        # The stale token is the refused attempt's: a receipt for the turn that
        # wrote the prompt into a pipe nobody was reading. It is the one a
        # crashed successor would leave behind, and it stays refused.
        self._receipt_from_prompt(
            str(kwargs.get("prompt") or ""), harness=name, stale=target(2)
        )
        (self.repo_root / "worked.txt").write_text("done\n", encoding="utf-8")
        self._complete_session()
        kwargs["abort"].release()
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=0,
            stdout="claude completed\n",
            stderr="",
            conversation_id="conv-2",
        )

    def _pause(self) -> dict[str, Any]:
        payload = self.run_loop_once(expect_exit_code=1)
        # Two launches: the corrected turn, and the successor whose CLI would
        # not take the conversation the correction was bound to.
        self.assertEqual(["codex", "codex"], self.harness_calls)
        self.assertEqual([None, RUN_CONVERSATION_ID], self.resumed)
        return payload

    def _recover_after_pause(self) -> dict[str, Any]:
        self._pause()
        return self.run_loop_once(
            run_id=self.RECOVERY_RUN_ID, fresh_conversation=True, prepare_plan=False
        )

    # -- the claims ----------------------------------------------------------

    def test_the_refused_turn_pauses_for_conversation_recovery(self) -> None:
        payload = self._pause()

        self.assertTrue(payload["paused"])
        self.assertEqual("conversation-recovery-required", payload["reason"])
        refused = payload["details"]["conversation_restore_refused"]
        self.assertEqual(
            conversation_resume.CONTINUATION_RESTORE_REFUSED, refused["disposition"]
        )
        # The CLI's own words, carried the whole way from the adapter.
        self.assertIn("No conversation found", refused["detail"])

    def test_the_run_was_never_told_the_prompt_reached_a_worker(self) -> None:
        self._pause()

        self.assertEqual([], self.submissions)

    def test_the_correction_the_refused_cli_never_read_is_handed_back(self) -> None:
        self._pause()

        request = self._request()
        self.assertFalse(request.delivered)
        self.assertFalse(request.reached_a_worker)
        self.assertTrue(request.released)

    def test_the_outstanding_correction_is_offered_to_the_next_run(self) -> None:
        self._pause()

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id=self.RECOVERY_RUN_ID,
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )

        self.assertEqual(("op-1",), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.uncertain_for(SESSION_ID))

    def test_the_fresh_conversation_is_handed_the_correction_it_lost(self) -> None:
        payload = self._recover_after_pause()

        self.assertFalse(payload["paused"])
        self.assertEqual("complete", self._session_status())
        # The recovering turn asked for nothing: fresh, not "the latest".
        self.assertEqual([None, RUN_CONVERSATION_ID, None], self.resumed)
        self.assertIn(FEEDBACK, self.prompts[2])
        self.assertTrue(self._request().recovered)

    def test_the_recovering_attempt_acknowledges_on_its_own_attempt(self) -> None:
        self._recover_after_pause()

        assert self.receipt is not None
        self.assertEqual("op-1", self.receipt.operation_id)
        self.assertEqual(f"{self.RECOVERY_RUN_ID}/{SESSION_ID}/1", self.receipt.attempt)
        self.assertFalse(self.receipt.repeated)
        self.assertTrue(self._request().acknowledged)

    def test_a_receipt_naming_the_refused_attempt_is_still_refused(self) -> None:
        self._recover_after_pause()

        assert self.stale_refusal is not None
        self.assertEqual(redirect_ack.ERROR_ATTEMPT_MISMATCH, self.stale_refusal.reason)
        with self.assertRaises(redirect_store.RedirectStoreError) as raised:
            redirect_store.acknowledge_request(
                self.plan_dir,
                "op-1",
                attempt=target(2),
                state_dir=self.state_dir,
            )
        self.assertEqual(
            redirect_store.ERROR_CONTINUATION_MISMATCH, raised.exception.reason
        )


class ClaudeRefusedRestoreBrokenPipeRunTests(ClaudeRefusedRestoreRunTests):
    """The same story, told by the pipe instead of by the CLI's output.

    The CLI was already gone when the prompt was written, so the write itself
    failed. Every claim above has to come out the same way: the two orderings
    are one refusal, and a repair that only covers the one the pipe reports
    leaves the other consuming the operator's words.
    """

    RECOVERY_RUN_ID = "run-restore-claude-broken-2"

    PIPE_TOOK_THE_WRITE = False


class TransientRestoreFailureRunTests(_NativeRestoreRun):
    """The same whole loop, when the provider was merely having a bad minute.

    Identical to :class:`RefusedRestoreRunTests` in every respect but one: the
    sentence ``thread/resume`` is answered with. That is the whole claim -- an
    outage and a disowned history arrive through the same call, on the same
    turn, against the same confirmed ledger entry, and only the second one may
    cost the operator their history.

    The failing turn still fails; what it must not do is decide anything about
    the conversation. So the recovery here is the one nobody has to authorize:
    the provider comes back, and an ordinary relaunch continues the exact
    conversation the failed turn asked for.
    """

    #: The relaunch after the provider recovered, minting its own id.
    REPAIRED_RUN_ID = "run-restore-3"

    #: What ``thread/resume`` is answered with, and the only thing subclasses
    #: vary: every claim below is about what the loop does with the sentence.
    RESUME_ERROR: dict[str, Any] = {
        "code": -32603,
        "message": "temporary service unavailable; retry later",
    }

    #: Where the real app-server for that answer is installed, so two classes
    #: never share one directory.
    FAILURE_DIR = "app-server"

    #: The provider's words the pause must still show an operator.
    REPORTED_WORDS = "temporary service unavailable"

    def setUp(self) -> None:
        super().setUp()
        self.resumed: list[str | None] = []
        self.prompts: list[str] = []
        self.failed = False
        self.failure = codex_resume_result(
            self.tmp / self.FAILURE_DIR,
            conversation=RUN_CONVERSATION_ID,
            mode="resume-rejected",
            resume_error=self.RESUME_ERROR,
        )

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.resumed.append(kwargs.get("resume_conversation_id"))
        self.prompts.append(str(kwargs.get("prompt") or ""))
        if not self.harness_calls:
            return super()._fake_worker(name, **kwargs)
        if self.failed:
            return self._repaired_turn(name, **kwargs)
        self.failed = True
        self.harness_calls.append(name)
        self._spawned(**kwargs)
        kwargs["abort"].release()
        return dataclasses.replace(self.failure, name=name)

    def _repaired_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """The relaunch once the provider is answering again.

        It reports the *same* conversation back, because that is what continuing
        means here: the history was never gone, so nothing was started over.
        """
        self.harness_calls.append(name)
        self._submitted(**kwargs)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation(RUN_CONVERSATION_ID)
        self._receipt_from_prompt(
            str(kwargs.get("prompt") or ""), harness=name, stale=target(2)
        )
        (self.repo_root / "worked.txt").write_text("done\n", encoding="utf-8")
        self._complete_session()
        kwargs["abort"].release()
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=0,
            stdout="codex completed\n",
            stderr="",
            conversation_id=RUN_CONVERSATION_ID,
        )

    def _pause(self) -> dict[str, Any]:
        payload = self.run_loop_once(expect_exit_code=1)
        self.assertEqual(["codex", "codex"], self.harness_calls)
        self.assertEqual([None, RUN_CONVERSATION_ID], self.resumed)
        return payload

    def test_the_failed_turn_stops_the_run_without_calling_it_a_refusal(self) -> None:
        payload = self._pause()

        self.assertTrue(payload["paused"])
        # A failed turn is a failed turn. What it is not is the one disposition
        # that spends the conversation, and the pause that asks the operator to
        # authorize a fresh one is the shape of having spent it.
        self.assertNotEqual("conversation-recovery-required", payload["reason"])
        self.assertNotIn(
            conversation_resume.CONTINUATION_RESTORE_REFUSED,
            [
                record.get("disposition")
                for record in self.events_named(events.CONVERSATION_CONTINUATION_EVENT)
            ],
        )

    def test_the_provider_still_says_what_went_wrong(self) -> None:
        self._pause()

        # Preserving the binding is not the same as swallowing the reason. The
        # words the provider used are what an operator decides on.
        self.assertIn(self.REPORTED_WORDS, self.failure.stderr)
        self.assertIsNone(self.failure.conversation_restore_refused)

    def test_the_conversation_is_still_the_one_a_later_launch_would_resume(
        self,
    ) -> None:
        self._pause()

        selection = self._selection()

        self.assertEqual(conversations.SELECTION_RESUMABLE, selection.state)
        assert selection.reference is not None
        self.assertEqual(RUN_CONVERSATION_ID, selection.reference.native_id)

    def test_an_ordinary_relaunch_continues_the_exact_conversation(self) -> None:
        payload = self._repair_after_pause()

        # The claim in one line: no ``--fresh-conversation``, and the third turn
        # asked for the same history the second one was refused.
        self.assertEqual([None, RUN_CONVERSATION_ID, RUN_CONVERSATION_ID], self.resumed)
        self.assertFalse(payload["paused"])
        self.assertEqual("complete", self._session_status())

    def test_the_correction_the_failed_turn_held_is_still_handed_over(self) -> None:
        self._repair_after_pause()

        # The failing turn never spoke, so the operator's words are still owed --
        # and they are owed to a turn that is continuing, not starting over.
        self.assertEqual(["codex", "codex", "codex"], self.harness_calls)
        self.assertIn(FEEDBACK, self.prompts[2])
        self.assertTrue(self._request().delivered)

    def _repair_after_pause(self) -> dict[str, Any]:
        self._pause()
        return self.run_loop_once(run_id=self.REPAIRED_RUN_ID, prepare_plan=False)


class UnrelatedAbsenceRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a host.

    The failure above announces itself as temporary; this one announces nothing
    of the kind, and an operator repairs it by fixing a name server rather than
    by waiting. It is here because the sentence contains the words "no such",
    which is the entire reason it once retired a conversation the provider was
    still holding -- and because no list of transient words could have stopped
    it, only requiring the absence to be *about a conversation* can.

    Every claim it inherits is the claim that matters: the turn fails, nothing
    is decided about the history, and an ordinary relaunch after the lookup
    starts working again continues the exact conversation.
    """

    REPAIRED_RUN_ID = "run-restore-4"

    RESUME_ERROR = UNRELATED_NO_SUCH_ERROR

    FAILURE_DIR = "app-server-unrelated-absence"

    REPORTED_WORDS = "no such host"


class SessionFileRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a file.

    The nearest miss of all of them. This sentence does not merely contain "no
    such" somewhere -- it names a session and then says "not found", which is
    the exact shape of a disowned history. What it is about is a configuration
    file on the provider's disk, and an operator who puts that file back has a
    provider that continues the conversation it was holding all along.

    So the claim is the one the classifier exists for: a history noun standing in
    front of a piece of infrastructure is not evidence about the history, and
    reading it as evidence costs an operator their accumulated context and makes
    them authorize a fresh conversation to get moving again.
    """

    REPAIRED_RUN_ID = "run-restore-5"

    RESUME_ERROR = SESSION_FILE_ERROR

    FAILURE_DIR = "app-server-session-file"

    REPORTED_WORDS = "session file not found"


class MethodNameRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a method.

    A provider one version behind answers an unrecognized call by naming it, and
    the name is "session.load". The dot is the whole story: it is how a sentence
    ends as well as how a method is spelled, so the sentence was once read as
    "no such session" with the rest discarded -- a conversation retired because
    the operator had not upgraded a binary yet.

    The claims it inherits are the ones that cost real work: the turn fails, the
    ledger keeps the exact conversation, and the relaunch after the upgrade
    continues it and hands over the correction the failed turn was holding.
    """

    REPAIRED_RUN_ID = "run-restore-6"

    RESUME_ERROR = METHOD_NAME_ERROR

    FAILURE_DIR = "app-server-method-name"

    REPORTED_WORDS = "no such session.load method"


class ConfigFileNameRestoreFailureRunTests(TransientRestoreFailureRunTests):
    """The same whole loop, when what the provider could not find was a filename.

    The nearest miss the punctuation rule has to catch on the other side: the
    noun, then "not found", with a word between them that carries a dot the way
    a minted identifier does. Read as a name, "session config.json not found" is
    a history the provider disowned; read as the compound it is, it is a file an
    operator puts back in a minute with the conversation still sitting there.
    """

    REPAIRED_RUN_ID = "run-restore-7"

    RESUME_ERROR = CONFIG_FILE_NAME_ERROR

    FAILURE_DIR = "app-server-config-file-name"

    REPORTED_WORDS = "session config.json not found"


if __name__ == "__main__":
    unittest.main()


class VersionedInfrastructureRestoreTests(TransientRestoreFailureRunTests):
    RESUME_ERROR = {"code": -32000, "message": "session config-v2.json not found"}
    REPORTED_WORDS = "session config-v2.json not found"
    FAILURE_DIR = "versioned-config"
    REPAIRED_RUN_ID = "versioned-config-repaired"


class CompoundMethodRestoreTests(TransientRestoreFailureRunTests):
    RESUME_ERROR = {"code": -32000, "message": "no such session::load method"}
    REPORTED_WORDS = "no such session::load method"
    FAILURE_DIR = "compound-method"
    REPAIRED_RUN_ID = "compound-method-repaired"
