"""Focused implementation extracted from :mod:`.supervisor_audit`."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    assurance_models_artifacts,
    assurance_models_findings,
    assurance_models_primitives,
    assurance_projection,
    assurance_report,
    harnesses,
)
from . import supervisor_audit_agent as audit_agent
from . import supervisor_audit_checks as audit_checks
from . import supervisor_audit_evidence_input as audit_evidence_input
from . import supervisor_audit_evidence_models as audit_evidence_models
from .assurance_models_summary import AssuranceSummary, AuditRole
from .supervisor_audit_agent_models import (
    AgentAuditOutcome,
)
from .supervisor_audit_agent_run import (
    run_agent_audit,
)
from .supervisor_audit_config import (
    REASON_EVIDENCE_UNAVAILABLE,
    REASON_NO_INDEPENDENT_ROLE,
    UNAVAILABLE_ARTIFACT_NAME,
    AuditPlan,
    initial_summary,
)


def _deterministic_only_audit(
    bundle: audit_evidence_models.AuditInput, *, run_id: str, generated_at: str
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
        recorded = assurance_models_primitives.canonical_json_bytes(
            {
                "run_id": run_id,
                "reason": REASON_EVIDENCE_UNAVAILABLE,
                "detail": audit_agent._inline(detail),
                "recorded_at": completed.isoformat(),
            }
        )
        assurance_models_artifacts.write_new_bytes(path, recorded, durable=durable)
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
    except OSError, assurance_models_primitives.AssuranceError:
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
        artifacts: Sequence[assurance_models_findings.EvidenceRef] = (),
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
        bundle = audit_evidence_input.collect_audit_input(
            run_id=run_id,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace_root,
            state_payload=state_payload,
            workspace_snapshot=workspace_snapshot,
            terminal_pending=True,
        )
    except (OSError, assurance_models_primitives.AssuranceError) as error:
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
    except OSError, assurance_models_primitives.AssuranceError:
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
    except OSError, assurance_models_primitives.AssuranceError:
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
