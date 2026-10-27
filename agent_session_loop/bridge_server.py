"""The stdio transport for the bridge: bounded frame reads and the serve loop."""

from __future__ import annotations

import errno
import os
import select
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from .agent_mux_projection import CONTRACT_SET_ID
from .bridge_projection import build_runs_snapshot
from .bridge_protocol import (
    ERROR_FRAME_TOO_LARGE,
    MAX_FRAME_BYTES,
    POLL_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    BridgeError,
    _error_response,
    encode_frame,
)
from .bridge_session import BridgeSession
from .bridge_workers import ProjectionWorker, WorkbenchLifecycle


class _FrameTooLarge(Exception):
    """One inbound line exceeded the frame ceiling and has been drained."""


_READ_CHUNK_BYTES = 65536


class _FrameReader:
    """Split an inbound byte stream into bounded NDJSON frames.

    The serve loop waits on the descriptor with ``select``, which only knows
    about bytes the kernel still holds. A ``BufferedReader`` can pull several
    frames into user space with one ``readline``, after which ``select`` reports
    nothing to read and the frames behind the first sit unanswered until the
    client happens to write again. Owning the buffer here is what lets the loop
    ask whether a frame is already waiting before it blocks, and a partial frame
    no longer holds the loop inside ``readline`` either.

    A line of more than ``limit`` payload bytes is never buffered whole: once
    the unterminated tail outgrows the ceiling it is dropped and the rest of
    the line is discarded as it arrives, surfacing as one ``_FrameTooLarge``.
    """

    _OVERSIZE = object()
    _END = object()

    def __init__(
        self, read: Callable[[int], bytes], *, limit: int = MAX_FRAME_BYTES
    ) -> None:
        self._read = read
        self._limit = limit
        self._partial = bytearray()
        self._discarding = False
        self._items: deque[object] = deque()
        self._ended = False

    def pending(self) -> bool:
        """Whether a frame, an oversize report, or the end is already queued."""
        return bool(self._items)

    def fill(self) -> None:
        """Do one read from the stream and queue every frame it completes."""
        if self._ended:
            return
        data = self._read(_READ_CHUNK_BYTES)
        if not data:
            self._ended = True
            last = bytes(self._partial).rstrip(b"\r\n")
            if self._discarding or len(last) > self._limit:
                self._items.append(self._OVERSIZE)
            elif last:
                self._items.append(last)
            self._partial.clear()
            self._items.append(self._END)
            return
        start = 0
        while start < len(data):
            newline = data.find(b"\n", start)
            if self._discarding:
                if newline < 0:
                    return
                self._discarding = False
                self._items.append(self._OVERSIZE)
                start = newline + 1
                continue
            if newline < 0:
                self._partial += data[start:]
                break
            self._partial += data[start : newline + 1]
            start = newline + 1
            line = bytes(self._partial).rstrip(b"\r\n")
            self._partial.clear()
            if len(line) > self._limit:
                self._items.append(self._OVERSIZE)
            else:
                self._items.append(line)
        # Room for a CRLF terminator still to come, since it is not counted.
        if len(self._partial) > self._limit + 2:
            self._partial.clear()
            self._discarding = True

    def next_frame(self) -> bytes | None:
        """Pop the next queued frame, or ``None`` once the stream has ended.

        Call only while ``pending()`` is true.
        """
        item = self._items[0]
        if item is self._END:
            return None
        self._items.popleft()
        if item is self._OVERSIZE:
            raise _FrameTooLarge
        assert isinstance(item, bytes)
        return item


def _discard_closed_writer(writer: TextIO) -> None:
    """Point a writer whose reader went away at the null device.

    The frame that hit the broken pipe is still in the writer's buffer, and the
    interpreter flushes ``sys.stdout`` again on the way out: that second flush
    fails too, prints ``Exception ignored ... BrokenPipeError``, and turns the
    clean exit 0 a closed client is owed into 120. Redirecting the descriptor
    lets that flush succeed into nothing. A writer with no descriptor (a test's
    in-memory sink) has no exit-time flush to defuse.
    """
    try:
        fd = writer.fileno()
    except AttributeError, OSError, ValueError:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, fd)
    except OSError:
        pass
    finally:
        os.close(devnull)


def serve_stdio(
    *,
    reader: BinaryIO,
    writer: TextIO,
    diagnostics: TextIO,
    state_dir: str | Path | None = None,
    plan_roots: Sequence[str | Path] = (),
    include_default_dirs: bool = True,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the bridge server loop until the client closes the stream."""

    def build() -> dict[str, Any]:
        return build_runs_snapshot(
            state_dir=state_dir,
            plan_roots=plan_roots,
            include_default_dirs=include_default_dirs,
        )

    lifecycle = WorkbenchLifecycle(
        state_dir=state_dir,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    worker = ProjectionWorker(build, interval=poll_interval, clock=clock)
    session = BridgeSession(
        # The session reads the published projection instead of walking the
        # corpus itself, so neither a snapshot response nor the next frame ever
        # waits on a build.
        snapshot_builder=worker.latest,
        state_dir=state_dir,
        workbench_lifecycle=lifecycle,
        clock=clock,
        poll_interval=poll_interval,
        projection_cadence=worker.cadence,
    )

    frames = _FrameReader(lambda size: os.read(reader.fileno(), size))

    def emit(payload: Mapping[str, Any]) -> bool:
        frame = encode_frame(payload)
        if len(frame) > MAX_FRAME_BYTES:
            print(
                f"agent-session-loop: dropping oversize {payload.get('type')} frame "
                f"({len(frame)} bytes)",
                file=diagnostics,
                flush=True,
            )
            return True
        try:
            writer.write(frame.decode("utf-8"))
            writer.flush()
        except BrokenPipeError:
            _discard_closed_writer(writer)
            return False
        except OSError as error:
            if error.errno in {errno.EPIPE, errno.ESHUTDOWN}:
                _discard_closed_writer(writer)
                return False
            raise
        return True

    def loop() -> int:
        if not emit(session.hello()):
            return 0
        worker.start()

        while True:
            if not frames.pending():
                timeout = session.select_timeout()
                try:
                    ready, _, _ = select.select([reader], [], [], timeout)
                except OSError, ValueError:
                    return 0
                if ready:
                    try:
                        frames.fill()
                    except OSError:
                        return 0
            if frames.pending():
                try:
                    raw = frames.next_frame()
                except _FrameTooLarge:
                    if not emit(
                        _error_response(
                            None,
                            BridgeError(
                                ERROR_FRAME_TOO_LARGE,
                                "request frame exceeded the protocol frame ceiling",
                                details={"max_frame_bytes": str(MAX_FRAME_BYTES)},
                            ),
                        )
                    ):
                        return 0
                    continue
                if raw is None:
                    return 0
                if raw:
                    response = session.handle_frame(raw)
                    if response is not None and not emit(response):
                        return 0
            if clock() >= session.next_projection_at():
                event = session.poll()
                if event is not None and not emit(event):
                    return 0
                for update in session.poll_operations():
                    if not emit(update):
                        return 0

    try:
        return loop()
    finally:
        # Both background threads belong to this process, so they die with it
        # rather than outliving the client they were started for.
        worker.stop()
        lifecycle.stop()


def bridge_serve_from_args(args: Any) -> int:
    """Serve protocol 2 on stdio for ``asl bridge serve``.

    There is no protocol-1 path left to select. A client pinned to the retired
    version fails here, before a byte of protocol is written, rather than
    discovering the mismatch halfway through a session.
    """
    requested = getattr(args, "protocol", PROTOCOL_VERSION)
    if requested != PROTOCOL_VERSION:
        print(
            f"agent-session-loop: unsupported bridge protocol {requested}; "
            f"this build speaks protocol {PROTOCOL_VERSION} "
            f"(contract set {CONTRACT_SET_ID})",
            file=sys.stderr,
        )
        return 2
    return serve_stdio(
        reader=sys.stdin.buffer,
        writer=sys.stdout,
        diagnostics=sys.stderr,
        state_dir=getattr(args, "state_dir", None),
        plan_roots=tuple(getattr(args, "plan_root", None) or ()),
    )
