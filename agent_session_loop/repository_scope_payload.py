"""Machine-readable footprint verdict shared by ``asl split`` and preflight."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .repository_scope import RepositoryFootprint, nearest_git_root, resolve_target
from .repository_scope_registry import (
    registry_table_lines,
    suggest_repositories,
    suggest_scopes,
    suggest_workspace_root,
)
from .repository_scope_targets import PathTarget
from .workspaces import WorkspaceError


def footprint_payload(
    targets_by_session: Mapping[str, Sequence[PathTarget]],
    footprint: RepositoryFootprint,
    *,
    registry_present: bool,
) -> dict[str, Any] | None:
    """Serialize the footprint verdict, or ``None`` when there is nothing to say.

    This is the shared machine-readable contract behind ``asl split --check``
    and ``asl preflight``, so a skill pastes a table ASL derived from disk
    instead of inventing aliases, paths, or origins of its own.
    """
    if not footprint.registry_required:
        return None
    suggested_root = suggest_workspace_root(footprint.repo_roots)
    workspace_root = suggested_root or footprint.root
    try:
        suggestions = (
            suggest_repositories(suggested_root, footprint.repo_roots)
            if suggested_root is not None
            else ()
        )
    except WorkspaceError:
        suggestions = ()
    alias_by_root = {suggestion.root: suggestion.alias for suggestion in suggestions}
    evidence = _evidence_by_root(targets_by_session, footprint.root)
    detected = [
        {
            "alias": suggestion.alias,
            "path": suggestion.path,
            "origin": suggestion.origin,
            "root": str(suggestion.root),
            "evidence": [
                target.as_dict() for target in evidence.get(suggestion.root, ())
            ],
        }
        for suggestion in suggestions
    ]
    scopes = suggest_scopes(
        targets_by_session, root=footprint.root, alias_by_root=alias_by_root
    )
    return {
        "workspace_root": str(workspace_root),
        "bound_root": str(footprint.root),
        "registry_present": registry_present,
        "registry_required": True,
        "detected": detected,
        "out_of_root": [target.as_dict() for target in footprint.out_of_root],
        "external": [target.as_dict() for target in footprint.external],
        "unattributed": [
            {
                "line": entry.target.line,
                "text": entry.target.value,
                "candidates": [
                    alias_by_root[candidate]
                    for candidate in entry.candidates
                    if candidate in alias_by_root
                ],
            }
            for entry in footprint.ambiguous
        ],
        "suggested_registry_table": (
            list(registry_table_lines(suggestions)) if suggestions else []
        ),
        "suggested_scopes": {
            session_id: {
                "reads": list(scope.reads),
                "writes": list(scope.writes),
            }
            for session_id, scope in sorted(scopes.items())
        },
    }


def _evidence_by_root(
    targets_by_session: Mapping[str, Sequence[PathTarget]], root: Path
) -> dict[Path, tuple[PathTarget, ...]]:
    git_root_cache: dict[Path, Path | None] = {}
    evidence: dict[Path, list[PathTarget]] = {}
    for targets in targets_by_session.values():
        for target in targets:
            if target.confidence != "hard":
                continue
            resolved = resolve_target(target, root)
            if resolved is None or not resolved.exists():
                continue
            git_root = nearest_git_root(resolved, git_root_cache)
            if git_root is None:
                continue
            evidence.setdefault(git_root, []).append(target)
    return {git_root: tuple(targets) for git_root, targets in evidence.items()}
