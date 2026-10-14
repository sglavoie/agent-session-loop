"""Repository registry checks and the per-plan workspace context."""

from __future__ import annotations

from pathlib import Path

from . import (
    repository_evidence,
    workspaces,
)
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_registry import read_repository_registry
from .preflight_models import (
    PreflightCategory,
    PreflightFinding,
    RepositorySummary,
    _finding,
)
from .verification_models import VerificationContext


def _repository_summaries(
    registry: workspaces.RepositoryRegistry | None,
    context: VerificationContext | None = None,
) -> tuple[RepositorySummary, ...]:
    if registry is None:
        return ()
    resolved_roots: dict[str, str] = {}
    if context is not None and context.workspace is not None:
        resolved_roots = {
            repository.alias: str(repository.root)
            for repository in context.workspace.repositories
        }
    return tuple(
        RepositorySummary(
            alias=repository.alias,
            path=repository.path,
            origin=repository.origin,
            resolved_root=resolved_roots.get(repository.alias),
        )
        for repository in registry
    )


def _check_repository_registry(
    canonical_dir: Path,
) -> tuple[
    tuple[PreflightFinding, ...],
    workspaces.RepositoryRegistry | None,
]:
    manifest_path = canonical_dir / "manifest.md"
    if not manifest_path.is_file():
        return (), None
    try:
        registry = read_repository_registry(canonical_dir)
    except PlanError as error:
        message = str(error)
        category: PreflightCategory = (
            "repository-overlap"
            if "overlap" in message or "paths must be unique" in message
            else "repository-registry"
        )
        return (
            _finding(
                "red",
                category,
                message,
                path=str(manifest_path),
            ),
        ), None
    return (), registry


def _workspace_error_category(message: str) -> PreflightCategory:
    if "origin" in message:
        return "repository-origin"
    if "overlap" in message:
        return "repository-overlap"
    if "workspace root" in message or "outside the workspace" in message:
        return "workspace-root"
    return "repository-root"


def _resolve_preflight_context(
    *,
    plan_dir: Path,
    cwd: str | Path | None,
    registry: workspaces.RepositoryRegistry | None,
) -> tuple[
    VerificationContext | None,
    tuple[PreflightFinding, ...],
    tuple[RepositorySummary, ...],
]:
    summaries = _repository_summaries(registry)
    if cwd is None:
        return None, (), summaries

    canonical_cwd = canonicalize_path(cwd)
    if registry is None:
        if not canonical_cwd.is_dir():
            return None, (), summaries
        return (
            VerificationContext.legacy(canonical_cwd, plan_dir=plan_dir),
            (),
            summaries,
        )

    if not canonical_cwd.exists():
        return (
            None,
            (
                _finding(
                    "red",
                    "workspace-root",
                    f"workspace root does not exist: {canonical_cwd}",
                    path=str(canonical_cwd),
                ),
            ),
            summaries,
        )
    if not canonical_cwd.is_dir():
        return (
            None,
            (
                _finding(
                    "red",
                    "workspace-root",
                    f"workspace root is not a directory: {canonical_cwd}",
                    path=str(canonical_cwd),
                ),
            ),
            summaries,
        )

    try:
        workspace = workspaces.resolve_workspace(canonical_cwd, registry)
    except workspaces.WorkspaceError as error:
        message = str(error)
        return (
            None,
            (
                _finding(
                    "red",
                    _workspace_error_category(message),
                    message,
                    path=str(canonical_cwd),
                ),
            ),
            summaries,
        )

    context = VerificationContext.resolved_workspace(workspace)
    summaries = _repository_summaries(registry, context)
    inventory_findings: list[PreflightFinding] = []
    registered_by_path = {
        repository.repository.path: repository for repository in workspace.repositories
    }
    for repository in registered_by_path.values():
        fingerprint = repository_evidence.capture_repository_fingerprint(
            repository.root,
            plan_dir,
        )
        unavailable = repository_evidence.repository_evidence_unavailable(fingerprint)
        if unavailable is not None or not fingerprint.get("git"):
            inventory_findings.append(
                _finding(
                    "red",
                    "repository-root",
                    "repository evidence is unavailable for registered "
                    f"repository {repository.alias}",
                    path=str(repository.root),
                    detail=str(unavailable or {"reason": "repository-unavailable"}),
                )
            )

    return context, tuple(inventory_findings), summaries
