# mypy: ignore-errors
"""Focused test_asl_redirect_store test group 1."""

# ruff: noqa: F403, F405
from ._test_asl_redirect_store_support import *


class SubmissionTests(RedirectStoreTestCase):
    """Persisting a correction, and what a submission is allowed to be."""

    def test_a_submission_is_durable_pending_and_addressable(self) -> None:
        request = self.submit()

        self.assertEqual(redirect_store_vocab.STATE_SUBMITTED, request.state)
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
            redirect_store_validation.feedback_digest(secret),
            payload["feedback_digest"],
        )
        self.assertEqual(len(secret.encode("utf-8")), payload["feedback_bytes"])

    def test_the_text_is_readable_only_by_naming_the_operation(self) -> None:
        request = self.submit(feedback="use the durable journal")

        self.assertEqual(
            "use the durable journal",
            redirect_store_journal.read_feedback(
                self.plan_dir, request.operation_id, state_dir=self.state_dir
            ),
        )
        self.assertIsNone(
            redirect_store_journal.read_feedback(
                self.plan_dir, "no-such-operation", state_dir=self.state_dir
            )
        )

    def test_empty_and_oversized_feedback_are_refused(self) -> None:
        for feedback in ("", "   \n\t "):
            with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
                self.submit(feedback=feedback)
            self.assertEqual(
                redirect_store_vocab.ERROR_INVALID_FEEDBACK, caught.exception.reason
            )

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.submit(
                feedback="x" * (redirect_store_vocab.REDIRECT_MAX_FEEDBACK_BYTES + 1)
            )
        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_FEEDBACK, caught.exception.reason
        )
        self.assertFalse(self.journal_path().exists())

    def test_a_malformed_target_is_refused_before_anything_is_written(self) -> None:
        for target in (
            redirect_store_models.RedirectTarget("", "session-9", 1),
            redirect_store_models.RedirectTarget("run-1", "", 1),
            redirect_store_models.RedirectTarget("run-1", "session-9", 0),
            redirect_store_models.RedirectTarget("run-1", "session-9", -3),
            redirect_store_models.RedirectTarget("run-1", "x" * 200, 1),
        ):
            with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
                self.submit(target=target)
            self.assertEqual(
                redirect_store_vocab.ERROR_INVALID_TARGET, caught.exception.reason
            )
        self.assertFalse(self.journal_path().exists())

    def test_a_ttl_outside_the_permitted_window_is_refused_not_clamped(self) -> None:
        """Clamping would silently retire a correction earlier than asked."""
        for ttl in (0, -1, redirect_store_vocab.REDIRECT_MAX_TTL_SECONDS + 1):
            with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
                self.submit(ttl_seconds=ttl)
            self.assertEqual(
                redirect_store_vocab.ERROR_INVALID_TTL, caught.exception.reason
            )

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
        redirect_store_requests.accept_request(
            self.plan_dir, "op-1", target=_target(), state_dir=self.state_dir
        )

        replayed = self.submit(operation_id="op-1")

        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, replayed.state)
        self.assertEqual(first.submitted_seq, replayed.submitted_seq)
        self.assertEqual(2, len(self.raw_lines()))

    def test_reusing_an_id_for_different_text_is_refused(self) -> None:
        self.submit(operation_id="op-1", feedback="first correction")

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.submit(operation_id="op-1", feedback="a completely different one")

        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_OPERATION_ID, caught.exception.reason
        )
        self.assertEqual(
            "first correction",
            redirect_store_journal.read_feedback(
                self.plan_dir, "op-1", state_dir=self.state_dir
            ),
        )

    def test_reusing_an_id_for_a_different_turn_is_refused(self) -> None:
        self.submit(operation_id="op-1", target=_target(1))

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            self.submit(operation_id="op-1", target=_target(2))

        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_OPERATION_ID, caught.exception.reason
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

        accepted = redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(2, accepted.settled_seq)
        lines = self.raw_lines()
        self.assertEqual(
            [redirect_store_vocab.KIND_SUBMITTED, redirect_store_vocab.KIND_ACCEPTED],
            [line["kind"] for line in lines],
        )
        self.assertEqual(request.operation_id, lines[0]["id"])

    def test_a_second_acceptance_changes_nothing(self) -> None:
        request = self.submit()
        first = redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        second = redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=_target(),
            state_dir=self.state_dir,
        )

        self.assertEqual(first, second)
        self.assertEqual(2, len(self.raw_lines()))

    def test_a_settled_request_cannot_be_accepted(self) -> None:
        request = self.submit()
        redirect_store_requests.reject_request(
            self.plan_dir,
            request.operation_id,
            reason=redirect_store_vocab.REASON_RUN_ENDED,
            state_dir=self.state_dir,
        )

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_ALREADY_SETTLED, caught.exception.reason
        )

    def test_an_unknown_operation_cannot_be_accepted(self) -> None:
        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                "never-submitted",
                target=_target(),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_UNKNOWN_REQUEST, caught.exception.reason
        )

    def test_a_stale_target_is_refused_and_durably_closed(self) -> None:
        """Advice written for one turn is never re-aimed at its successor."""
        request = self.submit(target=_target(1))

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(2),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_TARGET_MISMATCH, caught.exception.reason
        )
        settled = self.read().request(request.operation_id)
        self.assertEqual(redirect_store_vocab.STATE_REJECTED, settled.state)
        self.assertEqual(redirect_store_vocab.REASON_STALE_TARGET, settled.reason)

    def test_a_target_from_another_run_is_refused(self) -> None:
        request = self.submit(target=_target(run_id="run-1"))

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.accept_request(
                self.plan_dir,
                request.operation_id,
                target=_target(run_id="run-2"),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_TARGET_MISMATCH, caught.exception.reason
        )

    def test_a_re_selected_harness_does_not_make_the_turn_a_different_one(self) -> None:
        """Identity is run/session/turn; the harness says what runs it."""
        request = self.submit(target=_target())

        accepted = redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=redirect_store_models.RedirectTarget(
                "run-1", "session-9", 1, harness="codex"
            ),
            state_dir=self.state_dir,
        )

        self.assertTrue(accepted.accepted)

    def test_a_rejection_requires_a_known_reason(self) -> None:
        request = self.submit()

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_requests.reject_request(
                self.plan_dir,
                request.operation_id,
                reason="invented",
                state_dir=self.state_dir,
            )

        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_REASON, caught.exception.reason
        )
