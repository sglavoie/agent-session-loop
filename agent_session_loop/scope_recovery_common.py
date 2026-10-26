# ruff: noqa: F401
"""Evidence-bound operator recovery for rejected registered workspace turns.

This does not authorize a worker write or accept a review result. It closes one
historical guard incident under the plan lock, leaving ordinary readiness and
the next turn's complete mutation guard in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_registry,
    plans_session_scope,
    plans_snapshot,
    repository_evidence,
    responses,
    state,
    state_locks,
    workspace_evidence,
    workspaces,
)


class RecoveryError(ValueError):
    """The retained evidence cannot support this narrow acceptance."""


def _event_records(event_path: Path) -> Iterator[dict[str, Any]]:
    """Yield each event record that parses as an object, in journal order.

    A blank line or one torn by an interrupted append says nothing about any
    incident, so it is skipped rather than failing the whole recovery.
    """
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


#: ``reason`` for a recovery the evidence refused or that stopped partway.
RECOVERY_REASON_REFUSED = "recovery-refused"
RECOVERY_REASON_INTERRUPTED = "recovery-interrupted"


def recovery_error_reason(error: Exception) -> str:
    """The JSON ``reason`` for a recovery command that raised ``error``."""
    if isinstance(error, plans_errors.PlanNotFoundError):
        return "plan-not-found"
    if isinstance(error, plans_errors.PlanError):
        return "malformed-plan"
    if isinstance(error, state.StateError):
        return "state-error"
    if isinstance(error, workspaces.WorkspaceError):
        return "workspace-error"
    if isinstance(error, repository_evidence.RepositoryEvidenceError):
        return "repository-evidence-error"
    if isinstance(error, OSError):
        return "os-error"
    return RECOVERY_REASON_REFUSED


def recovery_failure(
    command: str, error: Exception | str, *, reason: str | None = None, **fields: Any
) -> dict[str, Any]:
    """A refused recovery in the common error envelope, keeping ``accepted``."""
    if reason is None:
        reason = (
            RECOVERY_REASON_REFUSED
            if isinstance(error, str)
            else recovery_error_reason(error)
        )
    return responses.error_payload(
        command, reason=reason, error=str(error), accepted=False, **fields
    )


def print_recovery_result(command: str, result: dict[str, Any]) -> int:
    """Print one recovery result as compact JSON; exit 0 only when it succeeded."""
    responses.print_json({"command": command, **result})
    return 0 if result["ok"] else 1


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _refs(fingerprint: dict[str, Any]) -> dict[str, str]:
    raw = fingerprint.get("refs")
    if not isinstance(raw, list) or any(
        not isinstance(pair, (list, tuple))
        or len(pair) != 2
        or not all(isinstance(item, str) and item for item in pair)
        for pair in raw
    ):
        raise RecoveryError("incomplete ref evidence")
    refs = dict(raw)
    if len(refs) != len(raw):
        raise RecoveryError("duplicate ref evidence")
    return refs


def _updates(guard: dict[str, Any]) -> list[dict[str, str]]:
    before, after = guard.get("snapshot_before"), guard.get("snapshot_after")
    if (
        guard.get("unavailable")
        or not isinstance(before, dict)
        or not before
        or not isinstance(after, dict)
        or before.keys() != after.keys()
    ):
        raise RecoveryError("complete unchanged repository topology is required")
    updates = []
    for repository, old in before.items():
        new = after[repository]
        if (
            not isinstance(old, dict)
            or not isinstance(new, dict)
            or old.get("git") is not True
            or new.get("git") is not True
            or repository_evidence.repository_evidence_unavailable(old)
            or repository_evidence.repository_evidence_unavailable(new)
        ):
            raise RecoveryError("complete repository evidence is required")
        # Compare the raw fingerprints, not a curated list of change fields:
        # new evidence categories must never silently become acceptable here.
        if {k: v for k, v in old.items() if k != "refs"} != {
            k: v for k, v in new.items() if k != "refs"
        }:
            raise RecoveryError("retained changes include more than remote-ref updates")
        old_refs, new_refs = _refs(old), _refs(new)
        if old_refs.keys() != new_refs.keys():
            raise RecoveryError("ref creation or deletion requires separate recovery")
        for ref in sorted(old_refs):
            if old_refs[ref] == new_refs[ref]:
                continue
            if not ref.startswith("refs/remotes/"):
                raise RecoveryError("local branch or tag changes cannot be accepted")
            updates.append(
                dict(
                    repository=repository,
                    ref=ref,
                    before=old_refs[ref],
                    after=new_refs[ref],
                )
            )
    if not updates:
        raise RecoveryError("no retained remote-ref updates")
    return updates
