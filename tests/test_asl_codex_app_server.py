"""The codex app-server transport: the protocol client and the adapter seam.

Two levels, because the two failure families live in different places. The
session tests drive :class:`codex_app_server.CodexAppServerSession` against an
in-process fake whose every answer is scripted, so framing, correlation, and
the exact preconditions on a steer are asserted without a race. The adapter
tests spawn ``tests/fixtures/fake_codex_app_server.py`` for real, so process
ownership, the access profile crossing the wire, and the fallback boundary are
asserted against an actual pipe.

The invariant that gets the most attention here is the one with the sharpest
consequence: a turn is never executed twice. The client may fall back to
``codex exec`` only while ``turn/start`` has not *gone out*, and the tests below
hold both sides of that line -- including the case that separates going out from
being accepted, an app-server that reads the prompt and then dies with its
answer unsent.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import codex_app_server, guidance_delivery, harnesses
from tests.support import REPO_ROOT, install_fake_codex

FAKE_APP_SERVER_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_codex_app_server.py"

#: Every bounded wait in this module. Long enough that a loaded machine does
#: not fail a correct implementation, short enough that a wrong one fails
#: rather than hangs.
WAIT_SECONDS = 20.0

GUIDANCE_BODY = "prefer the smaller change"
PROMPT = "do the work for session three"


def turn_identity() -> guidance_delivery.TurnIdentity:
    return guidance_delivery.TurnIdentity(
        run_id="run-1", session_id="3", turn_index=1, harness="codex"
    )


def guidance_request(text: str = GUIDANCE_BODY) -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id="11111111-1111-4111-8111-111111111111",
        seq=1,
        text=text,
        turn=turn_identity(),
    )


def worker_plan(**overrides: Any) -> codex_app_server.TurnPlan:
    fields: dict[str, Any] = {
        "thread_params": {
            "cwd": "/work",
            "sandbox": "workspace-write",
            # The real plan always states this, and a resumed thread is checked
            # against it: an approval policy that came back from the restored
            # thread rather than from this call is a different turn.
            "approvalPolicy": "never",
        },
        "turn_params": {"effort": "high"},
        "expected_sandbox": codex_app_server.SANDBOX_WORKSPACE_WRITE,
    }
    fields.update(overrides)
    return codex_app_server.TurnPlan(**fields)


class _FakeStdin:
    """The write end of a fake app-server, answering on the caller's thread."""

    def __init__(self, process: _FakeProcess) -> None:
        self._process = process
        self.closed = False

    def write(self, line: str) -> int:
        if self.closed:
            raise ValueError("write to a closed pipe")
        self._process.handle(line)
        return len(line)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("flush of a closed pipe")

    def close(self) -> None:
        self.closed = True
        self._process.on_stdin_closed()


class _FakeProcess:
    """A process-shaped object whose answers a test writes by hand.

    ``responder`` is called with each decoded request and returns the raw text
    to feed back, which is what lets a test split one response across reads,
    answer out of order, or never answer at all.
    """

    def __init__(
        self, responder: Any = None, *, exit_on_stdin_close: bool = True
    ) -> None:
        self.stdin = _FakeStdin(self)
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.requests: list[dict[str, Any]] = []
        self.responder = responder
        self.session: codex_app_server.CodexAppServerSession | None = None
        self._exit_on_stdin_close = exit_on_stdin_close

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def on_stdin_closed(self) -> None:
        if self._exit_on_stdin_close and self.returncode is None:
            self.returncode = 0

    def handle(self, line: str) -> None:
        message = json.loads(line)
        self.requests.append(message)
        if self.responder is None:
            return
        for chunk in self.responder(self, message):
            assert self.session is not None
            self.session.feed(chunk)

    def methods(self) -> list[str]:
        return [str(message.get("method")) for message in self.requests]

    def request_id(self, method: str) -> int:
        for message in self.requests:
            if message.get("method") == method and "id" in message:
                return int(message["id"])
        raise AssertionError(f"no {method} request was sent")


def scripted_responder(
    *,
    applied_sandbox: str | None = None,
    applied_settings: dict[str, Any] | None = None,
    applied_model: str = "fake-model-1",
    thread_error: dict[str, Any] | None = None,
    resume_error: dict[str, Any] | None = None,
    turn_error: dict[str, Any] | None = None,
    turn_result: dict[str, Any] | None = None,
    restored_thread_id: str | None = None,
    answer_steer: bool = True,
    steer_error: dict[str, Any] | None = None,
    answer_interrupt: bool = True,
    interrupt_error: dict[str, Any] | None = None,
):
    """The happy-path server, with one seam per failure a test wants.

    ``thread/start`` and ``thread/resume`` answer in the same shape, so both go
    through one builder: what a test varies is the echo, which is the only
    thing a restoring client has to go on.
    """

    def thread_result(params: dict[str, Any], thread_id: str) -> dict[str, Any]:
        sandbox = (
            applied_sandbox
            or {
                "read-only": "readOnly",
                "workspace-write": "workspaceWrite",
                "danger-full-access": "dangerFullAccess",
            }[params["sandbox"]]
        )
        result: dict[str, Any] = {
            "thread": {"id": thread_id},
            "sandbox": {"type": sandbox},
            "model": applied_model,
            "cwd": params.get("cwd"),
            "approvalPolicy": params.get("approvalPolicy"),
        }
        result.update(applied_settings or {})
        return result

    def respond(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
        method = message.get("method")
        if "id" not in message:
            return []
        request_id = message["id"]
        params = message.get("params") or {}
        if method == "initialize":
            return [_line({"id": request_id, "result": {"userAgent": "fake"}})]
        if method == "thread/start":
            if thread_error is not None:
                return [_line({"id": request_id, "error": thread_error})]
            return [
                _line({"id": request_id, "result": thread_result(params, "thread-1")})
            ]
        if method == "thread/resume":
            if resume_error is not None:
                return [_line({"id": request_id, "error": resume_error})]
            restored = restored_thread_id or str(params.get("threadId"))
            return [
                _line({"id": request_id, "result": thread_result(params, restored)})
            ]
        if method == "turn/start":
            if turn_error is not None:
                return [_line({"id": request_id, "error": turn_error})]
            return [
                _line(
                    {
                        "id": request_id,
                        "result": turn_result or {"turn": {"id": "turn-1"}},
                    }
                )
            ]
        if method == "turn/steer":
            if not answer_steer:
                return []
            if steer_error is not None:
                return [_line({"id": request_id, "error": steer_error})]
            return [_line({"id": request_id, "result": {"turnId": "turn-1"}})]
        if method == "turn/interrupt":
            if not answer_interrupt:
                return []
            if interrupt_error is not None:
                return [_line({"id": request_id, "error": interrupt_error})]
            return [_line({"id": request_id, "result": {}})]
        return []

    return respond


def _line(message: dict[str, Any]) -> str:
    return json.dumps(message) + "\n"


def notification(method: str, **params: Any) -> str:
    return _line({"method": method, "params": params})


def start_session(
    process: _FakeProcess,
    *,
    plan: codex_app_server.TurnPlan | None = None,
    prompt: str = PROMPT,
    **kwargs: Any,
) -> codex_app_server.CodexAppServerSession:
    """Start a session on ``process`` and wait out its handshake."""
    session = codex_app_server.CodexAppServerSession(
        process=process,  # type: ignore[arg-type]
        plan=plan or worker_plan(),
        prompt=prompt,
        **kwargs,
    )
    process.session = session
    session.start()
    wait_until(
        lambda: session.turn_accepted or session.protocol_error is not None,
        "the handshake neither completed nor failed",
    )
    return session


def lost_turn_response(**overrides: Any):
    """The happy path up to ``turn/start``, which is read and never answered.

    The app-server exits as it reads the prompt, which is how the real one loses
    an answer: the request is gone from this side, the reply never exists, and
    the client learns only that the process is no longer there.
    """
    scripted = scripted_responder(**overrides)

    def respond(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
        if message.get("method") == "turn/start":
            process.returncode = 9
            return []
        return scripted(process, message)

    return respond


def start_lost_session(
    process: _FakeProcess,
    **kwargs: Any,
) -> codex_app_server.CodexAppServerSession:
    """Start a session whose turn is expected to fail after its prompt went out.

    Neither :func:`start_session` nor :func:`start_refused_session` waits for
    this: there is no acceptance coming, and the refusal waiter would return the
    moment the protocol error is recorded, which is before the driver has
    finished saying what became of the turn.
    """
    session = codex_app_server.CodexAppServerSession(
        process=process,  # type: ignore[arg-type]
        plan=kwargs.pop("plan", None) or worker_plan(),
        prompt=kwargs.pop("prompt", PROMPT),
        **kwargs,
    )
    process.session = session
    session.start()
    wait_until(
        lambda: session.protocol_error is not None,
        "the turn neither failed nor was accepted",
    )
    return session


def start_refused_session(
    process: _FakeProcess,
    **kwargs: Any,
) -> codex_app_server.CodexAppServerSession:
    """Start a session that is expected to stop before it submits anything.

    A refusal is neither acceptance nor a protocol error -- that is the point
    of it -- so it needs its own waiter rather than :func:`start_session`'s.
    """
    session = codex_app_server.CodexAppServerSession(
        process=process,  # type: ignore[arg-type]
        plan=kwargs.pop("plan", None) or worker_plan(),
        prompt=kwargs.pop("prompt", PROMPT),
        **kwargs,
    )
    process.session = session
    session.start()
    wait_until(
        lambda: (
            session.conversation_error is not None
            or session.cancelled_before_submission is not None
            or session.turn_accepted
            or session.protocol_error is not None
        ),
        "the handshake neither refused nor completed",
    )
    return session


def wait_until(predicate: Any, message: str) -> None:
    """Poll ``predicate`` up to :data:`WAIT_SECONDS`, or fail saying why."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


class HandshakeTests(unittest.TestCase):
    """What the client says before a turn exists, and what it refuses to say."""

    def test_the_handshake_runs_in_order_and_carries_the_plan(self) -> None:
        process = _FakeProcess(scripted_responder())

        session = start_session(process)

        self.assertEqual(
            ["initialize", "initialized", "thread/start", "turn/start"],
            process.methods(),
        )
        thread_start = process.requests[2]["params"]
        self.assertEqual("workspace-write", thread_start["sandbox"])
        self.assertEqual("/work", thread_start["cwd"])
        turn_start = process.requests[3]["params"]
        self.assertEqual("thread-1", turn_start["threadId"])
        self.assertEqual("high", turn_start["effort"])
        self.assertEqual([{"type": "text", "text": PROMPT}], turn_start["input"])
        self.assertTrue(session.turn_accepted)
        self.assertEqual("fake-model-1", session.observed_model)
        self.assertIsNone(session.fallback_reason)

    def test_a_sandbox_the_server_widened_stops_the_turn_before_it_starts(
        self,
    ) -> None:
        process = _FakeProcess(scripted_responder(applied_sandbox="dangerFullAccess"))

        session = start_session(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertFalse(session.turn_accepted)
        self.assertIn("expected 'workspaceWrite'", str(session.protocol_error))
        # Refused before any work: the legacy transport, which states the
        # profile in argv, may still run this turn.
        self.assertIsNotNone(session.fallback_reason)

    def test_an_error_response_ends_the_conversation_with_a_fallback(self) -> None:
        process = _FakeProcess(
            scripted_responder(thread_error={"code": -32603, "message": "no thread"})
        )

        session = start_session(process)

        self.assertIn("no thread", str(session.protocol_error))
        self.assertIsNotNone(session.fallback_reason)

    def test_a_server_that_exits_mid_handshake_is_not_waited_out(self) -> None:
        def die(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
            process.returncode = 3
            return []

        process = _FakeProcess(die)

        started = time.monotonic()
        session = start_session(process, request_timeout=WAIT_SECONDS * 10)

        self.assertLess(time.monotonic() - started, WAIT_SECONDS)
        self.assertIn("exited before answering", str(session.protocol_error))
        self.assertIsNotNone(session.fallback_reason)

    def test_a_handshake_request_that_is_never_answered_times_out(self) -> None:
        process = _FakeProcess(lambda process, message: [])

        session = start_session(process, request_timeout=0.2)

        self.assertEqual("initialize timed out", session.protocol_error)
        self.assertIsNotNone(session.fallback_reason)


class FramingAndCorrelationTests(unittest.TestCase):
    """One byte stream, many messages, answered in whatever order they arrive."""

    def test_a_response_split_across_reads_is_reassembled(self) -> None:
        def fragmented(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
            whole = scripted_responder()(process, message)
            if not whole:
                return []
            text = "".join(whole)
            third = max(1, len(text) // 3)
            return [text[:third], text[third : 2 * third], text[2 * third :]]

        process = _FakeProcess(fragmented)

        session = start_session(process)

        self.assertTrue(session.turn_accepted)

    def test_responses_are_routed_by_id_not_by_arrival_order(self) -> None:
        process = _FakeProcess(scripted_responder(answer_steer=False))
        session = start_session(process)
        outcomes: dict[int, guidance_delivery.GuidanceOutcome] = {}
        ready = threading.Barrier(3, timeout=WAIT_SECONDS)

        def steer(index: int) -> None:
            ready.wait()
            outcomes[index] = session.steer(guidance_request(f"guidance {index}"))

        threads = [threading.Thread(target=steer, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        ready.wait()
        deadline = time.monotonic() + WAIT_SECONDS
        while len(process.requests) < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        steer_ids = [
            int(message["id"])
            for message in process.requests
            if message.get("method") == "turn/steer"
        ]
        self.assertEqual(2, len(steer_ids), process.requests)
        # Answer the second request first, and make the two answers differ so a
        # client that matched on arrival order would swap them.
        session.feed(
            _line({"id": steer_ids[1], "error": {"code": -32603, "message": "nope"}})
            + _line({"id": steer_ids[0], "result": {"turnId": "turn-1"}})
        )
        for thread in threads:
            thread.join(timeout=WAIT_SECONDS)

        delivered = [index for index, outcome in outcomes.items() if outcome.delivered]
        deferred = [
            index for index, outcome in outcomes.items() if not outcome.delivered
        ]
        self.assertEqual(1, len(delivered))
        self.assertEqual(1, len(deferred))
        # The delivered one is the thread whose request id was answered with a
        # result, not the one whose answer arrived first.
        sent_first = process.requests[4]["params"]["input"][0]["text"]
        expected_delivered = int(sent_first.rsplit(" ", 1)[1])
        self.assertEqual([expected_delivered], delivered)

    def test_an_answer_to_an_unknown_id_is_recorded_not_misrouted(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        session.feed(_line({"id": 9999, "result": {"turnId": "turn-1"}}))

        self.assertEqual("response to unknown request id 9999", session.protocol_error)
        # After acceptance a protocol error is never a licence to re-run.
        self.assertIsNone(session.fallback_reason)

    def test_a_server_to_client_request_is_a_protocol_error(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        session.feed(_line({"id": "req-1", "method": "approval/request"}))

        self.assertIn("unroutable", str(session.protocol_error))

    def test_text_that_is_not_protocol_passes_through_to_the_log(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        rendered = session.feed("warning: config file is deprecated\n")

        self.assertEqual("warning: config file is deprecated\n", rendered)


class OutputNormalizationTests(unittest.TestCase):
    """What a turn's stdout log says, and the one thing it must never say."""

    def setUp(self) -> None:
        self.process = _FakeProcess(scripted_responder())
        self.session = start_session(self.process)

    def test_deltas_stream_and_the_final_answer_is_captured(self) -> None:
        rendered = self.session.feed(
            notification("item/agentMessage/delta", delta="think")
            + notification("item/agentMessage/delta", delta="ing\n")
        )
        self.session.feed(
            notification(
                "item/completed",
                item={
                    "type": "agentMessage",
                    "text": "the turn is done",
                    "phase": "final_answer",
                },
            )
        )

        self.assertEqual("thinking\n", rendered)
        self.assertEqual("the turn is done", self.session.final_message)

    def test_a_command_execution_is_rendered_with_its_exit_code(self) -> None:
        rendered = self.session.feed(
            notification(
                "item/completed",
                item={
                    "type": "commandExecution",
                    "command": ["just", "test-fast"],
                    "exitCode": 1,
                },
            )
        )

        self.assertEqual("$ just test-fast (exit 1)\n", rendered)

    def test_the_echoed_prompt_and_guidance_never_reach_the_log(self) -> None:
        rendered = self.session.feed(
            notification("item/completed", item={"type": "userMessage", "text": PROMPT})
            + notification(
                "item/completed",
                item={"type": "userMessage", "text": GUIDANCE_BODY},
            )
        )

        self.assertEqual("", rendered)

    def test_a_completed_turn_is_terminal_and_reported(self) -> None:
        rendered = self.session.feed(
            notification("turn/completed", turn={"id": "turn-1", "status": "completed"})
        )

        self.assertEqual("turn completed\n", rendered)
        self.assertEqual(
            codex_app_server.TURN_STATUS_COMPLETED, self.session.turn_status
        )

    def test_a_failed_turn_carries_its_reason(self) -> None:
        rendered = self.session.feed(
            notification(
                "turn/completed",
                turn={
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "the model refused"},
                },
            )
        )

        self.assertEqual("turn failed: the model refused\n", rendered)
        self.assertEqual("the model refused", self.session.protocol_error)
        self.assertIsNone(self.session.fallback_reason)

    def test_another_turns_completion_is_not_mistaken_for_this_one(self) -> None:
        self.session.feed(
            notification("turn/completed", turn={"id": "turn-9", "status": "completed"})
        )

        self.assertIsNone(self.session.turn_status)


class SteeringTests(unittest.TestCase):
    """Same-turn delivery, and every way it is allowed to fail."""

    def test_a_successful_steer_states_the_thread_and_the_expected_turn(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        outcome = session.steer(guidance_request())

        self.assertTrue(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_DELIVERED, outcome.reason)
        params = process.requests[-1]["params"]
        self.assertEqual("thread-1", params["threadId"])
        self.assertEqual("turn-1", params["expectedTurnId"])
        self.assertEqual([{"type": "text", "text": GUIDANCE_BODY}], params["input"])

    def test_a_turn_that_cannot_be_steered_defers_without_quoting_the_body(
        self,
    ) -> None:
        process = _FakeProcess(
            scripted_responder(
                steer_error={"code": -32603, "message": "activeTurnNotSteerable"}
            )
        )
        session = start_session(process)

        outcome = session.steer(guidance_request())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_TRANSPORT_UNAVAILABLE, outcome.reason)
        self.assertEqual(codex_app_server.ACTIVE_TURN_NOT_STEERABLE, outcome.detail)
        self.assertNotIn(GUIDANCE_BODY, str(outcome.detail))

    def test_a_turn_that_moved_on_defers_as_stale(self) -> None:
        process = _FakeProcess(
            scripted_responder(
                steer_error={
                    "code": -32600,
                    "message": "expected active turn id `turn-1` but found `turn-2`",
                }
            )
        )
        session = start_session(process)

        outcome = session.steer(guidance_request())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertEqual("expected turn turn-1 is no longer active", outcome.detail)

    def test_a_steer_that_is_never_answered_defers_on_its_own_deadline(self) -> None:
        process = _FakeProcess(scripted_responder(answer_steer=False))
        session = start_session(process, steer_timeout=0.2)

        started = time.monotonic()
        outcome = session.steer(guidance_request())

        self.assertLess(time.monotonic() - started, WAIT_SECONDS)
        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_TRANSPORT_UNAVAILABLE, outcome.reason)
        self.assertEqual("turn/steer timed out", outcome.detail)

    def test_a_steer_before_a_turn_exists_defers_with_no_active_turn(self) -> None:
        process = _FakeProcess(scripted_responder(answer_steer=False))
        session = codex_app_server.CodexAppServerSession(
            process=process,  # type: ignore[arg-type]
            plan=worker_plan(),
            prompt=PROMPT,
        )

        outcome = session.steer(guidance_request())

        self.assertEqual(guidance_delivery.REASON_NO_ACTIVE_TURN, outcome.reason)
        self.assertEqual([], process.requests)

    def test_a_steer_after_the_turn_completed_defers_as_stale(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)
        session.feed(
            notification("turn/completed", turn={"id": "turn-1", "status": "completed"})
        )

        outcome = session.steer(guidance_request())

        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertNotIn(
            "turn/steer", [message.get("method") for message in process.requests]
        )

    def test_a_steer_never_raises_when_the_pipe_is_gone(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)
        process.stdin.closed = True

        outcome = session.steer(guidance_request())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_TRANSPORT_UNAVAILABLE, outcome.reason)
        self.assertNotIn(GUIDANCE_BODY, str(outcome.detail))


class ShutdownTests(unittest.TestCase):
    """Letting go of the conversation, on both the calm and the wedged path."""

    def test_shutdown_closes_stdin_and_lets_the_server_exit(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        session.feed(
            notification("turn/completed", turn={"id": "turn-1", "status": "completed"})
        )
        session.shutdown()

        self.assertTrue(process.stdin.closed)
        self.assertFalse(process.terminated)
        self.assertEqual(0, process.returncode)

    def test_a_server_that_will_not_exit_is_terminated_after_the_grace(self) -> None:
        process = _FakeProcess(scripted_responder(), exit_on_stdin_close=False)
        session = start_session(process, shutdown_grace=0.2)

        session.feed(
            notification("turn/completed", turn={"id": "turn-1", "status": "completed"})
        )
        session.shutdown()
        deadline = time.monotonic() + WAIT_SECONDS
        while not process.terminated and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertTrue(process.terminated)

    def test_a_steer_after_shutdown_defers_instead_of_writing(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)
        session.shutdown()

        outcome = session.steer(guidance_request())

        self.assertFalse(outcome.delivered)


class FallbackBoundaryTests(unittest.TestCase):
    """The exact line between "never started" and "must not be repeated"."""

    def test_a_failure_before_acceptance_offers_a_fallback(self) -> None:
        process = _FakeProcess(
            scripted_responder(thread_error={"code": -1, "message": "boom"})
        )

        session = start_session(process)

        self.assertFalse(session.turn_accepted)
        self.assertFalse(session.work_observed)
        self.assertIsNotNone(session.fallback_reason)

    def test_acceptance_forecloses_the_fallback_forever(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        session.feed("not json at all\n")
        session.feed(_line({"id": 4242, "result": {}}))

        self.assertIsNotNone(session.protocol_error)
        self.assertIsNone(session.fallback_reason)

    def test_emitted_work_forecloses_the_fallback_even_without_acceptance(
        self,
    ) -> None:
        process = _FakeProcess(scripted_responder(answer_steer=False))
        session = codex_app_server.CodexAppServerSession(
            process=process,  # type: ignore[arg-type]
            plan=worker_plan(),
            prompt=PROMPT,
        )

        session.feed(notification("item/agentMessage/delta", delta="already working"))
        session.feed(_line({"id": 4242, "result": {}}))

        self.assertTrue(session.work_observed)
        self.assertIsNone(session.fallback_reason)

    def test_a_dispatched_prompt_forecloses_the_fallback_without_an_answer(
        self,
    ) -> None:
        """The gap the other two miss: sent, never accepted, no work seen.

        An app-server that read ``turn/start`` and died before answering leaves
        no acceptance and no emitted work, so the two checks above both pass it
        -- and the fallback would re-run, on ``codex exec``, a prompt the
        provider may already be working on.
        """
        process = _FakeProcess(lost_turn_response())

        session = start_lost_session(process)

        self.assertTrue(session.turn_dispatched)
        self.assertFalse(session.turn_accepted)
        self.assertFalse(session.work_observed)
        self.assertIsNone(session.fallback_reason)


class LostSubmissionResponseTests(unittest.TestCase):
    """``turn/start`` left this process and nothing came back.

    The request that carries the prompt is the one request whose *write* decides
    what happened, because its answer can be lost in every way a protocol can
    lose one: never sent, refused, unintelligible. What the session owes a
    caller in all of those is the same, and it is not "nothing was submitted" --
    a caller holding an operator's correction acts on that by handing the words
    to somebody else.
    """

    def _session(self, process: _FakeProcess, **kwargs: Any) -> Any:
        return start_lost_session(process, **kwargs)

    def test_the_prompt_is_reported_out_before_it_is_written(self) -> None:
        order: list[str] = []
        scripted = scripted_responder()

        def responder(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
            if message.get("method") == "turn/start":
                order.append("written")
                process.returncode = 9
                return []
            return scripted(process, message)

        self._session(
            _FakeProcess(responder),
            on_submission_in_flight=lambda: order.append("in-flight"),
        )

        # Strictly before, not merely "also": the report has to be true even of
        # a write that fails halfway through, and a report made afterwards
        # cannot be.
        self.assertEqual(["in-flight", "written"], order)

    def test_a_lost_answer_is_not_a_prompt_that_stayed_home(self) -> None:
        submitted: list[str] = []
        in_flight: list[str] = []

        session = self._session(
            _FakeProcess(lost_turn_response()),
            on_submitted=lambda: submitted.append("submitted"),
            on_submission_in_flight=lambda: in_flight.append("in-flight"),
        )

        # The turn failed -- there is no acceptance to report -- but the failure
        # is silence about a prompt that left, which is what the first sink says
        # and the second one must not be made to say.
        self.assertIsNotNone(session.protocol_error)
        self.assertEqual(["in-flight"], in_flight)
        self.assertEqual([], submitted)
        self.assertTrue(session.turn_dispatched)

    def test_a_refused_turn_is_still_a_prompt_the_server_read(self) -> None:
        process = _FakeProcess(
            scripted_responder(
                turn_error={"code": -32602, "message": "turn/start refused"}
            )
        )

        session = self._session(process)

        # The server answered, and what it answered is "no" -- but it answered
        # *after* reading the prompt off the wire. Whether anything was done
        # with the words is the server's business and unknowable here.
        self.assertIsNotNone(session.protocol_error)
        self.assertTrue(session.turn_dispatched)
        self.assertFalse(session.turn_accepted)

    def test_an_unintelligible_answer_is_still_a_prompt_the_server_read(self) -> None:
        process = _FakeProcess(scripted_responder(turn_result={"turn": {}}))

        session = self._session(process)

        self.assertIsNotNone(session.protocol_error)
        self.assertTrue(session.turn_dispatched)
        self.assertFalse(session.turn_accepted)

    def test_a_write_that_broke_partway_counts_as_a_prompt_that_left(self) -> None:
        """Why the report is made before the write rather than after it.

        A pipe that breaks during the write has already carried some of the
        prompt, and nothing on this side knows how much. The only reading that
        is never wrong is the one this makes: the words may be out there.
        """
        process = _FakeProcess(scripted_responder())
        original = process.stdin.write

        def write(line: str) -> int:
            if '"turn/start"' in line:
                process.returncode = 9
                raise BrokenPipeError("the app-server is gone")
            return original(line)

        process.stdin.write = write  # type: ignore[method-assign]

        session = self._session(process)

        self.assertIsNotNone(session.protocol_error)
        self.assertTrue(session.turn_dispatched)
        self.assertIsNone(session.fallback_reason)

    def test_a_handshake_that_never_reached_the_turn_dispatched_nothing(self) -> None:
        process = _FakeProcess(
            scripted_responder(thread_error={"code": -1, "message": "boom"})
        )

        session = start_session(process)

        # The other end of the same rule. Nothing about the prompt was written,
        # so this turn is free to be run again -- which is what keeps a failed
        # handshake cheap.
        self.assertFalse(session.turn_dispatched)
        self.assertIsNotNone(session.fallback_reason)


class ResumeTests(unittest.TestCase):
    """Restoring a conversation is exact, re-checked, and never approximated."""

    def resume(self, process: _FakeProcess, **kwargs: Any) -> Any:
        return start_session(process, resume_conversation_id="thread-77", **kwargs)

    def test_a_resume_targets_the_named_thread_and_starts_none(self) -> None:
        process = _FakeProcess(scripted_responder())

        session = self.resume(process)

        self.assertEqual(
            ["initialize", "initialized", "thread/resume", "turn/start"],
            process.methods(),
        )
        resume_params = process.requests[2]["params"]
        self.assertEqual("thread-77", resume_params["threadId"])
        # The plan still travels: a restored thread is told what this turn is
        # allowed to do rather than inheriting whatever it last ran under.
        self.assertEqual("workspace-write", resume_params["sandbox"])
        self.assertEqual("/work", resume_params["cwd"])
        self.assertEqual("thread-77", process.requests[3]["params"]["threadId"])
        self.assertEqual("thread-77", session.thread_id)
        self.assertTrue(session.turn_accepted)

    def test_a_thread_nobody_asked_for_is_refused(self) -> None:
        process = _FakeProcess(scripted_responder(restored_thread_id="thread-other"))

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("thread-other", str(session.protocol_error))
        self.assertIn("thread-77", str(session.protocol_error))

    def test_a_restored_thread_that_runs_elsewhere_is_refused(self) -> None:
        process = _FakeProcess(
            scripted_responder(applied_settings={"cwd": "/somewhere/else"})
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("cwd", str(session.protocol_error))
        self.assertIn("/somewhere/else", str(session.protocol_error))

    def test_a_restored_approval_policy_that_drifted_is_refused(self) -> None:
        process = _FakeProcess(
            scripted_responder(applied_settings={"approvalPolicy": "on-request"})
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("approvalPolicy", str(session.protocol_error))

    def test_a_restored_sandbox_that_widened_is_refused(self) -> None:
        process = _FakeProcess(scripted_responder(applied_sandbox="dangerFullAccess"))

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("applied sandbox", str(session.protocol_error))

    def test_a_fresh_thread_is_not_held_to_the_resume_checks(self) -> None:
        """A ``thread/start`` echo cannot disagree about what it was told, so
        the extra checks would only ever fire on a server that answers with
        fields the client never sent."""
        process = _FakeProcess(scripted_responder(applied_settings={"cwd": None}))

        session = start_session(process)

        self.assertTrue(session.turn_accepted)

    def test_a_canonicalised_model_is_reported_rather_than_refused(self) -> None:
        process = _FakeProcess(scripted_responder(applied_model="gpt-5.1-codex-max"))

        session = self.resume(process)

        self.assertTrue(session.turn_accepted)
        self.assertEqual("gpt-5.1-codex-max", session.observed_model)

    def test_a_failed_resume_never_licenses_a_fresh_conversation(self) -> None:
        process = _FakeProcess(
            scripted_responder(
                resume_error={"code": -32602, "message": "no such thread"}
            )
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("no such thread", str(session.protocol_error))
        # The legacy transport starts a fresh session, so running it here would
        # answer "continue thread-77" with a turn that continues nothing.
        self.assertIsNone(session.fallback_reason)

    def test_no_fallback_survives_a_resume_even_before_the_thread_call(self) -> None:
        process = _FakeProcess(
            scripted_responder(thread_error={"code": -1, "message": "boom"})
        )

        fresh = start_session(
            _FakeProcess(
                scripted_responder(thread_error={"code": -1, "message": "boom"})
            )
        )
        resumed = start_session(process, resume_conversation_id="thread-77")

        # Same failure window, opposite verdicts: only the fresh start may be
        # re-run, because only it has nothing to lose by starting over.
        self.assertIsNotNone(fresh.fallback_reason)
        self.assertIsNone(resumed.fallback_reason)


class ConversationCaptureTests(unittest.TestCase):
    """The identifier is durable before the work exists, or there is no work."""

    def test_the_conversation_is_captured_before_the_turn_is_submitted(self) -> None:
        process = _FakeProcess(scripted_responder())
        seen: list[tuple[str, list[str]]] = []

        session = start_session(
            process,
            on_conversation=lambda native_id: seen.append(
                (native_id, process.methods())
            ),
        )

        self.assertEqual(1, len(seen))
        native_id, methods_at_capture = seen[0]
        self.assertEqual("thread-1", native_id)
        # The proof of ordering: no turn had been sent when the sink ran.
        self.assertNotIn("turn/start", methods_at_capture)
        self.assertTrue(session.turn_accepted)
        self.assertEqual("thread-1", session.thread_id)

    def test_a_resumed_conversation_is_captured_as_the_thread_it_restored(
        self,
    ) -> None:
        process = _FakeProcess(scripted_responder())
        captured: list[str] = []

        start_session(
            process,
            resume_conversation_id="thread-77",
            on_conversation=captured.append,
        )

        self.assertEqual(["thread-77"], captured)

    def test_a_capture_that_fails_submits_nothing(self) -> None:
        process = _FakeProcess(scripted_responder())

        def refuse(native_id: str) -> None:
            raise RuntimeError(f"the ledger is unwritable for {native_id}")

        session = start_refused_session(process, on_conversation=refuse)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("the ledger is unwritable", str(session.conversation_error))
        # Not a protocol failure, so it neither becomes one nor licenses a
        # legacy re-run of the work it just withheld.
        self.assertIsNone(session.protocol_error)
        self.assertIsNone(session.fallback_reason)
        self.assertTrue(process.stdin.closed)

    def test_a_gate_that_refuses_submits_nothing(self) -> None:
        process = _FakeProcess(scripted_responder())

        def refuse() -> None:
            raise RuntimeError("the turn was cancelled")

        session = start_refused_session(process, before_submit=refuse)

        self.assertNotIn("turn/start", process.methods())
        self.assertEqual("the turn was cancelled", session.cancelled_before_submission)
        self.assertIsNone(session.protocol_error)
        self.assertIsNone(session.fallback_reason)

    def test_the_gate_runs_after_the_conversation_is_recorded(self) -> None:
        """Ordered so a cancellation cannot cost the record of a thread that
        the app-server has already created."""
        process = _FakeProcess(scripted_responder())
        order: list[str] = []

        start_session(
            process,
            on_conversation=lambda native_id: order.append("capture"),
            before_submit=lambda: order.append("gate"),
        )

        self.assertEqual(["capture", "gate"], order)


class InterruptTests(unittest.TestCase):
    """Stopping a turn is a thing said to the conversation, not just a kill."""

    def test_an_interrupt_names_the_thread_and_the_turn(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        self.assertTrue(session.interrupt())

        self.assertEqual("turn/interrupt", process.methods()[-1])
        params = process.requests[-1]["params"]
        self.assertEqual("thread-1", params["threadId"])
        self.assertEqual("turn-1", params["turnId"])
        self.assertTrue(session.interrupt_requested)

    def test_an_interrupt_before_a_turn_exists_sends_nothing(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = codex_app_server.CodexAppServerSession(
            process=process,  # type: ignore[arg-type]
            plan=worker_plan(),
            prompt=PROMPT,
        )

        self.assertFalse(session.interrupt())

        self.assertEqual([], process.methods())
        self.assertFalse(session.interrupt_requested)

    def test_an_interrupt_after_the_turn_is_over_sends_nothing(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)
        session.feed(
            notification("turn/completed", turn={"id": "turn-1", "status": "completed"})
        )

        self.assertFalse(session.interrupt())

        self.assertNotIn("turn/interrupt", process.methods())

    def test_a_refused_interrupt_is_not_a_protocol_error(self) -> None:
        process = _FakeProcess(
            scripted_responder(
                interrupt_error={"code": -32600, "message": "no such turn"}
            )
        )
        session = start_session(process)

        self.assertFalse(session.interrupt())

        # The turn is ending regardless. Recording an error here would restate
        # a cancelled turn as one the harness failed to carry out.
        self.assertIsNone(session.protocol_error)
        self.assertTrue(session.interrupt_requested)

    def test_an_interrupt_that_is_never_answered_is_bounded(self) -> None:
        process = _FakeProcess(scripted_responder(answer_interrupt=False))
        session = start_session(process, interrupt_timeout=0.2)

        started = time.monotonic()
        self.assertFalse(session.interrupt())

        self.assertLess(time.monotonic() - started, WAIT_SECONDS)
        self.assertIsNone(session.protocol_error)

    def test_an_interrupted_turn_is_not_a_failed_turn(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        session.feed(
            notification(
                "turn/completed", turn={"id": "turn-1", "status": "interrupted"}
            )
        )

        self.assertTrue(session.turn_interrupted)
        self.assertEqual(codex_app_server.TURN_STATUS_INTERRUPTED, session.turn_status)
        self.assertIsNone(session.protocol_error)

    def test_a_failed_turn_still_reports_why_it_failed(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        session.feed(
            notification(
                "turn/completed",
                turn={
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "the model gave up"},
                },
            )
        )

        self.assertFalse(session.turn_interrupted)
        self.assertEqual("the model gave up", session.protocol_error)

    def test_shutdown_interrupts_a_turn_that_is_still_running(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)

        session.shutdown()

        self.assertIn("turn/interrupt", process.methods())
        self.assertTrue(process.stdin.closed)

    def test_shutdown_after_a_finished_turn_interrupts_nothing(self) -> None:
        process = _FakeProcess(scripted_responder())
        session = start_session(process)
        session.feed(
            notification("turn/completed", turn={"id": "turn-1", "status": "completed"})
        )

        session.shutdown()

        self.assertNotIn("turn/interrupt", process.methods())


class TurnPlanTests(unittest.TestCase):
    """The plan is what argv used to be, minus everything private."""

    def test_a_plan_payload_is_json_and_carries_no_prompt(self) -> None:
        invocation = harnesses.get_adapter("codex").build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt=PROMPT,
            last_message_path=Path("/work/last.txt"),
            model="gpt-5.1-codex",
            effort="high",
        )

        assert invocation.turn_plan is not None
        payload = invocation.turn_plan.to_payload()
        rendered = json.dumps(payload)
        self.assertNotIn(PROMPT, rendered)
        self.assertNotIn(PROMPT, json.dumps(invocation.argv))
        self.assertEqual("gpt-5.1-codex", payload["thread_params"]["model"])
        self.assertEqual("high", payload["turn_params"]["effort"])
        self.assertEqual(
            ["/plan"],
            payload["thread_params"]["config"]["sandbox_workspace_write"][
                "writable_roots"
            ],
        )

    def test_a_worker_turn_without_a_plan_dir_is_refused(self) -> None:
        with self.assertRaises(harnesses.HarnessError):
            harnesses.get_adapter("codex").build_invocation(
                cwd=Path("/work"),
                plan_dir=None,
                prompt=PROMPT,
                last_message_path=Path("/work/last.txt"),
            )


class AdapterTransportTests(unittest.TestCase):
    """The adapter against a real app-server process on a real pipe."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        self.record = self.tmp / "record.json"
        self.launch_log = self.tmp / "launches.log"
        # The real sweep shells out to ``ps``; these tests are about the
        # transport, not the process table.
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        self.env = mock.patch.dict(
            "os.environ",
            {
                "FAKE_APP_SERVER_RECORD": str(self.record),
                "FAKE_APP_SERVER_LAUNCH_LOG": str(self.launch_log),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _adapter(self, mode: str = "success") -> harnesses.CodexAdapter:
        os.environ["FAKE_APP_SERVER_MODE"] = mode
        executable = self.tmp / "codex-app-server"
        shutil.copyfile(FAKE_APP_SERVER_FIXTURE, executable)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return harnesses.CodexAdapter(executable=str(executable))

    def _run(self, adapter: harnesses.CodexAdapter, **kwargs: Any) -> Any:
        return adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
            **kwargs,
        )

    def _recorded(self) -> dict[str, Any]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_a_completed_turn_reports_success_and_its_final_message(self) -> None:
        chunks: list[tuple[str, str]] = []

        result = self._run(
            self._adapter(),
            stream_callback=lambda name, text: chunks.append((name, text)),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertEqual(2, len(result.argv))
        self.assertEqual("app-server", result.argv[1])
        self.assertEqual("the turn is done\n", result.last_message)
        self.assertEqual("fake-model-1", result.observed_model)
        self.assertIn("working\n", result.stdout)
        self.assertIn("$ just test-fast (exit 0)", result.stdout)
        self.assertIn("turn completed", result.stdout)

    def test_the_prompt_travels_the_protocol_and_never_the_log(self) -> None:
        result = self._run(self._adapter())

        self.assertEqual(PROMPT, self._recorded()["prompt"])
        self.assertNotIn(PROMPT, result.stdout)
        self.assertNotIn(PROMPT, " ".join(result.argv))

    def test_the_access_profile_crosses_the_wire(self) -> None:
        self._run(self._adapter())

        thread_params = self._recorded()["thread_params"]
        self.assertEqual("workspace-write", thread_params["sandbox"])
        self.assertEqual("never", thread_params["approvalPolicy"])
        self.assertEqual(
            [str(self.plan_dir)],
            thread_params["config"]["sandbox_workspace_write"]["writable_roots"],
        )

    def test_an_audit_turn_asks_for_the_read_only_sandbox(self) -> None:
        adapter = self._adapter()

        adapter.run(
            cwd=self.tmp,
            plan_dir=None,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
            access=harnesses.audit_read_only_access((self.tmp,)),
        )

        thread_params = self._recorded()["thread_params"]
        self.assertEqual("read-only", thread_params["sandbox"])
        self.assertNotIn("config", thread_params)

    def test_a_widened_sandbox_stops_the_turn_before_it_runs(self) -> None:
        result = self._run(self._adapter("widen-sandbox"))

        # No turn was ever sent, so the prompt never reached the app-server and
        # the legacy transport -- which states the profile in argv -- ran it.
        self.assertIsNone(self._recorded()["prompt"])
        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertIn("applied sandbox", result.stderr)

    def _resume_invocation(
        self, adapter: harnesses.CodexAdapter, native_id: str
    ) -> Any:
        return adapter.build_invocation(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            resume_conversation_id=native_id,
        )

    def test_a_resumed_turn_restores_exactly_the_thread_it_was_handed(self) -> None:
        adapter = self._adapter()
        captured: list[str] = []

        result = self._run(
            adapter,
            invocation=self._resume_invocation(adapter, "thread-77"),
            on_conversation=captured.append,
        )

        recorded = self._recorded()
        self.assertIsNone(recorded["thread_params"])
        self.assertEqual("thread-77", recorded["resume_params"]["threadId"])
        self.assertEqual("workspace-write", recorded["resume_params"]["sandbox"])
        self.assertEqual(["thread-77"], captured)
        self.assertEqual("thread-77", result.conversation_id)
        self.assertIsNone(result.continuity_unavailable)
        self.assertEqual(0, result.returncode)

    def test_a_fresh_turn_captures_the_thread_the_app_server_started(self) -> None:
        captured: list[str] = []

        result = self._run(self._adapter(), on_conversation=captured.append)

        self.assertEqual(["thread-1"], captured)
        self.assertEqual("thread-1", result.conversation_id)

    def test_a_resume_the_app_server_refuses_never_reruns_on_codex_exec(self) -> None:
        adapter = self._adapter("resume-unknown")

        result = self._run(
            adapter, invocation=self._resume_invocation(adapter, "thread-77")
        )

        # One launch: the fixture logs every spawn, so a second line here would
        # be `codex exec` answering "continue thread-77" with a fresh session.
        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertIsNone(self._recorded()["prompt"])
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no such thread", result.stderr)

    def test_a_widened_sandbox_on_a_resume_is_refused_without_a_fallback(self) -> None:
        adapter = self._adapter("widen-sandbox")

        result = self._run(
            adapter, invocation=self._resume_invocation(adapter, "thread-77")
        )

        # The same failure that falls back on a fresh start (above) does not
        # fall back here, because the turn asked to continue something.
        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertIn("applied sandbox", result.stderr)

    def test_a_conversation_that_cannot_be_recorded_abandons_the_turn(self) -> None:
        def refuse(native_id: str) -> None:
            raise RuntimeError(f"no room for {native_id}")

        result = self._run(self._adapter(), on_conversation=refuse)

        self.assertIsNone(self._recorded()["prompt"])
        self.assertIsNone(result.conversation_id)
        self.assertIn("no room for thread-1", str(result.conversation_error))
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(result.aborted)
        self.assertIn("turn abandoned", result.stderr)
        assert result.stream_io is not None
        self.assertEqual(len(result.stderr), result.stream_io["stderr_chars"])

    def test_an_abort_before_submission_ends_the_turn_at_the_handshake(self) -> None:
        adapter = self._adapter()
        abort = harnesses.AbortSignal()

        result = self._run(
            adapter,
            abort=abort,
            on_conversation=lambda native_id: abort.arm("the operator stopped it"),
        )

        self.assertIsNone(self._recorded()["prompt"])
        self.assertTrue(result.aborted)
        self.assertEqual(harnesses.CANCEL_STAGE_HANDSHAKE, result.cancel_stage)
        # The thread existed and was recorded before the cancellation landed,
        # so the turn that never ran still knows where it would have run.
        self.assertEqual("thread-1", result.conversation_id)

    def test_a_prompt_the_server_read_is_never_reported_as_unsubmitted(self) -> None:
        """Every way a ``turn/start`` answer can be lost, over a real pipe.

        The prompt is in the app-server's own record in all three, which is the
        fact that matters: the words are on the far side of the wire. What the
        client does not have is anything to say about them, and the result has
        to carry that absence rather than the ``False`` a caller would act on by
        handing the same words to the next worker.
        """
        for mode in ("die-before-turn-response", "reject-turn", "unidentified-turn"):
            with self.subTest(mode=mode):
                self.launch_log.unlink(missing_ok=True)

                result = self._run(self._adapter(mode))

                self.assertEqual(PROMPT, self._recorded()["prompt"])
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(result.prompt_submitted)
                self.assertFalse(harnesses.prompt_provably_unsubmitted(result))
                # No second launch either: the same silence that must not read
                # as "never sent" must not read as "safe to run again".
                self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
                self.assertEqual(
                    ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
                )

    def test_a_turn_the_server_never_saw_still_proves_its_prompt_stayed_home(
        self,
    ) -> None:
        submitted: list[str] = []
        adapter = self._adapter("resume-unknown")

        refused = self._run(
            adapter,
            invocation=self._resume_invocation(adapter, "thread-77"),
            on_prompt_submitted=lambda: submitted.append("submitted"),
        )

        # The counterweight to the test above: the refusal is answered before
        # ``turn/start`` is written, so this one *is* proof, and the run that
        # was carrying a correction gets it back.
        self.assertIsNone(self._recorded()["prompt"])
        self.assertEqual([], submitted)
        self.assertIs(False, refused.prompt_submitted)
        self.assertTrue(harnesses.prompt_provably_unsubmitted(refused))

    def test_guidance_reaches_the_running_turn_and_stays_out_of_the_log(self) -> None:
        adapter = self._adapter("await-steer")
        turn = turn_identity()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(turn)
        outcomes: list[guidance_delivery.GuidanceOutcome] = []

        def deliver() -> None:
            # The endpoint is opened the instant the process exists, which is
            # before the handshake has a turn to steer, so an early attempt
            # honestly defers with ``no-active-turn``. This retries the way the
            # mailbox poller's next tick would.
            deadline = time.monotonic() + WAIT_SECONDS
            while time.monotonic() < deadline:
                if endpoint.opened:
                    outcome = endpoint.submit(guidance_request())
                    outcomes.append(outcome)
                    if outcome.delivered:
                        return
                time.sleep(0.05)

        courier = threading.Thread(target=deliver)
        courier.start()
        self.addCleanup(courier.join, WAIT_SECONDS)

        result = self._run(adapter, live_delivery=endpoint)
        courier.join(timeout=WAIT_SECONDS)

        self.assertEqual([GUIDANCE_BODY], self._recorded()["steers"])
        self.assertTrue(outcomes[-1].delivered)
        self.assertTrue(
            all(
                outcome.reason == guidance_delivery.REASON_NO_ACTIVE_TURN
                for outcome in outcomes[:-1]
            ),
            outcomes,
        )
        self.assertEqual(0, result.returncode)
        self.assertNotIn(GUIDANCE_BODY, result.stdout)
        self.assertNotIn(GUIDANCE_BODY, result.stderr)


class AdapterFallbackTests(unittest.TestCase):
    """One turn runs once: before acceptance it may be retried, after it may not."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        self.launch_log = self.tmp / "launches.log"
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)

    def _dual_mode_codex(self, mode: str) -> Path:
        """One binary that answers both call shapes and logs every launch.

        The ``app-server`` half runs the real fixture under ``mode``; the
        ``exec`` half does nothing at all, so a turn that was run twice shows up
        as a second line in the log rather than as a difference in the output.
        """
        executable = self.tmp / "codex"
        executable.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import os, sys",
                    "from pathlib import Path",
                    f"log = Path({str(self.launch_log)!r})",
                    "with log.open('a', encoding='utf-8') as handle:",
                    "    handle.write(sys.argv[1] + '\\n')",
                    "if sys.argv[1] != 'app-server':",
                    "    raise SystemExit(0)",
                    f"os.environ['FAKE_APP_SERVER_MODE'] = {mode!r}",
                    f"exec(open({str(FAKE_APP_SERVER_FIXTURE)!r}).read())",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable

    def test_a_harness_without_an_app_server_runs_the_legacy_turn(self) -> None:
        executable = install_fake_codex(self.tmp)
        adapter = harnesses.CodexAdapter(executable=str(executable))
        chunks: list[tuple[str, str]] = []

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
            stream_callback=lambda name, text: chunks.append((name, text)),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertIn("exec", result.argv)
        self.assertIn("Runner status: complete", result.last_message)
        self.assertIn("codex app-server unavailable", result.stderr)
        # The declined attempt's own output is not the turn's output.
        streamed = "".join(text for _, text in chunks)
        self.assertNotIn("does not implement app-server", streamed)

    def test_the_declined_attempt_leaves_the_stream_accounting_honest(self) -> None:
        executable = install_fake_codex(self.tmp)
        adapter = harnesses.CodexAdapter(executable=str(executable))

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
        )

        assert result.stream_io is not None
        self.assertEqual(len(result.stderr), result.stream_io["stderr_chars"])

    def test_a_turn_that_died_after_acceptance_is_never_run_again(self) -> None:
        # The app-server dies after accepting the turn, and `exec` would run it
        # a second time if the adapter let it.
        executable = self._dual_mode_codex("die-after-accept")
        adapter = harnesses.CodexAdapter(executable=str(executable))

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
        )

        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertNotEqual(0, result.returncode)

    def test_a_turn_whose_answer_was_lost_is_never_run_again(self) -> None:
        """The same promise as above, for the death that leaves no acceptance.

        The app-server here reads the prompt and dies without answering, so
        nothing on this side was ever told the turn started. A fallback written
        from acceptance alone hands the identical prompt to ``codex exec``,
        which is a second execution of work the provider may already be doing.
        """
        executable = self._dual_mode_codex("die-before-turn-response")
        adapter = harnesses.CodexAdapter(executable=str(executable))

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
        )

        self.assertEqual(
            ["app-server"], self.launch_log.read_text(encoding="utf-8").split()
        )
        self.assertEqual(harnesses.TRANSPORT_APP_SERVER, result.transport)
        self.assertNotEqual(0, result.returncode)

    def test_a_fallback_reruns_the_legacy_argv_exactly_once(self) -> None:
        executable = install_fake_codex(self.tmp)
        adapter = harnesses.CodexAdapter(executable=str(executable))
        legacy = adapter.build_exec_argv(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
        )

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
        )

        self.assertEqual(legacy, result.argv)


class LegacyContinuityTests(unittest.TestCase):
    """What continuity a ``codex exec`` turn can and cannot honestly report."""

    SESSION_ID = "0199b0ce-2f5e-7c2a-9f11-4a1d0f0c8b31"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        self.adapter = harnesses.CodexAdapter(
            executable=str(install_fake_codex(self.tmp))
        )

    def _say(self, text: str) -> None:
        env = mock.patch.dict("os.environ", {"FAKE_CODEX_STDERR_TEXT": text})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, **kwargs: Any) -> Any:
        return self.adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
            **kwargs,
        )

    def test_a_legacy_turn_reports_the_session_its_output_names(self) -> None:
        self._say(f"session id: {self.SESSION_ID}\n")
        captured: list[str] = []

        result = self._run(on_conversation=captured.append)

        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertEqual([self.SESSION_ID], captured)
        self.assertEqual(self.SESSION_ID, result.conversation_id)
        self.assertIsNone(result.continuity_unavailable)

    def test_a_legacy_turn_that_names_nothing_says_so(self) -> None:
        result = self._run(on_conversation=lambda native_id: None)

        self.assertIsNone(result.conversation_id)
        # Stated rather than left empty: a caller that wanted to continue this
        # turn would otherwise read "no id" as "nobody has looked yet".
        self.assertEqual(
            "codex exec output named no session id", result.continuity_unavailable
        )

    def test_a_turn_nobody_asked_about_is_missing_nothing(self) -> None:
        result = self._run()

        self.assertIsNone(result.continuity_unavailable)
        self.assertEqual(0, result.returncode)

    def test_a_capture_that_fails_after_the_fact_does_not_lose_the_turn(self) -> None:
        self._say(f"session id: {self.SESSION_ID}\n")

        def refuse(native_id: str) -> None:
            raise RuntimeError("the ledger is unwritable")

        result = self._run(on_conversation=refuse)

        # `codex exec` submits its prompt on argv, so the work is already done
        # by the time its output names a session: there is nothing to retract,
        # and the only honest outcome is a turn without continuity.
        self.assertEqual(0, result.returncode)
        self.assertIn("Runner status: complete", result.last_message)
        self.assertIsNone(result.conversation_id)
        self.assertIn("the ledger is unwritable", str(result.continuity_unavailable))

    def test_a_legacy_invocation_cannot_be_built_for_a_resume(self) -> None:
        with self.assertRaises(harnesses.HarnessError) as caught:
            self.adapter.build_exec_invocation(
                cwd=self.tmp,
                plan_dir=self.plan_dir,
                prompt=PROMPT,
                last_message_path=self.last_message,
                resume_conversation_id="thread-77",
            )

        self.assertIn("fresh session", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
