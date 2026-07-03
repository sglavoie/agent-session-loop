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

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import fcntl
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Literal

from . import config, events, harnesses, plans, preflight, qna, responses, state


FINAL_REVIEW_FILE_NAME = "final-review.md"
LAST_MESSAGE_PREVIEW_CHARS = 280


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
    "run-finished",
    "run-started",
    "lock-created",
    "progress-journal",
]
LAST_ACTIVITY_SOURCES: tuple[LastActivitySource, ...] = (
    "active-verification",
    "active-turn",
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


REGISTRY_FILE_NAME = "workbench.json"
REGISTRY_SCHEMA_VERSION = 1
DEFAULT_PLANS_DIR_NAME = "agent-plans"

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


class PlanDeleteError(Exception):
    """Raised when a Workbench plan deletion cannot be completed."""

    def __init__(self, reason: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


@dataclass(frozen=True)
class LaunchProfile:
    """Operator launch defaults for one pinned plan.

    All fields are optional. ``None`` means "no preference recorded";
    consumers should fall back to their normal defaults.
    """

    cwd: str | None = None
    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout: int | None = None
    commit_mode: CommitMode | None = None
    review_harness: str | None = None
    unsafe_bypass: bool | None = None
    allow_not_loop_ready: bool | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "harness": self.harness,
            "model": self.model,
            "effort": self.effort,
            "timeout": self.timeout,
            "commit_mode": self.commit_mode,
            "review_harness": self.review_harness,
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
    path: Path | None = None
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pins": [pin.to_payload() for pin in self.pins],
            "plan_roots": list(self.plan_roots),
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


@dataclass(frozen=True)
class DeletePlanResult:
    """Metadata returned after deleting a Workbench-managed plan."""

    plan_dir: Path
    plan_removed: bool
    state_dir: Path
    state_removed: bool
    pin_removed: bool
    registry_path: Path

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "plan_removed": self.plan_removed,
            "state_dir": str(self.state_dir),
            "state_removed": self.state_removed,
            "pin_removed": self.pin_removed,
            "registry_path": str(self.registry_path),
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
    except (OSError, ValueError):
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
    raw: Any, *, warnings: list[str], context: str
) -> LaunchProfile:
    if raw is None:
        return LaunchProfile()
    if not isinstance(raw, Mapping):
        warnings.append(f"{context}: profile must be an object; ignoring")
        return LaunchProfile()
    return LaunchProfile(
        cwd=_coerce_optional_str(raw.get("cwd")),
        harness=_coerce_harness(
            raw.get("harness"), warnings=warnings, context=context
        ),
        model=_coerce_optional_str(raw.get("model")),
        effort=_coerce_optional_str(raw.get("effort")),
        timeout=_coerce_optional_int(raw.get("timeout")),
        commit_mode=_coerce_commit_mode(
            raw.get("commit_mode"), warnings=warnings, context=context
        ),
        review_harness=_coerce_harness(
            raw.get("review_harness"), warnings=warnings, context=context
        ),
        unsafe_bypass=_coerce_optional_bool(raw.get("unsafe_bypass")),
        allow_not_loop_ready=_coerce_optional_bool(raw.get("allow_not_loop_ready")),
        note=_coerce_optional_str(raw.get("note")),
    )


def _coerce_pin(raw: Any, *, warnings: list[str], index: int) -> RegistryPin | None:
    context = f"pin[{index}]"
    if not isinstance(raw, Mapping):
        warnings.append(f"{context}: entry must be an object; skipping")
        return None
    plan_dir = _canonical_plan_dir(raw.get("plan_dir"))
    if plan_dir is None:
        warnings.append(f"{context}: missing or invalid plan_dir; skipping")
        return None
    label = _coerce_optional_str(raw.get("label"))
    profile = _coerce_profile(raw.get("profile"), warnings=warnings, context=context)
    return RegistryPin(plan_dir=plan_dir, label=label, profile=profile)


def _coerce_plan_roots(
    raw: Any, *, warnings: list[str]
) -> tuple[str, ...]:
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
            warnings.append(
                f"plan_roots[{index}]: invalid path {entry!r}; skipping"
            )
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
    schema_version = _coerce_schema_version(raw.get("schema_version"), warnings=warnings)

    raw_pins = raw.get("pins", [])
    if raw_pins is None:
        raw_pins = []
    if not isinstance(raw_pins, Sequence) or isinstance(raw_pins, (str, bytes)):
        warnings.append("pins: must be a list; ignoring")
        raw_pins = []

    pins: list[RegistryPin] = []
    seen_plan_dirs: set[str] = set()
    for index, raw_pin in enumerate(raw_pins):
        pin = _coerce_pin(raw_pin, warnings=warnings, index=index)
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

    return WorkbenchRegistry(
        schema_version=schema_version,
        pins=tuple(pins),
        plan_roots=plan_roots,
        path=registry_path,
        warnings=tuple(warnings),
    )


def save_registry(
    registry: WorkbenchRegistry, path: str | Path | None = None
) -> Path:
    """Write the registry atomically, returning the path that was written."""
    registry_path = _coerce_registry_path(
        path if path is not None else registry.path
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "pins": [pin.to_payload() for pin in registry.pins],
        "plan_roots": list(registry.plan_roots),
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{REGISTRY_FILE_NAME}.",
        dir=str(registry_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_path, registry_path)
    except OSError as error:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
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


def remove_pin(
    registry: WorkbenchRegistry, plan_dir: str | Path
) -> WorkbenchRegistry:
    """Return a new registry without any pin for ``plan_dir``."""
    canonical = _canonical_plan_dir(str(plan_dir))
    if canonical is None:
        raise WorkbenchError(f"invalid plan_dir for pin removal: {plan_dir!r}")
    remaining = tuple(pin for pin in registry.pins if pin.plan_dir != canonical)
    return replace(registry, pins=remaining)


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


def workbench_pin_from_args(args: Any) -> int:
    """Pin ``args.plan_dir`` in the default registry from CLI inputs."""
    plan_dir = args.plan_dir
    label = getattr(args, "label", None)
    cwd = getattr(args, "cwd", None)
    profile = LaunchProfile(cwd=cwd) if cwd is not None else None
    try:
        updated, written_path = mutate_registry(
            None,
            lambda current: upsert_pin(
                current, plan_dir, label=label, profile=profile
            ),
        )
    except WorkbenchError as error:
        print(
            f"agent-session-loop workbench pin: {error}",
            file=sys.stderr,
        )
        return 1
    matching = next(
        (pin for pin in updated.pins if pin.plan_dir == _canonical_plan_dir(plan_dir)),
        None,
    )
    resolved = matching.plan_dir if matching is not None else plan_dir
    print(f"Pinned {resolved} in {written_path}")
    return 0


def workbench_unpin_from_args(args: Any) -> int:
    """Remove ``args.plan_dir`` from the default registry."""
    plan_dir = args.plan_dir
    try:
        _, written_path = mutate_registry(
            None,
            lambda current: remove_pin(current, plan_dir),
        )
    except WorkbenchError as error:
        print(
            f"agent-session-loop workbench unpin: {error}",
            file=sys.stderr,
        )
        return 1
    print(f"Unpinned {plan_dir} from {written_path}")
    return 0


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
                warnings.append(
                    f"{context}: could not canonicalize {child}: {error}"
                )
    return tuple(found)


def _iter_state_plan_dirs(
    state_root: Path, *, warnings: list[str]
) -> tuple[Path, ...]:
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
            except (OSError, json.JSONDecodeError):
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


def _is_discovered_plan(
    plan_dir: Path,
    *,
    registry: WorkbenchRegistry,
    state_dir: str | Path | None,
    plan_roots: Iterable[str | Path],
    include_default_dirs: bool,
) -> bool:
    discovery = discover_plans(
        state_root=state_dir,
        registry=registry,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    return any(discovered.plan_dir == plan_dir for discovered in discovery.plans)


def delete_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> DeletePlanResult:
    """Permanently delete a Workbench-visible plan and its local metadata."""
    try:
        canonical = plans.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise PlanDeleteError(
            "invalid-plan",
            f"invalid plan directory {plan_dir!r}: {error}",
        ) from error

    try:
        registry = load_registry(registry_path)
    except WorkbenchError as error:
        raise PlanDeleteError(
            "registry-unreadable", str(error), status=500
        ) from error

    discovered = _is_discovered_plan(
        canonical,
        registry=registry,
        state_dir=state_dir,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    if not discovered and not _looks_like_plan_dir(canonical):
        raise PlanDeleteError(
            "invalid-plan",
            f"refusing to delete non-plan path: {canonical}",
        )

    try:
        lock_info = state.read_lock_info(canonical, state_dir)
    except (OSError, ValueError) as error:
        raise PlanDeleteError(
            "state-error",
            f"cannot inspect plan lock for {canonical}: {error}",
        ) from error
    if lock_info is not None and lock_info.active:
        raise PlanDeleteError(
            "active-lock",
            f"active lock held by pid {lock_info.pid} at {lock_info.path}",
            status=409,
        )

    if canonical.exists() and not canonical.is_dir():
        raise PlanDeleteError(
            "invalid-plan",
            f"plan path is not a directory: {canonical}",
        )

    plan_state_dir = state.plan_state_dir(canonical, state_dir)
    state_removed = False
    try:
        if plan_state_dir.is_dir():
            shutil.rmtree(plan_state_dir)
            state_removed = True
        elif plan_state_dir.exists():
            plan_state_dir.unlink()
            state_removed = True
    except OSError as error:
        raise PlanDeleteError(
            "os-error",
            f"cannot delete plan state {plan_state_dir}: {error}",
            status=500,
        ) from error

    plan_removed = False
    if canonical.is_dir():
        try:
            shutil.rmtree(canonical)
        except OSError as error:
            raise PlanDeleteError(
                "os-error",
                f"cannot delete plan directory {canonical}: {error}",
                status=500,
            ) from error
        plan_removed = True

    pin_removed = False

    def remove_matching_pin(current: WorkbenchRegistry) -> WorkbenchRegistry:
        nonlocal pin_removed
        pin_removed = any(pin.plan_dir == str(canonical) for pin in current.pins)
        return remove_pin(current, canonical)

    try:
        _updated, written_path = mutate_registry(registry_path, remove_matching_pin)
    except WorkbenchError as error:
        raise PlanDeleteError(
            "registry-unreadable", str(error), status=500
        ) from error

    return DeletePlanResult(
        plan_dir=canonical,
        plan_removed=plan_removed,
        state_dir=plan_state_dir,
        state_removed=state_removed,
        pin_removed=pin_removed,
        registry_path=written_path,
    )


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
    pause_reason: str | None
    pause_message: str | None
    last_message_preview: str | None
    urgency_rank: int = 0
    urgency_group: UrgencyGroup = "needs-action"
    last_activity_at: str | None = None
    last_activity_source: LastActivitySource | None = None
    activity_label: str = ""
    concise_reason: str = ""
    searchable_text: str = ""
    recovery: dict[str, Any] = field(default_factory=dict)
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
            "searchable_text": self.searchable_text,
            "recovery": dict(self.recovery),
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
            "progress_path": (
                str(self.progress_path) if self.progress_path else None
            ),
            "final_review_path": (
                str(self.final_review_path) if self.final_review_path else None
            ),
            "artifact_dir": (
                str(self.artifact_dir) if self.artifact_dir else None
            ),
            "stdout_path": str(self.stdout_path) if self.stdout_path else None,
            "stderr_path": str(self.stderr_path) if self.stderr_path else None,
            "last_message_path": (
                str(self.last_message_path) if self.last_message_path else None
            ),
            "result_path": str(self.result_path) if self.result_path else None,
            "run_id": self.run_id,
            "harness": self.harness,
            "pause_reason": self.pause_reason,
            "pause_message": self.pause_message,
            "last_message_preview": self.last_message_preview,
            "warnings": list(self.warnings),
        }


RecoveryKind = Literal[
    "none",
    "reply-needed",
    "not-loop-ready",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    "stale-lock",
    "stale-state",
    "commit-preflight",
    "commit-failed",
    "generic",
]
RECOVERY_KINDS: tuple[RecoveryKind, ...] = (
    "none",
    "reply-needed",
    "not-loop-ready",
    "verify-failed",
    "no-contract",
    "malformed-contract",
    "stale-lock",
    "stale-state",
    "commit-preflight",
    "commit-failed",
    "generic",
)


PAUSE_REASON_RECOVERY_KIND_BY_REASON: dict[str, RecoveryKind] = {
    "blocked": "reply-needed",
    "commit-failed": "commit-failed",
    "commit-preflight": "commit-preflight",
    "dependent": "reply-needed",
    "malformed-contract": "malformed-contract",
    "no-contract": "no-contract",
    "not-loop-ready": "not-loop-ready",
    "runner-pause": "reply-needed",
    "unresolved-prerequisite": "reply-needed",
    "verify-failed": "verify-failed",
}


GENERIC_RECOVERY_PAUSE_REASONS: tuple[str, ...] = (
    "nonzero-exit",
    "timeout",
    "no-status-change",
    "multiple-status-changes",
    "malformed-plan",
    "unknown-status",
    "lock-contention",
    "unexpected-status-change",
    "no-pending-but-incomplete",
    "harness-unavailable",
    "unsafe",
    "interrupted",
    "review-harness-required",
    "review-mutated-worktree",
)


RecoverySeverity = Literal["none", "info", "warning", "error"]
RecoveryEvidenceSeverity = Literal["info", "warning", "error"]
RecoveryWarningSeverity = Literal["info", "warning", "error"]
RecoveryWarningCode = Literal[
    "state-missing-race",
    "state-unreadable",
    "state-malformed-json",
    "state-invalid-json-shape",
]
RecoveryActionKind = Literal["endpoint", "inspect", "navigate"]
RecoveryActionTarget = Literal[
    "reply",
    "events",
    "active",
    "verification",
    "progress",
    "artifacts",
    "stdout",
    "stderr",
    "profile",
    "launch",
    "unlock",
]
RECOVERY_ACTION_TARGETS: tuple[RecoveryActionTarget, ...] = (
    "reply",
    "events",
    "active",
    "verification",
    "progress",
    "artifacts",
    "stdout",
    "stderr",
    "profile",
    "launch",
    "unlock",
)


@dataclass(frozen=True)
class RecoveryWarning:
    """Structured warning metadata paired with legacy warning strings."""

    code: RecoveryWarningCode
    message: str
    severity: RecoveryWarningSeverity = "warning"

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class RecoveryEvidenceRow:
    """Small diagnosis row suitable for a compact recovery card."""

    label: str
    value: str | None
    detail: str | None = None
    severity: RecoveryEvidenceSeverity = "info"

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class RecoveryArtifactReference:
    """Reference to an existing ASL state, log, result, or artifact path."""

    label: str
    path: Path
    kind: str
    source: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": str(self.path),
            "kind": self.kind,
            "source": self.source,
        }


@dataclass(frozen=True)
class RecoveryCopyCommand:
    """Command text for the operator to copy; Workbench never executes it."""

    label: str
    command: str
    description: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "command": self.command,
            "description": self.description,
        }


@dataclass(frozen=True)
class RecoveryActionCard:
    """UI action backed only by existing Workbench affordances."""

    id: str
    label: str
    description: str
    kind: RecoveryActionKind
    target: RecoveryActionTarget | None = None
    endpoint: str | None = None
    method: str | None = None
    payload: Mapping[str, Any] | None = None
    disabled: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "kind": self.kind,
            "target": self.target,
            "endpoint": self.endpoint,
            "method": self.method,
            "payload": dict(self.payload) if self.payload is not None else None,
            "disabled": self.disabled,
        }


@dataclass(frozen=True)
class WorkbenchRecovery:
    """Read-only recovery diagnosis for the Workbench detail view."""

    plan_dir: Path
    kind: RecoveryKind
    title: str
    summary: str
    severity: RecoverySeverity
    recoverable: bool
    evidence: tuple[RecoveryEvidenceRow, ...] = ()
    actions: tuple[RecoveryActionCard, ...] = ()
    commands: tuple[RecoveryCopyCommand, ...] = ()
    artifacts: tuple[RecoveryArtifactReference, ...] = ()
    run_id: str | None = None
    session_id: str | None = None
    turn_index: int | None = None
    commit_index: int | None = None
    warnings: tuple[str, ...] = ()
    warning_details: tuple[RecoveryWarning, ...] = ()

    def compact_payload(self) -> dict[str, Any]:
        primary_action = self.actions[0] if self.actions else None
        return {
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "recoverable": self.recoverable,
            "primary_action": (
                primary_action.to_payload()
                if primary_action is not None
                else None
            ),
            "artifact_count": len(self.artifacts),
            "command_count": len(self.commands),
            "warning_count": len(self.warnings),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "recoverable": self.recoverable,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "commit_index": self.commit_index,
            "evidence": [row.to_payload() for row in self.evidence],
            "actions": [action.to_payload() for action in self.actions],
            "commands": [command.to_payload() for command in self.commands],
            "artifacts": [artifact.to_payload() for artifact in self.artifacts],
            "warnings": list(self.warnings),
            "warning_details": [
                warning.to_payload() for warning in self.warning_details
            ],
            "compact": self.compact_payload(),
        }


@dataclass(frozen=True)
class _RecoveryBuildContext:
    """State gathered once for one recovery payload build."""

    snapshot: plans.PlanSnapshot
    state_path: Path
    events_path: Path
    progress_path: Path
    lock_path: Path
    lock_info: state.LockInfo | None
    runner_state: Mapping[str, Any]
    raw_state: Mapping[str, Any]
    pause_reason: str | None
    pause_details: Mapping[str, Any]
    pause_message: str
    base_artifacts: tuple[RecoveryArtifactReference, ...]
    plan_dir_payload: Mapping[str, Any]
    stale_unfinished: bool
    stale_reason: str | None
    warnings: tuple[str, ...]
    warning_details: tuple[RecoveryWarning, ...]


def _coerce_path_value(value: Any) -> Path | None:
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _truncate_preview(text: str, *, limit: int = LAST_MESSAGE_PREVIEW_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"


def _session_identifier_from_payload(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("session_id")
    if isinstance(value, str) and value:
        return value
    fallback = payload.get("session")
    if isinstance(fallback, str) and fallback:
        return fallback
    if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback > 0:
        return str(fallback)
    return None


def _verification_position(active_verification: Mapping[str, Any]) -> str:
    check_count = active_verification.get("check_count")
    check_index = active_verification.get("current_check_index")
    if check_index is None:
        return f"-/{check_count}" if check_count is not None else "-"
    try:
        position = int(check_index) + 1
    except (TypeError, ValueError):
        return "-"
    if check_count is not None:
        return f"{position}/{check_count}"
    return str(position)


def _summary_base_paths(
    plan_dir: Path, state_dir: str | Path | None
) -> tuple[Path, Path, Path]:
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    return (
        plan_state / events.STATE_FILE_NAME,
        plan_state / events.EVENTS_FILE_NAME,
        plans.progress_path(plan_dir),
    )


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


def _payload_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _payload_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _payload_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return value
    return None


def _payload_mappings(payload: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _read_recovery_state_payload(
    state_path: Path,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[RecoveryWarning, ...]]:
    if not state_path.exists():
        return {}, (), ()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        message = "state.json disappeared before it could be read"
        warning = RecoveryWarning(code="state-missing-race", message=message)
        return {}, (message,), (warning,)
    except json.JSONDecodeError as error:
        message = f"state.json unreadable: {error}"
        warning = RecoveryWarning(code="state-malformed-json", message=message)
        return {}, (message,), (warning,)
    except OSError as error:
        message = f"state.json unreadable: {error}"
        warning = RecoveryWarning(code="state-unreadable", message=message)
        return {}, (message,), (warning,)
    if not isinstance(payload, dict):
        message = "state.json did not contain a JSON object"
        warning = RecoveryWarning(code="state-invalid-json-shape", message=message)
        return {}, (message,), (warning,)
    return payload, (), ()


def _recovery_base_artifacts(
    *,
    state_path: Path,
    events_path: Path,
    progress_path: Path,
) -> tuple[RecoveryArtifactReference, ...]:
    return (
        RecoveryArtifactReference(
            label="Runner state",
            path=state_path,
            kind="state",
            source="plan-state",
        ),
        RecoveryArtifactReference(
            label="Runner events",
            path=events_path,
            kind="events",
            source="plan-state",
        ),
        RecoveryArtifactReference(
            label="Progress journal",
            path=progress_path,
            kind="progress",
            source="plan",
        ),
    )


def _build_recovery_context(
    snapshot: plans.PlanSnapshot,
    state_dir: str | Path | None,
) -> _RecoveryBuildContext:
    state_path, events_path, progress_path = _summary_base_paths(
        snapshot.plan_dir, state_dir
    )
    lock_path = state.lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = state._read_lock_path(lock_path, snapshot.plan_dir)
    runner_state = state._runner_state_payload(state_path)
    raw_state, warnings, warning_details = _read_recovery_state_payload(state_path)
    stale_unfinished, stale_reason = state._stale_runner_state(
        runner_state, lock_info
    )
    base_artifacts = _recovery_base_artifacts(
        state_path=state_path,
        events_path=events_path,
        progress_path=progress_path,
    )
    reason = _pause_reason(raw_state, runner_state)
    return _RecoveryBuildContext(
        snapshot=snapshot,
        state_path=state_path,
        events_path=events_path,
        progress_path=progress_path,
        lock_path=lock_path,
        lock_info=lock_info,
        runner_state=runner_state,
        raw_state=raw_state,
        pause_reason=reason,
        pause_details=_pause_details(raw_state),
        pause_message=_pause_message(raw_state) or reason or "Paused",
        base_artifacts=base_artifacts,
        plan_dir_payload={"plan_dir": str(snapshot.plan_dir)},
        stale_unfinished=stale_unfinished,
        stale_reason=stale_reason,
        warnings=warnings,
        warning_details=warning_details,
    )


_RECOVERY_ARTIFACT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("artifact_dir", "artifact-dir", "Artifact directory"),
    ("stdout_path", "stdout", "Stdout"),
    ("stderr_path", "stderr", "Stderr"),
    ("last_message_path", "last-message", "Last message"),
    ("result_path", "result", "Result JSON"),
)


def _artifact_references_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    source: str,
    label_prefix: str,
) -> tuple[RecoveryArtifactReference, ...]:
    if payload is None:
        return ()
    refs: list[RecoveryArtifactReference] = []
    for key, kind, label in _RECOVERY_ARTIFACT_FIELDS:
        path = _coerce_path_value(payload.get(key))
        if path is None:
            continue
        refs.append(
            RecoveryArtifactReference(
                label=f"{label_prefix} {label}".strip(),
                path=path,
                kind=kind,
                source=source,
            )
        )
    return tuple(refs)


def _latest_turn_matching(
    raw_state: Mapping[str, Any],
    predicate: Callable[[Mapping[str, Any]], bool],
) -> Mapping[str, Any] | None:
    active_turn = _payload_mapping(raw_state, "active_turn")
    if active_turn is not None and predicate(active_turn):
        return active_turn
    for turn in reversed(_payload_mappings(raw_state, "turns")):
        if predicate(turn):
            return turn
    return None


def _turn_has_verification_reason(
    turn: Mapping[str, Any],
    reason: str,
) -> bool:
    verification = _payload_mapping(turn, "verification")
    return verification is not None and verification.get("reason") == reason


def _turn_failed(turn: Mapping[str, Any]) -> bool:
    if _payload_mapping(turn, "verification") is not None:
        verification = _payload_mapping(turn, "verification")
        if verification is not None and verification.get("passed") is False:
            return True
    status_result = _payload_str(turn, "status_result")
    if status_result and status_result not in {"complete", "unknown"}:
        return True
    return (
        turn.get("timed_out") is True
        or turn.get("interrupted") is True
        or _payload_int(turn, "returncode") not in {None, 0}
        or _payload_str(turn, "plan_error") is not None
    )


def _turn_paused(turn: Mapping[str, Any]) -> bool:
    return (
        _payload_str(turn, "runner_pause") is not None
        or _payload_str(turn, "action_required") is not None
        or _payload_str(turn, "last_message_path") is not None
    )


def _latest_failed_commit(raw_state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    pause_details = _pause_details(raw_state)
    commit = _payload_mapping(pause_details, "commit")
    if commit is not None and commit.get("success") is False:
        return commit
    for candidate in reversed(_payload_mappings(raw_state, "commits")):
        if candidate.get("success") is False:
            return candidate
    return None


def _latest_relevant_turn(
    raw_state: Mapping[str, Any],
    *,
    kind: RecoveryKind,
    reason: str | None,
) -> Mapping[str, Any] | None:
    if kind in {"verify-failed", "no-contract", "malformed-contract"}:
        if reason:
            turn = _latest_turn_matching(
                raw_state,
                lambda candidate: _turn_has_verification_reason(candidate, reason),
            )
            if turn is not None:
                return turn
        turn = _latest_turn_matching(
            raw_state,
            lambda candidate: _payload_mapping(candidate, "verification")
            is not None,
        )
        if turn is not None:
            return turn
    if kind == "reply-needed":
        turn = _latest_turn_matching(raw_state, _turn_paused)
        if turn is not None:
            return turn
    if kind == "generic":
        active_turn = _payload_mapping(raw_state, "active_turn")
        if active_turn is not None:
            return active_turn
    return _latest_turn_matching(raw_state, _turn_failed)


def _pause_reason(
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
) -> str | None:
    for payload in (raw_state, runner_state):
        for key in ("pause_reason", "reason"):
            value = _payload_str(payload, key)
            if value is not None:
                return value
    return None


def _pause_reason_recovery_kind(reason: str | None) -> RecoveryKind | None:
    if reason is None:
        return None
    kind = PAUSE_REASON_RECOVERY_KIND_BY_REASON.get(reason)
    if kind is not None:
        return kind
    if reason in GENERIC_RECOVERY_PAUSE_REASONS:
        return "generic"
    return None


def _pause_message(raw_state: Mapping[str, Any]) -> str | None:
    return _payload_str(raw_state, "pause_message")


def _pause_details(raw_state: Mapping[str, Any]) -> Mapping[str, Any]:
    details = _payload_mapping(raw_state, "pause_details")
    if details is not None:
        return details
    details = _payload_mapping(raw_state, "details")
    if details is not None:
        return details
    return {}


def _runtime_failure_reason(
    *,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
) -> str | None:
    for failure in _payload_mappings(details, "failures"):
        failure_reason = _payload_str(failure, "failure_reason")
        if failure_reason is not None:
            return failure_reason
    if verification is not None:
        for check in _payload_mappings(verification, "checks"):
            status = _payload_str(check, "status")
            failure_reason = _payload_str(check, "failure_reason")
            if failure_reason is not None and status != "pass":
                return failure_reason
        for check in _payload_mappings(verification, "checks"):
            failure_reason = _payload_str(check, "failure_reason")
            if failure_reason is not None:
                return failure_reason
    return None


def _hint_evidence_row(hint: preflight.RepairHint | None) -> RecoveryEvidenceRow | None:
    if hint is None:
        return None
    severity: RecoveryEvidenceSeverity
    if hint.severity_hint == "red":
        severity = "error"
    elif hint.severity_hint == "yellow":
        severity = "warning"
    else:
        severity = "info"
    return RecoveryEvidenceRow(
        label="Repair hint",
        value=hint.message,
        detail=hint.check_id,
        severity=severity,
    )


def _evidence_severity(value: Any) -> RecoveryEvidenceSeverity:
    if value == "red":
        return "error"
    if value == "yellow":
        return "warning"
    return "info"


def _readiness_gate_evidence(
    gate: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = [
        RecoveryEvidenceRow(
            label="Readiness state",
            value=(
                str(gate.get("readiness_state"))
                if gate.get("readiness_state") is not None
                else None
            ),
            severity="warning",
        )
    ]
    expected = _payload_str(gate, "expected_readiness_sha256")
    recorded = _payload_str(gate, "recorded_readiness_sha256")
    if expected is not None or recorded is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Readiness SHA",
                value=recorded,
                detail=f"expected {expected}" if expected else None,
                severity="warning" if expected != recorded else "info",
            )
        )
    counts = _payload_mapping(gate, "findings_by_severity")
    if counts is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Findings",
                value=(
                    f"red {counts.get('red', 0)}, "
                    f"yellow {counts.get('yellow', 0)}, "
                    f"green {counts.get('green', 0)}"
                ),
                severity=(
                    "error"
                    if counts.get("red")
                    else "warning"
                    if counts.get("yellow")
                    else "info"
                ),
            )
        )
    findings = _payload_mappings(gate, "findings")
    for finding in findings[:5]:
        category = _payload_str(finding, "category") or "finding"
        message = _payload_str(finding, "message")
        severity_value = _payload_str(finding, "severity")
        rows.append(
            RecoveryEvidenceRow(
                label=f"{severity_value or 'info'} finding",
                value=category,
                detail=message,
                severity=_evidence_severity(severity_value),
            )
        )
    if len(findings) > 5:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional findings",
                value=str(len(findings) - 5),
                severity="warning",
            )
        )
    return tuple(rows)


def _endpoint_action(
    *,
    action_id: str,
    label: str,
    description: str,
    endpoint: str,
    payload: Mapping[str, Any],
    target: RecoveryActionTarget | None = None,
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id=action_id,
        label=label,
        description=description,
        kind="endpoint",
        target=target,
        endpoint=endpoint,
        method="POST",
        payload=payload,
    )


def _inspect_action(
    description: str,
    *,
    target: RecoveryActionTarget = "artifacts",
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id="inspect-artifacts",
        label="Inspect artifacts",
        description=description,
        kind="inspect",
        target=target,
    )


def _navigate_action(
    *,
    action_id: str,
    label: str,
    description: str,
    target: RecoveryActionTarget,
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id=action_id,
        label=label,
        description=description,
        kind="navigate",
        target=target,
    )


def _retry_run_action(plan_dir_payload: Mapping[str, Any]) -> RecoveryActionCard:
    return _endpoint_action(
        action_id="retry-run",
        label="Retry run",
        description=(
            "Launch the existing runner flow after applying the recovery "
            "guidance."
        ),
        endpoint="/api/launch",
        payload=plan_dir_payload,
        target="launch",
    )


def _profile_action(description: str) -> RecoveryActionCard:
    return _navigate_action(
        action_id="review-profile",
        label="Review profile",
        description=description,
        target="profile",
    )


def _quote_command_path(path: Path | str) -> str:
    return shlex.quote(str(path))


def _preflight_repair_commands(plan_dir: Path) -> tuple[RecoveryCopyCommand, ...]:
    quoted_plan = _quote_command_path(plan_dir)
    quoted_skill_plan = _quote_command_path(str(plan_dir) + os.sep)
    return (
        RecoveryCopyCommand(
            label="Repair readiness",
            command=f"asl preflight {quoted_plan} --mark-loop-ready",
            description=(
                "Copy this into a shell after repairing plan-readiness "
                "findings."
            ),
        ),
        RecoveryCopyCommand(
            label="Iterate preflight",
            command=f"$plan-preflight-iterate {quoted_skill_plan}",
            description=(
                "Copy this into the harness when preflight findings need an "
                "interactive repair pass."
            ),
        ),
    )


def _verification_repair_commands(
    plan_dir: Path,
    *,
    session_id: str | None,
) -> tuple[RecoveryCopyCommand, ...]:
    quoted_plan = _quote_command_path(plan_dir)
    commands = [
        RecoveryCopyCommand(
            label="Run preflight",
            command=f"asl preflight {quoted_plan} --json",
            description=(
                "Copy this into a shell to inspect current contract and "
                "readiness findings."
            ),
        )
    ]
    if session_id:
        commands.insert(
            0,
            RecoveryCopyCommand(
                label="Re-run verification",
                command=(
                    "asl verify "
                    f"{quoted_plan} --session {shlex.quote(session_id)} --json"
                ),
                description=(
                    "Copy this into a shell after repairing the session or "
                    "its Completion Contract."
                ),
            ),
        )
    return tuple(commands)


def _session_file_path(plan_dir: Path, session_id: str | None) -> Path | None:
    if not session_id:
        return None
    try:
        return plan_dir / plans.session_id_to_filename(session_id)
    except ValueError:
        return None


def _session_reference_evidence(
    plan_dir: Path,
    *,
    session_id: str | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    rows: list[RecoveryEvidenceRow] = []
    if session_id:
        rows.append(
            RecoveryEvidenceRow(
                label="Session",
                value=session_id,
                severity="error",
            )
        )
    session_path = _session_file_path(plan_dir, session_id)
    if session_path is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Session file",
                value=str(session_path),
                severity="error",
            )
        )
    return tuple(rows)


def _verification_check_evidence(
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    if verification is None:
        return ()
    rows: list[RecoveryEvidenceRow] = []
    failed_checks = [
        check
        for check in _payload_mappings(verification, "checks")
        if check.get("status") != "pass" or _payload_str(check, "failure_reason")
    ]
    for check in failed_checks[:5]:
        name = _payload_str(check, "name") or "check"
        failure_reason = _payload_str(check, "failure_reason")
        rows.append(
            RecoveryEvidenceRow(
                label="Failed check",
                value=name,
                detail=failure_reason,
                severity="error",
            )
        )
    if len(failed_checks) > 5:
        rows.append(
            RecoveryEvidenceRow(
                label="Additional failed checks",
                value=str(len(failed_checks) - 5),
                severity="error",
            )
        )
    return tuple(rows)


def _harness_config_artifacts(
    details: Mapping[str, Any],
) -> tuple[RecoveryArtifactReference, ...]:
    config_path = _coerce_path_value(details.get("config_path"))
    if config_path is None:
        return ()
    return (
        RecoveryArtifactReference(
            label="Harness config",
            path=config_path,
            kind="config",
            source="pause-details",
        ),
    )


def _harness_config_evidence(
    *,
    reason: str | None,
    message: str,
    details: Mapping[str, Any],
) -> tuple[RecoveryEvidenceRow, ...]:
    rows = [
        RecoveryEvidenceRow(
            label="Pause reason",
            value=reason,
            severity="error",
        ),
        RecoveryEvidenceRow(
            label="Pause message",
            value=message,
            severity="error",
        ),
    ]
    requested_harness = _payload_str(details, "requested_harness")
    if requested_harness is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Requested harness",
                value=requested_harness,
                severity="error",
            )
        )
    config_path = _payload_str(details, "config_path")
    if config_path is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Harness config",
                value=config_path,
                severity="warning",
            )
        )
    error = _payload_str(details, "error")
    if error is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Harness error",
                value=error,
                severity="error",
            )
        )
    return tuple(rows)


def _recovery_common_context(
    *,
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
    selected_turn: Mapping[str, Any] | None,
    selected_commit: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None, int | None, int | None]:
    run_id = (
        _payload_str(raw_state, "run_id")
        or _payload_str(runner_state, "run_id")
        or (
            _payload_str(selected_turn, "run_id")
            if selected_turn is not None
            else None
        )
        or (
            _payload_str(selected_commit, "run_id")
            if selected_commit is not None
            else None
        )
    )
    session_id = None
    if selected_turn is not None:
        session_id = _session_identifier_from_payload(selected_turn)
    if session_id is None and selected_commit is not None:
        session_id = _session_identifier_from_payload(selected_commit)
    if session_id is None:
        details = _pause_details(raw_state)
        session_id = _session_identifier_from_payload(details)
    turn_index = (
        _payload_int(selected_turn, "turn_index")
        if selected_turn is not None
        else None
    )
    commit_index = (
        _payload_int(selected_commit, "commit_index")
        if selected_commit is not None
        else None
    )
    return run_id, session_id, turn_index, commit_index


def _runtime_recovery_evidence(
    *,
    reason: str,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    rows = [
        RecoveryEvidenceRow(label="Pause reason", value=reason, severity="error")
    ]
    failure_reason = _runtime_failure_reason(
        details=details, verification=verification
    )
    if failure_reason is not None:
        rows.append(
            RecoveryEvidenceRow(
                label="Failure reason",
                value=failure_reason,
                severity="error",
            )
        )
    hint = preflight.runtime_repair_hint(
        reason,
        failure_reason=failure_reason,
    )
    hint_row = _hint_evidence_row(hint)
    if hint_row is not None:
        rows.append(hint_row)
    if verification is not None:
        passed = verification.get("passed")
        if isinstance(passed, bool):
            rows.append(
                RecoveryEvidenceRow(
                    label="Verification passed",
                    value=str(passed).lower(),
                    severity="info" if passed else "error",
                )
            )
    return tuple(rows)


def _verification_recovery_evidence(
    *,
    plan_dir: Path,
    reason: str,
    details: Mapping[str, Any],
    verification: Mapping[str, Any] | None,
    session_id: str | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    return (
        _runtime_recovery_evidence(
            reason=reason,
            details=details,
            verification=verification,
        )
        + _session_reference_evidence(plan_dir, session_id=session_id)
        + _verification_check_evidence(verification)
    )


_VERIFICATION_RECOVERY_TITLES: dict[str, str] = {
    "verify-failed": "Verification failed",
    "no-contract": "Completion contract missing",
    "malformed-contract": "Completion contract malformed",
}


def _verification_recovery_title(reason: str) -> str:
    return _VERIFICATION_RECOVERY_TITLES[reason]


def _selected_verification_context(
    raw_state: Mapping[str, Any],
    details: Mapping[str, Any],
    *,
    reason: str,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
    selected_turn = _latest_relevant_turn(
        raw_state,
        kind=reason,  # type: ignore[arg-type]
        reason=reason,
    )
    verification = (
        _payload_mapping(selected_turn, "verification")
        if selected_turn is not None
        else None
    )
    selected_session_id = (
        _session_identifier_from_payload(selected_turn)
        if selected_turn is not None
        else _session_identifier_from_payload(details)
    )
    return selected_turn, verification, selected_session_id


def _commit_failed_evidence(
    failed_commit: Mapping[str, Any] | None,
) -> tuple[RecoveryEvidenceRow, ...]:
    status_result = (
        _payload_str(failed_commit, "status_result")
        if failed_commit is not None
        else None
    )
    returncode = (
        _payload_int(failed_commit, "returncode")
        if failed_commit is not None
        else None
    )
    return (
        RecoveryEvidenceRow(
            label="Pause reason",
            value="commit-failed",
            severity="error",
        ),
        RecoveryEvidenceRow(
            label="Commit status",
            value=status_result,
            detail=(
                f"return code {returncode}"
                if returncode is not None
                else None
            ),
            severity="error",
        ),
    )


def _build_recovery_payload(
    *,
    plan_dir: Path,
    kind: RecoveryKind,
    title: str,
    summary: str,
    severity: RecoverySeverity,
    recoverable: bool,
    raw_state: Mapping[str, Any],
    runner_state: Mapping[str, Any],
    artifacts: tuple[RecoveryArtifactReference, ...],
    evidence: tuple[RecoveryEvidenceRow, ...] = (),
    actions: tuple[RecoveryActionCard, ...] = (),
    commands: tuple[RecoveryCopyCommand, ...] = (),
    selected_turn: Mapping[str, Any] | None = None,
    selected_commit: Mapping[str, Any] | None = None,
    warnings: tuple[str, ...] = (),
    warning_details: tuple[RecoveryWarning, ...] = (),
) -> WorkbenchRecovery:
    run_id, session_id, turn_index, commit_index = _recovery_common_context(
        raw_state=raw_state,
        runner_state=runner_state,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
    )
    return WorkbenchRecovery(
        plan_dir=plan_dir,
        kind=kind,
        title=title,
        summary=summary,
        severity=severity,
        recoverable=recoverable,
        evidence=evidence,
        actions=actions,
        commands=commands,
        artifacts=artifacts,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        commit_index=commit_index,
        warnings=warnings,
        warning_details=warning_details,
    )


_VERIFICATION_FAILURE_REASONS = frozenset(
    {"verify-failed", "no-contract", "malformed-contract"}
)


def _build_context_recovery_payload(
    context: _RecoveryBuildContext,
    *,
    kind: RecoveryKind,
    title: str,
    summary: str,
    severity: RecoverySeverity,
    recoverable: bool,
    artifacts: tuple[RecoveryArtifactReference, ...],
    evidence: tuple[RecoveryEvidenceRow, ...] = (),
    actions: tuple[RecoveryActionCard, ...] = (),
    commands: tuple[RecoveryCopyCommand, ...] = (),
    selected_turn: Mapping[str, Any] | None = None,
    selected_commit: Mapping[str, Any] | None = None,
) -> WorkbenchRecovery:
    return _build_recovery_payload(
        plan_dir=context.snapshot.plan_dir,
        kind=kind,
        title=title,
        summary=summary,
        severity=severity,
        recoverable=recoverable,
        raw_state=context.raw_state,
        runner_state=context.runner_state,
        artifacts=artifacts,
        evidence=evidence,
        actions=actions,
        commands=commands,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
        warnings=context.warnings,
        warning_details=context.warning_details,
    )


def _build_active_run_recovery(
    context: _RecoveryBuildContext,
    *,
    selected_turn: Mapping[str, Any] | None,
) -> WorkbenchRecovery:
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_turn,
        source="active-turn",
        label_prefix="Active turn",
    )
    lock_info = context.lock_info
    return _build_context_recovery_payload(
        context,
        kind="none",
        title="Run in progress",
        summary="No recovery action is required while the runner is active.",
        severity="none",
        recoverable=False,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Runner lock",
                value="active",
                detail=lock_info.reason if lock_info is not None else None,
                severity="info",
            ),
        ),
    )


def _build_not_loop_ready_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    gate = (
        _payload_mapping(context.raw_state, "readiness_gate")
        or context.pause_details
    )
    evidence = (
        (
            RecoveryEvidenceRow(
                label="Pause message",
                value=context.pause_message,
                severity="warning",
            ),
        )
        + _readiness_gate_evidence(gate)
    )
    return _build_context_recovery_payload(
        context,
        kind="not-loop-ready",
        title="Plan is not loop-ready",
        summary=context.pause_message,
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=evidence,
        actions=(
            _retry_run_action(context.plan_dir_payload),
        ),
        commands=_preflight_repair_commands(context.snapshot.plan_dir),
    )


def _build_verification_failure_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str,
) -> WorkbenchRecovery:
    selected_turn, verification, selected_session_id = (
        _selected_verification_context(
            context.raw_state,
            context.pause_details,
            reason=reason,
        )
    )
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_turn,
        source="failed-turn",
        label_prefix="Failed turn",
    )
    return _build_context_recovery_payload(
        context,
        kind=reason,  # type: ignore[arg-type]
        title=_verification_recovery_title(reason),
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=_verification_recovery_evidence(
            plan_dir=context.snapshot.plan_dir,
            reason=reason,
            details=context.pause_details,
            verification=verification,
            session_id=selected_session_id,
        ),
        actions=(
            _inspect_action("Open the failed turn logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
        ),
        commands=_verification_repair_commands(
            context.snapshot.plan_dir,
            session_id=selected_session_id,
        ),
    )


def _build_commit_preflight_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="commit-preflight",
        title="Commit preflight failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Pause reason",
                value="commit-preflight",
                severity="error",
            ),
            RecoveryEvidenceRow(
                label="Pause message",
                value=context.pause_message,
                severity="error",
            ),
        ),
        actions=(
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review launch and commit settings before retrying."),
        ),
        commands=(
            RecoveryCopyCommand(
                label="Inspect worktree",
                command="git status --porcelain",
                description=(
                    "Copy this into the repository shell to inspect dirty files "
                    "before retrying commit mode."
                ),
            ),
        ),
    )


def _build_commit_failed_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    failed_commit = _latest_failed_commit(context.raw_state)
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        failed_commit,
        source="failed-commit",
        label_prefix="Failed commit",
    )
    return _build_context_recovery_payload(
        context,
        kind="commit-failed",
        title="Auto-commit failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_commit=failed_commit,
        artifacts=artifacts,
        evidence=_commit_failed_evidence(failed_commit),
        actions=(
            _inspect_action("Open the failed commit logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
            _profile_action(
                "Review commit mode and launch profile before retrying."
            ),
        ),
    )


def _is_harness_config_recovery(
    *,
    reason: str | None,
    details: Mapping[str, Any],
) -> bool:
    return (
        reason == "harness-unavailable"
        or _payload_str(details, "requested_harness") is not None
        or _payload_str(details, "config_path") is not None
    )


def _build_harness_config_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="generic",
        reason=reason,
    )
    artifacts = (
        context.base_artifacts
        + _harness_config_artifacts(context.pause_details)
        + _artifact_references_from_payload(
            selected_turn,
            source="failed-turn",
            label_prefix="Failed turn",
        )
    )
    return _build_context_recovery_payload(
        context,
        kind="generic",
        title="Harness configuration needs attention",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=_harness_config_evidence(
            reason=reason,
            message=context.pause_message,
            details=context.pause_details,
        ),
        actions=(
            _profile_action("Review harness and profile fields before retrying."),
            _inspect_action(
                "Open runner state, events, and harness config artifacts."
            ),
            _retry_run_action(context.plan_dir_payload),
        ),
    )


def _build_reply_needed_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="reply-needed",
        reason=reason,
    )
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_turn,
        source="paused-turn",
        label_prefix="Paused turn",
    )
    return _build_context_recovery_payload(
        context,
        kind="reply-needed",
        title="Reply needed",
        summary=context.pause_message,
        severity="warning",
        recoverable=True,
        selected_turn=selected_turn,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Pause reason",
                value=reason,
                severity="warning",
            ),
            RecoveryEvidenceRow(
                label="Pause message",
                value=context.pause_message,
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="reply",
                label="Reply",
                description=(
                    "Send an operator reply through the existing reply endpoint."
                ),
                endpoint="/api/reply",
                payload=context.plan_dir_payload,
                target="reply",
            ),
        ),
    )


def _build_stale_state_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="stale-state",
        title="Runner state is stale",
        summary=context.stale_reason or "Runner state is unfinished.",
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Stale state",
                value=context.stale_reason,
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="unlock",
                label="Unlock",
                description=(
                    "Clear stale runner state through the existing unlock endpoint."
                ),
                endpoint="/api/unlock",
                payload=context.plan_dir_payload,
                target="unlock",
            ),
        ),
    )


def _build_stale_lock_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    lock_info = context.lock_info
    if lock_info is None:
        return _build_clean_none_recovery(context)
    return _build_context_recovery_payload(
        context,
        kind="stale-lock",
        title="Runner lock is stale",
        summary=lock_info.reason or "Runner lock is stale.",
        severity="warning",
        recoverable=True,
        artifacts=context.base_artifacts
        + (
            RecoveryArtifactReference(
                label="Runner lock",
                path=lock_info.path,
                kind="lock",
                source="plan-state",
            ),
        ),
        evidence=(
            RecoveryEvidenceRow(
                label="Lock reason",
                value=lock_info.reason,
                detail=(
                    f"pid {lock_info.pid}" if lock_info.pid is not None else None
                ),
                severity="warning",
            ),
        ),
        actions=(
            _endpoint_action(
                action_id="unlock",
                label="Unlock",
                description=(
                    "Clear the stale lock through the existing unlock endpoint."
                ),
                endpoint="/api/unlock",
                payload=context.plan_dir_payload,
                target="unlock",
            ),
        ),
    )


def _build_failed_finished_recovery(
    context: _RecoveryBuildContext,
    *,
    reason: str | None,
) -> WorkbenchRecovery:
    selected_turn = _latest_relevant_turn(
        context.raw_state,
        kind="generic",
        reason=reason,
    )
    failed_commit = _latest_failed_commit(context.raw_state)
    selected_commit = failed_commit if reason == "commit-failed" else None
    artifacts = context.base_artifacts + _artifact_references_from_payload(
        selected_commit or selected_turn,
        source="failed-commit" if selected_commit is not None else "failed-turn",
        label_prefix=(
            "Failed commit" if selected_commit is not None else "Failed turn"
        ),
    )
    return _build_context_recovery_payload(
        context,
        kind="generic",
        title="Run failed",
        summary=context.pause_message,
        severity="error",
        recoverable=True,
        selected_turn=selected_turn,
        selected_commit=selected_commit,
        artifacts=artifacts,
        evidence=(
            RecoveryEvidenceRow(
                label="Failure reason",
                value=reason,
                severity="error",
            ),
            RecoveryEvidenceRow(
                label="Failure message",
                value=context.pause_message,
                severity="error",
            ),
        ),
        actions=(
            _inspect_action("Open the latest relevant logs and result JSON."),
            _retry_run_action(context.plan_dir_payload),
            _profile_action("Review launch profile fields before retrying."),
        ),
    )


def _build_clean_none_recovery(
    context: _RecoveryBuildContext,
) -> WorkbenchRecovery:
    return _build_context_recovery_payload(
        context,
        kind="none",
        title="No recovery needed",
        summary="The plan has no structured blocker.",
        severity="none",
        recoverable=False,
        artifacts=context.base_artifacts,
        evidence=(),
    )


def build_workbench_recovery(
    plan: DiscoveredPlan,
    *,
    state_dir: str | Path | None = None,
) -> WorkbenchRecovery:
    """Build a deterministic, read-only recovery diagnosis for one plan."""
    if not plan.exists:
        return WorkbenchRecovery(
            plan_dir=plan.plan_dir,
            kind="generic",
            title="Plan cannot be inspected",
            summary=f"Plan directory does not exist: {plan.plan_dir}",
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Plan directory",
                    value=str(plan.plan_dir),
                    severity="error",
                ),
            ),
        )

    try:
        snapshot = plans.inspect_plan(plan.plan_dir)
    except plans.PlanError as error:
        state_path, events_path, progress_path = _summary_base_paths(
            plan.plan_dir, state_dir
        )
        return WorkbenchRecovery(
            plan_dir=plan.plan_dir,
            kind="generic",
            title="Plan cannot be inspected",
            summary=str(error),
            severity="error",
            recoverable=True,
            evidence=(
                RecoveryEvidenceRow(
                    label="Plan error",
                    value=str(error),
                    severity="error",
                ),
            ),
            artifacts=_recovery_base_artifacts(
                state_path=state_path,
                events_path=events_path,
                progress_path=progress_path,
            ),
        )

    context = _build_recovery_context(snapshot, state_dir)

    active_turn = _payload_mapping(context.raw_state, "active_turn")
    active_verification = _payload_mapping(context.raw_state, "active_verification")
    lock_info = context.lock_info
    if lock_info is not None and lock_info.active and (
        active_turn is not None or active_verification is not None
    ):
        return _build_active_run_recovery(context, selected_turn=active_turn)

    reason = context.pause_reason
    details = context.pause_details
    recovery_kind = _pause_reason_recovery_kind(reason)

    if context.runner_state.get("paused") is True:
        if recovery_kind == "not-loop-ready":
            return _build_not_loop_ready_recovery(context)

        if recovery_kind in _VERIFICATION_FAILURE_REASONS:
            return _build_verification_failure_recovery(
                context, reason=recovery_kind
            )

        if recovery_kind == "commit-preflight":
            return _build_commit_preflight_recovery(context)

        if recovery_kind == "commit-failed":
            return _build_commit_failed_recovery(context)

        if _is_harness_config_recovery(reason=reason, details=details):
            return _build_harness_config_recovery(context, reason=reason)

        if recovery_kind == "generic":
            return _build_failed_finished_recovery(context, reason=reason)

        return _build_reply_needed_recovery(context, reason=reason)

    if context.stale_unfinished:
        return _build_stale_state_recovery(context)

    if lock_info is not None and lock_info.stale:
        return _build_stale_lock_recovery(context)

    if (
        context.runner_state.get("present")
        and context.runner_state.get("run_finished_at")
        and context.runner_state.get("ok") is False
    ):
        if recovery_kind == "commit-failed":
            return _build_commit_failed_recovery(context)

        if recovery_kind in _VERIFICATION_FAILURE_REASONS:
            return _build_verification_failure_recovery(
                context, reason=recovery_kind
            )

        return _build_failed_finished_recovery(context, reason=reason)

    return _build_clean_none_recovery(context)


def _build_invalid_summary(
    plan: DiscoveredPlan,
    *,
    detail: str,
    exists: bool,
    state_paths: tuple[Path, Path, Path] | None = None,
    state_dir: str | Path | None = None,
) -> WorkbenchSummary:
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
    recovery = build_workbench_recovery(
        replace(plan, exists=exists), state_dir=state_dir
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
        final_review_path=_final_review_path_if_exists(plan.plan_dir) if exists else None,
        artifact_dir=None,
        stdout_path=None,
        stderr_path=None,
        last_message_path=None,
        result_path=None,
        run_id=None,
        harness=None,
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
) -> WorkbenchSummary:
    """Aggregate one discovered plan into a Workbench queue summary.

    Pure Python builder: reads plan files and runner state, but never
    mutates them. Classification priority, highest first, is:

    1. ``invalid`` - plan directory missing or unparseable.
    2. ``verifying`` - active lock and ``active_verification`` set.
    3. ``running`` - active lock and ``active_turn`` set.
    4. ``paused-for-reply`` - runner state ``paused=True``.
    5. ``failed`` - finished run with ``ok=False``.
    6. ``stale`` - stale lock or stale-unfinished runner state.
    7. ``complete`` - no pending sessions remain.
    8. ``recent`` - pending sessions remain after a finished run.
    9. ``ready`` - pending sessions remain and no run state exists.
    """
    plan_dir = plan.plan_dir
    if not plan.exists:
        return _build_invalid_summary(
            plan,
            detail=f"plan directory does not exist: {plan_dir}",
            exists=False,
            state_dir=state_dir,
        )

    try:
        snapshot = plans.inspect_plan(plan_dir)
    except plans.PlanError as error:
        state_paths = _summary_base_paths(plan_dir, state_dir)
        return _build_invalid_summary(
            plan,
            detail=str(error),
            exists=True,
            state_paths=state_paths,
            state_dir=state_dir,
        )

    state_path, events_path, progress_path = _summary_base_paths(
        snapshot.plan_dir, state_dir
    )
    lock_path = state.lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = state._read_lock_path(lock_path, snapshot.plan_dir)
    runner_state = state._runner_state_payload(state_path)
    stale_unfinished, stale_reason = state._stale_runner_state(
        runner_state, lock_info
    )
    recovery = build_workbench_recovery(
        replace(plan, plan_dir=snapshot.plan_dir, exists=True),
        state_dir=state_dir,
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
        artifact_dir = _coerce_path_value(active_turn.get("artifact_dir"))
        stdout_path = _coerce_path_value(active_turn.get("stdout_path"))
        stderr_path = _coerce_path_value(active_turn.get("stderr_path"))
        last_message_path = _coerce_path_value(
            active_turn.get("last_message_path")
        )
        result_path = _coerce_path_value(active_turn.get("result_path"))

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
        concise_reason: str | None = None,
    ) -> WorkbenchSummary:
        urgency_group = URGENCY_GROUP_BY_STATE[primary_state]
        activity_label = ACTIVITY_LABEL_BY_STATE[primary_state]
        resolved_reason = concise_reason or detail or next_action_hint
        next_pending_session_id = (
            next_pending.id if next_pending is not None else None
        )
        searchable_text = _searchable_text(
            plan_dir=snapshot.plan_dir,
            label=plan.label,
            primary_state=primary_state,
            recommended_action=recommended_action,
            urgency_group=urgency_group,
            activity_label=activity_label,
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
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=last_message_preview,
            urgency_rank=URGENCY_RANK_BY_STATE[primary_state],
            urgency_group=urgency_group,
            last_activity_at=last_activity_at,
            last_activity_source=last_activity_source,
            activity_label=activity_label,
            concise_reason=resolved_reason,
            searchable_text=searchable_text,
            recovery=recovery,
            warnings=warnings,
        )

    if lock_info is not None and lock_info.active and isinstance(
        active_verification, dict
    ):
        session_label = (
            _session_identifier_from_payload(active_verification) or "?"
        )
        position = _verification_position(active_verification)
        check_name = active_verification.get("current_check_name") or "pending"
        verifying_activity, verifying_source = _first_activity((
            (
                active_verification.get("current_check_started_at"),
                "active-verification",
            ),
            (active_verification.get("started_at"), "active-verification"),
            (runner_state.get("run_started_at"), "run-started"),
        ))
        return base_kwargs(
            primary_state="verifying",
            recommended_action="watch",
            next_action_hint=(
                f"Verifying session {session_label} check {position} "
                f"({check_name})"
            ),
            detail=f"Active verification on session {session_label}",
            last_activity_at=verifying_activity,
            last_activity_source=verifying_source,
            concise_reason=(
                f"On check {position} ({check_name}) of session {session_label}"
            ),
        )

    if lock_info is not None and lock_info.active and isinstance(active_turn, dict):
        session_label = _session_identifier_from_payload(active_turn) or "?"
        turn_index = active_turn.get("turn_index")
        running_activity, running_source = _first_activity((
            (active_turn.get("last_activity_at"), "active-turn"),
            (active_turn.get("started_at"), "active-turn"),
            (runner_state.get("run_started_at"), "run-started"),
        ))
        return base_kwargs(
            primary_state="running",
            recommended_action="watch",
            next_action_hint=(
                f"Running session {session_label} turn {turn_index}"
            ),
            detail=f"Active turn on session {session_label}",
            last_activity_at=running_activity,
            last_activity_source=running_source,
            concise_reason=(
                f"Session {session_label}, turn {turn_index}"
            ),
        )

    if runner_state.get("paused") is True:
        warnings: list[str] = []
        pause_reason = runner_state.get("reason") or None
        pause_message = None
        preview = None
        qna_last_message_path = last_message_path
        paused_activity, paused_source = _first_activity((
            (runner_state.get("run_started_at"), "run-started"),
        ))
        try:
            context = qna.load_qna_context(snapshot.plan_dir, state_dir=state_dir)
        except qna.QnAError as error:
            warnings.append(
                f"paused-q&a context unreadable ({error.reason}): {error.error}"
            )
            session_label = (
                next_pending.id if next_pending is not None else "?"
            )
            hint = f"Reply needed for session {session_label}"
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
            concise_reason=(
                pause_message or pause_reason or "Awaiting operator reply"
            ),
        )

    if stale_unfinished or (lock_info is not None and lock_info.stale):
        if stale_unfinished:
            reason_text = stale_reason or "Runner state has not finished"
        else:
            reason_text = (
                lock_info.reason if lock_info is not None else "stale lock"
            )
        lock_created_at = (
            lock_info.created_at
            if lock_info is not None and lock_info.created_at
            else None
        )
        stale_activity, stale_source = _first_activity((
            (lock_created_at, "lock-created"),
            (runner_state.get("run_started_at"), "run-started"),
        ))
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
        failed_activity, failed_source = _first_activity((
            (runner_state.get("run_finished_at"), "run-finished"),
            (runner_state.get("run_started_at"), "run-started"),
        ))
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
        complete_activity, complete_source = _first_activity((
            (_last_progress_entry_at(snapshot.plan_dir), "progress-journal"),
            (runner_state.get("run_finished_at"), "run-finished"),
        ))
        return base_kwargs(
            primary_state="complete",
            recommended_action="view",
            next_action_hint="All sessions complete",
            detail="No pending sessions remain",
            last_activity_at=complete_activity,
            last_activity_source=complete_source,
            concise_reason="No pending sessions remain",
        )

    if runner_state.get("present") and runner_state.get("run_finished_at"):
        recent_activity, recent_source = _first_activity((
            (runner_state.get("run_finished_at"), "run-finished"),
        ))
        return base_kwargs(
            primary_state="recent",
            recommended_action="run",
            next_action_hint=f"Resume with session {next_pending.id}",
            detail=f"Last run finished at {runner_state.get('run_finished_at')}",
            last_activity_at=recent_activity,
            last_activity_source=recent_source,
            concise_reason=(
                f"Last run finished at {runner_state.get('run_finished_at')}"
            ),
        )

    ready_activity, ready_source = _first_activity((
        (_last_progress_entry_at(snapshot.plan_dir), "progress-journal"),
    ))
    return base_kwargs(
        primary_state="ready",
        recommended_action="run",
        next_action_hint=f"Start session {next_pending.id}",
        detail="Ready to run; no prior runner state",
        last_activity_at=ready_activity,
        last_activity_source=ready_source,
        concise_reason=f"Next: session {next_pending.id}",
    )


def build_workbench_summaries(
    discovered: Iterable[DiscoveredPlan],
    *,
    state_dir: str | Path | None = None,
) -> tuple[WorkbenchSummary, ...]:
    """Build a Workbench summary per discovered plan in iteration order."""
    return tuple(
        build_workbench_summary(plan, state_dir=state_dir) for plan in discovered
    )


class LaunchError(Exception):
    """Raised when a Workbench launch request cannot be honored."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


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
    return profile


def build_launch_argv(
    plan_dir: str | Path,
    *,
    profile: LaunchProfile | None = None,
    reply_via_stdin: bool = False,
    json_output: bool = True,
    state_dir: str | Path | None = None,
    python_executable: str | None = None,
) -> list[str]:
    """Return the argv for ``python -m agent_session_loop run ...``.

    The argv intentionally omits ``--reply``: reply text only ever flows
    through stdin via the matching ``--reply-stdin`` flag, so the operator
    answer never appears on the child process command line.
    """
    canonical = _validate_launch_plan_dir(plan_dir)
    validated = _validate_launch_profile(profile)
    interpreter = python_executable or sys.executable
    argv: list[str] = [interpreter, "-m", "agent_session_loop", "run", str(canonical)]

    cwd_value = validated.cwd
    if cwd_value:
        argv.extend(["--cwd", cwd_value])
    if validated.harness is not None:
        argv.extend(["--harness", validated.harness])
    if validated.review_harness is not None:
        argv.extend(["--review-harness", validated.review_harness])
    if validated.model is not None:
        argv.extend(["--model", validated.model])
    if validated.effort is not None:
        argv.extend(["--effort", validated.effort])
    if validated.timeout is not None:
        argv.extend(["--timeout", str(validated.timeout)])
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
) -> LaunchResult:
    """Spawn a background ``python -m agent_session_loop run`` process.

    ``reply_text`` is streamed to the child's stdin when set so the operator
    answer never appears in argv, environment variables, or persisted state.
    The child still emits its normal ASL state, events, and artifacts.

    Validation errors raise :class:`LaunchError` before any subprocess is
    spawned, so callers can map them to immediate API errors.
    """
    canonical = _validate_launch_plan_dir(plan_dir)
    validated = _validate_launch_profile(profile)
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
    )

    canonical_state_dir: Path | None = None
    if state_dir is not None:
        canonical_state_dir = Path(state_dir).expanduser().resolve()

    child_cwd = Path(cwd).expanduser().resolve() if cwd is not None else None
    popen_env = dict(env) if env is not None else None

    stdin_pipe = subprocess.PIPE if reply_via_stdin else subprocess.DEVNULL
    try:
        process = popen_factory(
            argv,
            stdin=stdin_pipe,
            stdout=stdout,
            stderr=stderr,
            env=popen_env,
            cwd=str(child_cwd) if child_cwd is not None else None,
            text=True if reply_via_stdin else False,
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
    """Launch ``asl run`` for a paused plan and stream ``reply_text`` to stdin."""
    return launch_plan(
        plan_dir,
        profile=profile,
        reply_text=reply_text,
        state_dir=state_dir,
        json_output=json_output,
        python_executable=python_executable,
        env=env,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        popen_factory=popen_factory,
    )
