"""How much of the tree a re-review must read, and what it re-reads against.

A review that found something is followed by a repair, and the repair is
followed by a *re*-review. That second review is the expensive one, and the
only honest way to make it cheaper is to prove which files moved since the
reviewer last read the tree. This module owns that proof: the durable evidence
of the last repair cycle, the review artifacts still on disk, and the Git
probes that turn the two into either a narrowed *delta* scope or an admission
that no narrowing can be justified.

Scope is computed twice for the same reason it is computed at all. A
single-repository run diffs one worktree; a registered run diffs every
repository in the ordered registry and combines the results into one
alias-qualified file list. Both routes share the same evidence readers and the
same fail-closed rule, which is why they live together rather than beside
their respective callers.

Durable records this boundary writes:

* The pending review session's ``## Review scope`` section, rewritten in place
  by :func:`_refresh_pending_review_scope`. It is the only file this module
  opens, and only that one section changes: the session keeps its identity, its
  version marker, and its pending handoff.
* The ``review-scope`` event that accompanies that rewrite, recording the mode,
  the base commit, the per-repository bases when there are any, and the reason
  a narrower scope was refused. It is appended through the ``_RunPersistence``
  handle the caller supplies, under that handle's lock.

Nothing here imports the runner. The one thing it needs from the loop -- the
review verdict reader -- enters through the :class:`ReviewVerdictReader` seam,
which ``handoff_guards._read_review_verdict`` satisfies as the module that owns
the read.

Invariants that survive the move:

* **Narrowing is only ever driven by Git.** A file list is adopted only when
  every probe behind it succeeded. A base that is missing, unresolvable, no
  longer an ancestor, or undiffable, an unlistable index, and a reviewed tree
  that was dirty with no recorded path evidence, each drop the scope back to
  full rather than substituting a list this module cannot prove complete.
* **The scope is a superset, never a sample.** The changed-file set unions the
  base-to-worktree diff, the base-to-index diff, the unignored untracked files,
  every assume-unchanged or skip-worktree entry (which no diff compares against
  the working tree at all), and the paths the reviewed tree already held
  uncommitted. That covers a path a repair restored to its committed content,
  which the worktree diff no longer shows, and a staged edit whose working file
  was restored, which only the index diff still shows.
* **A registered delta is all-or-nothing.** One repository whose base cannot be
  proved degrades the whole workspace to full scope, so a combined delta never
  narrows some repositories while silently leaving another unexamined.
* **Every probe is repository-wide and root-relative.** Neither a nested ``cwd``
  nor a ``diff.relative=true`` config can drop a repository-root repair from
  the scope.
* **A superseding reopen still has to name a base.** The newest materialized
  reopen record replaces the cycle it descends from as the repair evidence
  scope is computed against, so a record written before the baseline was copied
  has it read back here from the source cycle's own durable event. The recovery
  is in memory and attribution-only: a historical event is never rewritten, a
  field the record already carries is never overwritten, and an unattributable
  or self-contradicting lineage recovers nothing and stays full scope.
* **A missing digest is not a clean digest.** :func:`_review_artifact_digests`
  turns an unreadable artifact into ``None`` rather than letting the failure
  mask a cycle result the caller is about to compare against.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from . import (
    events,
    plans_errors,
    plans_models,
    review,
    review_continuation,
    review_guard,
    review_session_body,
    run_persistence,
    verification_models,
    verification_transitions,
    workspaces,
)
from .run_errors import RunError

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class ReviewVerdictReader(Protocol):
    """The review verdict on disk, as the deferral policy needs it.

    Declared structurally rather than imported: the concrete reader is still a
    runner symbol and does not move until Session 19, and this module may not
    reach back into the loop to get it. Passing it in also keeps the read where
    the caller can see it, which matters because the deferral is the one place
    a *failed* verification can still be accepted.
    """

    def __call__(
        self, plan_dir: Path, *, require_artifact: bool = False
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Verification-adjacent reads
# ---------------------------------------------------------------------------


def _review_broad_failure_acceptance(
    result: verification_models.VerificationResult,
    *,
    plan_dir: Path,
    require_artifact: bool,
    read_review_verdict: ReviewVerdictReader,
) -> verification_transitions.BroadFailureDeferral | None:
    """Read the verdict a deferral needs, but only when one could apply.

    The eligibility test is pure and lives with the deferral policy; this
    supplies the one thing it cannot compute, the review verdict on disk, and
    reads it only once the failures are known to be deferrable at all.
    """
    failures = verification_transitions.broad_failures_only(result)
    if failures is None:
        return None
    return verification_transitions.broad_failure_acceptance(
        failures, read_review_verdict(plan_dir, require_artifact=require_artifact)
    )


def _verification_repair_state(
    event_path: str | Path, session_id: str
) -> dict[str, Any]:
    """Compute repair context for the session's *next* verification attempt."""
    return verification_transitions.verification_repair_budget(
        event_path, session_id
    ).to_payload()


# ---------------------------------------------------------------------------
# Prior-cycle evidence
# ---------------------------------------------------------------------------


def _read_prior_review_cycles(event_path: str | Path) -> list[dict[str, Any]]:
    """Compatibility wrapper for the shared read-only history reader."""
    return review_continuation.read_prior_review_cycles(event_path)


def _review_cycle_state(
    event_path: str | Path,
    snapshot_or_plan_dir: plans_models.PlanSnapshot | str | Path | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for shared cross-process cap accounting."""
    return review_continuation.review_cycle_state(event_path, snapshot_or_plan_dir)


def _review_artifact_digests(plan_dir: Path) -> tuple[str | None, str | None]:
    """Return current review artifact digests without masking cycle results."""

    digests: list[str | None] = []
    for digest_fn in (
        review.final_review_output_sha256,
        review.final_review_artifact_sha256,
    ):
        try:
            digests.append(digest_fn(plan_dir))
        except plans_errors.PlanError:
            digests.append(None)
    return digests[0], digests[1]


def _recover_reopen_baseline(
    event_path: Path, reopen: dict[str, Any]
) -> dict[str, Any]:
    """Fill in baseline fields a reopen record was written without.

    Reopen records only began carrying the reviewed tree once the writer started
    copying it from the source cycle; every record written before that names no
    base, and a reopen supersedes the cycle record that did name one. The
    evidence is not lost, though — it is still on the source cycle's own
    append-only record — so it is read back here, in memory, for the one
    caller that needs it. Nothing is rewritten: a historical event says what it
    said, and a recovery that edited it would destroy the only copy of what was
    actually recorded at the time.

    Recovery is attribution-only, and never overrides evidence the reopen
    already carries. When the source cycle cannot be identified beyond doubt the
    fields stay missing and the rereview falls back to full scope, which is the
    same answer these records have always produced.
    """
    missing = [
        key
        for key in review_continuation.REVIEW_BASELINE_FIELDS
        if reopen.get(key) is None
    ]
    if not missing:
        return reopen
    source = review_continuation.find_reopen_source_cycle(event_path, reopen)
    if source is None:
        return reopen
    recovered = review_continuation.review_baseline_evidence(source)
    reopen.update({key: recovered[key] for key in missing if key in recovered})
    return reopen


def _last_repairs_materialized_cycle(
    event_path: Path,
) -> dict[str, Any] | None:
    """Return the newest durable repair materialization evidence."""
    for record in reversed(review_continuation.read_review_events(event_path)):
        payload = dict(record.payload)
        if record.name == events.REVIEW_CYCLE_EVENT:
            if (
                payload.get("disposition")
                == review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
            ):
                return payload
            continue
        if (
            record.name == events.REVIEW_REPAIR_REOPENED_EVENT
            and payload.get("materialized") is True
        ):
            repositories = payload.get("current_repositories")
            if isinstance(repositories, list):
                payload["repositories"] = repositories
            findings = payload.get("current_findings")
            if isinstance(findings, list):
                payload["findings"] = findings
            return _recover_reopen_baseline(event_path, payload)
    return None


def _prior_review_findings(plan_dir: Path) -> list[dict[str, Any]]:
    """Return the structured findings of the review still on disk."""
    verdict = review.load_review_verdict(plan_dir)
    if not verdict.get("ok"):
        return []
    findings = verdict.get("findings")
    return list(findings) if isinstance(findings, list) else []


def _reviewed_baseline_dirty_paths(
    cycle: Mapping[str, Any],
) -> list[str] | None:
    """Return the paths whose reviewed content differed from the cycle's base.

    ``[]`` means the recorded commit *is* the tree the reviewer saw. ``None``
    means the record cannot prove what that tree held: the reviewer saw
    uncommitted content and the cycle carries no exact path evidence for it, so
    no diff against the base can be shown to cover everything the repair
    touched.
    """
    dirty = cycle.get("worktree_dirty")
    if dirty is False:
        return []
    if dirty is not True:
        return None
    paths = cycle.get("dirty_paths")
    if not isinstance(paths, list):
        return None
    return [str(path) for path in paths if path]


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------


def _compute_review_delta_context(
    *,
    event_path: Path,
    plan_dir: Path,
    cwd: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Choose the scope of the review session about to be generated.

    Returns ``(delta, fallback_reason)``. ``delta`` is ``None`` when there is
    no prior repair cycle to re-verify at all; otherwise it carries ``mode``
    ``"delta"`` (a resolvable, ancestor-verified base commit plus the files
    Git reports as changed since it) or ``"full"`` (prior findings to
    re-verify on top of an unnarrowed review).

    Narrowing is only ever driven by Git. The changed-file set is the union of
    the base-to-worktree diff, the base-to-index diff, the untracked files git
    does not ignore, the
    tracked entries marked assume-unchanged or skip-worktree (which no diff
    compares against the working tree at all), and
    the paths the reviewed tree already held uncommitted, so it covers
    committed and uncommitted repairs alike and stays a superset of everything
    that moved since the reviewer read the tree — including a path the repair
    restored to its committed content, which the base-to-worktree diff no
    longer shows, and a staged edit whose working file was restored, which only
    the base-to-index diff still shows. A base
    that is missing, unresolvable, no longer an ancestor, or undiffable, an
    unlistable index, and a
    dirty reviewed tree with no recorded path evidence, all drop the scope back
    to full rather than substituting a file list the runner cannot prove
    complete.

    Every probe covers the whole repository and reports root-relative paths, so
    neither a nested ``cwd`` nor a ``diff.relative=true`` config can drop a
    repository-root repair from the scope.
    """
    cycle = _last_repairs_materialized_cycle(event_path)
    if cycle is None:
        return None, "no-prior-repair-cycle"

    findings = _prior_review_findings(plan_dir)
    if not findings:
        return None, "prior-findings-unavailable"

    degraded = {
        "mode": review_session_body.REVIEW_SCOPE_FULL,
        "prior_findings": findings,
    }

    base = cycle.get("repo_head")
    if not isinstance(base, str) or not base:
        return degraded, "no-recorded-base"

    baseline_dirty = _reviewed_baseline_dirty_paths(cycle)
    if baseline_dirty is None:
        return degraded, "dirty-base-evidence-missing"

    try:
        resolved = review_guard._run_git(
            cwd, ["rev-parse", "--verify", f"{base}^{{commit}}"]
        )
        if resolved.returncode != 0:
            return degraded, "base-unresolvable"
        ancestor = review_guard._run_git(
            cwd, ["merge-base", "--is-ancestor", base, "HEAD"]
        )
        if ancestor.returncode != 0:
            # A rebase, reset, or amend moved the recorded base off the current
            # history; diffing against it would misreport the repair scope.
            return degraded, "base-not-ancestor"
        # Both probes are pinned to repository-wide, root-relative, rename-free
        # output; see :data:`review_session_body._DELTA_DIFF_FLAGS` for what
        # each flag rules out.
        names = review_guard._run_git(
            cwd, ["diff", "--name-only", *review_session_body._DELTA_DIFF_FLAGS, base]
        )
        if names.returncode != 0:
            return degraded, "base-diff-failed"
        index_names = review_guard._run_git(
            cwd,
            [
                "diff",
                "--cached",
                "--name-only",
                *review_session_body._DELTA_DIFF_FLAGS,
                base,
            ],
        )
        if index_names.returncode != 0:
            return degraded, "index-diff-failed"
        # Rooted at the repository top, so a repair that lands in a nested
        # directory is still a repair the rereview sees, and filtered by git's
        # ignore rules, which are the operator's declaration of what is not code
        # under review. That deference is deliberately independent of
        # ``--review-guard-include-ignored``: the flag governs the guard's
        # evidence ("did anything move"), not the scope ("which code changed and
        # must be reread"). A failed scan degrades to full scope rather than
        # narrowing against a file list that is missing those paths.
        untracked = review_guard._run_git(
            cwd,
            [
                "ls-files",
                "--others",
                "--exclude-standard",
                "--full-name",
                "--",
                ":(top)",
            ],
        )
        if untracked.returncode != 0:
            return degraded, "untracked-scan-failed"
        # Git never compares an assume-unchanged or skip-worktree entry against
        # the working tree, so a repair to one is in none of the diffs above.
        # Every flagged path therefore joins the scope wholesale: the same
        # blindness that hides the repair also makes it impossible to tell
        # which flagged entries moved.
        index_flags = review_guard._run_git(
            cwd, ["ls-files", "-v", "--full-name", "--", ":(top)"]
        )
        if index_flags.returncode != 0:
            return degraded, "index-flags-scan-failed"
    except RunError:
        return degraded, "git-unavailable"

    return (
        {
            "mode": review_session_body.REVIEW_SCOPE_DELTA,
            "base_sha": base,
            "changed_files": sorted(
                {
                    line
                    for line in (
                        *names.stdout.splitlines(),
                        *index_names.stdout.splitlines(),
                        *untracked.stdout.splitlines(),
                        *review_guard._index_flagged_paths(index_flags.stdout),
                        *baseline_dirty,
                    )
                    if line
                }
            ),
            "baseline_dirty": bool(baseline_dirty),
            "prior_findings": findings,
        },
        None,
    )


def _compute_registered_review_delta_context(
    *,
    event_path: Path,
    plan_dir: Path,
    workspace: workspaces.ResolvedWorkspace,
) -> tuple[dict[str, Any] | None, str | None]:
    """Compute one fail-closed combined delta across the ordered registry."""
    cycle = _last_repairs_materialized_cycle(event_path)
    if cycle is None:
        return None, "no-prior-repair-cycle"
    findings = _prior_review_findings(plan_dir)
    if not findings:
        return None, "prior-findings-unavailable"
    degraded = {
        "mode": review_session_body.REVIEW_SCOPE_FULL,
        "prior_findings": findings,
    }
    raw_bases = cycle.get("repositories")
    if not isinstance(raw_bases, list):
        return degraded, "no-recorded-repository-bases"
    bases = {
        item.get("alias"): item
        for item in raw_bases
        if isinstance(item, Mapping) and isinstance(item.get("alias"), str)
    }
    repository_deltas: list[dict[str, Any]] = []
    all_changed: list[str] = []
    for repository in workspace.repositories:
        base_record = bases.get(repository.alias)
        if not isinstance(base_record, Mapping):
            return degraded, f"base-missing:{repository.alias}"
        base = base_record.get("head")
        if not isinstance(base, str) or not base:
            return degraded, f"no-recorded-base:{repository.alias}"
        dirty = base_record.get("worktree_dirty")
        raw_dirty_paths = base_record.get("dirty_paths")
        if dirty is False:
            baseline_dirty: list[str] = []
        elif dirty is True and isinstance(raw_dirty_paths, list):
            prefix = f"{repository.alias}:"
            baseline_dirty = [
                path[len(prefix) :]
                for path in raw_dirty_paths
                if isinstance(path, str) and path.startswith(prefix)
            ]
        else:
            return degraded, f"dirty-base-evidence-missing:{repository.alias}"
        try:
            resolved = review_guard._run_git(
                repository.root,
                ["rev-parse", "--verify", f"{base}^{{commit}}"],
            )
            ancestor = review_guard._run_git(
                repository.root,
                ["merge-base", "--is-ancestor", base, "HEAD"],
            )
            names = review_guard._run_git(
                repository.root,
                ["diff", "--name-only", *review_session_body._DELTA_DIFF_FLAGS, base],
            )
            index_names = review_guard._run_git(
                repository.root,
                [
                    "diff",
                    "--cached",
                    "--name-only",
                    *review_session_body._DELTA_DIFF_FLAGS,
                    base,
                ],
            )
            untracked = review_guard._run_git(
                repository.root,
                [
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "--full-name",
                    "--",
                    ":(top)",
                ],
            )
            index_flags = review_guard._run_git(
                repository.root,
                ["ls-files", "-v", "--full-name", "--", ":(top)"],
            )
        except RunError:
            return degraded, f"git-unavailable:{repository.alias}"
        probes = (
            (resolved, "base-unresolvable"),
            (ancestor, "base-not-ancestor"),
            (names, "base-diff-failed"),
            (index_names, "index-diff-failed"),
            (untracked, "untracked-scan-failed"),
            (index_flags, "index-flags-scan-failed"),
        )
        for result, reason in probes:
            if result.returncode != 0:
                return degraded, f"{reason}:{repository.alias}"
        relative_paths = sorted(
            {
                line
                for line in (
                    *names.stdout.splitlines(),
                    *index_names.stdout.splitlines(),
                    *untracked.stdout.splitlines(),
                    *review_guard._index_flagged_paths(index_flags.stdout),
                    *baseline_dirty,
                )
                if line
            }
        )
        qualified_paths = [f"{repository.alias}:{path}" for path in relative_paths]
        all_changed.extend(qualified_paths)
        repository_deltas.append(
            {
                "alias": repository.alias,
                "path": repository.repository.path,
                "base_sha": base,
                "changed_files": qualified_paths,
                "baseline_dirty": bool(baseline_dirty),
            }
        )
    return (
        {
            "mode": review_session_body.REVIEW_SCOPE_DELTA,
            "repositories": repository_deltas,
            "changed_files": all_changed,
            "prior_findings": findings,
        },
        None,
    )


# ---------------------------------------------------------------------------
# Pending-scope refresh
# ---------------------------------------------------------------------------


def _refresh_pending_review_scope(
    *,
    session: plans_models.SessionStatus,
    event_path: Path,
    plan_dir: Path,
    cwd: Path,
    workspace: workspaces.ResolvedWorkspace | None,
    run_id: str,
    persistence: run_persistence._RunPersistence,
) -> bool:
    """Re-derive a pending review session's scope, returning whether it moved.

    A pending delta review may have been written by an earlier run: its file
    list is static, and `git diff <base>` does not show untracked files, so an
    untracked file created since then would be invisible to the reviewer. The
    scope is therefore rebuilt from current Git evidence — widening to full
    whenever that evidence can no longer prove a narrower one — immediately
    before the session executes. Only the scope section is rewritten, so the
    session keeps its identity, version marker, and pending handoff.
    """
    text = session.path.read_text(encoding="utf-8")
    if review_session_body.REVIEW_SCOPE_HEADING not in text:
        return False
    if workspace is None:
        delta, fallback_reason = _compute_review_delta_context(
            event_path=event_path,
            plan_dir=plan_dir,
            cwd=cwd,
        )
    else:
        delta, fallback_reason = _compute_registered_review_delta_context(
            event_path=event_path,
            plan_dir=plan_dir,
            workspace=workspace,
        )
    rescoped = review_session_body.rescoped_review_session_body(text, delta)
    if rescoped is None or rescoped == text:
        return False
    session.path.write_text(rescoped, encoding="utf-8")
    persistence.append_event(
        event_path,
        events.REVIEW_SCOPE_EVENT,
        {
            "run_id": run_id,
            "session_id": session.id,
            "scope": (
                delta["mode"]
                if delta is not None
                else review_session_body.REVIEW_SCOPE_FULL
            ),
            "base_sha": (delta.get("base_sha") if delta is not None else None),
            **(
                {"repositories": delta.get("repositories")}
                if delta is not None and "repositories" in delta
                else {}
            ),
            "fallback_reason": fallback_reason,
            "refreshed": True,
        },
    )
    return True
