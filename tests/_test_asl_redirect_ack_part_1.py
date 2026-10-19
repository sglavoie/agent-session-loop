# mypy: ignore-errors
"""Focused test_asl_redirect_ack test group 1."""

# ruff: noqa: F403, F405
from ._test_asl_redirect_ack_support import *


class AttemptTokenTests(RedirectAckTestCase):
    """The identity a worker echoes back, and what it deliberately omits."""

    def test_the_token_is_the_turn_identity_without_the_harness(self) -> None:
        self.assertEqual(f"{RUN_ID}/1/2", redirect_ack.attempt_token(target(2)))

    def test_a_harness_switch_does_not_change_which_attempt_it_is(self) -> None:
        """Load-bearing, not incidental.

        The continuation may run on a re-selected harness, and a token that
        encoded the harness would refuse the receipt of the very worker the
        correction was delivered to.
        """
        self.assertEqual(
            redirect_ack.attempt_token(target(2, harness="claude")),
            redirect_ack.attempt_token(target(2, harness=None)),
        )

    def test_the_token_is_compared_whole_rather_than_parsed(self) -> None:
        attempt = target(2)
        token = redirect_ack.attempt_token(attempt)

        self.assertTrue(redirect_ack.token_identifies(token, attempt))
        for impostor in (
            token + "/",
            token.replace("/2", "/20"),
            f" {token}",
            token.upper(),
            redirect_ack.attempt_token(target(3)),
            redirect_ack.attempt_token(target(2, session_id="2")),
        ):
            with self.subTest(impostor=impostor):
                self.assertFalse(redirect_ack.token_identifies(impostor, attempt))


class CoalescingTests(RedirectAckTestCase):
    """What the prompt being finalized takes with it, and what it leaves."""

    def test_corrections_from_both_populations_merge_in_submission_order(self) -> None:
        """The operator's sequence, not the journal's bookkeeping order.

        The first was accepted by the gate while the turn was alive; the second
        arrived after that turn was already gone, so nobody accepted it. They
        are the same conversation and the prompt has to show it as one.
        """
        accepted = self.submit(FIRST, accept=True)
        late = self.submit(SECOND)

        carried = self.carry(accepted=(accepted.operation_id,))

        self.assertEqual(
            (accepted.operation_id, late.operation_id), carried.operation_ids()
        )
        self.assertEqual([FIRST, SECOND], [item.text for item in carried.corrections])
        self.assertEqual((), carried.refused)

    def test_a_correction_aimed_at_the_attempt_about_to_start_is_left_alone(
        self,
    ) -> None:
        """The other side of the boundary, and the reason it is a moment.

        Feedback naming the attempt this prompt is about to launch is an
        ordinary mid-turn redirect against a turn that is about to be live --
        session 11's path -- rather than a late edit to a prompt already
        written.
        """
        before = self.submit(FIRST)
        after = redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(2),
            feedback=SECOND,
            state_dir=self.state_dir,
        )

        carried = self.carry()

        self.assertEqual((before.operation_id,), carried.operation_ids())
        view = self.read()
        self.assertEqual(
            (after.operation_id,), tuple(r.operation_id for r in view.pending())
        )
        self.assertFalse(view.request(after.operation_id).carried)

    def test_every_message_is_carried_rather_than_a_bounded_window(self) -> None:
        """No record cap here, unlike pending guidance.

        Guidance can be re-read with one command; a correction dropped from
        this block has no other surface, so an operator's third thought would
        be silently lost.
        """
        texts = tuple(f"correction number {index}" for index in range(1, 13))
        for text in texts:
            self.submit(text)

        carried = self.carry()

        self.assertEqual(list(texts), [item.text for item in carried.corrections])

    def test_one_refusal_does_not_cost_the_corrections_beside_it(self) -> None:
        """An expired neighbour must not cost the attempt the good ones.

        A prompt that was never built is strictly worse than a prompt carrying
        three of four corrections and a refusal an operator can read in an
        event.
        """
        stale = self.submit(FIRST, ttl_seconds=1)
        good = self.submit(SECOND)

        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(2),
            state_dir=self.state_dir,
            now=clock.utc_now() + timedelta(seconds=30),
        )

        self.assertEqual((good.operation_id,), carried.operation_ids())
        self.assertEqual([SECOND], [item.text for item in carried.corrections])
        self.assertEqual(
            [(stale.operation_id, redirect_store_vocab.ERROR_REQUEST_EXPIRED)],
            [(item.operation_id, item.reason) for item in carried.refused],
        )
        self.assertEqual(
            redirect_store_vocab.STATE_EXPIRED,
            self.read().request(stale.operation_id).state,
        )

    def test_a_correction_for_another_session_is_never_swept_in(self) -> None:
        """Two runs, one journal: the target decides, never proximity in time."""
        mine = self.submit(FIRST)
        theirs = redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(1, session_id="2"),
            feedback=SECOND,
            state_dir=self.state_dir,
        )
        other_run = redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(1, run_id="run-other"),
            feedback=THIRD,
            state_dir=self.state_dir,
        )

        carried = self.carry()

        self.assertEqual((mine.operation_id,), carried.operation_ids())
        view = self.read()
        for operation_id in (theirs.operation_id, other_run.operation_id):
            self.assertEqual(
                redirect_store_vocab.STATE_SUBMITTED, view.request(operation_id).state
            )

    def test_an_id_the_journal_cannot_corroborate_is_not_invented(self) -> None:
        """The gate reports ids; the journal is what makes them corrections."""
        carried = self.carry(accepted=("op-that-never-existed",))

        self.assertEqual((), carried.operation_ids())
        self.assertEqual((), carried.refused)

    def test_carrying_is_a_write_that_happens_once(self) -> None:
        first = self.submit(FIRST)
        carried = self.carry()
        lines = len(self.journal_lines())

        # A prompt re-rendered for the same attempt: same corrections, no sweep.
        self.submit(SECOND)
        again = redirect_ack.carried_corrections(
            self.plan_dir,
            attempt=target(2, harness="codex"),
            state_dir=self.state_dir,
        )

        self.assertEqual(carried.operation_ids(), again.operation_ids())
        self.assertEqual((first.operation_id,), again.operation_ids())
        self.assertEqual(lines + 1, len(self.journal_lines()))

    def test_re_carrying_the_same_request_appends_nothing(self) -> None:
        """A runner that retries after a crash must not deliver one as two."""
        request = self.submit(FIRST)
        self.carry()
        lines = len(self.journal_lines())
        bound_at = self.read().request(request.operation_id).continued_seq

        self.carry()

        self.assertEqual(lines, len(self.journal_lines()))
        self.assertEqual(
            bound_at, self.read().request(request.operation_id).continued_seq
        )

    def test_a_correction_an_earlier_attempt_carried_is_not_offered_again(self) -> None:
        """The lineage keeps re-presenting it; the boundary does not.

        A second correction against an attempt that was itself corrected extends
        the same pending redirect, so the third attempt is handed the first
        correction's id again. It was already delivered on the conversation this
        attempt resumes, so re-rendering it would present an old instruction as
        new -- and reporting it as refused would claim something was dropped.
        """
        first = self.submit(FIRST)
        self.carry(continuation=target(2), accepted=(first.operation_id,))
        second = self.submit(SECOND, turn_index=2)

        later = self.carry(
            continuation=target(3),
            accepted=(first.operation_id, second.operation_id),
        )

        self.assertEqual((second.operation_id,), later.operation_ids())
        self.assertEqual((), later.refused)
        self.assertTrue(
            self.read().request(first.operation_id).continues_attempt(target(2))
        )

    def test_a_second_attempt_cannot_take_delivery_of_the_same_correction(self) -> None:
        """The journal's own refusal, reached directly.

        The boundary filters this case out before it gets here, but the store
        has to refuse it anyway: two attempts each able to prove delivery of one
        correction is the exact ambiguity the receipt exists to remove.
        """
        request = self.submit(FIRST)
        self.carry(continuation=target(2))

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_carry.carry_request(
                self.plan_dir,
                request.operation_id,
                continuation=target(3),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_ALREADY_CARRIED, caught.exception.reason
        )

    def test_an_annotation_never_moves_a_request_out_of_accepted(self) -> None:
        """The journal's "settled once" invariant, restated for session 12.

        ``continued`` and ``acknowledged`` are records *about* an accepted
        request. If either were a state, a carried correction would be settled
        twice and every reader that trusts ``terminal`` would have to be
        re-audited.
        """
        request = self.submit(FIRST, accept=True)

        self.carry(accepted=(request.operation_id,))
        redirect_ack.acknowledge_correction(
            self.plan_dir,
            request.operation_id,
            token=redirect_ack.attempt_token(target(2)),
            live_attempt=target(2, harness="codex"),
            state_dir=self.state_dir,
        )

        stored = self.read().request(request.operation_id)
        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, stored.state)
        self.assertTrue(stored.terminal)
        self.assertTrue(stored.carried)
        self.assertTrue(stored.acknowledged)
        self.assertEqual(
            [
                redirect_store_vocab.KIND_SUBMITTED,
                redirect_store_vocab.KIND_ACCEPTED,
                redirect_store_vocab.KIND_CONTINUED,
                redirect_store_vocab.KIND_ACKNOWLEDGED,
            ],
            [line["kind"] for line in self.journal_lines()],
        )

    def test_a_submission_the_gate_never_saw_is_accepted_as_it_is_carried(self) -> None:
        """Otherwise it expires unread.

        Nobody accepts a correction aimed at a turn that is already gone, so
        the carry is its only chance to become a delivered redirect.
        """
        request = self.submit(FIRST)

        self.carry()

        stored = self.read().request(request.operation_id)
        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, stored.state)
        self.assertIsNotNone(stored.settled_seq)

    def test_what_a_carried_projection_says_and_what_it_withholds(self) -> None:
        request = self.submit(FIRST)

        carried = self.carry()
        payload = carried.to_payload()

        self.assertNotIn(FIRST, json.dumps(payload))
        self.assertEqual(f"{RUN_ID}/1/2", payload["attempt"])
        self.assertEqual(1, payload["carried_count"])
        self.assertEqual(len(FIRST.encode("utf-8")), payload["feedback_bytes"])
        self.assertEqual(
            redirect_store_validation.feedback_digest(FIRST),
            payload["carried"][0]["feedback_digest"],
        )
        self.assertNotIn(FIRST, repr(carried.corrections[0]))
        self.assertEqual(request.operation_id, payload["carried"][0]["operation_id"])

    def test_an_empty_carry_is_falsey_so_no_block_is_rendered(self) -> None:
        self.assertFalse(self.carry())
        self.assertTrue(bool(self.submit(FIRST)) and bool(self.carry()))
