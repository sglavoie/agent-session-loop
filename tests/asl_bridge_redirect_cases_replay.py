from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_bridge_redirect_support import *


class ReplayTests(RedirectBridgeTestCase):
    """Retrying a request whose answer was lost returns the durable record."""

    def test_resubmitting_the_same_id_appends_nothing(self) -> None:
        self.live_run()
        self.accept_when_pending()
        first = self.submit(wait_seconds=5.0)["result"]

        second = self.submit(request_id="req-5")["result"]

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        # A replay submitted nothing, so it has no outcome of its own; the
        # durable operation carries the truth instead.
        self.assertIsNone(second["outcome"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(1, len(self.journal().requests))

    def test_a_replay_is_served_before_the_action_token_is_checked(self) -> None:
        """A correction that landed moves the plan, which moves the token."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        replay = self.submit(action_token="0" * 32, request_id="req-5")

        self.assertTrue(replay["ok"])
        self.assertTrue(replay["result"]["replayed"])
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            replay["result"]["operation"]["feedback_digest"],
        )
        self.assertEqual(1, len(self.journal().requests))

    def test_a_replay_needs_no_live_run(self) -> None:
        """Asking after a correction outlives the run that was corrected."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(run_id=None, turn_index=None)

        replay = self.submit(request_id="req-5")

        self.assertTrue(replay["ok"])
        self.assertTrue(replay["result"]["replayed"])


class ReplayIdentityTests(RedirectBridgeTestCase):
    """A retry is the same request, or it is not a retry.

    Replay is served before the action token is checked, so identity is the
    only thing left standing between "my answer was lost, here it is again" and
    "I changed my mind, send this instead". A client that submits different
    content under a durable id is told so, rather than handed a receipt for
    words the worker was never given.
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
                self.plan_dir, "amx-op-9c31aa", state_dir=self.state_dir
            ),
        )

    def test_a_replay_carrying_different_words_is_refused(self) -> None:
        self.submitted()

        refusal = self.submit(request_id="req-5", text=SECOND_FEEDBACK)

        self.assertFalse(refusal["ok"])
        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, refusal["error"]["code"]
        )
        self.assertEqual(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT,
            refusal["error"]["details"]["reason"],
        )
        self.assertEqual(
            redirect_store_vocab.CONFLICT_FEEDBACK,
            refusal["error"]["details"]["conflict"],
        )
        self.assertFalse(refusal["error"]["retryable"])
        self.assert_journal_unchanged()

    def test_a_replay_naming_a_different_turn_is_refused(self) -> None:
        """The id is bound to the turn it was submitted against."""
        self.submitted()

        refusal = self.submit(
            request_id="req-5",
            expected_target=self.target(turn_index=2).to_payload(),
        )

        self.assertFalse(refusal["ok"])
        self.assertEqual(
            redirect_store_vocab.CONFLICT_TARGET,
            refusal["error"]["details"]["conflict"],
        )
        self.assert_journal_unchanged()

    def test_a_refused_replay_never_quotes_either_correction(self) -> None:
        """Both corrections are the operator's words; neither travels."""
        self.submitted()

        refusal = self.submit(request_id="req-5", text=SECOND_FEEDBACK)

        frame = json.dumps(refusal)
        self.assertNotIn(FEEDBACK, frame)
        self.assertNotIn(SECOND_FEEDBACK, frame)

    def test_a_retry_naming_the_same_turn_still_replays(self) -> None:
        """A client that named its turn must be able to name it again."""
        self.submitted()

        replay = self.submit(
            request_id="req-5", expected_target=self.target().to_payload()
        )

        self.assertTrue(replay["ok"])
        self.assertTrue(replay["result"]["replayed"])
        self.assert_journal_unchanged()

    def test_a_second_bridge_process_refuses_the_same_conflict(self) -> None:
        """The identity check reads the journal, so it survives a restart."""
        self.submitted()

        refusal = self.submit(
            self.handshaken(), request_id="req-5", text=SECOND_FEEDBACK
        )

        self.assertFalse(refusal["ok"])
        self.assertEqual(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT,
            refusal["error"]["details"]["reason"],
        )
        self.assert_journal_unchanged()


class RestartTests(RedirectBridgeTestCase):
    """A correction's fate lives in the journal, not in a receipt cache."""

    def test_a_second_bridge_process_still_reports_the_correction(self) -> None:
        self.live_run()
        self.accept_when_pending()
        submitted = self.submit(wait_seconds=5.0)["result"]

        restarted = self.session()
        self.assertEqual({}, dict(restarted.operations))
        result = self.status(restarted)["result"]

        self.assertEqual(
            [submitted["operation_id"]],
            [operation["operation_id"] for operation in result["operations"]],
        )
        self.assertEqual(
            submitted["operation"]["feedback_digest"],
            result["operations"][0]["feedback_digest"],
        )

    def test_a_second_bridge_process_can_replay_the_first_ones_request(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        replay = self.submit(self.handshaken(), request_id="req-5")

        self.assertTrue(replay["result"]["replayed"])
        self.assertEqual(1, len(self.journal().requests))

    def test_status_reports_the_stage_the_journal_now_computes(self) -> None:
        """Acknowledgment lands after the submitting bridge is long gone."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        redirect_store_carry.carry_request(
            self.plan_dir,
            "amx-op-9c31aa",
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.acknowledge_request(
            self.plan_dir,
            "amx-op-9c31aa",
            attempt=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

        operation = self.status()["result"]["operations"][0]

        self.assertEqual(redirect_recovery.STATUS_ACKNOWLEDGED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_ACKNOWLEDGED, operation["stage"])
        self.assertEqual(
            self.target(turn_index=2).to_payload(), operation["continuation"]
        )
