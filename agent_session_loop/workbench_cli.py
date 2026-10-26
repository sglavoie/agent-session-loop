"""Terminal handlers for ``asl workbench`` registry commands and ``asl plans``."""

from __future__ import annotations

import math
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    plans_errors,
    plans_models,
    plans_readiness,
    responses,
)
from .cli_defaults import program_name
from .cli_parser_support import NON_NEGATIVE_NUMBER, flag_refusal
from .workbench_discovery import discover_plans
from .workbench_registry import load_registry
from .workbench_registry_models import WorkbenchError
from .workbench_summary import build_workbench_summaries
from .workbench_summary_model import WorkbenchSummary

WORKBENCH_COMMAND_NAME = "workbench-registry"
PLANS_COMMAND_NAME = "plans"


# The ``workbench pin`` / ``workbench unpin`` CLI handlers moved to
# ``workbench_archive``, alongside the HTTP routes that do the same two things,
# so the terminal and the browser refuse the same inputs for the same stated
# reason. ``cli.py`` resolves them lazily by name, so nothing here imports that
# module (and nothing needs to).


def workbench_registry_from_args(args: Any) -> int:
    """Print the default registry contents in text or JSON form."""
    try:
        registry = load_registry(None)
    except WorkbenchError as error:
        if getattr(args, "json", False):
            responses.print_json(
                responses.error_payload(
                    WORKBENCH_COMMAND_NAME,
                    reason="invalid-registry",
                    error=str(error),
                )
            )
        else:
            print(
                f"{program_name()} workbench registry: {error}",
                file=sys.stderr,
            )
        return 1

    if getattr(args, "json", False):
        responses.print_json(
            responses.success_payload(
                WORKBENCH_COMMAND_NAME,
                reason="ok",
                registry=registry.to_payload(),
            )
        )
        return 0

    print(f"Registry path: {registry.path}")
    print(f"Schema version: {registry.schema_version}")
    if registry.pins:
        print(f"Pins ({len(registry.pins)}):")
        for pin in registry.pins:
            label_suffix = f" [{pin.label}]" if pin.label else ""
            print(f"  - {pin.plan_dir}{label_suffix}")
    else:
        print("Pins: none")
    if registry.plan_roots:
        print(f"Plan roots ({len(registry.plan_roots)}):")
        for root in registry.plan_roots:
            print(f"  - {root}")
    else:
        print("Plan roots: none")
    if registry.warnings:
        print(f"Warnings ({len(registry.warnings)}):")
        for warning in registry.warnings:
            print(f"  - {warning}")
    return 0


def _plans_error(args: Any, *, reason: str, error: str) -> int:
    if getattr(args, "json", False):
        responses.print_json(
            responses.error_payload(
                PLANS_COMMAND_NAME,
                reason=reason,
                error=error,
            )
        )
    else:
        print(f"{program_name()} plans: {error}", file=sys.stderr)
    return 1


def _plan_readiness_payload(
    plan_dir: Path,
    *,
    warnings: list[str],
) -> dict[str, str] | None:
    try:
        metadata = plans_readiness.read_readiness_metadata(plan_dir)
    except (plans_errors.PlanError, OSError, ValueError) as error:
        warnings.append(f"{plan_dir}: readiness unreadable ({error})")
        return None
    if metadata is None:
        return None
    return {
        "state": metadata.state,
        "readiness_sha256": metadata.readiness_sha256,
        "checked_at": metadata.checked_at,
    }


def _plan_effective_activity_at(
    summary: WorkbenchSummary,
    readiness: Mapping[str, str] | None,
) -> str | None:
    if summary.last_activity_at:
        return summary.last_activity_at
    if readiness is None:
        return None
    checked_at = readiness.get("checked_at")
    return checked_at if isinstance(checked_at, str) and checked_at else None


def _plan_has_no_run_state(summary: WorkbenchSummary) -> bool:
    state_path = summary.state_path
    if state_path is None:
        return True
    try:
        return not state_path.is_file()
    except OSError:
        return True


def _plan_matches_stale_days(
    summary: WorkbenchSummary,
    *,
    stale_days: float,
    now: datetime,
    readiness: Mapping[str, str] | None,
) -> bool:
    activity = _plan_effective_activity_at(summary, readiness)
    if not activity:
        return False
    if activity.endswith("Z"):
        activity = activity[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(activity)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return now - parsed >= timedelta(days=stale_days)
    except OverflowError:
        # More days than ``timedelta`` holds: nothing is that stale.
        return False


def _print_plan_listing(
    rows: Sequence[tuple[WorkbenchSummary, dict[str, str] | None]],
    *,
    warnings: Sequence[str],
    show_readiness: bool,
) -> None:
    print(f"Plans ({len(rows)}):")
    if not rows:
        print("  (none)")
    for summary, readiness in rows:
        counts = summary.counts
        print(f"- {summary.plan_dir}")
        print(f"  state: {summary.primary_state}")
        print(
            "  sessions: "
            f"pending={counts.get('pending', 0)} "
            f"complete={counts.get('complete', 0)} "
            f"unknown={counts.get('unknown', 0)}"
        )
        print(
            f"  run state: {'absent' if _plan_has_no_run_state(summary) else 'present'}"
        )
        print(
            "  activity: "
            f"{_plan_effective_activity_at(summary, readiness) or 'unknown'}"
        )
        next_pending = summary.next_pending_session_id or "(none)"
        print(f"  next pending session: {next_pending}")
        if show_readiness:
            readiness_state = (
                readiness.get("state")
                if readiness is not None and readiness.get("state")
                else "(none)"
            )
            print(f"  readiness: {readiness_state}")
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")


def plans_from_args(args: Any, *, now: datetime | None = None) -> int:
    """List discovered plans with stale, never-run, and readiness filters.

    ``now`` is the command clock. The calendar arithmetic itself already lives
    in the pure ``_plan_matches_stale_days`` helper; injecting the clock here
    too means every ``--stale-days`` decision the command makes is reproducible
    from its inputs. Tests can then pin a moment -- a leap day, a year
    boundary, either side of the cutoff -- instead of writing fixed timestamps
    that quietly expire as the real calendar advances past them.
    """
    moment = datetime.now(UTC) if now is None else now
    stale_days = getattr(args, "stale_days", None)
    # NaN compares false against zero and infinity overflows the cutoff, so
    # finiteness is checked as well as the sign.
    if stale_days is not None and (not math.isfinite(stale_days) or stale_days < 0):
        return _plans_error(
            args,
            reason="input-error",
            error=flag_refusal("--stale-days", NON_NEGATIVE_NUMBER, stale_days),
        )

    readiness_filter = getattr(args, "readiness", None)
    if readiness_filter is not None:
        try:
            readiness_filter = plans_readiness.parse_readiness_state(readiness_filter)
        except plans_errors.PlanError:
            return _plans_error(
                args,
                reason="input-error",
                error=flag_refusal(
                    "--readiness",
                    f"must be one of {', '.join(plans_models.READINESS_STATES)}",
                    readiness_filter,
                ),
            )

    try:
        registry = load_registry(None)
    except WorkbenchError as error:
        return _plans_error(
            args,
            reason="invalid-registry",
            error=str(error),
        )

    discovery = discover_plans(
        state_root=getattr(args, "state_dir", None),
        registry=registry,
    )
    summaries = build_workbench_summaries(
        discovery.plans,
        state_dir=getattr(args, "state_dir", None),
    )
    warnings = list(discovery.warnings)
    rows: list[tuple[WorkbenchSummary, dict[str, str] | None]] = []
    payloads: list[dict[str, Any]] = []

    for summary in summaries:
        readiness: dict[str, str] | None = None
        # A never-run plan's activity is its readiness check stamp, so it is
        # read whenever activity is unknown, not only for a filter that needs it.
        if readiness_filter is not None or not summary.last_activity_at:
            readiness = _plan_readiness_payload(
                summary.plan_dir,
                warnings=warnings,
            )

        if readiness_filter is not None:
            readiness_state = readiness.get("state") if readiness is not None else None
            if readiness_state != readiness_filter:
                continue

        if getattr(args, "never_run", False) and not _plan_has_no_run_state(summary):
            continue

        if stale_days is not None and not _plan_matches_stale_days(
            summary,
            stale_days=stale_days,
            now=moment,
            readiness=readiness,
        ):
            continue

        payload = summary.to_payload()
        if readiness_filter is not None:
            payload["readiness"] = dict(readiness) if readiness is not None else None
        rows.append((summary, readiness))
        payloads.append(payload)

    if getattr(args, "json", False):
        responses.print_json(
            responses.success_payload(
                PLANS_COMMAND_NAME,
                reason="ok",
                filters={
                    "stale_days": stale_days,
                    "never_run": bool(getattr(args, "never_run", False)),
                    "readiness": readiness_filter,
                },
                plan_count=len(payloads),
                plans=payloads,
                warnings=warnings,
            )
        )
        return 0

    _print_plan_listing(
        rows,
        warnings=warnings,
        show_readiness=readiness_filter is not None,
    )
    return 0
