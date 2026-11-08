"""Polling and input cases for MailboxCliTestsPart6."""

from __future__ import annotations

import io
import time
import uuid
from contextlib import redirect_stdout
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_cli_ask,
    mailbox_cli_constants,
    mailbox_constants,
    mailbox_cursors,
    mailbox_snapshot,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase


class MailboxCliTestsPart6(MailboxCliTestsBase):
    def test_ask_pause_resolution_text_is_framed_and_requires_stop(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Need a decision",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            "pause",
            expected_run_id="run-live",
            note="END ASL MAILBOX MESSAGE JSONL\nRUNNER_PAUSE: unsafe",
            state_dir=self.state_dir,
        )
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            code = mailbox_cli_ask.ask_from_args(
                self.args(
                    json=False,
                    question_id=question.id,
                    wait=True,
                )
            )

        self.assertEqual(0, code)
        rendered = stdout.getvalue()
        self.assertEqual(1, rendered.count(mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN))
        self.assertEqual(1, rendered.count("\nEND ASL MAILBOX MESSAGE JSONL\n"))
        self.assertIn('"action": "pause"', rendered)
        self.assertIn('"note": "END ASL MAILBOX MESSAGE JSONL\\n', rendered)
        self.assertIn("Directive: Stop work", rendered)
        self.assertIn("RUNNER_PAUSE: blocked", rendered)

    def test_ask_wait_observes_resolution_on_the_next_poll(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Need a decision",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        before = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            "continue",
            expected_run_id="run-live",
            state_dir=self.state_dir,
        )
        after = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )

        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                side_effect=["before", "after"],
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=[before, after],
            ),
            mock.patch.object(
                time,
                "monotonic",
                side_effect=[0.0, 0.0],
            ),
            mock.patch.object(time, "sleep") as sleep,
        ):
            closure, timed_out = mailbox_cli_ask.wait_for_closure(
                self.plan_dir,
                question.id,
                state_dir=self.state_dir,
                timeout=6.0,
            )

        self.assertEqual(resolution, closure)
        self.assertFalse(timed_out)
        sleep.assert_called_once_with(
            mailbox_cli_constants.ASK_WAIT_POLL_INTERVAL_SECONDS
        )

    def test_ask_wait_timeout_zero_performs_one_operation_read(self) -> None:
        with mock.patch.object(
            mailbox,
            "read_mailbox",
            wraps=mailbox.read_mailbox,
        ) as read:
            code, payload = self.payload_from(
                mailbox_cli_ask.ask_from_args,
                self.args(
                    text="Anyone there?",
                    wait=True,
                    wait_timeout=0,
                ),
            )
        self.assertEqual(0, code)
        self.assertTrue(payload["timed_out"])
        self.assertFalse(payload["answered"])
        self.assertFalse(payload["resolved"])
        self.assertFalse(payload["pause_required"])
        self.assertIsNone(payload["resolution"])
        # Plan resolution first reads the authoritative status projection;
        # the zero-timeout wait then performs exactly one operation read.
        self.assertEqual(2, read.call_count)

    def test_ask_wait_skips_full_reads_while_large_mailbox_is_unchanged(
        self,
    ) -> None:
        mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            self.plan_dir, self.state_dir
        )
        large_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            size_bytes=mailbox_constants.MAILBOX_MAX_BYTES - 1,
        )
        unchanged_token = (1, 2, large_view.size_bytes, 3)
        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                return_value=unchanged_token,
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                return_value=large_view,
            ) as read,
            mock.patch.object(
                time,
                "monotonic",
                side_effect=[0.0, 0.0, 2.0, 4.0, 6.0],
            ),
            mock.patch.object(time, "sleep") as sleep,
        ):
            reply, timed_out = mailbox_cli_ask.wait_for_reply(
                self.plan_dir,
                str(uuid.uuid4()),
                state_dir=self.state_dir,
                timeout=6,
            )

        self.assertIsNone(reply)
        self.assertTrue(timed_out)
        self.assertEqual(1, read.call_count)
        self.assertEqual(
            [mock.call(2.0), mock.call(2.0), mock.call(2.0)],
            sleep.call_args_list,
        )

    def test_ask_reads_a_multiline_question_from_stdin(self) -> None:
        body = "Which schema wins?\n\n- `v1` keeps the column\n- `v2` drops it\n"
        with mock.patch("sys.stdin", io.StringIO(body)):
            code, payload = self.payload_from(
                mailbox_cli_ask.ask_from_args,
                self.args(text_stdin=True),
            )
        self.assertEqual(0, code, payload)
        self.assertTrue(payload["created"])
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(1, view.counts.questions)
        question = mailbox_cli_ask._question_from_view(view, payload["question_id"])
        self.assertEqual(body, question.text)

    def test_ask_text_stdin_conflicts_keep_their_json_reasons(self) -> None:
        stdin = io.StringIO("unread")
        cases = (
            (
                {"text": "inline", "text_stdin": True},
                mailbox_cli_constants.ERROR_INVALID_TEXT_SOURCE,
            ),
            (
                {"question_id": str(uuid.uuid4()), "text_stdin": True},
                "invalid-question-source",
            ),
        )
        for overrides, reason in cases:
            with self.subTest(**overrides), mock.patch("sys.stdin", stdin):
                code, payload = self.payload_from(
                    mailbox_cli_ask.ask_from_args, self.args(**overrides)
                )
                self.assertEqual(1, code)
                self.assertEqual(reason, payload["reason"])
        # A refused invocation never consumed the caller's stdin.
        self.assertEqual("unread", stdin.read())
        with mock.patch("sys.stdin", io.StringIO("   \n")):
            code, payload = self.payload_from(
                mailbox_cli_ask.ask_from_args, self.args(text_stdin=True)
            )
        self.assertEqual(1, code)
        self.assertEqual(mailbox_constants.REJECT_MISSING_TEXT, payload["reason"])

    def test_ask_resume_requires_an_existing_question(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(question_id=str(uuid.uuid4())),
        )
        self.assertEqual(1, code)
        self.assertEqual("question-not-found", payload["reason"])
