# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: B008, F401, F403, F405, F821, F822, I001
"""Write-guard evidence projection for registered workspaces."""

from __future__ import annotations

from . import workspace_evidence_bases as _bases

globals().update(
    {name: value for name, value in vars(_bases).items() if not name.startswith("__")}
)
del _bases

__all__ = [
    "_legacy_implementation_write_guard_evidence",
    "_workspace_guard_violation_summary",
    "_workspace_repair_required",
    "_workspace_write_guard_evidence",
]


def _workspace_write_guard_evidence(
    *,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans_session_scope.SessionScope,
    comparison: Mapping[str, Any],
    exclude: tuple[str, ...],
    include_ignored: bool,
    external_writes: Mapping[str, Any] | None = None,
    external_change_aliases: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Classify workspace-vector changes against one session's write scope.

    ``external_writes`` carries the per-path verdict from
    :func:`external_write_evidence._external_write_evidence`. Its unclassified rows join
    ``unavailable`` rather than ``violations`` for the same reason an
    unobserved repository does: the guard has no observation to classify.

    ``external_change_aliases`` are the aliases the operator declared someone
    else may change during the run (``--allow-external-changes``). A content
    change there that the session may not write is recorded under
    ``external_changes`` with an unknown writer instead of convicting the turn;
    adding or removing a checkout still does.
    """
    registered = {
        repository.repository.path: repository.alias
        for repository in workspace.repositories
    }
    changed_payload: dict[str, Any] = {}
    violations: list[dict[str, Any]] = []
    authorized: list[str] = []
    remote_ref_drift: dict[str, list[Any]] = {}
    external_changes: dict[str, list[str]] = {}

    raw_changed = comparison.get("changed")
    if isinstance(raw_changed, Mapping):
        for repository in sorted(raw_changed):
            raw_changes = raw_changed[repository]
            changes = dict(raw_changes) if isinstance(raw_changes, Mapping) else {}
            linked = repository_evidence.split_linked_worktree_key(repository)
            if linked is not None:
                changes["worktree_of"] = linked[0]
            # A linked worktree is another checkout of the repository that owns
            # it, so it answers to that repository's alias rather than to none.
            alias = registered.get(repository if linked is None else linked[0])
            qualifier = alias or repository
            qualified_paths = [
                f"{qualifier}:{path.split(':', 1)[1]}"
                if isinstance(path, str) and ":" in path
                else f"{qualifier}:{path}"
                for path in changes.get("qualified_paths", ())
            ]
            changes["qualified_paths"] = qualified_paths
            record = {
                "repository": repository,
                "alias": alias,
                **changes,
            }
            changed_payload[repository] = record
            if changes.get("remote_ref_drift_only") is True:
                # Only refs/remotes/* moved (ASI-150): a background sync or
                # fetch, or the session pushing a commit it already made. That
                # is recorded drift with an unknown writer, not a write, so it
                # needs no write authority and no durable-scope repair.
                remote_ref_drift[repository] = list(
                    changes.get("remote_ref_drift") or ()
                )
                continue
            if alias is not None and alias in scope.writes:
                authorized.append(alias)
                continue
            if alias is not None and alias in external_change_aliases:
                external_changes[repository] = list(qualified_paths)
                continue
            if alias in scope.reads:
                kind = "read-only-repository"
            else:
                kind = "outside-read-scope"
            violations.append(
                {
                    "kind": kind,
                    "repository": repository,
                    "alias": alias,
                    "qualified_paths": qualified_paths,
                }
            )

    for topology_kind in ("added", "removed"):
        roots = comparison.get(topology_kind)
        if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)):
            continue
        for repository in sorted(str(root) for root in roots):
            linked = repository_evidence.split_linked_worktree_key(repository)
            alias = registered.get(repository if linked is None else linked[0])
            # Adding or removing a checkout of a repository the session may
            # write is work it was authorized to do; the topology change is
            # still recorded, it just is not a breach on its own.
            if linked is not None and alias in scope.writes:
                continue
            violations.append(
                {
                    "kind": f"repository-{topology_kind}",
                    "repository": repository,
                    "alias": alias,
                    "qualified_paths": [],
                }
            )

    unavailable = comparison.get("unavailable")
    unavailable_payload: dict[str, Any] = (
        external_write_evidence._external_write_unavailable(external_writes)
        if external_writes is not None
        else {}
    )
    if isinstance(unavailable, Mapping):
        # A repository the comparison itself could not read has the more
        # specific reason, so it wins a key collision with a path verdict.
        unavailable_payload.update(
            {str(key): value for key, value in unavailable.items()}
        )
    authorized_set = set(authorized)
    return {
        "passed": not violations and not unavailable_payload,
        "external_writes": dict(external_writes) if external_writes is not None else {},
        "read_aliases": list(scope.reads),
        "write_aliases": list(scope.writes),
        "registered_repositories": [
            {
                "alias": repository.alias,
                "path": repository.repository.path,
            }
            for repository in workspace.repositories
        ],
        "changed": changed_payload,
        "authorized_aliases": [
            repository.alias
            for repository in workspace.repositories
            if repository.alias in authorized_set
        ],
        "added": list(comparison.get("added") or ()),
        "removed": list(comparison.get("removed") or ()),
        "unavailable": unavailable_payload,
        "violations": violations,
        "excluded": list(exclude),
        "include_ignored": include_ignored,
        **({"remote_ref_drift": remote_ref_drift} if remote_ref_drift else {}),
        **({"external_changes": external_changes} if external_changes else {}),
    }


def _legacy_implementation_write_guard_evidence(
    *,
    cwd: Path,
    comparison: Mapping[str, Any],
    exclude: tuple[str, ...],
    include_ignored: bool,
    unobserved: Mapping[str, Mapping[str, Any]] = MappingProxyType({}),
    external_writes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a legacy single-repository comparison into the guard schema.

    ``unobserved`` carries the repositories this turn reached that the
    single-repository snapshot never fingerprinted. They join ``unavailable``
    rather than ``violations`` because the guard has no observation to
    classify: it did not see the repository at all, which is the fail-closed
    lane's exact meaning. The comparison's own entries win a key collision --
    a repository that failed to fingerprint has a more specific reason than
    one that was never fingerprinted.

    ``external_writes`` carries the same judgement one level down, over paths
    rather than repositories, and contributes its unclassified rows to the same
    lane.
    """
    raw_changed = comparison.get("changed")
    changed = (
        {str(key): dict(value) for key, value in raw_changed.items()}
        if isinstance(raw_changed, Mapping)
        else {}
    )
    unavailable = comparison.get("unavailable")
    unavailable_payload: dict[str, Any] = {
        str(key): dict(value) for key, value in unobserved.items()
    }
    if external_writes is not None:
        unavailable_payload.update(
            external_write_evidence._external_write_unavailable(external_writes)
        )
    if isinstance(unavailable, Mapping):
        unavailable_payload.update(
            {str(key): value for key, value in unavailable.items()}
        )
    added = list(comparison.get("added") or ())
    removed = list(comparison.get("removed") or ())
    violations = [
        {
            "kind": f"repository-{kind}",
            "repository": str(repository),
            "alias": "legacy",
            "qualified_paths": [],
        }
        for kind, repositories in (("added", added), ("removed", removed))
        for repository in repositories
    ]
    return {
        "passed": not unavailable_payload and not violations,
        "scope_mode": "legacy-single-repository",
        "external_writes": dict(external_writes) if external_writes is not None else {},
        "repository_root": str(cwd),
        "read_aliases": ["legacy"],
        "write_aliases": ["legacy"],
        "registered_repositories": [],
        "changed": changed,
        "authorized_aliases": ["legacy"] if changed else [],
        "added": added,
        "removed": removed,
        "unavailable": unavailable_payload,
        "violations": violations,
        "excluded": list(exclude),
        "include_ignored": include_ignored,
    }


def _workspace_guard_violation_summary(evidence: Mapping[str, Any]) -> str:
    named: list[str] = []
    violations = evidence.get("violations")
    if isinstance(violations, Sequence):
        for violation in violations:
            if not isinstance(violation, Mapping):
                continue
            qualified = violation.get("qualified_paths")
            if isinstance(qualified, Sequence) and not isinstance(
                qualified, (str, bytes)
            ):
                named.extend(str(path) for path in qualified)
            if not qualified:
                alias = violation.get("alias")
                repository = violation.get("repository")
                named.append(str(alias or repository))
    named = list(dict.fromkeys(named))
    if not named:
        return ""
    suffix = f": {', '.join(named[:3])}"
    if len(named) > 3:
        suffix += f" (+{len(named) - 3} more)"
    return suffix


def _workspace_repair_required(
    evidence: Mapping[str, Any], reason: str
) -> dict[str, bool]:
    """Derive the durable repair a failed workspace guard asks for.

    A content, local-ref, or topology violation is what a durable scope edit,
    fresh expansion, and fresh preflight answer. Remote-tracking ref drift is
    never a violation (ASI-150), so a verdict carrying no violation asks for
    none of them -- except an unavailable verdict, which keeps the full
    requirement because unobserved evidence cannot prove the scope sound.
    """
    required = bool(evidence.get("violations")) or (
        reason == events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
    )
    return {
        "durable_scope": required,
        "fresh_expansion": required,
        "fresh_preflight": required,
    }
