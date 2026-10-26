# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F821
"""Workspace-scope repair evidence and generated-child scope updates."""

from __future__ import annotations

from . import durability
from . import workspace_evidence_bases as _bases
from . import workspace_evidence_guard as _guard

globals().update(
    {name: value for name, value in vars(_bases).items() if not name.startswith("__")}
)
globals().update(
    {name: value for name, value in vars(_guard).items() if not name.startswith("__")}
)
del _bases, _guard

__all__ = [
    "_latest_workspace_scope_repair_requirement",
    "_regenerated_repair_child_write_scope",
    "_session_expansion_evidence",
    "_workspace_scope_repair_comparison",
    "_workspace_scope_repair_is_fresh",
    "_workspace_scope_repair_restored",
    "_workspace_scope_repair_still_unauthorized",
]


def _session_expansion_evidence(plan_dir: Path, session_id: str) -> dict[str, Any]:
    path = plan_dir / plans_session_ids.session_id_to_expansion_filename(session_id)
    try:
        stat_result = path.stat()
        content = path.read_bytes()
    except FileNotFoundError:
        return {"path": str(path), "present": False}
    except OSError as error:
        return {
            "path": str(path),
            "present": True,
            "unavailable": str(error),
        }
    return {
        "path": str(path),
        "present": True,
        "mtime_ns": stat_result.st_mtime_ns,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _latest_workspace_scope_repair_requirement(
    event_path: Path, session_id: str
) -> dict[str, Any] | None:
    """Return the latest uncleared retained-mutation record for a session."""
    try:
        # Bytes, decoded line by line below: a stray non-UTF-8 byte then
        # spoils only its own line, the way a torn line does, not the history.
        lines = event_path.read_bytes().splitlines()
    except FileNotFoundError:
        return None
    except OSError:
        # Event history is operational evidence. If it cannot be read, the
        # ordinary readiness and pre-turn evidence gates still apply.
        return None
    accepted_guard_hashes: set[str] = set()
    for line in reversed(lines):
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError, RecursionError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        details = payload.get("details")
        if not isinstance(details, dict):
            continue
        guard = details.get("workspace_write_guard")
        guard_session_id = guard.get("session_id") if isinstance(guard, dict) else None
        detail_session_id = details.get("session_id")
        detail_session = details.get("session")
        if (
            guard_session_id != session_id
            and detail_session_id != session_id
            and str(detail_session) != session_id
        ):
            continue
        if record.get("name") == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT:
            return None
        if record.get("name") in (
            events.WORKSPACE_REF_UPDATES_ACCEPTED_EVENT,
            events.WORKSPACE_EXTERNAL_CHANGES_ACKNOWLEDGED_EVENT,
        ):
            acceptance = details.get("acceptance")
            if (
                isinstance(acceptance, dict)
                and acceptance.get("session_id") == session_id
                and isinstance(acceptance.get("retained_guard_sha256"), str)
                and isinstance(payload.get("reason"), str)
                and payload["reason"].strip()
            ):
                accepted_guard_hashes.add(acceptance["retained_guard_sha256"])
            continue
        if (
            record.get("name") == events.RUN_PAUSED_EVENT
            and payload.get("reason")
            == events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
        ):
            if isinstance(guard, dict):
                guard_hash = hashlib.sha256(
                    json.dumps(guard, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                return None if guard_hash in accepted_guard_hashes else dict(guard)
            return None
    return None


def _workspace_scope_repair_still_unauthorized(
    record: Mapping[str, Any],
    scope: plans_session_scope.SessionScope | None,
) -> bool:
    """Whether a retained scope violation is still outside the session's scope.

    The retained record freezes the aliases the session held when the guard
    rejected the turn, and the pause it raises tells the operator to widen the
    durable scope. Re-reading that record alone can therefore never observe
    that they did (ASI-134): the violating aliases are still listed exactly as
    they were, so a run-start refusal built on it refuses again on evidence
    that describes only the past -- and it does so while the retained changes
    are what keep the readiness gate red, which is a deadlock rather than a
    diagnosis.

    Reconciliation is judged per violation against the scope on disk now. A
    violation naming an alias the session may now write is answered here, and
    only far enough to let the run reach the turn-level repair check, which
    re-derives the whole verdict from freshly captured evidence and still
    pauses when the retained changes really are out of scope.

    Everything else keeps the refusal: a scope that could not be read at all,
    an unregistered repository, which no scope edit can authorize; a record
    whose observation was incomplete; and a record carrying no violations to
    judge at all.
    """
    if scope is None:
        return True
    if record.get("unavailable"):
        return True
    raw_violations = record.get("violations")
    if not isinstance(raw_violations, Sequence) or isinstance(
        raw_violations, (str, bytes)
    ):
        return True
    violations = [entry for entry in raw_violations if isinstance(entry, Mapping)]
    if not violations:
        return True
    writable = frozenset(scope.writes)
    return any(
        not isinstance(entry.get("alias"), str) or entry["alias"] not in writable
        for entry in violations
    )


def _workspace_scope_repair_restored(
    record: Mapping[str, Any],
    current: repository_evidence.WorkspaceSnapshot,
    *,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans_session_scope.SessionScope | None,
    exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
) -> bool:
    """Prove the rejected writes were restored without expanding authority.

    Compare complete fingerprints, including refs and index state, rather than
    cleanliness or HEAD alone. In-scope work may remain, but every repository
    the original violations name must match its pre-turn evidence exactly and
    the fresh vector must contain no new unauthorized change.
    """
    prior = record.get("snapshot_before")
    violations = record.get("violations")
    registered = [
        {"alias": repo.alias, "path": repo.repository.path}
        for repo in workspace.repositories
    ]
    if (
        scope is None
        or record.get("unavailable")
        or record.get("read_aliases") != list(scope.reads)
        or record.get("write_aliases") != list(scope.writes)
        or record.get("excluded") != list(exclude)
        or record.get("include_ignored") is not include_ignored
        or record.get("registered_repositories") != registered
        or not isinstance(prior, Mapping)
        or not prior
        or prior.keys() != current.keys()
        or not isinstance(violations, list)
        or not violations
        or any(not isinstance(value, Mapping) for value in prior.values())
    ):
        return False
    required = tuple(repo.repository.path for repo in workspace.repositories)
    if _workspace_snapshot_unavailable(
        prior, required_repository_paths=required
    ) or _workspace_snapshot_unavailable(current, required_repository_paths=required):
        return False
    aliases = {repo.alias: repo.repository.path for repo in workspace.repositories}
    for violation in violations:
        if not isinstance(violation, Mapping):
            return False
        repository = violation.get("repository")
        alias = violation.get("alias")
        if (
            not isinstance(alias, str)
            or not isinstance(repository, str)
            or aliases.get(alias) != repository
            or repository not in prior
            or prior[repository] != current[repository]
        ):
            return False
    evidence = _workspace_write_guard_evidence(
        workspace=workspace,
        scope=scope,
        comparison=repository_evidence.compare_workspace_snapshots(prior, current),
        exclude=exclude,
        include_ignored=include_ignored,
    )
    return evidence["passed"] is True


def _workspace_scope_repair_is_fresh(
    prior: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    if current.get("present") is not True or current.get("unavailable"):
        return False
    if prior.get("present") is not True:
        return True
    prior_mtime = prior.get("mtime_ns")
    current_mtime = current.get("mtime_ns")
    return (
        isinstance(prior_mtime, int)
        and isinstance(current_mtime, int)
        and current_mtime > prior_mtime
    )


def _workspace_scope_repair_comparison(
    comparison: Mapping[str, Any],
    registered_paths: frozenset[str],
) -> tuple[dict[str, Any], list[str]]:
    """Acknowledge topology moved by a repair that changed the registry.

    A retained-scope repair compares the original pre-turn snapshot with a
    newly captured baseline. Both snapshots cover only the repositories the
    plan registered when each was taken, so the sanctioned repair of
    registering the repository that was written to makes that repository
    appear from nothing, and deregistering one makes it vanish. Reading either
    as real topology change would make a registry repair permanently
    unpassable.

    A repository that is registered now cannot have been added by the harness
    and a repository that is not registered now is outside the guard's domain,
    so neither suppression can hide topology change to an in-scope repository.
    """
    repaired = dict(comparison)
    suppressed: list[str] = []
    for kind, suppress_registered in (("added", True), ("removed", False)):
        raw_roots = comparison.get(kind)
        if not isinstance(raw_roots, Sequence) or isinstance(raw_roots, (str, bytes)):
            continue
        retained: list[str] = []
        for raw_repository in raw_roots:
            repository = str(raw_repository)
            if (repository in registered_paths) == suppress_registered:
                suppressed.append(repository)
            else:
                retained.append(repository)
        repaired[kind] = retained
    return repaired, suppressed


def _regenerated_repair_child_write_scope(
    *,
    session: plans_models.SessionStatus,
    plan_dir: Path,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans_session_scope.SessionScope,
) -> dict[str, Any] | None:
    """Widen a materialized repair child's persisted write scope in place.

    Authoring already derives a child's write scope from the union its own
    findings attribute, so a materialized child can only be too narrow when the
    findings were recomputed — by a re-expansion or a reopen — after the file
    was written. Recomputing from that same union rewrites the durable file
    rather than the value in hand, so the file stays the single description of
    what the child may write and `run_prompts._session_scope` remains its only
    reader.

    Widening only, and only for ASL-generated children. A human-authored
    session's scope is its author's statement of intent and is never rewritten;
    neither is an alias the current findings no longer require. ``None`` means
    the file was left exactly as it was, which every fail-open branch here
    returns rather than persisting a scope it could not fully derive.
    """
    if not review_repair.is_generated_review_repair_session_file(session.path):
        return None
    try:
        text = session.path.read_text(encoding="utf-8")
    except OSError:
        return None
    group = review_repair.repair_group_from_session_text(text)
    if group is None:
        return None
    planning = review_repair.plan_review_repair_sessions(
        plan_dir,
        # Children hang beneath their review's top-level id, which is how
        # `review_repair` itself recovers a review's existing children.
        review_session_id=session.session_id.components[0],
    )
    grouped = dict(
        review_repair.group_findings_by_repair_group(list(planning.validated_findings))
    )
    group_findings = grouped.get(group)
    if not group_findings:
        # A group the current artifact no longer carries has no derivable
        # scope; leaving the child untouched keeps the artifact's own
        # diagnosis rather than replacing it with a narrower one.
        return None
    try:
        required = review_repair.required_write_repositories(
            group,
            group_findings,
            repository_registry=workspace.registry,
        )
    except review_repair.ReviewRepairError:
        # Findings this malformed fail closed where they are validated.
        return None
    added = tuple(alias for alias in required if alias not in scope.writes)
    if not added:
        return None
    aliases = workspace.registry.aliases
    reads = frozenset(scope.reads) | frozenset(required)
    writes = frozenset(scope.writes) | frozenset(required)
    try:
        widened = plans_session_scope.SessionScope(
            reads=tuple(alias for alias in aliases if alias in reads),
            # Writes must stay a subset of reads, so a child whose reads were
            # narrowed by hand widens both rather than persisting an
            # unparseable scope.
            writes=tuple(alias for alias in aliases if alias in writes),
        ).validate_registry(aliases)
        rewritten = plans_session_scope.replaced_session_scope_text(text, widened)
        plans_session_scope.parse_session_scope(rewritten, aliases)
    except plans_errors.PlanError, ValueError:
        return None
    try:
        durability.replace_text(session.path, rewritten)
    except OSError:
        # A child the runner cannot rewrite keeps its narrow durable scope, and
        # the caller's revalidation is what turns that into a refusal to launch
        # rather than a crash on the way to one.
        return None
    return {
        "session_id": session.id,
        "repair_group": group,
        "durable_write_repositories": list(scope.writes),
        "required_write_repositories": list(required),
        "added_write_repositories": list(added),
        "write_repositories": list(widened.writes),
        "read_repositories": list(widened.reads),
    }
