"""Overview data contracts for the `asl sessions` command.

Defines the typed payload surface for running activity rows, recent
turn rows, stale lock records, skipped artifact records, and aggregate
counts. Kept independent from CLI rendering so the recent and running
scanner sessions and the CLI/rendering session can share one shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    plans_errors,
    plans_files,
    plans_metadata,
    plans_registry,
    responses,
    state,
    state_locks,
    state_payloads,
    workspaces,
)
from .formatting import format_duration_seconds

DEFAULT_RECENT_LIMIT = 20


RUNNING_KIND = "running"
RECENT_KIND = "recent"

STATE_JSON_NAME = "state.json"
LOCK_JSON_NAME = "lock.json"
RUNS_DIR_NAME = "runs"
RESULT_JSON_NAME = "result.json"
TURN_PREFIX = "turn-"


@dataclass(frozen=True)
class RunningRow:
    """Currently running ASL-managed session turn."""

    kind: str
    plan_dir: Path
    state_dir: Path
    run_id: str
    harness: str
    session_id: str
    turn_index: int | None
    pid: int | None
    started_at: str
    elapsed_seconds: float | None
    idle_seconds: float | None
    last_activity_at: str | None
    lock_active: bool
    lock_stale: bool
    check_index: int | None = None
    check_count: int | None = None
    check_name: str | None = None
    check_kind: str | None = None
    cwd: str | None = None
    workspace_root: str | None = None
    registry_digest: str | None = None
    repositories: tuple[dict[str, Any], ...] = ()
    # Claimed by the lock itself, so it is readable before the run has written
    # any state; empty for a version 1 lock, which declares nothing.
    repository_roots: tuple[str, ...] = ()
    hostname: str | None = None


@dataclass(frozen=True)
class RecentRow:
    """Recently completed ASL-managed session turn."""

    plan_dir: Path
    state_dir: Path
    run_id: str
    harness: str
    session_id: str
    turn_index: int | None
    status: str
    finished_at: str
    duration_seconds: float | None
    artifact_dir: Path
    session_kind: str | None = None
    cwd: str | None = None
    workspace_root: str | None = None
    registry_digest: str | None = None
    repositories: tuple[dict[str, Any], ...] = ()


def _coerce_nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _workspace_fields(
    plan_dir: Path, payload: dict[str, Any]
) -> tuple[str | None, str | None, str | None, tuple[dict[str, Any], ...]]:
    root = _coerce_nonempty_str(payload.get("workspace_root"))
    digest = _coerce_nonempty_str(payload.get("registry_digest"))
    raw_rows = payload.get("repositories")
    rows = (
        tuple(dict(row) for row in raw_rows if isinstance(row, dict))
        if isinstance(raw_rows, list)
        else ()
    )
    cwd = _coerce_nonempty_str(payload.get("cwd"))
    if root is not None and rows:
        return cwd or root, root, digest, rows
    try:
        registry = plans_registry.read_repository_registry(plan_dir)
        metadata = plans_metadata.load_plan_metadata(plan_dir)
    except plans_errors.PlanError, OSError:
        return cwd, None, None, ()
    if registry is None or not isinstance(
        metadata, plans_metadata.WorkspacePlanMetadata
    ):
        return cwd, None, None, ()
    workspace_root = str(plans_files.canonicalize_path(metadata.workspace_root))
    repositories = tuple(
        {
            "alias": repository.alias,
            "path": repository.path,
            "origin": repository.origin,
            "resolved_root": str(
                plans_files.canonicalize_path(Path(workspace_root) / repository.path)
            ),
        }
        for repository in registry.repositories
    )
    return workspace_root, workspace_root, metadata.registry_digest, repositories


@dataclass(frozen=True)
class StaleLock:
    """Stale lock encountered while scanning state directories."""

    plan_dir: str
    lock_path: Path
    pid: int | None
    reason: str
    created_at: str
    # A lock recorded on another host is stale only in the sense that nothing
    # here can judge its owner, so it still holds whatever it claims.
    foreign_host: bool = False
    repository_roots: tuple[str, ...] = ()
    hostname: str | None = None


@dataclass(frozen=True)
class SkippedArtifact:
    """Artifact or state file skipped during overview scanning."""

    path: Path
    reason: str


@dataclass(frozen=True)
class OverviewCounts:
    """Aggregate counts surfaced alongside overview rows."""

    running: int = 0
    recent: int = 0
    stale_locks: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "running": self.running,
            "recent": self.recent,
            "stale_locks": self.stale_locks,
            "skipped": self.skipped,
        }


def parse_iso_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning None when unparseable.

    Accepts the trailing-Z form used by JSON state payloads and returns a
    timezone-aware datetime. Naive datetimes are coerced to UTC so callers
    can compare or sort safely.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def format_local_time(value: Any) -> str:
    """Format a timestamp value as local time for compact text output."""
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return "unknown"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def plan_basename_label(plan_dir: str | Path) -> str:
    """Return the basename of a plan directory for compact text output."""
    name = Path(str(plan_dir)).name
    return name or str(plan_dir)


def plan_label(plan_dir: str | Path, *, verbose: bool = False) -> str:
    """Return a plan label: full canonical path when verbose, basename otherwise."""
    if verbose:
        return str(plans_files.canonicalize_path(plan_dir))
    return plan_basename_label(plan_dir)


def running_sort_key(row: RunningRow) -> tuple[float, str, str, str]:
    """Sort running rows newest first with stable tie-breakers."""
    started = parse_iso_timestamp(row.started_at)
    started_key = -started.timestamp() if started is not None else float("inf")
    return (started_key, str(row.plan_dir), row.run_id, row.session_id)


def recent_sort_key(row: RecentRow) -> tuple[float, str, str, str]:
    """Sort recent rows by most recent finish first with stable tie-breakers."""
    finished = parse_iso_timestamp(row.finished_at)
    finished_key = -finished.timestamp() if finished is not None else float("inf")
    return (finished_key, str(row.plan_dir), row.run_id, row.session_id)


__all__ = ["argparse", "json", "sys", "Iterator", "dataclass", "UTC", "datetime", "Path", "Any", "plans_errors", "plans_files", "plans_metadata", "plans_registry", "responses", "state", "state_locks", "state_payloads", "workspaces", "format_duration_seconds", "DEFAULT_RECENT_LIMIT", "RUNNING_KIND", "RECENT_KIND", "STATE_JSON_NAME", "LOCK_JSON_NAME", "RUNS_DIR_NAME", "RESULT_JSON_NAME", "TURN_PREFIX", "RunningRow", "RecentRow", "_coerce_nonempty_str", "_workspace_fields", "StaleLock", "SkippedArtifact", "OverviewCounts", "parse_iso_timestamp", "format_local_time", "plan_basename_label", "plan_label", "running_sort_key", "recent_sort_key"]  # fmt: skip
