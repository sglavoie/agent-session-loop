"""Claude stream session continuity and output processing."""

from __future__ import annotations

from .support_asl_claude_stream_base import (
    HOOK_WAIT_SECONDS,
    Any,
    claude_stream,
    harness_claude,
    json,
    request_for,
    session_with,
    subprocess,
    sys,
    threading,
    time,
    unittest,
)


class StreamSessionContinuityTests(unittest.TestCase):
    """The conversation this turn runs on is stated, then re-checked."""

    def test_a_turn_knows_its_conversation_before_it_writes_anything(self) -> None:
        session, stdin = session_with(session_id="sess-1")

        self.assertEqual("sess-1", session.session_id)
        self.assertEqual([], stdin.messages())

    def test_the_reported_session_is_checked_against_the_stated_one(self) -> None:
        session, _ = session_with(session_id="sess-1")

        mismatch = session.note_session_id("sess-2")

        self.assertIsNotNone(mismatch)
        self.assertIn("sess-1", mismatch or "")
        self.assertIn("sess-2", mismatch or "")
        self.assertEqual(mismatch, session.session_mismatch)

    def test_the_first_disagreement_is_the_one_reported(self) -> None:
        session, _ = session_with(session_id="sess-1")

        first = session.note_session_id("sess-2")
        session.note_session_id("sess-3")

        self.assertEqual(first, session.session_mismatch)

    def test_the_session_the_turn_asked_for_is_no_disagreement(self) -> None:
        session, _ = session_with(session_id="sess-1")

        self.assertIsNone(session.note_session_id("sess-1"))
        self.assertIsNone(session.session_mismatch)

    def test_a_turn_that_stated_no_session_has_nothing_to_contradict(self) -> None:
        # An audit turn persists no session, so whatever the CLI names is its
        # own business.
        session, _ = session_with()

        self.assertIsNone(session.note_session_id("sess-2"))
        self.assertIsNone(session.session_mismatch)

    def test_a_mismatch_does_not_stop_the_turn(self) -> None:
        session, stdin = session_with(session_id="sess-1")
        session.send_prompt()

        session.note_session_id("sess-2")

        self.assertFalse(session.stdin_closed)
        self.assertTrue(session.steer(request_for()) is not None)
        self.assertIn("prefer the smaller change", json.dumps(stdin.messages()))


class StreamOutputProcessorTests(unittest.TestCase):
    """The one stdout reader is also the session's acknowledgement channel."""

    def processor(self, session: claude_stream.ClaudeStreamSession) -> Any:
        return harness_claude._ClaudeStreamJsonOutputProcessor(session)

    def test_a_user_echo_confirms_without_being_rendered(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps(claude_stream.user_message("rebase before you push")) + "\n",
        )

        # No guidance body reaches the turn log.
        self.assertEqual("", rendered)
        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertTrue(outcome.delivered)

    def test_an_echo_carrying_bare_text_still_confirms(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))
        processor = self.processor(session)

        processor.process_chunk(
            "stdout",
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "rebase before you push",
                    },
                }
            )
            + "\n",
        )

        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertTrue(outcome.delivered)

    def test_a_result_record_is_still_rendered_and_ends_the_turn(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps({"type": "result", "subtype": "success", "result": "done"})
            + "\n",
        )

        self.assertIn("claude result: success", rendered)
        self.assertTrue(processor.final_result_seen)
        self.assertTrue(stdin.closed_by_asl)

    def test_a_new_request_invalidates_the_prior_terminal_result(self) -> None:
        processor = harness_claude._ClaudeStreamJsonOutputProcessor()

        processor.process_chunk(
            "stdout", json.dumps({"type": "result", "subtype": "success"}) + "\n"
        )
        self.assertTrue(processor.final_result_seen)

        processor.process_chunk(
            "stdout",
            json.dumps({"type": "system", "subtype": "init", "session_id": "s"}) + "\n",
        )
        self.assertFalse(processor.final_result_seen)

        processor.process_chunk(
            "stdout", json.dumps({"type": "result", "subtype": "success"}) + "\n"
        )
        self.assertTrue(processor.final_result_seen)

    def test_an_error_result_does_not_render_its_provider_subtype_as_outcome(
        self,
    ) -> None:
        session, stdin = session_with()
        session.send_prompt()
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "API Error: Connection lost mid-response.\nIncomplete.",
                    "num_turns": 36,
                    "duration_ms": 177100,
                }
            )
            + "\n",
        )

        self.assertEqual(
            "claude result: error (provider subtype success, 36 turns, 177.1s): "
            "API Error: Connection lost mid-response. Incomplete.\n",
            rendered,
        )
        self.assertTrue(processor.final_result_seen)
        self.assertTrue(stdin.closed_by_asl)

    def test_a_control_response_answers_the_session_without_being_rendered(
        self,
    ) -> None:
        session, stdin = session_with()
        session.send_prompt()
        processor = self.processor(session)
        answered: list[bool] = []
        thread = threading.Thread(
            target=lambda: answered.append(session.interrupt()), daemon=True
        )
        thread.start()
        deadline = time.monotonic() + HOOK_WAIT_SECONDS
        request: dict[str, Any] | None = None
        while request is None and time.monotonic() < deadline:
            for message in stdin.messages():
                if message.get("type") == claude_stream.CONTROL_REQUEST:
                    request = message
            time.sleep(0.01)
        assert request is not None

        rendered = processor.process_chunk(
            "stdout",
            json.dumps(
                {
                    "type": claude_stream.CONTROL_RESPONSE,
                    "response": {
                        "subtype": "success",
                        "request_id": request["request_id"],
                    },
                }
            )
            + "\n",
        )

        thread.join(timeout=HOOK_WAIT_SECONDS)
        self.assertEqual([True], answered)
        # Protocol bookkeeping is not turn output.
        self.assertEqual("", rendered)

    def test_the_init_record_is_rechecked_against_the_stated_session(self) -> None:
        session, _ = session_with(session_id="sess-1")
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-2"})
            + "\n",
        )

        self.assertIn("claude session mismatch", rendered)
        self.assertIn("sess-1", rendered)
        self.assertIn("sess-2", rendered)
        self.assertIsNotNone(session.session_mismatch)

    def test_the_session_the_turn_asked_for_is_reported_without_alarm(self) -> None:
        session, _ = session_with(session_id="sess-1")
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"})
            + "\n",
        )

        self.assertIn("session sess-1", rendered)
        self.assertNotIn("mismatch", rendered)
        self.assertIsNone(session.session_mismatch)

    def test_normalization_still_works_without_a_session(self) -> None:
        processor = harness_claude._ClaudeStreamJsonOutputProcessor()

        rendered = processor.process_chunk(
            "stdout",
            "\n".join(
                [
                    json.dumps(
                        {"type": "system", "subtype": "init", "model": "opus-x"}
                    ),
                    json.dumps(claude_stream.user_message("the prompt")),
                    json.dumps({"type": "result", "subtype": "success"}),
                ]
            )
            + "\n",
        )

        self.assertIn("claude init; model opus-x", rendered)
        self.assertIn("claude result: success", rendered)
        self.assertNotIn("the prompt", rendered)
        self.assertEqual("opus-x", processor.observed_model)

    def test_a_process_with_merged_guidance_gets_eof_and_exits_successfully(
        self,
    ) -> None:
        # A real pipe reproduces the deadlock: the CLI has consumed both
        # messages in one turn, emits one result, and waits for stdin EOF.
        script = """
import json, sys
for _ in range(2):
    message = json.loads(sys.stdin.readline())
    message['isReplay'] = True
    print(json.dumps(message), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)
sys.stdin.read()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = claude_stream.ClaudeStreamSession(process=process, prompt="do work")
        processor = self.processor(session)
        rendered: list[str] = []

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                rendered.append(processor.process_chunk("stdout", line))

        reader = threading.Thread(target=read_output, daemon=True)
        try:
            session.send_prompt()
            session.steer(request_for())
            reader.start()
            self.assertEqual(0, process.wait(timeout=5))
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
            self.assertEqual("claude result: success\n", "".join(rendered))
        finally:
            session.shutdown()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
