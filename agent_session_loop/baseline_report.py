"""Baseline run payload and text rendering."""

from __future__ import annotations

from typing import Any

from .baseline_models import BaselineCheckOutcome, BaselineRunResult
from .verification_models import VerificationContext


def _outcome_payload(outcome: BaselineCheckOutcome) -> dict[str, Any]:
    payload = {
        "session_id": outcome.session_id,
        "key": outcome.key,
        "name": outcome.name,
        "kind": outcome.kind,
        "expectation": outcome.expectation,
        "status": outcome.status,
        "satisfied": outcome.satisfied,
        "duration_seconds": outcome.duration_seconds,
        "budget_seconds": outcome.budget_seconds,
        "near_timeout": outcome.near_timeout,
        "total_budget_exceeded": outcome.total_budget_exceeded,
        "detail": outcome.detail,
    }
    if outcome.registry_digest is not None:
        payload.update(
            {
                "repo": outcome.repo,
                "resolved_root": outcome.resolved_root,
                "qualified_cwd": outcome.qualified_cwd,
                "qualified_path": outcome.qualified_path,
                "registry_digest": outcome.registry_digest,
                "aggregate_repository_fingerprint": (
                    outcome.aggregate_repository_fingerprint
                ),
                "repository_evidence_available": (
                    outcome.repository_evidence_available
                ),
            }
        )
    return payload


def _run_result_payload(
    result: BaselineRunResult,
    *,
    plan_dir_arg: str,
    cwd_arg: str,
    per_check_timeout: float | None,
    total_timeout: float | None,
    verification_context: VerificationContext | None = None,
) -> dict[str, Any]:
    satisfied_count = sum(1 for o in result.outcomes if o.satisfied)
    payload = {
        "plan_dir": plan_dir_arg,
        "plan_dir_resolved": str(result.plan_dir),
        "cwd": cwd_arg,
        "cwd_resolved": str(result.cwd),
        "per_check_timeout": per_check_timeout,
        "total_timeout": total_timeout,
        "results_path": str(result.results_path),
        "check_count": len(result.outcomes),
        "satisfied_count": satisfied_count,
        "unsatisfied_count": len(result.outcomes) - satisfied_count,
        "all_satisfied": result.all_satisfied,
        "total_seconds": result.total_seconds,
        "total_budget_seconds": result.total_budget_seconds,
        "total_exceeded": result.total_exceeded,
        "checks": [_outcome_payload(o) for o in result.outcomes],
    }
    if result.registry_digest is not None:
        repositories = []
        if (
            verification_context is not None
            and verification_context.workspace is not None
        ):
            repositories = [
                {
                    "alias": repository.alias,
                    "path": repository.repository.path,
                    "origin": repository.origin,
                    "resolved_root": str(repository.root),
                }
                for repository in verification_context.workspace.repositories
            ]
        payload.update(
            {
                "workspace_root": str(result.cwd),
                "registry_digest": result.registry_digest,
                "repositories": repositories,
                "aggregate_repository_fingerprint": (
                    result.aggregate_repository_fingerprint
                ),
            }
        )
    return payload


def _print_text_baseline(result: BaselineRunResult, *, plan_dir_arg: str) -> None:
    print(f"Plan: {plan_dir_arg}")
    print(
        f"{'Workspace root' if result.registry_digest is not None else 'Repo root'}: "
        f"{result.cwd}"
    )
    print(f"Baseline checks: {len(result.outcomes)}")
    for outcome in result.outcomes:
        marker = "OK" if outcome.satisfied else "MISS"
        repository = f" {outcome.repo}:" if outcome.repo else ""
        print(
            f"  [{marker}]{repository} {outcome.kind} {outcome.name} "
            f"(expect {outcome.expectation}, got {outcome.status}, "
            f"{outcome.duration_seconds:.2f}s): {outcome.detail}"
        )
    if result.outcomes:
        state = "satisfied" if result.all_satisfied else "unsatisfied"
        print(f"Baseline: {state} ({result.results_path})")
    else:
        print("Baseline: no checks declare a baseline expectation")
