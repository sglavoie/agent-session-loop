"""Render the repository authority and scope a selected session may use."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    plans_errors,
    plans_files,
    plans_models,
    plans_session_scope,
    verification_models,
    workspaces,
)


def _prompt_plan_dir(plan_dir: str | Path) -> str:
    plan_dir_text = str(plans_files.canonicalize_path(plan_dir))
    if not plan_dir_text.endswith("/"):
        plan_dir_text += "/"
    return plan_dir_text


def _resolved_repositories_payload(
    workspace: workspaces.ResolvedWorkspace,
) -> list[dict[str, str]]:
    """Return the canonical ordered repository projection used by runner APIs."""
    return [
        {
            "alias": repository.alias,
            "path": repository.repository.path,
            "origin": repository.origin,
            "resolved_root": str(repository.root),
        }
        for repository in workspace.repositories
    ]


def _claimed_repository_roots(
    context: verification_models.VerificationContext,
) -> tuple[str, ...]:
    """Return the resolved repository roots this run is about to claim."""
    if context.workspace is None:
        return ()
    return tuple(str(repository.root) for repository in context.workspace.repositories)


def _workspace_payload_fields(
    context: verification_models.VerificationContext,
) -> dict[str, Any]:
    """Return additive workspace fields, leaving legacy payloads unchanged."""
    if context.workspace is None:
        return {}
    return {
        "workspace_root": str(context.root),
        "registry_digest": context.registry_digest,
        "repositories": _resolved_repositories_payload(context.workspace),
    }


def _session_scope(
    session: plans_models.SessionStatus,
    workspace: workspaces.ResolvedWorkspace,
) -> plans_session_scope.SessionScope:
    """Read the selected session's exact registry-validated scope."""
    try:
        text = session.path.read_text(encoding="utf-8")
    except OSError as error:
        raise plans_errors.PlanError(f"cannot read {session.path}: {error}") from error
    try:
        return plans_session_scope.parse_session_scope(text, workspace.registry.aliases)
    except (plans_session_scope.SessionScopeError, ValueError) as error:
        raise plans_errors.PlanError(
            f"invalid repository scope for session {session.id}: {error}"
        ) from error


def _workspace_prompt_lines(
    workspace: workspaces.ResolvedWorkspace,
    scope: plans_session_scope.SessionScope,
) -> list[str]:
    """Render bounded repository authority without unrelated resolved roots."""
    lines = [
        "## Repository workspace authority",
        "",
        f"- Workspace root (your process cwd): {workspace.root}",
        f"- Registry digest: {workspace.registry.digest}",
        f"- Expected read aliases: {', '.join(scope.reads)}",
        (
            "- Writable aliases: "
            + (", ".join(scope.writes) if scope.writes else "(none)")
        ),
        "",
        "The ordered registry is:",
        "",
    ]
    for repository in workspace.registry.repositories:
        lines.append(
            f"- {repository.alias}: path={repository.path}; origin={repository.origin}"
        )
    lines.extend(
        [
            "",
            "Resolved roots are provided only for repositories this session may read:",
            "",
        ]
    )
    for alias in scope.reads:
        resolved = workspace.get(alias)
        lines.append(f"- {alias}: {resolved.root}")
    lines.extend(
        [
            "",
            "- Read only repositories named by `Expected read aliases`.",
            "- Modify files only in repositories named by `Writable aliases`.",
            "- Verification tools can write caches and generated artifacts even "
            "when they only inspect source. Keep those writes out of read-only "
            "repositories, including untracked and ignored files. For Ruff, use "
            "`--no-cache` when checking a read-only dependency; configure other "
            "tools to disable writes or use an already-authorized scratch location. "
            "Do not widen write scope merely to accommodate tool caches.",
            "- For repository files in handoffs and cross-repository references "
            "use qualified paths "
            "in canonical `Alias:path/to/file` form; never use an "
            "absolute path or an unqualified cross-repository path.",
            "- `Files modified` records repository changes only, using the "
            "session's write aliases. Record plan-local artifact provenance "
            "(including `final-review.md` and `final-review.json`) in `Notes`. "
            "Use `(none)` when no repository files changed, even if plan-local "
            "artifacts were written; retain all repository entries for mixed changes.",
        ]
    )
    return lines
