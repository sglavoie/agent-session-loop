"""Focused implementation extracted from :mod:`.workbench_archive`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asl_workbench import archive as portable_archive

from . import (
    workbench_registry,
    workbench_registry_models,
)
from .workbench_archive_models import (
    ArchiveError,
    ArchiveMetadata,
)


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


@dataclass(frozen=True)
class RegistryUpdate:
    """A registry written under the lock, plus where it was written."""

    registry: workbench_registry_models.WorkbenchRegistry
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
        updated, written_path = workbench_registry.mutate_registry(
            registry_path, mutator
        )
    except workbench_registry_models.WorkbenchError as error:
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
    profile: workbench_registry_models.LaunchProfile | None = None,
) -> RegistryUpdate:
    """Pin one plan, or update the pin it already has."""
    return _mutate(
        registry_path,
        lambda current: workbench_registry.upsert_pin(
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
        lambda current: workbench_registry.remove_pin(current, plan_dir),
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
        lambda current: workbench_registry.set_archive_retention_days(current, days),
        reason=portable_archive.REASON_INVALID_REQUEST,
    )
