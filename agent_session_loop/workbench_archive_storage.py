"""Focused implementation extracted from :mod:`.workbench_archive`."""

from __future__ import annotations

import json
import secrets
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from asl_workbench import archive as portable_archive

from . import (
    durability,
    plans_files,
    state,
    state_locks,
    workbench_discovery,
    workbench_registry,
    workbench_registry_coerce,
    workbench_registry_models,
)
from .workbench_archive_models import (
    ARCHIVE_DIR_NAME,
    ARCHIVE_METADATA_FILE_NAME,
    ARCHIVE_PLAN_DIR_NAME,
    ARCHIVE_STATE_DIR_NAME,
    ArchiveError,
    ArchiveListing,
    ArchiveMetadata,
    DeletePlanResult,
    PlanDeleteError,
)


def _is_discovered_plan(
    plan_dir: Path,
    *,
    registry: workbench_registry_models.WorkbenchRegistry,
    state_dir: str | Path | None,
    plan_roots: Iterable[str | Path],
    include_default_dirs: bool,
) -> bool:
    discovery = workbench_discovery.discover_plans(
        state_root=state_dir,
        registry=registry,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    return any(discovered.plan_dir == plan_dir for discovered in discovery.plans)


def _validate_plan_for_mutation(
    plan_dir: str | Path,
    *,
    verb: str,
    error_cls: type[PlanDeleteError] | type[ArchiveError],
    state_dir: str | Path | None,
    registry_path: str | Path | None,
    plan_roots: Iterable[str | Path],
    include_default_dirs: bool,
) -> tuple[Path, workbench_registry_models.WorkbenchRegistry]:
    """Run the shared delete/archive safety gate for a Workbench plan.

    Both :func:`delete_plan` (permanent purge of a live plan) and
    :func:`archive_plan` must reject the same unsafe inputs: uncanonicalizable
    paths, unreadable registries, arbitrary non-plan directories, actively
    locked plans, and plan paths that exist but are not directories. Raising a
    caller-supplied ``error_cls`` (with reasons/status matching the original
    ``delete_plan`` guards) keeps the two entry points from drifting apart.
    Returns the canonical plan directory and the loaded registry.
    """
    try:
        canonical = plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise error_cls(
            "invalid-plan",
            f"invalid plan directory {plan_dir!r}: {error}",
        ) from error

    try:
        registry = workbench_registry.load_registry(registry_path)
    except workbench_registry_models.WorkbenchError as error:
        raise error_cls("registry-unreadable", str(error), status=500) from error

    discovered = _is_discovered_plan(
        canonical,
        registry=registry,
        state_dir=state_dir,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    if not discovered and not workbench_discovery._looks_like_plan_dir(canonical):
        raise error_cls(
            "invalid-plan",
            f"refusing to {verb} non-plan path: {canonical}",
        )

    try:
        lock_info = state_locks.read_lock_info(canonical, state_dir)
    except (OSError, ValueError) as error:
        raise error_cls(
            "state-error",
            f"cannot inspect plan lock for {canonical}: {error}",
        ) from error
    if lock_info is not None and lock_info.active:
        raise error_cls(
            "active-lock",
            f"active lock held by pid {lock_info.pid} at {lock_info.path}",
            status=409,
        )

    if canonical.exists() and not canonical.is_dir():
        raise error_cls(
            "invalid-plan",
            f"plan path is not a directory: {canonical}",
        )

    return canonical, registry


def delete_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> DeletePlanResult:
    """Permanently delete a Workbench-visible plan and its local metadata."""
    canonical, _registry = _validate_plan_for_mutation(
        plan_dir,
        verb="delete",
        error_cls=PlanDeleteError,
        state_dir=state_dir,
        registry_path=registry_path,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
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

    def remove_matching_pin(
        current: workbench_registry_models.WorkbenchRegistry,
    ) -> workbench_registry_models.WorkbenchRegistry:
        nonlocal pin_removed
        pin_removed = any(pin.plan_dir == str(canonical) for pin in current.pins)
        return workbench_registry.remove_pin(current, canonical)

    try:
        _updated, written_path = workbench_registry.mutate_registry(
            registry_path, remove_matching_pin
        )
    except workbench_registry_models.WorkbenchError as error:
        raise PlanDeleteError("registry-unreadable", str(error), status=500) from error

    return DeletePlanResult(
        plan_dir=canonical,
        plan_removed=plan_removed,
        state_dir=plan_state_dir,
        state_removed=state_removed,
        pin_removed=pin_removed,
        registry_path=written_path,
    )


def archive_root(state_dir: str | Path | None = None) -> Path:
    """Return the archive root directory under the active state root.

    Archived entries live at ``<state_root>/archive/<archive_id>/``. This
    directory is a sibling of the per-plan state directories, so the
    one-level-deep state scan in
    :func:`~.workbench_discovery._iter_state_plan_dirs` never
    surfaces archived plans as active plan roots.
    """
    return state.state_root(state_dir) / ARCHIVE_DIR_NAME


def _is_valid_archive_id(archive_id: str) -> bool:
    """Reject empty ids or ids that would escape the archive root.

    The rule itself lives in :mod:`asl_workbench.archive` so that every surface
    that can name an archive entry -- this module, the HTTP routes, a connector
    -- rejects the same ids. A traversal rule that two callers spell separately
    is a traversal rule one of them will eventually spell wrong.
    """
    return portable_archive.valid_archive_id(archive_id)


def new_archive_id() -> str:
    """Return a sortable, collision-resistant archive id.

    Combines a compact UTC timestamp prefix (so listings sort oldest-first by
    name) with random hex so concurrent archives in the same second cannot
    collide.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(6)}"


def archive_entry_dir(archive_id: str, state_dir: str | Path | None = None) -> Path:
    """Return the directory that holds one archive entry."""
    if not _is_valid_archive_id(archive_id):
        raise workbench_registry_models.WorkbenchError(
            f"invalid archive id: {archive_id!r}"
        )
    return archive_root(state_dir) / archive_id


def archive_plan_path(archive_id: str, state_dir: str | Path | None = None) -> Path:
    return archive_entry_dir(archive_id, state_dir) / ARCHIVE_PLAN_DIR_NAME


def archive_state_path(archive_id: str, state_dir: str | Path | None = None) -> Path:
    return archive_entry_dir(archive_id, state_dir) / ARCHIVE_STATE_DIR_NAME


def compute_expires_at(archived_at: str, retention_days: int) -> str:
    """Return the ISO expiry timestamp for an entry archived at ``archived_at``.

    ``retention_days`` is clamped to a non-negative count; a value of ``0``
    yields an expiry equal to the archive time (immediately purgeable).
    """
    try:
        archived = datetime.fromisoformat(archived_at)
    except (TypeError, ValueError) as error:
        raise workbench_registry_models.WorkbenchError(
            f"invalid archived_at timestamp {archived_at!r}: {error}"
        ) from error
    days = max(0, int(retention_days))
    return (archived + timedelta(days=days)).isoformat()


def _coerce_archive_metadata(
    raw: Any, *, entry_dir: Path, warnings: list[str], context: str
) -> ArchiveMetadata | None:
    if not isinstance(raw, Mapping):
        warnings.append(f"{context}: metadata must be a JSON object; skipping")
        return None
    archive_id = workbench_registry_coerce._coerce_optional_str(raw.get("archive_id"))
    if archive_id is None or not _is_valid_archive_id(archive_id):
        warnings.append(f"{context}: missing or invalid archive_id; skipping")
        return None
    original_plan_dir = workbench_registry_coerce._coerce_optional_str(
        raw.get("original_plan_dir")
    )
    if original_plan_dir is None:
        warnings.append(f"{context}: missing original_plan_dir; skipping")
        return None
    archived_at = workbench_registry_coerce._coerce_optional_str(raw.get("archived_at"))
    expires_at = workbench_registry_coerce._coerce_optional_str(raw.get("expires_at"))
    if archived_at is None or expires_at is None:
        warnings.append(f"{context}: missing archived_at/expires_at; skipping")
        return None
    summary = workbench_registry_coerce._coerce_optional_str(raw.get("summary"))
    pin_raw = raw.get("pin")
    pin = (
        workbench_registry_coerce._coerce_pin(
            pin_raw,
            # Archive metadata does not carry a registry schema version.  Its
            # embedded pin is written from the current in-memory profile, so
            # decode it as the current profile shape.
            schema_version=workbench_registry_models.REGISTRY_SCHEMA_VERSION,
            warnings=warnings,
            index=0,
        )
        if pin_raw is not None
        else None
    )
    plan_archived = bool(raw.get("plan_archived", False))
    state_archived = bool(raw.get("state_archived", False))
    return ArchiveMetadata(
        archive_id=archive_id,
        original_plan_dir=original_plan_dir,
        archived_at=archived_at,
        expires_at=expires_at,
        summary=summary,
        pin=pin,
        plan_archived=plan_archived,
        state_archived=state_archived,
        archive_dir=entry_dir,
        plan_path=entry_dir / ARCHIVE_PLAN_DIR_NAME,
        state_path=entry_dir / ARCHIVE_STATE_DIR_NAME,
    )


def save_archive_metadata(
    metadata: ArchiveMetadata, state_dir: str | Path | None = None
) -> Path:
    """Write one archive entry's ``archive.json`` atomically."""
    entry_dir = archive_entry_dir(metadata.archive_id, state_dir)
    entry_dir.mkdir(parents=True, exist_ok=True)
    meta_path = entry_dir / ARCHIVE_METADATA_FILE_NAME
    serialized = (
        json.dumps(metadata.to_storage_payload(), indent=2, sort_keys=True) + "\n"
    )
    try:
        # 0o600: the mode tempfile.mkstemp gave archive metadata before.
        durability.replace_text(meta_path, serialized, mode=0o600)
    except OSError as error:
        raise workbench_registry_models.WorkbenchError(
            f"cannot write archive metadata {meta_path}: {error}"
        ) from error
    return meta_path


def load_archive_metadata(
    archive_id: str, state_dir: str | Path | None = None
) -> ArchiveMetadata | None:
    """Load one archive entry, returning ``None`` when it is absent.

    Malformed JSON or an unreadable file raises
    :class:`~.workbench_registry_models.WorkbenchError`.
    Recoverable field-level problems are attached as ``warnings`` on the
    returned metadata.
    """
    entry_dir = archive_entry_dir(archive_id, state_dir)
    meta_path = entry_dir / ARCHIVE_METADATA_FILE_NAME
    if not meta_path.exists():
        return None
    try:
        raw_text = meta_path.read_text(encoding="utf-8")
    except OSError as error:
        raise workbench_registry_models.WorkbenchError(
            f"cannot read archive metadata {meta_path}: {error}"
        ) from error
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise workbench_registry_models.WorkbenchError(
            f"invalid JSON in archive metadata {meta_path}: {error}"
        ) from error
    warnings: list[str] = []
    metadata = _coerce_archive_metadata(
        raw, entry_dir=entry_dir, warnings=warnings, context=f"archive[{archive_id}]"
    )
    if metadata is None:
        raise workbench_registry_models.WorkbenchError(
            f"archive metadata {meta_path} is malformed: " + "; ".join(warnings)
        )
    return replace(metadata, warnings=tuple(warnings))


def list_archive_entries(
    state_dir: str | Path | None = None,
) -> ArchiveListing:
    """List every readable archive entry, sorted oldest-first by id.

    Missing archive root, unreadable entries, and malformed metadata become
    recoverable warnings rather than raising, so the Archive view can still
    render the entries it can read.
    """
    root = archive_root(state_dir)
    warnings: list[str] = []
    if not root.exists():
        return ArchiveListing()
    try:
        if not root.is_dir():
            warnings.append(f"archive: {root} is not a directory; ignoring")
            return ArchiveListing(warnings=tuple(warnings))
        children = sorted(root.iterdir())
    except OSError as error:
        warnings.append(f"archive: could not scan {root}: {error}")
        return ArchiveListing(warnings=tuple(warnings))

    entries: list[ArchiveMetadata] = []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        context = f"archive[{child.name}]"
        if not _is_valid_archive_id(child.name):
            warnings.append(f"{context}: invalid archive id; skipping")
            continue
        meta_path = child / ARCHIVE_METADATA_FILE_NAME
        if not meta_path.is_file():
            warnings.append(
                f"{context}: missing {ARCHIVE_METADATA_FILE_NAME}; skipping"
            )
            continue
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            warnings.append(f"{context}: unreadable metadata: {error}; skipping")
            continue
        entry_warnings: list[str] = []
        metadata = _coerce_archive_metadata(
            raw, entry_dir=child, warnings=entry_warnings, context=context
        )
        warnings.extend(entry_warnings)
        if metadata is None:
            continue
        entries.append(replace(metadata, warnings=tuple(entry_warnings)))

    entries.sort(key=lambda entry: entry.archive_id)
    return ArchiveListing(entries=tuple(entries), warnings=tuple(warnings))


def remove_archive_entry(archive_id: str, state_dir: str | Path | None = None) -> bool:
    """Remove one archive entry directory, returning whether it existed."""
    entry_dir = archive_entry_dir(archive_id, state_dir)
    if not entry_dir.exists():
        return False
    try:
        if entry_dir.is_dir():
            shutil.rmtree(entry_dir)
        else:
            entry_dir.unlink()
    except OSError as error:
        raise workbench_registry_models.WorkbenchError(
            f"cannot remove archive entry {entry_dir}: {error}"
        ) from error
    return True
