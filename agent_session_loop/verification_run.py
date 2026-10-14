"""Execute a completion contract's ``Verify`` recipe as gates plus diagnostics."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .contracts import CommandCheck, CompletionContract
from .verification_checks import run_check
from .verification_models import (
    CheckFinishedEvent,
    CheckResult,
    CheckSkippedEvent,
    SkippedCheck,
    VerificationContext,
    VerificationObserver,
    VerificationResult,
    _coerce_verification_context,
    _make_check_started_event,
    check_role,
)
from .verification_process import _ParallelCancellation, _VerificationCancelled


def run_verification(
    contract: CompletionContract,
    *,
    repo_root: str | Path | None = None,
    context: VerificationContext | None = None,
    observer: VerificationObserver | None = None,
    artifact_dir: str | Path | None = None,
) -> VerificationResult:
    """Execute every check through a legacy root or resolved workspace context.

    When ``observer`` is provided, its ``on_check_started`` and
    ``on_check_finished`` callbacks are invoked synchronously around each
    check. Under a sequential recipe they arrive in declared order on the
    calling thread; under ``contract.verify.parallel`` the command-check
    callbacks arrive from worker threads in completion order, so an observer
    must be thread-safe. The CLI behavior and JSON payload of
    :func:`agent_session_loop.verification_cli.verify_from_args` are unchanged
    when no observer is passed.

    ``artifact_dir`` — when set — is forwarded to each command check so failed
    commands retain their full stdout/stderr as gzip artifacts there; without
    it, only bounded previews and metadata are recorded.

    Checks without ``on_failure_of`` run first as authoritative gates. A
    conditional diagnostic runs afterward only when at least one named gate
    failed; otherwise ``on_check_skipped`` receives a structured skip event.
    Diagnostics never determine the aggregate pass/fail result.

    Within each phase, a ``parallel`` recipe with at least two command checks
    runs its file and regex checks sequentially in declared order first, then
    dispatches the command checks concurrently. Executed and skipped results
    are always assembled by declared index.

    Interrupting a parallel dispatch (``KeyboardInterrupt`` on the calling
    thread, or any exception surfacing from a worker) kills every command
    process still in flight, cancels every queued check, and joins the worker
    threads before re-raising. Abandoned checks therefore publish nothing: no
    result, no artifact, and no ``on_check_finished`` callback can arrive after
    the exception reaches the caller.
    """
    verification_context = _coerce_verification_context(
        repo_root=repo_root, context=context
    )
    resolved_artifact_dir = None if artifact_dir is None else Path(artifact_dir)
    on_started = observer.on_check_started if observer is not None else None
    on_finished = observer.on_check_finished if observer is not None else None
    on_skipped = observer.on_check_skipped if observer is not None else None
    checks = contract.verify.checks
    check_count = len(checks)
    gate_indexes = [
        index for index, check in enumerate(checks) if not check.on_failure_of
    ]
    diagnostic_indexes = [
        index for index, check in enumerate(checks) if check.on_failure_of
    ]
    cancellation = _ParallelCancellation() if contract.verify.parallel else None

    def execute(index: int) -> CheckResult:
        check = checks[index]
        resolved = verification_context.resolve_check(check)
        if cancellation is not None and cancellation.cancelled:
            raise _VerificationCancelled(check.name)
        if on_started is not None:
            on_started(
                _make_check_started_event(
                    contract.session_id,
                    check,
                    index=index,
                    count=check_count,
                    resolved=resolved,
                )
            )
        check_result = run_check(
            check,
            context=verification_context,
            artifact_dir=resolved_artifact_dir,
            cancellation=cancellation,
        )
        if cancellation is not None and cancellation.cancelled:
            raise _VerificationCancelled(check.name)
        if on_finished is not None:
            on_finished(
                CheckFinishedEvent(
                    session_id=contract.session_id,
                    check_index=index,
                    check_count=check_count,
                    name=check.name,
                    kind=check.kind,
                    result=check_result,
                    role=check_role(check),
                    repo=check.repo,
                    resolved_root=str(resolved.resolved_root),
                    cwd=str(resolved.cwd) if resolved.cwd is not None else None,
                    qualified_cwd=resolved.qualified_cwd,
                    path=str(resolved.path) if resolved.path is not None else None,
                    qualified_path=resolved.qualified_path,
                )
            )
        return check_result

    results_by_index: dict[int, CheckResult] = {}

    def execute_phase(indexes: list[int]) -> None:
        command_indexes = [
            index for index in indexes if isinstance(checks[index], CommandCheck)
        ]
        dispatch_parallel = contract.verify.parallel and len(command_indexes) >= 2
        if not dispatch_parallel:
            for index in indexes:
                results_by_index[index] = execute(index)
            return

        deferred = set(command_indexes)
        for index in indexes:
            if index not in deferred:
                results_by_index[index] = execute(index)
        executor = ThreadPoolExecutor(max_workers=min(4, len(command_indexes)))
        try:
            futures = {
                executor.submit(execute, index): index for index in command_indexes
            }
            for future in as_completed(futures):
                results_by_index[futures[future]] = future.result()
        except BaseException:
            assert cancellation is not None
            # Kill first, then wait: killing makes the in-flight checks return
            # at once, and the wait is what guarantees no abandoned worker
            # outlives this raise to emit a callback or write an artifact.
            cancellation.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        executor.shutdown(wait=True)

    execute_phase(gate_indexes)
    failed_gate_names = {
        checks[index].name
        for index in gate_indexes
        if results_by_index[index].status != "pass"
    }
    eligible_diagnostics: list[int] = []
    skipped_by_index: dict[int, SkippedCheck] = {}
    for index in diagnostic_indexes:
        check = checks[index]
        if failed_gate_names.intersection(check.on_failure_of):
            eligible_diagnostics.append(index)
            continue
        resolved = verification_context.resolve_check(check)
        skipped = SkippedCheck(
            name=check.name,
            kind=check.kind,
            on_failure_of=check.on_failure_of,
            repo=check.repo,
            resolved_root=str(resolved.resolved_root),
            cwd=str(resolved.cwd) if resolved.cwd is not None else None,
            qualified_cwd=resolved.qualified_cwd,
            path=str(resolved.path) if resolved.path is not None else None,
            qualified_path=resolved.qualified_path,
        )
        skipped_by_index[index] = skipped
        if on_skipped is not None:
            on_skipped(
                CheckSkippedEvent(
                    session_id=contract.session_id,
                    check_index=index,
                    check_count=check_count,
                    name=check.name,
                    kind=check.kind,
                    on_failure_of=check.on_failure_of,
                    reason=skipped.reason,
                    repo=check.repo,
                    resolved_root=str(resolved.resolved_root),
                    cwd=str(resolved.cwd) if resolved.cwd is not None else None,
                    qualified_cwd=resolved.qualified_cwd,
                    path=str(resolved.path) if resolved.path is not None else None,
                    qualified_path=resolved.qualified_path,
                )
            )

    execute_phase(eligible_diagnostics)
    results = [results_by_index[index] for index in sorted(results_by_index)]
    skipped_checks = [skipped_by_index[index] for index in sorted(skipped_by_index)]
    return VerificationResult(
        session_id=contract.session_id,
        checks=tuple(results),
        skipped_checks=tuple(skipped_checks),
        diagnostic_names=tuple(checks[index].name for index in diagnostic_indexes),
    )
