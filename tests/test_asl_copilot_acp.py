# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401, F403, F405, F822, I001
"""ACP handshake, control, permission, and capability tests."""

# ruff: noqa: F401, F403, F405
from tests.asl_copilot_acp_support import *
from tests.asl_copilot_acp_support import (
    FAKE_ACP_FIXTURE,
    PROMPT,
    SESSION_ID,
    WAIT_SECONDS,
    _FakeProcess,
    build_session,
    line,
    update,
)


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
