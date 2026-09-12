"""The redirect journal's durability, identity, reduction, and bounds.

Every test here is about a way feedback could be lost, duplicated, or falsely
reported as accepted. The journal's job is to make those outcomes structurally
unavailable, so the tests drive the real file on a temporary state root rather
than a fake: locking, ``fsync``, short-write rollback and replay are the
subject, and none of them exist in a substitute.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from agent_session_loop import clock, redirect_store
from tests.support import write_plan


def _target(
    turn_index: int = 1, *, run_id: str = "run-1"
) -> redirect_store.RedirectTarget:
    return redirect_store.RedirectTarget(
        run_id=run_id,
        session_id="session-9",
        turn_index=turn_index,
        harness="claude",
    )


class RedirectStoreTestCase(unittest.TestCase):
    """A plan with its own state root, so every test drives a real journal."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))

    def submit(self, **kwargs) -> redirect_store.RedirectRequest:
        params = {
            "target": _target(),
            "feedback": "stop editing README and re-read the contract",
            "state_dir": self.state_dir,
        }
        params.update(kwargs)
        return redirect_store.submit_request(self.plan_dir, **params)

    def read(self) -> redirect_store.RedirectJournalView:
        return redirect_store.read_journal(self.plan_dir, state_dir=self.state_dir)

    def journal_path(self) -> Path:
        return redirect_store.journal_path_for_plan(self.plan_dir, self.state_dir)

    def raw_lines(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.journal_path().read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append_raw(self, line: str) -> None:
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class SubmissionTests(RedirectStoreTestCase):
    """Persisting a correction, and what a submission is allowed to be."""

    def test_a_submission_is_durable_pending_and_addressable(self) -> None:
        request = self.submit()

        self.assertEqual(redirect_store.STATE_SUBMITTED, request.state)
        self.assertEqual(1, request.submitted_seq)
        self.assertFalse(request.terminal)
        view = self.read()
        self.assertEqual(
            (request.operation_id,), tuple(r.operation_id for r in view.pending())
        )
        self.assertEqual(
            request.operation_id, view.request(request.operation_id).operation_id
        )

    def test_the_journal_is_private_to_its_owner(self) -> None:
        self.submit()

        self.assertEqual(0o600, self.journal_path().stat().st_mode & 0o777)

    def test_an_empty_journal_reads_as_an_empty_view_rather_than_an_error(self) -> None:
        view = self.read()

        self.assertEqual((), view.requests)
        self.assertEqual(0, view.last_seq)
        self.assertEqual(0, view.size_bytes)

    def test_feedback_never_rides_on_a_projected_request(self) -> None:
        """The one guarantee a projection cannot opt out of.

        Not a style preference: `to_payload` feeds run state, events, the
        Workbench and bridge receipts, so a text field here would put the
        operator's words in every one of them.
        """
        secret = "do not paste this into a log"
        request = self.submit(feedback=secret)

        payload = request.to_payload()
        self.assertNotIn(secret, json.dumps(payload))
        self.assertNotIn(secret, json.dumps(self.read().to_payload()))
        self.assertEqual(
            redirect_store.feedback_digest(secret), payload["feedback_digest"]
        )
        self.assertEqual(len(secret.encode("utf-8")), payload["feedback_bytes"])

    def test_the_text_is_readable_only_by_naming_the_operation(self) -> None:
        request = self.submit(feedback="use the durable journal")

        self.assertEqual(
            "use the durable journal",
            redirect_store.read_feedback(
                self.plan_dir, request.operation_id, state_dir=self.state_dir
            ),
        )
        self.assertIsNone(
            redirect_store.read_feedback(
                self.plan_dir, "no-such-operation", state_dir=self.state_dir
            )
        )

    def test_empty_and_oversized_feedback_are_refused(self) -> None:
        for feedback in ("", "   \n\t "):
            with self.assertRaises(redirect_store.RedirectStoreError) as caught:
                self.submit(feedback=feedback)
            self.assertEqual(
                redirect_store.ERROR_INVALID_FEEDBACK, caught.exception.reason
            )

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.submit(feedback="x" * (redirect_store.REDIRECT_MAX_FEEDBACK_BYTES + 1))
        self.assertEqual(redirect_store.ERROR_INVALID_FEEDBACK, caught.exception.reason)
        self.assertFalse(self.journal_path().exists())

    def test_a_malformed_target_is_refused_before_anything_is_written(self) -> None:
        for target in (
            redirect_store.RedirectTarget("", "session-9", 1),
            redirect_store.RedirectTarget("run-1", "", 1),
            redirect_store.RedirectTarget("run-1", "session-9", 0),
            redirect_store.RedirectTarget("run-1", "session-9", -3),
            redirect_store.RedirectTarget("run-1", "x" * 200, 1),
        ):
            with self.assertRaises(redirect_store.RedirectStoreError) as caught:
                self.submit(target=target)
            self.assertEqual(
                redirect_store.ERROR_INVALID_TARGET, caught.exception.reason
            )
        self.assertFalse(self.journal_path().exists())

    def test_a_ttl_outside_the_permitted_window_is_refused_not_clamped(self) -> None:
        """Clamping would silently retire a correction earlier than asked."""
        for ttl in (0, -1, redirect_store.REDIRECT_MAX_TTL_SECONDS + 1):
            with self.assertRaises(redirect_store.RedirectStoreError) as caught:
                self.submit(ttl_seconds=ttl)
            self.assertEqual(redirect_store.ERROR_INVALID_TTL, caught.exception.reason)

    def test_the_expiry_window_follows_the_requested_ttl(self) -> None:
        now = clock.utc_now()

        request = self.submit(ttl_seconds=45, now=now)

        self.assertEqual(
            clock.isoformat(now + timedelta(seconds=45)), request.expires_at
        )


class IdempotencyTests(RedirectStoreTestCase):
    """A retry must not become a second correction."""

    def test_resubmitting_the_same_operation_appends_nothing(self) -> None:
        first = self.submit(operation_id="op-1")

        second = self.submit(operation_id="op-1")

        self.assertEqual(first, second)
        self.assertEqual(1, len(self.raw_lines()))
        self.assertEqual(1, len(self.read().requests))

    def test_a_retry_after_settlement_learns_the_outcome(self) -> None:
        """The point of idempotency: a producer that never saw its first answer."""
        first = self.submit(operation_id="op-1")
        redirect_store.accept_request(
            self.plan_dir, "op-1", target=_target(), state_dir=self.state_dir
        )

        replayed = self.submit(operation_id="op-1")

        self.assertEqual(redirect_store.STATE_ACCEPTED, replayed.state)
        self.assertEqual(first.submitted_seq, replayed.submitted_seq)
        self.assertEqual(2, len(self.raw_lines()))

    def test_reusing_an_id_for_different_text_is_refused(self) -> None:
        self.submit(operation_id="op-1", feedback="first correction")

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.submit(operation_id="op-1", feedback="a completely different one")

        self.assertEqual(
            redirect_store.ERROR_INVALID_OPERATION_ID, caught.exception.reason
        )
        self.assertEqual(
            "first correction",
            redirect_store.read_feedback(
                self.plan_dir, "op-1", state_dir=self.state_dir
            ),
        )

    def test_reusing_an_id_for_a_different_turn_is_refused(self) -> None:
        self.submit(operation_id="op-1", target=_target(1))

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.submit(operation_id="op-1", target=_target(2))

        self.assertEqual(
            redirect_store.ERROR_INVALID_OPERATION_ID, caught.exception.reason
        )

    def test_minted_identities_do_not_collide(self) -> None:
        ids = {
            self.submit(feedback=f"correction {index}").operation_id
            for index in range(25)
        }

        self.assertEqual(25, len(ids))


class AcceptanceTests(RedirectStoreTestCase):
    """Only the runner accepts, only once, and only for the turn it names."""

    def test_acceptance_is_a_terminal_record_not_a_rewrite(self) -> None:
        request = self.submit()

        accepted = redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(2, accepted.settled_seq)
        lines = self.raw_lines()
        self.assertEqual(
            [redirect_store.KIND_SUBMITTED, redirect_store.KIND_ACCEPTED],
            [line["kind"] for line in lines],
        )
        self.assertEqual(request.operation_id, lines[0]["id"])

    def test_a_second_acceptance_changes_nothing(self) -> None:
        request = self.submit()
        first = redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        second = redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertEqual(first, second)
        self.assertEqual(2, len(self.raw_lines()))

    def test_a_settled_request_cannot_be_accepted(self) -> None:
        request = self.submit()
        redirect_store.reject_request(
            self.plan_dir,
            request.operation_id,
            reason=redirect_store.REASON_RUN_ENDED,
            state_dir=self.state_dir,
        )

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(),
                state_dir=self.state_dir,
            )

        self.assertEqual(redirect_store.ERROR_ALREADY_SETTLED, caught.exception.reason)

    def test_an_unknown_operation_cannot_be_accepted(self) -> None:
        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.accept_request(
                self.plan_dir,
                "never-submitted",
                target=_target(),
                state_dir=self.state_dir,
            )

        self.assertEqual(redirect_store.ERROR_UNKNOWN_REQUEST, caught.exception.reason)

    def test_a_stale_target_is_refused_and_durably_closed(self) -> None:
        """Advice written for one turn is never re-aimed at its successor."""
        request = self.submit(target=_target(1))

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(2),
                state_dir=self.state_dir,
            )

        self.assertEqual(redirect_store.ERROR_TARGET_MISMATCH, caught.exception.reason)
        settled = self.read().request(request.operation_id)
        self.assertEqual(redirect_store.STATE_REJECTED, settled.state)
        self.assertEqual(redirect_store.REASON_STALE_TARGET, settled.reason)

    def test_a_target_from_another_run_is_refused(self) -> None:
        request = self.submit(target=_target(run_id="run-1"))

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(run_id="run-2"),
                state_dir=self.state_dir,
            )

        self.assertEqual(redirect_store.ERROR_TARGET_MISMATCH, caught.exception.reason)

    def test_a_re_selected_harness_does_not_make_the_turn_a_different_one(self) -> None:
        """Identity is run/session/turn; the harness says what runs it."""
        request = self.submit(target=_target())

        accepted = redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=redirect_store.RedirectTarget(
                "run-1", "session-9", 1, harness="codex"
            ),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)

    def test_a_rejection_requires_a_known_reason(self) -> None:
        request = self.submit()

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.reject_request(
                self.plan_dir,
                request.operation_id,
                reason="invented",
                state_dir=self.state_dir,
            )

        self.assertEqual(redirect_store.ERROR_INVALID_REASON, caught.exception.reason)


class ExpirationTests(RedirectStoreTestCase):
    """An expired correction must not surface in a turn nobody expects."""

    def test_accepting_past_the_deadline_expires_the_request_durably(self) -> None:
        request = self.submit(ttl_seconds=1)
        later = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(),
                state_dir=self.state_dir,
                now=later,
            )

        self.assertEqual(redirect_store.ERROR_REQUEST_EXPIRED, caught.exception.reason)
        settled = self.read().request(request.operation_id)
        self.assertEqual(redirect_store.STATE_EXPIRED, settled.state)
        self.assertEqual(redirect_store.REASON_EXPIRED, settled.reason)

    def test_an_expired_request_stays_refused_after_a_restart(self) -> None:
        """The refusal has to outlive the process that reached it."""
        request = self.submit(ttl_seconds=1)
        later = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)
        redirect_store.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            # A fresh reader with no memory of the sweep, at a moment inside the
            # original window: only the written record can refuse this.
            redirect_store.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(),
                state_dir=self.state_dir,
                now=clock.parse_datetime(request.created_at),
            )

        self.assertEqual(redirect_store.ERROR_REQUEST_EXPIRED, caught.exception.reason)

    def test_a_sweep_expires_only_what_is_due_and_still_open(self) -> None:
        due = self.submit(operation_id="due", ttl_seconds=1)
        fresh = self.submit(operation_id="fresh", ttl_seconds=3600)
        self.submit(operation_id="settled", ttl_seconds=1)
        redirect_store.accept_request(
            self.plan_dir, "settled", target=_target(), state_dir=self.state_dir
        )
        later = clock.parse_datetime(due.expires_at) + timedelta(seconds=1)

        expired = redirect_store.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )

        self.assertEqual(("due",), tuple(r.operation_id for r in expired))
        view = self.read()
        self.assertEqual(
            redirect_store.STATE_SUBMITTED, view.request(fresh.operation_id).state
        )
        self.assertEqual(redirect_store.STATE_ACCEPTED, view.request("settled").state)

    def test_a_repeated_sweep_writes_nothing_further(self) -> None:
        request = self.submit(ttl_seconds=1)
        later = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)
        redirect_store.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )
        lines = len(self.raw_lines())

        self.assertEqual(
            (),
            redirect_store.expire_due_requests(
                self.plan_dir, state_dir=self.state_dir, now=later
            ),
        )
        self.assertEqual(lines, len(self.raw_lines()))

    def test_an_unreadable_deadline_reads_as_expired(self) -> None:
        """A timestamp we cannot evaluate must not mean an open-ended window."""
        request = redirect_store.RedirectRequest(
            operation_id="op",
            target=_target(),
            state=redirect_store.STATE_SUBMITTED,
            created_at=clock.utc_iso_now(),
            expires_at="not-a-timestamp",
            feedback_bytes=1,
            feedback_digest="d",
            submitted_seq=1,
        )

        self.assertTrue(request.is_expired_at(clock.utc_now()))


class ReplayTests(RedirectStoreTestCase):
    """What the reducer does with lines it cannot or must not apply."""

    def test_a_torn_tail_is_reported_and_never_blocks_a_later_record(self) -> None:
        request = self.submit()
        with self.journal_path().open("a", encoding="utf-8") as handle:
            handle.write('{"version":1,"kind":"acce')

        accepted = redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(3, accepted.settled_seq)
        view = self.read()
        self.assertEqual(
            [redirect_store.REJECT_INVALID_JSON], [r.reason for r in view.rejected]
        )
        self.assertEqual([2], [r.seq for r in view.rejected])

    def test_a_rejected_line_is_reported_without_its_bytes(self) -> None:
        """A malformed submission still carries the operator's words."""
        self.append_raw(
            json.dumps(
                {
                    "version": 1,
                    "kind": "submitted",
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "run_id": "run-1",
                    "session_id": "session-9",
                    "turn_index": 0,
                    "expires_at": clock.utc_iso_now(),
                    "text": "confidential correction text",
                }
            )
        )

        view = self.read()

        self.assertEqual(
            [redirect_store.REJECT_INVALID_TARGET], [r.reason for r in view.rejected]
        )
        self.assertNotIn("confidential", json.dumps(view.to_payload()))

    def test_a_duplicate_submission_line_does_not_replace_the_first(self) -> None:
        first = self.submit(operation_id="op", feedback="the real correction")
        line = self.raw_lines()[0]
        self.append_raw(json.dumps({**line, "text": "a forged replacement"}))

        view = self.read()

        self.assertEqual(1, len(view.requests))
        self.assertEqual(first.feedback_digest, view.request("op").feedback_digest)
        self.assertEqual(
            [redirect_store.REJECT_DUPLICATE_ID], [r.reason for r in view.rejected]
        )
        self.assertEqual(
            "the real correction",
            redirect_store.read_feedback(self.plan_dir, "op", state_dir=self.state_dir),
        )

    def test_a_second_terminal_line_never_revises_the_answer(self) -> None:
        """Two racing writers cannot make one request redirect two turns."""
        self.submit(operation_id="op")
        redirect_store.accept_request(
            self.plan_dir, "op", target=_target(), state_dir=self.state_dir
        )
        self.append_raw(
            json.dumps(
                {
                    "version": 1,
                    "kind": "rejected",
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "reason": redirect_store.REASON_RUN_ENDED,
                }
            )
        )

        view = self.read()

        self.assertEqual(redirect_store.STATE_ACCEPTED, view.request("op").state)
        self.assertEqual(
            [redirect_store.REJECT_ALREADY_SETTLED], [r.reason for r in view.rejected]
        )

    def test_a_settlement_for_an_unknown_request_is_not_applied(self) -> None:
        self.append_raw(
            json.dumps(
                {
                    "version": 1,
                    "kind": "accepted",
                    "id": "ghost",
                    "created_at": clock.utc_iso_now(),
                }
            )
        )

        view = self.read()

        self.assertEqual((), view.requests)
        self.assertEqual(
            [redirect_store.REJECT_UNKNOWN_REQUEST], [r.reason for r in view.rejected]
        )

    def test_unsupported_versions_and_kinds_are_rejected_as_lines(self) -> None:
        base = {"id": "op", "created_at": clock.utc_iso_now()}
        self.append_raw(json.dumps({**base, "version": 99, "kind": "submitted"}))
        self.append_raw(json.dumps({**base, "version": 1, "kind": "invented"}))
        self.append_raw(
            json.dumps(
                {**base, "version": 1, "kind": "submitted", "created_at": "nope"}
            )
        )
        self.append_raw("[]")
        self.append_raw("   ")

        view = self.read()

        self.assertEqual(
            [
                redirect_store.REJECT_UNSUPPORTED_VERSION,
                redirect_store.REJECT_UNKNOWN_KIND,
                redirect_store.REJECT_INVALID_TIMESTAMP,
                redirect_store.REJECT_NOT_OBJECT,
                redirect_store.REJECT_EMPTY_LINE,
            ],
            [r.reason for r in view.rejected],
        )
        self.assertEqual((), view.requests)

    def test_an_invalid_line_consumes_a_sequence_rather_than_shifting_one(self) -> None:
        self.append_raw("not json at all")
        request = self.submit()

        self.assertEqual(2, request.submitted_seq)
        self.assertEqual(2, self.read().last_seq)

    def test_a_rejected_settlement_reason_leaves_the_request_open(self) -> None:
        self.submit(operation_id="op")
        self.append_raw(
            json.dumps(
                {
                    "version": 1,
                    "kind": "rejected",
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "reason": "invented",
                }
            )
        )

        view = self.read()

        self.assertEqual(redirect_store.STATE_SUBMITTED, view.request("op").state)
        self.assertEqual(
            [redirect_store.REJECT_INVALID_REASON], [r.reason for r in view.rejected]
        )


class LaunchDeclarationTests(RedirectStoreTestCase):
    """The line that makes a missing delivery mean something.

    A carry and a delivery bracket a window, and a writer that dies inside it
    leaves the same bytes on disk whether it sent the prompt or not.
    ``launching`` splits that window: before it nothing can have gone out, after
    it nobody reading the file may say so. Every claim here is about which side
    of that line a given shape falls on.
    """

    def carried(self, operation_id: str = "op") -> redirect_store.RedirectRequest:
        self.submit(operation_id=operation_id)
        redirect_store.accept_request(
            self.plan_dir,
            operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )
        return redirect_store.carry_request(
            self.plan_dir,
            operation_id,
            continuation=_target(2),
            state_dir=self.state_dir,
        )

    def launch(self, operation_id: str = "op") -> redirect_store.RedirectRequest:
        return redirect_store.launch_request(
            self.plan_dir,
            operation_id,
            continuation=_target(2),
            state_dir=self.state_dir,
        )

    def test_a_carry_with_no_declaration_is_answerable(self) -> None:
        """The shape automatic recovery is allowed to act on."""
        request = self.carried()

        self.assertTrue(request.carried)
        self.assertFalse(request.launching)
        self.assertFalse(request.launch_ambiguous)
        self.assertEqual(
            ("op",),
            tuple(item.operation_id for item in self.read().strandable_by("run-2")),
        )

    def test_a_declared_launch_with_no_outcome_is_not(self) -> None:
        self.carried()

        self.launch()

        request = self.read().request("op")
        self.assertTrue(request.launching)
        self.assertFalse(request.delivered)
        self.assertTrue(request.launch_ambiguous)
        # The words may already be in front of a worker, so no later run may be
        # handed them: stranding is the automatic path this shape is kept out of.
        self.assertEqual((), self.read().strandable_by("run-2"))

    def test_a_delivery_closes_the_window_it_opened(self) -> None:
        self.carried()
        self.launch()

        redirect_store.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        request = self.read().request("op")
        self.assertTrue(request.delivered)
        self.assertFalse(request.launch_ambiguous)

    def test_the_attempt_that_opened_the_window_may_still_close_it(self) -> None:
        """A live process saying "I did not send it" is evidence; silence is not."""
        self.carried()
        self.launch()

        released = redirect_store.release_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertTrue(released.released)
        self.assertFalse(released.carried)
        self.assertFalse(released.launching)
        self.assertFalse(released.launch_ambiguous)
        # Back in the ordinary recoverable set, exactly as if it had never been
        # declared: a known pre-submission refusal costs the operator nothing.
        self.assertEqual(
            ("op",),
            tuple(item.operation_id for item in self.read().strandable_by("run-2")),
        )

    def test_a_recarry_after_a_release_starts_the_window_closed(self) -> None:
        self.carried()
        self.launch()
        redirect_store.release_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        recarried = redirect_store.carry_request(
            self.plan_dir, "op", continuation=_target(3), state_dir=self.state_dir
        )

        self.assertFalse(recarried.launching)
        self.assertFalse(recarried.launch_ambiguous)

    def test_declaring_the_same_launch_twice_appends_nothing(self) -> None:
        """A retried launch is one launch, and the file is what proves it."""
        self.carried()
        first = self.launch()

        second = self.launch()

        self.assertEqual(first.launching_seq, second.launching_seq)
        self.assertEqual(4, len(self.raw_lines()))

    def test_a_declaration_after_the_delivery_it_precedes_is_a_no_op(self) -> None:
        self.carried()
        self.launch()
        redirect_store.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        again = self.launch()

        self.assertTrue(again.delivered)
        self.assertEqual(5, len(self.raw_lines()))

    def test_a_declaration_naming_another_attempt_is_refused(self) -> None:
        self.carried()

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.launch_request(
                self.plan_dir, "op", continuation=_target(3), state_dir=self.state_dir
            )

        self.assertEqual(
            redirect_store.ERROR_CONTINUATION_MISMATCH, caught.exception.reason
        )
        self.assertFalse(self.read().request("op").launching)

    def test_a_declaration_for_an_uncarried_request_is_refused(self) -> None:
        self.submit(operation_id="op")
        redirect_store.accept_request(
            self.plan_dir, "op", target=_target(), state_dir=self.state_dir
        )

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.launch()

        self.assertEqual(redirect_store.ERROR_NOT_CARRIED, caught.exception.reason)

    def test_replay_refuses_a_second_declaration_written_by_hand(self) -> None:
        """The appender's idempotence is not a rule replay may take on trust."""
        self.carried()
        self.launch()

        self.append_raw(
            json.dumps(
                {
                    "version": redirect_store.REDIRECT_JOURNAL_VERSION,
                    "kind": redirect_store.KIND_LAUNCHING,
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

        view = self.read()
        self.assertEqual(
            [redirect_store.REJECT_ALREADY_LAUNCHING],
            [item.reason for item in view.rejected],
        )
        self.assertEqual(4, view.request("op").launching_seq)

    def test_replay_refuses_a_declaration_after_the_launch_happened(self) -> None:
        self.carried()
        self.launch()
        redirect_store.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.append_raw(
            json.dumps(
                {
                    "version": redirect_store.REDIRECT_JOURNAL_VERSION,
                    "kind": redirect_store.KIND_LAUNCHING,
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

        self.assertEqual(
            [redirect_store.REJECT_ALREADY_DELIVERED],
            [item.reason for item in self.read().rejected],
        )

    def test_a_settled_correction_is_never_launch_ambiguous(self) -> None:
        """Ambiguity is about an open question, not about a missing line."""
        self.carried()
        self.launch()
        redirect_store.acknowledge_request(
            self.plan_dir, "op", attempt=_target(2), state_dir=self.state_dir
        )

        request = self.read().request("op")
        self.assertTrue(request.acknowledged)
        self.assertFalse(request.launch_ambiguous)


class RecordVersionTests(RedirectStoreTestCase):
    """What a line asserts depends on the version its writer stamped on it.

    ``continued`` is the record whose meaning moved, twice. Version 1 appended
    it once the worker holding the words was up, so the line *is* a delivery.
    Version 2 appends it while the prompt is still being assembled, so the same
    line is an undertaking -- but that writer had no way to say when the prompt
    was about to go out, so its silence afterwards settles nothing. Version 3
    has ``launching``, which is what finally makes a missing delivery mean the
    prompt never left. Replay has nothing but the version to tell the three
    apart, and getting it wrong in any direction loses a correction: read a
    version-1 carry as an undertaking and an upgraded plan hands a worker words
    it already had, read a version-2 carry as provably unsent and a prompt that
    may have gone out is sent again.
    """

    def accepted(self, operation_id: str = "op") -> redirect_store.RedirectRequest:
        self.submit(operation_id=operation_id)
        return redirect_store.accept_request(
            self.plan_dir,
            operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

    def append_legacy(self, kind: str, operation_id: str = "op") -> None:
        """Append the line a pre-split writer would have written."""
        self.append_raw(
            json.dumps(
                {
                    "version": redirect_store.REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION,
                    "kind": kind,
                    "id": operation_id,
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

    def test_every_line_a_writer_appends_names_the_current_version(self) -> None:
        self.accepted()
        redirect_store.carry_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )
        redirect_store.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertEqual(
            [redirect_store.REDIRECT_JOURNAL_VERSION] * 4,
            [line["version"] for line in self.raw_lines()],
        )

    def test_a_legacy_carry_replays_as_the_delivery_it_recorded(self) -> None:
        self.accepted()

        self.append_legacy("continued")

        stored = self.read().request("op")
        self.assertEqual((), self.read().rejected)
        self.assertTrue(stored.carried)
        self.assertTrue(stored.delivered)
        self.assertTrue(stored.delivered_legacy)
        self.assertTrue(stored.reached_a_worker)
        # One line, so one moment: the carry and the launch it stood for.
        self.assertEqual(stored.continued_at, stored.delivered_at)
        self.assertEqual(stored.continued_seq, stored.delivered_seq)

    def append_unlaunchable(self, kind: str, operation_id: str = "op") -> None:
        """Append the line a writer with the split but no launch record wrote."""
        self.append_raw(
            json.dumps(
                {
                    "version": redirect_store.REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION,
                    "kind": kind,
                    "id": operation_id,
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

    def test_a_current_carry_is_an_undertaking_until_the_launch_is_written(
        self,
    ) -> None:
        """The distinction the version bump exists to preserve."""
        self.accepted()

        redirect_store.carry_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        stored = self.read().request("op")
        self.assertTrue(stored.carried)
        self.assertFalse(stored.delivered)
        self.assertFalse(stored.delivered_legacy)
        self.assertFalse(stored.reached_a_worker)
        # And an undertaking whose writer *could* have declared the launch, so
        # the absence of that declaration is evidence rather than a gap.
        self.assertFalse(stored.launch_unrecorded)
        self.assertFalse(stored.launch_ambiguous)

    def test_a_pre_launch_version_carry_settles_nothing_by_its_silence(self) -> None:
        """The version-2 shape this repair exists to stop misreading.

        Its writer had no ``launching`` line, so "carried and nothing since"
        covers both a prompt that never went out and one that did. Reading it
        the first way is how a correction reaches a second worker.
        """
        self.accepted()

        self.append_unlaunchable("continued")

        stored = self.read().request("op")
        self.assertEqual((), self.read().rejected)
        self.assertTrue(stored.carried)
        self.assertFalse(stored.delivered)
        self.assertTrue(stored.launch_unrecorded)
        self.assertTrue(stored.launch_ambiguous)
        self.assertEqual((), self.read().strandable_by("run-2"))

    def test_a_pre_launch_version_carry_is_still_its_attempt_to_settle(self) -> None:
        """Ambiguous to a reader, not to the process that wrote it.

        An upgraded runner replaying its predecessor's carry may not act on it,
        but nothing about the older line stops a delivery or a release from
        being recorded against it in the ordinary way.
        """
        self.accepted()
        self.append_unlaunchable("continued")

        released = redirect_store.release_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertTrue(released.released)
        self.assertFalse(released.launch_unrecorded)
        self.assertFalse(released.launch_ambiguous)

    def test_a_line_whose_version_is_not_a_version_is_refused(self) -> None:
        """``True == 1``, and the legacy branch must not be reachable that way."""
        self.accepted()
        self.append_raw(
            json.dumps(
                {
                    "version": True,
                    "kind": "continued",
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "continuation": _target(2).to_payload(),
                }
            )
        )

        view = self.read()

        self.assertEqual(
            [redirect_store.REJECT_UNSUPPORTED_VERSION],
            [r.reason for r in view.rejected],
        )
        self.assertFalse(view.request("op").carried)
        self.assertFalse(view.request("op").delivered)

    def test_a_legacy_carry_can_never_be_released(self) -> None:
        """Releasing is the undo of a carry nothing saw, and this one was seen."""
        self.accepted()
        self.append_legacy("continued")

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.release_request(
                self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
            )

        self.assertEqual(
            redirect_store.ERROR_ALREADY_DELIVERED, caught.exception.reason
        )
        self.assertTrue(self.read().request("op").carried)

    def test_a_legacy_receipt_still_settles_its_correction(self) -> None:
        """An upgrade must not invalidate the receipts already on the file."""
        self.accepted()
        self.append_legacy("continued")

        self.append_legacy("acknowledged")

        stored = self.read().request("op")
        self.assertEqual((), self.read().rejected)
        self.assertTrue(stored.acknowledged)
        self.assertTrue(stored.delivered)

    def test_a_legacy_carry_leaves_no_room_for_a_second_delivery(self) -> None:
        """A mixed-version journal records one launch per carry, not two.

        ``deliver_request`` is idempotent rather than a refusal, so what proves
        the point is the file: the legacy line already said what this call
        would say, and nothing is appended over it.
        """
        self.accepted()
        self.append_legacy("continued")

        delivered = redirect_store.deliver_request(
            self.plan_dir, "op", continuation=_target(2), state_dir=self.state_dir
        )

        self.assertTrue(delivered.delivered_legacy)
        self.assertEqual(3, len(self.raw_lines()))


class BoundsTests(RedirectStoreTestCase):
    """A full journal must still be able to answer what is already in it."""

    def test_a_submission_stops_short_of_the_cap(self) -> None:
        limit = (
            redirect_store.REDIRECT_JOURNAL_MAX_BYTES
            - redirect_store.REDIRECT_SETTLEMENT_RESERVE_BYTES
        )
        self.journal_path().parent.mkdir(parents=True, exist_ok=True)
        self.journal_path().write_bytes(b"x\n" * (limit // 2))

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.submit()

        self.assertEqual(redirect_store.ERROR_JOURNAL_FULL, caught.exception.reason)

    def test_the_reserve_keeps_a_pending_request_settleable(self) -> None:
        """The failure this reserve exists to prevent is an unanswerable request."""
        request = self.submit(operation_id="op")
        padding = (
            redirect_store.REDIRECT_JOURNAL_MAX_BYTES
            - redirect_store.REDIRECT_SETTLEMENT_RESERVE_BYTES
            - self.journal_path().stat().st_size
        )
        with self.journal_path().open("ab") as handle:
            handle.write(b"x\n" * (padding // 2))

        with self.assertRaises(redirect_store.RedirectStoreError):
            self.submit(operation_id="op-2", feedback="a second correction")
        accepted = redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)

    def test_a_failed_append_leaves_the_journal_exactly_as_it_was(self) -> None:
        self.submit(operation_id="op")
        before = self.journal_path().read_bytes()

        with self.assertRaises(redirect_store.RedirectStoreError):
            self.submit(
                operation_id="op-2",
                feedback="x" * (redirect_store.REDIRECT_MAX_FEEDBACK_BYTES + 1),
            )

        self.assertEqual(before, self.journal_path().read_bytes())


class ConcurrencyTests(RedirectStoreTestCase):
    """Concurrent producers must not lose, merge, or duplicate a correction."""

    def test_concurrent_submissions_all_survive_with_distinct_sequences(self) -> None:
        count = 24

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            requests = list(
                pool.map(
                    lambda index: self.submit(feedback=f"correction {index}"),
                    range(count),
                )
            )

        self.assertEqual(count, len({r.operation_id for r in requests}))
        self.assertEqual(count, len({r.submitted_seq for r in requests}))
        view = self.read()
        self.assertEqual(count, len(view.requests))
        self.assertEqual((), view.rejected)
        self.assertEqual(
            {f"correction {index}" for index in range(count)},
            {
                redirect_store.read_feedback(
                    self.plan_dir, r.operation_id, state_dir=self.state_dir
                )
                for r in requests
            },
        )

    def test_concurrent_retries_of_one_id_produce_one_request(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _index: self.submit(operation_id="op"), range(16))
            )

        self.assertEqual({1}, {r.submitted_seq for r in results})
        self.assertEqual(1, len(self.raw_lines()))

    def test_concurrent_acceptances_settle_a_request_exactly_once(self) -> None:
        """The race that would otherwise redirect two turns from one request."""
        request = self.submit(operation_id="op")

        def attempt(_index: int) -> str:
            try:
                return redirect_store.accept_request(
                    self.plan_dir, "op", target=_target(), state_dir=self.state_dir
                ).state
            except redirect_store.RedirectStoreError as error:
                return error.reason

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(16)))

        self.assertEqual({redirect_store.STATE_ACCEPTED}, set(outcomes))
        kinds = [line["kind"] for line in self.raw_lines()]
        self.assertEqual(1, kinds.count(redirect_store.KIND_ACCEPTED))
        self.assertEqual(
            redirect_store.STATE_ACCEPTED,
            self.read().request(request.operation_id).state,
        )

    def test_an_acceptance_racing_an_expiry_yields_one_terminal_record(self) -> None:
        request = self.submit(operation_id="op", ttl_seconds=1)
        past = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)

        def accept(_index: int) -> str:
            try:
                return redirect_store.accept_request(
                    self.plan_dir, "op", target=_target(), state_dir=self.state_dir
                ).state
            except redirect_store.RedirectStoreError as error:
                return error.reason

        def expire(_index: int) -> str:
            return str(
                len(
                    redirect_store.expire_due_requests(
                        self.plan_dir, state_dir=self.state_dir, now=past
                    )
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: (accept(index), expire(index)), range(12)))

        kinds = [line["kind"] for line in self.raw_lines()]
        terminal = [k for k in kinds if k != redirect_store.KIND_SUBMITTED]
        self.assertEqual(1, len(terminal))
        self.assertIn(
            self.read().request("op").state,
            (redirect_store.STATE_ACCEPTED, redirect_store.STATE_EXPIRED),
        )


class DurabilityTests(RedirectStoreTestCase):
    """The append is flushed before the caller is told it happened."""

    def test_every_completed_append_is_fullsynced(self) -> None:
        synced: list[int] = []
        original = redirect_store.durability.fullsync

        def record(fd: int) -> None:
            synced.append(fd)
            original(fd)

        redirect_store.durability.fullsync = record
        self.addCleanup(setattr, redirect_store.durability, "fullsync", original)

        request = self.submit()
        redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertEqual(2, len(synced))

    def test_a_short_write_is_rolled_back_rather_than_left_torn(self) -> None:
        self.submit(operation_id="op")
        before = self.journal_path().read_bytes()
        journal_inode = self.journal_path().stat().st_ino
        original = os.write

        def truncated_write(fd: int, data: bytes) -> int:
            # Scoped to this journal by inode. `redirect_store.os` is the real
            # `os` module, so an unconditional patch would truncate the test
            # runner's own writes for the duration of this block.
            if os.fstat(fd).st_ino != journal_inode:
                return original(fd, data)
            return original(fd, data[: len(data) // 2])

        os.write = truncated_write
        self.addCleanup(setattr, os, "write", original)

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.submit(operation_id="op-2", feedback="second correction")

        os.write = original
        self.assertEqual(redirect_store.ERROR_JOURNAL_WRITE, caught.exception.reason)
        self.assertEqual(before, self.journal_path().read_bytes())
        self.assertEqual(1, len(self.read().requests))

    def test_a_failed_append_is_not_reported_as_a_submission(self) -> None:
        """A producer told the write failed must not find it accepted later."""
        original = redirect_store.durability.fullsync

        def failing(_fd: int) -> None:
            raise OSError("device is gone")

        redirect_store.durability.fullsync = failing
        self.addCleanup(setattr, redirect_store.durability, "fullsync", original)

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            self.submit(operation_id="op")

        self.assertEqual(redirect_store.ERROR_JOURNAL_WRITE, caught.exception.reason)
        redirect_store.durability.fullsync = original
        self.assertIsNone(self.read().request("op"))


if __name__ == "__main__":
    unittest.main()
