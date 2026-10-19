# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Control-request methods for a Claude stream session."""

from __future__ import annotations

from . import claude_stream_support as _support

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)
del _support


class _ClaudeStreamControl:
    def interrupt(self) -> bool:
        """Tell the CLI to stop the work this turn has in flight.

        Reports whether the CLI *acknowledged* the request, which is neither a
        promise that the work has stopped nor a substitute for the forced
        termination standing behind every caller. What it buys is the
        distinction: a turn told to stop ends as an interrupted one, while a
        turn whose pipes were merely taken away ends as one that stopped
        answering.

        Sent at most once per turn. A second request would be a second
        identifier to correlate for an outcome already decided, and the CLI's
        answer to it could only arrive after the first turned this turn into an
        interrupted one.

        Never raises. Every caller reaches for this while tearing a turn down,
        where an exception would only obscure why the turn ended.
        """
        with self._state_lock:
            if self._interrupt_requested:
                return False
            if self._stdin_closed or self._messages_written == 0:
                # Nothing was submitted, or the conversation is already over:
                # either way there is no work in flight to stop, and the
                # cancellation cost nothing to begin with.
                return False
            self._interrupt_requested = True
            self._control_seq += 1
            # ``req_<n>_<hex>`` is the shape the official SDK mints, and the
            # CLI treats it as opaque; matching it keeps ASL's traffic
            # indistinguishable from the traffic this control channel is
            # exercised with upstream.
            request_id = f"req_{self._control_seq}_{os.urandom(4).hex()}"
            pending = _PendingControl(request_id)
            self._pending_control[request_id] = pending

        try:
            self._write(control_request(request_id, SUBTYPE_INTERRUPT))
        except OSError:
            self._drop_pending(request_id)
            return False
        return self._await_control(pending)

    def _await_control(self, pending: _PendingControl) -> bool:
        """Wait out one control request, bounded by the clock and by liveness."""
        deadline = time.monotonic() + self._interrupt_timeout
        while not pending.event.wait(_LIVENESS_POLL_SECONDS):
            if self._process.poll() is not None:
                # The CLI is gone, which is the state the interrupt was asking
                # for. It is still not an acknowledgement: nothing said the turn
                # was stopped rather than killed.
                self._drop_pending(pending.request_id)
                return False
            if time.monotonic() >= deadline:
                self._drop_pending(pending.request_id)
                return False
        return pending.acknowledged

    def _drop_pending(self, request_id: str) -> None:
        """Stop waiting on a request, so a late answer correlates with nothing."""
        with self._state_lock:
            self._pending_control.pop(request_id, None)
