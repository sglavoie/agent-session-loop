"""Capture registered repositories and linked worktrees and compare their vectors."""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    repository_evidence_capture as evidence_capture,
)
from . import (
    repository_evidence_compare as evidence_compare,
)
from . import (
    repository_evidence_git as evidence_git,
)
from . import (
    repository_evidence_transitions as evidence_transitions,
)
from . import (
    repository_evidence_worktrees as evidence_worktrees,
)

if TYPE_CHECKING:
    from . import repository_evidence


def _is_exact_worktree_root(repository: Path) -> bool:
    """Report whether Git calls `repository` its own worktree root."""
    try:
        result = evidence_git._run_git(repository, ["rev-parse", "--show-toplevel"])
    except Exception:  # noqa: BLE001
        return False
    if result.returncode != 0:
        return False
    reported = result.stdout.strip()
    if not reported:
        return False
    try:
        resolved = Path(os.path.realpath(os.path.abspath(reported)))
    except OSError:
        return False
    return resolved == repository


def capture_registered_snapshot(
    workspace_root: str | Path,
    plan_dir: str | Path,
    repositories: Sequence[str],
    *,
    exclude: Mapping[str, tuple[str, ...]] | None = None,
    default_exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
    include_linked_worktrees: bool = True,
    _run_git_fn: evidence_git.GitRunner | None = None,
    _run_git_bytes_fn: evidence_git.GitBytesRunner | None = None,
) -> repository_evidence.WorkspaceSnapshot:
    """Capture an evidence vector for exactly the registered repositories.

    ``repositories`` holds workspace-relative POSIX paths as the plan registry
    declares them, and the snapshot keys repeat them verbatim. Nothing else
    under `workspace_root` is inspected, so repositories the plan never
    registered cannot enter the evidence at all.

    A registered path that is missing, is not a directory, or no longer names
    the node it reaches is omitted rather than recorded, so
    :func:`compare_workspace_snapshots` reports it as removed. A path that
    exists but is no longer its own Git worktree root carries the same
    fail-closed ``repository-disappeared`` evidence
    :func:`capture_workspace_snapshot` uses, because losing that fact is a race
    rather than proof the guard stopped applying.

    Each registered root's linked worktrees are captured beside it under a
    :func:`linked_worktree_key`, because a second checkout of a registered
    repository is a place a turn can move HEAD and the index without touching
    any registered root. A sibling that is itself registered keeps its own key
    rather than being recorded twice. ``include_linked_worktrees=False`` stops
    that enumeration, which is what a caller wants when the roots it passed are
    themselves a private checkout: from inside one, Git lists the very trees the
    caller isolated itself from, and fingerprinting them would put the traffic
    of every other checkout back into the evidence.
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
    git_token = evidence_git._GIT_RUNNER.set(_run_git_fn)
    bytes_token = evidence_git._GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        snapshot: repository_evidence.WorkspaceSnapshot = OrderedDict()
        per_repository_excludes = exclude or {}
        ordered = sorted(dict.fromkeys(repositories))
        registered_roots = {
            Path(os.path.abspath(root / relative)) for relative in ordered
        }
        for relative in ordered:
            repository = Path(os.path.abspath(root / relative))
            # Canonicalizing here would fingerprint whatever a replacement
            # symlink points at under the declared key, so a checkout swapped
            # for a link to an identical sibling would compare as unchanged.
            if os.path.realpath(repository) != os.fspath(repository):
                continue
            if not repository.is_dir():
                continue
            repository_exclude = tuple(
                per_repository_excludes.get(relative, default_exclude)
            )
            fingerprint: repository_evidence.RepositoryFingerprint = {"git": False}
            is_worktree_root = _is_exact_worktree_root(repository)
            if is_worktree_root:
                fingerprint = evidence_capture._capture_review_repo_fingerprint(
                    repository,
                    plan_dir,
                    exclude=repository_exclude,
                    include_ignored=include_ignored,
                )
            if not fingerprint.get("git"):
                fingerprint = {
                    "git": True,
                    "unavailable": {
                        "reason": "repository-disappeared",
                        "argv": ["rev-parse", "--show-toplevel"],
                    },
                }
            snapshot[relative] = fingerprint
            if not is_worktree_root or not include_linked_worktrees:
                continue
            snapshot.update(
                _capture_linked_worktrees(
                    repository,
                    plan_dir,
                    relative=relative,
                    registered_roots=registered_roots,
                    exclude=repository_exclude,
                    include_ignored=include_ignored,
                )
            )
        return snapshot
    finally:
        evidence_git._GIT_BYTES_RUNNER.reset(bytes_token)
        evidence_git._GIT_RUNNER.reset(git_token)


def _capture_linked_worktrees(
    repository: Path,
    plan_dir: str | Path,
    *,
    relative: str,
    registered_roots: AbstractSet[Path],
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> repository_evidence.WorkspaceSnapshot:
    """Fingerprint every linked worktree of one registered repository.

    An enumeration that fails leaves a fail-closed entry rather than silence:
    not knowing whether a repository has other checkouts is not evidence that
    it has none.
    """
    captured: repository_evidence.WorkspaceSnapshot = OrderedDict()
    try:
        worktrees = evidence_worktrees.linked_worktrees(repository)
    except evidence_git.RepositoryEvidenceError as error:
        captured[evidence_worktrees.linked_worktree_key(relative, "")] = {
            "git": True,
            "unavailable": {
                "reason": "linked-worktrees-unlistable",
                "error": str(error),
                "argv": ["worktree", "list", "--porcelain"],
            },
        }
        return captured
    for worktree in worktrees:
        if worktree in registered_roots:
            continue
        key = evidence_worktrees.linked_worktree_key(relative, worktree)
        fingerprint: repository_evidence.RepositoryFingerprint = {"git": False}
        if worktree.is_dir() and _is_exact_worktree_root(worktree):
            fingerprint = evidence_capture._capture_review_repo_fingerprint(
                worktree,
                plan_dir,
                exclude=exclude,
                include_ignored=include_ignored,
            )
        if not fingerprint.get("git"):
            fingerprint = {
                "git": True,
                "unavailable": {
                    "reason": "repository-disappeared",
                    "argv": ["rev-parse", "--show-toplevel"],
                },
            }
        captured[key] = fingerprint
    return captured


def compare_workspace_snapshots(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare repository vectors with deterministic qualified-path details."""
    before_roots = set(before)
    after_roots = set(after)
    unavailable: OrderedDict[str, dict[str, Any]] = OrderedDict()
    changed: OrderedDict[str, dict[str, Any]] = OrderedDict()

    for repository in sorted(before_roots | after_roots):
        before_fingerprint = before.get(repository)
        after_fingerprint = after.get(repository)
        before_unavailable = (
            evidence_compare._review_guard_unavailable_detail(before_fingerprint)
            if before_fingerprint is not None
            else None
        )
        after_unavailable = (
            evidence_compare._review_guard_unavailable_detail(after_fingerprint)
            if after_fingerprint is not None
            else None
        )
        if before_fingerprint is not None and not before_fingerprint.get("git"):
            before_unavailable = {"reason": "repository-unavailable"}
        if after_fingerprint is not None and not after_fingerprint.get("git"):
            after_unavailable = {"reason": "repository-unavailable"}
        if before_unavailable is not None or after_unavailable is not None:
            unavailable[repository] = {
                "before": before_unavailable,
                "after": after_unavailable,
            }
            continue
        if before_fingerprint is None or after_fingerprint is None:
            continue
        if before_fingerprint != after_fingerprint:
            changes = evidence_compare._review_guard_changed_paths(
                before_fingerprint, after_fingerprint
            )
            record = {
                **changes,
                "qualified_paths": evidence_transitions._qualified_paths(
                    repository, changes
                ),
            }
            drift = evidence_transitions.remote_ref_drift(
                before_fingerprint, after_fingerprint
            )
            if drift:
                # Additive: the raw ref lists stay complete for existing
                # readers, and the write guard reads these two keys so a
                # remote-tracking ref move alone never convicts a turn.
                record["remote_ref_drift"] = drift
                record["remote_ref_drift_only"] = (
                    evidence_transitions.without_remote_tracking_refs(
                        before_fingerprint
                    )
                    == evidence_transitions.without_remote_tracking_refs(
                        after_fingerprint
                    )
                )
            changed[repository] = record

    return {
        "changed": changed,
        "added": sorted(after_roots - before_roots),
        "removed": sorted(before_roots - after_roots),
        "unavailable": unavailable,
    }
