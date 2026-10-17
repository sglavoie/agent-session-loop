"""Git probe runners, fingerprint schema constants, and unavailable evidence."""

from __future__ import annotations

import contextvars
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_subprocess import run_git_or_raise


class RepositoryEvidenceError(RuntimeError):
    """Raised when repository inventory cannot be captured safely."""


@dataclass(frozen=True)
class GitWorktreeInventory:
    """The repositories and hidden workspace subtrees recursive inventory skipped."""

    repositories: tuple[Path, ...]
    skipped_hidden_subtrees: int


GitRunner = Callable[[Path, list[str]], subprocess.CompletedProcess[str]]


GitBytesRunner = Callable[[Path, list[str]], subprocess.CompletedProcess[bytes]]


_GIT_RUNNER: contextvars.ContextVar[GitRunner | None] = contextvars.ContextVar(
    "repository_evidence_git_runner", default=None
)


_GIT_BYTES_RUNNER: contextvars.ContextVar[GitBytesRunner | None] = (
    contextvars.ContextVar("repository_evidence_git_bytes_runner", default=None)
)


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    override = _GIT_RUNNER.get()
    if override is not None:
        return override(cwd, args)
    return run_git_or_raise(cwd, args, error=RepositoryEvidenceError)


def _run_git_bytes(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    override = _GIT_BYTES_RUNNER.get()
    if override is not None:
        return override(cwd, args)
    return run_git_or_raise(cwd, args, error=RepositoryEvidenceError, decode=False)


_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)


# Git cannot express a change of type (regular file, symlink, gitlink) as one
# patch, so it renders the old type's deletion followed by the new type's
# creation — two `diff --git` sections for the one `--name-status` entry every
# other status claims exactly one of.
_TYPE_CHANGED_STATUS = "T"


_TYPE_CHANGED_PATCH_SECTIONS = 2


# `git hash-object -t tree <devnull>` yields the repository's own empty tree id
# (sha1 and sha256 repositories disagree on its value), which is the only diff
# base that exists before the first commit.
_EMPTY_TREE_ARGV: tuple[str, ...] = ("hash-object", "-t", "tree", os.devnull)


# Carried by every guard diff probe so the evidence describes content rather
# than presentation. `--no-renames` keeps both sides of a rename in the
# evidence as one entry each: with detection on, a single `--name-status` line
# carries both paths, so the source would neither pair with its own patch nor
# reach the dirty-path list a later rereview scopes against.
# `--no-relative` keeps the `:(top)` pathspec from being re-narrowed to
# `cwd` by a `diff.relative=true` config. `--no-textconv` and `--no-ext-diff`
# keep the patch derived from the file's raw bytes: a repository can configure
# a textconv or external diff driver — in `.gitattributes` and git config, both
# of which a review turn could itself write — whose rendering is identical for
# two different contents, and git then reports no diff at all for a path whose
# bytes moved.
_GUARD_DIFF_FLAGS: tuple[str, ...] = (
    "--no-renames",
    "--no-relative",
    "--no-textconv",
    "--no-ext-diff",
)


# The exclusion file `--exclude-standard` reads on top of the worktree's
# `.gitignore` files and `.git/info/exclude`. The setting is fingerprinted under
# its own name, labelled so a pause reads as the setting rather than as a path.
# `--null` terminates the reported value with the one byte a pathname may not
# contain; a newline may not be trimmed off it, because git honours a pathname
# that ends in one.
_EXCLUDES_FILE_KEY = "core.excludesFile"


_EXCLUDES_FILE_ARGV: tuple[str, ...] = (
    "config",
    "--null",
    "--type=path",
    "--get",
    _EXCLUDES_FILE_KEY,
)


_EXCLUDES_CONFIG_REL = f"config:{_EXCLUDES_FILE_KEY}"


# Every `ignore_config` entry is `[role, rel, kind, sha256]`. The role is its
# own element rather than a prefix on `rel` because `config:` and `target:` are
# legal repository pathnames: keyed by name alone, a directly named source and a
# resolved link target claim one key in `_review_guard_changed_paths` and one of
# them is dropped, so a target-only mutation moves the fingerprint but names
# nothing. The prefixes stay on `rel`, which is what an operator reads.
_IGNORE_ROLE_CONFIG = "config"


_IGNORE_ROLE_SOURCE = "source"


_IGNORE_ROLE_TARGET = "target"


# The worktree exclusion source git reads from every directory it descends
# into. `--exclude-standard` applies an untracked one before answering, so a
# `.gitignore` whose own rules match itself is missing from the ordinary
# untracked scan; the negative command-line pattern lists it anyway, because
# git consults command-line patterns before every standard source and a
# negative match there wins. The pathspec keeps the scan to the ignore files
# themselves, so the ordinary ignored payload beside one stays out of the
# default evidence. A directory git does not descend into is still pruned, and
# a `.gitignore` inside one is a file git never applies. The negative pattern
# re-lists symlinks too, and git opens an ignore file with `O_NOFOLLOW`, so a
# linked one contributes no rules at all; the scan skips them rather than
# reporting a retargeting git never acted on.
_WORKTREE_IGNORE_FILE = ".gitignore"


_WORKTREE_IGNORE_ARGV: tuple[str, ...] = (
    "ls-files",
    "--others",
    "--full-name",
    "--exclude-standard",
    "-x",
    f"!{_WORKTREE_IGNORE_FILE}",
    "--",
    f":(top,glob)**/{_WORKTREE_IGNORE_FILE}",
)


class _ReviewGuardUnavailable(Exception):
    """A git probe the review mutation guard depends on could not be trusted.

    Raised instead of returning partial evidence so no probe failure can be
    laundered into an empty-but-valid fingerprint that compares equal across a
    review turn.
    """

    def __init__(
        self,
        reason: str,
        argv: Sequence[str],
        *,
        result: subprocess.CompletedProcess[str]
        | subprocess.CompletedProcess[bytes]
        | None = None,
        error: Exception | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(reason)
        detail: dict[str, Any] = {"reason": reason, "argv": list(argv)}
        if result is not None:
            detail["returncode"] = result.returncode
            stderr = result.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            detail["stderr"] = stderr.strip()
        if error is not None:
            detail["error"] = str(error)
        if path is not None:
            detail["path"] = path
        self.fingerprint: dict[str, Any] = {"git": True, "unavailable": detail}


def _review_guard_git(cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git probe the guard's evidence depends on, failing closed."""
    try:
        result = _run_git(cwd, argv)
    except Exception as error:
        raise _ReviewGuardUnavailable("git-unrunnable", argv, error=error) from error
    if result.returncode != 0:
        raise _ReviewGuardUnavailable("git-command-failed", argv, result=result)
    return result


def _review_guard_diff_base(cwd: Path) -> tuple[str, str]:
    """Resolve ``(head_sha, diff_base)`` for the guard's diff probes.

    Before the first commit ``HEAD`` does not resolve, so the empty tree stands
    in as the diff base and every staged or working-tree file fingerprints as an
    addition rather than disappearing from the evidence.
    """
    head_argv = ["rev-parse", "--verify", "HEAD"]
    try:
        head_result = _run_git(cwd, head_argv)
    except Exception as error:
        raise _ReviewGuardUnavailable(
            "git-unrunnable", head_argv, error=error
        ) from error
    head_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""
    if head_sha:
        return head_sha, "HEAD"
    empty_tree_argv = list(_EMPTY_TREE_ARGV)
    empty_tree = _review_guard_git(cwd, empty_tree_argv).stdout.strip()
    if not empty_tree:
        raise _ReviewGuardUnavailable("empty-tree-unresolved", empty_tree_argv)
    return "", empty_tree


def _fingerprint_refs(cwd: Path) -> list[list[str]]:
    """Capture every durable Git ref tip in a deterministic representation."""
    argv = ["for-each-ref", "--format=%(refname)%00%(objectname)"]
    text = _review_guard_git(cwd, argv).stdout
    refs: list[list[str]] = []
    for raw_line in text.splitlines():
        try:
            name, object_name = raw_line.split("\0", 1)
        except ValueError as error:
            raise _ReviewGuardUnavailable("git-ref-output-malformed", argv) from error
        if not name.startswith("refs/") or not object_name:
            raise _ReviewGuardUnavailable("git-ref-output-malformed", argv)
        refs.append([name, object_name])
    return sorted(refs)


def _literal_exclude_pathspec(rel_posix: str) -> str:
    """Build a pathspec excluding exactly ``rel_posix`` and everything under it.

    ``top`` anchors the value at the repository root and ``literal`` stops git
    from reading ``*``, ``?``, or ``[]`` as wildcards, so the tracked diff drops
    the same file-or-directory subtree :func:`_fingerprint_untracked_nodes`
    drops instead of an unrelated pattern match.
    """
    return f":(top,literal,exclude){rel_posix}"
