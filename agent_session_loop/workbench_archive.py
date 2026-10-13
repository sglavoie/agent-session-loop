"""One path for every registry and archive mutation, whoever asked.

The HTTP surface, the ``asl workbench`` CLI, and -- once the connector lands --
the Pi each had, or would have grown, their own spelling of the same eight
operations: pin, unpin, set the retention window, archive a plan, archive a
batch, restore one, empty entries permanently, and release a lock an operator
has proven dead. They agreed today because someone kept them agreeing.

This module is that one path. Every caller reaches ASL through the functions
here and then renders, so what differs between surfaces is the *shape of the
answer* rather than the answer:

* **The same serialization.** Every registry write goes through
  ``workbench.mutate_registry``, which holds a flock on ``workbench.json.lock``
  across the read-modify-write. A pin submitted from a browser while the CLI is
  unpinning cannot lose to it, and neither can a connector's.
* **The same confirmations.** Archiving one plan requires its full path retyped;
  emptying archive entries requires a typed count equal to the selection. Both
  are checked in :mod:`asl_workbench.archive` before anything is touched, so a
  mismatch is a refusal that leaves the archive exactly as it was.
* **The same refusals, differently spelled.** A refusal carries ASL's own reason
  plus the portable scope it falls in. Turning that into a status code, a
  protocol error, or an exit code stays with the surface.

What this module deliberately no longer does is delete on a read.
:func:`review_archive` classifies every entry against the live retention window
and returns that classification; it has no branch that removes anything. The
retention window is now the threshold that marks an entry a *candidate*, and
:func:`purge_entries` -- confirmed, explicit, operator-initiated -- is the only
function here that empties one.
"""

from __future__ import annotations

import json
import secrets
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from asl_workbench import archive as portable_archive

from . import durability, plans_errors, plans_files, state, state_locks, workbench

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

# Archive storage lives under the active Workbench state root so archived
# plans and their ASL state travel together and stay outside normal plan
# discovery. Layout: ``<state_root>/archive/<archive_id>/{archive.json,plan,state}``.
ARCHIVE_DIR_NAME = "archive"
ARCHIVE_METADATA_FILE_NAME = "archive.json"
ARCHIVE_PLAN_DIR_NAME = "plan"
ARCHIVE_STATE_DIR_NAME = "state"


class PlanDeleteError(Exception):
    """Raised when a Workbench plan deletion cannot be completed."""

    def __init__(self, reason: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


class ArchiveError(Exception):
    """Raised when an archive, restore, or purge operation cannot complete.

    Carries a stable ``reason`` string and an HTTP-style ``status`` so the
    Workbench API handlers (session 3) can translate helper failures into
    response payloads without re-deriving classification logic. Archive
    operations reuse the same safety-check reasons as :class:`PlanDeleteError`
    (``invalid-plan``, ``registry-unreadable``, ``state-error``,
    ``active-lock``, ``os-error``) plus archive-specific reasons
    (``restore-conflict``, ``not-found``, ``invalid-archive``).
    """

    def __init__(self, reason: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


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


@dataclass(frozen=True)
class ArchiveMetadata:
    """One archived plan entry stored under the Workbench archive root.

    Persisted fields (``archive.json``): ``archive_id``, ``original_plan_dir``,
    ``archived_at``, ``expires_at``, ``summary``, ``pin`` (the archived
    registry pin, if any), and the ``plan_archived`` / ``state_archived``
    booleans recording whether the plan directory and its ASL state were
    present at archive time. ``archive_dir`` / ``plan_path`` / ``state_path``
    are derived from the entry's on-disk location when loaded and are surfaced
    in API payloads; they are not part of the stored JSON.
    """

    archive_id: str
    original_plan_dir: str
    archived_at: str
    expires_at: str
    summary: str | None = None
    pin: workbench.RegistryPin | None = None
    plan_archived: bool = False
    state_archived: bool = False
    archive_dir: Path | None = None
    plan_path: Path | None = None
    state_path: Path | None = None
    warnings: tuple[str, ...] = ()

    def to_storage_payload(self) -> dict[str, Any]:
        """Return the persisted JSON body (no derived paths or warnings)."""
        return {
            "archive_id": self.archive_id,
            "original_plan_dir": self.original_plan_dir,
            "archived_at": self.archived_at,
            "expires_at": self.expires_at,
            "summary": self.summary,
            "pin": self.pin.to_payload() if self.pin is not None else None,
            "plan_archived": self.plan_archived,
            "state_archived": self.state_archived,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.to_storage_payload()
        payload.update(
            {
                "archive_dir": (
                    str(self.archive_dir) if self.archive_dir is not None else None
                ),
                "plan_path": (
                    str(self.plan_path) if self.plan_path is not None else None
                ),
                "state_path": (
                    str(self.state_path) if self.state_path is not None else None
                ),
                "warnings": list(self.warnings),
            }
        )
        return payload


@dataclass(frozen=True)
class ArchiveListing:
    """All readable archive entries plus recoverable warnings."""

    entries: tuple[ArchiveMetadata, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_payload() for entry in self.entries],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ArchivePlanResult:
    """Metadata returned after archiving a Workbench-managed plan."""

    archive_id: str
    plan_dir: Path
    metadata: ArchiveMetadata
    plan_archived: bool
    state_archived: bool
    pin_archived: bool
    registry_path: Path

    def to_payload(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "plan_dir": str(self.plan_dir),
            "metadata": self.metadata.to_payload(),
            "plan_archived": self.plan_archived,
            "state_archived": self.state_archived,
            "pin_archived": self.pin_archived,
            "registry_path": str(self.registry_path),
        }


@dataclass(frozen=True)
class RestoreArchiveResult:
    """Metadata returned after restoring an archived plan to its origin."""

    archive_id: str
    original_plan_dir: Path
    plan_restored: bool
    state_dir: Path
    state_restored: bool
    pin_restored: bool
    registry_path: Path | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "original_plan_dir": str(self.original_plan_dir),
            "plan_restored": self.plan_restored,
            "state_dir": str(self.state_dir),
            "state_restored": self.state_restored,
            "pin_restored": self.pin_restored,
            "registry_path": (
                str(self.registry_path) if self.registry_path is not None else None
            ),
        }


@dataclass(frozen=True)
class ArchivePurgeOutcome:
    """Per-entry result of a permanent purge -- the only pass that removes."""

    archive_id: str
    removed: bool
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "removed": self.removed,
            "error": self.error,
        }


@dataclass(frozen=True)
class ArchivePurgeResult:
    """Aggregate result of purging one or many archive entries.

    ``outcomes`` records one entry per requested (or expired) archive id so
    callers can report partial success: one blocked entry never prevents the
    others from being processed.
    """

    outcomes: tuple[ArchivePurgeOutcome, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def removed_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.removed)

    @property
    def failed_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.error is not None)

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcomes": [outcome.to_payload() for outcome in self.outcomes],
            "removed_count": self.removed_count,
            "failed_count": self.failed_count,
            "warnings": list(self.warnings),
        }


def _is_discovered_plan(
    plan_dir: Path,
    *,
    registry: workbench.WorkbenchRegistry,
    state_dir: str | Path | None,
    plan_roots: Iterable[str | Path],
    include_default_dirs: bool,
) -> bool:
    discovery = workbench.discover_plans(
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
) -> tuple[Path, workbench.WorkbenchRegistry]:
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
        registry = workbench.load_registry(registry_path)
    except workbench.WorkbenchError as error:
        raise error_cls("registry-unreadable", str(error), status=500) from error

    discovered = _is_discovered_plan(
        canonical,
        registry=registry,
        state_dir=state_dir,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    if not discovered and not workbench._looks_like_plan_dir(canonical):
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
        current: workbench.WorkbenchRegistry,
    ) -> workbench.WorkbenchRegistry:
        nonlocal pin_removed
        pin_removed = any(pin.plan_dir == str(canonical) for pin in current.pins)
        return workbench.remove_pin(current, canonical)

    try:
        _updated, written_path = workbench.mutate_registry(
            registry_path, remove_matching_pin
        )
    except workbench.WorkbenchError as error:
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
    one-level-deep state scan in :func:`~.workbench._iter_state_plan_dirs` never
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
        raise workbench.WorkbenchError(f"invalid archive id: {archive_id!r}")
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
        raise workbench.WorkbenchError(
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
    archive_id = workbench._coerce_optional_str(raw.get("archive_id"))
    if archive_id is None or not _is_valid_archive_id(archive_id):
        warnings.append(f"{context}: missing or invalid archive_id; skipping")
        return None
    original_plan_dir = workbench._coerce_optional_str(raw.get("original_plan_dir"))
    if original_plan_dir is None:
        warnings.append(f"{context}: missing original_plan_dir; skipping")
        return None
    archived_at = workbench._coerce_optional_str(raw.get("archived_at"))
    expires_at = workbench._coerce_optional_str(raw.get("expires_at"))
    if archived_at is None or expires_at is None:
        warnings.append(f"{context}: missing archived_at/expires_at; skipping")
        return None
    summary = workbench._coerce_optional_str(raw.get("summary"))
    pin_raw = raw.get("pin")
    pin = (
        workbench._coerce_pin(
            pin_raw,
            # Archive metadata does not carry a registry schema version.  Its
            # embedded pin is written from the current in-memory profile, so
            # decode it as the current profile shape.
            schema_version=workbench.REGISTRY_SCHEMA_VERSION,
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
        raise workbench.WorkbenchError(
            f"cannot write archive metadata {meta_path}: {error}"
        ) from error
    return meta_path


def load_archive_metadata(
    archive_id: str, state_dir: str | Path | None = None
) -> ArchiveMetadata | None:
    """Load one archive entry, returning ``None`` when it is absent.

    Malformed JSON or an unreadable file raises :class:`~.workbench.WorkbenchError`.
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
        raise workbench.WorkbenchError(
            f"cannot read archive metadata {meta_path}: {error}"
        ) from error
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise workbench.WorkbenchError(
            f"invalid JSON in archive metadata {meta_path}: {error}"
        ) from error
    warnings: list[str] = []
    metadata = _coerce_archive_metadata(
        raw, entry_dir=entry_dir, warnings=warnings, context=f"archive[{archive_id}]"
    )
    if metadata is None:
        raise workbench.WorkbenchError(
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
        raise workbench.WorkbenchError(
            f"cannot remove archive entry {entry_dir}: {error}"
        ) from error
    return True


def archive_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
    summary: str | None = None,
) -> ArchivePlanResult:
    """Archive a Workbench-visible plan and its ASL state under the archive root.

    Applies the same safety gate as :func:`delete_plan` (active-lock,
    discovered/non-plan, non-directory) but, instead of permanently removing
    the plan, moves the plan directory and its per-plan ASL state into a fresh
    ``<state_root>/archive/<archive_id>/`` entry and records restore metadata
    (original path, timestamps, summary snapshot, and the registry pin/profile
    if one existed). A missing plan directory (a stale discovered pin) archives
    only the available state and metadata. The registry pin is removed from the
    live registry after its metadata is captured for restore.
    """
    canonical, registry = _validate_plan_for_mutation(
        plan_dir,
        verb="archive",
        error_cls=ArchiveError,
        state_dir=state_dir,
        registry_path=registry_path,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )

    matching_pin = next(
        (pin for pin in registry.pins if pin.plan_dir == str(canonical)), None
    )

    archive_id = new_archive_id()
    archived_at = datetime.now(UTC).isoformat()
    expires_at = compute_expires_at(archived_at, registry.archive_retention_days)
    entry_dir = archive_entry_dir(archive_id, state_dir)
    entry_dir.mkdir(parents=True, exist_ok=True)

    plan_archived = False
    if canonical.is_dir():
        try:
            shutil.move(str(canonical), str(archive_plan_path(archive_id, state_dir)))
        except OSError as error:
            raise ArchiveError(
                "os-error",
                f"cannot archive plan directory {canonical}: {error}",
                status=500,
            ) from error
        plan_archived = True

    plan_state = state.plan_state_dir(canonical, state_dir)
    state_archived = False
    if plan_state.is_dir():
        try:
            shutil.move(str(plan_state), str(archive_state_path(archive_id, state_dir)))
        except OSError as error:
            raise ArchiveError(
                "os-error",
                f"cannot archive plan state {plan_state}: {error}",
                status=500,
            ) from error
        state_archived = True

    metadata = ArchiveMetadata(
        archive_id=archive_id,
        original_plan_dir=str(canonical),
        archived_at=archived_at,
        expires_at=expires_at,
        summary=summary,
        pin=matching_pin,
        plan_archived=plan_archived,
        state_archived=state_archived,
        archive_dir=entry_dir,
        plan_path=archive_plan_path(archive_id, state_dir),
        state_path=archive_state_path(archive_id, state_dir),
    )
    save_archive_metadata(metadata, state_dir)

    pin_archived = False

    def _remove_matching_pin(
        current: workbench.WorkbenchRegistry,
    ) -> workbench.WorkbenchRegistry:
        nonlocal pin_archived
        pin_archived = any(pin.plan_dir == str(canonical) for pin in current.pins)
        return workbench.remove_pin(current, canonical)

    try:
        _updated, written_path = workbench.mutate_registry(
            registry_path, _remove_matching_pin
        )
    except workbench.WorkbenchError as error:
        raise ArchiveError("registry-unreadable", str(error), status=500) from error

    return ArchivePlanResult(
        archive_id=archive_id,
        plan_dir=canonical,
        metadata=metadata,
        plan_archived=plan_archived,
        state_archived=state_archived,
        pin_archived=pin_archived,
        registry_path=written_path,
    )


def restore_archive(
    archive_id: str,
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> RestoreArchiveResult:
    """Restore an archived plan and its ASL state back to their origin.

    Moves the archived plan directory and per-plan state back to the original
    plan path and ASL state path recorded at archive time, restores the pin and
    launch profile when one was captured, and removes the now-empty archive
    entry. Raises :class:`ArchiveError` ``restore-conflict`` (HTTP 409) if
    either destination already exists, so a restore never clobbers a live plan
    or state directory.
    """
    if not _is_valid_archive_id(archive_id):
        raise ArchiveError("invalid-archive", f"invalid archive id: {archive_id!r}")
    try:
        metadata = load_archive_metadata(archive_id, state_dir)
    except workbench.WorkbenchError as error:
        raise ArchiveError("invalid-archive", str(error), status=500) from error
    if metadata is None:
        raise ArchiveError("not-found", f"no archive entry {archive_id!r}", status=404)

    try:
        original = plans_files.canonicalize_path(metadata.original_plan_dir)
    except (OSError, ValueError) as error:
        raise ArchiveError(
            "invalid-archive",
            f"invalid original_plan_dir {metadata.original_plan_dir!r}: {error}",
        ) from error

    dest_state = state.plan_state_dir(original, state_dir)
    archived_plan = archive_plan_path(archive_id, state_dir)
    archived_state = archive_state_path(archive_id, state_dir)

    plan_present = metadata.plan_archived and archived_plan.exists()
    state_present = metadata.state_archived and archived_state.exists()

    if plan_present and original.exists():
        raise ArchiveError(
            "restore-conflict",
            f"plan path already exists: {original}",
            status=409,
        )
    if state_present and dest_state.exists():
        raise ArchiveError(
            "restore-conflict",
            f"state path already exists: {dest_state}",
            status=409,
        )

    plan_restored = False
    if plan_present:
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archived_plan), str(original))
        except OSError as error:
            raise ArchiveError(
                "os-error",
                f"cannot restore plan directory {original}: {error}",
                status=500,
            ) from error
        plan_restored = True

    state_restored = False
    if state_present:
        try:
            dest_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archived_state), str(dest_state))
        except OSError as error:
            raise ArchiveError(
                "os-error",
                f"cannot restore plan state {dest_state}: {error}",
                status=500,
            ) from error
        state_restored = True

    pin_restored = False
    written_path: Path | None = None
    if metadata.pin is not None:
        pin = metadata.pin

        def _restore_pin(
            current: workbench.WorkbenchRegistry,
        ) -> workbench.WorkbenchRegistry:
            nonlocal pin_restored
            pin_restored = True
            return workbench.upsert_pin(
                current, original, label=pin.label, profile=pin.profile
            )

        try:
            _updated, written_path = workbench.mutate_registry(
                registry_path, _restore_pin
            )
        except workbench.WorkbenchError as error:
            raise ArchiveError("registry-unreadable", str(error), status=500) from error

    try:
        remove_archive_entry(archive_id, state_dir)
    except workbench.WorkbenchError as error:
        raise ArchiveError("os-error", str(error), status=500) from error

    return RestoreArchiveResult(
        archive_id=archive_id,
        original_plan_dir=original,
        plan_restored=plan_restored,
        state_dir=dest_state,
        state_restored=state_restored,
        pin_restored=pin_restored,
        registry_path=written_path,
    )


def purge_archive_entries(
    archive_ids: Iterable[str],
    *,
    state_dir: str | Path | None = None,
) -> ArchivePurgeResult:
    """Permanently purge one or many archive entries.

    Returns a per-entry :class:`ArchivePurgeOutcome` so bulk callers can report
    partial success: an invalid id or an unremovable entry is recorded as a
    failed outcome without aborting the remaining ids.
    """
    outcomes: list[ArchivePurgeOutcome] = []
    for archive_id in archive_ids:
        if not _is_valid_archive_id(archive_id):
            outcomes.append(
                ArchivePurgeOutcome(
                    str(archive_id), False, f"invalid archive id: {archive_id!r}"
                )
            )
            continue
        try:
            removed = remove_archive_entry(archive_id, state_dir)
        except workbench.WorkbenchError as error:
            outcomes.append(ArchivePurgeOutcome(archive_id, False, str(error)))
            continue
        outcomes.append(ArchivePurgeOutcome(archive_id, removed))
    return ArchivePurgeResult(outcomes=tuple(outcomes))


# ``maintain_archive`` used to live here: a pass that emptied every entry past
# the retention window, invoked from the archive read so that opening the
# Archive tab was the act that destroyed things. It is gone rather than merely
# unwired, because an unwired deleter is one call site away from returning.
# Retention now classifies -- see ``asl_workbench.archive.cleanup_review`` --
# and ``purge_archive_entries`` below, behind a typed count, is the only way an
# entry leaves the archive.


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class ArchiveRefused(Exception):
    """A registry or archive mutation refused, with what it was refused by.

    Carries ASL's own ``reason`` unrenamed -- an operator who reads one word in
    the browser and another in the terminal has to work out that they are the
    same refusal -- plus the portable ``scope`` a surface with no status
    vocabulary translates through.

    ``status`` is kept alongside the scope rather than derived from it because
    ASL's archive helpers already decide one, and where the two disagree the
    helper's answer is the wire contract. Today that is exactly one case:
    ``invalid-archive`` is a bad id in the request (400) *and* unreadable
    metadata on disk (500), and collapsing those to one status would report a
    corrupt entry as the client's mistake.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        scope: str | None = None,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.scope = scope or portable_archive.scope_for_reason(reason)
        self.status = (
            status
            if status is not None
            else portable_archive.status_for_scope(self.scope)
        )
        self.details: dict[str, Any] = dict(details or {})
        self.retryable = retryable


def _refuse_request(error: portable_archive.ArchiveRequestError) -> ArchiveRefused:
    """Lift a portable request refusal without restating its reason."""
    return ArchiveRefused(
        error.reason,
        error.message,
        scope=error.scope,
        details=error.details,
    )


def _refuse_archive(
    error: ArchiveError, *, details: Mapping[str, Any] | None = None
) -> ArchiveRefused:
    """Lift an ASL archive refusal, keeping the status it already decided."""
    return ArchiveRefused(
        error.reason,
        error.message,
        status=error.status,
        details=details,
    )


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryUpdate:
    """A registry written under the lock, plus where it was written."""

    registry: workbench.WorkbenchRegistry
    path: Path

    def to_payload(self) -> dict[str, Any]:
        return {
            "registry_path": str(self.path),
            "registry": self.registry.to_payload(),
        }


@dataclass(frozen=True)
class BulkArchiveResult:
    """Per-plan outcomes of a bulk archive, plus the two counts.

    One row per requested plan, always. A blocked plan is a row saying why,
    never a reason the plans beside it were left alone.
    """

    outcomes: tuple[portable_archive.BulkOutcome, ...] = ()

    @property
    def archived_count(self) -> int:
        return portable_archive.bulk_counts(self.outcomes)[0]

    @property
    def failed_count(self) -> int:
        return portable_archive.bulk_counts(self.outcomes)[1]

    def to_payload(self) -> dict[str, Any]:
        return {
            "results": [
                outcome.to_payload(target_field="plan_dir", ok_field="archived")
                for outcome in self.outcomes
            ],
            "archived_count": self.archived_count,
            "failed_count": self.failed_count,
        }


@dataclass(frozen=True)
class ArchiveView:
    """Everything a reader may learn about the archive, having changed none of it.

    The listing, the live retention window, and which entries have passed it.
    There is no removal count in this projection because the read that produces
    it removes nothing.
    """

    entries: tuple[ArchiveMetadata, ...]
    review: portable_archive.CleanupReview
    warnings: tuple[str, ...] = ()

    @property
    def retention_days(self) -> int:
        return self.review.retention_days

    def entry_payloads(self) -> list[dict[str, Any]]:
        """Each entry's payload with its retention verdict folded in.

        Folded in here rather than stored on :class:`ArchiveMetadata`
        because the verdict is a function of *now* and the registry's current
        window, not of the entry: a persisted ``cleanup_state`` would be a
        deletion recommendation with an expiry date of its own.
        """
        payloads: list[dict[str, Any]] = []
        for entry in self.entries:
            payload = entry.to_payload()
            verdict = self.review.verdict(entry.archive_id)
            if verdict is not None:
                payload.update(verdict.to_payload())
            payloads.append(payload)
        return payloads

    def to_payload(self) -> dict[str, Any]:
        return {
            "entries": self.entry_payloads(),
            "warnings": list(self.warnings),
            "archive_retention_days": self.retention_days,
            "archive_cleanup": self.review.to_payload(),
        }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def _mutate(
    registry_path: str | Path | None,
    mutator: Any,
    *,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> RegistryUpdate:
    """Apply one serialized registry mutation, naming its refusal reason.

    The reason differs by caller -- a bad plan path is ``invalid-plan`` and a
    bad retention window is ``invalid-request`` -- while the failure mode is the
    same ``WorkbenchError``, so the caller names it and the lock stays in one
    place.
    """
    try:
        updated, written_path = workbench.mutate_registry(registry_path, mutator)
    except workbench.WorkbenchError as error:
        raise ArchiveRefused(
            reason,
            str(error),
            scope=portable_archive.SCOPE_REQUEST,
            details=details,
        ) from error
    return RegistryUpdate(registry=updated, path=written_path)


def pin_plan(
    plan_dir: str,
    *,
    registry_path: str | Path | None = None,
    label: str | None = None,
    profile: workbench.LaunchProfile | None = None,
) -> RegistryUpdate:
    """Pin one plan, or update the pin it already has."""
    return _mutate(
        registry_path,
        lambda current: workbench.upsert_pin(
            current, plan_dir, label=label, profile=profile
        ),
        reason=portable_archive.REASON_INVALID_PLAN,
        details={"plan_dir": plan_dir},
    )


def unpin_plan(
    plan_dir: str, *, registry_path: str | Path | None = None
) -> RegistryUpdate:
    """Drop one registry pin. The plan itself is untouched."""
    return _mutate(
        registry_path,
        lambda current: workbench.remove_pin(current, plan_dir),
        reason=portable_archive.REASON_INVALID_PLAN,
        details={"plan_dir": plan_dir},
    )


def set_retention(
    raw_days: Any, *, registry_path: str | Path | None = None
) -> RegistryUpdate:
    """Set how long an archive entry is kept before it becomes a candidate.

    Shortening the window makes more entries candidates on the next read. It
    does not empty them, and that is the whole behavioural change: the setting
    now describes when an operator *may* clean up, not when the program will.
    """
    try:
        days = portable_archive.require_retention_days(raw_days)
    except portable_archive.ArchiveRequestError as error:
        raise _refuse_request(error) from error
    return _mutate(
        registry_path,
        lambda current: workbench.set_archive_retention_days(current, days),
        reason=portable_archive.REASON_INVALID_REQUEST,
    )


# --------------------------------------------------------------------------
# Archiving
# --------------------------------------------------------------------------


def _canonical(plan_dir: str, *, field: str) -> Path:
    try:
        return plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise ArchiveRefused(
            portable_archive.REASON_INVALID_PLAN,
            f"invalid {field}: {error}",
            details={field: plan_dir},
        ) from error


def archive_one(
    plan_dir: str,
    confirm_plan_dir: str,
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> ArchivePlanResult:
    """Archive one plan, once its full path has been retyped.

    Both paths are canonicalized before they are compared, so the confirmation
    is about the plan rather than about the spelling. The active-lock refusal
    behind this comes from ASL's own safety gate and is not re-derived here:
    whether a run holds a plan is ASL's answer, and asking it twice is how two
    surfaces come to disagree.
    """
    canonical = _canonical(plan_dir, field="plan_dir")
    confirmed = _canonical(confirm_plan_dir, field="confirm_plan_dir")
    try:
        portable_archive.require_path_confirmation(str(canonical), str(confirmed))
    except portable_archive.ArchiveRequestError as error:
        raise _refuse_request(error) from error
    try:
        return archive_plan(
            canonical,
            state_dir=state_dir,
            registry_path=registry_path,
            plan_roots=plan_roots,
            include_default_dirs=include_default_dirs,
        )
    except ArchiveError as error:
        raise _refuse_archive(error, details={"plan_dir": str(canonical)}) from error


def archive_many(
    raw_plan_dirs: Any,
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> BulkArchiveResult:
    """Archive a batch, reporting one row per plan.

    Deliberately not confirmed per plan: the browser's cleanup bar shows the
    selection it is about to act on, and demanding a retyped path per row would
    make the bulk path unusable enough that operators would stop using it. The
    batch is still non-destructive -- every plan is *moved* into the archive and
    can be restored -- which is what makes one confirmation for the set the right
    weight. Permanent emptying keeps its typed count.
    """
    try:
        plan_dirs = portable_archive.require_string_list(
            raw_plan_dirs, label="plan_dirs"
        )
    except portable_archive.ArchiveRequestError as error:
        raise _refuse_request(error) from error

    outcomes: list[portable_archive.BulkOutcome] = []
    for raw_plan_dir in plan_dirs:
        try:
            canonical = plans_files.canonicalize_path(raw_plan_dir)
        except (OSError, ValueError) as error:
            outcomes.append(
                portable_archive.BulkOutcome(
                    target=raw_plan_dir,
                    ok=False,
                    reason=portable_archive.REASON_INVALID_PLAN,
                    error=f"invalid plan_dir: {error}",
                )
            )
            continue
        try:
            result = archive_plan(
                canonical,
                state_dir=state_dir,
                registry_path=registry_path,
                plan_roots=plan_roots,
                include_default_dirs=include_default_dirs,
            )
        except ArchiveError as error:
            outcomes.append(
                portable_archive.BulkOutcome(
                    target=str(canonical),
                    ok=False,
                    reason=error.reason,
                    error=error.message,
                )
            )
            continue
        outcomes.append(
            portable_archive.BulkOutcome(
                target=str(canonical),
                ok=True,
                reason="ok",
                error=None,
                result=result.to_payload(),
            )
        )
    return BulkArchiveResult(outcomes=tuple(outcomes))


def restore_entry(
    archive_id: str,
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> RestoreArchiveResult:
    """Put one archived plan and its state back where they came from.

    Refuses rather than overwrites when either destination is occupied: a
    restore that clobbered a live plan would destroy the one thing an archive
    exists to avoid destroying.
    """
    try:
        return restore_archive(
            archive_id, state_dir=state_dir, registry_path=registry_path
        )
    except ArchiveError as error:
        raise _refuse_archive(error, details={"archive_id": archive_id}) from error


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


def review_archive(
    *,
    state_dir: str | Path | None = None,
    registry_path: str | Path | None = None,
    now: datetime | None = None,
) -> ArchiveView:
    """Read the archive and say what retention makes of it. Change nothing.

    This replaces the read-triggered maintenance pass. The classification is the
    same comparison the pass made -- ``now - archived_at >= retention`` against
    the window read live from the registry -- and the difference is entirely in
    what happens next: an entry past the window is reported as a candidate and
    left on disk. Emptying it requires :func:`purge_entries` and a typed count.
    """
    try:
        registry = workbench.load_registry(registry_path)
    except workbench.WorkbenchError as error:
        raise ArchiveRefused(
            portable_archive.REASON_REGISTRY_UNREADABLE, str(error)
        ) from error
    listing = list_archive_entries(state_dir)
    review = portable_archive.cleanup_review(
        [entry.to_payload() for entry in listing.entries],
        now=now if now is not None else datetime.now(UTC),
        retention_days=registry.archive_retention_days,
    )
    return ArchiveView(
        entries=listing.entries,
        review=review,
        warnings=tuple(listing.warnings),
    )


def purge_entries(
    raw_archive_ids: Any,
    raw_confirm_count: Any,
    *,
    state_dir: str | Path | None = None,
) -> ArchivePurgeResult:
    """Permanently empty archive entries. The only function here that destroys.

    The typed count is checked before a single entry is touched, so a mismatch
    leaves every one of them in place. Past that gate each id is processed
    independently and reported on: an id that names nothing is a row saying so,
    not a reason to abandon the four beside it.
    """
    try:
        archive_ids = portable_archive.require_archive_ids(raw_archive_ids)
        portable_archive.require_count_confirmation(archive_ids, raw_confirm_count)
    except portable_archive.ArchiveRequestError as error:
        raise _refuse_request(error) from error
    return purge_archive_entries(archive_ids, state_dir=state_dir)


# --------------------------------------------------------------------------
# Locks
# --------------------------------------------------------------------------


def unlock(
    plan_dir: str, *, state_dir: str | Path | None = None, force: bool = False
) -> state.UnlockResult:
    """Release a plan lock an operator has decided is dead.

    Here rather than with the run controls because of what it touches: like
    archiving, it reaches into a plan's ASL state without a run's consent. It is
    also the one place reclaim is still deliberate -- every read stopped doing it
    in sessions 2 and 4, which left this as the path an operator asks for.
    """
    try:
        return state_locks.unlock_plan(plan_dir, state_dir, force=force)
    except plans_errors.PlanError as error:
        raise ArchiveRefused(
            portable_archive.REASON_MALFORMED_PLAN,
            str(error),
            details={"plan_dir": plan_dir},
        ) from error
    except state.StateError as error:
        if "active lock" in str(error).lower():
            raise ArchiveRefused(
                portable_archive.REASON_ACTIVE_LOCK,
                str(error),
                details={"plan_dir": plan_dir},
            ) from error
        raise ArchiveRefused(
            portable_archive.REASON_STATE_ERROR,
            str(error),
            details={"plan_dir": plan_dir},
        ) from error
    except OSError as error:
        raise ArchiveRefused(
            portable_archive.REASON_OS_ERROR,
            str(error),
            details={"plan_dir": plan_dir},
        ) from error


# --------------------------------------------------------------------------
# CLI handlers
# --------------------------------------------------------------------------


def workbench_pin_from_args(args: Any) -> int:
    """``asl workbench pin`` -- the terminal's spelling of :func:`pin_plan`.

    Here rather than in ``workbench`` so the CLI and the HTTP route share more
    than the lock: they refuse the same plan paths with the same reason, and
    only the rendering (stderr and an exit code, versus a status and a JSON
    body) is per-surface.
    """
    plan_dir = args.plan_dir
    cwd = getattr(args, "cwd", None)
    try:
        update = pin_plan(
            plan_dir,
            label=getattr(args, "label", None),
            profile=workbench.LaunchProfile(cwd=cwd) if cwd is not None else None,
        )
    except ArchiveRefused as error:
        print(f"agent-session-loop workbench pin: {error.message}", file=sys.stderr)
        return 1
    # Echo the pin as stored rather than as typed: `upsert_pin` canonicalizes,
    # and reporting the raw argument would hide a symlink or a relative path
    # having resolved somewhere the operator did not mean.
    try:
        canonical: str | None = str(plans_files.canonicalize_path(plan_dir))
    except OSError, ValueError:
        canonical = None
    matching = next(
        (pin for pin in update.registry.pins if pin.plan_dir == canonical), None
    )
    resolved = matching.plan_dir if matching is not None else plan_dir
    print(f"Pinned {resolved} in {update.path}")
    return 0


def workbench_unpin_from_args(args: Any) -> int:
    """``asl workbench unpin`` -- the terminal's spelling of :func:`unpin_plan`."""
    plan_dir = args.plan_dir
    try:
        update = unpin_plan(plan_dir)
    except ArchiveRefused as error:
        print(f"agent-session-loop workbench unpin: {error.message}", file=sys.stderr)
        return 1
    print(f"Unpinned {plan_dir} from {update.path}")
    return 0


def unlock_payload(plan_dir: str, result: state.UnlockResult) -> dict[str, Any]:
    """The facts an unlock answer carries, whatever a surface calls them."""
    return {
        "plan_dir": str(plans_files.canonicalize_path(plan_dir)),
        "lock_path": str(result.path),
        "had_lock": result.had_lock,
        "removed": result.removed,
        "active": result.was_active,
        "stale": result.was_stale,
        "message": result.message,
    }


__all__: Sequence[str] = [
    "ArchiveRefused",
    "ArchiveView",
    "BulkArchiveResult",
    "RegistryUpdate",
    "archive_many",
    "archive_one",
    "pin_plan",
    "purge_entries",
    "restore_entry",
    "review_archive",
    "set_retention",
    "unlock",
    "unlock_payload",
    "unpin_plan",
    "workbench_pin_from_args",
    "workbench_unpin_from_args",
]
