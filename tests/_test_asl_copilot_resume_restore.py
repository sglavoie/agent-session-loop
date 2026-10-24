# mypy: ignore-errors
"""Copilot resume: restore, replay separation, early identifiers."""

# ruff: noqa: F403, F405
from ._test_asl_copilot_resume_support import *
from ._test_asl_copilot_resume_support import _RenderingProcess


class RestoreTests(unittest.TestCase):
    """``session/load``: what is asked for, and what is refused."""

    def test_a_turn_with_no_conversation_still_opens_a_fresh_one(self) -> None:
        process = _RenderingProcess(resume_responder())

        session = run_resume(process, resume=None)

        self.assertIn("session/new", process.methods())
        self.assertNotIn("session/load", process.methods())
        self.assertFalse(session.resumed)
        self.assertEqual(copilot_acp.STOP_REASON_END_TURN, session.stop_reason)

    def test_a_resume_loads_exactly_the_named_session_and_creates_none(self) -> None:
        process = _RenderingProcess(resume_responder())

        session = run_resume(process)

        self.assertEqual(
            ["initialize", "session/load", "session/prompt"], process.methods()
        )
        load = process.sent("session/load")["params"]
        self.assertEqual(SESSION_ID, load["sessionId"])
        self.assertTrue(session.resumed)
        self.assertEqual(SESSION_ID, session.session_id)

    def test_a_restored_session_is_configured_by_this_call_not_the_old_one(
        self,
    ) -> None:
        process = _RenderingProcess(resume_responder())

        run_resume(process, cwd="/repo/root")

        params = process.sent("session/load")["params"]
        # The same configuration ``session/new`` states, restated here: a
        # restore that named only the session would run this turn under
        # whatever working directory the session was last opened with.
        self.assertEqual("/repo/root", params["cwd"])
        self.assertEqual([], params["mcpServers"])

    def test_a_load_that_answers_null_is_a_successful_restore(self) -> None:
        process = _RenderingProcess(resume_responder(load_result=None))

        session = run_resume(process)

        self.assertTrue(session.resumed)
        self.assertIsNone(session.protocol_error)
        self.assertEqual(copilot_acp.STOP_REASON_END_TURN, session.stop_reason)

    def test_a_restored_session_reports_the_launch_controls_it_came_back_with(
        self,
    ) -> None:
        process = _RenderingProcess(
            resume_responder(
                load_result={
                    "configOptions": [
                        {"id": "model", "currentValue": "fake-model-1"},
                        {"id": "reasoning_effort", "currentValue": "low"},
                    ]
                }
            )
        )

        session = run_resume(process, expected_model="fake-model-1")

        self.assertEqual("fake-model-1", session.observed_model)
        self.assertEqual("low", session.observed_effort)
        self.assertEqual((), session.option_mismatches)

    def test_an_agent_that_never_advertised_the_capability_is_not_asked(self) -> None:
        process = _RenderingProcess(
            resume_responder(
                agent_capabilities={
                    copilot_acp.AGENT_CAPABILITY_LOAD_SESSION: False,
                }
            )
        )

        session = run_resume(process)

        # No load, and no ``session/new`` standing in for one: a fresh session
        # here would be exactly the silent new conversation this refuses.
        self.assertEqual(["initialize"], process.methods())
        self.assertFalse(session.prompt_submitted)
        self.assertIn(
            copilot_acp.AGENT_CAPABILITY_LOAD_SESSION, str(session.protocol_error)
        )
        self.assertIn(SESSION_ID, str(session.protocol_error))

    def test_a_capability_advertised_in_another_shape_is_not_an_advertisement(
        self,
    ) -> None:
        process = _RenderingProcess(
            resume_responder(
                agent_capabilities={
                    copilot_acp.AGENT_CAPABILITY_LOAD_SESSION: "yes",
                }
            )
        )

        session = run_resume(process)

        self.assertFalse(session.loads_sessions)
        self.assertEqual(["initialize"], process.methods())

    def test_a_session_the_agent_no_longer_has_fails_before_the_prompt(self) -> None:
        process = _RenderingProcess(
            resume_responder(load_error={"code": -32602, "message": "no such session"})
        )

        session = run_resume(process)

        self.assertNotIn("session/prompt", process.methods())
        self.assertFalse(session.resumed)
        # Cleared, so nothing downstream can report the turn as having run on a
        # conversation that was never restored.
        self.assertIsNone(session.session_id)
        self.assertIn(
            f"could not restore conversation {SESSION_ID}", str(session.protocol_error)
        )
        self.assertIn("no such session", str(session.protocol_error))

    def test_a_load_that_answers_with_a_different_session_is_refused(self) -> None:
        process = _RenderingProcess(
            resume_responder(load_result={"sessionId": OTHER_SESSION_ID})
        )

        session = run_resume(process)

        self.assertNotIn("session/prompt", process.methods())
        self.assertFalse(session.resumed)
        # Every refusal clears the session it optimistically named, not just
        # the one where the request itself failed.
        self.assertIsNone(session.session_id)
        self.assertIn(OTHER_SESSION_ID, str(session.protocol_error))
        self.assertIn(SESSION_ID, str(session.protocol_error))

    def test_a_load_result_that_is_not_an_object_is_refused(self) -> None:
        process = _RenderingProcess(resume_responder(load_result="restored"))

        session = run_resume(process)

        self.assertNotIn("session/prompt", process.methods())
        self.assertIn("session/load returned no result", str(session.protocol_error))

    def test_a_replay_that_could_not_be_read_is_not_a_restore(self) -> None:
        malformed = line(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": SESSION_ID, "update": "not an object"},
            }
        )
        process = _RenderingProcess(resume_responder(replay=(malformed,)))

        session = run_resume(process)

        self.assertNotIn("session/prompt", process.methods())
        self.assertFalse(session.resumed)
        # The framing complaint on its own would never say which conversation
        # it cost, so the restore failure replaces it rather than queueing
        # behind it.
        self.assertIn(
            f"conversation {SESSION_ID} was not restored", str(session.protocol_error)
        )
        self.assertIn("could not be read", str(session.protocol_error))


class ReplaySeparationTests(unittest.TestCase):
    """A replayed transcript is not this turn's work, output, or completion."""

    def session(self, **overrides: Any) -> tuple[_RenderingProcess, Any]:
        process = _RenderingProcess(
            resume_responder(
                replay=replayed_transcript(), updates=live_activity(), **overrides
            )
        )
        return process, run_resume(process)

    def test_a_replayed_transcript_never_reaches_the_turns_log(self) -> None:
        process, _ = self.session()

        log = process.log()
        self.assertNotIn(REPLAYED_PROMPT, log)
        self.assertNotIn(REPLAYED_ANSWER, log)
        self.assertNotIn("an earlier command", log)
        self.assertIn(LIVE_ANSWER, log)

    def test_a_replayed_answer_never_becomes_this_turns_final_message(self) -> None:
        _, session = self.session()

        self.assertEqual(LIVE_ANSWER, session.final_message)

    def test_replayed_updates_are_counted_apart_from_new_activity(self) -> None:
        _, session = self.session()

        # Both counts, because only their difference distinguishes a resumed
        # worker that did something from one that only had a transcript read
        # back to it.
        self.assertEqual(3, session.replayed_updates)
        self.assertEqual(1, session.live_updates)

    def test_a_replay_never_finishes_the_turn(self) -> None:
        # A transcript with a whole finished turn in it, and a prompt that
        # answers with no stop reason: completion has exactly one source.
        _, session = self.session(stop_reason=None)

        self.assertIsNone(session.stop_reason)
        self.assertIn("no stop reason", str(session.protocol_error))

    def test_another_sessions_update_during_replay_is_counted_by_neither(self) -> None:
        stranger = update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "not this conversation"},
            },
            session_id=OTHER_SESSION_ID,
        )
        process = _RenderingProcess(
            resume_responder(replay=(stranger,), updates=live_activity())
        )

        session = run_resume(process)

        self.assertEqual(0, session.replayed_updates)
        self.assertEqual(1, session.live_updates)
        self.assertNotIn("not this conversation", process.log())


class EarlyIdentifierTests(unittest.TestCase):
    """The conversation is durable before the turn is spent, or it is not run."""

    def test_the_conversation_is_recorded_before_the_prompt_is_sent(self) -> None:
        process = _RenderingProcess(resume_responder())
        seen: list[list[str]] = []
        captured: list[str] = []

        def sink(native_id: str) -> None:
            captured.append(native_id)
            seen.append(process.methods())

        run_resume(process, resume=None, on_conversation=sink)

        self.assertEqual([SESSION_ID], captured)
        # The whole ordering claim, read off the wire: the session existed and
        # the prompt had not gone out.
        self.assertIn("session/new", seen[0])
        self.assertNotIn("session/prompt", seen[0])

    def test_a_resumed_conversation_is_recorded_under_the_id_it_restored(self) -> None:
        process = _RenderingProcess(resume_responder())
        captured: list[str] = []

        run_resume(process, on_conversation=captured.append)

        self.assertEqual([SESSION_ID], captured)

    def test_a_sink_that_fails_abandons_the_turn_without_submitting(self) -> None:
        process = _RenderingProcess(resume_responder())

        def sink(native_id: str) -> None:
            raise RuntimeError("the ledger is unwritable")

        session = run_resume(process, resume=None, on_conversation=sink)

        self.assertNotIn("session/prompt", process.methods())
        self.assertFalse(session.prompt_submitted)
        self.assertEqual("the ledger is unwritable", session.conversation_error)
        # Nothing went wrong on the protocol. The agent was healthy and was
        # simply let go without a turn.
        self.assertIsNone(session.protocol_error)
