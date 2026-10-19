"""Focused implementation extracted from :mod:`.workbench_archive`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    workbench_registry_models,
)

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
  ``workbench_registry.mutate_registry``, which holds a flock on ``workbench.json.lock``
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
    pin: workbench_registry_models.RegistryPin | None = None
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
