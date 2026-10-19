# mypy: ignore-errors
"""Focused test_asl_redirect_store test group 2."""

# ruff: noqa: F403, F405
from ._test_asl_redirect_store_support import *


class ExpirationTests(RedirectStoreTestCase):
    """An expired correction must not surface in a turn nobody expects."""

    def test_accepting_past_the_deadline_expires_the_request_durably(self) -> None:
        request = self.submit(ttl_seconds=1)
        later = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(),
                state_dir=self.state_dir,
                now=later,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_REQUEST_EXPIRED, caught.exception.reason
        )
        settled = self.read().request(request.operation_id)
        self.assertEqual(redirect_store_vocab.STATE_EXPIRED, settled.state)
        self.assertEqual(redirect_store_vocab.REASON_EXPIRED, settled.reason)

    def test_an_expired_request_stays_refused_after_a_restart(self) -> None:
        """The refusal has to outlive the process that reached it."""
        request = self.submit(ttl_seconds=1)
        later = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)
        redirect_store_requests.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            # A fresh reader with no memory of the sweep, at a moment inside the
            # original window: only the written record can refuse this.
            redirect_store_requests.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(),
                state_dir=self.state_dir,
                now=clock.parse_datetime(request.created_at),
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_REQUEST_EXPIRED, caught.exception.reason
        )

    def test_a_sweep_expires_only_what_is_due_and_still_open(self) -> None:
        due = self.submit(operation_id="due", ttl_seconds=1)
        fresh = self.submit(operation_id="fresh", ttl_seconds=3600)
        self.submit(operation_id="settled", ttl_seconds=1)
        redirect_store_requests.accept_request(
            self.plan_dir, "settled", target=_target(), state_dir=self.state_dir
        )
        later = clock.parse_datetime(due.expires_at) + timedelta(seconds=1)

        expired = redirect_store_requests.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )

        self.assertEqual(("due",), tuple(r.operation_id for r in expired))
        view = self.read()
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED, view.request(fresh.operation_id).state
        )
        self.assertEqual(
            redirect_store_vocab.STATE_ACCEPTED, view.request("settled").state
        )

    def test_a_repeated_sweep_writes_nothing_further(self) -> None:
        request = self.submit(ttl_seconds=1)
        later = clock.parse_datetime(request.expires_at) + timedelta(seconds=1)
        redirect_store_requests.expire_due_requests(
            self.plan_dir, state_dir=self.state_dir, now=later
        )
        lines = len(self.raw_lines())

        self.assertEqual(
            (),
            redirect_store_requests.expire_due_requests(
                self.plan_dir, state_dir=self.state_dir, now=later
            ),
        )
        self.assertEqual(lines, len(self.raw_lines()))

    def test_an_unreadable_deadline_reads_as_expired(self) -> None:
        """A timestamp we cannot evaluate must not mean an open-ended window."""
        request = redirect_store_models.RedirectRequest(
            operation_id="op",
            target=_target(),
            state=redirect_store_vocab.STATE_SUBMITTED,
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

        accepted = redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(3, accepted.settled_seq)
        view = self.read()
        self.assertEqual(
            [redirect_store_vocab.REJECT_INVALID_JSON],
            [r.reason for r in view.rejected],
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
            [redirect_store_vocab.REJECT_INVALID_TARGET],
            [r.reason for r in view.rejected],
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
            [redirect_store_vocab.REJECT_DUPLICATE_ID],
            [r.reason for r in view.rejected],
        )
        self.assertEqual(
            "the real correction",
            redirect_store_journal.read_feedback(
                self.plan_dir, "op", state_dir=self.state_dir
            ),
        )

    def test_a_second_terminal_line_never_revises_the_answer(self) -> None:
        """Two racing writers cannot make one request redirect two turns."""
        self.submit(operation_id="op")
        redirect_store_requests.accept_request(
            self.plan_dir, "op", target=_target(), state_dir=self.state_dir
        )
        self.append_raw(
            json.dumps(
                {
                    "version": 1,
                    "kind": "rejected",
                    "id": "op",
                    "created_at": clock.utc_iso_now(),
                    "reason": redirect_store_vocab.REASON_RUN_ENDED,
                }
            )
        )

        view = self.read()

        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, view.request("op").state)
        self.assertEqual(
            [redirect_store_vocab.REJECT_ALREADY_SETTLED],
            [r.reason for r in view.rejected],
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
            [redirect_store_vocab.REJECT_UNKNOWN_REQUEST],
            [r.reason for r in view.rejected],
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
                redirect_store_vocab.REJECT_UNSUPPORTED_VERSION,
                redirect_store_vocab.REJECT_UNKNOWN_KIND,
                redirect_store_vocab.REJECT_INVALID_TIMESTAMP,
                redirect_store_vocab.REJECT_NOT_OBJECT,
                redirect_store_vocab.REJECT_EMPTY_LINE,
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

        self.assertEqual(redirect_store_vocab.STATE_SUBMITTED, view.request("op").state)
        self.assertEqual(
            [redirect_store_vocab.REJECT_INVALID_REASON],
            [r.reason for r in view.rejected],
        )
