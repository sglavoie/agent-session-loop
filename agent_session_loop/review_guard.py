"""Every git the run loop shells out to, and the refusals built on it.

The run loop makes claims about a repository it does not own: that the review
turn changed nothing outside its allowlist, that the worktree was clean before
auto-commit, that a generated repair child is not about to carry work it never
wrote. Each of those claims bottoms out in a ``git`` subprocess, and a claim is
only as honest as the process that produced it. This module owns that surface
so the loop is left deciding *what* to refuse rather than reimplementing *how*
git is invoked and how its failures are classified.

Four things live here:

* **The git shell.** :func:`_run_git` and :func:`_run_git_bytes` are the only
  places in the run loop that spawn ``git``. Both convert a missing executable
  or an OS-level failure into :class:`~agent_session_loop.run_errors.RunError`,
  so no caller above them ever handles a raw :class:`OSError`.
* **Repository fingerprinting.** The compatibility wrappers over
  :mod:`agent_session_loop.repository_evidence` that the review mutation guard
  captures before and after a turn, compares, and renders.
* **The allowlist.** :func:`_review_guard_excludes` resolves the review guard's
  exclusions from :data:`REVIEW_GUARD_EXCLUDE_ENV_VAR` and the CLI.
* **Commit preflight.** The clean-worktree gates the run takes before
  auto-commit, and the far narrower gate a generated repair child takes before
  its own owned commit.

Nothing here writes a durable record. Every function is a projection of the
filesystem, a parsed namespace, or a prior durable record into a value or an
exception; the run loop keeps the event log, the state payload, and the pause.

Nothing here imports the runner either. The one collaborator this module does
not own -- the projection that classifies a workspace comparison against a
session's write scope -- enters through the :class:`WorkspaceEvidenceProbe`
seam, which ``workspace_evidence`` satisfies at the single call site in
``turn_preparation``, and which is what lets the retained-scope branch of
commit preflight be typed without a cycle.

Four invariants survive the move and are load-bearing:

* **The allowlist is a refusal, not a filter.** An absolute path or a ``..``
  segment raises rather than being dropped, so an allowlist can never reach
  outside the repository and a typo fails the run instead of silently widening
  the guard's blind spot.
* **Fingerprint bytes are not decoded.** :func:`_run_git_bytes` exists because
  text mode translates ``\\r\\n`` and a lone ``\\r`` to ``\\n``, which collapses
  values differing only in those bytes onto one another and would report two
  distinct contents as unchanged.
* **A sanction requires proof.** :class:`_RetainedContentEvidence` reports
  ``available=False`` whenever the retained turn's own after-state could not be
  compared against the worktree now, and :func:`_repair_commit_preflight`
  sanctions nothing on that path. A pathname is not an origin: the bytes at a
  retained path now are whatever the last writer put there.
* **Preflight is fail-closed and runs before the harness.** Dirt the child did
  not author raises :class:`RepairCommitPreflightError` before a turn is
  launched, rather than riding along into a commit made under the repair's
  name.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from . import plans, repository_evidence, run_selection, verification, workspaces
from .run_errors import RunError

# Repo-relative paths the review mutation guard ignores, on top of any
# ``--review-guard-exclude`` values. Stow packages whose files the harness
# rewrites mid-turn (its own settings, caches) would otherwise revert a
# complete review turn.
REVIEW_GUARD_EXCLUDE_ENV_VAR = "ASL_REVIEW_GUARD_EXCLUDE"


class RepairCommitPreflightError(RunError):
    """A generated repair child cannot safely enter its owned commit phase."""

    def __init__(self, message: str, details: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.details = dict(details)


class WorkspaceEvidenceProbe(Protocol):
    """Classify a workspace comparison against one session's write scope.

    Declared structurally rather than imported so this module never depends on
    ``workspace_evidence``, which owns the concrete projection. The
    retained-scope
    branch of :func:`_repair_commit_preflight` needs exactly one thing from it:
    the per-path ``changed`` evidence, qualified by repository alias, that
    :func:`_retained_turn_changed_paths` reads. Passing the same projection the
    write guard itself uses is what makes the drifted paths qualified the same
    way as the retained paths they are subtracted from.
    """

    def __call__(
        self,
        *,
        workspace: workspaces.ResolvedWorkspace,
        scope: plans.SessionScope,
        comparison: Mapping[str, Any],
        exclude: tuple[str, ...],
        include_ignored: bool,
    ) -> dict[str, Any]: ...


def _review_guard_excludes(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve the review mutation guard's allowlist as sorted POSIX paths.

    Merges :data:`REVIEW_GUARD_EXCLUDE_ENV_VAR` (``os.pathsep``-separated) with
    every ``--review-guard-exclude`` value. Absolute paths and ``..`` segments
    raise :class:`RunError` so an allowlist can never reach outside the
    repository, and so a typo fails the run instead of silently widening the
    guard's blind spot.
    """
    raw: list[str] = []
    env_value = os.environ.get(REVIEW_GUARD_EXCLUDE_ENV_VAR)
    if env_value:
        raw.extend(env_value.split(os.pathsep))
    raw.extend(getattr(args, "review_guard_exclude", None) or ())

    excludes: set[str] = set()
    for entry in raw:
        candidate = entry.strip()
        if not candidate:
            continue
        pure = PurePosixPath(candidate)
        if pure.is_absolute():
            raise RunError(
                "--review-guard-exclude must be repo-relative, got "
                f"absolute path {candidate!r}"
            )
        if ".." in pure.parts:
            raise RunError(
                "--review-guard-exclude must not contain '..' segments, got "
                f"{candidate!r}"
            )
        normalized = pure.as_posix().rstrip("/")
        if normalized:
            excludes.add(normalized)
    return tuple(sorted(excludes))


def _review_guard_include_ignored(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "review_guard_include_ignored", False))


def _run_git(
    cwd: Path,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise RunError("git executable not found on PATH") from error
    except OSError as error:
        raise RunError(f"could not run git {' '.join(args)}: {error}") from error


def _run_git_bytes(
    cwd: Path,
    args: list[str],
) -> subprocess.CompletedProcess[bytes]:
    """Run git without decoding, for output whose bytes must survive intact.

    Text mode translates ``\\r\\n`` and a lone ``\\r`` to ``\\n``, which collapses
    values differing only in those bytes onto one another.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise RunError("git executable not found on PATH") from error
    except OSError as error:
        raise RunError(f"could not run git {' '.join(args)}: {error}") from error


def _read_git_head(cwd: Path) -> dict[str, Any]:
    result = _run_git(cwd, ["rev-parse", "--verify", "HEAD"])
    head = result.stdout.strip() if result.returncode == 0 else None
    return {
        "head": head,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _capture_review_repo_fingerprint(
    cwd: str | Path,
    plan_dir: str | Path,
    *,
    exclude: tuple[str, ...] = (),
    include_ignored: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper for the extracted repository evidence engine."""
    return repository_evidence.capture_repository_fingerprint(
        cwd,
        plan_dir,
        exclude=exclude,
        include_ignored=include_ignored,
        _run_git_fn=_run_git,
        _run_git_bytes_fn=_run_git_bytes,
    )


def _review_guard_unavailable_detail(
    fingerprint: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Compatibility wrapper for fail-closed repository evidence details."""
    return repository_evidence.repository_evidence_unavailable(fingerprint)


def _default_excludes_file() -> Path | None:
    """Compatibility wrapper for Git's default global excludes path."""
    return repository_evidence.default_excludes_file()


def _effective_excludes_file(cwd: Path, repo_root: Path) -> tuple[str, Path | None]:
    """Compatibility wrapper for effective global exclude resolution."""
    return repository_evidence.effective_excludes_file(
        cwd, repo_root, _run_git_bytes_fn=_run_git_bytes
    )


def _review_repo_state(fingerprint: Mapping[str, Any]) -> dict[str, Any] | None:
    """Compatibility wrapper for state derived from repository evidence."""
    return repository_evidence.repository_state(fingerprint)


def _index_flagged_paths(listing_text: str) -> list[str]:
    """Compatibility wrapper for diff-suppressed Git index entries."""
    return repository_evidence.index_flagged_paths(listing_text)


def _review_guard_changed_paths(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper for single-repository evidence comparison."""
    return repository_evidence.compare_repository_fingerprints(before, after)


def _review_guard_changed_summary(changed_paths: Mapping[str, Any]) -> str:
    """Compatibility wrapper for the historical bounded change summary."""
    return repository_evidence.format_repository_changes(changed_paths)


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


def _validate_commit_preflight(
    cwd: Path,
    *,
    verification_context: verification.VerificationContext | None = None,
    needed_aliases: frozenset[str] | None = None,
    clean_aliases: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Refuse an auto-commit run whose committed trees are not clean.

    ``needed_aliases`` selects the registered repositories to inspect and
    ``clean_aliases`` the subset that must be clean; ``None`` requires every
    inspected repository clean. A needed alias outside ``clean_aliases`` is one
    the session only reads: it must still be an inspectable worktree, but its
    dirt cannot enter the session's commit, so it is recorded and not refused.
    """
    context = verification_context or verification.VerificationContext.legacy(cwd)
    if context.workspace is None:
        _validate_git_worktree(cwd)
        _validate_clean_git_status(cwd)
        return {}

    repositories: list[dict[str, Any]] = []
    for repository in context.workspace.repositories:
        if needed_aliases is not None and repository.alias not in needed_aliases:
            continue
        _validate_git_worktree(repository.root)
        try:
            clean = _git_status_is_clean(repository.root)
        except RunError as error:
            raise RunError(
                f"could not inspect repository {repository.alias}: {error}"
            ) from error
        clean_required = clean_aliases is None or repository.alias in clean_aliases
        if needed_aliases is not None and clean_required:
            try:
                linked_roots = repository_evidence.linked_worktrees(repository.root)
            except repository_evidence.RepositoryEvidenceError as error:
                raise RunError(str(error)) from error
            registered_roots = {r.root for r in context.workspace.repositories}
            for linked in linked_roots:
                if linked in registered_roots:
                    continue
                try:
                    _validate_clean_git_status(linked)
                except RunError as error:
                    raise RunError(
                        f"needed repository {repository.alias} linked tree {linked}: "
                        f"{error}"
                    ) from error
        repositories.append(
            {
                "alias": repository.alias,
                "path": repository.repository.path,
                "resolved_root": str(repository.root),
                "clean": clean,
                "clean_required": clean_required,
            }
        )
        if not clean and clean_required:
            raise RunError(
                "git status --porcelain must be empty before auto-commit "
                f"in repository {repository.alias}"
            )
    return {"repositories": repositories}


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


def _retained_turn_changed_paths(record: Mapping[str, Any]) -> frozenset[str]:
    """Qualified paths the retained violating turn is proven to have written.

    The guard's own ``changed`` evidence is the proof: it is the before/after
    comparison of that turn and nothing else, already qualified by alias. The
    record's ``violations`` are a projection of these same paths, so reading
    ``changed`` reads both the authorized and the escaped half of the retained
    work. Anything the guard could not see -- an ignored path, an excluded one,
    the plan directory -- is deliberately absent: not being observed is not
    proof of origin.
    """
    paths: set[str] = set()
    changed = record.get("changed")
    if not isinstance(changed, Mapping):
        return frozenset()
    for entry in changed.values():
        if not isinstance(entry, Mapping):
            continue
        qualified = entry.get("qualified_paths")
        if isinstance(qualified, Sequence) and not isinstance(qualified, (str, bytes)):
            paths.update(str(path) for path in qualified)
    return frozenset(paths)


@dataclass(frozen=True)
class _RetainedContentEvidence:
    """Whether retained paths still hold the bytes their own turn left behind.

    ``available`` is the only key a caller may sanction on: it is false whenever
    the comparison could not be made at all, and an unproven path is never a
    proven one. ``drifted`` names the retained paths whose current worktree or
    index content no longer matches the post-turn record.
    """

    available: bool
    drifted: frozenset[str] = frozenset()
    reason: str | None = None
    repositories: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "available": self.available,
            "drifted_paths": sorted(self.drifted),
            "repositories": list(self.repositories),
        }
        if self.reason is not None:
            record["reason"] = self.reason
        return record


def _retained_turn_content_drift(
    record: Mapping[str, Any],
    *,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans.SessionScope,
    plan_dir: Path,
    workspace_evidence: WorkspaceEvidenceProbe,
) -> _RetainedContentEvidence:
    """Compare the writable repositories against the retained turn's own after-state.

    A pathname is not an origin. The violating turn's guard record proves which
    paths that turn wrote, but the bytes sitting at one of those paths now are
    whatever the last writer put there, and between the pause and this resume
    that writer may have been an operator. The record's ``snapshot_after`` is the
    durable description of what the turn actually left behind — per path, in the
    worktree and in the index — so recapturing the same evidence now and
    comparing the two vectors is what distinguishes a retained path that is still
    retained content from one that has since been rewritten.

    The comparison is made under the exclusion policy the record itself names
    rather than this run's, because a fingerprint captured under a different
    policy describes a different thing and would report drift where none
    happened. Everything that cannot be compared — a missing after-vector, a
    writable repository the vector never covered, a capture failure, a
    repository that has since disappeared — returns ``available=False`` so the
    caller sanctions nothing.
    """
    writable = tuple(
        repository.repository.path
        for repository in workspace.repositories
        if repository.alias in scope.writes
    )
    if not writable:
        return _RetainedContentEvidence(
            False, reason="the repair child declares no writable registered repository"
        )
    recorded_after = record.get("snapshot_after")
    if not isinstance(recorded_after, Mapping) or not recorded_after:
        return _RetainedContentEvidence(
            False, reason="retained turn recorded no post-turn content evidence"
        )
    recorded: repository_evidence.WorkspaceSnapshot = OrderedDict()
    for key, fingerprint in recorded_after.items():
        linked = repository_evidence.split_linked_worktree_key(str(key))
        owner = linked[0] if linked is not None else str(key)
        if owner in writable and isinstance(fingerprint, Mapping):
            recorded[str(key)] = dict(fingerprint)
    uncovered = [path for path in writable if path not in recorded]
    if uncovered:
        return _RetainedContentEvidence(
            False,
            reason=(
                "retained turn recorded no post-turn content evidence for "
                f"{', '.join(sorted(uncovered))}"
            ),
        )
    exclude = tuple(str(entry) for entry in record.get("excluded") or ())
    include_ignored = bool(record.get("include_ignored"))
    try:
        current = repository_evidence.capture_registered_snapshot(
            workspace.root,
            plan_dir,
            writable,
            default_exclude=exclude,
            include_ignored=include_ignored,
            _run_git_fn=_run_git,
            _run_git_bytes_fn=_run_git_bytes,
        )
    except repository_evidence.RepositoryEvidenceError as error:
        return _RetainedContentEvidence(
            False, reason=f"current content evidence unavailable: {error}"
        )
    comparison = repository_evidence.compare_workspace_snapshots(recorded, current)
    unavailable = comparison.get("unavailable") or {}
    removed = comparison.get("removed") or []
    if unavailable or removed:
        named = ", ".join(sorted({*map(str, unavailable), *map(str, removed)}))
        return _RetainedContentEvidence(
            False, reason=f"content evidence could not be compared for {named}"
        )
    evidence = workspace_evidence(
        workspace=workspace,
        scope=scope,
        comparison=comparison,
        exclude=exclude,
        include_ignored=include_ignored,
    )
    # Both sets are produced by one projection, so the drifted paths are
    # qualified exactly the way the retained paths they are subtracted from are.
    return _RetainedContentEvidence(
        True,
        drifted=_retained_turn_changed_paths(evidence),
        repositories=writable,
    )


def _repair_commit_preflight(
    cwd: Path,
    *,
    verification_context: verification.VerificationContext,
    session_scope: plans.SessionScope | None,
    workspace_evidence: WorkspaceEvidenceProbe,
    plan_dir: Path | None = None,
    retained_scope_repair: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require a clean writable set for one repair child.

    ``retained_scope_repair`` is the uncleared write-scope-violation record for
    this same child. On that path the runner is the author of *some* of the
    dirt: it deliberately retained the violating turn's changes and reverted the
    child to pending so its durable scope could be repaired. Demanding a clean
    writable set there dead-ends the very repair the retention exists to enable,
    so the sanction covers exactly the paths that turn is recorded as having
    written and still recorded as holding the content it left at them. The
    sanction stops there. A legacy repair child's commit is still
    repository-wide, so any other dirty path -- work an operator started after
    the pause in a repository the child is about to touch, or an edit they made
    to a retained path itself -- would be staged and committed under the
    repair's name; that case raises here, before the harness starts, rather than
    riding along. A registered child stages only the paths its own turn guard
    recorded, but the gate stays uniform: dirt the child did not author is not
    its to carry, whichever staging path it takes. Nothing escapes review
    either: the retained-scope branch still judges every retained change against
    the child's post-regeneration write scope before the turn starts, and pauses
    when any of it falls outside.
    """
    details: dict[str, Any] = {
        "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
        "repositories": [],
    }
    workspace = verification_context.workspace
    if workspace is None:
        selected = [(None, ".", cwd)]
    else:
        if session_scope is None or not session_scope.writes:
            message = "generated repair child declares no writable repository"
            details["error"] = message
            raise RepairCommitPreflightError(message, details)
        write_aliases = set(session_scope.writes)
        selected = [
            (repository.alias, repository.repository.path, repository.root)
            for repository in workspace.repositories
            if repository.alias in write_aliases
        ]

    dirty_repositories: list[dict[str, Any]] = []
    for alias, repository_path, root in selected:
        worktree = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
        if worktree.returncode != 0 or worktree.stdout.strip() != "true":
            detail = worktree.stderr.strip() or worktree.stdout.strip()
            message = f"writable repository {alias or '.'} is not a Git worktree" + (
                f": {detail}" if detail else ""
            )
            details["error"] = message
            raise RepairCommitPreflightError(message, details)
        status = _run_git(
            root, ["status", "--porcelain", "-z", "--untracked-files=all"]
        )
        if status.returncode != 0:
            detail = status.stderr.strip() or status.stdout.strip()
            message = f"could not inspect writable repository {alias or '.'}"
            if detail:
                message += f": {detail}"
            details["error"] = message
            raise RepairCommitPreflightError(message, details)
        dirty_paths = _porcelain_dirty_paths(status.stdout)
        qualified_paths = [
            f"{alias}:{path}" if alias is not None else path for path in dirty_paths
        ]
        row: dict[str, Any] = {
            "alias": alias,
            "path": repository_path,
            "resolved_root": str(root),
            "clean": not dirty_paths,
            "dirty_paths": qualified_paths,
        }
        details["repositories"].append(row)
        if dirty_paths:
            dirty_repositories.append(row)

    if dirty_repositories:
        if retained_scope_repair is not None:
            if workspace is None or plan_dir is None:
                content = _RetainedContentEvidence(
                    False,
                    reason="retained-scope recovery requires a registered workspace",
                )
            else:
                assert session_scope is not None
                content = _retained_turn_content_drift(
                    retained_scope_repair,
                    workspace=workspace,
                    scope=session_scope,
                    plan_dir=plan_dir,
                    workspace_evidence=workspace_evidence,
                )
            retained_paths = (
                _retained_turn_changed_paths(retained_scope_repair) - content.drifted
                if content.available
                else frozenset()
            )
            sanctioned: list[str] = []
            unsanctioned: list[str] = []
            drifted_dirty: list[str] = []
            unsanctioned_rows: list[dict[str, Any]] = []
            for row in dirty_repositories:
                stray = [
                    path for path in row["dirty_paths"] if path not in retained_paths
                ]
                sanctioned.extend(
                    path for path in row["dirty_paths"] if path in retained_paths
                )
                drifted_dirty.extend(path for path in stray if path in content.drifted)
                if stray:
                    unsanctioned.extend(stray)
                    unsanctioned_rows.append(row)
            details["retained_scope_repair"] = {
                "session_id": retained_scope_repair.get("session_id"),
                "sanctioned_repositories": ", ".join(
                    str(row["alias"] or ".")
                    for row in dirty_repositories
                    if any(path in retained_paths for path in row["dirty_paths"])
                ),
                "sanctioned_dirty_paths": sanctioned,
                "retained_turn_paths": sorted(
                    _retained_turn_changed_paths(retained_scope_repair)
                ),
                "unsanctioned_dirty_paths": unsanctioned,
                "content_drifted_dirty_paths": drifted_dirty,
                "content_evidence": content.payload(),
            }
            if not unsanctioned:
                return details
            named = ", ".join(unsanctioned[:3])
            if len(unsanctioned) > 3:
                named += f" (+{len(unsanctioned) - 3} more)"
            if not content.available:
                message = (
                    "generated repair child cannot recover its retained changes "
                    "without durable evidence of what the retained turn left "
                    f"behind: {content.reason}; dirty paths: {named}"
                )
            elif drifted_dirty:
                message = (
                    "generated repair child cannot recover its retained changes "
                    "after they were edited; dirty paths the retained turn did "
                    f"not leave behind: {named}"
                )
            else:
                message = (
                    "generated repair child cannot recover its retained changes "
                    "while unrelated work is present; dirty paths the retained "
                    f"turn did not write: {named}"
                )
            details["error"] = message
            details["dirty_repositories"] = unsanctioned_rows
            raise RepairCommitPreflightError(message, details)
        labels = ", ".join(str(row["alias"] or ".") for row in dirty_repositories)
        message = (
            "generated repair child requires clean writable repositories before "
            f"execution; dirty: {labels}"
        )
        details["error"] = message
        details["dirty_repositories"] = dirty_repositories
        raise RepairCommitPreflightError(message, details)
    return details


def _validate_commit_at_end_scope(
    *,
    snapshot: plans.PlanSnapshot,
    max_sessions: int | None,
) -> None:
    if max_sessions is None:
        return
    pending_count = plans.status_counts(snapshot)["pending"]
    if max_sessions < pending_count:
        raise RunError(
            "--commit-at-end cannot be combined with --max-sessions when "
            "not all currently pending sessions can complete in one invocation"
        )
