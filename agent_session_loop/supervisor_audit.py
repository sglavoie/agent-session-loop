"""Audit run configuration, independent role resolution, launch, and evidence.

This module owns the ``--audit-*`` half of the run flag surface: it reads the
resolved namespace, decides whether an audit is enabled, and resolves the
independent role the auditor would run as. It also launches that auditor, but
only ever under the read-only access profile and from a dedicated audit
directory, never with the worker's writable roots or bypass settings.

Independence is structural: an audit harness equal to the implementation
harness yields no role at all, so ASL can never quietly grade its own work. A
run whose configuration is valid and enabled but has no independent role
available starts (and ends) as ``unavailable`` rather than silently
self-auditing or pretending to be clean.

Safety is likewise structural rather than trusted. A harness that cannot
express the read-only profile is declared unavailable instead of run under a
weaker one, and the workspace is fingerprinted before and after every audit
turn so a sandbox that fails open is caught by evidence rather than by
assumption.

It also collects the bounded operational evidence an auditor reads. That
bundle is control-plane only: identities, paths, digests, exit codes, timings,
and guard results, never application source or semantic diffs. It is a pure
function of the run's persisted history, so two equivalent histories serialize
to the same bytes, and anything the size cap forces out is reported as a count,
a digest, and a reason rather than silently dropped.

Reading that bundle is a separate job from collecting it. The deterministic
checks turn known failure modes into reproducible verdicts: each answers
``pass``, ``fail``, or ``unavailable`` under a stable invariant key and names
the evidence it read. Evidence a check needed but could not see is
``unavailable``, never ``pass``, so a shed log or an unwritten result file
reads as a gap in the audit rather than as a clean run.

The same rule runs the other way for a claim built on a record's *absence*. The
size cap sheds by usefulness, not at random, so the records it keeps are the
ones that read as evidence of a problem and the records it drops are often the
ones that would refute them. A check that infers a fault from a record it
cannot find must therefore ask whether the log was shed before it counts the
absence as evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args

from . import (
    __version__,
    artifacts,
    assurance_ledger,
    assurance_models,
    assurance_projection,
    assurance_report,
    commit_groups,
    durability,
    events,
    harness_failures,
    harnesses,
    mailbox,
    model_availability,
    pause_policy,
    plans,
    preflight,
    repository_evidence,
    review,
    review_continuation,
    state,
    verification,
    workspaces,
)
from .assurance_models import (
    MAX_AUDIT_ATTEMPT_RECORDS,
    AssuranceError,
    AssuranceSummary,
    AttemptOutcome,
    AuditRole,
    AuditRoleSource,
    AuditState,
    CheckStatus,
    EvidenceKind,
    Finding,
    IssueObservation,
)
from .harnesses import HARNESS_NAMES
from .literals import as_literal

#: Seconds granted to each agent attempt, not to the audit as a whole.
DEFAULT_AUDIT_TIMEOUT_SECONDS = 600

#: Backoff before each delivery retry, indexed by how many delivery failures
#: have already been seen; the last entry repeats once the tuple is exhausted.
#: Deliberately not ``pause_policy.TRANSPORT_RETRY_DELAYS_SECONDS``: that
#: schedule is a published contract of the run loop, and an audit that waits
#: out a provider outage must be tunable without moving it.
AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (15.0, 60.0, 120.0)

#: Why an audit is not pending. Token-shaped so they survive
#: ``AssuranceSummary`` validation unchanged.
REASON_DRY_RUN = "dry-run"
REASON_OPTED_OUT = "opted-out"
REASON_NO_INDEPENDENT_ROLE = "no-independent-role"

#: Why a launched audit produced no usable verdict.
REASON_WORKSPACE_MUTATED = "workspace-mutated"
REASON_WORKSPACE_EVIDENCE_UNAVAILABLE = "workspace-evidence-unavailable"
REASON_HARNESS_FAILED = "harness-failed"
REASON_TIMEOUT = "timeout"

#: Why an audit produced no verdict before any auditor could be launched.
REASON_EVIDENCE_UNAVAILABLE = "evidence-unavailable"
REASON_INTERRUPTED = "interrupted"

#: The auditor's working directory, under the run directory so it is captured
#: with the rest of the run's evidence and never sits inside the workspace.
AUDIT_WORKDIR_NAME = "audit-workdir"

#: Run-relative artifact naming the mutation a read-only audit was not supposed
#: to be able to cause. It is the finding's only evidence reference.
MUTATION_ARTIFACT_NAME = "supervisor-audit-mutation.json"

#: Prefix of the private root the audited run's own commits are checked out
#: under for the boundary check. It sits outside the run directory on purpose:
#: the run directory is a read root for the auditor, and a checkout placed
#: there would hand it the application source the bundle exists to withhold.
PINNED_WORKSPACE_PREFIX = "asl-audit-pinned-"

#: Why a boundary check was judged on the live workspace rather than on a
#: checkout pinned to a fixed commit.
PIN_REASON_NOT_REQUESTED = "not-requested"
PIN_REASON_NO_RECORDED_COMMIT = "no-recorded-commit"
PIN_REASON_CHECKOUT_FAILED = "checkout-failed"

#: Run-relative artifact naming an audit that never reached an evidence bundle.
#: An unavailable audit has to point a reader at something it can open, and a
#: collection failure has no bundle and no attempt directory to offer.
UNAVAILABLE_ARTIFACT_NAME = "supervisor-audit-unavailable.json"

#: Run-relative directory the canonical reports are built in before they become
#: canonical. Nothing durable ever names a path inside it, so its contents are
#: scratch that only the publication that created them can be interested in.
AUDIT_STAGING_DIR_NAME = "supervisor-audit-staging"


class AuditCancelled(Exception):
    """A supervisor cancelled the audit while it was in flight.

    Raised instead of returning an attempt outcome so the cancel unwinds the
    whole audit at once: an operator asking a second time for the run to stop
    must not be answered by the next answer attempt or the next delivery
    retry.
    """


@dataclass(frozen=True)
class AuditConfig:
    """The effective audit settings a run persists in its ``run_config``."""

    enabled: bool = True
    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout_seconds: int = DEFAULT_AUDIT_TIMEOUT_SECONDS

    def to_run_config(self) -> dict[str, Any]:
        """Return the ``run_config`` fragment, keyed by ``RUN_FLAG_SPEC`` dests."""
        return {
            "supervisor_audit": self.enabled,
            "audit_harness": self.harness,
            "audit_harness_model": self.model,
            "audit_effort": self.effort,
            "audit_timeout": self.timeout_seconds,
        }


def config_from_namespace(args: argparse.Namespace) -> AuditConfig:
    """Read the audit settings off a resolved run namespace.

    Attributes are read defensively so a partial namespace resolves to the
    defaults a new run would persist: enabled, with the default per-attempt
    timeout.
    """
    enabled = getattr(args, "supervisor_audit", None)
    timeout = getattr(args, "audit_timeout", None)
    return AuditConfig(
        enabled=True if enabled is None else bool(enabled),
        harness=getattr(args, "audit_harness", None),
        model=getattr(args, "audit_harness_model", None),
        effort=getattr(args, "audit_effort", None),
        timeout_seconds=(
            DEFAULT_AUDIT_TIMEOUT_SECONDS if timeout is None else int(timeout)
        ),
    )


def resolve_role(
    config: AuditConfig,
    *,
    implementation_harness: str,
    review_harness: str | None = None,
    review_model: str | None = None,
) -> AuditRole | None:
    """Resolve the independent audit role, or ``None`` when none exists.

    An explicit ``--audit-harness`` wins; otherwise the effective review harness
    is inherited along with its model. An explicit audit model beats both the
    inherited review model and the audit harness native default, and audit
    effort never inherits from ``--effort``.
    """
    if config.harness is not None:
        harness = config.harness
        source: AuditRoleSource = "explicit"
        inherited_model: str | None = None
    elif review_harness is not None:
        harness = review_harness
        source = "review"
        inherited_model = review_model
    else:
        return None
    # Both names are free strings here -- `asl assurance process-pending`
    # accepts an unconstrained `--audit-harness`, and the implementation
    # harness is read back off a recorded run config -- so the closed
    # vocabulary is applied where the role is built. These are the same two
    # checks `AuditRole.__post_init__` makes, with the same labels and the
    # same error, moved one frame out so the narrowed type is what reaches
    # the durable record. Both are narrowed *before* the equality test:
    # otherwise an equal pair of unpermitted names would return early and a
    # malformed configuration would read as "no independent role" instead of
    # failing closed.
    selected = as_literal(
        harness, HARNESS_NAMES, "role 'harness'", error=AssuranceError
    )
    implementation = as_literal(
        implementation_harness,
        HARNESS_NAMES,
        "role 'implementation_harness'",
        error=AssuranceError,
    )
    if selected == implementation:
        return None
    return AuditRole(
        harness=selected,
        implementation_harness=implementation,
        source=source,
        timeout_seconds=float(config.timeout_seconds),
        model=config.model if config.model is not None else inherited_model,
        effort=config.effort,
    )


def attempt_timeouts(role: AuditRole) -> tuple[float, ...]:
    """Return the per-attempt timeout budget.

    The timeout is not a total budget: every one of the at most
    ``MAX_AUDIT_ATTEMPT_RECORDS`` attempts gets the full value, so a first
    attempt that burns its timeout does not shorten the retry.
    """
    return tuple(role.timeout_seconds for _ in range(MAX_AUDIT_ATTEMPT_RECORDS))


@dataclass(frozen=True)
class AuditPlan:
    """The audit decision for one run, before anything is launched."""

    config: AuditConfig
    role: AuditRole | None
    state: AuditState
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "state": self.state,
            "reason": self.reason,
            "timeout_seconds": self.config.timeout_seconds,
            "attempt_timeouts": (
                [] if self.role is None else list(attempt_timeouts(self.role))
            ),
            "role": None if self.role is None else self.role.to_dict(),
        }


def plan_audit(args: argparse.Namespace) -> AuditPlan:
    """Decide what an eligible run would do with its audit configuration."""
    config = config_from_namespace(args)
    if not config.enabled:
        return AuditPlan(
            config=config, role=None, state="skipped", reason=REASON_OPTED_OUT
        )
    role = resolve_role(
        config,
        implementation_harness=args.harness,
        review_harness=getattr(args, "review_harness", None),
        review_model=getattr(args, "review_harness_model", None),
    )
    if role is None:
        return AuditPlan(
            config=config,
            role=None,
            state="unavailable",
            reason=REASON_NO_INDEPENDENT_ROLE,
        )
    return AuditPlan(config=config, role=role, state="pending", reason=None)


def initial_summary(
    plan: AuditPlan, *, run_id: str, eligible: bool
) -> AssuranceSummary:
    """Return the assurance object a starting run persists.

    Only an eligible run can be ``pending``. Eligibility begins once a run ID
    and run directory exist, so a dry run records a skip instead: no reader
    should mistake a rehearsal for an audit that is still owed.
    """
    if not eligible:
        return AssuranceSummary(
            run_id=run_id, state="skipped", eligible=False, reason=REASON_DRY_RUN
        )
    return AssuranceSummary(
        run_id=run_id,
        state=plan.state,
        eligible=True,
        reason=plan.reason,
        role=plan.role,
    )


def cancelled_summary(plan: AuditPlan, *, run_id: str) -> AssuranceSummary:
    """Return the assurance a cancelled audit records.

    Cancelling an audit cancels only the audit. The run's outcome is already
    decided, so what is left to say is that its audit could not be judged.
    """
    return AssuranceSummary(
        run_id=run_id,
        state="unavailable",
        eligible=True,
        reason=REASON_INTERRUPTED,
        role=plan.role,
    )


def audit_workdir(run_dir: Path | str) -> Path:
    """Create and return the dedicated directory the auditor runs from.

    The auditor never runs from the workspace: a harness that resolves relative
    paths against its cwd would otherwise be one bug away from touching the
    application tree it is grading.
    """
    workdir = Path(run_dir) / AUDIT_WORKDIR_NAME
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def audit_read_roots(run_dir: Path | str) -> tuple[Path, ...]:
    """The directories an auditor may read.

    The run directory is the evidence bundle; the ASL package and skill trees
    are what the rubric is judged against. All three are handed over as read
    access only — no writable application root is ever among them.
    """
    package_root = Path(__file__).resolve().parent
    roots = [Path(run_dir).resolve(), package_root]
    skills_root = package_root.parent / "skills"
    if skills_root.is_dir():
        roots.append(skills_root)
    return tuple(roots)


def build_audit_invocation(
    role: AuditRole,
    *,
    run_dir: Path | str,
    prompt: str,
    last_message_path: Path | None = None,
) -> harnesses.HarnessInvocation:
    """Build the restricted invocation for one audit attempt.

    ``plan_dir`` is deliberately absent: it is the worker's writable grant, and
    passing it here would be exactly the leak this profile exists to prevent.
    """
    workdir = audit_workdir(run_dir)
    adapter = harnesses.get_adapter(role.harness)
    return adapter.build_invocation(
        cwd=workdir,
        plan_dir=None,
        prompt=prompt,
        unsafe_bypass=False,
        last_message_path=last_message_path,
        model=role.model,
        effort=role.effort,
        access=harnesses.audit_read_only_access(audit_read_roots(run_dir)),
    )


@dataclass(frozen=True)
class AuditAttemptResult:
    """What one restricted audit attempt produced, before grading its content."""

    outcome: AttemptOutcome
    state: AuditState
    reason: str | None = None
    argv: tuple[str, ...] = ()
    returncode: int | None = None
    last_message: str = ""
    mutation: dict[str, Any] | None = None
    issue_id: str | None = None
    classification: harness_failures.FailureClassification | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


#: Ref namespace holding a cache of some other repository's branches. It is
#: written by ``git fetch`` and by nothing a working tree does.
_REMOTE_TRACKING_REF_PREFIX = "refs/remotes/"

#: What a repository's comparison entry names when something moved in it. A
#: fingerprint difference outside these keys and ``head_changed`` cannot be
#: named, and in practice cannot occur: the diff digests are the only other
#: content in the fingerprint, and a diff whose text moves moves the per-path
#: patch digest that feeds ``tracked`` with it.
_MUTATION_CHANGE_KEYS = (
    "tracked",
    "untracked_added",
    "untracked_removed",
    "untracked_changed",
    "worktree_ignore",
    "ignore_config",
    "refs_added",
    "refs_removed",
    "refs_changed",
)


def _without_remote_tracking_refs(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the ref moves a pinned checkout shares with every other one.

    A linked worktree gets its own ``HEAD``, index, and working tree, but the
    ref store stays the origin repository's. A remote-tracking ref therefore
    moves under the auditor's feet whenever anything else fetches, and the pin
    that makes every other difference attributable does not isolate it. Counting
    it charged an auditor with a mutation it could not have made, and one that
    changed no file, no local branch, and no commit besides (ASI-13).
    """
    filtered = dict(changes)
    for key in ("refs_added", "refs_removed", "refs_changed"):
        entries = _sequence(changes.get(key))
        if not entries:
            continue
        filtered[key] = [
            entry
            for entry in entries
            if not str(_mapping(entry).get("ref") or "").startswith(
                _REMOTE_TRACKING_REF_PREFIX
            )
        ]
    return filtered


def _mutation_comparison(comparison: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the comparison when it proves a change, else ``None``.

    Unavailable repository evidence is not a mutation: it is the absence of
    proof either way, and treating it as one would turn every transient git
    failure into a fabricated integrity issue. A repository whose only remaining
    difference is a remote-tracking ref is dropped for the same reason -- see
    :func:`_without_remote_tracking_refs` -- and a comparison left with nothing
    else proves no mutation at all.
    """
    changed = {
        repository: filtered
        for repository, raw in _mapping(comparison.get("changed")).items()
        for filtered in (_without_remote_tracking_refs(_mapping(raw)),)
        if filtered.get("head_changed") is True
        or any(filtered.get(key) for key in _MUTATION_CHANGE_KEYS)
    }
    added = comparison.get("added") or []
    removed = comparison.get("removed") or []
    if not (changed or added or removed):
        return None
    return {
        "changed": changed,
        "added": list(added),
        "removed": list(removed),
        "unavailable": dict(comparison.get("unavailable") or {}),
    }


# The checks below decide when registered evidence is too thin to accept a
# verdict over. A path omitted from the before snapshot refuses the audit
# outright, and so does a repository the plan really registered whose evidence
# is already unavailable there: unavailable on both sides compares as unchanged,
# so an auditor could write inside it and still be graded clean. A path whose
# evidence stops or starts being probeable across the turn refuses the verdict
# after the turn. Evidence unavailable on both sides is tolerated only for a
# synthesized registry, because a plan that registered nothing names its run
# root as its one repository even when that root is not a worktree root, and
# refusing there would withdraw every such run's audit permanently.
def _evidence_unprobeable(fingerprint: Mapping[str, Any] | None) -> bool:
    """Report whether one snapshot entry carries no comparable content evidence."""
    if not fingerprint:
        return True
    return (
        not fingerprint.get("git")
        or repository_evidence.repository_evidence_unavailable(fingerprint) is not None
    )


def _snapshot_omits_registered(
    snapshot: Mapping[str, Mapping[str, Any]],
    repositories: Sequence[str],
) -> bool:
    """Report whether the snapshot has no entry for some registered repository.

    ``capture_registered_snapshot`` omits a declared path that is missing, is
    not a directory, or no longer names the node it declares. An omitted path
    contributes nothing to the comparison, so a repository omitted from both
    sides — a checkout that was already a symlink when the audit started — would
    otherwise compare as silently unchanged.
    """
    return any(relative not in snapshot for relative in repositories)


def _registered_evidence_unavailable(
    snapshot: Mapping[str, Mapping[str, Any]],
    repositories: Sequence[str],
) -> bool:
    """Report whether some registered repository is present but unfingerprintable.

    ``capture_registered_snapshot`` records fail-closed ``repository-disappeared``
    evidence for a declared directory that is not its own Git worktree root, so a
    checkout swapped for a plain directory keeps its key while carrying nothing
    the comparison can read.
    """
    return any(
        _evidence_unprobeable(snapshot.get(relative)) for relative in repositories
    )


def _observability_changed(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
    repositories: Sequence[str],
) -> bool:
    """Report whether any registered repository stopped or started being probeable.

    A repository the audit could fingerprint before the turn and not after it
    (or the reverse) has no comparable evidence, which is silence rather than
    proof of an intact boundary.
    """
    return any(
        _evidence_unprobeable(before.get(relative))
        != _evidence_unprobeable(after.get(relative))
        for relative in repositories
    )


@dataclass(frozen=True)
class PinnedWorkspace:
    """The workspace tree one audit's read-only boundary check is judged on.

    ``reason`` is set only when no pinned checkout could be made, in which case
    ``root`` is the live workspace root and the check falls back to comparing
    that across the turn.
    """

    root: Path
    commits: Mapping[str, str] = field(default_factory=dict)
    reason: str | None = None

    @property
    def pinned(self) -> bool:
        return self.reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pinned": self.pinned,
            "root": str(self.root),
            "commits": dict(self.commits),
            "reason": self.reason,
        }


def run_commits(
    run_dir: Path | str, repositories: Sequence[str]
) -> dict[str, str] | None:
    """Where the run left each registered repository's HEAD, or ``None``.

    ``None`` when even one registered repository has no recorded commit result,
    because a partial pin would leave the rest of the boundary check on the live
    tree — the mixture the pin exists to avoid.

    ``head_after`` rather than ``commit_sha`` is what a pinned checkout has to
    reproduce: the latter is recorded only for a commit that succeeded, while
    the former is where the repository was actually left either way.
    """
    latest: dict[str, tuple[int, str]] = {}
    try:
        directories = [
            entry
            for entry in Path(run_dir).iterdir()
            if entry.is_dir() and entry.name.startswith(_COMMIT_DIR_PREFIX)
        ]
    except OSError:
        return None
    for directory in directories:
        result, _ = _read_json_file(directory / state.RESULT_FILE_NAME)
        if not isinstance(result, Mapping):
            continue
        relative = result.get("repository_path")
        head = result.get("head_after")
        if not isinstance(relative, str) or not isinstance(head, str) or not head:
            continue
        index = result.get("commit_index")
        order = index if isinstance(index, int) else 0
        if relative not in latest or latest[relative][0] <= order:
            latest[relative] = (order, head)
    if any(relative not in latest for relative in repositories):
        return None
    return {relative: latest[relative][1] for relative in repositories}


def _pin_depth(relative: str) -> tuple[int, str]:
    """Order registered paths so an ancestor is checked out before a descendant."""
    return (0 if relative == "." else relative.count("/") + 1, relative)


@contextlib.contextmanager
def pinned_audit_workspace(
    workspace_root: Path | str,
    repositories: Sequence[str],
    commits: Mapping[str, str] | None,
) -> Iterator[PinnedWorkspace]:
    """Check the pinned commits out privately for the boundary check.

    The check asks whether the audit turn wrote to a registered repository, and
    on the live workspace that question has no honest answer: a concurrent run,
    or an operator with an editor open, changes the same tree for reasons this
    audit had nothing to do with, and the comparison reports it as the auditor's
    doing. A detached checkout of the commits the run itself recorded is a tree
    with no other writer, so a difference across the turn is evidence again
    rather than a coincidence.

    Pinning is all-or-nothing. A repository that cannot be checked out abandons
    the whole pin and yields the live root, because half a pinned comparison
    carries the false positive it was meant to remove while reading as though it
    did not.

    The checkouts are always removed, including their registration in the origin
    repository, so an audit cannot leave the repositories it graded listing
    worktrees that no longer exist.
    """
    live = Path(workspace_root)
    if not repositories:
        yield PinnedWorkspace(root=live, reason=PIN_REASON_NOT_REQUESTED)
        return
    if commits is None:
        yield PinnedWorkspace(root=live, reason=PIN_REASON_NO_RECORDED_COMMIT)
        return

    root = Path(tempfile.mkdtemp(prefix=PINNED_WORKSPACE_PREFIX))
    added: list[tuple[Path, Path]] = []
    try:
        try:
            # Shallowest first, so a repository registered at "." is checked out
            # into the still-empty root before anything nested lands under it.
            for relative in sorted(repositories, key=_pin_depth):
                origin = live / relative
                destination = repository_evidence.add_detached_worktree(
                    origin, root / relative, commits[relative]
                )
                added.append((origin, destination))
        except repository_evidence.RepositoryEvidenceError, OSError:
            yield PinnedWorkspace(root=live, reason=PIN_REASON_CHECKOUT_FAILED)
            return
        yield PinnedWorkspace(root=root, commits=dict(commits))
    finally:
        for origin, destination in reversed(added):
            repository_evidence.remove_worktree(origin, destination)
        shutil.rmtree(root, ignore_errors=True)


def _record_mutation_issue(
    mutation: Mapping[str, Any],
    *,
    role: AuditRole,
    run_dir: Path,
    run_id: str,
    state_dir: Path | str | None,
    observed_at: str,
) -> str | None:
    """Persist the mutation artifact and open a system issue for it.

    Best effort by design: a ledger or disk failure must not swallow the
    ``unavailable`` verdict the caller already reached, so this returns ``None``
    instead of raising.
    """
    try:
        assurance_models.write_audit_json(
            run_dir,
            MUTATION_ARTIFACT_NAME,
            {
                "run_id": run_id,
                "observed_at": observed_at,
                "role": role.to_dict(),
                "comparison": mutation,
            },
        )
    except OSError, assurance_models.AssuranceError:
        return None
    try:
        record = assurance_ledger.append_issue_observation(
            IssueObservation(
                run_id=run_id,
                finding=Finding(
                    category="audit-integrity",
                    component="supervisor-audit",
                    invariant_key="audit-read-only-mutation",
                    lane="mutation-commit-guards",
                    severity="critical",
                    summary=(
                        "The supervisor audit ran under the read-only access "
                        "profile, but repository evidence changed while it was "
                        "running."
                    ),
                    recommendation=(
                        "Treat the audit verdict as unavailable and confirm the "
                        f"{role.harness} read-only profile is still enforced by "
                        "the installed harness before auditing again."
                    ),
                    evidence=(MUTATION_ARTIFACT_NAME,),
                ),
                observed_at=observed_at,
                audit_artifact=MUTATION_ARTIFACT_NAME,
            ),
            state_dir=state_dir,
        )
    except OSError, assurance_ledger.LedgerError, assurance_models.AssuranceError:
        return None
    return record.issue_id


def run_restricted_audit(
    role: AuditRole,
    *,
    run_dir: Path | str,
    run_id: str,
    workspace_root: Path | str,
    plan_dir: Path | str,
    prompt: str,
    repositories: Sequence[str],
    registry_declared: bool = True,
    pinned: PinnedWorkspace | None = None,
    state_dir: Path | str | None = None,
    timeout: int | None = None,
    stream_callback: harnesses.StreamCallback | None = None,
    abort: harnesses.AbortSignal | None = None,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
) -> AuditAttemptResult:
    """Run one audit attempt under the read-only profile, guarded by evidence.

    ``repositories`` holds the workspace-relative paths the plan registered,
    and they alone are fingerprinted on both sides of the turn. Any change to
    one of them makes the audit unavailable regardless of what the auditor
    reported, because a read-only turn that mutated a registered repository has
    already disproved the boundary its verdict depends on. A registered
    repository the snapshot cannot name at all, one whose evidence is already
    unavailable before the turn, or one whose evidence stops (or starts) being
    probeable across the turn, makes the audit unavailable for the same reason:
    silence about a repository is not proof that it was left alone, and no
    verdict may be accepted over it. Worktrees the plan never registered are
    outside that boundary: concurrent runs write to them routinely, so reading
    their state as this auditor's doing would report a breach nobody committed.

    ``registry_declared`` is the caller's statement that ``repositories`` came
    from a plan registry. A run whose plan registered nothing is audited over a
    synthesized run root instead, which is routinely not a worktree root and
    therefore carries permanently unavailable evidence; passing ``False`` keeps
    that compatibility case auditable without extending the same tolerance to a
    repository a plan really registered.

    ``pinned`` names the tree the comparison is made over. A checkout pinned to
    the run's own commits has no writer but this audit, so a difference across
    the turn is the auditor's doing; the live workspace has other writers, and
    falling back to it keeps the check running while widening what a reported
    difference could mean. Its linked worktrees are enumerated only in that
    fallback: from inside a pinned checkout, Git lists the live trees the pin
    exists to stay out of.

    ``abort`` lets a supervisor cancel the attempt mid-turn; the cancel is
    raised as :class:`AuditCancelled` rather than returned, so no caller can
    mistake it for a failed attempt worth another try.
    """
    run_dir = Path(run_dir)
    unavailable = harnesses.audit_read_only_unavailable_reason(role.harness)
    if unavailable is not None:
        return AuditAttemptResult(
            outcome="unavailable", state="unavailable", reason=unavailable
        )
    guard = (
        PinnedWorkspace(root=Path(workspace_root), reason=PIN_REASON_NOT_REQUESTED)
        if pinned is None
        else pinned
    )

    def snapshot() -> repository_evidence.WorkspaceSnapshot:
        return repository_evidence.capture_registered_snapshot(
            guard.root,
            plan_dir,
            tuple(repositories),
            include_linked_worktrees=not guard.pinned,
        )

    try:
        before = snapshot()
    except repository_evidence.RepositoryEvidenceError:
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
        )
    if _snapshot_omits_registered(before, repositories) or (
        registry_declared and _registered_evidence_unavailable(before, repositories)
    ):
        # Nothing is launched: a registered path the snapshot cannot name, or
        # one it can name but not fingerprint, contributes the same evidence to
        # both sides of the comparison, so the boundary check would pass over it
        # no matter what the auditor did.
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
        )

    workdir = audit_workdir(run_dir)
    last_message_path = workdir / "last-message.txt"
    run_harness = harnesses.run_harness if _run_harness_fn is None else _run_harness_fn
    try:
        result = run_harness(
            role.harness,
            cwd=workdir,
            plan_dir=None,
            prompt=prompt,
            unsafe_bypass=False,
            last_message_path=last_message_path,
            timeout=int(role.timeout_seconds) if timeout is None else timeout,
            stream_callback=stream_callback,
            model=role.model,
            effort=role.effort,
            access=harnesses.audit_read_only_access(audit_read_roots(run_dir)),
            abort=abort,
        )
    except harnesses.HarnessError as error:
        return AuditAttemptResult(
            outcome="unavailable", state="unavailable", reason=str(error)
        )

    argv = tuple(result.argv)
    try:
        after = snapshot()
    except repository_evidence.RepositoryEvidenceError:
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )

    mutation = _mutation_comparison(
        repository_evidence.compare_workspace_snapshots(before, after)
    )
    if mutation is not None:
        mutation["workspace"] = guard.to_dict()
        clock = datetime.now(UTC) if now is None else now()
        return AuditAttemptResult(
            outcome="mutation-detected",
            state="unavailable",
            reason=REASON_WORKSPACE_MUTATED,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
            mutation=mutation,
            issue_id=_record_mutation_issue(
                mutation,
                role=role,
                run_dir=run_dir,
                run_id=run_id,
                state_dir=state_dir,
                observed_at=clock.isoformat(),
            ),
        )

    if _observability_changed(before, after, repositories):
        return AuditAttemptResult(
            outcome="unavailable",
            state="unavailable",
            reason=REASON_WORKSPACE_EVIDENCE_UNAVAILABLE,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )

    if result.aborted:
        # After the boundary check, so a cancelled auditor that wrote to a
        # registered repository is still reported; before every failure branch,
        # so the kill is never read as a transport error worth retrying.
        raise AuditCancelled(REASON_INTERRUPTED)
    if result.interrupted:
        # Ahead of the exit code, because the harness reports an interrupt as a
        # nonzero return: reading it as a transport failure would answer the
        # operator's first Ctrl+C with a second full audit turn.
        return AuditAttemptResult(
            outcome="interrupted",
            state="unavailable",
            reason=REASON_INTERRUPTED,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )
    if result.timed_out:
        return AuditAttemptResult(
            outcome="timeout",
            state="unavailable",
            reason=REASON_TIMEOUT,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )
    if result.returncode != 0:
        # A bare "harness-failed" is unreadable a week later, so the launch is
        # classified with the same rules the session loop uses and the streams
        # that carried the provider's own words are kept alongside it.
        return AuditAttemptResult(
            outcome="transport-error",
            state="unavailable",
            reason=REASON_HARNESS_FAILED,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
            classification=_redacted_classification(result),
            stdout_tail=_harness_tail(result.stdout),
            stderr_tail=_harness_tail(result.stderr),
        )
    return AuditAttemptResult(
        outcome="ok",
        state="pending",
        argv=argv,
        returncode=result.returncode,
        last_message=result.last_message,
    )


# ---------------------------------------------------------------------------
# Bounded operational evidence
# ---------------------------------------------------------------------------

#: Version of the ``supervisor-audit-input.json`` payload. Version 2 renamed the
#: events and mailbox summaries' ``rejected_count`` to
#: :data:`UNPARSABLE_LINE_COUNT_KEY`.
AUDIT_INPUT_SCHEMA_VERSION = 2

#: How many lines of a JSONL log would not parse. It sat beside
#: ``counts.turn_rejected`` under the name ``rejected_count``, and the auditing
#: agent read the two as one number: a summary saying zero rejections next to a
#: ``turn_rejected`` record looked like a contradiction, and was filed as one 46
#: times over runs that had all behaved correctly. A field an auditor is asked
#: to reason about has to say what it counts.
UNPARSABLE_LINE_COUNT_KEY = "unparsable_line_count"

#: What the same count was called before schema version 2. Bundles written under
#: version 1 are still the corpus every replay proof is drawn from, so the
#: reader falls back to the old spelling rather than reading a damaged log as an
#: undamaged one.
LEGACY_UNPARSABLE_LINE_COUNT_KEY = "rejected_count"

#: Serialized cap on one audit input bundle, well under the
#: ``assurance_models.MAX_ARTIFACT_BYTES`` ceiling the writer enforces.
AUDIT_INPUT_MAX_BYTES = 512 * 1024

#: How much of a refused bundle the oversized-input retry keeps. A ratio rather
#: than a smaller fixed ceiling because the observed refusals were not at one
#: size — 357 KB was refused where 550 KB went through — so the provider's limit
#: is in tokens, which bytes only approximate. Halving whatever was actually
#: refused moves it decisively under that limit without asserting a byte figure
#: the evidence does not support, and lowering
#: :data:`AUDIT_INPUT_MAX_BYTES` for every run would pay that cost on the runs
#: that never had the problem.
AUDIT_INPUT_OVERFLOW_SHRINK_RATIO = 0.5

#: Per-side preview budget for a captured log or last message.
PREVIEW_HEAD_BYTES = 2 * 1024
PREVIEW_TAIL_BYTES = 2 * 1024

#: Smallest preview the size cap shrinks to before dropping the text entirely.
MIN_PREVIEW_BYTES = 128

#: Longest string kept verbatim inside a copied control-plane payload.
MAX_INLINE_STRING_BYTES = 512

#: Entries kept from any one copied list before the remainder is counted.
MAX_SEQUENCE_ITEMS = 100

#: Files hashed when fingerprinting the ASL package or the skill tree.
MAX_TREE_FILES = 2000

#: Directories a tree digest walks past: the interpreter writes them, nobody
#: authors them.
GENERATED_TREE_DIRECTORIES = frozenset({"__pycache__"})

#: Suffixes of compiled artifacts a tree digest ignores, so equivalent sources
#: hash alike.
GENERATED_TREE_SUFFIXES = frozenset({".pyc", ".pyo"})

#: Turns kept regardless of age, on top of every anomalous one.
RECENT_TURN_COUNT = 3

#: Stand-in for a value withheld because it looked like a credential.
REDACTED = "[redacted]"

#: Why something is absent from the bundle. Token-shaped so they can be read
#: back as reasons without further parsing.
REASON_SIZE_CAP = "size-cap"
REASON_MISSING = "missing"
REASON_UNREADABLE = "unreadable"
REASON_MALFORMED = "malformed"
REASON_OUTSIDE_APPROVED_ROOTS = "outside-approved-roots"
REASON_NOT_RECORDED = "not-recorded"
REASON_APPLICATION_CONTENT = "application-content-excluded"

#: Symbolic names for the roots every reference in the bundle resolves under.
EVIDENCE_ROOT_RUN = "run"
EVIDENCE_ROOT_STATE = "state"
EVIDENCE_ROOT_PLAN = "plan"
EVIDENCE_ROOT_WORKSPACE = "workspace"
EVIDENCE_ROOT_PACKAGE = "package"
EVIDENCE_ROOT_SKILLS = "skills"

#: Keys whose value is application output or a semantic diff rather than
#: control-plane evidence. Their digest and size are kept; their bytes are not.
_CONTENT_KEYS = frozenset(
    {
        "body",
        "content",
        "diff",
        "output",
        "patch",
        "source",
        "stderr",
        "stdout",
        "text",
    }
)

#: Mailbox kinds the bundle gives up first under the size cap. They time-stamp
#: an exchange; the question, the ruling on it, and the notice never go.
_SHEDDABLE_MAILBOX_KINDS = frozenset(mailbox.LIFECYCLE_KINDS) | {mailbox.KIND_ACK}

#: Fields through which a mailbox record joins an exchange it does not name a
#: run for: a reply and a resolution point at their question, a receipt at the
#: closure it acknowledges, and a wait record at the wait it opened.
_MAILBOX_JOIN_KEYS = ("to", "wait_id", "closure_id")

#: How a mailbox record the collector claimed by delivery is marked, as opposed
#: to one that named this run outright or joined an exchange that did.
MAILBOX_OWNERSHIP_DELIVERED = "delivered"

#: Keys naming a Git origin, which is canonicalized rather than copied so a
#: credential embedded in a remote URL cannot reach the bundle.
_ORIGIN_KEYS = frozenset({"origin", "origins", "remote", "remote_url", "url"})

#: The bounded enum a reply origin is drawn from, mirroring
#: ``run_prompts.REPLY_ORIGINS``. No Git remote can collide with these tokens.
_REPLY_ORIGIN_VALUES = frozenset({"argv", "stdin", "mailbox"})

#: The only values ``assurance.role.source`` may carry.  ``source`` is
#: otherwise application content and remains withheld; these bounded enums are
#: control-plane provenance validated by :class:`assurance_models.AuditRole`.
_AUDIT_ROLE_SOURCE_VALUES = frozenset(assurance_models.AUDIT_ROLE_SOURCES)

#: Closed set of tokens a ``source`` may carry to name *where a runner control
#: signal was read*, as opposed to application output. Deliberately a value
#: allowlist rather than a parent-name exemption: a live run writes this
#: provenance under several different parents, while a ``source`` holding real
#: application text must stay withheld under every one of them.
#:
#: Mirrors, rather than imports, the values the run-loop boundaries above this
#: module own — this module is imported *by* them, so naming the values keeps
#: the layering one way round: ``get_args(harnesses.StreamName)``,
#: ``turn_execution.TRUSTED_RUNNER_SIGNAL_SOURCES`` minus the streams,
#: ``turn_execution.RUNNER_STATUS_SOURCE_ABSENT``, and the ``"pause"`` literal
#: ``run_termination`` stamps when the pause itself, not a stream, is the origin.
_CONTROL_PLANE_SOURCE_VALUES = frozenset(get_args(harnesses.StreamName)) | frozenset(
    {"last_message", "absent", "pause"}
)

#: Field parents under which ``origin`` names how an operator reply reached the
#: run rather than a repository. ``None`` covers the reply event, which carries
#: the enum at its payload root.
_REPLY_ORIGIN_PARENTS = frozenset({None, "reply_source", "source"})

#: Keys holding a process environment, whose values are withheld wholesale.
_ENVIRONMENT_KEYS = frozenset({"env", "environ", "environment"})

#: A key is secret-bearing when one of these words is the whole key or a whole
#: word inside it. The trailing boundary is what keeps ``auth`` from swallowing
#: ``authorized_aliases``; it admits a plural and an upper-case camelCase
#: continuation (``authToken``) so narrowing the match sheds no real secret.
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[-_.])(?:api[-_]?key|access[-_]?key|auth|authorization|bearer|"
    r"cookie|credential|credentials|passphrase|passwd|password|private[-_]?key|"
    r"pwd|secret|session[-_]?key|signature|token)s?(?=$|[-_.]|(?-i:[A-Z]))"
)
_SECRET_VALUE_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
_CREDENTIAL_URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")

#: Keys whose value is a command line. Argv is redacted positionally as well as
#: by shape, because a secret usually arrives as the argument after its flag.
_ARGV_KEYS = frozenset({"argv", "args", "command", "cmd"})
_SECRET_FLAG_RE = re.compile(
    r"(?i)^--?(?:api[-_]?key|auth|authorization|bearer|credential|credentials|"
    r"key|passphrase|passwd|password|pwd|secret|token)$"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)^(--?(?:api[-_]?key|auth|authorization|bearer|credential|credentials|"
    r"key|passphrase|passwd|password|pwd|secret|token)=)(.+)$"
)

#: Events emitted per heartbeat or per delivery. They dominate a long run's log
#: and say the least about it, so they are the first thing the cap sheds.
_BULK_EVENT_NAMES = frozenset(
    {
        events.AGENT_MUX_DELIVERY_EVENT,
        events.PROCESS_SWEEP_EVENT,
        events.TURN_HEARTBEAT_EVENT,
        events.VERIFICATION_CHECK_STARTED_EVENT,
        events.VERIFICATION_HEARTBEAT_EVENT,
    }
)

#: Events that are themselves the anomaly, a guard result, or the run's
#: terminal record. They are shed last and only when nothing else is left.
_ANOMALY_EVENT_NAMES = frozenset(
    {
        events.ACTION_REQUIRED_EVENT,
        events.PREFLIGHT_AUTO_REPAIRED_EVENT,
        events.RESOLUTION_CONSUMED_EVENT,
        events.REVIEW_REPAIR_REOPENED_EVENT,
        events.REVIEW_VERDICT_RETRY_EVENT,
        events.RUN_CONTROL_EVENT,
        events.RUN_FINISHED_EVENT,
        events.RUN_PAUSED_EVENT,
        events.TURN_HANG_CLEARED_EVENT,
        events.TURN_HANG_SUSPECTED_EVENT,
        events.VERIFICATION_CHECK_SKIPPED_EVENT,
        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
    }
)

_RUN_STARTED_EVENT = events.RUN_STARTED_EVENT
_TERMINAL_EVENT_NAMES = frozenset({events.RUN_FINISHED_EVENT, events.RUN_PAUSED_EVENT})

#: State keys copied verbatim (after redaction) into each projection.
_REVIEW_STATE_KEYS = (
    "review_artifact_sha256",
    "review_completed",
    "review_cycles",
    "review_harness",
    "review_output_path",
    "review_output_sha256",
    "review_requested",
    "review_session_executed",
    "review_session_id",
    "review_skipped_reason",
    "review_verdict",
)
_REPAIR_STATE_KEYS = (
    "review_fix_disposition",
    "review_reopen",
    "review_resume",
)
#: Mirrors ``commit_settlement.PAUSED_WORK_COMMIT_KEY`` as a literal. Importing
#: that module would drag the commit ledger, the loop's reporting and turn
#: execution into every audit; the bundle reads state as data, so a literal is
#: what the projection needs anyway.
PAUSED_WORK_COMMIT_KEY = "paused_work_commit"

_COMMIT_STATE_KEYS = (
    "commit_mode",
    "repair_commit_mode",
    "commit_groups",
    "commit_failures",
    "unresolved_commit_groups",
    "commits",
    # The one commit a run makes that no accepted turn produced. Without it the
    # bundle shows a `session-paused` group and a commit with no explanation for
    # either; with it, the settlement's own verdict travels beside them.
    PAUSED_WORK_COMMIT_KEY,
)
_COMMIT_DIR_PREFIX = "commit-"

#: How a projected commit was learned. The runner's own commit records carry no
#: origin, so this marks exactly the entries the audit derived from the write
#: guard rather than from a commit the runner ran itself.
OBSERVED_COMMIT_ORIGIN = "observed-write-guard"
_STATUS_STATE_KEYS = (
    "action_required",
    # A run that superseded a live ask on its way into a runner-declared pause
    # wrote the displaced record here. Projecting it is what stops the bundle
    # from showing an `action_required` of null with nothing saying what stood
    # there.
    events.ACTION_REQUIRED_SUPERSEDED_KEY,
    "assurance",
    "command",
    "completed_session_ids",
    "completed_sessions",
    "control",
    "details",
    "dry_run",
    "duration_seconds",
    "ok",
    "pause_details",
    "pause_message",
    "pause_reason",
    "pause_policy",
    "paused",
    "readiness_gate",
    "readiness_gate_reopened",
    "readiness_state_active",
    "readiness_state_current",
    "readiness_state_final",
    "readiness_state_initial",
    "reason",
    "run_finished_at",
    "run_started_at",
    "version",
)
#: Where a retired ``status.action_required`` is kept. The status section is
#: otherwise a verbatim copy of the terminal state payload, so a retirement has
#: to name what overrode the copy and keep the record it replaced: the history
#: an auditor reads is not allowed to shrink because the current state moved on.
#: Shared with ``run_termination``, which writes the same block onto the payload
#: when a runner-declared pause supersedes a live ask, so the copy the audit
#: makes and the copy it might inherit are spelled one way.
_STATUS_ACTION_REQUIRED_SUPERSEDED = events.ACTION_REQUIRED_SUPERSEDED_KEY

#: What retired it: the run's mailbox holds the ruling that answered the ask,
#: and a receipt proving the ruling reached the run.
SUPERSEDED_BY_CONSUMED_RULING = "mailbox-ruling-consumed"
_SNAPSHOT_IDENTITY_KEYS = (
    "content_sha256",
    "counts",
    "next_pending",
    "next_pending_id",
    "path_hash",
    "plan_hash",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes without splitting."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _redacted_classification(
    result: harnesses.HarnessResult,
) -> harness_failures.FailureClassification:
    """Classify a failed launch, with credentials stripped from its evidence."""
    classification = harness_failures.classify_harness_failure(result)
    return replace(classification, evidence=_redact_secrets(classification.evidence))


def _harness_tail(text: str) -> str:
    """Return the redacted end of a failing harness stream.

    The end, not the head: a harness prints its banner first and its error last,
    so a head-truncated capture of a chatty run keeps everything except the one
    line explaining why it died.
    """
    if not text:
        return ""
    encoded = _redact_secrets(text).encode("utf-8")
    if len(encoded) <= MAX_HARNESS_TAIL_BYTES:
        return encoded.decode("utf-8", errors="ignore")
    return encoded[-MAX_HARNESS_TAIL_BYTES:].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class EvidenceRoots:
    """The approved roots every reference in the bundle must resolve inside.

    References are rendered as ``<root>/<relative>`` rather than as absolute
    paths, which is what makes containment decidable: a locator that cannot be
    expressed this way names something outside the audit's reach and is
    withheld instead of copied.
    """

    roots: tuple[tuple[str, Path], ...]

    @classmethod
    def build(
        cls,
        *,
        run_dir: Path | str,
        state_dir: Path | str,
        plan_dir: Path | str,
        workspace_root: Path | str | None = None,
    ) -> EvidenceRoots:
        package_root = Path(__file__).resolve().parent
        declared: list[tuple[str, Path | str]] = [
            (EVIDENCE_ROOT_RUN, run_dir),
            (EVIDENCE_ROOT_STATE, state_dir),
            (EVIDENCE_ROOT_PLAN, plan_dir),
            (EVIDENCE_ROOT_PACKAGE, package_root),
            (EVIDENCE_ROOT_SKILLS, package_root.parent / "skills"),
        ]
        if workspace_root is not None:
            declared.append((EVIDENCE_ROOT_WORKSPACE, workspace_root))
        return cls(
            roots=tuple(
                (name, workspaces.canonicalize_filesystem_path(value))
                for name, value in declared
            )
        )

    def to_dict(self) -> dict[str, str]:
        return {name: str(path) for name, path in self.roots}

    def locate(self, path: Path | str) -> str | None:
        """Return the symbolic locator for ``path``, or ``None`` when unsafe.

        The deepest matching root wins, so an artifact inside the run directory
        is named ``run/...`` rather than ``state/runs/<id>/...`` even though
        both containments hold.
        """
        candidate = workspaces.canonicalize_filesystem_path(path)
        best: tuple[int, str] | None = None
        for name, root in self.roots:
            if candidate == root:
                locator = name
            elif workspaces.is_descendant(candidate, root):
                locator = f"{name}/{candidate.relative_to(root).as_posix()}"
            else:
                continue
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, locator)
        return None if best is None else best[1]


@dataclass(frozen=True)
class AuditInput:
    """The bounded, control-plane-only evidence bundle an auditor reads."""

    payload: dict[str, Any]

    def to_bytes(self) -> bytes:
        return assurance_models.canonical_json_bytes(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def evidence(self) -> tuple[assurance_models.EvidenceRef, ...]:
        return tuple(
            assurance_models.parse_evidence_ref(entry)
            for entry in self.payload.get("evidence", ())
        )

    @property
    def omissions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload.get("omissions", ()))

    def write(self, run_dir: Path | str, *, durable: bool = True) -> Path:
        """Write the bundle as the run's canonical audit input, exactly once."""
        return assurance_models.write_audit_artifact(
            run_dir,
            assurance_models.AUDIT_INPUT_FILENAME,
            self.to_bytes(),
            durable=durable,
        )


class _Omissions:
    """Counts, digests, and reasons for everything the bundle left out."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}

    def record(self, section: str, reason: str, payload: Any) -> None:
        entry = self._entries.setdefault(
            (section, reason),
            {
                "section": section,
                "reason": reason,
                "count": 0,
                "digest": hashlib.sha256(),
            },
        )
        entry["count"] += 1
        entry["digest"].update(assurance_models.canonical_json_bytes(payload))

    def seed(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Adopt already-rendered omissions so a later pass extends them.

        A rendered entry carries a digest, not the payloads it was built from,
        so the running hash is re-opened over that digest: the result still
        identifies exactly one set of withheld payloads, and whatever the next
        pass drops extends it. Counts stay exact, which is what a reader of the
        bundle acts on.
        """
        for entry in entries:
            section = str(entry.get("section") or "")
            reason = str(entry.get("reason") or "")
            count = entry.get("count")
            digest = hashlib.sha256()
            digest.update(assurance_models.canonical_json_bytes(entry.get("sha256")))
            self._entries[(section, reason)] = {
                "section": section,
                "reason": reason,
                "count": count if isinstance(count, int) and count > 0 else 0,
                "digest": digest,
            }

    def to_list(self) -> list[dict[str, Any]]:
        return [
            {
                "section": entry["section"],
                "reason": entry["reason"],
                "count": entry["count"],
                "sha256": entry["digest"].hexdigest(),
            }
            for entry in sorted(
                self._entries.values(),
                key=lambda item: (item["section"], item["reason"]),
            )
        ]


class _Collector:
    """Redacts, bounds, and validates every value on its way into the bundle."""

    def __init__(self, roots: EvidenceRoots) -> None:
        self._roots = roots
        self._evidence: dict[str, assurance_models.EvidenceRef] = {}
        self._previews: list[dict[str, Any]] = []
        self.omissions = _Omissions()

    # -- references ------------------------------------------------------

    def locator(self, path: Path | str | None) -> str | None:
        if path is None:
            return None
        locator = self._roots.locate(path)
        if locator is None:
            self.omissions.record(
                "references", REASON_OUTSIDE_APPROVED_ROOTS, str(path)
            )
        return locator

    def reference(
        self,
        path: Path | str | None,
        *,
        kind: EvidenceKind,
        digest: str | None = None,
        detail: str | None = None,
    ) -> str | None:
        """Register ``path`` in the evidence index and return its locator."""
        locator = self.locator(path)
        if locator is None:
            return None
        if (
            locator not in self._evidence
            and len(self._evidence) < assurance_models.MAX_EVIDENCE_ENTRIES
        ):
            self._evidence[locator] = assurance_models.EvidenceRef(
                kind=kind, locator=locator, digest=digest, detail=detail
            )
        return locator

    def evidence(self) -> list[dict[str, Any]]:
        return [self._evidence[locator].to_dict() for locator in sorted(self._evidence)]

    # -- redaction -------------------------------------------------------

    def redact(
        self, value: Any, *, key: str | None = None, parent: str | None = None
    ) -> Any:
        """Return ``value`` with credentials, content, and unsafe paths removed."""
        if parent == verification.FAILURE_DIAGNOSTIC_KEY:
            # A structured rejection travels as control-plane evidence, so
            # every field inside it is held to that standard: a value drawn
            # from ASL's own closed gate vocabulary passes, and anything else —
            # an unknown key, a nested structure, an application token in a
            # field that must name a gate or a reason — is the rejected
            # artifact's own text and is withheld like any output.
            if not verification.is_control_plane_diagnostic_field(key, value):
                return self._content_stub(
                    value
                    if isinstance(value, str)
                    else json.dumps(value, default=str, sort_keys=True)
                )
            return self._redact_string(value, key=key, parent=parent)
        if isinstance(value, Mapping):
            return {
                str(name): self.redact(item, key=str(name), parent=key)
                for name, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            items = list(value)
            if key in _ARGV_KEYS:
                items = _redact_argv(items)
            kept = [
                self.redact(item, key=key, parent=parent)
                for item in items[:MAX_SEQUENCE_ITEMS]
            ]
            for dropped in items[MAX_SEQUENCE_ITEMS:]:
                self.omissions.record(key or "payload", REASON_SIZE_CAP, dropped)
            return kept
        if isinstance(value, str):
            return self._redact_string(value, key=key, parent=parent)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return self._redact_string(str(value), key=key, parent=parent)

    def _redact_string(
        self, text: str, *, key: str | None, parent: str | None = None
    ) -> str:
        if key is not None:
            if _SECRET_KEY_RE.search(key):
                return REDACTED
            if key in _ENVIRONMENT_KEYS:
                return REDACTED
            if key in _CONTENT_KEYS:
                if parent == "role" and text in _AUDIT_ROLE_SOURCE_VALUES:
                    return text
                if key == "source" and text in _CONTROL_PLANE_SOURCE_VALUES:
                    return text
                return self._content_stub(text)
            if key in _ORIGIN_KEYS:
                if _is_reply_origin(text, key=key, parent=parent):
                    return text
                return _redact_origin(text)
        if text.startswith("/") and "\n" not in text:
            located = self._roots.locate(text)
            if located is not None:
                return located
            self.omissions.record("references", REASON_OUTSIDE_APPROVED_ROOTS, text)
            return f"[{REASON_OUTSIDE_APPROVED_ROOTS}:{_sha256_text(text)[:16]}]"
        cleaned = _redact_secrets(text)
        if len(cleaned.encode("utf-8")) <= MAX_INLINE_STRING_BYTES:
            return cleaned
        self.omissions.record(key or "payload", REASON_SIZE_CAP, text)
        return (
            f"{_truncate_utf8(cleaned, MAX_INLINE_STRING_BYTES)}"
            f"…[truncated sha256:{_sha256_text(text)[:16]}]"
        )

    def _content_stub(self, text: str) -> str:
        self.omissions.record("payload", REASON_APPLICATION_CONTENT, len(text))
        return (
            f"[{REASON_APPLICATION_CONTENT} bytes:{len(text.encode('utf-8'))} "
            f"sha256:{_sha256_text(text)[:16]}]"
        )

    # -- files -----------------------------------------------------------

    def file_digest(
        self, path: Path, *, kind: EvidenceKind, section: str | None = None
    ) -> dict[str, Any]:
        """Describe a file by identity alone: locator, size, and digest.

        ``section`` names the omission bucket an absent or unreadable file is
        counted under. Evidence kinds are a closed vocabulary, so a caller that
        wants a narrower bucket than its kind says so here.
        """
        record: dict[str, Any] = {"reference": None, "available": False}
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            record["reason"] = REASON_MISSING
        except OSError:
            record["reason"] = REASON_UNREADABLE
        else:
            digest = artifacts.sha256_hex(data)
            record.update(
                available=True,
                byte_count=len(data),
                sha256=digest,
                reference=self.reference(path, kind=kind, digest=digest),
            )
            return record
        record["reference"] = self.locator(path)
        self.omissions.record(section or kind, record["reason"], str(path))
        return record

    def file_preview(self, path: Path, *, kind: EvidenceKind) -> dict[str, Any]:
        """Describe a stream by identity plus a bounded, shrinkable preview."""
        capture = artifacts.StreamCapture(
            head_bytes=PREVIEW_HEAD_BYTES, tail_bytes=PREVIEW_TAIL_BYTES
        )
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    capture.feed(chunk)
        except FileNotFoundError:
            capture.discard()
            return self._unavailable_preview(path, kind, REASON_MISSING)
        except OSError:
            capture.discard()
            return self._unavailable_preview(path, kind, REASON_UNREADABLE)
        metadata = capture.finalize(failed=False)
        record = {
            "available": True,
            "reference": self.reference(path, kind=kind, digest=metadata.sha256),
            "byte_count": metadata.byte_count,
            "sha256": metadata.sha256,
            "truncated": metadata.truncated,
            "preview": _redact_secrets(metadata.preview),
        }
        self._previews.append(record)
        return record

    def _unavailable_preview(
        self, path: Path, kind: EvidenceKind, reason: str
    ) -> dict[str, Any]:
        self.omissions.record(kind, reason, str(path))
        return {
            "available": False,
            "reason": reason,
            "reference": self.locator(path),
        }

    def text_preview(self, text: str, *, label: str) -> dict[str, Any]:
        """Bound a string that is already in memory, keeping its full digest."""
        encoded = text.encode("utf-8")
        preview, truncated = artifacts.bounded_preview(
            encoded, head_bytes=PREVIEW_HEAD_BYTES, tail_bytes=PREVIEW_TAIL_BYTES
        )
        record = {
            "available": True,
            "label": label,
            "byte_count": len(encoded),
            "sha256": artifacts.sha256_hex(encoded),
            "truncated": truncated,
            "preview": _redact_secrets(preview),
        }
        self._previews.append(record)
        return record

    # -- size cap --------------------------------------------------------

    def shrink_previews(
        self, budget: int, *, only: Iterable[dict[str, Any]] | None = None
    ) -> bool:
        """Trim preview text to ``budget`` bytes, reporting whether anything moved."""
        targets = self._previews if only is None else list(only)
        changed = False
        for record in targets:
            preview = record.get("preview")
            if not isinstance(preview, str) or not preview:
                continue
            if len(preview.encode("utf-8")) <= budget:
                continue
            record["preview"] = _truncate_utf8(preview, budget)
            record["truncated"] = True
            self.omissions.record("previews", REASON_SIZE_CAP, record.get("sha256"))
            changed = True
        return changed

    def adopt_previews(self, payload: Any) -> None:
        """Register the previews already inside ``payload`` as shrinkable.

        A collector that did not build the payload has no preview list, and
        :meth:`shrink_previews` would then silently shrink nothing — leaving a
        second shedding pass to drop whole turns where trimming their text would
        have done. Recovering them from the payload keeps the give-up order
        intact across passes.
        """
        if isinstance(payload, Mapping):
            if isinstance(payload.get("preview"), str) and "sha256" in payload:
                self._previews.append(cast(dict[str, Any], payload))
                return
            for value in payload.values():
                self.adopt_previews(value)
        elif isinstance(payload, list):
            for value in payload:
                self.adopt_previews(value)

    @property
    def previews(self) -> list[dict[str, Any]]:
        return self._previews


def _redact_argv(items: Sequence[Any]) -> list[Any]:
    """Blank out argv values that a secret-bearing flag introduces or embeds."""
    redacted: list[Any] = []
    expect_secret = False
    for item in items:
        if not isinstance(item, str):
            redacted.append(item)
            expect_secret = False
            continue
        if expect_secret:
            redacted.append(REDACTED)
            expect_secret = False
            continue
        assignment = _SECRET_ASSIGNMENT_RE.match(item)
        if assignment is not None:
            redacted.append(f"{assignment.group(1)}{REDACTED}")
            continue
        redacted.append(item)
        expect_secret = _SECRET_FLAG_RE.match(item) is not None
    return redacted


def _is_reply_origin(value: str, *, key: str, parent: str | None) -> bool:
    """Whether an ``origin`` field names a reply channel rather than a remote.

    Canonicalizing one erases the only record of whether an operator reply
    arrived by argv, by stdin, or through the mailbox, and leaves an
    ``[unparsable-origin:…]`` hash in its place.
    """
    return (
        key == "origin"
        and parent in _REPLY_ORIGIN_PARENTS
        and value in _REPLY_ORIGIN_VALUES
    )


def _redact_origin(value: str) -> str:
    """Return a credential-free origin identity, or a stable stand-in."""
    try:
        return workspaces.canonicalize_origin(value)
    except workspaces.WorkspaceError:
        return f"[unparsable-origin:{_sha256_text(value)[:16]}]"


def _redact_secrets(text: str) -> str:
    """Strip URL userinfo and recognizable credential literals from ``text``."""
    cleaned = _CREDENTIAL_URL_RE.sub(rf"\1{REDACTED}@", text)
    return _SECRET_VALUE_RE.sub(REDACTED, cleaned)


def _read_json_file(path: Path) -> tuple[Any, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, REASON_MISSING
    except OSError:
        return None, REASON_UNREADABLE
    try:
        return json.loads(text), None
    except json.JSONDecodeError, ValueError:
        return None, REASON_MALFORMED


def _is_generated_artifact(relative: Path) -> bool:
    """True when a path is interpreter output rather than an authored source file."""
    if any(part in GENERATED_TREE_DIRECTORIES for part in relative.parts):
        return True
    return relative.suffix in GENERATED_TREE_SUFFIXES


def _tree_digest(root: Path, *, patterns: Sequence[str]) -> dict[str, Any]:
    """Hash a source tree by relative path and content, never by mtime.

    Generated bytecode is excluded, so two checkouts of the same sources hash
    alike no matter how often an interpreter has run against them.
    """
    if not root.is_dir():
        return {"available": False, "reason": REASON_MISSING}
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(
            candidate
            for candidate in root.rglob(pattern)
            if candidate.is_file()
            and not _is_generated_artifact(candidate.relative_to(root))
        )
    ordered = sorted(set(paths), key=lambda item: item.relative_to(root).as_posix())
    omitted = max(0, len(ordered) - MAX_TREE_FILES)
    digest = hashlib.sha256()
    for path in ordered[:MAX_TREE_FILES]:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"\x00unreadable")
    return {
        "available": True,
        "file_count": min(len(ordered), MAX_TREE_FILES),
        "omitted_file_count": omitted,
        "sha256": digest.hexdigest(),
    }


def _event_priority(name: str, payload: Mapping[str, Any]) -> int:
    """Return 0 for evidence the cap protects, 2 for what it sheds first."""
    if name in _ANOMALY_EVENT_NAMES or name == _RUN_STARTED_EVENT:
        return 0
    if _payload_shows_failure(payload):
        return 0
    if name in _BULK_EVENT_NAMES:
        return 2
    return 1


def _payload_shows_failure(payload: Mapping[str, Any]) -> bool:
    """Whether a record is evidence of an anomaly rather than of progress."""
    returncode = payload.get("returncode")
    if isinstance(returncode, int) and returncode != 0:
        return True
    if payload.get("passed") is False:
        return True
    if payload.get("status") in {"fail", "unavailable"}:
        return True
    for key in ("timed_out", "interrupted", "hang_suspected", "paused"):
        if payload.get(key) is True:
            return True
    for key in ("failure_reason", "failure_classification", "pause_reason"):
        if payload.get(key):
            return True
    return payload.get("ok") is False


_REVIEW_BROAD_FAILURES_DEFERRED_REASON = "findings-defer-broad-failures"
_BROAD_REVIEW_CHECK_PREFIX = "broad:"


def _deferred_review_acceptance(
    verification_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return a structurally valid broad-failure acceptance projection."""
    acceptance = verification_payload.get("acceptance")
    if not isinstance(acceptance, Mapping):
        return None
    names = acceptance.get("deferred_failure_names")
    if not isinstance(names, (list, tuple)) or not names:
        return None
    if any(
        not isinstance(name, str) or not name.startswith(_BROAD_REVIEW_CHECK_PREFIX)
        for name in names
    ):
        return None
    if not (
        acceptance.get("accepted") is True
        and acceptance.get("reason") == _REVIEW_BROAD_FAILURES_DEFERRED_REASON
        and acceptance.get("review_outcome") == "findings"
        and acceptance.get("deferred_failure_count") == len(names)
    ):
        return None
    return acceptance


def _turn_shows_failure(result: Mapping[str, Any]) -> bool:
    """Whether a turn result is an anomaly, including at exit code zero.

    A harness can exit cleanly and still leave the turn unusable: the handoff
    may no longer parse, or the plan may have moved in a way the loop refuses.
    Both are recorded on the turn result rather than in its exit code, so an
    audit that only reads the generic failure signals reports the turn as
    healthy and hides exactly the case an operator opened the audit to find.
    """
    if _payload_shows_failure(result):
        return True
    if result.get("plan_error"):
        return True
    if _guard_shows_failure(result.get("workspace_write_guard")):
        return True
    # Verification is the authoritative gate, and it runs after the harness has
    # already exited. A turn whose gate failed therefore looks healthy at every
    # top-level signal, so the nested result is the only place the failure is.
    verification = result.get("verification")
    if isinstance(verification, Mapping) and verification.get("passed") is False:
        return _deferred_review_acceptance(verification) is None
    status_result = result.get("status_result")
    return status_result is not None and status_result != "complete"


def _mark_rejected_turns(
    turns: list[dict[str, Any]], event_records: Sequence[Mapping[str, Any]]
) -> None:
    """Mark turns a post-execution guard refused after they had already run.

    A refusal is recorded in the event stream, not on the turn result the
    harness left behind, so the turn keeps claiming the clean exit it made
    before the guard reverted it. Correlating by turn index is what stops a
    refused turn from being projected as healthy.
    """
    rejected: set[int] = set()
    for record in event_records:
        if record.get("name") != events.TURN_REJECTED_EVENT:
            continue
        index = _mapping(record.get("payload")).get("turn_index")
        if isinstance(index, int):
            rejected.add(index)
    if not rejected:
        return
    for turn in turns:
        index = _mapping(turn.get("result")).get("turn_index")
        if isinstance(index, int) and index in rejected:
            turn["anomalous"] = True
            turn["rejected"] = True


def _unredeemed_deferral_turn_indexes(
    state_payload: Mapping[str, Any],
    event_records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[int, list[str]]:
    """Turn indexes whose accepted deferral the run then declared unredeemed.

    Read from the pause the run actually took rather than re-derived: the
    ``required-checks-incomplete`` blocker already resolved every deferral
    against the review cycles that could have closed it, and it names the ones
    that reached no all-clear. Entries carrying another run's id are skipped,
    because this bundle's turn indexes mean nothing in that run's numbering.
    """
    sources: list[Mapping[str, Any]] = []
    status_details = _mapping(state_payload.get("pause_details"))
    if state_payload.get("pause_reason") == (
        events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE
    ):
        sources.append(status_details)
    for record in event_records:
        if record.get("name") != events.RUN_PAUSED_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        if payload.get("reason") == events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE:
            sources.append(_mapping(payload.get("details")))
    indexes: dict[int, list[str]] = {}
    for details in sources:
        for item in _sequence(details.get("deferred_unresolved")):
            entry = _mapping(item)
            entry_run = entry.get("run_id")
            if isinstance(entry_run, str) and entry_run and entry_run != run_id:
                continue
            index = entry.get("turn_index")
            if not isinstance(index, int):
                continue
            checks = [
                check
                for check in _sequence(entry.get("checks"))
                if isinstance(check, str)
            ]
            indexes.setdefault(index, [])
            for check in checks:
                if check not in indexes[index]:
                    indexes[index].append(check)
    return indexes


def _mark_unredeemed_deferral_turns(
    turns: list[dict[str, Any]],
    state_payload: Mapping[str, Any],
    event_records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> None:
    """Mark turns whose deferred broad-check failures were never redeemed.

    A findings review may finish with the plan's own gates failing, because
    those failures are the repair specification. What the deferral buys is a
    delay, redeemed only by a later all-clear review -- so an accepted deferral
    exempts the turn from :func:`_turn_shows_failure` on the promise that the
    redemption is still coming. When the run then pauses as
    ``required-checks-incomplete`` naming those same checks, the promise is
    broken and the exemption outlived its premise: the turn was projected
    healthy while the run it belongs to was refusing to report success for
    exactly its verification (ASI-15). Marked here rather than inside
    ``_turn_shows_failure`` for the same reason a guard refusal is: the fact
    that settles it is recorded after the turn, not on its result.
    """
    indexes = _unredeemed_deferral_turn_indexes(
        state_payload, event_records, run_id=run_id
    )
    if not indexes:
        return
    for turn in turns:
        result = _mapping(turn.get("result"))
        index = result.get("turn_index")
        if not isinstance(index, int) or index not in indexes:
            continue
        if _mapping(result.get("verification")).get("passed") is not False:
            continue
        turn["anomalous"] = True
        turn["deferral_unredeemed"] = True
        if indexes[index]:
            turn["unredeemed_checks"] = list(indexes[index])


def _guard_shows_failure(guard: Any) -> bool:
    """Whether a workspace write guard refused the turn or could not judge it.

    A turn that wrote outside its declared scope exits zero and reports a
    complete status, so the guard result is the only place that failure is
    recorded. A guard that could not compare the workspace counts too: it
    proves nothing about the writes it was there to bound.
    """
    if not isinstance(guard, Mapping):
        return False
    if guard.get("passed") is False:
        return True
    if guard.get("violations"):
        return True
    if guard.get("unavailable"):
        return True
    return bool(guard.get("reason"))


def _turn_directories(run_dir: Path) -> list[Path]:
    try:
        entries = [
            entry
            for entry in run_dir.iterdir()
            if entry.is_dir() and entry.name.startswith(state.TURN_DIR_PREFIX)
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda entry: entry.name)


#: How ``repository_evidence`` keys a linked worktree: the repository it
#: belongs to, then the worktree's own root.
_WORKTREE_KEY_SEPARATOR = "::worktree::"


def _declared_repository_alias(guard: Mapping[str, Any], key: str) -> str:
    """Name a guard's changed-repository key the way the plan declares it.

    The change map is keyed by workspace-relative repository path, and by
    ``<path>::worktree::<root>`` for a linked worktree, while session scopes --
    and therefore every write-scope question asked of them -- are keyed by
    registry alias. Comparing the two namespaces directly reads a commit to a
    declared repository as a commit to an undeclared one, which is what made a
    routine run report a scope breach it never committed (ASI-117).

    A linked worktree resolves to the repository it belongs to because it
    shares that repository's object store and refs: a commit made there lands
    in the registered repository, so the registered repository is what the
    declared scope has to answer for. A key that resolves to no registered
    repository is returned verbatim, so an unknown repository stays unknown.
    """
    registered = [
        _mapping(entry) for entry in _sequence(guard.get("registered_repositories"))
    ]
    aliases: set[str] = set()
    by_path: dict[str, str] = {}
    for entry in registered:
        alias = entry.get("alias")
        if not isinstance(alias, str) or not alias:
            continue
        aliases.add(alias)
        path = entry.get("path")
        if isinstance(path, str) and path:
            by_path[path] = alias
    root = key.split(_WORKTREE_KEY_SEPARATOR, 1)[0]
    if root in aliases:
        return root
    return by_path.get(root, key)


def _observed_repository_writes(
    turns: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Repository writes the write guard saw, whoever made them.

    ``commit_mode`` describes only the commits the runner itself was asked to
    make, so a worker that commits under a null commit mode leaves the commit
    projection empty while the guard records the move. Harvesting the guard
    here means the projection answers "what landed" rather than "what the
    runner intended", which is the question an auditor is asking.

    A moved ref counts even when HEAD stayed put: a commit written to a branch
    the worktree does not have checked out still landed. A moved *remote-tracking*
    ref does not, because it is a cache of another repository that ``git fetch``
    rewrites and nothing a turn writes touches. Projecting one as a commit put a
    commit the session never made in front of the checks that read this
    projection, and one of them read it as the write-scope guard having failed to
    hold a violation it did hold (ASI-9).
    """
    observed: list[dict[str, Any]] = []
    for turn in turns:
        result = _mapping(turn.get("result"))
        guard = _mapping(result.get("workspace_write_guard"))
        for key, raw_changes in _mapping(guard.get("changed")).items():
            changes = _mapping(raw_changes)
            refs = [
                str(_mapping(entry).get("ref"))
                for entry in _sequence(changes.get("refs_changed"))
                if _mapping(entry).get("ref")
                and not str(_mapping(entry)["ref"]).startswith(
                    _REMOTE_TRACKING_REF_PREFIX
                )
            ]
            head_changed = changes.get("head_changed") is True
            if not head_changed and not refs:
                continue
            observed.append(
                {
                    "turn": turn.get("name"),
                    "directory": turn.get("directory"),
                    "repository": _declared_repository_alias(guard, str(key)),
                    "repository_key": str(key),
                    "session_id": result.get("session_id"),
                    "head_changed": head_changed,
                    "transition": _mapping(changes.get("head_transition")) or None,
                    "head_before": changes.get("head_before"),
                    "head_after": changes.get("head_after"),
                    "refs_changed": refs,
                }
            )
    return observed


def _observed_head_transitions(
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The observed writes that moved HEAD, in the shape bundles record."""
    return [
        {
            "turn": entry.get("turn"),
            "repository": entry.get("repository"),
            "transition": entry.get("transition"),
        }
        for entry in observed
        if entry.get("head_changed") is True
    ]


def _recorded_commit_heads(section: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The repository and resulting head of every commit the run recorded."""
    recorded = [_mapping(item) for item in _sequence(section.get("commits"))]
    recorded.extend(
        _mapping(_mapping(item).get("result"))
        for item in _sequence(section.get("attempts"))
    )
    pairs: set[tuple[str, str]] = set()
    for entry in recorded:
        alias = (
            entry.get("repository_alias")
            or entry.get("repo")
            or entry.get("repository_key")
        )
        for key in ("head_after", "commit_sha"):
            head = entry.get(key)
            if isinstance(alias, str) and isinstance(head, str):
                pairs.add((alias, head))
    return pairs


def _reconcile_observed_commits(
    section: dict[str, Any], observed: Sequence[Mapping[str, Any]]
) -> None:
    """Project what the guard saw onto the commit records the run kept.

    A worker that commits on its own leaves ``attempts``, ``commit_groups`` and
    ``commits`` empty while the commits themselves sit in the repository, so
    the projections are derived here rather than left contradicting the guard.
    Every derived entry names its ``origin``, and one whose resulting head a
    recorded commit already claims is dropped instead of counted twice.
    """
    accounted = _recorded_commit_heads(section)
    commits: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    groups: dict[Any, dict[str, Any]] = {}
    for entry in observed:
        alias = entry.get("repository")
        transition = _mapping(entry.get("transition"))
        head_after = entry.get("head_after") or transition.get("after")
        if (
            isinstance(alias, str)
            and isinstance(head_after, str)
            and (alias, head_after) in accounted
        ):
            continue
        commits_added = transition.get("commits_added")
        commit = {
            "origin": OBSERVED_COMMIT_ORIGIN,
            "turn": entry.get("turn"),
            "session_id": entry.get("session_id"),
            # The key stays verbatim so the exact repository the guard named is
            # never lost, while the alias is what a write-scope question reads.
            "repository_key": entry.get("repository_key") or alias,
            "repository_alias": alias,
            "repo": alias,
            "head_before": entry.get("head_before") or transition.get("before"),
            "head_after": head_after,
            "commit_sha": head_after if commits_added else None,
            "relation": transition.get("relation"),
            "commits_added": commits_added,
            "history_rewrite": transition.get("history_rewrite"),
            "refs_changed": list(entry.get("refs_changed") or ()),
        }
        commits.append(commit)
        attempts.append(
            {
                "name": entry.get("turn"),
                "reference": entry.get("directory"),
                "origin": OBSERVED_COMMIT_ORIGIN,
                "result": commit,
            }
        )
        session_id = entry.get("session_id")
        group = groups.setdefault(
            session_id if session_id is not None else entry.get("turn"),
            {
                "origin": OBSERVED_COMMIT_ORIGIN,
                "scope": "session" if session_id is not None else "end",
                "session_id": session_id,
                "ordered_aliases": [],
                "repositories": [],
            },
        )
        if alias not in group["ordered_aliases"]:
            group["ordered_aliases"].append(alias)
        group["repositories"].append(commit)
    if not commits:
        return
    section["attempts"] = list(_sequence(section.get("attempts"))) + attempts
    section["commits"] = list(_sequence(section.get("commits"))) + commits
    section["commit_groups"] = list(_sequence(section.get("commit_groups"))) + list(
        groups.values()
    )


def _reference_commit_streams(collector: _Collector, result: Mapping[str, Any]) -> None:
    """Index the retained streams a failed commit attempt points at.

    A failed attempt records the full ``git`` diagnostic in its own stdout and
    stderr files and carries only a bounded preview inline, so the paths are
    where the reason a commit was refused actually survives. Naming them in the
    result while indexing only the parent directory left the auditor unable to
    tell evidence it could read from a path it merely saw, and a path that is
    not on disk is recorded as an omission rather than passed over, so a missing
    stream is stated instead of being indistinguishable from one never written
    (ASI-137).
    """
    if not _commit_shows_failure(result):
        return
    for key in ("stderr_path", "stdout_path"):
        path = result.get(key)
        if not isinstance(path, str) or not path:
            continue
        stream = key.removesuffix("_path")
        if not Path(path).is_file():
            collector.omissions.record("commits", REASON_MISSING, path)
            continue
        collector.reference(
            path, kind="commit", detail=f"{_commit_record_label(result)} {stream}"
        )


def _commit_section(
    collector: _Collector,
    *,
    run_dir: Path,
    state_payload: Mapping[str, Any],
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What the run committed, from state projection plus per-commit results."""
    section = _projection(collector, state_payload, _COMMIT_STATE_KEYS)
    observed = _observed_repository_writes(turns)
    section["observed_head_transitions"] = _observed_head_transitions(observed)
    attempts: list[dict[str, Any]] = []
    try:
        directories = sorted(
            entry
            for entry in run_dir.iterdir()
            if entry.is_dir() and entry.name.startswith(_COMMIT_DIR_PREFIX)
        )
    except OSError:
        directories = []
    for directory in directories:
        result, reason = _read_json_file(directory / state.RESULT_FILE_NAME)
        entry: dict[str, Any] = {
            "name": directory.name,
            "reference": collector.reference(directory, kind="commit"),
        }
        if isinstance(result, Mapping):
            entry["result"] = collector.redact(result)
            _reference_commit_streams(collector, result)
        else:
            collector.omissions.record(
                "commits", reason or REASON_MALFORMED, str(directory)
            )
            entry["result"] = {"available": False, "reason": reason or REASON_MALFORMED}
        attempts.append(entry)
    section["attempts"] = attempts
    _reconcile_observed_commits(section, observed)
    return section


def _read_events(
    path: Path, run_id: str, collector: _Collector
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the run's slice of the shared event log, failing loudly on damage.

    The log is per-plan and append-only, so a run is the span from its own
    ``run_started`` to its terminal record, plus any record that names the run
    ID outright. Lines that will not parse are counted and digested rather than
    dropped: a damaged log is itself evidence about the run that wrote it.
    """
    summary: dict[str, Any] = {"available": False, UNPARSABLE_LINE_COUNT_KEY: 0}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        summary["reason"] = REASON_MISSING
        collector.omissions.record("events", REASON_MISSING, str(path))
        return [], summary
    except OSError:
        summary["reason"] = REASON_UNREADABLE
        collector.omissions.record("events", REASON_UNREADABLE, str(path))
        return [], summary

    summary["available"] = True
    records: list[dict[str, Any]] = []
    inside = False
    terminated = False
    rejected = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError, ValueError:
            rejected += 1
            collector.omissions.record("events", REASON_MALFORMED, line[:2000])
            continue
        if not isinstance(parsed, Mapping):
            rejected += 1
            collector.omissions.record("events", REASON_MALFORMED, line[:2000])
            continue
        payload = parsed.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        name = str(parsed.get("name") or "")
        named_run = payload.get("run_id")
        names_this_run = named_run == run_id
        if names_this_run and name == _RUN_STARTED_EVENT:
            inside = True
            terminated = False
        if not names_this_run and (not inside or terminated or named_run is not None):
            continue
        records.append(
            {
                "sequence": index,
                "name": name,
                "created_at": parsed.get("created_at"),
                "version": parsed.get("version"),
                "priority": _event_priority(name, payload),
                "payload": collector.redact(payload),
            }
        )
        if names_this_run and name in _TERMINAL_EVENT_NAMES:
            terminated = True
    summary[UNPARSABLE_LINE_COUNT_KEY] = rejected
    summary["reference"] = collector.reference(path, kind="event")
    return records, summary


def _delivered_mailbox_ids(
    event_records: Sequence[Mapping[str, Any]], run_id: str
) -> frozenset[str]:
    """Mailbox messages this run's own event stream says it took delivery of.

    Guidance is addressed to the plan, not to a run: a supervisor appends it
    without knowing which run — or which turn — will read it, so
    ``mailbox.append_guidance`` writes no ``run_id`` and the record cannot be
    claimed the way ``append_notice``'s is. Delivery is the tie, and delivery is
    already recorded: the runner appends a ``guidance-observed`` mailbox event
    naming the message it read into a turn. Joining on that accounts for the
    guidance a run acted on while leaving guidance meant for some other run
    where it belongs.
    """
    delivered: set[str] = set()
    for record in event_records:
        if record.get("name") != events.MAILBOX_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        if payload.get("action") != events.MAILBOX_ACTION_GUIDANCE_OBSERVED:
            continue
        if payload.get("run_id") not in (None, run_id):
            continue
        message_id = payload.get("message_id")
        if message_id is not None:
            delivered.add(str(message_id))
    return frozenset(delivered)


def _read_mailbox(
    path: Path,
    run_id: str,
    collector: _Collector,
    *,
    delivered_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the run's slice of the shared operator mailbox.

    Without it an audit sees the contract a session was materialized with and
    the recipe it actually ran, but nothing saying a human ruled between the
    two, and reads an authorized narrowing as an evaded gate.

    Only a record that names a run outright can be claimed by run ID; a reply,
    a receipt and the wait records join their exchange through ``to``,
    ``wait_id`` or ``closure_id``, so ownership is seeded from the named
    records and walked forward. ``delivered_ids`` adds the third seed a
    plan-addressed record needs, since guidance names no run and answers no
    question: the run's own event stream saying it read that message. A record
    that never joins belongs to another run or to the plan, and is counted
    rather than reported as withheld: an omission means evidence was kept back,
    not that it was someone else's.

    The file is read directly rather than through :mod:`mailbox`, which is
    addressed by plan directory and takes a shared lock that a live supervisor
    could stall collection behind.
    """
    summary: dict[str, Any] = {
        "available": False,
        "delivered_count": 0,
        UNPARSABLE_LINE_COUNT_KEY: 0,
        "unscoped_count": 0,
        "counts": {},
    }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        summary["reason"] = REASON_MISSING
        collector.omissions.record("mailbox", REASON_MISSING, str(path))
        return [], summary
    except OSError:
        summary["reason"] = REASON_UNREADABLE
        collector.omissions.record("mailbox", REASON_UNREADABLE, str(path))
        return [], summary

    summary["available"] = True
    parsed_lines: list[tuple[int, Mapping[str, Any]]] = []
    rejected = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError, ValueError:
            rejected += 1
            collector.omissions.record("mailbox", REASON_MALFORMED, line[:2000])
            continue
        if not isinstance(parsed, Mapping):
            rejected += 1
            collector.omissions.record("mailbox", REASON_MALFORMED, line[:2000])
            continue
        parsed_lines.append((index, parsed))

    owned = {
        str(parsed["id"])
        for _, parsed in parsed_lines
        if parsed.get("run_id") == run_id and parsed.get("id") is not None
    }
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    unscoped = 0
    delivered = 0
    for index, parsed in parsed_lines:
        identifier = parsed.get("id")
        was_delivered = identifier is not None and str(identifier) in delivered_ids
        joined = any(parsed.get(key) in owned for key in _MAILBOX_JOIN_KEYS)
        if not joined and not was_delivered and parsed.get("run_id") != run_id:
            unscoped += 1
            continue
        if identifier is not None:
            owned.add(str(identifier))
        kind = str(parsed.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        # The prose lives under ``text``, which redaction already treats as
        # application content, so the ruling's digest and length survive and
        # its bytes do not.
        record = dict(collector.redact(parsed))
        record["sequence"] = index
        if was_delivered and parsed.get("run_id") != run_id:
            # Say how a record naming no run reached this run's slice, so a
            # reader is not left inferring it from the absence of a ``run_id``.
            delivered += 1
            record["ownership"] = MAILBOX_OWNERSHIP_DELIVERED
        records.append(record)
    summary["delivered_count"] = delivered
    summary[UNPARSABLE_LINE_COUNT_KEY] = rejected
    summary["unscoped_count"] = unscoped
    summary["counts"] = counts
    summary["reference"] = collector.reference(path, kind="event")
    return records, summary


def _plan_section(
    collector: _Collector,
    *,
    plan_dir: Path,
    state_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Identity of the source plan directory and of the split it produced."""
    section: dict[str, Any] = {
        "reference": collector.reference(plan_dir, kind="plan"),
        "context": collector.file_digest(plan_dir / "context.md", kind="plan"),
        "manifest": collector.file_digest(plan_dir / "manifest.md", kind="plan"),
        # Every turn claims ``progress_appended``; without this the claim cites
        # nothing, and a journal that was never written goes unreported.
        "progress": collector.file_digest(plans.progress_path(plan_dir), kind="plan"),
        "metadata": _plan_metadata(collector, plan_dir),
    }
    for label, key in (("initial", "initial_snapshot"), ("final", "final_snapshot")):
        snapshot = state_payload.get(key)
        if isinstance(snapshot, Mapping):
            section[label] = {
                name: collector.redact(snapshot.get(name), key=name)
                for name in _SNAPSHOT_IDENTITY_KEYS
                if name in snapshot
            }
        else:
            section[label] = {"available": False, "reason": REASON_NOT_RECORDED}
    sessions = []
    for path in sorted(plan_dir.glob("session-*.md")):
        entry = collector.file_digest(path, kind="plan")
        entry["name"] = path.name
        sessions.append(entry)
    section["sessions"] = sessions
    return section


def _plan_metadata(collector: _Collector, plan_dir: Path) -> dict[str, Any]:
    """Read the durable workspace binding without recovering a transaction.

    ``plans.load_plan_metadata`` repairs a pending plan transaction as a side
    effect. An evidence collector must observe the run rather than change it,
    so the file is parsed directly.
    """
    path = plan_dir / plans.PLAN_METADATA_FILE_NAME
    payload, reason = _read_json_file(path)
    if payload is None:
        # A file holding the literal `null` parses to `None` with no reason, so
        # it is reported the way every other reader in this module reports an
        # unusable payload rather than as an omission with a null reason.
        reason = reason or REASON_MALFORMED
        collector.omissions.record("plan.metadata", reason, str(path))
        return {"available": False, "reason": reason}
    return {
        "available": True,
        "reference": collector.reference(
            path, kind="plan", digest=_sha256_text(json.dumps(payload, sort_keys=True))
        ),
        "content": collector.redact(payload),
    }


def _registry_section(
    collector: _Collector, state_payload: Mapping[str, Any]
) -> dict[str, Any]:
    """The workspace binding and repository registry the run executed under."""
    repositories = state_payload.get("repositories")
    if not isinstance(repositories, (list, tuple)):
        run_config = state_payload.get("run_config")
        if isinstance(run_config, Mapping):
            repositories = run_config.get("repositories")
    if not isinstance(repositories, (list, tuple)):
        return {"registered": False, "reason": REASON_NOT_RECORDED}
    return {
        "registered": True,
        "workspace_root": collector.locator(
            state_payload.get("workspace_root") or state_payload.get("cwd") or "."
        ),
        "digest": state_payload.get("registry_digest"),
        "repositories": collector.redact(list(repositories)),
    }


def _scopes_section(
    collector: _Collector, *, plan_dir: Path, aliases: Sequence[str]
) -> dict[str, Any]:
    """Per-session read and write scopes as the split plan declares them."""
    if not aliases:
        return {"available": False, "reason": REASON_NOT_RECORDED}
    sessions: list[dict[str, Any]] = []
    for path in sorted(plan_dir.glob("session-*.md")):
        if plans.SESSION_EXPANSION_FILE_RE.fullmatch(path.name):
            continue
        entry: dict[str, Any] = {"session": path.stem}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            entry["available"] = False
            entry["reason"] = REASON_UNREADABLE
            sessions.append(entry)
            continue
        try:
            scope = plans.parse_session_scope(text, tuple(aliases))
        except plans.PlanError, ValueError:
            entry["available"] = False
            entry["reason"] = REASON_MALFORMED
        else:
            entry.update(
                available=True, reads=list(scope.reads), writes=list(scope.writes)
            )
        sessions.append(entry)
    return {"available": True, "sessions": sessions}


def _reference_verification_stderr(
    collector: _Collector, result: Mapping[str, Any]
) -> None:
    """Index the stderr artifact each failed verification check points at.

    That artifact is where a rejected gate's full diagnostic survives: the check
    record's own copy is a preview the content rules withhold, and its inline
    detail is bounded. Naming the path without indexing it left the auditor
    unable to tell evidence it could read from a path it merely saw.
    """
    for check in _sequence(_mapping(result.get("verification")).get("checks")):
        entry = _mapping(check)
        if entry.get("status") == "pass":
            continue
        path = entry.get("stderr_artifact_path")
        if isinstance(path, str) and path:
            collector.reference(
                path, kind="artifact", detail=f"{entry.get('name')} stderr"
            )


def _turn_section(collector: _Collector, directory: Path) -> dict[str, Any]:
    """One turn's packet, prompt, harness metadata, and bounded streams."""
    result, reason = _read_json_file(directory / state.RESULT_FILE_NAME)
    turn: dict[str, Any] = {
        "directory": collector.reference(directory, kind="artifact"),
        "name": directory.name,
    }
    if isinstance(result, Mapping):
        _reference_verification_stderr(collector, result)
        turn["result"] = collector.redact(result)
        turn["anomalous"] = _turn_shows_failure(result)
        # Beside ``anomalous``, never folded into it: a harness that lost an
        # auxiliary transport and still produced a usable turn is degraded, and
        # counting that as an anomaly would contradict every successful run.
        turn["degraded"] = bool(result.get("harness_degradation"))
    else:
        collector.omissions.record("turns", reason or REASON_MALFORMED, str(directory))
        turn["result"] = {"available": False, "reason": reason or REASON_MALFORMED}
        turn["anomalous"] = True
        turn["degraded"] = False
    turn["logs"] = {
        "stdout": collector.file_preview(directory / "stdout.log", kind="harness"),
        "stderr": collector.file_preview(directory / "stderr.log", kind="harness"),
    }
    turn["last_message"] = collector.file_preview(
        directory / "last-message.txt", kind="harness"
    )
    return turn


def _verification_section(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Check-level outcomes across the run, without any command output."""
    sessions = []
    for turn in turns:
        result = turn.get("result")
        if not isinstance(result, Mapping):
            continue
        session_verification = result.get("verification")
        if not isinstance(session_verification, Mapping):
            continue
        sessions.append(
            {
                "turn": turn.get("name"),
                "session_id": result.get("session_id"),
                "verification": session_verification,
            }
        )
    return {"sessions": sessions, "session_count": len(sessions)}


def _moment(value: object) -> datetime | None:
    """Parse a recorded ISO timestamp, or ``None`` when it cannot be trusted."""
    if not isinstance(value, str) or not value:
        return None
    candidate = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _mailbox_asks(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every question in the run's mailbox slice, with how far it got.

    Mirrors :meth:`mailbox.MailboxView.delivery_state_for`: a question is
    answered once a reply or a resolution targets it, and answered *and
    consumed* once a receipt names that closure. Only the consumed state
    proves the answer reached the run rather than sitting unread.
    """
    closures: dict[str, Mapping[str, Any]] = {}
    receipted: set[str] = set()
    for record in records:
        kind = record.get("kind")
        target = record.get("to")
        if kind in mailbox.CLOSURE_KINDS and target is not None:
            closures.setdefault(str(target), record)
        elif kind == mailbox.KIND_RECEIPT and record.get("closure_id") is not None:
            receipted.add(str(record["closure_id"]))
    asks: list[dict[str, Any]] = []
    for record in records:
        if record.get("kind") != mailbox.KIND_QUESTION or record.get("id") is None:
            continue
        closure = closures.get(str(record["id"]))
        closure_id = None if closure is None else closure.get("id")
        asks.append(
            {
                "id": str(record["id"]),
                "session_id": record.get("session_id"),
                "turn_index": record.get("turn_index"),
                "closure_id": closure_id,
                "closure_action": None if closure is None else closure.get("action"),
                "closed_at": None if closure is None else closure.get("created_at"),
                "consumed": closure_id is not None and str(closure_id) in receipted,
            }
        )
    return asks


def _raised_by_same_turn(ask: Mapping[str, Any], signal: Mapping[str, Any]) -> bool:
    """Whether ``ask`` was raised by the turn that emitted ``signal``.

    Both identifiers are required on both sides. A signal that names no turn
    cannot be shown to correspond to any particular question, and the whole
    point of the comparison is to avoid retiring one turn's ask on another
    turn's answer.
    """
    for key in ("session_id", "turn_index"):
        expected = signal.get(key)
        actual = ask.get(key)
        if expected is None or actual is None or str(expected) != str(actual):
            return False
    return True


def _status_section(
    collector: _Collector,
    state_payload: Mapping[str, Any],
    mailbox_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The terminal state projection, with an answered ask retired.

    ``action_required`` is the current unresolved intervention, not the run's
    history — the reading ``turn_execution.clear_live_action_required``
    already enforces on live state, where starting another turn or finishing
    unpaused proves the signal was answered. Neither proof is available to a
    run that stopped *on* the signal, so a paused run reaches the bundle still
    advertising an ask a human has already ruled on, and an auditor cannot tell
    it from a run genuinely waiting on one.

    A ruling the run consumed is the third proof. When every question the
    signal's own turn raised has been answered and receipted, and at least one
    of those rulings is no older than the signal itself, the projection is
    retired and the record it replaced is kept beside it. ``action_required``
    of ``null`` next to a standing ``paused`` then reads as what it is: a run
    merely paused, not one still asking. The ordering requirement is what keeps
    an earlier, unrelated exchange in the same turn from retiring a signal
    raised after it; without a ruling that postdates the signal, nothing shows
    a human ever saw it.
    """
    status = _projection(collector, state_payload, _STATUS_STATE_KEYS)
    signal = status.get("action_required")
    if not isinstance(signal, Mapping):
        return status
    if signal.get("reason") not in events.LIVE_ACTION_REQUIRED_REASONS:
        return status
    raised_at = _moment(signal.get("created_at"))
    if raised_at is None:
        return status
    asks = [
        ask
        for ask in _mailbox_asks(mailbox_records)
        if _raised_by_same_turn(ask, signal)
    ]
    if not asks or not all(ask["consumed"] for ask in asks):
        return status
    answered_after = [
        ask
        for ask in asks
        if (closed_at := _moment(ask["closed_at"])) is not None
        and closed_at >= raised_at
    ]
    if not answered_after:
        return status
    status["action_required"] = None
    status[_STATUS_ACTION_REQUIRED_SUPERSEDED] = {
        "superseded_by": SUPERSEDED_BY_CONSUMED_RULING,
        "questions": asks,
        "record": dict(signal),
    }
    return status


def _projection(
    collector: _Collector, state_payload: Mapping[str, Any], keys: Sequence[str]
) -> dict[str, Any]:
    return {
        key: collector.redact(state_payload.get(key), key=key)
        for key in keys
        if key in state_payload
    }


def _repository_section(
    collector: _Collector,
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Per-repository mutation fingerprints: paths and hashes, never content."""
    if not isinstance(snapshot, Mapping):
        return {"available": False, "reason": REASON_NOT_RECORDED}
    return {
        "available": True,
        "repositories": {
            str(name): collector.redact(fingerprint)
            for name, fingerprint in snapshot.items()
        },
    }


def _versions_section() -> dict[str, Any]:
    """What ASL and its skills were, by version and by content digest."""
    package_root = Path(__file__).resolve().parent
    return {
        "asl": __version__,
        "package": _tree_digest(package_root, patterns=("*.py",)),
        "skills": _tree_digest(package_root.parent / "skills", patterns=("*",)),
        "assurance_schema_version": assurance_models.ASSURANCE_SCHEMA_VERSION,
        "event_schema_version": events.EVENT_SCHEMA_VERSION,
    }


def _shed(
    payload: dict[str, Any],
    collector: _Collector,
    *,
    max_bytes: int,
) -> None:
    """Shrink the bundle to ``max_bytes``, giving up the least useful evidence first.

    Ordering is the whole point: heartbeat chatter goes before ordinary
    records, ordinary records before old healthy turns, and the terminal,
    failing, and guard evidence goes last, so a bundle that had to shed
    something still explains how the run ended.
    """
    records: list[dict[str, Any]] = payload["events"]["records"]
    messages: list[dict[str, Any]] = payload["mailbox"]["records"]
    turns: list[dict[str, Any]] = payload["turns"]
    protected = {
        id(turn)
        for turn in turns[-RECENT_TURN_COUNT:]
        + [t for t in turns if t.get("anomalous")]
    }

    def size() -> int:
        payload["omissions"] = collector.omissions.to_list()
        return len(assurance_models.canonical_json_bytes(payload))

    def drop_events(priority: int, batch: int) -> bool:
        doomed = [
            index
            for index, record in enumerate(records)
            if record.get("priority") == priority
        ][:batch]
        for index in reversed(doomed):
            collector.omissions.record("events", REASON_SIZE_CAP, records[index])
            del records[index]
        return bool(doomed)

    def drop_mailbox(batch: int) -> bool:
        doomed = [
            index
            for index, message in enumerate(messages)
            if message.get("kind") in _SHEDDABLE_MAILBOX_KINDS
        ][:batch]
        for index in reversed(doomed):
            collector.omissions.record("mailbox", REASON_SIZE_CAP, messages[index])
            del messages[index]
        return bool(doomed)

    def drop_turn(only_unprotected: bool, batch: int) -> bool:
        doomed = [
            index
            for index, turn in enumerate(turns)
            if not (only_unprotected and id(turn) in protected)
        ][:batch]
        for index in reversed(doomed):
            collector.omissions.record(
                "turns", REASON_SIZE_CAP, turns[index].get("name")
            )
            del turns[index]
        return bool(doomed)

    steps: list[Callable[[int], bool]] = [
        lambda batch: collector.shrink_previews(
            MIN_PREVIEW_BYTES,
            only=[
                preview
                for turn in turns
                if id(turn) not in protected
                for preview in _turn_previews(turn)
            ],
        ),
        lambda batch: drop_events(2, batch),
        drop_mailbox,
        lambda batch: drop_events(1, batch),
        lambda batch: drop_turn(True, batch),
        lambda batch: collector.shrink_previews(MIN_PREVIEW_BYTES),
        lambda batch: collector.shrink_previews(0),
        lambda batch: drop_events(0, batch),
        lambda batch: drop_turn(False, batch),
    ]
    # Batches grow geometrically so a log with thousands of records is bounded
    # in a logarithmic number of serializations rather than one per record.
    for step in steps:
        batch = 1
        while size() > max_bytes:
            if not step(batch):
                break
            batch = min(batch * 2, 4096)
        if size() <= max_bytes:
            return


def _turn_previews(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    logs = turn.get("logs")
    if isinstance(logs, Mapping):
        found.extend(item for item in logs.values() if isinstance(item, dict))
    last_message = turn.get("last_message")
    if isinstance(last_message, dict):
        found.append(last_message)
    return found


def collect_audit_input(
    *,
    run_id: str,
    run_dir: Path | str,
    state_dir: Path | str,
    plan_dir: Path | str,
    workspace_root: Path | str | None = None,
    state_payload: Mapping[str, Any] | None = None,
    workspace_snapshot: Mapping[str, Any] | None = None,
    terminal_pending: bool = False,
    max_bytes: int = AUDIT_INPUT_MAX_BYTES,
) -> AuditInput:
    """Collect the bounded operational evidence bundle for one finished run.

    The bundle is a pure function of the run's persisted history and of the
    trees ASL itself ships, so two equivalent histories serialize to identical
    bytes — which is what lets the artifact's digest key the idempotent
    publication in :mod:`agent_session_loop.assurance_projection`. Nothing is
    collected by wall clock, and nothing is collected from the application
    under test: paths, digests, exit codes, timings, and guard results carry
    the control-plane story, while file contents and semantic diffs stay out.

    Set ``terminal_pending`` when the caller audits a run whose terminal record
    has not been written yet, so the checks that read for a missing terminal
    record do not report the caller's own ordering as a defect.
    """
    run_dir = Path(run_dir)
    state_dir = Path(state_dir)
    plan_dir = Path(plan_dir)
    roots = EvidenceRoots.build(
        run_dir=run_dir,
        state_dir=state_dir,
        plan_dir=plan_dir,
        workspace_root=workspace_root,
    )
    collector = _Collector(roots)
    # Registered first so it survives the evidence-index cap. A deterministic
    # check may not cite nothing, so this is the locator every check falls back
    # to when the artifact it read was shed or was never written.
    collector.reference(run_dir, kind="artifact", detail="run directory")

    state_path = state_dir / events.STATE_FILE_NAME
    if state_payload is None:
        loaded, reason = _read_json_file(state_path)
        if isinstance(loaded, Mapping):
            state_payload = loaded
        else:
            collector.omissions.record(
                "state", reason or REASON_MALFORMED, str(state_path)
            )
            state_payload = {}
    state_reference = collector.reference(state_path, kind="state")

    registry = _registry_section(collector, state_payload)
    aliases = [
        str(entry.get("alias"))
        for entry in (registry.get("repositories") or ())
        if isinstance(entry, Mapping) and entry.get("alias")
    ]
    event_records, event_summary = _read_events(
        state_dir / events.EVENTS_FILE_NAME, run_id, collector
    )
    mailbox_records, mailbox_summary = _read_mailbox(
        state_dir / mailbox.MAILBOX_FILE_NAME,
        run_id,
        collector,
        delivered_ids=_delivered_mailbox_ids(event_records, run_id),
    )
    turns = [
        _turn_section(collector, directory) for directory in _turn_directories(run_dir)
    ]
    _mark_rejected_turns(turns, event_records)
    _mark_unredeemed_deferral_turns(
        turns, state_payload, event_records, run_id=str(run_id)
    )

    payload: dict[str, Any] = {
        "schema_version": AUDIT_INPUT_SCHEMA_VERSION,
        "run_id": run_id,
        "roots": roots.to_dict(),
        "plan": _plan_section(
            collector, plan_dir=plan_dir, state_payload=state_payload
        ),
        "registry": registry,
        "scopes": _scopes_section(collector, plan_dir=plan_dir, aliases=aliases),
        "configuration": collector.redact(state_payload.get("run_config") or {}),
        "state": {
            "reference": state_reference,
            "harness": state_payload.get("harness"),
            # Requested/effective semantics are explicit for the audit.  The
            # historical ``model`` aliases remain for consumers of older
            # bundles, while ``model_projection`` is authoritative per role.
            "requested_model": collector.redact(
                state_payload.get("model"), key="requested_model"
            ),
            "requested_model_source": state_payload.get("model_source"),
            "model": collector.redact(state_payload.get("model"), key="model"),
            "model_source": state_payload.get("model_source"),
            "observed_model": collector.redact(
                state_payload.get("observed_model"), key="observed_model"
            ),
            "observed_model_source": state_payload.get("observed_model_source"),
            # Where the two are reconciled per role: an unresolved request that
            # every turn of a role answered the same way resolves here, and a
            # review harness keeps its own record.
            "model_projection": collector.redact(
                state_payload.get("model_projection") or [], key="model_projection"
            ),
            "effort": state_payload.get("effort"),
            "turn_count": len(turns),
        },
        "turns": turns,
        "events": {
            "records": event_records,
            "summary": event_summary,
            "counts": _event_counts(event_records),
            "terminal_pending": bool(terminal_pending),
        },
        "mailbox": {"records": mailbox_records, "summary": mailbox_summary},
        "verification": _verification_section(turns),
        "review": _projection(collector, state_payload, _REVIEW_STATE_KEYS),
        "repair": _projection(collector, state_payload, _REPAIR_STATE_KEYS),
        "commits": _commit_section(
            collector, run_dir=run_dir, state_payload=state_payload, turns=turns
        ),
        "status": _status_section(collector, state_payload, mailbox_records),
        "repository": _repository_section(collector, workspace_snapshot),
        "versions": _versions_section(),
    }
    # Digest rather than merely name each artifact: a locator alone cannot tell
    # the auditor whether the file it points at is the one the run produced.
    for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS:
        candidate = run_dir / name
        if candidate.exists():
            collector.file_digest(candidate, kind="artifact")
    # The review cycle records these two paths and their digests, so an index
    # that never mentions them leaves the auditor unable to tell a review that
    # produced no artifact from one whose artifact was simply never indexed.
    # The absence is therefore recorded rather than skipped.
    for candidate in (
        review.final_review_output_path(plan_dir),
        review.final_review_artifact_path(plan_dir),
    ):
        collector.file_digest(candidate, kind="artifact", section="review.artifacts")

    payload["evidence"] = collector.evidence()
    payload["omissions"] = collector.omissions.to_list()
    _shed(payload, collector, max_bytes=max_bytes)
    payload["evidence"] = collector.evidence()
    payload["omissions"] = collector.omissions.to_list()
    return AuditInput(payload=payload)


def tighten_audit_input(bundle: AuditInput, *, max_bytes: int) -> AuditInput:
    """Shed an already-collected bundle down to a smaller ceiling.

    Shed again rather than collected again: collecting reads the run directory,
    which by the time this is needed holds the audit's own input artifact and
    attempt records, so a second collection would describe a different run than
    the first one did — including a reference to its own predecessor. Shedding
    the payload already in hand keeps the result a strictly smaller view of the
    same evidence, given up in the same least-useful-first order, and leaves the
    canonical bundle on disk the one the deterministic verdicts were read from.

    The evidence index is not shed, so the locators an auditor may cite are the
    same either way.
    """
    payload = copy.deepcopy(bundle.payload)
    collector = _Collector(EvidenceRoots(roots=()))
    collector.omissions.seed(payload.get("omissions") or ())
    collector.adopt_previews(payload)
    _shed(payload, collector, max_bytes=max_bytes)
    payload["omissions"] = collector.omissions.to_list()
    return AuditInput(payload=payload)


def _event_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        name = str(record.get("name") or "")
        counts[name] = counts.get(name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

#: The locator every check falls back to. ``collect_audit_input`` registers the
#: run directory first, so this is always in the evidence index, and a result
#: that read nothing else still says where it looked.
RUN_ANCHOR = EVIDENCE_ROOT_RUN

#: Event names a deterministic check reads. A record that reports a failure
#: under any other name lands in the unknown-anomalies lane rather than going
#: unread, which is what keeps the rubric's last lane honest.
_CLASSIFIED_EVENT_NAMES = frozenset(
    {
        _RUN_STARTED_EVENT,
        events.COMMIT_FINISHED_EVENT,
        # Earned by `_check_paused_work_settlement`, which grades every field
        # of this record. It is listed here only because that check owns it:
        # the record carries the pause's reason, so the catch-all read every
        # successful settlement as an unclassified failure.
        events.PAUSED_WORK_COMMITTED_EVENT,
        events.PROCESS_SWEEP_EVENT,
        events.REVIEW_VERDICT_RETRY_EVENT,
        events.RUN_FINISHED_EVENT,
        events.RUN_PAUSED_EVENT,
        events.TURN_HANG_CLEARED_EVENT,
        events.TURN_HANG_SUSPECTED_EVENT,
        events.VERIFICATION_CHECK_FINISHED_EVENT,
        events.VERIFICATION_CHECK_SKIPPED_EVENT,
        events.VERIFICATION_FINISHED_EVENT,
        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
    }
)

#: Every outcome the runner records for a commit it ran, mirrored as literals
#: because ``runner`` imports this module and never the other way round.
#: ``success`` is listed beside the failures so a record naming a token from
#: neither set reads as an outcome nobody defined rather than as a clean one.
_COMMIT_FAILURE_RESULTS = frozenset(
    {
        "dirty-after-commit",
        "missing-head",
        "nonzero-exit",
        "status-unavailable",
        "unchanged-head",
    }
)
_COMMIT_RESULTS = _COMMIT_FAILURE_RESULTS | {"success"}

#: Every verdict the paused-work settlement records under ``result``, mirrored
#: as literals for the same reason as :data:`_COMMIT_RESULTS`. The set is
#: closed: ``commit_settlement`` picks exactly one of these on every path, so a
#: record naming anything else is an outcome nobody defined.
_PAUSED_WORK_RESULT_FAILED = "failed"
_PAUSED_WORK_RESULT_RESIDUE = "committed-with-residue"
_PAUSED_WORK_RESULT_COMMITTED = "committed"
_PAUSED_WORK_RESULTS = frozenset(
    {
        _PAUSED_WORK_RESULT_FAILED,
        "no-changes",
        _PAUSED_WORK_RESULT_RESIDUE,
        _PAUSED_WORK_RESULT_COMMITTED,
    }
)

#: The records that report one session's turn outcome. A pause names the
#: session it died on, so these are the only names its classification can
#: account for.
_PAUSED_TURN_EVENT_NAMES = frozenset(
    {
        events.TURN_EXECUTION_FINISHED_EVENT,
        events.TURN_FINISHED_EVENT,
    }
)

#: Records that describe work in progress. One of these after a run's terminal
#: record means the run kept working after it declared itself over -- unless it
#: is part of the one epilogue a run is allowed, see
#: :func:`_paused_work_settlement_group_ids`.
_POST_TERMINAL_WORK_EVENTS = frozenset(
    {
        events.COMMIT_FINISHED_EVENT,
        events.COMMIT_STARTED_EVENT,
        events.TURN_HEARTBEAT_EVENT,
        events.VERIFICATION_CHECK_FINISHED_EVENT,
        events.VERIFICATION_CHECK_STARTED_EVENT,
        events.VERIFICATION_FINISHED_EVENT,
        events.VERIFICATION_STARTED_EVENT,
    }
)

#: The records the paused-work settlement's own commit group writes. They carry
#: ``scope: "session"`` like any other session commit -- the settlement scope
#: lives on the group, not on the commit -- so they are recognized by the group
#: they name rather than by anything on the record itself.
_PAUSED_WORK_SETTLEMENT_COMMIT_EVENTS = frozenset(
    {
        events.COMMIT_FINISHED_EVENT,
        events.COMMIT_STARTED_EVENT,
    }
)

#: Turn-result keys naming an artifact, paired with the basename that artifact
#: must carry inside the turn directory. ``None`` names the directory itself.
_TURN_PATH_KEYS: tuple[tuple[str, str | None], ...] = (
    ("artifact_dir", None),
    ("stdout_path", "stdout.log"),
    ("stderr_path", "stderr.log"),
    ("last_message_path", "last-message.txt"),
    ("result_path", state.RESULT_FILE_NAME),
)

#: Lane rollup keeps the least reassuring status any of its checks returned.
_STATUS_PRECEDENCE = {"pass": 0, "unavailable": 1, "fail": 2}

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class CheckSpec:
    """The stable identity one deterministic check reports under.

    The invariant key is the check's contract with the issue inbox: it feeds
    the finding fingerprint, so the same defect in two runs deduplicates into
    one issue with two occurrences. Renaming one splits an issue's history.

    ``replayable`` records whether the check's verdict is a function of the
    bundle alone. Almost every check reads nothing else, which is what lets
    ``audit_replay`` re-judge a recorded bundle at HEAD and call the answer
    proof. A check that also consults the live filesystem answers about the
    tree as it stands now, not as the run left it, so replaying it can report a
    transition no commit caused. Those checks still run and still report on the
    live audit path; they are excluded from the replay matrix, where a verdict
    that cannot be reproduced is worse than no verdict at all.
    """

    check: str
    lane: assurance_models.RubricLane
    category: str
    component: str
    invariant_key: str
    severity: assurance_models.Severity
    recommendation: str
    replayable: bool = True


@dataclass(frozen=True)
class _Verdict:
    """What one check concluded, before it is stamped with its spec."""

    status: assurance_models.CheckStatus
    detail: str
    evidence: tuple[str, ...]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _detail(text: str) -> str:
    """Bound and flatten a detail so it survives ``DeterministicResult``."""
    collapsed = " ".join(_CONTROL_CHAR_RE.sub(" ", str(text)).split())
    return collapsed[: assurance_models.MAX_TEXT_CHARS] or "no detail recorded"


def _names(values: Iterable[Any], *, limit: int = 8) -> str:
    items = sorted({str(value) for value in values if value})
    if not items:
        return "none"
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        return f"{shown}, and {len(items) - limit} more"
    return shown


def _inside(locator: Any, root: str) -> bool:
    return isinstance(locator, str) and (
        locator == root or locator.startswith(f"{root}/")
    )


def _joined(claims: Iterable[str]) -> str:
    return "; ".join(sorted(set(claims)))


class _CheckContext:
    """A read-only view of one evidence bundle, shared by every check.

    Every path in the bundle has already been rewritten into a symbolic
    locator, so containment and agreement are decided by string comparison
    against an approved root rather than by touching the filesystem.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.roots = {
            str(name): str(value)
            for name, value in _mapping(payload.get("roots")).items()
        }
        self.declared = {
            str(entry.get("locator"))
            for entry in _sequence(payload.get("evidence"))
            if isinstance(entry, Mapping) and entry.get("locator")
        }
        self.turns = [
            turn
            for turn in _sequence(payload.get("turns"))
            if isinstance(turn, Mapping)
        ]
        self.records = [
            record
            for record in _sequence(self.section("events").get("records"))
            if isinstance(record, Mapping)
        ]
        self.omissions = [
            entry
            for entry in _sequence(payload.get("omissions"))
            if isinstance(entry, Mapping)
        ]

    def section(self, name: str) -> dict[str, Any]:
        return _mapping(self.payload.get(name))

    @property
    def events_shed(self) -> bool:
        """Whether the size cap dropped event records the checks would read."""
        return any(
            entry.get("section") == "events" and entry.get("reason") == REASON_SIZE_CAP
            for entry in self.omissions
        )

    @property
    def terminal_pending(self) -> bool:
        """Whether the run's terminal record is still owed at collection time.

        An audit that runs inside its own run reads the history before the
        terminal record exists. Absence then proves nothing, so the checks that
        read for a missing terminal record stay silent instead of reporting the
        ordering the audit itself requires.
        """
        return bool(self.section("events").get("terminal_pending"))

    @property
    def event_summary(self) -> dict[str, Any]:
        return _mapping(self.section("events").get("summary"))

    def turn_directories(self) -> list[str]:
        return [
            turn["directory"]
            for turn in self.turns
            if isinstance(turn.get("directory"), str)
        ]

    def evidence(self, *candidates: Any) -> tuple[str, ...]:
        """Keep the declared locators among ``candidates``, else the anchor."""
        chosen = [
            locator
            for locator in dict.fromkeys(candidates)
            if isinstance(locator, str) and locator in self.declared
        ]
        return tuple(chosen[: assurance_models.MAX_REFERENCES_PER_ITEM]) or (
            RUN_ANCHOR,
        )


def _check_root_registry_agreement(context: _CheckContext) -> _Verdict:
    plan = context.section("plan")
    metadata = _mapping(plan.get("metadata"))
    registry = context.section("registry")
    evidence = context.evidence(
        metadata.get("reference"),
        context.section("state").get("reference"),
        plan.get("reference"),
    )
    if EVIDENCE_ROOT_WORKSPACE not in context.roots:
        return _Verdict(
            "unavailable",
            "the run declared no workspace root, so no binding can be compared "
            "against one",
            evidence,
        )

    bindings: list[tuple[str, Any]] = []
    content = _mapping(metadata.get("content"))
    if metadata.get("available") and content.get("workspace_root") is not None:
        bindings.append(("the plan metadata", content.get("workspace_root")))
    if registry.get("registered"):
        if registry.get("workspace_root") is not None:
            bindings.append(("the run registry", registry.get("workspace_root")))
        for entry in _sequence(registry.get("repositories")):
            entry = _mapping(entry)
            alias = entry.get("alias") or "an unnamed repository"
            # ``path`` is deliberately workspace-relative in current runner
            # state.  Containment is about the repository ASL actually
            # resolved, so only the canonical ``resolved_root`` can establish
            # it.  Older, malformed, outside-root, or redacted entries fail
            # closed instead of being made safe by resolving a relative path
            # against the auditor process's cwd.
            bindings.append(
                (f"registered repository {alias}", entry.get("resolved_root"))
            )
    if not bindings:
        return _Verdict(
            "unavailable",
            "neither the plan metadata nor the run registry recorded a workspace "
            "binding to compare against the run's root, which is what a plan "
            "that predates workspace registration looks like rather than "
            "evidence of a disagreement",
            evidence,
        )

    disagreeing = [
        (
            f"{source} records no resolved root"
            if value is None
            else f"{source} resolves to {value}"
        )
        for source, value in bindings
        if value is None or not _inside(value, EVIDENCE_ROOT_WORKSPACE)
    ]
    if disagreeing:
        return _Verdict(
            "fail",
            "the run's workspace root disagrees with what its own records bind "
            f"it to: {_joined(disagreeing)}",
            evidence,
        )
    return _Verdict(
        "pass",
        f"all {len(bindings)} recorded workspace bindings resolve inside the "
        "run's workspace root",
        evidence,
    )


def _check_repository_intent(context: _CheckContext) -> _Verdict:
    plan = context.section("plan")
    evidence = context.evidence(
        plan.get("reference"), context.section("state").get("reference")
    )
    plan_root = context.roots.get(EVIDENCE_ROOT_PLAN)
    workspace_root = context.roots.get(EVIDENCE_ROOT_WORKSPACE)
    if not plan_root or not workspace_root or not Path(plan_root).is_dir():
        return _Verdict(
            "unavailable",
            "the run recorded no plan directory and workspace root to replay the "
            "repository-intent gate against",
            evidence,
        )
    # Replaying the gate reads the plan through ``plans``, which rolls a pending
    # publication forward as a side effect. An audit observes a run; it does not
    # finish one, so an interrupted plan is reported rather than repaired.
    if (Path(plan_root) / plans.PLAN_TRANSACTION_DIR_NAME).exists():
        return _Verdict(
            "unavailable",
            "the plan directory holds a pending publication transaction, and "
            "replaying the repository-intent gate would complete it rather than "
            "observe it",
            evidence,
        )
    exempt = [
        context.roots[name]
        for name in (EVIDENCE_ROOT_STATE, EVIDENCE_ROOT_RUN)
        if context.roots.get(name)
    ]
    try:
        intent, report = preflight.plan_repository_intent(
            plan_root, workspace_root, extra_exempt_roots=exempt
        )
    except (OSError, ValueError, plans.PlanError) as error:
        return _Verdict(
            "unavailable",
            f"the repository-intent gate could not be replayed after the run: {error}",
            evidence,
        )
    if report is None:
        if intent is not None:
            reachable = _names(Path(root).name for root in intent.git_roots)
            return _Verdict(
                "pass",
                "the legacy plan stays within its bound Git repository"
                + (f": {reachable}" if reachable else ""),
                evidence,
            )
        return _Verdict(
            "pass",
            "the plan declares a repository registry naming every repository it "
            "may reach",
            evidence,
        )
    reachable = _names(
        Path(str(root)).name for root in _sequence(report.get("git_roots"))
    )
    return _Verdict(
        "fail",
        f"the plan reaches {report.get('git_root_count')} git repositories but "
        f"declares no repository registry: {reachable}",
        evidence,
    )


def _check_task_packet_paths(context: _CheckContext) -> _Verdict:
    contradictions: list[str] = []
    references: list[str] = []
    observed = 0
    workspace_declared = EVIDENCE_ROOT_WORKSPACE in context.roots
    for turn in context.turns:
        directory = turn.get("directory")
        result = _mapping(turn.get("result"))
        if not isinstance(directory, str) or result.get("available") is False:
            continue
        references.append(directory)
        for key, basename in _TURN_PATH_KEYS:
            value = result.get(key)
            if value is None:
                continue
            observed += 1
            expected = directory if basename is None else f"{directory}/{basename}"
            if value != expected:
                contradictions.append(
                    f"{turn.get('name')} names {value} as its {key} rather than "
                    f"{expected}"
                )
        cwd = _mapping(result.get("invocation")).get("cwd")
        if isinstance(cwd, str) and workspace_declared:
            observed += 1
            if not _inside(cwd, EVIDENCE_ROOT_WORKSPACE):
                contradictions.append(
                    f"{turn.get('name')} ran in {cwd}, outside the run's workspace"
                )
    configured_cwd = context.section("configuration").get("cwd")
    if isinstance(configured_cwd, str) and workspace_declared:
        observed += 1
        if not _inside(configured_cwd, EVIDENCE_ROOT_WORKSPACE):
            contradictions.append(
                f"the run configuration names {configured_cwd} as its working "
                "directory, outside the run's workspace"
            )
    evidence = context.evidence(*references, context.section("state").get("reference"))
    if contradictions:
        return _Verdict(
            "fail",
            f"a task packet names paths its own run contradicts: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not observed:
        return _Verdict(
            "unavailable",
            "no turn recorded the artifact paths its packet was written from",
            evidence,
        )
    return _Verdict(
        "pass",
        f"all {observed} recorded packet paths agree with the artifacts they name",
        evidence,
    )


def _write_scope_session(*candidates: Any) -> str | None:
    """Name the session a write-scope record belongs to, first source wins.

    A guard record carries ``session_id`` as a string while a pause carries the
    numeric alias under ``session``, so both are normalized to text before they
    are matched against each other.
    """
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def _write_scope_violated_aliases(guard: Mapping[str, Any]) -> list[str]:
    """The repositories one guard record says were written out of scope."""
    return [
        str(alias)
        for alias in (
            _mapping(item).get("alias") or _mapping(item).get("repository")
            for item in _sequence(guard.get("violations"))
        )
        if alias
    ]


#: The pause reasons a write-guard record can arrive under. Both stop the run on
#: the guard's own verdict; the unavailable one is chosen when the same record
#: also failed to inventory something, which does not make the violations it
#: does name any less paused-upon.
_WRITE_SCOPE_PAUSE_REASONS = (
    events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
    events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
)

_ViolationFingerprint = tuple[tuple[str, str, str, tuple[str, ...]], ...]


def _write_scope_violation_fingerprint(
    guard: Mapping[str, Any],
) -> _ViolationFingerprint:
    """Reduce one guard record's violations to a comparable identity.

    Two records describe the same out-of-scope write only if they name the same
    repositories and the same paths inside them, so that is all the fingerprint
    keeps. The order the guard happened to list them in is not evidence of
    anything and is normalized away.
    """
    entries = [
        (
            str(violation.get("kind") or ""),
            str(violation.get("alias") or ""),
            str(violation.get("repository") or ""),
            tuple(
                sorted(
                    str(path)
                    for path in _sequence(violation.get("qualified_paths"))
                    if path
                )
            ),
        )
        for violation in (_mapping(item) for item in _sequence(guard.get("violations")))
    ]
    return tuple(sorted(entries))


def _commit_records(context: _CheckContext) -> list[Mapping[str, Any]]:
    """Every commit the bundle records, however that commit was learned.

    Covers the runner's own commit projections and the attempts read from
    ``commit-*`` directories alike, including the entries the audit derived
    from observed head transitions, because a commit the runner never ran
    still put the write it names into history.
    """
    commits = context.section("commits")
    records = [_mapping(item) for item in _sequence(commits.get("commits"))]
    records.extend(_mapping(item) for item in _sequence(commits.get("commit_groups")))
    records.extend(
        _mapping(_mapping(item).get("result"))
        for item in _sequence(commits.get("attempts"))
    )
    return [record for record in records if record]


def _commit_repository_alias(record: Mapping[str, Any]) -> str | None:
    """The repository one commit record names, or ``None`` when it names none.

    A record filed under a linked worktree is reduced to the repository the
    worktree belongs to, because a commit in a worktree lands in that
    repository's history and that is the repository a write-scope question asks
    about.
    """
    for key in ("repository_alias", "repo", "alias", "repository_key", "key"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value.split(_WORKTREE_KEY_SEPARATOR, 1)[0]
    return None


def _commit_attributions(context: _CheckContext) -> list[tuple[str | None, str | None]]:
    """The session and repository every commit in the bundle names.

    Either half may be ``None`` when the record does not say, and both readings
    matter: an unnamed session cannot be matched to a violation, and an unnamed
    repository cannot be ruled out of one.

    A commit group is descended into rather than read whole. The group is a
    container -- its per-repository detail lives in the entries beneath it, and
    the group itself names no repository -- so reading it whole would report a
    commit of unknown repository, which is the one shape that can never be
    ruled out (ASI-9). A group that names no repository anywhere committed
    nothing and contributes nothing.
    """
    pairs: list[tuple[str | None, str | None]] = []
    for record in _commit_records(context):
        session = _write_scope_session(record.get("session_id"))
        nested = [_mapping(item) for item in _sequence(record.get("repositories"))]
        if nested:
            pairs.extend(
                (
                    _write_scope_session(entry.get("session_id"), session),
                    _commit_repository_alias(entry),
                )
                for entry in nested
            )
            continue
        aliases = [
            alias
            for alias in _sequence(record.get("ordered_aliases"))
            if isinstance(alias, str) and alias
        ]
        if aliases:
            pairs.extend((session, alias) for alias in aliases)
        elif "repositories" not in record and "ordered_aliases" not in record:
            pairs.append((session, _commit_repository_alias(record)))
    return pairs


@dataclass(frozen=True)
class _WriteScopeAnalysis:
    """One reading of the write guard, split by whether the guard held.

    ``caught`` and ``escaped`` are disjoint by construction: every out-of-scope
    write lands in exactly one of them, so the two checks that report this
    analysis can never both name the same write.
    """

    caught: tuple[str, ...]
    escaped: tuple[str, ...]
    blind: tuple[str, ...]
    observed: int
    evidence: tuple[str, ...]


def _analyze_write_scope(context: _CheckContext) -> _WriteScopeAnalysis:
    """Judge every write-guard record, separating a catch from an escape.

    A write is *caught* only when the guard recorded the violation, reverted
    the session to pending, paused the run on
    ``workspace-write-scope-violation``, and no commit that could have carried
    the write followed: that is a working guard, not a containment failure. A
    commit could have carried it when it names the offending session and either
    a repository the violation names or no repository at all. Anything
    weaker is an *escape* — the change reached the tree unauthorized. Blindness
    is neither, because a guard that could not look proves nothing either way.

    Violations are grouped by session rather than reported per record. The
    runner writes one guard record into the turn result and the same record
    into the pause it raised, and a resumed run replays that pause with no turn
    of its own, so grouping is what keeps one out-of-scope write one claim.

    A violation no record attributes is correlated to the session named by a
    pause that recorded the very same violation; if nothing durable names one,
    it can still be caught, but only in a bundle that recorded no commit at
    all, because there is no session to match a commit against and claiming the
    guard held is a claim about evidence that was never checked.
    """
    escaped: list[str] = []
    blind: list[str] = []
    references: list[str] = []
    observed = 0
    offenders: dict[str | None, set[str]] = {}
    reverted: set[str | None] = set()
    unnamed: dict[_ViolationFingerprint, set[str]] = {}
    unnamed_reverted: set[_ViolationFingerprint] = set()
    paused_fingerprints: dict[_ViolationFingerprint, set[str]] = {}
    paused_sessions: set[str] = set()
    paused_any = False

    def _claim(
        session: str | None,
        aliases: list[str],
        fingerprint: _ViolationFingerprint,
        was_reverted: bool,
    ) -> None:
        """File one guard record's violation under the session it names.

        An unattributed record is filed under the violation it describes rather
        than under a shared unnamed bucket, because what it describes is the
        only thing that can later tie it to a session.
        """
        if session is None:
            unnamed.setdefault(fingerprint, set()).update(aliases)
            if was_reverted:
                unnamed_reverted.add(fingerprint)
            return
        offenders.setdefault(session, set()).update(aliases)
        if was_reverted:
            reverted.add(session)

    for turn in context.turns:
        result = _mapping(turn.get("result"))
        guard = _mapping(result.get("workspace_write_guard"))
        if not guard:
            continue
        observed += 1
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        aliases = _write_scope_violated_aliases(guard)
        if aliases:
            session = _write_scope_session(
                guard.get("session_id"), result.get("session_id")
            )
            _claim(
                session,
                aliases,
                _write_scope_violation_fingerprint(guard),
                guard.get("reverted_to_pending") is True,
            )
        if _mapping(guard.get("unavailable")):
            blind.append(f"{turn.get('name')} could not be inventoried")

    for record in context.records:
        name = record.get("name")
        record_payload = _mapping(record.get("payload"))
        reason = record_payload.get("reason")
        details = _mapping(record_payload.get("details"))
        guard = _mapping(details.get("workspace_write_guard"))
        if name == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT:
            observed += 1
            repair_aliases = _write_scope_violated_aliases(guard)
            if repair_aliases:
                # The repair is the run's second chance at containment: a
                # widened scope that still excludes a retained change means the
                # write outlived the guard rather than being held by it.
                escaped.append(
                    "the widened write scope still excludes retained changes "
                    f"({_names(repair_aliases)})"
                )
            elif _mapping(guard.get("unavailable")):
                blind.append("the widened write scope could not be inventoried")
            elif guard.get("passed") is not True:
                blind.append(
                    "the write-scope repair did not record a passing guard result"
                )
        elif name != events.RUN_PAUSED_EVENT:
            continue
        elif reason in _WRITE_SCOPE_PAUSE_REASONS:
            observed += 1
            if reason == events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE:
                blind.append("the write guard reported itself unavailable")
            aliases = _write_scope_violated_aliases(guard)
            # A guard that both found violations and failed to inventory
            # something pauses under the unavailable reason, because that is the
            # worse of the two. The pause is still the run stopping on this guard
            # record, and reading only the scope-violation reason reported a run
            # that did pause as one that never did (ASI-9). An unavailable pause
            # naming no violation is only blindness, and corroborates nothing.
            scope_pause = reason == events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
            if not (aliases or scope_pause):
                continue
            paused_any = True
            session = _write_scope_session(
                guard.get("session_id"),
                details.get("session_id"),
                details.get("session"),
            )
            if session is not None:
                paused_sessions.add(session)
            if aliases:
                fingerprint = _write_scope_violation_fingerprint(guard)
                if session is not None:
                    paused_fingerprints.setdefault(fingerprint, set()).add(session)
                _claim(
                    session,
                    aliases,
                    fingerprint,
                    guard.get("reverted_to_pending") is True,
                )

    attributions = _commit_attributions(context)
    any_commit = bool(attributions)

    # The runner writes one guard record into the turn result and the same
    # record into the pause it raises, so a bundle where only the pause carries
    # the session id splits one write into a named claim and an unnamed one.
    # Rejoining those halves needs proof that they are halves of one write, and
    # the guard's own violation list — the repositories it names and the paths
    # inside them — is the only durable evidence of that. An unnamed record
    # describing something no pause described is a second, distinct violation
    # that keeps its own unattributed reading, and a violation two pauses
    # attribute to two sessions makes the choice a guess, so it stays unnamed
    # too and is judged as ambiguous below.
    for fingerprint, aliases in unnamed.items():
        candidates = paused_fingerprints.get(fingerprint, set())
        attributed = next(iter(candidates)) if len(candidates) == 1 else None
        offenders.setdefault(attributed, set()).update(aliases)
        if fingerprint in unnamed_reverted:
            reverted.add(attributed)

    def _paused_for(session: str | None) -> bool:
        """Whether a scope-violation pause corroborates this session's catch.

        A pause names the session it stopped, so the match is per session. A
        bundle that recorded the pause without attributing it falls back to the
        run-level fact, because an unattributable pause is still a pause and
        reading it as none would report a working guard as an escape.
        """
        if session is not None and paused_sessions:
            return session in paused_sessions
        return paused_any

    pause_unknown = context.events_shed and not paused_any
    caught: list[str] = []
    for session, aliases in sorted(offenders.items(), key=lambda item: item[0] or ""):
        who = f"session {session}" if session is not None else "an unnamed session"
        claim = f"{who} wrote outside its declared scope ({_names(aliases)})"
        # A commit naming the offending session cannot predate the guard, which
        # runs after the turn it judges, so its presence says the run carried
        # the out-of-scope write forward instead of holding it. The commit must
        # also be able to carry *this* write: a commit writes one repository's
        # history, so one in a repository the violation does not name cannot
        # have carried it and does not defeat the catch (ASI-9). A commit whose
        # repository the record does not say still can, and is counted. A
        # violation no record attributes cannot be matched against a commit by
        # session at all, so every commit in the bundle is a candidate for
        # having carried it and the catch is conceded rather than claimed on
        # evidence that was never checked.
        if session is None:
            uncommitted = not any_commit
            commit_note = (
                "the run recorded commits that cannot be ruled out as carrying "
                "the unattributed violation"
            )
        else:
            uncommitted = not any(
                committed_session == session
                and (committed_alias is None or committed_alias in aliases)
                for committed_session, committed_alias in attributions
            )
            commit_note = (
                "a commit for that session followed that could have carried the write"
            )
        missing = [
            (key, note)
            for key, note, held in (
                (
                    "reverted",
                    "the session was not reverted to pending",
                    session in reverted,
                ),
                (
                    "paused",
                    "the run never paused on the violation",
                    _paused_for(session),
                ),
                ("committed", commit_note, uncommitted),
            )
            if not held
        ]
        # The pause is the one fact here that lives only in the event log, so a
        # bundle whose event records the size cap dropped cannot say the run
        # never paused -- only that this bundle does not hold the record. Where
        # that is the sole thing standing between the guard and a catch, the
        # honest reading is that containment could not be judged, not that a
        # write escaped (ASI-9).
        if pause_unknown and [key for key, _ in missing] == ["paused"]:
            blind.append(
                f"{claim}, and the event records the size cap dropped are where "
                "the pause that would settle it would be"
            )
        elif missing:
            escaped.append(f"{claim} and {', '.join(note for _, note in missing)}")
        else:
            caught.append(
                f"{claim}, and the guard reverted it to pending and paused the "
                "run before anything was committed"
            )

    return _WriteScopeAnalysis(
        caught=tuple(caught),
        escaped=tuple(escaped),
        blind=tuple(blind),
        observed=observed,
        evidence=context.evidence(
            *references, context.section("state").get("reference")
        ),
    )


def _check_scope_escape(context: _CheckContext) -> _Verdict:
    """Fail only for a write that got past the guard, never for one it held."""
    analysis = _analyze_write_scope(context)
    if analysis.escaped:
        return _Verdict("fail", _joined(analysis.escaped), analysis.evidence)
    if analysis.blind:
        return _Verdict(
            "unavailable",
            "the workspace write guard could not judge containment: "
            f"{_joined(analysis.blind)}",
            analysis.evidence,
        )
    if not analysis.observed:
        return _Verdict(
            "unavailable",
            "no turn recorded a workspace write guard result, so scope "
            "containment cannot be judged",
            analysis.evidence,
        )
    if analysis.caught:
        return _Verdict(
            "pass",
            f"the guard held every out-of-scope write across {analysis.observed} "
            "recorded write-guard and repair results, so none of them reached "
            "the tree unauthorized",
            analysis.evidence,
        )
    return _Verdict(
        "pass",
        f"all {analysis.observed} recorded write-guard and repair results kept "
        "every retained write inside the session's declared scope",
        analysis.evidence,
    )


def _check_scope_guard_catch(context: _CheckContext) -> _Verdict:
    """Report the writes the guard stopped, which are a plan defect, not a leak.

    Never ``unavailable``: a guard that could not look is reported by
    :func:`_check_scope_escape`, which owns the critical reading, and both
    checks share one lane so the rollup keeps the worse of the two.
    """
    analysis = _analyze_write_scope(context)
    if analysis.caught:
        return _Verdict("fail", _joined(analysis.caught), analysis.evidence)
    return _Verdict(
        "pass",
        "no session had to be stopped for writing outside its declared scope",
        analysis.evidence,
    )


def _check_state_event_agreement(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.section("state").get("reference"),
        context.event_summary.get("reference"),
    )
    terminal = [
        record
        for record in context.records
        if record.get("name") in _TERMINAL_EVENT_NAMES
    ]
    if "ok" not in status and "paused" not in status and not terminal:
        return _Verdict(
            "unavailable",
            "the run recorded neither a terminal outcome in its state nor a "
            "terminal event record",
            evidence,
        )
    paused_records = [
        record for record in terminal if record.get("name") == events.RUN_PAUSED_EVENT
    ]
    finished_records = [
        record for record in terminal if record.get("name") == events.RUN_FINISHED_EVENT
    ]

    contradictions: list[str] = []
    blind: list[str] = []

    def absent(claim: str) -> None:
        (blind if context.events_shed else contradictions).append(claim)

    if not context.terminal_pending:
        if status.get("paused") is True and not paused_records:
            absent("state records the run as paused but no run_paused record survives")
        if status.get("ok") is True and not finished_records:
            absent("state records the run as ok but no run_finished record survives")
    if paused_records and status.get("paused") is False:
        contradictions.append(
            "the event log records a pause the state projection denies"
        )
    if paused_records and status.get("ok") is True:
        contradictions.append(
            "the event log records a pause the state projection reports as success"
        )
    if (
        any(
            _mapping(record.get("payload")).get("ok") is False
            for record in finished_records
        )
        and status.get("ok") is True
    ):
        contradictions.append(
            "the run_finished record reports failure while the state projection "
            "reports success"
        )
    reason = status.get("pause_reason")
    for record in paused_records:
        recorded = _mapping(record.get("payload")).get("reason")
        if reason and recorded and recorded != reason:
            contradictions.append(
                f"state names {reason} as the pause reason while the run_paused "
                f"record names {recorded}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the persisted state and the event log tell different stories: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the event log was shed under the size cap, so {_joined(blind)}",
            evidence,
        )
    return _Verdict(
        "pass",
        "the persisted status agrees with the run's terminal event records",
        evidence,
    )


def _paused_work_settlement_group_ids(
    tail: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """Commit-group ids the paused-work settlement opened after the terminal record.

    Settling the work a recoverable pause retained is the one thing a run does
    after it has already declared itself over. ``run_loop`` calls the
    settlement at the point every loop break converges on, deliberately
    downstream of the ``run_paused`` record it settles for -- the revert, the
    plan re-read and the pause must all have happened before it can know what
    it is committing. Its commit records are therefore post-terminal by
    construction, and reading them as a run that kept working would fail every
    run that settles anything.

    Only a block that closed earns the exemption. The settlement writes its
    ``paused_work_committed`` record last, so a group with no such record after
    it is a settlement that died mid-commit. That is work after the terminal
    record in exactly the sense this check means, and is left to fail.
    """
    opened: dict[str, int] = {}
    closed_at: list[int] = []
    for index, record in enumerate(tail):
        name = str(record.get("name") or "")
        if name == events.PAUSED_WORK_COMMITTED_EVENT:
            closed_at.append(index)
            continue
        if name != events.COMMIT_GROUP_STARTED_EVENT:
            continue
        group = _mapping(_mapping(record.get("payload")).get("group"))
        if group.get("scope") != commit_groups.GROUP_SCOPE_SESSION_PAUSED:
            continue
        group_id = group.get("group_id")
        if isinstance(group_id, str) and group_id:
            opened.setdefault(group_id, index)
    if not opened or not closed_at:
        return frozenset()
    last_closed = closed_at[-1]
    return frozenset(
        group_id for group_id, index in opened.items() if index < last_closed
    )


def _settles_paused_work(
    record: Mapping[str, Any], settlement_group_ids: frozenset[str]
) -> bool:
    """Whether one post-terminal record is part of a closed settlement block."""
    if not settlement_group_ids:
        return False
    if str(record.get("name") or "") not in _PAUSED_WORK_SETTLEMENT_COMMIT_EVENTS:
        return False
    return _mapping(record.get("payload")).get("group_id") in settlement_group_ids


def _check_terminal_artifacts(context: _CheckContext) -> _Verdict:
    problems: list[str] = []
    blind: list[str] = []
    references: list[str] = []
    for turn in context.turns:
        result = _mapping(turn.get("result"))
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        if result.get("available") is False:
            problems.append(
                f"{turn.get('name')} wrote no readable {state.RESULT_FILE_NAME} "
                f"({result.get('reason')})"
            )
    evidence = context.evidence(context.event_summary.get("reference"), *references)
    names = [str(record.get("name") or "") for record in context.records]
    if not names:
        if problems:
            return _Verdict("fail", _joined(problems), evidence)
        return _Verdict(
            "unavailable",
            "the run's slice of the event log holds no records to order",
            evidence,
        )

    if _RUN_STARTED_EVENT not in names:
        (blind if context.events_shed else problems).append(
            "the run recorded no run_started event"
        )
    elif names[0] != _RUN_STARTED_EVENT:
        problems.append(
            f"the run's first record is {names[0]} rather than {_RUN_STARTED_EVENT}"
        )
    terminal_at = [
        index for index, name in enumerate(names) if name in _TERMINAL_EVENT_NAMES
    ]
    if not terminal_at:
        if not context.terminal_pending:
            (blind if context.events_shed else problems).append(
                "the run recorded no terminal event"
            )
    elif len(terminal_at) > 1:
        problems.append(f"the run recorded {len(terminal_at)} terminal events")
    settled = False
    if terminal_at:
        tail = context.records[terminal_at[-1] + 1 :]
        settlement_group_ids = _paused_work_settlement_group_ids(tail)
        settled = bool(settlement_group_ids)
        trailing = [
            str(record.get("name") or "")
            for record in tail
            if str(record.get("name") or "") in _POST_TERMINAL_WORK_EVENTS
            and not _settles_paused_work(record, settlement_group_ids)
        ]
        if trailing:
            problems.append(
                f"{len(trailing)} record(s) of work follow the run's terminal "
                f"record: {_names(trailing)}"
            )

    if problems:
        return _Verdict(
            "fail",
            f"the run's terminal artifacts are missing or out of order: "
            f"{_joined(problems)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the event log was shed under the size cap, so {_joined(blind)} "
            "cannot be distinguished from one the cap removed",
            evidence,
        )
    closing = (
        "has not yet written the terminal record it is being audited ahead of"
        if not terminal_at
        else "closes on exactly one terminal record"
    )
    epilogue = (
        ", followed only by the settlement of the work its pause retained"
        if settled
        else ""
    )
    return _Verdict(
        "pass",
        f"the run opens on {_RUN_STARTED_EVENT}, {closing}{epilogue}, and every "
        f"turn wrote a readable {state.RESULT_FILE_NAME}",
        evidence,
    )


def _is_expected_conditional_skip(entry: Any) -> bool:
    """Whether a skip is the recipe working as designed rather than a gap.

    A diagnostic that names the gates it explains and was omitted because none
    of them failed proves the recipe reached a clean result, so counting it
    against the run's success claim would make every healthy recipe look
    contradicted. Every other skip is a check that did not run.
    """

    if not isinstance(entry, Mapping):
        return False
    return bool(
        entry.get("reason") == verification.SKIP_REASON_CONDITION_NOT_MET
        and _sequence(entry.get("on_failure_of"))
    )


def _verification_covered_the_turn(session_verification: Mapping[str, Any]) -> bool:
    """Whether a recipe actually gated the changes the turn made.

    An expected skip is only neutral while something else still checked the
    work. A recipe that declared no gates, or that executed nothing, left the
    turn's mutation uncovered no matter how well-formed its skips were.
    """

    gate_count = _mapping(session_verification.get("recipe")).get("gate_count")
    if isinstance(gate_count, int) and gate_count <= 0:
        return False
    return bool(_sequence(session_verification.get("checks")))


def _superseded_turn_names(turns: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Turns a later clean completion of the same session replaced.

    The loop retries a session in place, so a turn that ended badly is
    counter-evidence only while it is still the last word on that session. Once
    a later turn completed the same session cleanly, counting the earlier
    attempt against the run's success claim would report every recovered run as
    a false success. A turn whose index or session cannot be read is never
    treated as superseded, so an unreadable projection keeps the finding.
    """

    completed: dict[str, int] = {}
    for turn in turns:
        if turn.get("anomalous") is True:
            continue
        result = _mapping(turn.get("result"))
        session_id = result.get("session_id")
        index = result.get("turn_index")
        if not isinstance(session_id, str) or not isinstance(index, int):
            continue
        if result.get("status_result") != "complete":
            continue
        completed[session_id] = max(completed.get(session_id, index), index)
    superseded: set[str] = set()
    for turn in turns:
        result = _mapping(turn.get("result"))
        session_id = result.get("session_id")
        index = result.get("turn_index")
        name = turn.get("name")
        if not isinstance(session_id, str) or not isinstance(index, int):
            continue
        if not isinstance(name, str):
            continue
        latest = completed.get(session_id)
        if latest is not None and latest > index:
            superseded.add(name)
    return frozenset(superseded)


#: A deferred broad-check failure is closed by a later all-clear review
#: (``resolved``), still waiting on a repair loop the run never finished
#: (``pending``), or accepted without either (``unresolved``).
_DeferralResolution = Literal["resolved", "pending", "unresolved"]


def _recorded_review_cycles(context: _CheckContext) -> tuple[Mapping[str, Any], ...]:
    """Return ordered cycle evidence, preferring the durable state projection."""
    projected = _sequence(context.section("review").get("review_cycles"))
    if projected:
        return tuple(_mapping(item) for item in projected)
    return tuple(
        _mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.REVIEW_CYCLE_EVENT
    )


def _cycle_is_all_clear(cycle: Mapping[str, Any]) -> bool:
    """Whether one recorded review cycle closed the loop with nothing left."""
    return review_continuation.cycle_is_all_clear(cycle)


def _run_is_in_flight(context: _CheckContext) -> bool:
    """Whether the run stopped short of an outcome rather than reaching one."""
    status = context.section("status")
    if status.get("paused") is True:
        return True
    names = {record.get("name") for record in context.records}
    if events.RUN_PAUSED_EVENT in names:
        return True
    return events.RUN_FINISHED_EVENT not in names and "ok" not in status


def _deferred_review_resolution(
    context: _CheckContext, session_id: Any
) -> _DeferralResolution:
    """How a deferred findings review ended: cleared, still open, or unearned.

    Only a later all-clear review closes a deferral. But a run that paused
    before its repair loop converged never got the chance to record one, and
    grading that as a false deferral claim reads an unfinished run as a
    dishonest one. Such a run is ``pending``: evidence the audit has not seen
    yet rather than evidence that contradicts the run.
    """
    cycles = _recorded_review_cycles(context)
    if review_continuation.deferred_review_is_cleared(
        cycles, review_session_id=session_id
    ):
        return "resolved"
    # Deliberately not gated on a repairs-materialized source: a run can pause
    # while its only cycle is malformed, and that is still an unfinished repair
    # loop rather than an unearned acceptance.
    if _run_is_in_flight(context):
        return "pending"
    return "unresolved"


def _accepted_verification_is_accounted_for(
    context: _CheckContext,
    verification_payload: Mapping[str, Any],
    *,
    session_id: Any,
) -> _DeferralResolution:
    """Validate deferral identity, raw failures, and its later all-clear.

    Identity and name validation stay a hard ``unresolved``: a malformed
    acceptance is a defect whether or not the run ever finished.
    """
    acceptance = _deferred_review_acceptance(verification_payload)
    if acceptance is None:
        return "unresolved"
    deferred = verification_payload.get("deferred_failures")
    if deferred is not None:
        failures = [_mapping(item) for item in _sequence(deferred)]
        names = [failure.get("name") for failure in failures]
        if names != list(_sequence(acceptance.get("deferred_failure_names"))):
            return "unresolved"
        if any(
            not isinstance(name, str) or not name.startswith(_BROAD_REVIEW_CHECK_PREFIX)
            for name in names
        ):
            return "unresolved"
    return _deferred_review_resolution(context, session_id)


def _review_cycles_ended_all_clear(context: _CheckContext) -> bool:
    """Whether the last review cycle the run recorded closed every finding."""
    cycles = _recorded_review_cycles(context)
    return bool(cycles) and _cycle_is_all_clear(cycles[-1])


def _check_misleading_success(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    finished = [
        record
        for record in context.records
        if record.get("name") == events.RUN_FINISHED_EVENT
    ]
    claimed = status.get("ok") is True or any(
        _mapping(record.get("payload")).get("ok") is True for record in finished
    )

    counter: list[str] = []
    references: list[str] = []
    superseded = _superseded_turn_names(context.turns)
    for turn in context.turns:
        result = _mapping(turn.get("result"))
        guard = _mapping(result.get("workspace_write_guard"))
        # A retry replaces the attempt, never the breach: a turn that wrote
        # outside its declared scope stays counter-evidence however cleanly the
        # session finished afterwards.
        anomalous = turn.get("anomalous") is True and turn.get("name") not in superseded
        contradicts = anomalous or bool(_sequence(guard.get("violations")))
        if anomalous:
            counter.append(f"{turn.get('name')} recorded an anomalous result")
        if _sequence(guard.get("violations")):
            counter.append(f"{turn.get('name')} exceeded its declared write scope")
        if contradicts and isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
    for session in _sequence(context.section("verification").get("sessions")):
        session_verification = _mapping(_mapping(session).get("verification"))
        # The whole entry, not just its failure: a coverage gap in a superseded
        # turn's recipe reappears on the turn that replaced it.
        if _mapping(session).get("turn") in superseded:
            continue
        if (
            session_verification.get("passed") is False
            # A deferral whose repair loop the run never finished is unfinished
            # evidence, not a contradiction of the success the run claims.
            and _accepted_verification_is_accounted_for(
                context,
                session_verification,
                session_id=_mapping(session).get("session_id")
                or session_verification.get("session"),
            )
            == "unresolved"
        ):
            counter.append(f"verification failed for {_mapping(session).get('turn')}")
        skipped = _sequence(session_verification.get("skipped_checks"))
        unexplained = [
            entry for entry in skipped if not _is_expected_conditional_skip(entry)
        ]
        if unexplained:
            counter.append(
                f"{len(unexplained)} verification check(s) were skipped for "
                f"{_mapping(session).get('turn')} without a condition that "
                "explains the skip"
            )
        elif skipped and not _verification_covered_the_turn(session_verification):
            counter.append(
                f"every verification check for {_mapping(session).get('turn')} was "
                "skipped, so nothing gated the changes it made"
            )
    skipped_records = [
        _mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.VERIFICATION_CHECK_SKIPPED_EVENT
    ]
    unexplained_records = [
        payload
        for payload in skipped_records
        if not _is_expected_conditional_skip(payload)
    ]
    if unexplained_records:
        counter.append(
            f"{len(unexplained_records)} verification check(s) recorded themselves "
            "as skipped without a condition that explains the skip"
        )
    elif skipped_records and not context.events_shed:
        # Absence of a ``verification_check_finished`` record is only evidence
        # in a log the cap kept whole. Shedding is not neutral here: a skipped
        # record outranks a finished one, so a shed bundle keeps exactly the
        # half of the pair that reads as a coverage gap and drops the half that
        # would refute it (ASI-23).
        executed_turns = {
            _mapping(record.get("payload")).get("turn_index")
            for record in context.records
            if record.get("name") == events.VERIFICATION_CHECK_FINISHED_EVENT
        }
        uncovered = sorted(
            {
                str(payload.get("turn_index"))
                for payload in skipped_records
                if payload.get("turn_index") not in executed_turns
            }
        )
        if uncovered:
            counter.append(
                f"every verification check recorded for turn(s) "
                f"{_joined(uncovered)} was skipped, so nothing gated the changes "
                "they made"
            )
    if status.get("paused") is True:
        counter.append("the state projection records the run as paused")
    if any(record.get("name") == events.RUN_PAUSED_EVENT for record in context.records):
        counter.append("the event log records the run as paused")
    verdict = _mapping(context.section("review").get("review_verdict"))
    # The verdict is rewritten per cycle, so this is what the last review
    # *found*, not what it left open. Only the cycle log says whether a later
    # all-clear closed them.
    found = verdict.get("total_findings")
    if (
        isinstance(found, int)
        and found > 0
        and not _review_cycles_ended_all_clear(context)
    ):
        counter.append(
            f"the recorded review verdict reports {found} finding(s) and no later "
            "all-clear review closed them"
        )

    evidence = context.evidence(context.section("state").get("reference"), *references)
    if claimed and counter:
        return _Verdict(
            "fail",
            f"the run reports success its own evidence contradicts: {_joined(counter)}",
            evidence,
        )
    if claimed:
        return _Verdict(
            "pass",
            "nothing the run recorded contradicts the success it claims",
            evidence,
        )
    if "ok" not in status and not finished:
        return _Verdict(
            "unavailable",
            "the run recorded no outcome to weigh against its own evidence",
            evidence,
        )
    return _Verdict(
        "pass", "the run claimed no success, so no success is unearned", evidence
    )


def _check_retry_resume(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.section("state").get("reference"),
        context.event_summary.get("reference"),
    )
    reason = status.get("pause_reason")
    paused = status.get("paused") is True or any(
        record.get("name") == events.RUN_PAUSED_EVENT for record in context.records
    )
    recorded = _mapping(status.get("pause_policy"))
    if paused and reason and not recorded:
        return _Verdict(
            "unavailable",
            f"the run paused for {reason} but recorded no retry classification to "
            "check against the pause policy",
            evidence,
        )

    contradictions: list[str] = []
    # A block written under an earlier policy version classified the same keys
    # by different rules, so diffing it against today's classifier reports a
    # schema change as a contradiction.
    if recorded and recorded.get("policy_version") == pause_policy.POLICY_VERSION:
        expected = pause_policy.classify_pause(
            reason if isinstance(reason, str) else None
        )
        for key in ("reason", "recoverable", "requires_user", "max_retries"):
            if recorded.get(key) != expected[key]:
                contradictions.append(
                    f"the recorded retry policy gives {key}={recorded.get(key)!r} "
                    f"where the pause classifier gives {expected[key]!r}"
                )
    retries: dict[str, list[dict[str, Any]]] = {}
    for record in context.records:
        if record.get("name") != events.REVIEW_VERDICT_RETRY_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        retries.setdefault(str(payload.get("session")), []).append(payload)
    for session, attempts in sorted(retries.items()):
        budgets = [
            payload["max_attempts"]
            for payload in attempts
            if isinstance(payload.get("max_attempts"), int)
        ]
        if not budgets:
            contradictions.append(
                f"session {session} recorded {len(attempts)} retry attempt(s) "
                "without naming the budget they were spent against"
            )
            continue
        budget = min(budgets)
        if len(attempts) > budget:
            contradictions.append(
                f"session {session} was relaunched {len(attempts)} times against a "
                f"budget of {budget}"
            )
        beyond = [
            payload["attempt"]
            for payload in attempts
            if isinstance(payload.get("attempt"), int) and payload["attempt"] > budget
        ]
        if beyond:
            contradictions.append(
                f"session {session} recorded retry attempt {max(beyond)} beyond its "
                f"budget of {budget}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the run's retry and resume records are inconsistent: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not recorded and not retries:
        if not paused:
            return _Verdict(
                "pass",
                "the run neither paused nor retried, so no retry budget was spent",
                evidence,
            )
        return _Verdict(
            "unavailable",
            "the run paused without recording either a reason or a retry "
            "classification",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the recorded retry classification matches the pause policy and "
        f"{len(retries)} session(s) stayed inside their retry budget",
        evidence,
    )


def _harness_failure_classification(context: _CheckContext) -> dict[str, Any]:
    """The classification the run attached to the harness failure it paused on.

    Empty when the run did not pause on a harness failure, which is every pause
    this invariant has nothing to say about.
    """
    status = context.section("status")
    if status.get("paused") is not True:
        return {}
    details = _mapping(status.get("pause_details"))
    return _mapping(details.get("failure_classification"))


def _classified_failure_sessions(context: _CheckContext) -> set[Any]:
    """Sessions whose turn failure the run named a reason for when it paused.

    The pause carries the classification for the turn it died on, and that
    classification is what the harness-failure check grades. Counting the same
    turn again as evidence nothing looked at reports a named failure as an
    unknown one, whether or not anything will relaunch it.
    """
    if not _harness_failure_classification(context).get("reason"):
        return set()
    session = _mapping(context.section("status").get("pause_details")).get("session")
    return set() if session is None else {session}


def _abort_turn_records(context: _CheckContext, action: str) -> list[dict[str, Any]]:
    """The run-control payloads carrying one turn-abort action, in order."""
    payloads: list[dict[str, Any]] = []
    for record in context.records:
        if record.get("name") != events.RUN_CONTROL_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        if payload.get("action") == action:
            payloads.append(payload)
    return payloads


def _aborted_turns(context: _CheckContext) -> set[tuple[Any, Any]]:
    """The turns the run killed because an operator asked it to.

    Keyed by session and turn index together, so the exemption covers the one
    turn the abort named rather than every turn that session ever ran.
    """
    return {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED
        )
        if payload.get("abort_reason")
    }


def _check_operator_abort(context: _CheckContext) -> _Verdict:
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    honored = _abort_turn_records(context, events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED)
    if not honored:
        return _Verdict(
            "pass",
            "the run killed no turn on an operator abort",
            evidence,
        )
    requested = {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED
        )
    }
    contradictions: list[str] = []
    for payload in honored:
        turn = (payload.get("session"), payload.get("turn_index"))
        if turn not in requested:
            contradictions.append(
                f"session {turn[0]!r} turn {turn[1]!r} was killed on an abort no "
                "recorded request asked for"
            )
        elif not payload.get("abort_reason"):
            contradictions.append(
                f"session {turn[0]!r} turn {turn[1]!r} was killed on an abort "
                "that names no reason"
            )
    if contradictions:
        return _Verdict(
            "fail",
            f"the run killed a turn without an accountable abort behind it: "
            f"{_joined(contradictions)}",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(honored)} turn(s) were killed on an operator abort the run "
        "recorded a request and a reason for",
        evidence,
    )


def _check_retryable_transport_failure(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    classification = _harness_failure_classification(context)
    if not classification:
        return _Verdict(
            "pass",
            "the run did not pause on a harness failure, so no retry was owed "
            "to any classification",
            evidence,
        )

    reason = classification.get("reason")
    retryable = classification.get("retryable")
    policy = _mapping(status.get("pause_policy"))
    if not policy:
        return _Verdict(
            "unavailable",
            f"the run classified its harness failure as {reason!r} but recorded "
            "no pause policy to judge that classification against",
            evidence,
        )

    contradictions: list[str] = []
    expected_retryable = reason in events.RETRYABLE_HARNESS_FAILURE_REASONS
    if isinstance(retryable, bool) and retryable is not expected_retryable:
        contradictions.append(
            f"the failure is classified retryable={retryable!r} while its reason "
            f"{reason!r} is {'' if expected_retryable else 'not '}one the "
            "classifier marks retryable"
        )
    if retryable is True:
        # A retryable failure is one the supervisor may relaunch unattended.
        # Either half of that going missing strands the run: a policy that
        # refuses the relaunch, or one that sends it to a human who has no
        # answer to give.
        if policy.get("recoverable") is not True:
            contradictions.append(
                "the failure is classified retryable while its pause policy "
                f"gives recoverable={policy.get('recoverable')!r}, so nothing "
                "will relaunch it"
            )
        if policy.get("requires_user") is not False:
            contradictions.append(
                "the failure is classified retryable while its pause policy "
                f"gives requires_user={policy.get('requires_user')!r}, so it "
                "waits on a person no reply can help"
            )
        budget = policy.get("max_retries")
        if not isinstance(budget, int) or budget <= 0:
            contradictions.append(
                f"the failure is classified retryable while its pause policy "
                f"grants max_retries={budget!r}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the run's harness-failure classification contradicts the policy it "
            f"paused under: {_joined(contradictions)}",
            evidence,
        )
    if retryable is True:
        fingerprint = classification.get("fingerprint") or reason
        return _Verdict(
            "pass",
            f"the turn failed with the retryable {fingerprint} "
            f"classification and the run paused under a policy granting "
            f"{policy.get('max_retries')} supervisor relaunch(es), which is where "
            "the retry is owed rather than inside this run",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the turn failed with the non-retryable {reason!r} classification and "
        "the run paused rather than relaunching it",
        evidence,
    )


def _check_review_independence(context: _CheckContext) -> _Verdict:
    review_section = context.section("review")
    run_state = context.section("state")
    evidence = context.evidence(run_state.get("reference"))
    if not review_section and not _sequence(
        context.section("verification").get("sessions")
    ):
        return _Verdict(
            "unavailable",
            "the run recorded neither a review verdict nor a verification result "
            "to judge independence by",
            evidence,
        )

    verdict = review_section.get("review_verdict")
    harness = run_state.get("harness")
    review_harness = review_section.get("review_harness")
    contradictions: list[str] = []
    if verdict and harness and review_harness == harness:
        contradictions.append(f"the {harness} harness reviewed its own work")
    if verdict == "approved" and review_section.get("review_completed") is False:
        contradictions.append("a verdict was approved without the review completing")
    if contradictions:
        return _Verdict(
            "fail",
            f"the run's review did not stand apart from the work it graded: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not verdict:
        return _Verdict(
            "unavailable",
            "the run recorded no review verdict, so review independence cannot be "
            "judged",
            evidence,
        )
    if not review_harness:
        return _Verdict(
            "unavailable",
            "the run recorded a review verdict without naming the harness that "
            "produced it",
            evidence,
        )
    if not harness:
        return _Verdict(
            "unavailable",
            "the run recorded a review verdict without naming the harness that "
            "did the work the review graded",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the {review_harness} review harness is independent of the {harness} "
        "implementation harness",
        evidence,
    )


def _verification_turn_label(key: tuple[Any, Any]) -> str:
    turn_index = key[1]
    return "an unidentified turn" if turn_index is None else f"turn {turn_index}"


def _check_verification_outcomes(context: _CheckContext) -> _Verdict:
    """Reconcile every failing check against the outcome its recipe reported.

    A verification only earns ``passed`` when every failure it saw was a
    conditional diagnostic, which by construction does not gate. So a failing
    check is accounted for in exactly two ways: the recipe reported the failure,
    or the recipe absorbed it as a diagnostic. Anything else is a verification
    that watched a check fail and said nothing. Both the turn result and the
    event records are read, because either can survive without the other.

    Event records carry the role each executed check played, so a failure is
    reconciled against what that check actually was. Records collected before
    the role was emitted fall back to the count of diagnostics that ran, which
    bounds how many failures a recipe could have absorbed.
    """

    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    executed: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    outcomes: dict[tuple[Any, Any], dict[str, Any]] = {}
    for record in context.records:
        name = record.get("name")
        if name not in (
            events.VERIFICATION_CHECK_FINISHED_EVENT,
            events.VERIFICATION_FINISHED_EVENT,
        ):
            continue
        payload = _mapping(record.get("payload"))
        key = (payload.get("run_id"), payload.get("turn_index"))
        if name == events.VERIFICATION_FINISHED_EVENT:
            outcomes[key] = payload
        else:
            executed.setdefault(key, []).append(payload)

    sessions = [
        _mapping(session)
        for session in _sequence(context.section("verification").get("sessions"))
        if _mapping(_mapping(session).get("verification"))
    ]
    if not executed and not outcomes and not sessions:
        return _Verdict(
            "unavailable",
            "the run recorded no verification outcomes to reconcile",
            evidence,
        )

    problems: list[str] = []
    blind: list[str] = []
    failing_total = 0
    for session in sessions:
        outcome = _mapping(session.get("verification"))
        if outcome.get("passed") is False:
            acceptance = outcome.get("acceptance")
            if acceptance is not None:
                deferred = [
                    _mapping(entry)
                    for entry in _sequence(outcome.get("deferred_failures"))
                ]
                failing_total += len(deferred)
                resolution = _accepted_verification_is_accounted_for(
                    context,
                    outcome,
                    session_id=session.get("session_id") or outcome.get("session"),
                )
                if resolution == "unresolved":
                    problems.append(
                        f"the failed verification recorded for {session.get('turn')} "
                        "claims broad-check deferral without a matching repair cycle "
                        "and later all-clear review"
                    )
                elif resolution == "pending":
                    blind.append(
                        f"the broad-check failures deferred by "
                        f"{session.get('turn')} reached no all-clear review "
                        "before the run stopped"
                    )
            continue
        if outcome.get("passed") is not True:
            continue
        failed = [
            entry
            for entry in _sequence(outcome.get("checks"))
            if _payload_shows_failure(_mapping(entry))
        ]
        failing_total += len(failed)
        absorbed = len(_sequence(outcome.get("diagnostic_failures")))
        if len(failed) > absorbed:
            problems.append(
                f"the verification recorded for {session.get('turn')} passed while "
                f"{len(failed)} of its check(s) failed and only {absorbed} of those "
                "was a diagnostic"
            )
    for key in sorted(
        set(executed) | set(outcomes), key=lambda item: (str(item[0]), str(item[1]))
    ):
        label = _verification_turn_label(key)
        failures = [
            payload
            for payload in executed.get(key, ())
            if _payload_shows_failure(payload)
        ]
        failing_total += len(failures)
        outcome = outcomes.get(key)
        if outcome is None:
            if failures:
                target = (
                    blind
                    if context.events_shed or context.terminal_pending
                    else problems
                )
                target.append(
                    f"{len(failures)} failing check record(s) for {label} never "
                    f"reached a {events.VERIFICATION_FINISHED_EVENT} record"
                )
            continue
        if outcome.get("passed") is False:
            acceptance = outcome.get("acceptance")
            if acceptance is not None:
                failing_total += len(failures)
                accepted_names = list(
                    _sequence(_mapping(acceptance).get("deferred_failure_names"))
                )
                failure_names = [failure.get("name") for failure in failures]
                resolution = _accepted_verification_is_accounted_for(
                    context,
                    outcome,
                    session_id=outcome.get("session_id") or outcome.get("session"),
                )
                # A deferral naming checks that never failed is a defect no
                # amount of unfinished repair loop excuses.
                if accepted_names != failure_names:
                    resolution = "unresolved"
                if resolution == "unresolved":
                    problems.append(
                        f"verification for {label} claims broad-check deferral "
                        "without matching failed checks, a repair cycle, and a "
                        "later all-clear review"
                    )
                elif resolution == "pending":
                    blind.append(
                        f"the broad-check failures deferred by {label} reached no "
                        "all-clear review before the run stopped"
                    )
            continue
        if outcome.get("passed") is not True:
            continue
        diagnostics = outcome.get("diagnostic_count")
        diagnostics = diagnostics if isinstance(diagnostics, int) else 0
        skipped = outcome.get("skipped_count")
        skipped = skipped if isinstance(skipped, int) else 0
        # Only a conditional diagnostic is ever skipped, and only a diagnostic
        # that actually ran can absorb a failure — the declared count on its own
        # lets a skipped diagnostic stand in for a gate that failed.
        absorbable = max(diagnostics - skipped, 0)
        roles = [payload.get("role") for payload in failures]
        gate_failures = sum(1 for role in roles if role == verification.CHECK_ROLE_GATE)
        diagnostic_failures = sum(
            1 for role in roles if role == verification.CHECK_ROLE_DIAGNOSTIC
        )
        unidentified = len(roles) - gate_failures - diagnostic_failures
        if gate_failures:
            problems.append(
                f"verification for {label} passed while {gate_failures} of its "
                "check record(s) reported a failure against a gate"
            )
        elif unidentified > max(absorbable - diagnostic_failures, 0):
            problems.append(
                f"verification for {label} passed while {unidentified} check "
                f"record(s) of unidentified role reported a failure against "
                f"{absorbable} executed diagnostic(s)"
            )
        executed_count = outcome.get("executed_count")
        passed_count = outcome.get("passed_count")
        if isinstance(executed_count, int) and isinstance(passed_count, int):
            unpassed = executed_count - passed_count
            if unpassed > absorbable:
                problems.append(
                    f"verification for {label} passed while its own counts leave "
                    f"{unpassed} of {executed_count} check(s) unpassed against "
                    f"{absorbable} executed diagnostic(s)"
                )

    if problems:
        return _Verdict(
            "fail",
            f"the run's verification outcomes do not account for the checks that "
            f"failed under them: {_joined(problems)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the run's verification records are incomplete, so {_joined(blind)} "
            "cannot be distinguished from evidence the audit never saw",
            evidence,
        )
    return _Verdict(
        "pass",
        f"every one of the {failing_total} failing verification check(s) is "
        "accounted for by the outcome of the recipe that ran it",
        evidence,
    )


def _check_mutation_commit_guards(context: _CheckContext) -> _Verdict:
    commits = context.section("commits")
    registry = context.section("registry")
    scopes = context.section("scopes")
    contradictions: list[str] = []
    references: list[str] = []

    mutation = f"{EVIDENCE_ROOT_RUN}/{MUTATION_ARTIFACT_NAME}"
    if mutation in context.declared:
        references.append(mutation)
        contradictions.append(
            "the audit recorded a workspace mutation under its own read-only profile"
        )

    writable = {
        str(alias)
        for session in _sequence(scopes.get("sessions"))
        for alias in _sequence(_mapping(session).get("writes"))
    }
    attempts = [_mapping(item) for item in _sequence(commits.get("attempts"))]
    # A commit the guard observed is by definition one the runner never ran, so
    # the commit mode it would have run under is absent for the same reason the
    # commit is: nobody asked the runner to make it.
    ran = [
        attempt
        for attempt in attempts
        if attempt.get("origin") != OBSERVED_COMMIT_ORIGIN
    ]
    repair_commit_mode = commits.get("repair_commit_mode")
    if ran and not commits.get("commit_mode") and repair_commit_mode != "per-child":
        contradictions.append(
            f"the run made {len(ran)} commit attempt(s) without recording the "
            "commit mode they ran under"
        )
    for attempt in attempts:
        if isinstance(attempt.get("reference"), str):
            references.append(attempt["reference"])
        result = _mapping(attempt.get("result"))
        if result.get("available") is False:
            contradictions.append(
                f"{attempt.get('name')} wrote no readable commit result"
            )
            continue
        alias = result.get("repository_alias")
        if scopes.get("available") and alias and str(alias) not in writable:
            contradictions.append(
                f"{attempt.get('name')} committed to {alias}, which no session "
                "declares as writable"
            )

    guarded = 0
    head_moves = 0
    history_rewrites = 0
    unguarded: list[str] = []
    for turn in context.turns:
        result = _mapping(turn.get("result"))
        if result.get("available") is False or turn.get("anomalous") is True:
            continue
        # Registered workspaces guard review turns as well. Legacy runs have a
        # separate read-only review guard, but every implementation turn must
        # carry repository evidence even when commit mode is disabled.
        requires_guard = registry.get("registered") or (
            result.get("session_kind") == "implementation"
        )
        if not requires_guard:
            continue
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        guard = _mapping(result.get("workspace_write_guard"))
        if not guard:
            unguarded.append(str(turn.get("name")))
            continue
        guarded += 1
        for raw_changes in _mapping(guard.get("changed")).values():
            changes = _mapping(raw_changes)
            if changes.get("head_changed") is not True:
                continue
            head_moves += 1
            transition = _mapping(changes.get("head_transition"))
            if not transition:
                contradictions.append(
                    f"{turn.get('name')} changed HEAD without recording its "
                    "history transition"
                )
                continue
            relation = transition.get("relation")
            if relation not in {
                "created",
                "deleted",
                "fast-forward",
                "rewind",
                "rewrite",
            }:
                contradictions.append(
                    f"{turn.get('name')} recorded an invalid HEAD transition"
                )
                continue
            if relation in {"deleted", "rewind", "rewrite"}:
                history_rewrites += 1
    if unguarded:
        contradictions.append(
            f"{_names(unguarded)} completed implementation work without a "
            "write-guard result"
        )

    evidence = context.evidence(*references, context.section("state").get("reference"))
    if contradictions:
        return _Verdict(
            "fail",
            f"the run's mutation and commit guards left a gap: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not attempts and not guarded:
        return _Verdict(
            "unavailable",
            "the run recorded neither a commit attempt nor a write-guard result to "
            "judge its mutation guards by",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{guarded} guarded turn(s), {len(attempts)} commit attempt(s), "
        f"{head_moves} worker HEAD move(s), and {history_rewrites} history "
        "rewrite(s) stayed inside the run's declared write scope",
        evidence,
    )


def _commit_record_label(payload: Mapping[str, Any]) -> str:
    """Name a commit record by the artifact it wrote, else by its index."""
    for key in ("artifact_dir", "result_path"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    index = payload.get("commit_index")
    return f"commit {index}" if index is not None else "an unnamed commit"


def _commit_shows_failure(payload: Mapping[str, Any]) -> bool:
    """Whether a commit record is a failure in the runner's own vocabulary.

    Most commit failures exit zero: ``unchanged-head``, ``missing-head``,
    ``dirty-after-commit`` and ``status-unavailable`` are all reached on a clean
    exit, and the runner reports them as ``success=False`` with the outcome
    token. The generic anomaly predicate reads neither field, so selecting on it
    alone skips every commit failure but the one that exited nonzero. Any
    ``status_result`` other than ``success`` counts, so an outcome ASL never
    writes is graded rather than silently read as a clean commit.
    """
    if payload.get("success") is False:
        return True
    status_result = payload.get("status_result")
    if isinstance(status_result, str) and status_result and status_result != "success":
        return True
    return _payload_shows_failure(payload)


def _commit_failure_classification(payload: Mapping[str, Any]) -> str | None:
    """The token the run gave a failed commit, explicit key before outcome."""
    for key in ("failure_classification", "status_result"):
        value = payload.get(key)
        if isinstance(value, str) and value and value != "success":
            return value
    return None


def _commit_failure_reason(payload: Mapping[str, Any]) -> str | None:
    """Where the run said why a commit failed.

    The bundle carries no command output by design, so a reason is either named
    in the record itself or written to a stream the record points at and sizes.
    A failure with neither left nothing for an auditor to read.
    """
    for key in ("failure_reason", "exec_error", "status_error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for path_key, size_key in (
        ("stderr_path", "stderr_chars"),
        ("stdout_path", "stdout_chars"),
    ):
        path = payload.get(path_key)
        size = payload.get(size_key)
        if isinstance(path, str) and path and isinstance(size, int) and size > 0:
            return path
    return None


def _check_commit_failure_accounting(context: _CheckContext) -> _Verdict:
    """Grade the run's own account of every commit it recorded as failed.

    A failed commit is recorded once, in ``commit_finished``, and that record
    already carries the outcome that classified the failure and the streams the
    failure was written to. Reading them here is what lets the unknown-anomalies
    catch-all stop reporting a failed commit: this check grades every record that
    catch-all would have selected and the zero-exit failures it would have
    missed, so a record that names no classification, contradicts its own exit
    code, or points at no reason still surfaces by name.
    """
    problems: list[str] = []
    references: list[str] = []
    recorded = 0
    failures = 0
    for record in context.records:
        if record.get("name") != events.COMMIT_FINISHED_EVENT:
            continue
        recorded += 1
        payload = _mapping(record.get("payload"))
        if not _commit_shows_failure(payload):
            continue
        failures += 1
        label = _commit_record_label(payload)
        references.append(str(payload.get("artifact_dir")))
        status_result = payload.get("status_result")
        returncode = payload.get("returncode")
        if _commit_failure_classification(payload) is None:
            problems.append(f"{label} failed without classifying the failure")
        elif isinstance(status_result, str) and status_result not in _COMMIT_RESULTS:
            problems.append(
                f"{label} recorded {status_result}, which is no commit outcome ASL "
                "writes"
            )
        elif (
            isinstance(status_result, str)
            and isinstance(returncode, int)
            and (returncode != 0) != (status_result == "nonzero-exit")
        ):
            problems.append(
                f"{label} recorded {status_result} against exit {returncode}"
            )
        if payload.get("success") is True:
            problems.append(f"{label} claimed success while recording a failure")
        if _commit_failure_reason(payload) is None:
            problems.append(f"{label} failed without recording why")

    evidence = context.evidence(
        *references,
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if problems:
        return _Verdict(
            "fail",
            f"the run left a commit failure unaccounted for: {_joined(problems)}",
            evidence,
        )
    if context.events_shed:
        return _Verdict(
            "unavailable",
            "the event log was shed under the size cap, so the run's commit "
            "results cannot be read in full",
            evidence,
        )
    if not context.records:
        return _Verdict(
            "unavailable",
            "the run recorded no events, so it recorded no commit outcome to grade",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{failures} of {recorded} recorded commit(s) failed, each naming its "
        "classification and where its reason was written",
        evidence,
    )


def _unresolved_commit_group_rows(context: _CheckContext) -> list[dict[str, Any]]:
    """The commit groups the run ended still owing, by its own account.

    ``unresolved_commit_groups`` is the field the runner writes as it closes,
    so its presence is the run stating the answer directly. A bundle without it
    predates that field, and is read from the durable group rows instead —
    restricted to groups the coordinator marked ``failed``, because a
    ``pending`` row in an older bundle is as likely to be a group still in
    flight as one that was lost.
    """
    section = context.section("commits")
    if "unresolved_commit_groups" in section:
        return [_mapping(row) for row in _sequence(section["unresolved_commit_groups"])]
    return [
        _mapping(row)
        for row in _sequence(section.get("commit_groups"))
        if _mapping(row).get("status") == commit_groups.GROUP_STATUS_FAILED
    ]


def _unresolved_group_label(row: Mapping[str, Any]) -> str:
    session_id = row.get("session_id")
    scope = "the end-of-run commit" if session_id is None else f"session {session_id}"
    alias = row.get("failed_alias")
    return f"{scope} in {alias}" if alias else scope


def _check_commit_group_resolution(context: _CheckContext) -> _Verdict:
    """Grade whether the run committed everything it set out to commit.

    A failed commit no longer pauses the run: the group is marked failed,
    retried at the next commit boundary, and the run carries on. That is only
    safe while a group still owed when the run ends is impossible to miss, so
    this check is where the loss is named. Without it the one failure mode
    soft-fail introduces — a run that finishes reporting success while a
    session's changes were never committed — would be graded by nothing.
    """
    unresolved = _unresolved_commit_group_rows(context)
    references = [
        str(attempt.get("reference"))
        for attempt in _sequence(context.section("commits").get("attempts"))
        if isinstance(attempt, Mapping)
    ]
    evidence = context.evidence(
        context.section("state").get("reference"),
        *references,
    )
    if unresolved:
        return _Verdict(
            "fail",
            "the run ended owing commits it never made: "
            f"{_joined([_unresolved_group_label(row) for row in unresolved])}",
            evidence,
        )
    if not context.section("state"):
        return _Verdict(
            "unavailable",
            "the run's state projection could not be read, so nothing says "
            "whether the commits it owed were made",
            evidence,
        )
    return _Verdict(
        "pass",
        "every commit group the run opened reached completion",
        evidence,
    )


def _paused_work_settlement_label(payload: Mapping[str, Any]) -> str:
    session_id = payload.get("session_id")
    return (
        "the paused-work settlement"
        if session_id is None
        else f"session {session_id}'s paused-work settlement"
    )


def _paused_work_settlement_reason(payload: Mapping[str, Any]) -> str | None:
    """Where a failed settlement wrote why it failed, if it wrote it anywhere.

    The two places are the pair ``run_reporting`` already reads when it prints
    the failure to an operator: the exception the settlement caught, and the
    outcome the underlying commit classified itself with.
    """
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error
    status_result = _mapping(payload.get("commit")).get("status_result")
    if isinstance(status_result, str) and status_result.strip():
        return status_result
    return None


def _check_paused_work_settlement(context: _CheckContext) -> _Verdict:
    """Grade the one commit a run makes that no accepted turn produced.

    A recoverable pause leaves the failed turn's changes in the worktree, and
    the settlement is the run's single chance to commit them before the process
    exits. Nothing downstream re-derives what it did: the next run meets either
    a clean tree or unexplained dirt at its startup commit gate, and only this
    record says which was intended.

    So the record is graded on its own terms rather than trusted. It must name
    an outcome the settlement actually writes; a failure must say where its
    reason was written and must leave the group among the ones the run ended
    owing, which ``run_loop`` guarantees by ordering the settlement ahead of the
    terminal record; and a success must agree with itself about residue, since
    ``committed`` and ``committed-with-residue`` differ by nothing else. The
    record must also be mirrored into state, because a record in the log with no
    mirror is a run that died between the append and the write, and every reader
    but this one reads the mirror.

    Grading it here is what earns ``paused_work_committed`` its place in
    :data:`_CLASSIFIED_EVENT_NAMES`. The catch-all reported every settlement as
    an unclassified failure, because the record carries the pause's reason; the
    exemption is only sound while this check owns the record instead.
    """
    problems: list[str] = []
    settlements = [
        _mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.PAUSED_WORK_COMMITTED_EVENT
    ]
    commits = context.section("commits")
    mirror = _mapping(commits.get(PAUSED_WORK_COMMIT_KEY))
    unresolved_scopes = {
        row.get("scope") for row in _unresolved_commit_group_rows(context)
    }
    for payload in settlements:
        label = _paused_work_settlement_label(payload)
        result = payload.get("result")
        if not isinstance(result, str) or result not in _PAUSED_WORK_RESULTS:
            problems.append(
                f"{label} recorded {result!r}, which is no settlement outcome ASL "
                "writes"
            )
        elif result == _PAUSED_WORK_RESULT_FAILED:
            if _paused_work_settlement_reason(payload) is None:
                problems.append(f"{label} failed without recording why")
            if commit_groups.GROUP_SCOPE_SESSION_PAUSED not in unresolved_scopes:
                problems.append(
                    f"{label} failed, but the run ended owing no paused-work "
                    "commit group for a later run to recover"
                )
        else:
            residue = _sequence(_mapping(payload.get("residue")).get("repositories"))
            if result == _PAUSED_WORK_RESULT_RESIDUE and not residue:
                problems.append(
                    f"{label} reported residue while naming no repository it was "
                    "left in"
                )
            elif result == _PAUSED_WORK_RESULT_COMMITTED and residue:
                problems.append(
                    f"{label} reported a clean commit while naming "
                    f"{len(residue)} repository(ies) it left dirty"
                )
        if not mirror:
            problems.append(f"{label} was never mirrored into the run's state")
        elif mirror.get("result") != result:
            problems.append(
                f"{label} recorded {result!r} while state mirrors "
                f"{mirror.get('result')!r}"
            )

    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if problems:
        return _Verdict(
            "fail",
            f"the run's settlement of its paused work does not account for "
            f"itself: {_joined(problems)}",
            evidence,
        )
    if not settlements:
        if context.events_shed:
            return _Verdict(
                "unavailable",
                "the event log was shed under the size cap, so a settlement the "
                "run made cannot be distinguished from one it never made",
                evidence,
            )
        return _Verdict(
            "pass",
            "the run left no paused work behind, so it settled none",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(settlements)} paused-work settlement(s) recorded an outcome ASL "
        "writes, agreed with the run's state, and accounted for what they left "
        "in the worktree",
        evidence,
    )


def _check_resource_anomalies(context: _CheckContext) -> _Verdict:
    anomalies: list[str] = []
    references: list[str] = []
    suspected = 0
    cleared = 0
    automatically_recovered = 0
    for record in context.records:
        name = record.get("name")
        payload = _mapping(record.get("payload"))
        if name == events.TURN_HANG_SUSPECTED_EVENT:
            suspected += 1
        elif name == events.TURN_HANG_CLEARED_EVENT:
            cleared += 1
            block = _mapping(payload.get("hang_suspected"))
            recovery = _mapping(block.get("recovery"))
            if _sequence(recovery.get("signaled_pids")):
                automatically_recovered += 1
        elif name == events.PROCESS_SWEEP_EVENT:
            if _sequence(payload.get("kill_failed")):
                anomalies.append(
                    "a post-turn process sweep failed to kill a process it owned"
                )
            if _sequence(payload.get("survivors_after")):
                anomalies.append("processes survived a post-turn sweep")
    if suspected > cleared:
        anomalies.append(f"{suspected - cleared} suspected hang(s) were never cleared")
    for turn in context.turns:
        result = _mapping(turn.get("result"))
        stalled = [
            label
            for key, label in (
                ("timed_out", "timed out"),
                ("interrupted", "was interrupted"),
            )
            if result.get(key) is True
        ]
        for label in stalled:
            anomalies.append(f"{turn.get('name')} {label}")
        if stalled and isinstance(turn.get("directory"), str):
            references.append(turn["directory"])

    evidence = context.evidence(
        *references,
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if anomalies:
        return _Verdict(
            "fail",
            f"the run stalled or leaked processes: {_joined(anomalies)}",
            evidence,
        )
    if context.events_shed:
        return _Verdict(
            "unavailable",
            "the event log was shed under the size cap, so the run's process "
            "sweeps cannot be read in full",
            evidence,
        )
    if not context.records and not context.turns:
        return _Verdict(
            "unavailable",
            "the run recorded neither events nor turns to judge stalls and timeouts by",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(context.turns)} turn(s) ran without a timeout, an interrupt, an "
        "uncleared hang, or a surviving process; "
        f"{automatically_recovered} post-result hang(s) recovered automatically",
        evidence,
    )


def _check_unclassified_anomalies(context: _CheckContext) -> _Verdict:
    summary = context.event_summary
    evidence = context.evidence(
        summary.get("reference"), context.section("state").get("reference")
    )
    unread: list[str] = []
    unparsable = summary.get(
        UNPARSABLE_LINE_COUNT_KEY, summary.get(LEGACY_UNPARSABLE_LINE_COUNT_KEY)
    )
    if isinstance(unparsable, int) and unparsable > 0:
        unread.append(f"{unparsable} event log line(s) would not parse")
    for omission in context.omissions:
        if omission.get("reason") in (REASON_UNREADABLE, REASON_MALFORMED):
            unread.append(
                f"{omission.get('count')} {omission.get('section')} artifact(s) were "
                f"{omission.get('reason')}"
            )
    unclassified: dict[str, int] = {}
    classified = _classified_failure_sessions(context)
    aborted = _aborted_turns(context)
    for record in context.records:
        name = str(record.get("name") or "")
        if name in _CLASSIFIED_EVENT_NAMES:
            continue
        payload = _mapping(record.get("payload"))
        # Narrowed to the session the pause names, because a run reports the
        # outcome of every session it ran and only one of them is the failure
        # the pause classified.
        if name in _PAUSED_TURN_EVENT_NAMES and payload.get("session") in classified:
            continue
        if (
            name in _PAUSED_TURN_EVENT_NAMES
            and (
                payload.get("session"),
                payload.get("turn_index"),
            )
            in aborted
        ):
            continue
        if _payload_shows_failure(payload):
            unclassified[name] = unclassified.get(name, 0) + 1
    for name in sorted(unclassified):
        unread.append(
            f"{unclassified[name]} {name} record(s) report a failure no "
            "deterministic check classifies"
        )

    if unread:
        return _Verdict(
            "fail",
            f"the run holds evidence no deterministic check accounts for: "
            f"{_joined(unread)}",
            evidence,
        )
    if not context.records and not context.omissions:
        return _Verdict(
            "unavailable",
            "the run recorded no events and no omissions, so there is nothing to "
            "classify",
            evidence,
        )
    return _Verdict(
        "pass",
        "every recorded failure is accounted for by a named deterministic check",
        evidence,
    )


_CHECKS: tuple[tuple[CheckSpec, Callable[[_CheckContext], _Verdict]], ...] = (
    (
        CheckSpec(
            check="root-registry-agreement",
            lane="identity-authority",
            category="identity",
            component="workspace-registry",
            invariant_key="identity.root-registry-agreement",
            severity="critical",
            recommendation=(
                "Re-bind the plan to the workspace it actually ran against, or "
                "re-register the repositories under the recorded root, before "
                "trusting any scope decision this run made."
            ),
        ),
        _check_root_registry_agreement,
    ),
    (
        CheckSpec(
            check="repository-intent",
            lane="identity-authority",
            category="identity",
            component="repository-intent",
            invariant_key="identity.repository-intent",
            severity="critical",
            recommendation=(
                "Declare a `## Repositories` table in manifest.md naming every "
                "repository the plan reaches, then re-run preflight."
            ),
            # The only check that replays the gate rather than reading its
            # recorded verdict: it walks the plan directory as it stands now.
            # A plan edited, published or deleted after the run answers for the
            # tree the replay is standing in, not for the run being judged.
            replayable=False,
        ),
        _check_repository_intent,
    ),
    (
        CheckSpec(
            check="task-packet-paths",
            lane="task-packet-coherence",
            category="packet",
            component="turn-artifacts",
            invariant_key="packet.artifact-path-agreement",
            severity="high",
            recommendation=(
                "Reconcile the paths written into the turn result with the "
                "directory the turn actually ran from."
            ),
        ),
        _check_task_packet_paths,
    ),
    (
        CheckSpec(
            check="scope-escape",
            lane="task-packet-coherence",
            category="packet",
            component="workspace-write-guard",
            invariant_key="packet.write-scope-containment",
            severity="critical",
            recommendation=(
                "Widen the session's declared writes or revert the out-of-scope "
                "change; do not resume until the guard passes on its own."
            ),
        ),
        _check_scope_escape,
    ),
    # Split from containment rather than expressed as a severity on the verdict:
    # severity lives on the spec, so one issue key cannot oscillate between
    # critical and medium across occurrences and corrupt the inbox's dedup
    # history for the escape that matters.
    (
        CheckSpec(
            check="scope-guard-catch",
            lane="task-packet-coherence",
            category="packet",
            component="workspace-write-guard-catch",
            invariant_key="packet.write-scope-guard-catch",
            severity="medium",
            recommendation=(
                "Widen the child's durable write scope to the aliases its own "
                "findings name, rerun expansion and preflight, then resume."
            ),
        ),
        _check_scope_guard_catch,
    ),
    (
        CheckSpec(
            check="retry-resume",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="pause-policy",
            invariant_key="lifecycle.retry-and-resume-consistency",
            severity="medium",
            recommendation=(
                "Persist the classification `pause_policy.classify_pause` returns "
                "for the recorded reason, and keep relaunches inside the budget "
                "the retry records name."
            ),
        ),
        _check_retry_resume,
    ),
    (
        CheckSpec(
            check="retryable-transport-failure",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="harness-failure-classifier",
            invariant_key="lifecycle.retryable-transport-failure",
            severity="high",
            recommendation=(
                "Keep the harness-failure classification and the pause policy "
                "telling the same story: a failure marked retryable must pause "
                "under a recoverable policy that needs no user and grants at "
                "least one relaunch, since the supervisor performs the retry "
                "rather than the run itself."
            ),
        ),
        _check_retryable_transport_failure,
    ),
    (
        CheckSpec(
            check="operator-abort",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="run-control",
            invariant_key="lifecycle.operator-abort",
            severity="high",
            recommendation=(
                "Record the request and the reason behind every turn the run "
                "kills, so a killed turn traces to the operator who asked for "
                "it rather than reading as an unexplained failure."
            ),
        ),
        _check_operator_abort,
    ),
    (
        CheckSpec(
            check="review-independence",
            lane="verification-review-independence",
            category="review",
            component="review-harness",
            invariant_key="review.independent-verification",
            severity="high",
            recommendation=(
                "Run the review under a harness other than the one that did the "
                "work, and record a verdict only once the review completes."
            ),
        ),
        _check_review_independence,
    ),
    (
        CheckSpec(
            check="verification-outcomes",
            lane="verification-review-independence",
            category="verification",
            component="verification-recipe",
            invariant_key="verification.failure-accounting",
            severity="high",
            recommendation=(
                "Reconcile the recipe's outcome with the checks that ran under "
                "it: a verification may only pass when every failure it recorded "
                "belongs to a conditional diagnostic."
            ),
        ),
        _check_verification_outcomes,
    ),
    (
        CheckSpec(
            check="mutation-commit-guards",
            lane="mutation-commit-guards",
            category="guards",
            component="mutation-and-commit",
            invariant_key="guards.mutation-and-commit",
            severity="critical",
            recommendation=(
                "Capture and record repository fingerprints around every "
                "implementation turn, including legacy and commit-mode-none runs; "
                "classify worker HEAD moves and commit only inside established "
                "repository write scope."
            ),
        ),
        _check_mutation_commit_guards,
    ),
    (
        CheckSpec(
            check="commit-failure-accounting",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-failure",
            invariant_key="guards.commit-failure-accounting",
            severity="medium",
            recommendation=(
                "Record the outcome that classified the failed commit and the "
                "stream its reason was written to, so a failed commit is read "
                "from its own record instead of landing in the catch-all."
            ),
        ),
        _check_commit_failure_accounting,
    ),
    # Split from failure accounting rather than folded into it: that check asks
    # whether a failed commit explained itself, this one asks whether the
    # changes ever landed. A soft-failed commit can pass the first and fail
    # this one, and the inbox must be able to tell those two defects apart.
    (
        CheckSpec(
            check="commit-group-resolution",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-group-resolution",
            invariant_key="guards.commit-group-resolution",
            severity="high",
            recommendation=(
                "Commit the changes the named group still owes, or rerun the "
                "plan so the recovery path completes that group, before "
                "treating the run's changes as landed."
            ),
        ),
        _check_commit_group_resolution,
    ),
    # The settlement is graded apart from both because it is the one commit no
    # accepted turn produced: it commits work a failed turn retained, under a
    # pause, after the run's terminal record. Whether it explained itself and
    # whether it landed are the same question here, and neither of the checks
    # above reads the record that answers it.
    (
        CheckSpec(
            check="paused-work-settlement",
            lane="mutation-commit-guards",
            category="guards",
            component="paused-work-settlement",
            invariant_key="guards.paused-work-settlement",
            severity="high",
            recommendation=(
                "Read the run's paused_work_commit record before relaunching: "
                "commit or discard what the pause left in the worktree, so the "
                "next run's startup commit gate does not meet it as unexplained "
                "dirt."
            ),
        ),
        _check_paused_work_settlement,
    ),
    (
        CheckSpec(
            check="state-event-agreement",
            lane="truthfulness-observability",
            category="observability",
            component="state-and-events",
            invariant_key="observability.state-event-agreement",
            severity="high",
            recommendation=(
                "Write the terminal event and the state projection under one lock "
                "so an operator reading either sees the same outcome."
            ),
        ),
        _check_state_event_agreement,
    ),
    (
        CheckSpec(
            check="terminal-artifacts",
            lane="truthfulness-observability",
            category="observability",
            component="terminal-artifacts",
            invariant_key="observability.terminal-artifacts",
            severity="medium",
            recommendation=(
                "Ensure every turn writes its result file and that the run opens "
                "on run_started and closes on exactly one terminal record."
            ),
        ),
        _check_terminal_artifacts,
    ),
    (
        CheckSpec(
            check="misleading-success",
            lane="truthfulness-observability",
            category="observability",
            component="success-claim",
            invariant_key="observability.success-is-earned",
            severity="high",
            recommendation=(
                "Withhold the success claim until the anomalies, failed checks, "
                "and skipped checks it contradicts are resolved or explained."
            ),
        ),
        _check_misleading_success,
    ),
    (
        CheckSpec(
            check="resource-anomalies",
            lane="resources-stalls",
            category="resources",
            component="process-lifecycle",
            invariant_key="resources.stall-and-timeout",
            severity="medium",
            recommendation=(
                "Trace the stalled turn or surviving process to the descendant "
                "that outlived its turn, and close the handle that kept it alive."
            ),
        ),
        _check_resource_anomalies,
    ),
    (
        CheckSpec(
            check="unclassified-anomalies",
            lane="unknown-anomalies",
            category="anomalies",
            component="unclassified-evidence",
            invariant_key="anomalies.unclassified-evidence",
            severity="low",
            recommendation=(
                "Read the unclassified records directly, then either repair the "
                "damaged evidence or add a deterministic check that owns them."
            ),
        ),
        _check_unclassified_anomalies,
    ),
)

#: Every deterministic check, in the order results are reported.
DETERMINISTIC_CHECK_SPECS: tuple[CheckSpec, ...] = tuple(spec for spec, _ in _CHECKS)


def run_deterministic_checks(
    bundle: AuditInput | Mapping[str, Any],
) -> tuple[assurance_models.DeterministicResult, ...]:
    """Judge one evidence bundle against every reproducible check.

    Each check answers ``pass``, ``fail``, or ``unavailable`` under a stable
    invariant key and names the evidence it read. Evidence a check needed but
    could not see yields ``unavailable``: an audit that cannot look is never
    the same thing as an audit that looked and found nothing.
    """
    payload = bundle.payload if isinstance(bundle, AuditInput) else bundle
    context = _CheckContext(payload)
    return tuple(
        assurance_models.DeterministicResult(
            check=spec.check,
            lane=spec.lane,
            status=verdict.status,
            invariant_key=spec.invariant_key,
            evidence=verdict.evidence,
            detail=_detail(verdict.detail),
        )
        for spec, verdict in ((spec, run(context)) for spec, run in _CHECKS)
    )


def findings_from_results(
    results: Iterable[assurance_models.DeterministicResult],
) -> tuple[Finding, ...]:
    """Raise each failing check into a finding under the check's fingerprint."""
    specs = {spec.check: spec for spec in DETERMINISTIC_CHECK_SPECS}
    findings = []
    for result in results:
        spec = specs.get(result.check)
        if result.status != "fail" or spec is None:
            continue
        findings.append(
            Finding(
                category=spec.category,
                component=spec.component,
                invariant_key=spec.invariant_key,
                lane=spec.lane,
                severity=spec.severity,
                summary=result.detail or f"the {spec.check} check failed",
                recommendation=spec.recommendation,
                evidence=result.evidence,
            )
        )
    return tuple(findings)


def lane_rollup(
    results: Iterable[assurance_models.DeterministicResult],
) -> dict[str, str]:
    """Reduce results to one status per rubric lane, keeping the worst.

    A lane no check reported on rolls up as ``unavailable`` rather than as
    ``pass``, so a gap in coverage reads as a gap.
    """
    worst: dict[str, str] = {}
    for result in results:
        current = worst.get(result.lane)
        if (
            current is None
            or _STATUS_PRECEDENCE[result.status] > _STATUS_PRECEDENCE[current]
        ):
            worst[result.lane] = result.status
    return {
        lane: worst.get(lane, "unavailable") for lane in assurance_models.RUBRIC_LANES
    }


# ---------------------------------------------------------------------------
# Independent agent audit
# ---------------------------------------------------------------------------

#: Version of the JSON contract the auditor answers in. v2 requires exactly one
#: identity decision per finding; a v1 answer carried none, so it is rejected
#: rather than read as an implicit request to mint a new issue.
AGENT_OUTPUT_SCHEMA_VERSION = 2

#: Largest identity catalog handed to an auditor. The inbox grows without bound
#: while a prompt does not, so the catalog is a bounded sample of a complete
#: index and says how much it dropped rather than pretending to be whole.
MAX_AGENT_CATALOG_BYTES = 64 * 1024

#: Longest summary carried per catalog entry. An auditor needs enough of a
#: summary to recognize its own defect in it, not the whole record.
MAX_CATALOG_SUMMARY_CHARS = 240

#: The only two identity statements a finding may make.
IDENTITY_DECISIONS: tuple[str, ...] = ("existing", "new")

#: Largest agent message parsed. An auditor that answers with more than this is
#: not answering the contract, and parsing an unbounded reply is how untrusted
#: output starts costing memory instead of being rejected.
MAX_AGENT_OUTPUT_BYTES = 256 * 1024

#: The only roots a localization may name. They are exactly the read-only
#: source trees the audit profile grants, so a localization pointing anywhere
#: else describes something the auditor could not have read.
LOCALIZATION_ROOTS: tuple[str, ...] = (EVIDENCE_ROOT_PACKAGE, EVIDENCE_ROOT_SKILLS)

#: Retryable outcomes where the request never reached the agent. They are
#: charged to the delivery budget and retried with the same prompt after a
#: backoff, because there is no answer to correct.
RETRYABLE_DELIVERY_OUTCOMES: frozenset[str] = frozenset(
    {
        "timeout",
        "transport-error",
    }
)

#: Retryable outcomes where the agent answered and the answer was unusable.
#: They are charged to the answer budget and retried with a correction.
RETRYABLE_ANSWER_OUTCOMES: frozenset[str] = frozenset(
    {
        "malformed-output",
        "missing-lanes",
        "invalid-reference",
    }
)

#: Attempt outcomes that earn a retry at all. A detected mutation, an
#: unenforceable profile, and an interrupt are absent on purpose: none is a
#: transient failure, and retrying would either run a second turn under a
#: boundary already known to be broken or answer the operator's Ctrl+C with more
#: of what they interrupted.
RETRYABLE_ATTEMPT_OUTCOMES: frozenset[str] = (
    RETRYABLE_DELIVERY_OUTCOMES | RETRYABLE_ANSWER_OUTCOMES
)

#: Why a launched audit reports no agent coverage.
REASON_AGENT_UNAVAILABLE = "agent-coverage-unavailable"

#: Why an audit with agent coverage and no findings was once recorded as
#: unavailable. No new audit is written under it; it stays so the records that
#: already carry it keep parsing.
REASON_LANE_COVERAGE = "incomplete-lane-coverage"

#: Why an audit that ran, found nothing, and could not judge every lane is
#: ``partial``: some lane had neither a reproducible verdict nor an agent claim.
REASON_LANE_EVIDENCE_ABSENT = "lane-evidence-absent"

#: Why an audit whose every attempt was well-formed is still not usable: a
#: finding asked ASL to allocate an issue for a defect the catalog already names
#: a candidate for, and said so again after being shown the candidates. ASL will
#: not merge on a semantic match and will not allocate past one, so the audit is
#: unavailable under a reason class that groups every such run into one issue.
REASON_IDENTITY_AMBIGUOUS = "identity-ambiguous"

#: The attempt outcome an identity-ambiguous answer is recorded under.
#: :data:`assurance_models.ATTEMPT_OUTCOMES` is a closed vocabulary persisted
#: inside audit reports, so this reuses the token for an identity that cannot be
#: resolved against the catalog rather than widening the wire contract; the
#: audit-level reason carries the distinction, and the detail names the issues.
IDENTITY_AMBIGUOUS_OUTCOME: AttemptOutcome = "invalid-reference"

#: Most candidates one retry prompt carries, across every finding that asked for
#: a new allocation. The listing is widened to make each one selectable, so this
#: also bounds how far a retry's catalog may exceed
#: :data:`MAX_AGENT_CATALOG_BYTES`.
MAX_PROMPTED_CANDIDATES = 8

#: Files written under each ``supervisor-audit-attempt-N/`` directory. The
#: harness streams appear only for an attempt whose launch failed, where they
#: are the only record of what the provider actually said.
ATTEMPT_PROMPT_FILE = "prompt.txt"
ATTEMPT_MESSAGE_FILE = "last-message.txt"
ATTEMPT_OUTCOME_FILE = "outcome.json"
ATTEMPT_STDOUT_FILE = "harness-stdout.txt"
ATTEMPT_STDERR_FILE = "harness-stderr.txt"

#: Per-stream cap on a captured harness tail. Large enough for a stack trace or
#: a JSON error envelope, small enough that a runaway log cannot fill the run
#: directory.
MAX_HARNESS_TAIL_BYTES = 16 * 1024

_JSON_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.DOTALL)

#: Keys the agent may set on a finding. ``fingerprint``, ``issue_id``, and
#: ``schema_version`` are absent because ASL computes them; an agent that sends
#: one is claiming an authority it does not have. ``identity`` is the one place
#: an issue may be named, and only by selecting one ASL already supplied.
_AGENT_FINDING_KEYS: frozenset[str] = frozenset(
    {
        "category",
        "component",
        "identity",
        "invariant_key",
        "lane",
        "severity",
        "summary",
        "recommendation",
        "evidence",
        "localization",
    }
)

_IDENTITY_KEYS: frozenset[str] = frozenset({"decision", "issue_id"})

_AGENT_IDENTITY_KEYS: frozenset[str] = frozenset(
    {"fingerprint", "id", "issue", "issue_id", "issue_ids"}
)

AUDIT_PROMPT_TEMPLATE = """\
You are auditing the control plane of one finished agent-session-loop (ASL) run.
You are not reviewing the application the run worked on.

Order of work, which is not optional:

1. Analyze the operational evidence below first, on its own terms. Every claim
   you make must rest on a record in that bundle: identities, paths, digests,
   exit codes, timings, guard results, events, and status projections.
2. Only after you have identified a specific anomaly in that evidence may you
   read ASL source to localize it. That reading is read-only and is limited to
   the `{package_root}` and `{skills_root}` locator roots.

Prohibited, without exception:

- Reviewing, grading, or commenting on the application implementation the run
  produced: its code quality, design, tests, or diffs are out of scope.
- Editing, creating, or deleting any file. You have read access only.
- Assigning issue IDs, fingerprints, or any identifier of your own invention.
  ASL computes fingerprints and allocates every issue ID. The only identity you
  ever state is a selection from the catalog ASL supplies below.

Judge these eight rubric lanes, all of them, every time:

{lane_list}

Answer with exactly one JSON object, in a single fenced ```json block, and
nothing that must be parsed outside it:

{schema}

Rules the answer must satisfy:

- `lanes` holds exactly one record per rubric lane named above. A lane you
  could not judge is `unavailable` and still names the evidence that proves it
  could not be judged; it is never `pass`.
- Every `evidence` entry is a locator copied verbatim from the `evidence` index
  of the bundle below. Invented, absolute, or `..`-bearing locators are
  rejected.
- `category`, `component`, and `invariant_key` are lowercase tokens of
  alphanumerics separated by `.`, `_`, `-`, or `/`. Reuse an existing
  `invariant_key` when you are reporting the same invariant a deterministic
  check already names.
- `severity` is one of low, medium, high, critical.
- `localization` is optional, appears only on a finding, and holds source
  pointers under `{package_root}/` or `{skills_root}/` such as
  `{package_root}/runner.py:8129`. Omit it when you did not localize anything.
- Report a finding only for a control-plane defect. An audit with no defects
  returns an empty `findings` list and passing lanes.
- A run that refused or paused before launching an unauthorized turn is the
  repository-intent control working, not a repository-intent defect. Report a
  defect only when evidence shows unauthorized work launched, the gate was
  bypassable, or the recorded authority disagreed with the work performed.
- A registry-free legacy plan confined to the Git repository containing its
  bound execution root is allowed by ASL's compatibility contract. Do not infer
  missing authority merely from the absence of registry and scope projections.
- In `state`, `requested_model`/`requested_model_source` (and their compatibility
  aliases `model`/`model_source`) describe the requested default. Per-role
  `model_projection` is authoritative for the effective model. An unresolved
  requested default beside a resolved effective role model is not a
  contradiction.
- A verification with `passed: false` is never a pass. It may carry an explicit
  `findings-defer-broad-failures` acceptance only when every deferred check is a
  `broad:*` replay under a valid findings verdict. Treat that deferral as
  accounted for only if the recorded review cycles then materialize repairs and
  a later review reaches all-clear; otherwise report the contradiction.
- A `failure_diagnostic` of `null` on a check ASL did not generate is the
  content boundary working, not a lost diagnostic. Only a gate the runner
  rendered itself may promote its last stderr line into control-plane text, so
  a `ci` command — or any other command authored in a plan's
  `## Completion Contract` — reports `null` by design, however much output it
  retained. The rejection survives in the retained stderr artifact, which the
  bundle indexes for every failed check. Report a defect here only if a
  generated gate lost its diagnostic, or if that stderr artifact is itself
  missing from the evidence index.

Every finding carries exactly one `identity` object, and there is no default:

- `{{"decision": "existing", "issue_id": "ASI-N"}}` says this defect is the one
  the catalog below already names under `ASI-N`. Copy that ID from the catalog
  verbatim; an ID the catalog does not list is rejected, and so is an ID
  anywhere on a finding other than inside its own `identity` object.
- `{{"decision": "new"}}` says the catalog names nothing that is this defect,
  and asks ASL to allocate an issue for it. It names no ID, because you do not
  have one to name.
- `omitted` counts the issues the size bound kept out of the listing below. It
  is not zero when the catalog is a sample: an ID you remember but cannot find
  listed is still not selectable, so answer `new` rather than naming it.

The catalog is ASL's canonical inbox, every lifecycle state included, so a
defect that recurs after being resolved, dismissed, or accepted still selects
the issue that already owns it:

```json
{catalog}
```

Operational evidence for run {run_id}:

```json
{bundle}
```
"""

AUDIT_CORRECTION_TEMPLATE = """\
Your previous answer was rejected by ASL's validator:

{detail}

This is a fresh invocation and your previous answer is gone. Redo the audit
from the evidence above and return only a corrected JSON object in a single
fenced ```json block.

If that rejection is about identity, the catalog above is the entire set of
issues you may select from, and it is the same catalog you were given before.
An ID it does not list cannot be selected no matter where you saw it: answer
`{{"decision": "new"}}` for that finding instead.
"""

AUDIT_CANDIDATE_TEMPLATE = """\
Your previous answer was well-formed and no validation rule rejected it. It
asked ASL to allocate a new issue for at least one defect, and ASL's catalog
already names issues that may be that same defect under another wording:

```json
{candidates}
```

`reasons` says what ASL matched on. `{invariant}` is an identical invariant key
under a different category or component, which is the rename a fingerprint
cannot see. `{localization}` is the same source file plus an overlapping
invariant-key segment. `{summary}` is normalized-summary
overlap of at least {threshold}, reported in `similarity`; that field is 0 when
the summaries are not what matched.

Every issue listed there is in the catalog above and is selectable, including
any the size bound had left out. This is a fresh invocation and your previous
answer is gone, so redo the audit from the evidence and decide each defect's
identity again. Select the issue that already owns a defect with
`{{"decision": "existing", "issue_id": "ASI-N"}}`, or answer
`{{"decision": "new"}}` if none of them is it.

ASL never merges a defect into a candidate on its own, and it will not allocate
past one either: a defect you report as new a second time leaves the audit
recorded as identity-ambiguous with no issue opened for it. Answer `new` when
that is what the evidence shows, not to move past this list.
"""


class AgentOutputError(Exception):
    """Raised when agent output fails validation, carrying the retry outcome.

    The outcome is the vocabulary the attempt record and the retry decision both
    read, so ``malformed-output``, ``missing-lanes``, and ``invalid-reference``
    stay distinguishable instead of collapsing into one opaque parse failure.
    """

    def __init__(self, outcome: AttemptOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


#: Who decided an identity. ``agent`` is a selection from the supplied catalog;
#: ``recurrence`` is ASL overruling a new-allocation request because the
#: finding's fingerprint already names an issue, which is a fact rather than a
#: judgement and therefore needs nobody's agreement.
IDENTITY_SOURCES: tuple[str, ...] = ("agent", "recurrence")


@dataclass(frozen=True)
class IdentityDecision:
    """The one identity statement a finding is allowed to make.

    ``existing`` names an issue the supplied catalog listed and nothing else;
    ``new`` asks ASL to allocate one and therefore names nothing. Both are
    requests: the decision records what the auditor asked for, and allocation
    remains ASL's.
    """

    decision: str
    issue_id: str | None = None
    source: str = "agent"

    def __post_init__(self) -> None:
        if self.source not in IDENTITY_SOURCES:
            raise AgentOutputError(
                "malformed-output",
                f"an identity decision comes from one of "
                f"{', '.join(IDENTITY_SOURCES)}; got {self.source!r}",
            )

    @property
    def is_new(self) -> bool:
        return self.decision == "new"


@dataclass(frozen=True)
class AgentAudit:
    """One validated agent answer: lanes, findings, and their identities.

    ``identities`` is positional against ``findings`` and the two are refused
    at different lengths, because an identity decision that cannot be attributed
    to a specific finding is worse than none: it would be attached to whichever
    defect happened to line up.
    """

    lanes: tuple[assurance_models.LaneEvidence, ...]
    findings: tuple[Finding, ...]
    identities: tuple[IdentityDecision, ...]

    def __post_init__(self) -> None:
        if len(self.identities) != len(self.findings):
            raise AgentOutputError(
                "malformed-output",
                "every finding carries exactly one identity decision; the answer "
                f"held {len(self.findings)} finding(s) and "
                f"{len(self.identities)} decision(s)",
            )

    @property
    def decisions(self) -> tuple[tuple[Finding, IdentityDecision], ...]:
        """Each finding paired with the identity decision it arrived with."""
        return tuple(zip(self.findings, self.identities, strict=True))


@dataclass(frozen=True)
class AgentAuditOutcome:
    """The audit an engine run produced, with the attempts that produced it."""

    audit: assurance_models.SupervisorAudit
    attempts: tuple[assurance_models.AuditAttempt, ...]
    agent_covered: bool
    mutation_issue_id: str | None = None
    #: The validated answer behind ``audit``, kept because the canonical audit
    #: carries no identity: allocation reads the decisions from here.
    agent: AgentAudit | None = None
    #: The catalog those decisions were made against, whose digest names the
    #: identity state they are valid for.
    catalog: AgentCatalog | None = None
    #: The issue each selecting finding named, keyed by fingerprint, in the form
    #: the publication revalidates. Empty for an audit that selected nothing.
    identity_selections: Mapping[str, str] = field(default_factory=dict)

    @property
    def catalog_digest(self) -> str | None:
        """The identity state this audit's decisions were reviewed against.

        ``None`` when no agent answer stands behind the audit: a digest is only
        worth revalidating beside a decision it could invalidate, and supplying
        one for an audit that decided nothing would ask the publisher to check
        an answer that does not exist.
        """
        if self.agent is None or self.catalog is None:
            return None
        return self.catalog.digest

    @property
    def linked_issue_ids(self) -> tuple[str, ...]:
        """Issues the audit's own guards already appended, for the summary.

        The mutation guard opens its issue while the attempt is still running,
        so the publication names that ID instead of observing the mutation a
        second time; without this the run's most severe issue would exist only
        in the inbox and be missing from every run-facing surface.
        """
        return () if self.mutation_issue_id is None else (self.mutation_issue_id,)


def _inline(text: str, *, limit: int = assurance_models.MAX_TEXT_CHARS) -> str:
    """Collapse validated text to one line so it can be rendered into a cell."""
    collapsed = " ".join(_CONTROL_CHAR_RE.sub(" ", str(text)).split())
    return collapsed[:limit]


@dataclass(frozen=True)
class AgentCatalogEntry:
    """One canonical issue as an auditor sees it.

    Normalized localization and summary tokens are deliberately absent: they
    exist so ASL can recognize a recurrence, and an auditor picking an ID by
    reading the record has no use for them but would pay for them out of the
    size bound.
    """

    issue_id: str
    state: str
    severity: str
    category: str
    component: str
    invariant_key: str
    lane: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {
            "issue_id": self.issue_id,
            "state": self.state,
            "severity": self.severity,
            "category": self.category,
            "component": self.component,
            "invariant_key": self.invariant_key,
            "lane": self.lane,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class AgentCatalog:
    """The bounded catalog supplied with a prompt, and what it left out.

    ``omitted`` names the issues the size bound dropped, which is not the same
    fact as an issue not existing. Answering "no such issue" to a selection the
    listing merely could not fit is what would teach an auditor to invent IDs,
    so the two are kept apart here and rejected with different explanations.
    """

    digest: str
    entries: tuple[AgentCatalogEntry, ...] = ()
    omitted: tuple[str, ...] = ()
    issue_ids: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", frozenset(entry.issue_id for entry in self.entries)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "omitted": len(self.omitted),
            "issues": [entry.to_dict() for entry in self.entries],
        }


def _agent_catalog_entry(
    entry: assurance_projection.CatalogEntry,
) -> AgentCatalogEntry:
    return AgentCatalogEntry(
        issue_id=entry.issue_id,
        state=entry.state,
        severity=entry.severity,
        category=entry.category,
        component=entry.component,
        invariant_key=entry.invariant_key,
        lane=entry.lane,
        summary=_inline(entry.summary, limit=MAX_CATALOG_SUMMARY_CHARS),
    )


def build_agent_catalog(catalog: assurance_projection.IdentityCatalog) -> AgentCatalog:
    """Return the agent-facing form of ``catalog``, bounded and self-describing.

    The bound is spent on the most recently seen issues, because a defect sighted
    lately is the one an auditor is most likely to be looking at again, and the
    survivors are listed in issue order so the listing reads as a catalog rather
    than as a ranking. The digest covers the whole catalog, not the sample: it
    names the identity state a decision was made against, which is what a later
    publisher revalidates, and a sample cannot answer that question.
    """
    number = assurance_models.issue_id_number
    ranked = sorted(
        catalog.entries,
        key=lambda entry: (entry.last_seen_at, number(entry.issue_id)),
        reverse=True,
    )
    envelope = len(
        assurance_models.canonical_json_bytes(
            {"digest": catalog.digest, "omitted": len(ranked), "issues": []}
        )
    )
    budget = MAX_AGENT_CATALOG_BYTES - envelope
    kept: list[AgentCatalogEntry] = []
    omitted: list[str] = []
    used = 0
    for entry in ranked:
        supplied = _agent_catalog_entry(entry)
        body = len(assurance_models.canonical_json_bytes(supplied.to_dict())) - 1
        # Every entry after the first also pays for the comma that precedes it.
        cost = body + (1 if kept else 0)
        if omitted or used + cost > budget:
            omitted.append(entry.issue_id)
            continue
        used += cost
        kept.append(supplied)
    return AgentCatalog(
        digest=catalog.digest,
        entries=tuple(sorted(kept, key=lambda entry: number(entry.issue_id))),
        omitted=tuple(sorted(omitted, key=number)),
    )


#: What an auditor is given when the ledger holds no issue, or holds issues
#: nobody can read. Every finding then has to request a new allocation, which is
#: the honest answer when no existing issue can be shown.
EMPTY_AGENT_CATALOG = build_agent_catalog(assurance_projection.IdentityCatalog())


def widen_agent_catalog(
    supplied: AgentCatalog,
    catalog: assurance_projection.IdentityCatalog,
    issue_ids: Sequence[str],
) -> AgentCatalog:
    """Return ``supplied`` with ``issue_ids`` promoted into the listing.

    A candidate the size bound had dropped is worth more to a retry than the
    entry recency ranking kept in its place: proposing an ID the validator would
    then refuse as unselectable would burn the attempt and teach nothing. The
    digest is unchanged because it names the whole inbox rather than the sample,
    so widening what is listed does not restate what the decision is valid for.
    """
    promoted = []
    for issue_id in issue_ids:
        if issue_id in supplied.issue_ids:
            continue
        entry = catalog.entry(issue_id)
        if entry is not None:
            promoted.append(_agent_catalog_entry(entry))
    if not promoted:
        return supplied
    number = assurance_models.issue_id_number
    added = frozenset(entry.issue_id for entry in promoted)
    return AgentCatalog(
        digest=supplied.digest,
        entries=tuple(
            sorted(
                (*supplied.entries, *promoted), key=lambda entry: number(entry.issue_id)
            )
        ),
        omitted=tuple(item for item in supplied.omitted if item not in added),
    )


def read_identity_catalog(
    state_dir: Path | str | None,
) -> assurance_projection.IdentityCatalog:
    """Return the inbox's identity catalog, or an empty one.

    An unreadable ledger costs the auditor its ability to recognize a recurrence,
    and costs ASL its ability to recognize one on the auditor's behalf. It must
    not also cost the run its audit, so this degrades to the empty catalog: the
    audit still runs, and every finding it reports asks for a new issue instead
    of selecting one nobody could show it.
    """
    try:
        projection = assurance_projection.read_projection(state_dir=state_dir)
    except (
        OSError,
        assurance_ledger.LedgerError,
        assurance_models.AssuranceError,
    ):
        return assurance_projection.IdentityCatalog()
    return projection.catalog


def read_agent_catalog(state_dir: Path | str | None) -> AgentCatalog:
    """Return the agent-facing catalog to supply for one audit."""
    return build_agent_catalog(read_identity_catalog(state_dir))


def _agent_schema_block() -> str:
    return json.dumps(
        {
            "schema_version": AGENT_OUTPUT_SCHEMA_VERSION,
            "lanes": [
                {
                    "lane": assurance_models.RUBRIC_LANES[0],
                    "status": "pass|fail|unavailable",
                    "summary": "what the evidence showed for this lane",
                    "evidence": ["<locator from the evidence index>"],
                }
            ],
            "findings": [
                {
                    "lane": assurance_models.RUBRIC_LANES[0],
                    "severity": "low|medium|high|critical",
                    "category": "identity",
                    "component": "workspace-registry",
                    "invariant_key": "identity.root-registry-agreement",
                    "summary": "the defect, stated as what the evidence shows",
                    "recommendation": "what a maintainer should change",
                    "evidence": ["<locator from the evidence index>"],
                    "localization": ["<source pointer>"],
                    "identity": {
                        "decision": "existing|new",
                        "issue_id": "<catalog issue_id, only when existing>",
                    },
                }
            ],
        },
        indent=2,
        sort_keys=True,
    )


def build_audit_prompt(
    bundle: AuditInput | Mapping[str, Any],
    *,
    catalog: AgentCatalog,
    correction: str | None = None,
    candidates: Sequence[assurance_projection.IdentityCandidate] = (),
) -> str:
    """Render the prompt for one audit attempt.

    ``correction`` carries the validation failure of the previous attempt and
    nothing else: the rejected answer itself is deliberately withheld, so the
    retry re-derives its verdict from the evidence rather than being handed its
    own earlier output to launder into a better-formed version of the same
    mistake. The catalog is repeated verbatim on that retry, since a correction
    that told an auditor to select from a catalog it no longer had in front of
    it would be asking it to remember an ID instead of reading one.

    ``candidates`` is the other reason a retry happens and is a separate channel
    on purpose. Nothing was rejected: the answer was well-formed and ASL merely
    recognized issues that may already own what it reported. Sending that
    through the correction string would tell an auditor its valid answer broke a
    validation rule, which is the one thing it did not do. The candidates are
    ASL's own catalog records rather than any part of the previous answer.
    """
    payload = bundle.payload if isinstance(bundle, AuditInput) else dict(bundle)
    prompt = AUDIT_PROMPT_TEMPLATE.format(
        package_root=EVIDENCE_ROOT_PACKAGE,
        skills_root=EVIDENCE_ROOT_SKILLS,
        lane_list="\n".join(f"- {lane}" for lane in assurance_models.RUBRIC_LANES),
        schema=_agent_schema_block(),
        catalog=assurance_models.canonical_json_bytes(catalog.to_dict()).decode(
            "utf-8"
        ),
        run_id=payload.get("run_id", "unknown"),
        bundle=assurance_models.canonical_json_bytes(payload).decode("utf-8"),
    )
    if candidates:
        prompt = f"{prompt}\n{_candidate_section(candidates)}"
    if correction is None:
        return prompt
    return f"{prompt}\n{AUDIT_CORRECTION_TEMPLATE.format(detail=_inline(correction))}"


def _candidate_section(
    candidates: Sequence[assurance_projection.IdentityCandidate],
) -> str:
    return AUDIT_CANDIDATE_TEMPLATE.format(
        candidates=assurance_models.canonical_json_bytes(
            [candidate.to_dict() for candidate in candidates]
        ).decode("utf-8"),
        invariant=assurance_projection.MATCH_INVARIANT,
        localization=assurance_projection.MATCH_LOCALIZATION,
        summary=assurance_projection.MATCH_SUMMARY,
        threshold=f"{assurance_projection.MIN_SUMMARY_SIMILARITY:.2f}",
    )


def _agent_payload(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise AgentOutputError("malformed-output", "the agent returned no output")
    if len(text.encode("utf-8")) > MAX_AGENT_OUTPUT_BYTES:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answered with more than the {MAX_AGENT_OUTPUT_BYTES}-byte "
            "cap on audit output",
        )
    fenced = _JSON_FENCE_RE.findall(text)
    candidate = fenced[-1] if fenced else text
    try:
        payload = json.loads(candidate)
    except (ValueError, RecursionError) as error:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer is not parseable JSON: {error}",
        ) from error
    if not isinstance(payload, Mapping):
        raise AgentOutputError(
            "malformed-output", "the agent answer must be a single JSON object"
        )
    unknown = sorted(
        str(key)
        for key in payload
        if key not in {"schema_version", "lanes", "findings"}
    )
    if unknown:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer has unknown key(s) {', '.join(unknown)}; only "
            "'schema_version', 'lanes', and 'findings' are read",
        )
    if payload.get("schema_version") != AGENT_OUTPUT_SCHEMA_VERSION:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer must declare schema_version "
            f"{AGENT_OUTPUT_SCHEMA_VERSION}; got "
            f"{payload.get('schema_version')!r}",
        )
    return dict(payload)


def _agent_lanes(
    payload: Mapping[str, Any],
) -> tuple[assurance_models.LaneEvidence, ...]:
    entries = payload.get("lanes")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise AgentOutputError(
            "malformed-output", "the agent answer must carry a 'lanes' list"
        )
    lanes = []
    for index, entry in enumerate(entries):
        try:
            lanes.append(
                assurance_models.parse_lane_evidence(entry, label=f"lane #{index + 1}")
            )
        except assurance_models.AssuranceError as error:
            raise AgentOutputError("malformed-output", str(error)) from error
    try:
        assurance_models.validate_lane_coverage(lanes, require_complete=True)
    except assurance_models.AssuranceError as error:
        raise AgentOutputError("missing-lanes", str(error)) from error
    return tuple(lanes)


def _identity_decision(
    entry: Mapping[str, Any], *, label: str, catalog: AgentCatalog
) -> IdentityDecision:
    """Validate one finding's identity statement against the supplied catalog.

    The two rejections a caller must be able to tell apart are both here: an ID
    the catalog does not list at all — invented, malformed, or merged away — and
    an ID that exists but did not fit the listing. Only the first is evidence
    the auditor made something up, and answering both with "no such issue" is
    what would teach it to guess.
    """
    payload = entry.get("identity")
    if not isinstance(payload, Mapping):
        raise AgentOutputError(
            "malformed-output",
            f"{label} must carry exactly one 'identity' object, either selecting "
            "an issue the supplied catalog lists or requesting a new allocation",
        )
    unknown = sorted(str(key) for key in payload if key not in _IDENTITY_KEYS)
    if unknown:
        raise AgentOutputError(
            "malformed-output",
            f"{label} 'identity' has unknown key(s) {', '.join(unknown)}; only "
            f"{_names(sorted(_IDENTITY_KEYS))} are read",
        )
    decision = payload.get("decision")
    if decision not in IDENTITY_DECISIONS:
        raise AgentOutputError(
            "malformed-output",
            f"{label} 'identity' must set 'decision' to one of "
            f"{', '.join(IDENTITY_DECISIONS)}; got "
            f"{_inline(str(decision), limit=assurance_models.MAX_TOKEN_CHARS)!r}",
        )
    declared = payload.get("issue_id")
    if decision == "new":
        if declared is not None:
            raise AgentOutputError(
                "malformed-output",
                f"{label} 'identity' requests a new allocation and must name no "
                "'issue_id'; ASL allocates the ID for it",
            )
        return IdentityDecision(decision="new")
    try:
        issue_id = assurance_models.parse_issue_id(declared)
    except assurance_models.AssuranceError as error:
        raise AgentOutputError(
            "malformed-output",
            f"{label} 'identity' selects an existing issue and must name the "
            f"'issue_id' the catalog lists for it: {error}",
        ) from error
    if issue_id in catalog.issue_ids:
        return IdentityDecision(decision="existing", issue_id=issue_id)
    if issue_id in catalog.omitted:
        raise AgentOutputError(
            "invalid-reference",
            f"{label} 'identity' selects {issue_id}, which the size bound kept "
            f"out of the {len(catalog.entries)} issues the catalog listed; select "
            "one it did list or request a new allocation instead",
        )
    raise AgentOutputError(
        "invalid-reference",
        f"{label} 'identity' selects {issue_id}, which the supplied catalog does "
        "not list; an ID ASL never allocated, and one that has been merged away, "
        "are both unselectable, so request a new allocation instead",
    )


def _agent_findings(
    payload: Mapping[str, Any], *, catalog: AgentCatalog
) -> tuple[tuple[Finding, ...], tuple[IdentityDecision, ...]]:
    entries = payload.get("findings", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise AgentOutputError(
            "malformed-output", "the agent answer must carry a 'findings' list"
        )
    if len(entries) > assurance_models.MAX_FINDINGS:
        raise AgentOutputError(
            "malformed-output",
            f"the agent answer must hold at most {assurance_models.MAX_FINDINGS} "
            f"findings; got {len(entries)}",
        )
    findings = []
    identities = []
    for index, entry in enumerate(entries):
        label = f"finding #{index + 1}"
        if not isinstance(entry, Mapping):
            raise AgentOutputError("malformed-output", f"{label} must be a JSON object")
        claimed = _AGENT_IDENTITY_KEYS.intersection(str(key) for key in entry)
        if claimed:
            raise AgentOutputError(
                "malformed-output",
                f"{label} sets {', '.join(sorted(claimed))}; the audit agent never "
                "assigns issue IDs or fingerprints of its own, ASL computes them "
                "and an existing issue is named only inside 'identity'",
            )
        unknown = sorted(str(key) for key in entry if key not in _AGENT_FINDING_KEYS)
        if unknown:
            raise AgentOutputError(
                "malformed-output",
                f"{label} has unknown key(s) {', '.join(unknown)}",
            )
        identities.append(_identity_decision(entry, label=label, catalog=catalog))
        try:
            findings.append(
                assurance_models.parse_finding(
                    {
                        **{
                            str(key): value
                            for key, value in entry.items()
                            if key != "identity"
                        },
                        "schema_version": assurance_models.ASSURANCE_SCHEMA_VERSION,
                    },
                    label=label,
                )
            )
        except assurance_models.AssuranceError as error:
            raise AgentOutputError("malformed-output", str(error)) from error
    return tuple(findings), tuple(identities)


def _validate_agent_references(
    lanes: Sequence[assurance_models.LaneEvidence],
    findings: Sequence[Finding],
    *,
    declared: Iterable[str],
) -> None:
    known = set(declared)
    referenced = {ref for lane in lanes for ref in lane.evidence}
    referenced.update(ref for finding in findings for ref in finding.evidence)
    unknown = sorted(referenced - known)
    if unknown:
        raise AgentOutputError(
            "invalid-reference",
            f"the agent cited evidence the bundle never declared: {_names(unknown)}",
        )
    outside = sorted(
        {
            ref
            for finding in findings
            for ref in finding.localization
            if not any(_inside(ref, root) for root in LOCALIZATION_ROOTS)
        }
    )
    if outside:
        raise AgentOutputError(
            "invalid-reference",
            f"the agent localized outside the read-only source roots "
            f"{', '.join(LOCALIZATION_ROOTS)}: {_names(outside)}",
        )


def parse_agent_audit(
    text: str, *, declared: Iterable[str], catalog: AgentCatalog
) -> AgentAudit:
    """Validate one agent answer strictly, or raise :class:`AgentOutputError`.

    Nothing the agent writes is trusted as output: the Markdown it may have
    produced is never rendered, every locator it names must already appear in the
    evidence index ASL built, and the only identity it may state is a selection
    from ``catalog`` — which is the catalog it was actually shown, not the
    current one, so an answer is judged against what it could read.
    """
    payload = _agent_payload(text)
    lanes = _agent_lanes(payload)
    findings, identities = _agent_findings(payload, catalog=catalog)
    _validate_agent_references(lanes, findings, declared=declared)
    return AgentAudit(lanes=lanes, findings=findings, identities=identities)


@dataclass(frozen=True)
class IdentityResolution:
    """What ASL made of one answer's identity decisions.

    ``audit`` is the answer with every decision ASL could settle already settled,
    so a caller downstream reads one list rather than re-deriving recurrences.
    ``candidates`` is non-empty exactly when at least one finding asked for an
    allocation the catalog can argue with, and the audit is unusable until that
    is answered — semantic similarity identifies a candidate and never merges
    one, so there is no third option where ASL decides for itself.
    """

    audit: AgentAudit
    candidates: tuple[assurance_projection.IdentityCandidate, ...] = ()
    ambiguous: int = 0

    @property
    def settled(self) -> bool:
        return self.ambiguous == 0

    @property
    def detail(self) -> str:
        return (
            f"{self.ambiguous} finding(s) requested a new allocation for a defect "
            f"the catalog already names a candidate for: "
            f"{_names(candidate.issue_id for candidate in self.candidates)}"
        )


def resolve_identities(
    audit: AgentAudit, *, catalog: assurance_projection.IdentityCatalog
) -> IdentityResolution:
    """Settle each finding's identity against the catalog, or report ambiguity.

    Two decisions need nobody: a selection the validator already checked against
    the supplied catalog, and a new-allocation request whose fingerprint the
    catalog already names an issue for. The second overrules the answer, because
    a fingerprint is derived by ASL from the finding's own category, component,
    and invariant key — an auditor that reworded a summary did not make the
    defect new, and allocating a second issue for it is exactly the split the
    inbox exists to prevent.

    Everything else the catalog recognizes is a candidate, and a candidate is
    never applied. It goes back as a question, and if the answer is still ``new``
    the audit fails closed rather than either merging or allocating.
    """
    resolved: list[IdentityDecision] = []
    candidates: dict[str, assurance_projection.IdentityCandidate] = {}
    ambiguous = 0
    for finding, decision in audit.decisions:
        if not decision.is_new:
            resolved.append(decision)
            continue
        recurrence = catalog.issue_for_fingerprint(finding.fingerprint)
        if recurrence is not None:
            resolved.append(
                IdentityDecision(
                    decision="existing", issue_id=recurrence, source="recurrence"
                )
            )
            continue
        matched = catalog.candidates(finding)
        resolved.append(decision)
        if not matched:
            continue
        ambiguous += 1
        for candidate in matched:
            known = candidates.get(candidate.issue_id)
            candidates[candidate.issue_id] = (
                candidate if known is None else known.merged(candidate)
            )
    ranked = sorted(candidates.values(), key=assurance_projection.candidate_order)
    return IdentityResolution(
        audit=replace(audit, identities=tuple(resolved)),
        candidates=tuple(ranked[:MAX_PROMPTED_CANDIDATES]),
        ambiguous=ambiguous,
    )


def _worst(*statuses: CheckStatus) -> CheckStatus:
    return max(statuses, key=lambda status: _STATUS_PRECEDENCE[status])


def _status_counts(results: Sequence[assurance_models.DeterministicResult]) -> str:
    if not results:
        return "no deterministic check reported"
    counts = {status: 0 for status in assurance_models.CHECK_STATUSES}
    for result in results:
        counts[result.status] += 1
    return ", ".join(f"{count} {status}" for status, count in counts.items() if count)


def _anchor(declared: Sequence[str]) -> tuple[str, ...]:
    if RUN_ANCHOR in declared:
        return (RUN_ANCHOR,)
    return (declared[0],) if declared else ()


def _deterministic_signal(
    results: Sequence[assurance_models.DeterministicResult],
) -> CheckStatus | None:
    """Return what the checks on one lane prove, or ``None`` when they prove nothing.

    A failure is the strongest signal and wins outright; otherwise a single pass
    is a real, reproducible verdict. Checks that all answered ``unavailable``
    prove neither, so they contribute nothing rather than a pessimistic verdict.
    """
    statuses = {result.status for result in results}
    if "fail" in statuses:
        return "fail"
    if "pass" in statuses:
        return "pass"
    return None


def _merged_lanes(
    results: Sequence[assurance_models.DeterministicResult],
    agent: AgentAudit | None,
    *,
    declared: Sequence[str],
) -> tuple[assurance_models.LaneEvidence, ...]:
    """Reduce deterministic results and agent lanes to one record per lane.

    Where both sides carry a signal the worse one wins: an agent that reports
    ``pass`` on a lane a deterministic check disproved does not get to overrule
    the check. A check that answered ``unavailable`` is not a signal, though — it
    says the check could not read its evidence, which is a statement about the
    check and not about the run. Letting it drag the lane down would mean a
    legacy plan with no registry binding could never produce a clean audit no
    matter what the auditor found, so an all-``unavailable`` lane defers to the
    agent's claim, and a lane with neither signal stays ``unavailable``.
    """
    reported = {lane.lane: lane for lane in (agent.lanes if agent else ())}
    lanes = []
    for name in assurance_models.RUBRIC_LANES:
        lane_results = [result for result in results if result.lane == name]
        claim = reported.get(name)
        claimed: CheckStatus = claim.status if claim else "unavailable"
        deterministic = _deterministic_signal(lane_results)
        status = claimed if deterministic is None else _worst(deterministic, claimed)
        evidence = list(
            dict.fromkeys(
                [
                    *(claim.evidence if claim else ()),
                    *(ref for result in lane_results for ref in result.evidence),
                ]
            )
        )[: assurance_models.MAX_REFERENCES_PER_ITEM]
        deterministic_note = f"Deterministic checks: {_status_counts(lane_results)}."
        summary = (
            f"{_inline(claim.summary)} {deterministic_note}"
            if claim
            else f"Agent coverage was unavailable for this lane. {deterministic_note}"
        )
        lanes.append(
            assurance_models.LaneEvidence(
                lane=name,
                status=status,
                summary=_inline(summary),
                evidence=tuple(evidence) or _anchor(declared),
            )
        )
    return tuple(lanes)


def _merged_findings(
    deterministic: Sequence[Finding], agent: Sequence[Finding]
) -> tuple[Finding, ...]:
    """Combine both sources, letting the reproducible finding own a fingerprint."""
    merged: dict[str, Finding] = {}
    for finding in (*deterministic, *agent):
        merged.setdefault(finding.fingerprint, finding)
    return tuple(merged.values())[: assurance_models.MAX_FINDINGS]


def _identity_selections(
    agent: AgentAudit, findings: Sequence[Finding]
) -> dict[str, str]:
    """Restate the agent's selections in the form the publication revalidates.

    Keyed by fingerprint because that is how a decision is attributed to a
    finding under the append lock, and restricted to the findings the canonical
    audit actually carries: merging and the finding cap can drop an agent
    finding, and a selection naming one the publication never sees is refused.
    The first decision for a fingerprint wins, which is the decision belonging to
    the finding :func:`_merged_findings` kept.
    """
    carried = {finding.fingerprint for finding in findings}
    selections: dict[str, str] = {}
    for finding, decision in agent.decisions:
        if decision.issue_id is None or finding.fingerprint not in carried:
            continue
        selections.setdefault(finding.fingerprint, decision.issue_id)
    return selections


def render_supervisor_audit(
    *,
    run_id: str,
    bundle: AuditInput,
    results: Sequence[assurance_models.DeterministicResult],
    generated_at: str,
    agent: AgentAudit | None = None,
    reason: str | None = None,
) -> assurance_models.SupervisorAudit:
    """Build the canonical audit from validated internal data only.

    Both reports are rendered from this object, so no byte of agent Markdown
    ever reaches an operator: the agent contributes validated lane statuses,
    summaries, findings, and locators, and ASL decides everything else.
    """
    evidence = bundle.evidence
    declared = [entry.locator for entry in evidence]
    lanes = _merged_lanes(results, agent, declared=declared)
    findings = _merged_findings(
        findings_from_results(results), agent.findings if agent else ()
    )
    if agent is None:
        state: assurance_models.AuditState = "unavailable"
        state_reason = reason or REASON_AGENT_UNAVAILABLE
    elif findings:
        state, state_reason = "findings", None
    elif all(lane.status == "pass" for lane in lanes):
        state, state_reason = "all-clear", None
    elif any(lane.status == "fail" for lane in lanes):
        # Unreachable while every failing lane mints a finding. Kept so a future
        # lane that can fail without one is held back rather than falling
        # through to ``partial``, which would read a disproved lane as merely
        # unjudged.
        state, state_reason = "unavailable", REASON_LANE_COVERAGE
    else:
        state, state_reason = "partial", REASON_LANE_EVIDENCE_ABSENT
    return assurance_models.SupervisorAudit(
        run_id=run_id,
        state=state,
        input_digest=bundle.sha256,
        generated_at=generated_at,
        reason=state_reason,
        evidence=evidence,
        lanes=lanes,
        findings=findings,
        deterministic=tuple(results)[: assurance_models.MAX_DETERMINISTIC_RESULTS],
    )


def _markdown_row(*cells: str) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def render_audit_markdown(audit: assurance_models.SupervisorAudit) -> str:
    """Render the operator-facing report from the validated audit object."""
    lines = [
        f"# Supervisor audit — {audit.run_id}",
        "",
        f"- State: `{audit.state}`",
        f"- Reason: {audit.reason if audit.reason else '(none)'}",
        f"- Generated: {audit.generated_at}",
        f"- Evidence digest: `{audit.input_digest}`",
        "",
        "## Rubric lanes",
        "",
        _markdown_row("Lane", "Status", "Summary", "Evidence"),
        _markdown_row("---", "---", "---", "---"),
    ]
    for lane in audit.lanes:
        lines.append(
            _markdown_row(
                lane.lane,
                lane.status,
                _inline(lane.summary),
                ", ".join(f"`{ref}`" for ref in lane.evidence),
            )
        )
    lines.extend(["", "## Findings", ""])
    if not audit.findings:
        lines.append("No findings.")
    for finding in audit.findings:
        lines.extend(
            [
                f"### {finding.invariant_key} ({finding.severity})",
                "",
                f"- Lane: {finding.lane}",
                f"- Component: {finding.component}",
                f"- Category: {finding.category}",
                f"- Fingerprint: `{finding.fingerprint}`",
                f"- Evidence: {', '.join(f'`{ref}`' for ref in finding.evidence)}",
            ]
        )
        if finding.localization:
            lines.append(
                "- Localization: "
                + ", ".join(f"`{ref}`" for ref in finding.localization)
            )
        lines.extend(
            [
                "",
                _inline(finding.summary),
                "",
                f"**Recommendation:** {_inline(finding.recommendation)}",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## Deterministic checks",
            "",
            _markdown_row("Check", "Status", "Invariant", "Detail"),
            _markdown_row("---", "---", "---", "---"),
        ]
    )
    for result in audit.deterministic:
        lines.append(
            _markdown_row(
                result.check,
                result.status,
                result.invariant_key,
                _inline(result.detail or ""),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class StagedAuditReports:
    """Both canonical reports, written in full but not yet canonical.

    The reports have to be readable before the ledger record that makes them
    true exists, because the record names them by digest. Staging separates the
    two facts: the bytes are final and hashed here, while the canonical names
    appear only once identity has been validated under the append lock. A
    publication that never gets that far therefore leaves no report for a reader
    to find, which is the difference between an audit that did not publish and a
    summary nobody wrote.
    """

    run_dir: Path
    directory: Path
    #: Each report as its staged path, the canonical name it is promoted to, and
    #: the digest of the bytes that were written.
    entries: tuple[tuple[Path, Path, str], ...]
    durable: bool = True
    #: Canonical names this staging has taken, so a publication that promoted
    #: and then failed can give them back.
    promoted: list[Path] = field(default_factory=list, repr=False, compare=False)

    @property
    def references(self) -> tuple[assurance_models.EvidenceRef, ...]:
        """Evidence naming the canonical paths, not the staged ones."""
        return tuple(
            _artifact_reference(canonical, self.run_dir, digest)
            for _, canonical, digest in self.entries
        )

    def promote(self) -> None:
        """Give both reports their canonical names, or neither of them.

        Each link is refused by the filesystem if the canonical name is already
        taken, which is the same rule :func:`assurance_models.write_new_bytes`
        enforces and for the same reason: two reports under one name would mean
        two audits disagree about one run. A failure part-way unlinks whatever
        was already promoted, so the caller can abort with the run directory
        holding no half-published audit.
        """
        for staged, canonical, _ in self.entries:
            try:
                os.link(staged, canonical)
            except OSError as error:
                self.unpromote()
                raise assurance_models.AssuranceError(
                    f"could not promote audit report {canonical}: {error}"
                ) from error
            self.promoted.append(canonical)
        if not self.durable:
            return
        try:
            descriptor = os.open(self.run_dir, os.O_RDONLY)
            try:
                durability.fullsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            self.unpromote()
            raise assurance_models.AssuranceError(
                f"could not sync audit report directory {self.run_dir}: {error}"
            ) from error

    def unpromote(self) -> None:
        """Release the canonical names again.

        Promotion happens before the ledger record is durable, so an append that
        fails afterwards would otherwise leave reports no summary names and a
        canonical name a later attempt could never take. Giving the names back
        keeps a transient failure retryable instead of permanent.
        """
        for path in self.promoted:
            with contextlib.suppress(OSError):
                path.unlink()
        self.promoted.clear()

    def discard(self) -> None:
        """Remove the staging directory, promoted or not.

        Best effort: the promoted reports are separate directory entries for the
        same data, so losing the staging copies costs nothing, and failing to
        remove them must not fail a publication that already succeeded.
        """
        shutil.rmtree(self.directory, ignore_errors=True)


def stage_audit_reports(
    run_dir: Path | str,
    audit: assurance_models.SupervisorAudit,
    *,
    durable: bool = True,
) -> StagedAuditReports:
    """Write both canonical reports into this run's staging directory.

    A staging directory left behind by an earlier attempt is cleared rather than
    refused. Nothing durable ever names a path inside it, so its contents are
    unreferenced scratch, and treating them as state to protect would make one
    crashed publication permanently unable to publish this run.
    """
    root = Path(run_dir)
    directory = root / AUDIT_STAGING_DIR_NAME
    shutil.rmtree(directory, ignore_errors=True)
    try:
        directory.mkdir(parents=True)
    except OSError as error:
        raise assurance_models.AssuranceError(
            f"could not create audit staging directory {directory}: {error}"
        ) from error
    entries: list[tuple[Path, Path, str]] = []
    for name, data in (
        (
            assurance_models.AUDIT_REPORT_FILENAME,
            assurance_models.canonical_json_bytes(audit.to_dict()),
        ),
        (
            assurance_models.AUDIT_MARKDOWN_FILENAME,
            render_audit_markdown(audit).encode("utf-8"),
        ),
    ):
        staged = assurance_models.write_audit_artifact(
            directory, name, data, durable=durable
        )
        entries.append(
            (
                staged,
                root / assurance_models.audit_artifact_name(name),
                artifacts.sha256_hex(data),
            )
        )
    return StagedAuditReports(
        run_dir=root,
        directory=directory,
        entries=tuple(entries),
        durable=durable,
    )


def write_audit_reports(
    run_dir: Path | str,
    audit: assurance_models.SupervisorAudit,
    *,
    durable: bool = True,
) -> tuple[Path, Path]:
    """Write both canonical reports, each exactly once."""
    staged = stage_audit_reports(run_dir, audit, durable=durable)
    try:
        staged.promote()
    finally:
        staged.discard()
    (_, report, _), (_, markdown, _) = staged.entries
    return report, markdown


def _reason_token(value: Any, fallback: str) -> str:
    """Keep a reason only when it groups; a raw exception string never does.

    ``run_restricted_audit`` reports launch failures with the harness error's
    own text, which differs on every machine. Falling back to the attempt
    outcome keeps one recurring failure deduplicating into one inbox issue.
    """
    try:
        return assurance_models.parse_reason_token(value)
    except assurance_models.AssuranceError:
        return fallback


def _bounded_argv(argv: Sequence[str], *, prompt: str) -> list[str]:
    """Replace the inlined prompt with its digest so an attempt record stays small."""
    return [
        f"[prompt sha256:{_sha256_text(prompt)}]" if item == prompt else item
        for item in argv
    ]


def _write_attempt_artifacts(
    run_dir: Path,
    number: int,
    *,
    prompt: str,
    attempt: AuditAttemptResult,
    outcome: AttemptOutcome,
    detail: str | None,
    durable: bool,
) -> str | None:
    """Archive one attempt, returning its locator, or ``None`` when it cannot.

    Best effort by design, exactly like the mutation artifact: losing the
    archive of an attempt must not lose the verdict the attempt produced.
    """
    try:
        directory = assurance_models.create_attempt_dir(run_dir, number)
        assurance_models.write_new_bytes(
            directory / ATTEMPT_PROMPT_FILE, prompt.encode("utf-8"), durable=durable
        )
        assurance_models.write_new_bytes(
            directory / ATTEMPT_MESSAGE_FILE,
            _truncate_utf8(attempt.last_message, MAX_AGENT_OUTPUT_BYTES).encode(
                "utf-8"
            ),
            durable=durable,
        )
        for name, stream in (
            (ATTEMPT_STDOUT_FILE, attempt.stdout_tail),
            (ATTEMPT_STDERR_FILE, attempt.stderr_tail),
        ):
            if stream:
                assurance_models.write_new_bytes(
                    directory / name, stream.encode("utf-8"), durable=durable
                )
        assurance_models.write_new_bytes(
            directory / ATTEMPT_OUTCOME_FILE,
            assurance_models.canonical_json_bytes(
                {
                    "number": number,
                    "outcome": outcome,
                    "detail": detail,
                    "reason": attempt.reason,
                    "returncode": attempt.returncode,
                    "argv": _bounded_argv(attempt.argv, prompt=prompt),
                    "classification": (
                        None
                        if attempt.classification is None
                        else attempt.classification.to_dict()
                    ),
                }
            ),
            durable=durable,
        )
    except OSError, assurance_models.AssuranceError:
        return None
    return assurance_models.attempt_dir_name(number)


def _delivery_is_retryable(attempt: AuditAttemptResult) -> bool:
    """Say whether relaunching this failed attempt could plausibly succeed.

    A timeout carries no classification and is retried on its own terms. Every
    other delivery failure defers to the shared classifier, which recognises
    only genuine transport faults as transient: authentication, quota, and
    unrecognised exits are permanent, so waiting out the backoff would just
    stall the run before reaching the same verdict.
    """
    classification = attempt.classification
    return classification is None or classification.retryable


def _refused_for_size(attempt: AuditAttemptResult) -> bool:
    """Say whether the provider rejected this attempt's request as too large.

    Read off the classification the launch already carried rather than by
    re-matching the harness text, so the rule that recognises the refusal lives
    in one place and the audit and the session loop agree about what happened.
    """
    classification = attempt.classification
    return (
        classification is not None
        and classification.marker == harness_failures.CONTEXT_OVERFLOW_MARKER
    )


def _record_capacity_outage(
    attempt: AuditAttemptResult,
    *,
    role: AuditRole,
    state_dir: Path | str | None,
    run_id: str,
    delay: float,
) -> None:
    """Note a provider capacity outage in the shared availability ledger.

    Recording it is all that happens: the auditor is never swapped for a model
    that happens to be up, because its identity is a structural property of the
    audit rather than a performance knob. The ledger entry is durable evidence
    that the outage was the provider's, and it expires on its own once the
    backoff this audit is about to wait has passed.
    """
    classification = attempt.classification
    if (
        classification is None
        or classification.marker != harness_failures.MODEL_CAPACITY_MARKER
        or role.model is None
    ):
        return
    with contextlib.suppress(OSError):
        model_availability.suppress_until(
            state.state_root(state_dir),
            role.model,
            reason=harness_failures.MODEL_CAPACITY_MARKER,
            retry_hint=classification.retry_at or str(int(delay)),
            run_id=run_id,
        )


def run_agent_audit(
    role: AuditRole,
    *,
    bundle: AuditInput,
    run_id: str,
    run_dir: Path | str,
    workspace_root: Path | str,
    plan_dir: Path | str,
    repositories: Sequence[str],
    registry_declared: bool = True,
    pinned: PinnedWorkspace | None = None,
    state_dir: Path | str | None = None,
    results: Sequence[assurance_models.DeterministicResult] | None = None,
    stream_callback: harnesses.StreamCallback | None = None,
    abort: harnesses.AbortSignal | None = None,
    durable: bool = True,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
    _sleep_fn: Callable[[float], None] = time.sleep,
) -> AgentAuditOutcome:
    """Run the semantic audit and return the audit its attempts earned.

    Two budgets are spent, not one, because a bad answer and an undelivered
    request are different failures. The agent may produce at most
    :data:`assurance_models.MAX_AUDIT_ATTEMPTS` unusable *answers*, each retry a
    fresh invocation told only which validation rule the previous one broke. A
    launch that never reached the agent — a timeout or a transport/capacity
    failure — is charged instead to
    :data:`assurance_models.MAX_AUDIT_DELIVERY_ATTEMPTS` and retried after a
    backoff with the identical prompt, since there is no answer to correct and
    the outage is the provider's doing. When either budget runs out the audit is
    ``unavailable`` under the reason that exhausted it — but it still carries the
    deterministic lanes, results, and findings, because losing reproducible
    evidence because an agent misbehaved would be a second failure, not a
    consequence of the first.

    A third budget,
    :data:`assurance_models.MAX_AUDIT_SHRINK_ATTEMPTS`, covers the one failure
    that is the caller's own: a provider that refuses the request for size
    before the agent runs. That attempt is retried with the bundle shed to
    :data:`AUDIT_INPUT_OVERFLOW_SHRINK_RATIO` of the size that was refused. Only
    the prompt shrinks — the canonical bundle, the deterministic verdicts read
    from it, and the locators the answer is validated against all stay the ones
    written to the run directory, so the retry sends less without grading a
    different run.

    A candidate retry comes out of the answer budget rather than adding to it. An
    answer that is malformed once and then identity-ambiguous therefore never
    sees the candidate list, and the audit lands ``unavailable`` under
    :data:`REASON_IDENTITY_AMBIGUOUS`. That is the safe end of the trade: the
    cost of not asking is one unpublished audit, while a third turn per audit is
    a cost every run pays. Nothing is merged and nothing is allocated either way.

    ``abort`` cancels the whole audit rather than one attempt: it is checked
    before each launch, so a cancel that arrives during a transport backoff is
    honored at the end of that wait instead of buying another launch.

    The pinned checkout the boundary check is judged on is made once and shared
    by every attempt. Each attempt still fingerprints it on both sides of its
    own turn, so an attempt is judged on what it did rather than on what an
    earlier one left behind; what the attempts share is the tree, not a verdict.
    """
    run_dir = Path(run_dir)
    clock = datetime.now(UTC) if now is None else now()
    verdicts = (
        tuple(results) if results is not None else run_deterministic_checks(bundle)
    )
    declared = [entry.locator for entry in bundle.evidence]
    # Read once, before the first attempt: both attempts must be judged against
    # the catalog they were shown, and re-reading between them could reject a
    # selection for naming an issue a concurrent publisher had just moved.
    inbox = read_identity_catalog(state_dir)
    catalog = build_agent_catalog(inbox)
    attempts: list[assurance_models.AuditAttempt] = []
    mutation_issue_id: str | None = None
    correction: str | None = None
    candidates: tuple[assurance_projection.IdentityCandidate, ...] = ()
    candidates_offered = False
    unavailable_reason = REASON_AGENT_UNAVAILABLE
    answers_spent = 0
    deliveries_spent = 0
    shrinks_spent = 0
    # What the prompt is built from, which the oversized-input retry may narrow.
    # ``bundle`` itself never moves: it is the artifact on disk, the input the
    # deterministic verdicts were read from, and the source of ``declared``.
    prompt_bundle = bundle

    while True:
        if abort is not None and abort.armed:
            raise AuditCancelled(REASON_INTERRUPTED)
        number = len(attempts) + 1
        prompt = build_audit_prompt(
            prompt_bundle, catalog=catalog, correction=correction, candidates=candidates
        )
        attempt = run_restricted_audit(
            role,
            run_dir=run_dir,
            run_id=run_id,
            workspace_root=workspace_root,
            plan_dir=plan_dir,
            prompt=prompt,
            repositories=repositories,
            registry_declared=registry_declared,
            pinned=pinned,
            state_dir=state_dir,
            stream_callback=stream_callback,
            abort=abort,
            now=now,
            _run_harness_fn=_run_harness_fn,
        )
        mutation_issue_id = attempt.issue_id or mutation_issue_id
        agent: AgentAudit | None = None
        matched: tuple[assurance_projection.IdentityCandidate, ...] = ()
        outcome: AttemptOutcome
        detail: str | None
        if attempt.outcome == "ok":
            try:
                answer = parse_agent_audit(
                    attempt.last_message, declared=declared, catalog=catalog
                )
            except AgentOutputError as error:
                outcome, detail = error.outcome, error.detail
                unavailable_reason = error.outcome
            else:
                resolution = resolve_identities(answer, catalog=inbox)
                outcome = "ok"
                detail = None
                if resolution.settled:
                    agent = resolution.audit
                else:
                    outcome = IDENTITY_AMBIGUOUS_OUTCOME
                    detail = resolution.detail
                    unavailable_reason = REASON_IDENTITY_AMBIGUOUS
                    # The candidate list is offered once. Answering `new` again
                    # after seeing it is the auditor's decision, not a failure to
                    # understand the question, so repeating it would only spend
                    # another turn arriving at the same fail-closed verdict.
                    matched = () if candidates_offered else resolution.candidates
        else:
            outcome, detail = attempt.outcome, attempt.reason
            unavailable_reason = _reason_token(attempt.reason, attempt.outcome)
        attempts.append(
            assurance_models.AuditAttempt(
                number=number,
                outcome=outcome,
                artifact_dir=_write_attempt_artifacts(
                    run_dir,
                    number,
                    prompt=prompt,
                    attempt=attempt,
                    outcome=outcome,
                    detail=detail,
                    durable=durable,
                ),
                detail=None if detail is None else _inline(detail),
            )
        )
        if agent is not None:
            rendered = render_supervisor_audit(
                run_id=run_id,
                bundle=bundle,
                results=verdicts,
                generated_at=clock.isoformat(),
                agent=agent,
            )
            return AgentAuditOutcome(
                audit=rendered,
                attempts=tuple(attempts),
                agent_covered=True,
                mutation_issue_id=mutation_issue_id,
                agent=agent,
                catalog=catalog,
                identity_selections=_identity_selections(agent, rendered.findings),
            )
        if _refused_for_size(attempt) and (
            shrinks_spent < assurance_models.MAX_AUDIT_SHRINK_ATTEMPTS
        ):
            # The provider refused the request before the agent ran, so there is
            # no answer to correct and no outage to wait out: what has to change
            # is how much was sent. Retry immediately with a bundle shed further,
            # and only if shedding actually gave something up — an unshrinkable
            # bundle would otherwise buy a second, identically refused request.
            # Measured in bytes, which is what the provider refused, rather than
            # by digest: a pass that drops nothing still restates the omissions
            # it inherited, so digests differ where the evidence does not.
            refused_bytes = len(prompt_bundle.to_bytes())
            tightened = tighten_audit_input(
                prompt_bundle,
                max_bytes=int(refused_bytes * AUDIT_INPUT_OVERFLOW_SHRINK_RATIO),
            )
            if len(tightened.to_bytes()) < refused_bytes:
                shrinks_spent += 1
                prompt_bundle = tightened
                continue
        if outcome in RETRYABLE_DELIVERY_OUTCOMES and _delivery_is_retryable(attempt):
            # The agent never saw this prompt, so nothing about it is corrected
            # and no candidate offer is consumed: the next launch repeats it
            # verbatim once the provider has had time to recover.
            deliveries_spent += 1
            if deliveries_spent >= assurance_models.MAX_AUDIT_DELIVERY_ATTEMPTS:
                break
            delay = AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS[
                min(deliveries_spent - 1, len(AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS) - 1)
            ]
            _record_capacity_outage(
                attempt, role=role, state_dir=state_dir, run_id=run_id, delay=delay
            )
            _sleep_fn(delay)
            continue
        if unavailable_reason == REASON_IDENTITY_AMBIGUOUS and not matched:
            break
        if outcome not in RETRYABLE_ANSWER_OUTCOMES:
            break
        answers_spent += 1
        if answers_spent >= assurance_models.MAX_AUDIT_ATTEMPTS:
            break
        if matched:
            correction = None
            candidates = matched
            candidates_offered = True
            catalog = widen_agent_catalog(
                catalog, inbox, [candidate.issue_id for candidate in matched]
            )
        else:
            correction = detail
            candidates = ()

    return AgentAuditOutcome(
        audit=render_supervisor_audit(
            run_id=run_id,
            bundle=bundle,
            results=verdicts,
            generated_at=clock.isoformat(),
            reason=unavailable_reason,
        ),
        attempts=tuple(attempts),
        agent_covered=False,
        mutation_issue_id=mutation_issue_id,
        catalog=catalog,
    )


def _deterministic_only_audit(
    bundle: AuditInput, *, run_id: str, generated_at: str
) -> AgentAuditOutcome:
    """Grade a run with no independent auditor available.

    The reproducible checks are worth running on their own, so what is missing
    is the semantic half rather than the audit. The verdict therefore stays
    ``unavailable`` under the missing role while carrying every lane, result,
    and finding the checks reached.
    """
    return AgentAuditOutcome(
        audit=render_supervisor_audit(
            run_id=run_id,
            bundle=bundle,
            results=run_deterministic_checks(bundle),
            generated_at=generated_at,
            reason=REASON_NO_INDEPENDENT_ROLE,
        ),
        attempts=(),
        agent_covered=False,
    )


def _artifact_reference(
    path: Path, run_dir: Path, digest: str
) -> assurance_models.EvidenceRef:
    return assurance_models.EvidenceRef(
        kind="artifact",
        locator=f"{EVIDENCE_ROOT_RUN}/{path.relative_to(run_dir).as_posix()}",
        digest=digest,
    )


def _publish_evidence_failure(
    *,
    run_id: str,
    run_dir: Path,
    role: AuditRole | None,
    detail: str,
    started: datetime,
    completed: datetime,
    ledger_state_dir: Path | str | None,
    durable: bool,
) -> AssuranceSummary | None:
    """Record an audit that never reached an evidence bundle, or ``None``.

    The failure itself becomes the artifact, because an unavailable audit that
    names nothing opens an issue nobody can investigate. When even that write
    or the ledger append fails there is no durable record to point at, and the
    caller degrades to ``pending`` rather than claiming one exists.
    """
    path = run_dir / UNAVAILABLE_ARTIFACT_NAME
    try:
        recorded = assurance_models.canonical_json_bytes(
            {
                "run_id": run_id,
                "reason": REASON_EVIDENCE_UNAVAILABLE,
                "detail": _inline(detail),
                "recorded_at": completed.isoformat(),
            }
        )
        assurance_models.write_new_bytes(path, recorded, durable=durable)
        result = assurance_projection.publish_audit(
            assurance_projection.AuditPublication(
                run_id=run_id,
                state="unavailable",
                eligible=True,
                reason=REASON_EVIDENCE_UNAVAILABLE,
                role=role,
                artifacts=(
                    _artifact_reference(path, run_dir, artifacts.sha256_hex(recorded)),
                ),
                started_at=started.isoformat(),
                completed_at=completed.isoformat(),
                duration_seconds=max(0.0, (completed - started).total_seconds()),
            ),
            state_dir=ledger_state_dir,
        )
    except OSError, assurance_models.AssuranceError:
        return None
    return result.summary


def audit_terminal_run(
    plan: AuditPlan,
    *,
    run_id: str,
    run_dir: Path | str,
    state_dir: Path | str,
    plan_dir: Path | str,
    workspace_root: Path | str,
    repositories: Sequence[str],
    registry_declared: bool = True,
    ledger_state_dir: Path | str | None = None,
    state_payload: Mapping[str, Any] | None = None,
    workspace_snapshot: Mapping[str, Any] | None = None,
    stream_callback: harnesses.StreamCallback | None = None,
    abort: harnesses.AbortSignal | None = None,
    durable: bool = True,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
) -> AssuranceSummary:
    """Audit one terminating run and publish the verdict, returning its summary.

    The only failure this raises is :class:`AuditCancelled`, and only when the
    caller armed ``abort``: an audit is advisory, so a failure to produce or
    record one degrades the run's assurance rather than its outcome. A run whose
    artifacts exist but whose ledger publication failed comes back ``pending``,
    because the artifacts alone are not the durable record an operator reads —
    claiming otherwise would report an all-clear nobody can look up.

    Opting out is the only way to run no checks at all. A run that stayed
    enabled but resolved no independent role still gets the reproducible half:
    the deterministic checks need no agent, so the audit lands ``unavailable``
    for the missing role while publishing whatever they proved.

    The bundle is collected with ``terminal_pending`` set: the caller runs this
    before writing the run's terminal record, which is what lets the audit's own
    artifacts and ledger records precede it.

    ``state_dir`` is the run's own plan state directory, where the evidence
    lives; ``ledger_state_dir`` is the state root the one shared assurance
    ledger sits in. They are different directories and conflating them would
    scatter the inbox one file per plan.
    """
    clock = (lambda: datetime.now(UTC)) if now is None else now
    if not plan.config.enabled:
        return initial_summary(plan, run_id=run_id, eligible=True)

    role = plan.role
    run_dir = Path(run_dir)
    started = clock()

    def pending(
        artifacts: Sequence[assurance_models.EvidenceRef] = (),
    ) -> AssuranceSummary:
        return AssuranceSummary(
            run_id=run_id,
            state="pending",
            eligible=True,
            role=role,
            artifacts=tuple(artifacts),
            started_at=started.isoformat(),
        )

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        bundle = collect_audit_input(
            run_id=run_id,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace_root,
            state_payload=state_payload,
            workspace_snapshot=workspace_snapshot,
            terminal_pending=True,
        )
    except (OSError, assurance_models.AssuranceError) as error:
        recorded = _publish_evidence_failure(
            run_id=run_id,
            run_dir=run_dir,
            role=role,
            detail=str(error),
            started=started,
            completed=clock(),
            ledger_state_dir=ledger_state_dir,
            durable=durable,
        )
        return pending() if recorded is None else recorded

    try:
        input_path = bundle.write(run_dir, durable=durable)
        if role is None:
            outcome = _deterministic_only_audit(
                bundle, run_id=run_id, generated_at=clock().isoformat()
            )
        else:
            # Pinned after the bundle is written: collecting the evidence reads
            # the live workspace the run actually left behind, while grading it
            # is judged on a checkout no concurrent run can touch.
            with pinned_audit_workspace(
                workspace_root,
                repositories,
                run_commits(run_dir, repositories),
            ) as pinned:
                outcome = run_agent_audit(
                    role,
                    bundle=bundle,
                    run_id=run_id,
                    run_dir=run_dir,
                    workspace_root=workspace_root,
                    plan_dir=plan_dir,
                    repositories=repositories,
                    registry_declared=registry_declared,
                    pinned=pinned,
                    state_dir=ledger_state_dir,
                    stream_callback=stream_callback,
                    abort=abort,
                    durable=durable,
                    now=now,
                    _run_harness_fn=_run_harness_fn,
                )
        staged = stage_audit_reports(run_dir, outcome.audit, durable=durable)
        input_reference = _artifact_reference(input_path, run_dir, bundle.sha256)
        audit_artifacts = (input_reference, *staged.references)
    except OSError, assurance_models.AssuranceError:
        return pending()

    completed = clock()
    try:
        result = assurance_projection.publish_audit(
            assurance_projection.AuditPublication(
                run_id=run_id,
                state=outcome.audit.state,
                eligible=True,
                findings=outcome.audit.findings,
                reason=outcome.audit.reason,
                role=role,
                attempts=outcome.attempts,
                artifacts=audit_artifacts,
                linked_issue_ids=outcome.linked_issue_ids,
                started_at=started.isoformat(),
                completed_at=completed.isoformat(),
                duration_seconds=max(0.0, (completed - started).total_seconds()),
                identity_selections=outcome.identity_selections,
                catalog_digest=outcome.catalog_digest,
            ),
            state_dir=ledger_state_dir,
            promote=staged.promote,
        )
    except OSError, assurance_models.AssuranceError:
        # Promotion runs inside the append lock, so a failure after it leaves
        # canonical reports no record names. Releasing them keeps the pending
        # summary honest, since it names only the evidence bundle, and leaves the
        # canonical names free for the attempt that does publish.
        staged.unpromote()
        return pending((input_reference,))
    finally:
        staged.discard()
    return result.summary


def status_line(
    assurance: Mapping[str, Any] | None,
    *,
    report: Mapping[str, Any] | None = None,
) -> str | None:
    """Render the one-line operator summary of a run's assurance object.

    Delegated so that ``asl status`` — which must never be able to launch an
    audit — renders the identical line without importing this module.
    """
    return assurance_report.status_line(assurance, report=report)
