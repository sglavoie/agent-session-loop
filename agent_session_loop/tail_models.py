"""Output protocol, defaults, and errors for live tailing."""

from __future__ import annotations

from typing import Protocol

DEFAULT_LINES = 40
POLL_INTERVAL_SECONDS = 0.1


class TextSink(Protocol):
    """The stream contract the tail actually needs from its output.

    Declared structurally rather than as ``TextIO`` because every write goes
    through ``print(..., file=out, flush=True)``: a caller that shares an
    output lock -- the runner's inline tail does -- hands in a proxy that
    forwards those two calls and is no more of a file than that.
    """

    def write(self, text: str, /) -> int: ...

    def flush(self) -> None: ...


class TailError(Exception):
    """Raised when tail arguments or runtime files are invalid."""


class TailInputError(TailError):
    """A tail argument the caller passed is out of range."""
