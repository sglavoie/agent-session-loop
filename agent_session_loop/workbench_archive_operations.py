"""Focused implementation extracted from :mod:`.workbench_archive`."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from . import (
    plans_files,
    state,
    workbench_registry,
    workbench_registry_models,
)
from .workbench_archive_models import (
    ArchiveError,
    ArchiveMetadata,
    ArchivePlanResult,
    ArchivePurgeOutcome,
    ArchivePurgeResult,
    RestoreArchiveResult,
)
from .workbench_archive_storage import (
    _is_valid_archive_id,
    _validate_plan_for_mutation,
    archive_entry_dir,
    archive_plan_path,
    archive_state_path,
    compute_expires_at,
    load_archive_metadata,
    new_archive_id,
    remove_archive_entry,
    save_archive_metadata,
)


def _discard_empty_entry(entry_dir: Path) -> None:
    """Remove an archive entry a failed archive left empty, best effort."""
    with contextlib.suppress(OSError):
        entry_dir.rmdir()


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
            _discard_empty_entry(entry_dir)
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
            # The plan is already inside an entry that has no archive.json yet,
            # which no listing would show. Put it back where it was; the entry
            # is then empty and goes too.
            if plan_archived:
                with contextlib.suppress(OSError):
                    shutil.move(
                        str(archive_plan_path(archive_id, state_dir)), str(canonical)
                    )
            _discard_empty_entry(entry_dir)
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
        current: workbench_registry_models.WorkbenchRegistry,
    ) -> workbench_registry_models.WorkbenchRegistry:
        nonlocal pin_archived
        pin_archived = any(pin.plan_dir == str(canonical) for pin in current.pins)
        return workbench_registry.remove_pin(current, canonical)

    try:
        _updated, written_path = workbench_registry.mutate_registry(
            registry_path, _remove_matching_pin
        )
    except workbench_registry_models.WorkbenchError as error:
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
    except workbench_registry_models.WorkbenchError as error:
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
            current: workbench_registry_models.WorkbenchRegistry,
        ) -> workbench_registry_models.WorkbenchRegistry:
            nonlocal pin_restored
            pin_restored = True
            return workbench_registry.upsert_pin(
                current, original, label=pin.label, profile=pin.profile
            )

        try:
            _updated, written_path = workbench_registry.mutate_registry(
                registry_path, _restore_pin
            )
        except workbench_registry_models.WorkbenchError as error:
            raise ArchiveError("registry-unreadable", str(error), status=500) from error

    try:
        remove_archive_entry(archive_id, state_dir)
    except workbench_registry_models.WorkbenchError as error:
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
        except workbench_registry_models.WorkbenchError as error:
            outcomes.append(ArchivePurgeOutcome(archive_id, False, str(error)))
            continue
        outcomes.append(ArchivePurgeOutcome(archive_id, removed))
    return ArchivePurgeResult(outcomes=tuple(outcomes))
