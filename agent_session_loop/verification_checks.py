"""Run a single verification check: command, file-exists, or regex-in-file."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .artifacts import StreamCapture
from .contracts import (
    CommandCheck,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
)
from .verification_command_diagnosis import diagnose_command_scope
from .verification_diagnostics import (
    FAILURE_DIAGNOSTIC_KEY,
    _parse_failure_diagnostic,
)
from .verification_models import (
    CheckResult,
    VerificationContext,
    _coerce_verification_context,
    _resolve_under_root,
)
from .verification_process import (
    _ABANDONED_READER_JOIN_SECONDS,
    _EXITED_READER_JOIN_SECONDS,
    _drain_stream,
    _join_readers,
    _kill_and_reap,
    _ParallelCancellation,
    _snapshot_descendants,
    _sweep_check_descendants,
    _sweep_process_table,
    _VerificationCancelled,
)


def _command_stream_fields(
    check: CommandCheck,
    stdout_capture: StreamCapture,
    stderr_capture: StreamCapture,
    *,
    failed: bool,
    artifact_dir: Path | None,
) -> dict[str, Any]:
    """Finalize both captures into the bounded ``CheckResult`` stream fields.

    Full-stream gzip retention only happens for failed checks with an
    ``artifact_dir``; ``StreamCapture`` fails open on artifact IO errors, so
    verification never crashes on a disk problem and still returns bounded
    metadata (preview + digest) with no artifact path. The
    ``{check.name}-{stream}`` names below flow through
    :func:`agent_session_loop.artifacts.derive_artifact_stem` when the capture
    finalizes, so two checks whose display names sanitize to the same stem
    still write distinct failure artifacts.
    """
    stdout_meta = stdout_capture.finalize(
        failed=failed,
        artifact_dir=artifact_dir,
        artifact_name=f"{check.name}-stdout",
    )
    stderr_meta = stderr_capture.finalize(
        failed=failed,
        artifact_dir=artifact_dir,
        artifact_name=f"{check.name}-stderr",
    )
    return {
        "stdout": stdout_meta.preview,
        "stderr": stderr_meta.preview,
        "stdout_byte_count": stdout_meta.byte_count,
        "stdout_sha256": stdout_meta.sha256,
        "stdout_truncated": stdout_meta.truncated,
        "stdout_artifact_path": stdout_meta.artifact_path,
        "stderr_byte_count": stderr_meta.byte_count,
        "stderr_sha256": stderr_meta.sha256,
        "stderr_truncated": stderr_meta.truncated,
        "stderr_artifact_path": stderr_meta.artifact_path,
        "failure_diagnostic": (
            _parse_failure_diagnostic(stderr_meta.preview, check=check)
            if failed
            else None
        ),
    }


_COMMAND_DIAGNOSTIC_LIMIT = 500


def _command_failure_detail(
    check: CommandCheck,
    returncode: int,
    stream_fields: Mapping[str, Any],
) -> str:
    """Render a failed command's detail from control-plane text alone.

    A validator that reports which predicate rejected the artifact says far
    more than the command itself, so an authenticated gate's rejection leads
    the detail into the pause record and the audit bundle instead of staying
    buried in the streams. A command with no such diagnostic contributes
    nothing but its exit code and its program: its last stream line is
    application output, and its remaining argv is authored text — a `python3
    -c` gate's is kilobytes of inline source — where ``detail`` is inlined into
    a bundle that quotes no other authored body. Both are recoverable at full
    fidelity, the streams from their artifacts and the argv from the contract
    this check's name indexes.
    """
    rendered = f"command exited {returncode}: {check.argv[0]}"
    diagnostic = stream_fields.get(FAILURE_DIAGNOSTIC_KEY)
    if isinstance(diagnostic, Mapping):
        lead = f"{diagnostic['check']}: {diagnostic['reason']}"
        return f"{lead[:_COMMAND_DIAGNOSTIC_LIMIT]}; {rendered}"
    return rendered


def _run_command_check(
    check: CommandCheck,
    *,
    repo_root: Path,
    artifact_dir: Path | None = None,
    cancellation: _ParallelCancellation | None = None,
) -> CheckResult:
    cwd = repo_root if check.cwd is None else _resolve_under_root(repo_root, check.cwd)
    scope_diagnostic = diagnose_command_scope(check)
    if scope_diagnostic is not None:
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="command-overbroad",
            detail=scope_diagnostic.detail,
        )
    start = time.monotonic()
    if cancellation is not None and cancellation.cancelled:
        raise _VerificationCancelled(check.name)
    try:
        process = subprocess.Popen(
            list(check.argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            # Its own session, so every descendant that inherits the check's
            # process group can be killed at once on timeout or cancellation.
            # Descendants that start a session of their own are out of reach:
            # see :func:`verification_process._kill_process_group`.
            start_new_session=True,
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

    # Registration is what lets an interrupt reach this process: it closes the
    # window where a cancellation fired while Popen was still spawning.
    if cancellation is not None and not cancellation.register(process):
        _kill_and_reap(process)
        # No reader thread exists yet, so nothing else will close the pipes.
        # Both are always present -- Popen was given PIPE for each -- but a
        # cleanup path is the last place to turn an absent handle into a crash.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        raise _VerificationCancelled(check.name)

    # Output is bounded as it arrives: each pipe feeds a StreamCapture that
    # keeps only the preview windows, running digest/counts, and — when an
    # artifact directory is set — a gzip spool on disk, so a noisy command can
    # never exhaust runner memory before truncation.
    spool = artifact_dir is not None
    stdout_capture = StreamCapture(spool=spool)
    stderr_capture = StreamCapture(spool=spool)
    timed_out = False
    prior_snapshot: tuple[Mapping[str, Any], ...] = ()
    sweep: dict[str, Any] | None = None
    try:
        readers = (
            threading.Thread(
                target=_drain_stream,
                args=(process.stdout, stdout_capture),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=(process.stderr, stderr_capture),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=check.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            prior_snapshot = _snapshot_descendants(process.pid, _sweep_process_table())
            _kill_and_reap(process)
        cancelled = False
        if cancellation is not None and cancellation.cancelled:
            cancelled = True
            if not timed_out:
                # The kill came from another thread, so the snapshot did too.
                prior_snapshot = cancellation.snapshot_for(process)
        killed = timed_out or cancelled
        _join_readers(
            readers,
            timeout=(
                _ABANDONED_READER_JOIN_SECONDS
                if killed
                else _EXITED_READER_JOIN_SECONDS
            ),
        )
        # After the join, so an in-group straggler gets to flush what it was
        # writing before the sweep takes it down.
        sweep = _sweep_check_descendants(process, prior_snapshot)
    except BaseException:
        # An interrupt under a sequential recipe lands on this thread while the
        # check is still running, so this branch owns its own snapshot: taken
        # here the root is still alive, and after the group kill an escapee is
        # reparented to pid 1 and no walk can reach it. A snapshot the timeout
        # or cancel path already recorded is carried through rather than
        # replaced -- it predates a kill that has already landed.
        prior_snapshot = (
            *prior_snapshot,
            *_snapshot_descendants(process.pid, _sweep_process_table()),
        )
        _kill_and_reap(process)
        _sweep_check_descendants(process, prior_snapshot)
        stdout_capture.discard()
        stderr_capture.discard()
        raise
    finally:
        if cancellation is not None:
            cancellation.unregister(process)

    # A cancelled check publishes nothing: its output is dropped rather than
    # finalized, so no artifact is written for a process the interrupt killed.
    if cancellation is not None and cancellation.cancelled:
        stdout_capture.discard()
        stderr_capture.discard()
        raise _VerificationCancelled(check.name)

    duration = time.monotonic() - start
    if timed_out:
        # Partial output captured before the timeout is retained under the
        # failure policy (gzip artifact when an artifact directory is set).
        stream_fields = _command_stream_fields(
            check,
            stdout_capture,
            stderr_capture,
            failed=True,
            artifact_dir=artifact_dir,
        )
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="fail",
            failure_reason="command-timeout",
            detail=(
                f"command timed out after {check.timeout_seconds:g}s: "
                f"{' '.join(check.argv)}"
            ),
            duration_seconds=duration,
            sweep=sweep,
            **stream_fields,
        )
    if process.returncode == 0:
        stream_fields = _command_stream_fields(
            check,
            stdout_capture,
            stderr_capture,
            failed=False,
            artifact_dir=artifact_dir,
        )
        return CheckResult(
            name=check.name,
            kind=check.kind,
            status="pass",
            detail=f"command exited 0: {' '.join(check.argv)}",
            returncode=0,
            duration_seconds=duration,
            sweep=sweep,
            **stream_fields,
        )
    stream_fields = _command_stream_fields(
        check,
        stdout_capture,
        stderr_capture,
        failed=True,
        artifact_dir=artifact_dir,
    )
    return CheckResult(
        name=check.name,
        kind=check.kind,
        status="fail",
        failure_reason="command-failed",
        detail=_command_failure_detail(check, process.returncode, stream_fields),
        returncode=process.returncode,
        duration_seconds=duration,
        sweep=sweep,
        **stream_fields,
    )


def _run_file_exists_check(check: FileExistsCheck, *, repo_root: Path) -> CheckResult:
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


def run_check(
    check: VerificationCheck,
    *,
    repo_root: str | Path | None = None,
    context: VerificationContext | None = None,
    artifact_dir: str | Path | None = None,
    cancellation: _ParallelCancellation | None = None,
) -> CheckResult:
    """Execute a single verification check and return a structured result.

    ``artifact_dir`` — when set — is where failed command checks retain their
    full gzip-compressed stdout/stderr; command previews and metadata are
    always bounded regardless. File and regex checks ignore it.

    ``cancellation`` is supplied by ``run_verification``'s parallel dispatch so
    an interrupt can kill this check's command process; file and regex checks
    ignore it.
    """
    verification_context = _coerce_verification_context(
        repo_root=repo_root, context=context
    )
    resolved = verification_context.resolve_check(check)
    resolved_artifact_dir = None if artifact_dir is None else Path(artifact_dir)
    if isinstance(check, CommandCheck):
        result = _run_command_check(
            replace(check, cwd=str(resolved.cwd)),
            repo_root=resolved.resolved_root,
            artifact_dir=resolved_artifact_dir,
            cancellation=cancellation,
        )
    elif isinstance(check, FileExistsCheck):
        file_check = (
            replace(check, path=str(resolved.path))
            if verification_context.workspace is not None
            else check
        )
        result = _run_file_exists_check(
            file_check,
            repo_root=resolved.resolved_root,
        )
    elif isinstance(check, RegexInFileCheck):
        regex_check = (
            replace(check, path=str(resolved.path))
            if verification_context.workspace is not None
            else check
        )
        result = _run_regex_in_file_check(
            regex_check,
            repo_root=resolved.resolved_root,
        )
    else:
        raise TypeError(f"unsupported verification check: {type(check).__name__}")
    return replace(
        result,
        repo=resolved.repo,
        resolved_root=str(resolved.resolved_root),
        cwd=str(resolved.cwd) if resolved.cwd is not None else None,
        qualified_cwd=resolved.qualified_cwd,
        path=str(resolved.path) if resolved.path is not None else None,
        qualified_path=resolved.qualified_path,
    )
