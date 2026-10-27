"""Serialized stderr output and the inline tail sharing that output sink."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import tail

#: How long to wait for the inline tail follower to stop. A run must
#: never end late because one would not stop.
INLINE_TAIL_JOIN_TIMEOUT_SECONDS = 1.0
#: How much of the active session's log the inline tail replays when it starts,
#: so enabling it mid-turn shows context rather than the next line only.
INLINE_TAIL_INITIAL_LOG_LINES = 40


def _local_timestamp(moment: datetime | None = None) -> str:
    if moment is None:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    try:
        value = moment.astimezone()
    except OverflowError, ValueError:
        # A persisted stamp the local zone cannot hold keeps its own offset.
        value = moment
    return value.strftime("%Y-%m-%d %H:%M:%S")


class _CoordinatedStderrStream:
    """Line-buffering stream proxy that shares the run's output lock.

    Handed to the collaborators that write the terminal on their own schedule
    — the control listener and the inline tail follower — because both accept
    a file-like sink and would otherwise default to the real ``sys.stderr``,
    which is a second unserialized writer racing the coordinator for a line.
    """

    def __init__(self, coordinator: _StderrOutputCoordinator) -> None:
        self._coordinator = coordinator

    def write(self, text: str) -> int:
        return self._coordinator.write_unprefixed(text)

    def flush(self) -> None:
        self._coordinator.flush_thread()

    def isatty(self) -> bool:
        return self._coordinator.isatty()

    def fileno(self) -> int:
        return self._coordinator.fileno()


class _StderrOutputCoordinator:
    """Shared stderr output path for progress, controls, status, and tails.

    The only holder of ``sys.stderr`` in the run's live output path. An
    explicit ``stream`` replaces it for tests; ``None`` means the process's own
    handle, resolved per write rather than captured at construction so a
    redirection installed after the run started is still honoured.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        stream: TextIO | None = None,
    ) -> None:
        self.enabled = enabled
        self._stream = stream
        self._lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._thread_buffers: dict[int, str] = {}
        self._stream_proxy = _CoordinatedStderrStream(self)

    @property
    def stream(self) -> _CoordinatedStderrStream:
        return self._stream_proxy

    def _target_stream(self) -> TextIO:
        return sys.stderr if self._stream is None else self._stream

    def isatty(self) -> bool:
        isatty = getattr(self._target_stream(), "isatty", None)
        if isatty is None:
            return False
        try:
            return bool(isatty())
        except OSError, ValueError:
            return False

    def fileno(self) -> int:
        return self._target_stream().fileno()

    def emit_line(self, message: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                print(
                    f"[{_local_timestamp()}] {message}",
                    file=self._target_stream(),
                    flush=True,
                )
            except BrokenPipeError, OSError:
                # The sink reader is gone (e.g. an escalating helper exited
                # and abandoned this run's pipe). Disable the sink for the
                # rest of the run; durable truth stays in state.json /
                # events.jsonl / plan files.
                self.enabled = False

    def emit_lines(self, messages: list[str]) -> None:
        if not self.enabled:
            return
        with self._lock:
            try:
                for message in messages:
                    print(
                        f"[{_local_timestamp()}] {message}",
                        file=self._target_stream(),
                        flush=True,
                    )
            except BrokenPipeError, OSError:
                self.enabled = False

    def write_unprefixed(self, text: str) -> int:
        if not self.enabled:
            return len(text)

        thread_id = threading.get_ident()
        complete_lines: list[str] = []
        with self._buffer_lock:
            buffered = self._thread_buffers.get(thread_id, "") + text
            while "\n" in buffered:
                line, buffered = buffered.split("\n", 1)
                complete_lines.append(line + "\n")
            if buffered:
                self._thread_buffers[thread_id] = buffered
            else:
                self._thread_buffers.pop(thread_id, None)

        if complete_lines:
            with self._lock:
                try:
                    stream = self._target_stream()
                    for line in complete_lines:
                        stream.write(line)
                    stream.flush()
                except BrokenPipeError, OSError:
                    self.enabled = False
        return len(text)

    def flush_thread(self) -> None:
        if not self.enabled:
            return

        thread_id = threading.get_ident()
        with self._buffer_lock:
            buffered = self._thread_buffers.pop(thread_id, "")
        with self._lock:
            try:
                stream = self._target_stream()
                if buffered:
                    stream.write(buffered)
                stream.flush()
            except BrokenPipeError, OSError:
                self.enabled = False


class _InlineTailController:
    """Owns the optional in-process tail follower for a live run."""

    def __init__(
        self,
        *,
        plan_dir: Path,
        state_dir: str | Path | None,
        output: _StderrOutputCoordinator,
    ) -> None:
        self._plan_dir = plan_dir
        self._state_dir = state_dir
        self._output = output
        self._lock = threading.Lock()
        self._follower: tail.TailFollower | None = None

    def enable(self) -> None:
        with self._lock:
            if self._follower is not None and self._follower.running:
                return
            self._follower = tail.TailFollower(
                self._plan_dir,
                state_dir=self._state_dir,
                out=self._output.stream,
                include_events=False,
                initial_log_lines=INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
            )
            self._follower.start()

    def disable(self) -> None:
        self._stop_current()

    def stop(self) -> None:
        self._stop_current()

    def _stop_current(self) -> None:
        with self._lock:
            follower = self._follower
            self._follower = None

        if follower is None:
            return
        follower.stop()
        try:
            stopped = follower.join(timeout=INLINE_TAIL_JOIN_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001
            self._output.emit_line(f"live tail stopped with error: {error}")
            return
        if not stopped:
            self._output.emit_line("live tail did not stop before cleanup timeout")
