"""Tests for test asl native restore recovery claude run."""

from __future__ import annotations

import dataclasses
import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    conversation_resume,
    harness_claude,
    harness_execution,
    harness_process,
    harnesses,
    redirect_ack_attempt,
    redirect_recovery,
    redirect_store_delivery,
    redirect_store_models,
    redirect_store_vocab,
)
from tests import support_asl_native_restore_recovery as fixtures
from tests.asl_redirect_continuation_support import (
    FEEDBACK,
    SESSION_ID,
    target,
)
from tests.support_asl_native_restore_recovery import RUN_CONVERSATION_ID
from tests.test_asl_harness_lifecycle import _FakePipe, _FakeProcess


class ClaudeRefusedRestoreRunTests(fixtures._NativeRestoreRun):
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
            process.stdin = fixtures._BrokenPipe()
        return process

    def _refused_turn(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """One real claude turn against that CLI, wired to this run's callbacks."""
        adapter = harness_claude.ClaudeAdapter(executable="claude")

        def submitted() -> None:
            self.submissions.append(name)
            sink = kwargs.get("on_prompt_submitted")
            if sink is not None:
                sink()

        with (
            mock.patch.object(harness_process, "sweep_enabled", return_value=False),
            mock.patch.object(
                harness_execution.subprocess,
                "Popen",
                return_value=self._refusing_process(),
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
        self.assertEqual(
            [None, RUN_CONVERSATION_ID],
            self.resumed,
        )
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
        self.assertEqual(
            [None, RUN_CONVERSATION_ID, None],
            self.resumed,
        )
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
        self.assertEqual(
            redirect_ack_attempt.ERROR_ATTEMPT_MISMATCH, self.stale_refusal.reason
        )
        with self.assertRaises(redirect_store_models.RedirectStoreError) as raised:
            redirect_store_delivery.acknowledge_request(
                self.plan_dir,
                "op-1",
                attempt=target(2),
                state_dir=self.state_dir,
            )
        self.assertEqual(
            redirect_store_vocab.ERROR_CONTINUATION_MISMATCH, raised.exception.reason
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


if __name__ == "__main__":
    unittest.main()
