"""Mailbox CLI outbox read cadence, lease renewal, and shared error envelopes.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import time
import unittest
import uuid
from contextlib import redirect_stderr
from unittest import mock

from agent_session_loop import (
    cli,
    control_cli,
    mailbox,
    mailbox_cli_args,
    mailbox_cli_ask,
    mailbox_cli_constants,
    mailbox_cli_guidance,
    mailbox_cli_inbox,
    mailbox_cli_notify,
    mailbox_cli_outbox,
    mailbox_cli_replies,
    mailbox_cli_status,
    mailbox_cli_waits,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    plans_files,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase
from tests.support import (
    _write_state_payload,
)


class MailboxCliTestsPart10(MailboxCliTestsBase):
    def test_outbox_wait_skips_full_reads_until_mailbox_changes(self) -> None:
        mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            self.plan_dir, self.state_dir
        )
        large_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            size_bytes=mailbox_constants.MAILBOX_MAX_BYTES - 1,
        )
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=str(uuid.uuid4()),
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="changed",
        )
        changed_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question,),
            last_seq=1,
            size_bytes=mailbox_constants.MAILBOX_MAX_BYTES,
        )
        unchanged_token = (1, 2, large_view.size_bytes, 3)
        changed_token = (1, 2, changed_view.size_bytes, 4)
        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                side_effect=[
                    unchanged_token,
                    unchanged_token,
                    changed_token,
                ],
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=[large_view, changed_view],
            ) as read,
            mock.patch.object(
                mailbox_cli_status,
                "write_supervisor_lease",
            ),
            mock.patch.object(
                time,
                "monotonic",
                side_effect=[0.0, 0.0, 5.0],
            ),
            mock.patch.object(time, "sleep") as sleep,
        ):
            result = mailbox_cli_waits.wait_for_questions(
                self.plan_dir,
                state_dir=self.state_dir,
                since=0,
                timeout=30,
                supervisor_id=str(uuid.uuid4()),
            )

        self.assertEqual((question,), result.questions)
        self.assertEqual(1, result.next_since)
        self.assertFalse(result.timed_out)
        self.assertEqual(2, read.call_count)
        self.assertEqual([mock.call(5.0), mock.call(5.0)], sleep.call_args_list)

    def test_outbox_renews_lease_after_thirty_seconds(self) -> None:
        empty = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_cursors.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            )
        )
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=str(uuid.uuid4()),
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="after renewal",
        )
        found = mailbox_snapshot.MailboxView(
            mailbox_path=empty.mailbox_path,
            messages=(question,),
            last_seq=1,
        )
        with (
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=[empty, found],
            ),
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                side_effect=[(1, 2, 0, 3), (1, 2, 1, 4)],
            ),
            mock.patch.object(
                mailbox_cli_status,
                "write_supervisor_lease",
            ) as renew,
            mock.patch.object(
                time,
                "monotonic",
                side_effect=[0.0, 30.0],
            ),
            mock.patch.object(time, "sleep"),
        ):
            result = mailbox_cli_waits.wait_for_questions(
                self.plan_dir,
                state_dir=self.state_dir,
                since=0,
                timeout=60,
                supervisor_id=str(uuid.uuid4()),
            )
        self.assertEqual((question,), result.questions)
        self.assertEqual(1, result.next_since)
        self.assertFalse(result.timed_out)
        self.assertEqual(2, renew.call_count)

    def test_outbox_wait_lease_renewal_failure_recovers_and_returns_question(
        self,
    ) -> None:
        mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            self.plan_dir, self.state_dir
        )
        empty = mailbox_snapshot.MailboxView(mailbox_path=mailbox_path)
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=str(uuid.uuid4()),
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="arrived after renewal recovery",
        )
        found = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question,),
            last_seq=1,
        )
        unchanged_token = (1, 2, 0, 3)
        changed_token = (1, 2, 1, 4)
        renewal_failure = mailbox_models.MailboxError(
            mailbox_constants.ERROR_LEASE_WRITE,
            "temporarily unavailable",
            path=mailbox_cursors.lease_path_for_plan(self.plan_dir, self.state_dir),
        )
        landed_lease = mock.Mock(supervisor_id=str(uuid.uuid4()))
        context = mock.Mock()
        context.snapshot.plan_dir = self.plan_dir
        with (
            mock.patch.object(
                mailbox_cli_args,
                "resolve",
                return_value=context,
            ),
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                side_effect=([unchanged_token] * 7) + [changed_token],
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=[empty, found],
            ),
            mock.patch.object(
                mailbox_cli_status,
                "write_supervisor_lease",
                side_effect=[landed_lease, renewal_failure, landed_lease],
            ) as renew,
            mock.patch.object(
                time,
                "monotonic",
                side_effect=[0.0, 30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0],
            ),
            mock.patch.object(time, "sleep"),
        ):
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=90),
            )

        self.assertEqual(0, code)
        self.assertEqual(
            [question.id],
            [item["id"] for item in payload["questions"]],
        )
        self.assertEqual(
            {
                "degraded": False,
                "renewal_failures": 1,
                "renewal_recoveries": 1,
            },
            payload["lease_supervision"],
        )
        self.assertEqual(3, renew.call_count)

    def test_outbox_wait_initial_lease_failure_remains_fail_closed(
        self,
    ) -> None:
        lease_path = mailbox_cursors.lease_path_for_plan(self.plan_dir, self.state_dir)
        context = mock.Mock()
        context.snapshot.plan_dir = self.plan_dir
        initial_failure = mailbox_models.MailboxError(
            mailbox_constants.ERROR_LEASE_WRITE,
            "cannot establish supervision",
            path=lease_path,
        )
        with (
            mock.patch.object(
                mailbox_cli_args,
                "resolve",
                return_value=context,
            ),
            mock.patch.object(
                mailbox_cli_status,
                "write_supervisor_lease",
                side_effect=initial_failure,
            ) as write,
            mock.patch.object(
                mailbox,
                "read_mailbox",
            ) as read,
        ):
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=90),
            )

        self.assertEqual(1, code)
        self.assertEqual(mailbox_constants.ERROR_LEASE_WRITE, payload["reason"])
        self.assertEqual({"path": str(lease_path)}, payload["details"])
        self.assertEqual(1, write.call_count)
        read.assert_not_called()

    def test_cli_wait_cadences_match_runner_and_supervisor(self) -> None:
        self.assertEqual(2.0, mailbox_cli_constants.ASK_WAIT_POLL_INTERVAL_SECONDS)
        self.assertEqual(5.0, mailbox_cli_constants.OUTBOX_WAIT_POLL_INTERVAL_SECONDS)

    def test_negative_cursor_and_unbounded_timeouts_are_json_errors(self) -> None:
        cases = (
            (
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(since=-1, timeout=0),
                "invalid-since",
            ),
            (
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=mailbox_cli_constants.MAX_WAIT_TIMEOUT_SECONDS + 1),
                "invalid-timeout",
            ),
            (
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(include="other", timeout=0),
                "invalid-include",
            ),
            (
                mailbox_cli_ask.ask_from_args,
                self.args(
                    text="question",
                    wait=True,
                    wait_timeout=mailbox_cli_constants.MAX_WAIT_TIMEOUT_SECONDS + 1,
                ),
                "invalid-timeout",
            ),
        )
        for handler, args, reason in cases:
            with self.subTest(reason=reason):
                code, payload = self.payload_from(handler, args)
                self.assertEqual(1, code)
                self.assertEqual(reason, payload["reason"])

    def test_mailbox_and_control_share_plan_error_envelopes(self) -> None:
        broken = self.tmp / "broken"
        broken.mkdir()
        control_code, control_payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(plan_dir=str(broken)),
        )
        mailbox_code, mailbox_payload = self.payload_from(
            mailbox_cli_guidance.inbox_guidance_from_args,
            self.args(plan_dir=str(broken), text="body"),
        )
        self.assertEqual((1, 1), (control_code, mailbox_code))
        for key in (
            "ok",
            "reason",
            "error",
            "paused",
            "details",
            "plan_reference",
        ):
            self.assertEqual(control_payload[key], mailbox_payload[key])

    def test_plan_dir_and_plan_reference_have_distinct_stable_semantics(
        self,
    ) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"plan_dir": str(plans_files.canonicalize_path(self.plan_dir))},
        )
        plan_reference = self.plan_dir.name

        success_code, success, success_stderr = self.cli_payload_from(
            [
                "inbox",
                "guidance",
                plan_reference,
                "--text",
                "body",
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )
        failure_code, failure, failure_stderr = self.cli_payload_from(
            [
                "inbox",
                "guidance",
                plan_reference,
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )

        self.assertEqual((0, 1), (success_code, failure_code))
        self.assertEqual("", success_stderr)
        self.assertEqual("", failure_stderr)
        self.assertEqual(
            str(plans_files.canonicalize_path(self.plan_dir)),
            success["plan_dir"],
        )
        self.assertNotIn("plan_reference", success)
        self.assertEqual(plan_reference, failure["plan_reference"])
        self.assertNotIn("plan_dir", failure)

    def test_cli_registers_every_mailbox_handler_lazily(self) -> None:
        parser = cli.build_parser()
        cases = (
            (
                ["inbox", "guidance", "/tmp/plan", "--text", "body"],
                "mailbox_cli_guidance:inbox_guidance_from_args",
                mailbox_cli_guidance.inbox_guidance_from_args,
            ),
            (
                [
                    "inbox",
                    "reply",
                    "/tmp/plan",
                    "--to",
                    str(uuid.uuid4()),
                    "--text-stdin",
                ],
                "mailbox_cli_replies:inbox_reply_from_args",
                mailbox_cli_replies.inbox_reply_from_args,
            ),
            (
                [
                    "inbox",
                    "resolve",
                    "/tmp/plan",
                    "--to",
                    str(uuid.uuid4()),
                    "--action",
                    "pause",
                    "--note-stdin",
                ],
                "mailbox_cli_replies:inbox_resolve_from_args",
                mailbox_cli_replies.inbox_resolve_from_args,
            ),
            (
                ["inbox", "read", "/tmp/plan", "--peek"],
                "mailbox_cli_inbox:inbox_read_from_args",
                mailbox_cli_inbox.inbox_read_from_args,
            ),
            (
                ["ask", "/tmp/plan", "question", "--wait", "--wait-timeout", "0"],
                "mailbox_cli_ask:ask_from_args",
                mailbox_cli_ask.ask_from_args,
            ),
            (
                ["notify", "/tmp/plan", "checkpoint"],
                "mailbox_cli_notify:notify_from_args",
                mailbox_cli_notify.notify_from_args,
            ),
            (
                [
                    "outbox",
                    "wait",
                    "/tmp/plan",
                    "--include",
                    "all",
                    "--since",
                    "2",
                    "--timeout",
                    "0",
                ],
                "mailbox_cli_outbox:outbox_wait_from_args",
                mailbox_cli_outbox.outbox_wait_from_args,
            ),
        )
        for argv, handler_ref, handler in cases:
            with self.subTest(argv=argv):
                parsed = parser.parse_args(argv)
                self.assertEqual(handler_ref, parsed.handler_ref)
                self.assertIs(handler, cli.resolve_handler(parsed))

    def test_parser_defers_message_and_question_source_validation(self) -> None:
        parser = cli.build_parser()
        cases = (
            ["inbox", "guidance", "/tmp/plan"],
            [
                "inbox",
                "guidance",
                "/tmp/plan",
                "--text",
                "one",
                "--text-stdin",
            ],
            ["ask", "/tmp/plan"],
            ["notify", "/tmp/plan"],
            ["notify", "/tmp/plan", "notice", "--text-stdin"],
            [
                "ask",
                "/tmp/plan",
                "question",
                "--question-id",
                str(uuid.uuid4()),
            ],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                parsed = parser.parse_args(argv)
                self.assertIsNotNone(parsed.handler_ref)

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["control", "select-harness", "/tmp/plan"])

        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
