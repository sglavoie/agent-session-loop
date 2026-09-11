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
    harnesses,
    redirect_store,
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
    SESSION_ID,
    WORKER_PID,
    _RedirectedRun,
)

#: The native conversation the adapter-level refusals ask for.
THREAD_ID = "thread-77"

#: The conversation the whole-loop run opens on its first turn and is refused on
#: its second. Fixed by the fixture's worker, not chosen here.
RUN_CONVERSATION_ID = "conv-1"


def _install(fixture: Path, destination: Path) -> Path:
    shutil.copyfile(fixture, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
    return destination


def refused_codex_resume(tmp: Path, *, conversation: str) -> harnesses.HarnessResult:
    """One real codex resume against an app-server that refuses to restore it.

    A module-level function rather than a test method because two very different
    cases need the same result: the adapter claims below, and the whole-loop run
    that has to be driven by a result the transport actually produces.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    executable = _install(FAKE_APP_SERVER_FIXTURE, tmp / "codex-app-server")
    adapter = harnesses.CodexAdapter(executable=str(executable))
    plan_dir = tmp / "plan"
    plan_dir.mkdir(exist_ok=True)
    last_message = tmp / "last-message.txt"
    with (
        mock.patch.object(harnesses, "sweep_enabled", return_value=False),
        mock.patch.dict(
            "os.environ",
            {
                "FAKE_APP_SERVER_MODE": "resume-unknown",
                "FAKE_APP_SERVER_RECORD": str(tmp / "record.json"),
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

    def _run(self, mode: str, *, resume: str | None = THREAD_ID) -> Any:
        os.environ["FAKE_APP_SERVER_MODE"] = mode
        executable = _install(FAKE_APP_SERVER_FIXTURE, self.tmp / "codex-app-server")
        adapter = harnesses.CodexAdapter(executable=str(executable))
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
    ) -> Any:
        adapter = harnesses.ClaudeAdapter(executable="claude")
        process = _FakeProcess(running=False)
        process.returncode = returncode
        process.stdout = _FakePipe(stdout)
        process.stderr = _FakePipe(stderr)
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

    def test_a_turn_that_ran_and_succeeded_is_not_a_refusal(self) -> None:
        result = self._resume(
            stdout=self._init_record(THREAD_ID), stderr="", returncode=0
        )

        self.assertIsNone(result.conversation_restore_refused)


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


class RefusedRestoreRunTests(_RedirectedRun):
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
        self.refused = False
        self.refusal = refused_codex_resume(
            self.tmp / "app-server", conversation=RUN_CONVERSATION_ID
        )

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.resumed.append(kwargs.get("resume_conversation_id"))
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

    def _spawned(self, **kwargs: Any) -> None:
        """Report the spawn, which is what records a carry as delivered.

        A refused restore happens in the provider's handshake, after the process
        holding the prompt exists -- so the correction it was carrying has
        reached a worker, and the run's account of it has to say so.
        """
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(WORKER_PID)

    def _recovered_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """The authorized relaunch: a new conversation, and the work done in it."""
        self.harness_calls.append(name)
        self._spawned(**kwargs)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation("conv-2")
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

    def test_a_correction_the_refused_worker_already_held_is_not_handed_back(
        self,
    ) -> None:
        self._pause()

        request = redirect_store.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        # Unlike the pauses that happen *before* a launch, this one happens in
        # the provider's handshake -- the process holding the operator's words
        # already existed. Handing them back would offer a second worker a
        # correction a first one was given.
        self.assertTrue(request.reached_a_worker)
        self.assertTrue(request.delivered)
        self.assertFalse(request.released)

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

    def test_an_authorized_launch_starts_over_and_finishes_the_session(self) -> None:
        self._pause()

        payload = self.run_loop_once(
            run_id=self.RECOVERY_RUN_ID, fresh_conversation=True, prepare_plan=False
        )

        # The recovering turn asked for nothing: fresh, not "the latest".
        self.assertEqual([None, RUN_CONVERSATION_ID, None], self.resumed)
        self.assertFalse(payload["paused"])
        self.assertEqual("complete", self._session_status())


if __name__ == "__main__":
    unittest.main()
