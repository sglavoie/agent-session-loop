"""Focused test_asl_claude_stream test group 4."""

from __future__ import annotations

from .support_asl_claude_stream_base import (
    GUIDANCE_ID,
    HOOK_WAIT_SECONDS,
    Any,
    Path,
    SimpleNamespace,
    TempDirTestCase,
    _Process,
    _Stdin,
    claude_stream,
    guidance_delivery,
    harness_claude,
    harness_copilot,
    harness_transport,
    harnesses,
    json,
    request_for,
    subprocess,
    sys,
    unittest,
    uuid,
)


class GuidanceHookScriptTests(TempDirTestCase):
    """The hook as the CLI runs it: a script, a payload, a JSON answer."""

    def setUp(self) -> None:
        super().setUp()
        self.spool = claude_stream.ClaudeGuidanceSpool(self.tmp / "spool")

    def run_hook(
        self, *, spool_root: Path | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(claude_stream.hook_script_path()),
                str(self.spool.root if spool_root is None else spool_root),
            ],
            input=json.dumps({"hook_event_name": claude_stream.HOOK_EVENT}),
            capture_output=True,
            text=True,
            timeout=HOOK_WAIT_SECONDS,
        )

    def test_pending_guidance_becomes_additional_context(self) -> None:
        self.spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase before you push")

        result = self.run_hook()

        self.assertEqual(0, result.returncode)
        payload = json.loads(result.stdout)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(claude_stream.HOOK_EVENT, specific["hookEventName"])
        self.assertIn(
            claude_stream.GUIDANCE_CONTEXT_HEADING, specific["additionalContext"]
        )
        self.assertIn("rebase before you push", specific["additionalContext"])
        self.assertTrue(self.spool.receipt(GUIDANCE_ID))

    def test_guidance_is_injected_at_most_once(self) -> None:
        self.spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase before you push")
        self.run_hook()

        second = self.run_hook()

        self.assertEqual(0, second.returncode)
        self.assertEqual("", second.stdout.strip())

    def test_records_are_injected_in_the_order_they_were_written(self) -> None:
        self.spool.offer(guidance_id="b" * 8, seq=2, text="second thing")
        self.spool.offer(guidance_id="a" * 8, seq=1, text="first thing")

        context = json.loads(self.run_hook().stdout)["hookSpecificOutput"][
            "additionalContext"
        ]

        self.assertLess(context.index("first thing"), context.index("second thing"))

    def test_a_withdrawn_record_is_never_injected(self) -> None:
        self.spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase before you push")
        self.assertTrue(self.spool.withdraw(GUIDANCE_ID))

        result = self.run_hook()

        self.assertEqual("", result.stdout.strip())
        self.assertFalse(self.spool.receipt(GUIDANCE_ID))

    def test_an_empty_spool_says_nothing(self) -> None:
        result = self.run_hook()

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())

    def test_a_removed_overlay_never_blocks_a_tool_call(self) -> None:
        result = self.run_hook(spool_root=self.tmp / "gone")

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())

    def test_a_corrupt_record_never_blocks_a_tool_call(self) -> None:
        (self.spool.root / "pending" / "broken.json").write_text(
            "{not json", encoding="utf-8"
        )

        result = self.run_hook()

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())

    def test_a_hook_with_no_spool_argument_exits_quietly(self) -> None:
        result = subprocess.run(
            [sys.executable, str(claude_stream.hook_script_path())],
            input="{}",
            capture_output=True,
            text=True,
            timeout=HOOK_WAIT_SECONDS,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())


class ClaudeAdapterTransportTests(TempDirTestCase):
    """What the adapter puts on argv, on disk, and behind the seam."""

    def adapter(self) -> harness_claude.ClaudeAdapter:
        return harness_claude.ClaudeAdapter(executable="claude")

    def invocation(self, **overrides: Any) -> harnesses.HarnessInvocation:
        fields: dict[str, Any] = {
            "cwd": self.tmp,
            "plan_dir": self.tmp / "plan",
            "prompt": "do the work",
        }
        fields.update(overrides)
        return self.adapter().build_invocation(**fields)

    def test_a_claude_turn_declares_the_stream_json_transport(self) -> None:
        invocation = self.invocation()

        self.assertEqual(claude_stream.TRANSPORT_STREAM_JSON, invocation.transport)
        self.assertNotEqual(harnesses.TRANSPORT_EXEC, invocation.transport)
        self.assertIn("--input-format", invocation.argv)
        self.assertIn("--replay-user-messages", invocation.argv)

    def test_opening_the_transport_sends_the_prompt_over_stdin(self) -> None:
        stdin = _Stdin()
        transport = self.adapter().open_transport(
            process=SimpleNamespace(stdin=stdin),
            invocation=self.invocation(),
            prompt="do the work",
        )

        self.assertIsInstance(transport, harness_claude._ClaudeStreamTransport)
        self.assertEqual([claude_stream.user_message("do the work")], stdin.messages())
        self.assertIsNotNone(transport.guidance_transport())

    def test_the_transport_carries_the_spool_from_the_overlay(self) -> None:
        overlay_dir = self.tmp / "overlay"
        overlay_dir.mkdir()
        stdin = _Stdin()
        invocation = harnesses.HarnessInvocation(
            name="claude",
            argv=["claude"],
            cwd=self.tmp,
            transport=claude_stream.TRANSPORT_STREAM_JSON,
            overlay_dir=overlay_dir,
        )

        transport = self.adapter().open_transport(
            process=SimpleNamespace(stdin=stdin),
            invocation=invocation,
            prompt="do the work",
        )

        transport.guidance_transport()(request_for())
        pending = overlay_dir / "spool" / "pending" / f"{GUIDANCE_ID}.json"
        self.assertTrue(pending.exists())

    def test_a_non_stream_invocation_opens_no_conversation(self) -> None:
        invocation = harnesses.HarnessInvocation(
            name="claude", argv=["claude"], cwd=self.tmp
        )

        self.assertIsNone(
            self.adapter().open_transport(
                process=SimpleNamespace(stdin=None),
                invocation=invocation,
                prompt="do the work",
            )
        )

    def audit_invocation(self, **overrides: Any) -> harnesses.HarnessInvocation:
        # An audit turn takes read roots instead of a writable plan dir.
        return self.invocation(
            plan_dir=None,
            access=harnesses.audit_read_only_access((self.tmp,)),
            **overrides,
        )

    def test_a_fresh_turn_states_the_session_it_is_opening(self) -> None:
        invocation = self.invocation()

        argv = invocation.argv
        self.assertIn("--session-id", argv)
        assigned = argv[argv.index("--session-id") + 1]
        # A real UUID, because the CLI rejects anything else.
        self.assertEqual(assigned, str(uuid.UUID(assigned)))
        self.assertEqual(assigned, invocation.assigned_conversation_id)

    def test_two_turns_never_open_the_same_conversation(self) -> None:
        self.assertNotEqual(
            self.invocation().assigned_conversation_id,
            self.invocation().assigned_conversation_id,
        )

    def test_a_resume_restores_exactly_the_conversation_asked_for(self) -> None:
        invocation = self.invocation(resume_conversation_id="sess-7")

        argv = invocation.argv
        self.assertIn("--resume", argv)
        self.assertEqual("sess-7", argv[argv.index("--resume") + 1])
        self.assertEqual("sess-7", invocation.assigned_conversation_id)
        self.assertNotIn("--session-id", argv)

    def test_a_resumed_turn_is_never_forked_onto_a_new_conversation(self) -> None:
        # ``--fork-session`` answers "continue conversation X" with a different
        # one, which is the substitution this path exists to prevent.
        self.assertNotIn("--fork-session", self.invocation().argv)
        self.assertNotIn(
            "--fork-session", self.invocation(resume_conversation_id="sess-7").argv
        )

    def test_an_audit_turn_names_no_conversation_at_all(self) -> None:
        invocation = self.audit_invocation()

        self.assertIn("--no-session-persistence", invocation.argv)
        self.assertNotIn("--session-id", invocation.argv)
        self.assertNotIn("--resume", invocation.argv)
        self.assertIsNone(invocation.assigned_conversation_id)

    def test_an_audit_turn_refuses_to_resume_a_conversation(self) -> None:
        with self.assertRaises(harnesses.HarnessError) as raised:
            self.audit_invocation(resume_conversation_id="sess-7")

        self.assertIn("sess-7", str(raised.exception))

    def test_the_session_reaches_the_transport_that_must_re_check_it(self) -> None:
        invocation = self.invocation(resume_conversation_id="sess-7")

        transport = self.adapter().open_transport(
            process=_Process(_Stdin()),
            invocation=invocation,
            prompt="do the work",
        )

        self.assertEqual("sess-7", transport.session.session_id)

    def test_the_conversation_is_recorded_before_the_prompt_is_written(self) -> None:
        stdin = _Stdin()
        invocation = self.invocation()
        written_at_capture: list[list[dict[str, Any]]] = []
        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(
                sink=lambda native_id: written_at_capture.append(stdin.messages())
            )
        )

        self.adapter().open_transport(
            process=_Process(stdin),
            invocation=invocation,
            prompt="do the work",
            lifecycle=lifecycle,
        )

        # Nothing had been submitted yet, which is what makes the capture
        # refusal below able to cost the turn nothing.
        self.assertEqual([[]], written_at_capture)
        self.assertEqual(
            invocation.assigned_conversation_id, lifecycle.conversation.native_id
        )

    def test_a_turn_whose_conversation_cannot_be_recorded_submits_nothing(
        self,
    ) -> None:
        stdin = _Stdin()

        def refuse(native_id: str) -> None:
            raise OSError("the ledger is unwritable")

        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(sink=refuse)
        )

        with self.assertRaises(harnesses.ConversationCaptureError):
            self.adapter().open_transport(
                process=_Process(stdin),
                invocation=self.invocation(),
                prompt="do the work",
                lifecycle=lifecycle,
            )

        self.assertEqual([], stdin.messages())

    def test_an_audit_turn_captures_nothing_and_still_runs(self) -> None:
        stdin = _Stdin()
        captured: list[str] = []
        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(sink=captured.append)
        )

        self.adapter().open_transport(
            process=_Process(stdin),
            invocation=self.audit_invocation(),
            prompt="do the work",
            lifecycle=lifecycle,
        )

        self.assertEqual([], captured)
        self.assertEqual([claude_stream.user_message("do the work")], stdin.messages())

    def test_claude_advertises_the_continuity_it_now_has(self) -> None:
        adapter = self.adapter()

        self.assertTrue(adapter.captures_conversations)
        self.assertTrue(adapter.resumes_conversations)
        self.assertTrue(harnesses.continues_conversations("claude"))

    def test_guidance_never_rides_the_next_prompt_for_claude(self) -> None:
        # Claude can be steered in-turn, so the ledger's verdict is the live
        # attempt's own. Recording a next-turn delivery on top would invent a
        # second receipt for one guidance record.
        self.assertFalse(harnesses.delivers_guidance_next_turn("claude"))


class CopilotSteerRefusalTests(unittest.TestCase):
    """Copilot answers a steer, and the answer is next turn."""

    def transport(self) -> harnesses.HarnessTransport:
        """The one-shot transport, which is the audit profile's.

        The worker profile's ACP transport gives the same answers, proved
        against a live conversation in ``tests/test_asl_copilot_acp.py``.
        """
        adapter = harness_copilot.CopilotAdapter(executable="copilot")
        transport = adapter.open_transport(
            process=SimpleNamespace(stdin=None),
            invocation=harnesses.HarnessInvocation(
                name="copilot", argv=["copilot"], cwd=Path("/tmp")
            ),
            prompt="do the work",
        )
        assert transport is not None
        return transport

    def test_an_active_steer_is_declined_rather_than_missing(self) -> None:
        steer = self.transport().guidance_transport()
        assert steer is not None

        outcome = steer(request_for())

        self.assertFalse(outcome.delivered)
        self.assertEqual(
            guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER, outcome.reason
        )
        # Not retriable: the answer will not change inside this turn.
        self.assertFalse(outcome.retriable_in_turn)

    def test_a_copilot_worker_turn_speaks_acp_and_still_refuses_a_steer(self) -> None:
        invocation = harness_copilot.CopilotAdapter(
            executable="copilot"
        ).build_invocation(
            cwd=Path("/tmp"), plan_dir=Path("/tmp/plan"), prompt="do the work"
        )

        # A worker turn now has a stdin pipe, but it carries the handshake and
        # one prompt: ``session/cancel`` is not wired, so there is still no
        # channel a steer could travel on and the refusal above stands.
        self.assertEqual(harnesses.TRANSPORT_ACP, invocation.transport)
        self.assertNotIn("do the work", invocation.argv)

    def test_an_audit_turn_stays_a_one_shot_process(self) -> None:
        invocation = harness_copilot.CopilotAdapter(
            executable="copilot"
        ).build_invocation(
            cwd=Path("/tmp"),
            plan_dir=None,
            prompt="read the evidence",
            access=harnesses.audit_read_only_access([Path("/tmp/evidence")]),
        )

        # The audit profile did not move: its prompt is on argv, no stdin pipe
        # is opened, and nothing can cancel or restart the turn through one.
        self.assertEqual(harnesses.TRANSPORT_EXEC, invocation.transport)
        self.assertIn("read the evidence", invocation.argv)

    def test_declining_leaves_the_guidance_for_the_next_turns_prompt(self) -> None:
        self.assertTrue(harnesses.delivers_guidance_next_turn("copilot"))

    def test_the_output_stream_is_read_exactly_as_it_always_was(self) -> None:
        processor = self.transport().output_processor()

        self.assertIs(type(processor), harness_transport._OutputProcessor)
        self.assertEqual(
            "plain text\n", processor.process_chunk("stdout", "plain text\n")
        )


class NextTurnDeliveryOwnershipTests(unittest.TestCase):
    """Only a harness with no in-turn channel may claim a next-turn delivery."""

    def test_codex_and_claude_report_live_attempts_instead(self) -> None:
        for name in ("codex", "claude"):
            with self.subTest(harness=name):
                self.assertFalse(harnesses.delivers_guidance_next_turn(name))

    def test_an_unknown_harness_claims_nothing(self) -> None:
        self.assertFalse(harnesses.delivers_guidance_next_turn("nope"))
