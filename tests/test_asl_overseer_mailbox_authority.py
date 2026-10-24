"""CurrentAuthority, RevokedAuthority, States coverage."""

from __future__ import annotations

import unittest
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_vocabulary,
    overseer_decisions,
    overseer_mailbox,
)
from tests.asl_overseer_mailbox_support import (
    RUN,
    _MailboxCase,
    _ruling,
    _state,
)

ASL_TEST_PORTABLE = True


class CurrentAuthority(_MailboxCase):
    """No append without a ruling that is about this, and still in force."""

    def assert_refused(
        self,
        posting: overseer_mailbox.Posting,
        refusal: str,
    ) -> None:
        self.assertEqual(posting.state, "rejected")
        self.assertEqual(posting.refusal, refusal)
        self.assertIn(posting.refusal, overseer_mailbox.REFUSALS)
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 0)
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_GUIDANCE), 0)

    def test_no_ruling_refuses_before_the_mailbox_is_touched(self) -> None:
        with mock.patch.object(
            mailbox,
            "read_mailbox",
            side_effect=AssertionError("read before authority"),
        ):
            posting = self.post_reply(ruling=None)

        self.assert_refused(posting, "authority_absent")

    def test_a_ruling_about_another_operation_is_silent(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(operation="resume")),
            "authority_silent",
        )

    def test_an_unauthorized_ruling_refuses(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(authorized=False)),
            "authority_refused",
        )

    def test_a_ruling_carrying_a_refusal_refuses(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(refusal="unauthorized")),
            "authority_refused",
        )

    def test_a_hold_refuses_even_when_the_ruling_says_authorized(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(stance="hold")),
            "authority_held",
        )

    def test_an_unsettled_human_topic_refuses(self) -> None:
        unsettled = overseer_decisions.Settlement(
            topic="acceptance",
            scope=self.ruling().scope,
            decision=None,
        )
        self.assert_refused(
            self.post_reply(ruling=self.ruling(settlements=(unsettled,))),
            "authority_held",
        )

    def test_a_ruling_resting_on_no_decision_refuses(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(with_decision=False)),
            "authority_absent",
        )

    def test_a_ruling_about_another_plan_refuses(self) -> None:
        elsewhere = _ruling(
            plan_dir=self.new_plan("plan-elsewhere"),
            incident_id=self.incident_id,
        )
        self.assert_refused(self.post_reply(ruling=elsewhere), "authority_unscoped")

    def test_a_ruling_about_another_incident_refuses(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(incident_id=self.incident_id + 1)),
            "authority_unscoped",
        )

    def test_a_ruling_about_another_run_refuses(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(run_id="run-9")),
            "authority_unscoped",
        )

    def test_a_ruling_naming_no_project_refuses(self) -> None:
        self.assert_refused(
            self.post_reply(ruling=self.ruling(project="")),
            "authority_unscoped",
        )


class RevokedAuthority(_MailboxCase):
    """The window between the read and the append is the one that matters."""

    def test_authority_withdrawn_during_the_read_stops_the_reply(self) -> None:
        posting = self.post_reply(authority=lambda: None)

        self.assertEqual(posting.refusal, "authority_absent")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 0)

    def test_a_replaced_decision_is_superseded_not_honored(self) -> None:
        posting = self.post_reply(
            authority=lambda: self.ruling(decision_id="d-2"),
        )

        self.assertEqual(posting.refusal, "authority_superseded")
        self.assertIn("d-1", posting.detail)
        self.assertIn("d-2", posting.detail)
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 0)

    def test_a_decision_that_kept_its_id_and_changed_its_stance(self) -> None:
        posting = self.post_reply(
            authority=lambda: self.ruling(stance="hold"),
        )

        self.assertEqual(posting.refusal, "authority_held")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 0)

    def test_a_decision_re_read_at_a_different_moment_is_superseded(self) -> None:
        posting = self.post_reply(
            authority=lambda: self.ruling(at="2026-02-02T00:00:00+00:00"),
        )

        self.assertEqual(posting.refusal, "authority_superseded")

    def test_guidance_is_revoked_in_the_same_window(self) -> None:
        posting = self.post_guidance(authority=lambda: None)

        self.assertEqual(posting.refusal, "authority_absent")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_GUIDANCE), 0)

    def test_unchanged_authority_posts(self) -> None:
        ruling = self.ruling()
        posting = self.post_reply(ruling=ruling, authority=lambda: ruling)

        self.assertEqual(posting.state, "accepted")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 1)


class States(_MailboxCase):
    """Five states, and the mailbox's own word kept next to each."""

    def inspect(
        self,
        *,
        operation: str,
        target: str,
    ) -> overseer_mailbox.Posting | None:
        return overseer_mailbox.inspect_posting(
            self.view(),
            plan_dir=self.plan,
            incident_id=self.incident_id,
            operation=operation,
            target=target,
        )

    def test_nothing_posted_is_not_a_state(self) -> None:
        self.assertIsNone(self.inspect(operation="reply", target=self.question))

    def test_a_reply_is_accepted_until_the_worker_takes_it(self) -> None:
        posting = self.post_reply()
        self.assertEqual(posting.state, "accepted")
        self.assertEqual(posting.mailbox_state, "accepted")
        self.assertTrue(posting.outstanding)
        self.assertFalse(posting.arrived)

        mailbox_appenders.append_receipt(
            self.plan,
            self.question,
            posting.message_id,
            state_dir=self.root,
        )

        settled = self.inspect(operation="reply", target=self.question)
        assert settled is not None
        self.assertEqual(settled.state, "consumed")
        self.assertEqual(settled.mailbox_state, "consumed")
        self.assertFalse(settled.outstanding)
        self.assertTrue(settled.arrived)

    def test_deferred_guidance_is_queued_and_says_so(self) -> None:
        posting = self.post_guidance()
        self.assertEqual(posting.state, "queued")
        self.assertEqual(
            posting.mailbox_state, mailbox_vocabulary.GUIDANCE_STATE_QUEUED
        )

        mailbox_appenders.append_guidance_delivery(
            self.plan,
            posting.message_id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_NO_ACTIVE_TURN,
            channel=mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
            state_dir=self.root,
        )

        deferred = self.inspect(operation="guidance", target=posting.target)
        assert deferred is not None
        self.assertEqual(deferred.state, "queued")
        self.assertEqual(
            deferred.mailbox_state, mailbox_vocabulary.GUIDANCE_STATE_DEFERRED
        )
        self.assertTrue(deferred.outstanding)
        self.assertFalse(deferred.arrived)

    def test_delivered_guidance_is_delivered(self) -> None:
        posting = self.post_guidance()
        mailbox_appenders.append_guidance_delivery(
            self.plan,
            posting.message_id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
            channel=mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
            run_id=RUN,
            session_id="1",
            turn_index=2,
            harness="claude",
            state_dir=self.root,
        )

        delivered = self.inspect(operation="guidance", target=posting.target)
        assert delivered is not None
        self.assertEqual(delivered.state, "delivered")
        self.assertEqual(
            delivered.mailbox_state,
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
        )
        self.assertTrue(delivered.arrived)

    def test_every_state_is_reachable_and_the_vocabulary_is_closed(self) -> None:
        reply = self.post_reply()
        guidance = self.post_guidance()
        mailbox_appenders.append_receipt(
            self.plan, self.question, reply.message_id, state_dir=self.root
        )
        mailbox_appenders.append_guidance_delivery(
            self.plan,
            guidance.message_id,
            delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_BY_READ,
            delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
            channel=mailbox_vocabulary.GUIDANCE_CHANNEL_READ,
            state_dir=self.root,
        )
        rejected = self.post_reply(question_id="q-nobody-asked")
        queued = self.post_guidance(text="one more thought")

        seen = {
            rejected.state,
            queued.state,
            reply.state,
            _state(self.inspect(operation="reply", target=self.question)),
            _state(self.inspect(operation="guidance", target=guidance.target)),
        }
        self.assertEqual(seen, set(overseer_mailbox.STATES))


if __name__ == "__main__":
    unittest.main()
