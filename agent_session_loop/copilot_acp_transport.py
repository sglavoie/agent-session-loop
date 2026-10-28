# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""ACP request waiting, writes, cancellation, and process shutdown."""

from .copilot_acp_types import *  # noqa: F403
from .copilot_acp_types import _Pending


class _AcpTransportMixin:
    def cancel(self) -> bool:
        """Tell the agent to stop the turn this session submitted.

        A notification rather than a request, which is the protocol's own
        shape: there is nothing to answer, and the acknowledgment is the pending
        ``session/prompt`` coming back with ``cancelled`` instead of
        ``end_turn``. It reports whether the cancellation was *sent*, not
        whether the turn has stopped -- the caller is already on its way to a
        forced termination, and the point of asking first is that the
        conversation records the turn as cancelled rather than as one that
        stopped answering, which is what a later turn resuming it reads.

        Named with the session, like every other call here, so it can only ever
        stop this session's turn. Never raises: every caller reaches for this
        while tearing a turn down, where an exception would only obscure why
        the turn ended.
        """
        with self._state_lock:
            session_id = self._session_id
            submitted = self._prompt_submitted
            stopped = self._stop_reason is not None
        if session_id is None or not submitted:
            # Nothing was submitted, so there is no turn to cancel: a
            # cancellation here already cost nothing.
            return False
        if stopped or self._turn_finished.is_set():
            return False
        if self._process.poll() is not None:
            return False
        with self._state_lock:
            self._cancel_requested = True
        try:
            self._notify("session/cancel", {"sessionId": session_id})
        except AcpError:
            # Not a protocol error: the turn is ending regardless, and
            # recording one here would restate a cancelled turn as a failed
            # one. What was requested is reported by ``cancel_requested``.
            return False
        return True

    # -- transport primitives ----------------------------------------------

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout: float | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> Any:
        """Send one request and wait for its answer.

        ``on_sent`` runs between the write and the wait, for the one caller that
        cannot wait for the answer to learn what happened: ``session/prompt``
        returns only when the turn is over, so "the prompt went out" and "the
        turn finished" would otherwise be the same event.
        """
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
        if on_sent is not None:
            on_sent()
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
        if pending.rejection is not None:
            raise _RequestRejected(
                f"{method} failed: {pending.rejection.rendered}",
                rejection=pending.rejection,
            )
        if pending.error is not None:
            # The transport gave up on this request rather than the agent
            # answering it. Same failed turn, but nothing was stated, so it must
            # not reach the handler that can retire a conversation.
            raise AcpError(f"{method} failed: {pending.error}")
        return pending.result

    def _notify(self, method: str, params: Mapping[str, Any] | None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._write(payload)

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

        Allow one grace period after EOF and another after SIGTERM. An agent
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

    def shutdown(self) -> None:
        """Release the conversation once the harness process is gone.

        Runs on the turn's own thread from the harness ``finally``, so it must
        not wait on anything unbounded: the driver is joined with a bound, and a
        driver that outlives it is a daemon thread that finishes the bounded
        process teardown.
        """
        self._turn_finished.set()
        self._fail_pending("copilot acp session is closed")
        self._close_stdin()
        with self._state_lock:
            driver = self._driver
        if driver is not None:
            driver.join(timeout=self._shutdown_grace)
