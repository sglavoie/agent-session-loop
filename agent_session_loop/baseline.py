"""Execute completion-contract baseline recipes and record their outcomes.

Session 7 delivered the parsed ``baseline_expectation`` field, the clamped
per-check/total budgets, and the :class:`~agent_session_loop.baseline_cache.
BaselineCache` store. This module is the *execution* engine that consumes them:
it walks a plan's completion contracts, collects the checks that declare a
non-``skip`` baseline expectation, deduplicates equivalent checks across
sessions, runs each one under the clamped baseline budget, and records the
pass/fail/timeout outcome plus the repository fingerprint into the cache.

The recorded evidence is what the preflight readiness gate consults (without
ever running commands itself) to decide whether a pre-existing broad-suite
failure should block before implementation. See
:func:`agent_session_loop.preflight._check_baselines`.

Storage uses a *fingerprint-independent identity key* (recipe + cwd +
executable + environment, with the repository fingerprint deliberately
excluded) so a recorded outcome remains findable after the working tree
changes; the fingerprint captured at record time lives inside the stored
result, letting preflight distinguish a *missing* baseline from a *stale* one.

The module is stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from . import contracts, plans, responses
from .baseline_cache import (
    BASELINE_RESULTS_FILENAME,
    BaselineCache,
    BaselineRecord,
    _recipe_executable_identity,
    cache_key,
    environment_signature,
    normalize_recipe,
    repository_fingerprint,
)
from .contracts import CommandCheck, VerificationCheck, VerifyRecipe
from .plans import PlanError, canonicalize_path
from .verification import CheckResult, run_check


#: Baseline outcome statuses recorded in the cache. ``pass``/``fail`` mirror the
#: verification status; ``timeout`` and ``error`` capture command executions
#: that never produced a clean pass/fail signal.
BaselineStatus = str

#: A baseline check whose measured duration exceeds this fraction of its budget
#: is flagged as "near timeout" so an unrealistically slow recipe surfaces
#: before it starts timing out.
NEAR_TIMEOUT_FRACTION = 0.8

BASELINE_EXIT_REASON_OK = "ok"
BASELINE_EXIT_REASON_UNSATISFIED = "baseline-unsatisfied"
BASELINE_EXIT_REASON_NO_CHECKS = "no-baseline-checks"
BASELINE_EXIT_REASON_CWD_ERROR = "cwd-error"
BASELINE_EXIT_REASON_PLAN_STRUCTURE = "plan-structure"
BASELINE_EXIT_REASON_MALFORMED_CONTRACT = "malformed-contract"


def _single_check_recipe(check: VerificationCheck) -> VerifyRecipe:
    return VerifyRecipe(checks=(check,))


def baseline_identity_key(
    check: VerificationCheck,
    cwd: str | Path,
    *,
    env: dict | None = None,
) -> str:
    """Return the fingerprint-independent cache key for a single check.

    The repository fingerprint is intentionally fixed to the empty string so
    that a recorded outcome stays findable after the working tree changes;
    staleness is detected by comparing the fingerprint stored *inside* the
    result, not by the key. Equivalent checks across sessions (same argv/path,
    possibly different names) collapse to one key because
    :func:`~agent_session_loop.baseline_cache.normalize_recipe` excludes the
    check name.
    """
    recipe = _single_check_recipe(check)
    return cache_key(
        recipe=recipe,
        cwd=cwd,
        repo_fingerprint="",
        executable=_recipe_executable_identity(recipe),
        environment=environment_signature(env),
    )


def collect_baseline_checks(
    contracts_by_session: dict[str, Any],
) -> list[tuple[str, VerificationCheck]]:
    """Return ``(session_id, check)`` pairs that declare a baseline expectation.

    Only checks whose ``baseline_expectation`` is ``pass`` or ``fail`` are
    returned; ``skip`` (the legacy default) is omitted so plans authored before
    the field carry no baseline obligations. Pairs are sorted by session id then
    check name for deterministic execution and reporting order.
    """
    collected: list[tuple[str, VerificationCheck]] = []
    for session_id, contract in contracts_by_session.items():
        if contract is None:
            continue
        for check in contract.verify.checks:
            expectation = getattr(
                check, "baseline_expectation", contracts.DEFAULT_BASELINE_EXPECTATION
            )
            if expectation in ("pass", "fail"):
                collected.append((session_id, check))
    collected.sort(key=lambda pair: (plans.parse_session_id(pair[0]), pair[1].name))
    return collected


def _baseline_status(result: CheckResult) -> BaselineStatus:
    if result.status == "pass":
        return "pass"
    reason = result.failure_reason
    if reason == "command-timeout":
        return "timeout"
    if reason in ("command-not-found", "command-error", "file-error"):
        return "error"
    return "fail"


def _satisfied(expectation: str, status: BaselineStatus) -> bool:
    """Whether ``status`` satisfies ``expectation``.

    ``timeout`` and ``error`` never satisfy an expectation — a timed-out or
    unrunnable command is not evidence that the check passed *or* failed.
    """
    if expectation == "pass":
        return status == "pass"
    if expectation == "fail":
        return status == "fail"
    return False


@dataclass(frozen=True)
class BaselineCheckOutcome:
    """The measured outcome of running one baseline check."""

    session_id: str
    key: str
    name: str
    kind: str
    expectation: str
    status: BaselineStatus
    satisfied: bool
    duration_seconds: float
    budget_seconds: float
    near_timeout: bool
    total_budget_exceeded: bool
    detail: str
    repository_fingerprint: str
    recorded_at: str

    def to_result(self) -> dict[str, Any]:
        """Return the cache ``result`` payload for this outcome."""
        return {
            "check_name": self.name,
            "kind": self.kind,
            "expectation": self.expectation,
            "status": self.status,
            "satisfied": self.satisfied,
            "duration_seconds": self.duration_seconds,
            "budget_seconds": self.budget_seconds,
            "near_timeout": self.near_timeout,
            "total_budget_exceeded": self.total_budget_exceeded,
            "detail": self.detail,
            "repository_fingerprint": self.repository_fingerprint,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class BaselineRunResult:
    """Aggregated result of executing every collected baseline check."""

    plan_dir: Path
    cwd: Path
    outcomes: tuple[BaselineCheckOutcome, ...]
    total_seconds: float
    total_budget_seconds: float
    total_exceeded: bool
    results_path: Path

    @property
    def all_satisfied(self) -> bool:
        return all(outcome.satisfied for outcome in self.outcomes)

    @property
    def unsatisfied(self) -> tuple[BaselineCheckOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.satisfied)


def _per_check_budget(
    check: VerificationCheck, per_check_timeout: float | None
) -> float:
    budget = contracts.baseline_check_budget_seconds(check)
    if per_check_timeout is not None:
        budget = min(budget, float(per_check_timeout))
    return budget


def _clamp_command_timeout(check: VerificationCheck, budget: float) -> VerificationCheck:
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
    """
    canonical_dir = canonicalize_path(plan_dir)
    canonical_cwd = canonicalize_path(cwd)
    snapshot = plans.inspect_plan(canonical_dir)

    contracts_by_session: dict[str, Any] = {}
    for session in snapshot.sessions:
        contract = contracts.read_completion_contract(canonical_dir, session.id)
        if contract is not None:
            contracts_by_session[session.id] = contract

    collected = collect_baseline_checks(contracts_by_session)

    results_path = canonical_dir / BASELINE_RESULTS_FILENAME
    if cache is None:
        cache = BaselineCache.load(results_path)

    total_budget = (
        float(total_timeout)
        if total_timeout is not None
        else contracts.recipe_total_budget_seconds(
            VerifyRecipe(checks=tuple(check for _, check in collected))
        )
    )
    recorded_at = now or datetime.now(timezone.utc).isoformat()
    fingerprint = repository_fingerprint(canonical_cwd)

    outcomes: list[BaselineCheckOutcome] = []
    seen_keys: set[str] = set()
    total_seconds = 0.0
    total_exceeded = False
    for session_id, check in collected:
        key = baseline_identity_key(check, canonical_cwd)
        if key in seen_keys:
            # Deduplicated: an equivalent check already ran this pass.
            continue
        seen_keys.add(key)
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
        result = run_check(clamped, repo_root=canonical_cwd)
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
        outcome = BaselineCheckOutcome(
            session_id=session_id,
            key=key,
            name=check.name,
            kind=check.kind,
            expectation=expectation,
            status=status,
            # A total-budget overrun is never satisfied evidence, even when
            # the command itself matched its expectation.
            satisfied=_satisfied(expectation, status) and not crossed_total,
            duration_seconds=result.duration_seconds,
            budget_seconds=budget,
            near_timeout=near_timeout,
            total_budget_exceeded=crossed_total,
            detail=detail,
            repository_fingerprint=fingerprint,
            recorded_at=recorded_at,
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
    )


def _outcome_payload(outcome: BaselineCheckOutcome) -> dict[str, Any]:
    return {
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


def _run_result_payload(
    result: BaselineRunResult,
    *,
    plan_dir_arg: str,
    cwd_arg: str,
    per_check_timeout: float | None,
    total_timeout: float | None,
) -> dict[str, Any]:
    satisfied_count = sum(1 for o in result.outcomes if o.satisfied)
    return {
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


def _print_text_baseline(result: BaselineRunResult, *, plan_dir_arg: str) -> None:
    print(f"Plan: {plan_dir_arg}")
    print(f"Repo root: {result.cwd}")
    print(f"Baseline checks: {len(result.outcomes)}")
    for outcome in result.outcomes:
        marker = "OK" if outcome.satisfied else "MISS"
        print(
            f"  [{marker}] {outcome.kind} {outcome.name} "
            f"(expect {outcome.expectation}, got {outcome.status}, "
            f"{outcome.duration_seconds:.2f}s): {outcome.detail}"
        )
    if result.outcomes:
        state = "satisfied" if result.all_satisfied else "unsatisfied"
        print(f"Baseline: {state} ({result.results_path})")
    else:
        print("Baseline: no checks declare a baseline expectation")


def baseline_from_args(args: Any) -> int:
    """Run the ``baseline`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    cwd_arg = args.cwd
    want_json = args.json
    per_check_timeout = getattr(args, "per_check_timeout", None)
    total_timeout = getattr(args, "total_timeout", None)

    common_fields = {
        "plan_dir": str(plan_dir_arg),
        "cwd": str(cwd_arg),
        "per_check_timeout": per_check_timeout,
        "total_timeout": total_timeout,
    }

    try:
        canonicalize_path(cwd_arg)
    except (PlanError, OSError) as error:
        _emit_baseline_error(
            want_json=want_json,
            reason=BASELINE_EXIT_REASON_CWD_ERROR,
            error=str(error),
            **common_fields,
        )
        return 1

    try:
        result = run_baseline(
            plan_dir_arg,
            cwd_arg,
            per_check_timeout=per_check_timeout,
            total_timeout=total_timeout,
        )
    except PlanError as error:
        reason = (
            BASELINE_EXIT_REASON_MALFORMED_CONTRACT
            if "contract" in str(error).lower()
            else BASELINE_EXIT_REASON_PLAN_STRUCTURE
        )
        _emit_baseline_error(
            want_json=want_json,
            reason=reason,
            error=str(error),
            **common_fields,
        )
        return 1

    payload_fields = _run_result_payload(
        result,
        plan_dir_arg=str(plan_dir_arg),
        cwd_arg=str(cwd_arg),
        per_check_timeout=per_check_timeout,
        total_timeout=total_timeout,
    )

    if not result.outcomes:
        reason = BASELINE_EXIT_REASON_NO_CHECKS
    elif result.all_satisfied:
        reason = BASELINE_EXIT_REASON_OK
    else:
        reason = BASELINE_EXIT_REASON_UNSATISFIED

    exit_code = 0 if reason in (BASELINE_EXIT_REASON_OK, BASELINE_EXIT_REASON_NO_CHECKS) else 1

    if want_json:
        if exit_code == 0:
            responses.print_json(
                responses.success_payload("baseline", reason=reason, **payload_fields)
            )
        else:
            responses.print_json(
                responses.error_payload(
                    "baseline",
                    reason=reason,
                    error=(
                        f"{payload_fields['unsatisfied_count']} of "
                        f"{payload_fields['check_count']} baseline check(s) "
                        "did not satisfy their declared expectation"
                    ),
                    paused=False,
                    **payload_fields,
                )
            )
    else:
        _print_text_baseline(result, plan_dir_arg=str(plan_dir_arg))
        if exit_code != 0:
            print(
                f"agent-session-loop baseline: {payload_fields['unsatisfied_count']} "
                f"of {payload_fields['check_count']} baseline check(s) unsatisfied",
                file=sys.stderr,
            )

    return exit_code


def _emit_baseline_error(
    *,
    want_json: bool,
    reason: str,
    error: str,
    plan_dir: str,
    cwd: str,
    per_check_timeout: float | None,
    total_timeout: float | None,
) -> None:
    if want_json:
        responses.print_json(
            responses.error_payload(
                "baseline",
                reason=reason,
                error=error,
                paused=False,
                plan_dir=plan_dir,
                cwd=cwd,
                per_check_timeout=per_check_timeout,
                total_timeout=total_timeout,
            )
        )
    else:
        print(f"agent-session-loop baseline: {error}", file=sys.stderr)
