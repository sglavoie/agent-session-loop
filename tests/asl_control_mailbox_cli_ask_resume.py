"""Resume cases for MailboxCliTestsPart6."""

from __future__ import annotations

import io
import os
import signal
import time
from contextlib import redirect_stdout
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_cli_ask,
    mailbox_cli_constants,
    mailbox_vocabulary,
    run_errors,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase
from tests.support import (
    _write_lock_file,
    _write_state_payload,
)


class MailboxCliTestsPart6(MailboxCliTestsBase):
    def test_ask_stamps_the_live_run_and_degrades_without_one(self) -> None:
        code, unattributed = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(text="asked before any run wrote state"),
        )
        self.assertEqual(0, code)

        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": "run-live", "ok": True, "paused": False},
        )
        code, attributed = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(text="asked by the live run"),
        )
        self.assertEqual(0, code)

        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-live",
                "ok": True,
                "paused": False,
                "active_turn": {
                    "run_id": "run-live",
                    "session_id": "1",
                    "turn_index": 4,
                },
            },
        )
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=os.getpid(),
        )
        code, turn_owned = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(text="asked by the active worker turn"),
        )
        self.assertEqual(0, code)

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        by_id = {question.id: question for question in view.messages}
        self.assertIsNone(by_id[unattributed["question_id"]].run_id)
        self.assertEqual("run-live", by_id[attributed["question_id"]].run_id)
        exact = by_id[turn_owned["question_id"]]
        self.assertEqual("run-live", exact.run_id)
        self.assertEqual("1", exact.session_id)
        self.assertEqual(4, exact.turn_index)
        self.assertEqual(mailbox_vocabulary.WAITER_ORIGIN_WORKER, exact.waiter_origin)
        self.assertEqual(
            (by_id[attributed["question_id"]], exact),
            view.open_questions("run-live"),
        )

    def test_ask_new_then_resume_is_idempotent(self) -> None:
        code, created = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(text="Need a decision"),
        )
        self.assertEqual(0, code)
        self.assertTrue(created["created"])
        question_id = created["question_id"]

        code, resumed = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(question_id=question_id),
        )
        self.assertEqual(0, code)
        self.assertFalse(resumed["created"])
        self.assertEqual(question_id, resumed["question_id"])
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(1, view.counts.questions)

    def test_interrupted_ask_wait_ends_its_wait_and_can_be_resumed(self) -> None:
        with (
            mock.patch.object(time, "sleep", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
            redirect_stdout(io.StringIO()),
        ):
            mailbox_cli_ask.ask_from_args(
                self.args(text="Need a decision", wait=True, wait_timeout=60)
            )

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        question = next(
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_QUESTION
        )
        wait_start = next(
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_WAIT_START
        )
        wait_end = view.messages[-1]
        self.assertEqual(mailbox_vocabulary.KIND_WAIT_END, wait_end.kind)
        self.assertEqual(wait_start.id, wait_end.wait_id)
        self.assertEqual(
            mailbox_vocabulary.WAIT_END_REASON_ERROR, wait_end.wait_end_reason
        )
        self.assertIsNone(view.active_wait_for(question.id))
        self.assertNotEqual("waiting", view.delivery_state_for(question.id))

        code, resumed = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(question_id=question.id, wait=True, wait_timeout=0),
        )
        self.assertEqual(0, code)
        self.assertTrue(resumed["timed_out"])
        self.assertIsNotNone(resumed["wait_start"])
        self.assertNotEqual(wait_start.id, resumed["wait_start"]["id"])

    def test_sigterm_during_ask_wait_ends_its_wait(self) -> None:
        # A harness timing out the tool call sends SIGTERM, not SIGINT.
        def terminate(_seconds: float) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        previous = signal.getsignal(signal.SIGTERM)
        with (
            mock.patch.object(time, "sleep", side_effect=terminate),
            self.assertRaises(run_errors.RunTerminatedBySignal),
            redirect_stdout(io.StringIO()),
        ):
            mailbox_cli_ask.ask_from_args(
                self.args(text="Need a decision", wait=True, wait_timeout=60)
            )
        self.assertIs(previous, signal.getsignal(signal.SIGTERM))

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        wait_end = view.messages[-1]
        self.assertEqual(mailbox_vocabulary.KIND_WAIT_END, wait_end.kind)
        self.assertEqual(
            mailbox_vocabulary.WAIT_END_REASON_ERROR, wait_end.wait_end_reason
        )

    def test_ask_resume_returns_an_existing_reply_without_appending(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Need a decision",
            state_dir=self.state_dir,
        )
        reply = mailbox_appenders.append_reply(
            self.plan_dir,
            question.id,
            "Proceed.",
            state_dir=self.state_dir,
        )
        code, payload = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(question_id=question.id, wait=True),
        )
        self.assertEqual(0, code)
        self.assertFalse(payload["created"])
        self.assertTrue(payload["answered"])
        self.assertFalse(payload["resolved"])
        self.assertFalse(payload["pause_required"])
        self.assertFalse(payload["timed_out"])
        self.assertEqual(
            mailbox_cli_constants.DEFAULT_ASK_WAIT_TIMEOUT_SECONDS,
            payload["wait_timeout"],
        )
        self.assertEqual(reply.id, payload["reply"]["id"])
        self.assertIsNone(payload["resolution"])
        self.assertEqual(
            1,
            mailbox.read_mailbox(
                self.plan_dir,
                state_dir=self.state_dir,
            ).counts.questions,
        )

    def test_ask_resume_returns_existing_continue_resolution(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Need a decision",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            "continue",
            expected_run_id="run-live",
            note="Not an answer.",
            state_dir=self.state_dir,
        )

        code, payload = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(question_id=question.id, wait=True),
        )

        self.assertEqual(0, code)
        self.assertFalse(payload["answered"])
        self.assertTrue(payload["resolved"])
        self.assertFalse(payload["pause_required"])
        self.assertFalse(payload["timed_out"])
        self.assertIsNone(payload["reply"])
        self.assertEqual(resolution.id, payload["resolution"]["id"])
