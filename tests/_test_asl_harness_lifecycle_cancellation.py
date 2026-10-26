# mypy: ignore-errors
"""Harness turn cancellation windows and the submission stated on a result."""

# ruff: noqa: F403, F405
from ._test_asl_harness_lifecycle_support import *


class OrdinaryTurnTests(_TurnTestCase):
    """The lifecycle is inert on a turn that nothing cancels or captures."""

    def test_a_plain_turn_is_unchanged(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess(running=False)

        result = self.run_turn(adapter, process)

        self.assertEqual(1, self.popen_calls)
        self.assertEqual(0, result.returncode)
        self.assertFalse(result.aborted)
        self.assertIsNone(result.cancel_stage)
        self.assertIsNone(result.conversation_id)
        self.assertIsNone(result.conversation_error)
        self.assertEqual(["do the work"], adapter.submitted)

    def test_an_unarmed_signal_completes_when_the_turn_ends(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess(running=False)
        signal = harnesses.AbortSignal()

        result = self.run_turn(adapter, process, abort=signal)

        self.assertEqual(0, result.returncode)
        self.assertFalse(signal.armed)
        self.assertIsNone(signal.cancel_stage)
        # Completion means "this turn has stopped", which is true whether or
        # not anyone asked it to.
        self.assertTrue(signal.completed)


class SubmissionStatedOnTheResultTests(_TurnTestCase):
    """What the turn tells a caller that was not watching its callbacks.

    A caller holding an operator's correction has to know whether the prompt
    went out, and the callback alone cannot tell it: no callback means either
    "never sent" or "sent, and whatever would have said so did not survive".
    So the adapter answers on the result, where a turn that reached one has an
    answer and a turn that never reached one leaves the question open.
    """

    def test_a_turn_that_submitted_says_so(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess(running=False)

        result = self.run_turn(adapter, process)

        self.assertTrue(result.prompt_submitted)
        self.assertFalse(harnesses.prompt_provably_unsubmitted(result))

    def test_a_turn_cancelled_before_the_spawn_says_the_opposite(self) -> None:
        adapter = _LifecycleAdapter()
        signal = harnesses.AbortSignal()
        signal.arm("operator stopped the run")

        result = self.run_turn(adapter, _FakeProcess(), abort=signal)

        self.assertFalse(result.prompt_submitted)
        self.assertTrue(harnesses.prompt_provably_unsubmitted(result))

    def test_a_handshake_that_refused_says_the_opposite_too(self) -> None:
        signal = harnesses.AbortSignal()
        adapter = _LifecycleAdapter(
            transport=harnesses.TRANSPORT_STREAM_JSON,
            before_submit=lambda: signal.arm("operator stopped the run"),
        )

        result = self.run_turn(adapter, _FakeProcess(), abort=signal)

        self.assertFalse(result.prompt_submitted)
        self.assertTrue(harnesses.prompt_provably_unsubmitted(result))

    def test_a_one_shot_turn_cancelled_late_is_not_read_as_unsubmitted(self) -> None:
        """Where the seam and the cancellation window disagree, the seam wins.

        A turn that carries its prompt on argv submitted at the spawn, so a
        cancellation named for the handshake window says nothing about whether
        the worker got the words -- and it did. Reading the window instead
        would hand an already-delivered correction to a second worker.
        """
        signal = harnesses.AbortSignal()
        adapter = _LifecycleAdapter(
            before_submit=lambda: signal.arm("operator stopped the run")
        )

        result = self.run_turn(adapter, _FakeProcess(), abort=signal)

        self.assertEqual(harnesses.CANCEL_STAGE_HANDSHAKE, result.cancel_stage)
        self.assertTrue(result.prompt_submitted)
        self.assertFalse(harnesses.prompt_provably_unsubmitted(result))

    def test_a_protocol_failure_with_no_field_of_its_own_still_says_it(
        self,
    ) -> None:
        """The case the older refusal fields miss, and the reason for this one.

        A provider that was simply unreachable refuses nothing about the
        history, so it sets neither ``conversation_restore_refused`` nor
        ``conversation_error`` -- it is an ordinary failed turn. The seam still
        knows no prompt was sent, and saying so is what keeps that correction
        recoverable instead of merely doubtful.
        """
        adapter = _LifecycleAdapter(transport=harnesses.TRANSPORT_STREAM_JSON)
        process = _FakeProcess(running=False)

        def refuse() -> None:
            raise harnesses.HarnessError("temporary service unavailable")

        adapter._before_submit = refuse
        with self.assertRaises(harnesses.HarnessError):
            self.run_turn(adapter, process)

        # Raised rather than returned, which is the other half of the claim: a
        # turn with no result to stamp reports nothing, and the caller reads
        # that silence as the open question it is.
        self.assertTrue(process.killed)

    def test_a_result_nobody_stamped_proves_nothing(self) -> None:
        """A fabricated result is silent, and silence is not a refusal."""
        result = harnesses.HarnessResult(
            name="codex", argv=["codex"], returncode=1, stdout="", stderr=""
        )

        self.assertIsNone(result.prompt_submitted)
        self.assertFalse(harnesses.prompt_provably_unsubmitted(result))


class CancellationWindowTests(_TurnTestCase):
    """The four windows a cancellation can arrive in."""

    def test_cancelling_before_spawn_starts_nothing(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess()
        signal = harnesses.AbortSignal()
        signal.arm("operator stopped the run")

        result = self.run_turn(adapter, process, abort=signal)

        self.assertEqual(0, self.popen_calls)
        self.assertEqual([], adapter.submitted)
        self.assertTrue(result.aborted)
        self.assertEqual(137, result.returncode)
        self.assertEqual(harnesses.CANCEL_STAGE_UNSTARTED, result.cancel_stage)
        self.assertTrue(signal.completed)

    def test_cancelling_during_the_handshake_submits_nothing(self) -> None:
        signal = harnesses.AbortSignal()
        # Armed from inside the handshake, in the window a real cancellation
        # would land in: after the process exists, before work goes out.
        adapter = _LifecycleAdapter(
            before_submit=lambda: signal.arm("operator stopped the run")
        )
        process = _FakeProcess()

        result = self.run_turn(adapter, process, abort=signal)

        self.assertEqual(1, self.popen_calls)
        self.assertEqual([], adapter.submitted)
        self.assertTrue(result.aborted)
        self.assertEqual(137, result.returncode)
        self.assertEqual(harnesses.CANCEL_STAGE_HANDSHAKE, result.cancel_stage)
        self.assertEqual([process.pid], self.killed_groups)
        self.assertTrue(process.killed)
        self.assertTrue(signal.completed)

    def test_cancelling_during_execution_interrupts_the_work(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess()
        signal = harnesses.AbortSignal()

        def cancel_once_running() -> None:
            deadline = time.monotonic() + WAIT_SECONDS
            while not adapter.submitted and time.monotonic() < deadline:
                time.sleep(0.005)
            signal.arm("operator stopped the run")

        canceller = threading.Thread(target=cancel_once_running)
        canceller.start()
        result = self.run_turn(adapter, process, abort=signal)
        canceller.join(WAIT_SECONDS)

        self.assertEqual(["do the work"], adapter.submitted)
        self.assertFalse(process.wait_timed_out)
        self.assertTrue(result.aborted)
        self.assertEqual(137, result.returncode)
        self.assertEqual(harnesses.CANCEL_STAGE_EXECUTION, result.cancel_stage)
        self.assertTrue(signal.completed)

    def test_cancelling_during_shutdown_is_completed_not_left_pending(self) -> None:
        signal = harnesses.AbortSignal()
        # Armed while the transport is being torn down: the turn is past every
        # checkpoint and past its wait loop, so there is nothing left to stop.
        adapter = _LifecycleAdapter(
            transport_shutdown=lambda: signal.arm("operator stopped the run")
        )
        process = _FakeProcess(running=False)

        result = self.run_turn(adapter, process, abort=signal)

        self.assertEqual(["do the work"], adapter.submitted)
        self.assertEqual(0, result.returncode)
        self.assertFalse(result.aborted)
        self.assertTrue(signal.armed)
        self.assertTrue(signal.completed)
        self.assertEqual(harnesses.CANCEL_STAGE_SHUTDOWN, signal.cancel_stage)

    def test_cancelling_after_the_turn_cannot_revive_it(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess(running=False)
        signal = harnesses.AbortSignal()

        self.run_turn(adapter, process, abort=signal)
        signal.arm("operator stopped the run")

        self.assertTrue(signal.wait_completed(0))
        self.assertEqual(harnesses.CANCEL_STAGE_SHUTDOWN, signal.cancel_stage)
        self.assertEqual([], self.killed_groups)

    def test_a_late_callback_cannot_record_against_a_finished_turn(self) -> None:
        adapter = _LifecycleAdapter(captures=True, native_id="thread-77")
        process = _FakeProcess(running=False)
        recorded: list[str] = []

        result = self.run_turn(adapter, process, on_conversation=recorded.append)

        self.assertEqual("thread-77", result.conversation_id)
        lifecycle = adapter.lifecycles[0]
        with self.assertRaises(harnesses.ConversationCaptureError):
            lifecycle.capture_conversation("thread-78")
        with self.assertRaises(harnesses.ConversationCaptureError):
            lifecycle.capture_conversation("thread-77")
        self.assertEqual(["thread-77"], recorded)


class PromptSubmissionBoundaryTests(_TurnTestCase):
    """Spawning a worker and handing it the prompt are two separate moments.

    They coincide for a one-shot turn, whose prompt was on the argv the process
    was started with. For every protocol transport a handshake stands between
    them, and a handshake can end in a live process that was never told
    anything -- cancelled, or refused by the provider. A caller that reads the
    spawn as the submission would record those turns as having received words
    nothing ever read, which is why the turn reports the two separately.
    """

    def _hooks(self) -> tuple[list[int], list[int]]:
        """Spawns and submissions, each recording how far the other had got."""
        spawned: list[int] = []
        submitted: list[int] = []
        self.spawned, self.submitted = spawned, submitted
        return spawned, submitted

    def _run(self, adapter: _LifecycleAdapter, process: _FakeProcess, **kwargs: Any):
        spawned, submitted = self._hooks()
        return self.run_turn(
            adapter,
            process,
            on_spawn=spawned.append,
            on_prompt_submitted=lambda: submitted.append(len(spawned)),
            **kwargs,
        )

    def test_a_prompt_on_the_argv_is_submitted_at_spawn(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess(running=False)

        result = self._run(adapter, process)

        # One-shot: there is no handshake that could refuse, so the pid is the
        # whole proof. Reported after the spawn and not instead of it, because
        # a caller that acts on the submission may need the worker's identity.
        self.assertEqual([process.pid], self.spawned)
        self.assertEqual([1], self.submitted)
        self.assertEqual(0, result.returncode)

    def test_a_protocol_turn_reports_submission_from_its_handshake(self) -> None:
        seen_mid_handshake: list[int] = []
        adapter = _LifecycleAdapter(
            transport=harnesses.TRANSPORT_APP_SERVER,
            before_submit=lambda: seen_mid_handshake.append(len(self.submitted)),
        )
        process = _FakeProcess(running=False)

        result = self._run(adapter, process)

        # Spawned, into a handshake that had not yet submitted anything.
        self.assertEqual([process.pid], self.spawned)
        self.assertEqual([0], seen_mid_handshake)
        self.assertEqual([1], self.submitted)
        self.assertEqual(["do the work"], adapter.submitted)
        self.assertEqual(0, result.returncode)

    def test_a_cancellation_between_spawn_and_submission_hands_over_nothing(
        self,
    ) -> None:
        signal = harnesses.AbortSignal()
        adapter = _LifecycleAdapter(
            transport=harnesses.TRANSPORT_APP_SERVER,
            before_submit=lambda: signal.arm("operator stopped the run"),
        )
        process = _FakeProcess()

        result = self._run(adapter, process, abort=signal)

        # A process existed and was killed, and nothing was ever submitted to
        # it. Both are true at once, and only the second one is about the words.
        self.assertEqual([process.pid], self.spawned)
        self.assertEqual([], self.submitted)
        self.assertEqual([], adapter.submitted)
        self.assertEqual(harnesses.CANCEL_STAGE_HANDSHAKE, result.cancel_stage)
        self.assertTrue(process.killed)

    def test_a_handshake_the_provider_refused_hands_over_nothing(self) -> None:
        adapter = _LifecycleAdapter(
            transport=harnesses.TRANSPORT_APP_SERVER,
            before_submit=_raise(RuntimeError("app-server refused the thread")),
        )
        process = _FakeProcess()

        with self.assertRaises(RuntimeError):
            self._run(adapter, process)

        self.assertEqual([process.pid], self.spawned)
        self.assertEqual([], self.submitted)

    def test_a_report_that_arrives_after_the_turn_is_ignored(self) -> None:
        adapter = _LifecycleAdapter(transport=harnesses.TRANSPORT_APP_SERVER)
        process = _FakeProcess(running=False)

        self._run(adapter, process)
        adapter.lifecycles[0].note_prompt_submitted()

        # The turn is over and its carry has already been settled against what
        # the turn reported. A straggling transport thread may not reopen that.
        self.assertEqual([1], self.submitted)


class PostHandshakeEscapeTests(_TurnTestCase):
    """A failure between the handshake and the stream readers kills the turn."""

    def test_an_interrupt_in_the_transport_callback_does_not_orphan_the_harness(
        self,
    ) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess()
        transports: list[Any] = []

        def interrupt(transport: Any) -> None:
            transports.append(transport)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_turn(adapter, process, on_transport=interrupt)

        # The harness runs in a session of its own, so nothing else would ever
        # stop it once the run gave up the plan lock.
        self.assertEqual([process.pid], self.killed_groups)
        self.assertTrue(process.killed)
        self.assertFalse(process.wait_timed_out)
        self.assertEqual(1, transports[0].shutdown_calls)

    def test_a_stream_start_failure_does_not_orphan_the_harness(self) -> None:
        adapter = _LifecycleAdapter()
        process = _FakeProcess()

        with (
            mock.patch.object(
                harness_execution.harness_streams,
                "start_turn_streams",
                side_effect=RuntimeError("can't start new thread"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.run_turn(adapter, process)

        self.assertEqual([process.pid], self.killed_groups)
        self.assertTrue(process.killed)
