# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""ACP handshake, restore, and prompt-submission orchestration."""

from .copilot_acp_types import *  # noqa: F403
from .copilot_acp_types import (
    _ConversationNotRecorded,
    _RequestRejected,
    _SubmissionRefused,
    __version__,
)
from .copilot_acp_utils import _text_or_empty


class _AcpTurnMixin:
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
        except _ConversationNotRecorded as error:
            self._abandon_before_submission(conversation_error=str(error))
        except _SubmissionRefused as error:
            self._abandon_before_submission(submission_refused=str(error))
        except RestoreRefused as error:
            self._note_restore_refused(str(error))
        except AcpError as error:
            self._note_protocol_error(str(error))
        self._turn_finished.set()
        # A refused or failed handshake can leave the agent alive too. Every
        # outcome needs the same bounded EOF, terminate, and kill sequence.
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
        self._open_session()
        # Between the session and the turn, which is the only ordering that
        # makes continuity survive a crash: after this returns the identifier is
        # durable, and before it returns there is no work to be lost.
        self._capture_conversation(self._require_session_id())

    def _open_session(self) -> None:
        """Create a fresh session, or restore exactly the one asked for."""
        resume = self._resume_conversation_id
        if resume is None:
            self._adopt_session(self._request("session/new", self._session_params()))
            return
        self._load_session(resume)

    def _load_session(self, session_id: str) -> None:
        """Restore one named session, or fail the turn saying why it could not.

        Four refusals, and every one of them happens before anything is
        submitted, because the alternative to each is a turn that ran somewhere
        other than where it was told to:

        - the agent never advertised ``loadSession``, so there is no restore to
          attempt and a ``session/new`` here would be a fresh conversation
          wearing an old conversation's name;
        - the load failed, which includes the ordinary case of a session the
          agent no longer has;
        - the answer named a different session than the one requested;
        - the replay could not be read, so what came back cannot be claimed to
          be the conversation that was asked for.

        The session is named *before* the request goes out, because the replay
        arrives while it is still in flight and the notification filter has to
        know which session owns it.
        """
        if not self.loads_sessions:
            raise AcpError(
                f"copilot did not advertise the {AGENT_CAPABILITY_LOAD_SESSION} "
                f"capability, so conversation {session_id} cannot be continued"
            )
        params = self._session_params()
        params["sessionId"] = session_id
        with self._state_lock:
            self._session_id = session_id
            self._replaying = True
        try:
            try:
                response = self._request("session/load", params)
            except _RequestRejected as error:
                detail = f"could not restore conversation {session_id}: {error}"
                if not error.rejection.diagnoses_missing_history():
                    # The agent answered, but it did not say the history is
                    # gone -- it said something about itself, its credentials,
                    # or this moment. The turn still fails; the session stays a
                    # session, so repairing whatever this was and re-running
                    # continues it.
                    raise AcpError(detail) from error
                # This answer *is* about the history. Nothing a later launch
                # does changes it, so the refusal may outlive the turn.
                raise RestoreRefused(detail) from error
            except AcpError as error:
                raise AcpError(
                    f"could not restore conversation {session_id}: {error}"
                ) from error
            finally:
                with self._state_lock:
                    self._replaying = False
            self._adopt_loaded_session(session_id, response)
        except AcpError:
            # No refused restore leaves a session id behind, whichever of the
            # four refusals it was. The turn is over either way, and an
            # identifier still sitting here would say this turn had a session
            # it was in fact denied.
            with self._state_lock:
                self._session_id = None
            raise

    def _adopt_loaded_session(self, session_id: str, response: Any) -> None:
        """Accept a restored session, or refuse one that is not the one asked for."""
        # ``session/load`` answers with ``null`` in the base protocol and with a
        # result object where the agent has something to restate; both are a
        # successful restore, and only a *contradictory* one is not.
        if response is None:
            response = {}
        if not isinstance(response, Mapping):
            raise AcpError("session/load returned no result")
        returned = response.get("sessionId")
        if isinstance(returned, str) and returned and returned != session_id:
            raise AcpError(
                f"session/load returned session {returned!r}, expected {session_id!r}"
            )
        replay_error = self.protocol_error
        if replay_error is not None:
            detail = (
                f"conversation {session_id} was not restored, "
                f"its replayed transcript could not be read: {replay_error}"
            )
            # Overwritten rather than noted, which is the one place this class
            # replaces a recorded error instead of keeping the first. The
            # replay failure is already recorded and is what the turn's log
            # would otherwise say -- a framing complaint, with nothing to
            # connect it to the restore it actually killed.
            with self._state_lock:
                self._protocol_error = detail
            raise AcpError(detail)
        with self._state_lock:
            self._session_id = session_id
            self._resumed = True
        self._adopt_config_options(response.get("configOptions"))

    def _session_params(self) -> dict[str, Any]:
        # The session's whole configuration, and the reason ``session/load``
        # sends it too: a restored session is configured by this call rather
        # than by whatever the session was configured with when it was last
        # open, so restoring one without restating it would run the turn under
        # a stale working directory and a stale server list.
        #
        # Model, effort, tool filters and repository grants are not here
        # because they are launch flags on this agent rather than session
        # parameters: ``cwd`` names where the session runs and ``mcpServers``
        # is the empty list that adds none.
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
        capabilities = response.get("agentCapabilities")
        # ``is True`` rather than truthiness: a capability advertised in any
        # other shape is one this client cannot claim was advertised, and the
        # cost of guessing wrong is a resume that silently becomes a fresh
        # conversation.
        loads_sessions = (
            isinstance(capabilities, Mapping)
            and capabilities.get(AGENT_CAPABILITY_LOAD_SESSION) is True
        )
        info = response.get("agentInfo")
        label = ""
        if isinstance(info, Mapping):
            name = _text_or_empty(info.get("name"))
            agent_version = _text_or_empty(info.get("version"))
            label = " ".join(part for part in (name, agent_version) if part)
        with self._state_lock:
            self._loads_sessions = loads_sessions
            if label:
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
            # Reported when the request is written, not when it answers: this
            # one answers only when the whole turn is over, and a caller still
            # holding the operator's words needs to know they went out the
            # moment they did. Everything above this call can still refuse --
            # a ``session/load`` the agent declines most of all -- so nothing
            # above it may be read as delivery.
            on_sent=self._note_submitted,
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

    def _capture_conversation(self, session_id: str) -> None:
        """Hand the session identifier to the caller, or abandon the turn."""
        sink = self._on_conversation
        if sink is None:
            return
        try:
            sink(session_id)
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

    def _note_submitted(self) -> None:
        """Tell the caller the prompt is the worker's now.

        Never raises: the request is already on the wire, so there is nothing
        left to abandon, and an exception here would be caught by :meth:`_drive`
        as a protocol failure the agent never had.
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

        No protocol error is recorded, because none happened: the agent is
        working and is simply being let go without a prompt.
        """
        with self._state_lock:
            if conversation_error is not None:
                self._conversation_error = conversation_error
            if submission_refused is not None:
                self._submission_refused = submission_refused
        self._turn_finished.set()

    def _require_session_id(self) -> str:
        with self._state_lock:
            session_id = self._session_id
        if session_id is None:
            raise AcpError("no acp session has been created")
        return session_id

    # -- interruption ------------------------------------------------------
