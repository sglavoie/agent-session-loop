"""Tests for test asl native restore recovery claude."""

from __future__ import annotations

import json
import unittest
from typing import Any

from agent_session_loop import (
    harness_claude,
    harnesses,
)
from tests import support_asl_native_restore_recovery as fixtures
from tests.support_asl_native_restore_recovery import THREAD_ID
from tests.test_asl_harness_lifecycle import _FakePipe, _FakeProcess, _TurnTestCase


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
        adapter = harness_claude.ClaudeAdapter(executable="claude")
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
            stdout=self._init_record(THREAD_ID),
            stderr="",
            returncode=0,
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
            stdin=fixtures._BrokenPipe(),
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


if __name__ == "__main__":
    unittest.main()
