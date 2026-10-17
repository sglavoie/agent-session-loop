"""Read harness pipes with synchronized callbacks and bounded abandonment."""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO

from . import (
    harness_transport,
    harness_types,
)


@dataclass(frozen=True)
class TurnStreams:
    """Output buffers and reader lifecycle for exactly one turn."""

    stdout_chunks: list[str]
    stderr_chunks: list[str]
    stream_io: dict[str, int | float]
    reader_errors: list[BaseException]
    stdout_thread: threading.Thread
    stderr_thread: threading.Thread
    abandon: Callable[[], None]


def start_turn_streams(
    process: subprocess.Popen[str],
    output_processor: harness_transport._OutputProcessor,
    *,
    harness_name: str,
    stream_callback: harness_types.StreamCallback | None,
    on_final_result: Callable[[], None] | None,
    on_observed_model: Callable[[str], None] | None,
) -> TurnStreams:
    """Start synchronized pipe readers whose callbacks can be quiesced at exit."""
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stream_io: dict[str, int | float] = {
        "stdout_chunks": 0,
        "stderr_chunks": 0,
        "stdout_chars": 0,
        "stderr_chars": 0,
        "callback_seconds": 0.0,
    }
    reader_errors: list[BaseException] = []
    final_result_fired = threading.Event()
    observed_model_fired = threading.Event()
    # Quiescence guard for abandoned readers: a reader that outlives the
    # bounded join still holds the stream callback, hooks, and output
    # state, so a late write from a pipe-holding descendant could fire
    # them after this turn has finished. Every per-chunk side effect runs
    # under this lock behind an abandonment check, and abandonment is
    # flipped under the same lock, so once ``abandon_readers`` returns no
    # reader can start a new side effect and any in-flight one is done.
    side_effect_lock = threading.Lock()
    abandoned = threading.Event()

    def abandon_readers() -> None:
        with side_effect_lock:
            abandoned.set()

    def read_stream(
        stream_name: harness_types.StreamName,
        chunks: list[str],
        pipe: IO[str],
    ) -> None:
        try:
            for chunk in iter(pipe.readline, ""):
                with side_effect_lock:
                    if abandoned.is_set():
                        break
                    processed_chunk = output_processor.process_chunk(
                        stream_name,
                        chunk,
                    )
                    if (
                        stream_name == "stdout"
                        and not output_processor.final_result_seen
                    ):
                        # A protocol request boundary re-arms the hook;
                        # duplicate result records within one request
                        # remain suppressed by the set Event below.
                        final_result_fired.clear()
                    # Only stdout parsing can flip final_result_seen, so
                    # this branch runs on a single thread and the Event
                    # guard makes the hook fire exactly once.
                    if (
                        stream_name == "stdout"
                        and on_final_result is not None
                        and not final_result_fired.is_set()
                        and output_processor.final_result_seen
                    ):
                        final_result_fired.set()
                        with contextlib.suppress(Exception):
                            on_final_result()
                    if (
                        stream_name == "stdout"
                        and on_observed_model is not None
                        and not observed_model_fired.is_set()
                    ):
                        observed = output_processor.observed_model
                        if observed is not None:
                            observed_model_fired.set()
                            with contextlib.suppress(Exception):
                                on_observed_model(observed)
                    if not processed_chunk:
                        continue
                    chunks.append(processed_chunk)
                    chunks_key = f"{stream_name}_chunks"
                    chars_key = f"{stream_name}_chars"
                    stream_io[chunks_key] = int(stream_io[chunks_key]) + 1
                    stream_io[chars_key] = int(stream_io[chars_key]) + len(
                        processed_chunk
                    )
                    if stream_callback is not None:
                        callback_started = time.monotonic()
                        try:
                            stream_callback(stream_name, processed_chunk)
                        except Exception as error:
                            reader_errors.append(error)
                        finally:
                            stream_io["callback_seconds"] = float(
                                stream_io["callback_seconds"]
                            ) + (time.monotonic() - callback_started)
        except Exception as error:
            reader_errors.append(error)
        finally:
            pipe.close()

    if process.stdout is None or process.stderr is None:
        raise harness_types.HarnessError(
            f"{harness_name} did not expose stdout/stderr pipes"
        )

    stdout_thread = threading.Thread(
        target=read_stream,
        args=("stdout", stdout_chunks, process.stdout),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=read_stream,
        args=("stderr", stderr_chunks, process.stderr),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    return TurnStreams(
        stdout_chunks,
        stderr_chunks,
        stream_io,
        reader_errors,
        stdout_thread,
        stderr_thread,
        abandon_readers,
    )
