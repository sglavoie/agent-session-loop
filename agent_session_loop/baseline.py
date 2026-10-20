"""Execute completion-contract baseline recipes and record their outcomes.

Session 7 delivered the parsed ``baseline_expectation`` field, the clamped
per-check/total budgets, and the :class:`~agent_session_loop.baseline_cache.
BaselineCache` store. This module is the *execution* engine that consumes them:
it walks a plan's completion contracts, collects the checks that declare a
non-``skip`` baseline expectation, deduplicates equivalent checks across
sessions, runs each one under the clamped baseline budget, and records the
pass/fail/timeout outcome plus repository evidence into the cache. Registered
plans aggregate every declared repository; legacy plans retain one fingerprint.

The recorded evidence is what the preflight readiness gate consults (without
ever running commands itself) to decide whether a pre-existing broad-suite
failure should block before implementation. See
:func:`agent_session_loop.preflight_baseline_checks._check_baselines`.

Storage uses a *fingerprint-independent identity key* (recipe + cwd +
executable + environment, plus a registry digest when present, with repository
fingerprints deliberately excluded) so a recorded outcome remains findable
after the working tree changes; the fingerprint captured at record time lives
inside the stored result, letting preflight distinguish a *missing* baseline
from a *stale* one.

This module owns the ``baseline`` subcommand entry point and re-exports the
public API; the sibling modules own the code: ``baseline_evidence`` (identity
keys, fingerprints, check collection), ``baseline_models`` (outcomes and
statuses), ``baseline_run`` (execution; patch ``run_check`` there) and
``baseline_report`` (payloads and text output).

The module is stdlib-only.
"""

from __future__ import annotations

import sys
from typing import Any, TypedDict

from . import responses
from .baseline_cache import BASELINE_RESULTS_FILENAME as BASELINE_RESULTS_FILENAME
from .baseline_cache import BaselineCache as BaselineCache
from .baseline_cache import BaselineRecord as BaselineRecord
from .baseline_cache import normalize_recipe as normalize_recipe
from .baseline_cache import repository_fingerprint as repository_fingerprint
from .baseline_evidence import (
    _registered_repository_fingerprint as _registered_repository_fingerprint,
)
from .baseline_evidence import _single_check_recipe as _single_check_recipe
from .baseline_evidence import (
    aggregate_repository_fingerprint as aggregate_repository_fingerprint,
)
from .baseline_evidence import (
    baseline_evidence_is_current as baseline_evidence_is_current,
)
from .baseline_evidence import baseline_identity_key as baseline_identity_key
from .baseline_evidence import (
    capture_baseline_repository_evidence as capture_baseline_repository_evidence,
)
from .baseline_evidence import collect_baseline_checks as collect_baseline_checks
from .baseline_models import (
    BASELINE_EXIT_REASON_CWD_ERROR,
    BASELINE_EXIT_REASON_MALFORMED_CONTRACT,
    BASELINE_EXIT_REASON_NO_CHECKS,
    BASELINE_EXIT_REASON_OK,
    BASELINE_EXIT_REASON_PLAN_STRUCTURE,
    BASELINE_EXIT_REASON_UNSATISFIED,
)
from .baseline_models import NEAR_TIMEOUT_FRACTION as NEAR_TIMEOUT_FRACTION
from .baseline_models import BaselineCheckOutcome as BaselineCheckOutcome
from .baseline_models import BaselineRunResult as BaselineRunResult
from .baseline_models import BaselineStatus as BaselineStatus
from .baseline_report import _print_text_baseline, _run_result_payload
from .baseline_run import run_baseline as run_baseline
from .plans_errors import PlanError
from .verification_models import verification_context_from_plan


class _BaselineErrorFields(TypedDict):
    """The four request facts every ``baseline`` error payload repeats.

    Written down as a type rather than an inline dict because the dict is
    splatted into :func:`_emit_baseline_error`: without the annotation the
    argparse values widen to ``Any | None`` and the callee's parameter types
    stop being checked at the only two sites that use them.
    """

    plan_dir: str
    cwd: str
    per_check_timeout: float | None
    total_timeout: float | None


def baseline_from_args(args: Any) -> int:
    """Run the ``baseline`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    cwd_arg = args.cwd
    want_json = args.json
    per_check_timeout = getattr(args, "per_check_timeout", None)
    total_timeout = getattr(args, "total_timeout", None)

    common_fields: _BaselineErrorFields = {
        "plan_dir": str(plan_dir_arg),
        "cwd": str(cwd_arg),
        "per_check_timeout": per_check_timeout,
        "total_timeout": total_timeout,
    }

    try:
        verification_context = verification_context_from_plan(plan_dir_arg, cwd_arg)
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
            context=verification_context,
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
        verification_context=verification_context,
    )

    if not result.outcomes:
        reason = BASELINE_EXIT_REASON_NO_CHECKS
    elif result.all_satisfied:
        reason = BASELINE_EXIT_REASON_OK
    else:
        reason = BASELINE_EXIT_REASON_UNSATISFIED

    exit_code = (
        0 if reason in (BASELINE_EXIT_REASON_OK, BASELINE_EXIT_REASON_NO_CHECKS) else 1
    )

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
