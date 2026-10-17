"""The copilot ACP transport: the protocol client and the adapter seam.

Two levels, because the two failure families live in different places. The
session tests drive :class:`copilot_acp.CopilotAcpSession` against an in-process
fake whose every answer is scripted, so capability negotiation, framing, and the
exact answer to a permission request are asserted without a race. The adapter
tests spawn ``tests/fixtures/fake_copilot_acp.py`` for real, so process
ownership, the access restrictions crossing the wire, and the truthfulness of
the result are asserted against an actual pipe.

The invariant that gets the most attention here is the one the transport was
introduced against: moving a worker turn off ``-p`` must not widen what it may
do. The client advertises no filesystem and no terminal capability, it never
selects a standing permission option, and it only ever allows what the launch
argv already granted.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    copilot_acp,
    guidance_delivery,
    harness_adapter,
    harness_copilot,
    harness_process,
    harnesses,
)
from tests.support import REPO_ROOT, wait_until

FAKE_ACP_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_copilot_acp.py"

#: Every bounded wait in this module. Long enough that a loaded machine does not
#: fail a correct implementation, short enough that a wrong one fails rather
#: than hangs.
WAIT_SECONDS = 20.0

PROMPT = "do the work for session five"
SESSION_ID = "acp-session-1"

WORKER_GRANTED = copilot_acp.PermissionPolicy.for_turn(
    is_audit=False, unsafe_bypass=True
)
WORKER_UNGRANTED = copilot_acp.PermissionPolicy.for_turn(
    is_audit=False, unsafe_bypass=False
)
AUDIT = copilot_acp.PermissionPolicy.for_turn(is_audit=True, unsafe_bypass=True)


def line(payload: dict[str, Any]) -> str:
    return json.dumps(payload) + "\n"


def permission_request(options: list[dict[str, Any]] | None = None) -> str:
    if options is None:
        options = [
            {"optionId": "aa", "kind": "allow_always"},
            {"optionId": "ao", "kind": "allow_once"},
            {"optionId": "ro", "kind": "reject_once"},
            {"optionId": "ra", "kind": "reject_always"},
        ]
    return line(
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "method": "session/request_permission",
            "params": {
                "sessionId": SESSION_ID,
                "toolCall": {"toolCallId": "t1", "title": "rm -rf /"},
                "options": options,
            },
        }
    )


def update(payload: dict[str, Any], *, session_id: str = SESSION_ID) -> str:
    return line(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }
    )


class _FakeStdin:
    """The write end of a fake agent, answering on the caller's thread."""

    def __init__(self, process: _FakeProcess) -> None:
        self._process = process
        self.closed = False

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("write to a closed pipe")
        self._process.handle(text)
        return len(text)

    def flush(self) -> None:
        if self.closed:
            raise ValueError("flush of a closed pipe")

    def close(self) -> None:
        self.closed = True
        self._process.on_stdin_closed()


class _FakeProcess:
    """A process-shaped object whose answers a test writes by hand.

    ``responder`` is called with each decoded message and returns the raw text
    to feed back, which is what lets a test split one response across reads,
    answer out of order, or never answer at all.
    """

    def __init__(self, responder: Any = None) -> None:
        self.stdin = _FakeStdin(self)
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.messages: list[dict[str, Any]] = []
        self.responder = responder
        self.session: copilot_acp.CopilotAcpSession | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def on_stdin_closed(self) -> None:
        if self.returncode is None:
            self.returncode = 0

    def handle(self, text: str) -> None:
        message = json.loads(text)
        self.messages.append(message)
        if self.responder is None:
            return
        for chunk in self.responder(self, message):
            assert self.session is not None
            self.session.feed(chunk)

    def methods(self) -> list[str]:
        return [
            str(message.get("method"))
            for message in self.messages
            if "method" in message
        ]

    def sent(self, method: str) -> dict[str, Any]:
        for message in self.messages:
            if message.get("method") == method:
                return message
        raise AssertionError(f"no {method} message was sent")

    def answer_to(self, request_id: object) -> dict[str, Any]:
        for message in self.messages:
            if message.get("id") == request_id and "method" not in message:
                return message
        raise AssertionError(f"no answer to request {request_id!r}")


def scripted_responder(
    *,
    protocol_version: int = copilot_acp.PROTOCOL_VERSION,
    config_options: list[dict[str, Any]] | None = None,
    session_id: str = SESSION_ID,
    stop_reason: str | None = copilot_acp.STOP_REASON_END_TURN,
    prompt_error: dict[str, Any] | None = None,
    updates: list[str] | None = None,
):
    """The happy-path agent, with one seam per failure a test wants."""

    if config_options is None:
        config_options = [
            {"id": "model", "currentValue": "fake-model-1"},
            {"id": "reasoning_effort", "currentValue": "low"},
        ]

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
                            "protocolVersion": protocol_version,
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
                        "result": {
                            "sessionId": session_id,
                            "configOptions": config_options,
                        },
                    }
                )
            ]
        if method == "session/prompt":
            chunks = list(updates or [])
            if prompt_error is not None:
                chunks.append(
                    line({"jsonrpc": "2.0", "id": request_id, "error": prompt_error})
                )
                return chunks
            result: dict[str, Any] = {}
            if stop_reason is not None:
                result["stopReason"] = stop_reason
            chunks.append(line({"jsonrpc": "2.0", "id": request_id, "result": result}))
            return chunks
        return []

    return respond


def build_session(
    process: _FakeProcess,
    *,
    permissions: copilot_acp.PermissionPolicy = WORKER_GRANTED,
    **overrides: Any,
) -> copilot_acp.CopilotAcpSession:
    fields: dict[str, Any] = {
        "process": process,
        "prompt": PROMPT,
        "cwd": "/work",
        "permissions": permissions,
        "request_timeout": WAIT_SECONDS,
        "shutdown_grace": WAIT_SECONDS,
    }
    fields.update(overrides)
    session = copilot_acp.CopilotAcpSession(**fields)
    process.session = session
    return session


def run_session(
    process: _FakeProcess, **overrides: Any
) -> copilot_acp.CopilotAcpSession:
    """Drive one whole turn against a scripted agent and return the session."""
    session = build_session(process, **overrides)
    session.start()
    session.shutdown()
    return session


class HandshakeTests(unittest.TestCase):
    """Initialization and capability negotiation, before any work is sent."""

    def test_initialize_advertises_no_filesystem_and_no_terminal(self) -> None:
        process = _FakeProcess(scripted_responder())

        run_session(process)

        params = process.sent("initialize")["params"]
        self.assertEqual(copilot_acp.PROTOCOL_VERSION, params["protocolVersion"])
        self.assertEqual(
            {"readTextFile": False, "writeTextFile": False},
            params["clientCapabilities"]["fs"],
        )
        self.assertFalse(params["clientCapabilities"]["terminal"])
        self.assertEqual(copilot_acp.CLIENT_NAME, params["clientInfo"]["name"])

    def test_a_version_the_client_does_not_speak_stops_the_turn(self) -> None:
        process = _FakeProcess(scripted_responder(protocol_version=99))

        session = run_session(process)

        self.assertNotIn("session/new", process.methods())
        self.assertIsNone(session.session_id)
        self.assertIsNone(session.stop_reason)
        self.assertIn("protocol version 99", str(session.protocol_error))

    def test_session_new_names_the_cwd_and_adds_no_mcp_servers(self) -> None:
        process = _FakeProcess(scripted_responder())

        run_session(process, cwd="/repo/root")

        params = process.sent("session/new")["params"]
        self.assertEqual("/repo/root", params["cwd"])
        self.assertEqual([], params["mcpServers"])

    def test_the_session_id_and_agent_identity_are_learned_from_the_handshake(
        self,
    ) -> None:
        process = _FakeProcess(scripted_responder())

        session = run_session(process)

        self.assertEqual(SESSION_ID, session.session_id)
        self.assertEqual("FakeCopilot 0.0.1", session.agent_info)

    def test_a_session_without_an_id_stops_the_turn_before_the_prompt(self) -> None:
        process = _FakeProcess(scripted_responder(session_id=""))

        session = run_session(process)

        self.assertNotIn("session/prompt", process.methods())
        self.assertIn("no session id", str(session.protocol_error))
        self.assertFalse(session.prompt_submitted)


class LaunchControlEchoTests(unittest.TestCase):
    """Model and effort are launch flags, so the echo is the only check."""

    def test_the_echo_becomes_the_observed_model_and_effort(self) -> None:
        process = _FakeProcess(scripted_responder())

        session = run_session(process, expected_model="fake-model-1")

        self.assertEqual("fake-model-1", session.observed_model)
        self.assertEqual("low", session.observed_effort)
        self.assertEqual((), session.option_mismatches)

    def test_a_drifted_control_is_reported_and_the_turn_still_runs(self) -> None:
        process = _FakeProcess(scripted_responder())

        session = run_session(
            process, expected_model="asked-for-this", expected_effort="high"
        )

        self.assertEqual(
            (
                "model is 'fake-model-1', expected 'asked-for-this'",
                "effort is 'low', expected 'high'",
            ),
            session.option_mismatches,
        )
        # Reported, never enforced: neither control changes what the turn is
        # allowed to do, so the work still happened.
        self.assertEqual(copilot_acp.STOP_REASON_END_TURN, session.stop_reason)

    def test_a_config_option_update_revises_the_observed_model(self) -> None:
        process = _FakeProcess(
            scripted_responder(
                updates=[
                    update(
                        {
                            "sessionUpdate": "config_option_update",
                            "id": "model",
                            "currentValue": "switched-mid-turn",
                        }
                    )
                ]
            )
        )

        session = run_session(process)

        self.assertEqual("switched-mid-turn", session.observed_model)


class PermissionTests(unittest.TestCase):
    """``session/request_permission``, answered explicitly and never blankly."""

    def _answer(
        self,
        policy: copilot_acp.PermissionPolicy,
        *,
        request: str | None = None,
    ) -> tuple[_FakeProcess, copilot_acp.CopilotAcpSession, str]:
        process = _FakeProcess()
        session = build_session(process, permissions=policy)
        rendered = session.feed(request or permission_request())
        return process, session, rendered

    def test_a_worker_with_tools_granted_allows_exactly_one_call(self) -> None:
        process, session, rendered = self._answer(WORKER_GRANTED)

        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "ao"}},
            process.answer_to("perm-1")["result"],
        )
        decision = session.permission_decisions[0]
        self.assertEqual(copilot_acp.DECISION_ALLOWED, decision.decision)
        self.assertEqual("rm -rf /", decision.tool)
        self.assertIn("permission allowed", rendered)

    def test_a_worker_without_tools_granted_rejects_the_call(self) -> None:
        process, session, _ = self._answer(WORKER_UNGRANTED)

        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "ro"}},
            process.answer_to("perm-1")["result"],
        )
        self.assertEqual(
            copilot_acp.DECISION_REJECTED, session.permission_decisions[0].decision
        )

    def test_an_audit_turn_rejects_even_with_the_bypass_flag_set(self) -> None:
        """The profile decides, not the flag: an audit grants nothing."""
        process, session, _ = self._answer(AUDIT)

        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "ro"}},
            process.answer_to("perm-1")["result"],
        )
        self.assertIn("audit-read-only", session.permission_decisions[0].reason)

    def test_a_standing_option_is_never_selected(self) -> None:
        """Only the two ``_always`` kinds are offered, so nothing may be picked."""
        process, session, _ = self._answer(
            WORKER_GRANTED,
            request=permission_request(
                [
                    {"optionId": "aa", "kind": "allow_always"},
                    {"optionId": "ra", "kind": "reject_always"},
                ]
            ),
        )

        self.assertEqual(
            {"outcome": {"outcome": "cancelled"}},
            process.answer_to("perm-1")["result"],
        )
        decision = session.permission_decisions[0]
        self.assertEqual(copilot_acp.DECISION_CANCELLED, decision.decision)
        self.assertIsNone(decision.option_id)

    def test_an_unnamed_tool_call_still_produces_a_decision(self) -> None:
        _, session, _ = self._answer(
            WORKER_GRANTED,
            request=line(
                {
                    "jsonrpc": "2.0",
                    "id": "perm-1",
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": SESSION_ID,
                        "options": [{"optionId": "ao", "kind": "allow_once"}],
                    },
                }
            ),
        )

        self.assertEqual("unnamed tool call", session.permission_decisions[0].tool)


class UnadvertisedRequestTests(unittest.TestCase):
    """The client answers only what it said it could do."""

    def test_a_filesystem_request_is_refused_as_an_unknown_method(self) -> None:
        process = _FakeProcess()
        session = build_session(process)

        rendered = session.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": "fs-1",
                    "method": "fs/read_text_file",
                    "params": {"path": "/etc/passwd"},
                }
            )
        )

        error = process.answer_to("fs-1")["error"]
        self.assertEqual(-32601, error["code"])
        self.assertIn("fs/read_text_file", error["message"])
        self.assertIn("refused unadvertised request", rendered)
        self.assertEqual((), session.permission_decisions)

    def test_a_terminal_request_is_refused_the_same_way(self) -> None:
        process = _FakeProcess()
        session = build_session(process)

        session.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": "term-1",
                    "method": "terminal/create",
                    "params": {"command": "sh"},
                }
            )
        )

        self.assertEqual(-32601, process.answer_to("term-1")["error"]["code"])


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
            session._request("initialize", {})  # noqa: SLF001 - the closed path


class AdapterTransportTests(unittest.TestCase):
    """The adapter against a real copilot process on a real pipe."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        self.record = self.tmp / "record.json"
        # The real sweep shells out to ``ps``; these tests are about the
        # transport, not the process table.
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        self.env = mock.patch.dict("os.environ", {"FAKE_ACP_RECORD": str(self.record)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _adapter(self, mode: str = "success") -> harness_copilot.CopilotAdapter:
        os.environ["FAKE_ACP_MODE"] = mode
        executable = self.tmp / "copilot"
        shutil.copyfile(FAKE_ACP_FIXTURE, executable)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return harness_copilot.CopilotAdapter(executable=str(executable))

    def _run(self, adapter: harness_copilot.CopilotAdapter, **kwargs: Any) -> Any:
        options: dict[str, Any] = {
            "cwd": self.tmp,
            "plan_dir": self.plan_dir,
            "prompt": PROMPT,
            "last_message_path": self.last_message,
            "timeout": WAIT_SECONDS,
            "unsafe_bypass": True,
        }
        options.update(kwargs)
        return adapter.run(**options)

    def _recorded(self) -> dict[str, Any]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_an_ordinary_worker_turn_runs_through_fake_acp(self) -> None:
        chunks: list[tuple[str, str]] = []

        result = self._run(
            self._adapter(),
            stream_callback=lambda name, text: chunks.append((name, text)),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(harnesses.TRANSPORT_ACP, result.transport)
        self.assertEqual("working\n", result.last_message)
        self.assertEqual("fake-model-1", result.observed_model)
        self.assertIn("$ just test-fast", result.stdout)
        self.assertIn("working\n", result.stdout)
        # Streamed as it arrived, one protocol message at a time, rather than
        # withheld until the turn was over.
        self.assertEqual(
            ["$ just test-fast\n", "work", "ing\n"],
            [text for name, text in chunks if name == "stdout"],
        )

    def test_the_agents_last_unterminated_line_still_reaches_the_log(self) -> None:
        result = self._run(self._adapter("trailing-prose"))

        # ``readline`` returns an unterminated string only at EOF, so prose the
        # binary printed on its way out arrives with no newline to frame it and
        # would otherwise be left in the decoder's buffer.
        self.assertEqual(0, result.returncode)
        self.assertIn("copilot: goodbye\n", result.stdout)

    def test_the_prompt_travels_the_protocol_and_never_the_log(self) -> None:
        result = self._run(self._adapter())

        self.assertEqual(PROMPT, self._recorded()["prompt"])
        self.assertNotIn(PROMPT, result.stdout)
        self.assertNotIn(PROMPT, " ".join(result.argv))
        self.assertNotIn("-p", result.argv)

    def test_the_worker_argv_keeps_its_access_restrictions(self) -> None:
        self._run(self._adapter())

        argv = self._recorded()["argv"]
        self.assertEqual(["-C", str(self.tmp)], argv[:2])
        self.assertIn("--add-dir", argv)
        self.assertEqual(str(self.plan_dir), argv[argv.index("--add-dir") + 1])
        self.assertIn("--allow-all-tools", argv)
        self.assertIn("--allow-all-paths", argv)
        self.assertIn("--no-ask-user", argv)
        self.assertIn("--silent", argv)
        self.assertEqual("--acp", argv[-1])

    def test_a_guard_deny_rule_still_reaches_the_launch_argv(self) -> None:
        policy = self.tmp / "policy.json"
        policy.write_text(
            json.dumps({"guards": [{"command": ["git", "push"]}]}), encoding="utf-8"
        )
        env = dict(os.environ)
        env[harness_adapter.guards.GUARD_POLICY_ENV_VAR] = str(policy)

        self._run(self._adapter(), env=env)

        self.assertIn("--deny-tool=shell(git push)", self._recorded()["argv"])

    def test_the_negotiated_capabilities_cross_the_wire(self) -> None:
        self._run(self._adapter())

        recorded = self._recorded()
        self.assertEqual(1, recorded["protocol_version"])
        self.assertEqual(
            {"readTextFile": False, "writeTextFile": False},
            recorded["client_capabilities"]["fs"],
        )
        self.assertFalse(recorded["client_capabilities"]["terminal"])
        self.assertEqual(str(self.tmp), recorded["session_params"]["cwd"])

    def test_an_audit_turn_keeps_its_established_one_shot_path(self) -> None:
        adapter = self._adapter()

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=None,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
            access=harnesses.audit_read_only_access((self.tmp,)),
        )

        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertNotIn("--acp", result.argv)
        self.assertIn("-p", result.argv)
        self.assertEqual(PROMPT, result.argv[-1])
        self.assertIn(
            f"--available-tools={harness_copilot.COPILOT_AUDIT_TOOLS}", result.argv
        )
        self.assertIn("--deny-tool=shell", result.argv)
        # Nothing spoke ACP, so the process recorded no handshake at all.
        self.assertIsNone(self._recorded()["client_capabilities"])
        self.assertIsNone(self._recorded()["prompt"])

    def test_a_permission_request_is_answered_once_under_bypass(self) -> None:
        result = self._run(self._adapter("permission"))

        answer = self._recorded()["permissions"][0]
        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "ao"}}, answer["result"]
        )
        self.assertEqual(0, result.returncode)

    def test_a_permission_request_without_bypass_is_rejected(self) -> None:
        result = self._run(self._adapter("permission"), unsafe_bypass=False)

        answer = self._recorded()["permissions"][0]
        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "ro"}}, answer["result"]
        )
        self.assertIn("rejected a permission request", result.stderr)

    def test_an_unadvertised_request_is_refused_over_a_real_pipe(self) -> None:
        result = self._run(self._adapter("unadvertised"))

        answer = self._recorded()["unadvertised"][0]
        self.assertEqual(-32601, answer["error"]["code"])
        self.assertIn("refused unadvertised request", result.stdout)

    def test_a_refusal_stop_reason_is_not_reported_as_success(self) -> None:
        result = self._run(self._adapter("refusal"))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("copilot acp stopped early: refusal", result.stderr)

    def test_an_agent_that_dies_after_the_prompt_is_not_reported_as_success(
        self,
    ) -> None:
        result = self._run(self._adapter("die-after-submit"))

        # The turn was spent: the prompt crossed the wire and the agent left
        # without a verdict, which must never read as a clean run.
        self.assertEqual(PROMPT, self._recorded()["prompt"])
        self.assertEqual(9, result.returncode)
        self.assertIn("copilot acp", result.stderr)

    def test_an_agent_with_no_acp_mode_fails_rather_than_falls_back(self) -> None:
        result = self._run(self._adapter("decline"))

        self.assertEqual(harnesses.TRANSPORT_ACP, result.transport)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("this build has no acp mode", result.stderr)

    def test_a_drifted_launch_control_is_reported_on_the_turn(self) -> None:
        result = self._run(self._adapter("model-drift"), model="asked-for-this")

        self.assertIn("launch control mismatch", result.stderr)
        self.assertEqual(0, result.returncode)

    def test_a_live_steer_is_declined_as_unsupported_rather_than_ignored(self) -> None:
        adapter = self._adapter()
        outcomes: list[guidance_delivery.GuidanceOutcome] = []

        def steer(transport: harnesses.HarnessTransport | None) -> None:
            assert transport is not None
            deliver = transport.guidance_transport()
            assert deliver is not None
            outcomes.append(deliver(_guidance_request()))

        self._run(adapter, on_transport=steer)

        self.assertEqual(
            [guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER],
            [outcome.reason for outcome in outcomes],
        )


def _guidance_request() -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id="11111111-1111-4111-8111-111111111111",
        seq=1,
        text="prefer the smaller change",
        turn=guidance_delivery.TurnIdentity(
            run_id="run-1", session_id="5", turn_index=1, harness="copilot"
        ),
    )


if __name__ == "__main__":
    unittest.main()
