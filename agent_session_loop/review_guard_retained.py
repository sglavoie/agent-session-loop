"""Retained-scope evidence a repair child may sanction dirt against."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import plans_session_scope, repository_evidence, review_guard_git, workspaces
from .run_errors import RunError


class RepairCommitPreflightError(RunError):
    """A generated repair child cannot safely enter its owned commit phase."""

    def __init__(self, message: str, details: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.details = dict(details)


class WorkspaceEvidenceProbe(Protocol):
    """Classify a workspace comparison against one session's write scope.

    Declared structurally rather than imported so this module never depends on
    ``workspace_evidence``, which owns the concrete projection. The
    retained-scope
    branch of :func:`_repair_commit_preflight` needs exactly one thing from it:
    the per-path ``changed`` evidence, qualified by repository alias, that
    :func:`_retained_turn_changed_paths` reads. Passing the same projection the
    write guard itself uses is what makes the drifted paths qualified the same
    way as the retained paths they are subtracted from.
    """

    def __call__(
        self,
        *,
        workspace: workspaces.ResolvedWorkspace,
        scope: plans_session_scope.SessionScope,
        comparison: Mapping[str, Any],
        exclude: tuple[str, ...],
        include_ignored: bool,
    ) -> dict[str, Any]: ...


def _retained_turn_changed_paths(record: Mapping[str, Any]) -> frozenset[str]:
    """Qualified paths the retained violating turn is proven to have written.

    The guard's own ``changed`` evidence is the proof: it is the before/after
    comparison of that turn and nothing else, already qualified by alias. The
    record's ``violations`` are a projection of these same paths, so reading
    ``changed`` reads both the authorized and the escaped half of the retained
    work. Anything the guard could not see -- an ignored path, an excluded one,
    the plan directory -- is deliberately absent: not being observed is not
    proof of origin.
    """
    paths: set[str] = set()
    changed = record.get("changed")
    if not isinstance(changed, Mapping):
        return frozenset()
    for entry in changed.values():
        if not isinstance(entry, Mapping):
            continue
        qualified = entry.get("qualified_paths")
        if isinstance(qualified, Sequence) and not isinstance(qualified, (str, bytes)):
            paths.update(str(path) for path in qualified)
    return frozenset(paths)


@dataclass(frozen=True)
class _RetainedContentEvidence:
    """Whether retained paths still hold the bytes their own turn left behind.

    ``available`` is the only key a caller may sanction on: it is false whenever
    the comparison could not be made at all, and an unproven path is never a
    proven one. ``drifted`` names the retained paths whose current worktree or
    index content no longer matches the post-turn record.
    """

    available: bool
    drifted: frozenset[str] = frozenset()
    reason: str | None = None
    repositories: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "available": self.available,
            "drifted_paths": sorted(self.drifted),
            "repositories": list(self.repositories),
        }
        if self.reason is not None:
            record["reason"] = self.reason
        return record


def _retained_turn_content_drift(
    record: Mapping[str, Any],
    *,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans_session_scope.SessionScope,
    plan_dir: Path,
    workspace_evidence: WorkspaceEvidenceProbe,
) -> _RetainedContentEvidence:
    """Compare the writable repositories against the retained turn's own after-state.

    A pathname is not an origin. The violating turn's guard record proves which
    paths that turn wrote, but the bytes sitting at one of those paths now are
    whatever the last writer put there, and between the pause and this resume
    that writer may have been an operator. The record's ``snapshot_after`` is the
    durable description of what the turn actually left behind — per path, in the
    worktree and in the index — so recapturing the same evidence now and
    comparing the two vectors is what distinguishes a retained path that is still
    retained content from one that has since been rewritten.

    The comparison is made under the exclusion policy the record itself names
    rather than this run's, because a fingerprint captured under a different
    policy describes a different thing and would report drift where none
    happened. Everything that cannot be compared — a missing after-vector, a
    writable repository the vector never covered, a capture failure, a
    repository that has since disappeared — returns ``available=False`` so the
    caller sanctions nothing.
    """
    writable = tuple(
        repository.repository.path
        for repository in workspace.repositories
        if repository.alias in scope.writes
    )
    if not writable:
        return _RetainedContentEvidence(
            False, reason="the repair child declares no writable registered repository"
        )
    recorded_after = record.get("snapshot_after")
    if not isinstance(recorded_after, Mapping) or not recorded_after:
        return _RetainedContentEvidence(
            False, reason="retained turn recorded no post-turn content evidence"
        )
    recorded: repository_evidence.WorkspaceSnapshot = OrderedDict()
    for key, fingerprint in recorded_after.items():
        linked = repository_evidence.split_linked_worktree_key(str(key))
        owner = linked[0] if linked is not None else str(key)
        if owner in writable and isinstance(fingerprint, Mapping):
            recorded[str(key)] = dict(fingerprint)
    uncovered = [path for path in writable if path not in recorded]
    if uncovered:
        return _RetainedContentEvidence(
            False,
            reason=(
                "retained turn recorded no post-turn content evidence for "
                f"{', '.join(sorted(uncovered))}"
            ),
        )
    exclude = tuple(str(entry) for entry in record.get("excluded") or ())
    include_ignored = bool(record.get("include_ignored"))
    try:
        current = repository_evidence.capture_registered_snapshot(
            workspace.root,
            plan_dir,
            writable,
            default_exclude=exclude,
            include_ignored=include_ignored,
            _run_git_fn=review_guard_git._run_git,
            _run_git_bytes_fn=review_guard_git._run_git_bytes,
        )
    except repository_evidence.RepositoryEvidenceError as error:
        return _RetainedContentEvidence(
            False, reason=f"current content evidence unavailable: {error}"
        )
    comparison = repository_evidence.compare_workspace_snapshots(recorded, current)
    unavailable = comparison.get("unavailable") or {}
    removed = comparison.get("removed") or []
    if unavailable or removed:
        named = ", ".join(sorted({*map(str, unavailable), *map(str, removed)}))
        return _RetainedContentEvidence(
            False, reason=f"content evidence could not be compared for {named}"
        )
    evidence = workspace_evidence(
        workspace=workspace,
        scope=scope,
        comparison=comparison,
        exclude=exclude,
        include_ignored=include_ignored,
    )
    # Both sets are produced by one projection, so the drifted paths are
    # qualified exactly the way the retained paths they are subtracted from are.
    return _RetainedContentEvidence(
        True,
        drifted=_retained_turn_changed_paths(evidence),
        repositories=writable,
    )
