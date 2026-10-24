# mypy: ignore-errors
"""Focused test_asl_copilot_resume test group 2: cancel and adapter."""

# ruff: noqa: F403, F405
from ._test_asl_copilot_resume_support import *
from ._test_asl_copilot_resume_support import (
    _FakeProcess,
    _guidance_request,
    _RenderingProcess,
    _SurvivingProcess,
)


class CancelTests(unittest.TestCase):
    """``session/cancel``: a notification, and only against a live turn."""

    def held(
        self,
        *,
        process_class: type[_RenderingProcess] = _RenderingProcess,
        **overrides: Any,
    ) -> tuple[_RenderingProcess, Any]:
        process = process_class(
            resume_responder(hold_prompt=True, updates=live_activity())
        )
        session = build_session(process, **overrides)
        self.addCleanup(session.shutdown)
        session.start()
        wait_until(lambda: session.prompt_submitted, timeout=WAIT_SECONDS)
        return process, session

    def test_a_turn_that_was_never_submitted_is_not_cancelled(self) -> None:
        process = _RenderingProcess(resume_responder())
        session = build_session(process)

        self.assertFalse(session.cancel())
        self.assertEqual([], process.methods())
        self.assertFalse(session.cancel_requested)

    def test_a_live_turn_is_cancelled_by_a_notification_naming_the_session(
        self,
    ) -> None:
        process, session = self.held()

        self.assertTrue(session.cancel())

        cancel = process.sent("session/cancel")
        self.assertEqual(SESSION_ID, cancel["params"]["sessionId"])
        # A notification, not a request: there is nothing to answer, and an id
        # here would leave the client waiting for a reply the agent owes it.
        self.assertNotIn("id", cancel)
        self.assertTrue(session.cancel_requested)

    def test_the_pending_prompt_is_what_acknowledges_the_cancellation(self) -> None:
        _, session = self.held()

        session.cancel()

        wait_until(lambda: session.turn_finished, timeout=WAIT_SECONDS)
        self.assertEqual(copilot_acp.STOP_REASON_CANCELLED, session.stop_reason)
        # Stopped, not broken.
        self.assertIsNone(session.protocol_error)

    def test_a_finished_turn_is_not_cancelled(self) -> None:
        process = _RenderingProcess(resume_responder())
        session = run_resume(process, resume=None)

        self.assertFalse(session.cancel())
        self.assertNotIn("session/cancel", process.methods())

    def test_a_dead_process_is_not_cancelled(self) -> None:
        process, session = self.held()
        process.returncode = 0

        self.assertFalse(session.cancel())
        self.assertNotIn("session/cancel", process.methods())

    def test_a_cancellation_that_cannot_be_written_is_not_an_error(self) -> None:
        process, session = self.held(
            process_class=_SurvivingProcess, shutdown_grace=0.5
        )
        session._close_stdin()

        self.assertFalse(session.cancel())
        # Reported as not sent rather than recorded as a failure: the turn is
        # ending regardless, and a protocol error here would restate a
        # cancelled turn as a broken one.
        self.assertIsNone(session.protocol_error)
        self.assertTrue(session.cancel_requested)
        self.assertNotIn("session/cancel", process.methods())


class AdapterContinuityTests(unittest.TestCase):
    """What the copilot adapter now claims, and what it still refuses."""

    def setUp(self) -> None:
        self.adapter = harness_copilot.CopilotAdapter()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def invocation(self, **overrides: Any) -> harnesses.HarnessInvocation:
        options: dict[str, Any] = {
            "cwd": self.tmp,
            "plan_dir": self.tmp,
            "prompt": PROMPT,
        }
        options.update(overrides)
        return self.adapter.build_invocation(**options)

    def audit_invocation(self, **overrides: Any) -> harnesses.HarnessInvocation:
        # An audit turn takes read roots and no writable plan directory, which
        # ``check_access`` enforces before anything here is reached.
        return self.invocation(
            plan_dir=None,
            access=harnesses.audit_read_only_access((self.tmp,)),
            **overrides,
        )

    def test_the_adapter_reports_both_halves_of_continuity(self) -> None:
        self.assertTrue(self.adapter.captures_conversations)
        self.assertTrue(self.adapter.resumes_conversations)
        self.assertTrue(harnesses.continues_conversations("copilot"))

    def test_the_capability_report_now_offers_copilot(self) -> None:
        report = conversations.capability_report()

        self.assertIn("copilot", report.available_harnesses)

    def test_a_worker_invocation_carries_the_resume_intent_onto_the_transport(
        self,
    ) -> None:
        invocation = self.invocation(resume_conversation_id=SESSION_ID)

        self.assertEqual(harnesses.TRANSPORT_ACP, invocation.transport)
        self.assertEqual(SESSION_ID, invocation.resume_conversation_id)

    def test_an_audit_turn_refuses_to_resume_a_conversation(self) -> None:
        with self.assertRaises(harnesses.HarnessError) as caught:
            self.audit_invocation(resume_conversation_id=SESSION_ID)

        self.assertIn("one-shot", str(caught.exception))
        self.assertIn(SESSION_ID, str(caught.exception))

    def test_a_one_shot_transport_refuses_a_resume_assembled_by_hand(self) -> None:
        invocation = dataclasses.replace(
            self.audit_invocation(),
            resume_conversation_id=SESSION_ID,
        )

        with self.assertRaises(harnesses.HarnessError) as caught:
            self.adapter.open_transport(
                process=_FakeProcess(),
                invocation=invocation,
                prompt=PROMPT,
            )

        self.assertIn(SESSION_ID, str(caught.exception))


class AdapterResumeTransportTests(unittest.TestCase):
    """The adapter resuming, abandoning, and cancelling on a real pipe."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.record = self.tmp / "record.json"
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        env = mock.patch.dict("os.environ", {"FAKE_ACP_RECORD": str(self.record)})
        env.start()
        self.addCleanup(env.stop)

    def _adapter(self, mode: str = "success") -> harness_copilot.CopilotAdapter:
        os.environ["FAKE_ACP_MODE"] = mode
        executable = self.tmp / "copilot"
        shutil.copyfile(FAKE_ACP_FIXTURE, executable)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return harness_copilot.CopilotAdapter(executable=str(executable))

    def _run(
        self,
        adapter: harness_copilot.CopilotAdapter,
        *,
        resume: str | None = SESSION_ID,
        **kwargs: Any,
    ) -> Any:
        options: dict[str, Any] = {
            "cwd": self.tmp,
            "plan_dir": self.plan_dir,
            "prompt": PROMPT,
            "timeout": WAIT_SECONDS,
            "unsafe_bypass": True,
        }
        options.update(kwargs)
        if resume is not None:
            options["invocation"] = adapter.build_invocation(
                cwd=options["cwd"],
                plan_dir=options["plan_dir"],
                prompt=options["prompt"],
                unsafe_bypass=True,
                resume_conversation_id=resume,
            )
        return adapter.run(**options)

    def _recorded(self) -> dict[str, Any]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_a_resumed_worker_turn_restores_the_conversation_and_runs_it(self) -> None:
        captured: list[str] = []

        result = self._run(self._adapter("resume"), on_conversation=captured.append)

        self.assertEqual(0, result.returncode)
        self.assertEqual(harnesses.TRANSPORT_ACP, result.transport)
        self.assertEqual([SESSION_ID], captured)
        self.assertEqual(SESSION_ID, result.conversation_id)
        load = self._recorded()["load_params"]
        self.assertEqual(SESSION_ID, load["sessionId"])
        self.assertEqual(str(self.tmp), load["cwd"])
        self.assertEqual([], load["mcpServers"])
        # Both counts on the turn's own stderr: the transcript proves the
        # conversation came back, and the new activity is the only evidence the
        # resumed worker did anything with it.
        self.assertIn(
            f"copilot acp resumed conversation {SESSION_ID}: "
            "3 transcript updates replayed, 7 new",
            result.stderr,
        )

    def test_the_replayed_transcript_never_reaches_the_turns_log(self) -> None:
        result = self._run(self._adapter("resume"))

        self.assertNotIn("the prompt of the turn that came before", result.stdout)
        self.assertNotIn("what the earlier turn answered", result.stdout)
        self.assertNotIn("an earlier command", result.stdout)
        self.assertEqual("working\n", result.last_message)

    def test_an_agent_without_the_capability_does_not_start_a_fresh_turn(self) -> None:
        result = self._run(self._adapter("no-load-capability"))

        self.assertNotEqual(0, result.returncode)
        self.assertIn(copilot_acp.AGENT_CAPABILITY_LOAD_SESSION, result.stderr)
        # The turn was never spent, which is the point of refusing here.
        self.assertIsNone(self._recorded()["prompt"])

    def test_a_session_the_agent_no_longer_has_fails_the_turn(self) -> None:
        result = self._run(self._adapter("load-missing"))

        self.assertNotEqual(0, result.returncode)
        self.assertIn(f"could not restore conversation {SESSION_ID}", result.stderr)
        self.assertIsNone(self._recorded()["prompt"])

    def test_a_restore_that_answers_with_a_different_session_is_refused(self) -> None:
        result = self._run(self._adapter("load-drift"))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("acp-session-somewhere-else", result.stderr)
        self.assertIsNone(self._recorded()["prompt"])

    def test_a_conversation_that_cannot_be_recorded_abandons_the_turn(self) -> None:
        def sink(native_id: str) -> None:
            raise RuntimeError("the ledger is unwritable")

        result = self._run(self._adapter("resume"), on_conversation=sink)

        self.assertEqual(70, result.returncode)
        self.assertIsNone(result.conversation_id)
        self.assertIn("the ledger is unwritable", result.conversation_error or "")
        self.assertIn("turn abandoned", result.stderr)
        self.assertIsNone(self._recorded()["prompt"])

    def test_a_cancelled_turn_is_said_to_the_conversation(self) -> None:
        adapter = self._adapter("cancel")
        failures: list[BaseException] = []

        def cancel_when_live(transport: harnesses.HarnessTransport | None) -> None:
            # Read off the transport the adapter actually opened, because the
            # cancellation has to wait for the submission the driver thread
            # makes and there is no other way to see it happen.
            assert isinstance(transport, harness_copilot._CopilotAcpTransport)
            session = transport.session

            def stop() -> None:
                try:
                    wait_until(lambda: session.prompt_submitted, timeout=WAIT_SECONDS)
                    self.assertTrue(transport.interrupt())
                except BaseException as error:
                    failures.append(error)

            worker = threading.Thread(target=stop, name="cancel-copilot")
            worker.start()
            self.addCleanup(worker.join)

        result = self._run(adapter, resume=None, on_transport=cancel_when_live)

        self.assertEqual([], failures)
        self.assertEqual([{"sessionId": SESSION_ID}], self._recorded()["cancelled"])
        self.assertNotEqual(0, result.returncode)
        self.assertIn("stopped early: cancelled", result.stderr)
        self.assertIn("cancelled the turn in flight", result.stderr)

    def test_an_audit_turn_asked_for_a_conversation_says_it_has_none(self) -> None:
        captured: list[str] = []

        result = self._run(
            self._adapter(),
            resume=None,
            plan_dir=None,
            access=harnesses.audit_read_only_access((self.tmp,)),
            unsafe_bypass=False,
            on_conversation=captured.append,
        )

        self.assertEqual([], captured)
        self.assertIsNone(result.conversation_id)
        self.assertIn("names no conversation", result.continuity_unavailable or "")
        self.assertIn("names no conversation", result.stderr)

    def test_guidance_on_a_resumed_turn_still_defers(self) -> None:
        outcomes: list[guidance_delivery.GuidanceOutcome] = []

        def steer(transport: harnesses.HarnessTransport | None) -> None:
            assert transport is not None
            deliver = transport.guidance_transport()
            assert deliver is not None
            outcomes.append(deliver(_guidance_request()))

        self._run(self._adapter("resume"), on_transport=steer)

        self.assertEqual(
            [guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER],
            [outcome.reason for outcome in outcomes],
        )
