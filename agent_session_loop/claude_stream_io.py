# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Pipe writing and shutdown methods for a Claude stream session."""

from __future__ import annotations

from . import claude_stream_support as _support

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)
del _support


class _ClaudeStreamIO:
    def _write(
        self,
        payload: dict[str, Any],
        *,
        echo_body: str | None = None,
        guidance_id: str | None = None,
    ) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise OSError("claude was spawned without a stdin pipe")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            expectation: _EchoExpectation | None = None
            with self._state_lock:
                if self._stdin_closed:
                    raise OSError("claude stdin is closed")
                # The echo can come back while ``flush`` is still returning: the
                # output processor is another thread reading the same CLI. So
                # the expectation is published before the bytes move, and the
                # write is undone -- expectation included -- if they never do.
                # Recording it afterwards leaves a window in which the message's
                # own echo matches nothing and is discarded for good.
                if echo_body is not None:
                    expectation = _EchoExpectation(echo_body, guidance_id)
                    self._echo_expectations.append(expectation)
            try:
                stdin.write(line)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                with self._state_lock:
                    if expectation is not None:
                        # A message that never reached the pipe has no echo
                        # coming, and a stale expectation would let some later
                        # unrelated echo of the same words confirm it.
                        self._echo_expectations.remove(expectation)
                raise OSError(str(error) or type(error).__name__) from error
            with self._state_lock:
                if echo_body is not None:
                    # Only a conversation message is counted. A control request
                    # is neither echoed back nor answered by a ``result``, so
                    # counting one would leave the conversation permanently
                    # short an answer and stdin permanently open.
                    self._messages_written += 1

    def close_stdin(self) -> None:
        """End the conversation, which is how the CLI is asked to finish."""
        with self._write_lock:
            self._close_stdin_locked()

    def _close_stdin_locked(self) -> None:
        """Close with the caller holding the write lock."""
        with self._state_lock:
            if self._stdin_closed:
                return
            self._stdin_closed = True
        stdin = self._process.stdin
        if stdin is None:
            return
        # An already-gone CLI is the state closing stdin was trying to
        # reach, so every failure mode here is a success.
        with contextlib.suppress(BrokenPipeError, OSError, ValueError):
            stdin.close()

    def shutdown(self) -> None:
        """Release the conversation once the harness process is gone."""
        self.close_stdin()
