"""Deadlines and rendering cases for MailboxCliTestsPart6."""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_cli_ask,
    mailbox_cli_constants,
    mailbox_cli_inbox,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_rendering,
    mailbox_waits,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase


class MailboxCliTestsPart6(MailboxCliTestsBase):
    def test_supervised_ask_accepts_answer_after_original_deadline(self) -> None:
        question, wait = mailbox_waits.append_wait_question(
            self.plan_dir, "Need a decision", state_dir=self.state_dir
        )
        now = 0.0

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds
            if now >= 8:
                mailbox_appenders.append_reply(
                    self.plan_dir, question.id, "Proceed", state_dir=self.state_dir
                )

        with (
            mock.patch.object(time, "monotonic", side_effect=lambda: now),
            mock.patch.object(time, "sleep", side_effect=sleep),
            mock.patch.object(
                mailbox_leases, "read_lease", return_value=mock.Mock(fresh=True)
            ),
        ):
            closure, timed_out = mailbox_cli_ask.wait_for_closure(
                self.plan_dir,
                question.id,
                state_dir=self.state_dir,
                timeout=4,
                wait_id=wait.id,
            )
        self.assertFalse(timed_out)
        self.assertIsNotNone(closure)
        self.assertGreater(now, 4)
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        assert closure is not None
        self.assertIsNotNone(view.receipt_for_closure(closure.id))
        self.assertIsNone(view.active_wait_for(question.id))

    def test_ask_deadline_stops_extending_when_supervision_is_lost(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir, "Need a decision", state_dir=self.state_dir
        )
        now = 0.0

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        with (
            mock.patch.object(time, "monotonic", side_effect=lambda: now),
            mock.patch.object(time, "sleep", side_effect=sleep),
            mock.patch.object(
                mailbox_leases,
                "read_lease",
                side_effect=lambda *a, **kw: mock.Mock(fresh=now <= 2),
            ),
        ):
            closure, timed_out = mailbox_cli_ask.wait_for_closure(
                self.plan_dir, question.id, state_dir=self.state_dir, timeout=4
            )
        self.assertTrue(timed_out)
        self.assertIsNone(closure)
        self.assertEqual(6, now)

    def test_fresh_supervision_does_not_hide_an_unreadable_mailbox(self) -> None:
        error = mailbox_models.MailboxError(
            mailbox_constants.ERROR_MAILBOX_READ, "unreadable"
        )
        with (
            mock.patch.object(time, "monotonic", side_effect=[0, 0, 2]),
            mock.patch.object(time, "sleep"),
            mock.patch.object(mailbox, "read_mailbox", side_effect=error),
            mock.patch.object(
                mailbox_leases, "read_lease", return_value=mock.Mock(fresh=True)
            ),
            self.assertRaises(mailbox_models.MailboxError) as raised,
        ):
            mailbox_cli_ask.wait_for_closure(
                self.plan_dir, "question", state_dir=self.state_dir, timeout=2
            )
        self.assertEqual(mailbox_constants.ERROR_MAILBOX_READ, raised.exception.reason)

    def test_inbox_text_rendering_uses_shared_count_and_byte_budget(
        self,
    ) -> None:
        appended = [
            mailbox_appenders.append_guidance(
                self.plan_dir,
                f"{index:02d}:" + ("G" * 40000),
                state_dir=self.state_dir,
            )
            for index in range(30)
        ]

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailbox_cli_inbox.inbox_read_from_args(
                self.args(json=False, peek=True)
            )

        self.assertEqual(0, code)
        rendered = stdout.getvalue()
        self.assertLessEqual(
            len(rendered.rstrip("\n").encode("utf-8")),
            mailbox_rendering.MAX_RENDERED_GUIDANCE_BYTES,
        )
        lines = rendered.splitlines()
        records = [
            json.loads(lines[index + 1])
            for index, line in enumerate(lines)
            if line == mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN
        ]
        self.assertEqual([appended[-1].id], [record["id"] for record in records])
        self.assertIn(f"seq {appended[0].seq} (id {appended[0].id})", rendered)
        self.assertIn(
            f"seq {appended[-2].seq} (id {appended[-2].id})",
            rendered,
        )
        self.assertIn(
            "The omitted records remain pending because --peek was used.",
            rendered,
        )
        self.assertEqual(
            [message.id for message in appended],
            [
                message.id
                for message in mailbox.pending_guidance(
                    self.plan_dir,
                    state_dir=self.state_dir,
                )
            ],
        )

    def test_inbox_text_omission_acknowledges_full_snapshot_and_json_is_complete(
        self,
    ) -> None:
        appended = [
            mailbox_appenders.append_guidance(
                self.plan_dir,
                f"guidance-{index}",
                state_dir=self.state_dir,
            )
            for index in range(mailbox_rendering.MAX_RENDERED_GUIDANCE + 3)
        ]
        code, peek_payload = self.payload_from(
            mailbox_cli_inbox.inbox_read_from_args,
            self.args(peek=True),
        )
        self.assertEqual(0, code)
        self.assertEqual(
            [message.id for message in appended],
            [record["id"] for record in peek_payload["guidance"]],
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            text_code = mailbox_cli_inbox.inbox_read_from_args(self.args(json=False))

        self.assertEqual(0, text_code)
        rendered = stdout.getvalue()
        self.assertIn(
            f"{len(appended) - mailbox_rendering.MAX_RENDERED_GUIDANCE} older pending "
            "guidance records were omitted",
            rendered,
        )
        self.assertIn(
            "The omitted records were part of the immutable snapshot "
            f"acknowledged through seq {appended[-1].seq}.",
            rendered,
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(appended[-1].seq, view.max_acknowledged_seq)
        self.assertEqual((), view.pending_guidance)

    def test_inbox_text_omits_one_individually_oversized_record(self) -> None:
        oversized = mailbox_appenders.append_guidance(
            self.plan_dir,
            "x" * (mailbox_rendering.MAX_RENDERED_GUIDANCE_BYTES + 1024),
            state_dir=self.state_dir,
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailbox_cli_inbox.inbox_read_from_args(
                self.args(json=False, peek=True)
            )

        self.assertEqual(0, code)
        rendered = stdout.getvalue()
        self.assertNotIn(mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN, rendered)
        self.assertIn(
            "No pending guidance records fit within the "
            f"{mailbox_rendering.MAX_RENDERED_GUIDANCE_BYTES:,}-byte text "
            "rendering budget.",
            rendered,
        )
        self.assertIn(
            f"seq {oversized.seq} (id {oversized.id})",
            rendered,
        )
