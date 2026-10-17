"""Resolve and fingerprint Git ignore configuration and its source files."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from pathlib import Path

from . import repository_evidence_git as evidence_git
from . import repository_evidence_nodes as evidence_nodes


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
        raise evidence_git._ReviewGuardUnavailable(
            f"{reason_prefix}-target-unresolved",
            argv,
            error=error,
            path=str(link),
        ) from error
    return [
        evidence_git._IGNORE_ROLE_TARGET,
        *evidence_nodes._fingerprint_repo_node(
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
        raise evidence_git._ReviewGuardUnavailable(
            f"{reason_prefix}-unresolved", argv, error=error, path=str(named)
        ) from error
    entry = [
        evidence_git._IGNORE_ROLE_SOURCE,
        *evidence_nodes._fingerprint_repo_node(
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


def _effective_excludes_file(cwd: Path, repo_root: Path) -> tuple[str, Path | None]:
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
    argv = list(evidence_git._EXCLUDES_FILE_ARGV)
    try:
        result = evidence_git._run_git_bytes(cwd, argv)
    except Exception as error:
        raise evidence_git._ReviewGuardUnavailable(
            "git-unrunnable", argv, error=error
        ) from error
    if result.returncode == 1:
        return "unset", _default_excludes_file()
    if result.returncode != 0:
        raise evidence_git._ReviewGuardUnavailable(
            "git-command-failed", argv, result=result
        )
    terminator = result.stdout.find(b"\0")
    if terminator < 0:
        raise evidence_git._ReviewGuardUnavailable(
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
    reported = evidence_git._review_guard_git(cwd, argv).stdout.strip()
    if not reported:
        raise evidence_git._ReviewGuardUnavailable("ignore-config-unresolved", argv)
    payload = _fingerprint_ignore_source(
        cwd / reported,
        repo_root=repo_root,
        argv=argv,
        reason_prefix="ignore-config",
    )
    marker, excludes_file = _effective_excludes_file(cwd, repo_root)
    payload.append(
        [
            evidence_git._IGNORE_ROLE_CONFIG,
            evidence_git._EXCLUDES_CONFIG_REL,
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
                argv=list(evidence_git._EXCLUDES_FILE_ARGV),
                reason_prefix="excludes-file",
            )
        )
    return payload
