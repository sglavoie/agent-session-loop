"""Keyboard controls for interactive text runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import select
import sys
import termios
import threading
import tty
from typing import Any, Literal, TextIO


RunControlActionName = Literal["request", "cancel", "consumed"]
ActionCallback = Callable[["RunControlAction"], None]

HELP_KEY = "h"
STOP_AFTER_CURRENT_KEY = "p"
_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class RunControlAction:
    """State transition caused by a run-control key or lifecycle checkpoint."""

    action: RunControlActionName
    stop_after_current: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "stop_after_current": self.stop_after_current,
        }


class RunControlState:
    """Thread-safe stop-after-current state shared with the runner."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_after_current = False

    @property
    def stop_after_current(self) -> bool:
        with self._lock:
            return self._stop_after_current

    def snapshot(self) -> dict[str, bool]:
        return {"stop_after_current": self.stop_after_current}

    def toggle_stop_after_current(self) -> RunControlAction:
        with self._lock:
            self._stop_after_current = not self._stop_after_current
            return RunControlAction(
                action="request" if self._stop_after_current else "cancel",
                stop_after_current=self._stop_after_current,
            )

    def consume_stop_after_current(self) -> RunControlAction | None:
        with self._lock:
            if not self._stop_after_current:
                return None
            self._stop_after_current = False
            return RunControlAction(action="consumed", stop_after_current=False)

    def help_text(self) -> str:
        status = "on" if self.stop_after_current else "off"
        return (
            "controls: "
            f"{STOP_AFTER_CURRENT_KEY} toggle stop-after-current "
            f"(currently {status}); "
            f"{HELP_KEY} show help"
        )


def _local_timestamp(moment: datetime | None = None) -> str:
    value = datetime.now().astimezone() if moment is None else moment.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _stream_is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
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


def _format_toggle_line(action: RunControlAction) -> str:
    if action.action == "request":
        return "control: stop-after-current requested; press p to cancel"
    if action.action == "cancel":
        return "control: stop-after-current canceled"
    return "control: stop-after-current consumed"


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
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        timestamp: Callable[[], str] = _local_timestamp,
    ) -> None:
        self.enabled = enabled
        self.state = RunControlState() if state is None else state
        self._stdin = stdin
        self._stderr = stderr
        self._on_action = on_action
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
    ) -> "RunControlListener":
        return cls(
            enabled=should_enable_run_controls(args, stdin=stdin, stderr=stderr),
            state=state,
            stdin=stdin,
            stderr=stderr,
            on_action=on_action,
        )

    def __enter__(self) -> "RunControlListener":
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
            except Exception:
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

    def handle_key(self, key: str) -> RunControlAction | None:
        if key == STOP_AFTER_CURRENT_KEY:
            action = self.state.toggle_stop_after_current()
            self._emit_line(_format_toggle_line(action))
            self._notify_action(action)
            return action
        if key == HELP_KEY:
            self._emit_line(self.state.help_text())
        return None

    def _listen(self) -> None:
        while not self._stop.is_set():
            fd = self._fd
            if fd is None:
                return
            try:
                readable, _, _ = select.select([fd], [], [], self._poll_interval)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                raw_key = self._stdin.read(1)
            except (OSError, ValueError):
                return
            if not raw_key:
                continue
            if isinstance(raw_key, bytes):
                key = raw_key.decode("utf-8", errors="ignore")
            else:
                key = raw_key
            if key:
                self.handle_key(key[0])

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
        except (OSError, ValueError, termios.error):
            pass
        finally:
            self._fd = None
            self._original_settings = None
