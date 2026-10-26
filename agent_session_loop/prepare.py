"""End-to-end ``asl prepare`` orchestration.

:func:`prepare_plan` drives the autonomous pipeline: deterministic authoring
(:mod:`agent_session_loop.authoring_split`), validation through the validator
packets and fail-closed ingest, exact factual correction, baseline execution
(:mod:`agent_session_loop.baseline`), and a final ``--mark-loop-ready``
preflight through :mod:`agent_session_loop.preflight`.

The stages live in the ``prepare_*`` modules:

- ``prepare_models`` / ``prepare_parsing``: validator result schema and its
  fail-closed parser.
- ``prepare_packet``: bounded validator task packets.
- ``prepare_validation_cache`` / ``prepare_validation_store``: the per-session
  cache and the derived ``prepare-validation.json`` aggregate.
- ``prepare_expansion`` / ``prepare_corrections`` / ``prepare_expand_ingest``:
  ``session-<id>.expand.md`` rendering, exact factual corrections, and the
  atomic ``asl expand-ingest`` batch.
- ``prepare_validator`` / ``prepare_validation_support`` /
  ``prepare_validation_stage``: running validator workers and reusing cached
  results.
- ``prepare_structure`` / ``prepare_baseline`` / ``prepare_commands`` /
  ``prepare_rebind`` / ``prepare_results``: the remaining stages and records.
- ``prepare_cli``: argparse handlers and output rendering.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import (
    authoring_parse,
    authoring_source_models,
    contracts,
    harnesses,
    plans_registry,
    preflight,
    preflight_cli,
    repository_intent,
)
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_snapshot import inspect_plan
from .prepare_baseline import _run_baseline_stage
from .prepare_commands import _format_resume_command, _format_run_command
from .prepare_corrections import CorrectionOutcome
from .prepare_models import (
    CONTRACT_PROBLEM_CATEGORY,
    CORRECTION_ALREADY_APPLIED,
    CORRECTION_APPLIED,
    CORRECTION_BLOCKED,
    DEFAULT_VALIDATION_BATCH_SIZE,
    DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
    DEFAULT_VALIDATOR_JOBS,
    DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
    REPOSITORY_AMBIGUITY_ISSUE,
    RepositoryRegistryRequiredError,
)
from .prepare_rebind import _prepare_workspace_rebind, _workspace_rebind_summary
from .prepare_results import (
    PREPARE_EXIT_REASON_BASELINE_UNSATISFIED,
    PREPARE_EXIT_REASON_NEEDS_PLAN_ITERATION,
    PREPARE_EXIT_REASON_NOT_LOOP_READY,
    PREPARE_EXIT_REASON_OK,
    PREPARE_EXIT_REASON_REGISTRY_REQUIRED,
    PREPARE_EXIT_REASON_VALIDATION_FAILED,
    PREPARE_STAGE_AUTHORING,
    PREPARE_STAGE_BASELINE,
    PREPARE_STAGE_CORRECTIONS,
    PREPARE_STAGE_PREFLIGHT,
    PREPARE_STAGE_STRUCTURE,
    PREPARE_STAGE_VALIDATION,
    PREPARE_STEP_FAILED,
    PREPARE_STEP_OK,
    PREPARE_STEP_SKIPPED,
    PrepareBlocker,
    PrepareResult,
    PrepareStep,
)
from .prepare_structure import (
    _author_plan_directory,
    _refresh_draft_readiness,
    _structure_blockers,
)
from .prepare_validation_stage import (
    _contract_problem_findings,
    _repository_ambiguity_findings,
    _run_validation_stage,
)
from .prepare_validation_store import ensure_plan_metadata
from .prepare_validation_support import _positive_validator_control
from .prepare_validator import ValidatorRunner, _launch_validator
from .verification_models import VerificationContext


def prepare_plan(
    source: str | Path | None = None,
    *,
    cwd: str | Path,
    harness: str = "codex",
    harness_model: str | None = None,
    spark: bool = False,
    validator_harness: str = "codex",
    validator_model: str | None = None,
    validator_effort: str | None = None,
    validator_jobs: int = DEFAULT_VALIDATOR_JOBS,
    validator_timeout: int = DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
    validator_heartbeat: int = DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
    review_harness: str | None = None,
    review_harness_model: str | None = None,
    base_dir: str | Path | None = None,
    force_new: bool = False,
    resume_plan_dir: str | Path | None = None,
    validator: ValidatorRunner | None = None,
    batch_size: int = DEFAULT_VALIDATION_BATCH_SIZE,
    unsafe_bypass: bool = True,
    state_dir: str | Path | None = None,
    rebind_workspace: bool = False,
) -> PrepareResult:
    """Run the full preparation pipeline for one source plan (or resume one).

    Stages run strictly in order: structure gate, deterministic authoring,
    structured validation, exact factual correction, baseline execution, and
    the final ``--mark-loop-ready`` preflight. A source plan without at least
    two explicit session headings and complete per-session contracts returns
    ``needs-plan-iteration`` before any output directory is created. Any later
    failure preserves the authored directory as ``draft`` with structured
    blockers and a resumable ``asl prepare --resume`` command.

    ``resume_plan_dir`` reuses only cache entries whose complete per-session
    identity still matches, revalidates every stale session, and reuses
    baseline evidence whose repository fingerprint still matches — all against
    an existing split directory without creating another split.
    ``rebind_workspace`` is valid only with a registered resumed plan and
    delegates to an isolated full replacement pipeline whose successful
    artifacts publish through one transaction.
    """
    canonical_cwd = canonicalize_path(cwd)
    validator_jobs = _positive_validator_control(validator_jobs, name="validator jobs")
    validator_timeout = _positive_validator_control(
        validator_timeout, name="validator timeout"
    )
    validator_heartbeat = _positive_validator_control(
        validator_heartbeat, name="validator heartbeat"
    )
    if validator is None:

        def selected_validator(packet):
            return _launch_validator(
                validator_harness,
                packet,
                model=validator_model,
                effort=validator_effort,
                timeout=validator_timeout,
            )
    else:
        selected_validator = validator

    if rebind_workspace:
        if resume_plan_dir is None:
            raise PlanError("--rebind-workspace requires --resume")
        registry = plans_registry.read_repository_registry(resume_plan_dir)
        if registry is None:
            raise PlanError("--rebind-workspace requires a registered plan")
        return _prepare_workspace_rebind(
            resume_plan_dir,
            cwd=canonical_cwd,
            harness=harness,
            harness_model=harness_model,
            spark=spark,
            validator_harness=validator_harness,
            validator_model=validator_model,
            validator_effort=validator_effort,
            validator_jobs=validator_jobs,
            validator_heartbeat=validator_heartbeat,
            review_harness=review_harness,
            review_harness_model=review_harness_model,
            validator=selected_validator,
            batch_size=batch_size,
            unsafe_bypass=unsafe_bypass,
            validator_timeout=validator_timeout,
            state_dir=state_dir,
            prepare_staged=prepare_plan,
        )

    steps: list[PrepareStep] = []
    blockers: list[PrepareBlocker] = []

    if resume_plan_dir is None:
        if source is None:
            raise PlanError(
                "prepare requires a source plan file (or a resume plan directory)"
            )
        source_path = os.path.realpath(
            os.path.abspath(os.path.expanduser(os.fspath(source)))
        )
        try:
            text = Path(source_path).read_text(encoding="utf-8-sig")
        except OSError as error:
            raise PlanError(
                f"cannot read source plan {source_path}: {error}"
            ) from error

        parsed: authoring_source_models.ParsedPlan | None
        try:
            parsed = authoring_parse.parse_plan_document(text)
            structure_blockers, contracts_by_id = _structure_blockers(parsed)
        except PlanError as error:
            parsed = None
            structure_blockers = [
                PrepareBlocker(stage=PREPARE_STAGE_STRUCTURE, message=str(error))
            ]
            contracts_by_id = {}
        if structure_blockers:
            steps.append(
                PrepareStep(
                    PREPARE_STAGE_STRUCTURE,
                    PREPARE_STEP_FAILED,
                    (
                        "source plan is not preparation-ready; iterate on the "
                        "plan and rerun `asl prepare`"
                    ),
                )
            )
            return PrepareResult(
                ok=False,
                reason=PREPARE_EXIT_REASON_NEEDS_PLAN_ITERATION,
                plan_dir=None,
                source_path=source_path,
                session_ids=parsed.session_ids if parsed is not None else (),
                steps=tuple(steps),
                blockers=tuple(structure_blockers),
            )
        assert parsed is not None
        steps.append(
            PrepareStep(
                PREPARE_STAGE_STRUCTURE,
                PREPARE_STEP_OK,
                (
                    f"{len(parsed.sessions)} sessions with parseable "
                    "completion contracts"
                ),
            )
        )
        plan_dir = _author_plan_directory(
            source_path,
            text,
            parsed,
            contracts_by_id,
            base_dir=base_dir,
            force_new=force_new,
        )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_AUTHORING,
                PREPARE_STEP_OK,
                f"split authored at {plan_dir}",
            )
        )
        session_ids = parsed.session_ids
    else:
        plan_dir = canonicalize_path(resume_plan_dir)
        source_path = None
        snapshot = inspect_plan(plan_dir)
        session_ids = tuple(status.id for status in snapshot.sessions)
        steps.append(
            PrepareStep(
                PREPARE_STAGE_STRUCTURE,
                PREPARE_STEP_SKIPPED,
                "resume mode: existing split directory reused",
            )
        )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_AUTHORING,
                PREPARE_STEP_SKIPPED,
                "resume mode: no new split created",
            )
        )

    ensure_plan_metadata(plan_dir, canonical_cwd, defer_registered_binding=True)
    verification_context = VerificationContext.from_plan(plan_dir, canonical_cwd)
    workspace_summary = (
        _workspace_rebind_summary(
            verification_context.root,
            verification_context.workspace.registry,
            resolved=verification_context.workspace,
        )
        if verification_context.workspace is not None
        else None
    )

    resume_command = _format_resume_command(
        plan_dir,
        cwd=canonical_cwd,
        harness_name=harness,
        harness_model=harness_model,
        spark=spark,
        validator_harness=validator_harness,
        validator_model=validator_model,
        validator_effort=validator_effort,
        validator_jobs=validator_jobs,
        validator_timeout=validator_timeout,
        validator_heartbeat=validator_heartbeat,
        review_harness=review_harness,
        review_harness_model=review_harness_model,
    )

    def _failed(
        reason: str,
        *,
        corrections: tuple[CorrectionOutcome, ...] = (),
        baseline_summary: dict[str, Any] | None = None,
    ) -> PrepareResult:
        _refresh_draft_readiness(plan_dir)
        return PrepareResult(
            ok=False,
            reason=reason,
            plan_dir=plan_dir,
            source_path=source_path,
            session_ids=session_ids,
            steps=tuple(steps),
            blockers=tuple(blockers),
            corrections=corrections,
            baseline=baseline_summary,
            resume_command=resume_command,
            workspace=workspace_summary,
        )

    try:
        aggregate, newly_validated, corrections = _run_validation_stage(
            plan_dir,
            session_ids,
            codebase_root=str(canonical_cwd),
            validator=selected_validator,
            batch_size=batch_size,
            validator_harness=validator_harness,
            validator_model=validator_model,
            validator_effort=validator_effort,
            validator_jobs=validator_jobs,
            validator_heartbeat=validator_heartbeat,
        )
    except RepositoryRegistryRequiredError as error:
        blockers.append(
            PrepareBlocker(
                stage=PREPARE_STAGE_VALIDATION,
                category=repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED,
                message=str(error),
            )
        )
        steps.append(
            PrepareStep(PREPARE_STAGE_VALIDATION, PREPARE_STEP_FAILED, str(error))
        )
        return _failed(PREPARE_EXIT_REASON_REGISTRY_REQUIRED)
    except (PlanError, harnesses.HarnessError) as error:
        blockers.append(
            PrepareBlocker(stage=PREPARE_STAGE_VALIDATION, message=str(error))
        )
        steps.append(
            PrepareStep(PREPARE_STAGE_VALIDATION, PREPARE_STEP_FAILED, str(error))
        )
        return _failed(PREPARE_EXIT_REASON_VALIDATION_FAILED)
    ambiguity_findings = _repository_ambiguity_findings(aggregate)
    if ambiguity_findings:
        for session_id, issue in ambiguity_findings:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_VALIDATION,
                    session_id=session_id,
                    category=REPOSITORY_AMBIGUITY_ISSUE,
                    message=issue.summary,
                )
            )
        detail = (
            f"{len(ambiguity_findings)} ambiguous unqualified repository "
            "path claim(s) require plan correction"
        )
        steps.append(PrepareStep(PREPARE_STAGE_VALIDATION, PREPARE_STEP_FAILED, detail))
        return _failed(PREPARE_EXIT_REASON_VALIDATION_FAILED, corrections=corrections)
    contract_problem_findings = _contract_problem_findings(aggregate)
    if contract_problem_findings:
        for session_id, problem in contract_problem_findings:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_VALIDATION,
                    session_id=session_id,
                    category=CONTRACT_PROBLEM_CATEGORY,
                    message=f"{problem.message}: {problem.suggested_fix}",
                )
            )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_VALIDATION,
                PREPARE_STEP_FAILED,
                (
                    f"{len(contract_problem_findings)} completion-contract "
                    "problem(s) require repair and revalidation"
                ),
            )
        )
        return _failed(PREPARE_EXIT_REASON_VALIDATION_FAILED, corrections=corrections)
    validation_detail = (
        f"{newly_validated} session(s) validated"
        if newly_validated
        else "all sessions reused from the validator cache; aggregate refreshed"
    )
    steps.append(
        PrepareStep(
            PREPARE_STAGE_VALIDATION,
            PREPARE_STEP_OK,
            f"{validation_detail}; expansions written",
        )
    )

    ensure_plan_metadata(plan_dir, canonical_cwd)
    verification_context = VerificationContext.from_plan(plan_dir, canonical_cwd)
    workspace_summary = (
        _workspace_rebind_summary(
            verification_context.root,
            verification_context.workspace.registry,
            resolved=verification_context.workspace,
        )
        if verification_context.workspace is not None
        else None
    )

    applied = sum(1 for o in corrections if o.status == CORRECTION_APPLIED)
    already = sum(1 for o in corrections if o.status == CORRECTION_ALREADY_APPLIED)
    blocked = sum(1 for o in corrections if o.status == CORRECTION_BLOCKED)
    steps.append(
        PrepareStep(
            PREPARE_STAGE_CORRECTIONS,
            PREPARE_STEP_OK,
            f"{applied} applied, {already} already applied, {blocked} blocked",
        )
    )
    _refresh_draft_readiness(plan_dir)

    contracts_by_session = {
        sid: contracts.read_completion_contract(plan_dir, sid) for sid in session_ids
    }
    baseline_summary = _run_baseline_stage(
        plan_dir, verification_context, contracts_by_session
    )
    if baseline_summary["unsatisfied"]:
        for entry in baseline_summary["unsatisfied"]:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_BASELINE,
                    session_id=entry["session_id"],
                    message=(
                        f"check {entry['name']!r} expected "
                        f"{entry['expectation']!r}, got {entry['status']!r}: "
                        f"{entry['detail']}"
                    ),
                )
            )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_BASELINE,
                PREPARE_STEP_FAILED,
                (
                    f"{len(baseline_summary['unsatisfied'])} of "
                    f"{baseline_summary['check_count']} baseline check(s) "
                    "unsatisfied"
                ),
            )
        )
        return _failed(
            PREPARE_EXIT_REASON_BASELINE_UNSATISFIED,
            corrections=corrections,
            baseline_summary=baseline_summary,
        )
    if baseline_summary["check_count"] == 0:
        baseline_detail = "no checks declare a baseline expectation"
    else:
        baseline_detail = (
            f"{baseline_summary['check_count']} baseline check(s) satisfied"
        )
        if baseline_summary["reused"]:
            baseline_detail += " (cached evidence reused)"
    steps.append(PrepareStep(PREPARE_STAGE_BASELINE, PREPARE_STEP_OK, baseline_detail))

    report = preflight.run_preflight(plan_dir, cwd=canonical_cwd, state_dir=state_dir)
    mark_blockers = preflight_cli._mark_loop_ready_blockers(report)
    if mark_blockers:
        for finding in mark_blockers:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_PREFLIGHT,
                    category=finding.category,
                    session_id=finding.session_id or None,
                    message=finding.message,
                )
            )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_PREFLIGHT,
                PREPARE_STEP_FAILED,
                preflight_cli._mark_loop_ready_refusal_message(mark_blockers),
            )
        )
        return _failed(
            PREPARE_EXIT_REASON_NOT_LOOP_READY,
            corrections=corrections,
            baseline_summary=baseline_summary,
        )
    marked, mark_error = preflight_cli._mark_loop_ready(report)
    if not marked:
        blockers.append(
            PrepareBlocker(
                stage=PREPARE_STAGE_PREFLIGHT,
                message=mark_error or "could not mark the plan loop-ready",
            )
        )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_PREFLIGHT,
                PREPARE_STEP_FAILED,
                mark_error or "could not mark the plan loop-ready",
            )
        )
        return _failed(
            PREPARE_EXIT_REASON_NOT_LOOP_READY,
            corrections=corrections,
            baseline_summary=baseline_summary,
        )
    steps.append(
        PrepareStep(PREPARE_STAGE_PREFLIGHT, PREPARE_STEP_OK, "plan marked loop-ready")
    )

    return PrepareResult(
        ok=True,
        reason=PREPARE_EXIT_REASON_OK,
        plan_dir=plan_dir,
        source_path=source_path,
        session_ids=session_ids,
        steps=tuple(steps),
        blockers=tuple(blockers),
        corrections=corrections,
        baseline=baseline_summary,
        marked_loop_ready=True,
        run_command=_format_run_command(
            plan_dir,
            cwd=canonical_cwd,
            harness_name=harness,
            harness_model=harness_model,
            spark=spark,
            review_harness=review_harness,
            review_harness_model=review_harness_model,
        ),
        resume_command=resume_command,
        workspace=workspace_summary,
    )
