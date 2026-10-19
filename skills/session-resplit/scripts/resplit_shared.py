# mypy: ignore-errors
# ruff: noqa
#!/usr/bin/env python3
"""Resplit one pending session into nested child sessions.

This helper performs the filesystem-safe parts of the session-resplit skill:
plan inspection, target validation, lock acquisition, archival, child file
creation, and derived ledger updates. Deterministic rendering, table mutation,
archive-path, readiness, and atomic multi-file writes are delegated to
the ``agent_session_loop.authoring_*`` modules so split and resplit share one
implementation.

Child completion contracts are never placeholders. Explicit child specs must
allocate the parent's verification checks by name (``inherit_checks``) or
replace them (``checks``), and must state explicit ``done_when`` criteria;
ambiguous allocation is rejected. Without explicit specs, every generated
child derives its contract from the parent's own completion contract, and a
parent without a contract is rejected instead of receiving a
``python3 -m unittest`` placeholder.

Sequential by design: this script does not read `parallel-validation.md`, does
not emit `## Parallel Metadata` blocks for generated children, and leaves
legacy parallel manifest tables (waves, ownership) untouched. Generated
children include a parseable `## Completion Contract` so `asl verify` /
`asl run` enforce the same gate used elsewhere.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_session_loop import (  # noqa: E402 - import needs the sys.path insertion above
    authoring_manifest,
    authoring_render,
    authoring_transaction,
    contracts,
    harnesses,
    plans_errors,
    plans_files,
    plans_models,
    plans_registry,
    plans_session_ids,
    plans_session_scope,
    plans_snapshot,
    responses,
    state,
    state_locks,
)
from agent_session_loop.workspaces import (  # noqa: E402 - needs the sys.path insert above
    RepositoryRegistry,
)

MIN_PARTS = 2
MAX_PARTS = 20
HANDOFF_HEADING = "## Handoff"
ARCHIVE_ROOT_NAME = authoring_transaction.ARCHIVE_ROOT_NAME
CHILD_SPEC_KEYS = frozenset(
    {"title", "instructions", "done_when", "checks", "inherit_checks"}
)

#: One-line remediation per pause reason so a supervising agent (or human) can
#: recover without re-reading the skill. Keyed by every ``ResplitError.reason``
#: this script raises (plus ``malformed-plan`` from a bubbled ``PlanError``).
PAUSE_REMEDIATION: dict[str, str] = {
    "no-contract": (
        "Add a parseable `## Completion Contract` to the parent session, or "
        "pass --child-spec with explicit 'done_when' and "
        "'checks'/'inherit_checks' per child."
    ),
    "ambiguous-contract": (
        "Fix --child-spec so every parent check is inherited or replaced and "
        "each child states explicit 'done_when' plus checks."
    ),
    "ambiguous-input": (
        "Re-run with an explicit plan directory or session-<id>.md file and a "
        f"child count between {MIN_PARTS} and {MAX_PARTS}."
    ),
    "blocked": (
        "Select a pending session to resplit; the chosen session is already "
        "complete or the plan has no pending session."
    ),
    "unknown-status": (
        "Repair the session's final handoff `- **Status**:` line to a known "
        "value (pending/complete) before resplitting."
    ),
    "malformed-plan": (
        "Repair the malformed session or plan file (missing handoff, contract, "
        "or instructions above the handoff) and re-run."
    ),
    "collision": (
        "Remove or rename the conflicting descendant/child files, then re-run "
        "the resplit."
    ),
    "cannot-split": (
        "Add implementation instructions above the parent handoff, or resplit a "
        "different session."
    ),
    "lock-contention": (
        "Another live runner holds an active lock on this plan (the helper "
        "already self-cleared any stale lock). Wait for that runner to finish, "
        "or investigate and stop it, then re-run the resplit."
    ),
}


def _next_steps(plan_dir: str) -> list[dict[str, Any]]:
    """Per-harness `session-expand` invocations to run after a resplit."""
    step = responses.invocation_step(
        (
            "Rerun session-expand so expansion notes match the nested child "
            "sessions before continuing execution."
        ),
        harnesses.skill_invocations("session-expand", f"{plan_dir}/"),
    )
    return responses.next_steps_payload([step])["next_steps"]


class ResplitError(Exception):
    """A recoverable resplit refusal with a stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ChildSpec:
    """One generated child session."""

    session_id: plans_session_ids.SessionId
    title: str
    instructions: str
    contract: contracts.CompletionContract
    previous_session_id: plans_session_ids.SessionId | None

    @property
    def filename(self) -> str:
        return self.session_id.filename


@dataclass(frozen=True)
class ParentDetails:
    """Parsed parent session content needed for child generation."""

    title: str
    body: str
    had_expansion: bool
    prior_session_file: str | None
    contract: contracts.CompletionContract | None
    scope: plans_session_scope.SessionScope | None
    registry: RepositoryRegistry | None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _session_sort_key(session_id: plans_session_ids.SessionId) -> tuple[int, ...]:
    return session_id.sort_key


def _is_descendant(
    candidate: plans_session_ids.SessionId, parent: plans_session_ids.SessionId
) -> bool:
    return (
        len(candidate.components) > len(parent.components)
        and candidate.components[: len(parent.components)] == parent.components
    )


def _child_id(
    parent: plans_session_ids.SessionId, index: int
) -> plans_session_ids.SessionId:
    return plans_session_ids.SessionId(parent.components + (index,))


def _validate_parts(value: int) -> int:
    if value < MIN_PARTS or value > MAX_PARTS:
        raise ResplitError(
            "ambiguous-input",
            f"child count must be between {MIN_PARTS} and {MAX_PARTS}: {value}",
        )
    return value


def _status_by_id(
    snapshot: plans_models.PlanSnapshot,
) -> dict[str, plans_models.SessionStatus]:
    return {session.id: session for session in snapshot.sessions}


def _select_parent(
    snapshot: plans_models.PlanSnapshot,
    requested_session_id: str | None,
) -> plans_session_ids.SessionId:
    if requested_session_id is not None:
        try:
            return plans_session_ids.parse_session_id(requested_session_id)
        except ValueError as error:
            raise ResplitError(
                "ambiguous-input", f"invalid session id: {requested_session_id}"
            ) from error

    next_pending = snapshot.next_pending
    if next_pending is None:
        raise ResplitError("blocked", "no pending session is available to resplit")
    return next_pending.session_id


def _validate_snapshot(
    snapshot: plans_models.PlanSnapshot,
    parent_id: plans_session_ids.SessionId,
    parts: int,
) -> plans_models.SessionStatus:
    for session in snapshot.sessions:
        if session.status == "unknown":
            raise ResplitError(
                "unknown-status",
                f"{session.session.filename} has unknown status {session.raw_value!r}",
            )
        if session.status == "empty":
            raise ResplitError(
                "malformed-plan",
                f"{session.session.filename} is missing a populated final handoff "
                f"status",
            )

    parent_status = _status_by_id(snapshot).get(parent_id.value)
    if parent_status is None:
        raise ResplitError(
            "ambiguous-input", f"session {parent_id.value} does not exist"
        )
    if parent_status.status == "complete":
        raise ResplitError("blocked", f"session {parent_id.value} is already complete")
    if parent_status.status != "pending":
        raise ResplitError(
            "unknown-status",
            f"session {parent_id.value} has status {parent_status.raw_value!r}",
        )

    for session in snapshot.sessions:
        if _is_descendant(session.session_id, parent_id):
            raise ResplitError(
                "collision",
                f"descendant session already exists: {session.session.filename}",
            )

    for index in range(1, parts + 1):
        child = _child_id(parent_id, index)
        child_path = snapshot.plan_dir / child.filename
        child_expansion_path = snapshot.plan_dir / child.expansion_filename
        if child_path.exists():
            raise ResplitError(
                "collision", f"child file already exists: {child.filename}"
            )
        if child_expansion_path.exists():
            raise ResplitError(
                "collision",
                f"child expansion file already exists: {child.expansion_filename}",
            )

    return parent_status


__all__ = [name for name in globals() if not name.startswith("__")]
