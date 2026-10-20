"""Asl turn dispositions pause regressions."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    mailbox_vocabulary,
    run_coordination_constants,
    run_coordination_mailbox_question,
    run_coordination_mailbox_wait,
    run_coordination_wait_models,
    run_coordination_wait_state,
    turn_dispositions,
)

from .support_asl_turn_dispositions import (
    SESSION_ID,
    STATE_DIR_ARG,
    DispositionTestCase,
    fake_message,
)


class RunnerPauseTests(DispositionTestCase):
    """A worker's own pause, and the answer that can undo it."""

    def setUp(self) -> None:
        super().setUp()
        self.prepared: Any = mock.Mock()
        self.prepared.question = fake_message(id="question-1")
        self.prepared.context = mock.Mock()
        self.wait_calls: list[dict[str, Any]] = []

    def settle(
        self,
        *,
        resolution_for_turn: Any = None,
        can_wait: bool = True,
        wait_result: Any = None,
        prepare_error: Exception | None = None,
        mailbox_enabled: bool = True,
    ) -> Any:
        coordinator = mock.Mock()
        coordinator.return_value.run.return_value = wait_result

        def prepare_wait(**kwargs: Any) -> Any:
            self.wait_calls.append(kwargs)
            if prepare_error is not None:
                raise prepare_error
            return self.prepared

        with (
            mock.patch.object(
                run_coordination_mailbox_question,
                "worker_pause_resolution_for_turn",
                return_value=resolution_for_turn,
            ),
            mock.patch.object(
                run_coordination_mailbox_question,
                "runner_pause_can_wait_for_mailbox_reply",
                return_value=can_wait,
            ),
            mock.patch.object(
                run_coordination_mailbox_wait, "MailboxWaitCoordinator", coordinator
            ),
            mock.patch.object(
                run_coordination_wait_state,
                "record_mailbox_reply_wait_preparation_failed",
            ) as failed,
        ):
            self.coordinator = coordinator
            self.preparation_failed = failed
            return turn_dispositions.settle_runner_pause(
                self.ctx,
                after=self.after,
                session_id=SESSION_ID,
                turn_index=1,
                result=self.result(returncode=0),
                turn_payload={
                    "runner_pause": "blocked - needs a decision",
                    "runner_pause_source": "worker",
                },
                session_details=self.session_details,
                paused_status_repair={"repaired": True},
                mailbox_enabled=mailbox_enabled,
                mailbox_poll=mock.Mock(),
                plan_state=self.tmp_dir / "state",
                state_path=self.tmp_dir / "state" / "state.json",
                state_dir_arg=STATE_DIR_ARG,
                prepare_wait=prepare_wait,
                wait_for_reply=mock.Mock(),
                finish_lifecycle=mock.Mock(),
            )

    def test_an_answer_that_beat_the_pause_short_circuits_the_wait(self) -> None:
        resolution = fake_message(
            id="res-1",
            kind=mailbox_vocabulary.KIND_RESOLUTION,
            to="question-1",
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            note="carry on",
        )
        outcome = self.settle(resolution_for_turn=resolution)
        self.assertIsNone(outcome)
        self.assertEqual([], self.wait_calls)
        self.coordinator.assert_not_called()
        recorded = self.ctx.pauses[0]["details"]["mailbox_resolution"]
        self.assertEqual("question-1", recorded["question_id"])
        self.assertEqual("res-1", recorded["resolution_message_id"])
        self.assertEqual(len("carry on"), recorded["note_length"])
        self.assertEqual(
            mailbox_vocabulary.WAITER_ORIGIN_WORKER, recorded["waiter_origin"]
        )

    def test_the_pause_carries_its_source_and_status_repair(self) -> None:
        self.settle(can_wait=False)
        details = self.ctx.pauses[0]["details"]
        self.assertEqual("worker", details["source"])
        self.assertEqual({"repaired": True}, details["handoff_status_repair"])
        self.assertEqual("blocked - needs a decision", self.ctx.pauses[0]["message"])
        self.assertEqual([self.after], self.ctx.final_snapshots)

    def test_a_pause_that_cannot_be_answered_never_enters_the_wait(self) -> None:
        outcome = self.settle(can_wait=False)
        self.assertIsNone(outcome)
        self.assertEqual([], self.wait_calls)
        self.assertFalse(self.ctx.idle_baseline_pinned)

    def test_a_reply_produces_the_continuation_and_pins_the_idle_baseline(
        self,
    ) -> None:
        reply = fake_message(id="reply-1", seq=11, text="do this instead")
        outcome = self.settle(
            wait_result=run_coordination_wait_models.MailboxWaitResult(
                reason=run_coordination_constants.WAIT_REASON_REPLY,
                pending_outbox=0,
                reply=reply,
            )
        )
        assert outcome is not None
        self.assertIs(self.after, outcome.snapshot)
        self.assertIsNone(outcome.pending_resolution)
        assert outcome.pending_reply is not None
        self.assertEqual("do this instead", outcome.pending_reply.text)
        self.assertEqual("question-1", outcome.pending_reply.question_id)
        self.assertEqual("reply-1", outcome.pending_reply.reply_message_id)
        self.assertEqual(11, outcome.pending_reply.reply_seq)
        self.assertFalse(outcome.pending_reply.process_start)
        self.assertEqual([], self.ctx.pauses)
        self.assertTrue(self.ctx.idle_baseline_pinned)
        self.assertIsNotNone(self.ctx.runner_work_finished_at)

    def test_a_continue_resolution_produces_an_answerless_continuation(self) -> None:
        resolution = fake_message(
            id="res-2",
            seq=12,
            kind=mailbox_vocabulary.KIND_RESOLUTION,
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            note="proceed",
        )
        outcome = self.settle(
            wait_result=run_coordination_wait_models.MailboxWaitResult(
                reason=run_coordination_constants.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution=resolution,
            )
        )
        assert outcome is not None
        self.assertIsNone(outcome.pending_reply)
        assert outcome.pending_resolution is not None
        self.assertEqual("proceed", outcome.pending_resolution.note)
        self.assertEqual("question-1", outcome.pending_resolution.question_id)
        self.assertEqual("res-2", outcome.pending_resolution.resolution_message_id)
        self.assertEqual([], self.ctx.pauses)

    def test_a_pause_resolution_leaves_the_run_paused(self) -> None:
        resolution = fake_message(
            id="res-3",
            kind=mailbox_vocabulary.KIND_RESOLUTION,
            action=mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
        )
        outcome = self.settle(
            wait_result=run_coordination_wait_models.MailboxWaitResult(
                reason=run_coordination_constants.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution=resolution,
            )
        )
        self.assertIsNone(outcome)
        self.assertEqual(1, len(self.ctx.pauses))
        closure = self.ctx.pauses[0]["details"]["mailbox_resolution"]
        self.assertEqual("question-1", closure["question_id"])
        self.assertEqual("res-3", closure["resolution_message_id"])
        self.assertEqual("pause", closure["action"])
        self.assertEqual(
            mailbox_vocabulary.WAITER_ORIGIN_RUNNER, closure["waiter_origin"]
        )

    def test_a_timed_out_wait_leaves_the_run_paused(self) -> None:
        outcome = self.settle(
            wait_result=run_coordination_wait_models.MailboxWaitResult(
                reason=run_coordination_constants.WAIT_REASON_TIMEOUT, pending_outbox=0
            )
        )
        self.assertIsNone(outcome)
        self.assertEqual(1, len(self.ctx.pauses))

    def test_an_unpreparable_wait_is_recorded_and_the_run_pauses(self) -> None:
        outcome = self.settle(
            prepare_error=run_coordination_wait_models.MailboxWaitUnavailable(
                "no-state-path"
            )
        )
        self.assertIsNone(outcome)
        self.coordinator.assert_not_called()
        self.preparation_failed.assert_called_once()
        self.assertEqual(
            "no-state-path", self.preparation_failed.call_args.kwargs["reason"]
        )
        self.assertFalse(self.ctx.idle_baseline_pinned)
        self.assertEqual(1, len(self.ctx.pauses))

    def test_a_disabled_mailbox_asks_for_no_prior_resolution(self) -> None:
        with mock.patch.object(
            run_coordination_mailbox_question, "worker_pause_resolution_for_turn"
        ) as lookup:
            self.settle(mailbox_enabled=False, can_wait=False)
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
