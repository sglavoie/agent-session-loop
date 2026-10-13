"""Workbench registry constants and record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    plans_errors,
    workbench_workspace,
)

REGISTRY_FILE_NAME = "workbench.json"
REGISTRY_SCHEMA_VERSION = 2
DEFAULT_PLANS_DIR_NAME = "agent-plans"

DEFAULT_ARCHIVE_RETENTION_DAYS = 30

CommitMode = Literal["per-session", "end"]
COMMIT_MODES: tuple[CommitMode, ...] = ("per-session", "end")

DiscoverySource = Literal[
    "state",
    "registry",
    "plan-root",
    "agent-plans-dir",
    "tmp-default",
    "share-default",
]
DISCOVERY_SOURCES: tuple[DiscoverySource, ...] = (
    "state",
    "registry",
    "plan-root",
    "agent-plans-dir",
    "tmp-default",
    "share-default",
)


class WorkbenchError(Exception):
    """Raised when the Workbench registry cannot be used safely."""


@dataclass(frozen=True)
class LaunchProfile:
    """Operator launch defaults for one pinned plan.

    All fields are optional. ``None`` means "no preference recorded";
    consumers should fall back to their normal defaults.
    """

    cwd: str | None = None
    harness: str | None = None
    harness_model: str | None = None
    effort: str | None = None
    timeout: int | None = None
    commit_mode: CommitMode | None = None
    review_harness: str | None = None
    review_harness_model: str | None = None
    max_review_fix_cycles: int | None = None
    unsafe_bypass: bool | None = None
    allow_not_loop_ready: bool | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "harness": self.harness,
            "harness_model": self.harness_model,
            "effort": self.effort,
            "timeout": self.timeout,
            "commit_mode": self.commit_mode,
            "review_harness": self.review_harness,
            "review_harness_model": self.review_harness_model,
            "max_review_fix_cycles": self.max_review_fix_cycles,
            "unsafe_bypass": self.unsafe_bypass,
            "allow_not_loop_ready": self.allow_not_loop_ready,
            "note": self.note,
        }


@dataclass(frozen=True)
class RegistryPin:
    """One pinned plan with optional label and launch profile."""

    plan_dir: str
    label: str | None = None
    profile: LaunchProfile = field(default_factory=LaunchProfile)

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": self.plan_dir,
            "label": self.label,
            "profile": self.profile.to_payload(),
        }


@dataclass(frozen=True)
class WorkbenchRegistry:
    """Loaded Workbench registry plus any recoverable warnings."""

    schema_version: int = REGISTRY_SCHEMA_VERSION
    pins: tuple[RegistryPin, ...] = ()
    plan_roots: tuple[str, ...] = ()
    archive_retention_days: int = DEFAULT_ARCHIVE_RETENTION_DAYS
    path: Path | None = None
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        pins_payload: list[dict[str, Any]] = []
        for pin in self.pins:
            payload = pin.to_payload()
            try:
                workspace = workbench_workspace._workspace_projection(pin.plan_dir)
            except plans_errors.PlanError, OSError, ValueError:
                workspace = {}
            if workspace:
                payload.update(workspace)
                payload["workspace"] = dict(workspace)
            pins_payload.append(payload)
        return {
            "schema_version": self.schema_version,
            "pins": pins_payload,
            "plan_roots": list(self.plan_roots),
            "archive_retention_days": self.archive_retention_days,
            "path": str(self.path) if self.path is not None else None,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DiscoveredPlan:
    """One plan directory discovered by the Workbench.

    ``sources`` lists every discovery source that surfaced this plan, in
    the canonical order defined by :data:`DISCOVERY_SOURCES`. ``label``
    and ``profile`` come from the registry pin when present.
    """

    plan_dir: Path
    sources: tuple[DiscoverySource, ...] = ()
    label: str | None = None
    profile: LaunchProfile | None = None
    exists: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "sources": list(self.sources),
            "label": self.label,
            "profile": self.profile.to_payload() if self.profile is not None else None,
            "exists": self.exists,
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """Output of :func:`discover_plans` with recoverable warnings."""

    plans: tuple[DiscoveredPlan, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "plans": [plan.to_payload() for plan in self.plans],
            "warnings": list(self.warnings),
        }
