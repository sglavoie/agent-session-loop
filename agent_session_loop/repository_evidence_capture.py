"""Capture a repository fingerprint from Git probes and filesystem evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    repository_evidence_git as evidence_git,
)
from . import (
    repository_evidence_ignore as evidence_ignore,
)
from . import (
    repository_evidence_nodes as evidence_nodes,
)


def _split_raw_from_patch(text: str, *, argv: Sequence[str]) -> tuple[str, str]:
    """Split ``--patch-with-raw`` output into ``--name-status`` text and the patch.

    One probe renders both, so the name list and the patches are read from the
    same diff queue instead of two runs that a concurrent write could set
    apart. Git prints every raw entry, ``:<mode> <mode> <sha> <sha> <status>``
    then a tab and the path, followed by one blank line and the patch exactly
    as plain ``git diff`` renders it, so ``diff_sha256`` stays the hash of the
    same bytes. Dropping the four fields before the status leaves the
    ``<status>\t<path>`` line ``--name-status`` prints. A raw entry that does
    not have that shape fails the capture closed.
    """
    names: list[str] = []
    cursor = 0
    while text.startswith(":", cursor):
        end = text.find("\n", cursor)
        if end < 0:
            raise evidence_git._ReviewGuardUnavailable("diff-raw-malformed", argv)
        fields = text[cursor + 1 : end].split(" ", 4)
        if len(fields) != 5 or "\t" not in fields[4]:
            raise evidence_git._ReviewGuardUnavailable("diff-raw-malformed", argv)
        names.append(fields[4] + "\n")
        cursor = end + 1
    if names and text.startswith("\n", cursor):
        cursor += 1
    return "".join(names), text[cursor:]


def _pair_diff_names_with_patches(
    names_text: str, diff_text: str, *, argv: Sequence[str], reason: str
) -> tuple[list[str], list[list[str]]]:
    """Attribute each patch in ``diff_text`` to its path in ``names_text``.

    ``names_text`` is ``--name-status`` output (see
    :func:`_split_raw_from_patch`), which walks the same diff queue in the same
    order as the full patch. Its status is what says how many patch
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
            raise evidence_git._ReviewGuardUnavailable(reason, argv)
        changed.append(rel)
        section_counts.append(
            evidence_git._TYPE_CHANGED_PATCH_SECTIONS
            if status == evidence_git._TYPE_CHANGED_STATUS
            else 1
        )
    patches = evidence_git._DIFF_FILE_HEADER_RE.split(diff_text)[1:]
    if len(patches) != sum(section_counts):
        raise evidence_git._ReviewGuardUnavailable(reason, argv)
    paired: list[list[str]] = []
    cursor = 0
    for rel, count in zip(changed, section_counts, strict=False):
        section = "".join(patches[cursor : cursor + count])
        cursor += count
        paired.append(
            [
                rel,
                hashlib.sha256(
                    section.encode("utf-8", evidence_git.GUARD_TEXT_ERRORS)
                ).hexdigest(),
            ]
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
    ``{"git": True, "head": <sha or "">, "refs": [[name, sha], ...],
    "diff_sha256": <sha>,
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
    a review turn, which is exactly why they are gitignored.
    ``include_ignored`` — the run flag ``--review-guard-include-ignored`` —
    restores the strict scan for a repository whose ignored tree is expected to
    stay still, and is off by default so the strict behavior is opt-in
    everywhere, direct calls included.

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
        repo_root_result = evidence_git._run_git(cwd_path, root_argv)
    except Exception as error:  # noqa: BLE001
        return evidence_git._ReviewGuardUnavailable(
            "git-unrunnable", root_argv, error=error
        ).fingerprint
    if repo_root_result.returncode != 0:
        return {"git": False}

    try:
        try:
            repo_root = Path(repo_root_result.stdout.strip()).resolve()
        except OSError as error:
            raise evidence_git._ReviewGuardUnavailable(
                "repo-root-unresolved", root_argv, error=error
            ) from error

        head_sha, diff_base = evidence_git._review_guard_diff_base(cwd_path)
        refs_payload = evidence_git._fingerprint_refs(cwd_path)

        # ``:(top)`` anchors the positive pathspec at the repository root, so a
        # nested ``cwd`` still fingerprints the whole repository instead of one
        # subtree, and the reported paths stay repo-root-relative so they line
        # up with the untracked scan and the allowlist.
        pathspecs = [":(top)"]
        try:
            rel_plan = plan_path.relative_to(repo_root)
            pathspecs.append(
                evidence_git._literal_exclude_pathspec(rel_plan.as_posix())
            )
        except ValueError:
            pass
        pathspecs.extend(
            evidence_git._literal_exclude_pathspec(path) for path in exclude
        )

        diff_argv = [
            "diff",
            diff_base,
            "--patch-with-raw",
            *evidence_git._GUARD_DIFF_FLAGS,
            "--",
            *pathspecs,
        ]
        names_text, diff_text = _split_raw_from_patch(
            evidence_git._review_guard_git_output(cwd_path, diff_argv),
            argv=diff_argv,
        )
        index_diff_argv = [
            "diff",
            "--cached",
            diff_base,
            "--patch-with-raw",
            *evidence_git._GUARD_DIFF_FLAGS,
            "--",
            *pathspecs,
        ]
        index_names_text, index_diff_text = _split_raw_from_patch(
            evidence_git._review_guard_git_output(cwd_path, index_diff_argv),
            argv=index_diff_argv,
        )
        # ``--full-name`` keeps the reported paths repo-root-relative when
        # ``cwd`` is a subdirectory, so they line up with the repo-root-relative
        # allowlist and with the ``repo_root / rel`` lookup below; the
        # ``:(top)`` pathspec is what widens the scan itself past that
        # subdirectory. ``--exclude-standard`` defers to git's ignore rules,
        # which are the operator's existing declaration of what is not the code
        # under review: harness settings are commonly gitignored precisely
        # because the harness rewrites them mid-turn, so a scan that ignored
        # that declaration would revert essentially every review turn.
        untracked_argv = [
            "ls-files",
            "--others",
            "--full-name",
            *(() if include_ignored else ("--exclude-standard",)),
            "-z",
            "--",
            ":(top)",
        ]
        untracked_text = evidence_git._review_guard_git(cwd_path, untracked_argv).stdout
        untracked_payload = evidence_nodes._fingerprint_untracked_nodes(
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
        worktree_ignore_argv = list(evidence_git._WORKTREE_IGNORE_ARGV)
        worktree_ignore_text = evidence_git._review_guard_git(
            cwd_path, worktree_ignore_argv
        ).stdout
        worktree_ignore_payload = evidence_nodes._fingerprint_untracked_nodes(
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
            "-z",
            "--",
            ":(top)",
        ]
        index_flags_text = evidence_git._review_guard_git(
            cwd_path, index_flags_argv
        ).stdout
        index_flagged_payload = evidence_nodes._fingerprint_index_flagged_nodes(
            index_flags_text,
            repo_root=repo_root,
            plan_path=plan_path,
            exclude=exclude,
            argv=index_flags_argv,
        )
        ignore_config_payload = evidence_ignore._fingerprint_ignore_config(
            cwd_path, repo_root
        )

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
    except evidence_git._ReviewGuardUnavailable as unavailable:
        return unavailable.fingerprint

    return {
        "git": True,
        "head": head_sha,
        "refs": refs_payload,
        "diff_sha256": hashlib.sha256(
            diff_text.encode("utf-8", evidence_git.GUARD_TEXT_ERRORS)
        ).hexdigest(),
        "tracked_changed": sorted(tracked_changed),
        "tracked_patches": tracked_patches,
        "index_diff_sha256": hashlib.sha256(
            index_diff_text.encode("utf-8", evidence_git.GUARD_TEXT_ERRORS)
        ).hexdigest(),
        "index_changed": sorted(index_changed),
        "index_patches": index_patches,
        "untracked": untracked_payload,
        "index_flagged": index_flagged_payload,
        "worktree_ignore": worktree_ignore_payload,
        "ignore_config": ignore_config_payload,
    }
