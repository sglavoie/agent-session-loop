"""Workbench registry persistence: load, save, and locked mutation."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import (
    config,
    durability,
)
from .workbench_registry_coerce import (
    _canonical_plan_dir,
    _coerce_pin,
    _coerce_plan_roots,
    _coerce_retention_days,
    _coerce_schema_version,
)
from .workbench_registry_models import (
    REGISTRY_FILE_NAME,
    REGISTRY_SCHEMA_VERSION,
    LaunchProfile,
    RegistryPin,
    WorkbenchError,
    WorkbenchRegistry,
)


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
