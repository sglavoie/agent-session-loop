"""Evidence-bound operator recovery for rejected registered workspace turns.

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

from . import (
    events,
    plans,
    repository_evidence,
    state,
    state_locks,
    workspace_evidence,
    workspaces,
)


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
    state_locks.acquire_lock(plan_dir, state_dir, owner="asl-accept-ref-updates")
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
        state_locks.release_lock(plan_dir, state_dir)


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


RECOVERY_FILE_NAME = "external-change-recovery.json"


def _dirty_paths(fingerprint: dict[str, Any]) -> list[str]:
    evidence = repository_evidence.repository_state(fingerprint)
    if evidence is None:
        raise RecoveryError("repository state evidence is unavailable")
    return evidence["dirty_paths"]


def _external_proposal(
    plan_dir: Path,
    session_id: str,
    aliases: tuple[str, ...],
    event_path: Path,
    *,
    completed_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    scope = plans.parse_session_scope(
        snapshot.next_pending.path.read_text(), registry.aliases
    )
    if (
        not aliases
        or len(set(aliases)) != len(aliases)
        or set(aliases) - set(registry.aliases)
    ):
        raise RecoveryError("select distinct registered repository aliases")
    if set(aliases) & set(scope.writes):
        raise RecoveryError(
            "cannot acknowledge external changes in a writable repository"
        )
    guard = workspace_evidence._latest_workspace_scope_repair_requirement(
        event_path, session_id
    )
    if guard is None:
        guard = completed_guard
    if guard is None or guard.get("session_id") != session_id:
        raise RecoveryError("no retained scope failure for this session")
    _check_original_binding(event_path, guard, workspace)
    if guard.get("registered_repositories") != [
        dict(alias=r.alias, path=r.path) for r in registry
    ]:
        raise RecoveryError("repository registry changed since the rejected turn")
    if guard.get("read_aliases") != list(scope.reads) or guard.get(
        "write_aliases"
    ) != list(scope.writes):
        raise RecoveryError("session scope changed since the rejected turn")
    if guard.get("unavailable"):
        raise RecoveryError(
            "unavailable or external filesystem evidence requires separate recovery"
        )
    # Root and origin projections from the rejected run must still resolve exactly.
    starts = {}
    original_start = None
    for line in event_path.read_text().splitlines():
        event = json.loads(line)
        payload = event["payload"]
        if event["name"] == events.RUN_STARTED_EVENT:
            starts[payload.get("run_id")] = payload
        if payload.get("details", {}).get("workspace_write_guard") == guard:
            original_start = starts.get(payload.get("run_id"))
            break
    roots = [
        dict(
            alias=r.alias,
            path=r.repository.path,
            origin=r.repository.origin,
            resolved_root=str(r.root),
        )
        for r in workspace.repositories
    ]
    if original_start is None or original_start.get("repositories") != roots:
        raise RecoveryError("original registered roots/origins are missing or changed")
    if original_start.get("commit_mode") != "per-session":
        raise RecoveryError("recovery requires a registered per-session run")
    before, after = guard.get("snapshot_before"), guard.get("snapshot_after")
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or not before
        or not after
    ):
        raise RecoveryError("complete historical snapshots are required")
    exclude = tuple(guard.get("excluded", ()))
    include_ignored = guard.get("include_ignored")
    if not isinstance(include_ignored, bool):
        raise RecoveryError("original guard policy is unavailable")
    current = repository_evidence.capture_registered_snapshot(
        workspace.root,
        plan_dir,
        tuple(r.path for r in registry),
        default_exclude=exclude,
        include_ignored=include_ignored,
    )
    for vector in (before, after, current):
        for fp in vector.values():
            if (
                not isinstance(fp, dict)
                or fp.get("git") is not True
                or repository_evidence.repository_evidence_unavailable(fp)
            ):
                raise RecoveryError("complete repository evidence is required")
            _refs(fp)
    if set(current) != set(after) or set(before) - set(after):
        raise RecoveryError("repository topology changed outside the retained incident")
    state_path = event_path.parent / events.STATE_FILE_NAME
    run_config = (
        json.loads(state_path.read_text()).get("run_config", {})
        if state_path.exists()
        else {}
    )
    # Aliases the operator told the run someone else may change: their
    # independent dirt and movement are expected, not evidence against the turn.
    external = frozenset(run_config.get("allow_external_changes") or ())
    owners = {}
    head_refs = {}
    targets = []
    blockers = []
    for key, fp in current.items():
        linked = repository_evidence.split_linked_worktree_key(key)
        owner = key if linked is None else linked[0]
        repository = next(
            (r for r in workspace.repositories if r.repository.path == owner), None
        )
        if repository is None:
            raise RecoveryError("unregistered repository evidence")
        owners[key] = repository.alias
        checkout = repository.root if linked is None else Path(linked[1])
        head_ref = repository_evidence._run_git(
            checkout, ["symbolic-ref", "--quiet", "HEAD"]
        )
        if head_ref.returncode not in (0, 1):
            raise RecoveryError("current branch evidence is unavailable")
        head_refs[key] = head_ref.stdout.strip() if head_ref.returncode == 0 else None
        if key not in before and linked is None:
            raise RecoveryError("added registered roots cannot be acknowledged")
        tolerated = repository.alias in external
        if repository.alias in aliases:
            if repository.alias in scope.reads and _dirty_paths(fp) and not tolerated:
                blockers.append(f"needed read-only repository {key} is dirty")
            continue
        if repository.alias not in scope.writes:
            if not tolerated and (fp != after[key] or before.get(key) != after[key]):
                blockers.append(f"unacknowledged repository changes in {key}")
            if repository.alias in scope.reads and _dirty_paths(fp) and not tolerated:
                blockers.append(f"needed read-only repository {key} is dirty")
            continue
        dirty = _dirty_paths(after[key])
        old_dirty = set(_dirty_paths(before[key])) if key in before else set()
        changed = guard.get("changed", {}).get(key, {}).get("qualified_paths", [])
        owned = {p.split(":", 1)[1] for p in changed if isinstance(p, str) and ":" in p}
        if key not in before:
            # Added worktrees are owned by their alias, with their own staging root.
            owned = set(dirty)
        if old_dirty or set(dirty) - owned or after[key].get("index_flagged"):
            blockers.append(
                f"dirty paths in {key} cannot all be proven retained owned work"
            )
        if fp != after[key]:
            blockers.append(f"retained content or index evidence drifted in {key}")
        if dirty:
            targets.append(
                dict(
                    repository=key,
                    alias=repository.alias,
                    root=str(repository.root if linked is None else Path(linked[1])),
                    paths=dirty,
                    head_before=fp["head"],
                )
            )
    violations = guard.get("violations")
    if (
        not isinstance(violations, list)
        or not violations
        or any(v.get("alias") not in aliases for v in violations)
    ):
        blockers.append("selected aliases do not cover the original violations")
    run_policy = {
        key: run_config.get(key)
        for key in (
            "allow_external_changes",
            "commit_mode",
            "review_guard_exclude",
            "review_guard_include_ignored",
            "registry_digest",
            "workspace_root",
        )
    }
    proposal = dict(
        plan_dir=str(plan_dir),
        session_id=session_id,
        repositories=list(aliases),
        registry_digest=registry.digest,
        workspace_root=str(workspace.root),
        registered_roots=roots,
        plan_sha256=snapshot.content_sha256,
        retained_guard_sha256=_digest(guard),
        guard_policy=dict(excluded=list(exclude), include_ignored=include_ignored),
        run_policy=run_policy,
        original_pause_sha256=_digest(event),
        original_incident=guard,
        historical_before=before,
        historical_after=after,
        current_snapshot=current,
        owners=owners,
        head_refs=head_refs,
        checkpoint_targets=targets,
        remaining_blockers=blockers,
        original_run_id=original_start["run_id"],
        authorship="Operator assertion required; authorship is not established",
    )
    return {**proposal, "evidence_digest": _digest(proposal)}


def _check_recovery_authority(
    proposal: dict[str, Any],
    event_path: Path,
    *,
    completed: bool = False,
) -> dict[str, Any]:
    fresh = _external_proposal(
        Path(proposal["plan_dir"]),
        proposal["session_id"],
        tuple(proposal["repositories"]),
        event_path,
        completed_guard=proposal["original_incident"] if completed else None,
    )
    for key in (
        "plan_sha256",
        "retained_guard_sha256",
        "registered_roots",
        "registry_digest",
        "workspace_root",
        "guard_policy",
        "repositories",
        "run_policy",
        "original_pause_sha256",
        "head_refs",
    ):
        if fresh[key] != proposal[key]:
            raise RecoveryError("recovery authority changed; inspect durable progress")

    return fresh["current_snapshot"]


def _capture_recovery(proposal: dict[str, Any]) -> dict[str, Any]:
    policy = proposal["guard_policy"]
    current = repository_evidence.capture_registered_snapshot(
        Path(proposal["workspace_root"]),
        Path(proposal["plan_dir"]),
        tuple(r["path"] for r in proposal["registered_roots"]),
        default_exclude=tuple(policy["excluded"]),
        include_ignored=policy["include_ignored"],
    )

    for fp in current.values():
        if fp.get(
            "git"
        ) is not True or repository_evidence.repository_evidence_unavailable(fp):
            raise RecoveryError("current recovery evidence is unavailable")
    return current


def _checkpoint_content(root: Path, paths: list[str]) -> list[list[Any]]:
    return [
        repository_evidence._fingerprint_repo_node(
            root / path,
            rel=path,
            argv=(),
            reason_prefix="checkpoint",
            absent_kind="absent",
        )
        for path in paths
    ]


def _finish_checkpoint(progress: dict[str, Any], current: dict[str, Any]) -> None:
    """Reconcile an interrupted commit only from its exact parent/message/content."""
    intent = progress["in_flight"]
    target = intent["target"]
    key, root = target["repository"], Path(target["root"])
    head = current[key]["head"]

    def git(*args: str) -> str:
        result = repository_evidence._run_git(root, list(args))
        if result.returncode:
            raise RecoveryError("checkpoint Git proof unavailable")
        return result.stdout.rstrip("\n")

    if head == intent["head_before"]:
        raise RecoveryError("checkpoint has not committed; retry the resumable command")
    if (
        git("show", "-s", "--format=%P", head) != intent["head_before"]
        or git("show", "-s", "--format=%B", head).strip() != progress["message"].strip()
        or sorted(
            git("diff-tree", "--no-commit-id", "--name-only", "-r", head).splitlines()
        )
        != sorted(target["paths"])
        or _dirty_paths(current[key])
        or _checkpoint_content(root, target["paths"]) != intent["content"]
    ):
        raise RecoveryError(
            "interrupted checkpoint cannot be proven; preserve and inspect it"
        )
    expected = progress["expected_snapshot"]
    for repository, fp in current.items():
        old = expected.get(repository)
        if old is None:
            raise RecoveryError("checkpoint topology changed")
        allowed = (
            {
                "head",
                "refs",
                "diff_sha256",
                "tracked_changed",
                "tracked_patches",
                "index_diff_sha256",
                "index_changed",
                "index_patches",
                "untracked",
                "worktree_ignore",
            }
            if repository == key
            else {"refs"}
        )
        if {k: v for k, v in fp.items() if k not in allowed} != {
            k: v for k, v in old.items() if k not in allowed
        }:
            raise RecoveryError("unrelated evidence changed during checkpoint")
        if repository == key:
            expected_ignore = [
                entry
                for entry in old.get("worktree_ignore", ())
                if entry[0] not in target["paths"]
            ]
            if fp.get("worktree_ignore", []) != expected_ignore:
                raise RecoveryError(
                    "unrelated ignore content changed during checkpoint"
                )
        old_refs, new_refs = _refs(old), _refs(fp)
        if old_refs.keys() != new_refs.keys() or any(
            old_refs[ref] != new_refs[ref]
            and not (
                old_refs[ref] == intent["head_before"]
                and new_refs[ref] == head
                and ref == progress["proposal"]["head_refs"][key]
                and progress["proposal"]["owners"][repository] == target["alias"]
            )
            for ref in old_refs
        ):
            raise RecoveryError("unrelated refs changed during checkpoint")
    if current.keys() != expected.keys():
        raise RecoveryError("checkpoint topology changed")
    progress["checkpoints"][key] = head
    progress["expected_snapshot"] = current
    progress["in_flight"] = None


def _reconcile_checkpoint_staging(
    progress: dict[str, Any], current: dict[str, Any]
) -> None:
    """A failed commit may have staged exactly its authorized retained content."""
    intent = progress["in_flight"]
    target = intent["target"]
    key, root = target["repository"], Path(target["root"])
    expected = progress["expected_snapshot"]
    if current.keys() != expected.keys():
        raise RecoveryError("checkpoint topology changed")
    for repository, fp in current.items():
        old = expected[repository]
        allowed = (
            {
                "diff_sha256",
                "tracked_changed",
                "tracked_patches",
                "index_diff_sha256",
                "index_changed",
                "index_patches",
                "untracked",
                "worktree_ignore",
            }
            if repository == key
            else set()
        )
        if {k: v for k, v in fp.items() if k not in allowed} != {
            k: v for k, v in old.items() if k not in allowed
        }:
            raise RecoveryError("unrelated evidence changed after failed checkpoint")
    if (
        set(_dirty_paths(current[key])) != set(target["paths"])
        or _checkpoint_content(root, target["paths"]) != intent["content"]
    ):
        raise RecoveryError("retained content changed after failed checkpoint")
    staged = current[key]["index_changed"]
    expected_ignore = [
        entry
        for entry in expected[key].get("worktree_ignore", ())
        if entry[0] not in staged
    ]
    if current[key].get("worktree_ignore", []) != expected_ignore:
        raise RecoveryError("unrelated ignore content changed after failed checkpoint")
    if set(staged) - set(target["paths"]):
        raise RecoveryError("unrelated staged content after failed checkpoint")
    if (
        staged
        and repository_evidence._run_git(
            root, ["diff", "--quiet", "--", *staged]
        ).returncode
        != 0
    ):
        raise RecoveryError("staged content differs from retained files")
    progress["expected_snapshot"] = current


def acknowledge_external_changes(
    plan_dir: str | Path,
    session_id: str,
    repositories: tuple[str, ...],
    *,
    state_dir: str | Path | None = None,
    accept: str | None = None,
    reason: str | None = None,
    checkpoint_retained: bool = False,
) -> dict[str, Any]:
    """Preview independent changes; acceptance never grants ongoing write authority."""
    from . import durability, run_commit

    if accept is not None and (reason is None or not reason.strip()):
        raise RecoveryError("acceptance requires an explicit operator reason")
    plan_dir = plans.canonicalize_path(plan_dir)
    state_locks.acquire_lock(
        plan_dir, state_dir, owner="asl-acknowledge-external-changes"
    )
    try:
        state_root = state.plan_state_dir(plan_dir, state_dir)
        event_path = state_root / events.EVENTS_FILE_NAME
        journal = state_root / RECOVERY_FILE_NAME
        progress = json.loads(journal.read_text()) if journal.exists() else None
        if progress is not None and progress.get("complete"):
            progress = None
        if progress is None:
            proposal = _external_proposal(
                plan_dir, session_id, repositories, event_path
            )
        else:
            proposal = progress["proposal"]
            completed_event = any(
                record.get("name")
                == events.WORKSPACE_EXTERNAL_CHANGES_ACKNOWLEDGED_EVENT
                and record.get("payload", {}).get("details", {}).get("acceptance")
                == proposal
                for record in (
                    json.loads(line) for line in event_path.read_text().splitlines()
                )
            )
            _check_recovery_authority(proposal, event_path, completed=completed_event)
        resume = [
            "asl",
            "acknowledge-external-changes",
            str(plan_dir),
            "--session",
            session_id,
        ]
        for alias in repositories:
            resume.extend(["--repository", alias])
        if state_dir is not None:
            resume.extend(["--state-dir", str(state_dir)])
        resume.extend(
            [
                "--accept",
                proposal["evidence_digest"],
                "--reason",
                reason or (progress or {}).get("reason", "OPERATOR_REASON"),
                "--checkpoint-retained",
                "--json",
            ]
        )
        if accept is None:
            return dict(
                ok=True,
                accepted=False,
                **proposal,
                recovery_progress=progress,
                resume_argv=resume,
            )
        if accept != proposal["evidence_digest"]:
            raise RecoveryError("reviewed evidence changed; preview and review again")
        if progress is not None and completed_event:
            current = _capture_recovery(proposal)
            if current != progress["expected_snapshot"]:
                raise RecoveryError("evidence changed after completed acknowledgement")
            progress["complete"] = True
            durability.write_json_file(journal, progress, durable=True)
            return dict(
                ok=True, accepted=True, **proposal, checkpoints=progress["checkpoints"]
            )
        if proposal["remaining_blockers"]:
            raise RecoveryError("; ".join(proposal["remaining_blockers"]))
        if proposal["checkpoint_targets"] and not checkpoint_retained:
            raise RecoveryError("retained checkpoints require --checkpoint-retained")
        if progress is None:
            if (
                _external_proposal(plan_dir, session_id, repositories, event_path)
                != proposal
            ):
                raise RecoveryError("evidence changed during acceptance")
            message = run_commit._paused_work_commit_message(
                session_label=f"session {session_id}",
                pause_reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                commit_mode="per-session",
                repair_attempt=None,
                repair_max_attempts=None,
                run_id=proposal["original_run_id"],
            )
            progress = dict(
                proposal=proposal,
                reason=reason,
                expected_snapshot=proposal["current_snapshot"],
                checkpoints={},
                in_flight=None,
                message=message,
                complete=False,
                resume_argv=resume,
            )
            durability.write_json_file(journal, progress, durable=True)

        def persist() -> None:
            durability.write_json_file(journal, progress, durable=True)

        try:
            current = _capture_recovery(proposal)
            if (
                progress["in_flight"] is not None
                and current != progress["expected_snapshot"]
            ):
                intent = progress["in_flight"]
                if (
                    current[intent["target"]["repository"]]["head"]
                    == intent["head_before"]
                ):
                    _reconcile_checkpoint_staging(progress, current)
                else:
                    _finish_checkpoint(progress, current)
                persist()
            if current != progress["expected_snapshot"]:
                raise RecoveryError(
                    "evidence changed during recovery; preserve and inspect it"
                )
            for index, target in enumerate(proposal["checkpoint_targets"], 1):
                if target["repository"] in progress["checkpoints"]:
                    continue
                _check_recovery_authority(proposal, event_path)
                if _capture_recovery(proposal) != progress["expected_snapshot"]:
                    raise RecoveryError("evidence changed before checkpoint")
                root = Path(target["root"])
                progress["in_flight"] = dict(
                    target=target,
                    head_before=current[target["repository"]]["head"],
                    content=_checkpoint_content(root, target["paths"]),
                )
                persist()
                artifacts = (
                    state_root
                    / "external-change-recovery"
                    / proposal["evidence_digest"]
                )
                message_file = run_commit._write_paused_work_message(
                    artifacts, progress["message"]
                )
                result = run_commit._run_asl_commit(
                    cwd=root,
                    run_dir=artifacts,
                    commit_index=index,
                    session_id=session_id,
                    repository_key=target["repository"],
                    repository_alias=target["alias"],
                    staging_paths=tuple(
                        f"{target['alias']}:{p}" for p in target["paths"]
                    ),
                    generation=run_commit._CommitGeneration(message_file=message_file),
                )
                current = _capture_recovery(proposal)
                if (
                    current[target["repository"]]["head"]
                    != progress["in_flight"]["head_before"]
                ):
                    _finish_checkpoint(progress, current)
                    persist()
                if not result["success"]:
                    raise RecoveryError(
                        f"checkpoint failed in {target['repository']}; "
                        f"inspect {result['result_path']}"
                    )
            current = _capture_recovery(proposal)
            if current != progress["expected_snapshot"] or any(
                _dirty_paths(current[t["repository"]])
                for t in proposal["checkpoint_targets"]
            ):
                raise RecoveryError(
                    "checkpoint targets are not clean or unrelated evidence changed"
                )
            if _check_recovery_authority(proposal, event_path) != current:
                raise RecoveryError("evidence changed during final acknowledgement")
            events.append_event(
                event_path,
                events.Event(
                    name=events.WORKSPACE_EXTERNAL_CHANGES_ACKNOWLEDGED_EVENT,
                    payload=dict(
                        reason=progress["reason"],
                        details=dict(
                            session_id=session_id,
                            acceptance=proposal,
                            checkpoints=progress["checkpoints"],
                            final_snapshot=current,
                        ),
                    ),
                ),
            )
            progress["complete"] = True
            persist()
            return dict(
                ok=True, accepted=True, **proposal, checkpoints=progress["checkpoints"]
            )
        except (RecoveryError, OSError) as error:
            return dict(
                ok=False,
                accepted=False,
                error=str(error),
                checkpoints=progress["checkpoints"],
                resume_argv=resume,
            )
    finally:
        state_locks.release_lock(plan_dir, state_dir)


def acknowledge_external_changes_from_args(args: argparse.Namespace) -> int:
    try:
        result = acknowledge_external_changes(
            args.plan_dir,
            args.session,
            tuple(args.repository),
            state_dir=args.state_dir,
            accept=args.accept,
            reason=args.reason,
            checkpoint_retained=args.checkpoint_retained,
        )
    except (
        RecoveryError,
        plans.PlanError,
        state.StateError,
        workspaces.WorkspaceError,
        repository_evidence.RepositoryEvidenceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        result = dict(ok=False, accepted=False, error=str(error))
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def recovery_diagnostics(
    plan_dir: Path,
    session_id: str,
    guard: dict[str, Any],
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Offer explicit operator recovery without claiming independent authorship."""
    import shlex

    aliases = sorted(
        {
            v["alias"]
            for v in guard.get("violations", ())
            if isinstance(v.get("alias"), str)
            and v["alias"] not in guard.get("write_aliases", ())
        }
    )
    argv = [
        "asl",
        "acknowledge-external-changes",
        str(plan_dir),
        "--session",
        session_id,
    ]
    for alias in aliases:
        argv.extend(["--repository", alias])
    if state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    argv.append("--json")
    return dict(
        preview_command=shlex.join(argv) if aliases else None,
        acceptance=(
            "Review evidence; use --accept DIGEST --reason TEXT "
            "and --checkpoint-retained"
        ),
        **{
            "progress": None,
            **recovery_journal_projection(
                plan_dir, state_dir=state_dir, session_id=session_id, guard=guard
            ),
        },
    )


def _incident_run_id(event_path: Path, guard: dict[str, Any]) -> str | None:
    """The run whose recorded pause retained ``guard``, when it is on record.

    A guard that has not been published yet (the pause is being built right
    now) has no event, and its digest alone already tells it apart from any
    earlier incident.
    """
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        details = payload.get("details")
        if isinstance(details, dict) and details.get("workspace_write_guard") == guard:
            run_id = payload.get("run_id")
            return run_id if isinstance(run_id, str) else None
    return None


def recovery_journal_projection(
    plan_dir: Path,
    *,
    state_dir: str | Path | None = None,
    session_id: str | None = None,
    guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the plan-level recovery journal onto one incident.

    The journal outlives the incident it recovered, so reading it bare would
    report an earlier session's finished recovery as progress on whatever
    pause is current (ASI-157). ``progress`` is therefore only projected when
    the journal's proposal names this incident -- the same session, the same
    retained guard digest, and the same original run. With no current incident
    only an unfinished recovery is projected, since acknowledging resumes it
    whatever pause is current. Any other journal is reported as
    ``historical_recovery``, without its completion state.
    """
    state_root = state.plan_state_dir(plan_dir, state_dir)
    path = state_root / RECOVERY_FILE_NAME
    if not path.exists():
        return {}
    try:
        progress = json.loads(path.read_text())
    except ValueError, OSError:
        return {"progress": {"error": "recovery journal is unreadable"}}
    proposal = progress.get("proposal") if isinstance(progress, dict) else None
    if not isinstance(proposal, dict):
        return {"progress": {"error": "recovery journal has no proposal"}}
    if guard is None:
        current = not progress.get("complete")
    else:
        incident_run_id = _incident_run_id(state_root / events.EVENTS_FILE_NAME, guard)
        current = (
            proposal.get("session_id") == session_id
            and proposal.get("retained_guard_sha256") == _digest(guard)
            and (
                incident_run_id is None
                or proposal.get("original_run_id") == incident_run_id
            )
        )
    if current:
        return {"progress": progress}
    return {
        "historical_recovery": {
            "session_id": proposal.get("session_id"),
            "original_run_id": proposal.get("original_run_id"),
            "retained_guard_sha256": proposal.get("retained_guard_sha256"),
        }
    }
