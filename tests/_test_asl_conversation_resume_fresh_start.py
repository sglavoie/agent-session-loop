# mypy: ignore-errors
"""The one-use fresh-conversation authorization, its reach, and capture."""

# ruff: noqa: F403, F405
from ._test_asl_conversation_resume_support import *
from ._test_asl_conversation_resume_support import _load_autopilot


class AuthorizedFreshStartTests(ContinuationTestBase):
    """What ``--fresh-conversation`` buys, and how far it reaches."""

    def unresumable(self, native_id: str = "thread-7") -> None:
        """Leave a conversation that exists but may not be continued."""
        conversations.open_conversation(self.plan_state, self.reference(native_id))

    def test_authorization_turns_a_refusal_into_a_fresh_start(self) -> None:
        self.unresumable()
        authorization = self.granted()
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_FRESH
        )
        self.assertFalse(continuation.paused)
        self.assertTrue(continuation.captures)
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertTrue(authorization.consumed)
        self.assertIsNotNone(continuation.intent)
        self.assertFalse(continuation.intent.resumes)

    def test_the_superseded_conversation_is_retired_with_a_reason(self) -> None:
        self.unresumable()
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.granted()
        )
        self.assertEqual(continuation.superseded, ("codex:thread-7",))
        ledger = conversations.read_conversations(self.plan_state)
        retired = [
            record
            for record in ledger.history(self.binding().key)
            if record.kind == conversations.KIND_CONVERSATION_RETIRED
        ]
        self.assertEqual(
            [record.retirement_reason for record in retired], ["superseded"]
        )

    def test_a_retired_record_is_not_retired_a_second_time(self) -> None:
        reference = self.confirmed("thread-7")
        conversations.retire_conversation(
            self.plan_state, reference, reason="session-completed"
        )
        before = self.ledger_path.read_bytes()
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.granted()
        )
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_FRESH
        )
        self.assertEqual(continuation.superseded, ())
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_a_failed_retirement_is_reported_not_raised(self) -> None:
        self.unresumable()
        with mock.patch.object(
            conversations,
            "retire_conversation",
            side_effect=OSError("read-only ledger"),
        ):
            continuation = conversation_resume.plan_turn_continuation(
                self.plan_state, binding=self.binding(), authorization=self.granted()
            )
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_FRESH
        )
        self.assertEqual(continuation.superseded, ())
        self.assertTrue(any("read-only ledger" in note for note in continuation.notes))

    def test_the_authorization_does_not_survive_into_a_later_turn(self) -> None:
        # A retry inside the same run reads the same object the operator
        # authorized once, and is refused by it.
        self.unresumable()
        authorization = self.granted()
        first = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        self.assertEqual(first.disposition, conversation_resume.CONTINUATION_FRESH)
        self.unresumable("thread-8")
        second = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        self.assertEqual(
            second.disposition, conversation_resume.CONTINUATION_RECOVERY_REQUIRED
        )
        self.assertTrue(second.paused)

    def test_the_authorization_does_not_survive_into_a_later_session(self) -> None:
        self.unresumable()
        authorization = self.granted()
        conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        conversations.open_conversation(
            self.plan_state, self.reference("thread-8", session_id="8")
        )
        later = conversation_resume.plan_turn_continuation(
            self.plan_state,
            binding=self.binding(session_id="8"),
            authorization=authorization,
        )
        self.assertTrue(later.paused)


class AuthorizationContainmentTests(unittest.TestCase):
    """The three replay surfaces the one-use flag is kept out of."""

    def flag(self) -> run_flags.RunFlag:
        for flag in run_flags.RUN_FLAG_SPEC:
            if flag.dest == "fresh_conversation":
                return flag
        raise AssertionError("RUN_FLAG_SPEC declares no --fresh-conversation flag")

    def test_the_flag_is_a_declared_store_true_option(self) -> None:
        flag = self.flag()
        self.assertEqual(flag.option_strings, ("--fresh-conversation",))
        self.assertEqual(flag.action, "store_true")
        self.assertIs(flag.cli_default, False)

    def test_resume_does_not_rehydrate_the_authorization(self) -> None:
        self.assertIn(
            "fresh_conversation", run_entry_resume._RESUME_OVERLAY_EXCLUDED_DESTS
        )
        self.assertNotIn("fresh_conversation", run_entry_resume._resume_overlay_dests())

    def test_the_supervisor_does_not_replay_the_authorization_to_a_child(self) -> None:
        autopilot = _load_autopilot()

        self.assertIn("fresh_conversation", autopilot._CHILD_RUN_ARG_SKIP_DESTS)

    def test_nothing_persists_the_authorization_in_run_config(self) -> None:
        from agent_session_loop import run_payloads

        source = inspect.getsource(run_payloads._run_config_payload)
        self.assertNotIn("fresh_conversation", source)


class RecoveryPauseRegistrationTests(unittest.TestCase):
    """The pause reason, wired everywhere a pause reason has to be wired."""

    reason = conversation_resume.RECOVERY_PAUSE_REASON

    def test_the_reason_is_registered_in_the_event_vocabulary(self) -> None:
        self.assertEqual(self.reason, "conversation-recovery-required")
        self.assertIn(self.reason, events.PAUSE_REASONS)

    def test_the_reason_is_environment_blocked_and_never_auto_retried(self) -> None:
        # A supervisor retry that "recovered" this pause would be a retry that
        # discarded an agent's context without anyone deciding to.
        self.assertIn(self.reason, pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS)
        self.assertNotIn(self.reason, pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_the_workbench_routes_the_reason_to_its_own_recovery_card(self) -> None:
        """The dedicated card, not the generic one.

        This reason was in the generic allowlist when it was registered, because
        nothing yet knew how to render it. The Workbench later grew a
        `conversation-recovery` card that reads the decision, the one-use
        authorization, and the evidence behind both, and the two tables are
        disjoint by construction, so the card is now the only place the reason
        may appear.
        """
        self.assertEqual(
            "conversation-recovery",
            workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON[self.reason],
        )
        self.assertNotIn(
            self.reason, workbench_recovery_models.GENERIC_RECOVERY_PAUSE_REASONS
        )

    def test_a_repair_hint_explains_the_two_ways_forward(self) -> None:
        hint = preflight_runtime_hints.RUNTIME_REPAIR_HINTS[self.reason]
        self.assertEqual(hint.check_id, f"runtime.{self.reason}")
        self.assertIn("--fresh-conversation", hint.message)

    def test_the_continuation_event_is_named_once(self) -> None:
        self.assertEqual(
            events.CONVERSATION_CONTINUATION_EVENT, "conversation_continuation"
        )


class ConversationCaptureTests(ContinuationTestBase):
    """The sink that records a conversation before any work is submitted."""

    def capture(
        self, expected: str | None = None
    ) -> conversation_resume.ConversationCapture:
        return conversation_resume.ConversationCapture(
            self.plan_state, binding=self.binding(), expected_native_id=expected
        )

    def test_a_new_conversation_is_opened_before_the_turn_submits(self) -> None:
        capture = self.capture()
        capture("thread-7")
        self.assertEqual(capture.reference.native_id, "thread-7")
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_UNCONFIRMED)

    def test_a_resumed_conversation_is_not_re_opened(self) -> None:
        self.confirmed("thread-7")
        before = self.ledger_path.read_bytes()
        capture = self.capture("thread-7")
        capture("thread-7")
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertEqual(capture.reference.native_id, "thread-7")

    def test_a_substituted_conversation_refuses_the_turn(self) -> None:
        capture = self.capture("thread-7")
        with self.assertRaises(harnesses.ConversationCaptureError):
            capture("thread-9")
        self.assertIn("thread-9", capture.error)
        self.assertIsNone(capture.reference)

    def test_repeating_the_same_identifier_writes_nothing_further(self) -> None:
        capture = self.capture()
        capture("thread-7")
        after_first = self.ledger_path.read_bytes()
        capture("thread-7")
        self.assertEqual(self.ledger_path.read_bytes(), after_first)

    def test_a_continuation_builds_the_capture_matched_to_its_decision(self) -> None:
        self.confirmed("thread-7")
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.withheld()
        )
        capture = continuation.capture(self.plan_state)
        self.assertIsNotNone(capture)
        with self.assertRaises(harnesses.ConversationCaptureError):
            capture("thread-9")

    def test_a_paused_continuation_builds_no_capture(self) -> None:
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.withheld()
        )
        self.assertIsNone(continuation.capture(self.plan_state))
