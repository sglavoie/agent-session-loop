"""Clean-worktree gates the run takes before auto-commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import repository_evidence, review_guard_git, verification_models
from .run_errors import RunError


def commit_preflight_pause_details(
    context: verification_models.VerificationContext,
) -> dict[str, Any]:
    """Name actual commit checkouts without claiming which one is dirty."""
    return {
        "commit_repositories": (
            [{"alias": ".", "resolved_root": str(context.root)}]
            if context.workspace is None
            else [
                {"alias": repository.alias, "resolved_root": str(repository.root)}
                for repository in context.workspace.repositories
            ]
        )
    }


def _validate_commit_preflight(
    cwd: Path,
    *,
    verification_context: verification_models.VerificationContext | None = None,
    needed_aliases: frozenset[str] | None = None,
    clean_aliases: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Refuse an auto-commit run whose committed trees are not clean.

    ``needed_aliases`` selects the registered repositories to inspect and
    ``clean_aliases`` the subset that must be clean; ``None`` requires every
    inspected repository clean. A needed alias outside ``clean_aliases`` is one
    the session only reads: it must still be an inspectable worktree, but its
    dirt cannot enter the session's commit, so it is recorded and not refused.
    """
    context = verification_context or verification_models.VerificationContext.legacy(
        cwd
    )
    if context.workspace is None:
        review_guard_git._validate_git_worktree(cwd)
        review_guard_git._validate_clean_git_status(cwd)
        return {}

    repositories: list[dict[str, Any]] = []
    for repository in context.workspace.repositories:
        if needed_aliases is not None and repository.alias not in needed_aliases:
            continue
        review_guard_git._validate_git_worktree(repository.root)
        try:
            clean = review_guard_git._git_status_is_clean(repository.root)
        except RunError as error:
            raise RunError(
                f"could not inspect repository {repository.alias}: {error}"
            ) from error
        clean_required = clean_aliases is None or repository.alias in clean_aliases
        if needed_aliases is not None and clean_required:
            try:
                linked_roots = repository_evidence.linked_worktrees(repository.root)
            except repository_evidence.RepositoryEvidenceError as error:
                raise RunError(str(error)) from error
            registered_roots = {r.root for r in context.workspace.repositories}
            for linked in linked_roots:
                if linked in registered_roots:
                    continue
                try:
                    review_guard_git._validate_clean_git_status(linked)
                except RunError as error:
                    raise RunError(
                        f"needed repository {repository.alias} linked tree {linked}: "
                        f"{error}"
                    ) from error
        repositories.append(
            {
                "alias": repository.alias,
                "path": repository.repository.path,
                "resolved_root": str(repository.root),
                "clean": clean,
                "clean_required": clean_required,
            }
        )
        if not clean and clean_required:
            raise RunError(
                "git status --porcelain must be empty before auto-commit "
                f"in repository {repository.alias}"
            )
    return {"repositories": repositories}
