"""MailboxTestsPart2.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import contextlib
import stat
import unittest
from unittest import mock

from agent_session_loop import (
    handoff_guards,
    mailbox,
    mailbox_append_record,
    mailbox_appenders,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    mailbox_waits,
)
from tests.asl_control_mailbox_support import MailboxTestsBase


class MailboxTestsPart2(MailboxTestsBase):
    def test_delivery_projections_report_latest_successful_and_deliverable(
        self,
    ) -> None:
        first = self.guidance("first")
        second = self.guidance("second")
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            (first.id, second.id),
            tuple(message.id for message in view.deliverable_guidance),
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_QUEUED,
            view.guidance_delivery_state(first.id),
        )
        self.assertIsNone(view.latest_guidance_delivery(first.id))

        deferred = self.deliver(
            first.id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_NO_ACTIVE_TURN,
        )
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        # Deferral is "not yet", never "no".
        self.assertEqual(deferred, view.latest_guidance_delivery(first.id))
        self.assertIsNone(view.successful_guidance_delivery(first.id))
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            view.guidance_delivery_state(first.id),
        )
        self.assertIn(first.id, [m.id for m in view.deliverable_guidance])

        delivered = self.deliver(
            first.id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_NEXT_TURN,
            channel=mailbox_vocabulary.GUIDANCE_CHANNEL_NEXT_TURN,
            run_id="run-A",
            session_id="1",
            turn_index=2,
        )
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(delivered, view.latest_guidance_delivery(first.id))
        self.assertEqual(delivered, view.successful_guidance_delivery(first.id))
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_NEXT_TURN,
            view.guidance_delivery_state(first.id),
        )
        self.assertEqual(
            (second.id,),
            tuple(message.id for message in view.deliverable_guidance),
        )
        self.assertEqual(
            (second.id,),
            tuple(
                message.id
                for message in mailbox_appenders.deliverable_guidance(
                    self.plan_dir, state_dir=self.state_dir
                )
            ),
        )
        self.assertEqual(2, view.counts.guidance_deliveries)
        self.assertEqual(1, view.counts.deliverable_guidance)
        self.assertEqual(2, view.counts.to_payload()["guidance_deliveries"])

    def test_legacy_acknowledgement_still_counts_as_read_delivery(self) -> None:
        guidance = self.guidance("read me the old way")
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_QUEUED,
            view.guidance_delivery_state(guidance.id),
        )

        mailbox_appenders.append_acknowledgement(
            self.plan_dir, guidance.seq, state_dir=self.state_dir
        )

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_BY_READ,
            view.guidance_delivery_state(guidance.id),
        )
        self.assertEqual((), view.deliverable_guidance)
        # Nothing was rewritten to say so: the ledger stayed empty.
        self.assertEqual((), view.guidance_deliveries(guidance.id))

    def test_timeout_wins_lock_then_late_reply_requires_resumed_wait(self) -> None:
        question, first_wait = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Still blocked?",
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=1,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        timed_out = mailbox_waits.finish_wait(
            self.plan_dir,
            question.id,
            first_wait.id,
            reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
            state_dir=self.state_dir,
        )
        self.assertIsNone(timed_out.closure)
        with self.assertRaises(mailbox_models.MailboxError) as raised:
            mailbox_appenders.append_reply(
                self.plan_dir,
                question.id,
                "too late",
                state_dir=self.state_dir,
            )
        self.assertEqual(
            mailbox_constants.REJECT_QUESTION_NOT_WAITING, raised.exception.reason
        )

        resumed_wait = mailbox_waits.append_wait_start(
            self.plan_dir, question.id, state_dir=self.state_dir
        )
        reply = mailbox_appenders.append_reply(
            self.plan_dir,
            question.id,
            "now accepted",
            state_dir=self.state_dir,
        )
        consumed = mailbox_waits.finish_wait(
            self.plan_dir,
            question.id,
            resumed_wait.id,
            reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
            state_dir=self.state_dir,
        )
        self.assertEqual(reply, consumed.closure)
        self.assertTrue(consumed.consumed)

    def test_closure_wins_lock_over_timeout_and_records_receipt(self) -> None:
        question, wait_start = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Race?",
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=1,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            expected_run_id="run-A",
            state_dir=self.state_dir,
        )
        completion = mailbox_waits.finish_wait(
            self.plan_dir,
            question.id,
            wait_start.id,
            reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
            state_dir=self.state_dir,
        )
        self.assertEqual(resolution, completion.closure)
        self.assertTrue(completion.consumed)
        self.assertEqual(
            mailbox_vocabulary.WAIT_END_REASON_CLOSURE,
            completion.wait_end.wait_end_reason,
        )

    def test_runner_completion_guard_uses_body_free_lifecycle_evidence(self) -> None:
        secret_question = "private blocking body"
        question, wait_start = mailbox_waits.append_wait_question(
            self.plan_dir,
            secret_question,
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=4,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        evidence = handoff_guards._mailbox_completion_guard_evidence(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            run_id="run-A",
            session_id="1",
            turn_index=4,
        )
        assert evidence is not None
        self.assertEqual("unanswered", evidence["questions"][0]["violation"])
        self.assertNotIn(secret_question, repr(evidence))

        mailbox_appenders.append_reply(
            self.plan_dir, question.id, "private answer", state_dir=self.state_dir
        )
        evidence = handoff_guards._mailbox_completion_guard_evidence(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            run_id="run-A",
            session_id="1",
            turn_index=4,
        )
        assert evidence is not None
        self.assertEqual("unconsumed", evidence["questions"][0]["violation"])
        self.assertNotIn("private answer", repr(evidence))

        mailbox_waits.consume_wait_closure(
            self.plan_dir,
            question.id,
            wait_start.id,
            state_dir=self.state_dir,
        )
        self.assertIsNone(
            handoff_guards._mailbox_completion_guard_evidence(
                plan_dir=self.plan_dir,
                state_dir_arg=str(self.state_dir),
                run_id="run-A",
                session_id="1",
                turn_index=4,
            )
        )

        pause_question, pause_wait = mailbox_waits.append_wait_question(
            self.plan_dir,
            "pause privately",
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=5,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        mailbox_appenders.append_resolution(
            self.plan_dir,
            pause_question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
            expected_run_id="run-A",
            note="private note",
            state_dir=self.state_dir,
        )
        mailbox_waits.consume_wait_closure(
            self.plan_dir,
            pause_question.id,
            pause_wait.id,
            state_dir=self.state_dir,
        )
        evidence = handoff_guards._mailbox_completion_guard_evidence(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            run_id="run-A",
            session_id="1",
            turn_index=5,
        )
        assert evidence is not None
        self.assertEqual("pause-resolved", evidence["questions"][0]["violation"])
        self.assertNotIn("private note", repr(evidence))

    def test_capacity_reserves_cannot_strand_closure_end_or_receipt(self) -> None:
        limits = {
            "MAILBOX_MAX_BYTES": 4096,
            "MAILBOX_CLOSURE_RESERVE_BYTES": 1536,
            "MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES": 768,
            "MAILBOX_WAIT_END_RESERVE_BYTES": 768,
            "MAILBOX_RECEIPT_RESERVE_BYTES": 384,
        }
        with contextlib.ExitStack() as stack:
            for module in (
                mailbox_append_record,
                mailbox_snapshot,
                mailbox_waits,
            ):
                present = {k: v for k, v in limits.items() if hasattr(module, k)}
                stack.enter_context(mock.patch.multiple(module, **present))
            question, wait_start = mailbox_waits.append_wait_question(
                self.plan_dir,
                "Capacity?",
                state_dir=self.state_dir,
                run_id="run-A",
                session_id="1",
                turn_index=1,
                waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
            )
            last_guidance = None
            while True:
                try:
                    last_guidance = mailbox_appenders.append_guidance(
                        self.plan_dir,
                        "g" * 80,
                        state_dir=self.state_dir,
                    )
                except mailbox_models.MailboxError as error:
                    self.assertEqual(mailbox_constants.ERROR_MAILBOX_FULL, error.reason)
                    break
            assert last_guidance is not None
            # The guidance that saturated the ordinary tier must still be able
            # to say what became of it.
            delivery = mailbox_appenders.append_guidance_delivery(
                self.plan_dir,
                last_guidance.id,
                delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
                delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_HARNESS_UNSUPPORTED,
                channel=mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
                state_dir=self.state_dir,
            )
            self.assertEqual(last_guidance.id, delivery.to)
            reply = mailbox_appenders.append_reply(
                self.plan_dir,
                question.id,
                "accepted after ordinary capacity exhausted",
                state_dir=self.state_dir,
            )
            completion = mailbox_waits.finish_wait(
                self.plan_dir,
                question.id,
                wait_start.id,
                reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
                state_dir=self.state_dir,
            )
            self.assertEqual(reply, completion.closure)
            self.assertTrue(completion.consumed)

    def test_missing_mailbox_is_an_empty_view_and_read_does_not_create_it(
        self,
    ) -> None:
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual((), view.messages)
        self.assertEqual((), view.rejected)
        self.assertEqual(0, view.last_seq)
        self.assertFalse(self.mailbox_path().exists())

    def test_malformed_lines_keep_physical_sequences_without_poisoning_appends(
        self,
    ) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-json")
        path.chmod(0o644)

        appended = mailbox_appenders.append_guidance(
            self.plan_dir,
            "Still valid.",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )

        self.assertEqual(2, appended.seq)
        self.assertEqual((appended,), view.messages)
        self.assertEqual(2, view.last_seq)
        self.assertEqual(1, len(view.rejected))
        self.assertEqual(mailbox_constants.REJECT_INVALID_JSON, view.rejected[0].reason)
        self.assertEqual(1, view.rejected[0].seq)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(2, len(path.read_bytes().splitlines()))

    def test_validation_rejects_bad_records_and_preserves_valid_neighbors(
        self,
    ) -> None:
        valid_id = self.message_id()
        self.append_raw([])
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": self.message_id(),
                "kind": "launch",
                "version": 1,
            }
        )
        self.append_raw(
            {
                "created_at": "not-a-time",
                "id": self.message_id(),
                "kind": "guidance",
                "text": "x",
                "version": 1,
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": valid_id,
                "kind": "guidance",
                "text": "valid",
                "version": 1,
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": valid_id,
                "kind": "guidance",
                "text": "duplicate",
                "version": 1,
            }
        )

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual([4], [message.seq for message in view.messages])
        self.assertEqual(
            [
                mailbox_constants.REJECT_NOT_OBJECT,
                mailbox_constants.REJECT_UNKNOWN_KIND,
                mailbox_constants.REJECT_INVALID_CREATED_AT,
                mailbox_constants.REJECT_DUPLICATE_ID,
            ],
            [record.reason for record in view.rejected],
        )
        self.assertEqual(5, view.last_seq)


if __name__ == "__main__":
    unittest.main()
