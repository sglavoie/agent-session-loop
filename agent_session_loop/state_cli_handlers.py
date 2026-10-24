"""Command-line handlers for ``unlock`` and ``migrate-state``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import plans_errors, plans_files, plans_snapshot, responses
from .cli_defaults import program_name
from .state import StateError, UnlockResult, lock_path_for_plan, plan_state_dir
from .state_locks import unlock_plan
from .state_migration import migrate_plan_state
from .state_schema_upgrade import (
    MIGRATION_MODE_UPGRADE,
    MigrationError,
    MigrationReport,
)


def _unlock_payload(
    result: UnlockResult,
    *,
    plan_dir: str | Path,
    state_dir: str | Path | None,
) -> dict[str, Any]:
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    return responses.success_payload(
        "unlock",
        reason="ok",
        plan_dir=str(canonical_plan_dir),
        state_dir=str(plan_state_dir(canonical_plan_dir, state_dir)),
        lock_path=str(result.path),
        had_lock=result.had_lock,
        removed=result.removed,
        active=result.was_active,
        stale=result.was_stale,
        message=result.message,
    )


def _unlock_error_reason(error: Exception) -> str:
    if isinstance(error, plans_errors.PlanError):
        return "malformed-plan"
    if isinstance(error, OSError):
        return "os-error"
    if "active lock" in str(error).lower():
        return "active-lock"
    return "state-error"


def _unlock_error_details(args: argparse.Namespace) -> dict[str, Any]:
    try:
        canonical_plan_dir = plans_files.canonicalize_path(args.plan_dir)
        return {
            "plan_dir": str(canonical_plan_dir),
            "lock_path": str(lock_path_for_plan(canonical_plan_dir, args.state_dir)),
        }
    except plans_errors.PlanError, OSError:
        return {"plan_dir": str(args.plan_dir)}


def unlock_from_args(args: argparse.Namespace) -> int:
    """Remove a plan lock from CLI arguments."""
    try:
        plans_snapshot.require_plan_directory(args.plan_dir)
        result = unlock_plan(args.plan_dir, args.state_dir, force=args.force)
    except (plans_errors.PlanError, StateError, OSError) as error:
        if args.json:
            details = _unlock_error_details(args)
            responses.print_json(
                responses.error_payload(
                    "unlock",
                    reason=_unlock_error_reason(error),
                    error=str(error),
                    paused=False,
                    details=details,
                    plan_dir=details.get("plan_dir", str(args.plan_dir)),
                    lock_path=details.get("lock_path"),
                )
            )
        else:
            print(f"{program_name()} unlock: {error}", file=sys.stderr)
        return 1

    if args.json:
        responses.print_json(
            _unlock_payload(result, plan_dir=args.plan_dir, state_dir=args.state_dir)
        )
    else:
        print(result.message)
    return 0


def _print_migration_report(report: MigrationReport) -> None:
    outcome = "applied" if report.applied else "dry-run"
    upgrade_only = report.mode == MIGRATION_MODE_UPGRADE
    print(f"{'Upgrade' if upgrade_only else 'Migrate'} state ({outcome})")
    if upgrade_only:
        print(f"Plan:      {report.plan_dir}")
        print(f"State dir: {report.state_dir}")
    else:
        print(f"From plan: {report.from_plan_dir}")
        print(f"To plan:   {report.plan_dir}")
        print(f"From state dir: {report.from_state_dir}")
        print(f"To state dir:   {report.state_dir}")
    if report.rewritten_fields:
        print(f"Rewritten fields: {', '.join(report.rewritten_fields)}")
    if report.schema_note:
        print(f"Schema: {report.schema_note}")
    if report.conversation_note:
        print(f"Conversations: {report.conversation_note}")
    if report.redirect_note:
        print(f"Redirects: {report.redirect_note}")
    if report.unmigrated_artifacts_note:
        print(report.unmigrated_artifacts_note)
    if not report.applied:
        if upgrade_only:
            if report.schema_upgraded:
                print("Re-run with --apply to write the upgraded state file.")
        else:
            print("Re-run with --apply to migrate the state directory.")


def _migrate_state_error_reason(error: Exception) -> str:
    if isinstance(error, MigrationError):
        return error.reason
    if isinstance(error, plans_errors.PlanError):
        return "malformed-plan"
    if isinstance(error, OSError):
        return "os-error"
    return "state-error"


def migrate_state_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl migrate-state``."""
    from_plan_dir = getattr(args, "from_plan_dir", None)
    try:
        report = migrate_plan_state(
            args.plan_dir,
            from_plan_dir=from_plan_dir,
            state_dir=getattr(args, "state_dir", None),
            apply=args.apply,
        )
    except (MigrationError, plans_errors.PlanError, StateError, OSError) as error:
        if getattr(args, "json", False):
            responses.print_json(
                responses.error_payload(
                    "migrate-state",
                    reason=_migrate_state_error_reason(error),
                    error=str(error),
                    paused=False,
                    details=getattr(error, "details", {}),
                    plan_dir=str(args.plan_dir),
                    from_plan_dir=(
                        None if from_plan_dir is None else str(from_plan_dir)
                    ),
                )
            )
        else:
            print(f"{program_name()} migrate-state: {error}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        responses.print_json(report.to_dict())
    else:
        _print_migration_report(report)
    return 0
