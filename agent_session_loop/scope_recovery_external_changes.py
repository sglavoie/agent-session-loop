# ruff: noqa: F403, F405
from .scope_recovery_common import *
from .scope_recovery_common import _digest, _event_records, _refs
from .scope_recovery_ref_updates import _check_original_binding

RECOVERY_FILE_NAME = "external-change-recovery.json"


def _dirty_paths(fingerprint: dict[str, Any]) -> list[str]:
    evidence = repository_evidence.repository_state(fingerprint)
    if evidence is None:
        raise RecoveryError("repository state evidence is unavailable")
    return evidence["dirty_paths"]


def _recorded_run_config(state_path: Path) -> dict[str, Any]:
    """The run's recorded config, or a recovery refusal when it cannot be read.

    A state that is torn, not UTF-8, or not an object used to escape as a raw
    decode error; it is refused like every other unusable piece of evidence.
    """
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError, ValueError, RecursionError:
        raise RecoveryError("recorded run state is unreadable") from None
    if not isinstance(payload, dict):
        raise RecoveryError("recorded run state is unreadable")
    run_config = payload.get("run_config") or {}
    if not isinstance(run_config, dict):
        raise RecoveryError("recorded run config is unreadable")
    return run_config


def _external_proposal(
    plan_dir: Path,
    session_id: str,
    aliases: tuple[str, ...],
    event_path: Path,
    *,
    completed_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = plans_snapshot.inspect_plan(plan_dir)
    if snapshot.next_pending is None or snapshot.next_pending.id != session_id:
        raise RecoveryError("session must be the next pending session")
    registry = plans_registry.read_repository_registry(plan_dir)
    metadata = plans_metadata.load_plan_metadata(plan_dir)
    if (
        registry is None
        or not isinstance(metadata, plans_metadata.WorkspacePlanMetadata)
        or metadata.registry_digest != registry.digest
    ):
        raise RecoveryError("an unchanged registered workspace binding is required")
    workspace = workspaces.resolve_workspace(metadata.workspace_root, registry)
    scope = plans_session_scope.parse_session_scope(
        snapshot.next_pending.path.read_text(encoding="utf-8"), registry.aliases
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
    for event in _event_records(event_path):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("name") == events.RUN_STARTED_EVENT:
            starts[payload.get("run_id")] = payload
        details = payload.get("details")
        if isinstance(details, dict) and details.get("workspace_write_guard") == guard:
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
    run_config = _recorded_run_config(event_path.parent / events.STATE_FILE_NAME)
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
        if _embedded_repositories(fp):
            # Staging an embedded repository records a gitlink with no
            # submodule entry, never the files inside it.
            blockers.append(
                f"dirty paths in {key} include an untracked embedded repository "
                "that cannot be checkpointed"
            )
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


def _embedded_repositories(fingerprint: dict[str, Any]) -> list[str]:
    return [
        entry[0]
        for entry in fingerprint.get("untracked") or ()
        if isinstance(entry, list)
        and len(entry) == 3
        and entry[1] == repository_evidence.NESTED_REPOSITORY_KIND
    ]


def _checkpoint_content(root: Path, paths: list[str]) -> list[list[Any]]:
    try:
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
    except repository_evidence._ReviewGuardUnavailable as error:
        raise RecoveryError(f"checkpoint content is unreadable: {error}") from None


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
            path
            for path in git(
                "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", head
            ).split("\0")
            if path
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
