"""Which corrections a resumed prompt carries, and what counts as receipt.

Session 11 delivered an accepted correction to the next attempt of the same
session. These tests are about the two things it left undecided, and both are
about *losing* something -- a correction the operator sent, or the evidence that
a worker read one.

The claims, in the order the module argues them:

* the boundary is a moment rather than a filter: everything unsettled against an
  earlier attempt of this session is swept into the prompt being finalized,
  merged with what the gate accepted mid-turn and ordered by submission, while
  anything aimed at the attempt about to start is left to interrupt it in turn;
* every carried message survives that merge, and a refusal never costs the
  corrections beside it;
* carrying is a write that happens once -- a re-prepared prompt re-reads the
  same set instead of sweeping up whatever arrived since, and no annotation
  moves a request out of ``accepted``, so the journal's "settled once"
  invariant is untouched;
* a receipt needs both halves of its identity, so the two cheap impostors --
  the stale worker still alive from the interrupted turn, and a transcript
  replaying an older prompt -- are refused, as is a worker naming an attempt
  the journal never recorded as the carrier;
* a receipt is idempotent, and a hand-written one is refused on replay too,
  because the journal is the only evidence and a file is editable;
* and the wait is observational: a timeout writes nothing, retracts nothing,
  and reports no failure.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_session_loop import (
    clock,
    mailbox_cli,
    redirect_ack,
    redirect_continuation,
    redirect_store,
    run_loop,
)

from .support import write_plan
from .test_asl_turn_dispositions import RUN_ID, FakeRunContext

SESSION_ID = "1"

#: A caller that passed nothing, which is not the same as one that passed
#: ``None`` -- "no live attempt" is one of the cases under test.
_DEFAULT = object()

FIRST = "stop editing README and re-read the contract"
SECOND = "the parser you picked cannot see the second column"
THIRD = "and leave the fixture alone"


def target(
    turn_index: int = 1,
    *,
    harness: str | None = "claude",
    run_id: str = RUN_ID,
    session_id: str = SESSION_ID,
) -> redirect_store.RedirectTarget:
    return redirect_store.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )


class _FakeCommandContext:
    """Only the member ``inbox acknowledge`` reads off a resolved plan."""

    def __init__(self, plan_dir: Path) -> None:
        self.snapshot = type("_Snapshot", (), {"plan_dir": plan_dir})()


class RedirectAckTestCase(unittest.TestCase):
    """A plan with its own state root, driving the real journal file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plan_dir = self.root / "plan"
        self.state_dir = self.root / "state"
        write_plan(self.plan_dir, ("pending",))

    def submit(
        self,
        feedback: str = FIRST,
        *,
        turn_index: int = 1,
        accept: bool = False,
        **kwargs: Any,
    ) -> redirect_store.RedirectRequest:
        request = redirect_store.submit_request(
            self.plan_dir,
            target=target(turn_index),
            feedback=feedback,
            state_dir=self.state_dir,
            **kwargs,
        )
        if accept:
            return redirect_store.accept_request(
                self.plan_dir,
                request.operation_id,
                target=target(turn_index),
                state_dir=self.state_dir,
            )
        return request

    def carry(
        self,
        *,
        continuation: redirect_store.RedirectTarget | None = None,
        accepted: tuple[str, ...] = (),
    ) -> redirect_ack.CarriedCorrections:
        return redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=continuation or target(2, harness="codex"),
            accepted_operation_ids=accepted,
            state_dir=self.state_dir,
        )

    def read(self) -> redirect_store.RedirectJournalView:
        return redirect_store.read_journal(self.plan_dir, state_dir=self.state_dir)

    def journal_path(self) -> Path:
        return redirect_store.journal_path_for_plan(self.plan_dir, self.state_dir)

    def journal_lines(self) -> list[dict[str, Any]]:
        path = self.journal_path()
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append_raw(self, payload: dict[str, Any]) -> None:
        with self.journal_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")


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
        after = redirect_store.submit_request(
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
            [(stale.operation_id, redirect_store.ERROR_REQUEST_EXPIRED)],
            [(item.operation_id, item.reason) for item in carried.refused],
        )
        self.assertEqual(
            redirect_store.STATE_EXPIRED, self.read().request(stale.operation_id).state
        )

    def test_a_correction_for_another_session_is_never_swept_in(self) -> None:
        """Two runs, one journal: the target decides, never proximity in time."""
        mine = self.submit(FIRST)
        theirs = redirect_store.submit_request(
            self.plan_dir,
            target=target(1, session_id="2"),
            feedback=SECOND,
            state_dir=self.state_dir,
        )
        other_run = redirect_store.submit_request(
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
                redirect_store.STATE_SUBMITTED, view.request(operation_id).state
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

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.carry_request(
                self.plan_dir,
                request.operation_id,
                continuation=target(3),
                state_dir=self.state_dir,
            )

        self.assertEqual(redirect_store.ERROR_ALREADY_CARRIED, caught.exception.reason)

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
        self.assertEqual(redirect_store.STATE_ACCEPTED, stored.state)
        self.assertTrue(stored.terminal)
        self.assertTrue(stored.carried)
        self.assertTrue(stored.acknowledged)
        self.assertEqual(
            [
                redirect_store.KIND_SUBMITTED,
                redirect_store.KIND_ACCEPTED,
                redirect_store.KIND_CONTINUED,
                redirect_store.KIND_ACKNOWLEDGED,
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
        self.assertEqual(redirect_store.STATE_ACCEPTED, stored.state)
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
            redirect_store.feedback_digest(FIRST),
            payload["carried"][0]["feedback_digest"],
        )
        self.assertNotIn(FIRST, repr(carried.corrections[0]))
        self.assertEqual(request.operation_id, payload["carried"][0]["operation_id"])

    def test_an_empty_carry_is_falsey_so_no_block_is_rendered(self) -> None:
        self.assertFalse(self.carry())
        self.assertTrue(bool(self.submit(FIRST)) and bool(self.carry()))


class AcknowledgmentTests(RedirectAckTestCase):
    """The receipt, and the cheaper things that are not one."""

    def setUp(self) -> None:
        super().setUp()
        self.request = self.submit(FIRST, accept=True)
        self.continuation = target(2, harness="codex")
        self.carry(
            continuation=self.continuation, accepted=(self.request.operation_id,)
        )
        self.token = redirect_ack.attempt_token(self.continuation)

    def acknowledge(
        self,
        *,
        token: str | None = None,
        live: redirect_store.RedirectTarget | None | object = _DEFAULT,
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
                else cast("redirect_store.RedirectTarget | None", live)
            ),
            state_dir=self.state_dir,
        )

    def test_the_worker_running_the_attempt_receipts_its_correction(self) -> None:
        receipt = self.acknowledge()

        self.assertEqual(self.token, receipt.attempt)
        self.assertFalse(receipt.repeated)
        self.assertEqual(redirect_store.feedback_digest(FIRST), receipt.feedback_digest)
        self.assertTrue(self.read().request(self.request.operation_id).acknowledged)

    def test_a_stale_worker_from_the_interrupted_turn_cannot_receipt_it(self) -> None:
        """The first impostor, and the reason the token is checked twice.

        The predecessor may still be alive for seconds after the correction was
        accepted, holding a prompt of its own. Its token names the turn that was
        interrupted, which is not the turn that carried the correction.
        """
        with self.assertRaises(redirect_ack.RedirectAckError) as caught:
            self.acknowledge(token=redirect_ack.attempt_token(target(1)))

        self.assertEqual(redirect_ack.ERROR_ATTEMPT_MISMATCH, caught.exception.reason)
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_a_transcript_echo_of_an_older_prompt_cannot_receipt_it(self) -> None:
        """The second impostor.

        A resumed conversation replays the prompt, token and all. What makes the
        replay harmless is that the live attempt has moved on, so the token in
        the transcript no longer names it.
        """
        with self.assertRaises(redirect_ack.RedirectAckError) as caught:
            self.acknowledge(live=target(3, harness="codex"))

        self.assertEqual(redirect_ack.ERROR_ATTEMPT_MISMATCH, caught.exception.reason)
        self.assertEqual(
            redirect_ack.attempt_token(target(3)),
            caught.exception.details["live_attempt"],
        )
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_an_unverifiable_receipt_is_refused_rather_than_trusted(self) -> None:
        """No live attempt is a refusal, not a pass.

        Otherwise "the run state was unreadable" becomes the way a receipt gets
        written for an attempt nobody can show was running.
        """
        with self.assertRaises(redirect_ack.RedirectAckError) as caught:
            self.acknowledge(live=None)

        self.assertEqual(redirect_ack.ERROR_NO_LIVE_ATTEMPT, caught.exception.reason)
        self.assertEqual([], self.acknowledged_ids())

    def test_a_worker_naming_an_attempt_that_carried_nothing_is_refused(self) -> None:
        """The journal's half of the check.

        A token that matches the live attempt still proves nothing if that
        attempt is not the one the journal recorded as the carrier.
        """
        other = self.submit(SECOND, accept=True)

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.acknowledge(operation_id=other.operation_id)

        self.assertEqual(redirect_store.ERROR_NOT_CARRIED, caught.exception.reason)

    def test_a_receipt_for_a_correction_carried_elsewhere_is_refused(self) -> None:
        elsewhere = self.submit(SECOND)
        self.carry(continuation=target(4), accepted=(elsewhere.operation_id,))

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.acknowledge(operation_id=elsewhere.operation_id)

        self.assertEqual(
            redirect_store.ERROR_CONTINUATION_MISMATCH, caught.exception.reason
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
                "kind": redirect_store.KIND_ACKNOWLEDGED,
                "id": other.operation_id,
                "created_at": clock.isoformat(clock.utc_now()),
                "continuation": self.continuation.to_payload(),
            }
        )

        view = self.read()

        self.assertFalse(view.request(other.operation_id).acknowledged)
        self.assertEqual(
            [redirect_store.REJECT_NOT_CARRIED],
            [record.reason for record in view.rejected],
        )

    def test_a_forged_receipt_for_a_foreign_attempt_is_refused_on_replay(self) -> None:
        self.append_raw(
            {
                "version": 1,
                "kind": redirect_store.KIND_ACKNOWLEDGED,
                "id": self.request.operation_id,
                "created_at": clock.isoformat(clock.utc_now()),
                "continuation": target(5).to_payload(),
            }
        )

        view = self.read()

        self.assertFalse(view.request(self.request.operation_id).acknowledged)
        self.assertEqual(
            [redirect_store.REJECT_CONTINUATION_MISMATCH],
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
                "kind": redirect_store.KIND_ACKNOWLEDGED,
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
            [redirect_store.REJECT_ALREADY_ACKNOWLEDGED],
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


class AnnotationReserveTests(RedirectAckTestCase):
    """A journal too full for new corrections still accounts for its old ones."""

    def fill_to_the_submission_cap(self) -> None:
        padding = (
            redirect_store.REDIRECT_JOURNAL_MAX_BYTES
            - redirect_store.REDIRECT_SETTLEMENT_RESERVE_BYTES
            - self.journal_path().stat().st_size
        )
        # Padded in long lines rather than two-byte ones: the cap counts bytes,
        # and a few thousand junk lines replay in a fraction of the time two
        # million of them take.
        line = b"x" * 8191 + b"\n"
        whole, remainder = divmod(padding, len(line))
        with self.journal_path().open("ab") as handle:
            handle.write(line * whole)
            if remainder:
                handle.write(b"x" * (remainder - 1) + b"\n")

    def test_delivery_and_receipt_still_land_past_the_submission_cap(self) -> None:
        # The reserve's justification: an accepted correction whose delivery
        # could not be recorded would be one no receipt could ever be checked
        # against, which is worse than a journal that refuses new corrections.
        request = self.submit(accept=True)
        self.fill_to_the_submission_cap()
        with self.assertRaises(redirect_store.RedirectStoreError) as refused:
            self.submit(SECOND)
        self.assertEqual(redirect_store.ERROR_JOURNAL_FULL, refused.exception.reason)

        continuation = target(2, harness="codex")
        carried = self.carry(
            continuation=continuation, accepted=(request.operation_id,)
        )
        receipt = redirect_ack.acknowledge_correction(
            self.plan_dir,
            request.operation_id,
            token=redirect_ack.attempt_token(continuation),
            live_attempt=continuation,
            state_dir=self.state_dir,
        )

        self.assertEqual([request.operation_id], list(carried.operation_ids()))
        self.assertFalse(receipt.repeated)
        # Read back through replay, so both annotations are proven to be in the
        # file rather than only in the returned records.
        settled = self.read().request(request.operation_id)
        self.assertTrue(settled.carried)
        self.assertTrue(settled.acknowledged)


class ObservationalWaitTests(RedirectAckTestCase):
    """What an observer may conclude from silence: nothing actionable."""

    def setUp(self) -> None:
        super().setUp()
        self.request = self.submit(FIRST)
        self.continuation = target(2, harness="codex")
        self.carry(continuation=self.continuation)

    def wait(self, **kwargs: Any) -> redirect_ack.AcknowledgmentWait:
        clock_values = kwargs.pop("clock_values", None)
        slept: list[float] = kwargs.pop("slept", [])
        ticks = iter(clock_values or (0.0, 0.0))

        def monotonic() -> float:
            try:
                return next(ticks)
            except StopIteration:
                return 10_000.0

        params: dict[str, Any] = {
            "attempt": self.continuation,
            "state_dir": self.state_dir,
            "monotonic": monotonic,
            "sleep": slept.append,
        }
        params.update(kwargs)
        return redirect_ack.wait_for_acknowledgment(self.plan_dir, **params)

    def test_a_timeout_writes_nothing_and_reports_no_failure(self) -> None:
        """The session's rule, in one assertion set.

        A worker can be busy applying the correction for far longer than any
        interesting wait, so a timeout that cancelled or re-interrupted would
        attack exactly the work the correction asked for.
        """
        lines = len(self.journal_lines())

        result = self.wait(timeout_seconds=0.0)

        self.assertFalse(result.complete)
        self.assertEqual((self.request.operation_id,), result.outstanding)
        self.assertEqual((), result.acknowledged)
        self.assertEqual(lines, len(self.journal_lines()))
        payload = result.to_payload()
        self.assertTrue(payload["observational"])
        self.assertNotIn("failed", payload)
        # The correction is still carried, still accepted, still unacknowledged:
        # the wait ending changed nothing about it.
        stored = self.read().request(self.request.operation_id)
        self.assertTrue(stored.carried)
        self.assertFalse(stored.acknowledged)

    def test_a_receipt_that_lands_during_the_wait_completes_it(self) -> None:
        slept: list[float] = []

        def acknowledge_on_second_poll(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 1:
                redirect_ack.acknowledge_correction(
                    self.plan_dir,
                    self.request.operation_id,
                    token=redirect_ack.attempt_token(self.continuation),
                    live_attempt=self.continuation,
                    state_dir=self.state_dir,
                )

        result = self.wait(
            timeout_seconds=30.0,
            clock_values=(0.0, 1.0, 2.0),
            sleep=acknowledge_on_second_poll,
        )

        self.assertTrue(result.complete)
        self.assertEqual((self.request.operation_id,), result.acknowledged)
        self.assertEqual([redirect_ack.ACK_WAIT_POLL_INTERVAL_SECONDS], slept)

    def test_an_id_the_journal_never_recorded_stays_outstanding(self) -> None:
        result = self.wait(operation_ids=("op-missing",), timeout_seconds=0.0)

        self.assertEqual(("op-missing",), result.outstanding)

    def test_patience_is_bounded_even_when_a_caller_asks_for_more(self) -> None:
        """A caller cannot ask an observer to watch forever.

        The ceiling is what keeps a mistaken timeout from turning a read-only
        observation into a process that outlives the run it was watching.
        """
        slept: list[float] = []

        result = self.wait(
            timeout_seconds=10_000.0,
            clock_values=(0.0, redirect_ack.MAX_ACK_WAIT_SECONDS + 1.0),
            slept=slept,
        )

        self.assertFalse(result.complete)
        # Past the ceiling the observer is done; had it honoured the 10,000
        # seconds it was handed, it would have slept and polled again.
        self.assertEqual([], slept)


class AcknowledgeCommandTests(RedirectAckTestCase):
    """``asl inbox acknowledge``: the worker's own surface."""

    def setUp(self) -> None:
        super().setUp()
        self.request = self.submit(FIRST, accept=True)
        self.continuation = target(2, harness="codex")
        self.carry(
            continuation=self.continuation, accepted=(self.request.operation_id,)
        )

    def args(self, **overrides: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "plan": None,
            "state_dir": str(self.state_dir),
            "json": True,
            "redirect_id": None,
            "attempt": redirect_ack.attempt_token(self.continuation),
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def run_command(
        self,
        *,
        live: redirect_store.RedirectTarget | None = None,
        **overrides: Any,
    ) -> tuple[int, dict[str, Any]]:
        """Drive the handler with the plan resolved and the live attempt dialled.

        Plan resolution and the run-state read are both patched, because the
        subject here is what the command does with an attempt it was given --
        which attempt is live is ``_live_attempt``'s own contract, and the run
        state that would carry one is not something this journal has.
        """
        captured: list[dict[str, Any]] = []
        context = _FakeCommandContext(self.plan_dir)
        with (
            mock.patch.object(mailbox_cli, "_resolve", return_value=context),
            mock.patch.object(mailbox_cli, "_live_attempt", return_value=live),
            mock.patch.object(mailbox_cli.responses, "print_json", captured.append),
        ):
            code = mailbox_cli.inbox_acknowledge_from_args(self.args(**overrides))
        return code, captured[0]

    def test_the_default_receipts_exactly_what_the_attempt_carries(self) -> None:
        code, payload = self.run_command(live=self.continuation)

        self.assertEqual(0, code)
        self.assertEqual([self.request.operation_id], payload["requested"])
        self.assertEqual(
            [self.request.operation_id],
            [record["operation_id"] for record in payload["acknowledged"]],
        )
        self.assertEqual([], payload["refused"])
        self.assertTrue(self.read().request(self.request.operation_id).acknowledged)

    def test_a_mismatched_token_refuses_every_named_correction(self) -> None:
        """One claim, checked once: a bad token is not a per-id problem."""
        code, payload = self.run_command(
            live=self.continuation,
            redirect_id=[self.request.operation_id],
            attempt=redirect_ack.attempt_token(target(1)),
        )

        self.assertEqual(1, code)
        self.assertEqual([], payload["acknowledged"])
        self.assertEqual(
            [redirect_ack.ERROR_ATTEMPT_MISMATCH],
            [record["reason"] for record in payload["refused"]],
        )
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_a_command_run_with_no_live_attempt_refuses(self) -> None:
        code, payload = self.run_command(
            live=None, redirect_id=[self.request.operation_id]
        )

        self.assertEqual(1, code)
        self.assertIsNone(payload["live_attempt"])
        self.assertEqual(
            [redirect_ack.ERROR_NO_LIVE_ATTEMPT],
            [record["reason"] for record in payload["refused"]],
        )

    def test_a_bare_command_with_no_live_attempt_refuses_rather_than_reassures(
        self,
    ) -> None:
        # Nothing to enumerate and nothing to check an enumeration against, so
        # the honest answer is the refusal rather than "no corrections carried",
        # which a stale worker would read as permission to skip the receipt.
        code, payload = self.run_command(live=None)

        self.assertEqual(1, code)
        self.assertEqual(redirect_ack.ERROR_NO_LIVE_ATTEMPT, payload["reason"])
        self.assertFalse(self.read().request(self.request.operation_id).acknowledged)

    def test_a_live_attempt_carrying_nothing_reports_an_empty_receipt(self) -> None:
        code, payload = self.run_command(live=target(3, harness="codex"))

        self.assertEqual(0, code)
        self.assertEqual([], payload["requested"])
        self.assertEqual([], payload["acknowledged"])
        self.assertEqual([], payload["refused"])

    def test_an_unknown_id_does_not_suppress_the_receipt_beside_it(self) -> None:
        code, payload = self.run_command(
            live=self.continuation,
            redirect_id=["op-missing", self.request.operation_id],
        )

        self.assertEqual(1, code)
        self.assertEqual(
            [self.request.operation_id],
            [record["operation_id"] for record in payload["acknowledged"]],
        )
        self.assertEqual(
            [redirect_store.ERROR_UNKNOWN_REQUEST],
            [record["reason"] for record in payload["refused"]],
        )

    def test_the_receipt_payload_never_carries_the_operators_words(self) -> None:
        _, payload = self.run_command(live=self.continuation)

        self.assertNotIn(FIRST, json.dumps(payload))


class RunLoopBoundaryTests(RedirectAckTestCase):
    """Where the run loop draws the boundary, and what it records there."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = FakeRunContext(event_path=self.root / "events.jsonl")

    def pending(
        self,
        *,
        session_id: str = SESSION_ID,
        operation_ids: tuple[str, ...] = (),
    ) -> redirect_continuation.PendingRedirect:
        return redirect_continuation.PendingRedirect(
            session_id=session_id,
            harness="claude",
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=operation_ids,
            feedback_digests=(),
            feedback_bytes=0,
        )

    def carried(
        self,
        *,
        pending: redirect_continuation.PendingRedirect | None,
        session_id: str = SESSION_ID,
        turn_index: int = 2,
    ) -> redirect_ack.CarriedCorrections | None:
        return run_loop._carried_corrections(
            self.ctx,
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir,
            pending=pending,
            session_id=session_id,
            turn_index=turn_index,
            harness="codex",
        )

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self.ctx.persistence.events)

    def test_a_turn_that_was_not_corrected_sweeps_nothing(self) -> None:
        """Not an optimisation: the gate's session match, restated.

        A correction is never swept into a session the operator was not talking
        about, so a fresh turn reads a prompt with no correction block even if
        the journal holds feedback for another session.
        """
        self.submit(FIRST)

        self.assertIsNone(self.carried(pending=None))
        self.assertEqual([], self.events())

    def test_a_correction_for_a_different_session_does_not_cross_over(self) -> None:
        self.submit(FIRST)

        self.assertIsNone(self.carried(pending=self.pending(session_id="2")))
        self.assertEqual([], self.events())

    def test_the_continuation_records_what_it_carried(self) -> None:
        accepted = self.submit(FIRST, accept=True)
        late = self.submit(SECOND)

        carried = self.carried(
            pending=self.pending(operation_ids=(accepted.operation_id,))
        )

        self.assertEqual(
            (accepted.operation_id, late.operation_id), carried.operation_ids()
        )
        name, payload = self.events()[-1]
        self.assertEqual("redirect_continuation", name)
        self.assertEqual("carried", payload["outcome"])
        self.assertEqual(f"{RUN_ID}/1/2", payload["attempt"])
        self.assertEqual(2, payload["carried_count"])
        self.assertNotIn(FIRST, json.dumps(payload))
        self.assertNotIn(SECOND, json.dumps(payload))

    def test_an_unusable_journal_degrades_to_session_eleven_behaviour(self) -> None:
        """The prompt is still built, and the corrections are not lost.

        The resumed worker is still told which operations were interrupted, and
        the failure is recorded rather than silently changing what it reads.
        """
        self.journal_path().parent.mkdir(parents=True, exist_ok=True)
        self.journal_path().write_text("not json\n", encoding="utf-8")
        self.journal_path().chmod(0o000)
        self.addCleanup(self.journal_path().chmod, 0o600)

        carried = self.carried(pending=self.pending(operation_ids=("op-1",)))

        self.assertIsNone(carried)
        name, payload = self.events()[-1]
        self.assertEqual("redirect_continuation", name)
        self.assertEqual("carry-failed", payload["outcome"])
        self.assertIn("reason", payload["error"])


if __name__ == "__main__":
    unittest.main()
