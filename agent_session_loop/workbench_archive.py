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

import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asl_workbench import archive as portable_archive

from . import plans, state, workbench

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
    error: workbench.ArchiveError, *, details: Mapping[str, Any] | None = None
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

    entries: tuple[workbench.ArchiveMetadata, ...]
    review: portable_archive.CleanupReview
    warnings: tuple[str, ...] = ()

    @property
    def retention_days(self) -> int:
        return self.review.retention_days

    def entry_payloads(self) -> list[dict[str, Any]]:
        """Each entry's payload with its retention verdict folded in.

        Folded in here rather than stored on :class:`~.workbench.ArchiveMetadata`
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
        return plans.canonicalize_path(plan_dir)
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
) -> workbench.ArchivePlanResult:
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
        return workbench.archive_plan(
            canonical,
            state_dir=state_dir,
            registry_path=registry_path,
            plan_roots=plan_roots,
            include_default_dirs=include_default_dirs,
        )
    except workbench.ArchiveError as error:
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
            canonical = plans.canonicalize_path(raw_plan_dir)
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
            result = workbench.archive_plan(
                canonical,
                state_dir=state_dir,
                registry_path=registry_path,
                plan_roots=plan_roots,
                include_default_dirs=include_default_dirs,
            )
        except workbench.ArchiveError as error:
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
) -> workbench.RestoreArchiveResult:
    """Put one archived plan and its state back where they came from.

    Refuses rather than overwrites when either destination is occupied: a
    restore that clobbered a live plan would destroy the one thing an archive
    exists to avoid destroying.
    """
    try:
        return workbench.restore_archive(
            archive_id, state_dir=state_dir, registry_path=registry_path
        )
    except workbench.ArchiveError as error:
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
    listing = workbench.list_archive_entries(state_dir)
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
) -> workbench.ArchivePurgeResult:
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
    return workbench.purge_archive_entries(archive_ids, state_dir=state_dir)


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
        return state.unlock_plan(plan_dir, state_dir, force=force)
    except plans.PlanError as error:
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
        canonical: str | None = str(plans.canonicalize_path(plan_dir))
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
        "plan_dir": str(plans.canonicalize_path(plan_dir)),
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
