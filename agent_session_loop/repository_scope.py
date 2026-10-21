"""Deterministic repository-footprint detection for plan documents.

A plan that names locations in more than one Git repository, or whose
completion contracts point outside the root it is bound to, cannot execute
safely in legacy single-repository mode: nothing snapshots the other
repositories, so a stray write there is invisible to the workspace guard.

This module turns plan text and contract targets into that verdict, so
``asl split``, ``asl prepare``, ``asl preflight``, and the runner all reach
the same conclusion from the same evidence instead of each re-deriving it.

Three layers, each in its own module:

* :mod:`repository_scope_targets` is pure. It finds path-shaped values in plan
  prose and contract checks and classifies each as ``hard`` or ``soft``.
* This module needs a filesystem. :func:`resolve_footprint` resolves each
  target, finds its owning Git worktree, and reports the verdict.
* :mod:`repository_scope_registry` suggests registry rows and per-session
  scopes, and :mod:`repository_scope_payload` serializes the verdict.

Only ``hard`` targets can force a refusal. ``soft`` prose is advisory and
feeds :attr:`RepositoryFootprint.ambiguous`, which tells a caller to ask a
human rather than guess an alias.

Escaping the bound root is not by itself a refusal. As in
:mod:`repository_intent`, a target that resolves outside any Git worktree is
classified separately, in :attr:`RepositoryFootprint.external`, and never
forces a registry: external artifacts are legitimately non-Git, and a registry
row can only describe a Git repository, so demanding one is unsatisfiable.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .repository_scope_targets import PathTarget
from .workspaces import canonicalize_filesystem_path, is_descendant

#: Absolute prefixes that never denote plan-owned source, so an interpreter or
#: toolchain path cannot trip the out-of-root gate. Matched against the path a
#: plan literally declares, not its real path: on macOS the temp directories
#: that hold real workspaces resolve under ``/private``.
SYSTEM_PATH_PREFIXES: tuple[str, ...] = (
    "/Applications",
    "/Library",
    "/System",
    "/bin",
    "/dev",
    "/etc",
    "/opt",
    "/proc",
    "/run",
    "/sbin",
    "/usr",
)


@dataclass(frozen=True)
class AmbiguousTarget:
    """Folder-shaped prose that exists in more than one detected repository."""

    target: PathTarget
    candidates: tuple[Path, ...]


@dataclass(frozen=True)
class RepositoryFootprint:
    """Which repositories a plan touches, judged against one bound root.

    ``out_of_root`` holds only the escaping targets a registry could describe:
    each one is owned by some Git worktree. A target that escapes the bound
    root and belongs to no Git worktree lands in ``external`` instead, matching
    :mod:`repository_intent` — it is a legitimately non-Git artifact, and no
    registry row could ever name it.
    """

    root: Path
    repo_roots: tuple[Path, ...]
    out_of_root: tuple[PathTarget, ...]
    unresolved: tuple[PathTarget, ...]
    ambiguous: tuple[AmbiguousTarget, ...] = ()
    external: tuple[PathTarget, ...] = ()

    @property
    def registry_required(self) -> bool:
        """Whether this plan may no longer stay in legacy single-repo mode."""
        return len(self.repo_roots) > 1 or bool(self.out_of_root)


def resolve_footprint(
    targets: Iterable[PathTarget],
    root: str | Path,
    *,
    plan_dir: str | Path | None = None,
    provenance_roots: Iterable[str | Path] = (),
) -> RepositoryFootprint:
    """Judge ``targets`` against ``root``, the single root the plan is bound to.

    ``plan_dir`` names the plan's own directory. ASL's generated review
    contracts verify plan artifacts that live there, so a target under it
    describes the plan, not a second repository.

    ``provenance_roots`` names the document the plan was split from and the
    directory holding it, as bounded by
    :func:`repository_intent.provenance_exempt_roots`. A plan citing where it
    came from is not a plan that reaches there.
    """
    bound_root = canonicalize_filesystem_path(root)
    plan_root = canonicalize_filesystem_path(plan_dir) if plan_dir is not None else None
    provenance = tuple(
        canonicalize_filesystem_path(entry) for entry in provenance_roots
    )
    git_root_cache: dict[Path, Path | None] = {}
    repo_roots: dict[Path, None] = {}
    out_of_root: list[PathTarget] = []
    external: list[PathTarget] = []
    unresolved: list[PathTarget] = []
    soft_targets: list[tuple[PathTarget, Path]] = []

    for target in targets:
        resolved = resolve_target(target, bound_root)
        if resolved is None:
            continue
        if plan_root is not None and is_descendant(
            resolved, plan_root, allow_equal=True
        ):
            continue
        if any(
            is_descendant(resolved, entry, allow_equal=True) for entry in provenance
        ):
            continue
        if target.confidence == "soft":
            soft_targets.append((target, resolved))
            continue
        if not resolved.exists():
            unresolved.append(target)
            continue
        git_root = nearest_git_root(resolved, git_root_cache)
        if not is_descendant(resolved, bound_root, allow_equal=True):
            if git_root is None:
                external.append(target)
            else:
                out_of_root.append(target)
        if git_root is not None:
            repo_roots[git_root] = None

    ordered_roots = tuple(sorted(repo_roots, key=str))
    ambiguous = _ambiguous_targets(soft_targets, ordered_roots)
    return RepositoryFootprint(
        root=bound_root,
        repo_roots=ordered_roots,
        out_of_root=tuple(out_of_root),
        unresolved=tuple(unresolved),
        ambiguous=ambiguous,
        external=tuple(external),
    )


def _ambiguous_targets(
    soft_targets: Sequence[tuple[PathTarget, Path]],
    repo_roots: Sequence[Path],
) -> tuple[AmbiguousTarget, ...]:
    """Report folder-shaped prose that resolves inside two or more repositories."""
    if len(repo_roots) < 2:
        return ()
    ambiguous: list[AmbiguousTarget] = []
    for target, _resolved in soft_targets:
        relative = target.value.strip("/")
        if not relative:
            continue
        candidates = tuple(
            repo_root for repo_root in repo_roots if (repo_root / relative).exists()
        )
        if len(candidates) > 1:
            ambiguous.append(AmbiguousTarget(target=target, candidates=candidates))
    return tuple(ambiguous)


def resolve_target(target: PathTarget, root: Path) -> Path | None:
    """Resolve one target, or ``None`` when it names a system location."""
    value = target.value
    if target.kind == "handoff":
        _, _, value = value.partition(":")
    if not value:
        return None
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = root / candidate
    if is_system_path(candidate):
        return None
    return canonicalize_filesystem_path(candidate)


def is_system_path(path: Path) -> bool:
    """Return whether ``path`` names an interpreter or toolchain location."""
    text = Path(os.path.abspath(path)).as_posix()
    return any(
        text == prefix or text.startswith(f"{prefix}/")
        for prefix in SYSTEM_PATH_PREFIXES
    )


def nearest_git_root(
    path: Path, cache: dict[Path, Path | None] | None = None
) -> Path | None:
    """Walk up to the nearest ``.git`` marker without invoking Git."""
    if cache is None:
        cache = {}
    start = path if path.is_dir() else path.parent
    chain: list[Path] = []
    current = start
    found: Path | None = None
    while True:
        if current in cache:
            found = cache[current]
            break
        chain.append(current)
        marker = current / ".git"
        try:
            mode = marker.lstat().st_mode
        except OSError:
            mode = None
        if mode is not None and (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            found = current
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    for entry in chain:
        cache[entry] = found
    return found
