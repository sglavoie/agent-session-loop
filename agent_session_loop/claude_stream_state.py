# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F821
"""State, prompt, and output-observation methods for a Claude stream session."""

from __future__ import annotations

from . import claude_stream_support as _support

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)
del _support


class _ClaudeStreamState:
    """One bidirectional stream-JSON conversation, scoped to one harness turn.

    Constructed around a spawned process, given its prompt immediately, fed
    observations by the output processor, and torn down once the process is
    gone. It owns the process's stdin outright: nothing else in the turn may
    write to it.
    """

    def __init__(
        self,
        *,
        process: subprocess.Popen,
        prompt: str,
        spool: ClaudeGuidanceSpool | None = None,
        session_id: str | None = None,
        interrupt_timeout: float = INTERRUPT_TIMEOUT_SECONDS,
        on_session_observed: Callable[[], None] | None = None,
    ) -> None:
        self._process = process
        self._prompt = prompt
        self._spool = spool
        self._session_id = session_id
        self._interrupt_timeout = interrupt_timeout
        #: Called once, the first time the CLI names a session of its own. That
        #: record is the CLI saying it got past its own startup -- including the
        #: ``--resume`` it could still have rejected -- so it is the earliest
        #: point anything may treat this turn as one the CLI took up.
        self._on_session_observed = on_session_observed

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()

        self._offers: dict[str, _Offer] = {}
        self._echo_expectations: list[_EchoExpectation] = []
        self._messages_written = 0
        self._messages_answered = 0
        self._stdin_closed = False
        self._prompt_error: str | None = None
        self._session_mismatch: str | None = None
        self._session_observed = False

        self._pending_control: dict[str, _PendingControl] = {}
        self._control_seq = 0
        self._interrupt_requested = False
        self._turn_interrupted = False
        self._background_tasks: set[str] = set()
        #: Whether a terminal result has been noted, so a hold reason is only
        #: reported for a stdin kept open *after* the CLI answered.
        self._result_noted = False

    # -- state a caller reads ----------------------------------------------

    @property
    def prompt_error(self) -> str | None:
        """Why the prompt never reached the CLI, or ``None``.

        A claude turn has no fallback transport: if the prompt could not be
        written there is nothing for the model to do, and the turn's stderr is
        where an operator finds out why.
        """
        with self._state_lock:
            return self._prompt_error

    @property
    def stdin_closed(self) -> bool:
        with self._state_lock:
            return self._stdin_closed

    @property
    def stdin_hold_reason(self) -> str | None:
        """Why stdin is still open after a terminal result, or ``None``.

        ``None`` before any result and once stdin is closed. Otherwise names
        what :meth:`note_result` is waiting for: background tasks the CLI
        announced and never reported finished, or written messages no result
        has answered yet.
        """
        with self._state_lock:
            if self._stdin_closed or not self._result_noted:
                return None
            if self._background_tasks:
                return "background-tasks"
            if self._messages_answered < self._messages_written:
                return "unanswered-messages"
            return "open"

    def release_stdin(self) -> bool:
        """Close a stdin held open after a result; ``True`` if this closed it.

        The recovery for a CLI left alone and idle behind that hold: EOF is
        how it is asked to finish, and it is tried before any signal.
        """
        with self._write_lock:
            with self._state_lock:
                if self._stdin_closed:
                    return False
            self._close_stdin_locked()
            return True

    @property
    def session_id(self) -> str | None:
        """The conversation this turn was launched under, if it was told one."""
        with self._state_lock:
            return self._session_id

    @property
    def session_observed(self) -> bool:
        """Whether the CLI ever named a session of its own on this turn.

        ``False`` means the CLI never got as far as announcing a conversation,
        which is what separates a ``--resume`` the CLI *refused* from one it
        honoured and then failed inside: the second announced its session first.
        """
        with self._state_lock:
            return self._session_observed

    @property
    def session_mismatch(self) -> str | None:
        """Why the conversation this turn ran on is not the one it was given.

        ``None`` on every ordinary turn, including one that stated no session at
        all. A value here means the recorded identifier does not name the
        conversation the work went into, so nothing may continue from it.
        """
        with self._state_lock:
            return self._session_mismatch

    @property
    def interrupt_requested(self) -> bool:
        """Whether an ``interrupt`` control request was written for this turn."""
        with self._state_lock:
            return self._interrupt_requested

    @property
    def turn_interrupted(self) -> bool:
        """Whether the turn's terminal result was the one it was stopped at.

        Kept separate from :attr:`interrupt_requested` because they answer
        different questions: one says ASL asked, the other says the CLI
        answered with the result of a turn that was cut short. Only the second
        is a statement about what the turn produced.
        """
        with self._state_lock:
            return self._turn_interrupted

    # -- the prompt ---------------------------------------------------------

    def send_prompt(self) -> bool:
        """Write the turn's initial prompt as the conversation's first message.

        Returns whether the write succeeded. A failure is recorded rather than
        raised: the process is already spawned and the turn has to be collected
        and reported like any other failed turn.
        """
        try:
            self._write(user_message(self._prompt), echo_body=self._prompt)
        except OSError as error:
            with self._state_lock:
                self._prompt_error = f"{type(error).__name__}: {error}"
            # Without a prompt the CLI would sit on an open stdin waiting for
            # work that is never coming, and the turn would end at its timeout
            # rather than at the failure. Closing turns that into a prompt exit.
            self.close_stdin()
            return False
        return True

    # -- observations from the output processor -----------------------------

    def note_background_task(self, task_id: str, *, finished: bool) -> None:
        """Keep EOF behind task completion and a subsequent result boundary.

        A task notification is not a turn boundary: the CLI still needs stdin
        for further control responses. Only a subsequent result may close it.
        That result may belong to the main turn when it consumes task outcomes
        inline, or to a synthetic follow-up. Identifiers keep tasks separate.
        """
        with self._state_lock:
            if finished:
                self._background_tasks.discard(task_id)
            else:
                self._background_tasks.add(task_id)

    def note_result(
        self, *, background_followup: bool = False, is_error: bool = False
    ) -> bool:
        """Record one terminal ``result`` record and close stdin when idle.

        The CLI in streaming-input mode runs until its stdin reaches EOF, so
        something has to close it or the turn never ends. It is closed once
        every written message has been answered. A result covers all messages
        echoed before it, including guidance taken up in the active tool loop.
        Without a new echo it covers one message, preserving the older CLI
        behavior. Unacknowledged queued guidance and background tasks keep
        stdin open. Synthetic task results do not answer queued user messages.
        Returns whether this result ends the conversation.
        """
        # Serialize the decision and close with writes: a guidance write must
        # either be counted before the decision or observe the closed pipe.
        with self._write_lock:
            with self._state_lock:
                self._result_noted = True
                if self._interrupt_requested:
                    # This result reports the turn the CLI was told to stop, so
                    # it is drained rather than counted: it answers whatever it
                    # answers, but crediting it against the messages still
                    # queued would let an interrupted turn settle guidance the
                    # model never ran, and would leave a later result looking
                    # like the resumed turn's own. The conversation is over
                    # either way, so stdin closes regardless of the queue.
                    self._turn_interrupted = True
                    idle = True
                elif is_error:
                    idle = True
                else:
                    echoed_through = max(
                        (
                            index + 1
                            for index, expectation in enumerate(self._echo_expectations)
                            if expectation.consumed
                        ),
                        default=0,
                    )
                    if not background_followup:
                        self._messages_answered = max(
                            self._messages_answered + 1, echoed_through
                        )
                    idle = (
                        self._messages_answered >= self._messages_written
                        and not self._background_tasks
                    )
            if idle:
                self._close_stdin_locked()
            return idle

    def note_user_echo(self, text: str) -> None:
        """Record the CLI echoing a user message it has taken up.

        This is the protocol acknowledgement: the CLI emits the echo when it
        starts processing the message, so an echo carrying a guidance body is
        that body reaching the model. An echo is matched against the messages
        this session wrote, by body equality and in write order, and each echo
        is spent on at most one of them. That is what makes the
        acknowledgement specific: the turn's prompt claims its own echo however
        late it arrives, and an unrelated message that merely happens to
        contain a guidance phrase matches nothing at all.
        """
        if not text:
            return
        body = normalized_echo_body(text)
        if not body:
            return
        with self._state_lock:
            interrupted = self._interrupt_requested
            for expectation in self._echo_expectations:
                if expectation.consumed or expectation.body != body:
                    continue
                # Consumed either way, so this echo can never be spent twice:
                # the accounting is about which write it belongs to, and that
                # does not change because the turn is ending.
                expectation.consumed = True
                if expectation.guidance_id is None or interrupted:
                    # An echo the CLI emits while draining its stdin on the way
                    # out of an interrupted turn says it read the message, not
                    # that the model acted on it. Recording a delivery from it
                    # would spend a guidance record on work that was stopped.
                    return
                offer = self._offers.get(expectation.guidance_id)
                if offer is not None and offer.confirmed_by is None:
                    offer.confirmed_by = "protocol-echo"
                return

    def note_session_id(self, reported: str) -> str | None:
        """Re-check the session the CLI opened against the one it was given.

        Returns a description of the disagreement, or ``None`` when there is
        none — including when this turn stated no session at all, which is the
        ordinary shape of an audit turn that persists none.

        The turn is *not* stopped here. The prompt was written before the first
        record arrived, so by the time this can be checked the model is already
        changing the workspace, and abandoning it mid-edit to correct a
        bookkeeping fact would cost more than the fact is worth. What the
        mismatch does is disqualify the recorded identifier: the result reports
        it, and nothing continues from a conversation the work did not go into.
        """
        announced = False
        with self._state_lock:
            if reported and not self._session_observed:
                self._session_observed = True
                announced = True
            expected = self._session_id
            mismatch: str | None = None
            if not (expected is None or not reported or reported == expected):
                if self._session_mismatch is None:
                    self._session_mismatch = (
                        f"launched under session {expected} "
                        f"but the CLI reported session {reported}"
                    )
                mismatch = self._session_mismatch
        if announced:
            # Outside the state lock, and before the mismatch is returned: a
            # sink that blocks would otherwise block every other reader of this
            # session's state, and a turn the CLI took up is one it took up
            # whatever it then says about the identifier.
            self._announce_session()
        return mismatch

    def _announce_session(self) -> None:
        """Tell the turn's owner the CLI named a session, at most once.

        The sink's failure is swallowed: this reports something the CLI already
        did, and an exception raised here would be raised on the output reader's
        thread, where it would cost the turn its remaining output.
        """
        sink = self._on_session_observed
        self._on_session_observed = None
        if sink is None:
            return
        with contextlib.suppress(Exception):
            sink()

    def note_control_response(self, response: object) -> None:
        """Resolve the control request this response names, if it names one.

        Attribution is by ``request_id`` and by nothing else. A response
        carrying an identifier this session is not waiting on — one already
        given up on, one belonging to a request the CLI raised itself — resolves
        nothing, because the alternative is letting arrival order decide, and
        arrival order is what makes a late answer look like the next one.
        """
        if not isinstance(response, dict):
            return
        request_id = response.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._state_lock:
            pending = self._pending_control.pop(request_id, None)
        if pending is None:
            return
        pending.resolve(acknowledged=response.get("subtype") == SUBTYPE_SUCCESS)
