"""Value types and key bindings shared by the run-control state and listener."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

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
