"""Classify HEAD and remote-ref transitions and qualify repository paths."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import repository_evidence_compare as evidence_compare
from . import repository_evidence_git as evidence_git

#: Ref namespace holding a local cache of another repository's branches. Only
#: ``git fetch`` and ``git push`` write it, and moving it changes no worktree
#: file, index entry, local branch, tag, or HEAD in this repository.
REMOTE_TRACKING_REF_PREFIX = "refs/remotes/"


_REF_CHANGE_KEYS = ("refs_added", "refs_removed", "refs_changed")


def is_remote_tracking_ref(name: object) -> bool:
    """Whether ``name`` is a remote-tracking ref (``refs/remotes/*``)."""
    return str(name or "").startswith(REMOTE_TRACKING_REF_PREFIX)


def without_remote_tracking_refs(fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``fingerprint`` with its remote-tracking ref tips dropped.

    Every other field is kept verbatim, so two fingerprints that compare equal
    after this projection differ at most in ``refs/remotes/*`` tips -- the
    whole-fingerprint comparison the guards rely on stays exhaustive for every
    other evidence category, including ones added later.
    """
    projected = dict(fingerprint)
    refs = fingerprint.get("refs")
    if isinstance(refs, (list, tuple)):
        projected["refs"] = [
            entry
            for entry in refs
            if not (
                isinstance(entry, (list, tuple))
                and entry
                and is_remote_tracking_ref(entry[0])
            )
        ]
    return projected


def remote_ref_drift(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Describe every ``refs/remotes/*`` move between two fingerprints.

    A remote-tracking ref moves whenever anything fetches or pushes -- a
    background sync job, another checkout sharing the ref store, or the session
    publishing a commit it made -- so the writer is recorded as unknown. The
    move is drift evidence, never a content mutation: the guards judge the
    worktree, index, local refs, and HEAD, none of which a remote-ref move can
    change. ``new_tip_is_local`` records whether the new tip equals a local ref
    tip or HEAD in either snapshot (a push of an existing local commit), so an
    auditor can tell a push from a fetch without re-probing.
    """

    def entries(fingerprint: Mapping[str, Any]) -> list[tuple[str, str]]:
        return [
            (str(entry[0]), str(entry[1]))
            for entry in fingerprint.get("refs") or ()
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ]

    local_tips: set[str] = set()
    for fingerprint in (before, after):
        head = fingerprint.get("head")
        if isinstance(head, str) and head:
            local_tips.add(head)
        local_tips.update(
            sha
            for name, sha in entries(fingerprint)
            if not is_remote_tracking_ref(name)
        )
    old = {n: sha for n, sha in entries(before) if is_remote_tracking_ref(n)}
    new = {n: sha for n, sha in entries(after) if is_remote_tracking_ref(n)}
    drift: list[dict[str, Any]] = []
    for name in sorted(set(old) | set(new)):
        tip_before, tip_after = old.get(name), new.get(name)
        if tip_before == tip_after:
            continue
        if tip_before is None:
            change = "added"
        elif tip_after is None:
            change = "removed"
        else:
            change = "changed"
        drift.append(
            {
                "ref": name,
                "change": change,
                "before": tip_before,
                "after": tip_after,
                "writer": "unknown",
                "new_tip_is_local": tip_after is not None and tip_after in local_tips,
            }
        )
    return drift


def split_remote_ref_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Drop remote-tracking ref moves from a fingerprint comparison.

    Local branches and tags stay in ``refs_added``/``refs_removed``/
    ``refs_changed``; :func:`remote_ref_drift` carries the dropped moves.
    """
    filtered = dict(changes)
    for key in _REF_CHANGE_KEYS:
        moves = changes.get(key)
        if not isinstance(moves, (list, tuple)):
            continue
        filtered[key] = [
            entry
            for entry in moves
            if not (
                isinstance(entry, Mapping) and is_remote_tracking_ref(entry.get("ref"))
            )
        ]
    return filtered


def unchanged_head_transition(head: str) -> dict[str, Any]:
    """The transition verdict for a tip that did not move.

    Named rather than inlined because it is the one verdict that needs no
    repository to reach: there is no ancestry to probe, so a caller that cannot
    resolve a checkout root can still classify the tip honestly instead of
    reporting the whole entry unobservable.
    """
    return {
        "before": head,
        "after": head,
        "relation": "unchanged",
        "commits_added": 0,
        "commits_removed": 0,
        "history_rewrite": False,
    }


def classify_head_transition(
    cwd: str | Path,
    before_head: str,
    after_head: str,
    *,
    _run_git_fn: evidence_git.GitRunner | None = None,
) -> dict[str, Any]:
    """Classify one observed HEAD move and retain bounded commit counts.

    The comparison fingerprint proves that the tips moved. These ancestry
    probes distinguish ordinary worker-created commits from rewinds and
    rewritten/diverged history while the pre-turn objects are still present.
    Any indeterminate probe fails closed rather than labelling a rewrite as an
    ordinary commit.
    """
    token = evidence_git._GIT_RUNNER.set(_run_git_fn)
    root = Path(cwd)
    try:

        def count_range(revision_range: str) -> int:
            argv = ["rev-list", "--count", revision_range]
            try:
                result = evidence_git._run_git(root, argv)
            except Exception as error:
                raise evidence_git.RepositoryEvidenceError(
                    f"could not count repository history: {error}"
                ) from error
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                raise evidence_git.RepositoryEvidenceError(
                    "could not count repository history: "
                    f"{detail or 'git rev-list failed'}"
                )
            try:
                return int(result.stdout.strip())
            except ValueError as error:
                raise evidence_git.RepositoryEvidenceError(
                    "could not count repository history: malformed git output"
                ) from error

        if before_head == after_head:
            return unchanged_head_transition(before_head)
        if not before_head:
            return {
                "before": before_head,
                "after": after_head,
                "relation": "created",
                "commits_added": count_range(after_head),
                "commits_removed": 0,
                "history_rewrite": False,
            }
        if not after_head:
            return {
                "before": before_head,
                "after": after_head,
                "relation": "deleted",
                "commits_added": 0,
                "commits_removed": count_range(before_head),
                "history_rewrite": True,
            }

        def is_ancestor(ancestor: str, descendant: str) -> bool:
            argv = ["merge-base", "--is-ancestor", ancestor, descendant]
            try:
                result = evidence_git._run_git(root, argv)
            except Exception as error:
                raise evidence_git.RepositoryEvidenceError(
                    f"could not classify repository history: {error}"
                ) from error
            if result.returncode in (0, 1):
                return result.returncode == 0
            detail = result.stderr.strip() or result.stdout.strip()
            raise evidence_git.RepositoryEvidenceError(
                "could not classify repository history: "
                f"{detail or 'git merge-base failed'}"
            )

        forward = is_ancestor(before_head, after_head)
        backward = False if forward else is_ancestor(after_head, before_head)
        if forward:
            relation = "fast-forward"
            commits_added = count_range(f"{before_head}..{after_head}")
            commits_removed = 0
        elif backward:
            relation = "rewind"
            commits_added = 0
            commits_removed = count_range(f"{after_head}..{before_head}")
        else:
            relation = "rewrite"
            commits_added = count_range(f"{before_head}..{after_head}")
            commits_removed = count_range(f"{after_head}..{before_head}")
        return {
            "before": before_head,
            "after": after_head,
            "relation": relation,
            "commits_added": commits_added,
            "commits_removed": commits_removed,
            "history_rewrite": relation in {"rewind", "rewrite"},
        }
    finally:
        evidence_git._GIT_RUNNER.reset(token)


def head_transition_paths(
    cwd: str | Path,
    before_head: str,
    after_head: str,
) -> list[str]:
    """Name every path whose committed content differs between two tips.

    A status comparison only sees changes relative to the HEAD current at each
    snapshot, so a change the worker committed shows up as a moved tip and
    nothing else. These are the paths that tip move carried, read from the
    trees themselves so a rewind or rewrite is named as fully as a commit.
    An empty tip (an unborn branch on one side) names its whole tree. Paths
    are repository-root relative, whatever directory ``cwd`` is.
    """
    root = Path(cwd)
    if before_head == after_head:
        return []
    if before_head and after_head:
        argv = [
            "diff-tree",
            "-r",
            "-z",
            "--no-renames",
            "--name-only",
            before_head,
            after_head,
        ]
    else:
        argv = [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "--name-only",
            before_head or after_head,
        ]
    try:
        result = evidence_git._run_git_bytes(root, argv)
    except Exception as error:
        raise evidence_git.RepositoryEvidenceError(
            f"could not list the paths a HEAD move changed: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise evidence_git.RepositoryEvidenceError(
            "could not list the paths a HEAD move changed: "
            f"{detail or 'git ' + argv[0] + ' failed'}"
        )
    names = result.stdout.decode("utf-8", evidence_git.GUARD_TEXT_ERRORS)
    return sorted({name for name in names.split("\0") if name})


def qualified_repository_paths(
    repository: str,
    changed_paths: Mapping[str, Any],
) -> list[str]:
    """Name one comparison's changed paths as ``repository:path`` strings.

    The single-repository comparison and the workspace one describe the same
    change keys, so both are named here rather than in two places that could
    drift apart on the next key added to a comparison.
    """
    return _qualified_paths(repository, changed_paths)


def format_repository_changes(changed_paths: Mapping[str, Any]) -> str:
    """Render the bounded historical changed-path suffix."""
    return evidence_compare._review_guard_changed_summary(changed_paths)


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
        paths.extend(f"{repository}:{path}" for path in changes.get(key) or ())
    return list(dict.fromkeys(paths))
