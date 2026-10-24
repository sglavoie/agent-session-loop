# mypy: ignore-errors
"""Workbench redirect idempotency, replay identity, and status reads."""

# ruff: noqa: F403, F405
from ._test_asl_workbench_redirect_support import *


class RedirectIdempotencyTests(WorkbenchRedirectTestCase):
    """A browser may retry, and a retry is the same correction."""

    def test_a_resent_correction_replays_the_original_receipt(self) -> None:
        self.live_run()
        self.accept_when_pending()
        first_status, first, _ = self.submit(wait_seconds=5.0)
        self.assertEqual(202, first_status)

        status, body, _ = self.submit(wait_seconds=5.0)

        # 200 rather than 202: this call accepted nothing, it recognized a
        # request that was already accepted once.
        self.assertEqual(200, status)
        receipt = body["redirect"]
        self.assertTrue(receipt["replayed"])
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        # No submission happened, so there is no submission outcome to report;
        # the projection carries the truth instead.
        self.assertIsNone(receipt["outcome"])
        self.assertEqual(
            first["redirect"]["operation"]["status"], receipt["operation"]["status"]
        )
        self.assertEqual(1, len(self.journal().requests))

    def test_a_retry_whose_token_went_stale_still_replays(self) -> None:
        """The correction that landed is what made the client's token stale."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.submit(action_token="0" * 32, wait_seconds=5.0)

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assertEqual(1, len(self.journal().requests))

    def test_a_retry_after_the_turn_ended_still_replays(self) -> None:
        """Replay is read from the journal, so it outlives the live turn."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(turn_index=None)

        status, body, _ = self.submit()

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assertEqual(
            redirect_cli.STAGE_STOPPED, body["redirect"]["operation"]["stage"]
        )

    def test_a_different_operation_id_is_a_second_correction(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.accept_when_pending()

        status, body, _ = self.submit(operation_id="wb-op-second", wait_seconds=5.0)

        self.assertEqual(202, status)
        self.assertFalse(body["redirect"]["replayed"])
        self.assertEqual(2, len(self.journal().requests))

    def test_a_retry_to_a_restarted_server_still_replays(self) -> None:
        """Replay is a property of the journal, not of the process that wrote it."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.submit(server=self.start_server())

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assertEqual(1, len(self.journal().requests))


class ReplayIdentityTests(WorkbenchRedirectTestCase):
    """A retry is the same correction, or it is not a retry.

    A lost response leaves the browser holding both halves of the request: the
    operation id, and an editable textarea. Retrying after an edit is the one
    way a second correction can wear a durable id, and answering it with the
    original receipt would clear the edited draft while telling the operator it
    landed — the composer's whole contract, inverted.
    """

    def submitted(self) -> None:
        """One accepted correction under the default operation id."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

    def assert_journal_unchanged(self) -> None:
        requests = self.journal().requests
        self.assertEqual(1, len(requests))
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            requests[0].feedback_digest,
        )
        self.assertEqual(self.target(), requests[0].target)
        self.assertEqual(
            FEEDBACK,
            redirect_store_journal.read_feedback(
                self.plan_dir, OPERATION_ID, state_dir=self.state_dir
            ),
        )

    def test_an_edited_draft_under_the_same_id_is_refused(self) -> None:
        self.submitted()

        status, body, _ = self.submit(text=EDITED_FEEDBACK, wait_seconds=5.0)

        # 409, not 400: the body is well formed and what it contradicts is a
        # durable record, which a client resolves by reconciling the id.
        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assertEqual(
            redirect_store_vocab.CONFLICT_FEEDBACK, body["details"]["conflict"]
        )
        self.assertFalse(body["retryable"])
        self.assert_journal_unchanged()

    def test_the_refusal_hands_back_the_recorded_correction(self) -> None:
        """What the composer needs to offer reconciliation: the durable record."""
        self.submitted()

        _, body, _ = self.submit(text=EDITED_FEEDBACK, wait_seconds=5.0)

        receipt = body["details"]["redirect"]
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        self.assertTrue(receipt["replayed"])
        # The digest of the correction that landed, so a browser can say which
        # one it is refusing to overwrite without ever holding its words.
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            receipt["operation"]["feedback_digest"],
        )

    def test_a_retry_naming_a_different_turn_is_refused(self) -> None:
        """The id is bound to the turn its submission was accepted against."""
        self.submitted()

        status, body, _ = self.submit(
            expected_target=self.target(turn_index=2).to_payload(), wait_seconds=5.0
        )

        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assertEqual(
            redirect_store_vocab.CONFLICT_TARGET, body["details"]["conflict"]
        )
        self.assert_journal_unchanged()

    def test_a_retry_naming_the_same_turn_still_replays(self) -> None:
        """A client that named its turn must be able to name it again."""
        self.submitted()

        status, body, _ = self.submit(
            expected_target=self.target().to_payload(), wait_seconds=5.0
        )

        self.assertEqual(200, status)
        self.assertTrue(body["redirect"]["replayed"])
        self.assert_journal_unchanged()

    def test_a_refusal_never_quotes_either_correction(self) -> None:
        """Both are the operator's words; neither belongs in a response."""
        self.submitted()

        _, body, _ = self.submit(text=EDITED_FEEDBACK, wait_seconds=5.0)

        rendered = json.dumps(body)
        self.assertNotIn(FEEDBACK, rendered)
        self.assertNotIn(EDITED_FEEDBACK, rendered)

    def test_an_edited_draft_is_refused_by_a_restarted_server_too(self) -> None:
        """The check reads the journal, so it outlives the process that wrote it."""
        self.submitted()

        status, body, _ = self.submit(
            server=self.start_server(), text=EDITED_FEEDBACK, wait_seconds=5.0
        )

        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assert_journal_unchanged()

    def test_an_edited_draft_is_refused_after_the_turn_ended(self) -> None:
        """Turn completion is what makes the original unrecallable, not writable."""
        self.submitted()
        self.write_state(turn_index=None)

        status, body, _ = self.submit(text=EDITED_FEEDBACK)

        self.assertEqual(409, status)
        self.assertEqual(redirect_store_vocab.ERROR_REPLAY_CONFLICT, body["reason"])
        self.assert_journal_unchanged()

    def test_a_new_operation_id_sends_the_edited_draft(self) -> None:
        """The way out the composer offers: a second correction, said so."""
        self.submitted()
        self.accept_when_pending()

        status, body, _ = self.submit(
            operation_id="wb-op-edited", text=EDITED_FEEDBACK, wait_seconds=5.0
        )

        self.assertEqual(202, status)
        self.assertFalse(body["redirect"]["replayed"])
        digests = {request.feedback_digest for request in self.journal().requests}
        self.assertEqual(
            {
                redirect_store_validation.feedback_digest(FEEDBACK),
                redirect_store_validation.feedback_digest(EDITED_FEEDBACK),
            },
            digests,
        )


class RedirectStatusTests(WorkbenchRedirectTestCase):
    """What the status read establishes, and what it hands a composer."""

    def test_the_read_reports_the_journal_and_the_live_turn(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.status()

        self.assertEqual(200, status)
        self.assertEqual(str(self.canonical_plan_dir), body["plan_dir"])
        self.assertEqual(RUN_ID, body["run_id"])
        self.assertEqual("live", body["owner"])
        # Read from the lock rather than from the state file, which is what makes
        # "who holds this plan" independent of what a run claims about itself.
        # This fixture's lock names a pid and no run, and the answer says so.
        self.assertIsNone(body["owner_run_id"])
        self.assertEqual(self.target().to_payload(), body["live_target"])
        self.assertEqual(
            [OPERATION_ID], [row["operation_id"] for row in body["operations"]]
        )
        self.assertEqual(
            [redirect_cli.STAGE_INTERRUPTING],
            [row["stage"] for row in body["operations"]],
        )
        self.assertEqual([], body["uncertain_operation_ids"])
        self.assertTrue(body["journal_complete"])
        self.assertFalse(body["torn_tail"])
        self.assertFalse(body["truncated"])
        self.assertEqual(0, body["unreadable_lines"])
        self.assertTrue(body["checked_at"])

    def test_the_read_hands_out_the_token_a_correction_needs(self) -> None:
        """A composer never has to derive its own freshness proof."""
        self.live_run()

        _, body, _ = self.status()

        self.assertEqual(self.action_token(), body["action_token"])
        self.assertIn(intervention.ACTION_REDIRECT, body["permitted_actions"])
        self.assertNotIn(intervention.ACTION_REDIRECT, body["disabled_reasons"])

    def test_the_read_explains_why_a_correction_is_not_permitted(self) -> None:
        self.write_state(turn_index=None)

        _, body, _ = self.status()

        self.assertNotIn(intervention.ACTION_REDIRECT, body["permitted_actions"])
        self.assertIn(intervention.ACTION_REDIRECT, body["disabled_reasons"])

    def test_the_read_filters_by_operation_id(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.accept_when_pending()
        self.submit(operation_id="wb-op-second", wait_seconds=5.0)

        _, body, _ = self.status(params=(("operation_id", "wb-op-second"),))

        self.assertEqual(
            ["wb-op-second"], [row["operation_id"] for row in body["operations"]]
        )

    def test_the_filter_accepts_several_ids(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.accept_when_pending()
        self.submit(operation_id="wb-op-second", wait_seconds=5.0)

        _, body, _ = self.status(
            params=(
                ("operation_id", OPERATION_ID),
                ("operation_id", "wb-op-second"),
            )
        )

        self.assertEqual(
            [OPERATION_ID, "wb-op-second"],
            [row["operation_id"] for row in body["operations"]],
        )

    def test_a_second_server_reports_the_correction_from_the_journal(self) -> None:
        """A restart, from the browser's side: the answer is not in this process."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        status, body, _ = self.status(server=self.start_server())

        self.assertEqual(200, status)
        self.assertEqual(
            [OPERATION_ID], [row["operation_id"] for row in body["operations"]]
        )

    def test_a_plan_with_no_corrections_reports_an_empty_history(self) -> None:
        """Nothing submitted is an answer in itself, not an error."""
        self.write_state()

        status, body, _ = self.status()

        self.assertEqual(200, status)
        self.assertEqual([], body["operations"])
        self.assertEqual([], body["uncertain_operation_ids"])
        self.assertTrue(body["journal_complete"])
        # No lock, so nothing is answerable for this journal.
        self.assertEqual("gone", body["owner"])

    def test_an_unreadable_journal_is_retryable_on_every_redirect_surface(self) -> None:
        """A journal this machine will not open is not a plan without history.

        Answering 200 with an empty history would be the one wrong answer that
        looks like a right one, and answering 4xx would tell a client to stop
        asking. Both reads and the submission start from the same journal read,
        so all three have to fail the same retryable way.
        """
        self.live_run()
        self.submit()
        journal = self.journal().journal_path
        self.addCleanup(journal.chmod, 0o600)
        journal.chmod(0o000)

        for label, call in (
            ("redirect-status", self.status),
            ("conversation-recovery", self.recovery),
            ("control/redirect", lambda: self.submit(operation_id="wb-op-second")),
        ):
            with self.subTest(surface=label):
                status, body, _ = call()
                self.assertEqual(503, status)
                self.assertEqual("redirect-unavailable", body["reason"])
                self.assertTrue(body["retryable"])
