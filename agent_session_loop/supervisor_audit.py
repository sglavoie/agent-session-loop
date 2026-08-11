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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    assurance_ledger,
    assurance_models,
    events,
    harnesses,
    repository_evidence,
    workspaces,
)
from .assurance_models import (
    MAX_AUDIT_ATTEMPTS,
    AssuranceSummary,
    AttemptOutcome,
    AuditRole,
    AuditState,
    Finding,
    IssueObservation,
)

#: Seconds granted to each agent attempt, not to the audit as a whole.
DEFAULT_AUDIT_TIMEOUT_SECONDS = 600

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

#: The auditor's working directory, under the run directory so it is captured
#: with the rest of the run's evidence and never sits inside the workspace.
AUDIT_WORKDIR_NAME = "audit-workdir"

#: Run-relative artifact naming the mutation a read-only audit was not supposed
#: to be able to cause. It is the finding's only evidence reference.
MUTATION_ARTIFACT_NAME = "supervisor-audit-mutation.json"


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
        source = "explicit"
        inherited_model: str | None = None
    elif review_harness is not None:
        harness = review_harness
        source = "review"
        inherited_model = review_model
    else:
        return None
    if harness == implementation_harness:
        return None
    return AuditRole(
        harness=harness,
        implementation_harness=implementation_harness,
        source=source,
        timeout_seconds=float(config.timeout_seconds),
        model=config.model if config.model is not None else inherited_model,
        effort=config.effort,
    )


def attempt_timeouts(role: AuditRole) -> tuple[float, ...]:
    """Return the per-attempt timeout budget.

    The timeout is not a total budget: every one of the at most
    ``MAX_AUDIT_ATTEMPTS`` attempts gets the full value, so a first attempt that
    burns its timeout does not shorten the retry.
    """
    return tuple(role.timeout_seconds for _ in range(MAX_AUDIT_ATTEMPTS))


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


def _mutation_comparison(comparison: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the comparison when it proves a change, else ``None``.

    Unavailable repository evidence is not a mutation: it is the absence of
    proof either way, and treating it as one would turn every transient git
    failure into a fabricated integrity issue.
    """
    changed = comparison.get("changed") or {}
    added = comparison.get("added") or []
    removed = comparison.get("removed") or []
    if not (changed or added or removed):
        return None
    return {
        "changed": dict(changed),
        "added": list(added),
        "removed": list(removed),
        "unavailable": dict(comparison.get("unavailable") or {}),
    }


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
    except (OSError, assurance_models.AssuranceError):
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
    except (OSError, assurance_ledger.LedgerError, assurance_models.AssuranceError):
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
    state_dir: Path | str | None = None,
    timeout: int | None = None,
    inventory_excludes: Sequence[str] = (),
    stream_callback: harnesses.StreamCallback | None = None,
    now: Callable[[], datetime] | None = None,
    _run_harness_fn: Callable[..., harnesses.HarnessResult] | None = None,
) -> AuditAttemptResult:
    """Run one audit attempt under the read-only profile, guarded by evidence.

    The workspace is fingerprinted on both sides of the turn. Any change makes
    the audit unavailable regardless of what the auditor reported, because a
    read-only turn that mutated anything has already disproved the boundary its
    verdict depends on.
    """
    run_dir = Path(run_dir)
    unavailable = harnesses.audit_read_only_unavailable_reason(role.harness)
    if unavailable is not None:
        return AuditAttemptResult(
            outcome="unavailable", state="unavailable", reason=unavailable
        )

    def snapshot() -> repository_evidence.WorkspaceSnapshot:
        return repository_evidence.capture_workspace_snapshot(
            workspace_root,
            plan_dir,
            inventory_excludes=tuple(inventory_excludes),
        )

    try:
        before = snapshot()
    except repository_evidence.RepositoryEvidenceError:
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
        clock = datetime.now(timezone.utc) if now is None else now()
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
        return AuditAttemptResult(
            outcome="transport-error",
            state="unavailable",
            reason=REASON_HARNESS_FAILED,
            argv=argv,
            returncode=result.returncode,
            last_message=result.last_message,
        )
    return AuditAttemptResult(
        outcome="ok",
        state="pending",
        argv=argv,
        returncode=result.returncode,
        last_message=result.last_message,
    )


def _format_timeout(value: Any) -> str:
    if isinstance(value, (int, float)) and float(value).is_integer():
        return f"{int(value)}s"
    return f"{value}s"


def status_line(assurance: Mapping[str, Any] | None) -> str | None:
    """Render the one-line operator summary of a run's assurance object."""
    if not isinstance(assurance, Mapping):
        return None
    state = assurance.get("state")
    if not state:
        return None
    role = assurance.get("role")
    if isinstance(role, Mapping) and role.get("harness"):
        detail = (
            f" ({role.get('harness')} via {role.get('source')} role, "
            f"{_format_timeout(role.get('timeout_seconds'))} per attempt)"
        )
    elif assurance.get("reason"):
        detail = f" ({assurance.get('reason')})"
    else:
        detail = ""
    return f"Supervisor audit: {state}{detail}"
