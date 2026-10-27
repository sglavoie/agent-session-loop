"""Discover, identify, create, and remove linked Git worktrees."""

from __future__ import annotations

import os
from pathlib import Path

from . import repository_evidence_git as evidence_git

#: Separates a registered repository's snapshot key from one of its linked
#: worktrees. Registered keys are workspace-relative POSIX paths, which never
#: contain this marker, so a composed key stays unambiguous.
LINKED_WORKTREE_KEY_MARKER = "::worktree::"


def linked_worktree_key(repository: str, worktree: str | Path) -> str:
    """Compose the snapshot key for one linked worktree of ``repository``."""
    return f"{repository}{LINKED_WORKTREE_KEY_MARKER}{Path(worktree).as_posix()}"


def split_linked_worktree_key(key: str) -> tuple[str, str] | None:
    """Split a linked-worktree key into its owner and worktree path.

    Returns ``None`` for an ordinary registered key, so a caller can tell the
    two apart without knowing the marker.
    """
    owner, marker, worktree = key.partition(LINKED_WORKTREE_KEY_MARKER)
    if not marker:
        return None
    return owner, worktree


def linked_worktrees(
    repository: str | Path,
    *,
    include_prunable: bool = False,
    _run_git_fn: evidence_git.GitRunner | None = None,
) -> tuple[Path, ...]:
    """Every worktree sharing ``repository``'s object store, except itself.

    A linked worktree is a second checkout of a repository the registry does
    name, so a turn can move its HEAD and index without touching any registered
    root. Enumerating them is what puts those checkouts inside the guard's field
    of view instead of leaving the run to take a handoff's word for it.

    A registration git itself marks ``prunable`` -- its directory is already
    gone -- is left out unless ``include_prunable`` asks for it: there is no
    checkout there to observe, and listing it made every turn's snapshot report
    the repository as disappeared. One a turn deletes still drops out of the
    next snapshot only, so the comparison sees it removed.
    """
    token = evidence_git._GIT_RUNNER.set(_run_git_fn)
    root = Path(os.path.realpath(os.path.abspath(os.fspath(repository))))
    try:
        result = evidence_git._run_git(root, ["worktree", "list", "--porcelain"])
    except Exception as error:
        raise evidence_git.RepositoryEvidenceError(
            f"linked worktrees for {root} could not be listed: {error}"
        ) from error
    finally:
        evidence_git._GIT_RUNNER.reset(token)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise evidence_git.RepositoryEvidenceError(
            f"linked worktrees for {root} could not be listed: "
            f"{detail or 'Git probe failed'}"
        )
    prefix = "worktree "
    listed: list[Path] = []
    # One record per worktree: it opens with its ``worktree`` line, and
    # ``prunable`` is a later line of the same record.
    records: list[tuple[Path, bool]] = []
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            path = line[len(prefix) :].strip()
            if path:
                records.append((Path(os.path.realpath(os.path.abspath(path))), False))
        elif records and (line == "prunable" or line.startswith("prunable ")):
            records[-1] = (records[-1][0], True)
    for resolved, prunable in records:
        if resolved != root and (include_prunable or not prunable):
            listed.append(resolved)
    return tuple(dict.fromkeys(listed))


def add_detached_worktree(
    repository: str | Path,
    destination: str | Path,
    commit: str,
    *,
    _run_git_fn: evidence_git.GitRunner | None = None,
) -> Path:
    """Check ``commit`` out at ``destination`` as a detached worktree.

    Detached on purpose: the checkout exists to hold one commit still, and a
    branch would make it a second place that commit could move from.
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(repository))))
    target = Path(os.path.abspath(os.fspath(destination)))
    token = evidence_git._GIT_RUNNER.set(_run_git_fn)
    try:
        result = evidence_git._run_git(
            root, ["worktree", "add", "--detach", str(target), commit]
        )
    except Exception as error:
        raise evidence_git.RepositoryEvidenceError(
            f"{commit} could not be checked out from {root}: {error}"
        ) from error
    finally:
        evidence_git._GIT_RUNNER.reset(token)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise evidence_git.RepositoryEvidenceError(
            f"{commit} could not be checked out from {root}: "
            f"{detail or 'Git refused to add the worktree'}"
        )
    return target


def remove_worktree(
    repository: str | Path,
    destination: str | Path,
    *,
    _run_git_fn: evidence_git.GitRunner | None = None,
) -> bool:
    """Drop a worktree added by :func:`add_detached_worktree`.

    Reports whether Git let go of it, because a leftover registration keeps the
    origin repository listing a checkout that is no longer there.
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(repository))))
    target = Path(os.path.abspath(os.fspath(destination)))
    token = evidence_git._GIT_RUNNER.set(_run_git_fn)
    try:
        evidence_git._run_git(root, ["worktree", "remove", "--force", str(target)])
        # Verify this registration, including when a failed add never created
        # it. Never prune unrelated worktrees as a side effect of audit cleanup.
        remaining = linked_worktrees(
            root, include_prunable=True, _run_git_fn=_run_git_fn
        )
    except Exception:  # noqa: BLE001
        return False
    finally:
        evidence_git._GIT_RUNNER.reset(token)
    return target.resolve() not in remaining
