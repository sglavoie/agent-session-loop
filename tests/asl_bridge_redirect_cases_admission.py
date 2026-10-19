from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_bridge_redirect_support import *


class CapabilityTests(RedirectBridgeTestCase):
    """What the protocol advertises about the three methods."""

    def test_the_three_methods_are_advertised_with_handlers(self) -> None:
        for method in (
            intervention.ACTION_REDIRECT,
            bridge_protocol.METHOD_REDIRECT_STATUS,
            bridge_protocol.METHOD_RECOVER_CONVERSATION,
        ):
            with self.subTest(method=method):
                self.assertIn(method, bridge_session.CAPABILITIES)
                self.assertIn(method, bridge_session.METHOD_HANDLERS)
                self.assertIn(method, bridge_session.hello_payload()["capabilities"])

    def test_only_submission_is_gated(self) -> None:
        """Submitting appends to the journal; the other two write nothing."""
        self.assertIn(intervention.ACTION_REDIRECT, bridge_protocol.MUTATION_METHODS)
        self.assertNotIn(
            bridge_protocol.METHOD_REDIRECT_STATUS, bridge_protocol.MUTATION_METHODS
        )
        self.assertNotIn(
            bridge_protocol.METHOD_RECOVER_CONVERSATION,
            bridge_protocol.MUTATION_METHODS,
        )

    def test_the_methods_append_to_the_capability_order(self) -> None:
        """A client compares capability lists byte for byte across restarts."""
        self.assertEqual(
            [
                intervention.ACTION_REDIRECT,
                bridge_protocol.METHOD_REDIRECT_STATUS,
                bridge_protocol.METHOD_RECOVER_CONVERSATION,
            ],
            list(bridge_session.CAPABILITIES[-3:]),
        )


class SubmissionTests(RedirectBridgeTestCase):
    """A compatible client submits a correction and is told where it went."""

    def test_a_handshaken_client_submits_against_the_live_turn(self) -> None:
        self.live_run()
        self.accept_when_pending()

        response = self.submit(wait_seconds=5.0)

        self.assertEqual([], problems(response, "redirect_receipt_response"))
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(redirect_admission.OUTCOME_ACCEPTED, result["outcome"])
        self.assertEqual("amx-op-9c31aa", result["operation_id"])
        self.assertFalse(result["replayed"])
        self.assertEqual(self.target().to_payload(), result["target"])
        self.assertTrue(self.wakeup.is_set())

    def test_the_correction_reaches_the_durable_journal(self) -> None:
        """The receipt is only worth anything if the journal agrees with it."""
        self.live_run()
        self.accept_when_pending()

        result = self.submit(wait_seconds=5.0)["result"]

        requests = self.journal().requests
        self.assertEqual(1, len(requests))
        self.assertEqual("amx-op-9c31aa", requests[0].operation_id)
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            requests[0].feedback_digest,
        )
        self.assertEqual(
            requests[0].feedback_digest, result["operation"]["feedback_digest"]
        )
        self.assertEqual(
            FEEDBACK,
            redirect_store_journal.read_feedback(
                self.plan_dir, "amx-op-9c31aa", state_dir=self.state_dir
            ),
        )

    def test_an_expected_target_that_moved_is_refused_as_stale(self) -> None:
        """The race a client that rendered a row and waited for a human runs."""
        self.live_run()

        result = self.submit(
            expected_target={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 4,
                "harness": "claude",
            }
        )["result"]

        self.assertEqual(redirect_admission.OUTCOME_REJECTED, result["outcome"])
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, result["reason"])
        # Refused before anything was written, so the correction is still the
        # client's to reissue against the turn the run is actually on.
        self.assertEqual((), self.journal().requests)
        self.assertIsNone(result["operation_id"])
        self.assertIsNone(result["operation"])

    def test_a_run_holding_no_turn_has_nothing_to_correct(self) -> None:
        self.write_state(turn_index=None)
        self.serve_redirects()

        response = self.submit()

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            intervention.REASON_ACTION_NOT_PERMITTED, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_an_undeliverable_urgent_channel_settles_durably(self) -> None:
        """Persisted then unringable is settled, not left looking pending."""
        self.write_state()
        self.write_lock(redirect_endpoint=self.state_dir / "nothing-here.fifo")

        result = self.submit()["result"]

        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, result["outcome"])
        self.assertEqual(
            redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE, result["reason"]
        )
        # Durably settled rather than merely reported: a runner replaying this
        # journal later must not decide the correction is still owed.
        self.assertEqual(redirect_cli.STAGE_BLOCKED, result["operation"]["stage"])
        self.assertEqual(
            redirect_store_vocab.STATE_REJECTED, result["operation"]["journal_state"]
        )


class MutationGateTests(RedirectBridgeTestCase):
    """A correction is a mutation, and a refused mutation writes nothing."""

    def test_a_client_that_never_handshook_cannot_submit(self) -> None:
        self.live_run()

        response = self.request(
            self.session(),
            intervention.ACTION_REDIRECT,
            self.redirect_params(),
            request_id="req-2",
        )

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, response["error"]["code"]
        )
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual((), self.journal().requests)

    def test_an_incompatible_client_cannot_submit(self) -> None:
        self.live_run()
        session = self.session()
        self.request(
            session,
            bridge_protocol.METHOD_HANDSHAKE,
            {"contract_set_id": "asl-amx-1.deadbeefdeadbeef"},
        )

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(),
            request_id="req-2",
        )

        self.assertEqual(
            bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_a_handshake_against_a_snapshot_that_moved_is_stale(self) -> None:
        self.live_run()
        session = self.handshaken()
        moved = dict(self.snapshots[-1])
        moved["warnings"] = ["the corpus moved"]
        moved["digest"] = bridge_projection.snapshot_digest(moved)
        self.snapshots.append(moved)
        self.clock.now += 60.0

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(),
            request_id="req-2",
        )

        self.assertEqual(
            bridge_protocol.ERROR_STALE_SNAPSHOT, response["error"]["code"]
        )
        self.assertTrue(response["error"]["retryable"])
        self.assertEqual((), self.journal().requests)

    def test_a_stale_action_token_is_refused_after_the_handshake(self) -> None:
        """The two gates are complementary: the token pins the plan, not the build."""
        self.live_run()
        session = self.handshaken()

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(action_token="0" * 32),
            request_id="req-2",
        )

        self.assertEqual([], problems(response, "error_response"))
        self.assertEqual(
            intervention.REASON_STALE_ACTION_TOKEN, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_empty_correction_text_is_refused(self) -> None:
        self.live_run()

        response = self.submit(text="   \n  ")

        self.assertEqual(intervention.REASON_EMPTY_TEXT, response["error"]["code"])
        self.assertEqual((), self.journal().requests)

    def test_the_two_reads_never_require_a_handshake(self) -> None:
        """The client that most needs these is the one that cannot handshake."""
        self.live_run()

        status = self.status()
        recovery = self.recovery()

        self.assertTrue(status["ok"])
        self.assertTrue(recovery["ok"])
        self.assertEqual([], problems(status, "redirect_status_response"))
        self.assertEqual([], problems(recovery, "recovery_options_response"))


class FieldBoundTests(RedirectBridgeTestCase):
    """Every client-chosen number and object is bounded before it is used."""

    def test_a_wait_above_the_admission_ceiling_is_refused(self) -> None:
        self.live_run()

        response = self.submit(
            wait_seconds=redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS + 1
        )

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )
        self.assertEqual(
            str(redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS),
            response["error"]["details"]["maximum"],
        )
        self.assertEqual((), self.journal().requests)

    def test_a_negative_wait_is_refused(self) -> None:
        self.live_run()

        response = self.submit(wait_seconds=-1.0)

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )

    def test_a_boolean_wait_is_not_a_number(self) -> None:
        """``True`` is an ``int`` in Python and must not be read as one here."""
        self.live_run()

        response = self.submit(wait_seconds=True)

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )

    def test_the_default_wait_is_the_admission_window(self) -> None:
        """Restating the bound would let the two ends promise different things."""
        self.live_run()
        session = self.handshaken()
        params = self.redirect_params()
        params.pop("wait_seconds")

        self.assertEqual(
            redirect_admission.ACCEPTANCE_WAIT_SECONDS,
            session._redirect_wait_seconds(params),
        )

    def test_an_expected_target_that_is_not_an_object_is_refused(self) -> None:
        self.live_run()

        response = self.submit(expected_target="session-9")

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_an_expected_target_with_a_boolean_turn_index_is_refused(self) -> None:
        self.live_run()

        response = self.submit(
            expected_target={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": True,
                "harness": "claude",
            }
        )

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )

    def test_an_expected_target_the_journal_would_reject_is_refused(self) -> None:
        self.live_run()

        response = self.submit(
            expected_target={
                "run_id": RUN_ID,
                "session_id": SESSION_ID,
                "turn_index": 0,
                "harness": "claude",
            }
        )

        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
        )
        self.assertIn("reason", response["error"]["details"])
