"""Harness failure classification."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import (
    events,
    harness_failures,
    harnesses,
    pause_policy,
    process_tree,
)
from tests.harness_integration_support import (
    OBSERVED_AUTH_EXPIRY,
    OBSERVED_LAUNCH_CLOSURE,
    OBSERVED_PROVIDER_SERVER_ERROR,
    OBSERVED_SOCKET_CLOSURE,
)


class HarnessTransportClassificationTests(unittest.TestCase):
    """Transport failures the classifier retries, and the ones it survives."""

    def _result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "name": "codex",
            "argv": ["codex"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "last_message": "",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def test_connection_was_closed_unexpectedly_classifies_as_retryable_transport(
        self,
    ) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(last_message=OBSERVED_SOCKET_CLOSURE)
        )

        self.assertEqual(events.PAUSE_REASON_TRANSPORT_FAILED, classification.reason)
        self.assertTrue(classification.retryable)
        policy = pause_policy.classify_pause(classification.reason)
        self.assertTrue(policy["recoverable"])
        self.assertFalse(policy["requires_user"])
        self.assertEqual(pause_policy.DEFAULT_MAX_RETRIES, policy["max_retries"])
        self.assertEqual(
            list(pause_policy.TRANSPORT_RETRY_DELAYS_SECONDS),
            policy["backoff_seconds"],
        )

    def test_widened_transport_wording_stays_retryable(self) -> None:
        for text in (
            "the connection was closed by the remote host",
            "Error: connection reset by peer",
            "the connection has been refused",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_TRANSPORT_FAILED, classification.reason
                )
                self.assertTrue(classification.retryable)

    def test_unrecognized_failure_still_fails_closed(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(stderr="fake codex failure", returncode=3)
        )

        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, classification.reason)
        self.assertFalse(classification.retryable)

    def test_invalid_model_and_capacity_outrank_the_widened_transport_rule(
        self,
    ) -> None:
        invalid = harness_failures.classify_harness_failure(
            self._result(stderr="model not found; the connection was closed")
        )
        self.assertEqual(events.PAUSE_REASON_INVALID_MODEL, invalid.reason)
        self.assertFalse(invalid.retryable)

        capacity = harness_failures.classify_harness_failure(
            self._result(stderr="model is at capacity; the connection was closed")
        )
        self.assertEqual(harness_failures.MODEL_CAPACITY_MARKER, capacity.marker)

    def test_the_observed_provider_500_is_a_retryable_transport_failure(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(last_message=OBSERVED_PROVIDER_SERVER_ERROR)
        )

        self.assertEqual(events.PAUSE_REASON_TRANSPORT_FAILED, classification.reason)
        self.assertEqual("provider-server-error", classification.marker)
        self.assertTrue(classification.retryable)
        policy = pause_policy.classify_pause(classification.reason)
        self.assertTrue(policy["recoverable"])
        self.assertFalse(policy["requires_user"])

    def test_the_provider_error_envelope_is_what_matches_not_the_number(self) -> None:
        for text in (
            'API Error: 500 {"type":"error"}',
            'the provider returned {"type": "api_error", "message": "boom"}',
            "Internal Server Error",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual("provider-server-error", classification.marker)
                self.assertTrue(classification.retryable)

    def test_prose_that_merely_contains_500_is_not_retried(self) -> None:
        # The point of anchoring on the envelope: a turn whose input is a
        # transcript of ASL itself says "500" constantly, and a bare `\b500\b`
        # would relaunch a session that failed for an unrelated reason.
        for text in (
            "the file is 500 lines long",
            "listening on port 5000",
            "the budget is $500 per month",
            "reduced the suite from 500 to 200 cases",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_NONZERO_EXIT, classification.reason
                )
                self.assertFalse(classification.retryable)

    def test_the_rules_that_outrank_the_provider_error_still_win(self) -> None:
        quota = harness_failures.classify_harness_failure(
            self._result(stderr="429 too many requests; API Error: 500")
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, quota.reason)

        capacity = harness_failures.classify_harness_failure(
            self._result(stderr="overloaded_error; API Error: 500")
        )
        self.assertEqual(harness_failures.MODEL_CAPACITY_MARKER, capacity.marker)

    def test_launch_transport_closure_is_a_degradation_not_a_failure(self) -> None:
        result = self._result(
            returncode=0,
            stderr=f"{OBSERVED_LAUNCH_CLOSURE}\nOpenAI Codex v0.147.0\n",
        )

        degradation = harness_failures.classify_harness_degradation(result)

        assert degradation is not None
        self.assertEqual(
            harness_failures.TRANSPORT_DEGRADATION_MARKER, degradation.marker
        )
        self.assertFalse(degradation.affected_turn)
        self.assertIn("Transport channel closed", degradation.evidence)

    def test_degradation_records_whether_the_turn_also_failed(self) -> None:
        degradation = harness_failures.classify_harness_degradation(
            self._result(returncode=7, stderr=OBSERVED_LAUNCH_CLOSURE)
        )

        assert degradation is not None
        self.assertTrue(degradation.affected_turn)

    def test_a_transcript_quoting_the_closure_is_not_a_degradation(self) -> None:
        transcript = f"OpenAI Codex v0.147.0\n{'session transcript ' * 400}"

        self.assertIsNone(
            harness_failures.classify_harness_degradation(
                self._result(
                    returncode=0, stderr=f"{transcript}\n{OBSERVED_LAUNCH_CLOSURE}\n"
                )
            )
        )

    def test_clean_stderr_records_no_degradation(self) -> None:
        self.assertIsNone(
            harness_failures.classify_harness_degradation(
                self._result(returncode=0, stderr="OpenAI Codex v0.147.0\n")
            )
        )


class HarnessShutdownClassificationTests(unittest.TestCase):
    def test_shutdown_provenance_keeps_failure_and_suppresses_text_guessing(
        self,
    ) -> None:
        recovery = {
            "policy": process_tree.POST_RESULT_ROOT_RECOVERY_POLICY,
            "root_pid": 123,
            "signal": "SIGTERM",
            "signaled_pids": [123],
        }
        result = harnesses.HarnessResult(
            name="claude",
            argv=[],
            returncode=143,
            stdout="",
            stderr="",
            last_message="The task involved credentials and rate limits.",
        )
        classified = harness_failures.classify_harness_failure(
            result, shutdown={"recovery": recovery}
        )
        self.assertEqual("nonzero-exit", classified.reason)
        self.assertEqual("post-result-shutdown", classified.marker)
        self.assertFalse(classified.retryable)
        canceled = harness_failures.classify_harness_failure(
            result, shutdown={"recovery": recovery, "canceled_by": "activity"}
        )
        self.assertNotEqual("post-result-shutdown", canceled.marker)
        changes_to_try: list[dict[str, Any]] = [
            {"signaled_pids": []},
            {"signaled_pids": [456]},
            {"signal": "SIGKILL"},
            {"policy": "unknown"},
        ]
        for changes in changes_to_try:
            classified = harness_failures.classify_harness_failure(
                result, shutdown={"recovery": {**recovery, **changes}}
            )
            self.assertNotEqual("post-result-shutdown", classified.marker)

    def test_a_provider_error_keeps_its_failure_category_after_shutdown(self) -> None:
        result = harnesses.HarnessResult(
            name="claude",
            argv=[],
            returncode=143,
            stdout="claude result: error: authentication failed\n",
            stderr="",
            last_message="authentication failed",
        )
        classified = harness_failures.classify_harness_failure(
            result,
            shutdown={
                "recovery": {
                    "policy": process_tree.POST_RESULT_ROOT_RECOVERY_POLICY,
                    "root_pid": 123,
                    "signal": "SIGTERM",
                    "signaled_pids": [123],
                }
            },
        )
        self.assertEqual("auth-failed", classified.reason)


class HarnessAuthenticationClassificationTests(unittest.TestCase):
    """Credential failures reach the category no reply can clear."""

    def _result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "name": "claude",
            "argv": ["claude"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "last_message": "",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def test_the_observed_oauth_expiry_is_auth_failed_not_a_nonzero_exit(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(last_message=OBSERVED_AUTH_EXPIRY)
        )

        self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)
        self.assertEqual("authentication", classification.marker)
        self.assertFalse(classification.retryable)

    def test_an_expired_credential_asks_for_inspection_not_a_reply(self) -> None:
        # The point of the category: an operator cannot refresh a harness's
        # OAuth session by answering a mailbox question, so the pause has to
        # escalate rather than hold the turn open waiting for one.
        classification = harness_failures.classify_harness_failure(
            self._result(last_message=OBSERVED_AUTH_EXPIRY)
        )

        policy = pause_policy.classify_pause(classification.reason)

        self.assertFalse(policy["recoverable"])
        self.assertTrue(policy["requires_user"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, policy["user_action"])

    def test_every_authenticate_word_form_reaches_the_same_category(self) -> None:
        for text in (
            "Failed to authenticate",
            "the request was not authenticated",
            "authenticating with the provider failed",
            "authentication failed",
            "user is unauthenticated",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)

    def test_affirmative_authenticated_prose_preserves_exit_provenance(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(
                last_message=(
                    "Ten real input-to-visible-result-ms values from two "
                    "authenticated Kiln launches."
                ),
                returncode=143,
            )
        )

        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, classification.reason)
        self.assertEqual("exit-143", classification.marker)

    def test_oauth_and_an_expired_session_join_the_expired_token(self) -> None:
        for text in (
            "oauth token could not be refreshed",
            "your session expired",
            "token expired",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)

    def test_the_rules_that_outrank_authentication_still_win(self) -> None:
        # Widening the pattern must not reorder the table: the oversized-request
        # rule and the quota rule both precede it, and both describe requests an
        # auth message may legitimately mention.
        overflow = harness_failures.classify_harness_failure(
            self._result(last_message="Prompt is too long; failed to authenticate")
        )
        self.assertEqual(harness_failures.CONTEXT_OVERFLOW_MARKER, overflow.marker)

        quota = harness_failures.classify_harness_failure(
            self._result(stderr="usage limit reached; please authenticate again")
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, quota.reason)
