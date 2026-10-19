"""Shared guard records and run context fixtures for continuation tests."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import continuation_guards, plans_snapshot

from .support import DEFAULT_MULTI_REPO_SPECS

RUN_ID = "run-continuation-1"

SESSION_ID = "1"

SESSION_LABEL = "Session 1"

SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

# ---------------------------------------------------------------------------
# Guard-record fixtures
# ---------------------------------------------------------------------------


def registered_guard(
    *,
    changes: dict[str, tuple[str, ...]],
    aliases: tuple[str, ...] = ("SVC", "OAK"),
    passed: bool = True,
    violations: tuple[dict[str, Any], ...] = (),
    unavailable: dict[str, Any] | None = None,
    added: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The evidence one passing registered turn leaves in its result artifact.

    ``changes`` maps an alias to the untracked paths that attempt added, which
    is the shape both the commit staging and the protected-path projection read
    back out.
    """
    return {
        "passed": passed,
        "read_aliases": list(aliases),
        "write_aliases": list(aliases),
        "registered_repositories": [
            {"alias": alias, "path": f"/repos/{alias}"} for alias in aliases
        ],
        "changed": {
            alias: {
                "repository": f"/repos/{alias}",
                "alias": alias,
                "tracked": [],
                "untracked_added": list(paths),
                "untracked_changed": [],
                "untracked_removed": [],
                "worktree_ignore": [],
                "ignore_config": [],
                "qualified_paths": [f"{alias}:{path}" for path in paths],
                "worktree_of": None,
            }
            for alias, paths in changes.items()
        },
        "authorized_aliases": [alias for alias in aliases if alias in changes],
        "added": list(added),
        "removed": [],
        "unavailable": dict(unavailable or {}),
        "violations": list(violations),
        "external_writes": {},
        "excluded": [".venv"],
        "include_ignored": False,
    }


def legacy_guard(*paths: str) -> dict[str, Any]:
    """The evidence one passing legacy single-repository turn leaves behind."""
    return {
        "passed": True,
        "scope_mode": "legacy-single-repository",
        "repository_root": "/repo",
        "read_aliases": ["legacy"],
        "write_aliases": ["legacy"],
        "registered_repositories": [],
        "changed": {
            ".": {
                "tracked": [],
                "untracked_added": list(paths),
                "qualified_paths": [f".:{path}" for path in paths],
            }
        },
        "authorized_aliases": ["legacy"] if paths else [],
        "added": [],
        "removed": [],
        "unavailable": {},
        "violations": [],
        "external_writes": {},
        "excluded": [],
        "include_ignored": False,
    }


# ---------------------------------------------------------------------------
# The guard sequence
# ---------------------------------------------------------------------------


class RecordingPersistence:
    """The run's durable-write mutex, kept real, with its events retained."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))


class FakeRunContext:
    """A :class:`turn_postflight.RunContext` with a real attempt ledger on it."""

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {"run_id": RUN_ID}
        self.persistence = RecordingPersistence()
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.guard_controller = mock.Mock()
        self.session_attempts = continuation_guards.SessionAttempts()
        self.pauses: list[dict[str, Any]] = []

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})
