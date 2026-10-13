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

This is the entry module of the supervisor audit and the only one the run
loop and the assurance commands call: it plans the audit, drives the attempt
loop, and publishes the terminal audit through :func:`audit_terminal_run`. The
work it orchestrates lives below it, and dependencies flow one way:
:mod:`.supervisor_audit_evidence` collects the bounded evidence bundle,
:mod:`.supervisor_audit_checks` grades it deterministically, and
:mod:`.supervisor_audit_agent` owns the auditor's prompt and answer contract,
identity resolution, and the rendered reports.
"""

from __future__ import annotations

import argparse
import contextlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    assurance_models,
    assurance_projection,
    assurance_report,
    events,
    harness_failures,
    harnesses,
    model_availability,
    repository_evidence,
    state,
    supervisor_audit_vocabulary,
)
from . import supervisor_audit_agent as audit_agent
from . import supervisor_audit_checks as audit_checks
from . import supervisor_audit_evidence as audit_evidence
from .assurance_models import (
    MAX_AUDIT_ATTEMPT_RECORDS,
    AssuranceError,
    AssuranceSummary,
    AttemptOutcome,
    AuditRole,
    AuditRoleSource,
    AuditState,
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
REASON_WORKSPACE_CHANGED = "workspace-changed-during-audit"
REASON_WORKSPACE_EVIDENCE_UNAVAILABLE = "workspace-evidence-unavailable"
REASON_HARNESS_FAILED = "harness-failed"
REASON_QUOTA_EXHAUSTED = "quota-exhausted"
REASON_TIMEOUT = "timeout"

#: Classifier reasons that earn an availability class of their own rather than
#: landing in :data:`REASON_HARNESS_FAILED`. The set is deliberately one entry
#: wide. ``harness-failed`` is the class for "the auditor was launched and the
#: launch broke", and every issue filed under it is read as an ASL defect worth
#: investigating. A provider refusing the request because the account is out of
#: quota is not that: nothing about the audit, the run, or ASL is wrong, the
#: condition clears on a clock nobody here controls, and the standing
#: recommendation to re-run the audit cannot be followed until it does (ASI-127).
#: A second entry belongs here only for a condition with the same three
#: properties -- not ASL's doing, not retryable, and cleared by an operator or a
#: clock rather than by a re-run -- and only once a run has actually recorded it,
#: because an availability class nothing files is a name with no evidence behind
#: it.
_DISTINCT_AVAILABILITY_REASONS: Mapping[str, str] = {
    events.PAUSE_REASON_QUOTA_EXHAUSTED: REASON_QUOTA_EXHAUSTED,
}

#: Why an audit produced no verdict before any auditor could be launched.
REASON_EVIDENCE_UNAVAILABLE = "evidence-unavailable"
REASON_INTERRUPTED = "interrupted"

#: The auditor's working directory, under the run directory so it is captured
#: with the rest of the run's evidence and never sits inside the workspace.
AUDIT_WORKDIR_NAME = "audit-workdir"

#: Run-relative artifact naming an audit that never reached an evidence bundle.
#: An unavailable audit has to point a reader at something it can open, and a
#: collection failure has no bundle and no attempt directory to offer.
UNAVAILABLE_ARTIFACT_NAME = "supervisor-audit-unavailable.json"


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
    workspace_change: dict[str, Any] | None = None
    classification: harness_failures.FailureClassification | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


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
    """Drop remote-tracking ref moves from the workspace consistency check.

    These cache refs can move whenever another checkout fetches, without
    changing a file, local branch, or commit in the audited workspace (ASI-13).
    Local branches and tags still invalidate the audit when they change.
    """
    filtered = dict(changes)
    for key in ("refs_added", "refs_removed", "refs_changed"):
        entries = audit_evidence._sequence(changes.get(key))
        if not entries:
            continue
        filtered[key] = [
            entry
            for entry in entries
            if not str(audit_evidence._mapping(entry).get("ref") or "").startswith(
                audit_evidence._REMOTE_TRACKING_REF_PREFIX
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
        for repository, raw in audit_evidence._mapping(
            comparison.get("changed")
        ).items()
        for filtered in (_without_remote_tracking_refs(audit_evidence._mapping(raw)),)
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


def _record_workspace_change(
    comparison: Mapping[str, Any],
    *,
    role: AuditRole,
    run_dir: Path,
    run_id: str,
    observed_at: str,
) -> None:
    """Record observed activity without inventing a writer's identity.

    Snapshots prove a change, not who caused it. Recording is best effort:
    losing this artifact cannot turn the already-unavailable audit into a
    usable verdict. The attempt archive also retains the comparison.
    """
    with contextlib.suppress(OSError, assurance_models.AssuranceError):
        assurance_models.write_audit_json(
            run_dir,
            supervisor_audit_vocabulary.WORKSPACE_CHANGE_ARTIFACT_NAME,
            {
                "run_id": run_id,
                "observed_at": observed_at,
                "role": role.to_dict(),
                "attribution": {
                    "status": "unattributed",
                    "reason": "snapshot-comparison-has-no-writer-identity",
                },
                "comparison": comparison,
            },
        )


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
    state_dir: Path | str | None = None,
    timeout: int | None = None,
    stream_callback: harnesses.StreamCallback | None = None,
    abort: harnesses.AbortSignal | None = None,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
) -> AuditAttemptResult:
    """Run read-only and reject a verdict over a changing live workspace.

    ``repositories`` holds the workspace-relative paths the plan registered,
    and they alone are fingerprinted on both sides of the turn. Any change to
    one of them makes the audit unavailable regardless of what the auditor
    reported. Snapshots establish activity, never a writer's identity: an
    auditor escaping its profile and an unrelated concurrent edit produce the
    same evidence, so neither is accepted as a stable audit or charged to the
    auditor as a critical breach. A registered repository the snapshot cannot
    name at all, one whose evidence is already
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

    The comparison always covers the live workspace, including linked worktrees.
    A private pin the auditor cannot reach observes no writer and would miss
    escaped writes to the live tree, even when the run recorded commits.

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

    def snapshot() -> repository_evidence.WorkspaceSnapshot:
        return repository_evidence.capture_registered_snapshot(
            Path(workspace_root),
            plan_dir,
            tuple(repositories),
            include_linked_worktrees=True,
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

    change = _mutation_comparison(
        repository_evidence.compare_workspace_snapshots(before, after)
    )
    if change is not None:
        change["workspace"] = {"root": str(Path(workspace_root))}
        clock = datetime.now(UTC) if now is None else now()
        _record_workspace_change(
            change,
            role=role,
            run_dir=run_dir,
            run_id=run_id,
            observed_at=clock.isoformat(),
        )
        return AuditAttemptResult(
            outcome="workspace-changed",
            state="unavailable",
            reason=REASON_WORKSPACE_CHANGED,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
            workspace_change=change,
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
        # After the consistency check, so workspace activity during a cancelled
        # attempt is still reported without attribution; before every failure branch,
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
        # that carried the provider's own words are kept alongside it. A few of
        # those classifications name a condition that is not a failure of the
        # audit at all, and those keep an availability class of their own rather
        # than being filed as one -- see `_DISTINCT_AVAILABILITY_REASONS`.
        classification = _redacted_classification(result)
        return AuditAttemptResult(
            outcome="transport-error",
            state="unavailable",
            reason=_availability_reason(classification),
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
            classification=classification,
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


def _redacted_classification(
    result: harnesses.HarnessResult,
) -> harness_failures.FailureClassification:
    """Classify a failed launch, with credentials stripped from its evidence."""
    classification = harness_failures.classify_harness_failure(result)
    return replace(
        classification, evidence=audit_evidence._redact_secrets(classification.evidence)
    )


def _availability_reason(
    classification: harness_failures.FailureClassification,
) -> str:
    """Name the availability class a failed launch belongs in.

    Most failed launches are ``harness-failed``, which is the class for "the
    auditor was launched and the launch broke" and is read as an ASL defect
    worth investigating. A handful of classifications say something else
    entirely -- the account is out of quota -- and filing those as harness
    failures reported an outage nobody here caused as a defect, under a
    recommendation to re-run the audit that could not be followed until the
    quota reset (ASI-127). The class is taken from the classifier's own reason
    rather than re-matched here, so the audit and the session loop cannot
    disagree about what the provider said.
    """
    return _DISTINCT_AVAILABILITY_REASONS.get(
        classification.reason, REASON_HARNESS_FAILED
    )


def _harness_tail(text: str) -> str:
    """Return the redacted end of a failing harness stream.

    The end, not the head: a harness prints its banner first and its error last,
    so a head-truncated capture of a chatty run keeps everything except the one
    line explaining why it died.
    """
    if not text:
        return ""
    encoded = audit_evidence._redact_secrets(text).encode("utf-8")
    if len(encoded) <= MAX_HARNESS_TAIL_BYTES:
        return encoded.decode("utf-8", errors="ignore")
    return encoded[-MAX_HARNESS_TAIL_BYTES:].decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Running the agent audit
# ---------------------------------------------------------------------------

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

#: Attempt outcomes that earn a retry at all. Observed workspace activity, an
#: unenforceable profile, and an interrupt are absent on purpose: none is a
#: transient failure, and retrying would either run a second turn under a
#: workspace already known to be changing or answer the operator's Ctrl+C with more
#: of what they interrupted.
RETRYABLE_ATTEMPT_OUTCOMES: frozenset[str] = (
    RETRYABLE_DELIVERY_OUTCOMES | RETRYABLE_ANSWER_OUTCOMES
)

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


@dataclass(frozen=True)
class AgentAuditOutcome:
    """The audit an engine run produced, with the attempts that produced it."""

    audit: assurance_models.SupervisorAudit
    attempts: tuple[assurance_models.AuditAttempt, ...]
    agent_covered: bool
    #: The validated answer behind ``audit``, kept because the canonical audit
    #: carries no identity: allocation reads the decisions from here.
    agent: audit_agent.AgentAudit | None = None
    #: The catalog those decisions were made against, whose digest names the
    #: identity state they are valid for.
    catalog: audit_agent.AgentCatalog | None = None
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
        f"[prompt sha256:{audit_evidence._sha256_text(prompt)}]"
        if item == prompt
        else item
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

    Best effort by design, exactly like the workspace-change artifact: losing the
    archive of an attempt must not lose the verdict the attempt produced.
    """
    try:
        directory = assurance_models.create_attempt_dir(run_dir, number)
        assurance_models.write_new_bytes(
            directory / ATTEMPT_PROMPT_FILE, prompt.encode("utf-8"), durable=durable
        )
        assurance_models.write_new_bytes(
            directory / ATTEMPT_MESSAGE_FILE,
            audit_evidence._truncate_utf8(
                attempt.last_message, audit_agent.MAX_AGENT_OUTPUT_BYTES
            ).encode("utf-8"),
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
                    "workspace_change": attempt.workspace_change,
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
    bundle: audit_evidence.AuditInput,
    run_id: str,
    run_dir: Path | str,
    workspace_root: Path | str,
    plan_dir: Path | str,
    repositories: Sequence[str],
    registry_declared: bool = True,
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

    Every attempt checks the live workspace on both sides of its own turn.
    Observed activity invalidates the verdict without attributing that activity
    to the auditor; it is not retried automatically over a changing workspace.
    """
    run_dir = Path(run_dir)
    clock = datetime.now(UTC) if now is None else now()
    verdicts = (
        tuple(results)
        if results is not None
        else audit_checks.run_deterministic_checks(bundle)
    )
    declared = [entry.locator for entry in bundle.evidence]
    # Read once, before the first attempt: both attempts must be judged against
    # the catalog they were shown, and re-reading between them could reject a
    # selection for naming an issue a concurrent publisher had just moved.
    inbox = audit_agent.read_identity_catalog(state_dir)
    catalog = audit_agent.build_agent_catalog(inbox)
    attempts: list[assurance_models.AuditAttempt] = []
    correction: str | None = None
    candidates: tuple[assurance_projection.IdentityCandidate, ...] = ()
    candidates_offered = False
    unavailable_reason = audit_agent.REASON_AGENT_UNAVAILABLE
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
        prompt = audit_agent.build_audit_prompt(
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
            state_dir=state_dir,
            stream_callback=stream_callback,
            abort=abort,
            now=now,
            _run_harness_fn=_run_harness_fn,
        )
        agent: audit_agent.AgentAudit | None = None
        matched: tuple[assurance_projection.IdentityCandidate, ...] = ()
        outcome: AttemptOutcome
        detail: str | None
        if attempt.outcome == "ok":
            try:
                answer = audit_agent.parse_agent_audit(
                    attempt.last_message, declared=declared, catalog=catalog
                )
            except audit_agent.AgentOutputError as error:
                outcome, detail = error.outcome, error.detail
                unavailable_reason = error.outcome
            else:
                resolution = audit_agent.resolve_identities(answer, catalog=inbox)
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
                detail=None if detail is None else audit_agent._inline(detail),
            )
        )
        if agent is not None:
            rendered = audit_agent.render_supervisor_audit(
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
                agent=agent,
                catalog=catalog,
                identity_selections=audit_agent._identity_selections(
                    agent, rendered.findings
                ),
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
            tightened = audit_evidence.tighten_audit_input(
                prompt_bundle,
                max_bytes=int(
                    refused_bytes * audit_evidence.AUDIT_INPUT_OVERFLOW_SHRINK_RATIO
                ),
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
            catalog = audit_agent.widen_agent_catalog(
                catalog, inbox, [candidate.issue_id for candidate in matched]
            )
        else:
            correction = detail
            candidates = ()

    return AgentAuditOutcome(
        audit=audit_agent.render_supervisor_audit(
            run_id=run_id,
            bundle=bundle,
            results=verdicts,
            generated_at=clock.isoformat(),
            reason=unavailable_reason,
        ),
        attempts=tuple(attempts),
        agent_covered=False,
        catalog=catalog,
    )


def _deterministic_only_audit(
    bundle: audit_evidence.AuditInput, *, run_id: str, generated_at: str
) -> AgentAuditOutcome:
    """Grade a run with no independent auditor available.

    The reproducible checks are worth running on their own, so what is missing
    is the semantic half rather than the audit. The verdict therefore stays
    ``unavailable`` under the missing role while carrying every lane, result,
    and finding the checks reached.
    """
    return AgentAuditOutcome(
        audit=audit_agent.render_supervisor_audit(
            run_id=run_id,
            bundle=bundle,
            results=audit_checks.run_deterministic_checks(bundle),
            generated_at=generated_at,
            reason=REASON_NO_INDEPENDENT_ROLE,
        ),
        attempts=(),
        agent_covered=False,
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
                "detail": audit_agent._inline(detail),
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
                    audit_agent._artifact_reference(
                        path, run_dir, artifacts.sha256_hex(recorded)
                    ),
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
        bundle = audit_evidence.collect_audit_input(
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
            outcome = run_agent_audit(
                role,
                bundle=bundle,
                run_id=run_id,
                run_dir=run_dir,
                workspace_root=workspace_root,
                plan_dir=plan_dir,
                repositories=repositories,
                registry_declared=registry_declared,
                state_dir=ledger_state_dir,
                stream_callback=stream_callback,
                abort=abort,
                durable=durable,
                now=now,
                _run_harness_fn=_run_harness_fn,
            )
        staged = audit_agent.stage_audit_reports(
            run_dir, outcome.audit, durable=durable
        )
        input_reference = audit_agent._artifact_reference(
            input_path, run_dir, bundle.sha256
        )
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
