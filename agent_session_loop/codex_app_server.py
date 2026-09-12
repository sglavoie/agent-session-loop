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
- ``thread/resume``, which restores one named thread and answers in the same
  shape. Its echo is checked harder than ``thread/start``'s, because a
  restored thread carries settings from the thread it restores rather than
  from the call that asked for it.
- ``turn/start``, whose response carries the accepted ``turnId``. Acceptance
  is the point of no return for the fallback in :mod:`.harnesses`.
- Streamed notifications (``turn/started``, ``item/*``, ``error``) until the
  terminal ``turn/completed``.
- ``turn/steer`` with a required ``expectedTurnId`` precondition.
- ``turn/interrupt``, which names the thread and the turn it stops. It is how
  a cancelled turn is recorded as *interrupted* by the side that owns the
  conversation, rather than as a turn that stopped answering.

Continuity. The thread identifier is learned from ``thread/start`` or
``thread/resume`` and handed to ``on_conversation`` *between* that call and
``turn/start``, which is the only window in which it can be made durable before
any work exists to be lost. A sink that raises, or a ``before_submit`` gate that
refuses, ends the conversation with nothing submitted -- and neither is a
protocol failure, so neither licenses the fallback in :mod:`.harnesses`.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import __version__, guidance_delivery, restore_diagnosis

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

#: How long ``turn/interrupt`` may take. Bounded well under the shutdown grace
#: because every caller has already decided the turn is over: the interrupt is
#: how the conversation is *told* so, and a forced termination stands behind it
#: either way.
INTERRUPT_TIMEOUT_SECONDS = 5.0

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

#: Requested thread settings re-checked against a ``thread/resume`` echo. Only
#: the ones whose disagreement changes what the turn is allowed to do: a
#: restored thread that runs somewhere else, or under a different approval
#: policy, is not the turn that was asked for. ``model`` is deliberately absent
#: -- the server canonicalizes it, so it is reported through ``observed_model``
#: rather than enforced.
_RESTORED_SETTINGS = ("cwd", "approvalPolicy")

#: What a session calls with the native thread identifier it just learned,
#: before it submits anything. Raising from it abandons the turn.
ConversationSink = Callable[[str], None]

#: What a session calls immediately before submitting work, as its last chance
#: to be told not to. Raising from it abandons the turn.
SubmissionGate = Callable[[], None]

#: What a session calls once the app-server has accepted ``turn/start``, which
#: is the first moment the prompt is demonstrably the worker's rather than this
#: process's. Unlike the two above it cannot refuse anything -- the turn is
#: already running -- so a failure is swallowed by the caller's own seam.
SubmissionSink = Callable[[], None]

#: What a session calls in the instant before ``turn/start`` goes out, which is
#: the last moment this process can still say the prompt has not left it. The
#: acceptance that follows may never arrive, so this is what keeps the silence
#: after it from reading as a turn that submitted nothing.
SubmissionInFlightSink = Callable[[], None]


class AppServerError(Exception):
    """The app-server conversation cannot continue.

    Raised for a transport failure (a dead process, a closed pipe, a request
    that never answered) and for a protocol failure (an error response, an
    unparseable or contradictory message). One exception type, because every
    caller does the same thing with both: stop driving the turn and record why.
    """


class _RequestRejected(AppServerError):
    """The app-server answered a request with an error response.

    Distinct from the transport failures that share :class:`AppServerError` --
    a dead process, a closed pipe, a request that never answered -- because only
    this one is the app-server *stating* something. What it states can outlive
    the turn; a broken pipe says nothing about the conversation behind it.

    It carries the error structured rather than only rendered, because the one
    caller that acts durably on a rejection has to weigh the code against the
    message instead of pattern-matching the sentence the two were folded into.
    """

    def __init__(
        self, message: str, *, rejection: restore_diagnosis.ProviderRejection
    ) -> None:
        super().__init__(message)
        self.rejection = rejection


class RestoreRefused(AppServerError):
    """The app-server would not restore the thread this turn was told to resume.

    A subclass because every ``AppServerError`` handler should keep treating it
    as one: nothing ran, the turn is over, and the fallback stays shut. What the
    separate type adds is durability -- this failure is a statement about the
    provider's own history, not about this turn, so retrying the same resume
    later cannot succeed either.

    Deliberately narrow, and narrow twice over. Only the ``thread/resume``
    request itself answering with an error can raise it, and only when that
    error is a supported missing-history diagnosis under
    :mod:`~agent_session_loop.restore_diagnosis` -- so the internal-error code a
    server wraps every unhandled failure in, a temporary outage, an expired
    credential, and a refused workspace all stay ordinary failures that leave
    the saved conversation exactly where it was. A thread that came back under a
    different id, a widened sandbox, and drifted settings are excluded for the
    same reason: they are thread-level disagreements about a conversation the
    provider still holds, and discarding that history because this turn could
    not use it would throw away recoverable work.
    """


class _ConversationNotRecorded(Exception):
    """The thread identifier could not be made durable, so nothing was sent.

    Deliberately *not* an :class:`AppServerError`. The app-server did nothing
    wrong here, and re-running the turn on the legacy transport would spend the
    very work this refusal exists to withhold -- so this must never become a
    ``protocol_error`` and must never open the fallback window.
    """


class _SubmissionRefused(Exception):
    """The turn was cancelled before ``turn/start``, so nothing was sent.

    Not an :class:`AppServerError` either, and for the same reason: the
    conversation is healthy, and re-running the turn would defeat the
    cancellation that stopped it.
    """


@dataclass
class _Pending:
    """One in-flight request, waiting for the reader thread to answer it.

    A request can stop waiting for two unrelated reasons, and they are kept in
    two fields rather than one string: the app-server *answered* with an error,
    or the transport failed underneath it. Only the first is the app-server
    stating something, and folding both into ``error`` is what let a broken pipe
    during ``thread/resume`` read as a provider disowning its own history.
    """

    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str | None = None
    rejection: restore_diagnosis.ProviderRejection | None = None

    def resolve(
        self,
        *,
        result: Any = None,
        error: str | None = None,
        rejection: restore_diagnosis.ProviderRejection | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.rejection = rejection
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
    only turn it will ever run: the thread is started or resumed, one turn is
    sent, and the session closes its own stdin as soon as that turn completes.

    ``resume_conversation_id`` names the thread to restore instead of starting
    a fresh one. It is a requirement, not a preference: a resume that cannot be
    honoured fails the turn, because the caller asking for it has state in that
    conversation and a fresh thread would silently discard it.
    """

    def __init__(
        self,
        *,
        process: subprocess.Popen,
        plan: TurnPlan,
        prompt: str,
        resume_conversation_id: str | None = None,
        on_conversation: ConversationSink | None = None,
        before_submit: SubmissionGate | None = None,
        on_submitted: SubmissionSink | None = None,
        on_submission_in_flight: SubmissionInFlightSink | None = None,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        steer_timeout: float = STEER_TIMEOUT_SECONDS,
        shutdown_grace: float = SHUTDOWN_GRACE_SECONDS,
        interrupt_timeout: float = INTERRUPT_TIMEOUT_SECONDS,
    ) -> None:
        self._process = process
        self._plan = plan
        self._prompt = prompt
        self._resume_conversation_id = resume_conversation_id
        self._on_conversation = on_conversation
        self._before_submit = before_submit
        self._on_submitted = on_submitted
        self._on_submission_in_flight = on_submission_in_flight
        self._request_timeout = request_timeout
        self._steer_timeout = steer_timeout
        self._shutdown_grace = shutdown_grace
        self._interrupt_timeout = interrupt_timeout

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
        self._turn_dispatched = False
        self._protocol_error: str | None = None
        self._restore_refused: str | None = None
        self._conversation_error: str | None = None
        self._submission_refused: str | None = None
        self._interrupt_requested = False
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
    def thread_id(self) -> str | None:
        """The native conversation this turn runs on, once one is known."""
        with self._state_lock:
            return self._thread_id

    @property
    def turn_status(self) -> str | None:
        with self._state_lock:
            return self._turn_status

    @property
    def turn_interrupted(self) -> bool:
        """Whether the turn ended because it was interrupted rather than failed.

        The app-server reports the two as different terminal statuses, and this
        keeps them different: an interrupted turn did the work it did and was
        stopped, while a failed turn is one the harness could not carry out.
        """
        with self._state_lock:
            return self._turn_status == TURN_STATUS_INTERRUPTED

    @property
    def interrupt_requested(self) -> bool:
        """Whether ``turn/interrupt`` was sent for this turn."""
        with self._state_lock:
            return self._interrupt_requested

    @property
    def conversation_error(self) -> str | None:
        """Why the conversation could not be recorded, if that is why nothing ran."""
        with self._state_lock:
            return self._conversation_error

    @property
    def cancelled_before_submission(self) -> str | None:
        """Why the turn was refused before ``turn/start``, if it was."""
        with self._state_lock:
            return self._submission_refused

    @property
    def turn_accepted(self) -> bool:
        """Whether ``turn/start`` was accepted, which forecloses any fallback."""
        with self._state_lock:
            return self._turn_accepted

    @property
    def turn_dispatched(self) -> bool:
        """Whether ``turn/start`` was put on the wire, answered or not.

        The weaker half of :attr:`turn_accepted`, and the one that matters when
        the answer never comes. Acceptance says the app-server took the turn;
        this says only that the prompt left here -- which is enough to stop
        anything from re-running it or from reporting that it was never sent.
        """
        with self._state_lock:
            return self._turn_dispatched

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
    def restore_refused(self) -> str | None:
        """Why the app-server would not restore the thread that was asked for.

        Set only by :class:`RestoreRefused`, so it means the provider no longer
        offers this history -- not that this turn happened to fail while
        resuming. Also reported as ``protocol_error``, because for everything
        that only needs to know the turn failed, it is one.
        """
        with self._state_lock:
            return self._restore_refused

    @property
    def fallback_reason(self) -> str | None:
        """Why the legacy transport may be run instead, or ``None``.

        Non-``None`` is a promise, not a suggestion: it is only ever set when
        the conversation failed *before* ``turn/start`` went out, before it was
        accepted, and before the app-server emitted any work. Replaying a turn
        after any of those would execute it twice.

        Dispatch is the first of the three and the one that holds when the
        others cannot be read. An app-server that took the prompt and then died
        without answering leaves no acceptance and no emitted work, so a rule
        written from those two alone would offer a fallback for a turn the
        provider may well be running.

        A resumption request forecloses it outright. The legacy transport
        starts a fresh conversation, so falling back would answer "continue
        thread X" with a turn that continues nothing -- the silent fresh start
        this transport exists to prevent. The same holds for the two refusals
        that stop a healthy conversation on purpose: neither is a protocol
        error, and re-running either one would defeat what it refused.
        """
        with self._state_lock:
            if self._turn_dispatched or self._turn_accepted or self._work_observed:
                return None
            if self._resume_conversation_id is not None:
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
            pending.resolve(
                rejection=restore_diagnosis.ProviderRejection.from_payload(
                    message.get("error")
                )
            )
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
            # Only `failed` is an error. An interrupted turn ran the work it
            # ran and was stopped on purpose, and recording that as a protocol
            # error would restate a cancellation as a harness that broke.
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

    def _note_restore_refused(self, detail: str) -> None:
        with self._state_lock:
            if self._restore_refused is None:
                self._restore_refused = detail
        self._note_protocol_error(detail)

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
        except _ConversationNotRecorded as error:
            self._abandon_before_submission(conversation_error=str(error))
            return
        except _SubmissionRefused as error:
            self._abandon_before_submission(submission_refused=str(error))
            return
        except RestoreRefused as error:
            self._note_restore_refused(str(error))
            self._turn_finished.set()
            self._close_stdin()
            return
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
        self._open_thread()
        # Between the thread and the turn, which is the only ordering that
        # makes continuity survive a crash: after this returns the identifier
        # is durable, and before it returns there is no work to be lost.
        self._capture_conversation(self._require_thread_id())
        # The last checkpoint. Everything above is recoverable; the request
        # below is the one that spends the turn.
        self._gate_submission()
        turn_params = dict(self._plan.turn_params)
        turn_params["threadId"] = self._require_thread_id()
        turn_params["input"] = [{"type": "text", "text": self._prompt}]
        # Two reports, because the request has two edges and a process can be
        # lost between them. The first fires as the prompt goes out and says
        # only that it did; the second fires on the answer and says the
        # app-server has it. Everything above this line is a conversation that
        # can still be declined -- a refused ``thread/resume`` most of all --
        # and a caller told the prompt was submitted there would have consumed
        # words this process is still holding.
        turn = self._request(
            "turn/start",
            turn_params,
            on_sending=self._note_submission_in_flight,
        )
        self._adopt_turn(turn)
        self._note_submitted()

    def _open_thread(self) -> None:
        """Start a fresh thread, or restore exactly the one that was asked for.

        The two calls answer in the same shape, so they share an adoption path;
        what differs is how much of the answer has to be re-checked, and that a
        resume names the thread it expects back.
        """
        params = dict(self._plan.thread_params)
        resume = self._resume_conversation_id
        if resume is None:
            self._adopt_thread(self._request("thread/start", params), resumed=False)
            return
        params["threadId"] = resume
        try:
            response = self._request("thread/resume", params)
        except _RequestRejected as error:
            detail = f"could not restore conversation {resume}: {error}"
            if not error.rejection.diagnoses_missing_history():
                # The app-server answered, but it did not say the history is
                # gone -- it said something about itself, its credentials, or
                # this moment. The turn still fails; the thread stays a thread,
                # so repairing whatever this was and re-running continues it.
                raise AppServerError(detail) from error
            # This answer *is* about the history. Nothing a later launch does
            # changes it, so the refusal is allowed to outlive the turn.
            raise RestoreRefused(detail) from error
        self._adopt_thread(response, resumed=True)

    def _adopt_thread(self, response: Any, *, resumed: bool) -> None:
        method = "thread/resume" if resumed else "thread/start"
        if not isinstance(response, Mapping):
            raise AppServerError(f"{method} returned no result")
        thread = response.get("thread")
        if not isinstance(thread, Mapping) or not isinstance(thread.get("id"), str):
            raise AppServerError(f"{method} returned no thread id")
        thread_id = str(thread["id"])
        if resumed and thread_id != self._resume_conversation_id:
            # "Resume the latest" is the failure mode this whole path exists to
            # avoid, and an id that came back different is exactly that -- a
            # conversation nobody asked to continue, about to be handed a turn.
            raise AppServerError(
                f"thread/resume returned thread {thread_id!r}, "
                f"expected {self._resume_conversation_id!r}"
            )
        sandbox = response.get("sandbox")
        actual = sandbox.get("type") if isinstance(sandbox, Mapping) else None
        if actual != self._plan.expected_sandbox:
            # The access profile is a promise made to the operator, and the
            # only honest place to check it is the app-server's own echo of
            # what it applied. Failing here is before `turn/start`, so the
            # legacy transport -- which states the profile in argv -- can
            # still run this turn.
            raise AppServerError(
                f"{method} applied sandbox "
                f"{actual!r}, expected {self._plan.expected_sandbox!r}"
            )
        if resumed:
            self._require_restored_settings(response)
        model = response.get("model")
        with self._state_lock:
            self._thread_id = thread_id
            if isinstance(model, str) and model:
                self._observed_model = model

    def _require_restored_settings(self, response: Mapping[str, Any]) -> None:
        """Refuse a restored thread whose settings are not the ones requested.

        A fresh thread applies what ``thread/start`` asked for, so its echo can
        only disagree about the sandbox the server derived. A restored one
        carries the settings of the thread it restores, and only the echo says
        which of the two won -- so every requested setting that changes what
        the turn may do is re-checked here rather than assumed.
        """
        for key in _RESTORED_SETTINGS:
            if key not in self._plan.thread_params:
                continue
            requested = self._plan.thread_params[key]
            applied = response.get(key)
            if applied != requested:
                raise AppServerError(
                    f"thread/resume applied {key} {applied!r}, expected {requested!r}"
                )

    def _capture_conversation(self, thread_id: str) -> None:
        """Hand the thread identifier to the caller, or abandon the turn."""
        sink = self._on_conversation
        if sink is None:
            return
        try:
            sink(thread_id)
        except Exception as error:  # noqa: BLE001 - any failure is a refusal
            raise _ConversationNotRecorded(
                str(error) or type(error).__name__
            ) from error

    def _gate_submission(self) -> None:
        """Ask the caller whether this turn may still be submitted."""
        gate = self._before_submit
        if gate is None:
            return
        try:
            gate()
        except Exception as error:  # noqa: BLE001 - any failure is a refusal
            raise _SubmissionRefused(str(error) or type(error).__name__) from error

    def _note_submission_in_flight(self) -> None:
        """Record that ``turn/start`` is going out, before it can be seen to.

        Runs from inside the write, after the pipe has been found open and
        before a byte of the prompt is handed to it, which is the last instant
        at which "nothing was sent" is still true. Placing it after the write
        would leave the partial write -- prompt on the wire, exception here --
        looking exactly like a turn that never sent anything.

        Never raises, for the same reason :meth:`_note_submitted` does not: it
        would surface as a protocol error the app-server never made, and it
        would do so while the prompt is already leaving.
        """
        with self._state_lock:
            self._turn_dispatched = True
        sink = self._on_submission_in_flight
        if sink is None:
            return
        with contextlib.suppress(Exception):
            sink()

    def _note_submitted(self) -> None:
        """Tell the caller the prompt is the worker's now.

        Never raises: this runs on the driver thread, after the turn has been
        accepted, where there is nothing left to abandon and an exception would
        only be swallowed by :meth:`_drive` as a protocol error the app-server
        never made.
        """
        sink = self._on_submitted
        if sink is None:
            return
        with contextlib.suppress(Exception):
            sink()

    def _abandon_before_submission(
        self,
        *,
        conversation_error: str | None = None,
        submission_refused: str | None = None,
    ) -> None:
        """End a healthy conversation that was told not to submit its turn.

        No protocol error is recorded, because none happened: the app-server is
        working and is simply being let go without a turn. That distinction is
        what keeps this out of the fallback window.
        """
        with self._state_lock:
            if conversation_error is not None:
                self._conversation_error = conversation_error
            if submission_refused is not None:
                self._submission_refused = submission_refused
        self._turn_finished.set()
        self._close_stdin()

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

    # -- interruption ------------------------------------------------------

    def interrupt(self) -> bool:
        """Tell the app-server to stop the turn this session started.

        Names the exact thread and turn, like every other call here, so it can
        only ever stop the turn this session is running. It reports whether the
        app-server accepted the interrupt, not whether the turn has stopped:
        the caller is already on its way to a forced termination and the point
        of asking first is that the conversation records the turn as
        *interrupted* rather than as one that stopped answering.

        Never raises. Every caller reaches for this while tearing a turn down,
        where an exception would only obscure why the turn ended.
        """
        with self._state_lock:
            thread_id = self._thread_id
            turn_id = self._turn_id
            finished = self._turn_status is not None
        if thread_id is None or turn_id is None:
            # Nothing was submitted, so there is no turn to interrupt: a
            # cancellation here already cost nothing.
            return False
        if finished or self._turn_finished.is_set():
            return False
        if self._process.poll() is not None:
            return False
        with self._state_lock:
            self._interrupt_requested = True
        try:
            self._request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout=self._interrupt_timeout,
            )
        except AppServerError:
            # Not a protocol error: the turn is ending regardless, and
            # recording one here would restate a cancelled turn as a failed
            # one. What was requested is reported by ``interrupt_requested``.
            return False
        return True

    # -- transport primitives ----------------------------------------------

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout: float | None = None,
        on_sending: Callable[[], None] | None = None,
    ) -> Any:
        """Send one request and wait for its answer.

        ``on_sending`` runs inside the write, in the instant before the payload
        reaches the pipe, for the one caller whose request cannot be un-sent:
        ``turn/start`` carries the prompt, so what this request does is settled
        by the write rather than by the answer it may never get.
        """
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
            self._write(payload, on_sending=on_sending)
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
        if pending.rejection is not None:
            raise _RequestRejected(
                f"{method} failed: {pending.rejection.rendered}",
                rejection=pending.rejection,
            )
        if pending.error is not None:
            # The transport gave up on this request rather than the app-server
            # answering it. Same failed turn, but nothing was stated, so it must
            # not reach the handler that can retire a conversation.
            raise AppServerError(f"{method} failed: {pending.error}")
        return pending.result

    def _notify(self, method: str, params: Mapping[str, Any] | None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._write(payload)

    def _write(
        self,
        payload: Mapping[str, Any],
        *,
        on_sending: Callable[[], None] | None = None,
    ) -> None:
        """Put one payload on the app-server's stdin.

        ``on_sending`` is called after the pipe has been found open and before
        anything is written to it: the two refusals above it prove nothing left
        this process, while everything from the write down is ambiguous, and a
        caller that has to tell those apart can only be told here.
        """
        stdin = self._process.stdin
        if stdin is None:
            raise AppServerError("app-server was spawned without a stdin pipe")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            with self._state_lock:
                if self._stdin_closed:
                    raise AppServerError("app-server stdin is closed")
            if on_sending is not None:
                on_sending()
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

        A turn still running at this point is one the harness stopped waiting
        for, so it is interrupted before the pipe is taken away -- bounded by
        the interrupt timeout, and skipped outright (in the usual case) when
        the turn has a terminal status or the process is already gone.
        """
        self.interrupt()
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
