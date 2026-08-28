"""A bounded JSON-RPC client for the Codex app-server transport.

``codex exec`` is a one-shot process: a prompt goes in on argv, output comes
back on stdout, and there is no way to say anything to the model once the turn
has begun. The app-server speaks a long-lived JSON-RPC conversation over the
same two pipes instead, and that conversation has ``turn/steer`` -- the one
call that hands a running turn new input while it is still running. Same-turn
guidance delivery is why this module exists; everything else here is the cost
of keeping the rest of the turn's guarantees intact while the transport
changes underneath them.

The protocol surface this module uses, all of it verified against
``codex-cli`` and the app-server JSON schema bundle:

- ``initialize`` request, then the ``initialized`` notification.
- ``thread/start``, whose response echoes the *effective* sandbox policy,
  model, and reasoning effort. That echo is checked, not trusted: an audit
  turn that did not get a read-only sandbox never gets to send a turn.
- ``turn/start``, whose response carries the accepted ``turnId``. Acceptance
  is the point of no return for the fallback in :mod:`.harnesses`.
- Streamed notifications (``turn/started``, ``item/*``, ``error``) until the
  terminal ``turn/completed``.
- ``turn/steer`` with a required ``expectedTurnId`` precondition.

Threading. Three threads touch a session and each has one job:

- the harness's stdout reader calls :meth:`feed` and nothing else;
- a driver thread owned by this module runs the handshake and the orderly
  shutdown;
- the mailbox poller calls :meth:`steer`.

``_state_lock`` guards the mutable state and the pending-request table and is
never held across a blocking wait. ``_write_lock`` serializes stdin writes,
which is the only resource the driver and the poller share.

Privacy. Prompts and guidance bodies go *out* over this transport and must not
come back in through the log: the app-server echoes every user message,
including a steered one, as a ``userMessage`` item. :meth:`feed` therefore
renders no user message at all. Nothing derived from a guidance body is
rendered, returned, or persisted.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import __version__, guidance_delivery

#: The transport discriminator persisted alongside ``argv``.
TRANSPORT_APP_SERVER = "app-server"
#: The legacy one-shot ``codex exec`` transport.
TRANSPORT_EXEC = "exec"

#: Client identity sent with ``initialize``. The app-server folds this into its
#: user agent, so it is what a Codex-side log shows the turn was launched by.
CLIENT_NAME = "agent-session-loop"
CLIENT_TITLE = "ASL"

#: How long a handshake request may take before the turn is declared unusable.
#: Generous, because ``thread/start`` boots the configured MCP servers.
REQUEST_TIMEOUT_SECONDS = 120.0

#: How long ``turn/steer`` may take. Deliberately far tighter than the
#: handshake: the caller is a poll tick holding the live-delivery endpoint
#: lock, so a slow steer delays closing the endpoint at end of turn.
STEER_TIMEOUT_SECONDS = 10.0

#: How long the app-server gets to exit on its own after its stdin is closed
#: at end of turn. It exits in milliseconds in practice; this only bounds what
#: a wedged one costs before the driver terminates it.
SHUTDOWN_GRACE_SECONDS = 10.0

#: How often a bounded wait re-checks whether the process is still alive. A
#: dead app-server can never answer, so noticing quickly is what keeps a
#: handshake failure from costing the full request timeout.
_LIVENESS_POLL_SECONDS = 0.05

#: Turn statuses the app-server reports on ``turn/completed``.
TURN_STATUS_COMPLETED = "completed"
TURN_STATUS_INTERRUPTED = "interrupted"
TURN_STATUS_FAILED = "failed"

#: Sandbox policy discriminators as the ``thread/start`` response echoes them.
SANDBOX_READ_ONLY = "readOnly"
SANDBOX_WORKSPACE_WRITE = "workspaceWrite"
SANDBOX_DANGER_FULL_ACCESS = "dangerFullAccess"

#: The app-server's own name for "this turn exists but cannot take steering",
#: e.g. a `/review` or a manual `/compact` turn.
ACTIVE_TURN_NOT_STEERABLE = "activeTurnNotSteerable"


class AppServerError(Exception):
    """The app-server conversation cannot continue.

    Raised for a transport failure (a dead process, a closed pipe, a request
    that never answered) and for a protocol failure (an error response, an
    unparseable or contradictory message). One exception type, because every
    caller does the same thing with both: stop driving the turn and record why.
    """


@dataclass
class _Pending:
    """One in-flight request, waiting for the reader thread to answer it."""

    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str | None = None

    def resolve(self, *, result: Any = None, error: str | None = None) -> None:
        self.result = result
        self.error = error
        self.event.set()


class _LineDecoder:
    """Reassemble complete lines from arbitrarily fragmented reads.

    The harness reader hands over whatever ``readline`` produced, which is
    normally one whole line but is not guaranteed to be: a long JSON message
    can arrive split across reads. Buffering here means a fragment is never
    parsed as a message and never mistaken for passthrough text.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        self._buffer += chunk
        if "\n" not in self._buffer:
            return []
        *lines, self._buffer = self._buffer.split("\n")
        return lines

    def flush(self) -> list[str]:
        """Return whatever the last read left behind without a newline."""
        remainder, self._buffer = self._buffer, ""
        return [remainder] if remainder else []


@dataclass(frozen=True)
class TurnPlan:
    """The prompt-free description of the conversation one turn will have.

    Persisted and printed in place of the argv that used to carry the whole
    invocation: with the app-server, argv says only which binary runs, and
    *this* is where the access profile, model, effort, and writable roots
    actually live. It never contains the prompt or any guidance body, so it is
    safe everywhere argv was safe and safer than argv ever was.
    """

    thread_params: Mapping[str, Any]
    turn_params: Mapping[str, Any]
    #: The sandbox discriminator this turn requires the app-server to confirm.
    expected_sandbox: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "thread_params": json.loads(json.dumps(self.thread_params)),
            "turn_params": json.loads(json.dumps(self.turn_params)),
            "expected_sandbox": self.expected_sandbox,
        }


class CodexAppServerSession:
    """One app-server conversation, scoped to exactly one harness turn.

    Constructed around a spawned process, driven by :meth:`start`, fed by the
    harness reader, and torn down by :meth:`shutdown`. The turn it runs is the
    only turn it will ever run: the thread is started, one turn is sent, and
    the session closes its own stdin as soon as that turn completes.
    """

    def __init__(
        self,
        *,
        process: subprocess.Popen,
        plan: TurnPlan,
        prompt: str,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        steer_timeout: float = STEER_TIMEOUT_SECONDS,
        shutdown_grace: float = SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self._process = process
        self._plan = plan
        self._prompt = prompt
        self._request_timeout = request_timeout
        self._steer_timeout = steer_timeout
        self._shutdown_grace = shutdown_grace

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._decoder = _LineDecoder()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0

        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._observed_model: str | None = None
        self._final_message = ""
        self._final_answer_seen = False
        self._turn_status: str | None = None
        self._work_observed = False
        self._turn_accepted = False
        self._protocol_error: str | None = None
        self._closed_reason: str | None = None
        self._stdin_closed = False

        self._turn_finished = threading.Event()
        self._driver: threading.Thread | None = None

    # -- state a caller reads once the turn is over ------------------------

    @property
    def observed_model(self) -> str | None:
        """The model the app-server said the thread actually runs with."""
        with self._state_lock:
            return self._observed_model

    @property
    def final_message(self) -> str:
        """The turn's final assistant message, as the app-server reported it."""
        with self._state_lock:
            return self._final_message

    @property
    def turn_status(self) -> str | None:
        with self._state_lock:
            return self._turn_status

    @property
    def turn_accepted(self) -> bool:
        """Whether ``turn/start`` was accepted, which forecloses any fallback."""
        with self._state_lock:
            return self._turn_accepted

    @property
    def work_observed(self) -> bool:
        """Whether the app-server has emitted anything the turn itself did."""
        with self._state_lock:
            return self._work_observed

    @property
    def protocol_error(self) -> str | None:
        with self._state_lock:
            return self._protocol_error

    @property
    def fallback_reason(self) -> str | None:
        """Why the legacy transport may be run instead, or ``None``.

        Non-``None`` is a promise, not a suggestion: it is only ever set when
        the conversation failed *before* ``turn/start`` was accepted and
        before the app-server emitted any work. Replaying a turn after either
        of those would execute it twice.
        """
        with self._state_lock:
            if self._turn_accepted or self._work_observed:
                return None
            return self._protocol_error

    # -- intake: called on the harness stdout reader thread -----------------

    def feed(self, chunk: str) -> str:
        """Consume one stdout read and return what should be logged for it.

        Never raises: this runs on the reader thread, under the harness's
        side-effect lock, and an exception there would cost the turn its
        output. A message that cannot be understood is recorded as a protocol
        error and rendered as plain text instead.
        """
        rendered: list[str] = []
        for line in self._decoder.feed(chunk):
            rendered.append(self._consume_line(line))
        return "".join(rendered)

    def flush(self) -> str:
        """Render whatever a final partial read left in the buffer."""
        return "".join(self._consume_line(line) for line in self._decoder.flush())

    def _consume_line(self, line: str) -> str:
        stripped = line.strip()
        if not stripped:
            return ""
        try:
            message = json.loads(stripped)
        except ValueError:
            # Not every byte on an app-server's stdout is protocol: a wrapper
            # script or a misconfigured binary can print plain text there. It
            # is passed through rather than dropped, so a failure that only
            # says something in prose still reaches the log.
            return line + "\n"
        if not isinstance(message, dict):
            return line + "\n"
        try:
            return self._dispatch(message)
        except Exception as error:  # noqa: BLE001 - the reader must not die
            self._note_protocol_error(f"{type(error).__name__}: {error}")
            return ""

    def _dispatch(self, message: dict[str, Any]) -> str:
        if "method" in message:
            if "id" in message:
                # A server-to-client *request* (an approval prompt, say):
                # a method and an id together. Every turn runs with
                # `approvalPolicy: never`, so one arriving means the access
                # profile is not the one that was asked for.
                self._note_protocol_error(
                    f"unroutable app-server request: {message['method']!r}"
                )
                return ""
            return self._handle_notification(
                str(message["method"]),
                message.get("params") or {},
            )
        raw_id = message.get("id")
        if not isinstance(raw_id, int):
            self._note_protocol_error(f"unroutable app-server message: {raw_id!r}")
            return ""
        with self._state_lock:
            pending = self._pending.pop(raw_id, None)
        if pending is None:
            self._note_protocol_error(f"response to unknown request id {raw_id}")
            return ""
        if "error" in message:
            pending.resolve(error=_render_error(message.get("error")))
            return ""
        pending.resolve(result=message.get("result"))
        return ""

    def _handle_notification(self, method: str, params: Mapping[str, Any]) -> str:
        if method in _WORK_NOTIFICATIONS or method.startswith("item/"):
            with self._state_lock:
                self._work_observed = True
        if method == "turn/started":
            return ""
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            return delta if isinstance(delta, str) else ""
        if method == "item/completed":
            return self._render_completed_item(params.get("item"))
        if method == "error":
            error = params.get("error")
            message = ""
            if isinstance(error, Mapping):
                message = str(error.get("message", ""))
            return f"error: {message}\n" if message else ""
        if method == "turn/completed":
            return self._handle_turn_completed(params)
        return ""

    def _render_completed_item(self, item: object) -> str:
        if not isinstance(item, Mapping):
            return ""
        item_type = item.get("type")
        if item_type == "userMessage":
            # Deliberately unrendered. The app-server echoes the prompt and
            # every steered guidance body back as a user message, and this is
            # the one place they could reach a persisted artifact.
            return ""
        if item_type == "agentMessage":
            text = item.get("text")
            if isinstance(text, str):
                self._note_agent_message(text, phase=item.get("phase"))
            return "\n"
        if item_type == "commandExecution":
            command = item.get("command")
            if isinstance(command, list):
                command = " ".join(str(token) for token in command)
            exit_code = item.get("exitCode")
            suffix = "" if exit_code is None else f" (exit {exit_code})"
            return f"$ {command}{suffix}\n"
        return ""

    def _handle_turn_completed(self, params: Mapping[str, Any]) -> str:
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            self._note_protocol_error("turn/completed carried no turn")
            return ""
        turn_id = turn.get("id")
        with self._state_lock:
            expected = self._turn_id
        if expected is not None and turn_id != expected:
            # Another turn on the same thread finished. This session owns one
            # turn and must not treat a stranger's completion as its own.
            return ""
        status = str(turn.get("status", TURN_STATUS_COMPLETED))
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping) and item.get("type") == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str):
                        self._note_agent_message(text, phase=item.get("phase"))
        error = turn.get("error")
        detail = ""
        if isinstance(error, Mapping):
            detail = str(error.get("message", ""))
        with self._state_lock:
            self._turn_status = status
            if status == TURN_STATUS_FAILED and self._protocol_error is None:
                self._protocol_error = detail or "turn failed"
        self._turn_finished.set()
        line = f"turn {status}"
        if detail:
            line = f"{line}: {detail}"
        return line + "\n"

    def _note_agent_message(self, text: str, *, phase: object) -> None:
        with self._state_lock:
            if phase == "final_answer":
                self._final_message = text
                self._final_answer_seen = True
            elif not self._final_answer_seen:
                self._final_message = text

    def _note_protocol_error(self, detail: str) -> None:
        with self._state_lock:
            if self._protocol_error is None:
                self._protocol_error = detail

    # -- driving: the handshake and the orderly shutdown --------------------

    def start(self) -> None:
        """Begin the conversation on a background thread.

        Backgrounded because the answers arrive on the harness reader thread,
        which the caller has not started yet: the handshake has to be able to
        block without blocking the pipe it is waiting on.
        """
        driver = threading.Thread(
            target=self._drive,
            name="codex-app-server",
            daemon=True,
        )
        with self._state_lock:
            self._driver = driver
        driver.start()

    def _drive(self) -> None:
        try:
            self._handshake()
        except AppServerError as error:
            self._note_protocol_error(str(error))
            self._turn_finished.set()
            self._close_stdin()
            return
        self._turn_finished.wait()
        self._end_process()

    def _handshake(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": CLIENT_TITLE,
                    "version": __version__,
                }
            },
        )
        self._notify("initialized", None)
        thread = self._request("thread/start", dict(self._plan.thread_params))
        self._adopt_thread(thread)
        turn_params = dict(self._plan.turn_params)
        turn_params["threadId"] = self._require_thread_id()
        turn_params["input"] = [{"type": "text", "text": self._prompt}]
        turn = self._request("turn/start", turn_params)
        self._adopt_turn(turn)

    def _adopt_thread(self, response: Any) -> None:
        if not isinstance(response, Mapping):
            raise AppServerError("thread/start returned no result")
        thread = response.get("thread")
        if not isinstance(thread, Mapping) or not isinstance(thread.get("id"), str):
            raise AppServerError("thread/start returned no thread id")
        sandbox = response.get("sandbox")
        actual = sandbox.get("type") if isinstance(sandbox, Mapping) else None
        if actual != self._plan.expected_sandbox:
            # The access profile is a promise made to the operator, and the
            # only honest place to check it is the app-server's own echo of
            # what it applied. Failing here is before `turn/start`, so the
            # legacy transport -- which states the profile in argv -- can
            # still run this turn.
            raise AppServerError(
                "thread/start applied sandbox "
                f"{actual!r}, expected {self._plan.expected_sandbox!r}"
            )
        model = response.get("model")
        with self._state_lock:
            self._thread_id = str(thread["id"])
            if isinstance(model, str) and model:
                self._observed_model = model

    def _adopt_turn(self, response: Any) -> None:
        turn = response.get("turn") if isinstance(response, Mapping) else None
        if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
            raise AppServerError("turn/start returned no turn id")
        with self._state_lock:
            self._turn_id = str(turn["id"])
            self._turn_accepted = True
            self._work_observed = True

    def _require_thread_id(self) -> str:
        with self._state_lock:
            thread_id = self._thread_id
        if thread_id is None:
            raise AppServerError("no thread has been started")
        return thread_id

    # -- steering ----------------------------------------------------------

    def steer(
        self, request: guidance_delivery.GuidanceRequest
    ) -> guidance_delivery.GuidanceOutcome:
        """Hand ``request`` to the running turn and report what happened.

        Every steer states the turn it believes is active. The app-server
        rejects the call outright when that belief is wrong, which is what
        makes a ``delivered-live`` receipt a claim about *this* turn rather
        than about whichever turn happened to be running.
        """
        with self._state_lock:
            thread_id = self._thread_id
            turn_id = self._turn_id
            finished = self._turn_status is not None
        if thread_id is None or turn_id is None:
            return guidance_delivery.deferred(guidance_delivery.REASON_NO_ACTIVE_TURN)
        if finished or self._turn_finished.is_set():
            return guidance_delivery.deferred(guidance_delivery.REASON_STALE_TURN)
        try:
            self._request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": request.text}],
                },
                timeout=self._steer_timeout,
            )
        except AppServerError as error:
            return guidance_delivery.deferred(
                _steer_deferral_reason(str(error)),
                detail=_redacted_steer_detail(str(error), turn_id=turn_id),
            )
        return guidance_delivery.DELIVERED

    # -- transport primitives ----------------------------------------------

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout: float | None = None,
    ) -> Any:
        pending = _Pending()
        with self._state_lock:
            if self._closed_reason is not None:
                raise AppServerError(self._closed_reason)
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = pending
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = dict(params)
        try:
            self._write(payload)
        except AppServerError:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise
        deadline = time.monotonic() + (
            self._request_timeout if timeout is None else timeout
        )
        while not pending.event.wait(_LIVENESS_POLL_SECONDS):
            if self._process.poll() is not None:
                with self._state_lock:
                    self._pending.pop(request_id, None)
                raise AppServerError(f"app-server exited before answering {method}")
            if time.monotonic() >= deadline:
                with self._state_lock:
                    self._pending.pop(request_id, None)
                raise AppServerError(f"{method} timed out")
        if pending.error is not None:
            raise AppServerError(f"{method} failed: {pending.error}")
        return pending.result

    def _notify(self, method: str, params: Mapping[str, Any] | None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._write(payload)

    def _write(self, payload: Mapping[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise AppServerError("app-server was spawned without a stdin pipe")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            with self._state_lock:
                if self._stdin_closed:
                    raise AppServerError("app-server stdin is closed")
            try:
                stdin.write(line)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                self._fail_pending(f"app-server stdin failed: {error}")
                raise AppServerError(f"app-server stdin failed: {error}") from error

    def _fail_pending(self, reason: str) -> None:
        with self._state_lock:
            self._closed_reason = reason
            pending = list(self._pending.values())
            self._pending.clear()
        for entry in pending:
            entry.resolve(error=reason)

    def _close_stdin(self) -> None:
        with self._write_lock:
            with self._state_lock:
                if self._stdin_closed:
                    return
                self._stdin_closed = True
            stdin = self._process.stdin
            if stdin is None:
                return
            # An already-gone app-server is the state closing stdin was trying
            # to reach, so every failure mode here is a success.
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                stdin.close()

    def _end_process(self) -> None:
        """Close stdin and give the app-server a bounded chance to exit.

        Closing stdin is how the app-server is asked to shut down; it exits on
        its own in milliseconds. Terminating after the grace period keeps a
        wedged one from holding a turn open forever when the turn itself has
        no timeout.
        """
        self._close_stdin()
        deadline = time.monotonic() + self._shutdown_grace
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                return
            time.sleep(_LIVENESS_POLL_SECONDS)
        if self._process.poll() is None:
            self._process.terminate()

    def shutdown(self) -> None:
        """Release the conversation once the harness process is gone.

        Runs on the turn's own thread from the harness ``finally``, so it must
        not wait on anything unbounded: the driver is joined with a bound, and
        a driver that outlives it is a daemon thread whose only remaining act
        would be closing an already-closed pipe.
        """
        self._turn_finished.set()
        self._fail_pending("app-server session is closed")
        self._close_stdin()
        with self._state_lock:
            driver = self._driver
        if driver is not None:
            driver.join(timeout=self._shutdown_grace)


#: Notifications that prove the turn itself has begun doing something. Any
#: ``item/*`` notification counts too; these are the ones that do not match
#: that prefix.
_WORK_NOTIFICATIONS = frozenset(
    {
        "turn/started",
        "turn/completed",
        "turn/diff/updated",
        "turn/plan/updated",
        "command/exec/outputDelta",
        "process/outputDelta",
        "process/exited",
        "error",
    }
)


def _render_error(error: object) -> str:
    if isinstance(error, Mapping):
        code = error.get("code")
        message = str(error.get("message", ""))
        data = error.get("data")
        rendered = f"[{code}] {message}" if code is not None else message
        if data is not None:
            rendered = f"{rendered} {json.dumps(data, separators=(',', ':'))}"
        return rendered
    return str(error)


def _steer_deferral_reason(detail: str) -> str:
    """Map one steer failure onto the ledger's fixed deferral vocabulary.

    The three outcomes are kept distinct because they mean different things to
    a supervisor: the turn moved on, the turn is one that cannot be steered at
    all, or the channel itself failed.
    """
    if ACTIVE_TURN_NOT_STEERABLE in detail:
        return guidance_delivery.REASON_TRANSPORT_UNAVAILABLE
    if "expected active turn id" in detail:
        return guidance_delivery.REASON_STALE_TURN
    return guidance_delivery.REASON_TRANSPORT_UNAVAILABLE


def _redacted_steer_detail(detail: str, *, turn_id: str) -> str:
    """Return a diagnosis string that cannot carry a guidance body.

    ``detail`` is never persisted, but it is the one value on this path that
    is derived from a failed request, and that request carried the guidance
    text. Only the app-server's own vocabulary survives.
    """
    if ACTIVE_TURN_NOT_STEERABLE in detail:
        return ACTIVE_TURN_NOT_STEERABLE
    if "expected active turn id" in detail:
        return f"expected turn {turn_id} is no longer active"
    if "timed out" in detail:
        return "turn/steer timed out"
    return "turn/steer failed"
