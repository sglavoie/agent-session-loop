"""Harness failure classification tests.

Split from tests/test_asl_compaction.py."""

from __future__ import annotations

import json
import unittest

from agent_session_loop import (
    events,
    harness_failures,
    harnesses,
    turn_execution_artifacts,
    turn_execution_signals,
)
from tests.support import (
    CLAUDE_SESSION_LIMIT_FIXTURE,
    CODEX_USAGE_LIMIT_FIXTURE,
)


class HarnessFailureClassificationTests(unittest.TestCase):
    """Stable, evidence-based classification of nonzero harness turns."""

    def _result(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        last_message: str = "",
        returncode: int = 1,
        interrupted: bool = False,
        timed_out: bool = False,
    ) -> harnesses.HarnessResult:
        return harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            last_message=last_message,
            interrupted=interrupted,
            timed_out=timed_out,
        )

    def test_fixture_files_exist(self) -> None:
        # The observed usage/session-limit fixtures must ship with the tests.
        self.assertTrue(CODEX_USAGE_LIMIT_FIXTURE.is_file())
        self.assertTrue(CLAUDE_SESSION_LIMIT_FIXTURE.is_file())

    def test_codex_usage_limit_fixture_is_quota_non_retryable(self) -> None:
        text = CODEX_USAGE_LIMIT_FIXTURE.read_text(encoding="utf-8")
        classification = harness_failures.classify_harness_failure(
            self._result(stderr=text)
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertFalse(classification.retryable)
        # Reset time is parsed but does not gate retryability.
        self.assertIsNotNone(classification.retry_at)
        self.assertIn("2026-07-10T09:00:00Z", classification.retry_at)
        self.assertTrue(classification.evidence)

    def test_claude_session_limit_fixture_is_quota_non_retryable(self) -> None:
        text = CLAUDE_SESSION_LIMIT_FIXTURE.read_text(encoding="utf-8")
        classification = harness_failures.classify_harness_failure(
            self._result(last_message=text)
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertFalse(classification.retryable)

    def test_rate_limit_and_429_are_quota_non_retryable(self) -> None:
        for text in (
            "Error: 429 Too Many Requests",
            "rate limit exceeded, slow down",
            "insufficient_quota: you have run out of credits",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_QUOTA_EXHAUSTED,
                    classification.reason,
                )
                self.assertFalse(classification.retryable)

    def test_authentication_failures_are_non_retryable(self) -> None:
        for text in (
            "Error: 401 Unauthorized",
            "authentication failed: invalid api key",
            "You are not logged in. Please sign in.",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(events.PAUSE_REASON_AUTH_FAILED, classification.reason)
                self.assertFalse(classification.retryable)

    def test_invalid_model_failures_are_non_retryable(self) -> None:
        for text in (
            "invalid model: gpt-9",
            "Error: model not found",
            "unknown model 'sonnet-99'",
            (
                "There's an issue with the selected model (gpt-9). It may not "
                "exist or you may not have access to it. Run --model to pick a "
                "different model."
            ),
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_INVALID_MODEL, classification.reason
                )
                self.assertFalse(classification.retryable)

    def test_capacity_failures_are_transport_retryable(self) -> None:
        for text in (
            '"message": "Selected model is at capacity. Please try a different model."',
            '"codex_error_info": "server_overloaded"',
            "overloaded_error: the model is currently busy",
            "stream error: Overloaded",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_TRANSPORT_FAILED,
                    classification.reason,
                )
                self.assertTrue(classification.retryable)
                self.assertEqual(
                    harness_failures.MODEL_CAPACITY_MARKER, classification.marker
                )

    def test_invalid_model_outranks_capacity(self) -> None:
        # A typo'd model name must still fail fast even when the same output
        # suggests trying a different model.
        classification = harness_failures.classify_harness_failure(
            self._result(stderr="model not found; please try a different model")
        )
        self.assertEqual(events.PAUSE_REASON_INVALID_MODEL, classification.reason)
        self.assertFalse(classification.retryable)

    def test_oversized_prompt_is_named_without_becoming_retryable(self) -> None:
        for text in (
            "API Error: 400 {'message': 'prompt is too long: 210000 tokens'}",
            "input length exceeds the maximum of 200000 tokens",
            "context window exceeded",
            "Context length exceeded for this model",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                # Nothing here can be relaunched unchanged, so the reason and the
                # retryability are the fail-closed ones. Only the marker is new,
                # and it is stable rather than exit-code-derived so a caller that
                # can send less has something to branch on.
                self.assertEqual(
                    events.PAUSE_REASON_NONZERO_EXIT, classification.reason
                )
                self.assertFalse(classification.retryable)
                self.assertEqual(
                    harness_failures.CONTEXT_OVERFLOW_MARKER, classification.marker
                )
                self.assertEqual(
                    f"{events.PAUSE_REASON_NONZERO_EXIT}:"
                    f"{harness_failures.CONTEXT_OVERFLOW_MARKER}",
                    classification.fingerprint,
                )

    def test_oversized_prompt_outranks_the_vocabulary_it_carries(self) -> None:
        # The audit sends a transcript of ASL itself, so the refused request can
        # quote every word the quota and auth rules match on. The refusal is
        # about the request, not about the account, and must win the race.
        classification = harness_failures.classify_harness_failure(
            self._result(
                stderr=(
                    "prompt is too long: 517000 tokens > 200000 maximum\n"
                    "context: turn quoted 'rate limit reached' and 'invalid api "
                    "key' and 'connection closed' from the run under audit"
                )
            )
        )
        self.assertEqual(
            harness_failures.CONTEXT_OVERFLOW_MARKER, classification.marker
        )
        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, classification.reason)
        self.assertFalse(classification.retryable)

    def test_transport_failures_are_retryable(self) -> None:
        for text in (
            "read ECONNRESET",
            "Error: connection reset by peer",
            "502 Bad Gateway",
            "HTTP/1.1 503",
            "upstream returned status code: 504",
            "service unavailable, try again",
            "stream disconnected unexpectedly",
        ):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stderr=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_TRANSPORT_FAILED,
                    classification.reason,
                )
                self.assertTrue(classification.retryable)

    def test_a_bare_gateway_number_is_not_a_transport_failure(self) -> None:
        # A line range or port that happens to be 502-504 is prose, not a
        # response status, and must not make an unknown failure retryable.
        for text in ("Updated parser.py lines 480-503.", "listening on :8504 and 503"):
            with self.subTest(text=text):
                classification = harness_failures.classify_harness_failure(
                    self._result(stdout=text)
                )
                self.assertEqual(
                    events.PAUSE_REASON_NONZERO_EXIT, classification.reason
                )
                self.assertFalse(classification.retryable)

    def test_unknown_nonzero_fails_closed(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(stderr="fake codex failure", returncode=3)
        )
        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, classification.reason)
        self.assertFalse(classification.retryable)
        self.assertEqual(3, classification.returncode)
        self.assertIn("exit-3", classification.fingerprint)

    def test_interruption_is_classified_and_non_retryable(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(returncode=130, interrupted=True)
        )
        self.assertEqual(events.PAUSE_REASON_INTERRUPTED, classification.reason)
        self.assertFalse(classification.retryable)

    def test_quota_precedes_transport_when_both_present(self) -> None:
        # A 429 rate-limit alongside a transient marker still classifies as
        # quota (non-retryable), never as a retryable transport failure.
        classification = harness_failures.classify_harness_failure(
            self._result(
                stderr="429 rate limit; connection reset by peer",
            )
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertFalse(classification.retryable)

    def test_quota_without_reset_time_still_non_retryable(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(stderr="you've hit your usage limit")
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, classification.reason)
        self.assertIsNone(classification.retry_at)
        self.assertFalse(classification.retryable)

    def test_fingerprint_is_normalized_and_stable(self) -> None:
        first = harness_failures.classify_harness_failure(
            self._result(stderr="429 Too Many Requests at 10:00")
        )
        second = harness_failures.classify_harness_failure(
            self._result(stderr="rate limit reached at 11:30")
        )
        # Same category -> same normalized fingerprint despite differing text.
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertTrue(first.fingerprint.startswith("quota-exhausted:"))

    def test_evidence_preview_is_bounded(self) -> None:
        noisy = "connection reset " + ("x" * 5000)
        classification = harness_failures.classify_harness_failure(
            self._result(stderr=noisy)
        )
        self.assertLessEqual(
            len(classification.evidence), harness_failures.FAILURE_EVIDENCE_MAX_CHARS
        )

    def test_to_dict_is_json_serializable_and_complete(self) -> None:
        classification = harness_failures.classify_harness_failure(
            self._result(stderr="401 Unauthorized")
        )
        payload = classification.to_dict()
        json.dumps(payload)
        self.assertEqual(
            {
                "reason",
                "retryable",
                "fingerprint",
                "evidence",
                "returncode",
                "retry_at",
                "marker",
            },
            set(payload),
        )

    def test_all_classifier_reasons_are_registered_pause_reasons(self) -> None:
        reasons = {
            events.PAUSE_REASON_NONZERO_EXIT,
            events.PAUSE_REASON_QUOTA_EXHAUSTED,
            events.PAUSE_REASON_AUTH_FAILED,
            events.PAUSE_REASON_INVALID_MODEL,
            events.PAUSE_REASON_TRANSPORT_FAILED,
            events.PAUSE_REASON_INTERRUPTED,
        }
        self.assertTrue(reasons.issubset(set(events.PAUSE_REASONS)))

    def test_only_transport_is_a_retryable_category(self) -> None:
        self.assertEqual(
            (events.PAUSE_REASON_TRANSPORT_FAILED,),
            events.RETRYABLE_HARNESS_FAILURE_REASONS,
        )

    def test_turn_status_result_uses_classified_reason(self) -> None:
        result = self._result(stderr="429 Too Many Requests")
        status = turn_execution_artifacts.turn_status_result(
            result=result,
            status_changes=(),
            pending_completed=(),
            session_id="5",
            runner_signals=turn_execution_signals.runner_signals(result),
            plan_error=None,
        )
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, status)

    def test_turn_status_result_generic_failure_stays_nonzero_exit(self) -> None:
        result = self._result(stderr="fake codex failure")
        status = turn_execution_artifacts.turn_status_result(
            result=result,
            status_changes=(),
            pending_completed=(),
            session_id="5",
            runner_signals=turn_execution_signals.runner_signals(result),
            plan_error=None,
        )
        self.assertEqual(events.PAUSE_REASON_NONZERO_EXIT, status)


if __name__ == "__main__":
    unittest.main()
