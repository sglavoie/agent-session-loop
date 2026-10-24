"""The ``asl verify`` subcommand: run one session's recipe and report it."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from typing import Any

from . import plans_errors, plans_session_ids, responses
from .cli_defaults import program_name
from .contracts import CompletionContract, read_completion_contract
from .plans_session_ids import SessionId
from .verification_models import (
    CheckResult,
    VerificationContext,
    VerificationResult,
    verification_context_from_plan,
)
from .verification_run import run_verification

VERIFY_EXIT_REASON_OK = "ok"
VERIFY_EXIT_REASON_FAILED = "verify-failed"
VERIFY_EXIT_REASON_NO_CONTRACT = "no-contract"
VERIFY_EXIT_REASON_MALFORMED_CONTRACT = "malformed-contract"
VERIFY_EXIT_REASON_INVALID_SESSION = "invalid-session"


def _check_result_payload(result: CheckResult) -> dict[str, Any]:
    return asdict(result)


def _verification_context_payload(
    context: VerificationContext,
) -> dict[str, Any]:
    if context.workspace is None:
        return {}
    return {
        "workspace_root": str(context.root),
        "registry_digest": context.registry_digest,
        "repositories": [
            {"alias": repository.alias, "resolved_root": str(repository.root)}
            for repository in context.workspace.repositories
        ],
    }


def _recipe_summary(contract: CompletionContract) -> dict[str, Any]:
    diagnostic_count = sum(
        bool(check.on_failure_of) for check in contract.verify.checks
    )
    return {
        "check_count": len(contract.verify.checks),
        "gate_count": len(contract.verify.checks) - diagnostic_count,
        "diagnostic_count": diagnostic_count,
        "kinds": [check.kind for check in contract.verify.checks],
        "names": [check.name for check in contract.verify.checks],
        "repos": [check.repo for check in contract.verify.checks],
    }


def _print_text_verify_success(
    contract: CompletionContract,
    result: VerificationResult,
    *,
    plan_dir: str,
    cwd: str,
    context: VerificationContext | None = None,
) -> None:
    print(f"Plan: {plan_dir}")
    print(f"Session: {contract.session_id.value}")
    if context is not None and context.workspace is not None:
        print(f"Workspace root: {cwd}")
        print(
            "Repositories: "
            + ", ".join(
                repository.alias for repository in context.workspace.repositories
            )
        )
    else:
        print(f"Repo root: {cwd}")
    print(f"Checks: {result.configured_check_count}")
    for check in result.checks:
        marker = "PASS" if check.status == "pass" else "FAIL"
        repository = f" {check.repo}:" if check.repo else ""
        print(f"  [{marker}]{repository} {check.kind} {check.name}: {check.detail}")
        if check.status != "pass":
            if check.stdout:
                snippet = (
                    check.stdout.strip().splitlines()[-1]
                    if check.stdout.strip()
                    else ""
                )
                if snippet:
                    print(f"      stdout: {snippet}", file=sys.stderr)
            if check.stderr:
                snippet = (
                    check.stderr.strip().splitlines()[-1]
                    if check.stderr.strip()
                    else ""
                )
                if snippet:
                    print(f"      stderr: {snippet}", file=sys.stderr)
    for skipped in result.skipped_checks:
        dependencies = ", ".join(skipped.on_failure_of)
        print(
            f"  [SKIP] {skipped.kind} {skipped.name}: {skipped.reason} "
            f"(on failure of: {dependencies})"
        )
    if result.passed:
        print("Verify: passed")
    else:
        print(
            f"Verify: failed ({len(result.failures)} of {result.gate_count} gates)",
            file=sys.stderr,
        )


def _print_text_no_contract(session_id: SessionId, *, plan_dir: str) -> None:
    print(f"Plan: {plan_dir}")
    print(f"Session: {session_id.value}")
    print("Verify: no completion contract", file=sys.stderr)


def _no_contract_skill_remediation(plan_dir: str) -> dict[str, Any]:
    """Manual-mode remediation for a missing Completion Contract.

    ``session-run`` has no ``--allow-not-loop-ready`` equivalent and fails
    closed on ``no-contract``. This names both escape hatches with the plan
    directory substituted: bypass the requirement via ``asl run
    --allow-not-loop-ready`` or repair the plan and re-mark it loop-ready via
    ``asl preflight --mark-loop-ready``. Both commands are rendered through the
    shared shell-safe :func:`agent_session_loop.responses.format_command`
    builder, so a plan directory containing whitespace or shell metacharacters
    stays a single argument. The steps use Session 6's shared ``responses``
    builder so every surface describes next actions with one payload shape.
    """
    run_command = responses.format_command(
        ["asl", "run", plan_dir, "--allow-not-loop-ready"]
    )
    preflight_command = responses.format_command(
        ["asl", "preflight", plan_dir, "--json", "--mark-loop-ready"]
    )
    steps = [
        responses.command_step(run_command),
        responses.command_step(preflight_command),
    ]
    message = (
        "session-run has no bypass flag and fails closed when a session has "
        "no `## Completion Contract`. Bypass the requirement with "
        f"`{run_command}`, or add a contract and re-mark the plan loop-ready "
        f"with `{preflight_command}`."
    )
    return {"message": message, **responses.next_steps_payload(steps)}


def _verify_remediation_fields(
    *,
    reason: str,
    failure_reason: str | None,
    plan_dir: str,
    cwd: str | None,
) -> dict[str, Any]:
    """Attach a ``repair_hint`` (and ``no-contract`` ``skill_remediation``).

    The hint mirrors ``workbench``'s runtime-recovery usage of
    :func:`agent_session_loop.preflight_runtime_hints.runtime_repair_hint`; the
    import is deferred because ``preflight`` imports the verification modules.
    """
    from . import preflight_auto_repair, preflight_runtime_hints

    fields: dict[str, Any] = {}
    hint = preflight_runtime_hints.runtime_repair_hint(
        reason, failure_reason=failure_reason
    )
    if hint is not None:
        fields["repair_hint"] = preflight_auto_repair.repair_hint_payload(
            hint, plan_dir=plan_dir, cwd=cwd
        )
    if reason == VERIFY_EXIT_REASON_NO_CONTRACT:
        fields["skill_remediation"] = _no_contract_skill_remediation(plan_dir)
    return fields


def _emit_verify_error(
    *,
    want_json: bool,
    reason: str,
    error: str,
    plan_dir: str,
    session: str,
    cwd: str | None,
    extra: dict[str, Any] | None = None,
    failure_reason: str | None = None,
) -> None:
    if want_json:
        fields: dict[str, Any] = {"plan_dir": plan_dir, "session": session}
        if cwd is not None:
            fields["cwd"] = cwd
        if extra:
            fields.update(extra)
        fields.update(
            _verify_remediation_fields(
                reason=reason,
                failure_reason=failure_reason,
                plan_dir=plan_dir,
                cwd=cwd,
            )
        )
        responses.print_json(
            responses.error_payload(
                "verify",
                reason=reason,
                error=error,
                paused=False,
                **fields,
            )
        )
    else:
        print(f"{program_name()} verify: {error}", file=sys.stderr)


def verify_from_args(args: argparse.Namespace) -> int:
    """Run the ``verify`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    raw_session = args.session
    cwd_arg = args.cwd
    want_json = args.json
    artifact_dir_arg = getattr(args, "artifact_dir", None)

    try:
        session_id = plans_session_ids.parse_session_id(raw_session)
    except ValueError as error:
        _emit_verify_error(
            want_json=want_json,
            reason=VERIFY_EXIT_REASON_INVALID_SESSION,
            error=str(error),
            plan_dir=str(plan_dir_arg),
            session=str(raw_session),
            cwd=None,
        )
        return 1

    try:
        verification_context = verification_context_from_plan(plan_dir_arg, cwd_arg)
    except (plans_errors.PlanError, OSError) as error:
        _emit_verify_error(
            want_json=want_json,
            reason="cwd-error",
            error=str(error),
            plan_dir=str(plan_dir_arg),
            session=session_id.value,
            cwd=str(cwd_arg),
        )
        return 1

    try:
        contract = read_completion_contract(plan_dir_arg, session_id)
    except plans_errors.PlanError as error:
        _emit_verify_error(
            want_json=want_json,
            reason=VERIFY_EXIT_REASON_MALFORMED_CONTRACT,
            error=str(error),
            plan_dir=str(plan_dir_arg),
            session=session_id.value,
            cwd=str(verification_context.root),
        )
        return 1

    if contract is None:
        _emit_verify_error(
            want_json=want_json,
            reason=VERIFY_EXIT_REASON_NO_CONTRACT,
            error=(f"session {session_id.value} has no Completion Contract section"),
            plan_dir=str(plan_dir_arg),
            session=session_id.value,
            cwd=str(verification_context.root),
            extra={
                "passed": False,
                "recipe": {
                    "check_count": 0,
                    "gate_count": 0,
                    "diagnostic_count": 0,
                    "kinds": [],
                    "names": [],
                },
                "checks": [],
                "skipped_checks": [],
                "diagnostic_failures": [],
            },
        )
        if not want_json:
            _print_text_no_contract(session_id, plan_dir=str(plan_dir_arg))
        return 1

    for check in contract.verify.checks:
        try:
            verification_context.resolve_check(check)
        except plans_errors.PlanError as error:
            _emit_verify_error(
                want_json=want_json,
                reason=VERIFY_EXIT_REASON_MALFORMED_CONTRACT,
                error=str(error),
                plan_dir=str(plan_dir_arg),
                session=session_id.value,
                cwd=str(verification_context.root),
                extra={
                    **_verification_context_payload(verification_context),
                    "passed": False,
                    "check": check.name,
                    "repo": check.repo,
                    "recipe": _recipe_summary(contract),
                    "checks": [
                        {
                            "name": check.name,
                            "type": check.kind,
                            "repo": check.repo,
                            "status": "error",
                            "failure_reason": "repository-contract",
                            "detail": str(error),
                        }
                    ],
                    "skipped_checks": [],
                    "diagnostic_failures": [],
                },
            )
            return 1

    try:
        result = run_verification(
            contract,
            context=verification_context,
            artifact_dir=artifact_dir_arg,
        )
    except plans_errors.PlanError as error:
        _emit_verify_error(
            want_json=want_json,
            reason=VERIFY_EXIT_REASON_MALFORMED_CONTRACT,
            error=str(error),
            plan_dir=str(plan_dir_arg),
            session=session_id.value,
            cwd=str(verification_context.root),
            extra={
                **_verification_context_payload(verification_context),
                "passed": False,
                "recipe": _recipe_summary(contract),
                "checks": [],
                "skipped_checks": [],
                "diagnostic_failures": [],
            },
        )
        return 1

    if want_json:
        if result.passed:
            responses.print_json(
                responses.success_payload(
                    "verify",
                    reason=VERIFY_EXIT_REASON_OK,
                    plan_dir=str(plan_dir_arg),
                    session=result.session_id.value,
                    cwd=str(verification_context.root),
                    **_verification_context_payload(verification_context),
                    passed=True,
                    recipe=_recipe_summary(contract),
                    checks=[_check_result_payload(check) for check in result.checks],
                    skipped_checks=[asdict(check) for check in result.skipped_checks],
                    diagnostic_failures=[],
                )
            )
        else:
            first_failure = result.failures[0] if result.failures else None
            _emit_verify_error(
                want_json=True,
                reason=VERIFY_EXIT_REASON_FAILED,
                error=(
                    f"{len(result.failures)} of {result.gate_count} "
                    "verification check(s) failed"
                ),
                plan_dir=str(plan_dir_arg),
                session=result.session_id.value,
                cwd=str(verification_context.root),
                failure_reason=(
                    first_failure.failure_reason if first_failure is not None else None
                ),
                extra={
                    **_verification_context_payload(verification_context),
                    "passed": False,
                    "recipe": _recipe_summary(contract),
                    "checks": [_check_result_payload(check) for check in result.checks],
                    "skipped_checks": [
                        asdict(check) for check in result.skipped_checks
                    ],
                    "diagnostic_failures": [
                        _check_result_payload(check)
                        for check in result.diagnostic_failures
                    ],
                },
            )
    else:
        _print_text_verify_success(
            contract,
            result,
            plan_dir=str(plan_dir_arg),
            cwd=str(verification_context.root),
            context=verification_context,
        )

    return 0 if result.passed else 1
