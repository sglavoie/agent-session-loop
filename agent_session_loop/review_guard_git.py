"""The git shell and worktree status checks behind the review guard.

``review_guard`` re-exports these names; patch them here, where callers look
them up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .git_subprocess import run_git_or_raise
from .run_errors import RunError


def _run_git(
    cwd: Path,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    """Run git, raising :class:`RunError` when it cannot be spawned.

    Deliberately not the ``repository_evidence`` runner: this function is what
    the fingerprint wrappers install as that module's override, so honoring
    the override here would call itself, and its failures must be ``RunError``.
    """
    return run_git_or_raise(cwd, args, error=RunError)


def _run_git_bytes(
    cwd: Path,
    args: list[str],
) -> subprocess.CompletedProcess[bytes]:
    """Run git without decoding, for output whose bytes must survive intact.

    Text mode translates ``\\r\\n`` and a lone ``\\r`` to ``\\n``, which collapses
    values differing only in those bytes onto one another.
    """
    return run_git_or_raise(cwd, args, error=RunError, decode=False)


def _read_git_head(cwd: Path) -> dict[str, Any]:
    result = _run_git(cwd, ["rev-parse", "--verify", "HEAD"])
    head = result.stdout.strip() if result.returncode == 0 else None
    return {
        "head": head,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _validate_git_worktree(cwd: Path) -> None:
    result = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0 or result.stdout.strip() != "true":
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            raise RunError(f"cwd is not inside a git repository: {detail}")
        raise RunError("cwd is not inside a git repository")


def _validate_clean_git_status(cwd: Path) -> None:
    result = _run_git(cwd, ["status", "--porcelain"])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            raise RunError(f"could not inspect git status: {detail}")
        raise RunError("could not inspect git status")
    if result.stdout:
        raise RunError("git status --porcelain must be empty before auto-commit")


def _git_status_is_clean(cwd: Path) -> bool:
    result = _run_git(cwd, ["status", "--porcelain"])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            raise RunError(f"could not inspect git status: {detail}")
        raise RunError("could not inspect git status")
    return not bool(result.stdout)


def _porcelain_dirty_paths(output: str) -> list[str]:
    """Name every dirty path individually from NUL-framed porcelain output.

    ``--untracked-files=all`` is what makes a newly created directory read as
    the files inside it, which is the only form the write guard ever records a
    path in; the collapsed ``dir/`` entry the default listing prints matches no
    recorded path at all. ``-z`` is what keeps a path holding a quote or a
    newline from arriving as a quoted rendering that no recorded path matches
    either. A rename or copy entry carries its source path as a second field,
    and that source is dirt of its own, so both sides are named.
    """
    fields = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            if index < len(fields) and fields[index]:
                paths.append(fields[index])
            index += 1
    return list(dict.fromkeys(paths))
