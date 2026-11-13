"""Commit preflight for one generated repair child."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    plans_session_scope,
    review_guard_git,
    review_guard_provenance,
    run_selection,
    verification_models,
)
from .review_guard_retained import (
    RepairCommitPreflightError,
    WorkspaceEvidenceProbe,
    _retained_turn_changed_paths,
    _retained_turn_content_drift,
    _RetainedContentEvidence,
)
from .run_errors import RunError


def _repair_commit_preflight(
    cwd: Path,
    *,
    verification_context: verification_models.VerificationContext,
    session_scope: plans_session_scope.SessionScope | None,
    workspace_evidence: WorkspaceEvidenceProbe,
    plan_dir: Path | None = None,
    retained_scope_repair: Mapping[str, Any] | None = None,
    provenance: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Require a clean writable set for one repair child.

    ``provenance`` maps repository-qualified paths to the content digest the
    run recorded for them at a moment it could attest them as its own (see
    ``repair_provenance``): plan artifacts a review cycle left in a plan
    directory tracked inside the repository, and the work this same child's
    earlier turn in this run retained when it ended without completing. A dirty
    path is sanctioned only while its digest is unchanged, so an edit made
    after the record, a path that was never recorded, and content that cannot
    be digested all still refuse below exactly as unrelated dirt does.

    ``retained_scope_repair`` is the uncleared write-scope-violation record for
    this same child. On that path the runner is the author of *some* of the
    dirt: it deliberately retained the violating turn's changes and reverted the
    child to pending so its durable scope could be repaired. Demanding a clean
    writable set there dead-ends the very repair the retention exists to enable,
    so the sanction covers exactly the paths that turn is recorded as having
    written and still recorded as holding the content it left at them. The
    sanction stops there. A legacy repair child's commit is still
    repository-wide, so any other dirty path -- work an operator started after
    the pause in a repository the child is about to touch, or an edit they made
    to a retained path itself -- would be staged and committed under the
    repair's name; that case raises here, before the harness starts, rather than
    riding along. A registered child stages only the paths its own turn guard
    recorded, but the gate stays uniform: dirt the child did not author is not
    its to carry, whichever staging path it takes. Nothing escapes review
    either: the retained-scope branch still judges every retained change against
    the child's post-regeneration write scope before the turn starts, and pauses
    when any of it falls outside.
    """
    details: dict[str, Any] = {
        "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
        "repositories": [],
    }
    workspace = verification_context.workspace
    if workspace is None:
        selected: list[tuple[str | None, str, Path]] = [(None, ".", cwd)]
    else:
        if session_scope is None or not session_scope.writes:
            message = "generated repair child declares no writable repository"
            details["error"] = message
            raise RepairCommitPreflightError(message, details)
        write_aliases = set(session_scope.writes)
        selected = [
            (repository.alias, repository.repository.path, repository.root)
            for repository in workspace.repositories
            if repository.alias in write_aliases
        ]

    dirty_repositories: list[dict[str, Any]] = []
    provenance_sanctioned: list[str] = []
    provenance_drifted: list[str] = []
    provenance_errors: list[str] = []
    for alias, repository_path, root in selected:
        worktree = review_guard_git._run_git(
            root, ["rev-parse", "--is-inside-work-tree"]
        )
        if worktree.returncode != 0 or worktree.stdout.strip() != "true":
            detail = worktree.stderr.strip() or worktree.stdout.strip()
            message = f"writable repository {alias or '.'} is not a Git worktree" + (
                f": {detail}" if detail else ""
            )
            details["error"] = message
            raise RepairCommitPreflightError(message, details)
        status = review_guard_git._run_git(
            root, ["status", "--porcelain", "-z", "--untracked-files=all"]
        )
        if status.returncode != 0:
            detail = status.stderr.strip() or status.stdout.strip()
            message = f"could not inspect writable repository {alias or '.'}"
            if detail:
                message += f": {detail}"
            details["error"] = message
            raise RepairCommitPreflightError(message, details)
        dirty_paths = review_guard_git._porcelain_dirty_paths(status.stdout)
        qualified_paths = [
            f"{alias}:{path}" if alias is not None else path for path in dirty_paths
        ]
        row: dict[str, Any] = {
            "alias": alias,
            "path": repository_path,
            "resolved_root": str(root),
            "clean": not dirty_paths,
            "dirty_paths": qualified_paths,
        }
        details["repositories"].append(row)
        residual = list(qualified_paths)
        if residual and provenance:
            proven, drifted, error = _provenance_sanctioned(
                root, dirty_paths, qualified_paths, provenance
            )
            if error is not None:
                provenance_errors.append(f"{alias or '.'}: {error}")
            provenance_sanctioned.extend(proven)
            provenance_drifted.extend(drifted)
            residual = [path for path in residual if path not in set(proven)]
            if proven:
                row["provenance_sanctioned_paths"] = proven
        if residual:
            dirty_repositories.append(
                row if residual == qualified_paths else {**row, "dirty_paths": residual}
            )
    if provenance_sanctioned or provenance_drifted or provenance_errors:
        details["provenance"] = {
            "sanctioned_dirty_paths": provenance_sanctioned,
            "content_drifted_dirty_paths": provenance_drifted,
            **({"errors": provenance_errors} if provenance_errors else {}),
        }

    if dirty_repositories:
        if retained_scope_repair is not None:
            if workspace is None or plan_dir is None:
                content = _RetainedContentEvidence(
                    False,
                    reason="retained-scope recovery requires a registered workspace",
                )
            else:
                assert session_scope is not None
                content = _retained_turn_content_drift(
                    retained_scope_repair,
                    workspace=workspace,
                    scope=session_scope,
                    plan_dir=plan_dir,
                    workspace_evidence=workspace_evidence,
                )
            retained_paths = (
                _retained_turn_changed_paths(retained_scope_repair) - content.drifted
                if content.available
                else frozenset()
            )
            sanctioned: list[str] = []
            unsanctioned: list[str] = []
            drifted_dirty: list[str] = []
            unsanctioned_rows: list[dict[str, Any]] = []
            for row in dirty_repositories:
                stray = [
                    path for path in row["dirty_paths"] if path not in retained_paths
                ]
                sanctioned.extend(
                    path for path in row["dirty_paths"] if path in retained_paths
                )
                drifted_dirty.extend(path for path in stray if path in content.drifted)
                if stray:
                    unsanctioned.extend(stray)
                    unsanctioned_rows.append(row)
            details["retained_scope_repair"] = {
                "session_id": retained_scope_repair.get("session_id"),
                "sanctioned_repositories": ", ".join(
                    str(row["alias"] or ".")
                    for row in dirty_repositories
                    if any(path in retained_paths for path in row["dirty_paths"])
                ),
                "sanctioned_dirty_paths": sanctioned,
                "retained_turn_paths": sorted(
                    _retained_turn_changed_paths(retained_scope_repair)
                ),
                "unsanctioned_dirty_paths": unsanctioned,
                "content_drifted_dirty_paths": drifted_dirty,
                "content_evidence": content.payload(),
            }
            if not unsanctioned:
                return details
            named = ", ".join(unsanctioned[:3])
            if len(unsanctioned) > 3:
                named += f" (+{len(unsanctioned) - 3} more)"
            if not content.available:
                message = (
                    "generated repair child cannot recover its retained changes "
                    "without durable evidence of what the retained turn left "
                    f"behind: {content.reason}; dirty paths: {named}"
                )
            elif drifted_dirty:
                message = (
                    "generated repair child cannot recover its retained changes "
                    "after they were edited; dirty paths the retained turn did "
                    f"not leave behind: {named}"
                )
            else:
                message = (
                    "generated repair child cannot recover its retained changes "
                    "while unrelated work is present; dirty paths the retained "
                    f"turn did not write: {named}"
                )
            details["error"] = message
            details["dirty_repositories"] = unsanctioned_rows
            raise RepairCommitPreflightError(message, details)
        labels = ", ".join(str(row["alias"] or ".") for row in dirty_repositories)
        message = (
            "generated repair child requires clean writable repositories before "
            f"execution; dirty: {labels}"
        )
        if provenance_drifted:
            named = ", ".join(provenance_drifted[:3])
            if len(provenance_drifted) > 3:
                named += f" (+{len(provenance_drifted) - 3} more)"
            message += (
                "; content changed after the run recorded it as its own: " + named
            )
        details["error"] = message
        details["dirty_repositories"] = dirty_repositories
        raise RepairCommitPreflightError(message, details)
    return details


def _provenance_sanctioned(
    root: Path,
    dirty_paths: list[str],
    qualified_paths: list[str],
    provenance: Mapping[str, str],
) -> tuple[list[str], list[str], str | None]:
    """Split one repository's dirty paths by whether their recorded content holds.

    Returns the sanctioned qualified paths, the recorded ones whose content has
    since changed, and why the repository could not be measured at all. An
    unmeasurable repository sanctions nothing: not being able to look is not
    evidence that nothing moved.
    """
    if not any(path in provenance for path in qualified_paths):
        return [], [], None
    try:
        evidence = review_guard_provenance._dirty_path_evidence(root)
    except RunError as error:
        return [], [], str(error)
    sanctioned: list[str] = []
    drifted: list[str] = []
    for path, qualified in zip(dirty_paths, qualified_paths, strict=True):
        recorded = provenance.get(qualified)
        if recorded is None:
            continue
        current = evidence.digest(path)
        if current is not None and current == recorded:
            sanctioned.append(qualified)
        else:
            drifted.append(qualified)
    return sanctioned, drifted, None
