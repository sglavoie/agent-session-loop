"""Mailbox CLI ask deadlines, resume, and inbox text budgets.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import json
import os
import time
import unittest
import uuid
from contextlib import redirect_stdout
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_cli_ask,
    mailbox_cli_constants,
    mailbox_cli_inbox,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    mailbox_rendering,
    mailbox_snapshot,
    mailbox_vocabulary,
    mailbox_waits,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase
from tests.support import (
    _write_lock_file,
    _write_state_payload,
)


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

    def test_ask_resume_requires_an_existing_question(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli_ask.ask_from_args,
            self.args(question_id=str(uuid.uuid4())),
        )
        self.assertEqual(1, code)
        self.assertEqual("question-not-found", payload["reason"])


if __name__ == "__main__":
    unittest.main()
