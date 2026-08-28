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
``codex exec`` only while ``turn/start`` has not been accepted and no work has
been emitted, and the tests below hold both sides of that line.
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
        "thread_params": {"cwd": "/work", "sandbox": "workspace-write"},
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
    thread_error: dict[str, Any] | None = None,
    answer_steer: bool = True,
    steer_error: dict[str, Any] | None = None,
):
    """The happy-path server, with one seam per failure a test wants."""

    def respond(process: _FakeProcess, message: dict[str, Any]) -> list[str]:
        method = message.get("method")
        if "id" not in message:
            return []
        request_id = message["id"]
        if method == "initialize":
            return [_line({"id": request_id, "result": {"userAgent": "fake"}})]
        if method == "thread/start":
            if thread_error is not None:
                return [_line({"id": request_id, "error": thread_error})]
            params = message.get("params") or {}
            sandbox = (
                applied_sandbox
                or {
                    "read-only": "readOnly",
                    "workspace-write": "workspaceWrite",
                    "danger-full-access": "dangerFullAccess",
                }[params["sandbox"]]
            )
            return [
                _line(
                    {
                        "id": request_id,
                        "result": {
                            "thread": {"id": "thread-1"},
                            "sandbox": {"type": sandbox},
                            "model": "fake-model-1",
                        },
                    }
                )
            ]
        if method == "turn/start":
            return [_line({"id": request_id, "result": {"turn": {"id": "turn-1"}}})]
        if method == "turn/steer":
            if not answer_steer:
                return []
            if steer_error is not None:
                return [_line({"id": request_id, "error": steer_error})]
            return [_line({"id": request_id, "result": {"turnId": "turn-1"}})]
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
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if session.turn_accepted or session.protocol_error is not None:
            return session
        time.sleep(0.01)
    raise AssertionError("the handshake neither completed nor failed")


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
        sweep = mock.patch.object(harnesses, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)

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
        executable = self.tmp / "codex"
        launch_log = self.tmp / "launches.log"
        # One binary that answers both call shapes: the app-server dies after
        # accepting the turn, and `exec` would run it a second time if the
        # adapter let it.
        executable.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import os, sys",
                    "from pathlib import Path",
                    f"log = Path({str(launch_log)!r})",
                    "with log.open('a', encoding='utf-8') as handle:",
                    "    handle.write(sys.argv[1] + '\\n')",
                    "if sys.argv[1] != 'app-server':",
                    "    raise SystemExit(0)",
                    "os.environ['FAKE_APP_SERVER_MODE'] = 'die-after-accept'",
                    f"exec(open({str(FAKE_APP_SERVER_FIXTURE)!r}).read())",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        adapter = harnesses.CodexAdapter(executable=str(executable))

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=self.plan_dir,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
        )

        self.assertEqual(["app-server"], launch_log.read_text(encoding="utf-8").split())
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


if __name__ == "__main__":
    unittest.main()
