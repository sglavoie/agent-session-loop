"""A bounded Agent Client Protocol client for the Copilot worker transport.

``copilot -p <prompt>`` is a one-shot process: the prompt goes out on argv, the
answer comes back as prose on stdout, and the only thing the turn's outcome can
be read from afterwards is an exit code. ``copilot --acp`` speaks
newline-delimited JSON-RPC 2.0 over the same two pipes instead, and that
conversation says out loud what the one-shot form could only be inferred from:
which session the turn ran in, which model and reasoning effort the server
actually applied, every tool call as it happens, and a terminal stop reason.
Turning those into an honest :class:`~.harnesses.HarnessResult` is why this
module exists.

The protocol surface used here, all of it verified against ``copilot`` 1.0.82
except where noted:

- ``initialize``, which negotiates the protocol version and states what *this*
  client can do. It advertises no filesystem and no terminal capability, so the
  agent has no way to ask ASL to read, write, or run anything on its behalf --
  the turn's access stays exactly what the launch argv granted it.
- ``session/new``, whose response carries the session identifier and echoes the
  effective ``model`` and ``reasoning_effort`` config options. Those are the
  launch flags coming back, which is the only place their parity can be
  checked rather than assumed.
- ``session/prompt``, which submits the turn's work and answers once with a
  terminal ``stopReason``.
- ``session/update`` notifications, streamed until the prompt answers.
- ``session/request_permission``, a server-to-client *request*: see
  :class:`PermissionPolicy` for why it is answered explicitly and why it can
  never grant more than the launch argv already did.

Two calls are deliberately absent. ``session/cancel`` and ``session/load`` are
what turn a copilot turn into an interruptible, resumable conversation, and
neither is wired here: an adapter that could not be interrupted before is not
made to look interruptible by this transport.

Threading. Two threads touch a session and each has one job:

- the harness's stdout reader calls :meth:`feed`, which renders updates and
  answers server-to-client requests inline;
- a driver thread owned by this module runs the handshake, the submission, and
  the orderly shutdown.

``_state_lock`` guards the mutable state and the pending-request table and is
never held across a blocking wait. ``_write_lock`` serializes stdin writes,
which is the only resource the driver and the reader share.

Privacy. The prompt goes *out* over this transport and must not come back in
through the log. Copilot echoes it twice -- as a ``user_message_chunk`` and as
the ``title`` of a ``session_info_update`` -- and :meth:`feed` renders neither.
"""

from __future__ import annotations

import contextlib
import json
import math
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import __version__

#: The transport discriminator persisted alongside ``argv``.
TRANSPORT_ACP = "acp"

#: The ACP major version this client speaks. ``initialize`` sends it and the
#: agent answers with the version it will actually use; anything else is a
#: conversation this client cannot hold.
PROTOCOL_VERSION = 1

#: Client identity sent with ``initialize``.
CLIENT_NAME = "agent-session-loop"
CLIENT_TITLE = "ASL"

#: How long a handshake request may take before the turn is declared unusable.
#: Generous, because ``session/new`` boots the agent's configured MCP servers.
REQUEST_TIMEOUT_SECONDS = 120.0

#: How long the agent gets to exit on its own after its stdin is closed at end
#: of turn. It exits in milliseconds in practice; this only bounds what a
#: wedged one costs before the driver terminates it.
SHUTDOWN_GRACE_SECONDS = 10.0

#: How often a bounded wait re-checks whether the process is still alive. A
#: dead agent can never answer, so noticing quickly is what keeps a handshake
#: failure from costing the full request timeout.
_LIVENESS_POLL_SECONDS = 0.05

#: ``session/prompt`` is the turn, so it gets no deadline of its own: the
#: harness timeout is what bounds a turn, and a second bound here would kill
#: long work that was proceeding normally.
_NO_DEADLINE = math.inf

#: Terminal ``stopReason`` values ``session/prompt`` answers with. ``end_turn``
#: is the only one that means the agent finished what it was asked to do.
STOP_REASON_END_TURN = "end_turn"
STOP_REASON_CANCELLED = "cancelled"
STOP_REASON_REFUSAL = "refusal"
STOP_REASON_MAX_TOKENS = "max_tokens"
STOP_REASON_MAX_TURN_REQUESTS = "max_turn_requests"

#: Permission option kinds, as ``session/request_permission`` offers them. The
#: two ``_always`` kinds are named here only so the responder can refuse to
#: pick them.
PERMISSION_ALLOW_ONCE = "allow_once"
PERMISSION_ALLOW_ALWAYS = "allow_always"
PERMISSION_REJECT_ONCE = "reject_once"
PERMISSION_REJECT_ALWAYS = "reject_always"

#: Option kinds this client will never select, whatever the request. Both make
#: one answer stand for every later request, which is the blanket approval an
#: unattended turn must not be able to hand out -- and ``reject_always`` is
#: refused for the same reason in the other direction: a standing refusal is a
#: decision about calls nobody has seen yet.
_STANDING_KINDS = frozenset({PERMISSION_ALLOW_ALWAYS, PERMISSION_REJECT_ALWAYS})

#: The decisions the responder records, in the vocabulary the turn's log uses.
DECISION_ALLOWED = "allowed"
DECISION_REJECTED = "rejected"
DECISION_CANCELLED = "cancelled"

#: JSON-RPC's "no such method", answered to any client-side method this turn
#: did not advertise a capability for.
_METHOD_NOT_FOUND = -32601

#: What a session calls immediately before submitting work, as its last chance
#: to be told not to. Raising from it abandons the turn.
SubmissionGate = Callable[[], None]


class AcpError(Exception):
    """The ACP conversation cannot continue.

    Raised for a transport failure (a dead process, a closed pipe, a request
    that never answered) and for a protocol failure (an error response, an
    unparseable or contradictory message). One exception type, because every
    caller does the same thing with both: stop driving the turn and record why.
    """


class _SubmissionRefused(Exception):
    """The turn was cancelled before ``session/prompt``, so nothing was sent.

    Deliberately *not* an :class:`AcpError`. The agent is healthy and is simply
    being let go without work; recording a protocol error here would restate a
    cancellation as a harness that broke.
    """


@dataclass(frozen=True)
class PermissionPolicy:
    """What this turn may answer ``session/request_permission`` with.

    The responder exists to be explicit, not to be generous. It restates the
    grant the launch argv already made and can never exceed it:

    - an audit turn grants nothing, so every request is rejected;
    - a worker turn without ``--allow-all-tools --allow-all-paths`` grants
      nothing either -- ``--no-ask-user`` means there is no operator at this
      turn to approve anything, and approving on their behalf is precisely the
      blanket approval an unattended run must not invent;
    - a worker turn launched with those flags has already granted the tools,
      so ``allow_once`` restates that grant for one call rather than making a
      new one.

    Guard deny rules are deliberately not re-derived here. They are command
    *text* rules (``shell(git push)``) that the agent applies to its own tool
    calls before it would ever ask, so matching them again against a permission
    payload could only ever be a weaker guess at the same rule -- and a weaker
    guess that appeared to enforce something would be worse than not guessing.
    Copilot's guard lane remains its argv, exactly as ``guards.guard_coverage``
    reports it.
    """

    #: Whether the launch argv already granted the tools this turn may use.
    may_allow: bool
    #: The access profile's name, recorded with each decision so the turn's log
    #: says which rule answered rather than only what it answered.
    profile: str

    @classmethod
    def for_turn(cls, *, is_audit: bool, unsafe_bypass: bool) -> PermissionPolicy:
        if is_audit:
            return cls(may_allow=False, profile="audit-read-only")
        return cls(may_allow=unsafe_bypass, profile="worker")


@dataclass(frozen=True)
class PermissionDecision:
    """One answered permission request, as the turn recorded it."""

    decision: str
    reason: str
    tool: str
    option_id: str | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "tool": self.tool,
            "option_id": self.option_id,
        }


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


class CopilotAcpSession:
    """One ACP conversation, scoped to exactly one harness turn.

    Constructed around a spawned process, driven by :meth:`start`, fed by the
    harness reader, and torn down by :meth:`shutdown`. The turn it runs is the
    only turn it will ever run: one session is created, one prompt is sent, and
    the session closes its own stdin as soon as that prompt answers.
    """

    def __init__(
        self,
        *,
        process: subprocess.Popen,
        prompt: str,
        cwd: str,
        permissions: PermissionPolicy,
        expected_model: str | None = None,
        expected_effort: str | None = None,
        before_submit: SubmissionGate | None = None,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        shutdown_grace: float = SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self._process = process
        self._prompt = prompt
        self._cwd = cwd
        self._permissions = permissions
        self._expected_model = expected_model
        self._expected_effort = expected_effort
        self._before_submit = before_submit
        self._request_timeout = request_timeout
        self._shutdown_grace = shutdown_grace

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._decoder = _LineDecoder()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0

        self._session_id: str | None = None
        self._agent_info: str | None = None
        self._observed_model: str | None = None
        self._observed_effort: str | None = None
        self._option_mismatches: list[str] = []
        self._message_parts: list[str] = []
        self._stop_reason: str | None = None
        self._prompt_submitted = False
        self._protocol_error: str | None = None
        self._submission_refused: str | None = None
        self._permission_decisions: list[PermissionDecision] = []
        self._closed_reason: str | None = None
        self._stdin_closed = False

        self._turn_finished = threading.Event()
        self._driver: threading.Thread | None = None

    # -- state a caller reads once the turn is over ------------------------

    @property
    def session_id(self) -> str | None:
        """The ACP session this turn ran in, once one exists."""
        with self._state_lock:
            return self._session_id

    @property
    def agent_info(self) -> str | None:
        """The agent's own name and version, as ``initialize`` reported them."""
        with self._state_lock:
            return self._agent_info

    @property
    def observed_model(self) -> str | None:
        """The model the agent said the session actually runs with."""
        with self._state_lock:
            return self._observed_model

    @property
    def observed_effort(self) -> str | None:
        """The reasoning effort the agent said the session actually runs with."""
        with self._state_lock:
            return self._observed_effort

    @property
    def option_mismatches(self) -> tuple[str, ...]:
        """Launch controls the agent's own echo did not agree with.

        Reported, never enforced. Copilot canonicalizes these values, so a
        disagreement is as likely to be a spelling as a substitution -- and
        unlike a sandbox policy, neither model nor effort changes what the turn
        is *allowed* to do. The operator is told; the turn still runs.
        """
        with self._state_lock:
            return tuple(self._option_mismatches)

    @property
    def final_message(self) -> str:
        """The turn's assistant message, reassembled from its streamed chunks."""
        with self._state_lock:
            return "".join(self._message_parts)

    @property
    def stop_reason(self) -> str | None:
        """How ``session/prompt`` said the turn ended, once it has answered."""
        with self._state_lock:
            return self._stop_reason

    @property
    def prompt_submitted(self) -> bool:
        """Whether ``session/prompt`` was sent, which forecloses any replay."""
        with self._state_lock:
            return self._prompt_submitted

    @property
    def protocol_error(self) -> str | None:
        with self._state_lock:
            return self._protocol_error

    @property
    def cancelled_before_submission(self) -> str | None:
        """Why the turn was refused before ``session/prompt``, if it was."""
        with self._state_lock:
            return self._submission_refused

    @property
    def permission_decisions(self) -> tuple[PermissionDecision, ...]:
        """Every ``session/request_permission`` this turn answered, in order."""
        with self._state_lock:
            return tuple(self._permission_decisions)

    @property
    def turn_finished(self) -> bool:
        """Whether the turn has reached a terminal state of any kind."""
        return self._turn_finished.is_set()

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
            # Not every byte on the agent's stdout is protocol: a wrapper
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
            method = str(message["method"])
            if "id" in message:
                # A method and an id together is a server-to-client *request*,
                # which this client answers rather than logs.
                return self._handle_request(message["id"], method, _params(message))
            return self._handle_notification(method, _params(message))
        raw_id = message.get("id")
        if not isinstance(raw_id, int):
            self._note_protocol_error(f"unroutable acp message: {raw_id!r}")
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

    # -- server-to-client requests -----------------------------------------

    def _handle_request(
        self, request_id: object, method: str, params: Mapping[str, Any]
    ) -> str:
        if method == "session/request_permission":
            return self._answer_permission(request_id, params)
        # Everything else is a capability this client did not advertise:
        # ``fs/*`` and ``terminal/*`` ask ASL to read, write, or run something
        # on the agent's behalf, which is exactly the access the launch argv
        # decided. Refusing by method rather than by policy is what keeps the
        # audit profile's restricted path restricted no matter what is asked.
        self._answer(
            request_id,
            error={
                "code": _METHOD_NOT_FOUND,
                "message": f"agent-session-loop does not implement {method}",
            },
        )
        return f"acp: refused unadvertised request {method}\n"

    def _answer_permission(self, request_id: object, params: Mapping[str, Any]) -> str:
        tool = _tool_label(params.get("toolCall"))
        options = params.get("options")
        options = options if isinstance(options, Sequence) else ()
        decision, reason = self._decide_permission()
        wanted = (
            PERMISSION_ALLOW_ONCE
            if decision == DECISION_ALLOWED
            else PERMISSION_REJECT_ONCE
        )
        option_id = _select_option(options, wanted)
        if option_id is None:
            # The agent offered nothing this client is willing to pick -- in
            # practice, only the two standing kinds. ``cancelled`` is the
            # protocol's own way to answer without selecting anything, so the
            # request is declined rather than escalated into a standing grant.
            decision = DECISION_CANCELLED
            reason = f"{reason}; no {wanted} option offered"
            outcome: dict[str, Any] = {"outcome": "cancelled"}
        else:
            outcome = {"outcome": "selected", "optionId": option_id}
        record = PermissionDecision(
            decision=decision, reason=reason, tool=tool, option_id=option_id
        )
        with self._state_lock:
            self._permission_decisions.append(record)
        self._answer(request_id, result={"outcome": outcome})
        return f"permission {decision} ({reason}): {tool}\n"

    def _decide_permission(self) -> tuple[str, str]:
        policy = self._permissions
        if policy.may_allow:
            return DECISION_ALLOWED, f"{policy.profile} launched with tools granted"
        return DECISION_REJECTED, f"{policy.profile} granted no tools on launch"

    def _answer(
        self,
        request_id: object,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = dict(error)
        else:
            payload["result"] = dict(result or {})
        try:
            self._write(payload)
        except AcpError as failure:
            # The reader thread cannot raise, and an agent whose stdin is gone
            # is one that will never see this answer anyway.
            self._note_protocol_error(str(failure))

    # -- notifications ------------------------------------------------------

    def _handle_notification(self, method: str, params: Mapping[str, Any]) -> str:
        if method != "session/update":
            return ""
        update = params.get("update")
        if not isinstance(update, Mapping):
            self._note_protocol_error("session/update carried no update")
            return ""
        session_id = params.get("sessionId")
        with self._state_lock:
            expected = self._session_id
        if expected is not None and session_id is not None and session_id != expected:
            # Another session on the same agent said something. This session
            # owns one turn and must not log a stranger's work as its own.
            return ""
        return self._render_update(update)

    def _render_update(self, update: Mapping[str, Any]) -> str:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                with self._state_lock:
                    self._message_parts.append(text)
            return text
        if kind == "tool_call":
            title = _text_or_empty(update.get("title"))
            return f"$ {title}\n" if title else ""
        if kind == "tool_call_update":
            # Only failure is rendered. A completed tool call already said what
            # it was doing when it started, and restating every one of them
            # would bury the turn's own output in bookkeeping.
            if update.get("status") == "failed":
                title = _text_or_empty(update.get("title"))
                return f"tool call failed: {title}\n" if title else "tool call failed\n"
            return ""
        if kind == "config_option_update":
            self._note_config_option(update.get("id"), update.get("currentValue"))
            return ""
        # Everything else is deliberately unrendered. ``user_message_chunk``
        # echoes the prompt back and ``session_info_update`` carries it as the
        # session title -- this is the one place either could reach a persisted
        # artifact. ``agent_thought_chunk``, ``plan``,
        # ``available_commands_update``, ``current_mode_update`` and
        # ``usage_update`` are noise rather than the turn's output.
        return ""

    def _note_config_option(self, option_id: object, value: object) -> None:
        if not isinstance(value, str) or not value:
            return
        with self._state_lock:
            if option_id == "model":
                self._observed_model = value
            elif option_id == "reasoning_effort":
                self._observed_effort = value

    def _note_protocol_error(self, detail: str) -> None:
        with self._state_lock:
            if self._protocol_error is None:
                self._protocol_error = detail

    # -- driving: the handshake, the submission, the orderly shutdown -------

    def start(self) -> None:
        """Begin the conversation on a background thread.

        Backgrounded because the answers arrive on the harness reader thread,
        which the caller has not started yet: the handshake has to be able to
        block without blocking the pipe it is waiting on.
        """
        driver = threading.Thread(target=self._drive, name="copilot-acp", daemon=True)
        with self._state_lock:
            self._driver = driver
        driver.start()

    def _drive(self) -> None:
        try:
            self._handshake()
            self._submit()
        except _SubmissionRefused as error:
            self._abandon_before_submission(str(error))
            return
        except AcpError as error:
            self._note_protocol_error(str(error))
            self._turn_finished.set()
            self._close_stdin()
            return
        self._turn_finished.set()
        self._end_process()

    def _handshake(self) -> None:
        response = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    # Stated as refusals, not omissions. An agent that reads the
                    # negotiated capabilities knows before it asks that this
                    # client will not read files, write files, or run terminals
                    # for it, so the turn's only access is the one its launch
                    # argv granted -- which is what keeps the audit profile's
                    # restricted tool set restricted.
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": CLIENT_TITLE,
                    "version": __version__,
                },
            },
        )
        self._adopt_initialize(response)
        self._adopt_session(self._request("session/new", self._session_params()))

    def _session_params(self) -> dict[str, Any]:
        # Model, effort, tool filters and repository grants are all launch
        # flags on this agent rather than session parameters, so there is
        # nothing to restate here: ``cwd`` names where the session runs and
        # ``mcpServers`` is the empty list that adds none.
        return {"cwd": self._cwd, "mcpServers": []}

    def _adopt_initialize(self, response: Any) -> None:
        if not isinstance(response, Mapping):
            raise AcpError("initialize returned no result")
        version = response.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            # The agent answers with the version it will actually use. One this
            # client does not speak has to fail here, before a session exists:
            # guessing at an unknown dialect is how a turn ends up believing it
            # granted or refused something it did not.
            raise AcpError(
                f"agent negotiated acp protocol version {version!r}, "
                f"expected {PROTOCOL_VERSION}"
            )
        info = response.get("agentInfo")
        if isinstance(info, Mapping):
            name = _text_or_empty(info.get("name"))
            agent_version = _text_or_empty(info.get("version"))
            label = " ".join(part for part in (name, agent_version) if part)
            if label:
                with self._state_lock:
                    self._agent_info = label

    def _adopt_session(self, response: Any) -> None:
        if not isinstance(response, Mapping):
            raise AcpError("session/new returned no result")
        session_id = response.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError("session/new returned no session id")
        with self._state_lock:
            self._session_id = session_id
        self._adopt_config_options(response.get("configOptions"))

    def _adopt_config_options(self, options: object) -> None:
        """Read the launch controls back out of the agent's own echo.

        This is the only place the ``--model`` and ``--effort`` flags can be
        confirmed: they configure the server at launch, so nothing else in the
        conversation would ever mention them.
        """
        if not isinstance(options, Sequence):
            return
        for option in options:
            if not isinstance(option, Mapping):
                continue
            self._note_config_option(option.get("id"), option.get("currentValue"))
        with self._state_lock:
            mismatches = [
                f"{label} is {actual!r}, expected {expected!r}"
                for label, expected, actual in (
                    ("model", self._expected_model, self._observed_model),
                    ("effort", self._expected_effort, self._observed_effort),
                )
                if expected is not None and actual is not None and actual != expected
            ]
            self._option_mismatches.extend(mismatches)

    def prompt_params(self, session_id: str) -> dict[str, Any]:
        """The ``session/prompt`` payload carrying this turn's prompt.

        Named rather than inlined because this is the whole channel the prompt
        travels on: argv no longer carries it, so a caller checking that the
        prompt arrives verbatim has nowhere else to look.
        """
        return {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": self._prompt}],
        }

    def _submit(self) -> None:
        # The last checkpoint. Everything above is recoverable; the request
        # below is the one that spends the turn.
        self._gate_submission()
        session_id = self._require_session_id()
        with self._state_lock:
            self._prompt_submitted = True
        response = self._request(
            "session/prompt",
            self.prompt_params(session_id),
            timeout=_NO_DEADLINE,
        )
        self._adopt_stop_reason(response)

    def _adopt_stop_reason(self, response: Any) -> None:
        stop_reason = (
            response.get("stopReason") if isinstance(response, Mapping) else None
        )
        if not isinstance(stop_reason, str) or not stop_reason:
            raise AcpError("session/prompt returned no stop reason")
        with self._state_lock:
            self._stop_reason = stop_reason

    def _gate_submission(self) -> None:
        """Ask the caller whether this turn may still be submitted."""
        gate = self._before_submit
        if gate is None:
            return
        try:
            gate()
        except Exception as error:  # noqa: BLE001 - any failure is a refusal
            raise _SubmissionRefused(str(error) or type(error).__name__) from error

    def _abandon_before_submission(self, reason: str) -> None:
        """End a healthy conversation that was told not to submit its turn.

        No protocol error is recorded, because none happened: the agent is
        working and is simply being let go without a prompt.
        """
        with self._state_lock:
            self._submission_refused = reason
        self._turn_finished.set()
        self._close_stdin()

    def _require_session_id(self) -> str:
        with self._state_lock:
            session_id = self._session_id
        if session_id is None:
            raise AcpError("no acp session has been created")
        return session_id

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
                raise AcpError(self._closed_reason)
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
        except AcpError:
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
                raise AcpError(f"copilot exited before answering {method}")
            if time.monotonic() >= deadline:
                with self._state_lock:
                    self._pending.pop(request_id, None)
                raise AcpError(f"{method} timed out")
        if pending.error is not None:
            raise AcpError(f"{method} failed: {pending.error}")
        return pending.result

    def _write(self, payload: Mapping[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise AcpError("copilot was spawned without a stdin pipe")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            with self._state_lock:
                if self._stdin_closed:
                    raise AcpError("copilot stdin is closed")
            try:
                stdin.write(line)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                self._fail_pending(f"copilot stdin failed: {error}")
                raise AcpError(f"copilot stdin failed: {error}") from error

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
            # An already-gone agent is the state closing stdin was trying to
            # reach, so every failure mode here is a success.
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                stdin.close()

    def _end_process(self) -> None:
        """Close stdin and give the agent a bounded chance to exit.

        Closing stdin is how an ACP agent is asked to shut down; it exits on
        its own in milliseconds. Terminating after the grace period keeps a
        wedged one from holding a turn open forever when the turn itself has no
        timeout.
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
        not wait on anything unbounded: the driver is joined with a bound, and a
        driver that outlives it is a daemon thread whose only remaining act
        would be closing an already-closed pipe.
        """
        self._turn_finished.set()
        self._fail_pending("copilot acp session is closed")
        self._close_stdin()
        with self._state_lock:
            driver = self._driver
        if driver is not None:
            driver.join(timeout=self._shutdown_grace)


def _params(message: Mapping[str, Any]) -> Mapping[str, Any]:
    params = message.get("params")
    return params if isinstance(params, Mapping) else {}


def _select_option(options: Sequence[Any], wanted: str) -> str | None:
    """Return the id of the first option of ``wanted`` kind, or ``None``.

    Kinds this client refuses outright are skipped before anything else, so an
    agent cannot get a standing grant selected by offering it under a name that
    happens to sort first.
    """
    for option in options:
        if not isinstance(option, Mapping):
            continue
        kind = option.get("kind")
        if kind in _STANDING_KINDS or kind != wanted:
            continue
        option_id = option.get("optionId")
        if isinstance(option_id, str) and option_id:
            return option_id
    return None


def _tool_label(tool_call: object) -> str:
    """A short, agent-authored description of what permission was asked for."""
    if not isinstance(tool_call, Mapping):
        return "unnamed tool call"
    for key in ("title", "toolName", "name", "kind", "toolCallId"):
        label = _text_or_empty(tool_call.get(key))
        if label:
            return label
    return "unnamed tool call"


def _content_text(content: object) -> str:
    """The text of one ACP content block, or ``""`` for any other kind."""
    if not isinstance(content, Mapping):
        return ""
    if content.get("type") != "text":
        return ""
    return _text_or_empty(content.get("text"))


def _text_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


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
