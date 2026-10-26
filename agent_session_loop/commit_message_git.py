"""Commit message git."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import commit_message_diff as diff
from . import commit_message_models as models
from .git_subprocess import GIT_TIMEOUT_SECONDS, run_git_reporting

# ---------------------------------------------------------------------------
# Git surface
# ---------------------------------------------------------------------------


def _git(
    cwd: Path,
    args: Sequence[str],
    *,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run git without raising, mirroring the runner's subprocess pattern.

    ``env`` overlays the ambient environment rather than replacing it, so a
    caller can point one invocation at a throwaway ``GIT_INDEX_FILE`` without
    stripping the credentials, locale, and git configuration every other call
    inherits.
    """
    return run_git_reporting(cwd, args, stdin_text=stdin_text, env=env, timeout=timeout)


def _git_paths(
    cwd: Path,
    args: Sequence[str],
    *,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git over paths named literally, never as pathspec patterns.

    Every path this module hands git is a filename the run recorded, so
    ``app/[id]/page.tsx`` must not glob-match ``app/i/page.tsx`` and a file
    called ``:notes.md`` must not be parsed as pathspec magic. Only these
    calls carry the override: the commit itself does not, because a
    repository's hooks inherit its environment and may rely on globbing.
    """
    literal = {**(env or {}), "GIT_LITERAL_PATHSPECS": "1"}
    return _git(cwd, args, stdin_text=stdin_text, env=literal)


def find_git_root(start: Path) -> Path | None:
    """Walk upward from ``start`` for the directory holding ``.git``."""
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def has_changes(cwd: Path) -> bool:
    """True when the worktree has any staged, unstaged, or untracked change."""
    result = _git(cwd, ["status", "--porcelain"])
    return result.returncode == 0 and bool(result.stdout.strip())


def _pathspec_args(pathspec: Sequence[str] | None) -> list[str]:
    """The ``-- <paths>`` tail that narrows a git command, or nothing."""
    if not pathspec:
        return []
    return ["--", *pathspec]


def has_staged_changes(cwd: Path, pathspec: Sequence[str] | None = None) -> bool:
    """True when the index differs from HEAD, within ``pathspec`` when given.

    ``git diff --cached --quiet`` exits 1 for a difference and 0 for none;
    anything else is a git failure and must not read as staged content.
    """
    argv = ["diff", "--cached", "--quiet", *_pathspec_args(pathspec)]
    return _git_paths(cwd, argv).returncode == 1


def stage_all(cwd: Path) -> subprocess.CompletedProcess[str]:
    """Stage every change in the worktree."""
    return _git(cwd, ["add", "-A"])


def stage_paths(cwd: Path, paths: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Stage only ``paths``, leaving every other change unstaged.

    A path whose deletion is already staged (``git rm``) is in neither the
    worktree nor the index, so ``git add`` would reject it with "did not match
    any files" although there is nothing left to stage. Such paths are skipped;
    the staged deletion is still committed.
    """
    paths = [path for path in paths if not _deletion_already_staged(cwd, path)]
    if not paths:
        return subprocess.CompletedProcess(
            ["git", "add", "--"], 0, stdout="", stderr=""
        )
    return _git_paths(cwd, ["add", "--", *paths])


def _deletion_already_staged(cwd: Path, path: str) -> bool:
    target = cwd / path
    if target.exists() or target.is_symlink():
        return False
    in_index = _git_paths(cwd, ["ls-files", "--", path])
    if in_index.returncode != 0 or in_index.stdout.strip():
        return False
    in_head = _git_paths(cwd, ["ls-tree", "--name-only", "-z", "HEAD", "--", path])
    return in_head.returncode == 0 and in_head.stdout.rstrip("\0") == path


def dequalify_paths(qualified: Sequence[str]) -> list[str]:
    """Strip the ``"<repository>:"`` prefix repository evidence attaches.

    ``repository_evidence._qualified_paths`` emits ``f"{repository}:{path}"``,
    where the repository is an alias or the legacy ``"."`` single-repository
    key.  Splitting on the first colon keeps colons inside a path intact.
    """
    paths: list[str] = []
    for entry in qualified:
        _, separator, remainder = entry.partition(":")
        path = remainder if separator else entry
        if path:
            paths.append(path)
    return list(dict.fromkeys(paths))


# The captured diff is parsed by `commit_message_diff`, so its shape must not
# follow the user's git configuration: `diff.noprefix` / `diff.mnemonicPrefix`
# move the side prefixes, `color.diff=always` wraps every header in escapes, an
# external or textconv driver replaces the patch, `diff.submodule=log` drops a
# submodule's `diff --git` header, and `core.quotePath` octal-escapes every
# non-ASCII name. Names git still has to quote (a tab, a quote, a backslash)
# are decoded by the parser.
_DIFF_CONFIG = ("-c", "core.quotePath=false")


_DIFF_FORMAT = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--submodule=short",
)


def capture_staged_diff(
    cwd: Path, pathspec: Sequence[str] | None = None
) -> models.DiffResult:
    """Capture the staged diff, reduced to the prompt budget.

    ``pathspec`` narrows the capture to the paths this invocation owns, so an
    unrelated entry staged before the run never reaches the prompt.
    """
    result = _git_paths(
        cwd,
        [
            *_DIFF_CONFIG,
            "diff",
            "--cached",
            *_DIFF_FORMAT,
            *_pathspec_args(pathspec),
        ],
    )
    if result.returncode != 0:
        raise models.CommitMessageError(
            f"failed to capture staged diff: {result.stderr.strip()}",
            reason=models.REASON_DIFF_FAILED,
        )
    return diff.process_raw_diff(result.stdout)


def capture_commit_diff(sha: str, cwd: Path) -> models.DiffResult:
    """Capture an existing commit's diff, reduced to the prompt budget."""
    resolved = _git(cwd, ["rev-parse", "--verify", f"{sha}^{{commit}}"])
    if resolved.returncode != 0:
        raise models.CommitMessageError(
            f"commit not found: {sha}",
            reason=models.REASON_COMMIT_NOT_FOUND,
        )
    full_sha = resolved.stdout.strip()

    result = _git(cwd, [*_DIFF_CONFIG, "show", full_sha, "--format=", *_DIFF_FORMAT])
    if result.returncode != 0:
        raise models.CommitMessageError(
            f"failed to capture diff for commit {sha}: {result.stderr.strip()}",
            reason=models.REASON_DIFF_FAILED,
        )
    return diff.process_raw_diff(result.stdout)


def changes_are_all_binary(
    cwd: Path,
    *,
    from_id: str | None = None,
    pathspec: Sequence[str] | None = None,
) -> bool:
    """True when every changed file is binary, so no text diff can exist.

    ``git --numstat`` reports a binary file as ``-\t-\t<path>``, which is how
    an empty diff over real changes is told apart from an empty diff over no
    changes at all.  A failed or silent probe answers ``False``: the caller
    then reports the ordinary empty-diff error rather than a specific cause it
    could not actually establish.
    """
    # `--no-textconv` keeps a binary file binary here exactly as it is in the
    # captured diff, whatever textconv driver the repository configures.
    if from_id is not None:
        args = [
            "show",
            from_id,
            "--numstat",
            "--format=",
            "--no-renames",
            "--no-textconv",
        ]
    else:
        args = [
            "diff",
            "--cached",
            "--numstat",
            "--no-renames",
            "--no-textconv",
            *_pathspec_args(pathspec),
        ]
    result = _git_paths(cwd, args)
    if result.returncode != 0 or not result.stdout.strip():
        return False
    return all(
        line.startswith("-\t-\t")
        for line in result.stdout.strip().splitlines()
        if line.strip()
    )


def _staged_index_entries(cwd: Path, pathspec: Sequence[str]) -> str:
    """The ``ls-files --stage`` listing of the named paths, NUL-separated.

    This is the same listing :func:`pathspec_snapshot` digests, so what the
    scoped commit is built from and what the snapshot token attests to are
    read out of the index by the same command.
    """
    result = _git_paths(cwd, ["ls-files", "--stage", "-z", "--", *pathspec])
    if result.returncode != 0:
        raise models.CommitMessageError(
            f"failed to read the staged entries: {result.stderr.strip()}",
            reason=models.REASON_COMMIT_FAILED,
        )
    return result.stdout


def _paths_staged_for_deletion(
    cwd: Path, pathspec: Sequence[str], staged_entries: str
) -> list[str]:
    """Named paths HEAD still tracks that the index no longer holds.

    Seeding the scoped index from HEAD brings back every path HEAD carries,
    including one the run staged as a *deletion*: no ``--index-info`` entry
    exists to overwrite it, so it would silently survive into the commit.
    Removing it explicitly is what makes a scoped deletion commit as one.
    """
    if not head_sha(cwd):
        return []
    tracked = _git_paths(
        cwd, ["ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *pathspec]
    )
    if tracked.returncode != 0:
        raise models.CommitMessageError(
            f"failed to read the tracked paths: {tracked.stderr.strip()}",
            reason=models.REASON_COMMIT_FAILED,
        )
    # Each `ls-files --stage -z` record is `<mode> <sha> <stage>\t<path>\0`.
    records = (entry.partition("\t") for entry in staged_entries.split("\0"))
    staged = {path for _, tab, path in records if tab}
    return [path for path in tracked.stdout.split("\0") if path and path not in staged]


def _build_scoped_index(
    cwd: Path, pathspec: Sequence[str], env: Mapping[str, str]
) -> None:
    """Fill the ``GIT_INDEX_FILE`` in ``env`` with HEAD plus the named paths.

    Everything a scoped commit must carry already sits in the real index, so
    the throwaway index is HEAD with those entries laid over it: a named path
    the run staged takes its staged blob, one staged as a deletion is removed,
    and every unrelated path keeps whatever HEAD holds.  Writing that overlay
    into a separate file is what leaves the real index untouched.
    """
    if head_sha(cwd):
        seeded = _git(cwd, ["read-tree", "HEAD"], env=env)
        if seeded.returncode != 0:
            raise models.CommitMessageError(
                f"failed to seed the scoped index: {seeded.stderr.strip()}",
                reason=models.REASON_COMMIT_FAILED,
            )
    staged_entries = _staged_index_entries(cwd, pathspec)
    if staged_entries:
        applied = _git(
            cwd,
            ["update-index", "-z", "--index-info"],
            stdin_text=staged_entries,
            env=env,
        )
        if applied.returncode != 0:
            raise models.CommitMessageError(
                f"failed to populate the scoped index: {applied.stderr.strip()}",
                reason=models.REASON_COMMIT_FAILED,
            )
    for path in _paths_staged_for_deletion(cwd, pathspec, staged_entries):
        removed = _git_paths(
            cwd, ["update-index", "--force-remove", "--", path], env=env
        )
        if removed.returncode != 0:
            raise models.CommitMessageError(
                f"failed to stage {path} for deletion: {removed.stderr.strip()}",
                reason=models.REASON_COMMIT_FAILED,
            )


def commit_staged(
    cwd: Path,
    message: str,
    *,
    verify: bool = False,
    pathspec: Sequence[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Commit the staged index, handing the message to git on stdin.

    ``git commit -F -`` avoids both the argv length limit a long body would hit
    and the quoting hazards of a heredoc, so no message shape can change what
    git is asked to do.

    A scoped commit is built from a throwaway index and committed with no
    pathspec at all.  ``git commit --only`` would instead re-read the named
    paths from the *worktree*, which reopens the window between the caller's
    last snapshot check and the commit itself — an edit landing in that
    interval gets committed unchecked.  Sourcing the content from an index
    fixed before git is asked to commit closes that window by construction.
    The real index is never written, so an entry staged before the run is
    still neither committed nor unstaged by it.
    """
    argv = ["commit", "-F", "-"]
    if not verify:
        argv.append("--no-verify")
    # A verified commit runs the repository's own hooks, which may run a whole
    # test suite: that is the repository's budget to set, not git's, so only
    # the hook-free commit keeps the default ceiling.
    timeout = None if verify else GIT_TIMEOUT_SECONDS
    if not pathspec:
        return _git(cwd, argv, stdin_text=message, timeout=timeout)
    with tempfile.TemporaryDirectory(prefix="asl-commit-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _build_scoped_index(cwd, pathspec, env)
        return _git(cwd, argv, stdin_text=message, env=env, timeout=timeout)


def pathspec_snapshot(cwd: Path, pathspec: Sequence[str]) -> str:
    """Name the staged content a scoped commit is about to summarize.

    ``git ls-files --stage`` prints the mode, blob, stage, and path of every
    index entry under ``pathspec``, so digesting that listing yields a token
    that changes the moment any named path's staged content does and ignores
    every unrelated index entry.  It is what lets a commit prove, minutes
    later, that it is still committing what the message describes.
    """
    result = _git_paths(cwd, ["ls-files", "--stage", "--", *pathspec])
    if result.returncode != 0:
        raise models.CommitMessageError(
            f"failed to read the staged snapshot: {result.stderr.strip()}",
            reason=models.REASON_DIFF_FAILED,
        )
    digest = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    return digest[: models.SNAPSHOT_TOKEN_LENGTH]


def worktree_drifted_paths(cwd: Path, pathspec: Sequence[str]) -> list[str]:
    """The named paths whose worktree side no longer matches the index.

    A scoped commit carries the index, so drift here does not change what is
    committed — it changes whether the operator would recognize it.  Left
    unreported, the run would commit a stale blob under a message describing
    it while a newer edit sat on disk unmentioned, so drift is refused rather
    than silently outrun.  A porcelain entry drifts when its worktree column
    is not blank (edited or deleted since staging) or when it is untracked
    altogether, which is how a path staged as a deletion and then recreated is
    caught.

    A submodule is committed as its HEAD alone, so only a HEAD that moved
    since staging drifts; edited or untracked files inside it never reach the
    commit and are ignored, whatever ``submodule.<name>.ignore`` says.
    """
    result = _git_paths(
        cwd,
        [
            "status",
            "--porcelain",
            "-uall",
            "--ignore-submodules=dirty",
            *_pathspec_args(pathspec),
        ],
    )
    if result.returncode != 0:
        raise models.CommitMessageError(
            f"failed to probe the worktree: {result.stderr.strip()}",
            reason=models.REASON_DIFF_FAILED,
        )
    drifted: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        if line.startswith("??") or line[1] != " ":
            path = line[3:]
            # A rename entry reads `R  old -> new`; the new name is the one a
            # commit would carry.
            _, arrow, renamed = path.partition(" -> ")
            drifted.append(renamed if arrow else path)
    return drifted


def assert_pathspec_snapshot(cwd: Path, pathspec: Sequence[str], expected: str) -> None:
    """Refuse a scoped commit whose named paths changed after capture.

    Passing this leaves worktree, index, and the captured diff in agreement
    for every named path.  It is a report, not the guarantee: an edit landing
    after it returns would still slip past, so what actually binds the commit
    to ``expected`` is :func:`commit_staged` building its own index from those
    same entries instead of letting git re-read the worktree.
    """
    drifted = worktree_drifted_paths(cwd, pathspec)
    if drifted:
        raise models.CommitMessageError(
            "the worktree changed after the diff was captured: "
            + ", ".join(sorted(drifted)),
            reason=models.REASON_SNAPSHOT_DRIFTED,
            details={"paths": sorted(drifted), "expected_snapshot": expected},
        )
    current = pathspec_snapshot(cwd, pathspec)
    if current != expected:
        raise models.CommitMessageError(
            "the staged snapshot changed after the diff was captured "
            f"({expected} -> {current})",
            reason=models.REASON_SNAPSHOT_DRIFTED,
            details={"expected_snapshot": expected, "actual_snapshot": current},
        )


def head_sha(cwd: Path) -> str:
    """The current HEAD commit, or an empty string when there is none."""
    result = _git(cwd, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
