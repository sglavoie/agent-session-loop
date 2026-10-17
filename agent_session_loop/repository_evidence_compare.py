"""Compare repository fingerprints and summarize changed or unavailable evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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

    def moved(before_map: Mapping[str, str], after_map: Mapping[str, str]) -> set[str]:
        return {
            path
            for path in set(before_map) | set(after_map)
            if before_map.get(path) != after_map.get(path)
        }

    def node_map(fingerprint: Mapping[str, Any], key: str) -> dict[str, str]:
        # Node kind is part of the identity: a symlink replaced by a regular
        # file holding the target's bytes must not compare equal.
        return {
            str(rel): f"{kind}:{sha}" for rel, kind, sha in (fingerprint.get(key) or ())
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

    def refs_map(fingerprint: Mapping[str, Any]) -> dict[str, str]:
        return {
            str(name): str(object_name)
            for name, object_name in (fingerprint.get("refs") or ())
        }

    untracked_before = node_map(before, "untracked")
    untracked_after = node_map(after, "untracked")
    ignore_before = ignore_map(before)
    ignore_after = ignore_map(after)

    refs_before = refs_map(before)
    refs_after = refs_map(after)
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
                if ignore_before.get((role, rel)) != ignore_after.get((role, rel))
            }
        ),
        "head_changed": before.get("head") != after.get("head"),
        "head_before": before.get("head"),
        "head_after": after.get("head"),
        "refs_added": [
            {"ref": name, "after": refs_after[name]}
            for name in sorted(set(refs_after) - set(refs_before))
        ],
        "refs_removed": [
            {"ref": name, "before": refs_before[name]}
            for name in sorted(set(refs_before) - set(refs_after))
        ],
        "refs_changed": [
            {
                "ref": name,
                "before": refs_before[name],
                "after": refs_after[name],
            }
            for name in sorted(set(refs_before) & set(refs_after))
            if refs_before[name] != refs_after[name]
        ],
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
