"""The turn lifecycle: conversation capture, resume intent, and cancellation.

Everything here runs in-process. The turn tests drive the real
``HarnessAdapter.run`` boundary with a fake process object and fake transports
rather than a fake executable, so the module spawns nothing and belongs in the
portable shard while still exercising the code that owns the live process.

``_kill_process_group`` is patched out on every cancellation path: it signals a
whole process group by PID, and a fake process's invented PID is a real PID on
the machine running the tests.
"""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import conversations, guidance_delivery, harnesses

#: Every bounded wait in this module. Long enough that a loaded machine does
#: not fail a correct implementation, short enough that a wrong one fails
#: rather than hangs.
WAIT_SECONDS = 10.0

#: What "returns promptly" means for cancellation initiation. Generous by two
#: orders of magnitude against the thing it rules out -- a call that blocks
#: behind a transport holding a lock for ``WAIT_SECONDS`` -- so it fails on the
#: coupling it is about rather than on scheduler noise.
PROMPT_SECONDS = 2.0


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


def _raise(error: BaseException) -> Any:
    """A zero-argument hook that fails, for the handshake-blew-up paths."""

    def hook() -> None:
        raise error

    return hook


class _FakePipe(io.StringIO):
    """A stream that is at EOF at once, like a harness that printed nothing."""

    def __init__(self, initial: str = "") -> None:
        super().__init__(initial)
        #: Kept beside the buffer so a test can still see what a turn wrote
        #: after the pipe has been closed.
        self.written: list[str] = []

    def write(self, text: str) -> int:  # type: ignore[override]
        self.written.append(text)
        return super().write(text)


class _FakeProcess:
    """An in-process stand-in for the harness child.

    It stays "running" -- blocked in :meth:`wait` -- until something finishes
    it, which is what lets a test hold a turn open in the window it is about.
    """

    def __init__(self, *, running: bool = True) -> None:
        self.pid = 424243
        self.returncode: int | None = None
        self.stdin = _FakePipe("")
        self.stdout = _FakePipe("")
        self.stderr = _FakePipe("")
        self.killed = False
        self.wait_timed_out = False
        self._exit = threading.Event()
        if not running:
            self._exit.set()

    def finish(self) -> None:
        self._exit.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        # The abort poll loop waits in short slices and expects the same
        # exception a real ``Popen`` raises; only an unbounded wait may block
        # to the end of the test's patience.
        if not self._exit.wait(WAIT_SECONDS if timeout is None else timeout):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="fake-harness", timeout=timeout)
            self.wait_timed_out = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.finish()

    def terminate(self) -> None:
        self.kill()


class _FakeTransport(harnesses.HarnessTransport):
    """A protocol conversation that records whether it was shut down."""

    def __init__(self, processor: Any, on_shutdown: Any = None) -> None:
        self.shutdown_calls = 0
        self._processor = processor
        self._on_shutdown = on_shutdown

    def output_processor(self) -> Any:
        return self._processor

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self._on_shutdown is not None:
            self._on_shutdown()


class _LifecycleAdapter(harnesses.HarnessAdapter):
    """A harness whose handshake is scripted by the test that runs it.

    Its ``open_transport`` is the shape a wired protocol adapter has: learn the
    conversation, report it, check for a cancellation, then submit. What the
    test varies is which of those steps fails.
    """

    name = "codex"
    skill_prefix = "$"

    def __init__(
        self,
        *,
        native_id: str | None = None,
        captures: bool = False,
        resumes: bool = False,
        before_submit: Any = None,
        transport_shutdown: Any = None,
    ) -> None:
        super().__init__(executable="fake-harness")
        self.captures_conversations = captures
        self.resumes_conversations = resumes
        self.native_id = native_id
        self.submitted: list[str] = []
        self.lifecycles: list[harnesses.TurnLifecycle] = []
        self.resumed: list[str | None] = []
        self._before_submit = before_submit
        self._transport_shutdown = transport_shutdown

    def build_argv(self, **kwargs: Any) -> list[str]:
        return [self.executable]

    def open_transport(
        self,
        *,
        process: Any,
        invocation: harnesses.HarnessInvocation,
        prompt: str,
        lifecycle: harnesses.TurnLifecycle | None = None,
    ) -> harnesses.HarnessTransport | None:
        assert lifecycle is not None
        self.lifecycles.append(lifecycle)
        self.resumed.append(invocation.resume_conversation_id)
        if self.native_id is not None:
            lifecycle.capture_conversation(self.native_id)
        if self._before_submit is not None:
            self._before_submit()
        lifecycle.check_not_cancelled()
        self.submitted.append(prompt)
        return _FakeTransport(self.output_processor(), self._transport_shutdown)


class _TurnTestCase(unittest.TestCase):
    """Shared plumbing for the tests that drive a whole turn."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # The post-turn sweep shells out to `ps`; nothing here is about
        # descendants, and the module stays spawn-free without it.
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        # A fake process's PID is a real PID on this machine, and the group
        # kill signals by PID. Stand in for it with the child's own kill.
        self.killed_groups: list[int] = []
        self.popen_calls = 0

        def kill_group(process: Any) -> None:
            self.killed_groups.append(process.pid)
            process.kill()

        killer = mock.patch.object(harnesses, "_kill_process_group", kill_group)
        killer.start()
        self.addCleanup(killer.stop)

    def run_turn(
        self,
        adapter: harnesses.HarnessAdapter,
        process: _FakeProcess,
        **kwargs: Any,
    ) -> Any:
        with mock.patch.object(
            harnesses.subprocess, "Popen", return_value=process
        ) as popen:
            try:
                return adapter.run(
                    cwd=self.tmp,
                    plan_dir=self.tmp,
                    prompt="do the work",
                    last_message_path=self.tmp / "last.txt",
                    **kwargs,
                )
            finally:
                self.popen_calls = popen.call_count


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


class CancellationIndependenceTests(_TurnTestCase):
    """Initiation must not queue behind the live-guidance lock."""

    def test_a_request_returns_while_a_guidance_delivery_is_in_flight(self) -> None:
        turn = guidance_delivery.TurnIdentity(
            run_id="run-1", session_id="7", turn_index=2, harness="codex"
        )
        endpoint = guidance_delivery.LiveDeliveryEndpoint(turn)
        signal = harnesses.AbortSignal()
        in_transport = threading.Event()
        finish_transport = threading.Event()
        self.addCleanup(finish_transport.set)

        def slow_transport(
            request: guidance_delivery.GuidanceRequest,
        ) -> guidance_delivery.GuidanceOutcome:
            in_transport.set()
            finish_transport.wait(WAIT_SECONDS)
            return guidance_delivery.DELIVERED

        endpoint.open(slow_transport)
        deliverer = threading.Thread(
            target=endpoint.submit,
            args=(
                guidance_delivery.GuidanceRequest(
                    guidance_id="11111111-1111-4111-8111-111111111111",
                    seq=1,
                    text="prefer the smaller change",
                    turn=turn,
                ),
            ),
        )
        deliverer.start()
        self.assertTrue(in_transport.wait(WAIT_SECONDS))

        started = time.monotonic()
        signal.arm("operator stopped the run")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, PROMPT_SECONDS)
        self.assertTrue(signal.armed)
        # The transport is still holding the endpoint, which is exactly the
        # state this test needed the request to be independent of.
        self.assertFalse(finish_transport.is_set())
        finish_transport.set()
        deliverer.join(WAIT_SECONDS)


class FailedTurnCompletionTests(_TurnTestCase):
    """A turn that never starts still answers whoever is waiting on it."""

    def test_a_missing_executable_completes_the_request(self) -> None:
        adapter = _LifecycleAdapter()
        signal = harnesses.AbortSignal()
        signal.arm("operator stopped the run", grace_seconds=60.0)

        with (
            mock.patch.object(
                harnesses.subprocess, "Popen", side_effect=FileNotFoundError
            ),
            self.assertRaises(harnesses.HarnessError),
        ):
            adapter.run(
                cwd=self.tmp,
                plan_dir=self.tmp,
                prompt="do the work",
                abort=signal,
            )

        self.assertTrue(signal.completed)

    def test_a_failed_handshake_stops_the_process_it_spawned(self) -> None:
        adapter = _LifecycleAdapter(
            before_submit=_raise(RuntimeError("app-server refused the thread"))
        )
        process = _FakeProcess()
        signal = harnesses.AbortSignal()

        with self.assertRaises(RuntimeError):
            self.run_turn(adapter, process, abort=signal)

        self.assertEqual([], adapter.submitted)
        self.assertTrue(process.killed)
        self.assertEqual([process.pid], self.killed_groups)
        self.assertTrue(signal.completed)


class ConversationCaptureTurnTests(_TurnTestCase):
    def test_a_captured_conversation_is_persisted_before_work_is_submitted(
        self,
    ) -> None:
        order: list[str] = []
        adapter = _LifecycleAdapter(captures=True, native_id="thread-77")
        process = _FakeProcess(running=False)

        def persist(native_id: str) -> None:
            order.append(f"persisted:{native_id}")

        original = adapter.open_transport

        def watched(**kwargs: Any) -> Any:
            transport = original(**kwargs)
            order.append("submitted")
            return transport

        with mock.patch.object(adapter, "open_transport", watched):
            result = self.run_turn(adapter, process, on_conversation=persist)

        self.assertEqual(["persisted:thread-77", "submitted"], order)
        self.assertEqual("thread-77", result.conversation_id)
        self.assertIsNone(result.conversation_error)
        self.assertEqual(0, result.returncode)

    def test_a_persistence_failure_abandons_the_turn(self) -> None:
        adapter = _LifecycleAdapter(captures=True, native_id="thread-77")
        process = _FakeProcess()

        def explode(native_id: str) -> None:
            raise OSError("ledger is read-only")

        result = self.run_turn(adapter, process, on_conversation=explode)

        self.assertEqual([], adapter.submitted)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(result.aborted)
        self.assertIsNone(result.cancel_stage)
        self.assertIsNone(result.conversation_id)
        self.assertIn("ledger is read-only", result.conversation_error or "")
        self.assertIn("ledger is read-only", result.stderr)
        self.assertEqual([process.pid], self.killed_groups)
        self.assertTrue(process.killed)

    def test_a_capture_sink_is_refused_by_an_adapter_that_cannot_report_one(
        self,
    ) -> None:
        adapter = _LifecycleAdapter(captures=False)
        process = _FakeProcess(running=False)

        with self.assertRaises(harnesses.HarnessError) as caught:
            self.run_turn(adapter, process, on_conversation=lambda native_id: None)

        self.assertIn("codex", str(caught.exception))
        self.assertEqual(0, self.popen_calls)

    def test_a_resume_intent_is_refused_by_an_adapter_that_cannot_honour_it(
        self,
    ) -> None:
        adapter = _LifecycleAdapter(resumes=False)
        process = _FakeProcess(running=False)
        invocation = adapter.build_invocation(
            cwd=self.tmp,
            plan_dir=self.tmp,
            prompt="do the work",
            resume_conversation_id="thread-77",
        )

        with self.assertRaises(harnesses.HarnessError) as caught:
            self.run_turn(adapter, process, invocation=invocation)

        self.assertIn("thread-77", str(caught.exception))
        self.assertEqual(0, self.popen_calls)

    def test_a_resume_intent_reaches_the_transport_that_can_honour_it(self) -> None:
        adapter = _LifecycleAdapter(resumes=True)
        process = _FakeProcess(running=False)
        invocation = adapter.build_invocation(
            cwd=self.tmp,
            plan_dir=self.tmp,
            prompt="do the work",
            resume_conversation_id="thread-77",
        )

        result = self.run_turn(adapter, process, invocation=invocation)

        self.assertEqual(["thread-77"], adapter.resumed)
        self.assertEqual(0, result.returncode)


class ClaudeContinuityTurnTests(_TurnTestCase):
    """A whole claude turn, from the identifier on argv to the result."""

    def adapter(self) -> harnesses.ClaudeAdapter:
        return harnesses.ClaudeAdapter(executable="claude")

    def init_record(self, session_id: str) -> str:
        return (
            json.dumps({"type": "system", "subtype": "init", "session_id": session_id})
            + "\n"
        )

    def test_a_turn_records_the_conversation_it_stated_on_argv(self) -> None:
        adapter = self.adapter()
        process = _FakeProcess(running=False)
        captured: list[str] = []
        invocation = adapter.build_invocation(
            cwd=self.tmp, plan_dir=self.tmp, prompt="do the work"
        )
        assigned = invocation.assigned_conversation_id
        assert assigned is not None
        process.stdout = _FakePipe(self.init_record(assigned))

        result = self.run_turn(
            adapter,
            process,
            invocation=invocation,
            on_conversation=captured.append,
        )

        self.assertEqual([assigned], captured)
        self.assertEqual(assigned, result.conversation_id)
        self.assertIsNone(result.continuity_unavailable)

    def test_a_turn_that_ran_elsewhere_reports_no_conversation(self) -> None:
        adapter = self.adapter()
        process = _FakeProcess(running=False)
        invocation = adapter.build_invocation(
            cwd=self.tmp, plan_dir=self.tmp, prompt="do the work"
        )
        assigned = invocation.assigned_conversation_id
        assert assigned is not None
        process.stdout = _FakePipe(self.init_record("some-other-session"))

        result = self.run_turn(
            adapter,
            process,
            invocation=invocation,
            on_conversation=lambda native_id: None,
        )

        # The work ran, so the turn is not retracted -- but nothing may resume
        # from an identifier the CLI declined to use.
        self.assertIn("session mismatch", result.continuity_unavailable or "")
        self.assertIn(assigned, result.continuity_unavailable or "")
        self.assertIn("some-other-session", result.continuity_unavailable or "")
        self.assertIsNone(result.conversation_id)
        self.assertIn("claude session mismatch", result.stderr)

    def test_a_turn_that_could_not_write_its_conversation_submits_nothing(
        self,
    ) -> None:
        adapter = self.adapter()
        process = _FakeProcess()

        def explode(native_id: str) -> None:
            raise OSError("ledger is read-only")

        result = self.run_turn(adapter, process, on_conversation=explode)

        self.assertEqual([], process.stdin.written)
        self.assertIn("ledger is read-only", result.conversation_error or "")
        self.assertIsNone(result.conversation_id)
        self.assertTrue(process.killed)

    def test_an_audit_turn_says_it_has_no_conversation_to_offer(self) -> None:
        adapter = self.adapter()
        process = _FakeProcess(running=False)
        captured: list[str] = []

        with mock.patch.object(harnesses.subprocess, "Popen", return_value=process):
            result = adapter.run(
                cwd=self.tmp,
                plan_dir=None,
                prompt="do the work",
                last_message_path=self.tmp / "last.txt",
                access=harnesses.audit_read_only_access((self.tmp,)),
                on_conversation=captured.append,
            )

        self.assertEqual([], captured)
        self.assertIn("persists no session", result.continuity_unavailable or "")
        self.assertIn("persists no session", result.stderr)

    def test_the_overlay_is_still_gone_when_the_turn_is_over(self) -> None:
        adapter = self.adapter()
        process = _FakeProcess(running=False)
        overlays: list[Path] = []
        original = adapter.open_transport

        def watched(**kwargs: Any) -> Any:
            overlay_dir = kwargs["invocation"].overlay_dir
            if overlay_dir is not None:
                overlays.append(overlay_dir)
            return original(**kwargs)

        with mock.patch.object(adapter, "open_transport", watched):
            self.run_turn(adapter, process, on_conversation=lambda native_id: None)

        self.assertEqual(1, len(overlays))
        self.assertFalse(overlays[0].exists())


class ShippedAdapterCapabilityTests(unittest.TestCase):
    """No shipped adapter claims continuity it has not been wired for."""

    #: The adapters whose transports actually capture and resume today. Named
    #: rather than derived, so wiring a harness is a deliberate edit here and
    #: not something a flipped class flag can do quietly.
    WIRED = frozenset({"codex", "claude"})

    def test_only_wired_adapters_capture_or_resume(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                wired = name in self.WIRED
                self.assertEqual(wired, adapter.captures_conversations)
                self.assertEqual(wired, adapter.resumes_conversations)
                self.assertEqual(wired, harnesses.continues_conversations(name))

    def test_both_halves_are_wired_together(self) -> None:
        """Capture without resume records what nothing can use; resume without
        capture continues a conversation the run never wrote down."""
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                self.assertEqual(
                    adapter.captures_conversations, adapter.resumes_conversations
                )

    def test_an_unknown_harness_does_not_continue_conversations(self) -> None:
        self.assertFalse(harnesses.continues_conversations("nonesuch"))

    def test_the_capability_report_follows_the_adapters(self) -> None:
        """One producer: the report cannot promise what the turn would refuse."""
        report = conversations.capability_report()

        reported = {
            capability.harness: capability.state for capability in report.capabilities
        }
        self.assertEqual(set(harnesses.HARNESS_NAMES), set(reported))
        for name, state in reported.items():
            with self.subTest(harness=name):
                expected = (
                    "available" if harnesses.continues_conversations(name) else "staged"
                )
                self.assertEqual(expected, state)

    def test_every_adapter_accepts_a_lifecycle_on_open_transport(self) -> None:
        """The seam is on the boundary, not on one adapter that opted in."""
        lifecycle = harnesses.TurnLifecycle(conversation=harnesses.TurnConversation())
        process = _FakeProcess(running=False)
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                invocation = harnesses.HarnessInvocation(
                    name=adapter.name, argv=[adapter.executable], cwd=Path.cwd()
                )
                transport = adapter.open_transport(
                    process=process,  # type: ignore[arg-type]
                    invocation=invocation,
                    prompt="do the work",
                    lifecycle=lifecycle,
                )
                # An exec invocation opens no protocol conversation; copilot's
                # transport exists only to decline a live steer.
                self.assertIsNone(lifecycle.conversation.native_id)
                if transport is not None:
                    self.assertIsInstance(transport, harnesses.HarnessTransport)


if __name__ == "__main__":
    unittest.main()
