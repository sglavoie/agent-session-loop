"""MailboxCliTestsPart4.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import hashlib
import io
import json
import os
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    events,
    mailbox,
    mailbox_cli,
    state,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    read_jsonl,
)


class MailboxCliTestsPart4(MailboxCliTestsBase):
    def test_predates_fast_clear_still_defers_to_post_append_open_question(
        self,
    ) -> None:
        first = mailbox.append_question(
            self.plan_dir,
            "First?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        second = mailbox.append_question(
            self.plan_dir,
            "Second?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        # The lease predates `second`, so the projection is captured, but the
        # authoritative post-append re-read still sees `first` open.
        mailbox.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-live",
            projection_seq=first.seq,
            unanswered_questions=1,
            latest_unanswered_question_id=first.id,
            latest_unanswered_question_created_at=first.created_at,
        )

        with mock.patch.object(agent_mux_delivery, "notify") as notify:
            code, _payload = self.payload_from(
                mailbox_cli.inbox_reply_from_args,
                self.args(text="Second answer.", to=second.id),
            )

        self.assertEqual(0, code)
        notify.assert_not_called()
        self.assertEqual(
            (first,),
            mailbox.unanswered_questions(
                self.plan_dir,
                state_dir=self.state_dir,
            ),
        )

    def test_predates_fast_clear_rejects_lease_naming_a_different_run(
        self,
    ) -> None:
        mailbox.append_guidance(
            self.plan_dir,
            "Prefer the flat layout.",
            state_dir=self.state_dir,
        )
        question = mailbox.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        # Same predates shape as the firing case, but the lease attributes a
        # different run; without the run comparison the post-append re-read
        # would find no open questions for `run-other` and fire.
        mailbox.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-other",
            projection_seq=1,
            unanswered_questions=0,
        )

        with mock.patch.object(agent_mux_delivery, "notify") as notify:
            code, _payload = self.payload_from(
                mailbox_cli.inbox_reply_from_args,
                self.args(text="Use one object.", to=question.id),
            )

        self.assertEqual(0, code)
        notify.assert_not_called()

    def test_reply_succeeds_when_agent_mux_delivery_fails(self) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Proceed?",
            state_dir=self.state_dir,
            run_id="run-1",
        )
        mailbox.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-1",
            projection_seq=question.seq,
            unanswered_questions=1,
            latest_unanswered_question_id=question.id,
            latest_unanswered_question_created_at=question.created_at,
        )

        with mock.patch.object(
            agent_mux_delivery,
            "notify",
            side_effect=RuntimeError("AgentMux unavailable"),
        ):
            code, payload = self.payload_from(
                mailbox_cli.inbox_reply_from_args,
                self.args(text="Proceed.", to=question.id),
            )

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("reply", payload["message"]["kind"])

    def test_guidance_reads_multiline_stdin_verbatim(self) -> None:
        body = "first line\nsecond line\n"
        with mock.patch("sys.stdin", io.StringIO(body)):
            code, payload = self.payload_from(
                mailbox_cli.inbox_guidance_from_args,
                self.args(text_stdin=True),
            )
        self.assertEqual(0, code)
        self.assertEqual(body, payload["message"]["text"])

    def test_notify_publishes_exact_active_turn_without_opening_a_wait(
        self,
    ) -> None:
        self.write_active_turn(session_id="5.1", turn_index=7)

        code, payload, stderr = self.cli_payload_from(
            [
                "notify",
                str(self.plan_dir),
                "Checkpoint reached.",
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual("notify", payload["command"])
        self.assertEqual("run-live", payload["run_id"])
        self.assertEqual("5.1", payload["session_id"])
        self.assertEqual(7, payload["turn_index"])
        self.assertEqual("notice", payload["message"]["kind"])
        self.assertEqual("Checkpoint reached.", payload["message"]["text"])
        self.assertEqual("run-live", payload["message"]["run_id"])
        self.assertEqual("5.1", payload["message"]["session_id"])
        self.assertEqual(7, payload["message"]["turn_index"])

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(1, view.counts.notices)
        self.assertEqual(0, view.counts.questions)
        self.assertEqual(0, view.counts.unanswered_questions)
        self.assertEqual((), view.unanswered_questions)
        self.assertEqual((), view.pending_guidance)
        self.assertEqual(0, view.max_acknowledged_seq)
        self.assertFalse(
            mailbox.lease_path_for_plan(
                self.plan_dir,
                self.state_dir,
            ).exists()
        )
        [notice_event] = read_jsonl(
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        self.assertEqual(events.MAILBOX_EVENT, notice_event["name"])
        event_payload = notice_event["payload"]
        self.assertEqual(
            events.MAILBOX_ACTION_NOTICE_PUBLISHED,
            event_payload["action"],
        )
        self.assertEqual(payload["message"]["id"], event_payload["message_id"])
        self.assertEqual(payload["message"]["seq"], event_payload["seq"])
        self.assertEqual(len("Checkpoint reached."), event_payload["length"])
        self.assertEqual(
            hashlib.sha256(b"Checkpoint reached.").hexdigest(),
            event_payload["digest"],
        )
        event_text = json.dumps(notice_event, sort_keys=True)
        self.assertNotIn("Checkpoint reached.", event_text)

    def test_notify_reads_stdin_and_rejects_inactive_or_incomplete_turns(
        self,
    ) -> None:
        self.write_active_turn()
        body = "phase one\nphase two\n"
        with mock.patch("sys.stdin", io.StringIO(body)):
            code, payload = self.payload_from(
                mailbox_cli.notify_from_args,
                self.args(text_stdin=True),
            )
        self.assertEqual(0, code)
        self.assertEqual(body, payload["message"]["text"])

        inactive_states = (
            {"run_id": "run-live", "ok": True, "paused": False},
            {
                "run_id": "run-live",
                "ok": True,
                "paused": True,
                "active_turn": {
                    "run_id": "run-live",
                    "session_id": "5",
                    "turn_index": 2,
                },
            },
            {
                "run_id": "run-live",
                "ok": True,
                "paused": False,
                "run_finished_at": "2026-08-01T12:00:00+00:00",
                "active_turn": {
                    "run_id": "run-live",
                    "session_id": "5",
                    "turn_index": 2,
                },
            },
            {
                "run_id": "run-live",
                "ok": True,
                "paused": False,
                "active_turn": {
                    "run_id": "run-other",
                    "session_id": "5",
                    "turn_index": 2,
                },
            },
        )
        for runner_state in inactive_states:
            with self.subTest(runner_state=runner_state):
                _write_state_payload(
                    self.plan_dir,
                    self.state_dir,
                    runner_state,
                )
                code, error = self.payload_from(
                    mailbox_cli.notify_from_args,
                    self.args(text="must not publish"),
                )
                self.assertEqual(1, code)
                self.assertEqual(mailbox_cli.ERROR_NO_ACTIVE_RUN, error["reason"])

        self.assertEqual(
            1,
            mailbox.read_mailbox(
                self.plan_dir,
                state_dir=self.state_dir,
            ).counts.notices,
        )

    def test_notify_rejects_when_no_active_run_exists(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli.notify_from_args,
            self.args(text="must not publish"),
        )

        self.assertEqual(1, code)
        self.assertEqual(mailbox_cli.ERROR_NO_ACTIVE_RUN, payload["reason"])
        self.assertFalse(
            mailbox.mailbox_path_for_plan(
                self.plan_dir,
                self.state_dir,
            ).exists()
        )

    def test_notify_rejects_stale_lock_and_unreadable_state(self) -> None:
        state_path = _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-live",
                "ok": True,
                "paused": False,
                "active_turn": {
                    "run_id": "run-live",
                    "session_id": "5",
                    "turn_index": 2,
                },
            },
        )

        code, payload = self.payload_from(
            mailbox_cli.notify_from_args,
            self.args(text="must not publish"),
        )

        self.assertEqual(1, code)
        self.assertEqual(mailbox_cli.ERROR_NO_ACTIVE_RUN, payload["reason"])
        mailbox_path = mailbox.mailbox_path_for_plan(
            self.plan_dir,
            self.state_dir,
        )
        self.assertFalse(mailbox_path.exists())

        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        state_path.write_text("{", encoding="utf-8")

        code, payload = self.payload_from(
            mailbox_cli.notify_from_args,
            self.args(text="must not publish"),
        )

        self.assertEqual(1, code)
        self.assertEqual(mailbox_cli.ERROR_NO_ACTIVE_RUN, payload["reason"])
        self.assertFalse(mailbox_path.exists())

    def test_notify_append_failure_is_an_ordinary_command_error(self) -> None:
        self.write_active_turn()
        mailbox_path = mailbox.mailbox_path_for_plan(
            self.plan_dir,
            self.state_dir,
        )
        failure = mailbox.MailboxError(
            mailbox.ERROR_MAILBOX_WRITE,
            "cannot append notice",
            path=mailbox_path,
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(
                mailbox,
                "append_notice",
                side_effect=failure,
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = mailbox_cli.notify_from_args(self.args(text="checkpoint"))

        self.assertEqual(1, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(mailbox.ERROR_MAILBOX_WRITE, payload["reason"])
        self.assertEqual({"path": str(mailbox_path)}, payload["details"])
        self.assertNotIn("RUNNER_PAUSE:", stdout.getvalue())
        self.assertNotIn("RUNNER_PAUSE:", stderr.getvalue())

    def test_stdin_utf8_decode_and_absent_stdin_fail_in_one_json_envelope(
        self,
    ) -> None:
        cases = (
            (
                "guidance",
                mailbox_cli.inbox_guidance_from_args,
                self.args(text_stdin=True),
            ),
            (
                "reply",
                mailbox_cli.inbox_reply_from_args,
                self.args(text_stdin=True, to=str(uuid.uuid4())),
            ),
            (
                "notify",
                mailbox_cli.notify_from_args,
                self.args(text_stdin=True),
            ),
        )
        for label, handler, args in cases:
            for failure, stdin in (
                (
                    "decode",
                    io.TextIOWrapper(
                        io.BytesIO(b"\xff"),
                        encoding="ascii",
                        errors="surrogateescape",
                    ),
                ),
                ("absent", None),
            ):
                with self.subTest(command=label, failure=failure):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        mock.patch("sys.stdin", stdin),
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                    ):
                        code = handler(args)
                    self.assertEqual(1, code)
                    self.assertEqual("", stderr.getvalue())
                    payload = json.loads(stdout.getvalue())
                    self.assertFalse(payload["ok"])
                    self.assertEqual(mailbox_cli.ERROR_STDIN_READ, payload["reason"])
                    self.assertNotIn("Traceback", payload["error"])
                    self.assertEqual(str(self.plan_dir), payload["plan_reference"])

    def test_missing_and_conflicting_text_sources_have_distinct_reasons(
        self,
    ) -> None:
        question_id = str(uuid.uuid4())
        cases = (
            (
                "guidance-missing-text",
                ["inbox", "guidance", str(self.plan_dir)],
                mailbox.REJECT_MISSING_TEXT,
            ),
            (
                "guidance-conflicting-text",
                [
                    "inbox",
                    "guidance",
                    str(self.plan_dir),
                    "--text",
                    "inline",
                    "--text-stdin",
                ],
                mailbox_cli.ERROR_INVALID_TEXT_SOURCE,
            ),
            (
                "reply-missing-text",
                [
                    "inbox",
                    "reply",
                    str(self.plan_dir),
                    "--to",
                    question_id,
                ],
                mailbox.REJECT_MISSING_TEXT,
            ),
            (
                "reply-conflicting-text",
                [
                    "inbox",
                    "reply",
                    str(self.plan_dir),
                    "--to",
                    question_id,
                    "--text",
                    "inline",
                    "--text-stdin",
                ],
                mailbox_cli.ERROR_INVALID_TEXT_SOURCE,
            ),
            (
                "notify-missing-text",
                ["notify", str(self.plan_dir)],
                mailbox.REJECT_MISSING_TEXT,
            ),
            (
                "notify-conflicting-text",
                [
                    "notify",
                    str(self.plan_dir),
                    "inline",
                    "--text-stdin",
                ],
                mailbox_cli.ERROR_INVALID_TEXT_SOURCE,
            ),
        )
        for label, argv, expected_reason in cases:
            with self.subTest(case=label):
                code, payload, stderr = self.cli_payload_from(
                    [*argv, "--state-dir", str(self.state_dir), "--json"]
                )
                self.assertEqual(1, code)
                self.assertEqual("", stderr)
                self.assertFalse(payload["ok"])
                self.assertEqual(expected_reason, payload["reason"])
                self.assertEqual({}, payload["details"])
                self.assertEqual(str(self.plan_dir), payload["plan_reference"])


if __name__ == "__main__":
    unittest.main()
