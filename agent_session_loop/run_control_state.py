"""Thread-safe runtime control state shared with the runner."""

from __future__ import annotations

import threading
from typing import Any

from .run_control_models import (
    HARNESS_SELECTION_KEYS,
    HELP_KEY,
    STATUS_SNAPSHOT_KEY,
    STOP_AFTER_CURRENT_KEY,
    TAIL_TOGGLE_KEY,
    UNCHANGED_OVERRIDE_OPERATION,
    HarnessName,
    ImplementationOverrideField,
    OverrideOperation,
    RunControlAction,
    RunControlActionName,
    _copy_implementation_overrides,
)


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
