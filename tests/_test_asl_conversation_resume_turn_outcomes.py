# mypy: ignore-errors
"""What a turn's outcome records, session completion, and launch wiring."""

# ruff: noqa: F403, F405
from ._test_asl_conversation_resume_support import *


class TurnOutcomeTests(ContinuationTestBase):
    """What the ledger learns after the turn, and what it deliberately does not."""

    def opened_capture(self) -> conversation_resume.ConversationCapture:
        capture = conversation_resume.ConversationCapture(
            self.plan_state, binding=self.binding()
        )
        capture("thread-7")
        return capture

    def record(
        self, capture: conversation_resume.ConversationCapture, **overrides: object
    ) -> conversation_resume.TurnOutcomeRecord:
        return conversation_resume.record_turn_outcome(
            self.plan_state,
            capture=capture,
            result=harness_result(**overrides),
            run_id="run-1",
            turn=3,
        )

    def test_a_finished_turn_makes_its_conversation_resumable(self) -> None:
        outcome = self.record(self.opened_capture())
        self.assertTrue(outcome.confirmed)
        self.assertIsNone(outcome.attempt)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_RESUMABLE)
        self.assertEqual(selection.reference.native_id, "thread-7")

    def test_an_interrupted_turn_is_resumable_and_carries_its_attempt(self) -> None:
        outcome = self.record(self.opened_capture(), interrupted=True)
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.attempt.outcome, "interrupted")
        self.assertEqual(outcome.attempt.run_id, "run-1")
        self.assertEqual(outcome.attempt.turn, 3)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_RESUMABLE)

    def test_a_timed_out_turn_is_an_interrupted_attempt(self) -> None:
        self.assertEqual(
            conversation_resume.attempt_outcome(harness_result(timed_out=True)),
            "interrupted",
        )
        self.assertEqual(
            conversation_resume.attempt_outcome(harness_result(aborted=True)),
            "interrupted",
        )
        self.assertEqual(
            conversation_resume.attempt_outcome(
                harness_result(cancel_stage=harnesses.CANCEL_STAGE_EXECUTION)
            ),
            "interrupted",
        )

    def test_a_transport_failure_is_a_failed_attempt_that_still_resumes(self) -> None:
        outcome = self.record(self.opened_capture(), returncode=2)
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.attempt.outcome, "failed")
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_RESUMABLE)

    def test_a_turn_that_submitted_nothing_leaves_it_unconfirmed(self) -> None:
        for overrides in (
            {"cancel_stage": harnesses.CANCEL_STAGE_UNSTARTED},
            {"cancel_stage": harnesses.CANCEL_STAGE_HANDSHAKE},
            {"conversation_error": "ledger unwritable"},
        ):
            with self.subTest(**overrides):
                self.assertFalse(
                    conversation_resume.turn_submitted_work(harness_result(**overrides))
                )

    def test_an_unsubmitted_turn_does_not_confirm_its_conversation(self) -> None:
        outcome = self.record(
            self.opened_capture(), cancel_stage=harnesses.CANCEL_STAGE_HANDSHAKE
        )
        self.assertFalse(outcome.confirmed)
        self.assertIsNotNone(outcome.error)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_UNCONFIRMED)

    def test_a_turn_that_never_reported_a_conversation_records_nothing(self) -> None:
        capture = conversation_resume.ConversationCapture(
            self.plan_state, binding=self.binding()
        )
        outcome = self.record(capture)
        self.assertFalse(outcome.confirmed)
        self.assertIsNone(outcome.conversation)

    def test_a_failed_confirmation_is_reported_rather_than_raised(self) -> None:
        capture = self.opened_capture()
        with mock.patch.object(
            conversations, "confirm_conversation", side_effect=OSError("disk full")
        ):
            outcome = self.record(capture)
        self.assertFalse(outcome.confirmed)
        self.assertIn("disk full", outcome.error)
        # Fail closed: the next attempt reads unconfirmed and asks the operator.
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_UNCONFIRMED)


class SessionCompletionTests(ContinuationTestBase):
    """A completed session's conversation stops being reachable."""

    def test_completion_retires_every_harness_conversation_for_the_session(
        self,
    ) -> None:
        self.confirmed("thread-7", harness="codex")
        self.confirmed("thread-7c", harness="claude")
        self.confirmed("thread-8", session_id="8")
        retired, error = conversation_resume.retire_session_conversations(
            self.plan_state, session_id="7"
        )
        self.assertIsNone(error)
        self.assertEqual(set(retired), {"codex:thread-7", "claude:thread-7c"})
        self.assertEqual(
            conversations.select_conversation(self.plan_state, self.binding()).state,
            conversations.SELECTION_RETIRED,
        )
        self.assertEqual(
            conversations.select_conversation(
                self.plan_state, self.binding(session_id="8")
            ).state,
            conversations.SELECTION_RESUMABLE,
        )

    def test_a_later_attempt_at_a_finished_session_stops_for_recovery(self) -> None:
        self.confirmed("thread-7")
        conversation_resume.retire_session_conversations(
            self.plan_state, session_id="7"
        )
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.withheld()
        )
        self.assertTrue(continuation.paused)

    def test_a_failed_retirement_is_reported_rather_than_raised(self) -> None:
        self.confirmed("thread-7")
        with mock.patch.object(
            conversations,
            "retire_completed_session",
            side_effect=conversations.ConversationError("invalid-record", "boom"),
        ):
            retired, error = conversation_resume.retire_session_conversations(
                self.plan_state, session_id="7"
            )
        self.assertEqual(retired, ())
        self.assertIn("boom", error)


class TurnLaunchWiringTests(unittest.TestCase):
    """The decision reaches the adapter through the turn launch, unaltered."""

    def test_the_launch_carries_both_halves_of_continuity(self) -> None:
        annotations = turn_execution_executor.TurnLaunch.__annotations__
        self.assertIn("resume_conversation_id", annotations)
        self.assertIn("on_conversation", annotations)

    def test_the_executor_forwards_both_halves_to_the_harness(self) -> None:
        source = inspect.getsource(turn_execution_executor.TurnExecutor.launch)
        self.assertIn("resume_conversation_id=launch.resume_conversation_id", source)
        self.assertIn("on_conversation=launch.on_conversation", source)

    def test_run_harness_accepts_the_resume_request(self) -> None:
        parameters = inspect.signature(harnesses.run_harness).parameters
        self.assertIn("resume_conversation_id", parameters)
        self.assertIn("on_conversation", parameters)

    def test_every_adapter_accepts_the_resume_request(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                self.assertIn(
                    "resume_conversation_id",
                    inspect.signature(adapter.run).parameters,
                )
