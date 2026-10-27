"""Fingerprint tracked, untracked, and index-hidden filesystem nodes."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Sequence
from pathlib import Path

from . import repository_evidence_git as evidence_git

#: The node kind of an untracked embedded repository, hashed over that
#: repository's own fingerprint rather than over any one file's bytes.
NESTED_REPOSITORY_KIND = "nested-repo"


def _fingerprint_repo_node(
    node: Path,
    *,
    rel: str,
    argv: Sequence[str],
    reason_prefix: str,
    absent_kind: str | None = None,
    unsupported_kind: str | None = None,
) -> list[str]:
    """Fingerprint one worktree path as ``[rel, kind, sha256]``.

    Symlinks are never followed: a link contributes its kind and its link text,
    so a retargeted or dangling link is visible and a link replaced by a regular
    file holding the target's bytes cannot compare equal. A path that cannot be
    fingerprinted (including a node that is neither a regular file nor a
    symlink) raises, because silently dropping it from both captures would hide
    every later change to it. ``absent_kind`` names the kind recorded for a path
    that does not exist; without it a missing path raises too, and
    ``unsupported_kind`` does the same for a node that is neither a regular file
    nor a symlink.
    """
    try:
        mode = node.lstat().st_mode
    except FileNotFoundError as error:
        if absent_kind is None:
            raise evidence_git._ReviewGuardUnavailable(
                f"{reason_prefix}-path-unreadable", argv, error=error, path=rel
            ) from error
        return [rel, absent_kind, hashlib.sha256(b"").hexdigest()]
    except OSError as error:
        raise evidence_git._ReviewGuardUnavailable(
            f"{reason_prefix}-path-unreadable", argv, error=error, path=rel
        ) from error
    if stat.S_ISLNK(mode):
        kind = "symlink"
        try:
            data = os.fsencode(os.readlink(node))
        except OSError as error:
            raise evidence_git._ReviewGuardUnavailable(
                f"{reason_prefix}-link-unreadable", argv, error=error, path=rel
            ) from error
    elif stat.S_ISREG(mode):
        kind = "file"
        try:
            data = node.read_bytes()
        except OSError as error:
            raise evidence_git._ReviewGuardUnavailable(
                f"{reason_prefix}-path-unreadable", argv, error=error, path=rel
            ) from error
    else:
        if unsupported_kind is None:
            raise evidence_git._ReviewGuardUnavailable(
                f"{reason_prefix}-node-unsupported", argv, path=rel
            )
        return [rel, unsupported_kind, hashlib.sha256(b"").hexdigest()]
    return [rel, kind, hashlib.sha256(data).hexdigest()]


def _listed_paths(listing_text: str) -> list[str]:
    """Split ``git ls-files -z`` output into its records.

    Without ``-z`` git C-quotes any path holding a quote, a tab, a newline, or
    (under the default ``core.quotePath``) a non-ASCII byte, so ``café.md``
    arrives as ``"caf\\303\\251.md"`` and names no file on disk.
    """
    return [record for record in listing_text.split("\0") if record]


def _review_guard_skips_path(
    rel: str,
    *,
    repo_root: Path,
    plan_path: Path,
    exclude: tuple[str, ...],
) -> bool:
    """Report whether the allowlist or the plan directory covers ``rel``."""
    if any(rel == path or rel.startswith(f"{path}/") for path in exclude):
        return True
    try:
        (repo_root / rel).relative_to(plan_path)
    except ValueError:
        return False
    return True


def _fingerprint_untracked_nodes(
    untracked_text: str,
    *,
    repo_root: Path,
    plan_path: Path,
    exclude: tuple[str, ...],
    argv: Sequence[str],
    reason_prefix: str,
    skip_symlinks: bool = False,
    nested_repository: Callable[[str], str] | None = None,
) -> list[list[str]]:
    """Fingerprint each git-reported untracked path as a repository node.

    A path git reported that has since vanished fails the capture closed: git
    just listed it, so its disappearance mid-scan is a race rather than a state
    the evidence can describe. ``skip_symlinks`` drops a reported link instead,
    for a scan whose subject is what git *reads* from the path rather than the
    node itself; a link that cannot be `lstat`ed still reaches
    :func:`_fingerprint_repo_node` and fails the capture closed there.

    Git never descends into an embedded repository it does not track, and
    lists it once as ``<rel>/``. ``nested_repository`` maps that path to a
    digest of the repository's own evidence, recorded as a
    :data:`NESTED_REPOSITORY_KIND` node so a write inside it still moves the
    fingerprint; without it the directory fails the capture closed.
    """
    payload: list[list[str]] = []
    for rel in sorted(_listed_paths(untracked_text)):
        if not rel:
            continue
        if _review_guard_skips_path(
            rel, repo_root=repo_root, plan_path=plan_path, exclude=exclude
        ):
            continue
        if skip_symlinks and (repo_root / rel).is_symlink():
            continue
        if nested_repository is not None and rel.endswith("/"):
            node_rel = rel.rstrip("/")
            payload.append(
                [node_rel, NESTED_REPOSITORY_KIND, nested_repository(node_rel)]
            )
            continue
        payload.append(
            _fingerprint_repo_node(
                repo_root / rel, rel=rel, argv=argv, reason_prefix=reason_prefix
            )
        )
    return payload


def _index_flagged_paths(listing_text: str) -> list[str]:
    """Return the tracked paths whose index entry hides worktree changes.

    ``git ls-files -v`` tags a skip-worktree entry ``S`` and lowercases the tag
    of an assume-unchanged entry. Git honours both flags by never comparing the
    entry against the working tree, so a mutation to either is absent from every
    diff — including the guard's.
    """
    flagged: list[str] = []
    for line in _listed_paths(listing_text):
        if len(line) < 3 or line[1] != " ":
            continue
        tag, rel = line[0], line[2:]
        if tag == "S" or tag.islower():
            flagged.append(rel)
    return flagged


def _fingerprint_index_flagged_nodes(
    listing_text: str,
    *,
    repo_root: Path,
    plan_path: Path,
    exclude: tuple[str, ...],
    argv: Sequence[str],
) -> list[list[str]]:
    """Fingerprint the worktree content of every diff-suppressed index entry.

    A skip-worktree entry is routinely absent from the worktree (that is what
    sparse checkouts use it for), so absence is recorded as its own kind rather
    than failing the capture closed; a file reappearing at that path still moves
    the fingerprint.
    """
    payload: list[list[str]] = []
    for rel in sorted(set(_index_flagged_paths(listing_text))):
        if _review_guard_skips_path(
            rel, repo_root=repo_root, plan_path=plan_path, exclude=exclude
        ):
            continue
        payload.append(
            _fingerprint_repo_node(
                repo_root / rel,
                rel=rel,
                argv=argv,
                reason_prefix="index-flagged",
                absent_kind="absent",
            )
        )
    return payload
