# mypy: ignore-errors
"""Focused test_asl_harness_lifecycle test group 3."""

# ruff: noqa: F403, F405
from ._test_asl_harness_lifecycle_support import *


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
                harness_execution.subprocess, "Popen", side_effect=FileNotFoundError
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

    def adapter(self) -> harness_claude.ClaudeAdapter:
        return harness_claude.ClaudeAdapter(executable="claude")

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
        submitted: list[str] = []

        def explode(native_id: str) -> None:
            raise OSError("ledger is read-only")

        result = self.run_turn(
            adapter,
            process,
            on_conversation=explode,
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        self.assertEqual([], process.stdin.written)
        # The pipe is what the prompt travels down, so nothing written is
        # nothing submitted -- and the turn says so rather than leaving the
        # spawn to be read as delivery.
        self.assertEqual([], submitted)
        self.assertIn("ledger is read-only", result.conversation_error or "")
        self.assertIsNone(result.conversation_id)
        self.assertTrue(process.killed)

    def test_a_prompt_written_to_the_stream_is_a_submission(self) -> None:
        adapter = self.adapter()
        process = _FakeProcess(running=False)
        submitted: list[str] = []

        result = self.run_turn(
            adapter,
            process,
            on_conversation=lambda native_id: None,
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        self.assertIn("do the work", "".join(process.stdin.written))
        self.assertEqual(["submitted"], submitted)
        self.assertEqual(0, result.returncode)

    def test_an_audit_turn_says_it_has_no_conversation_to_offer(self) -> None:
        adapter = self.adapter()
        process = _FakeProcess(running=False)
        captured: list[str] = []

        with mock.patch.object(
            harness_execution.subprocess, "Popen", return_value=process
        ):
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
