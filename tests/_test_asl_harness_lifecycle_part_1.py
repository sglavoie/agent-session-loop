# mypy: ignore-errors
"""Focused test_asl_harness_lifecycle test group 1."""

# ruff: noqa: F403, F405
from ._test_asl_harness_lifecycle_support import *


class CancelStageVocabularyTests(unittest.TestCase):
    def test_every_stage_constant_is_in_the_tuple_exactly_once(self) -> None:
        constants = (
            harnesses.CANCEL_STAGE_UNSTARTED,
            harnesses.CANCEL_STAGE_HANDSHAKE,
            harnesses.CANCEL_STAGE_EXECUTION,
            harnesses.CANCEL_STAGE_SHUTDOWN,
        )
        self.assertEqual(constants, harnesses.CANCEL_STAGES)
        self.assertEqual(len(set(constants)), len(constants))


class AbortSignalCompletionTests(unittest.TestCase):
    """Initiation and completion are two observations, not one."""

    def test_arming_does_not_complete_the_request(self) -> None:
        signal = harnesses.AbortSignal()
        signal.arm("operator stopped the run")

        self.assertTrue(signal.armed)
        self.assertFalse(signal.completed)
        self.assertFalse(signal.wait_completed(0.01))
        self.assertEqual("operator stopped the run", signal.reason)

    def test_completion_is_observed_once_the_turn_is_released(self) -> None:
        signal = harnesses.AbortSignal()
        signal.begin_turn()
        signal.arm("stop")
        waiter_saw: list[bool] = []

        def wait() -> None:
            waiter_saw.append(signal.wait_completed(WAIT_SECONDS))

        waiter = threading.Thread(target=wait)
        waiter.start()
        self.assertFalse(signal.completed)
        signal.release()
        waiter.join(WAIT_SECONDS)

        self.assertEqual([True], waiter_saw)
        self.assertTrue(signal.completed)

    def test_a_request_after_the_turn_is_over_cannot_revive_it(self) -> None:
        """The late-arm case: answered on the spot rather than left waiting."""
        signal = harnesses.AbortSignal()
        signal.begin_turn()
        signal.release()

        signal.arm("too late")

        self.assertTrue(signal.completed)
        self.assertEqual(harnesses.CANCEL_STAGE_SHUTDOWN, signal.cancel_stage)

    def test_the_first_stage_named_is_the_one_reported(self) -> None:
        signal = harnesses.AbortSignal()
        signal.begin_turn()
        signal.arm("stop")

        signal.observed(harnesses.CANCEL_STAGE_HANDSHAKE)
        signal.observed(harnesses.CANCEL_STAGE_EXECUTION)

        self.assertEqual(harnesses.CANCEL_STAGE_HANDSHAKE, signal.cancel_stage)

    def test_an_unarmed_signal_starts_each_turn_clean(self) -> None:
        """The codex fallback runs a second turn on the same signal."""
        signal = harnesses.AbortSignal()
        signal.begin_turn()
        signal.release()

        signal.begin_turn()

        self.assertFalse(signal.completed)
        self.assertIsNone(signal.cancel_stage)

    def test_an_armed_signal_keeps_its_request_across_a_turn_boundary(self) -> None:
        signal = harnesses.AbortSignal()
        signal.begin_turn()
        signal.arm("stop")
        signal.observed(harnesses.CANCEL_STAGE_UNSTARTED)
        signal.release()

        signal.begin_turn()

        self.assertTrue(signal.armed)
        self.assertTrue(signal.completed)
        self.assertEqual(harnesses.CANCEL_STAGE_UNSTARTED, signal.cancel_stage)


class TurnConversationTests(unittest.TestCase):
    def test_a_captured_conversation_reaches_the_sink(self) -> None:
        recorded: list[str] = []
        conversation = harnesses.TurnConversation(recorded.append)

        conversation.capture("thread-77")

        self.assertEqual(["thread-77"], recorded)
        self.assertEqual("thread-77", conversation.native_id)

    def test_the_same_conversation_named_twice_is_recorded_once(self) -> None:
        recorded: list[str] = []
        conversation = harnesses.TurnConversation(recorded.append)

        conversation.capture("thread-77")
        conversation.capture("thread-77")

        self.assertEqual(["thread-77"], recorded)

    def test_a_second_conversation_is_refused(self) -> None:
        conversation = harnesses.TurnConversation()
        conversation.capture("thread-77")

        with self.assertRaises(harnesses.ConversationCaptureError) as caught:
            conversation.capture("thread-78")

        self.assertIn("thread-77", str(caught.exception))
        self.assertEqual("thread-77", conversation.native_id)

    def test_a_blank_conversation_id_is_refused(self) -> None:
        conversation = harnesses.TurnConversation()

        for candidate in ("", "   ", None, 7):
            with (
                self.subTest(candidate=candidate),
                self.assertRaises(harnesses.ConversationCaptureError),
            ):
                conversation.capture(candidate)  # type: ignore[arg-type]
        self.assertIsNone(conversation.native_id)

    def test_a_sink_failure_leaves_nothing_captured(self) -> None:
        """Persistence failed, so the turn has no conversation -- not a lost one."""

        def explode(native_id: str) -> None:
            raise OSError("ledger is read-only")

        conversation = harnesses.TurnConversation(explode)

        with self.assertRaises(harnesses.ConversationCaptureError) as caught:
            conversation.capture("thread-77")

        self.assertIn("ledger is read-only", str(caught.exception))
        self.assertIsNone(conversation.native_id)

    def test_a_late_callback_cannot_record_against_a_closed_turn(self) -> None:
        recorded: list[str] = []
        conversation = harnesses.TurnConversation(recorded.append)
        conversation.close()

        with self.assertRaises(harnesses.ConversationCaptureError):
            conversation.capture("thread-77")

        self.assertEqual([], recorded)
        self.assertTrue(conversation.closed)

    def test_closing_waits_out_a_capture_already_in_flight(self) -> None:
        """The same guarantee the live-delivery endpoint makes, for capture."""
        entered = threading.Event()
        release = threading.Event()
        finished: list[str] = []

        def slow_sink(native_id: str) -> None:
            entered.set()
            release.wait(WAIT_SECONDS)
            finished.append(native_id)

        conversation = harnesses.TurnConversation(slow_sink)
        capturer = threading.Thread(target=conversation.capture, args=("thread-77",))
        capturer.start()
        self.assertTrue(entered.wait(WAIT_SECONDS))

        returned = threading.Event()

        def close_it() -> None:
            conversation.close()
            returned.set()

        closer = threading.Thread(target=close_it)
        closer.start()
        # The close is blocked on the in-flight capture, so nothing is
        # finished and the close has not returned. Reading ``closed`` here
        # would block on the same lock, which is itself the guarantee.
        self.assertFalse(returned.wait(0.2))
        self.assertEqual([], finished)

        release.set()
        capturer.join(WAIT_SECONDS)
        closer.join(WAIT_SECONDS)

        self.assertTrue(returned.is_set())
        self.assertEqual(["thread-77"], finished)
        self.assertTrue(conversation.closed)


class TurnSubmissionTests(unittest.TestCase):
    """The record of whether a worker was ever handed the turn's prompt.

    Everything downstream of it is an accounting decision about words the
    operator entrusted to the run, so the two facts it has to keep apart are
    "said once" and "never said" -- and, unlike a capture, a report that
    arrives is not something a failing sink may retract.
    """

    def test_a_submitted_prompt_reaches_the_sink(self) -> None:
        recorded: list[str] = []
        submission = harnesses.TurnSubmission(lambda: recorded.append("submitted"))

        submission.note_submitted()

        self.assertEqual(["submitted"], recorded)
        self.assertTrue(submission.submitted)

    def test_a_turn_that_never_submitted_says_so(self) -> None:
        submission = harnesses.TurnSubmission(lambda: None)

        submission.close()

        self.assertFalse(submission.submitted)

    def test_the_same_submission_reported_twice_is_recorded_once(self) -> None:
        recorded: list[str] = []
        submission = harnesses.TurnSubmission(lambda: recorded.append("submitted"))

        submission.note_submitted()
        submission.note_submitted()

        self.assertEqual(["submitted"], recorded)

    def test_a_sink_failure_does_not_unsay_the_submission(self) -> None:
        """The words are already with the worker; bookkeeping cannot recall them.

        The opposite of :class:`TurnConversation`, where a sink that fails means
        nothing was persisted and so nothing was captured. Here the sink is told
        about something that already happened out on the wire.
        """

        def explode() -> None:
            raise OSError("journal is read-only")

        submission = harnesses.TurnSubmission(explode)

        submission.note_submitted()

        self.assertTrue(submission.submitted)

    def test_a_late_report_cannot_reach_a_closed_turn(self) -> None:
        recorded: list[str] = []
        submission = harnesses.TurnSubmission(lambda: recorded.append("submitted"))
        submission.close()

        submission.note_submitted()

        self.assertEqual([], recorded)
        self.assertFalse(submission.submitted)

    def test_a_seam_that_said_nothing_denies_the_submission(self) -> None:
        submission = harnesses.TurnSubmission(lambda: None)

        self.assertIs(False, submission.verdict)

    def test_a_prompt_on_the_wire_is_neither_sent_nor_unsent(self) -> None:
        """The third state, and the only honest one while a write is happening.

        A transport says this at the last instant it could still have said
        "nothing left here". If the answer never comes, the turn ends in this
        state -- and the whole point is that ending here says nothing, because a
        turn that claimed non-submission would hand the same operator's words to
        the next worker.
        """
        recorded: list[str] = []
        submission = harnesses.TurnSubmission(lambda: recorded.append("submitted"))

        submission.note_submission_in_flight()

        self.assertIsNone(submission.verdict)
        # Nothing has been received, so nobody is told anything: the report is
        # what makes a correction *delivered*, and delivery is not this.
        self.assertEqual([], recorded)
        self.assertFalse(submission.submitted)

    def test_closing_a_turn_does_not_resolve_a_prompt_in_flight(self) -> None:
        submission = harnesses.TurnSubmission(lambda: None)
        submission.note_submission_in_flight()

        submission.close()

        self.assertIsNone(submission.verdict)

    def test_an_answered_submission_settles_what_was_in_flight(self) -> None:
        recorded: list[str] = []
        submission = harnesses.TurnSubmission(lambda: recorded.append("submitted"))
        submission.note_submission_in_flight()

        submission.note_submitted()

        self.assertIs(True, submission.verdict)
        self.assertEqual(["submitted"], recorded)

    def test_a_late_in_flight_mark_cannot_unsay_a_submission(self) -> None:
        submission = harnesses.TurnSubmission(lambda: None)
        submission.note_submitted()

        submission.note_submission_in_flight()

        self.assertIs(True, submission.verdict)

    def test_a_lifecycle_reports_the_seam_it_owns(self) -> None:
        lifecycle = harnesses.TurnLifecycle(conversation=harnesses.TurnConversation())

        self.assertIs(False, lifecycle.prompt_submitted)
        lifecycle.note_submission_in_flight()
        self.assertIsNone(lifecycle.prompt_submitted)
        lifecycle.note_prompt_submitted()
        self.assertIs(True, lifecycle.prompt_submitted)


class TurnLifecycleTests(unittest.TestCase):
    def test_a_pending_cancellation_stops_the_next_checkpoint(self) -> None:
        signal = harnesses.AbortSignal()
        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(), abort=signal
        )
        lifecycle.check_not_cancelled()

        signal.arm("operator stopped the run")

        self.assertTrue(lifecycle.cancel_due)
        with self.assertRaises(harnesses.TurnCancelled) as caught:
            lifecycle.check_not_cancelled()
        self.assertIn("operator stopped the run", str(caught.exception))

    def test_a_turn_without_a_signal_is_never_cancelled(self) -> None:
        lifecycle = harnesses.TurnLifecycle(conversation=harnesses.TurnConversation())

        lifecycle.begin()
        lifecycle.check_not_cancelled()
        lifecycle.note_cancelled(harnesses.CANCEL_STAGE_EXECUTION)

        self.assertFalse(lifecycle.cancel_due)
        self.assertIsNone(lifecycle.cancel_stage)
        lifecycle.close()
        self.assertTrue(lifecycle.conversation.closed)

    def test_completion_is_reported_only_after_capture_is_closed(self) -> None:
        signal = harnesses.AbortSignal()
        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(), abort=signal
        )
        lifecycle.begin()
        signal.arm("stop")
        seen_closed: list[bool] = []

        def observe() -> None:
            signal.wait_completed(WAIT_SECONDS)
            seen_closed.append(lifecycle.conversation.closed)

        observer = threading.Thread(target=observe)
        observer.start()
        lifecycle.close()
        observer.join(WAIT_SECONDS)

        self.assertEqual([True], seen_closed)
