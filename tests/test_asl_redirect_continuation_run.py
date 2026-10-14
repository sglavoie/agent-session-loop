"""A redirected turn end to end through the run loop, and its submitted correction.

Split from tests/test_asl_redirect_continuation.py."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from agent_session_loop import (
    events,
    harnesses,
    redirect_ack,
    redirect_continuation,
    redirect_recovery,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_vocab,
    turn_dispositions,
)

from .asl_redirect_continuation_support import (
    FEEDBACK,
    RUN_ID,
    SESSION_ID,
    WORKER_PID,
    target,
)
from .asl_redirect_run_support import _RedirectedRun


class RedirectedRunTests(_RedirectedRun):
    """A fake worker, corrected mid-turn, resuming with what it was told.

    The one case that cannot be made from parts: the loop has to reach the
    disposition, carry the correction across the iteration, and not mistake any
    of it for a failure.
    """

    def test_the_corrected_session_is_attempted_again_with_its_correction(
        self,
    ) -> None:
        payload = self.run_loop_once()

        self.assertEqual(["codex", "codex"], self.harness_calls)
        continuations = self.events_named(events.REDIRECT_CONTINUATION_EVENT)
        # `carried` falls between the first two because binding a correction to
        # the attempt that will deliver it happens while that attempt's prompt
        # is being assembled — after the predecessor is settled, before the
        # resumed turn is planned. `launching` is the last thing said while the
        # words demonstrably have not gone out, so it sits immediately before
        # the launch and is what makes the silence after a crash readable.
        # `delivered` is last because it is the one fact none of the others
        # establish: a worker was actually handed the prompt. Not spawned
        # holding it — a spawned worker can still be refused the turn it asked
        # for, and then it read nothing.
        self.assertEqual(
            [
                turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
                "carried",
                "resumed",
                "launching",
                "delivered",
            ],
            [record["outcome"] for record in continuations],
        )
        launching = continuations[3]
        self.assertEqual(["op-1"], launching["operation_ids"])
        self.assertEqual(f"{RUN_ID}/{SESSION_ID}/2", launching["attempt"])
        self.assertEqual([], launching["refused"])
        delivered = continuations[4]
        self.assertEqual(["op-1"], delivered["operation_ids"])
        self.assertEqual(f"{RUN_ID}/{SESSION_ID}/2", delivered["attempt"])
        self.assertEqual([], delivered["refused"])
        carried = continuations[1]
        self.assertEqual(
            ["op-1"], [item["operation_id"] for item in carried["carried"]]
        )
        self.assertEqual(f"{RUN_ID}/{SESSION_ID}/2", carried["attempt"])
        resumed = continuations[2]
        self.assertEqual(SESSION_ID, resumed["session_id"])
        self.assertEqual(2, resumed["turn_index"])
        self.assertEqual(["op-1"], resumed["interrupted_operations"])
        self.assertEqual("codex", resumed["harness"])
        self.assertEqual(1, resumed["redirect"]["attempt"])
        self.assertNotIn(FEEDBACK, json.dumps(continuations))
        self.assertFalse(payload["paused"])
        self.assertEqual([1], payload["completed_sessions"])

    def test_the_resumed_attempt_continues_the_conversation_it_corrected(self) -> None:
        self.run_loop_once()

        continuation = self.events_named(events.CONVERSATION_CONTINUATION_EVENT)[1]
        self.assertEqual("resumed", continuation["disposition"])
        self.assertEqual("conv-1", continuation["conversation"])
        self.assertEqual("codex", continuation["harness"])
        self.assertEqual(["op-1"], continuation["intent"]["interrupted_operations"])

    def test_the_redirected_turn_advances_nothing(self) -> None:
        payload = self.run_loop_once()

        # The second turn started with the session exactly as the first one
        # left it: the correction bought another attempt, not progress.
        self.assertEqual("pending", self.status_at_resume)
        turns = payload["turns"]
        self.assertEqual(2, len(turns))
        self.assertEqual(
            [SESSION_ID, SESSION_ID], [str(turn["session"]) for turn in turns]
        )
        self.assertTrue(
            redirect_continuation.turn_was_redirected(turns[0]),
            turns[0].get("redirect"),
        )
        self.assertFalse(redirect_continuation.turn_was_redirected(turns[1]))

    def test_the_redirected_turn_spends_no_failure_budget(self) -> None:
        self.run_loop_once()

        # A redirect is not a failed turn, so none of the recoveries a failed
        # turn earns may be charged for it.
        for name in (
            events.TURN_HANDOFF_CORRECTION_EVENT,
            events.SESSION_REOPENED_EVENT,
            events.TURN_REJECTED_EVENT,
            "spark_fallback",
        ):
            with self.subTest(event=name):
                self.assertEqual([], self.events_named(name))

    def test_the_redirected_turn_creates_no_commit(self) -> None:
        self.run_loop_once()

        # Commits are enabled for this run, so the absence is the branch's and
        # not the mode's: the one commit belongs to the session that completed.
        self.assertEqual(0, self.commits_before_resume)
        self.assertEqual(
            [SESSION_ID], [call["session_id"] for call in self.commit_calls]
        )


class SubmittedCorrectionTests(_RedirectedRun):
    """A correction that did go out, to a worker whose turn then failed.

    The mirror image of the recovery cases, and the reason they may not be
    written as "hand it back whenever the turn ends badly". These words were
    read. Handing them to a second worker would repeat an instruction that has
    already had its effect, which is why a submitted correction stays spent no
    matter what becomes of the turn that carried it.
    """

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        if not self.harness_calls:
            return super()._fake_worker(name, **kwargs)
        self.harness_calls.append(name)
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(WORKER_PID)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation("conv-1")
        on_prompt_submitted = kwargs.get("on_prompt_submitted")
        if on_prompt_submitted is not None:
            on_prompt_submitted()
        kwargs["abort"].release()
        # Handed the turn, and then lost: the worker read the correction and
        # died with the work unfinished.
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=1,
            stdout="",
            stderr="the worker died mid-turn\n",
            conversation_id="conv-1",
        )

    def _request(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request

    def test_a_failed_turn_does_not_hand_back_what_it_was_given(self) -> None:
        self.run_loop_once(expect_exit_code=1)

        request = self._request()
        self.assertTrue(request.delivered)
        self.assertTrue(request.reached_a_worker)
        self.assertFalse(request.released)
        self.assertEqual(
            [],
            [
                record
                for record in self.events_named(events.REDIRECT_CONTINUATION_EVENT)
                if record["outcome"] == "released"
            ],
        )

    def test_the_next_run_is_not_offered_the_words_again(self) -> None:
        self.run_loop_once(expect_exit_code=1)

        # Uncertain, and uncertain is terminal: nothing replays a correction a
        # worker may have acted on. Automatic recovery is for the words that
        # provably reached no one.
        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id="run-redirect-2",
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )
        self.assertEqual((), report.recoverable_for(SESSION_ID))
        self.assertEqual(("op-1",), report.uncertain_for(SESSION_ID))


class PrelaunchPauseRecoveryTests(_RedirectedRun):
    """The gap between assembling a prompt and spawning the worker that holds it.

    The same run as :class:`RedirectedRunTests` up to the moment the successor
    would launch, and then a worker whose conversation was never confirmed, so
    the run refuses to open a second one and pauses. Nothing was ever told the
    correction. The claims are what the operator is owed afterwards: the words
    are still outstanding, the journal does not say otherwise, and the one-use
    authorization to start fresh is what finally delivers them.
    """

    #: The relaunch mints its own id, exactly as a second ``asl run`` would.
    RECOVERY_RUN_ID = "run-redirect-2"

    def setUp(self) -> None:
        super().setUp()
        self.prompts: list[str] = []

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """The shared worker, with the first turn's conversation left unconfirmed.

        ``CANCEL_STAGE_HANDSHAKE`` is the harness saying the conversation id it
        reported never carried accepted work. The successor's prompt is built
        and its correction bound to it regardless -- that happens first -- and
        only then does conversation recovery refuse the launch.
        """
        prompt = str(kwargs.get("prompt") or "")
        self.prompts.append(prompt)
        result = super()._fake_worker(name, **kwargs)
        if len(self.harness_calls) == 1:
            return dataclasses.replace(
                result, cancel_stage=harnesses.CANCEL_STAGE_HANDSHAKE
            )
        # The stale token is the receipt the paused run's successor would have
        # written if it had ever started: it names an attempt that no longer
        # exists, and is refused for the same reason a stale worker's would be.
        self._receipt_from_prompt(prompt, harness=name, stale=target(2))
        return result

    def _request(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        ).request("op-1")
        assert request is not None
        return request

    def _pause(self) -> dict[str, Any]:
        """The first run, up to and including the refusal to launch."""
        payload = self.run_loop_once(expect_exit_code=1)
        self.assertTrue(payload["paused"])
        self.assertEqual("conversation-recovery-required", payload["reason"])
        self.assertEqual(["codex"], self.harness_calls)
        return payload

    def _recover(self) -> dict[str, Any]:
        """The authorized relaunch: a new run id, one fresh conversation."""
        return self.run_loop_once(
            run_id=self.RECOVERY_RUN_ID,
            fresh_conversation=True,
            prepare_plan=False,
        )

    def test_a_pause_before_launch_leaves_the_correction_undelivered(self) -> None:
        self._pause()

        request = self._request()
        self.assertTrue(request.accepted)
        # The three facts that have to move together. Delivery is what no
        # worker established here, so the carry that anticipated it is withdrawn
        # rather than left standing as a record of an attempt that never ran.
        self.assertFalse(request.delivered)
        self.assertFalse(request.reached_a_worker)
        self.assertFalse(request.carried)
        self.assertTrue(request.released)
        self.assertEqual(
            redirect_ack.attempt_token(target(2)),
            redirect_ack.attempt_token(request.released_from),
        )

    def test_the_paused_run_hands_the_correction_back_itself(self) -> None:
        self._pause()

        # The run that bound the correction is the one that releases it, before
        # it exits. Leaving that to the next run's startup reconciliation would
        # make recovery depend on a relaunch ever happening.
        continuations = self.events_named(events.REDIRECT_CONTINUATION_EVENT)
        self.assertEqual(
            [turn_dispositions.REDIRECT_OUTCOME_CONTINUED, "carried", "released"],
            [record["outcome"] for record in continuations],
        )
        released = continuations[2]
        self.assertEqual(["op-1"], released["operation_ids"])
        self.assertEqual(f"{RUN_ID}/{SESSION_ID}/2", released["attempt"])
        self.assertEqual([], released["refused"])
        self.assertNotIn(FEEDBACK, json.dumps(continuations))

    def test_the_outstanding_correction_is_offered_to_the_next_run(self) -> None:
        self._pause()

        # What a relaunch sees before it does anything: a correction owed to the
        # session, not one in doubt. Uncertainty is the failure this repair is
        # about -- it is terminal for the words, because nothing may be handed
        # to a second attempt once the journal says a first one was given it.
        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id=self.RECOVERY_RUN_ID,
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
        )
        self.assertEqual(("op-1",), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.uncertain_for(SESSION_ID))

    def test_an_authorized_fresh_start_is_told_what_the_pause_withheld(self) -> None:
        payload = self._recover_after_pause()

        self.assertEqual(["codex", "codex"], self.harness_calls)
        self.assertIn(FEEDBACK, self.prompts[1])
        request = self._request()
        # Carried again, by a different run than the one that accepted it, and
        # this time by an attempt that actually launched.
        self.assertTrue(request.carried)
        self.assertTrue(request.delivered)
        self.assertTrue(request.recovered)
        self.assertEqual(
            f"{self.RECOVERY_RUN_ID}/{SESSION_ID}/1",
            redirect_ack.attempt_token(request.continuation),
        )
        self.assertFalse(payload["paused"])
        self.assertEqual([1], payload["completed_sessions"])

    def test_the_fresh_attempt_acknowledges_on_its_own_attempt(self) -> None:
        self._recover_after_pause()

        assert self.receipt is not None
        self.assertEqual("op-1", self.receipt.operation_id)
        self.assertEqual(f"{self.RECOVERY_RUN_ID}/{SESSION_ID}/1", self.receipt.attempt)
        self.assertFalse(self.receipt.repeated)
        self.assertTrue(self._request().acknowledged)

    def test_a_receipt_naming_the_attempt_that_never_launched_is_refused(self) -> None:
        self._recover_after_pause()

        # Refused twice over, and both refusals matter: the surface will not
        # verify a token against a live attempt it does not name, and the
        # journal will not take a receipt from an attempt it never recorded as
        # the carrier -- which is the one that survives a forged live attempt.
        assert self.stale_refusal is not None
        self.assertEqual(redirect_ack.ERROR_ATTEMPT_MISMATCH, self.stale_refusal.reason)
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
        return self._recover()
