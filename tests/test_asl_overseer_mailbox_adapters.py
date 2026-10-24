"""Adapters, DelayedOutcomes coverage."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    overseer_decisions,
    overseer_dispatch,
    overseer_mailbox,
)
from tests.asl_overseer_mailbox_support import (
    RUN,
    _MailboxCase,
)

ASL_TEST_PORTABLE = True


class Adapters(_MailboxCase):
    """The two adapters, behind the dispatcher's interface."""

    def setUp(self) -> None:
        super().setUp()
        self.rulings: dict[str, overseer_decisions.Ruling | None] = {
            "reply": self.ruling(),
            "guidance": self.ruling(operation="guidance"),
        }
        self.registry = overseer_mailbox.adapters(
            authority=lambda action: self.rulings[action.operation],
            state_dir=self.root,
        )

    def reply_adapter(self) -> overseer_mailbox.ReplyAdapter:
        return overseer_mailbox.ReplyAdapter(
            authority=lambda action: self.rulings[action.operation],
            state_dir=self.root,
            inspect=lambda _plan_dir, **_kwargs: self.facts(),
        )

    def test_the_registry_holds_exactly_the_two_mailbox_operations(self) -> None:
        self.assertTrue(self.registry.supports("reply"))
        self.assertTrue(self.registry.supports("guidance"))
        for operation in ("verify", "commit", "resume"):
            self.assertFalse(self.registry.supports(operation), msg=operation)

    def test_preparation_declines_a_question_that_is_no_longer_open(self) -> None:
        adapter = overseer_mailbox.ReplyAdapter(
            authority=lambda action: self.rulings[action.operation],
            state_dir=self.root,
            inspect=lambda _plan_dir, **_kwargs: self.facts(unanswered=()),
        )
        action = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )

        preparation = adapter.prepare(action)

        self.assertFalse(preparation.ready)
        self.assertIn("question_unknown", preparation.detail)

    def test_preparation_declines_without_authority(self) -> None:
        self.rulings["reply"] = None
        action = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )

        preparation = self.reply_adapter().prepare(action)

        self.assertFalse(preparation.ready)
        self.assertIn("authority_absent", preparation.detail)

    def test_executing_a_reply_appends_it_and_locates_the_message(self) -> None:
        action = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )

        receipt = self.reply_adapter().execute(action)

        self.assertEqual(receipt.outcome, "executed")
        landed = self.view().reply_for(self.question)
        assert landed is not None
        self.assertEqual(receipt.locator, landed.id)

    def test_executing_a_reply_twice_is_one_message(self) -> None:
        adapter = self.reply_adapter()
        action = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )

        first = adapter.execute(action)
        second = adapter.execute(action)

        self.assertEqual(first.locator, second.locator)
        self.assertEqual(second.outcome, "executed")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 1)

    def test_a_refused_posting_is_a_failure_not_a_dispatch_refusal(self) -> None:
        mailbox_appenders.append_resolution(
            self.plan,
            self.question,
            "continue",
            expected_run_id=RUN,
            note="the user resolved it",
            state_dir=self.root,
        )
        action = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )

        receipt = self.reply_adapter().execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("question_closed", receipt.detail)

    def test_a_payload_of_the_wrong_type_is_refused_by_both_adapters(self) -> None:
        guidance = overseer_mailbox.GuidanceAdapter(
            authority=lambda action: self.rulings["guidance"],
            state_dir=self.root,
        )
        wrong = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )

        self.assertFalse(guidance.prepare(wrong).ready)
        self.assertEqual(guidance.execute(wrong).outcome, "failed")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_GUIDANCE), 0)

    def test_executing_guidance_queues_it_and_never_claims_delivery(self) -> None:
        adapter = overseer_mailbox.GuidanceAdapter(
            authority=lambda action: self.rulings["guidance"],
            state_dir=self.root,
        )
        action = self.action(overseer_dispatch.Guidance(text="prefer the smaller one"))

        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "executed")
        self.assertIn("queued", receipt.detail)
        self.assertNotIn("delivered", receipt.detail)

    def test_an_unwritable_and_unreadable_mailbox_is_uncertain(self) -> None:
        """Neither landed nor did not: the row must stay in doubt."""
        reads = {"count": 0}
        real_read = mailbox.read_mailbox

        def flaky(*args: Any, **kwargs: Any) -> mailbox_snapshot.MailboxView:
            reads["count"] += 1
            if reads["count"] == 1:
                return real_read(*args, **kwargs)
            raise OSError("disk gone")

        action = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )
        with (
            mock.patch.object(mailbox, "read_mailbox", side_effect=flaky),
            mock.patch.object(
                mailbox_appenders,
                "append_reply",
                side_effect=mailbox_models.MailboxError(
                    mailbox_constants.ERROR_MAILBOX_WRITE, "short write"
                ),
            ),
        ):
            receipt = self.reply_adapter().execute(action)

        self.assertEqual(receipt.outcome, "uncertain")

    def test_a_full_mailbox_is_a_failure_the_adapter_can_name(self) -> None:
        action = self.action(
            overseer_dispatch.Reply(question_id=self.question, text="carry on")
        )
        with mock.patch.object(
            mailbox_appenders,
            "append_reply",
            side_effect=mailbox_models.MailboxError(
                mailbox_constants.ERROR_MAILBOX_FULL, "no room"
            ),
        ):
            receipt = self.reply_adapter().execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("mailbox_full", receipt.detail)


class DelayedOutcomes(_MailboxCase):
    """Reconciling what a process that is gone may or may not have done."""

    def setUp(self) -> None:
        super().setUp()
        self.adapter = overseer_mailbox.ReplyAdapter(
            authority=lambda _action: self.ruling(),
            state_dir=self.root,
        )
        self.record = self.ledger.attempt_action(
            grant=self.grant(self.plan),
            plan_dir=self.plan,
            operation="reply",
            target=self.question,
            incident_id=self.incident_id,
            progress_token="pt-1",
        )

    def test_reconciling_finds_a_reply_the_dead_process_appended(self) -> None:
        identity = overseer_mailbox.message_id(
            plan_dir=self.plan,
            incident_id=self.incident_id,
            operation="reply",
            target=self.question,
        )
        mailbox_appenders.append_reply(
            self.plan,
            self.question,
            "carry on",
            state_dir=self.root,
            message_id=identity,
        )

        receipt = self.adapter.reconcile(self.record)

        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(receipt.locator, identity)

    def test_reconciling_an_append_that_never_landed_is_a_failure(self) -> None:
        receipt = self.adapter.reconcile(self.record)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("record_rejected", receipt.detail)

    def test_reconciling_names_a_foreign_closure_rather_than_claiming_it(
        self,
    ) -> None:
        mailbox_appenders.append_reply(
            self.plan,
            self.question,
            "somebody else answered",
            state_dir=self.root,
        )

        receipt = self.adapter.reconcile(self.record)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("question_closed", receipt.detail)

    def test_an_unreadable_mailbox_reconciles_to_uncertain(self) -> None:
        with mock.patch.object(
            mailbox,
            "read_mailbox",
            side_effect=OSError("disk gone"),
        ):
            receipt = self.adapter.reconcile(self.record)

        self.assertEqual(receipt.outcome, "uncertain")

    def test_a_row_for_another_operation_is_never_reconciled_here(self) -> None:
        guidance = overseer_mailbox.GuidanceAdapter(
            authority=lambda _action: self.ruling(operation="guidance"),
            state_dir=self.root,
        )

        receipt = guidance.reconcile(self.record)

        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("payload_mismatch", receipt.detail)


if __name__ == "__main__":
    unittest.main()
