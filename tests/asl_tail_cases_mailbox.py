from .asl_tail_support import *


class TailFollowerMailboxTests(TailFollowerTestCase):
    def test_mailbox_guidance_event_renders_metadata_without_body(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_GUIDANCE_OBSERVED,
                "message_id": "guidance-1",
                "seq": 4,
                "length": 37,
                "digest": "abc123",
                "session_id": "7",
                "turn_index": 2,
            },
        )

        self.assertEqual(
            "mailbox: guidance observed "
            "(id guidance-1, sequence 4, length 37, digest abc123, "
            "session 7, turn 2)",
            message,
        )

    def test_mailbox_notice_event_renders_metadata_without_body(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_NOTICE_PUBLISHED,
                "message_id": "notice-1",
                "seq": 8,
                "length": 23,
                "digest": "def456",
                "run_id": "run-1",
                "session_id": "7",
                "turn_index": 2,
            },
        )

        self.assertEqual(
            "mailbox: notice published "
            "(id notice-1, sequence 8, length 23, digest def456, "
            "run run-1, session 7, turn 2)",
            message,
        )
        self.assertNotIn("notice body", message)

    def test_mailbox_wait_started_event_renders_rolling_ceiling(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "pending_outbox": 2,
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                "unsupervised_ceiling_seconds": 1800.0,
                "last_lease_activity_at": "2026-07-24T12:01:00+00:00",
            },
        )

        self.assertEqual(
            "mailbox: reply wait started "
            "(question question-1, session 9, turn 3, "
            "pending outbox 2, "
            "base deadline 2026-07-24T12:05:00+00:00, "
            "unsupervised deadline 2026-07-24T12:30:00+00:00, "
            "unsupervised ceiling 1800.0s, "
            "last lease activity 2026-07-24T12:01:00+00:00)",
            message,
        )
        self.assertNotIn("hard deadline", message)

    def test_mailbox_wait_started_event_renders_as_nonterminal_wait(self) -> None:
        # Historical records carry no rolling ceiling; they keep their original
        # `hard deadline` rendering.
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "pending_outbox": 2,
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "hard_deadline": "2026-07-24T12:15:00+00:00",
                "last_lease_activity_at": "2026-07-24T12:01:00+00:00",
            },
        )

        self.assertEqual(
            "mailbox: reply wait started "
            "(question question-1, session 9, turn 3, "
            "pending outbox 2, "
            "base deadline 2026-07-24T12:05:00+00:00, "
            "hard deadline 2026-07-24T12:15:00+00:00, "
            "last lease activity 2026-07-24T12:01:00+00:00)",
            message,
        )
        self.assertNotIn("paused", message)

    def test_mailbox_wait_cleared_event_renders_reason_and_count(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "supervisor-lost",
                "pending_outbox": 2,
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason supervisor-lost, pending outbox 2)",
            message,
        )
        self.assertNotIn("paused", message)

    def test_mailbox_wait_cleared_event_renders_resolution_metadata(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "resolution",
                "pending_outbox": 0,
                "resolution_message_id": "resolution-1",
                "resolution_seq": 12,
                "resolution_action": "continue",
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, reason resolution, "
            "pending outbox 0, resolution resolution-1, "
            "resolution sequence 12, resolution action continue)",
            message,
        )
        self.assertNotIn("question body", message)
        self.assertNotIn("resolution note", message)

    def test_mailbox_wait_cleared_event_renders_lease_evidence(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "supervisor-lost",
                "pending_outbox": 2,
                "lease_supervisor_id": "supervisor-1",
                "lease_updated_at": "2026-07-24T12:01:00+00:00",
                "lease_reason": "expired",
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason supervisor-lost, pending outbox 2, "
            "lease supervisor supervisor-1, "
            "lease updated 2026-07-24T12:01:00+00:00, "
            "lease reason expired)",
            message,
        )

    def test_mailbox_wait_cleared_event_renders_suspend_evidence(self) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "supervisor-lost",
                "pending_outbox": 2,
                "lease_supervisor_id": "supervisor-1",
                "lease_updated_at": "2026-07-24T12:01:00+00:00",
                "lease_reason": "expired",
                "suspend_skew_seconds": 3600.0,
                "suspend_lease_grace_seconds": 90.0,
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason supervisor-lost, pending outbox 2, "
            "lease supervisor supervisor-1, "
            "lease updated 2026-07-24T12:01:00+00:00, "
            "lease reason expired, "
            "suspend skew 3600.0s, "
            "suspend lease grace 90.0s)",
            message,
        )

    def test_mailbox_wait_cleared_event_renders_partial_lease_evidence(
        self,
    ) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-1",
                "session_id": "9",
                "turn_index": 3,
                "reason": "timeout",
                "pending_outbox": 1,
                "lease_supervisor_id": "supervisor-1",
                "lease_updated_at": "2026-07-24T12:14:00+00:00",
            },
        )

        self.assertEqual(
            "mailbox: reply wait cleared "
            "(question question-1, session 9, turn 3, "
            "reason timeout, pending outbox 1, "
            "lease supervisor supervisor-1, "
            "lease updated 2026-07-24T12:14:00+00:00)",
            message,
        )
        self.assertNotIn("lease reason", message)

    def test_mailbox_wait_preparation_failed_renders_reason_and_identity(
        self,
    ) -> None:
        message = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_WAIT_PREPARATION_FAILED,
                "reason": "expired",
                "session_id": "9",
                "turn_index": 3,
                "run_id": "run-abc",
            },
        )

        self.assertEqual(
            "mailbox: reply wait preparation failed "
            "(reason expired, session 9, turn 3, run run-abc)",
            message,
        )

    def test_mailbox_guidance_delivery_renders_transport_evidence_only(
        self,
    ) -> None:
        delivered = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_GUIDANCE_DELIVERED,
                "guidance_id": "guidance-1",
                "delivery_id": "delivery-1",
                "seq": 4,
                "delivery_state": "delivered-live",
                "delivery_reason": "delivered",
                "channel": "live",
                "harness": "codex",
                "run_id": "run-abc",
                "session_id": "9",
                "turn_index": 3,
            },
        )

        self.assertEqual(
            "mailbox: guidance delivered "
            "(guidance guidance-1, state delivered-live, reason delivered, "
            "channel live, harness codex, run run-abc, session 9, turn 3)",
            delivered,
        )

        deferred = tail._event_message(
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED,
                "guidance_id": "guidance-1",
                "delivery_state": "deferred",
                "delivery_reason": "harness-unsupported",
                "channel": "live",
            },
        )

        self.assertEqual(
            "mailbox: guidance delivery deferred "
            "(guidance guidance-1, state deferred, "
            "reason harness-unsupported, channel live)",
            deferred,
        )

    def test_every_mailbox_action_has_a_specific_tail_renderer(self) -> None:
        payload = {
            "message_id": "message-1",
            "seq": 1,
            "length": 12,
            "digest": "abc123",
            "session_id": "1",
            "turn_index": 1,
            "question_id": "question-1",
            "pending_outbox": 0,
            "base_deadline": "base",
            "hard_deadline": "hard",
            "reason": "expired",
            "run_id": "run-abc",
            "resolution_message_id": "resolution-1",
            "resolution_seq": 2,
            "resolution_action": "continue",
        }

        for action in events.MAILBOX_ACTIONS:
            with self.subTest(action=action):
                rendered = tail._mailbox_message({"action": action, **payload})
                self.assertNotEqual(f"mailbox: {action}", rendered)
