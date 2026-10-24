# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F403, F405, I001
"""ACP rendering, framing, submission, and shutdown tests."""

# ruff: noqa: F403, F405
from tests.asl_copilot_acp_support import *


ASL_TEST_LANE = "integration"


class UpdateRenderingTests(unittest.TestCase):
    """What a turn's log says, and the two echoes it must never say."""

    def setUp(self) -> None:
        self.process = _FakeProcess()
        self.session = build_session(self.process)

    def test_agent_message_chunks_are_the_turn_output_and_its_final_message(
        self,
    ) -> None:
        rendered = self.session.feed(
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "work"},
                }
            )
        ) + self.session.feed(
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "ing\n"},
                }
            )
        )

        self.assertEqual("working\n", rendered)
        self.assertEqual("working\n", self.session.final_message)

    def test_the_prompt_echoes_are_never_rendered(self) -> None:
        rendered = self.session.feed(
            update(
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": PROMPT},
                }
            )
        ) + self.session.feed(
            update({"sessionUpdate": "session_info_update", "title": PROMPT})
        )

        self.assertEqual("", rendered)
        self.assertEqual("", self.session.final_message)

    def test_a_tool_call_is_announced_and_only_its_failure_is_restated(self) -> None:
        started = self.session.feed(
            update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "t1",
                    "title": "just test-fast",
                    "status": "pending",
                }
            )
        )
        completed = self.session.feed(
            update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1",
                    "status": "completed",
                }
            )
        )
        failed = self.session.feed(
            update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1",
                    "title": "just lint",
                    "status": "failed",
                }
            )
        )

        self.assertEqual("$ just test-fast\n", started)
        self.assertEqual("", completed)
        self.assertEqual("tool call failed: just lint\n", failed)

    def test_an_update_for_another_session_is_not_logged(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = run_session(process)

        rendered = session.feed(
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "not mine"},
                },
                session_id="somebody-else",
            )
        )

        self.assertEqual("", rendered)

    def test_noise_updates_render_nothing(self) -> None:
        for kind in (
            "agent_thought_chunk",
            "plan",
            "available_commands_update",
            "current_mode_update",
            "usage_update",
        ):
            with self.subTest(update=kind):
                self.assertEqual("", self.session.feed(update({"sessionUpdate": kind})))


class FramingTests(unittest.TestCase):
    """A fragment is never a message, and prose is never dropped."""

    def setUp(self) -> None:
        self.process = _FakeProcess()
        self.session = build_session(self.process)

    def test_a_line_split_across_reads_is_buffered_until_it_is_complete(self) -> None:
        whole = update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "halves"},
            }
        )
        head, tail = whole[:20], whole[20:]

        self.assertEqual("", self.session.feed(head))
        self.assertEqual("halves", self.session.feed(tail))

    def test_non_protocol_stdout_is_passed_through(self) -> None:
        self.assertEqual(
            "copilot: warming up\n", self.session.feed("copilot: warming up\n")
        )
        self.assertIsNone(self.session.protocol_error)

    def test_a_trailing_partial_line_is_flushed(self) -> None:
        self.session.feed("half a line")

        self.assertEqual("half a line\n", self.session.flush())

    def test_an_answer_to_an_unknown_request_is_a_protocol_error(self) -> None:
        self.session.feed(line({"jsonrpc": "2.0", "id": 77, "result": {}}))

        self.assertIn("unknown request id 77", str(self.session.protocol_error))


class SubmissionTests(unittest.TestCase):
    """What the prompt is, and the checkpoint immediately before it."""

    def test_the_prompt_travels_the_protocol_and_answers_with_a_stop_reason(
        self,
    ) -> None:
        process = _FakeProcess(scripted_responder())

        session = run_session(process)

        params = process.sent("session/prompt")["params"]
        self.assertEqual(SESSION_ID, params["sessionId"])
        self.assertEqual([{"type": "text", "text": PROMPT}], params["prompt"])
        self.assertTrue(session.prompt_submitted)
        self.assertEqual(copilot_acp.STOP_REASON_END_TURN, session.stop_reason)

    def test_a_refused_submission_sends_no_prompt_and_is_not_an_error(self) -> None:
        process = _FakeProcess(scripted_responder())

        def refuse() -> None:
            raise harnesses.TurnCancelled("turn cancelled before submission")

        session = run_session(process, before_submit=refuse)

        self.assertNotIn("session/prompt", process.methods())
        self.assertFalse(session.prompt_submitted)
        self.assertIsNone(session.protocol_error)
        self.assertEqual(
            "turn cancelled before submission", session.cancelled_before_submission
        )

    def test_a_missing_stop_reason_is_a_protocol_error(self) -> None:
        process = _FakeProcess(scripted_responder(stop_reason=None))

        session = run_session(process)

        self.assertTrue(session.prompt_submitted)
        self.assertIsNone(session.stop_reason)
        self.assertIn("no stop reason", str(session.protocol_error))

    def test_a_failed_prompt_is_recorded_rather_than_raised(self) -> None:
        process = _FakeProcess(
            scripted_responder(prompt_error={"code": -32603, "message": "quota"})
        )

        session = run_session(process)

        self.assertIn("quota", str(session.protocol_error))
        self.assertIsNone(session.stop_reason)


class ShutdownTests(unittest.TestCase):
    """The orderly end of a conversation, and the forced one behind it."""

    def test_a_finished_turn_closes_its_own_stdin(self) -> None:
        process = _FakeProcess(scripted_responder())

        run_session(process)

        self.assertTrue(process.stdin.closed)
        self.assertFalse(process.terminated)

    def test_shutdown_is_safe_on_a_conversation_that_never_started(self) -> None:
        process = _FakeProcess()
        session = build_session(process)

        session.shutdown()

        self.assertTrue(process.stdin.closed)
        self.assertTrue(session.turn_finished)

    def test_a_wedged_agent_is_terminated_after_the_grace_period(self) -> None:
        process = _FakeProcess(scripted_responder())
        # An agent that ignores its stdin closing is the only thing the grace
        # period exists for.
        process.on_stdin_closed = lambda: None  # type: ignore[method-assign]
        session = build_session(process, shutdown_grace=0.01)

        session.start()
        session.shutdown()

        wait_until(lambda: process.terminated, timeout=WAIT_SECONDS)
        self.assertTrue(process.terminated)

    def test_writing_after_shutdown_fails_rather_than_hangs(self) -> None:
        process = _FakeProcess()
        session = build_session(process)
        session.shutdown()

        with self.assertRaises(copilot_acp.AcpError):
            session._request("initialize", {})
