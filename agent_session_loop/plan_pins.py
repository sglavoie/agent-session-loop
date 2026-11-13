"""Plan registry loading: the hand-edited pins and plan roots discovery reads."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import (
    config,
)
from .plan_pins_coerce import (
    _coerce_pin,
    _coerce_plan_roots,
    _coerce_schema_version,
)
from .plan_pins_models import (
    REGISTRY_FILE_NAME,
    PlanRegistry,
    PlanRegistryError,
    RegistryPin,
)


def default_registry_path() -> Path:
    """Return the canonical path of the plan registry file."""
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


def load_registry(path: str | Path | None = None) -> PlanRegistry:
    """Load the plan registry, returning an empty one when absent.

    Malformed entries are recorded as warnings on the returned registry
    rather than raising. Outright JSON parse failures or unreadable
    files raise :class:`PlanRegistryError` so callers can decide whether to
    surface a hard error.
    """
    registry_path = _coerce_registry_path(path)
    if not registry_path.exists():
        return PlanRegistry(path=registry_path)

    try:
        raw_text = registry_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PlanRegistryError(
            f"cannot read plan registry {registry_path}: {error}"
        ) from error

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise PlanRegistryError(
            f"invalid JSON in plan registry {registry_path}: {error}"
        ) from error

    if not isinstance(raw, Mapping):
        raise PlanRegistryError(
            f"plan registry {registry_path} must contain a JSON object"
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

    return PlanRegistry(
        schema_version=schema_version,
        pins=tuple(pins),
        plan_roots=plan_roots,
        path=registry_path,
        warnings=tuple(warnings),
    )
