"""Common output processing, transport hooks, and native interrupt handling."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

from . import (
    guidance_delivery,
    harness_types,
)


class _OutputProcessor:
    """Normalize harness output before it is logged and persisted."""

    @property
    def final_result_seen(self) -> bool:
        """Whether the current request has emitted a terminal result record.

        Only the claude stream-json processor can report one; processors that
        do not parse structured output never do, which correctly disables any
        result-based detection for their harnesses. A processor that can carry
        more than one request resets this at the next request boundary.
        """
        return False

    @property
    def observed_model(self) -> str | None:
        """The model the harness reported it actually executed with.

        Only the claude stream-json processor can report one; processors that
        do not parse structured output never do, which correctly leaves the
        observed model unknown for their harnesses.
        """
        return None

    def process_chunk(self, stream_name: harness_types.StreamName, text: str) -> str:
        del stream_name
        return text

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        del stderr
        return _read_last_message(last_message_path) or stdout


class HarnessTransport:
    """The in-turn conversation an adapter has with the process it spawned.

    A one-shot harness has none: argv said everything, and stdout is a report
    rather than a dialogue. A protocol harness has exactly one, opened right
    after the process exists and released once it is gone, and it is the only
    thing in the turn that may write to that process's stdin.

    The four methods are what the generic turn loop needs to know about a
    conversation it otherwise ignores: how to read the process's output, how
    (or whether) to inject guidance into it, how to stop the work in flight,
    and how to let go at the end.
    """

    def output_processor(self) -> _OutputProcessor:
        """The processor that turns this transport's stdout into log text."""
        raise NotImplementedError

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        """The live-delivery channel, or ``None`` when there is none."""
        return None

    def interrupt(self) -> bool:
        """Tell this turn's conversation to stop the work it has in flight.

        ``False`` -- the base answer -- means the transport has no native
        interruption at all, which is the honest report for a one-shot harness:
        stopping it *is* the process kill and nothing else. A transport that
        returns ``True`` has told its harness to stop; it has not waited for it
        to have stopped, which is what the forced termination behind this call
        is for.
        """
        return False

    def shutdown(self) -> None:
        """Release the conversation. Called once, on every path out of a turn."""

    def input_hold_reason(self) -> str | None:
        """Why stdin is still held open after a terminal result, or ``None``.

        ``None`` -- the base answer -- means there is no held input to release.
        """
        return None

    def release_input(self) -> bool:
        """Close a held stdin; ``True`` when this call closed it."""
        return False


def _request_native_interrupt(transport: HarnessTransport | None) -> None:
    """Tell a protocol harness its turn was interrupted, without waiting for it.

    Issued on the way to the forced kill, never instead of it. The termination
    contract is that the deadline fires independently of any blocked protocol
    call, so this runs on a daemon thread the kill does not join: at worst the
    interrupt is still in flight when the process group dies under it, which is
    the same outcome as not sending one.

    It exists because a killed pipe says nothing. An app-server that records
    its own thread state ends the turn as *interrupted* when told so, and as a
    turn that simply stopped answering when it is only killed -- and a later
    turn resuming that conversation reads the difference.
    """
    if transport is None:
        return

    def send() -> None:
        with contextlib.suppress(Exception):
            transport.interrupt()

    threading.Thread(target=send, name="harness-interrupt", daemon=True).start()


def _read_last_message(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        # Decoded with replacement rather than strictly: a harness killed
        # mid-write can leave the file cut inside a multibyte character, and a
        # strict decode would lose the whole aborted turn's result over it.
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
