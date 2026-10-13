"""The stdio transport for the bridge: bounded frame reads and the serve loop."""

from __future__ import annotations

import errno
import select
import sys
import time
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


def read_frame(reader: BinaryIO, *, limit: int = MAX_FRAME_BYTES) -> bytes | None:
    """Read one bounded NDJSON frame, or ``None`` at end of stream.

    Reads ``limit + 2`` so a frame of exactly ``limit`` payload bytes plus its
    newline is accepted and anything longer is provably over budget without
    ever buffering it.
    """
    chunk = reader.readline(limit + 2)
    if not chunk:
        return None
    if len(chunk) > limit + 1:
        while chunk and not chunk.endswith(b"\n"):
            chunk = reader.readline(limit + 2)
        raise _FrameTooLarge
    return chunk.rstrip(b"\r\n")


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
            return False
        except OSError as error:
            if error.errno in {errno.EPIPE, errno.ESHUTDOWN}:
                return False
            raise
        return True

    def loop() -> int:
        if not emit(session.hello()):
            return 0
        worker.start()

        while True:
            timeout = session.select_timeout()
            try:
                ready, _, _ = select.select([reader], [], [], timeout)
            except OSError, ValueError:
                return 0
            if ready:
                try:
                    raw = read_frame(reader)
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
                except OSError:
                    return 0
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
