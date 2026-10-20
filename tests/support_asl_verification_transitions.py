"""Shared fixtures for test_asl_verification_transitions."""

from __future__ import annotations

from agent_session_loop import (
    contracts,
    plans_session_ids,
    verification_models,
    verification_transitions,
)

MACHINE = verification_transitions.VERIFICATION_MACHINE


DEFERRED_REASON = verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON


def _check(
    name: str,
    *,
    status: str = "fail",
    kind: str = "command",
    failure_reason: str | None = "exit-status",
    returncode: int | None = 1,
    stdout_sha256: str | None = "aa",
    stderr_sha256: str | None = "bb",
) -> verification_models.CheckResult:
    return verification_models.CheckResult(
        name=name,
        kind=kind,
        status=status,
        returncode=returncode,
        duration_seconds=0.0,
        failure_reason=failure_reason,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
    )


def _result(
    *checks: verification_models.CheckResult,
    session_id: str = "1",
) -> verification_models.VerificationResult:
    return verification_models.VerificationResult(
        session_id=plans_session_ids.SessionId.parse(session_id),
        checks=list(checks),
        skipped_checks=[],
    )


def _contract(*names: str) -> contracts.CompletionContract:
    return contracts.CompletionContract(
        session_id=plans_session_ids.SessionId.parse("1"),
        done_when=("done",),
        verify=contracts.VerifyRecipe(
            checks=tuple(
                contracts.CommandCheck(name=name, argv=("true",), timeout_seconds=1)
                for name in names
            ),
        ),
    )
