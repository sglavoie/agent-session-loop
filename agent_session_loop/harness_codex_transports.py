"""The codex harness's two transports and their output processors.

Owns the app-server conversation handle, the legacy `codex exec` continuity
observer and the fallback-window stream. `harness_codex` composes them."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path

from . import codex_app_server, guidance_delivery
from .harnesses import (
    ConversationCaptureError,
    HarnessResult,
    HarnessTransport,
    StreamCallback,
    StreamName,
    TurnLifecycle,
    _OutputProcessor,
    _read_last_message,
)


class _CodexAppServerOutputProcessor(_OutputProcessor):
    """Render one app-server conversation as the turn's readable stdout.

    Thin by design: every decision about the protocol lives in the session, and
    this only routes stdout into it and reports what it learned. stderr is left
    alone, because the app-server writes its own diagnostics there in prose.
    """

    def __init__(self, session: codex_app_server.CodexAppServerSession) -> None:
        self._session = session

    @property
    def final_result_seen(self) -> bool:
        # ``turn/completed`` is the app-server's terminal record, and it is as
        # authoritative as claude's ``result`` record: the turn is over even if
        # the process has not exited yet.
        return self._session.turn_status is not None

    @property
    def observed_model(self) -> str | None:
        return self._session.observed_model

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if stream_name != "stdout":
            return text
        return self._session.feed(text)

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        # The protocol reports the final message directly, so it is preferred
        # over the file the legacy transport had to be told to write.
        final = self._session.final_message
        if final:
            return final if final.endswith("\n") else f"{final}\n"
        del stderr
        return _read_last_message(last_message_path) or stdout


class _FallbackWindowStream:
    """Hold an app-server turn's output back while a fallback is still possible.

    Anything printed before ``turn/start`` is accepted is handshake noise, and
    if the conversation fails there the legacy transport runs the same turn for
    real. Streaming both would show an operator two turns' output for one turn
    and would leave the discarded attempt's bytes in the persisted log, so the
    early chunks are buffered until acceptance forecloses the fallback -- after
    which every chunk passes straight through, live, as it always did.

    Called only from the reader threads, which serialize on the harness's
    side-effect lock, and released or discarded from the turn's own thread once
    the readers are done with it.
    """

    def __init__(self, downstream: StreamCallback | None) -> None:
        self._downstream = downstream
        self._session: codex_app_server.CodexAppServerSession | None = None
        self._buffer: list[tuple[StreamName, str]] = []
        self._passing = False

    def attach(self, session: codex_app_server.CodexAppServerSession) -> None:
        self._session = session

    def __call__(self, stream_name: StreamName, text: str) -> None:
        if not self._passing:
            session = self._session
            if session is None or not (session.turn_accepted or session.work_observed):
                self._buffer.append((stream_name, text))
                return
            self.release()
        if self._downstream is not None:
            self._downstream(stream_name, text)

    def release(self) -> None:
        """Emit what was withheld: this turn is the one that really ran."""
        self._passing = True
        buffered, self._buffer = self._buffer, []
        if self._downstream is None:
            return
        for stream_name, text in buffered:
            self._downstream(stream_name, text)

    def discard(self) -> None:
        """Drop what was withheld: another run of this turn will be streamed."""
        self._passing = True
        self._buffer.clear()


class _CodexAppServerTransport(HarnessTransport):
    """The adapter's handle on one live app-server conversation."""

    def __init__(self, session: codex_app_server.CodexAppServerSession) -> None:
        self.session = session
        self._processor = _CodexAppServerOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return self.session.steer

    def interrupt(self) -> bool:
        return self.session.interrupt()

    def shutdown(self) -> None:
        self.session.shutdown()


#: The session identifier ``codex exec`` states in its own header. Anchored on
#: the UUID shape the app-server uses for the same value, so the only thing
#: this can match is a conversation id -- and a header spelled any other way
#: reports unavailable continuity instead of capturing a guess.
_CODEX_EXEC_SESSION_ID = re.compile(
    r"session[ _-]?id[^\n]{0,24}?"
    r"(?P<session_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


class _CodexExecOutputProcessor(_OutputProcessor):
    """Read the legacy transport's one continuity fact out of its own output.

    ``codex exec`` takes its prompt on argv, so the work is submitted the
    moment the process is spawned and there is no handshake in which to learn a
    conversation first. The session it ran on is stated in its header instead,
    which is continuity observed *after* the fact -- the opposite of the
    ordering the app-server transport keeps, and the reason a capture failure
    here cannot retract anything.

    Everything else is the plain processor's behaviour: the text is passed
    through untouched, because this reads the output rather than rendering it.
    """

    def __init__(self, lifecycle: TurnLifecycle | None) -> None:
        self._lifecycle = lifecycle
        self.native_id: str | None = None
        self.capture_error: str | None = None

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if self.native_id is None and self.capture_error is None:
            self._observe(text)
        return text

    def _observe(self, text: str) -> None:
        """Capture the first session id the turn names, and only the first.

        The header is one line and the readers deliver whole lines, so a match
        is either in this chunk or in none. A second id would be a different
        conversation for the same turn, which the capture seam refuses anyway;
        stopping at the first keeps that refusal from being reached at all.
        """
        match = _CODEX_EXEC_SESSION_ID.search(text)
        if match is None:
            return
        native_id = match.group("session_id")
        lifecycle = self._lifecycle
        if lifecycle is not None:
            try:
                lifecycle.capture_conversation(native_id)
            except ConversationCaptureError as error:
                # Raising would surface as a stream-callback failure and lose
                # the turn, which already ran. Reported as continuity that
                # could not be had instead -- the honest outcome for work that
                # cannot be un-submitted.
                self.capture_error = str(error)
                return
        self.native_id = native_id


class _CodexExecTransport(HarnessTransport):
    """The legacy one-shot turn's continuity observer.

    It holds no conversation -- ``codex exec`` has none to hold -- and exists
    only so the turn's own output still yields the session it ran on. There is
    no live channel and nothing to interrupt beyond the process itself, so
    guidance defers and interruption declines exactly as they did when this
    transport was ``None``.
    """

    def __init__(self, lifecycle: TurnLifecycle | None) -> None:
        self._processor = _CodexExecOutputProcessor(lifecycle)
        self._wanted = lifecycle is not None and lifecycle.wants_conversation

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    @property
    def continuity_note(self) -> str | None:
        """Why this turn has no conversation to continue, if it has none.

        ``None`` when one was captured, and also when nobody asked: an ordinary
        one-shot turn is not missing something it was never meant to have. The
        identifier is still read out of the output either way, so a result
        reports the session it ran on whether or not a sink wanted it.
        """
        if not self._wanted:
            return None
        if self._processor.capture_error is not None:
            return self._processor.capture_error
        if self._processor.native_id is not None:
            return None
        return "codex exec output named no session id"


class _ExecContinuityObserver:
    """Keep hold of a legacy turn's transport so its result can report it.

    ``open_transport`` builds the transport and ``run`` returns the result, and
    nothing in between hands one to the other -- so this rides along as the
    ``on_transport`` hook, passing every call through to the caller's own hook
    and keeping the one transport it recognizes.
    """

    def __init__(
        self,
        downstream: Callable[[HarnessTransport | None], None] | None,
    ) -> None:
        self._downstream = downstream
        self.transport: _CodexExecTransport | None = None

    def __call__(self, transport: HarnessTransport | None) -> None:
        if isinstance(transport, _CodexExecTransport):
            self.transport = transport
        if self._downstream is not None:
            self._downstream(transport)

    def apply(self, result: HarnessResult) -> HarnessResult:
        """Report what continuity the turn's output did or did not name.

        The identifier itself is already on the result: it was captured through
        the lifecycle, which is what fills ``conversation_id``. Only its
        *absence* needs stating, because a caller that asked to continue this
        conversation later would otherwise read "none" as "not yet looked".
        """
        transport = self.transport
        if transport is None:
            return result
        note = transport.continuity_note
        if note is None:
            return result
        return dataclasses.replace(result, continuity_unavailable=note)
