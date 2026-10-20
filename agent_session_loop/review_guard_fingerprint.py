"""Compatibility wrappers over the repository evidence engine."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import repository_evidence, review_guard_git


def _capture_review_repo_fingerprint(
    cwd: str | Path,
    plan_dir: str | Path,
    *,
    exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper for the extracted repository evidence engine."""
    return repository_evidence.capture_repository_fingerprint(
        cwd,
        plan_dir,
        exclude=exclude,
        include_ignored=include_ignored,
        _run_git_fn=review_guard_git._run_git,
        _run_git_bytes_fn=review_guard_git._run_git_bytes,
    )


def _review_guard_unavailable_detail(
    fingerprint: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Compatibility wrapper for fail-closed repository evidence details."""
    return repository_evidence.repository_evidence_unavailable(fingerprint)


def _default_excludes_file() -> Path | None:
    """Compatibility wrapper for Git's default global excludes path."""
    return repository_evidence.default_excludes_file()


def _effective_excludes_file(cwd: Path, repo_root: Path) -> tuple[str, Path | None]:
    """Compatibility wrapper for effective global exclude resolution."""
    return repository_evidence.effective_excludes_file(
        cwd, repo_root, _run_git_bytes_fn=review_guard_git._run_git_bytes
    )


def _review_repo_state(fingerprint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Compatibility wrapper for state derived from repository evidence."""
    return repository_evidence.repository_state(fingerprint)


def _index_flagged_paths(listing_text: str) -> list[str]:
    """Compatibility wrapper for diff-suppressed Git index entries."""
    return repository_evidence.index_flagged_paths(listing_text)


def _review_guard_changed_paths(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper for single-repository evidence comparison."""
    return repository_evidence.compare_repository_fingerprints(before, after)


def _review_guard_changed_summary(changed_paths: Mapping[str, Any]) -> str:
    """Compatibility wrapper for the historical bounded change summary."""
    return repository_evidence.format_repository_changes(changed_paths)
