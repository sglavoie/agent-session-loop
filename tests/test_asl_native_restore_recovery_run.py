"""Tests for test asl native restore recovery run."""

from __future__ import annotations

import dataclasses
import unittest
from typing import Any

from agent_session_loop import (
    conversation_resume,
    conversations,
    events,
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


class RefusedRestoreRunTests(fixtures._NativeRestoreRun):
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
        self.refusal = fixtures.codex_resume_result(
            self.tmp / "app-server",
            conversation=RUN_CONVERSATION_ID,
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
        self.assertEqual(
            [None, RUN_CONVERSATION_ID],
            self.resumed,
        )
        return payload

    def test_the_refused_turn_pauses_for_conversation_recovery(self) -> None:
        payload = self._pause()

        self.assertTrue(payload["paused"])
        self.assertEqual("conversation-recovery-required", payload["reason"])
        refused = payload["details"]["conversation_restore_refused"]
        self.assertEqual(
            conversation_resume.CONTINUATION_RESTORE_REFUSED, refused["disposition"]
        )
        self.assertEqual(
            f"codex:{RUN_CONVERSATION_ID}",
            refused["conversation"],
        )
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
        self.assertEqual(
            f"codex:{RUN_CONVERSATION_ID}",
            refusals[0]["conversation"],
        )

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
        self.assertEqual(
            [None, RUN_CONVERSATION_ID, None],
            self.resumed,
        )
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
            redirect_ack_attempt.attempt_token(request.continuation),
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

    def _recover_after_pause(self) -> dict[str, Any]:
        self._pause()
        return self.run_loop_once(
            run_id=self.RECOVERY_RUN_ID, fresh_conversation=True, prepare_plan=False
        )


if __name__ == "__main__":
    unittest.main()
