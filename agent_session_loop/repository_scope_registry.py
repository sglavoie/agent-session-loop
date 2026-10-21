"""Registry and per-session scope suggestions derived from detected repositories.

Everything here is derived from disk: aliases from directory names, paths from
the workspace-relative location, and origins from each repository's own
``origin`` remote. ASL invents nothing a human has not been asked to confirm.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .repository_scope import nearest_git_root, resolve_target
from .repository_scope_targets import PathTarget
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
    canonicalize_declared_origin,
    canonicalize_filesystem_path,
    is_descendant,
    normalize_repository_path,
    validate_repository_alias,
)

#: Rendered in a suggested registry row when a repository declares no origin
#: remote. Deliberately not URL-shaped so the row cannot be pasted unedited.
UNKNOWN_ORIGIN_PLACEHOLDER = "<add-origin-remote>"


@dataclass(frozen=True)
class SessionScopeSuggestion:
    """Read and write aliases derived from one session's own path targets."""

    reads: tuple[str, ...]
    writes: tuple[str, ...]


@dataclass(frozen=True)
class RepositorySuggestion:
    """One proposed registry row, with ``origin`` unset when unobservable."""

    alias: str
    path: str
    origin: str | None
    root: Path


def suggest_workspace_root(repo_roots: Sequence[Path]) -> Path | None:
    """Return the shallowest directory containing every detected repository.

    ``None`` when the repositories have no usable common ancestor, or when one
    contains another: a registry forbids overlapping roots, so that topology
    needs a human decision rather than a generated table.
    """
    roots = tuple(
        dict.fromkeys(canonicalize_filesystem_path(root) for root in repo_roots)
    )
    if not roots:
        return None
    if len(roots) == 1:
        parent = roots[0].parent
        return parent if parent != roots[0] else None
    common = Path(os.path.commonpath([str(root) for root in roots]))
    if common in roots or common == common.parent:
        return None
    return common


def suggest_repositories(
    workspace_root: str | Path, repo_roots: Sequence[Path]
) -> tuple[RepositorySuggestion, ...]:
    """Describe each detected root as a registry row, inventing nothing.

    Aliases come from directory names, paths from the workspace-relative
    location, and origins from each repository's own ``origin`` remote. A
    repository with no usable origin keeps its row with ``origin=None``: the
    alias and path are still derived from disk, and only the one field ASL
    cannot observe is left for a human to fill in.
    """
    root = canonicalize_filesystem_path(workspace_root)
    suggestions: list[RepositorySuggestion] = []
    used: set[str] = set()
    for repo_root in sorted(dict.fromkeys(repo_roots), key=str):
        canonical = canonicalize_filesystem_path(repo_root)
        if not is_descendant(canonical, root):
            raise WorkspaceError(
                f"repository {canonical} is not inside the workspace root {root}"
            )
        suggestions.append(
            RepositorySuggestion(
                alias=_unique_alias(canonical, used),
                path=normalize_repository_path(canonical.relative_to(root).as_posix()),
                origin=_origin_of(canonical),
                root=canonical,
            )
        )
    return tuple(suggestions)


def suggest_registry(
    workspace_root: str | Path, repo_roots: Sequence[Path]
) -> RepositoryRegistry:
    """Build a validated registry from detected roots.

    Raises :class:`WorkspaceError` when a repository has no usable origin
    remote, because a registry entry without one cannot be validated.
    """
    repositories: list[Repository] = []
    for suggestion in suggest_repositories(workspace_root, repo_roots):
        if suggestion.origin is None:
            raise WorkspaceError(
                f"repository {suggestion.root} has no usable origin remote"
            )
        repositories.append(
            Repository(
                alias=suggestion.alias,
                path=suggestion.path,
                origin=suggestion.origin,
            )
        )
    return RepositoryRegistry(tuple(repositories))


def _unique_alias(repo_root: Path, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "_", repo_root.name).lstrip("_-")
    if not base or not base[0].isalpha():
        base = f"repo_{base}" if base else "repo"
    alias = validate_repository_alias(base)
    suffix = 2
    while alias in used:
        alias = f"{base}_{suffix}"
        suffix += 1
    used.add(alias)
    return alias


def _origin_of(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    try:
        return canonicalize_declared_origin(completed.stdout.strip())
    except WorkspaceError:
        return None


def registry_table_lines(
    registry: Iterable[Repository | RepositorySuggestion],
) -> tuple[str, ...]:
    """Render an ordered registry as Markdown table lines.

    A suggested row whose origin could not be observed renders
    :data:`UNKNOWN_ORIGIN_PLACEHOLDER`, which is visibly not a URL so the table
    cannot be pasted without the human supplying the missing remote.
    """
    return (
        "| Alias | Path | Origin |",
        "| --- | --- | --- |",
        *(
            f"| {repository.alias} | {repository.path} | "
            f"{repository.origin or UNKNOWN_ORIGIN_PLACEHOLDER} |"
            for repository in registry
        ),
    )


def suggest_scopes(
    targets_by_session: Mapping[str, Sequence[PathTarget]],
    *,
    root: str | Path,
    alias_by_root: Mapping[Path, str],
) -> dict[str, SessionScopeSuggestion]:
    """Derive per-session read and write aliases from that session's targets.

    Reads cover every repository the session mentions. Writes cover only the
    repositories its completion contract targets, because a contract target is
    a location the session must produce. Under-declaring a write fails closed
    at run time, which is the safe direction to err in.
    """
    bound_root = canonicalize_filesystem_path(root)
    git_root_cache: dict[Path, Path | None] = {}
    suggestions: dict[str, SessionScopeSuggestion] = {}
    for session_id, targets in targets_by_session.items():
        reads: dict[str, None] = {}
        writes: dict[str, None] = {}
        for target in targets:
            if target.confidence != "hard":
                continue
            resolved = resolve_target(target, bound_root)
            if resolved is None or not resolved.exists():
                continue
            git_root = nearest_git_root(resolved, git_root_cache)
            alias = alias_by_root.get(git_root) if git_root is not None else None
            if alias is None:
                continue
            reads[alias] = None
            if target.kind in {"contract-path", "contract-cwd"}:
                writes[alias] = None
        suggestions[session_id] = SessionScopeSuggestion(
            reads=tuple(reads), writes=tuple(writes)
        )
    return suggestions
