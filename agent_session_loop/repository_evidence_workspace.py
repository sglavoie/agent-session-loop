"""Discover workspace repositories and capture deterministic snapshot vectors."""

from __future__ import annotations

import os
import stat
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    repository_evidence_capture as evidence_capture,
)
from . import (
    repository_evidence_git as evidence_git,
)
from . import (
    repository_evidence_transitions as evidence_transitions,
)

if TYPE_CHECKING:
    from . import repository_evidence


def _inventory_candidates(
    workspace_root: Path,
) -> tuple[list[Path], list[OSError], int]:
    candidates: list[Path] = []
    errors: list[OSError] = []
    skipped_hidden_subtrees = 0

    def record_error(error: OSError) -> None:
        errors.append(error)

    for current_text, directory_names, file_names in os.walk(
        workspace_root, topdown=True, onerror=record_error, followlinks=False
    ):
        current = Path(current_text)
        marker = current / ".git"
        marker_present = False
        if ".git" in directory_names or ".git" in file_names:
            try:
                marker_mode = marker.lstat().st_mode
            except OSError as error:
                errors.append(error)
            else:
                marker_present = stat.S_ISDIR(marker_mode) or stat.S_ISREG(marker_mode)
        descendable: list[str] = []
        for name in directory_names:
            if name == ".git":
                # This marker belongs to ``current`` and was considered above;
                # never recurse into Git metadata or count it as an excluded
                # workspace subtree.
                continue
            if name.startswith("."):
                skipped_hidden_subtrees += 1
                continue
            if (current / name).is_symlink():
                continue
            descendable.append(name)
        directory_names[:] = sorted(descendable)
        if marker_present:
            # A .git file is the linked-worktree form; a directory is an
            # ordinary worktree. Its internals were pruned above.
            candidates.append(current)
    return candidates, errors, skipped_hidden_subtrees


def discover_git_worktree_inventory(
    workspace_root: str | Path,
    *,
    max_repositories: int = 1024,
) -> evidence_git.GitWorktreeInventory:
    """Recursively inventory worktree roots and skipped hidden subtrees.

    The supplied workspace root is always inspected, even if it is itself
    hidden. Descendant directories whose names begin with ``.`` are pruned
    before they can yield a Git candidate; Git metadata directories are pruned
    separately and do not inflate ``skipped_hidden_subtrees``.
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
    if not root.is_dir():
        raise evidence_git.RepositoryEvidenceError(
            f"workspace root does not exist or is not a directory: {root}"
        )
    if max_repositories < 1:
        raise ValueError("max_repositories must be positive")
    candidates, errors, skipped_hidden_subtrees = _inventory_candidates(root)
    if errors:
        raise evidence_git.RepositoryEvidenceError(
            f"workspace inventory unreadable: {errors[0]}"
        )
    if len(candidates) > max_repositories:
        raise evidence_git.RepositoryEvidenceError(
            "workspace repository inventory exceeds "
            f"the {max_repositories}-repository bound"
        )

    repositories: list[Path] = []
    for candidate in sorted(
        candidates,
        key=lambda path: evidence_transitions._workspace_relative(root, path),
    ):
        relative = evidence_transitions._workspace_relative(root, candidate)
        argv = ["rev-parse", "--show-toplevel"]
        try:
            result = evidence_git._run_git(candidate, argv)
        except Exception as error:
            raise evidence_git.RepositoryEvidenceError(
                f"repository {relative} could not be inspected: {error}"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise evidence_git.RepositoryEvidenceError(
                f"repository {relative} "
                f"could not be inspected: {detail or 'Git probe failed'}"
            )
        reported = Path(os.path.realpath(os.path.abspath(result.stdout.strip())))
        if reported != candidate:
            raise evidence_git.RepositoryEvidenceError(
                f"repository marker at {relative} "
                "does not identify an exact Git worktree root"
            )
        repositories.append(candidate)
    return evidence_git.GitWorktreeInventory(
        repositories=tuple(repositories),
        skipped_hidden_subtrees=skipped_hidden_subtrees,
    )


def discover_git_worktrees(
    workspace_root: str | Path,
    *,
    max_repositories: int = 1024,
) -> tuple[Path, ...]:
    """Return exact Git worktree roots without diagnostic inventory details."""
    return discover_git_worktree_inventory(
        workspace_root,
        max_repositories=max_repositories,
    ).repositories


def capture_workspace_snapshot(
    workspace_root: str | Path,
    plan_dir: str | Path,
    *,
    exclude: Mapping[str, tuple[str, ...]] | None = None,
    default_exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
    max_repositories: int = 1024,
    _run_git_fn: evidence_git.GitRunner | None = None,
    _run_git_bytes_fn: evidence_git.GitBytesRunner | None = None,
) -> repository_evidence.WorkspaceSnapshot:
    """Capture an ordered root-to-evidence vector for discovered worktrees.

    ``exclude`` overrides exclusions for named workspace-relative roots, while
    ``default_exclude`` applies to every other discovered repository, so a
    caller can state one content policy for roots discovery has yet to find.
    Guards capture the plan's registry through ``capture_registered_snapshot``
    instead; this walking capture describes a whole workspace tree.
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
    git_token = evidence_git._GIT_RUNNER.set(_run_git_fn)
    bytes_token = evidence_git._GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        repositories = discover_git_worktree_inventory(
            root,
            max_repositories=max_repositories,
        ).repositories
        snapshot: repository_evidence.WorkspaceSnapshot = OrderedDict()
        per_repository_excludes = exclude or {}
        for repository in repositories:
            relative = evidence_transitions._workspace_relative(root, repository)
            fingerprint = evidence_capture._capture_review_repo_fingerprint(
                repository,
                plan_dir,
                exclude=tuple(per_repository_excludes.get(relative, default_exclude)),
                include_ignored=include_ignored,
            )
            if not fingerprint.get("git"):
                # Discovery proved this was a worktree moments ago. Losing that
                # fact during capture is a race, not evidence that the guard no
                # longer applies.
                fingerprint = {
                    "git": True,
                    "unavailable": {
                        "reason": "repository-disappeared",
                        "argv": ["rev-parse", "--show-toplevel"],
                    },
                }
            snapshot[relative] = fingerprint
        return snapshot
    finally:
        evidence_git._GIT_BYTES_RUNNER.reset(bytes_token)
        evidence_git._GIT_RUNNER.reset(git_token)
