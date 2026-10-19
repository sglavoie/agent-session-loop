"""Capture write-scope baselines and identify unobserved legacy repositories."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import repository_evidence, repository_intent, review_guard, workspaces


def _capture_workspace_write_guard_snapshot(
    *,
    workspace: workspaces.ResolvedWorkspace,
    plan_dir: Path,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> repository_evidence.WorkspaceSnapshot:
    """Capture exactly the repositories the plan registered in ``workspace``.

    Repositories the plan never registered are not the plan's business: a
    concurrent run working in a sibling worktree must not appear as evidence
    about this turn. The public review-guard exclusions are
    repository-relative and apply to every registered repository. Git's
    ordinary ignore policy is still honored unless ``include_ignored`` was
    explicitly requested.
    """
    return repository_evidence.capture_registered_snapshot(
        workspace.root,
        plan_dir,
        tuple(repository.repository.path for repository in workspace.repositories),
        default_exclude=exclude,
        include_ignored=include_ignored,
        _run_git_fn=review_guard._run_git,
        _run_git_bytes_fn=review_guard._run_git_bytes,
    )


def _capture_legacy_implementation_snapshot(
    *,
    cwd: Path,
    plan_dir: Path,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> repository_evidence.WorkspaceSnapshot:
    """Capture the one Git repository that grants a legacy run authority."""
    return OrderedDict(
        (
            (
                ".",
                review_guard._capture_review_repo_fingerprint(
                    cwd,
                    plan_dir,
                    exclude=exclude,
                    include_ignored=include_ignored,
                ),
            ),
        )
    )


def _legacy_unobserved_repositories(
    *,
    cwd: Path,
    artifact_paths: Sequence[Path],
    exempt_roots: Sequence[Path],
    provenance_roots: Sequence[Path],
    limit: int = repository_intent.INTENT_REPORT_LIMIT,
) -> dict[str, dict[str, Any]]:
    """Name every repository this turn reached that the snapshot never saw.

    :func:`_capture_legacy_implementation_snapshot` fingerprints exactly one
    repository, the one containing ``cwd``. A write landing anywhere else
    therefore produces no ``changed`` entry, no added or removed repository,
    and nothing ``unavailable`` -- so the guard reported ``passed: True`` while
    an edit landed in a repository it had never looked at. Silence about a
    place you did not look is not evidence of no change.

    The repositories a turn reaches are re-derived here from the same intent
    machinery the ``identity.repository-intent`` check reads, so the two stop
    contradicting each other. The pre-turn packet gate scans what the runner
    hands the worker; this scan runs after the turn over what came back --
    plan artifacts the worker just rewrote and the message it signed off with
    -- because that is where a write the fingerprint could not see gets named.

    Only the exemptions the pre-turn gate already honors apply: runner-owned
    trees, the document the plan was split from, non-Git paths, and system
    paths. Whatever survives that and is not the bound repository is reported
    through ``unavailable``, the guard's existing fail-closed lane.
    """
    candidates: list[repository_intent.IntentCandidate] = []
    for path in artifact_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # An artifact that cannot be read names no repository. The turn is
            # still judged on every artifact that can be, and a missing
            # fingerprint is caught by the comparison, not by this scan.
            continue
        candidates.extend(repository_intent.candidates_from_plan_document(text))
    candidates.extend(repository_intent.candidates_from_execution_roots([cwd]))
    intent = repository_intent.resolve_intent(
        candidates,
        root=cwd,
        exempt_roots=exempt_roots,
        provenance_roots=provenance_roots,
    )
    unobserved = [root for root in intent.git_roots if root != intent.bound_git_root]
    reported, omitted = workspaces.bounded_repository_rows(unobserved, limit=limit)
    observed = (
        repository_intent.redact_credentials(str(intent.bound_git_root))
        if intent.bound_git_root is not None
        else None
    )
    payload: dict[str, dict[str, Any]] = {}
    for root in reported:
        payload[repository_intent.redact_credentials(str(root))] = {
            "reason": "repository-not-observed",
            "observed_repository": observed,
        }
    if omitted:
        # The count matters more than the names once the list is truncated:
        # the guard fails closed on the first entry either way.
        payload["<omitted>"] = {
            "reason": "repository-not-observed",
            "observed_repository": observed,
            "omitted_repositories": omitted,
        }
    return payload
