"""Shared fixtures for test_asl_codex_app_server."""

from __future__ import annotations

import json
import time
from typing import Any

from agent_session_loop import (
    codex_app_server,
    guidance_delivery,
)
from tests.support import REPO_ROOT

FAKE_APP_SERVER_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_codex_app_server.py"


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
