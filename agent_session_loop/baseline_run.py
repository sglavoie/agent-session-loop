"""Execute a plan's baseline checks and record outcomes into the cache."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import contracts, plans_snapshot
from .baseline_cache import (
    BASELINE_RESULTS_FILENAME,
    BaselineCache,
    BaselineRecord,
    normalize_recipe,
    repository_fingerprint,
)
from .baseline_evidence import (
    _registered_repository_fingerprint,
    _single_check_recipe,
    baseline_identity_key,
    collect_baseline_checks,
)
from .baseline_models import (
    NEAR_TIMEOUT_FRACTION,
    BaselineCheckOutcome,
    BaselineRunResult,
    _baseline_status,
    _satisfied,
)
from .contracts import CommandCheck, VerificationCheck, VerifyRecipe
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .verification_checks import run_check
from .verification_models import (
    VerificationContext,
    verification_context_from_plan,
)


def _per_check_budget(
    check: VerificationCheck, per_check_timeout: float | None
) -> float:
    budget = contracts.baseline_check_budget_seconds(check)
    if per_check_timeout is not None:
        budget = min(budget, float(per_check_timeout))
    return budget


def _clamp_command_timeout(
    check: VerificationCheck, budget: float
) -> VerificationCheck:
    if isinstance(check, CommandCheck):
        return replace(check, timeout_seconds=budget)
    return check


def run_baseline(
    plan_dir: str | Path,
    cwd: str | Path,
    *,
    per_check_timeout: float | None = None,
    total_timeout: float | None = None,
    cache: BaselineCache | None = None,
    now: str | None = None,
    context: VerificationContext | None = None,
    session_ids: Collection[str] | None = None,
) -> BaselineRunResult:
    """Execute the plan's baseline checks and record outcomes into the cache.

    Checks are deduplicated by :func:`baseline_identity_key`, run under a
    per-check budget clamped to :data:`contracts.DEFAULT_BASELINE_CHECK_BUDGET_SECONDS`
    (or ``per_check_timeout`` when smaller) and further clamped to the remaining
    total budget, and stopped once the cumulative wall-clock exceeds the total
    budget (remaining checks record as ``skip``). The total budget is a
    fail-closed gate: a check whose execution crosses it records an unsatisfied
    outcome regardless of exit code, so overrun evidence can never make a plan
    loop-ready. Every executed outcome is persisted to ``baseline-results.json``.

    ``session_ids`` restricts the run to those sessions' checks. Without it every
    session is collected, including completed ones whose ``fail`` expectations
    their own work has since turned into passes.
    """
    canonical_dir = canonicalize_path(plan_dir)
    verification_context = context or verification_context_from_plan(plan_dir, cwd)
    canonical_cwd = verification_context.root
    snapshot = plans_snapshot.inspect_plan(canonical_dir)

    contracts_by_session: dict[str, Any] = {}
    for session in snapshot.sessions:
        if session_ids is not None and session.id not in session_ids:
            continue
        contract = contracts.read_completion_contract(canonical_dir, session.id)
        if contract is not None:
            contracts_by_session[session.id] = contract

    collected = collect_baseline_checks(contracts_by_session)

    results_path = canonical_dir / BASELINE_RESULTS_FILENAME
    if cache is None:
        cache = BaselineCache.load(results_path)

    # Resolve every location before executing any baseline command. Registered
    # paths can fail only after filesystem resolution (for example, when an
    # in-repository symlink escapes its repository), and discovering that late
    # would otherwise discard earlier side-effecting outcomes and repeat them
    # on the next baseline attempt.
    resolved_checks = []
    seen_keys: set[str] = set()
    try:
        for session_id, check in collected:
            key = baseline_identity_key(
                check, canonical_cwd, context=verification_context
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            resolved_checks.append(
                (
                    session_id,
                    check,
                    key,
                    verification_context.resolve_check(check),
                )
            )
    except PlanError:
        # Persist the loaded cache even when no new command was eligible to run.
        # Besides preserving prior evidence, this leaves a valid derived journal
        # instead of repeatedly entering an evidence-free failed pass.
        cache.save()
        raise

    total_budget = (
        float(total_timeout)
        if total_timeout is not None
        # Budgeted over the checks that will run: a check two sessions share
        # executes once, so it must not earn the total budget twice.
        else contracts.recipe_total_budget_seconds(
            VerifyRecipe(checks=tuple(check for _, check, _, _ in resolved_checks))
        )
    )
    recorded_at = now or datetime.now(UTC).isoformat()
    registry_digest = verification_context.registry_digest
    if verification_context.workspace is None:
        fingerprint = repository_fingerprint(canonical_cwd, canonical_dir)
        aggregate_fingerprint = None
        repository_evidence_available: bool | None = None
    else:
        fingerprint, repository_evidence_available = _registered_repository_fingerprint(
            verification_context, canonical_dir
        )
        aggregate_fingerprint = fingerprint

    outcomes: list[BaselineCheckOutcome] = []
    total_seconds = 0.0
    total_exceeded = False
    for session_id, check, key, resolved in resolved_checks:
        budget = _per_check_budget(check, per_check_timeout)
        expectation = getattr(check, "baseline_expectation", "skip")

        if total_seconds >= total_budget:
            total_exceeded = True
            outcome = BaselineCheckOutcome(
                session_id=session_id,
                key=key,
                name=check.name,
                kind=check.kind,
                expectation=expectation,
                status="skip",
                satisfied=False,
                duration_seconds=0.0,
                budget_seconds=budget,
                near_timeout=False,
                total_budget_exceeded=True,
                detail=(
                    "skipped: total baseline budget of "
                    f"{total_budget:g}s exhausted before this check"
                ),
                repository_fingerprint=fingerprint,
                recorded_at=recorded_at,
                repo=check.repo,
                resolved_root=str(resolved.resolved_root),
                qualified_cwd=resolved.qualified_cwd,
                qualified_path=resolved.qualified_path,
                registry_digest=registry_digest,
                aggregate_repository_fingerprint=aggregate_fingerprint,
                repository_evidence_available=repository_evidence_available,
            )
            outcomes.append(outcome)
            cache.put(
                key,
                BaselineRecord(
                    key=key,
                    recipe=normalize_recipe(_single_check_recipe(check)),
                    result=outcome.to_result(),
                ),
            )
            continue

        # A single check may never run past the total budget on its own:
        # its command timeout is clamped to whatever total budget remains.
        budget = min(budget, total_budget - total_seconds)
        clamped = _clamp_command_timeout(check, budget)
        if verification_context.workspace is None:
            # Retain the legacy keyword call for downstream monkeypatch seams.
            result = run_check(clamped, repo_root=canonical_cwd)
        else:
            result = run_check(clamped, context=verification_context)
        total_seconds += result.duration_seconds
        crossed_total = total_seconds > total_budget
        if crossed_total:
            total_exceeded = True
        status = _baseline_status(result)
        near_timeout = (
            isinstance(check, CommandCheck)
            and budget > 0
            and result.duration_seconds >= NEAR_TIMEOUT_FRACTION * budget
        )
        detail = result.detail
        if crossed_total:
            detail = (
                f"{detail}; total baseline budget of {total_budget:g}s "
                "exceeded during this check"
            )
        if repository_evidence_available is False:
            detail = f"{detail}; registered repository evidence was unavailable"
        outcome = BaselineCheckOutcome(
            session_id=session_id,
            key=key,
            name=check.name,
            kind=check.kind,
            expectation=expectation,
            status=status,
            # A total-budget overrun is never satisfied evidence, even when
            # the command itself matched its expectation.
            satisfied=(
                _satisfied(expectation, status)
                and not crossed_total
                and repository_evidence_available is not False
            ),
            duration_seconds=result.duration_seconds,
            budget_seconds=budget,
            near_timeout=near_timeout,
            total_budget_exceeded=crossed_total,
            detail=detail,
            repository_fingerprint=fingerprint,
            recorded_at=recorded_at,
            repo=check.repo,
            resolved_root=str(resolved.resolved_root),
            qualified_cwd=resolved.qualified_cwd,
            qualified_path=resolved.qualified_path,
            registry_digest=registry_digest,
            aggregate_repository_fingerprint=aggregate_fingerprint,
            repository_evidence_available=repository_evidence_available,
        )
        outcomes.append(outcome)
        cache.put(
            key,
            BaselineRecord(
                key=key,
                recipe=normalize_recipe(_single_check_recipe(check)),
                result=outcome.to_result(),
            ),
        )

    cache.save()
    return BaselineRunResult(
        plan_dir=canonical_dir,
        cwd=canonical_cwd,
        outcomes=tuple(outcomes),
        total_seconds=total_seconds,
        total_budget_seconds=total_budget,
        total_exceeded=total_exceeded,
        results_path=results_path,
        registry_digest=registry_digest,
        aggregate_repository_fingerprint=aggregate_fingerprint,
    )
