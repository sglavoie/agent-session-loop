# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""ACP message decoding and protocol-state handling."""

from .copilot_acp_types import *  # noqa: F403
from .copilot_acp_types import _LineDecoder, _Pending, __version__
from .harness_json import decode_harness_line
from .copilot_acp_utils import (
    _content_text,
    _params,
    _select_option,
    _text_or_empty,
    _tool_label,
)


class _AcpProtocolMixin:
    """One ACP conversation, scoped to exactly one harness turn.

    Constructed around a spawned process, driven by :meth:`start`, fed by the
    harness reader, and torn down by :meth:`shutdown`. The turn it runs is the
    only turn it will ever run: one session is created or restored, one prompt
    is sent, and the session closes its own stdin as soon as that prompt
    answers.

    ``resume_conversation_id`` names the session to restore instead of creating
    a fresh one. It is a requirement, not a preference: a resume that cannot be
    performed fails the turn rather than falling back to a new conversation,
    which is the silent substitution the whole continuity contract exists to
    prevent.
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
        on_conversation: ConversationSink | None = None,
        on_submitted: SubmissionSink | None = None,
        resume_conversation_id: str | None = None,
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
        self._on_conversation = on_conversation
        self._on_submitted = on_submitted
        self._resume_conversation_id = resume_conversation_id
        self._request_timeout = request_timeout
        self._shutdown_grace = shutdown_grace

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._decoder = _LineDecoder()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0

        self._session_id: str | None = None
        self._agent_info: str | None = None
        self._loads_sessions = False
        self._resumed = False
        self._replaying = False
        self._replayed_updates = 0
        self._live_updates = 0
        self._observed_model: str | None = None
        self._observed_effort: str | None = None
        self._option_mismatches: list[str] = []
        self._message_parts: list[str] = []
        self._stop_reason: str | None = None
        self._prompt_submitted = False
        self._cancel_requested = False
        self._protocol_error: str | None = None
        self._restore_refused: str | None = None
        self._submission_refused: str | None = None
        self._conversation_error: str | None = None
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
    def loads_sessions(self) -> bool:
        """Whether ``initialize`` advertised the ``loadSession`` capability."""
        with self._state_lock:
            return self._loads_sessions

    @property
    def resumed(self) -> bool:
        """Whether this turn runs on a session ``session/load`` restored."""
        with self._state_lock:
            return self._resumed

    @property
    def replayed_updates(self) -> int:
        """How many transcript updates the restored session replayed.

        History, counted rather than rendered. It says the load really did
        return a conversation without letting any of that conversation be
        mistaken for something this turn did.
        """
        with self._state_lock:
            return self._replayed_updates

    @property
    def live_updates(self) -> int:
        """How many updates arrived as this turn's own activity.

        Everything the agent said after the session was open and the replay, if
        there was one, had finished. Zero on a resumed turn means the worker was
        handed the prompt and said nothing new -- which a replayed transcript
        must never be allowed to disguise.
        """
        with self._state_lock:
            return self._live_updates

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
    def restore_refused(self) -> str | None:
        """Why the agent would not load the session that was asked for.

        Set only by :class:`RestoreRefused`, so it means the agent no longer
        offers this history -- not that this turn happened to fail while
        restoring. Also reported as ``protocol_error``, because for everything
        that only needs to know the turn failed, it is one.
        """
        with self._state_lock:
            return self._restore_refused

    @property
    def cancelled_before_submission(self) -> str | None:
        """Why the turn was refused before ``session/prompt``, if it was."""
        with self._state_lock:
            return self._submission_refused

    @property
    def conversation_error(self) -> str | None:
        """Why the session identifier was not persisted, if it was not."""
        with self._state_lock:
            return self._conversation_error

    @property
    def cancel_requested(self) -> bool:
        """Whether ``session/cancel`` was sent for this turn."""
        with self._state_lock:
            return self._cancel_requested

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
            message = decode_harness_line(stripped)
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
                request_id = message["id"]
                try:
                    return self._handle_request(request_id, method, _params(message))
                except Exception as error:  # noqa: BLE001 - answer, then report
                    # The agent blocks on its request, so a failure here still
                    # owes it an answer: unanswered, the turn would wait out
                    # its whole timeout for a reply that is never coming.
                    detail = f"{type(error).__name__}: {error}"
                    self._note_protocol_error(f"failed to handle {method}: {detail}")
                    self._answer(
                        request_id,
                        error={"code": _INTERNAL_ERROR, "message": detail},
                    )
                    return ""
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
            pending.resolve(
                rejection=restore_diagnosis.ProviderRejection.from_payload(
                    message.get("error")
                )
            )
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
            replaying = self._replaying
        if expected is not None and session_id is not None and session_id != expected:
            # Another session on the same agent said something. This session
            # owns one turn and must not log a stranger's work as its own.
            return ""
        if replaying:
            return self._note_replayed()
        with self._state_lock:
            self._live_updates += 1
        return self._render_update(update)

    def _note_replayed(self) -> str:
        """Count one replayed transcript update and render nothing for it.

        ``session/load`` streams the restored conversation back before it
        answers, and every part of treating that as this turn would be a lie.
        Rendering it would reprint earlier turns into this turn's log -- prompt
        echoes included, which is the one thing this transport exists to keep
        out. Appending it would hand the caller an old answer as this turn's
        final message. Counting it as activity would let a transcript stand in
        for a worker that has not yet done, said, or acknowledged anything.
        Completion is unreachable from here by construction: only the
        ``session/prompt`` response carries a stop reason.
        """
        with self._state_lock:
            self._replayed_updates += 1
        return ""

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

    def _note_restore_refused(self, detail: str) -> None:
        with self._state_lock:
            if self._restore_refused is None:
                self._restore_refused = detail
        self._note_protocol_error(detail)

    # -- driving: the handshake, the submission, the orderly shutdown -------
