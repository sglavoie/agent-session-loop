"""Reusable fail-closed Git repository and workspace evidence.

The single-repository fingerprint schema is intentionally identical to the
historical review mutation guard. Workspace inventory layers deterministic,
symlink-safe worktree discovery and vector comparison over that schema."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
    repository_evidence_ignore as evidence_ignore,
)
from . import (
    repository_evidence_nodes as evidence_nodes,
)
from .repository_evidence_capture import (
    _capture_review_repo_fingerprint as _capture_review_repo_fingerprint,
)
from .repository_evidence_capture import (
    _pair_diff_names_with_patches as _pair_diff_names_with_patches,
)
from .repository_evidence_compare import (
    _review_guard_changed_paths as _review_guard_changed_paths,
)
from .repository_evidence_compare import (
    _review_guard_changed_summary as _review_guard_changed_summary,
)
from .repository_evidence_compare import (
    _review_guard_unavailable_detail as _review_guard_unavailable_detail,
)
from .repository_evidence_compare import _review_repo_state as _review_repo_state
from .repository_evidence_git import _DIFF_FILE_HEADER_RE as _DIFF_FILE_HEADER_RE
from .repository_evidence_git import _EMPTY_TREE_ARGV as _EMPTY_TREE_ARGV
from .repository_evidence_git import _EXCLUDES_CONFIG_REL as _EXCLUDES_CONFIG_REL
from .repository_evidence_git import _EXCLUDES_FILE_ARGV as _EXCLUDES_FILE_ARGV
from .repository_evidence_git import _EXCLUDES_FILE_KEY as _EXCLUDES_FILE_KEY
from .repository_evidence_git import _GIT_BYTES_RUNNER as _GIT_BYTES_RUNNER
from .repository_evidence_git import _GIT_RUNNER as _GIT_RUNNER
from .repository_evidence_git import _GUARD_DIFF_FLAGS as _GUARD_DIFF_FLAGS
from .repository_evidence_git import _IGNORE_ROLE_CONFIG as _IGNORE_ROLE_CONFIG
from .repository_evidence_git import _IGNORE_ROLE_SOURCE as _IGNORE_ROLE_SOURCE
from .repository_evidence_git import _IGNORE_ROLE_TARGET as _IGNORE_ROLE_TARGET
from .repository_evidence_git import (
    _TYPE_CHANGED_PATCH_SECTIONS as _TYPE_CHANGED_PATCH_SECTIONS,
)
from .repository_evidence_git import _TYPE_CHANGED_STATUS as _TYPE_CHANGED_STATUS
from .repository_evidence_git import _WORKTREE_IGNORE_ARGV as _WORKTREE_IGNORE_ARGV
from .repository_evidence_git import _WORKTREE_IGNORE_FILE as _WORKTREE_IGNORE_FILE
from .repository_evidence_git import GitBytesRunner as GitBytesRunner
from .repository_evidence_git import GitRunner as GitRunner
from .repository_evidence_git import GitWorktreeInventory as GitWorktreeInventory
from .repository_evidence_git import RepositoryEvidenceError as RepositoryEvidenceError
from .repository_evidence_git import _fingerprint_refs as _fingerprint_refs
from .repository_evidence_git import (
    _literal_exclude_pathspec as _literal_exclude_pathspec,
)
from .repository_evidence_git import _review_guard_diff_base as _review_guard_diff_base
from .repository_evidence_git import _review_guard_git as _review_guard_git
from .repository_evidence_git import _ReviewGuardUnavailable as _ReviewGuardUnavailable
from .repository_evidence_git import _run_git as _run_git
from .repository_evidence_git import _run_git_bytes as _run_git_bytes
from .repository_evidence_ignore import _default_excludes_file as _default_excludes_file
from .repository_evidence_ignore import (
    _effective_excludes_file as _effective_excludes_file,
)
from .repository_evidence_ignore import (
    _fingerprint_ignore_config as _fingerprint_ignore_config,
)
from .repository_evidence_ignore import (
    _fingerprint_ignore_source as _fingerprint_ignore_source,
)
from .repository_evidence_ignore import (
    _fingerprint_ignore_target as _fingerprint_ignore_target,
)
from .repository_evidence_ignore import _ignore_source_rel as _ignore_source_rel
from .repository_evidence_nodes import NESTED_REPOSITORY_KIND as NESTED_REPOSITORY_KIND
from .repository_evidence_nodes import (
    _fingerprint_index_flagged_nodes as _fingerprint_index_flagged_nodes,
)
from .repository_evidence_nodes import _fingerprint_repo_node as _fingerprint_repo_node
from .repository_evidence_nodes import (
    _fingerprint_untracked_nodes as _fingerprint_untracked_nodes,
)
from .repository_evidence_nodes import _index_flagged_paths as _index_flagged_paths
from .repository_evidence_nodes import (
    _review_guard_skips_path as _review_guard_skips_path,
)
from .repository_evidence_registered import (
    _capture_linked_worktrees as _capture_linked_worktrees,
)
from .repository_evidence_registered import (
    _is_exact_worktree_root as _is_exact_worktree_root,
)
from .repository_evidence_registered import (
    capture_registered_snapshot as capture_registered_snapshot,
)
from .repository_evidence_registered import (
    compare_workspace_snapshots as compare_workspace_snapshots,
)
from .repository_evidence_transitions import _REF_CHANGE_KEYS as _REF_CHANGE_KEYS
from .repository_evidence_transitions import (
    REMOTE_TRACKING_REF_PREFIX as REMOTE_TRACKING_REF_PREFIX,
)
from .repository_evidence_transitions import _qualified_paths as _qualified_paths
from .repository_evidence_transitions import _workspace_relative as _workspace_relative
from .repository_evidence_transitions import (
    classify_head_transition as classify_head_transition,
)
from .repository_evidence_transitions import (
    format_repository_changes as format_repository_changes,
)
from .repository_evidence_transitions import (
    head_transition_paths as head_transition_paths,
)
from .repository_evidence_transitions import (
    is_remote_tracking_ref as is_remote_tracking_ref,
)
from .repository_evidence_transitions import (
    qualified_repository_paths as qualified_repository_paths,
)
from .repository_evidence_transitions import remote_ref_drift as remote_ref_drift
from .repository_evidence_transitions import (
    split_remote_ref_changes as split_remote_ref_changes,
)
from .repository_evidence_transitions import (
    unchanged_head_transition as unchanged_head_transition,
)
from .repository_evidence_transitions import (
    without_remote_tracking_refs as without_remote_tracking_refs,
)
from .repository_evidence_workspace import (
    _inventory_candidates as _inventory_candidates,
)
from .repository_evidence_workspace import (
    capture_workspace_snapshot as capture_workspace_snapshot,
)
from .repository_evidence_workspace import (
    discover_git_worktree_inventory as discover_git_worktree_inventory,
)
from .repository_evidence_workspace import (
    discover_git_worktrees as discover_git_worktrees,
)
from .repository_evidence_worktrees import (
    LINKED_WORKTREE_KEY_MARKER as LINKED_WORKTREE_KEY_MARKER,
)
from .repository_evidence_worktrees import (
    add_detached_worktree as add_detached_worktree,
)
from .repository_evidence_worktrees import linked_worktree_key as linked_worktree_key
from .repository_evidence_worktrees import linked_worktrees as linked_worktrees
from .repository_evidence_worktrees import remove_worktree as remove_worktree
from .repository_evidence_worktrees import (
    split_linked_worktree_key as split_linked_worktree_key,
)

RepositoryFingerprint = dict[str, Any]


WorkspaceSnapshot = OrderedDict[str, RepositoryFingerprint]


def capture_repository_fingerprint(
    cwd: str | Path,
    plan_dir: str | Path,
    *,
    exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
    _run_git_fn: evidence_git.GitRunner | None = None,
    _run_git_bytes_fn: evidence_git.GitBytesRunner | None = None,
) -> RepositoryFingerprint:
    """Capture one repository using the stable review-guard payload schema.

    The private runner hooks let the legacy runner wrappers preserve their
    monkeypatch seam during migration. New callers should leave them unset.
    """
    git_token = evidence_git._GIT_RUNNER.set(_run_git_fn)
    bytes_token = evidence_git._GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        return evidence_capture._capture_review_repo_fingerprint(
            cwd,
            plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    finally:
        evidence_git._GIT_BYTES_RUNNER.reset(bytes_token)
        evidence_git._GIT_RUNNER.reset(git_token)


def repository_evidence_unavailable(
    fingerprint: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return fail-closed probe details from one repository fingerprint."""
    return evidence_compare._review_guard_unavailable_detail(fingerprint)


def default_excludes_file() -> Path | None:
    """Return Git's default global excludes path, when one is configured."""
    return evidence_ignore._default_excludes_file()


def effective_excludes_file(
    cwd: Path,
    repo_root: Path,
    *,
    _run_git_bytes_fn: evidence_git.GitBytesRunner | None = None,
) -> tuple[str, Path | None]:
    """Resolve Git's effective global excludes marker and source path."""
    token = evidence_git._GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        return evidence_ignore._effective_excludes_file(cwd, repo_root)
    finally:
        evidence_git._GIT_BYTES_RUNNER.reset(token)


def repository_state(
    fingerprint: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return HEAD and dirty-path state derived without further Git probes."""
    return evidence_compare._review_repo_state(fingerprint)


def index_flagged_paths(listing_text: str) -> list[str]:
    """Return paths hidden from worktree diffs by Git index flags."""
    return evidence_nodes._index_flagged_paths(listing_text)


def compare_repository_fingerprints(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two single-repository evidence payloads."""
    return evidence_compare._review_guard_changed_paths(before, after)


__all__ = [
    "GitWorktreeInventory",
    "RepositoryEvidenceError",
    "RepositoryFingerprint",
    "WorkspaceSnapshot",
    "capture_registered_snapshot",
    "capture_repository_fingerprint",
    "capture_workspace_snapshot",
    "classify_head_transition",
    "compare_repository_fingerprints",
    "compare_workspace_snapshots",
    "discover_git_worktree_inventory",
    "discover_git_worktrees",
    "format_repository_changes",
    "head_transition_paths",
    "is_remote_tracking_ref",
    "remote_ref_drift",
    "repository_evidence_unavailable",
    "repository_state",
    "split_remote_ref_changes",
    "without_remote_tracking_refs",
]
