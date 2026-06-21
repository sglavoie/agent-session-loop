"""Execute the ``Verify`` recipe for a session's completion contract.

The executor consumes a parsed :class:`contracts.CompletionContract` and runs
each :class:`contracts.VerificationCheck` against a caller-supplied repo root,
returning structured pass/fail results that the CLI and runner enforcement can
serialize to JSON.

Command checks always use ``shell=False`` and an argv list; recipe-time
validation (``contracts.parse_completion_contract``) already enforces argv
non-emptiness, valid regex, positive timeouts within the documented cap, and
unknown-type rejection, so this module focuses on translating execution-time
failures into structured ``CheckResult`` rows.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Literal

from . import plans, responses
from .contracts import (
    CommandCheck,
    CompletionContract,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
    read_completion_contract,
)
from .plans import SessionId, canonicalize_path


CheckStatus = Literal["pass", "fail"]
FailureReason = Literal[
    "command-failed",
    "command-timeout",
    "command-not-found",
    "command-error",
    "file-missing",
    "file-error",
    "regex-not-matched",
]


@dataclass(frozen=True)
class CheckResult:
    """Structured outcome for a single verification check."""

    name: str
    kind: str
    status: CheckStatus
    detail: str = ""
    failure_reason: FailureReason | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class VerificationResult:
    """Aggregated result of running every check in a recipe."""

    session_id: SessionId
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.status == "pass" for check in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.status != "pass")


@dataclass(frozen=True)
class CheckStartedEvent:
    """Observer payload emitted before a single check executes."""

    session_id: SessionId
    check_index: int
    check_count: int
    name: str
    kind: str
    argv: tuple[str, ...] | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class CheckFinishedEvent:
    """Observer payload emitted after a single check finishes."""

    session_id: SessionId
    check_index: int
    check_count: int
    name: str
    kind: str
    result: CheckResult


CheckStartedCallback = Callable[[CheckStartedEvent], None]
CheckFinishedCallback = Callable[[CheckFinishedEvent], None]


@dataclass(frozen=True)
class VerificationObserver:
    """Optional callbacks invoked around each check in :func:`run_verification`.

    Each callback is fired synchronously, in declared check order, on the same
    thread that runs the check. ``on_check_started`` is invoked before the
    check executes; ``on_check_finished`` is invoked after the check completes
    (including failures). Either field may be ``None`` to skip that event.
    Exceptions raised by a callback propagate to the caller.
    """

    on_check_started: CheckStartedCallback | None = None
    on_check_finished: CheckFinishedCallback | None = None


def _make_check_started_event(
    session_id: SessionId,
    check: VerificationCheck,
    *,
    index: int,
    count: int,
    repo_root: Path,
) -> CheckStartedEvent:
    argv: tuple[str, ...] | None = None
    cwd: str | None = None
    if isinstance(check, CommandCheck):
        argv = tuple(check.argv)
        resolved = (
            repo_root if check.cwd is None
            else _resolve_under_root(repo_root, check.cwd)
        )
        cwd = str(resolved)
    return CheckStartedEvent(
        session_id=session_id,
        check_index=index,
        check_count=count,
        name=check.name,
        kind=check.kind,
        argv=argv,
        cwd=cwd,
    )


def _resolve_under_root(repo_root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        return candidate
    return repo_root / candidate


def _run_command_check(check: CommandCheck, *, repo_root: Path) -> CheckResult:
    cwd = repo_root if check.cwd is None else _resolve_under_root(repo_root, check.cwd)
    start = time.monotonic()
    try:
        completed = subprocess.run(
            list(check.argv),
            cwd=str(cwd),
            timeout=check.timeout_seconds,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        duration = time.monotonic() - start
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="command-timeout",
            detail=(
                f"command timed out after {check.timeout_seconds:g}s: "
                f"{' '.join(check.argv)}"
            ),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )
    except FileNotFoundError as error:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="command-not-found",
            detail=f"command not found: {check.argv[0]}: {error.strerror or error}",
            duration_seconds=time.monotonic() - start,
        )
    except OSError as error:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="command-error",
            detail=f"command could not be executed: {error}",
            duration_seconds=time.monotonic() - start,
        )

    duration = time.monotonic() - start
    if completed.returncode == 0:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="pass",
            detail=f"command exited 0: {' '.join(check.argv)}",
            returncode=0,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=duration,
        )
    return CheckResult(
        name=check.name,
        kind=check.kind,
        status="fail",
        failure_reason="command-failed",
        detail=(
            f"command exited {completed.returncode}: {' '.join(check.argv)}"
        ),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_seconds=duration,
    )


def _run_file_exists_check(
    check: FileExistsCheck, *, repo_root: Path
) -> CheckResult:
    target = _resolve_under_root(repo_root, check.path)
    start = time.monotonic()
    try:
        exists = target.exists()
    except OSError as error:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="file-error",
            detail=f"cannot stat {check.path}: {error}",
            duration_seconds=time.monotonic() - start,
        )
    duration = time.monotonic() - start
    if exists:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="pass",
            detail=f"file exists: {check.path}",
            duration_seconds=duration,
        )
    return CheckResult(
        name=check.name,
        kind=check.kind,
        status="fail",
        failure_reason="file-missing",
        detail=f"file does not exist: {check.path}",
        duration_seconds=duration,
    )


def _run_regex_in_file_check(
    check: RegexInFileCheck, *, repo_root: Path
) -> CheckResult:
    target = _resolve_under_root(repo_root, check.path)
    start = time.monotonic()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="file-missing",
            detail=f"file does not exist: {check.path}",
            duration_seconds=time.monotonic() - start,
        )
    except OSError as error:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="file-error",
            detail=f"cannot read {check.path}: {error}",
            duration_seconds=time.monotonic() - start,
        )
    pattern = re.compile(check.pattern, re.MULTILINE)
    duration = time.monotonic() - start
    if pattern.search(text) is not None:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="pass",
            detail=f"pattern matched in {check.path}",
            duration_seconds=duration,
        )
    return CheckResult(
        name=check.name,
        kind=check.kind,
        status="fail",
        failure_reason="regex-not-matched",
        detail=f"pattern did not match in {check.path}: {check.pattern}",
        duration_seconds=duration,
    )


def run_check(check: VerificationCheck, *, repo_root: str | Path) -> CheckResult:
    """Execute a single verification check and return a structured result."""
    canonical_root = canonicalize_path(repo_root)
    if isinstance(check, CommandCheck):
        return _run_command_check(check, repo_root=canonical_root)
    if isinstance(check, FileExistsCheck):
        return _run_file_exists_check(check, repo_root=canonical_root)
    if isinstance(check, RegexInFileCheck):
        return _run_regex_in_file_check(check, repo_root=canonical_root)
    raise TypeError(f"unsupported verification check: {type(check).__name__}")


def run_verification(
    contract: CompletionContract,
    *,
    repo_root: str | Path,
    observer: VerificationObserver | None = None,
) -> VerificationResult:
    """Execute every check in ``contract.verify`` against ``repo_root``.

    When ``observer`` is provided, its ``on_check_started`` and
    ``on_check_finished`` callbacks are invoked synchronously around each
    check in declared order. The CLI behavior and JSON payload of
    :func:`verify_from_args` are unchanged when no observer is passed.
    """
    canonical_root = canonicalize_path(repo_root)
    on_started = observer.on_check_started if observer is not None else None
    on_finished = observer.on_check_finished if observer is not None else None
    checks = contract.verify.checks
    check_count = len(checks)
    results: list[CheckResult] = []
    for index, check in enumerate(checks):
        if on_started is not None:
            on_started(
                _make_check_started_event(
                    contract.session_id,
                    check,
                    index=index,
                    count=check_count,
                    repo_root=canonical_root,
                )
            )
        check_result = run_check(check, repo_root=canonical_root)
        results.append(check_result)
        if on_finished is not None:
            on_finished(
                CheckFinishedEvent(
                    session_id=contract.session_id,
                    check_index=index,
                    check_count=check_count,
                    name=check.name,
                    kind=check.kind,
                    result=check_result,
                )
            )
    return VerificationResult(
        session_id=contract.session_id,
        checks=tuple(results),
    )


VERIFY_EXIT_REASON_OK = "ok"
VERIFY_EXIT_REASON_FAILED = "verify-failed"
VERIFY_EXIT_REASON_NO_CONTRACT = "no-contract"
VERIFY_EXIT_REASON_MALFORMED_CONTRACT = "malformed-contract"
VERIFY_EXIT_REASON_INVALID_SESSION = "invalid-session"


def _check_result_payload(result: CheckResult) -> dict[str, Any]:
    return asdict(result)


def _recipe_summary(contract: CompletionContract) -> dict[str, Any]:
    return {
        "check_count": len(contract.verify.checks),
        "kinds": [check.kind for check in contract.verify.checks],
        "names": [check.name for check in contract.verify.checks],
    }


def _print_text_verify_success(
    contract: CompletionContract,
    result: VerificationResult,
    *,
    plan_dir: str,
    cwd: str,
) -> None:
    print(f"Plan: {plan_dir}")
    print(f"Session: {contract.session_id.value}")
    print(f"Repo root: {cwd}")
    print(f"Checks: {len(result.checks)}")
    for check in result.checks:
        marker = "PASS" if check.status == "pass" else "FAIL"
        print(f"  [{marker}] {check.kind} {check.name}: {check.detail}")
        if check.status != "pass":
            if check.stdout:
                snippet = check.stdout.strip().splitlines()[-1] if check.stdout.strip() else ""
                if snippet:
                    print(f"      stdout: {snippet}", file=sys.stderr)
            if check.stderr:
                snippet = check.stderr.strip().splitlines()[-1] if check.stderr.strip() else ""
                if snippet:
                    print(f"      stderr: {snippet}", file=sys.stderr)
    if result.passed:
        print("Verify: passed")
    else:
        print(
            f"Verify: failed ({len(result.failures)} of {len(result.checks)} checks)",
            file=sys.stderr,
        )


def _print_text_no_contract(session_id: SessionId, *, plan_dir: str) -> None:
    print(f"Plan: {plan_dir}")
    print(f"Session: {session_id.value}")
    print("Verify: no completion contract", file=sys.stderr)


def _emit_verify_error(
    *,
    want_json: bool,
    reason: str,
    error: str,
    plan_dir: str,
    session: str,
    cwd: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    if want_json:
        fields: dict[str, Any] = {"plan_dir": plan_dir, "session": session}
        if cwd is not None:
            fields["cwd"] = cwd
        if extra:
            fields.update(extra)
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
        print(f"agent-session-loop verify: {error}", file=sys.stderr)


def verify_from_args(args: argparse.Namespace) -> int:
    """Run the ``verify`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    raw_session = args.session
    cwd_arg = args.cwd
    want_json = args.json

    try:
        session_id = plans.parse_session_id(raw_session)
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
        canonical_cwd = canonicalize_path(cwd_arg)
    except (plans.PlanError, OSError) as error:
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
    except plans.PlanError as error:
        _emit_verify_error(
            want_json=want_json,
            reason=VERIFY_EXIT_REASON_MALFORMED_CONTRACT,
            error=str(error),
            plan_dir=str(plan_dir_arg),
            session=session_id.value,
            cwd=str(canonical_cwd),
        )
        return 1

    if contract is None:
        _emit_verify_error(
            want_json=want_json,
            reason=VERIFY_EXIT_REASON_NO_CONTRACT,
            error=(
                f"session {session_id.value} has no Completion Contract section"
            ),
            plan_dir=str(plan_dir_arg),
            session=session_id.value,
            cwd=str(canonical_cwd),
            extra={
                "passed": False,
                "recipe": {"check_count": 0, "kinds": [], "names": []},
                "checks": [],
            },
        )
        if not want_json:
            _print_text_no_contract(session_id, plan_dir=str(plan_dir_arg))
        return 1

    result = run_verification(contract, repo_root=canonical_cwd)

    if want_json:
        if result.passed:
            responses.print_json(
                responses.success_payload(
                    "verify",
                    reason=VERIFY_EXIT_REASON_OK,
                    plan_dir=str(plan_dir_arg),
                    session=result.session_id.value,
                    cwd=str(canonical_cwd),
                    passed=True,
                    recipe=_recipe_summary(contract),
                    checks=[_check_result_payload(check) for check in result.checks],
                )
            )
        else:
            responses.print_json(
                responses.error_payload(
                    "verify",
                    reason=VERIFY_EXIT_REASON_FAILED,
                    error=(
                        f"{len(result.failures)} of {len(result.checks)} "
                        "verification check(s) failed"
                    ),
                    paused=False,
                    plan_dir=str(plan_dir_arg),
                    session=result.session_id.value,
                    cwd=str(canonical_cwd),
                    passed=False,
                    recipe=_recipe_summary(contract),
                    checks=[_check_result_payload(check) for check in result.checks],
                )
            )
    else:
        _print_text_verify_success(
            contract, result, plan_dir=str(plan_dir_arg), cwd=str(canonical_cwd)
        )

    return 0 if result.passed else 1
