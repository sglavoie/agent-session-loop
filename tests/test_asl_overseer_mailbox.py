"""Focused tests for the overseer's reply and guidance adapters.

The mailbox already guarantees that an append is atomic and that a question
gets one closure. What this module adds is everything a *fleet* supervisor
needs on top of that, and the tests are grouped by the way each of those
additions can go wrong.

*Duplicates.* The same posting attempted twice must produce one message, and
must do so whether the second attempt sees the first in the mailbox or only
discovers it by being refused ``duplicate-id`` at the append. Both are the same
retry, one caught early and one caught late.

*Delayed outcomes.* A process that appended and died leaves a row nobody
settled. Reconciliation has to answer "did this happen?" from the row alone,
and has to say ``uncertain`` rather than ``failed`` when the mailbox cannot be
read -- because a mailbox nobody could open is not evidence of anything.

*Stale and concurrently resolved questions.* A question that is no longer open,
one that never existed, and one another supervisor or the user answered first
are three different refusals, and none of them may append anything.

*Revoked authority.* A ruling withdrawn, replaced, held, or made about another
plan refuses -- including when the withdrawal happens between the read and the
append, which is the only window this module can be surprised in.

*The five states.* ``queued``, ``accepted``, ``delivered``, ``consumed`` and
``rejected`` are tracked separately, and the case that matters most is guidance
whose delivery was *deferred*: it is still queued, and reporting it as anything
else would tell an operator the worker has seen something it has not.

*Independent plans.* One plan whose receipt has not arrived, and one whose
mailbox cannot be read at all, must not stop the rest of the fleet from being
polled -- and nothing here may sleep.
"""

from __future__ import annotations

import ast
import contextlib
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    intervention,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    overseer_decisions,
    overseer_dispatch,
    overseer_mailbox,
    overseer_policy_client,
    overseer_store,
)
from tests.support import assert_no_runner_import

PROJECT = "/home/dev/scripts"
RUN = "run-1"
FINGERPRINT = "fp-first"
MOMENT = "2026-01-01T00:00:00+00:00"


def _ruling(
    *,
    plan_dir: str,
    incident_id: int,
    operation: str = "reply",
    run_id: str = RUN,
    project: str = PROJECT,
    decision_id: str = "d-1",
    stance: str = "allow",
    at: str = MOMENT,
    authorized: bool = True,
    refusal: str | None = None,
    with_decision: bool = True,
    settlements: tuple[overseer_decisions.Settlement, ...] = (),
) -> overseer_decisions.Ruling:
    """A ruling that authorizes one operation on one plan, run and incident."""
    scope = overseer_decisions.Scope(
        project=project,
        plan_dir=plan_dir,
        run_id=run_id,
        incident=str(incident_id),
    )
    decision = overseer_decisions.Decision(
        decision_id=decision_id,
        statement_id="s-1",
        topic="operations",
        stance=stance,  # type: ignore[arg-type]
        scope=scope,
        operations=(operation,),  # type: ignore[arg-type]
        at=at,
        moment=datetime.fromisoformat(at),
        sources=("s-1",),
        text="answer the question",
    )
    return overseer_decisions.Ruling(
        operation=operation,
        scope=scope,
        authorized=authorized,
        refusal=refusal,  # type: ignore[arg-type]
        decision=decision if with_decision else None,
        settlements=settlements,
    )


class _MailboxCase(unittest.TestCase):
    """A real state root, a real ledger, and one plan with one open question."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-mailbox-"))
        self.addCleanup(self._close)
        self.ledger = overseer_store.open_ledger(state_dir=self.root)
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.plan = self.new_plan("plan-a")
        self.incident_id = self.ledger.open_incident(
            grant=None,
            plan_dir=self.plan,
            run_id=RUN,
            kind="question_pending",
            summary="waiting on a reply",
        ).incident_id
        self.question = mailbox_appenders.append_question(
            self.plan,
            "may I proceed?",
            state_dir=self.root,
            run_id=RUN,
        ).id

    def _close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.ledger.close()

    def new_plan(self, name: str) -> str:
        """A real plan directory, canonical, so every read agrees on its name."""
        path = self.root / "plans" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def grant(self, plan_dir: str) -> overseer_policy_client.Grant:
        return overseer_policy_client.Grant(
            plan_dir=plan_dir,
            access=overseer_policy_client.ACCESS_ACTION,
            request_digest="rd-1",
            config_fingerprint=FINGERPRINT,
        )

    def break_mailbox(self, plan_dir: str) -> None:
        """Leave a directory where the mailbox log belongs: unreadable, durably."""
        path = mailbox_cursors.mailbox_path_for_plan(plan_dir, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        path.mkdir()

    def view(self, plan_dir: str | None = None) -> mailbox_snapshot.MailboxView:
        return mailbox.read_mailbox(plan_dir or self.plan, state_dir=self.root)

    def kinds(self, plan_dir: str | None = None) -> list[str]:
        return [message.kind for message in self.view(plan_dir).messages]

    def ruling(self, **kwargs: Any) -> overseer_decisions.Ruling:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
        }
        arguments.update(kwargs)
        return _ruling(**arguments)

    def post_reply(self, **kwargs: Any) -> overseer_mailbox.Posting:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "question_id": self.question,
            "text": "carry on",
            "ruling": self.ruling(),
            "run_id": RUN,
            "state_dir": self.root,
        }
        arguments.update(kwargs)
        return overseer_mailbox.post_reply(**arguments)

    def post_guidance(self, **kwargs: Any) -> overseer_mailbox.Posting:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "text": "prefer the smaller change",
            "ruling": self.ruling(operation="guidance"),
            "run_id": RUN,
            "state_dir": self.root,
        }
        arguments.update(kwargs)
        return overseer_mailbox.post_guidance(**arguments)

    def action(
        self,
        payload: overseer_dispatch.Payload,
        *,
        plan_dir: str | None = None,
    ) -> overseer_dispatch.Action:
        return overseer_dispatch.Action(
            plan_dir=plan_dir or self.plan,
            run_id=RUN,
            incident_id=self.incident_id,
            payload=payload,
            token="token-1",
            scope="sha256:scope",
            repositories=(),
        )

    def facts(self, *, unanswered: tuple[str, ...] | None = None) -> Any:
        ids = (self.question,) if unanswered is None else unanswered
        return intervention.RunFacts(
            plan_dir=Path(self.plan),
            run_id=RUN,
            lock_active=True,
            run_active=True,
            paused=True,
            turn_active=False,
            context_kind="waiting",
            question_id=ids[0] if ids else None,
            unanswered_ids=ids,
        )


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


class FleetPolling(_MailboxCase):
    """One pass, many plans, and no plan waiting on another."""

    def setUp(self) -> None:
        super().setUp()
        self.plans = [self.plan, self.new_plan("plan-b"), self.new_plan("plan-c")]
        self.stamps = iter(
            f"2026-01-01T00:00:{index:02d}+00:00" for index in range(1, 60)
        )
        self.ledger.close()
        self.ledger = overseer_store.open_ledger(
            state_dir=self.root,
            now=lambda: next(self.stamps),
        )

    def enrol(self, plan_dir: str, *, operation: str, target: str) -> None:
        self.ledger.attempt_action(
            grant=self.grant(plan_dir),
            plan_dir=plan_dir,
            operation=operation,
            target=target,
            incident_id=self.incident_id,
            progress_token="pt-1",
        )

    def test_a_plan_with_no_receipt_yet_is_outstanding_not_waited_on(self) -> None:
        posting = self.post_reply()
        self.enrol(self.plan, operation="reply", target=self.question)

        poll = overseer_mailbox.poll_plan(
            plan_dir=self.plan,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertTrue(poll.read)
        self.assertEqual(
            [item.message_id for item in poll.outstanding],
            [posting.message_id],
        )
        self.assertEqual(poll.arrived, ())

    def test_a_receipt_that_has_arrived_settles_the_posting(self) -> None:
        posting = self.post_reply()
        self.enrol(self.plan, operation="reply", target=self.question)
        mailbox_appenders.append_receipt(
            self.plan, self.question, posting.message_id, state_dir=self.root
        )

        poll = overseer_mailbox.poll_plan(
            plan_dir=self.plan,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(poll.outstanding, ())
        self.assertEqual([item.state for item in poll.arrived], ["consumed"])

    def test_a_row_whose_append_never_landed_is_not_projected(self) -> None:
        self.enrol(self.plan, operation="reply", target=self.question)

        poll = overseer_mailbox.poll_plan(
            plan_dir=self.plan,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(poll.postings, ())

    def test_an_unreadable_plan_does_not_stop_the_others(self) -> None:
        broken = self.plans[1]
        self.break_mailbox(broken)

        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertTrue(fleet.complete)
        self.assertEqual(
            {poll.plan_dir for poll in fleet.polls},
            set(self.plans),
        )
        unreadable = [poll for poll in fleet.polls if not poll.read]
        self.assertEqual([poll.plan_dir for poll in unreadable], [broken])
        self.assertTrue(unreadable[0].detail)

    def test_the_rotation_is_least_recently_polled_first(self) -> None:
        first = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )
        self.assertEqual([poll.plan_dir for poll in first.polls], sorted(self.plans))

        overseer_mailbox.poll_fleet(
            plan_dirs=[self.plans[0]],
            ledger=self.ledger,
            state_dir=self.root,
        )
        again = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(again.polls[-1].plan_dir, self.plans[0])

    def test_the_rotation_survives_the_process_that_recorded_it(self) -> None:
        overseer_mailbox.poll_fleet(
            plan_dirs=[self.plans[0]],
            ledger=self.ledger,
            state_dir=self.root,
        )
        self.ledger.close()
        self.ledger = overseer_store.open_ledger(
            state_dir=self.root,
            now=lambda: next(self.stamps),
        )

        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(fleet.polls[-1].plan_dir, self.plans[0])

    def test_an_unreadable_plan_still_rotates_out_of_the_way(self) -> None:
        broken = self.plans[0]
        self.break_mailbox(broken)

        overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )
        overseer_mailbox.poll_fleet(
            plan_dirs=[self.plans[1], self.plans[2]],
            ledger=self.ledger,
            state_dir=self.root,
        )
        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
        )

        self.assertEqual(fleet.polls[0].plan_dir, broken)

    def test_a_deadline_defers_the_rest_and_reports_the_truncation(self) -> None:
        moments = iter([0.0, 100.0, 100.0, 100.0])

        fleet = overseer_mailbox.poll_fleet(
            plan_dirs=self.plans,
            ledger=self.ledger,
            state_dir=self.root,
            deadline=10.0,
            monotonic=lambda: next(moments),
        )

        self.assertEqual(len(fleet.polls), 1)
        self.assertEqual(len(fleet.deferred), 2)
        self.assertFalse(fleet.complete)

    def test_polling_never_sleeps(self) -> None:
        self.post_reply()
        self.enrol(self.plan, operation="reply", target=self.question)

        with mock.patch.object(
            time,
            "sleep",
            side_effect=AssertionError("the overseer slept on a receipt"),
        ):
            fleet = overseer_mailbox.poll_fleet(
                plan_dirs=self.plans,
                ledger=self.ledger,
                state_dir=self.root,
            )

        self.assertTrue(fleet.complete)
        self.assertEqual(len(fleet.outstanding), 1)


class ModuleShape(unittest.TestCase):
    """What the module may not reach for, asserted from its source."""

    def test_the_module_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_mailbox)

    def test_the_module_calls_nothing_that_blocks(self) -> None:
        """A fleet poll that could wait would be a fleet poll that stops."""
        source = Path(overseer_mailbox.__file__ or "").read_text(encoding="utf-8")
        forbidden = {
            "sleep",
            "finish_wait",
            "consume_wait_closure",
            "append_wait_start",
            "append_wait_question",
            "wait_for_closure",
        }
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
        }
        self.assertEqual(called & forbidden, set())

    def test_the_module_never_writes_a_supervisor_lease(self) -> None:
        """The overseer is not the plan's supervisor and may not displace one."""
        source = Path(overseer_mailbox.__file__ or "").read_text(encoding="utf-8")
        for name in ("write_supervisor_lease", "append_question_reply", "write_lease"):
            self.assertNotIn(f"{name}(", source, msg=name)

    def test_the_refusal_and_state_vocabularies_are_closed(self) -> None:
        refusals = overseer_mailbox.REFUSALS
        states = overseer_mailbox.STATES
        self.assertEqual(len(set(refusals)), len(refusals))
        self.assertEqual(len(set(states)), len(states))
        self.assertEqual(
            set(overseer_mailbox.MAIL_OPERATIONS) - set(overseer_dispatch.OPERATIONS),
            set(),
        )


def _state(posting: overseer_mailbox.Posting | None) -> str:
    assert posting is not None
    return posting.state


def _stale_first_read(
    case: _MailboxCase,
    stale: mailbox_snapshot.MailboxView,
) -> Any:
    """Serve one stale snapshot, then the mailbox as it actually stands.

    This is the race the module cannot prevent and has to survive: the world
    moved between the read that checked and the append that acted.
    """
    real_read = mailbox.read_mailbox
    served = {"count": 0}

    def reader(*args: Any, **kwargs: Any) -> mailbox_snapshot.MailboxView:
        served["count"] += 1
        if served["count"] == 1:
            return stale
        return real_read(*args, **kwargs)

    return mock.patch.object(mailbox, "read_mailbox", side_effect=reader)


if __name__ == "__main__":
    unittest.main()
