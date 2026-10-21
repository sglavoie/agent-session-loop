# mypy: ignore-errors
from .asl_redirect_supervision_support import *


class PreservedRetryPolicyTests(_SupervisionCase):
    """The safety work must not cost the plan its ordinary transient retries."""

    def test_timeout_and_transport_stay_recoverable_with_the_same_budget(self) -> None:
        self.assertIn("timeout", pause_policy.RECOVERABLE_PAUSE_REASONS)
        self.assertIn("transport-failed", pause_policy.RECOVERABLE_PAUSE_REASONS)
        self.assertEqual(2, pause_policy.DEFAULT_MAX_RETRIES)
        self.assertEqual((5.0, 30.0), pause_policy.TRANSPORT_RETRY_DELAYS_SECONDS)
        for reason in ("timeout", "transport-failed"):
            with self.subTest(reason=reason):
                policy = pause_policy.classify_pause(reason)
                self.assertTrue(policy["recoverable"])
                self.assertFalse(policy["requires_user"])
                self.assertEqual(
                    pause_policy.DEFAULT_MAX_RETRIES, policy["max_retries"]
                )

    def test_timeout_retries_twice_without_backing_off(self) -> None:
        # A timeout is retried on the recoverable budget and, unlike a transport
        # failure, relaunches immediately: nothing about it says the provider
        # needs time.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_pause("timeout", message="harness timed out")])
        sleeps: list[float] = []

        result = self._supervise(status, run, sleeps=sleeps, max_retries=2)

        self.assertEqual("paused", result["reason"])
        self.assertEqual([1, 2, 3], run.chunks)
        self.assertEqual([], sleeps)
        self.assertEqual(2, result["retries"]["count"])
        self.assertTrue(result["retries"]["exhausted"])

    def test_transport_retries_still_sleep_five_then_thirty(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_transport_pause()])
        sleeps: list[float] = []

        result = self._supervise(status, run, sleeps=sleeps, max_retries=2)

        self.assertEqual("paused", result["reason"])
        self.assertEqual([1, 2, 3], run.chunks)
        self.assertEqual([5.0, 30.0], sleeps)
        self.assertEqual(35.0, result["retries"]["delayed_seconds"])


class RecoveryPausesSpendNothingTests(_SupervisionCase):
    """Neither recovery pause is retryable, so neither can consume a budget."""

    def test_neither_recovery_reason_is_recoverable(self) -> None:
        for reason in self.autopilot.RECOVERY_DECISION_PAUSE_REASONS:
            with self.subTest(reason=reason):
                policy = pause_policy.classify_pause(reason)
                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual("inspect", policy["user_action"])
                self.assertEqual(0, policy["max_retries"])
                self.assertEqual([], list(policy["backoff_seconds"]))
        self.assertEqual(
            (
                events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
                events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
            ),
            self.autopilot.RECOVERY_DECISION_PAUSE_REASONS,
        )

    def test_recovery_pauses_stop_on_the_first_chunk_and_retry_nothing(self) -> None:
        # The budget is generous (max_retries=2) precisely so an accidental
        # relaunch would show up as extra chunks rather than as a silent
        # difference in a counter.
        for payload in (
            _conversation_recovery_pause(),
            _successor_blocked_pause(),
        ):
            with self.subTest(reason=payload["pause_reason"]):
                status = _StatusScript([_status_payload(1)])
                run = _RunScript([payload])
                sleeps: list[float] = []

                result = self._supervise(status, run, sleeps=sleeps, max_retries=2)

                self.assertFalse(result["ok"], result)
                self.assertEqual("user_required", result["reason"])
                self.assertEqual([1], run.chunks)
                self.assertEqual([], sleeps)
                retries = result["retries"]
                self.assertEqual(0, retries["count"])
                self.assertFalse(retries["exhausted"])
                self.assertEqual(0.0, retries["delayed_seconds"])

    def test_a_repeated_recovery_pause_is_no_worse_the_second_time(self) -> None:
        """Spending nothing has to mean the next supervision starts even."""
        for _ in range(2):
            status = _StatusScript([_status_payload(1)])
            run = _RunScript([_conversation_recovery_pause()])
            result = self._supervise(status, run, max_retries=2)
            self.assertEqual("user_required", result["reason"])
            self.assertEqual([1], run.chunks)
            self.assertEqual(0, result["retries"]["count"])

    def test_supervision_end_reports_a_recovery_stop_as_blocked(self) -> None:
        outcome, text = self.autopilot._supervision_end_fields(
            {"ok": False, "reason": "user_required"}
        )
        self.assertEqual("blocked", outcome)
        self.assertIn("user_required", text)


class RecoveryDecisionSurfacedTests(_SupervisionCase):
    """A stop nobody can act on is the same as no stop at all."""

    def _recovery_block(self, payload, **kwargs):
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([payload])
        result = self._supervise(status, run, **kwargs)
        self.assertEqual("user_required", result["reason"])
        return result["recovery"]

    def test_conversation_recovery_names_the_one_use_authorization(self) -> None:
        recovery = self._recovery_block(
            _conversation_recovery_pause(
                selection_state=conversations.SELECTION_INCOMPATIBLE,
                mismatched_fields=("root", "harness"),
            )
        )

        self.assertEqual(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED, recovery["reason"]
        )
        self.assertEqual(
            self.autopilot.RECOVERY_DECISION_FRESH_CONVERSATION, recovery["decision"]
        )
        self.assertFalse(recovery["auto_resume"])
        self.assertEqual("operator", recovery["authorized_by"])
        self.assertEqual(
            conversations.SELECTION_INCOMPATIBLE, recovery["selection_state"]
        )
        self.assertEqual(["root", "harness"], recovery["mismatched_fields"])
        self.assertEqual(
            f"asl control recover-conversation {PLAN_DIR} --json",
            recovery["inspect_command"],
        )
        # Rendered by the single renderer, so the helper and the CLI cannot
        # print two different relaunches for the same decision.
        self.assertEqual(
            redirect_cli.fresh_conversation_command(PLAN_DIR),
            recovery["authorization_command"],
        )
        self.assertIn("--fresh-conversation", recovery["authorization_command"])

    def test_a_blocked_successor_offers_evidence_and_no_authorization(self) -> None:
        # Nothing an operator can say makes a second worker safe to start beside
        # a predecessor that cannot be shown stopped, so there is no command to
        # offer -- and the absence is stated rather than left as a missing key.
        recovery = self._recovery_block(_successor_blocked_pause("worker-still-alive"))

        self.assertEqual(
            events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED, recovery["reason"]
        )
        self.assertEqual(
            self.autopilot.RECOVERY_DECISION_PREDECESSOR_ALIVE, recovery["decision"]
        )
        self.assertFalse(recovery["auto_resume"])
        self.assertEqual("worker-still-alive", recovery["blocked_reason"])
        self.assertIn("authorization_command", recovery)
        self.assertIsNone(recovery["authorization_command"])
        self.assertEqual(
            f"asl control redirect-status {PLAN_DIR} --json",
            recovery["inspect_command"],
        )

    def test_the_state_dir_travels_into_every_command_offered(self) -> None:
        # A supervised run pointed at an overridden state dir must not hand back
        # commands that read the default one -- an operator following them would
        # inspect a different plan's evidence and see nothing wrong.
        parsed = dict(PARSED, state_dir="/tmp/state")
        for payload in (
            _successor_blocked_pause(),
            _conversation_recovery_pause(),
        ):
            with self.subTest(reason=payload["pause_reason"]):
                recovery = self.autopilot._recovery_payload(
                    parsed, payload, payload["pause_reason"]
                )
                self.assertIn("--state-dir /tmp/state", recovery["inspect_command"])
                authorization = recovery["authorization_command"]
                if authorization is not None:
                    self.assertIn("--state-dir /tmp/state", authorization)

    def test_an_ordinary_pause_carries_no_recovery_block(self) -> None:
        # Only the two decisions get one; a verify-failed pause is answered by
        # the ordinary reply/repair path and would be muddled by a block that
        # names an authorization it does not need.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([_pause("quota-exhausted", message="quota exhausted")])
        result = self._supervise(status, run)

        self.assertEqual("user_required", result["reason"])
        self.assertNotIn("recovery", result)

    def test_building_the_block_reads_only_the_pause_it_was_given(self) -> None:
        """The block is derived, so producing it cannot change what it describes."""
        payload = _successor_blocked_pause()
        before = repr(payload)
        parsed = dict(PARSED)

        first = self.autopilot._recovery_payload(
            parsed, payload, payload["pause_reason"]
        )
        second = self.autopilot._recovery_payload(
            parsed, payload, payload["pause_reason"]
        )

        self.assertEqual(first, second)
        self.assertEqual(before, repr(payload))
        self.assertEqual(PARSED, parsed)

    def test_a_recovery_pause_without_details_still_names_the_decision(self) -> None:
        # An older runner, or a pause truncated on its way here, must not cost
        # the operator the one thing the block exists to say.
        recovery = self.autopilot._recovery_payload(
            dict(PARSED),
            {
                "paused": True,
                "pause_reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
            },
            events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
        )
        self.assertEqual(
            self.autopilot.RECOVERY_DECISION_PREDECESSOR_ALIVE, recovery["decision"]
        )
        self.assertFalse(recovery["auto_resume"])
        self.assertIsNone(recovery["blocked_reason"])
