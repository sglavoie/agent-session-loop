"""Focused implementation extracted from :mod:`.supervisor_audit`."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    events,
    harnesses,
)
from .assurance_models_primitives import AssuranceError
from .assurance_models_summary import AssuranceSummary, AuditRole
from .assurance_models_vocab import (
    MAX_AUDIT_ATTEMPT_RECORDS,
    AuditRoleSource,
    AuditState,
)
from .harnesses import HARNESS_NAMES
from .literals import as_literal

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
:mod:`.supervisor_audit_evidence_input` collects the bounded evidence bundle,
:mod:`.supervisor_audit_checks` grades it deterministically, and
:mod:`.supervisor_audit_agent` owns the auditor's prompt and answer contract,
identity resolution, and the rendered reports.
"""

DEFAULT_AUDIT_TIMEOUT_SECONDS = 600

AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (15.0, 60.0, 120.0)

REASON_DRY_RUN = "dry-run"

REASON_OPTED_OUT = "opted-out"

REASON_NO_INDEPENDENT_ROLE = "no-independent-role"

REASON_WORKSPACE_CHANGED = "workspace-changed-during-audit"

REASON_WORKSPACE_EVIDENCE_UNAVAILABLE = "workspace-evidence-unavailable"

REASON_HARNESS_FAILED = "harness-failed"

REASON_QUOTA_EXHAUSTED = "quota-exhausted"

REASON_TIMEOUT = "timeout"

_DISTINCT_AVAILABILITY_REASONS: Mapping[str, str] = {
    events.PAUSE_REASON_QUOTA_EXHAUSTED: REASON_QUOTA_EXHAUSTED,
}

REASON_EVIDENCE_UNAVAILABLE = "evidence-unavailable"

REASON_INTERRUPTED = "interrupted"

AUDIT_WORKDIR_NAME = "audit-workdir"

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
