"""Repository claims held by other live runs.

Two runs writing one working tree corrupt each other, and the only attribution
signal a session guard has is a time window, so the overlap has to be refused
before either run starts rather than explained afterwards. This module answers
one question over the lock registry: which repositories that a starting run is
about to claim are already claimed by somebody else?

It is pure logic over :func:`session_overview.scan_running_rows` output, so the
semantics are testable without spawning runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any

from . import plans, session_overview


__all__ = [
    "LIVENESS_RUNNING",
    "LIVENESS_FOREIGN_HOST",
    "RepositoryConflict",
    "detect_conflicts",
    "conflict_message",
    "conflict_details",
]


LIVENESS_RUNNING = "running"
LIVENESS_FOREIGN_HOST = "foreign-host"


@dataclass(frozen=True)
class RepositoryConflict:
    """One other run whose claimed repositories overlap this run's registry."""

    plan_dir: Path
    pid: int | None
    hostname: str | None
    run_id: str
    started_at: str
    liveness: str
    repositories: tuple[str, ...]


def _canonical_roots(roots: Iterable[Any]) -> frozenset[str]:
    canonical: set[str] = set()
    for root in roots:
        if not isinstance(root, (str, Path)):
            continue
        text = str(root).strip()
        if not text:
            continue
        try:
            canonical.add(str(plans.canonicalize_path(text)))
        except (OSError, ValueError):
            continue
    return frozenset(canonical)


def _row_roots(row: session_overview.RunningRow) -> frozenset[str]:
    """Roots a running row claims, preferring the lock's own declaration.

    The lock is written before the run's first ``state.json``, so its claim is
    readable in the window where the projected registry is not. Both sources
    are absolute; the workspace-relative ``path`` is never comparable, because
    two workspace roots produce the same relative path.
    """
    if row.repository_roots:
        return _canonical_roots(row.repository_roots)
    return _canonical_roots(
        entry.get("resolved_root")
        for entry in row.repositories
        if isinstance(entry, dict)
    )


def _warn_undeclared_foreign_lock(stale: session_overview.StaleLock) -> None:
    host = stale.hostname or "another host"
    print(
        f"agent-session-loop: the lock at {stale.lock_path} was recorded on "
        f"{host} and declares no repositories, so its claim cannot be checked "
        "from here; clear it with 'asl unlock' once you have confirmed that "
        "runner is gone",
        file=sys.stderr,
    )


def _conflict_sort_key(conflict: RepositoryConflict) -> tuple[str, int, str, str]:
    return (
        str(conflict.plan_dir),
        conflict.pid if conflict.pid is not None else -1,
        conflict.run_id,
        conflict.started_at,
    )


def detect_conflicts(
    *,
    plan_dir: str | Path,
    repository_roots: Iterable[str],
    state_dir: str | Path | None = None,
) -> tuple[RepositoryConflict, ...]:
    """Return the live claims that overlap ``repository_roots``.

    Only two lock classes own anything. A live row owns what it claims. A lock
    recorded on another host owns it too: nothing here can tell a dead remote
    runner from a live one, which is why :func:`state.acquire_lock` refuses to
    steal one, and refusing on its declared overlap is the same stance. Every
    other stale class — dead pid, pidless, plan-hash mismatch, unparseable —
    owns nothing.

    A foreign lock that declares no repositories is the version 1 case, and
    also the version 2 lock of a run that claims nothing. Neither can be
    checked, so the overlap is warned about on stderr rather than refused: one
    legacy lock would otherwise brick every run in the state root.

    The scan reclaims locks this host has proven dead. That is deliberate: this
    caller is about to write anyway, and reclaiming keeps ``asl sessions`` in
    agreement with the refusal.
    """
    claimed = _canonical_roots(repository_roots)
    if not claimed:
        return ()
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    rows, stale_locks, _skipped = session_overview.scan_running_rows(
        state_dir, reclaim=True
    )
    conflicts: list[RepositoryConflict] = []
    for row in rows:
        if row.plan_dir == canonical_plan_dir and row.pid == os.getpid():
            continue
        shared = claimed & _row_roots(row)
        if not shared:
            continue
        conflicts.append(
            RepositoryConflict(
                plan_dir=row.plan_dir,
                pid=row.pid,
                hostname=row.hostname,
                run_id=row.run_id,
                started_at=row.started_at,
                liveness=LIVENESS_RUNNING,
                repositories=tuple(sorted(shared)),
            )
        )
    for stale in stale_locks:
        if not stale.foreign_host:
            continue
        declared = _canonical_roots(stale.repository_roots)
        if not declared:
            _warn_undeclared_foreign_lock(stale)
            continue
        shared = claimed & declared
        if not shared:
            continue
        conflicts.append(
            RepositoryConflict(
                plan_dir=Path(stale.plan_dir),
                pid=stale.pid,
                hostname=stale.hostname,
                run_id="",
                started_at=stale.created_at,
                liveness=LIVENESS_FOREIGN_HOST,
                repositories=tuple(sorted(shared)),
            )
        )
    conflicts.sort(key=_conflict_sort_key)
    return tuple(conflicts)


def _describe_owner(conflict: RepositoryConflict) -> str:
    parts = [
        f"pid {conflict.pid}" if conflict.pid is not None else "unknown pid",
    ]
    if conflict.hostname:
        parts.append(f"host {conflict.hostname}")
    if conflict.run_id:
        parts.append(f"run {conflict.run_id}")
    if conflict.started_at:
        parts.append(f"started {conflict.started_at}")
    if conflict.liveness == LIVENESS_FOREIGN_HOST:
        parts.append("liveness unknown from this host")
    return ", ".join(parts)


def conflict_message(
    plan_dir: str | Path, conflicts: Iterable[RepositoryConflict]
) -> str:
    """Render the refusal, naming every owner and every shared repository."""
    ordered = tuple(conflicts)
    if not ordered:
        return ""
    lines = [
        "another run already claims repositories registered by "
        f"{plans.canonicalize_path(plan_dir)}:"
    ]
    for conflict in ordered:
        repositories = ", ".join(conflict.repositories)
        lines.append(
            f"  - {conflict.plan_dir} ({_describe_owner(conflict)}) claims "
            f"{repositories}"
        )
    if any(
        conflict.liveness == LIVENESS_FOREIGN_HOST for conflict in ordered
    ):
        lines.append(
            "  A lock recorded on another host is never stolen; clear it with "
            "'asl unlock' once you have confirmed that runner is gone."
        )
    lines.append(
        "  Two agents in one working tree cannot be made safe, so there is no "
        "override: stop the other run, or narrow this plan's registry."
    )
    return "\n".join(lines)


def _conflict_payload(conflict: RepositoryConflict) -> dict[str, Any]:
    return {
        "plan_dir": str(conflict.plan_dir),
        "pid": conflict.pid,
        "hostname": conflict.hostname,
        "run_id": conflict.run_id,
        "started_at": conflict.started_at,
        "liveness": conflict.liveness,
        "repositories": list(conflict.repositories),
    }


def conflict_details(
    conflicts: Iterable[RepositoryConflict],
) -> dict[str, Any]:
    """Project conflicts as the JSON details of a startup refusal payload."""
    ordered = tuple(conflicts)
    return {
        "repository_contention": {
            "conflicts": [_conflict_payload(conflict) for conflict in ordered],
            "repositories": sorted(
                {
                    repository
                    for conflict in ordered
                    for repository in conflict.repositories
                }
            ),
        }
    }
