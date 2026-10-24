"""Adversarial redirect cases: missing conversation history and expired submissions."""

from __future__ import annotations

from agent_session_loop import conversation_resume_plan

from .support_asl_redirect_adversarial_base import (
    PATIENCE,
    SESSION_ID,
    ContinuationTestBase,
    JournalTestCase,
    clock,
    contextlib,
    conversation_resume,
    conversations,
    events,
    mock,
    redirect_continuation,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
    target,
    threading,
    timedelta,
)


class MissingHistoryTests(ContinuationTestBase):
    """What happens to an accepted correction when the history it is about is gone.

    ``tests/test_asl_conversation_resume.py`` already pins the continuation
    decision itself for every selection state. These are the cases that only
    exist once a correction has been accepted: the operator wrote it *about* a
    conversation, so losing that conversation is losing the thing the correction
    refers to, and the three ways forward -- stop, authorized fresh start,
    harness with no continuity at all -- must each be explicit about whether the
    correction travels.
    """

    def plan(self, *, authorization=None, operations=("op-1",), **kwargs):
        return conversation_resume.plan_turn_continuation(
            self.plan_state,
            binding=self.binding(**kwargs),
            authorization=authorization or self.withheld(),
            interrupted_operations=operations,
        )

    def test_a_correction_is_never_delivered_into_a_different_conversation(
        self,
    ) -> None:
        # The conversation the correction was written about cannot be continued
        # and no fresh start was authorized. Delivering the correction anyway --
        # into a conversation that never saw the work being corrected -- is the
        # failure this pause exists to prevent.
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))

        continuation = self.plan()

        self.assertEqual(
            conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
            continuation.disposition,
        )
        self.assertTrue(continuation.paused)
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertIsNone(continuation.intent, "a refused turn carried an intent")
        self.assertEqual(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
            conversation_resume.RECOVERY_PAUSE_REASON,
        )

    def test_an_unreadable_ledger_stops_rather_than_losing_the_correction(self) -> None:
        self.confirmed("thread-7")
        with self.ledger_path.open("ab") as handle:
            handle.write(b"{not json\n")

        continuation = self.plan(authorization=self.withheld())

        self.assertTrue(continuation.paused)
        self.assertIsNone(continuation.intent)
        # The pause says what was refused and both ways forward, because the
        # operator's correction is still pending on the other side of it.
        message = conversation_resume.recovery_message(continuation)
        self.assertIn("--fresh-conversation", message)
        self.assertIn("outstanding feedback are kept", message)

    def test_an_authorized_fresh_start_still_carries_the_correction_forward(
        self,
    ) -> None:
        # Starting over discards the agent's accumulated context, which is the
        # cost the operator accepted. It must not also discard the correction:
        # the ids travel into the fresh intent so the new conversation is told
        # what it is picking up.
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))
        authorization = self.granted()

        continuation = self.plan(
            authorization=authorization, operations=("op-1", "op-2")
        )

        self.assertEqual(
            conversation_resume.CONTINUATION_FRESH, continuation.disposition
        )
        self.assertTrue(authorization.consumed)
        self.assertIsNotNone(continuation.intent)
        self.assertEqual(
            ("op-1", "op-2"), tuple(continuation.intent.interrupted_operations)
        )

    def test_a_resumed_conversation_is_told_which_operations_were_cut_short(
        self,
    ) -> None:
        self.confirmed("thread-7")

        continuation = self.plan(operations=("op-1",))

        self.assertTrue(continuation.resumes)
        self.assertEqual("thread-7", continuation.resume_conversation_id)
        self.assertEqual(("op-1",), tuple(continuation.intent.interrupted_operations))

    def test_a_harness_without_continuity_carries_no_correction_through_here(
        self,
    ) -> None:
        # A harness that cannot resume is not a refusal, but it also cannot be
        # told what was interrupted, so the intent is absent rather than empty:
        # the correction reaches that worker through the prompt the run builds,
        # and a caller that read an empty intent as "nothing pending" would be
        # reading the wrong surface.
        with mock.patch.object(
            conversation_resume_plan,
            "_harness_capability",
            return_value=conversations.HarnessCapability(
                harness="codex",
                state="unsupported",
                detail="this harness carries no conversation across turns",
            ),
        ):
            continuation = self.plan()

        self.assertEqual(
            conversation_resume.CONTINUATION_UNAVAILABLE, continuation.disposition
        )
        self.assertFalse(continuation.paused)
        self.assertFalse(continuation.captures)
        self.assertIsNone(continuation.intent)

    def test_the_ids_a_resumed_conversation_accepts_are_narrowed_not_raised(
        self,
    ) -> None:
        # The journal's id charset is wider than the ledger's and the ledger caps
        # the count, so a correction with an id the ledger would refuse must be
        # dropped from the resume hint rather than failing the turn -- the worker
        # still reads its words out of the journal.
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=("ok-1", "not ok: spaces and colons", "ok-2"),
            feedback_digests=("a" * 64, "b" * 64, "c" * 64),
            feedback_bytes=30,
        )

        self.assertEqual(("ok-1", "ok-2"), pending.resumable_operation_ids())
        # The full list stays on the record, so what was narrowed is visible.
        self.assertEqual(3, len(pending.to_payload()["operation_ids"]))

    def test_the_resume_hint_is_capped_at_the_ledgers_limit(self) -> None:
        count = conversations.MAX_INTERRUPTED_OPERATIONS + 5
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="codex:thread-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=tuple(f"op-{index}" for index in range(count)),
            feedback_digests=tuple("a" * 64 for _ in range(count)),
            feedback_bytes=count,
        )

        self.assertEqual(
            conversations.MAX_INTERRUPTED_OPERATIONS,
            len(pending.resumable_operation_ids()),
        )
        self.assertEqual(count, len(pending.operation_ids))


class ExpiredSubmissionTests(JournalTestCase):
    """Expiry is a written refusal, and nothing may accept past it."""

    def test_an_expired_request_cannot_be_accepted_by_any_later_caller(self) -> None:
        created = clock.utc_now()
        request = self.submit("op-expired", ttl_seconds=1, now=created)
        later = created + timedelta(seconds=5)

        expired = redirect_store_requests.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )

        self.assertEqual([request.operation_id], [r.operation_id for r in expired])
        self.assertEqual(
            redirect_store_vocab.STATE_EXPIRED,
            self.journal().request("op-expired").state,
        )
        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                "op-expired",
                target=target(),
                state_dir=self.state_dir,
                now=later,
            )
        # Expiry answers before settlement does, because "its deadline passed" is
        # the more concrete thing to tell an operator than "it was already
        # settled" -- and either way the acceptance is refused.
        self.assertEqual(
            redirect_store_vocab.ERROR_REQUEST_EXPIRED, caught.exception.reason
        )

    def test_an_acceptance_past_the_deadline_expires_the_request_durably(self) -> None:
        created = clock.utc_now()
        self.submit("op-late", ttl_seconds=1, now=created)
        later = created + timedelta(seconds=5)

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                "op-late",
                target=target(),
                state_dir=self.state_dir,
                now=later,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_REQUEST_EXPIRED, caught.exception.reason
        )
        # The refusal outlives the process that reached it.
        self.assertEqual(
            redirect_store_vocab.STATE_EXPIRED, self.journal().request("op-late").state
        )
        self.assertEqual(
            redirect_store_vocab.REASON_EXPIRED,
            self.journal().request("op-late").reason,
        )

    #: Enough rounds that both orders occur on a normal machine, few enough that
    #: the test stays well under a second.
    ROUNDS = 20

    def _expiry_race_round(self, turn_index: int, created, later) -> str:
        """Accept and expire one request at once, and name the answer it got."""
        operation_id = f"op-race-expiry-{turn_index}"
        self.submit(operation_id, turn_index=turn_index, ttl_seconds=1, now=created)
        start = threading.Barrier(2)

        def do_accept() -> None:
            start.wait(PATIENCE)
            with contextlib.suppress(redirect_store_models.RedirectStoreError):
                redirect_store_requests.accept_request(
                    self.plan_dir,
                    operation_id,
                    target=target(turn_index),
                    state_dir=self.state_dir,
                    now=created,
                )

        def do_expire() -> None:
            start.wait(PATIENCE)
            redirect_store_requests.expire_due_requests(
                self.plan_dir, state_dir=self.state_dir, now=later
            )

        threads = [
            threading.Thread(target=do_accept, daemon=True),
            threading.Thread(target=do_expire, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(PATIENCE)
            self.assertFalse(thread.is_alive(), "the journal lock deadlocked")

        # Exactly one terminal record, whichever answer won the lock.
        terminal = [
            record
            for record in self.raw_records()
            if record.get("id") == operation_id
            and record.get("kind")
            in (redirect_store_vocab.KIND_ACCEPTED, redirect_store_vocab.KIND_EXPIRED)
        ]
        self.assertEqual(1, len(terminal), terminal)
        return self.journal().request(operation_id).state

    def test_an_acceptance_racing_expiry_produces_one_answer_not_two(self) -> None:
        created = clock.utc_now()
        later = created + timedelta(seconds=5)

        states = [
            self._expiry_race_round(index + 1, created, later)
            for index in range(self.ROUNDS)
        ]

        self.assertEqual(self.ROUNDS, len(states))
        self.assertLessEqual(
            set(states),
            {redirect_store_vocab.STATE_ACCEPTED, redirect_store_vocab.STATE_EXPIRED},
            "a racing request reached a state neither caller wrote",
        )
