"""DerivedIdentity, Duplicates, ConcurrentClosure, StaleQuestions coverage."""

from __future__ import annotations

import unittest
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_vocabulary,
    overseer_mailbox,
)
from tests.asl_overseer_mailbox_support import (
    RUN,
    _MailboxCase,
    _stale_first_read,
)

ASL_TEST_PORTABLE = True


class DerivedIdentity(_MailboxCase):
    """The id is a function of the row, so a retry cannot become a message."""

    def test_identity_is_the_same_for_the_same_four_fields(self) -> None:
        first = overseer_mailbox.message_id(
            plan_dir=self.plan,
            incident_id=self.incident_id,
            operation="reply",
            target=self.question,
        )
        again = overseer_mailbox.message_id(
            plan_dir=self.plan,
            incident_id=self.incident_id,
            operation="reply",
            target=self.question,
        )
        self.assertEqual(first, again)

    def test_identity_moves_with_every_field_it_is_derived_from(self) -> None:
        base = dict(
            plan_dir=self.plan,
            incident_id=self.incident_id,
            operation="reply",
            target=self.question,
        )
        identity = overseer_mailbox.message_id(**base)
        for field, value in (
            ("plan_dir", self.new_plan("plan-other")),
            ("incident_id", self.incident_id + 1),
            ("operation", "guidance"),
            ("target", "q-other"),
        ):
            moved = dict(base)
            moved[field] = value
            self.assertNotEqual(
                identity,
                overseer_mailbox.message_id(**moved),
                msg=field,
            )

    def test_the_posted_message_carries_the_derived_id(self) -> None:
        posting = self.post_reply()
        self.assertEqual(
            posting.message_id,
            overseer_mailbox.message_id(
                plan_dir=self.plan,
                incident_id=self.incident_id,
                operation="reply",
                target=self.question,
            ),
        )
        landed = self.view().reply_for(self.question)
        assert landed is not None
        self.assertEqual(landed.id, posting.message_id)


class Duplicates(_MailboxCase):
    """One posting, however many times it is attempted."""

    def test_posting_the_same_reply_twice_appends_once(self) -> None:
        first = self.post_reply()
        second = self.post_reply()

        self.assertEqual(first.message_id, second.message_id)
        self.assertEqual(first.seq, second.seq)
        self.assertEqual(second.state, "accepted")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 1)

    def test_posting_the_same_guidance_twice_appends_once(self) -> None:
        first = self.post_guidance()
        second = self.post_guidance()

        self.assertEqual(first.message_id, second.message_id)
        self.assertEqual(second.state, "queued")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_GUIDANCE), 1)

    def test_different_guidance_text_is_a_different_posting(self) -> None:
        first = self.post_guidance()
        second = self.post_guidance(text="prefer the smaller change, please")

        self.assertNotEqual(first.message_id, second.message_id)
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_GUIDANCE), 2)

    def test_a_retry_that_only_learns_of_the_first_at_the_append(self) -> None:
        """The crash-and-retry case: the pre-read misses what the append hits."""
        stale = self.view()
        identity = overseer_mailbox.message_id(
            plan_dir=self.plan,
            incident_id=self.incident_id,
            operation="reply",
            target=self.question,
        )
        landed = mailbox_appenders.append_reply(
            self.plan,
            self.question,
            "carry on",
            state_dir=self.root,
            message_id=identity,
        )

        with _stale_first_read(self, stale):
            posting = self.post_reply()

        self.assertEqual(posting.state, "accepted")
        self.assertEqual(posting.message_id, identity)
        self.assertEqual(posting.seq, landed.seq)
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 1)


class ConcurrentClosure(_MailboxCase):
    """Another supervisor, or the user, getting there first."""

    def test_a_foreign_reply_seen_before_the_append_refuses(self) -> None:
        mailbox_appenders.append_reply(
            self.plan,
            self.question,
            "somebody else answered",
            state_dir=self.root,
        )

        posting = self.post_reply()

        self.assertEqual(posting.state, "rejected")
        self.assertEqual(posting.refusal, "question_closed")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 1)

    def test_a_foreign_resolution_seen_before_the_append_refuses(self) -> None:
        mailbox_appenders.append_resolution(
            self.plan,
            self.question,
            "continue",
            expected_run_id=RUN,
            note="the user resolved it",
            state_dir=self.root,
        )

        posting = self.post_reply()

        self.assertEqual(posting.refusal, "question_closed")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 0)

    def test_losing_the_race_after_the_read_refuses_the_same_way(self) -> None:
        """A closure that lands between the read and the append reads alike."""
        stale = self.view()
        mailbox_appenders.append_reply(
            self.plan,
            self.question,
            "somebody else answered",
            state_dir=self.root,
        )

        with _stale_first_read(self, stale):
            posting = self.post_reply()

        self.assertEqual(posting.state, "rejected")
        self.assertEqual(posting.refusal, "question_closed")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 1)


class StaleQuestions(_MailboxCase):
    """Three ways a question is not answerable, kept apart."""

    def test_an_unknown_question_is_named_as_unknown(self) -> None:
        posting = self.post_reply(question_id="q-nobody-asked")

        self.assertEqual(posting.refusal, "question_unknown")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 0)

    def test_a_question_from_a_finished_run_is_stale_not_unknown(self) -> None:
        older = mailbox_appenders.append_question(
            self.plan,
            "asked by a run that has since ended",
            state_dir=self.root,
            run_id="run-0",
        ).id

        posting = self.post_reply(question_id=older)

        self.assertEqual(posting.refusal, "question_stale")
        self.assertEqual(self.kinds().count(mailbox_vocabulary.KIND_REPLY), 0)

    def test_a_mailbox_that_cannot_be_read_is_not_a_stale_question(self) -> None:
        with mock.patch.object(
            mailbox,
            "read_mailbox",
            side_effect=OSError("disk gone"),
        ):
            posting = self.post_reply()

        self.assertEqual(posting.refusal, "mailbox_unreadable")


if __name__ == "__main__":
    unittest.main()
