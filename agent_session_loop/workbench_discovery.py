"""Workbench plan discovery across ASL state, the registry, and well-known roots."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from . import (
    plans_files,
    state,
)
from .workbench_registry_coerce import _canonical_plan_dir
from .workbench_registry_models import (
    DEFAULT_PLANS_DIR_NAME,
    DISCOVERY_SOURCES,
    DiscoveredPlan,
    DiscoveryResult,
    DiscoverySource,
    RegistryPin,
    WorkbenchRegistry,
)


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
                found.append(plans_files.canonicalize_path(child))
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
