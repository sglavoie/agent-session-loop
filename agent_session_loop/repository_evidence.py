"""Reusable fail-closed Git repository and workspace evidence.

The single-repository fingerprint schema is intentionally identical to the
historical review mutation guard. Workspace inventory layers deterministic,
symlink-safe worktree discovery and vector comparison over that schema.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
import contextvars
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any


__all__ = [
    "RepositoryEvidenceError",
    "RepositoryFingerprint",
    "WorkspaceSnapshot",
    "capture_registered_snapshot",
    "capture_repository_fingerprint",
    "capture_workspace_snapshot",
    "compare_repository_fingerprints",
    "compare_workspace_snapshots",
    "discover_git_worktrees",
    "format_repository_changes",
    "repository_evidence_unavailable",
    "repository_state",
]


class RepositoryEvidenceError(RuntimeError):
    """Raised when repository inventory cannot be captured safely."""


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
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as error:
        raise RepositoryEvidenceError("git executable not found on PATH") from error
    except OSError as error:
        raise RepositoryEvidenceError(
            f"could not run git {' '.join(args)}: {error}"
        ) from error


def _run_git_bytes(
    cwd: Path, args: list[str]
) -> subprocess.CompletedProcess[bytes]:
    override = _GIT_BYTES_RUNNER.get()
    if override is not None:
        return override(cwd, args)
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as error:
        raise RepositoryEvidenceError("git executable not found on PATH") from error
    except OSError as error:
        raise RepositoryEvidenceError(
            f"could not run git {' '.join(args)}: {error}"
        ) from error


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


def _review_guard_git(
    cwd: Path, argv: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run a git probe the guard's evidence depends on, failing closed."""
    try:
        result = _run_git(cwd, argv)
    except Exception as error:
        raise _ReviewGuardUnavailable(
            "git-unrunnable", argv, error=error
        ) from error
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


def _literal_exclude_pathspec(rel_posix: str) -> str:
    """Build a pathspec excluding exactly ``rel_posix`` and everything under it.

    ``top`` anchors the value at the repository root and ``literal`` stops git
    from reading ``*``, ``?``, or ``[]`` as wildcards, so the tracked diff drops
    the same file-or-directory subtree :func:`_fingerprint_untracked_nodes`
    drops instead of an unrelated pattern match.
    """
    return f":(top,literal,exclude){rel_posix}"


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
            raise _ReviewGuardUnavailable(
                f"{reason_prefix}-path-unreadable", argv, error=error, path=rel
            ) from error
        return [rel, absent_kind, hashlib.sha256(b"").hexdigest()]
    except OSError as error:
        raise _ReviewGuardUnavailable(
            f"{reason_prefix}-path-unreadable", argv, error=error, path=rel
        ) from error
    if stat.S_ISLNK(mode):
        kind = "symlink"
        try:
            data = os.fsencode(os.readlink(node))
        except OSError as error:
            raise _ReviewGuardUnavailable(
                f"{reason_prefix}-link-unreadable", argv, error=error, path=rel
            ) from error
    elif stat.S_ISREG(mode):
        kind = "file"
        try:
            data = node.read_bytes()
        except OSError as error:
            raise _ReviewGuardUnavailable(
                f"{reason_prefix}-path-unreadable", argv, error=error, path=rel
            ) from error
    else:
        if unsupported_kind is None:
            raise _ReviewGuardUnavailable(
                f"{reason_prefix}-node-unsupported", argv, path=rel
            )
        return [rel, unsupported_kind, hashlib.sha256(b"").hexdigest()]
    return [rel, kind, hashlib.sha256(data).hexdigest()]


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
) -> list[list[str]]:
    """Fingerprint each git-reported untracked path as a repository node.

    A path git reported that has since vanished fails the capture closed: git
    just listed it, so its disappearance mid-scan is a race rather than a state
    the evidence can describe. ``skip_symlinks`` drops a reported link instead,
    for a scan whose subject is what git *reads* from the path rather than the
    node itself; a link that cannot be `lstat`ed still reaches
    :func:`_fingerprint_repo_node` and fails the capture closed there.
    """
    payload: list[list[str]] = []
    for rel in sorted(untracked_text.splitlines()):
        if not rel:
            continue
        if _review_guard_skips_path(
            rel, repo_root=repo_root, plan_path=plan_path, exclude=exclude
        ):
            continue
        if skip_symlinks and (repo_root / rel).is_symlink():
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
    for line in listing_text.splitlines():
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


def _ignore_source_rel(node: Path, repo_root: Path) -> str:
    """Name an exclusion source repo-root-relatively, or absolutely if outside.

    A linked worktree maps ``info/exclude`` to the common directory and
    ``core.excludesFile`` usually names a file in the operator's home, so a
    source outside the repository keeps its absolute path.
    """
    try:
        return node.relative_to(repo_root).as_posix()
    except ValueError:
        return node.as_posix()


def _fingerprint_ignore_target(
    link: Path,
    *,
    repo_root: Path,
    argv: Sequence[str],
    reason_prefix: str,
) -> list[str]:
    """Fingerprint the file git actually reads through a symlinked source.

    The link node alone carries only its link text, so a turn could rewrite the
    target's rules through a link that never moved. The ``target:`` label says
    which file the entry names; the identity that keeps it apart from a source
    entry over the same pathname is the role element (see
    :data:`_IGNORE_ROLE_TARGET`).

    Resolution is strict: a symlink cycle (``ELOOP``) and every other resolution
    failure fail the guard closed, because git may still read rules the evidence
    then cannot describe. A dangling link is not a failure — its target
    records ``absent``, so creating that file later still moves the
    fingerprint — but a target that is neither a regular file nor a symlink
    fails closed rather than recording its kind alone, since a link is never the
    way ``core.excludesFile=/dev/null`` switches the global excludes off.
    """
    try:
        resolved = Path(os.path.realpath(link, strict=True))
    except FileNotFoundError:
        resolved = Path(os.path.realpath(link))
    except OSError as error:
        raise _ReviewGuardUnavailable(
            f"{reason_prefix}-target-unresolved",
            argv,
            error=error,
            path=str(link),
        ) from error
    return [
        _IGNORE_ROLE_TARGET,
        *_fingerprint_repo_node(
            resolved,
            rel=f"target:{_ignore_source_rel(resolved, repo_root)}",
            argv=argv,
            reason_prefix=f"{reason_prefix}-target",
            absent_kind="absent",
        ),
    ]


def _fingerprint_ignore_source(
    named: Path,
    *,
    repo_root: Path,
    argv: Sequence[str],
    reason_prefix: str,
) -> list[list[str]]:
    """Fingerprint one exclusion file that no worktree scan reaches.

    Only the parent is resolved, never the final component, so a symlinked
    exclusion file stays a ``symlink`` whose link text is its own evidence and a
    retargeted or replaced link is visible. Git follows that link, though, so a
    symlinked source contributes a second entry over the fully resolved file git
    reads (see :func:`_fingerprint_ignore_target`) — without it a turn could
    rewrite the target's rules through a link that never moved.

    The file legitimately may not exist; ``absent`` is its own kind so creating
    it later still moves the fingerprint. A node that is neither a regular file
    nor a symlink records its kind alone rather than failing the guard closed:
    ``core.excludesFile=/dev/null`` is a common way to switch the global
    excludes off, and no rules can hide behind it.
    """
    try:
        node = named.parent.resolve() / named.name
    except OSError as error:
        raise _ReviewGuardUnavailable(
            f"{reason_prefix}-unresolved", argv, error=error, path=str(named)
        ) from error
    entry = [
        _IGNORE_ROLE_SOURCE,
        *_fingerprint_repo_node(
            node,
            rel=_ignore_source_rel(node, repo_root),
            argv=argv,
            reason_prefix=reason_prefix,
            absent_kind="absent",
            unsupported_kind="unsupported",
        ),
    ]
    if entry[2] != "symlink":
        return [entry]
    return [
        entry,
        _fingerprint_ignore_target(
            node, repo_root=repo_root, argv=argv, reason_prefix=reason_prefix
        ),
    ]


def _default_excludes_file() -> Path | None:
    """Return the excludes file git reads when ``core.excludesFile`` is unset.

    Git falls back to ``$XDG_CONFIG_HOME/git/ignore``, or to
    ``$HOME/.config/git/ignore`` when that variable is unset or empty, and reads
    no global excludes when neither variable is set. No plumbing command reports
    the resolved fallback, so the lookup is mirrored here against the same
    environment the git subprocess inherits.
    """
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "git" / "ignore"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".config" / "git" / "ignore"
    return None


def _effective_excludes_file(
    cwd: Path, repo_root: Path
) -> tuple[str, Path | None]:
    """Resolve ``core.excludesFile`` into a marker and the file git reads.

    The marker carries the configured value itself, so repointing the setting
    moves the evidence under the setting's own name rather than only under the
    two paths it moved between, and an explicitly empty value (which selects no
    file at all) stays distinguishable from an unset one.

    ``--get`` reports the merged value the way git resolves it — the last one
    wins across the system, global, and repository files — and ``--type=path``
    expands ``~`` as git does. The value is read as raw bytes up to the ``--null``
    terminator rather than trimmed of a trailing newline: a newline is legal
    inside a pathname and git honours such a value, so trimming it would let a
    turn repoint the setting at a near-identical name — and at the different file
    that name selects — while the evidence compared equal. A relative value is
    joined against the repository root because git chdirs to the top level before
    reading it, which also keeps the evidence identical from a nested ``cwd``.
    """
    argv = list(_EXCLUDES_FILE_ARGV)
    try:
        result = _run_git_bytes(cwd, argv)
    except Exception as error:
        raise _ReviewGuardUnavailable(
            "git-unrunnable", argv, error=error
        ) from error
    if result.returncode == 1:
        return "unset", _default_excludes_file()
    if result.returncode != 0:
        raise _ReviewGuardUnavailable("git-command-failed", argv, result=result)
    terminator = result.stdout.find(b"\0")
    if terminator < 0:
        raise _ReviewGuardUnavailable(
            "excludes-file-unterminated", argv, result=result
        )
    value = os.fsdecode(result.stdout[:terminator])
    if not value:
        return "set:", None
    return f"set:{value}", repo_root / value


def _fingerprint_ignore_config(cwd: Path, repo_root: Path) -> list[list[str]]:
    """Fingerprint the ignore rules that no other guard probe covers.

    ``--exclude-standard`` honours three exclusion sources. The worktree's
    ``.gitignore`` files are covered already — tracked ones by the tracked diff,
    untracked ones by the untracked scan — but ``.git/info/exclude`` and the
    file ``core.excludesFile`` selects both live outside the worktree, so a turn
    could otherwise hide a mutation by ignoring the path it wrote. The
    ``core.excludesFile`` setting is fingerprinted beside the file it names,
    because repointing it swaps the active rules without editing either file.
    Either source may be a symlink, which git follows, so a symlinked one
    contributes a second ``target:`` entry over the file git actually reads.
    Each entry leads with its role (see :data:`_IGNORE_ROLE_SOURCE`), so the
    marker, a named source, and a resolved target stay distinct even when one
    repository pathname spells another's label.

    ``--git-path`` follows the gitdir indirection a linked worktree or submodule
    uses (where ``.git`` is a file, not a directory) and answers relative to the
    directory git ran in, so the result is joined against that directory rather
    than the repository root.
    """
    argv = ["rev-parse", "--git-path", "info/exclude"]
    reported = _review_guard_git(cwd, argv).stdout.strip()
    if not reported:
        raise _ReviewGuardUnavailable("ignore-config-unresolved", argv)
    payload = _fingerprint_ignore_source(
        cwd / reported,
        repo_root=repo_root,
        argv=argv,
        reason_prefix="ignore-config",
    )
    marker, excludes_file = _effective_excludes_file(cwd, repo_root)
    payload.append(
        [
            _IGNORE_ROLE_CONFIG,
            _EXCLUDES_CONFIG_REL,
            "config",
            # `os.fsencode`, not a plain utf-8 encode: a pathname git reported
            # as undecodable bytes reaches here as surrogate escapes.
            hashlib.sha256(os.fsencode(marker)).hexdigest(),
        ]
    )
    if excludes_file is not None:
        payload.extend(
            _fingerprint_ignore_source(
                excludes_file,
                repo_root=repo_root,
                argv=list(_EXCLUDES_FILE_ARGV),
                reason_prefix="excludes-file",
            )
        )
    return payload


def _pair_diff_names_with_patches(
    names_text: str, diff_text: str, *, argv: Sequence[str], reason: str
) -> tuple[list[str], list[list[str]]]:
    """Attribute each patch in ``diff_text`` to its path in ``names_text``.

    ``names_text`` is ``--name-status`` output, which walks the same diff queue
    in the same order as the full patch. Its status is what says how many patch
    sections the path claims, so consuming that many per entry attributes each
    patch without re-parsing ``diff --git`` headers, whose two paths cannot be
    told apart when one of them contains a space. ``--no-renames`` is what keeps
    every entry to a single ``<status>\\t<path>`` line.

    A typechange is the one status claiming two sections
    (:data:`_TYPE_CHANGED_PATCH_SECTIONS`); both are hashed together, so the
    pair is that path's content evidence. Pairing one section per entry instead
    would misalign every remaining path in the queue.

    A queue that does not add up fails the capture closed rather than pairing
    short and dropping its tail: a path with no patch evidence carries no
    content in the fingerprint, so any two of its contents would compare equal.
    """
    changed: list[str] = []
    section_counts: list[int] = []
    for line in names_text.splitlines():
        if not line:
            continue
        status, tab, rel = line.partition("\t")
        if not tab or not status or not rel:
            raise _ReviewGuardUnavailable(reason, argv)
        changed.append(rel)
        section_counts.append(
            _TYPE_CHANGED_PATCH_SECTIONS
            if status == _TYPE_CHANGED_STATUS
            else 1
        )
    patches = _DIFF_FILE_HEADER_RE.split(diff_text)[1:]
    if len(patches) != sum(section_counts):
        raise _ReviewGuardUnavailable(reason, argv)
    paired: list[list[str]] = []
    cursor = 0
    for rel, count in zip(changed, section_counts):
        section = "".join(patches[cursor : cursor + count])
        cursor += count
        paired.append(
            [rel, hashlib.sha256(section.encode("utf-8")).hexdigest()]
        )
    return changed, paired


def _capture_review_repo_fingerprint(
    cwd: str | Path,
    plan_dir: str | Path,
    *,
    exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
) -> dict[str, Any]:
    """Snapshot repo content under `cwd` while excluding paths under `plan_dir`.

    Returns ``{"git": False}`` when git reports `cwd` is not inside a
    repository — the review mutation guard treats that case as "no guard
    applies". Returns ``{"git": True, "unavailable": {...}}`` when a probe the
    evidence depends on failed; :func:`_review_guard_unavailable_detail`
    recognizes that shape so the runner can fail closed instead of comparing
    fingerprints that cannot see a mutation. Otherwise returns
    ``{"git": True, "head": <sha or "">, "diff_sha256": <sha>,
    "tracked_changed": [rel, ...], "tracked_patches": [[rel, patch_sha256], ...],
    "index_diff_sha256": <sha>, "index_changed": [rel, ...],
    "index_patches": [[rel, patch_sha256], ...],
    "untracked": [[rel, kind, sha256], ...],
    "index_flagged": [[rel, kind, sha256], ...],
    "worktree_ignore": [[rel, kind, sha256], ...],
    "ignore_config": [[role, rel, kind, sha256], ...]}``
    where ``diff_sha256`` covers
    ``git diff HEAD`` (the working tree against the base, against the empty tree
    when HEAD is unborn, with rename detection off so both sides of a rename are
    listed), ``index_diff_sha256`` covers the same diff ``--cached``, the tracked
    and index keys carry the per-path evidence
    :func:`_review_guard_changed_paths` reports on, and ``untracked`` enumerates
    every untracked path outside `plan_dir` as a repository node (``kind`` is
    ``file`` over its bytes or ``symlink`` over its link text; see
    :func:`_fingerprint_untracked_nodes`).

    The index is probed separately because ``git diff <base>`` reads the working
    tree: staging a mutation and then restoring the file to its base content
    leaves that diff empty while the index still holds the staged bytes a commit
    would publish.

    Both diffs render the raw file content (see :data:`_GUARD_DIFF_FLAGS`), so a
    configured textconv or external diff driver cannot decide what the guard
    sees; a path git names but renders no patch for makes the capture
    unavailable rather than entering the evidence with no content at all.

    Tracked entries marked assume-unchanged or skip-worktree are fingerprinted
    from their raw worktree content under ``index_flagged``, because git honours
    those flags by never comparing the entry against the working tree — so
    neither diff above can see a mutation to one (``kind`` is ``absent`` for a
    flagged entry with no worktree file, which is the ordinary skip-worktree
    state).

    Every probe is anchored at the repository root rather than at `cwd`, so a
    run whose `cwd` is a nested directory still fingerprints the whole
    repository and still reports repo-root-relative paths.

    Gitignored files are left out of the untracked scan by default: the
    repository's ignore rules are the operator's existing declaration of what is
    not the code under review, and the caches they name (byte-compiled output,
    test and lint caches, harness-rewritten settings) churn on their own during
    a review turn (commit ``bf421f9`` gitignored the harness's own stowed
    settings for exactly that reason). ``include_ignored`` — the run flag
    ``--review-guard-include-ignored`` — restores the strict scan for a
    repository whose ignored tree is expected to stay still, and is off by
    default so the strict behavior is opt-in everywhere, direct calls included.

    Deferring to those rules would otherwise let a turn write the rule that
    blinds the guard, so ``worktree_ignore`` fingerprints every untracked
    ``.gitignore`` git can apply — including one hidden by its own rules —
    under either mode, without putting the ignored payload beside it back into
    the evidence (see :data:`_WORKTREE_IGNORE_ARGV`).

    ``ignore_config`` fingerprints the exclusion sources that live outside the
    worktree under either mode — ``.git/info/exclude``, the ``core.excludesFile``
    setting, and the file it selects — because no scan above can see them and a
    turn could otherwise hide a mutation by ignoring the path it wrote (see
    :func:`_fingerprint_ignore_config`). Its entries lead with a ``role``,
    unlike the three keys above, because their names share one space with the
    synthetic labels that describe them.

    ``exclude`` holds repo-relative POSIX paths (files or directories) the guard
    ignores entirely, on top of `plan_dir`. Each value drops exactly that path
    and its subtree from both the tracked and the untracked evidence; ``*``,
    ``?``, and ``[]`` are matched literally rather than as git wildcards. It is
    how a *tracked* path, or an untracked one git does not ignore, leaves the
    evidence.
    """
    cwd_path = Path(cwd)
    try:
        plan_path = Path(plan_dir).resolve()
    except OSError:
        plan_path = Path(plan_dir)

    root_argv = ["rev-parse", "--show-toplevel"]
    try:
        repo_root_result = _run_git(cwd_path, root_argv)
    except Exception as error:
        return _ReviewGuardUnavailable(
            "git-unrunnable", root_argv, error=error
        ).fingerprint
    if repo_root_result.returncode != 0:
        return {"git": False}

    try:
        try:
            repo_root = Path(repo_root_result.stdout.strip()).resolve()
        except OSError as error:
            raise _ReviewGuardUnavailable(
                "repo-root-unresolved", root_argv, error=error
            ) from error

        head_sha, diff_base = _review_guard_diff_base(cwd_path)

        # ``:(top)`` anchors the positive pathspec at the repository root, so a
        # nested ``cwd`` still fingerprints the whole repository instead of one
        # subtree, and the reported paths stay repo-root-relative so they line
        # up with the untracked scan and the allowlist.
        pathspecs = [":(top)"]
        try:
            rel_plan = plan_path.relative_to(repo_root)
            pathspecs.append(_literal_exclude_pathspec(rel_plan.as_posix()))
        except ValueError:
            pass
        pathspecs.extend(_literal_exclude_pathspec(path) for path in exclude)

        diff_argv = ["diff", diff_base, *_GUARD_DIFF_FLAGS, "--", *pathspecs]
        diff_text = _review_guard_git(cwd_path, diff_argv).stdout
        names_argv = [
            "diff",
            diff_base,
            "--name-status",
            *_GUARD_DIFF_FLAGS,
            "--",
            *pathspecs,
        ]
        names_text = _review_guard_git(cwd_path, names_argv).stdout
        index_diff_argv = [
            "diff",
            "--cached",
            diff_base,
            *_GUARD_DIFF_FLAGS,
            "--",
            *pathspecs,
        ]
        index_diff_text = _review_guard_git(cwd_path, index_diff_argv).stdout
        index_names_argv = [
            "diff",
            "--cached",
            diff_base,
            "--name-status",
            *_GUARD_DIFF_FLAGS,
            "--",
            *pathspecs,
        ]
        index_names_text = _review_guard_git(cwd_path, index_names_argv).stdout
        # ``--full-name`` keeps the reported paths repo-root-relative when
        # ``cwd`` is a subdirectory, so they line up with the repo-root-relative
        # allowlist and with the ``repo_root / rel`` lookup below; the
        # ``:(top)`` pathspec is what widens the scan itself past that
        # subdirectory. ``--exclude-standard`` defers to git's ignore rules,
        # which are the operator's existing declaration of what is not the code
        # under review: commit bf421f9 gitignored the harness's own stowed
        # settings precisely because it rewrites them mid-turn, so a scan that
        # ignored that declaration would revert essentially every review turn.
        untracked_argv = [
            "ls-files",
            "--others",
            "--full-name",
            *(() if include_ignored else ("--exclude-standard",)),
            "--",
            ":(top)",
        ]
        untracked_text = _review_guard_git(cwd_path, untracked_argv).stdout
        untracked_payload = _fingerprint_untracked_nodes(
            untracked_text,
            repo_root=repo_root,
            plan_path=plan_path,
            exclude=exclude,
            argv=untracked_argv,
            reason_prefix="untracked",
        )
        # The scan above defers to git's ignore rules, and git applies an
        # untracked `.gitignore` before answering it — so a turn can write one
        # that hides both itself and the file beside it. Every worktree ignore
        # source git can apply is fingerprinted here instead, under either mode
        # (see :data:`_WORKTREE_IGNORE_ARGV`); the strict scan merely lists the
        # same files a second time.
        worktree_ignore_argv = list(_WORKTREE_IGNORE_ARGV)
        worktree_ignore_text = _review_guard_git(
            cwd_path, worktree_ignore_argv
        ).stdout
        worktree_ignore_payload = _fingerprint_untracked_nodes(
            worktree_ignore_text,
            repo_root=repo_root,
            plan_path=plan_path,
            exclude=exclude,
            argv=worktree_ignore_argv,
            reason_prefix="worktree-ignore",
            skip_symlinks=True,
        )
        # Git never compares an index entry marked assume-unchanged or
        # skip-worktree against the working tree, so every diff above is blind
        # to a mutation of one. Those entries are fingerprinted from their raw
        # worktree content instead, which is the only evidence that moves.
        index_flags_argv = [
            "ls-files",
            "-v",
            "--full-name",
            "--",
            ":(top)",
        ]
        index_flags_text = _review_guard_git(cwd_path, index_flags_argv).stdout
        index_flagged_payload = _fingerprint_index_flagged_nodes(
            index_flags_text,
            repo_root=repo_root,
            plan_path=plan_path,
            exclude=exclude,
            argv=index_flags_argv,
        )
        ignore_config_payload = _fingerprint_ignore_config(cwd_path, repo_root)

        tracked_changed, tracked_patches = _pair_diff_names_with_patches(
            names_text,
            diff_text,
            argv=diff_argv,
            reason="tracked-patch-missing",
        )
        index_changed, index_patches = _pair_diff_names_with_patches(
            index_names_text,
            index_diff_text,
            argv=index_diff_argv,
            reason="index-patch-missing",
        )
    except _ReviewGuardUnavailable as unavailable:
        return unavailable.fingerprint

    return {
        "git": True,
        "head": head_sha,
        "diff_sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "tracked_changed": sorted(tracked_changed),
        "tracked_patches": tracked_patches,
        "index_diff_sha256": hashlib.sha256(
            index_diff_text.encode("utf-8")
        ).hexdigest(),
        "index_changed": sorted(index_changed),
        "index_patches": index_patches,
        "untracked": untracked_payload,
        "index_flagged": index_flagged_payload,
        "worktree_ignore": worktree_ignore_payload,
        "ignore_config": ignore_config_payload,
    }


def _review_guard_unavailable_detail(
    fingerprint: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return why a fingerprint carries no usable evidence, or ``None``.

    ``{"git": False}`` is not unavailable: git answered that `cwd` is outside a
    repository, so the guard legitimately does not apply.
    """
    if not fingerprint.get("git"):
        return None
    detail = fingerprint.get("unavailable")
    if isinstance(detail, Mapping):
        return dict(detail)
    return None


def _review_repo_state(fingerprint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Derive the reviewed repo state from an already-captured fingerprint.

    Pure over the fingerprint dict, so recording the base ref of a review turn
    costs no extra git calls. Returns ``None`` when the fingerprint carries no
    usable commit (not a git repo, or an unborn HEAD), which downstream delta
    scoping treats as "no base ref recorded".

    ``dirty_paths`` names every path whose reviewed content differed from
    ``head`` (working-tree and index changes plus untracked files), plus every
    diff-suppressed index entry, whose relationship to ``head`` no diff can
    establish either way. Delta scoping needs those exact paths, not just the
    Boolean: a repair that restores one of them to its committed content, or
    deletes one of the untracked ones, leaves no trace in a later
    ``git diff <head>``.
    """
    if not fingerprint.get("git"):
        return None
    head = fingerprint.get("head")
    if not isinstance(head, str) or not head:
        return None
    dirty_paths = sorted(
        {str(rel) for rel in fingerprint.get("tracked_changed") or ()}
        | {str(rel) for rel in fingerprint.get("index_changed") or ()}
        | {str(entry[0]) for entry in fingerprint.get("untracked") or ()}
        | {str(entry[0]) for entry in fingerprint.get("index_flagged") or ()}
    )
    return {
        "head": head,
        "worktree_dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
    }


def _review_guard_changed_paths(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Name what moved between two review fingerprints.

    Pure over the two fingerprint dicts so the guard can report *which* paths
    tripped it instead of only that some sha differed. A staged-only mutation
    moves the index evidence while the working-tree diff stays put, and a
    mutation to a diff-suppressed index entry moves only its raw content
    fingerprint, so all three feed the reported ``tracked`` set.

    The ignore sources get their own keys rather than being folded into the
    untracked sets — ``worktree_ignore`` for the untracked ``.gitignore`` files
    git applies, which the default untracked scan may not list at all, and
    ``ignore_config`` for ``.git/info/exclude``, the ``core.excludesFile``
    setting, and the file it selects, which live outside the worktree the other
    keys describe. :func:`_review_guard_changed_summary` names both, so a pause
    reads as the ignore source that moved rather than as an unexplained
    mismatch.
    """

    def tracked_map(
        fingerprint: Mapping[str, Any], changed_key: str, patches_key: str
    ) -> dict[str, str]:
        paths: dict[str, str] = {
            str(rel): "" for rel in fingerprint.get(changed_key) or ()
        }
        for entry in fingerprint.get(patches_key) or ():
            rel, sha = entry
            paths[str(rel)] = str(sha)
        return paths

    def moved(
        before_map: Mapping[str, str], after_map: Mapping[str, str]
    ) -> set[str]:
        return {
            path
            for path in set(before_map) | set(after_map)
            if before_map.get(path) != after_map.get(path)
        }

    def node_map(fingerprint: Mapping[str, Any], key: str) -> dict[str, str]:
        # Node kind is part of the identity: a symlink replaced by a regular
        # file holding the target's bytes must not compare equal.
        return {
            str(rel): f"{kind}:{sha}"
            for rel, kind, sha in (fingerprint.get(key) or ())
        }

    def ignore_map(
        fingerprint: Mapping[str, Any],
    ) -> dict[tuple[str, str], str]:
        # Keyed by role *and* name, because `config:` and `target:` are legal
        # repository pathnames: under a name-only key a directly named source
        # overwrites the marker or the resolved target that spells the same
        # string, and the entry it displaced leaves no evidence at all.
        return {
            (str(role), str(rel)): f"{kind}:{sha}"
            for role, rel, kind, sha in (fingerprint.get("ignore_config") or ())
        }

    untracked_before = node_map(before, "untracked")
    untracked_after = node_map(after, "untracked")
    ignore_before = ignore_map(before)
    ignore_after = ignore_map(after)

    return {
        "tracked": sorted(
            moved(
                tracked_map(before, "tracked_changed", "tracked_patches"),
                tracked_map(after, "tracked_changed", "tracked_patches"),
            )
            | moved(
                tracked_map(before, "index_changed", "index_patches"),
                tracked_map(after, "index_changed", "index_patches"),
            )
            | moved(
                node_map(before, "index_flagged"),
                node_map(after, "index_flagged"),
            )
        ),
        "untracked_added": sorted(set(untracked_after) - set(untracked_before)),
        "untracked_removed": sorted(set(untracked_before) - set(untracked_after)),
        "untracked_changed": sorted(
            path
            for path in set(untracked_before) & set(untracked_after)
            if untracked_before[path] != untracked_after[path]
        ),
        "worktree_ignore": sorted(
            moved(
                node_map(before, "worktree_ignore"),
                node_map(after, "worktree_ignore"),
            )
        ),
        "ignore_config": sorted(
            {
                rel
                for role, rel in set(ignore_before) | set(ignore_after)
                if ignore_before.get((role, rel))
                != ignore_after.get((role, rel))
            }
        ),
        "head_changed": before.get("head") != after.get("head"),
    }


def _review_guard_changed_summary(changed_paths: Mapping[str, Any]) -> str:
    """Render up to three changed paths as a suffix for the pause message."""
    named: list[str] = []
    for key in (
        "tracked",
        "untracked_added",
        "untracked_changed",
        "untracked_removed",
        "worktree_ignore",
        "ignore_config",
    ):
        named.extend(str(path) for path in changed_paths.get(key) or ())
    # An untracked `.gitignore` is reported under both its own key and the
    # untracked sets whenever the strict scan lists it too.
    named = list(dict.fromkeys(named))
    if not named:
        return ""
    suffix = f": {', '.join(named[:3])}"
    if len(named) > 3:
        suffix += f" (+{len(named) - 3} more)"
    return suffix



RepositoryFingerprint = dict[str, Any]
WorkspaceSnapshot = OrderedDict[str, RepositoryFingerprint]


def capture_repository_fingerprint(
    cwd: str | Path,
    plan_dir: str | Path,
    *,
    exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
    _run_git_fn: GitRunner | None = None,
    _run_git_bytes_fn: GitBytesRunner | None = None,
) -> RepositoryFingerprint:
    """Capture one repository using the stable review-guard payload schema.

    The private runner hooks let the legacy runner wrappers preserve their
    monkeypatch seam during migration. New callers should leave them unset.
    """
    git_token = _GIT_RUNNER.set(_run_git_fn)
    bytes_token = _GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        return _capture_review_repo_fingerprint(
            cwd,
            plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    finally:
        _GIT_BYTES_RUNNER.reset(bytes_token)
        _GIT_RUNNER.reset(git_token)


def repository_evidence_unavailable(
    fingerprint: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return fail-closed probe details from one repository fingerprint."""
    return _review_guard_unavailable_detail(fingerprint)


def default_excludes_file() -> Path | None:
    """Return Git's default global excludes path, when one is configured."""
    return _default_excludes_file()


def effective_excludes_file(
    cwd: Path,
    repo_root: Path,
    *,
    _run_git_bytes_fn: GitBytesRunner | None = None,
) -> tuple[str, Path | None]:
    """Resolve Git's effective global excludes marker and source path."""
    token = _GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        return _effective_excludes_file(cwd, repo_root)
    finally:
        _GIT_BYTES_RUNNER.reset(token)


def repository_state(
    fingerprint: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return HEAD and dirty-path state derived without further Git probes."""
    return _review_repo_state(fingerprint)


def index_flagged_paths(listing_text: str) -> list[str]:
    """Return paths hidden from worktree diffs by Git index flags."""
    return _index_flagged_paths(listing_text)


def compare_repository_fingerprints(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two single-repository evidence payloads."""
    return _review_guard_changed_paths(before, after)


def format_repository_changes(changed_paths: Mapping[str, Any]) -> str:
    """Render the bounded historical changed-path suffix."""
    return _review_guard_changed_summary(changed_paths)


def _workspace_relative(root: Path, repository: Path) -> str:
    relative = repository.relative_to(root)
    return relative.as_posix() if relative.parts else "."


def _qualified_paths(repository: str, changes: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in (
        "tracked",
        "untracked_added",
        "untracked_changed",
        "untracked_removed",
        "worktree_ignore",
        "ignore_config",
    ):
        paths.extend(
            f"{repository}:{path}" for path in changes.get(key) or ()
        )
    return list(dict.fromkeys(paths))


def _inventory_candidates(
    workspace_root: Path,
    inventory_excludes: tuple[str, ...] = (),
) -> tuple[list[Path], list[OSError]]:
    candidates: list[Path] = []
    errors: list[OSError] = []
    excluded = set(inventory_excludes)

    def record_error(error: OSError) -> None:
        errors.append(error)

    for current_text, directory_names, file_names in os.walk(
        workspace_root, topdown=True, onerror=record_error, followlinks=False
    ):
        current = Path(current_text)
        marker = current / ".git"
        marker_present = False
        if ".git" in directory_names or ".git" in file_names:
            try:
                marker_mode = marker.lstat().st_mode
            except OSError as error:
                errors.append(error)
            else:
                marker_present = stat.S_ISDIR(marker_mode) or stat.S_ISREG(
                    marker_mode
                )
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name != ".git"
            and _workspace_relative(root=workspace_root, repository=current / name)
            not in excluded
            and not (current / name).is_symlink()
        )
        if marker_present:
            # A .git file is the linked-worktree form; a directory is an
            # ordinary worktree. Its internals were pruned above.
            candidates.append(current)
    return candidates, errors


def discover_git_worktrees(
    workspace_root: str | Path,
    *,
    inventory_excludes: tuple[str, ...] = (),
    max_repositories: int = 1024,
) -> tuple[Path, ...]:
    """Recursively find exact Git worktree roots without following symlinks.

    The explicit repository bound prevents an accidentally broad workspace
    from producing unbounded Git subprocess work.
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
    if not root.is_dir():
        raise RepositoryEvidenceError(
            f"workspace root does not exist or is not a directory: {root}"
        )
    if max_repositories < 1:
        raise ValueError("max_repositories must be positive")
    candidates, errors = _inventory_candidates(root, inventory_excludes)
    if errors:
        raise RepositoryEvidenceError(
            f"workspace inventory unreadable: {errors[0]}"
        )
    if len(candidates) > max_repositories:
        raise RepositoryEvidenceError(
            "workspace repository inventory exceeds "
            f"the {max_repositories}-repository bound"
        )

    repositories: list[Path] = []
    for candidate in sorted(
        candidates, key=lambda path: _workspace_relative(root, path)
    ):
        argv = ["rev-parse", "--show-toplevel"]
        try:
            result = _run_git(candidate, argv)
        except Exception as error:
            raise RepositoryEvidenceError(
                f"repository {_workspace_relative(root, candidate)} "
                f"could not be inspected: {error}"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RepositoryEvidenceError(
                f"repository {_workspace_relative(root, candidate)} "
                f"could not be inspected: {detail or 'Git probe failed'}"
            )
        reported = Path(
            os.path.realpath(os.path.abspath(result.stdout.strip()))
        )
        if reported != candidate:
            raise RepositoryEvidenceError(
                f"repository marker at {_workspace_relative(root, candidate)} "
                "does not identify an exact Git worktree root"
            )
        repositories.append(candidate)
    return tuple(repositories)


def capture_workspace_snapshot(
    workspace_root: str | Path,
    plan_dir: str | Path,
    *,
    exclude: Mapping[str, tuple[str, ...]] | None = None,
    default_exclude: tuple[str, ...] = (),
    inventory_excludes: tuple[str, ...] = (),
    include_ignored: bool = False,
    max_repositories: int = 1024,
    _run_git_fn: GitRunner | None = None,
    _run_git_bytes_fn: GitBytesRunner | None = None,
) -> WorkspaceSnapshot:
    """Capture an ordered root-to-evidence vector for every nested worktree.

    ``exclude`` overrides exclusions for named workspace-relative roots, while
    ``default_exclude`` applies to every other discovered repository, so a
    caller can state one content policy for roots discovery has yet to find.
    Guards capture the plan's registry through ``capture_registered_snapshot``
    instead; this walking capture describes a whole workspace tree.
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
    git_token = _GIT_RUNNER.set(_run_git_fn)
    bytes_token = _GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        repositories = discover_git_worktrees(
            root,
            inventory_excludes=inventory_excludes,
            max_repositories=max_repositories,
        )
        snapshot: WorkspaceSnapshot = OrderedDict()
        per_repository_excludes = exclude or {}
        for repository in repositories:
            relative = _workspace_relative(root, repository)
            fingerprint = _capture_review_repo_fingerprint(
                repository,
                plan_dir,
                exclude=tuple(
                    per_repository_excludes.get(relative, default_exclude)
                ),
                include_ignored=include_ignored,
            )
            if not fingerprint.get("git"):
                # Discovery proved this was a worktree moments ago. Losing that
                # fact during capture is a race, not evidence that the guard no
                # longer applies.
                fingerprint = {
                    "git": True,
                    "unavailable": {
                        "reason": "repository-disappeared",
                        "argv": ["rev-parse", "--show-toplevel"],
                    },
                }
            snapshot[relative] = fingerprint
        return snapshot
    finally:
        _GIT_BYTES_RUNNER.reset(bytes_token)
        _GIT_RUNNER.reset(git_token)


def _is_exact_worktree_root(repository: Path) -> bool:
    """Report whether Git calls `repository` its own worktree root."""
    try:
        result = _run_git(repository, ["rev-parse", "--show-toplevel"])
    except Exception:
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
    _run_git_fn: GitRunner | None = None,
    _run_git_bytes_fn: GitBytesRunner | None = None,
) -> WorkspaceSnapshot:
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
    """
    root = Path(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
    git_token = _GIT_RUNNER.set(_run_git_fn)
    bytes_token = _GIT_BYTES_RUNNER.set(_run_git_bytes_fn)
    try:
        snapshot: WorkspaceSnapshot = OrderedDict()
        per_repository_excludes = exclude or {}
        for relative in sorted(dict.fromkeys(repositories)):
            repository = Path(os.path.abspath(root / relative))
            # Canonicalizing here would fingerprint whatever a replacement
            # symlink points at under the declared key, so a checkout swapped
            # for a link to an identical sibling would compare as unchanged.
            if os.path.realpath(repository) != os.fspath(repository):
                continue
            if not repository.is_dir():
                continue
            fingerprint: RepositoryFingerprint = {"git": False}
            if _is_exact_worktree_root(repository):
                fingerprint = _capture_review_repo_fingerprint(
                    repository,
                    plan_dir,
                    exclude=tuple(
                        per_repository_excludes.get(relative, default_exclude)
                    ),
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
        return snapshot
    finally:
        _GIT_BYTES_RUNNER.reset(bytes_token)
        _GIT_RUNNER.reset(git_token)


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
            repository_evidence_unavailable(before_fingerprint)
            if before_fingerprint is not None
            else None
        )
        after_unavailable = (
            repository_evidence_unavailable(after_fingerprint)
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
            changes = compare_repository_fingerprints(
                before_fingerprint, after_fingerprint
            )
            changed[repository] = {
                **changes,
                "qualified_paths": _qualified_paths(repository, changes),
            }

    return {
        "changed": changed,
        "added": sorted(after_roots - before_roots),
        "removed": sorted(before_roots - after_roots),
        "unavailable": unavailable,
    }
