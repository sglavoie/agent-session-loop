"""Locked app-server state and bounded JSON-RPC transport primitives."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from .codex_app_server_types import (
    _LIVENESS_POLL_SECONDS,
    INTERRUPT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    STEER_TIMEOUT_SECONDS,
    TURN_STATUS_INTERRUPTED,
    AppServerError,
    ConversationSink,
    SubmissionGate,
    SubmissionInFlightSink,
    SubmissionSink,
    TurnPlan,
    _LineDecoder,
    _Pending,
    _RequestRejected,
)


class AppServerTransport:
    """Locked app-server state and bounded JSON-RPC transport primitives."""

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
        # Requests this client stopped waiting for. The app-server may still
        # answer one late, and that answer is the end of an exchange already
        # settled as a timeout rather than a message nobody sent.
        self._abandoned: set[int] = set()
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

    def _note_protocol_error(self, detail: str) -> None:
        with self._state_lock:
            if self._protocol_error is None:
                self._protocol_error = detail

    def _note_restore_refused(self, detail: str) -> None:
        with self._state_lock:
            if self._restore_refused is None:
                self._restore_refused = detail
        self._note_protocol_error(detail)

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
                    if self._pending.pop(request_id, None) is not None:
                        self._abandoned.add(request_id)
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

        Allow one grace period after EOF and another after SIGTERM. A server
        ignoring both must be killed, or a turn with no harness timeout can
        wait forever. The harness owns the final wait that reaps the process.
        """
        self._close_stdin()
        if self._wait_for_exit():
            return
        with contextlib.suppress(ProcessLookupError):
            self._process.terminate()
        if not self._wait_for_exit():
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()

    def _wait_for_exit(self) -> bool:
        """Wait at most one shutdown grace period for the process to exit."""
        deadline = time.monotonic() + self._shutdown_grace
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                return True
            time.sleep(_LIVENESS_POLL_SECONDS)
        return self._process.poll() is not None
