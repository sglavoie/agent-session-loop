"""Focused implementation extracted from :mod:`.workbench_archive`."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asl_workbench import archive as portable_archive

from . import (
    plans_errors,
    plans_files,
    responses,
    state,
    state_locks,
    workbench_registry,
    workbench_registry_models,
)
from .cli_defaults import program_name
from .workbench_archive_commands import (
    ArchiveRefused,
    ArchiveView,
    BulkArchiveResult,
    _refuse_archive,
    _refuse_request,
    pin_plan,
    unpin_plan,
)
from .workbench_archive_models import (
    ArchiveError,
    ArchivePlanResult,
    ArchivePurgeResult,
    RestoreArchiveResult,
)
from .workbench_archive_operations import (
    archive_plan,
    purge_archive_entries,
    restore_archive,
)
from .workbench_archive_storage import (
    list_archive_entries,
)


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
        registry = workbench_registry.load_registry(registry_path)
    except workbench_registry_models.WorkbenchError as error:
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


def workbench_pin_from_args(args: Any) -> int:
    """``asl workbench pin`` -- the terminal's spelling of :func:`pin_plan`.

    Here rather than in ``workbench_cli`` so the CLI and the HTTP route share more
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
            profile=workbench_registry_models.LaunchProfile(cwd=cwd)
            if cwd is not None
            else None,
        )
    except ArchiveRefused as error:
        return _print_refusal(args, "pin", error)
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
    if getattr(args, "json", False):
        responses.print_json(
            responses.success_payload(
                "workbench-pin",
                reason="pinned",
                plan_dir=resolved,
                label=matching.label if matching is not None else None,
                registry_path=str(update.path),
            )
        )
        return 0
    print(f"Pinned {resolved} in {update.path}")
    return 0


def workbench_unpin_from_args(args: Any) -> int:
    """``asl workbench unpin`` -- the terminal's spelling of :func:`unpin_plan`."""
    plan_dir = args.plan_dir
    # Unpinning is idempotent, so a path with no pin still exits 0; it just
    # says so instead of claiming to have removed something.
    try:
        was_pinned = workbench_registry.is_pinned(
            workbench_registry.load_registry(), plan_dir
        )
    except workbench_registry_models.WorkbenchError:
        was_pinned = True
    try:
        update = unpin_plan(plan_dir)
    except ArchiveRefused as error:
        return _print_refusal(args, "unpin", error)
    if getattr(args, "json", False):
        responses.print_json(
            responses.success_payload(
                "workbench-unpin",
                reason="unpinned" if was_pinned else "not-pinned",
                plan_dir=plan_dir,
                registry_path=str(update.path),
            )
        )
        return 0
    if was_pinned:
        print(f"Unpinned {plan_dir} from {update.path}")
    else:
        print(f"{plan_dir} was not pinned in {update.path}; nothing to unpin")
    return 0


def _print_refusal(args: Any, leaf: str, error: ArchiveRefused) -> int:
    """Report a refused pin or unpin as one JSON object or one stderr line."""
    if getattr(args, "json", False):
        responses.print_json(
            responses.error_payload(
                f"workbench-{leaf}",
                reason=error.reason,
                error=error.message,
                details=error.details,
            )
        )
    else:
        print(f"{program_name()} workbench {leaf}: {error.message}", file=sys.stderr)
    return 1


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
