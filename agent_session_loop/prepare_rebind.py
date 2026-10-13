"""Workspace rebind staging for ``asl prepare``."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import harnesses, plans, state, state_locks, workspaces
from .authoring import PlanTransaction
from .baseline_cache import BASELINE_RESULTS_FILENAME, BaselineCache
from .plans import PlanError, canonicalize_path, inspect_plan, parse_session_id
from .prepare_commands import _format_resume_command, _format_run_command
from .prepare_corrections import CorrectionOutcome
from .prepare_models import (
    PREPARE_VALIDATION_CACHE_FILENAME,
    PREPARE_VALIDATION_FILENAME,
)
from .prepare_results import (
    PREPARE_EXIT_REASON_OK,
    PREPARE_EXIT_REASON_WORKSPACE_REBIND_FAILED,
    PREPARE_STAGE_AUTHORING,
    PREPARE_STAGE_METADATA,
    PREPARE_STAGE_PUBLICATION,
    PREPARE_STAGE_STRUCTURE,
    PREPARE_STAGE_VALIDATION,
    PREPARE_STAGE_WORKSPACE,
    PREPARE_STEP_FAILED,
    PREPARE_STEP_SKIPPED,
    PrepareBlocker,
    PrepareResult,
    PrepareStep,
    _WorkspaceRebindConcurrencyError,
)
from .prepare_validator import ValidatorRunner


def _workspace_rebind_summary(
    workspace_root: str | Path,
    registry: workspaces.RepositoryRegistry,
    *,
    resolved: workspaces.ResolvedWorkspace | None = None,
) -> dict[str, Any]:
    """Return a credential-free workspace binding projection for prepare JSON."""
    canonical_root = canonicalize_path(workspace_root)
    resolved_by_alias = (
        {repository.alias: repository for repository in resolved.repositories}
        if resolved is not None
        else {}
    )
    repositories: list[dict[str, str]] = []
    for repository in registry:
        resolved_repository = resolved_by_alias.get(repository.alias)
        root = (
            resolved_repository.root
            if resolved_repository is not None
            else canonicalize_path(canonical_root / repository.path)
        )
        repositories.append(
            {
                "alias": repository.alias,
                "path": repository.path,
                "origin": repository.origin,
                "root": str(root),
            }
        )
    return {
        "workspace_root": str(canonical_root),
        "registry_digest": registry.digest,
        "repositories": repositories,
    }


def _workspace_rebind_payload(
    old_workspace: dict[str, Any],
    new_workspace: dict[str, Any],
    *,
    failure_stage: str | None = None,
    error: str | None = None,
    published_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "old": old_workspace,
        "new": new_workspace,
        "failure_stage": failure_stage,
        "error": error,
        "published_files": list(published_files),
    }


def _workspace_rebind_failure(
    *,
    plan_dir: Path,
    session_ids: tuple[str, ...],
    old_workspace: dict[str, Any],
    new_workspace: dict[str, Any],
    stage: str,
    message: str,
    resume_command: str,
    reason: str = PREPARE_EXIT_REASON_WORKSPACE_REBIND_FAILED,
    steps: tuple[PrepareStep, ...] = (),
    blockers: tuple[PrepareBlocker, ...] = (),
    corrections: tuple[CorrectionOutcome, ...] = (),
    baseline_summary: dict[str, Any] | None = None,
) -> PrepareResult:
    if not steps:
        steps = (
            PrepareStep(
                PREPARE_STAGE_STRUCTURE,
                PREPARE_STEP_SKIPPED,
                "rebind mode: existing split directory reused",
            ),
            PrepareStep(
                PREPARE_STAGE_AUTHORING,
                PREPARE_STEP_SKIPPED,
                "rebind mode: no new split created",
            ),
            PrepareStep(stage, PREPARE_STEP_FAILED, message),
        )
    if not blockers:
        blockers = (PrepareBlocker(stage=stage, message=message),)
    return PrepareResult(
        ok=False,
        reason=reason,
        plan_dir=plan_dir,
        source_path=None,
        session_ids=session_ids,
        steps=steps,
        blockers=blockers,
        corrections=corrections,
        baseline=baseline_summary,
        resume_command=resume_command,
        workspace_rebind=_workspace_rebind_payload(
            old_workspace,
            new_workspace,
            failure_stage=stage,
            error=message,
        ),
        workspace=old_workspace,
    )


def _rebase_staged_rebind_text(
    value: str,
    *,
    staged_plan_dir: Path,
    plan_dir: Path,
) -> str:
    """Replace an ephemeral staged-plan prefix with the durable plan path."""
    return value.replace(str(staged_plan_dir), str(plan_dir))


def _rebase_staged_rebind_diagnostics(
    staged_result: PrepareResult,
    *,
    staged_plan_dir: Path,
    plan_dir: Path,
) -> tuple[tuple[PrepareStep, ...], tuple[PrepareBlocker, ...]]:
    """Replace ephemeral staged-plan paths in operator-facing diagnostics."""

    def rewrite(value: str) -> str:
        return _rebase_staged_rebind_text(
            value,
            staged_plan_dir=staged_plan_dir,
            plan_dir=plan_dir,
        )

    steps = tuple(
        PrepareStep(step.name, step.status, rewrite(step.detail))
        for step in staged_result.steps
    )
    blockers = tuple(
        PrepareBlocker(
            stage=blocker.stage,
            message=rewrite(blocker.message),
            session_id=blocker.session_id,
            category=blocker.category,
        )
        for blocker in staged_result.blockers
    )
    return steps, blockers


def _prepare_workspace_rebind(
    plan_dir_value: str | Path,
    *,
    cwd: Path,
    harness: str,
    harness_model: str | None,
    spark: bool,
    validator_harness: str,
    validator_model: str | None,
    validator_effort: str | None,
    validator_jobs: int,
    validator_heartbeat: int,
    review_harness: str | None,
    review_harness_model: str | None,
    validator: ValidatorRunner,
    batch_size: int,
    unsafe_bypass: bool,
    validator_timeout: int,
    state_dir: str | Path | None,
    prepare_staged: Callable[..., PrepareResult],
) -> PrepareResult:
    """Rebuild workspace-dependent preparation evidence off-disk, then publish."""
    plan_dir = canonicalize_path(plan_dir_value)
    snapshot = inspect_plan(plan_dir)
    session_ids = tuple(status.id for status in snapshot.sessions)
    registry = plans.read_repository_registry(plan_dir)
    if registry is None:
        raise PlanError("--rebind-workspace requires a registered plan")
    metadata = plans.load_plan_metadata(plan_dir)
    if isinstance(metadata, plans.WorkspacePlanMetadata):
        if metadata.registry_digest != registry.digest:
            raise PlanError("stored workspace binding does not match the plan registry")
        if canonicalize_path(metadata.workspace_root) == cwd:
            raise PlanError("--rebind-workspace requires a changed workspace binding")
        old_workspace = _workspace_rebind_summary(
            metadata.workspace_root,
            registry,
        )
    elif isinstance(metadata, plans.PlanMetadata):
        # A plan that grew a registry after it was bound in legacy mode: the
        # stale v1 binding names no repositories, so it is discarded wholesale
        # rather than compared against the registry.
        old_workspace = {
            "workspace_root": str(canonicalize_path(metadata.cwd)),
            "registry_digest": "",
            "repositories": [],
        }
    else:
        raise PlanError("--rebind-workspace requires an existing workspace binding")

    unresolved_new_workspace = _workspace_rebind_summary(cwd, registry)
    lock_info = state_locks.inspect_lock_info(plan_dir, state_dir)
    if lock_info is not None and lock_info.active:
        raise _WorkspaceRebindConcurrencyError(
            "--rebind-workspace is unavailable while the plan has an active lock",
            old_workspace=old_workspace,
            new_workspace=unresolved_new_workspace,
            stage=PREPARE_STAGE_WORKSPACE,
        )

    resume_command = _format_resume_command(
        plan_dir,
        cwd=cwd,
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
        rebind_workspace=True,
    )
    try:
        resolved_workspace = workspaces.resolve_workspace(cwd, registry)
    except workspaces.WorkspaceError as error:
        return _workspace_rebind_failure(
            plan_dir=plan_dir,
            session_ids=session_ids,
            old_workspace=old_workspace,
            new_workspace=unresolved_new_workspace,
            stage=PREPARE_STAGE_WORKSPACE,
            message=f"invalid replacement workspace: {error}",
            resume_command=resume_command,
        )
    new_workspace = _workspace_rebind_summary(
        cwd,
        registry,
        resolved=resolved_workspace,
    )

    try:
        state_locks.acquire_lock(plan_dir, state_dir, owner="asl-prepare-rebind")
    except state.StateError as error:
        raise _WorkspaceRebindConcurrencyError(
            f"cannot lock plan for workspace rebind: {error}",
            old_workspace=old_workspace,
            new_workspace=new_workspace,
            stage=PREPARE_STAGE_WORKSPACE,
        ) from error

    try:
        if plans.read_repository_registry(plan_dir) != registry:
            raise _WorkspaceRebindConcurrencyError(
                "plan registry changed while workspace rebind was starting",
                old_workspace=old_workspace,
                new_workspace=new_workspace,
                stage=PREPARE_STAGE_METADATA,
            )
        if plans.load_plan_metadata(plan_dir) != metadata:
            raise _WorkspaceRebindConcurrencyError(
                "workspace binding changed while rebind was starting",
                old_workspace=old_workspace,
                new_workspace=new_workspace,
                stage=PREPARE_STAGE_METADATA,
            )

        try:
            with tempfile.TemporaryDirectory(
                prefix="asl-rebind-"
            ) as staging_root_value:
                staged_plan_dir = Path(staging_root_value) / "plan"
                shutil.copytree(plan_dir, staged_plan_dir, symlinks=True)
                (staged_plan_dir / PREPARE_VALIDATION_FILENAME).unlink(missing_ok=True)
                (staged_plan_dir / PREPARE_VALIDATION_CACHE_FILENAME).unlink(
                    missing_ok=True
                )
                (staged_plan_dir / BASELINE_RESULTS_FILENAME).unlink(missing_ok=True)
                try:
                    plans.save_workspace_plan_metadata(
                        staged_plan_dir,
                        resolved_workspace.root,
                        registry.digest,
                    )
                except (OSError, PlanError) as error:
                    return _workspace_rebind_failure(
                        plan_dir=plan_dir,
                        session_ids=session_ids,
                        old_workspace=old_workspace,
                        new_workspace=new_workspace,
                        stage=PREPARE_STAGE_METADATA,
                        message=_rebase_staged_rebind_text(
                            f"could not stage replacement metadata: {error}",
                            staged_plan_dir=staged_plan_dir,
                            plan_dir=plan_dir,
                        ),
                        resume_command=resume_command,
                    )

                try:
                    staged_result = prepare_staged(
                        None,
                        cwd=cwd,
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
                        resume_plan_dir=staged_plan_dir,
                        validator=validator,
                        batch_size=batch_size,
                        unsafe_bypass=unsafe_bypass,
                        validator_timeout=validator_timeout,
                        state_dir=state_dir,
                    )
                except (OSError, PlanError, harnesses.HarnessError) as error:
                    return _workspace_rebind_failure(
                        plan_dir=plan_dir,
                        session_ids=session_ids,
                        old_workspace=old_workspace,
                        new_workspace=new_workspace,
                        stage=PREPARE_STAGE_VALIDATION,
                        message=_rebase_staged_rebind_text(
                            f"replacement preparation failed: {error}",
                            staged_plan_dir=staged_plan_dir,
                            plan_dir=plan_dir,
                        ),
                        resume_command=resume_command,
                    )

                if not staged_result.ok:
                    rebased_steps, rebased_blockers = _rebase_staged_rebind_diagnostics(
                        staged_result,
                        staged_plan_dir=staged_plan_dir,
                        plan_dir=plan_dir,
                    )
                    failure_stage = next(
                        (
                            step.name
                            for step in rebased_steps
                            if step.status == PREPARE_STEP_FAILED
                        ),
                        rebased_blockers[0].stage
                        if rebased_blockers
                        else PREPARE_STAGE_VALIDATION,
                    )
                    message = (
                        rebased_blockers[0].message
                        if rebased_blockers
                        else f"replacement preparation failed: {staged_result.reason}"
                    )
                    return _workspace_rebind_failure(
                        plan_dir=plan_dir,
                        session_ids=session_ids,
                        old_workspace=old_workspace,
                        new_workspace=new_workspace,
                        stage=failure_stage,
                        message=message,
                        resume_command=resume_command,
                        reason=staged_result.reason,
                        steps=rebased_steps,
                        blockers=rebased_blockers,
                        corrections=staged_result.corrections,
                        baseline_summary=staged_result.baseline,
                    )

                baseline_path = staged_plan_dir / BASELINE_RESULTS_FILENAME
                if not baseline_path.exists():
                    BaselineCache(baseline_path).save()

                relative_paths = [
                    "manifest.md",
                    plans.PLAN_METADATA_FILE_NAME,
                    PREPARE_VALIDATION_FILENAME,
                    PREPARE_VALIDATION_CACHE_FILENAME,
                    BASELINE_RESULTS_FILENAME,
                ]
                for session_id in session_ids:
                    parsed_id = parse_session_id(session_id)
                    relative_paths.extend(
                        [parsed_id.filename, parsed_id.expansion_filename]
                    )

                publication_texts: dict[str, str] = {}
                for relative_path in relative_paths:
                    staged_path = staged_plan_dir / relative_path
                    try:
                        staged_text = staged_path.read_text(encoding="utf-8")
                    except OSError as error:
                        return _workspace_rebind_failure(
                            plan_dir=plan_dir,
                            session_ids=session_ids,
                            old_workspace=old_workspace,
                            new_workspace=new_workspace,
                            stage=PREPARE_STAGE_PUBLICATION,
                            message=(
                                "replacement preparation did not produce "
                                f"{relative_path}: {error}"
                            ),
                            resume_command=resume_command,
                            corrections=staged_result.corrections,
                            baseline_summary=staged_result.baseline,
                        )
                    publication_texts[relative_path] = staged_text
        except OSError as error:
            return _workspace_rebind_failure(
                plan_dir=plan_dir,
                session_ids=session_ids,
                old_workspace=old_workspace,
                new_workspace=new_workspace,
                stage=PREPARE_STAGE_PUBLICATION,
                message=f"could not stage replacement plan view: {error}",
                resume_command=resume_command,
            )

        transaction = PlanTransaction(plan_dir)
        for relative_path in relative_paths:
            transaction.stage(relative_path, publication_texts[relative_path])
        try:
            transaction.commit()
        except PlanError as error:
            return _workspace_rebind_failure(
                plan_dir=plan_dir,
                session_ids=session_ids,
                old_workspace=old_workspace,
                new_workspace=new_workspace,
                stage=PREPARE_STAGE_PUBLICATION,
                message=f"could not publish replacement workspace: {error}",
                resume_command=resume_command,
                corrections=staged_result.corrections,
                baseline_summary=staged_result.baseline,
            )
    finally:
        state_locks.release_lock(plan_dir, state_dir)

    published_files = tuple(relative_paths)
    return PrepareResult(
        ok=True,
        reason=PREPARE_EXIT_REASON_OK,
        plan_dir=plan_dir,
        source_path=None,
        session_ids=session_ids,
        steps=staged_result.steps,
        blockers=staged_result.blockers,
        corrections=staged_result.corrections,
        baseline=staged_result.baseline,
        marked_loop_ready=True,
        run_command=_format_run_command(
            plan_dir,
            cwd=cwd,
            harness_name=harness,
            harness_model=harness_model,
            spark=spark,
            review_harness=review_harness,
            review_harness_model=review_harness_model,
        ),
        resume_command=resume_command,
        workspace_rebind=_workspace_rebind_payload(
            old_workspace,
            new_workspace,
            published_files=published_files,
        ),
        workspace=new_workspace,
    )
