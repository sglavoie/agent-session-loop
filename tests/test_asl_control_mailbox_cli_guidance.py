"""Mailbox CLI guidance and reply waits and lease heartbeats.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import os
import unittest
import uuid
from contextlib import redirect_stdout
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    mailbox,
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_guidance,
    mailbox_cli_receipts,
    mailbox_cli_replies,
    mailbox_leases,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
)
from tests.asl_control_mailbox_cli_support import MailboxCliTestsBase
from tests.support import (
    _write_lock_file,
    _write_state_payload,
)


class MailboxCliTests(MailboxCliTestsBase):
    def test_reply_consumption_timeout_keeps_successful_append_truthful(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        question, _wait_start = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Need answer",
            state_dir=self.state_dir,
            run_id="run-live",
            session_id="5",
            turn_index=2,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        with mock.patch.object(
            mailbox_cli_receipts, "wait_for_receipt", return_value=None
        ):
            code, payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(
                    to=question.id,
                    text="answer",
                    wait_consumed=True,
                    wait_timeout=0,
                ),
            )
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("accepted", payload["delivery_state"])
        self.assertFalse(payload["consumed"])
        self.assertIsNone(payload["receipt"])

    def test_reply_json_exposes_consumption_receipt(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        question, wait_start = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Need answer",
            state_dir=self.state_dir,
            run_id="run-live",
            session_id="5",
            turn_index=2,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        receipt = mailbox_models.MailboxMessage(
            seq=4,
            id=str(uuid.uuid4()),
            kind=mailbox_vocabulary.KIND_RECEIPT,
            created_at="2026-08-01T00:00:00+00:00",
            to=question.id,
            wait_id=wait_start.id,
            closure_id=str(uuid.uuid4()),
        )
        with mock.patch.object(
            mailbox_cli_receipts, "wait_for_receipt", return_value=receipt
        ):
            code, payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(
                    to=question.id,
                    text="answer",
                    wait_consumed=True,
                    wait_timeout=1,
                ),
            )
        self.assertEqual(0, code)
        self.assertEqual("consumed", payload["delivery_state"])
        self.assertTrue(payload["consumed"])
        self.assertEqual(receipt.id, payload["receipt"]["id"])

    def test_guidance_and_reply_append_records_and_renew_lease(self) -> None:
        code, guidance_payload = self.payload_from(
            mailbox_cli_guidance.inbox_guidance_from_args,
            self.args(text="Keep the parser lazy."),
        )
        self.assertEqual(0, code)
        self.assertEqual("inbox-guidance", guidance_payload["command"])
        self.assertEqual("guidance", guidance_payload["message"]["kind"])
        self.assertEqual("Keep the parser lazy.", guidance_payload["message"]["text"])
        first_lease = mailbox_leases.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertTrue(first_lease.fresh)
        self.assertEqual(guidance_payload["supervisor_id"], first_lease.supervisor_id)

        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
        )
        code, reply_payload = self.payload_from(
            mailbox_cli_replies.inbox_reply_from_args,
            self.args(text="Use one object.", to=question.id),
        )
        self.assertEqual(0, code)
        self.assertEqual("reply", reply_payload["message"]["kind"])
        self.assertEqual(question.id, reply_payload["message"]["to"])
        self.assertEqual(
            "Use one object.",
            mailbox.find_reply(
                self.plan_dir,
                question.id,
                state_dir=self.state_dir,
            ).text,
        )
        second_lease = mailbox_leases.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(reply_payload["supervisor_id"], second_lease.supervisor_id)

    def test_guidance_without_wait_reports_what_it_did_not_observe(self) -> None:
        """The old line said "Queued guidance"; it now says why that is all.

        Without ``--wait-delivered`` nothing looked for a delivery, so the only
        truthful report is that the record is durable and undelivered so far --
        never that it was sent.
        """
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailbox_cli_guidance.inbox_guidance_from_args(
                self.args(text="Keep going.", json=False)
            )

        self.assertEqual(0, code)
        output = stdout.getvalue()
        self.assertIn("Guidance queued but not delivered", output)
        self.assertIn("no delivery wait requested", output)
        self.assertNotIn("sent", output)

        code, payload = self.payload_from(
            mailbox_cli_guidance.inbox_guidance_from_args,
            self.args(text="Keep going again."),
        )
        self.assertEqual(0, code)
        self.assertTrue(payload["appended"])
        self.assertFalse(payload["waited"])
        self.assertFalse(payload["delivered"])
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_QUEUED, payload["delivery_state"]
        )
        self.assertIsNone(payload["delivery"])

    def test_guidance_wait_reports_the_delivery_that_ended_it(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        with self._deliver_on_first_poll(
            run_id="run-live", session_id="5", turn_index=2, harness="codex"
        ):
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="Deliver me.", wait_delivered=True, wait_timeout=5.0),
            )

        self.assertEqual(0, code)
        self.assertTrue(payload["appended"])
        self.assertTrue(payload["waited"])
        self.assertTrue(payload["delivered"])
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE, payload["delivery_state"]
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_REASON_DELIVERED, payload["delivery_reason"]
        )
        self.assertEqual(
            payload["message"]["id"],
            payload["delivery"]["to"],
        )

    def test_guidance_wait_reports_a_deferral_for_the_active_turn(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        with self._deliver_on_first_poll(
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_HARNESS_UNSUPPORTED,
            run_id="run-live",
            session_id="5",
            turn_index=2,
        ):
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="Maybe later.", wait_delivered=True, wait_timeout=5.0),
            )

        # A deferral is a real answer for this turn, but it is not a delivery,
        # and the durable append still succeeded.
        self.assertEqual(0, code)
        self.assertTrue(payload["appended"])
        self.assertFalse(payload["delivered"])
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DEFERRED, payload["delivery_state"]
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_REASON_HARNESS_UNSUPPORTED,
            payload["delivery_reason"],
        )

    def test_guidance_wait_ignores_a_deferral_from_another_turn(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        with self._deliver_on_first_poll(
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_STALE_TURN,
            run_id="run-live",
            session_id="5",
            turn_index=1,
        ):
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="Stale news.", wait_delivered=True, wait_timeout=0.0),
            )

        self.assertEqual(0, code)
        self.assertFalse(payload["delivered"])
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_QUEUED, payload["delivery_state"]
        )
        self.assertIsNone(payload["delivery"])

    def test_guidance_wait_ignores_an_unconfirmed_attempt(self) -> None:
        """``awaiting-confirmation`` is not an answer, so it never ends a wait.

        The attempt is still running: reporting it would tell a supervisor the
        guidance was not delivered about guidance that is very likely about to
        be.
        """
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        with self._deliver_on_first_poll(
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_AWAITING_CONFIRMATION,
            run_id="run-live",
            session_id="5",
            turn_index=2,
            harness="claude",
        ):
            code, payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="Still working.", wait_delivered=True, wait_timeout=0.0),
            )

        self.assertEqual(0, code)
        self.assertTrue(payload["appended"])
        self.assertFalse(payload["delivered"])
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_QUEUED, payload["delivery_state"]
        )
        self.assertIsNone(payload["delivery"])

    def test_guidance_wait_text_says_delivered_live_and_to_whom(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        stdout = io.StringIO()
        with (
            self._deliver_on_first_poll(
                run_id="run-live", session_id="5", turn_index=2, harness="codex"
            ),
            redirect_stdout(stdout),
        ):
            code = mailbox_cli_guidance.inbox_guidance_from_args(
                self.args(
                    text="Deliver me.",
                    json=False,
                    wait_delivered=True,
                    wait_timeout=5.0,
                )
            )

        self.assertEqual(0, code)
        output = stdout.getvalue()
        self.assertIn("Guidance delivered live to codex", output)
        self.assertNotIn("Guidance sent", output)

    def test_guidance_wait_text_says_queued_and_why(self) -> None:
        self.write_active_turn(run_id="run-live", session_id="5", turn_index=2)
        stdout = io.StringIO()
        with (
            self._deliver_on_first_poll(
                delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
                delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER,
                run_id="run-live",
                session_id="5",
                turn_index=2,
                harness="copilot",
            ),
            redirect_stdout(stdout),
        ):
            code = mailbox_cli_guidance.inbox_guidance_from_args(
                self.args(
                    text="Maybe later.",
                    json=False,
                    wait_delivered=True,
                    wait_timeout=5.0,
                )
            )

        self.assertEqual(0, code)
        output = stdout.getvalue()
        self.assertIn("Guidance queued but not delivered", output)
        self.assertIn("this harness cannot be steered mid-turn", output)
        self.assertNotIn("Guidance sent", output)

    def test_guidance_wait_timeout_keeps_the_append_successful(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli_guidance.inbox_guidance_from_args,
            self.args(text="Nobody home.", wait_delivered=True, wait_timeout=0.0),
        )

        self.assertEqual(0, code)
        self.assertTrue(payload["appended"])
        self.assertTrue(payload["waited"])
        self.assertFalse(payload["delivered"])
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_QUEUED, payload["delivery_state"]
        )
        self.assertIsNone(payload["delivery_reason"])
        # The guidance is durable regardless of what the wait observed.
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            [payload["message"]["id"]],
            [message.id for message in view.deliverable_guidance],
        )

    def test_guidance_wait_timeout_defaults_to_thirty_seconds(self) -> None:
        observed: dict[str, float] = {}

        def capture(plan_dir, guidance_id, *, state_dir, timeout, active_turn):
            observed["timeout"] = timeout
            return None

        with mock.patch.object(
            mailbox_cli_receipts, "wait_for_guidance_delivery", side_effect=capture
        ):
            code, _payload = self.payload_from(
                mailbox_cli_guidance.inbox_guidance_from_args,
                self.args(text="Default me.", wait_delivered=True),
            )

        self.assertEqual(0, code)
        self.assertEqual(
            mailbox_cli_constants.DEFAULT_GUIDANCE_DELIVERED_TIMEOUT_SECONDS,
            observed["timeout"],
        )

    def test_guidance_timeout_without_wait_is_refused_before_appending(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli_guidance.inbox_guidance_from_args,
            self.args(text="Too eager.", wait_timeout=5.0),
        )

        self.assertEqual(1, code)
        self.assertEqual("invalid-timeout", payload["reason"])
        self.assertEqual(
            (),
            mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir).messages,
        )

    def test_guidance_wait_rejects_unbounded_timeouts(self) -> None:
        for requested in (-1.0, float("inf"), float("nan"), 1801.0):
            with self.subTest(timeout=requested):
                code, payload = self.payload_from(
                    mailbox_cli_guidance.inbox_guidance_from_args,
                    self.args(
                        text="Forever?", wait_delivered=True, wait_timeout=requested
                    ),
                )

                self.assertEqual(1, code)
                self.assertEqual("invalid-timeout", payload["reason"])

    def test_inbox_guidance_heartbeats_fresh_projected_lease(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        supervisor_id = str(uuid.uuid4())
        original = self._write_projected_lease(supervisor_id, question)

        code, payload = self.payload_from(
            mailbox_cli_guidance.inbox_guidance_from_args,
            self.args(text="Keep the parser lazy."),
        )

        self.assertEqual(0, code)
        after = self._lease_on_disk()
        self.assert_heartbeat(original, after)
        self.assertEqual(supervisor_id, after.supervisor_id)
        self.assertEqual(supervisor_id, payload["supervisor_id"])

    def test_inbox_reply_heartbeats_fresh_projected_lease(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        supervisor_id = str(uuid.uuid4())
        original = self._write_projected_lease(supervisor_id, question)

        with mock.patch.object(
            agent_mux_delivery,
            "notify",
            return_value={"final_status": "accepted"},
        ):
            code, payload = self.payload_from(
                mailbox_cli_replies.inbox_reply_from_args,
                self.args(text="Use one object.", to=question.id),
            )

        self.assertEqual(0, code)
        after = self._lease_on_disk()
        self.assert_heartbeat(original, after)
        self.assertEqual(supervisor_id, after.supervisor_id)
        self.assertEqual(supervisor_id, payload["supervisor_id"])

    def test_inbox_resolve_heartbeats_and_appends_owned_resolution(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": "run-live", "ok": True, "paused": False},
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Proceed without an answer?",
            state_dir=self.state_dir,
            run_id="run-live",
        )
        supervisor_id = str(uuid.uuid4())
        original = self._write_projected_lease(supervisor_id, question)

        code, payload = self.payload_from(
            mailbox_cli_replies.inbox_resolve_from_args,
            self.args(
                action="continue",
                note="Informational only.",
                to=question.id,
            ),
        )

        self.assertEqual(0, code)
        self.assertEqual("inbox-resolve", payload["command"])
        self.assertEqual("run-live", payload["run_id"])
        self.assertEqual("resolution", payload["message"]["kind"])
        self.assertEqual("continue", payload["message"]["action"])
        self.assertEqual("Informational only.", payload["message"]["note"])
        self.assertEqual(question.id, payload["message"]["to"])
        after = self._lease_on_disk()
        self.assert_heartbeat(original, after)
        self.assertEqual(supervisor_id, after.supervisor_id)
        resolution = mailbox.find_resolution(
            self.plan_dir,
            question.id,
            state_dir=self.state_dir,
        )
        self.assertIsNotNone(resolution)
        assert resolution is not None
        self.assertEqual(payload["message"]["id"], resolution.id)


if __name__ == "__main__":
    unittest.main()
