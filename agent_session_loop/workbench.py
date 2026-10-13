"""Workbench registry and plan discovery.

The Workbench surfaces ASL plans through a local web UI. Registry data
holds operator-facing affordances that the rest of the runner does not
need: pinned plans, optional labels, configured plan roots, and per-plan
launch profiles. Plan discovery scans ASL state, the registry, supplied
plan roots, and a small set of well-known directories to produce a
deduplicated list of plan directories ready for the Workbench UI.

The registry is stored as JSON under the ASL config directory and is
schema-versioned. Malformed entries are reported as recoverable warnings
on the loaded payload instead of aborting the load, so the Workbench can
still render usable state when the file has been edited by hand.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from . import (
    assurance_report,
    cli_trace,
    config,
    durability,
    events,
    harnesses,
    plans,
    qna,
    responses,
    review_continuation,
    run_contention,
    state,
    state_abandonment,
    state_locks,
    state_payloads,
    workbench_recovery,
    workbench_workspace,
)

FINAL_REVIEW_FILE_NAME = "final-review.md"
FINAL_REVIEW_ARTIFACT_FILE_NAME = "final-review.json"
LAST_MESSAGE_PREVIEW_CHARS = 280


def _workspace_launch_profile(
    plan_dir: Path, profile: LaunchProfile
) -> tuple[LaunchProfile, dict[str, Any]]:
    """Bind a registered Workbench launch to its prepared workspace root."""
    workspace = workbench_workspace._workspace_projection(plan_dir)
    bound_root = workspace.get("workspace_root")
    if not isinstance(bound_root, str) or not bound_root:
        return profile, workspace
    if profile.cwd is not None:
        try:
            requested = plans.canonicalize_path(profile.cwd)
            bound = plans.canonicalize_path(bound_root)
        except (OSError, ValueError) as error:
            raise LaunchError(
                LAUNCH_REASON_INVALID_PROFILE,
                f"invalid workspace root in launch profile: {error}",
            ) from error
        if requested != bound:
            raise LaunchError(
                LAUNCH_REASON_INVALID_PROFILE,
                "launch cwd does not match the prepared workspace binding; "
                "use `asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE "
                "--rebind-workspace` to change it atomically",
            )
    return replace(profile, cwd=bound_root), workspace


PrimaryState = Literal[
    "invalid",
    "verifying",
    "running",
    "paused-for-reply",
    "failed",
    "stale",
    "complete",
    "recent",
    "ready",
]
PRIMARY_STATES: tuple[PrimaryState, ...] = (
    "invalid",
    "verifying",
    "running",
    "paused-for-reply",
    "failed",
    "stale",
    "complete",
    "recent",
    "ready",
)


RecommendedAction = Literal[
    "watch",
    "reply",
    "inspect-failure",
    "unlock",
    "view",
    "run",
    "repair-plan",
]
RECOMMENDED_ACTIONS: tuple[RecommendedAction, ...] = (
    "watch",
    "reply",
    "inspect-failure",
    "unlock",
    "view",
    "run",
    "repair-plan",
)


UrgencyGroup = Literal["needs-action", "in-progress", "ready", "complete"]
URGENCY_GROUPS: tuple[UrgencyGroup, ...] = (
    "needs-action",
    "in-progress",
    "ready",
    "complete",
)


LastActivitySource = Literal[
    "active-verification",
    "active-turn",
    "turn-record",
    "run-finished",
    "run-started",
    "lock-created",
    "progress-journal",
]
LAST_ACTIVITY_SOURCES: tuple[LastActivitySource, ...] = (
    "active-verification",
    "active-turn",
    "turn-record",
    "run-finished",
    "run-started",
    "lock-created",
    "progress-journal",
)


# Lower rank == surface higher in the triage queue.
URGENCY_RANK_BY_STATE: dict[PrimaryState, int] = {
    "paused-for-reply": 10,
    "failed": 20,
    "stale": 30,
    "invalid": 40,
    "verifying": 50,
    "running": 60,
    "recent": 70,
    "ready": 80,
    "complete": 90,
}


URGENCY_GROUP_BY_STATE: dict[PrimaryState, UrgencyGroup] = {
    "paused-for-reply": "needs-action",
    "failed": "needs-action",
    "stale": "needs-action",
    "invalid": "needs-action",
    "verifying": "in-progress",
    "running": "in-progress",
    "recent": "ready",
    "ready": "ready",
    "complete": "complete",
}


ACTIVITY_LABEL_BY_STATE: dict[PrimaryState, str] = {
    "invalid": "Plan unreadable",
    "verifying": "Verifying",
    "running": "Active turn",
    "paused-for-reply": "Reply needed",
    "failed": "Last run failed",
    "stale": "Stale state",
    "complete": "All sessions complete",
    "recent": "Run finished, sessions remain",
    "ready": "Ready to run",
}


# ``recent`` also represents an eligible review continuation after all of its
# repair sessions have completed.  Its normal label would incorrectly imply
# that the queue still contains pending sessions in that zero-pending case.
REVIEW_CONTINUATION_ACTIVITY_LABEL = "Review continuation ready"


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
            except plans.PlanError, OSError, ValueError:
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


def default_registry_path() -> Path:
    """Return the canonical path of the Workbench registry file."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        base = Path(xdg_config_home).expanduser().resolve()
    else:
        base = Path("~/.config").expanduser().resolve()
    return base / config.APP_NAME / REGISTRY_FILE_NAME


def _coerce_registry_path(path: str | Path | None) -> Path:
    if path is None:
        return default_registry_path()
    return Path(path).expanduser().resolve()


def _canonical_plan_dir(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return str(plans.canonicalize_path(text))
    except OSError, ValueError:
        return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _coerce_max_review_fix_cycles(
    value: Any, *, warnings: list[str], context: str
) -> int | None:
    """Return a persisted review-fix cap only when it is non-negative."""
    cap = _coerce_optional_int(value)
    if cap is not None and cap < 0:
        warnings.append(f"{context}: ignoring negative max_review_fix_cycles {cap!r}")
        return None
    return cap


def _coerce_harness(value: Any, *, warnings: list[str], context: str) -> str | None:
    text = _coerce_optional_str(value)
    if text is None:
        return None
    if text not in harnesses.HARNESS_NAMES:
        warnings.append(f"{context}: ignoring unsupported harness {text!r}")
        return None
    return text


def _coerce_commit_mode(
    value: Any, *, warnings: list[str], context: str
) -> CommitMode | None:
    text = _coerce_optional_str(value)
    if text is None:
        return None
    if text not in COMMIT_MODES:
        warnings.append(f"{context}: ignoring unsupported commit_mode {text!r}")
        return None
    return text  # type: ignore[return-value]


def _coerce_profile(
    raw: Any,
    *,
    schema_version: int,
    warnings: list[str],
    context: str,
) -> LaunchProfile:
    if raw is None:
        return LaunchProfile()
    if not isinstance(raw, Mapping):
        warnings.append(f"{context}: profile must be an object; ignoring")
        return LaunchProfile()
    if "model" in raw and "harness_model" not in raw:
        warnings.append(
            f"{context}: ignoring legacy profile.model from registry schema "
            f"v{schema_version}; "
            "use harness_model and review_harness_model"
        )
    return LaunchProfile(
        cwd=_coerce_optional_str(raw.get("cwd")),
        harness=_coerce_harness(raw.get("harness"), warnings=warnings, context=context),
        harness_model=_coerce_optional_str(raw.get("harness_model")),
        effort=_coerce_optional_str(raw.get("effort")),
        timeout=_coerce_optional_int(raw.get("timeout")),
        commit_mode=_coerce_commit_mode(
            raw.get("commit_mode"), warnings=warnings, context=context
        ),
        review_harness=_coerce_harness(
            raw.get("review_harness"), warnings=warnings, context=context
        ),
        review_harness_model=_coerce_optional_str(raw.get("review_harness_model")),
        max_review_fix_cycles=_coerce_max_review_fix_cycles(
            raw.get("max_review_fix_cycles"), warnings=warnings, context=context
        ),
        unsafe_bypass=_coerce_optional_bool(raw.get("unsafe_bypass")),
        allow_not_loop_ready=_coerce_optional_bool(raw.get("allow_not_loop_ready")),
        note=_coerce_optional_str(raw.get("note")),
    )


def _coerce_pin(
    raw: Any, *, schema_version: int, warnings: list[str], index: int
) -> RegistryPin | None:
    context = f"pin[{index}]"
    if not isinstance(raw, Mapping):
        warnings.append(f"{context}: entry must be an object; skipping")
        return None
    plan_dir = _canonical_plan_dir(raw.get("plan_dir"))
    if plan_dir is None:
        warnings.append(f"{context}: missing or invalid plan_dir; skipping")
        return None
    label = _coerce_optional_str(raw.get("label"))
    profile = _coerce_profile(
        raw.get("profile"),
        schema_version=schema_version,
        warnings=warnings,
        context=context,
    )
    return RegistryPin(plan_dir=plan_dir, label=label, profile=profile)


def _coerce_plan_roots(raw: Any, *, warnings: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        warnings.append("plan_roots: must be a list; ignoring")
        return ()
    roots: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        canonical = _canonical_plan_dir(entry)
        if canonical is None:
            warnings.append(f"plan_roots[{index}]: invalid path {entry!r}; skipping")
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        roots.append(canonical)
    return tuple(roots)


def _coerce_schema_version(raw: Any, *, warnings: list[str]) -> int:
    if raw is None:
        warnings.append("schema_version: missing; assuming current version")
        return REGISTRY_SCHEMA_VERSION
    if isinstance(raw, bool) or not isinstance(raw, int):
        warnings.append(
            f"schema_version: expected integer, got {type(raw).__name__}; "
            "assuming current version"
        )
        return REGISTRY_SCHEMA_VERSION
    if raw < 1:
        warnings.append(
            f"schema_version: invalid value {raw!r}; assuming current version"
        )
        return REGISTRY_SCHEMA_VERSION
    if raw > REGISTRY_SCHEMA_VERSION:
        warnings.append(
            f"schema_version {raw} is newer than supported "
            f"v{REGISTRY_SCHEMA_VERSION}; reading best-effort"
        )
    return raw


def _coerce_retention_days(raw: Any, *, warnings: list[str]) -> int:
    if raw is None:
        # Old schema-v1 registries predate archive support; default silently.
        return DEFAULT_ARCHIVE_RETENTION_DAYS
    if isinstance(raw, bool) or not isinstance(raw, int):
        warnings.append(
            "archive_retention_days: expected positive integer, got "
            f"{type(raw).__name__}; using default "
            f"{DEFAULT_ARCHIVE_RETENTION_DAYS}"
        )
        return DEFAULT_ARCHIVE_RETENTION_DAYS
    if raw < 1:
        warnings.append(
            f"archive_retention_days: invalid value {raw!r}; using default "
            f"{DEFAULT_ARCHIVE_RETENTION_DAYS}"
        )
        return DEFAULT_ARCHIVE_RETENTION_DAYS
    return raw


def load_registry(path: str | Path | None = None) -> WorkbenchRegistry:
    """Load the Workbench registry, returning an empty one when absent.

    Malformed entries are recorded as warnings on the returned registry
    rather than raising. Outright JSON parse failures or unreadable
    files raise :class:`WorkbenchError` so callers can decide whether to
    surface a hard error.
    """
    registry_path = _coerce_registry_path(path)
    if not registry_path.exists():
        return WorkbenchRegistry(path=registry_path)

    try:
        raw_text = registry_path.read_text(encoding="utf-8")
    except OSError as error:
        raise WorkbenchError(
            f"cannot read workbench registry {registry_path}: {error}"
        ) from error

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise WorkbenchError(
            f"invalid JSON in workbench registry {registry_path}: {error}"
        ) from error

    if not isinstance(raw, Mapping):
        raise WorkbenchError(
            f"workbench registry {registry_path} must contain a JSON object"
        )

    warnings: list[str] = []
    schema_version = _coerce_schema_version(
        raw.get("schema_version"), warnings=warnings
    )

    raw_pins = raw.get("pins", [])
    if raw_pins is None:
        raw_pins = []
    if not isinstance(raw_pins, Sequence) or isinstance(raw_pins, (str, bytes)):
        warnings.append("pins: must be a list; ignoring")
        raw_pins = []

    pins: list[RegistryPin] = []
    seen_plan_dirs: set[str] = set()
    for index, raw_pin in enumerate(raw_pins):
        pin = _coerce_pin(
            raw_pin,
            schema_version=schema_version,
            warnings=warnings,
            index=index,
        )
        if pin is None:
            continue
        if pin.plan_dir in seen_plan_dirs:
            warnings.append(
                f"pin[{index}]: duplicate plan_dir {pin.plan_dir!r}; "
                "keeping first occurrence"
            )
            continue
        seen_plan_dirs.add(pin.plan_dir)
        pins.append(pin)

    plan_roots = _coerce_plan_roots(raw.get("plan_roots"), warnings=warnings)
    archive_retention_days = _coerce_retention_days(
        raw.get("archive_retention_days"), warnings=warnings
    )

    return WorkbenchRegistry(
        schema_version=schema_version,
        pins=tuple(pins),
        plan_roots=plan_roots,
        archive_retention_days=archive_retention_days,
        path=registry_path,
        warnings=tuple(warnings),
    )


def save_registry(registry: WorkbenchRegistry, path: str | Path | None = None) -> Path:
    """Write the registry atomically, returning the path that was written."""
    registry_path = _coerce_registry_path(path if path is not None else registry.path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "pins": [pin.to_payload() for pin in registry.pins],
        "plan_roots": list(registry.plan_roots),
        "archive_retention_days": registry.archive_retention_days,
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        # 0o600: the mode tempfile.mkstemp gave the registry before.
        durability.replace_text(registry_path, serialized, mode=0o600)
    except OSError as error:
        raise WorkbenchError(
            f"cannot write workbench registry {registry_path}: {error}"
        ) from error
    return registry_path


def upsert_pin(
    registry: WorkbenchRegistry,
    plan_dir: str | Path,
    *,
    label: str | None = None,
    profile: LaunchProfile | None = None,
) -> WorkbenchRegistry:
    """Return a new registry with ``plan_dir`` pinned or updated."""
    canonical = _canonical_plan_dir(str(plan_dir))
    if canonical is None:
        raise WorkbenchError(f"invalid plan_dir for pin: {plan_dir!r}")
    new_pin = RegistryPin(
        plan_dir=canonical,
        label=label,
        profile=profile if profile is not None else LaunchProfile(),
    )
    existing = list(registry.pins)
    for index, pin in enumerate(existing):
        if pin.plan_dir == canonical:
            existing[index] = new_pin
            return replace(registry, pins=tuple(existing))
    existing.append(new_pin)
    return replace(registry, pins=tuple(existing))


def remove_pin(registry: WorkbenchRegistry, plan_dir: str | Path) -> WorkbenchRegistry:
    """Return a new registry without any pin for ``plan_dir``."""
    canonical = _canonical_plan_dir(str(plan_dir))
    if canonical is None:
        raise WorkbenchError(f"invalid plan_dir for pin removal: {plan_dir!r}")
    remaining = tuple(pin for pin in registry.pins if pin.plan_dir != canonical)
    return replace(registry, pins=remaining)


def set_archive_retention_days(
    registry: WorkbenchRegistry, days: int
) -> WorkbenchRegistry:
    """Return a new registry with ``archive_retention_days`` set to ``days``."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise WorkbenchError(
            f"archive_retention_days must be a positive integer: {days!r}"
        )
    return replace(registry, archive_retention_days=days)


@contextmanager
def _registry_lock(registry_path: Path):
    """Hold a flock on a sidecar ``.lock`` file for the registry path.

    The lock is advisory and process-local; concurrent workers in the same
    Workbench process serialize read-modify-write of ``workbench.json`` so
    a second writer cannot stomp the first one's updates.
    """
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def mutate_registry(
    path: str | Path | None,
    mutator: Callable[[WorkbenchRegistry], WorkbenchRegistry],
) -> tuple[WorkbenchRegistry, Path]:
    """Read, mutate, and persist the registry under an advisory file lock.

    ``mutator`` receives the freshly-loaded registry and returns the
    updated one; the helper serializes concurrent callers by holding a
    flock on ``workbench.json.lock`` for the duration of the read +
    write. Returns the persisted registry and the path it was written to
    so handlers can echo a stable response payload.
    """
    registry_path = _coerce_registry_path(path)
    with _registry_lock(registry_path):
        current = load_registry(registry_path)
        updated = mutator(current)
        written_path = save_registry(updated, registry_path)
    return updated, written_path


WORKBENCH_COMMAND_NAME = "workbench"
PLANS_COMMAND_NAME = "plans"


# The ``workbench pin`` / ``workbench unpin`` CLI handlers moved to
# ``workbench_archive``, alongside the HTTP routes that do the same two things,
# so the terminal and the browser refuse the same inputs for the same stated
# reason. ``cli.py`` resolves them lazily by name, so nothing here imports that
# module (and nothing needs to).


def workbench_registry_from_args(args: Any) -> int:
    """Print the default registry contents in text or JSON form."""
    try:
        registry = load_registry(None)
    except WorkbenchError as error:
        if getattr(args, "json", False):
            responses.print_json(
                responses.error_payload(
                    WORKBENCH_COMMAND_NAME,
                    reason="invalid-registry",
                    error=str(error),
                )
            )
        else:
            print(
                f"agent-session-loop workbench registry: {error}",
                file=sys.stderr,
            )
        return 1

    if getattr(args, "json", False):
        responses.print_json(
            responses.success_payload(
                WORKBENCH_COMMAND_NAME,
                reason="ok",
                registry=registry.to_payload(),
            )
        )
        return 0

    print(f"Registry path: {registry.path}")
    print(f"Schema version: {registry.schema_version}")
    if registry.pins:
        print(f"Pins ({len(registry.pins)}):")
        for pin in registry.pins:
            label_suffix = f" [{pin.label}]" if pin.label else ""
            print(f"  - {pin.plan_dir}{label_suffix}")
    else:
        print("Pins: none")
    if registry.plan_roots:
        print(f"Plan roots ({len(registry.plan_roots)}):")
        for root in registry.plan_roots:
            print(f"  - {root}")
    else:
        print("Plan roots: none")
    if registry.warnings:
        print(f"Warnings ({len(registry.warnings)}):")
        for warning in registry.warnings:
            print(f"  - {warning}")
    return 0


def _plans_error(args: Any, *, reason: str, error: str) -> int:
    if getattr(args, "json", False):
        responses.print_json(
            responses.error_payload(
                PLANS_COMMAND_NAME,
                reason=reason,
                error=error,
            )
        )
    else:
        print(f"agent-session-loop plans: {error}", file=sys.stderr)
    return 1


def _plan_readiness_payload(
    plan_dir: Path,
    *,
    warnings: list[str],
) -> dict[str, str] | None:
    try:
        metadata = plans.read_readiness_metadata(plan_dir)
    except (plans.PlanError, OSError, ValueError) as error:
        warnings.append(f"{plan_dir}: readiness unreadable ({error})")
        return None
    if metadata is None:
        return None
    return {
        "state": metadata.state,
        "readiness_sha256": metadata.readiness_sha256,
        "checked_at": metadata.checked_at,
    }


def _plan_effective_activity_at(
    summary: WorkbenchSummary,
    readiness: Mapping[str, str] | None,
) -> str | None:
    if summary.last_activity_at:
        return summary.last_activity_at
    if readiness is None:
        return None
    checked_at = readiness.get("checked_at")
    return checked_at if isinstance(checked_at, str) and checked_at else None


def _plan_has_no_run_state(summary: WorkbenchSummary) -> bool:
    state_path = summary.state_path
    if state_path is None:
        return True
    try:
        return not state_path.is_file()
    except OSError:
        return True


def _plan_matches_stale_days(
    summary: WorkbenchSummary,
    *,
    stale_days: float,
    now: datetime,
    readiness: Mapping[str, str] | None,
) -> bool:
    activity = _plan_effective_activity_at(summary, readiness)
    if not activity:
        return False
    if activity.endswith("Z"):
        activity = activity[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(activity)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return now - parsed >= timedelta(days=stale_days)


def _print_plan_listing(
    rows: Sequence[tuple[WorkbenchSummary, dict[str, str] | None]],
    *,
    warnings: Sequence[str],
    show_readiness: bool,
) -> None:
    print(f"Plans ({len(rows)}):")
    if not rows:
        print("  (none)")
    for summary, readiness in rows:
        counts = summary.counts
        print(f"- {summary.plan_dir}")
        print(f"  state: {summary.primary_state}")
        print(
            "  sessions: "
            f"pending={counts.get('pending', 0)} "
            f"complete={counts.get('complete', 0)} "
            f"unknown={counts.get('unknown', 0)}"
        )
        print(
            f"  run state: {'absent' if _plan_has_no_run_state(summary) else 'present'}"
        )
        print(
            "  activity: "
            f"{_plan_effective_activity_at(summary, readiness) or 'unknown'}"
        )
        next_pending = summary.next_pending_session_id or "(none)"
        print(f"  next pending session: {next_pending}")
        if show_readiness:
            readiness_state = (
                readiness.get("state")
                if readiness is not None and readiness.get("state")
                else "(none)"
            )
            print(f"  readiness: {readiness_state}")
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")


def plans_from_args(args: Any, *, now: datetime | None = None) -> int:
    """List discovered plans with stale, never-run, and readiness filters.

    ``now`` is the command clock. The calendar arithmetic itself already lives
    in the pure ``_plan_matches_stale_days`` helper; injecting the clock here
    too means every ``--stale-days`` decision the command makes is reproducible
    from its inputs. Tests can then pin a moment -- a leap day, a year
    boundary, either side of the cutoff -- instead of writing fixed timestamps
    that quietly expire as the real calendar advances past them.
    """
    moment = datetime.now(UTC) if now is None else now
    stale_days = getattr(args, "stale_days", None)
    if stale_days is not None and stale_days < 0:
        return _plans_error(
            args,
            reason="invalid-argument",
            error="--stale-days must be non-negative",
        )

    readiness_filter = getattr(args, "readiness", None)
    if readiness_filter is not None:
        try:
            readiness_filter = plans.parse_readiness_state(readiness_filter)
        except plans.PlanError as error:
            return _plans_error(
                args,
                reason="invalid-readiness",
                error=str(error),
            )

    try:
        registry = load_registry(None)
    except WorkbenchError as error:
        return _plans_error(
            args,
            reason="invalid-registry",
            error=str(error),
        )

    discovery = discover_plans(
        state_root=getattr(args, "state_dir", None),
        registry=registry,
    )
    summaries = build_workbench_summaries(
        discovery.plans,
        state_dir=getattr(args, "state_dir", None),
    )
    warnings = list(discovery.warnings)
    rows: list[tuple[WorkbenchSummary, dict[str, str] | None]] = []
    payloads: list[dict[str, Any]] = []

    for summary in summaries:
        readiness: dict[str, str] | None = None
        if readiness_filter is not None or (
            stale_days is not None and not summary.last_activity_at
        ):
            readiness = _plan_readiness_payload(
                summary.plan_dir,
                warnings=warnings,
            )

        if readiness_filter is not None:
            readiness_state = readiness.get("state") if readiness is not None else None
            if readiness_state != readiness_filter:
                continue

        if getattr(args, "never_run", False) and not _plan_has_no_run_state(summary):
            continue

        if stale_days is not None and not _plan_matches_stale_days(
            summary,
            stale_days=stale_days,
            now=moment,
            readiness=readiness,
        ):
            continue

        payload = summary.to_payload()
        if readiness_filter is not None:
            payload["readiness"] = dict(readiness) if readiness is not None else None
        rows.append((summary, readiness))
        payloads.append(payload)

    if getattr(args, "json", False):
        responses.print_json(
            responses.success_payload(
                PLANS_COMMAND_NAME,
                reason="ok",
                filters={
                    "stale_days": stale_days,
                    "never_run": bool(getattr(args, "never_run", False)),
                    "readiness": readiness_filter,
                },
                plan_count=len(payloads),
                plans=payloads,
                warnings=warnings,
            )
        )
        return 0

    _print_plan_listing(
        rows,
        warnings=warnings,
        show_readiness=readiness_filter is not None,
    )
    return 0


def _looks_like_plan_dir(path: Path) -> bool:
    try:
        return (path / "manifest.md").is_file() and (path / "context.md").is_file()
    except OSError:
        return False


def _scan_plan_root(
    root: Path, *, warnings: list[str], context: str
) -> tuple[Path, ...]:
    try:
        if not root.exists():
            return ()
        if not root.is_dir():
            warnings.append(f"{context}: {root} is not a directory; ignoring")
            return ()
        entries = sorted(root.iterdir())
    except OSError as error:
        warnings.append(f"{context}: could not scan {root}: {error}")
        return ()
    found: list[Path] = []
    for child in entries:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        if _looks_like_plan_dir(child):
            try:
                found.append(plans.canonicalize_path(child))
            except (OSError, ValueError) as error:
                warnings.append(f"{context}: could not canonicalize {child}: {error}")
    return tuple(found)


def _iter_state_plan_dirs(state_root: Path, *, warnings: list[str]) -> tuple[Path, ...]:
    if not state_root.exists():
        return ()
    try:
        if not state_root.is_dir():
            warnings.append(f"state: {state_root} is not a directory; ignoring")
            return ()
        children = sorted(state_root.iterdir())
    except OSError as error:
        warnings.append(f"state: could not scan {state_root}: {error}")
        return ()
    candidates: list[Path] = []
    seen: set[str] = set()
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        for name in ("state.json", "lock.json"):
            payload_path = child / name
            if not payload_path.is_file():
                continue
            try:
                raw = json.loads(payload_path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                continue
            if not isinstance(raw, Mapping):
                continue
            plan_dir_value = raw.get("plan_dir")
            canonical = _canonical_plan_dir(plan_dir_value)
            if canonical is None:
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            candidates.append(Path(canonical))
            break
    return tuple(candidates)


def _default_share_dir() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser().resolve() / DEFAULT_PLANS_DIR_NAME
    return Path("~/.local/share").expanduser().resolve() / DEFAULT_PLANS_DIR_NAME


def _default_tmp_dir() -> Path:
    tmp_root = os.environ.get("TMPDIR", "/tmp")
    return Path(tmp_root).expanduser().resolve() / DEFAULT_PLANS_DIR_NAME


def _agent_plans_dir_override() -> Path | None:
    raw = os.environ.get("AGENT_PLANS_DIR")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def discover_plans(
    *,
    state_root: str | Path | None = None,
    registry: WorkbenchRegistry | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> DiscoveryResult:
    """Discover plan directories from every Workbench-visible source.

    Sources scanned, in canonical order:

    1. ASL state (per-plan state directories under ``state_root``).
    2. Registry pins (one canonical plan path per pin).
    3. Registry ``plan_roots`` entries from ``workbench.json`` (each
       scanned one level deep for directories containing ``manifest.md``
       and ``context.md``).
    4. Explicit ``plan_roots`` paths supplied by the caller (same scan
       rules; merged with registry roots under the ``plan-root`` source
       and deduplicated by canonical path).
    5. ``AGENT_PLANS_DIR`` environment override (same scan rules).
    6. ``${TMPDIR:-/tmp}/agent-plans`` (same scan rules).
    7. ``$XDG_DATA_HOME/agent-plans`` or ``~/.local/share/agent-plans``
       (same scan rules).

    Plans surfaced by multiple sources are deduplicated by canonical
    path; each result records every source it appeared in. Missing
    directories, unreadable scan roots, and malformed entries become
    recoverable warnings on the returned :class:`DiscoveryResult`.
    """
    warnings: list[str] = []
    accumulator: dict[str, list[DiscoverySource]] = {}
    order: list[str] = []

    def record(plan_dir: Path, source: DiscoverySource) -> None:
        canonical = str(plan_dir)
        sources = accumulator.get(canonical)
        if sources is None:
            accumulator[canonical] = [source]
            order.append(canonical)
            return
        if source not in sources:
            sources.append(source)

    state_root_path = state.state_root(state_root)
    for plan_dir in _iter_state_plan_dirs(state_root_path, warnings=warnings):
        record(plan_dir, "state")

    pin_lookup: dict[str, RegistryPin] = {}
    if registry is not None:
        for pin in registry.pins:
            pin_lookup[pin.plan_dir] = pin
            record(Path(pin.plan_dir), "registry")

    if registry is not None:
        for index, raw_root in enumerate(registry.plan_roots):
            for plan_dir in _scan_plan_root(
                Path(raw_root),
                warnings=warnings,
                context=f"registry_plan_root[{index}]",
            ):
                record(plan_dir, "plan-root")

    for index, raw_root in enumerate(plan_roots):
        canonical = _canonical_plan_dir(str(raw_root))
        if canonical is None:
            warnings.append(f"plan_root[{index}]: invalid path {raw_root!r}")
            continue
        for plan_dir in _scan_plan_root(
            Path(canonical), warnings=warnings, context=f"plan_root[{index}]"
        ):
            record(plan_dir, "plan-root")

    if include_default_dirs:
        override = _agent_plans_dir_override()
        if override is not None:
            for plan_dir in _scan_plan_root(
                override, warnings=warnings, context="AGENT_PLANS_DIR"
            ):
                record(plan_dir, "agent-plans-dir")

        for plan_dir in _scan_plan_root(
            _default_tmp_dir(), warnings=warnings, context="tmp default"
        ):
            record(plan_dir, "tmp-default")

        for plan_dir in _scan_plan_root(
            _default_share_dir(), warnings=warnings, context="share default"
        ):
            record(plan_dir, "share-default")

    discovered: list[DiscoveredPlan] = []
    source_order = {source: index for index, source in enumerate(DISCOVERY_SOURCES)}
    for canonical in order:
        sources = tuple(
            sorted(accumulator[canonical], key=lambda src: source_order[src])
        )
        plan_path = Path(canonical)
        pin = pin_lookup.get(canonical)
        try:
            exists = plan_path.is_dir()
        except OSError:
            exists = False
        discovered.append(
            DiscoveredPlan(
                plan_dir=plan_path,
                sources=sources,
                label=pin.label if pin is not None else None,
                profile=pin.profile if pin is not None else None,
                exists=exists,
            )
        )

    return DiscoveryResult(plans=tuple(discovered), warnings=tuple(warnings))


def _parse_archive_timestamp(value: str) -> datetime | None:
    """Parse an archive ISO timestamp, normalizing naive values to UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True)
class WorkbenchSummary:
    """Aggregate per-plan view that drives the Workbench action queue.

    Combines durable plan state (sessions, progress journal), the latest
    runner state (paused, finished, active turn/verification), the active
    lock, and registry-derived label/profile metadata. Exposes a single
    ``primary_state`` plus a single ``recommended_action`` so the queue UI
    can render one row per plan without re-deriving classification logic.
    """

    plan_dir: Path
    label: str | None
    profile: LaunchProfile | None
    sources: tuple[DiscoverySource, ...]
    exists: bool
    primary_state: PrimaryState
    recommended_action: RecommendedAction
    next_action_hint: str
    detail: str | None
    next_pending_session_id: str | None
    next_pending_session_path: Path | None
    counts: dict[str, int]
    state_path: Path | None
    events_path: Path | None
    progress_path: Path | None
    final_review_path: Path | None
    artifact_dir: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    last_message_path: Path | None
    result_path: Path | None
    run_id: str | None
    harness: str | None
    cwd: str | None
    workspace_root: str | None
    registry_digest: str | None
    repositories: tuple[dict[str, Any], ...]
    repository_count: int
    pause_reason: str | None
    pause_message: str | None
    last_message_preview: str | None
    urgency_rank: int = 0
    urgency_group: UrgencyGroup = "needs-action"
    last_activity_at: str | None = None
    last_activity_source: LastActivitySource | None = None
    completed_at: str | None = None
    activity_label: str = ""
    concise_reason: str = ""
    searchable_text: str = ""
    recovery: dict[str, Any] = field(default_factory=dict)
    review_resume: dict[str, Any] = field(default_factory=dict)
    #: Bounded, display-only projection of the last run's supervisor audit. The
    #: Workbench reports what the audit found and where the inbox is; it has no
    #: field with which to configure, launch, retry, or dispose of an audit.
    assurance: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "label": self.label,
            "profile": (
                self.profile.to_payload() if self.profile is not None else None
            ),
            "sources": list(self.sources),
            "exists": self.exists,
            "primary_state": self.primary_state,
            "recommended_action": self.recommended_action,
            "urgency_rank": self.urgency_rank,
            "urgency_group": self.urgency_group,
            "next_action_hint": self.next_action_hint,
            "activity_label": self.activity_label,
            "concise_reason": self.concise_reason,
            "last_activity_at": self.last_activity_at,
            "last_activity_source": self.last_activity_source,
            "completed_at": self.completed_at,
            "searchable_text": self.searchable_text,
            "recovery": dict(self.recovery),
            "review_resume": dict(self.review_resume),
            "assurance": dict(self.assurance),
            "detail": self.detail,
            "next_pending_session_id": self.next_pending_session_id,
            "next_pending_session_path": (
                str(self.next_pending_session_path)
                if self.next_pending_session_path is not None
                else None
            ),
            "counts": dict(self.counts),
            "state_path": str(self.state_path) if self.state_path else None,
            "events_path": str(self.events_path) if self.events_path else None,
            "progress_path": (str(self.progress_path) if self.progress_path else None),
            "final_review_path": (
                str(self.final_review_path) if self.final_review_path else None
            ),
            "artifact_dir": (str(self.artifact_dir) if self.artifact_dir else None),
            "stdout_path": str(self.stdout_path) if self.stdout_path else None,
            "stderr_path": str(self.stderr_path) if self.stderr_path else None,
            "last_message_path": (
                str(self.last_message_path) if self.last_message_path else None
            ),
            "result_path": str(self.result_path) if self.result_path else None,
            "run_id": self.run_id,
            "harness": self.harness,
            **workbench_workspace._workspace_payload(
                {
                    "workspace_root": self.workspace_root,
                    "registry_digest": self.registry_digest,
                    "repositories": self.repositories,
                    "repository_count": self.repository_count,
                },
                cwd=self.cwd,
            ),
            "pause_reason": self.pause_reason,
            "pause_message": self.pause_message,
            "last_message_preview": self.last_message_preview,
            "warnings": list(self.warnings),
        }


def _truncate_preview(text: str, *, limit: int = LAST_MESSAGE_PREVIEW_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"


def _verification_position(active_verification: Mapping[str, Any]) -> str:
    check_count = active_verification.get("check_count")
    check_index = active_verification.get("current_check_index")
    if check_index is None:
        return f"-/{check_count}" if check_count is not None else "-"
    try:
        position = int(check_index) + 1
    except TypeError, ValueError:
        return "-"
    if check_count is not None:
        return f"{position}/{check_count}"
    return str(position)


def _final_review_path_if_exists(plan_dir: Path) -> Path | None:
    candidate = plan_dir / FINAL_REVIEW_FILE_NAME
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _first_activity(
    candidates: Sequence[tuple[Any, LastActivitySource]],
) -> tuple[str | None, LastActivitySource | None]:
    for value, source in candidates:
        if isinstance(value, str) and value:
            return value, source
    return None, None


def _paused_activity(
    runner_state: Mapping[str, Any],
) -> tuple[str | None, LastActivitySource | None]:
    """Latest valid event in the displayed run; its start is only a fallback."""
    candidates: list[tuple[Any, LastActivitySource]] = [
        (runner_state.get("run_finished_at"), "run-finished"),
    ]
    turns = runner_state.get("turns")
    records = list(turns) if isinstance(turns, list) else []
    if isinstance(runner_state.get("active_turn"), Mapping):
        records.append(runner_state["active_turn"])
    for turn in records:
        if isinstance(turn, Mapping):
            candidates.extend(
                (turn.get(key), "turn-record")
                for key in ("finished_at", "last_activity_at", "started_at")
            )
    valid = [
        (parsed, value, source)
        for value, source in candidates
        if isinstance(value, str)
        and (parsed := _parse_archive_timestamp(value)) is not None
    ]
    if valid:
        _, value, source = max(valid, key=lambda item: item[0])
        return value, source
    started = runner_state.get("run_started_at")
    if isinstance(started, str) and _parse_archive_timestamp(started) is not None:
        return started, "run-started"
    return None, None


def _last_progress_entry_at(plan_dir: Path) -> str | None:
    try:
        entries = plans.read_progress_entries(plan_dir)
    except plans.PlanError:
        return None
    if not entries:
        return None
    return entries[-1].completed_at


def _searchable_text(
    *,
    plan_dir: Path,
    label: str | None,
    primary_state: PrimaryState,
    recommended_action: RecommendedAction,
    urgency_group: UrgencyGroup,
    activity_label: str,
    concise_reason: str,
    detail: str | None,
    next_action_hint: str,
    next_pending_session_id: str | None,
    pause_reason: str | None,
    pause_message: str | None,
    last_message_preview: str | None,
    run_id: str | None,
    harness: str | None,
    sources: tuple[DiscoverySource, ...],
) -> str:
    parts: list[str] = [
        str(plan_dir),
        plan_dir.name,
        primary_state,
        recommended_action,
        urgency_group,
        activity_label,
        concise_reason,
        next_action_hint,
    ]
    if label:
        parts.append(label)
    if detail:
        parts.append(detail)
    if next_pending_session_id:
        parts.extend(["session", next_pending_session_id])
    if pause_reason:
        parts.append(pause_reason)
    if pause_message:
        parts.append(pause_message)
    if last_message_preview:
        parts.append(last_message_preview)
    if run_id:
        parts.append(run_id)
    if harness:
        parts.append(harness)
    parts.extend(sources)
    joined = " ".join(part for part in parts if part)
    return " ".join(joined.lower().split())


def _build_invalid_summary(
    plan: DiscoveredPlan,
    *,
    detail: str,
    exists: bool,
    state_paths: tuple[Path, Path, Path] | None = None,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> WorkbenchSummary:
    try:
        workspace = (
            workbench_workspace._workspace_projection(plan.plan_dir) if exists else {}
        )
    except plans.PlanError, OSError, ValueError:
        workspace = {}
    if state_paths is not None:
        state_path, events_path, progress_path = state_paths
    else:
        state_path = events_path = progress_path = None
    primary_state: PrimaryState = "invalid"
    recommended_action: RecommendedAction = "repair-plan"
    next_action_hint = "Plan cannot be inspected; repair it before continuing."
    activity_label = ACTIVITY_LABEL_BY_STATE[primary_state]
    concise_reason = detail
    searchable_text = _searchable_text(
        plan_dir=plan.plan_dir,
        label=plan.label,
        primary_state=primary_state,
        recommended_action=recommended_action,
        urgency_group=URGENCY_GROUP_BY_STATE[primary_state],
        activity_label=activity_label,
        concise_reason=concise_reason,
        detail=detail,
        next_action_hint=next_action_hint,
        next_pending_session_id=None,
        pause_reason=None,
        pause_message=None,
        last_message_preview=None,
        run_id=None,
        harness=None,
        sources=plan.sources,
    )
    recovery = workbench_recovery.build_workbench_recovery(
        replace(plan, exists=exists), state_dir=state_dir, reclaim=reclaim
    ).compact_payload()
    return WorkbenchSummary(
        plan_dir=plan.plan_dir,
        label=plan.label,
        profile=plan.profile,
        sources=plan.sources,
        exists=exists,
        primary_state=primary_state,
        recommended_action=recommended_action,
        next_action_hint=next_action_hint,
        detail=detail,
        next_pending_session_id=None,
        next_pending_session_path=None,
        counts={"pending": 0, "complete": 0, "empty": 0, "unknown": 0},
        state_path=state_path,
        events_path=events_path,
        progress_path=progress_path,
        final_review_path=_final_review_path_if_exists(plan.plan_dir)
        if exists
        else None,
        artifact_dir=None,
        stdout_path=None,
        stderr_path=None,
        last_message_path=None,
        result_path=None,
        run_id=None,
        harness=None,
        cwd=(
            str(workspace.get("workspace_root"))
            if workspace.get("workspace_root") is not None
            else (plan.profile.cwd if plan.profile is not None else None)
        ),
        workspace_root=workspace.get("workspace_root"),
        registry_digest=workspace.get("registry_digest"),
        repositories=workbench_workspace._repository_rows(
            workspace.get("repositories")
        ),
        repository_count=int(workspace.get("repository_count") or 0),
        pause_reason=None,
        pause_message=None,
        last_message_preview=None,
        urgency_rank=URGENCY_RANK_BY_STATE[primary_state],
        urgency_group=URGENCY_GROUP_BY_STATE[primary_state],
        last_activity_at=None,
        last_activity_source=None,
        activity_label=activity_label,
        concise_reason=concise_reason,
        searchable_text=searchable_text,
        recovery=recovery,
        warnings=(),
    )


def build_workbench_summary(
    plan: DiscoveredPlan,
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> WorkbenchSummary:
    """Aggregate one discovered plan into a Workbench queue summary.

    Reads plan files and runner state without rewriting them, but the shared
    lock reader reclaims a lock this host has proven dead. ``reclaim=False``
    suppresses that hygiene so an observational caller — the stdio bridge —
    projects a function of the corpus rather than of its own read order.
    Classification priority, highest first, is:

    1. ``invalid`` - plan directory missing or unparseable.
    2. ``verifying`` - active lock and ``active_verification`` set.
    3. ``running`` - active lock and ``active_turn`` set, or a live mailbox
       reply wait is holding the run between harness turns.
    4. ``paused-for-reply`` - runner state ``paused=True`` or an expired
       nonterminal reply wait can be resumed with an operator answer.
    5. ``failed`` - finished run with ``ok=False``.
    6. ``stale`` - stale lock or stale-unfinished runner state.
    7. ``recent`` - review continuation is ready despite zero pending sessions.
    8. ``complete`` - no pending sessions or eligible continuation remain.
    9. ``recent`` - pending sessions remain after a finished run.
    10. ``ready`` - pending sessions remain and no run state exists.
    """
    plan_dir = plan.plan_dir
    if not plan.exists:
        return _build_invalid_summary(
            plan,
            detail=f"plan directory does not exist: {plan_dir}",
            exists=False,
            state_dir=state_dir,
            reclaim=reclaim,
        )

    try:
        snapshot = plans.inspect_plan(plan_dir)
    except plans.PlanError as error:
        state_paths = workbench_recovery._summary_base_paths(plan_dir, state_dir)
        return _build_invalid_summary(
            plan,
            detail=str(error),
            exists=True,
            state_paths=state_paths,
            state_dir=state_dir,
            reclaim=reclaim,
        )

    state_path, events_path, progress_path = workbench_recovery._summary_base_paths(
        snapshot.plan_dir, state_dir
    )
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, events_path
    ).to_payload()
    lock_path = state.lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = state_locks._read_lock_path(
        lock_path, snapshot.plan_dir, reclaim=reclaim
    )
    runner_state = state_payloads._runner_state_payload(
        state_path,
        lock_info=lock_info,
        include_turn_activity=True,
    )
    try:
        workspace = workbench_workspace._workspace_projection(
            snapshot.plan_dir, runtime=runner_state
        )
    except (plans.PlanError, OSError, ValueError) as error:
        return _build_invalid_summary(
            replace(plan, plan_dir=snapshot.plan_dir, exists=True),
            detail=f"workspace metadata unreadable: {error}",
            exists=True,
            state_paths=(state_path, events_path, progress_path),
            state_dir=state_dir,
            reclaim=reclaim,
        )
    summary_cwd = (
        runner_state.get("cwd")
        or workspace.get("workspace_root")
        or (plan.profile.cwd if plan.profile is not None else None)
    )
    expired_wait_context = workbench_recovery._expired_wait_qna_context(
        snapshot,
        runner_state,
        state_dir,
        reclaim=reclaim,
    )
    stale_unfinished, stale_reason = state_abandonment._stale_runner_state(
        runner_state, lock_info
    )
    recovery = workbench_recovery.build_workbench_recovery(
        replace(plan, plan_dir=snapshot.plan_dir, exists=True),
        state_dir=state_dir,
        reclaim=reclaim,
    ).compact_payload()

    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1
    next_pending = snapshot.next_pending
    final_review_path = _final_review_path_if_exists(snapshot.plan_dir)

    active_turn = runner_state.get("active_turn")
    active_verification = runner_state.get("active_verification")
    base_run_id = runner_state.get("run_id")
    base_harness = runner_state.get("harness")

    artifact_dir = None
    stdout_path = None
    stderr_path = None
    last_message_path = None
    result_path = None
    if isinstance(active_turn, dict):
        artifact_dir = workbench_recovery._coerce_path_value(
            active_turn.get("artifact_dir")
        )
        stdout_path = workbench_recovery._coerce_path_value(
            active_turn.get("stdout_path")
        )
        stderr_path = workbench_recovery._coerce_path_value(
            active_turn.get("stderr_path")
        )
        last_message_path = workbench_recovery._coerce_path_value(
            active_turn.get("last_message_path")
        )
        result_path = workbench_recovery._coerce_path_value(
            active_turn.get("result_path")
        )

    def base_kwargs(
        *,
        primary_state: PrimaryState,
        recommended_action: RecommendedAction,
        next_action_hint: str,
        detail: str | None,
        warnings: tuple[str, ...] = (),
        run_id: str | None = base_run_id,
        harness: str | None = base_harness,
        artifact_dir: Path | None = artifact_dir,
        stdout_path: Path | None = stdout_path,
        stderr_path: Path | None = stderr_path,
        last_message_path: Path | None = last_message_path,
        result_path: Path | None = result_path,
        pause_reason: str | None = None,
        pause_message: str | None = None,
        last_message_preview: str | None = None,
        last_activity_at: str | None = None,
        last_activity_source: LastActivitySource | None = None,
        completed_at: str | None = None,
        concise_reason: str | None = None,
        activity_label: str | None = None,
    ) -> WorkbenchSummary:
        urgency_group = URGENCY_GROUP_BY_STATE[primary_state]
        resolved_activity_label = (
            activity_label or ACTIVITY_LABEL_BY_STATE[primary_state]
        )
        resolved_reason = concise_reason or detail or next_action_hint
        next_pending_session_id = next_pending.id if next_pending is not None else None
        searchable_text = _searchable_text(
            plan_dir=snapshot.plan_dir,
            label=plan.label,
            primary_state=primary_state,
            recommended_action=recommended_action,
            urgency_group=urgency_group,
            activity_label=resolved_activity_label,
            concise_reason=resolved_reason,
            detail=detail,
            next_action_hint=next_action_hint,
            next_pending_session_id=next_pending_session_id,
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=last_message_preview,
            run_id=run_id,
            harness=harness,
            sources=plan.sources,
        )
        return WorkbenchSummary(
            plan_dir=snapshot.plan_dir,
            label=plan.label,
            profile=plan.profile,
            sources=plan.sources,
            exists=True,
            primary_state=primary_state,
            recommended_action=recommended_action,
            next_action_hint=next_action_hint,
            detail=detail,
            next_pending_session_id=next_pending_session_id,
            next_pending_session_path=(
                next_pending.path if next_pending is not None else None
            ),
            counts=counts,
            state_path=state_path,
            events_path=events_path,
            progress_path=progress_path,
            final_review_path=final_review_path,
            artifact_dir=artifact_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            last_message_path=last_message_path,
            result_path=result_path,
            run_id=run_id,
            harness=harness,
            cwd=str(summary_cwd) if summary_cwd is not None else None,
            workspace_root=workspace.get("workspace_root"),
            registry_digest=workspace.get("registry_digest"),
            repositories=workbench_workspace._repository_rows(
                workspace.get("repositories")
            ),
            repository_count=int(workspace.get("repository_count") or 0),
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=last_message_preview,
            urgency_rank=URGENCY_RANK_BY_STATE[primary_state],
            urgency_group=urgency_group,
            last_activity_at=last_activity_at,
            last_activity_source=last_activity_source,
            completed_at=completed_at,
            activity_label=resolved_activity_label,
            concise_reason=resolved_reason,
            searchable_text=searchable_text,
            recovery=recovery,
            review_resume=review_resume,
            assurance=assurance_report.bounded_summary(runner_state.get("assurance")),
            warnings=warnings,
        )

    if (
        lock_info is not None
        and lock_info.active
        and isinstance(active_verification, dict)
    ):
        session_label = (
            workbench_recovery._session_identifier_from_payload(active_verification)
            or "?"
        )
        position = _verification_position(active_verification)
        check_name = active_verification.get("current_check_name") or "pending"
        verifying_activity, verifying_source = _first_activity(
            (
                (
                    active_verification.get("current_check_started_at"),
                    "active-verification",
                ),
                (active_verification.get("started_at"), "active-verification"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="verifying",
            recommended_action="watch",
            next_action_hint=(
                f"Verifying session {session_label} check {position} ({check_name})"
            ),
            detail=f"Active verification on session {session_label}",
            last_activity_at=verifying_activity,
            last_activity_source=verifying_source,
            concise_reason=(
                f"On check {position} ({check_name}) of session {session_label}"
            ),
        )

    if lock_info is not None and lock_info.active and isinstance(active_turn, dict):
        session_label = (
            workbench_recovery._session_identifier_from_payload(active_turn) or "?"
        )
        turn_index = active_turn.get("turn_index")
        running_activity, running_source = _first_activity(
            (
                (active_turn.get("last_activity_at"), "active-turn"),
                (active_turn.get("started_at"), "active-turn"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="running",
            recommended_action="watch",
            next_action_hint=(f"Running session {session_label} turn {turn_index}"),
            detail=f"Active turn on session {session_label}",
            last_activity_at=running_activity,
            last_activity_source=running_source,
            concise_reason=(f"Session {session_label}, turn {turn_index}"),
        )

    waiting_for_reply = runner_state.get("waiting_for_reply")
    if (
        lock_info is not None
        and lock_info.active
        and isinstance(waiting_for_reply, dict)
        and runner_state.get("waiting_for_reply_status")
        == state_payloads.WAITING_FOR_REPLY_STATUS_LIVE
    ):
        session_label = (
            workbench_recovery._session_identifier_from_payload(waiting_for_reply)
            or "?"
        )
        waiting_activity, waiting_source = _first_activity(
            ((runner_state.get("run_started_at"), "run-started"),)
        )
        return base_kwargs(
            primary_state="running",
            recommended_action="watch",
            next_action_hint=f"Awaiting reply for session {session_label}",
            detail=f"Live mailbox reply wait on session {session_label}",
            last_activity_at=waiting_activity,
            last_activity_source=waiting_source,
            concise_reason=f"Awaiting reply for session {session_label}",
        )

    if runner_state.get("paused") is True or expired_wait_context is not None:
        pause_reason = (
            runner_state.get("pause_reason") or runner_state.get("reason") or None
        )
        if (
            pause_reason == events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR
            and active_turn is None
            and recovery.get("kind") == "review-continuation"
        ):
            failed_activity, failed_source = _first_activity(
                (
                    (runner_state.get("run_finished_at"), "run-finished"),
                    (runner_state.get("run_started_at"), "run-started"),
                )
            )
            if (
                recovery.get("severity") == "info"
                and review_resume.get("eligible") is True
                and review_resume.get("action")
                in {"materialize-repairs", "run-repairs", "rereview"}
            ):
                return base_kwargs(
                    primary_state="recent",
                    recommended_action="run",
                    next_action_hint=recovery["title"],
                    detail=recovery["summary"],
                    pause_reason=pause_reason,
                    pause_message=runner_state.get("pause_message") or None,
                    last_activity_at=failed_activity,
                    last_activity_source=failed_source,
                    concise_reason=recovery["summary"],
                    activity_label=(
                        REVIEW_CONTINUATION_ACTIVITY_LABEL
                        if next_pending is None
                        else None
                    ),
                )
            return base_kwargs(
                primary_state="failed",
                recommended_action="inspect-failure",
                next_action_hint=recovery["title"],
                detail=recovery["summary"],
                pause_reason=pause_reason,
                pause_message=runner_state.get("pause_message") or None,
                last_activity_at=failed_activity,
                last_activity_source=failed_source,
                concise_reason=recovery["summary"],
            )

        warnings: list[str] = []
        pause_message = None
        preview = None
        qna_last_message_path = last_message_path
        paused_activity, paused_source = _paused_activity(runner_state)
        try:
            context = expired_wait_context or qna.load_qna_context(
                snapshot.plan_dir,
                state_dir=state_dir,
                reclaim=reclaim,
            )
        except qna.QnAError as error:
            # Not every pause is a question. A run that stopped on, say, an
            # unready review repair has no Q&A context, and recovery has
            # already named what it needs; offering "reply" there sends the
            # operator to a form that can only refuse them.
            if recovery.get("kind") not in {"none", "reply-needed"} and recovery.get(
                "title"
            ):
                return base_kwargs(
                    primary_state="failed",
                    recommended_action="inspect-failure",
                    next_action_hint=recovery["title"],
                    detail=recovery.get("summary") or recovery["title"],
                    pause_reason=pause_reason,
                    pause_message=runner_state.get("pause_message") or None,
                    last_activity_at=paused_activity,
                    last_activity_source=paused_source,
                    concise_reason=recovery.get("summary") or recovery["title"],
                )
            warnings.append(
                f"paused-q&a context unreadable ({error.reason}): {error.error}"
            )
            hint = (
                f"Reply needed for session {next_pending.id}"
                if next_pending is not None
                else "Reply needed"
            )
            detail = "Paused without readable last-message context"
        else:
            pause_reason = context.pause_reason or pause_reason
            pause_message = context.pause_message
            preview = _truncate_preview(context.last_message)
            qna_last_message_path = context.last_message_path
            if context.stdout_path is not None:
                stdout_path = context.stdout_path
            if context.stderr_path is not None:
                stderr_path = context.stderr_path
            if context.artifact_dir is not None:
                artifact_dir = context.artifact_dir
            if context.result_path is not None:
                result_path = context.result_path
            base_run_id_local = context.run_id or base_run_id
            base_harness_local = context.harness or base_harness
            session_label = context.session_id
            hint = f"Reply needed for session {session_label}"
            detail = pause_message or pause_reason or "Paused awaiting reply"
            return base_kwargs(
                primary_state="paused-for-reply",
                recommended_action="reply",
                next_action_hint=hint,
                detail=detail,
                warnings=tuple(warnings),
                run_id=base_run_id_local,
                harness=base_harness_local,
                artifact_dir=artifact_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                last_message_path=qna_last_message_path,
                result_path=result_path,
                pause_reason=pause_reason,
                pause_message=pause_message,
                last_message_preview=preview,
                last_activity_at=paused_activity,
                last_activity_source=paused_source,
                concise_reason=(
                    pause_message or pause_reason or "Awaiting operator reply"
                ),
            )

        return base_kwargs(
            primary_state="paused-for-reply",
            recommended_action="reply",
            next_action_hint=hint,
            detail=detail,
            warnings=tuple(warnings),
            last_message_path=qna_last_message_path,
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=preview,
            last_activity_at=paused_activity,
            last_activity_source=paused_source,
            concise_reason=(pause_message or pause_reason or "Awaiting operator reply"),
        )

    if stale_unfinished or (lock_info is not None and lock_info.stale):
        if stale_unfinished:
            reason_text = stale_reason or "Runner state has not finished"
        else:
            reason_text = lock_info.reason if lock_info is not None else "stale lock"
        lock_created_at = (
            lock_info.created_at
            if lock_info is not None and lock_info.created_at
            else None
        )
        stale_activity, stale_source = _first_activity(
            (
                (lock_created_at, "lock-created"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="stale",
            recommended_action="unlock",
            next_action_hint=f"Stale state detected: {reason_text}",
            detail=reason_text,
            last_activity_at=stale_activity,
            last_activity_source=stale_source,
            concise_reason=reason_text,
        )

    if (
        runner_state.get("present")
        and runner_state.get("run_finished_at")
        and runner_state.get("ok") is False
    ):
        reason_text = runner_state.get("reason") or "Last run finished with errors"
        failed_activity, failed_source = _first_activity(
            (
                (runner_state.get("run_finished_at"), "run-finished"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="failed",
            recommended_action="inspect-failure",
            next_action_hint=f"Last run failed: {reason_text}",
            detail=reason_text,
            last_activity_at=failed_activity,
            last_activity_source=failed_source,
            concise_reason=reason_text,
        )

    if next_pending is None:
        complete_activity, complete_source = _first_activity(
            (
                (_last_progress_entry_at(snapshot.plan_dir), "progress-journal"),
                (runner_state.get("run_finished_at"), "run-finished"),
            )
        )
        review_action = review_resume.get("action")
        if review_resume.get("eligible") and review_action in {
            "materialize-repairs",
            "rereview",
        }:
            if review_action == "materialize-repairs":
                hint = (
                    "Resume review repairs from session "
                    f"{review_resume.get('review_session_id')}"
                )
                detail = "Review repair materialization is ready"
            else:
                hint = "Rerun review after completed repair sessions"
                detail = "Completed review repairs are awaiting rereview"
            return base_kwargs(
                primary_state="recent",
                recommended_action="run",
                next_action_hint=hint,
                detail=detail,
                last_activity_at=complete_activity,
                last_activity_source=complete_source,
                concise_reason=detail,
                activity_label=REVIEW_CONTINUATION_ACTIVITY_LABEL,
            )
        if review_resume.get("blocker") == "higher-cap-required":
            minimum = review_resume.get("minimum_required_cap")
            selected_cap = review_resume.get("selected_cap")
            if selected_cap is None:
                hint = f"Supply a review-fix cap of {minimum} or higher"
                detail = "Review repairs require a review-fix cap to resume"
            else:
                hint = f"Increase to a review-fix cap of {minimum} or higher"
                detail = (
                    "Review repair budget is exhausted at the selected cap "
                    f"of {selected_cap}"
                )
        elif review_resume.get("blocker") == "artifact-invalid":
            hint = "Correct the structured review artifact before rerunning"
            detail = review_resume.get("artifact_error") or (
                "Review artifact validation failed"
            )
        elif (
            review_action == "terminal"
            and review_resume.get("blocker")
            not in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
        ):
            hint = recovery.get("title") or ("Inspect inconsistent review continuation")
            detail = recovery.get("summary") or (
                "Recorded review repair history cannot be resumed safely: "
                f"{review_resume.get('blocker') or 'unknown blocker'}."
            )
        else:
            hint = "All sessions complete"
            detail = "No pending sessions remain"
        return base_kwargs(
            primary_state="complete",
            recommended_action="view",
            next_action_hint=hint,
            detail=detail,
            last_activity_at=complete_activity,
            last_activity_source=complete_source,
            completed_at=complete_activity,
            concise_reason=detail,
        )

    if runner_state.get("present") and runner_state.get("run_finished_at"):
        recent_activity, recent_source = _first_activity(
            ((runner_state.get("run_finished_at"), "run-finished"),)
        )
        review_hint = (
            f"Resume review repairs with session {next_pending.id}"
            if review_resume.get("action") == "run-repairs"
            else f"Resume with session {next_pending.id}"
        )
        return base_kwargs(
            primary_state="recent",
            recommended_action="run",
            next_action_hint=review_hint,
            detail=f"Last run finished at {runner_state.get('run_finished_at')}",
            last_activity_at=recent_activity,
            last_activity_source=recent_source,
            concise_reason=(
                f"Last run finished at {runner_state.get('run_finished_at')}"
            ),
        )

    ready_activity, ready_source = _first_activity(
        ((_last_progress_entry_at(snapshot.plan_dir), "progress-journal"),)
    )
    ready_hint = (
        f"Start review repairs with session {next_pending.id}"
        if review_resume.get("action") == "run-repairs"
        else f"Start session {next_pending.id}"
    )
    return base_kwargs(
        primary_state="ready",
        recommended_action="run",
        next_action_hint=ready_hint,
        detail="Ready to run; no prior runner state",
        last_activity_at=ready_activity,
        last_activity_source=ready_source,
        concise_reason=f"Next: session {next_pending.id}",
    )


def build_workbench_summaries(
    discovered: Iterable[DiscoveredPlan],
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> tuple[WorkbenchSummary, ...]:
    """Build a Workbench summary per discovered plan in iteration order."""
    return tuple(
        build_workbench_summary(plan, state_dir=state_dir, reclaim=reclaim)
        for plan in discovered
    )


class LaunchError(Exception):
    """Raised when a Workbench launch request cannot be honored.

    ``spawned`` answers the question a durable journal has to ask about every
    failure: was there a child? ``process-failed`` is raised from both sides of
    the spawn, and only the far side has a process that existed, ran, and may
    have written to the plan before it was killed. A caller that records
    "certainly did not run" for that case is recording something it cannot
    know, so the flag travels with the refusal rather than being guessed from
    the reason.
    """

    def __init__(self, reason: str, message: str, *, spawned: bool = False) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.spawned = spawned


LAUNCH_REASON_INVALID_PLAN = "invalid-plan"
LAUNCH_REASON_INVALID_PROFILE = "invalid-profile"
LAUNCH_REASON_EMPTY_REPLY = "empty-reply"
LAUNCH_REASON_PROCESS_FAILED = "process-failed"


@dataclass(frozen=True)
class LaunchResult:
    """Process metadata returned by :func:`launch_plan`.

    The reply text is never stored on this dataclass — only its length when
    a reply was streamed, so callers can confirm the byte count was non-zero
    without holding a copy of the operator answer.
    """

    pid: int
    argv: tuple[str, ...]
    plan_dir: Path
    profile_payload: dict[str, Any]
    reply_provided: bool
    reply_length: int | None
    reply_origin: str | None
    state_dir: Path | None
    cwd: str | None = None
    workspace_root: str | None = None
    registry_digest: str | None = None
    repositories: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "argv": list(self.argv),
            "plan_dir": str(self.plan_dir),
            "profile": dict(self.profile_payload),
            "reply_provided": self.reply_provided,
            "reply_length": self.reply_length,
            "reply_origin": self.reply_origin,
            "state_dir": str(self.state_dir) if self.state_dir is not None else None,
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "registry_digest": self.registry_digest,
            "repositories": [dict(row) for row in self.repositories],
        }


def _validate_launch_plan_dir(plan_dir: str | Path) -> Path:
    try:
        canonical = plans.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"invalid plan directory {plan_dir!r}: {error}",
        ) from error
    if not canonical.is_dir():
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"plan directory does not exist: {canonical}",
        )
    if not (canonical / "manifest.md").is_file():
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"plan directory is missing manifest.md: {canonical}",
        )
    if not (canonical / "context.md").is_file():
        raise LaunchError(
            LAUNCH_REASON_INVALID_PLAN,
            f"plan directory is missing context.md: {canonical}",
        )
    return canonical


def _validate_launch_profile(profile: LaunchProfile | None) -> LaunchProfile:
    if profile is None:
        return LaunchProfile()
    if not isinstance(profile, LaunchProfile):
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "profile must be a LaunchProfile instance",
        )
    if profile.harness is not None and profile.harness not in harnesses.HARNESS_NAMES:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"unsupported harness {profile.harness!r}",
        )
    if (
        profile.review_harness is not None
        and profile.review_harness not in harnesses.HARNESS_NAMES
    ):
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"unsupported review_harness {profile.review_harness!r}",
        )
    if (
        profile.review_harness is not None
        and profile.harness is not None
        and profile.review_harness == profile.harness
    ):
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "review_harness must differ from harness",
        )
    if profile.review_harness_model is not None and profile.review_harness is None:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "review_harness_model requires review_harness",
        )
    if profile.commit_mode is not None and profile.commit_mode not in COMMIT_MODES:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"unsupported commit_mode {profile.commit_mode!r}",
        )
    if profile.timeout is not None and profile.timeout <= 0:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            f"timeout must be positive: {profile.timeout!r}",
        )
    if profile.max_review_fix_cycles is not None and profile.max_review_fix_cycles < 0:
        raise LaunchError(
            LAUNCH_REASON_INVALID_PROFILE,
            "max_review_fix_cycles must be zero or a positive integer: "
            f"{profile.max_review_fix_cycles!r}",
        )
    return profile


def build_launch_argv(
    plan_dir: str | Path,
    *,
    profile: LaunchProfile | None = None,
    reply_via_stdin: bool = False,
    json_output: bool = True,
    state_dir: str | Path | None = None,
    python_executable: str | None = None,
    resume: bool = False,
) -> list[str]:
    """Return the argv for ``python -m agent_session_loop run ...``.

    The argv intentionally omits ``--reply``: reply text only ever flows
    through stdin via the matching ``--reply-stdin`` flag, so the operator
    answer never appears on the child process command line.

    ``resume`` adds ``--resume``, which makes the child rehydrate the harness,
    model, effort, and workspace binding this plan's ``run_config`` persisted.
    Explicit profile flags still win, so a caller can resume and override.
    """
    canonical = _validate_launch_plan_dir(plan_dir)
    validated = _validate_launch_profile(profile)
    validated, _workspace = _workspace_launch_profile(canonical, validated)
    interpreter = python_executable or sys.executable
    argv: list[str] = [interpreter, "-m", "agent_session_loop", "run", str(canonical)]
    if resume:
        argv.append("--resume")

    cwd_value = validated.cwd
    if cwd_value:
        argv.extend(["--cwd", cwd_value])
    if validated.harness is not None:
        argv.extend(["--harness", validated.harness])
    if validated.review_harness is not None:
        argv.extend(["--review-harness", validated.review_harness])
    if validated.harness_model is not None:
        argv.extend(["--harness-model", validated.harness_model])
    if validated.review_harness_model is not None:
        argv.extend(["--review-harness-model", validated.review_harness_model])
    if validated.effort is not None:
        argv.extend(["--effort", validated.effort])
    if validated.timeout is not None:
        argv.extend(["--timeout", str(validated.timeout)])
    if validated.max_review_fix_cycles is not None:
        argv.extend(["--max-review-fix-cycles", str(validated.max_review_fix_cycles)])
    if validated.commit_mode == "per-session":
        argv.append("--commit-per-session")
    elif validated.commit_mode == "end":
        argv.append("--commit-at-end")
    if validated.unsafe_bypass is False:
        argv.append("--no-unsafe-bypass")
    elif validated.unsafe_bypass is True:
        argv.append("--unsafe-bypass")
    if validated.allow_not_loop_ready is True:
        argv.append("--allow-not-loop-ready")
    if validated.note is not None:
        argv.extend(["--note", validated.note])
    if state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    if json_output:
        argv.append("--json")
    if reply_via_stdin:
        argv.append("--reply-stdin")
    return argv


def launch_plan(
    plan_dir: str | Path,
    *,
    profile: LaunchProfile | None = None,
    reply_text: str | None = None,
    state_dir: str | Path | None = None,
    json_output: bool = True,
    python_executable: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    popen_factory: Any = subprocess.Popen,
    resume: bool = False,
    isolate_locks: bool = False,
) -> LaunchResult:
    """Spawn a background ``python -m agent_session_loop run`` process.

    ``reply_text`` is streamed to the child's stdin when set so the operator
    answer never appears in argv, environment variables, or persisted state.
    The child still emits its normal ASL state, events, and artifacts.

    ``isolate_locks`` narrows the child's lock authorization to the plan being
    launched: it still takes that plan's lock and still refuses on an overlap,
    but it reclaims no other plan's stale lock. It defaults to false because an
    operator-driven launch is an ordinary start and wants the ordinary hygiene;
    a launcher authorized one plan at a time passes true, and the marker
    travels in the environment because the argv belongs to the resumed run.

    Validation errors raise :class:`LaunchError` before any subprocess is
    spawned, so callers can map them to immediate API errors.
    """
    canonical = _validate_launch_plan_dir(plan_dir)
    validated = _validate_launch_profile(profile)
    validated, workspace = _workspace_launch_profile(canonical, validated)
    reply_via_stdin = reply_text is not None
    reply_length: int | None = None
    if reply_via_stdin:
        assert reply_text is not None
        if not reply_text.strip():
            raise LaunchError(
                LAUNCH_REASON_EMPTY_REPLY,
                "reply text must contain non-whitespace content",
            )
        reply_length = len(reply_text)

    argv = build_launch_argv(
        canonical,
        profile=validated,
        reply_via_stdin=reply_via_stdin,
        json_output=json_output,
        state_dir=state_dir,
        python_executable=python_executable,
        resume=resume,
    )

    canonical_state_dir: Path | None = None
    if state_dir is not None:
        canonical_state_dir = Path(state_dir).expanduser().resolve()

    child_cwd = Path(cwd).expanduser().resolve() if cwd is not None else None
    # Every caller-provided entry is preserved; only the origin marker is the
    # launcher's to set, and it is materialized even when no mapping was passed
    # so an inherited environment still attributes the child to the Workbench.
    popen_env = cli_trace.child_env(
        os.environ if env is None else env, cli_trace.ORIGIN_WORKBENCH
    )
    if isolate_locks:
        popen_env = run_contention.isolated_child_env(popen_env)

    stdin_pipe = subprocess.PIPE if reply_via_stdin else subprocess.DEVNULL
    try:
        process = popen_factory(
            argv,
            stdin=stdin_pipe,
            stdout=stdout,
            stderr=stderr,
            env=popen_env,
            cwd=str(child_cwd) if child_cwd is not None else None,
            text=bool(reply_via_stdin),
            # The run outlives whoever started it. A Workbench server stopped
            # with Ctrl-C, a bridge whose connector went away, a terminal
            # closing -- each delivers its signal to a process *group*, and a
            # child in the launcher's group would be killed mid-turn by an
            # event that has nothing to do with the plan. Its own session is
            # what makes an operator's run survive the operator's tooling.
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        raise LaunchError(
            LAUNCH_REASON_PROCESS_FAILED,
            f"failed to spawn agent_session_loop run: {error}",
        ) from error

    if reply_via_stdin:
        assert reply_text is not None
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(reply_text)
            finally:
                process.stdin.close()
        except (OSError, BrokenPipeError) as error:
            try:
                process.kill()
            finally:
                process.wait()
            raise LaunchError(
                LAUNCH_REASON_PROCESS_FAILED,
                f"failed to stream reply text to child stdin: {error}",
                # A child existed here, and killing it is not the same as never
                # having started it: it may have reached the plan first.
                spawned=True,
            ) from error

    return LaunchResult(
        pid=process.pid,
        argv=tuple(argv),
        plan_dir=canonical,
        profile_payload=validated.to_payload(),
        reply_provided=reply_via_stdin,
        reply_length=reply_length,
        reply_origin="stdin" if reply_via_stdin else None,
        state_dir=canonical_state_dir,
        cwd=validated.cwd,
        workspace_root=workspace.get("workspace_root"),
        registry_digest=workspace.get("registry_digest"),
        repositories=workbench_workspace._repository_rows(
            workspace.get("repositories")
        ),
    )


def resume_plan_with_reply(
    plan_dir: str | Path,
    *,
    reply_text: str,
    profile: LaunchProfile | None = None,
    state_dir: str | Path | None = None,
    json_output: bool = True,
    python_executable: str | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    popen_factory: Any = subprocess.Popen,
) -> LaunchResult:
    """Launch ``asl run --resume`` for a paused plan, streaming the reply to stdin.

    Resuming without ``--resume`` would start the paused plan under whichever
    defaults the launching process happens to carry rather than the harness,
    model, effort, and workspace binding the original run persisted.
    """
    from . import intervention

    return intervention.resume_paused_run(
        plan_dir,
        reply_text=reply_text,
        profile=profile,
        state_dir=state_dir,
        json_output=json_output,
        python_executable=python_executable,
        env=env,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        popen_factory=popen_factory,
    )
