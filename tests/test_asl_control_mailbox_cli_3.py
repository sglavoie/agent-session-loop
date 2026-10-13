"""MailboxCliTestsPart3.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import json
import unittest
import uuid
from datetime import UTC, datetime
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_cli_guidance,
    mailbox_cli_outbox,
    mailbox_cli_replies,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    state,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase
from tests.support import (
    patched_env,
)


class MailboxCliTestsPart3(MailboxCliTestsBase):
    def test_outbox_wait_does_not_notify_closed_question_history(self) -> None:
        self.write_active_turn(run_id="run-live")
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Already handled",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        mailbox_appenders.append_reply(
            self.plan_dir,
            question.id,
            "done",
            state_dir=self.state_dir,
        )

        with mock.patch.object(agent_mux_delivery, "notify") as notify:
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=0),
            )

        self.assertEqual(0, code)
        self.assertEqual([question.id], [q["id"] for q in payload["questions"]])
        notify.assert_not_called()
        self.assertIsNone(self._lease_on_disk().run_id)

    def test_outbox_wait_question_notification_is_fail_open(self) -> None:
        self.write_active_turn(run_id="run-live")
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Proceed?",
            state_dir=self.state_dir,
            run_id="run-live",
        )

        with mock.patch.object(
            agent_mux_delivery,
            "notify",
            side_effect=RuntimeError("AgentMux unavailable"),
        ):
            code, payload = self.payload_from(
                mailbox_cli_outbox.outbox_wait_from_args,
                self.args(timeout=0),
            )

        self.assertEqual(0, code)
        self.assertEqual([question.id], [q["id"] for q in payload["questions"]])
        lease = self._lease_on_disk()
        self.assertEqual("run-live", lease.run_id)
        self.assertEqual(1, lease.unanswered_questions)

    def test_stale_projected_lease_is_taken_over_without_projection(
        self,
    ) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        stale_id = str(uuid.uuid4())
        self._write_projected_lease(
            stale_id,
            question,
            age_seconds=mailbox_constants.MAILBOX_LEASE_FRESH_SECONDS + 60.0,
        )

        code, payload = self.payload_from(
            mailbox_cli_guidance.inbox_guidance_from_args,
            self.args(text="Keep the parser lazy."),
        )

        self.assertEqual(0, code)
        after = self._lease_on_disk()
        self.assertNotEqual(stale_id, after.supervisor_id)
        self.assertIsNone(after.run_id)
        self.assertIsNone(after.unanswered_questions)
        self.assertEqual(after.supervisor_id, payload["supervisor_id"])

    def test_missing_v1_and_unprojected_leases_are_taken_over(self) -> None:
        lease_path = mailbox_cursors.lease_path_for_plan(self.plan_dir, self.state_dir)

        with self.subTest(state="missing"):
            self.assertFalse(lease_path.exists())
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="first"),
            )
            self.assertEqual(0, code)
            after = self._lease_on_disk()
            self.assertIsNone(after.run_id)
            self.assertEqual(after.supervisor_id, payload["supervisor_id"])

        with self.subTest(state="v1-fresh"):
            v1_id = str(uuid.uuid4())
            raw = json.dumps(
                {
                    "supervisor_id": v1_id,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "version": 1,
                },
                separators=(",", ":"),
            ).encode()
            lease_path.write_bytes(
                (raw + b"\n").ljust(mailbox_constants.MAILBOX_LEASE_BYTES, b" ")
            )
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="second"),
            )
            self.assertEqual(0, code)
            after = self._lease_on_disk()
            self.assertNotEqual(v1_id, after.supervisor_id)
            self.assertEqual(mailbox_constants.MAILBOX_LEASE_VERSION, after.version)
            self.assertIsNone(after.run_id)
            self.assertEqual(after.supervisor_id, payload["supervisor_id"])

        with self.subTest(state="unprojected-fresh-v2"):
            before = self._lease_on_disk()
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="third"),
            )
            self.assertEqual(0, code)
            after = self._lease_on_disk()
            self.assertNotEqual(before.supervisor_id, after.supervisor_id)
            self.assertIsNone(after.run_id)
            self.assertEqual(after.supervisor_id, payload["supervisor_id"])

    def test_unreadable_lease_degrades_to_takeover(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        live_id = str(uuid.uuid4())
        self._write_projected_lease(live_id, question)
        unreadable = mailbox_models.MailboxError(
            mailbox_constants.ERROR_LEASE_READ,
            "temporarily unavailable",
            path=mailbox_cursors.lease_path_for_plan(self.plan_dir, self.state_dir),
        )

        with mock.patch.object(
            mailbox_leases,
            "read_lease",
            side_effect=unreadable,
        ):
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="Keep the parser lazy."),
            )

        self.assertEqual(0, code)
        after = self._lease_on_disk()
        self.assertNotEqual(live_id, after.supervisor_id)
        self.assertIsNone(after.run_id)
        self.assertEqual(after.supervisor_id, payload["supervisor_id"])

    def test_reply_emits_text_free_agent_mux_clear_from_prewrite_projection(
        self,
    ) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="lease-run-not-current-state",
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="lease-run-not-current-state",
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
            code, reply_payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(text="Use one object.", to=question.id),
            )

        self.assertEqual(0, code)
        notify.assert_called_once()
        event_name, payload = notify.call_args.args
        kwargs = notify.call_args.kwargs
        self.assertEqual(agent_mux_delivery.MAILBOX_REPLY_EVENT, event_name)
        self.assertEqual("lease-run-not-current-state", payload["run_id"])
        self.assertEqual(str(self.plan_dir.resolve()), payload["plan_dir"])
        self.assertEqual(
            str(
                state.plan_state_dir(self.plan_dir, self.state_dir)
                / events.STATE_FILE_NAME
            ),
            payload["state_path"],
        )
        self.assertEqual(42, payload["kitty_window_id"])
        self.assertEqual(agent_mux_delivery.MAILBOX_REPLY_REASON, kwargs["reason"])
        self.assertEqual(question.id, kwargs["extra"]["question_id"])
        self.assertEqual(
            str(question.seq),
            kwargs["extra"]["mailbox_projection_sequence"],
        )
        reply_seq = reply_payload["message"]["seq"]
        self.assertEqual(reply_seq, kwargs["allocate_sequence"]())
        self.assertEqual(
            f"lease-run-not-current-state:mailbox-reply:{reply_seq}",
            kwargs["extra"]["source_event_id"],
        )
        delivery_shape = json.dumps(
            {"payload": payload, "extra": kwargs["extra"]},
            sort_keys=True,
        )
        self.assertNotIn("Which shape?", delivery_shape)
        self.assertNotIn("Use one object.", delivery_shape)

    def test_reply_fast_clear_ignores_a_question_abandoned_by_an_earlier_run(
        self,
    ) -> None:
        mailbox_appenders.append_question(
            self.plan_dir,
            "Wrangler auth expired?",
            state_dir=self.state_dir,
            run_id="run-that-already-paused",
        )
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-2",
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-2",
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
            code, _payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(text="Use one object.", to=question.id),
            )

        self.assertEqual(0, code)
        notify.assert_called_once()
        # The orphan stays in the file as history; it just no longer gates.
        self.assertEqual(
            ("run-that-already-paused",),
            tuple(
                message.run_id
                for message in mailbox.unanswered_questions(
                    self.plan_dir,
                    state_dir=self.state_dir,
                )
            ),
        )

    def test_reply_fast_clear_requires_fresh_matching_sole_question(
        self,
    ) -> None:
        first = mailbox_appenders.append_question(
            self.plan_dir,
            "First?",
            state_dir=self.state_dir,
            run_id="run-1",
        )
        second = mailbox_appenders.append_question(
            self.plan_dir,
            "Second?",
            state_dir=self.state_dir,
            run_id="run-1",
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-1",
            projection_seq=second.seq,
            unanswered_questions=2,
            latest_unanswered_question_id=second.id,
            latest_unanswered_question_created_at=second.created_at,
        )

        with mock.patch.object(agent_mux_delivery, "notify") as notify:
            code, _payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(text="Second answer", to=second.id),
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

    def test_reply_before_first_supervisor_poll_fires_predates_fast_clear(
        self,
    ) -> None:
        guidance = mailbox_appenders.append_guidance(
            self.plan_dir,
            "Prefer the flat layout.",
            state_dir=self.state_dir,
        )
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        # The supervisor's last poll predates the question: the lease still
        # projects zero unanswered questions at the guidance sequence.
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-live",
            projection_seq=guidance.seq,
            unanswered_questions=0,
        )

        with mock.patch.object(
            agent_mux_delivery,
            "notify",
            return_value={"final_status": "accepted"},
        ) as notify:
            code, reply_payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(text="Use one object.", to=question.id),
            )

        self.assertEqual(0, code)
        notify.assert_called_once()
        event_name, payload = notify.call_args.args
        kwargs = notify.call_args.kwargs
        self.assertEqual(agent_mux_delivery.MAILBOX_REPLY_EVENT, event_name)
        self.assertEqual("run-live", payload["run_id"])
        self.assertEqual(question.id, kwargs["extra"]["question_id"])
        reply_seq = reply_payload["message"]["seq"]
        self.assertEqual(
            str(reply_seq),
            kwargs["extra"]["mailbox_projection_sequence"],
        )
        self.assertNotEqual(
            str(guidance.seq),
            kwargs["extra"]["mailbox_projection_sequence"],
        )

    def test_reply_before_first_supervisor_poll_at_sequence_zero_fires_fast_clear(
        self,
    ) -> None:
        # The supervisor polled while the mailbox was still empty: its lease
        # validly projects zero unanswered questions at sequence 0, which
        # predates the mailbox's first-ever question.
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000099",
            state_dir=self.state_dir,
            run_id="run-live",
            projection_seq=0,
            unanswered_questions=0,
        )
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        self.assertEqual(1, question.seq)

        with mock.patch.object(
            agent_mux_delivery,
            "notify",
            return_value={"final_status": "accepted"},
        ) as notify:
            code, reply_payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(text="Use one object.", to=question.id),
            )

        self.assertEqual(0, code)
        notify.assert_called_once()
        event_name, payload = notify.call_args.args
        kwargs = notify.call_args.kwargs
        self.assertEqual(agent_mux_delivery.MAILBOX_REPLY_EVENT, event_name)
        self.assertEqual("run-live", payload["run_id"])
        self.assertEqual(question.id, kwargs["extra"]["question_id"])
        reply_seq = reply_payload["message"]["seq"]
        self.assertEqual(
            str(reply_seq),
            kwargs["extra"]["mailbox_projection_sequence"],
        )
        self.assertNotEqual(
            "0",
            kwargs["extra"]["mailbox_projection_sequence"],
        )


if __name__ == "__main__":
    unittest.main()
