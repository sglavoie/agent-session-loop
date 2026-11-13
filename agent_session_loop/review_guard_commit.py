"""Clean-worktree gates the run takes before auto-commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import repository_evidence, review_guard_git, verification_models
from .run_errors import RunError


class CommitPreflightRefusal(RunError):
    """The commit gate refused one named registered checkout.

    The message already names the repository, but only as prose. Carrying the
    alias and the root it resolved to lets the pause name the checkout to
    inspect instead of every checkout the workspace registers (ASI-164).
    """

    def __init__(self, message: str, *, alias: str, resolved_root: Path) -> None:
        super().__init__(message)
        self.alias = alias
        self.resolved_root = resolved_root


def commit_preflight_pause_details(
    context: verification_models.VerificationContext,
    refusal: BaseException | None = None,
) -> dict[str, Any]:
    """Name actual commit checkouts, and the refused one when the gate knows it.

    ``commit_repositories`` lists every checkout the run may commit, without
    claiming which one is dirty. ``refused_repository`` is added only when the
    refusal itself named a checkout, so the remediation can point at exactly
    that repository rather than at the workspace parent or at all of them.
    """
    details: dict[str, Any] = {
        "commit_repositories": (
            [{"alias": ".", "resolved_root": str(context.root)}]
            if context.workspace is None
            else [
                {"alias": repository.alias, "resolved_root": str(repository.root)}
                for repository in context.workspace.repositories
            ]
        )
    }
    if isinstance(refusal, CommitPreflightRefusal):
        details["refused_repository"] = {
            "alias": refusal.alias,
            "resolved_root": str(refusal.resolved_root),
        }
    return details


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
            raise CommitPreflightRefusal(
                f"could not inspect repository {repository.alias}: {error}",
                alias=repository.alias,
                resolved_root=repository.root,
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
                    raise CommitPreflightRefusal(
                        f"needed repository {repository.alias} linked tree {linked}: "
                        f"{error}",
                        alias=repository.alias,
                        resolved_root=linked,
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
            raise CommitPreflightRefusal(
                "git status --porcelain must be empty before auto-commit "
                f"in repository {repository.alias}",
                alias=repository.alias,
                resolved_root=repository.root,
            )
    return {"repositories": repositories}
