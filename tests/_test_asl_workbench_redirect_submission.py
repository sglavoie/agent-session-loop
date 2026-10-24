# mypy: ignore-errors
"""Workbench redirect routing, authorization, submission, and refusals."""

# ruff: noqa: F403, F405
from ._test_asl_workbench_redirect_support import *


class RoutingTests(WorkbenchRedirectTestCase):
    """Which method reaches which of the three endpoints."""

    def test_submitting_a_correction_is_post_only(self) -> None:
        self.live_run()
        status, body, _ = self.get(
            "/api/control/redirect",
            params=(("plan_dir", str(self.plan_dir)),),
        )
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_the_two_reads_are_get_only(self) -> None:
        self.write_state()
        for path in ("/api/redirect-status", "/api/conversation-recovery"):
            with self.subTest(path=path):
                status, body, _ = self.post(path, {"plan_dir": str(self.plan_dir)})
                self.assertEqual(405, status)
                self.assertEqual("method-not-allowed", body["reason"])

    def test_the_reads_require_a_plan_dir(self) -> None:
        for path in ("/api/redirect-status", "/api/conversation-recovery"):
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(400, status)
                self.assertIn("plan_dir", body["error"])


class RedirectAuthorizationTests(WorkbenchRedirectTestCase):
    """The two gates a correction passes, and what a refusal must not write."""

    def test_an_unauthenticated_correction_is_refused_and_writes_nothing(self) -> None:
        self.live_run()

        status, body, _ = self.submit(token=False)

        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_stale_action_token_is_a_conflict_and_writes_nothing(self) -> None:
        self.live_run()

        status, body, _ = self.submit(action_token="0" * 32)

        # 409 rather than 400: the body is fine, the caller's picture of the run
        # is not, and re-reading the plan is the fix.
        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_STALE_ACTION_TOKEN, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_run_with_no_live_turn_permits_no_correction(self) -> None:
        """A valid token against a run that is not on a turn is still refused."""
        self.write_state(turn_index=None)
        self.write_lock()

        status, body, _ = self.submit()

        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_ACTION_NOT_PERMITTED, body["reason"])
        self.assertEqual((), self.journal().requests)
        # The refused action travels with the refusal, and the message is the
        # shared service's own explanation rather than one written here.
        self.assertEqual(intervention.ACTION_REDIRECT, body["details"]["action"])
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            intervention.disabled_reasons(facts)[intervention.ACTION_REDIRECT],
            body["error"],
        )

    def test_a_run_id_the_caller_names_must_match(self) -> None:
        self.live_run()

        status, body, _ = self.submit(run_id="20000101T000000Z-9")

        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_RUN_MISMATCH, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_the_status_read_needs_no_token(self) -> None:
        """The client most in need of this read is one that lost its own record."""
        self.live_run()

        status, body, _ = self.status()

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])


class RedirectSubmissionTests(WorkbenchRedirectTestCase):
    """An authorized correction reaches the journal and is reported truthfully."""

    def test_a_correction_against_the_live_turn_is_accepted(self) -> None:
        self.live_run()
        self.accept_when_pending()

        status, body, _ = self.submit(wait_seconds=5.0)

        self.assertEqual(202, status)
        self.assertTrue(body["accepted"])
        receipt = body["redirect"]
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        self.assertEqual(redirect_admission.OUTCOME_ACCEPTED, receipt["outcome"])
        self.assertIsNone(receipt["refusal"])
        self.assertFalse(receipt["replayed"])
        self.assertEqual(str(self.canonical_plan_dir), receipt["plan_dir"])
        self.assertEqual(RUN_ID, receipt["run_id"])
        self.assertEqual(self.target().to_payload(), receipt["target"])
        self.assertTrue(receipt["journal_complete"])
        assert self.wakeup is not None
        self.assertTrue(self.wakeup.is_set())

    def test_the_receipt_reports_the_stage_the_journal_supports(self) -> None:
        """The projection comes back from a re-read, not from the outcome."""
        self.live_run()
        self.accept_when_pending()

        _, body, _ = self.submit(wait_seconds=5.0)

        operation = body["redirect"]["operation"]
        self.assertEqual(redirect_cli.STAGE_INTERRUPTING, operation["stage"])
        self.assertEqual(
            redirect_cli.STAGE_DESCRIPTIONS[redirect_cli.STAGE_INTERRUPTING],
            operation["stage_description"],
        )

    def test_the_correction_reaches_the_durable_journal(self) -> None:
        self.live_run()
        self.accept_when_pending()

        self.submit(wait_seconds=5.0)

        requests = self.journal().requests
        self.assertEqual(1, len(requests))
        self.assertEqual(OPERATION_ID, requests[0].operation_id)
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            requests[0].feedback_digest,
        )

    def test_a_persisted_but_unaccepted_correction_is_still_a_receipt(self) -> None:
        """No acceptance inside the wait is not a refusal: the id is the answer."""
        self.live_run()

        status, body, _ = self.submit(wait_seconds=0.05)

        self.assertEqual(202, status)
        self.assertFalse(body["accepted"])
        receipt = body["redirect"]
        self.assertEqual(OPERATION_ID, receipt["operation_id"])
        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, receipt["outcome"])
        self.assertEqual(1, len(self.journal().requests))

    def test_no_response_carries_the_operators_words(self) -> None:
        """Digest and byte count travel; the text stays behind ``read_feedback``."""
        self.live_run()
        self.accept_when_pending()

        _, submitted, _ = self.submit(wait_seconds=5.0)
        _, listed, _ = self.status()
        self.write_state(paused=True, pause_reason="conversation-recovery-required")
        _, recovered, _ = self.recovery()

        for label, payload in (
            ("receipt", submitted),
            ("status", listed),
            ("recovery", recovered),
        ):
            with self.subTest(response=label):
                self.assertNotIn(FEEDBACK, json.dumps(payload))
        # And the words are in the journal all along, so their absence above is
        # a fact about the frames rather than about a lost correction.
        self.assertEqual(
            FEEDBACK,
            redirect_store_journal.read_feedback(
                self.plan_dir, OPERATION_ID, state_dir=self.state_dir
            ),
        )


class RedirectRefusalTests(WorkbenchRedirectTestCase):
    """Refusals that never reach the journal, and never read as accepted."""

    def test_a_correction_with_no_text_is_refused(self) -> None:
        self.live_run()

        status, body, _ = self.submit(text="   \n ")

        self.assertEqual(400, status)
        self.assertEqual(intervention.REASON_EMPTY_TEXT, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_oversized_correction_is_the_clients_error(self) -> None:
        """The journal's feedback cap is reported as a 400, not as a 500."""
        self.live_run()
        oversized = "x" * (redirect_store_vocab.REDIRECT_MAX_FEEDBACK_BYTES + 1)

        status, body, _ = self.submit(text=oversized)

        self.assertEqual(400, status)
        self.assertEqual(redirect_store_vocab.ERROR_INVALID_FEEDBACK, body["reason"])
        self.assertEqual(
            redirect_store_vocab.REDIRECT_MAX_FEEDBACK_BYTES,
            body["details"]["max_feedback_bytes"],
        )
        self.assertEqual((), self.journal().requests)

    def test_an_overlong_operation_id_is_the_clients_error(self) -> None:
        """Every journal field cap is a 400, not a retryable server fault."""
        self.live_run()
        oversized = "o" * (redirect_store_vocab.REDIRECT_MAX_OPERATION_ID_CHARS + 1)

        status, body, _ = self.submit(operation_id=oversized)

        self.assertEqual(400, status)
        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_OPERATION_ID, body["reason"]
        )
        # Telling a client to retry a key that can never be accepted is the one
        # unhelpful answer available here.
        self.assertFalse(body.get("retryable", False))
        self.assertEqual((), self.journal().requests)

    def test_a_correction_naming_a_turn_that_moved_on_is_refused(self) -> None:
        """The race a composer actually loses: the turn ended while typing."""
        self.live_run()

        status, body, _ = self.submit(
            expected_target=self.target(turn_index=7).to_payload()
        )

        self.assertEqual(409, status)
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, body["reason"])
        self.assertEqual((), self.journal().requests)
        # The receipt travels with the refusal and says nothing was persisted,
        # so a client cannot come away with an id to ask after.
        self.assertIsNone(body["details"]["redirect"]["operation_id"])

    def test_a_correction_to_a_plan_with_no_run_is_refused(self) -> None:
        status, body, _ = self.submit()

        self.assertEqual(409, status)
        self.assertEqual(intervention.REASON_ACTION_NOT_PERMITTED, body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_out_of_range_wait_is_refused(self) -> None:
        self.live_run()

        status, body, _ = self.submit(
            wait_seconds=redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS + 1
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_correction_needs_an_operation_id(self) -> None:
        """The idempotency key is required, because replay depends on it."""
        self.live_run()
        body = self.redirect_body()
        del body["operation_id"]

        status, payload, _ = self.post("/api/control/redirect", body)

        self.assertEqual(400, status)
        self.assertIn("operation_id", payload["error"])
        self.assertEqual((), self.journal().requests)
