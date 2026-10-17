"""Copilot's conversation: restoring one, recording one, and stopping one.

The transport module next door proves a copilot turn can run over ACP at all.
This one proves the turn is *continuable*: that it restores exactly the
conversation it was handed or fails saying why, that the identifier is durable
before any work is submitted, that a replayed transcript is never mistaken for
the resumed worker's own activity, and that stopping a turn is said to the
conversation rather than only to the process.

The fakes are imported from :mod:`tests.test_asl_copilot_acp` rather than
copied. Two hand-written copies of one protocol drift, and the failure that
finds the drift is a test that passes against a fake no agent resembles.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    conversations,
    copilot_acp,
    guidance_delivery,
    harness_copilot,
    harness_process,
    harnesses,
)
from tests.support import wait_until
from tests.test_asl_copilot_acp import (
    FAKE_ACP_FIXTURE,
    PROMPT,
    SESSION_ID,
    WAIT_SECONDS,
    _FakeProcess,
    build_session,
    line,
    update,
)

#: The session a restore is answered with when the test wants a contradiction.
OTHER_SESSION_ID = "acp-session-somewhere-else"

#: What the fixture replays and what it says this turn did. Kept apart on
#: purpose: every assertion about the log is an assertion that one of these
#: texts is present and the other is not.
REPLAYED_PROMPT = "the prompt of the turn that came before"
REPLAYED_ANSWER = "what the earlier turn answered\n"
LIVE_ANSWER = "working\n"

_UNSET = object()


class _RenderingProcess(_FakeProcess):
    """A fake process that keeps what each fed chunk rendered into the log.

    ``feed`` returns the text the turn's log receives, and the plain fake
    discards it. Every replay assertion here is about that return value being
    empty, so it has to be kept.
    """

    def __init__(self, responder: Any = None) -> None:
        super().__init__(responder)
        self.rendered: list[str] = []

    def handle(self, text: str) -> None:
        message = json.loads(text)
        self.messages.append(message)
        if self.responder is None:
            return
        for chunk in self.responder(self, message):
            assert self.session is not None
            self.rendered.append(self.session.feed(chunk))

    def log(self) -> str:
        return "".join(self.rendered)


class _SurvivingProcess(_RenderingProcess):
    """An agent that outlives its own stdin.

    Closing stdin is how this client asks an ACP agent to shut down, and the
    grace period exists because a wedged one does not always take the hint.
    That is the only state where a cancellation is attempted against a process
    that is still running through a pipe that is already gone.
    """

    def on_stdin_closed(self) -> None:
        return


def resume_responder(
    *,
    agent_capabilities: Any = _UNSET,
    load_error: dict[str, Any] | None = None,
    load_result: Any = _UNSET,
    replay: tuple[str, ...] = (),
    updates: tuple[str, ...] = (),
    stop_reason: str | None = copilot_acp.STOP_REASON_END_TURN,
    hold_prompt: bool = False,
):
    """An agent that can restore a session, with one seam per refusal.

    ``hold_prompt`` never answers ``session/prompt``, which is how a test gets
    to a submitted-but-unfinished turn without a timer: the answer arrives only
    when the test cancels, and the cancellation is what produces it.
    """
    if agent_capabilities is _UNSET:
        agent_capabilities = {copilot_acp.AGENT_CAPABILITY_LOAD_SESSION: True}
    #: Remembered so the cancellation can answer the prompt it interrupted.
    live: dict[str, Any] = {"prompt_id": None}

    def respond(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            return [
                line(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": copilot_acp.PROTOCOL_VERSION,
                            "agentCapabilities": agent_capabilities,
                            "agentInfo": {"name": "FakeCopilot", "version": "0.0.1"},
                        },
                    }
                )
            ]
        if method == "session/new":
            return [
                line(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"sessionId": SESSION_ID},
                    }
                )
            ]
        if method == "session/load":
            # The transcript first, then the answer, which is the ordering the
            # protocol specifies and the one the client has to survive.
            chunks = list(replay)
            if load_error is not None:
                chunks.append(
                    line({"jsonrpc": "2.0", "id": request_id, "error": load_error})
                )
                return chunks
            result = {} if load_result is _UNSET else load_result
            chunks.append(line({"jsonrpc": "2.0", "id": request_id, "result": result}))
            return chunks
        if method == "session/prompt":
            live["prompt_id"] = request_id
            if hold_prompt:
                return list(updates)
            return [*updates, _prompt_answer(request_id, stop_reason)]
        if method == "session/cancel":
            if not hold_prompt:
                return []
            # The acknowledgment the protocol actually defines: the pending
            # prompt comes back cancelled.
            return [
                _prompt_answer(live["prompt_id"], copilot_acp.STOP_REASON_CANCELLED)
            ]
        return []

    return respond


def _prompt_answer(request_id: object, stop_reason: str | None) -> str:
    result: dict[str, Any] = {}
    if stop_reason is not None:
        result["stopReason"] = stop_reason
    return line({"jsonrpc": "2.0", "id": request_id, "result": result})


def replayed_transcript() -> tuple[str, ...]:
    """One earlier turn, streamed back the way ``session/load`` streams it."""
    return (
        update(
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": REPLAYED_PROMPT},
            }
        ),
        update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "old-tool-1",
                "title": "an earlier command",
                "status": "completed",
            }
        ),
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": REPLAYED_ANSWER},
            }
        ),
    )


def live_activity() -> tuple[str, ...]:
    return (
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": LIVE_ANSWER},
            }
        ),
    )


def run_resume(
    process: _FakeProcess,
    *,
    resume: str | None = SESSION_ID,
    **overrides: Any,
) -> copilot_acp.CopilotAcpSession:
    session = build_session(process, resume_conversation_id=resume, **overrides)
    session.start()
    session.shutdown()
    return session


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
                except BaseException as error:  # noqa: BLE001 - reported below
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


def _guidance_request() -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id="22222222-2222-4222-8222-222222222222",
        seq=1,
        text="prefer the smaller change",
        turn=guidance_delivery.TurnIdentity(
            run_id="run-1", session_id="6", turn_index=1, harness="copilot"
        ),
    )


if __name__ == "__main__":
    unittest.main()
