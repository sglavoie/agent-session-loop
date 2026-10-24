# mypy: ignore-errors
"""Redirect launch boundary and worker acknowledgments."""

# ruff: noqa: F403, F405
from ._test_asl_redirect_ack_support import *


class LaunchBoundaryTests(RedirectAckTestCase):
    """The window between binding a correction to a prompt and starting a worker.

    A carry says an attempt undertook to deliver the words. Everything between
    that and a running process can still refuse -- the prompt build, the
    conversation selection, an operator's pause -- and the attempt that refuses
    is the one that has to say so, because nothing else can tell a correction
    that was never shown to anybody from one that was.
    """

    def setUp(self) -> None:
        super().setUp()
        self.request = self.submit(FIRST, accept=True)
        self.continuation = target(2, harness="codex")
        self.carry(
            continuation=self.continuation, accepted=(self.request.operation_id,)
        )

    def stored(self) -> redirect_store_models.RedirectRequest:
        stored = self.read().request(self.request.operation_id)
        assert stored is not None
        return stored

    def deliver(self, **kwargs: Any) -> redirect_ack_annotate.CarryAnnotations:
        return redirect_ack_annotate.deliver_corrections(
            self.plan_dir,
            continuation=self.continuation,
            state_dir=self.state_dir,
            **kwargs,
        )

    def release(self, **kwargs: Any) -> redirect_ack_annotate.CarryAnnotations:
        return redirect_ack_annotate.release_corrections(
            self.plan_dir,
            continuation=self.continuation,
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_a_carry_is_not_yet_a_delivery(self) -> None:
        """The distinction the whole boundary rests on."""
        stored = self.stored()

        self.assertTrue(stored.carried)
        self.assertFalse(stored.delivered)
        self.assertFalse(stored.released)
        self.assertFalse(stored.reached_a_worker)

    def test_delivery_settles_whatever_the_attempt_was_holding(self) -> None:
        """Named by the attempt, not by a list.

        The caller on the launch side is a spawn callback that knows which turn
        started and nothing about which ids were swept into its prompt; asking
        it to repeat them would be asking it to be right about something it did
        not compute.
        """
        annotations = self.deliver()

        self.assertEqual(redirect_store_vocab.KIND_DELIVERED, annotations.kind)
        self.assertEqual((self.request.operation_id,), annotations.operation_ids)
        self.assertEqual((), annotations.refused)
        self.assertEqual(f"{RUN_ID}/1/2", annotations.attempt)
        stored = self.stored()
        self.assertTrue(stored.delivered)
        self.assertTrue(stored.reached_a_worker)
        self.assertTrue(stored.carried)

    def test_a_release_hands_the_correction_back_uncarried(self) -> None:
        annotations = self.release()

        self.assertEqual(redirect_store_vocab.KIND_RELEASED, annotations.kind)
        self.assertEqual((self.request.operation_id,), annotations.operation_ids)
        self.assertEqual((), annotations.refused)
        stored = self.stored()
        self.assertFalse(stored.carried)
        self.assertFalse(stored.delivered)
        self.assertFalse(stored.reached_a_worker)
        self.assertTrue(stored.released)
        # Which attempt gave it back is kept: an operator reading a journal
        # after the fact needs to see the attempt that was planned and failed,
        # and the carry it would otherwise read is gone.
        self.assertIsNotNone(stored.released_from)
        self.assertTrue(stored.released_from.identifies_same_turn(self.continuation))

    def test_a_released_correction_is_carryable_again(self) -> None:
        """What the release is for, and the whole of what it does.

        A second carry is refused outright once one is on the record, so a
        correction whose attempt never launched would be unreachable forever
        without this. The release does not re-offer it -- the sweep still only
        volunteers corrections nobody has settled -- it makes it takeable by the
        next attempt that names it, which is what recovery does after a crash.
        """
        self.release()

        later = self.carry(
            continuation=target(3), accepted=(self.request.operation_id,)
        )

        self.assertEqual((self.request.operation_id,), later.operation_ids())
        self.assertEqual((), later.refused)
        self.assertTrue(self.stored().continues_attempt(target(3)))

    def test_neither_annotation_settles_an_accepted_request_twice(self) -> None:
        """The journal's "settled once" invariant, restated for two more kinds."""
        self.release()
        self.carry(continuation=target(3), accepted=(self.request.operation_id,))
        redirect_ack_annotate.deliver_corrections(
            self.plan_dir,
            continuation=target(3),
            state_dir=self.state_dir,
        )

        stored = self.stored()
        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, stored.state)
        self.assertTrue(stored.terminal)
        self.assertEqual(
            [
                redirect_store_vocab.KIND_SUBMITTED,
                redirect_store_vocab.KIND_ACCEPTED,
                redirect_store_vocab.KIND_CONTINUED,
                redirect_store_vocab.KIND_RELEASED,
                redirect_store_vocab.KIND_CONTINUED,
                redirect_store_vocab.KIND_DELIVERED,
            ],
            [line["kind"] for line in self.journal_lines()],
        )

    def test_a_correction_that_reached_a_worker_cannot_be_taken_back(self) -> None:
        """The asymmetry that makes the release safe to call on any refusal.

        A release after a launch would offer the same words to a second worker
        as if they were new. It is refused by the journal rather than by the
        caller's judgement, because the caller is a pause path that cannot know
        whether the process it is unwinding ever started.
        """
        self.deliver()

        annotations = self.release()

        self.assertEqual((), annotations.operation_ids)
        self.assertEqual(
            [redirect_store_vocab.ERROR_ALREADY_DELIVERED],
            [item.reason for item in annotations.refused],
        )
        stored = self.stored()
        self.assertTrue(stored.delivered)
        self.assertTrue(stored.carried)
        self.assertFalse(stored.released)

    def test_a_receipted_correction_cannot_be_taken_back_either(self) -> None:
        """A worker that answered is proof enough on its own.

        Reaching a worker is what forbids the release, and a receipt says so
        more strongly than the delivery record does -- so a receipt recorded
        without one still closes the door.
        """
        redirect_ack.acknowledge_correction(
            self.plan_dir,
            self.request.operation_id,
            token=redirect_ack_attempt.attempt_token(self.continuation),
            live_attempt=self.continuation,
            state_dir=self.state_dir,
        )

        annotations = self.release()

        self.assertEqual((), annotations.operation_ids)
        self.assertEqual(
            [redirect_store_vocab.ERROR_ALREADY_ACKNOWLEDGED],
            [item.reason for item in annotations.refused],
        )
        self.assertTrue(self.stored().acknowledged)

    def test_repeating_a_delivery_appends_nothing(self) -> None:
        """A runner retried after a crash must not deliver one as two."""
        self.deliver()
        lines = len(self.journal_lines())
        delivered_at = self.stored().delivered_seq

        again = self.deliver()

        self.assertEqual((self.request.operation_id,), again.operation_ids)
        self.assertEqual(lines, len(self.journal_lines()))
        self.assertEqual(delivered_at, self.stored().delivered_seq)

    def test_repeating_a_release_appends_nothing(self) -> None:
        """Named explicitly, because the first release erased the carry.

        A pause path that is re-entered has no journal record left to ask, so
        the ids come from the caller -- and the second pass has to be a no-op
        rather than a refusal, or a retried pause would report a loss it did
        not cause.
        """
        self.release()
        lines = len(self.journal_lines())
        released_at = self.stored().released_seq

        again = self.release(operation_ids=(self.request.operation_id,))

        self.assertEqual((self.request.operation_id,), again.operation_ids)
        self.assertEqual((), again.refused)
        self.assertEqual(lines, len(self.journal_lines()))
        self.assertEqual(released_at, self.stored().released_seq)

    def test_one_refusal_does_not_cost_the_annotations_beside_it(self) -> None:
        second = self.submit(SECOND, accept=True)
        self.carry(continuation=self.continuation, accepted=(second.operation_id,))

        annotations = self.deliver(
            operation_ids=(
                self.request.operation_id,
                "op-that-never-existed",
                second.operation_id,
            )
        )

        self.assertEqual(
            (self.request.operation_id, second.operation_id),
            annotations.operation_ids,
        )
        self.assertEqual(
            [("op-that-never-existed", redirect_store_vocab.ERROR_UNKNOWN_REQUEST)],
            [(item.operation_id, item.reason) for item in annotations.refused],
        )

    def test_an_annotation_by_an_attempt_that_holds_nothing_moves_nothing(self) -> None:
        """The foreign-carrier case, reached through the boundary.

        Two attempts each able to prove delivery of one correction is the exact
        ambiguity the receipt exists to remove, so an attempt annotating a carry
        that is not its own is refused rather than believed.
        """
        annotations = redirect_ack_annotate.deliver_corrections(
            self.plan_dir,
            continuation=target(3),
            operation_ids=(self.request.operation_id,),
            state_dir=self.state_dir,
        )

        self.assertEqual((), annotations.operation_ids)
        self.assertEqual(
            [redirect_store_vocab.ERROR_CONTINUATION_MISMATCH],
            [item.reason for item in annotations.refused],
        )
        self.assertFalse(self.stored().delivered)

    def test_an_annotation_projection_never_carries_the_operators_words(self) -> None:
        payload = self.deliver().to_payload()

        self.assertNotIn(FIRST, json.dumps(payload))
        self.assertEqual(f"{RUN_ID}/1/2", payload["attempt"])
        self.assertEqual(redirect_store_vocab.KIND_DELIVERED, payload["kind"])
        self.assertEqual([self.request.operation_id], payload["operation_ids"])
        self.assertEqual(1, payload["count"])
        self.assertEqual([], payload["refused"])

    def test_an_empty_annotation_is_falsey_so_no_event_is_written(self) -> None:
        self.assertTrue(self.deliver())
        self.assertFalse(
            redirect_ack_annotate.deliver_corrections(
                self.plan_dir,
                continuation=target(3),
                state_dir=self.state_dir,
            )
        )


class AcknowledgmentTests(RedirectAckTestCase):
    """The receipt, and the cheaper things that are not one."""

    def setUp(self) -> None:
        super().setUp()
        self.request = self.submit(FIRST, accept=True)
        self.continuation = target(2, harness="codex")
        self.carry(
            continuation=self.continuation, accepted=(self.request.operation_id,)
        )
        self.token = redirect_ack_attempt.attempt_token(self.continuation)

    def acknowledge(
        self,
        *,
        token: str | None = None,
        live: redirect_store_models.RedirectTarget | None | object = _DEFAULT,
        operation_id: str | None = None,
    ) -> redirect_ack.Acknowledgment:
        """``live=None`` is a case, so a sentinel distinguishes it from unset."""
        return redirect_ack.acknowledge_correction(
            self.plan_dir,
            operation_id or self.request.operation_id,
            token=self.token if token is None else token,
            live_attempt=(
                self.continuation
                if live is _DEFAULT
                else cast("redirect_store_models.RedirectTarget | None", live)
            ),
            state_dir=self.state_dir,
        )

    def test_the_worker_running_the_attempt_receipts_its_correction(self) -> None:
        receipt = self.acknowledge()

        self.assertEqual(self.token, receipt.attempt)
        self.assertFalse(receipt.repeated)
        self.assertEqual(
            redirect_store_validation.feedback_digest(FIRST), receipt.feedback_digest
        )
        self.assertTrue(self.read().request(self.request.operation_id).acknowledged)

    def test_a_stale_worker_from_the_interrupted_turn_cannot_receipt_it(self) -> None:
        """The first impostor, and the reason the token is checked twice.

        The predecessor may still be alive for seconds after the correction was
        accepted, holding a prompt of its own. Its token names the turn that was
        interrupted, which is not the turn that carried the correction.
        """
        with self.assertRaises(redirect_ack_attempt.RedirectAckError) as caught:
            self.acknowledge(token=redirect_ack_attempt.attempt_token(target(1)))

        self.assertEqual(
            redirect_ack_attempt.ERROR_ATTEMPT_MISMATCH, caught.exception.reason
        )
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_a_transcript_echo_of_an_older_prompt_cannot_receipt_it(self) -> None:
        """The second impostor.

        A resumed conversation replays the prompt, token and all. What makes the
        replay harmless is that the live attempt has moved on, so the token in
        the transcript no longer names it.
        """
        with self.assertRaises(redirect_ack_attempt.RedirectAckError) as caught:
            self.acknowledge(live=target(3, harness="codex"))

        self.assertEqual(
            redirect_ack_attempt.ERROR_ATTEMPT_MISMATCH, caught.exception.reason
        )
        self.assertEqual(
            redirect_ack_attempt.attempt_token(target(3)),
            caught.exception.details["live_attempt"],
        )
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_an_unverifiable_receipt_is_refused_rather_than_trusted(self) -> None:
        """No live attempt is a refusal, not a pass.

        Otherwise "the run state was unreadable" becomes the way a receipt gets
        written for an attempt nobody can show was running.
        """
        with self.assertRaises(redirect_ack_attempt.RedirectAckError) as caught:
            self.acknowledge(live=None)

        self.assertEqual(
            redirect_ack_attempt.ERROR_NO_LIVE_ATTEMPT, caught.exception.reason
        )
        self.assertEqual([], self.acknowledged_ids())

    def test_a_worker_naming_an_attempt_that_carried_nothing_is_refused(self) -> None:
        """The journal's half of the check.

        A token that matches the live attempt still proves nothing if that
        attempt is not the one the journal recorded as the carrier.
        """
        other = self.submit(SECOND, accept=True)

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.acknowledge(operation_id=other.operation_id)

        self.assertEqual(
            redirect_store_vocab.ERROR_NOT_CARRIED, caught.exception.reason
        )

    def test_a_receipt_for_a_correction_carried_elsewhere_is_refused(self) -> None:
        elsewhere = self.submit(SECOND)
        self.carry(continuation=target(4), accepted=(elsewhere.operation_id,))

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.acknowledge(operation_id=elsewhere.operation_id)

        self.assertEqual(
            redirect_store_vocab.ERROR_CONTINUATION_MISMATCH, caught.exception.reason
        )

    def test_repeating_a_receipt_appends_nothing_and_says_so(self) -> None:
        first = self.acknowledge()
        lines = len(self.journal_lines())

        again = self.acknowledge()

        self.assertTrue(again.repeated)
        self.assertEqual(first.acknowledged_seq, again.acknowledged_seq)
        self.assertEqual(lines, len(self.journal_lines()))

    def test_a_hand_written_receipt_is_refused_on_replay_too(self) -> None:
        """The journal is a file, and the receipt is the only evidence.

        Replay re-checks every condition the appender checks, so a line added
        outside the API cannot credit an attempt that carried nothing.
        """
        other = self.submit(SECOND, accept=True)
        self.append_raw(
            {
                "version": 1,
                "kind": redirect_store_vocab.KIND_ACKNOWLEDGED,
                "id": other.operation_id,
                "created_at": clock.isoformat(clock.utc_now()),
                "continuation": self.continuation.to_payload(),
            }
        )

        view = self.read()

        self.assertFalse(view.request(other.operation_id).acknowledged)
        self.assertEqual(
            [redirect_store_vocab.REJECT_NOT_CARRIED],
            [record.reason for record in view.rejected],
        )

    def test_a_forged_receipt_for_a_foreign_attempt_is_refused_on_replay(self) -> None:
        self.append_raw(
            {
                "version": 1,
                "kind": redirect_store_vocab.KIND_ACKNOWLEDGED,
                "id": self.request.operation_id,
                "created_at": clock.isoformat(clock.utc_now()),
                "continuation": target(5).to_payload(),
            }
        )

        view = self.read()

        self.assertFalse(view.request(self.request.operation_id).acknowledged)
        self.assertEqual(
            [redirect_store_vocab.REJECT_CONTINUATION_MISMATCH],
            [record.reason for record in view.rejected],
        )

    def test_a_duplicate_receipt_line_is_rejected_rather_than_applied_twice(
        self,
    ) -> None:
        self.acknowledge()
        recorded = self.read().request(self.request.operation_id)

        self.append_raw(
            {
                "version": 1,
                "kind": redirect_store_vocab.KIND_ACKNOWLEDGED,
                "id": self.request.operation_id,
                "created_at": clock.isoformat(clock.utc_now()),
                "continuation": self.continuation.to_payload(),
            }
        )

        view = self.read()
        self.assertEqual(
            recorded.acknowledged_at,
            view.request(self.request.operation_id).acknowledged_at,
        )
        self.assertEqual(
            [redirect_store_vocab.REJECT_ALREADY_ACKNOWLEDGED],
            [record.reason for record in view.rejected],
        )

    def test_a_receipt_never_carries_the_operators_words(self) -> None:
        receipt = self.acknowledge()

        self.assertNotIn(FIRST, json.dumps(receipt.to_payload()))
        self.assertNotIn(FIRST, repr(receipt))

    def test_the_summary_reports_receipt_state_without_writing(self) -> None:
        second = self.submit(SECOND)
        self.carry(continuation=self.continuation)
        self.acknowledge()
        lines = len(self.journal_lines())

        summary = redirect_ack.acknowledgment_summary(
            self.plan_dir, attempt=self.continuation, state_dir=self.state_dir
        )

        self.assertEqual(
            [self.request.operation_id, second.operation_id], summary["carried"]
        )
        self.assertEqual([self.request.operation_id], summary["acknowledged"])
        self.assertEqual([second.operation_id], summary["outstanding"])
        self.assertEqual(lines, len(self.journal_lines()))

    def acknowledged_ids(self) -> list[str]:
        return [
            request.operation_id
            for request in self.read().requests
            if request.acknowledged
        ]
