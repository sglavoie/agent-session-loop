"""Mailbox CLI inbox resolve and its Agent Mux clear.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import json
import os
import unittest
import uuid
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    mailbox,
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_outbox,
    mailbox_cli_replies,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    patched_env,
)


class MailboxCliTestsPart2(MailboxCliTestsBase):
    def test_inbox_resolve_requires_a_live_owned_question(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Historical question",
            state_dir=self.state_dir,
            run_id="run-old",
        )
        cases = (
            (
                "no-active-run",
                None,
                question.id,
                mailbox_cli_constants.ERROR_NO_ACTIVE_RUN,
            ),
            (
                "paused-run",
                {"run_id": "run-old", "paused": True},
                question.id,
                mailbox_cli_constants.ERROR_NO_ACTIVE_RUN,
            ),
            (
                "finished-run",
                {
                    "run_id": "run-old",
                    "paused": False,
                    "run_finished_at": "2026-08-01T12:00:00+00:00",
                },
                question.id,
                mailbox_cli_constants.ERROR_NO_ACTIVE_RUN,
            ),
            (
                "historical-question",
                {"run_id": "run-live", "ok": True, "paused": False},
                question.id,
                mailbox_constants.REJECT_QUESTION_NOT_OPEN,
            ),
            (
                "missing-question",
                {"run_id": "run-live", "ok": True, "paused": False},
                str(uuid.uuid4()),
                mailbox_constants.REJECT_QUESTION_NOT_FOUND,
            ),
        )
        for label, runner_state, target, expected_reason in cases:
            with self.subTest(case=label):
                if runner_state is not None:
                    _write_state_payload(
                        self.plan_dir,
                        self.state_dir,
                        runner_state,
                    )
                    _write_lock_file(
                        self.plan_dir,
                        self.state_dir,
                        pid=os.getpid(),
                    )
                code, payload = self.payload_from(
                    mailbox_cli_replies.inbox_resolve_from_args,
                    self.args(action="continue", to=target),
                )
                self.assertEqual(1, code)
                self.assertEqual(expected_reason, payload["reason"])

    def test_inbox_resolve_rejects_stale_lock_and_unreadable_state(
        self,
    ) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Still active?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        state_path = _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": "run-live", "ok": True, "paused": False},
        )

        code, payload = self.payload_from(
            mailbox_cli_replies.inbox_resolve_from_args,
            self.args(action="continue", to=question.id),
        )

        self.assertEqual(1, code)
        self.assertEqual(mailbox_cli_constants.ERROR_NO_ACTIVE_RUN, payload["reason"])
        self.assertEqual(
            0,
            mailbox.read_mailbox(
                self.plan_dir,
                state_dir=self.state_dir,
            ).counts.resolutions,
        )

        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        state_path.write_text("{", encoding="utf-8")

        code, payload = self.payload_from(
            mailbox_cli_replies.inbox_resolve_from_args,
            self.args(action="continue", to=question.id),
        )

        self.assertEqual(1, code)
        self.assertEqual(mailbox_cli_constants.ERROR_NO_ACTIVE_RUN, payload["reason"])
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(0, view.counts.resolutions)
        self.assertEqual((question,), view.messages)

    def test_inbox_resolve_first_close_wins_errors_stay_distinct(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": "run-live", "ok": True, "paused": False},
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        answered = mailbox_appenders.append_question(
            self.plan_dir,
            "Answered?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_appenders.append_reply(
            self.plan_dir,
            answered.id,
            "Yes.",
            state_dir=self.state_dir,
        )
        resolved = mailbox_appenders.append_question(
            self.plan_dir,
            "Resolved?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_appenders.append_resolution(
            self.plan_dir,
            resolved.id,
            "pause",
            expected_run_id="run-live",
            state_dir=self.state_dir,
        )

        for target, expected_reason in (
            (answered.id, mailbox_constants.REJECT_QUESTION_ALREADY_ANSWERED),
            (resolved.id, mailbox_constants.REJECT_QUESTION_ALREADY_RESOLVED),
        ):
            with self.subTest(reason=expected_reason):
                code, payload = self.payload_from(
                    mailbox_cli_replies.inbox_resolve_from_args,
                    self.args(action="continue", to=target),
                )
                self.assertEqual(1, code)
                self.assertEqual(expected_reason, payload["reason"])

    def test_inbox_resolve_validates_action_and_optional_note_sources(self) -> None:
        invalid_cases = (
            (
                self.args(action=None, to=str(uuid.uuid4())),
                mailbox_constants.REJECT_INVALID_ACTION,
            ),
            (
                self.args(
                    action="continue",
                    note="inline",
                    note_stdin=True,
                    to=str(uuid.uuid4()),
                ),
                mailbox_cli_constants.ERROR_INVALID_NOTE_SOURCE,
            ),
            (
                self.args(
                    action="other",
                    to=str(uuid.uuid4()),
                ),
                mailbox_constants.REJECT_INVALID_ACTION,
            ),
        )
        for args, expected_reason in invalid_cases:
            with self.subTest(reason=expected_reason):
                code, payload = self.payload_from(
                    mailbox_cli_replies.inbox_resolve_from_args,
                    args,
                )
                self.assertEqual(1, code)
                self.assertEqual(expected_reason, payload["reason"])
        self.assertFalse(
            mailbox_cursors.mailbox_path_for_plan(
                self.plan_dir,
                self.state_dir,
            ).exists()
        )

    def test_inbox_resolve_reads_an_empty_note_from_stdin(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": "run-live", "ok": True, "paused": False},
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Continue?",
            state_dir=self.state_dir,
            run_id="run-live",
        )

        with mock.patch("sys.stdin", io.StringIO("")):
            code, payload = self.payload_from(
                mailbox_cli_replies.inbox_resolve_from_args,
                self.args(
                    action="continue",
                    note_stdin=True,
                    to=question.id,
                ),
            )

        self.assertEqual(0, code)
        self.assertEqual("", payload["message"]["note"])

    def test_resolution_emits_text_free_agent_mux_clear_from_fresh_projection(
        self,
    ) -> None:
        self.write_active_turn()
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "May I continue?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-live",
            projection_seq=question.seq,
            unanswered_questions=1,
            latest_unanswered_question_id=question.id,
            latest_unanswered_question_created_at=question.created_at,
        )

        with (
            patched_env(KITTY_WINDOW_ID="42"),
            mock.patch.object(
                agent_mux_delivery,
                "notify",
                return_value={"final_status": "accepted"},
            ) as notify,
        ):
            code, resolution_payload = self.payload_from(
                mailbox_cli_replies.inbox_resolve_from_args,
                self.args(
                    action="continue",
                    note="Private supervisor note.",
                    to=question.id,
                ),
            )

        self.assertEqual(0, code)
        notify.assert_called_once()
        event_name, payload = notify.call_args.args
        kwargs = notify.call_args.kwargs
        self.assertEqual(
            agent_mux_delivery.MAILBOX_RESOLUTION_EVENT,
            event_name,
        )
        self.assertEqual("run-live", payload["run_id"])
        self.assertEqual(42, payload["kitty_window_id"])
        self.assertEqual(
            agent_mux_delivery.MAILBOX_RESOLUTION_REASON,
            kwargs["reason"],
        )
        self.assertEqual(question.id, kwargs["extra"]["question_id"])
        self.assertEqual("continue", kwargs["extra"]["resolution_action"])
        self.assertEqual(
            str(question.seq),
            kwargs["extra"]["mailbox_projection_sequence"],
        )
        resolution_seq = resolution_payload["message"]["seq"]
        self.assertEqual(resolution_seq, kwargs["allocate_sequence"]())
        self.assertEqual(
            f"run-live:mailbox-resolution:{resolution_seq}",
            kwargs["extra"]["source_event_id"],
        )
        delivery_shape = json.dumps(
            {"payload": payload, "extra": kwargs["extra"]},
            sort_keys=True,
        )
        self.assertNotIn("May I continue?", delivery_shape)
        self.assertNotIn("Private supervisor note.", delivery_shape)

    def test_resolution_fast_clear_requires_no_open_sibling_question(
        self,
    ) -> None:
        self.write_active_turn()
        first = mailbox_appenders.append_question(
            self.plan_dir,
            "First?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        second = mailbox_appenders.append_question(
            self.plan_dir,
            "Second?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-live",
            projection_seq=second.seq,
            unanswered_questions=2,
            latest_unanswered_question_id=second.id,
            latest_unanswered_question_created_at=second.created_at,
        )

        with mock.patch.object(agent_mux_delivery, "notify") as notify:
            code, _payload = self.payload_from(
                mailbox_cli_replies.inbox_resolve_from_args,
                self.args(action="pause", to=second.id),
            )

        self.assertEqual(0, code)
        notify.assert_not_called()
        self.assertEqual(
            (first,),
            mailbox.read_mailbox(
                self.plan_dir, state_dir=self.state_dir
            ).open_questions("run-live"),
        )

    def test_resolution_succeeds_when_agent_mux_delivery_fails(self) -> None:
        self.write_active_turn()
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Proceed?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-live",
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
                mailbox_cli_replies.inbox_resolve_from_args,
                self.args(action="continue", to=question.id),
            )

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("resolution", payload["message"]["kind"])

    def test_outbox_wait_heartbeats_fresh_projected_lease(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        supervisor_id = str(uuid.uuid4())
        original = self._write_projected_lease(supervisor_id, question)

        code, payload = self.payload_from(
            mailbox_cli_outbox.outbox_wait_from_args,
            self.args(since=0, timeout=0),
        )

        self.assertEqual(0, code)
        self.assertEqual(
            [question.id],
            [item["id"] for item in payload["questions"]],
        )
        after = self._lease_on_disk()
        self.assert_heartbeat(original, after)
        self.assertEqual(supervisor_id, after.supervisor_id)

    def test_outbox_wait_projects_and_notifies_one_actionable_open_set(
        self,
    ) -> None:
        self.write_active_turn(run_id="run-live")
        first = mailbox_appenders.append_question(
            self.plan_dir,
            "First private question",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        second = mailbox_appenders.append_question(
            self.plan_dir,
            "Second private question",
            state_dir=self.state_dir,
            run_id="run-live",
        )

        with (
            patched_env(KITTY_WINDOW_ID="42"),
            mock.patch.object(
                agent_mux_delivery,
                "notify",
                return_value={"final_status": "accepted"},
            ) as notify,
        ):
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=0),
            )

        self.assertEqual(0, code)
        self.assertNotIn("agent_mux", payload)
        notify.assert_called_once()
        event_name, hook_payload = notify.call_args.args
        kwargs = notify.call_args.kwargs
        self.assertEqual(
            agent_mux_delivery.MAILBOX_QUESTION_EVENT,
            event_name,
        )
        self.assertEqual("run-live", hook_payload["run_id"])
        self.assertEqual(42, hook_payload["kitty_window_id"])
        self.assertEqual(
            agent_mux_delivery.MAILBOX_QUESTION_REASON,
            kwargs["reason"],
        )
        self.assertEqual(second.id, kwargs["extra"]["question_id"])
        self.assertEqual(2, kwargs["extra"]["unanswered_questions"])
        self.assertEqual("open", kwargs["extra"]["delivery_state"])
        self.assertEqual(
            str(second.seq),
            kwargs["extra"]["mailbox_projection_sequence"],
        )
        self.assertEqual(second.seq, kwargs["allocate_sequence"]())
        self.assertEqual(
            f"run-live:mailbox-question:{second.seq}",
            kwargs["extra"]["source_event_id"],
        )
        lease = self._lease_on_disk()
        self.assertEqual(payload["supervisor_id"], lease.supervisor_id)
        self.assertEqual("run-live", lease.run_id)
        self.assertEqual(second.seq, lease.projection_seq)
        self.assertEqual(2, lease.unanswered_questions)
        self.assertEqual(second.id, lease.latest_unanswered_question_id)
        delivery_shape = repr((hook_payload, kwargs["extra"]))
        self.assertNotIn(first.text, delivery_shape)
        self.assertNotIn(second.text, delivery_shape)


if __name__ == "__main__":
    unittest.main()
