"""Keyboard controls for interactive text runs."""

from __future__ import annotations

import select
import sys
import termios
import threading
import tty
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, TextIO

HarnessName = Literal["codex", "claude", "copilot"]
OverrideOperationName = Literal["unchanged", "set", "clear"]
ImplementationOverrideField = Literal["model", "effort"]
RunControlActionName = Literal[
    "request",
    "cancel",
    "consumed",
    "abort-requested",
    "abort-consumed",
    "status",
    "tail-enabled",
    "tail-disabled",
    "harness-selected",
    "implementation-model-updated",
]
ActionCallback = Callable[["RunControlAction"], None]
StatusCallback = Callable[["RunControlAction"], None]

HELP_KEY = "h"
STATUS_SNAPSHOT_KEY = "s"
TAIL_TOGGLE_KEY = "t"
STOP_AFTER_CURRENT_KEY = "p"
HARNESS_SELECTION_KEYS: dict[str, HarnessName] = {
    "1": "codex",
    "2": "claude",
    "3": "copilot",
}
_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class OverrideOperation:
    """Explicit set-or-clear operation for one implementation override."""

    operation: OverrideOperationName
    value: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in ("unchanged", "set", "clear"):
            raise ValueError(f"unknown override operation: {self.operation}")
        if self.operation == "set":
            if not isinstance(self.value, str) or self.value == "":
                raise ValueError("set override operations require a non-empty value")
            return
        if self.value is not None:
            raise ValueError(
                f"{self.operation} override operations must not carry a value"
            )

    @classmethod
    def unchanged(cls) -> OverrideOperation:
        return cls("unchanged")

    @classmethod
    def set(cls, value: str) -> OverrideOperation:
        return cls("set", value)

    @classmethod
    def clear(cls) -> OverrideOperation:
        return cls("clear")

    def to_payload(self) -> dict[str, str | None]:
        return {
            "operation": self.operation,
            "value": self.value,
        }


UNCHANGED_OVERRIDE_OPERATION = OverrideOperation.unchanged()


def _copy_implementation_overrides(
    overrides: Mapping[HarnessName, Mapping[ImplementationOverrideField, str]],
) -> dict[HarnessName, dict[ImplementationOverrideField, str]]:
    return {harness: dict(values) for harness, values in overrides.items()}


def _freeze_implementation_overrides(
    overrides: Mapping[HarnessName, Mapping[ImplementationOverrideField, str]],
) -> Mapping[HarnessName, Mapping[ImplementationOverrideField, str]]:
    return MappingProxyType(
        {
            harness: MappingProxyType(dict(values))
            for harness, values in overrides.items()
        }
    )


@dataclass(frozen=True)
class RunControlAction:
    """State transition caused by a run-control key or lifecycle checkpoint."""

    action: RunControlActionName
    stop_after_current: bool
    tail_enabled: bool = False
    abort_requested: bool = False
    abort_grace_seconds: int | None = None
    abort_reason: str | None = None
    next_harness: HarnessName | None = None
    target_harness: HarnessName | None = None
    changed_fields: tuple[ImplementationOverrideField, ...] = ()
    implementation_overrides: (
        Mapping[HarnessName, Mapping[ImplementationOverrideField, str]] | None
    ) = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.implementation_overrides is not None:
            object.__setattr__(
                self,
                "implementation_overrides",
                _freeze_implementation_overrides(self.implementation_overrides),
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "stop_after_current": self.stop_after_current,
            "tail_enabled": self.tail_enabled,
            "abort_requested": self.abort_requested,
            "abort_grace_seconds": self.abort_grace_seconds,
            "abort_reason": self.abort_reason,
            "next_harness": self.next_harness,
            "target_harness": self.target_harness,
            "changed_fields": list(self.changed_fields),
            "implementation_overrides": (
                None
                if self.implementation_overrides is None
                else _copy_implementation_overrides(self.implementation_overrides)
            ),
        }


class RunControlState:
    """Thread-safe runtime control state shared with the runner."""

    def __init__(
        self,
        *,
        current_harness: HarnessName | None = None,
        tail_enabled: bool = False,
        next_harness: HarnessName | None = None,
        implementation_overrides: dict[
            HarnessName, dict[ImplementationOverrideField, str]
        ]
        | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._stop_after_current = False
        self._abort_turn_requested = False
        self._abort_grace_seconds: int | None = None
        self._abort_reason: str | None = None
        self._tail_enabled = tail_enabled
        self._current_harness = current_harness
        self._next_harness = next_harness
        self._implementation_overrides = _copy_implementation_overrides(
            implementation_overrides or {}
        )

    @property
    def stop_after_current(self) -> bool:
        with self._lock:
            return self._stop_after_current

    @property
    def abort_turn_requested(self) -> bool:
        with self._lock:
            return self._abort_turn_requested

    @property
    def abort_grace_seconds(self) -> int | None:
        with self._lock:
            return self._abort_grace_seconds

    @property
    def abort_reason(self) -> str | None:
        with self._lock:
            return self._abort_reason

    @property
    def tail_enabled(self) -> bool:
        with self._lock:
            return self._tail_enabled

    @property
    def next_harness(self) -> HarnessName | None:
        with self._lock:
            return self._next_harness

    @property
    def implementation_overrides(
        self,
    ) -> dict[HarnessName, dict[ImplementationOverrideField, str]]:
        with self._lock:
            return _copy_implementation_overrides(self._implementation_overrides)

    def _action(
        self,
        action: RunControlActionName,
        *,
        target_harness: HarnessName | None = None,
        changed_fields: tuple[ImplementationOverrideField, ...] = (),
    ) -> RunControlAction:
        return RunControlAction(
            action=action,
            stop_after_current=self._stop_after_current,
            tail_enabled=self._tail_enabled,
            abort_requested=self._abort_turn_requested,
            abort_grace_seconds=self._abort_grace_seconds,
            abort_reason=self._abort_reason,
            next_harness=self._next_harness,
            target_harness=target_harness,
            changed_fields=changed_fields,
            implementation_overrides=_copy_implementation_overrides(
                self._implementation_overrides
            ),
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stop_after_current": self._stop_after_current,
                "abort_turn_requested": self._abort_turn_requested,
                "abort_grace_seconds": self._abort_grace_seconds,
                "abort_reason": self._abort_reason,
                "tail_enabled": self._tail_enabled,
                "next_harness": self._next_harness,
                "implementation_overrides": _copy_implementation_overrides(
                    self._implementation_overrides
                ),
            }

    def toggle_stop_after_current(self) -> RunControlAction:
        with self._lock:
            self._stop_after_current = not self._stop_after_current
            return self._action("request" if self._stop_after_current else "cancel")

    def request_stop_after_current(self) -> RunControlAction | None:
        """Idempotently mark stop-after-current as requested.

        Returns the resulting action only when the flag flipped from
        unset to set. Returns ``None`` when stop-after-current was
        already requested so external producers cannot generate
        duplicate "request" events by repeating the same command.
        """
        with self._lock:
            if self._stop_after_current:
                return None
            self._stop_after_current = True
            return self._action("request")

    def consume_stop_after_current(self) -> RunControlAction | None:
        with self._lock:
            if not self._stop_after_current:
                return None
            self._stop_after_current = False
            return self._action("consumed")

    def request_abort_turn(
        self,
        *,
        grace_seconds: int | None = None,
        reason: str | None = None,
    ) -> RunControlAction | None:
        """Idempotently mark the in-flight turn as abort-requested.

        Returns ``None`` when an abort is already pending, so a repeated
        command cannot restart the grace window or emit a duplicate
        "requested" event.
        """
        with self._lock:
            if self._abort_turn_requested:
                return None
            self._abort_turn_requested = True
            self._abort_grace_seconds = grace_seconds
            self._abort_reason = reason
            return self._action("abort-requested")

    def consume_abort_turn(self) -> RunControlAction | None:
        with self._lock:
            if not self._abort_turn_requested:
                return None
            # Built before the reset so the consumer still sees the grace and
            # reason it has to act on.
            action = self._action("abort-consumed")
            self._abort_turn_requested = False
            self._abort_grace_seconds = None
            self._abort_reason = None
            return action

    def request_status_snapshot(self) -> RunControlAction:
        with self._lock:
            return self._action("status")

    def toggle_tail(self) -> RunControlAction:
        with self._lock:
            self._tail_enabled = not self._tail_enabled
            return self._action(
                "tail-enabled" if self._tail_enabled else "tail-disabled"
            )

    def select_harness(self, harness: HarnessName) -> RunControlAction | None:
        with self._lock:
            selected_harness = self._next_harness or self._current_harness
            if harness == selected_harness:
                return None
            self._next_harness = harness
            return self._action("harness-selected")

    def set_implementation_model(
        self,
        *,
        target_harness: HarnessName | None = None,
        model: OverrideOperation = UNCHANGED_OVERRIDE_OPERATION,
        effort: OverrideOperation = UNCHANGED_OVERRIDE_OPERATION,
    ) -> RunControlAction | None:
        """Apply model/effort override operations for one implementation harness.

        An omitted target is resolved while holding the state lock, after any
        earlier harness-selection transition. This preserves queue order when
        a drain applies multiple commands in sequence.
        """
        if model.operation == "unchanged" and effort.operation == "unchanged":
            raise ValueError(
                "at least one model or effort override operation is required"
            )

        with self._lock:
            resolved_harness = (
                target_harness or self._next_harness or self._current_harness
            )
            if resolved_harness is None:
                raise ValueError(
                    "an implementation harness is required to apply overrides"
                )
            if resolved_harness not in HARNESS_SELECTION_KEYS.values():
                raise ValueError(f"unknown implementation harness: {resolved_harness}")

            current = dict(self._implementation_overrides.get(resolved_harness, {}))
            updated = dict(current)
            changed_fields: list[ImplementationOverrideField] = []
            requested: tuple[
                tuple[ImplementationOverrideField, OverrideOperation], ...
            ] = (
                ("model", model),
                ("effort", effort),
            )
            for field_name, operation in requested:
                if operation.operation == "unchanged":
                    continue
                if operation.operation == "clear":
                    if field_name in updated:
                        del updated[field_name]
                        changed_fields.append(field_name)
                    continue
                assert operation.value is not None
                if updated.get(field_name) != operation.value:
                    updated[field_name] = operation.value
                    changed_fields.append(field_name)

            if not changed_fields:
                return None
            if updated:
                self._implementation_overrides[resolved_harness] = updated
            else:
                self._implementation_overrides.pop(resolved_harness, None)
            return self._action(
                "implementation-model-updated",
                target_harness=resolved_harness,
                changed_fields=tuple(changed_fields),
            )

    def help_text(self) -> str:
        snapshot = self.snapshot()
        stop_status = "on" if snapshot["stop_after_current"] else "off"
        tail_status = "on" if snapshot["tail_enabled"] else "off"
        next_harness = snapshot["next_harness"] or "unchanged"
        return (
            "controls: "
            f"{STOP_AFTER_CURRENT_KEY} toggle stop-after-current "
            f"(currently {stop_status}); "
            f"{STATUS_SNAPSHOT_KEY} show status; "
            f"{TAIL_TOGGLE_KEY} toggle live tail (currently {tail_status}); "
            "1 codex, 2 claude, 3 copilot select next harness "
            f"(currently {next_harness}); "
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
            action = self.state.select_harness(harness)
            if action is None:
                self._emit_line(_format_harness_already_selected_line(harness))
                return None
            self._emit_line(_format_harness_selection_line(action))
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
            except OSError, ValueError:
                return
            if not readable:
                continue
            try:
                raw_key = self._stdin.read(1)
            except OSError, ValueError:
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
        except OSError, ValueError, termios.error:
            pass
        finally:
            self._fd = None
            self._original_settings = None
