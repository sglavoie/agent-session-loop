"""Explicit operator acceptance of a rejected turn's remote-ref-only delta.

This does not authorize a worker write or accept a review result. It closes one
historical guard incident under the plan lock, leaving ordinary readiness and
the next turn's complete mutation guard in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from . import events, plans, repository_evidence, state, workspace_evidence, workspaces


class RecoveryError(ValueError):
    """The retained evidence cannot support this narrow acceptance."""


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


def _check_original_binding(
    event_path: Path, guard: dict[str, Any], workspace: workspaces.ResolvedWorkspace
) -> None:
    """Bind recovery to the workspace of the original failure, before retries."""
    starts: dict[str, dict[str, Any]] = {}
    for line in event_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise RecoveryError("unreadable event history") from None
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            raise RecoveryError("incomplete event history")
        payload = event["payload"]
        run_id = payload.get("run_id")
        if event.get("name") == events.RUN_STARTED_EVENT and isinstance(run_id, str):
            starts[run_id] = payload
        details = payload.get("details")
        if (
            event.get("name") == events.RUN_PAUSED_EVENT
            and isinstance(details, dict)
            and details.get("workspace_write_guard") == guard
        ):
            start = starts.get(run_id, {}) if isinstance(run_id, str) else {}
            if (
                start.get("workspace_root") != str(workspace.root)
                or start.get("registry_digest") != workspace.registry.digest
            ):
                raise RecoveryError(
                    "original run workspace binding is missing or changed"
                )
            return
    raise RecoveryError("original scope failure is missing")


def _proposal(plan_dir: Path, session_id: str, event_path: Path) -> dict[str, Any]:
    snapshot = plans.inspect_plan(plan_dir)
    if snapshot.next_pending is None or snapshot.next_pending.id != session_id:
        raise RecoveryError("session must be the next pending session")
    registry = plans.read_repository_registry(plan_dir)
    metadata = plans.load_plan_metadata(plan_dir)
    if (
        registry is None
        or not isinstance(metadata, plans.WorkspacePlanMetadata)
        or metadata.registry_digest != registry.digest
    ):
        raise RecoveryError("an unchanged registered workspace binding is required")
    workspace = workspaces.resolve_workspace(metadata.workspace_root, registry)
    guard = workspace_evidence._latest_workspace_scope_repair_requirement(
        event_path, session_id
    )
    if guard is None or guard.get("session_id") != session_id:
        raise RecoveryError("no retained scope failure for this session")
    _check_original_binding(event_path, guard, workspace)
    registered = [dict(alias=repo.alias, path=repo.path) for repo in registry]
    if guard.get("registered_repositories") != registered:
        raise RecoveryError("repository registry changed since the rejected turn")
    updates = _updates(guard)
    current = repository_evidence.capture_registered_snapshot(
        workspace.root, plan_dir, tuple(repo.path for repo in registry)
    )
    if set(current) != set(guard["snapshot_after"]):
        raise RecoveryError("repository topology changed since the rejected turn")
    for fingerprint in current.values():
        if fingerprint.get(
            "git"
        ) is not True or repository_evidence.repository_evidence_unavailable(
            fingerprint
        ):
            raise RecoveryError("current repository evidence is unavailable")
        if any(
            fingerprint.get(key)
            for key in (
                "tracked_changed",
                "index_changed",
                "untracked",
                "index_flagged",
            )
        ):
            raise RecoveryError("commit or restore current working-tree changes first")
    for update in updates:
        if _refs(current[update["repository"]]).get(update["ref"]) != update["after"]:
            raise RecoveryError(
                "retained remote ref has moved again; inspect it separately"
            )
    proposal = {
        "plan_dir": str(plan_dir),
        "session_id": session_id,
        "registry_digest": registry.digest,
        "workspace_root": str(workspace.root),
        "retained_guard_sha256": _digest(guard),
        "plan_sha256": snapshot.content_sha256,
        "current_snapshot_sha256": _digest(current),
        "current_heads": {key: value.get("head") for key, value in current.items()},
        "updates": updates,
    }
    return {**proposal, "evidence_digest": _digest(proposal)}


def accept_ref_updates(
    plan_dir: str | Path,
    session_id: str,
    *,
    state_dir: str | Path | None = None,
    accept: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Preview by default; accept only an explicitly reviewed, still-current digest."""
    if accept is not None and (reason is None or not reason.strip()):
        raise RecoveryError("acceptance requires an explicit operator reason")
    plan_dir = plans.canonicalize_path(plan_dir)
    state.acquire_lock(plan_dir, state_dir, owner="asl-accept-ref-updates")
    try:
        event_path = state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
        proposal = _proposal(plan_dir, session_id, event_path)
        if accept is not None:
            if accept != proposal["evidence_digest"]:
                raise RecoveryError(
                    "reviewed evidence changed; preview and review again"
                )
            # Recheck after preparation so an intervening edit cannot ride the
            # operator's earlier digest. The runner is excluded by the lock.
            if _proposal(plan_dir, session_id, event_path) != proposal:
                raise RecoveryError("evidence changed during acceptance")
            events.append_event(
                event_path,
                events.Event(
                    name=events.WORKSPACE_REF_UPDATES_ACCEPTED_EVENT,
                    payload={
                        "reason": reason,
                        "details": {
                            "session_id": session_id,
                            "acceptance": proposal,
                        },
                    },
                ),
            )
        return {"ok": True, "accepted": accept is not None, **proposal}
    finally:
        state.release_lock(plan_dir, state_dir)


def accept_ref_updates_from_args(args: argparse.Namespace) -> int:
    try:
        result = accept_ref_updates(
            args.plan_dir,
            args.session,
            state_dir=args.state_dir,
            accept=args.accept,
            reason=args.reason,
        )
    except (
        RecoveryError,
        plans.PlanError,
        state.StateError,
        workspaces.WorkspaceError,
        OSError,
    ) as error:
        result = {"ok": False, "accepted": False, "error": str(error)}
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1
