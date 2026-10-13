"""MailboxTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import json
import stat
import unittest

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
    state,
    turn_execution_executor,
)
from tests.asl_control_mailbox_support import MailboxTestsBase


class MailboxTests(MailboxTestsBase):
    def test_paths_use_canonical_per_plan_state_directory(self) -> None:
        plan_state_dir = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.assertEqual(
            plan_state_dir / mailbox_constants.MAILBOX_FILE_NAME,
            self.mailbox_path(),
        )
        self.assertEqual(
            plan_state_dir / mailbox_constants.MAILBOX_LEASE_FILE_NAME,
            self.lease_path(),
        )

    def test_append_and_non_destructive_read_round_trip(self) -> None:
        guidance = mailbox_appenders.append_guidance(
            self.plan_dir,
            "Prefer the existing parser.",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Should this preserve legacy output?",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )
        reply = mailbox_appenders.append_reply(
            self.plan_dir,
            question.id,
            "Yes.",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )

        first = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        second = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(first, second)
        self.assertEqual((guidance, question, reply), first.messages)
        self.assertEqual(3, first.last_seq)
        self.assertEqual((), first.rejected)
        self.assertEqual(reply, first.reply_for(question.id))
        self.assertEqual((), first.unanswered_questions)
        self.assertEqual(
            {
                "acknowledgements": 0,
                "guidance": 1,
                "pending_guidance": 1,
                "questions": 1,
                "rejected": 0,
                "replies": 1,
                "resolutions": 0,
                "notices": 0,
                "unanswered_questions": 0,
            },
            first.counts.to_payload(),
        )
        self.assertGreater(first.size_bytes, 0)
        self.assertEqual(
            stat.S_IMODE(self.mailbox_path().stat().st_mode),
            0o600,
        )

    def test_v3_wait_question_creation_and_consumption_are_atomic(self) -> None:
        question, wait_start = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Choose A or B?",
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=2,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
            created_at=self.timestamp(),
        )
        initial = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((question, wait_start), initial.messages)
        self.assertEqual(mailbox_vocabulary.DELIVERY_MODE_WAIT, question.delivery_mode)
        self.assertEqual(wait_start, initial.active_wait_for(question.id))
        self.assertEqual(
            wait_start,
            initial.active_worker_wait(run_id="run-A", session_id="1", turn_index=2),
        )
        self.assertTrue(
            turn_execution_executor.worker_input_wait_probe(
                plan_dir=self.plan_dir,
                state_dir=self.state_dir,
                run_id="run-A",
                session_id="1",
                turn_index=2,
            )
        )
        self.assertIsNone(
            initial.active_worker_wait(run_id="run-A", session_id="other", turn_index=2)
        )

        reply = mailbox_appenders.append_reply(
            self.plan_dir,
            question.id,
            "A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        completion = mailbox_waits.consume_wait_closure(
            self.plan_dir,
            question.id,
            wait_start.id,
            state_dir=self.state_dir,
        )
        assert completion is not None
        self.assertEqual(reply, completion.closure)
        self.assertIsNotNone(completion.receipt)
        self.assertEqual(
            mailbox_vocabulary.WAIT_END_REASON_CLOSURE,
            completion.wait_end.wait_end_reason,
        )
        final = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("consumed", final.delivery_state_for(question.id))
        self.assertIsNone(final.active_wait_for(question.id))
        self.assertIsNone(
            final.active_worker_wait(run_id="run-A", session_id="1", turn_index=2)
        )
        self.assertEqual(1, final.counts.receipts)

    def test_mixed_v1_v2_v3_v4_log_parses_without_rewrite(self) -> None:
        legacy_guidance_id = self.message_id()
        v2_question_id = self.message_id()
        v2_reply_id = self.message_id()
        v3_wait_question_id = self.message_id()
        v3_wait_start_id = self.message_id()
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": legacy_guidance_id,
                "kind": mailbox_vocabulary.KIND_GUIDANCE,
                "text": "v1",
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": v2_question_id,
                "kind": mailbox_vocabulary.KIND_QUESTION,
                "run_id": "run-old",
                "text": "v2",
                "version": mailbox_constants.MAILBOX_PREVIOUS_SCHEMA_VERSION,
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": v2_reply_id,
                "kind": mailbox_vocabulary.KIND_REPLY,
                "text": "old answer",
                "to": v2_question_id,
                "version": mailbox_constants.MAILBOX_PREVIOUS_SCHEMA_VERSION,
            }
        )
        # A genuine v3 wait-mode question written before the v4 writer existed.
        # Wait strictness is a statement about the question's own vintage, so
        # bumping the write version must not quietly relax it.
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "delivery_mode": mailbox_vocabulary.DELIVERY_MODE_WAIT,
                "id": v3_wait_question_id,
                "kind": mailbox_vocabulary.KIND_QUESTION,
                "run_id": "run-v3",
                "session_id": "1",
                "text": "v3 wait",
                "turn_index": 1,
                "version": mailbox_constants.MAILBOX_WAIT_SCHEMA_VERSION,
                "waiter_origin": mailbox_vocabulary.WAITER_ORIGIN_WORKER,
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": v3_wait_start_id,
                "kind": mailbox_vocabulary.KIND_WAIT_START,
                "to": v3_wait_question_id,
                "version": mailbox_constants.MAILBOX_WAIT_SCHEMA_VERSION,
            }
        )
        v4_question, v4_wait = mailbox_waits.append_wait_question(
            self.plan_dir,
            "v4",
            state_dir=self.state_dir,
            run_id="run-new",
            session_id="1",
            turn_index=1,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), view.rejected)
        self.assertEqual(
            [
                mailbox_constants.MAILBOX_LEGACY_SCHEMA_VERSION,
                mailbox_constants.MAILBOX_PREVIOUS_SCHEMA_VERSION,
                mailbox_constants.MAILBOX_PREVIOUS_SCHEMA_VERSION,
                mailbox_constants.MAILBOX_WAIT_SCHEMA_VERSION,
                mailbox_constants.MAILBOX_WAIT_SCHEMA_VERSION,
                mailbox_constants.MAILBOX_SCHEMA_VERSION,
                mailbox_constants.MAILBOX_SCHEMA_VERSION,
            ],
            [message.version for message in view.messages],
        )
        self.assertEqual(v4_wait, view.active_wait_for(v4_question.id))
        self.assertEqual(
            v3_wait_start_id,
            view.active_wait_for(v3_wait_question_id).id,
        )

        # The v3 question is still strict under the v4 writer: ending its wait
        # makes a later reply inadmissible.
        mailbox_waits.finish_wait(
            self.plan_dir,
            v3_wait_question_id,
            v3_wait_start_id,
            reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
            state_dir=self.state_dir,
        )
        with self.assertRaises(mailbox_models.MailboxError) as raised:
            mailbox_appenders.append_reply(
                self.plan_dir,
                v3_wait_question_id,
                "too late",
                state_dir=self.state_dir,
            )
        self.assertEqual(
            mailbox_constants.REJECT_QUESTION_NOT_WAITING, raised.exception.reason
        )

    def test_delivery_record_names_guidance_and_never_quotes_it(self) -> None:
        guidance = self.guidance("Do not repeat me anywhere.")
        delivery = self.deliver(
            guidance.id,
            run_id="run-A",
            session_id="1",
            turn_index=2,
            harness="codex",
        )

        self.assertEqual(mailbox_vocabulary.KIND_GUIDANCE_DELIVERY, delivery.kind)
        self.assertEqual(guidance.id, delivery.to)
        self.assertIsNone(delivery.text)
        record = delivery.to_record()
        self.assertNotIn("text", record)
        self.assertEqual(mailbox_constants.MAILBOX_SCHEMA_VERSION, record["version"])
        self.assertEqual(
            {
                "channel": mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
                "delivery_reason": mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
                "delivery_state": mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
                "harness": "codex",
                "run_id": "run-A",
                "session_id": "1",
                "to": guidance.id,
                "turn_index": 2,
            },
            {
                key: value
                for key, value in record.items()
                if key not in {"created_at", "id", "kind", "version"}
            },
        )

        payload = mailbox_appenders.guidance_delivery_event_payload(delivery)
        self.assertEqual(guidance.id, payload["guidance_id"])
        self.assertEqual(mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE, payload["channel"])
        serialized = json.dumps(payload)
        self.assertNotIn("Do not repeat me anywhere.", serialized)
        self.assertEqual(
            (),
            tuple(key for key in payload if "text" in key or "digest" in key),
        )
        with self.assertRaises(ValueError):
            mailbox_appenders.guidance_delivery_event_payload(guidance)

    def test_delivery_writes_validate_state_reason_channel_and_identity(self) -> None:
        guidance = self.guidance()
        cases = (
            (
                mailbox_constants.REJECT_GUIDANCE_NOT_FOUND,
                {"guidance_id": self.message_id()},
            ),
            (mailbox_constants.REJECT_INVALID_TARGET, {"guidance_id": "not-a-uuid"}),
            (
                mailbox_constants.REJECT_INVALID_DELIVERY_STATE,
                {"delivery_state": mailbox_vocabulary.GUIDANCE_STATE_QUEUED},
            ),
            (
                mailbox_constants.REJECT_INVALID_DELIVERY_REASON,
                {"delivery_reason": mailbox_vocabulary.GUIDANCE_REASON_STALE_TURN},
            ),
            (
                mailbox_constants.REJECT_INVALID_DELIVERY_CHANNEL,
                {"channel": mailbox_vocabulary.GUIDANCE_CHANNEL_READ},
            ),
            (mailbox_constants.REJECT_INVALID_HARNESS, {"harness": "x" * 65}),
            (mailbox_constants.REJECT_INCOMPLETE_TURN_IDENTITY, {"run_id": "run-A"}),
            (
                mailbox_constants.REJECT_INCOMPLETE_TURN_IDENTITY,
                {"run_id": "run-A", "session_id": "1"},
            ),
        )
        for reason, overrides in cases:
            with self.subTest(reason=reason, overrides=overrides):
                guidance_id = overrides.pop("guidance_id", guidance.id)
                with self.assertRaises(mailbox_models.MailboxError) as raised:
                    self.deliver(guidance_id, **overrides)
                self.assertEqual(reason, raised.exception.reason)

        self.assertEqual(
            (),
            mailbox_appenders.guidance_deliveries(
                self.plan_dir, guidance.id, state_dir=self.state_dir
            ),
        )

    def test_only_one_terminal_attempt_per_guidance_and_turn(self) -> None:
        guidance = self.guidance()
        turn = {"run_id": "run-A", "session_id": "1", "turn_index": 1}
        first = self.deliver(
            guidance.id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_NO_ACTIVE_TURN,
            **turn,
        )
        second = self.deliver(
            guidance.id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_TRANSPORT_UNAVAILABLE,
            **turn,
        )
        # Deferred attempts may repeat within a turn: none of them is terminal
        # for the guidance.
        self.assertEqual(
            (first, second),
            mailbox.read_mailbox(
                self.plan_dir, state_dir=self.state_dir
            ).deferred_guidance_deliveries(guidance.id),
        )

        delivered = self.deliver(guidance.id, **turn)
        with self.assertRaises(mailbox_models.MailboxError) as raised:
            self.deliver(guidance.id, **turn)
        self.assertEqual(
            mailbox_constants.REJECT_GUIDANCE_ALREADY_DELIVERED, raised.exception.reason
        )

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(delivered, view.successful_guidance_delivery(guidance.id))
        self.assertEqual(3, len(view.guidance_deliveries(guidance.id)))

    def test_malformed_delivery_lines_are_rejected_without_rewriting(self) -> None:
        guidance = self.guidance()
        good = self.deliver(
            guidance.id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_HARNESS_UNSUPPORTED,
            run_id="run-A",
            session_id="1",
            turn_index=1,
        )
        base = {
            "channel": mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
            "created_at": self.timestamp(),
            "delivery_reason": mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
            "delivery_state": mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
            "kind": mailbox_vocabulary.KIND_GUIDANCE_DELIVERY,
            "to": guidance.id,
            "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
        }
        expected = []
        for reason, overrides in (
            (mailbox_constants.REJECT_UNEXPECTED_TEXT, {"text": "the guidance body"}),
            (mailbox_constants.REJECT_INVALID_TARGET, {"to": "not-a-uuid"}),
            (mailbox_constants.REJECT_GUIDANCE_NOT_FOUND, {"to": self.message_id()}),
            (
                mailbox_constants.REJECT_INVALID_DELIVERY_STATE,
                {"delivery_state": "sent"},
            ),
            (
                mailbox_constants.REJECT_INVALID_DELIVERY_CHANNEL,
                {"channel": mailbox_vocabulary.GUIDANCE_CHANNEL_READ},
            ),
            (
                mailbox_constants.REJECT_INVALID_DELIVERY_REASON,
                {"delivery_reason": "because"},
            ),
            (mailbox_constants.REJECT_INCOMPLETE_TURN_IDENTITY, {"session_id": "1"}),
            (mailbox_constants.REJECT_INVALID_HARNESS, {"harness": "x" * 65}),
        ):
            self.append_raw({**base, "id": self.message_id(), **overrides})
            expected.append(reason)
        # A second terminal attempt for the same turn identity, appended behind
        # the writer's back, is still refused at parse time.
        self.append_raw(
            {
                **base,
                "id": self.message_id(),
                "run_id": "run-B",
                "session_id": "2",
                "turn_index": 1,
            }
        )
        self.append_raw(
            {
                **base,
                "id": self.message_id(),
                "run_id": "run-B",
                "session_id": "2",
                "turn_index": 1,
            }
        )
        expected.append(mailbox_constants.REJECT_GUIDANCE_ALREADY_DELIVERED)

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(expected, [record.reason for record in view.rejected])
        self.assertEqual(
            [guidance.id, good.id],
            [message.id for message in view.messages[:2]],
        )
        self.assertEqual(3, len(view.messages))


if __name__ == "__main__":
    unittest.main()
