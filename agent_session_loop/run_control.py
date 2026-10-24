"""Keyboard controls for interactive text runs.

The value types live in :mod:`agent_session_loop.run_control_models` and the
thread-safe state in :mod:`agent_session_loop.run_control_state`; both are
re-exported here, so this module remains the public entry point.
"""

from __future__ import annotations

import codecs
import os
import select
import sys
import termios
import threading
import tty
from collections.abc import Callable
from datetime import datetime
from typing import Any, TextIO

from .run_control_models import (
    _POLL_INTERVAL_SECONDS,
)
from .run_control_models import (
    HARNESS_SELECTION_KEYS as HARNESS_SELECTION_KEYS,
)
from .run_control_models import (
    HELP_KEY as HELP_KEY,
)
from .run_control_models import (
    STATUS_SNAPSHOT_KEY as STATUS_SNAPSHOT_KEY,
)
from .run_control_models import (
    STOP_AFTER_CURRENT_KEY as STOP_AFTER_CURRENT_KEY,
)
from .run_control_models import (
    TAIL_TOGGLE_KEY as TAIL_TOGGLE_KEY,
)
from .run_control_models import (
    UNCHANGED_OVERRIDE_OPERATION as UNCHANGED_OVERRIDE_OPERATION,
)
from .run_control_models import (
    ActionCallback as ActionCallback,
)
from .run_control_models import (
    HarnessName as HarnessName,
)
from .run_control_models import (
    ImplementationOverrideField as ImplementationOverrideField,
)
from .run_control_models import (
    OverrideOperation as OverrideOperation,
)
from .run_control_models import (
    OverrideOperationName as OverrideOperationName,
)
from .run_control_models import (
    RunControlAction as RunControlAction,
)
from .run_control_models import (
    RunControlActionName as RunControlActionName,
)
from .run_control_models import (
    StatusCallback as StatusCallback,
)
from .run_control_state import RunControlState as RunControlState


def _local_timestamp(moment: datetime | None = None) -> str:
    value = datetime.now().astimezone() if moment is None else moment.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _stream_is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except OSError, ValueError:
        return False


def should_enable_run_controls(
    args: Any,
    *,
    stdin: TextIO = sys.stdin,
    stderr: TextIO = sys.stderr,
) -> bool:
    """Return whether keyboard controls may be enabled for a run invocation."""

    if getattr(args, "json", False):
        return False
    if getattr(args, "dry_run", False):
        return False
    return _stream_is_tty(stdin) and _stream_is_tty(stderr)


def _format_stop_toggle_line(action: RunControlAction) -> str:
    if action.action == "request":
        return "control: stop-after-current requested; press p to cancel"
    if action.action == "cancel":
        return "control: stop-after-current canceled"
    return "control: stop-after-current consumed"


def _format_status_snapshot_line(action: RunControlAction) -> str:
    stop_status = "on" if action.stop_after_current else "off"
    tail_status = "on" if action.tail_enabled else "off"
    next_harness = action.next_harness or "unchanged"
    return (
        "control status: "
        f"stop-after-current {stop_status}; "
        f"live tail {tail_status}; "
        f"next harness {next_harness}"
    )


def _format_tail_toggle_line(action: RunControlAction) -> str:
    if action.action == "tail-enabled":
        return "control: live tail enabled; press t to disable"
    return "control: live tail disabled"


def _format_harness_selection_line(action: RunControlAction) -> str:
    return f"control: next harness selected: {action.next_harness}"


def _format_harness_already_selected_line(harness: HarnessName) -> str:
    return f"control: next harness already {harness}"


class RunControlListener:
    """Background keyboard listener for text TTY runs."""

    def __init__(
        self,
        *,
        enabled: bool,
        state: RunControlState | None = None,
        stdin: TextIO = sys.stdin,
        stderr: TextIO = sys.stderr,
        on_action: ActionCallback | None = None,
        on_status: StatusCallback | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        timestamp: Callable[[], str] = _local_timestamp,
    ) -> None:
        self.enabled = enabled
        self.state = RunControlState() if state is None else state
        self._stdin = stdin
        self._stderr = stderr
        self._on_action = on_action
        self._on_status = on_status
        self._poll_interval = poll_interval
        self._timestamp = timestamp
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._original_settings: Any | None = None

    @classmethod
    def for_run(
        cls,
        args: Any,
        *,
        state: RunControlState | None = None,
        stdin: TextIO = sys.stdin,
        stderr: TextIO = sys.stderr,
        on_action: ActionCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> RunControlListener:
        return cls(
            enabled=should_enable_run_controls(args, stdin=stdin, stderr=stderr),
            state=state,
            stdin=stdin,
            stderr=stderr,
            on_action=on_action,
            on_status=on_status,
        )

    def __enter__(self) -> RunControlListener:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None

    def start(self) -> bool:
        """Start the listener if enabled; return False on any setup failure."""

        if not self.enabled:
            return False

        with self._lock:
            if self._thread is not None:
                return True
            try:
                fd = self._stdin.fileno()
                original_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                thread = threading.Thread(
                    target=self._listen,
                    name="agent-session-loop-run-control",
                    daemon=True,
                )
                self._fd = fd
                self._original_settings = original_settings
                self._thread = thread
                self._stop.clear()
                thread.start()
            except Exception:  # noqa: BLE001
                self._restore_terminal_locked()
                self._fd = None
                self._original_settings = None
                self._thread = None
                return False
            return True

    def stop(self) -> None:
        thread: threading.Thread | None
        with self._lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
            self._restore_terminal_locked()

        if thread is None or thread is threading.current_thread():
            return
        if thread.is_alive():
            thread.join(timeout=1.0)

    def consume_stop_after_current(self) -> RunControlAction | None:
        action = self.state.consume_stop_after_current()
        if action is not None:
            self._notify_action(action)
        return action

    def consume_abort_turn(self) -> RunControlAction | None:
        action = self.state.consume_abort_turn()
        if action is not None:
            self._notify_action(action)
        return action

    def handle_key(self, key: str) -> RunControlAction | None:
        if key == STOP_AFTER_CURRENT_KEY:
            action = self.state.toggle_stop_after_current()
            self._emit_line(_format_stop_toggle_line(action))
            self._notify_action(action)
            return action
        if key == STATUS_SNAPSHOT_KEY:
            action = self.state.request_status_snapshot()
            if self._on_status is None:
                self._emit_line(_format_status_snapshot_line(action))
            else:
                self._on_status(action)
            return action
        if key == TAIL_TOGGLE_KEY:
            action = self.state.toggle_tail()
            self._emit_line(_format_tail_toggle_line(action))
            self._notify_action(action)
            return action
        if key in HARNESS_SELECTION_KEYS:
            harness = HARNESS_SELECTION_KEYS[key]
            selection = self.state.select_harness(harness)
            if selection is None:
                self._emit_line(_format_harness_already_selected_line(harness))
                return None
            self._emit_line(_format_harness_selection_line(selection))
            self._notify_action(selection)
            return selection
        if key == HELP_KEY:
            self._emit_line(self.state.help_text())
        return None

    def _listen(self) -> None:
        # Read the descriptor directly rather than through ``self._stdin``: a
        # buffered ``read(1)`` pulls every pending byte into Python's buffer and
        # returns one character, after which ``select`` reports nothing left,
        # so the rest of a paste would wait for the next keypress.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        while not self._stop.is_set():
            fd = self._fd
            if fd is None:
                return
            try:
                readable, _, _ = select.select([fd], [], [], self._poll_interval)
            except OSError, ValueError:
                return
            if not readable:
                continue
            try:
                chunk = os.read(fd, 1024)
            except OSError, ValueError:
                return
            if not chunk:
                # End of input stays readable forever; looping on it would spin.
                return
            text = decoder.decode(chunk)
            # An escape sequence (an arrow key, say) arrives as one read; its
            # tail can contain digits that would otherwise select a harness.
            text = text.split("\x1b", 1)[0]
            for key in text:
                self.handle_key(key)

    def _emit_line(self, message: str) -> None:
        print(f"[{self._timestamp()}] {message}", file=self._stderr, flush=True)

    def _notify_action(self, action: RunControlAction) -> None:
        if self._on_action is None:
            return
        self._on_action(action)

    def _restore_terminal_locked(self) -> None:
        if self._fd is None or self._original_settings is None:
            return
        try:
            termios.tcsetattr(
                self._fd,
                termios.TCSADRAIN,
                self._original_settings,
            )
        except OSError, ValueError, termios.error:
            pass
        finally:
            self._fd = None
            self._original_settings = None
