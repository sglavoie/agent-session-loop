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
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Literal

from . import plans, responses
from .artifacts import StreamCapture
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
    "command-overbroad",
    "file-missing",
    "file-error",
    "regex-not-matched",
]


@dataclass(frozen=True)
class CheckResult:
    """Structured outcome for a single verification check.

    ``stdout`` and ``stderr`` are bounded UTF-8 previews (first/last 8 KiB) so
    the serialized result can never embed a full large command output. The
    ``*_byte_count``/``*_sha256``/``*_truncated``/``*_artifact_path`` fields carry
    the original size, digest, truncation flag, and — when a failed command's
    full stream was retained under an artifact directory — the gzip artifact
    path for byte-for-byte recovery. File and regex checks leave the streaming
    fields at their defaults.
    """

    name: str
    kind: str
    status: CheckStatus
    detail: str = ""
    failure_reason: FailureReason | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    stdout_byte_count: int = 0
    stdout_sha256: str = ""
    stdout_truncated: bool = False
    stdout_artifact_path: str | None = None
    stderr_byte_count: int = 0
    stderr_sha256: str = ""
    stderr_truncated: bool = False
    stderr_artifact_path: str | None = None


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


# Pipe reads are folded into a StreamCapture in chunks of this size, bounding
# the transient memory per read regardless of how much the command emits.
_STREAM_CHUNK_BYTES = 64 * 1024


def _drain_stream(stream: Any, capture: StreamCapture) -> None:
    """Feed a process pipe into ``capture`` chunk by chunk until EOF."""
    try:
        while True:
            chunk = stream.read1(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            capture.feed(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


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
    }


def _run_command_check(
    check: CommandCheck,
    *,
    repo_root: Path,
    artifact_dir: Path | None = None,
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
    try:
        process = subprocess.Popen(
            list(check.argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
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

    # Output is bounded as it arrives: each pipe feeds a StreamCapture that
    # keeps only the preview windows, running digest/counts, and — when an
    # artifact directory is set — a gzip spool on disk, so a noisy command can
    # never exhaust runner memory before truncation.
    spool = artifact_dir is not None
    stdout_capture = StreamCapture(spool=spool)
    stderr_capture = StreamCapture(spool=spool)
    timed_out = False
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
            process.kill()
            process.wait()
        for reader in readers:
            reader.join()
    except BaseException:
        process.kill()
        process.wait()
        stdout_capture.discard()
        stderr_capture.discard()
        raise

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
        detail=(
            f"command exited {process.returncode}: {' '.join(check.argv)}"
        ),
        returncode=process.returncode,
        duration_seconds=duration,
        **stream_fields,
    )


CommandDiagnosticReason = Literal["executable-not-found", "module-not-importable"]
CommandScopeDiagnosticReason = Literal[
    "operator-health-check-all",
    "operator-health-unscoped",
]

_PYTHON_BASENAME_PATTERN = re.compile(r"python(\d+(\.\d+)?)?\Z")
_MODULE_PROBE_TIMEOUT_SECONDS = 15.0
_OPERATOR_HEALTH_SCOPED_SUBCOMMANDS = frozenset(("check-ts", "lint", "test"))
_OPERATOR_HEALTH_NON_SELECTOR_FLAGS = frozenset(
    ("--failed-only", "--resume", "--help")
)


@dataclass(frozen=True)
class CommandDiagnostic:
    """Why a completion-contract command check is not runnable in this env.

    ``reason`` is a stable enum the preflight layer maps to a severity; ``detail``
    is a human-readable one-liner. A ``None`` diagnostic (returned by
    :func:`diagnose_command`) means "no problem positively determined" — the
    helper is deliberately fail-open so environment quirks never manufacture a
    false finding.
    """

    reason: CommandDiagnosticReason
    detail: str


@dataclass(frozen=True)
class CommandScopeDiagnostic:
    """Why a completion-contract command is too broad for ASL verification."""

    reason: CommandScopeDiagnosticReason
    detail: str


def _has_path_separator(value: str) -> bool:
    if os.sep in value:
        return True
    return os.altsep is not None and os.altsep in value


def _is_operator_health_command(argv: tuple[str, ...]) -> bool:
    if len(argv) < 3:
        return False
    return Path(argv[0]).name == "operator" and argv[1] == "health"


def _has_operator_health_target_flag(argv_tail: tuple[str, ...]) -> bool:
    for arg in argv_tail:
        if not arg.startswith("--"):
            continue
        if arg in _OPERATOR_HEALTH_NON_SELECTOR_FLAGS:
            continue
        return True
    return False


def _is_module_invocation(argv: tuple[str, ...]) -> bool:
    """True when ``argv`` looks like ``[<python>, "-m", <module>, ...]``."""
    if len(argv) < 3 or argv[1] != "-m":
        return False
    basename = Path(argv[0]).name
    return _PYTHON_BASENAME_PATTERN.fullmatch(basename) is not None


def _default_module_probe(
    interpreter: str, module: str, *, cwd: Path
) -> bool | None:
    """Bounded, read-only importability probe.

    Returns ``True`` when the module is importable, ``False`` on an unambiguous
    "missing" signal (exit 3 from ``find_spec`` returning ``None``, or an
    ``ImportError``/``ModuleNotFoundError`` raised by ``find_spec`` when a parent
    package on a dotted module path is missing), and ``None`` for any other
    error, timeout, or non-decisive exit so callers can fail open. Models the
    subprocess degradation shape of :func:`_run_command_check`.
    """
    source = (
        "import importlib.util, sys\n"
        "try:\n"
        f"    found = importlib.util.find_spec({module!r}) is not None\n"
        "except ImportError:\n"
        "    found = False\n"
        "except Exception:\n"
        "    sys.exit(2)\n"
        "sys.exit(0 if found else 3)"
    )
    try:
        completed = subprocess.run(
            [interpreter, "-c", source],
            cwd=str(cwd),
            timeout=_MODULE_PROBE_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 3:
        return False
    return None


def diagnose_command(
    check: CommandCheck,
    *,
    repo_root: Path | None,
    which: Callable[[str], str | None] = shutil.which,
    module_probe: Callable[..., bool | None] = _default_module_probe,
) -> CommandDiagnostic | None:
    """Diagnose whether a command check is runnable, without running it.

    Returns ``None`` unless a problem is positively determined:

    - ``executable-not-found`` when ``argv[0]`` has no path separator and does
      not resolve via ``which`` (a path-bearing ``argv[0]`` is skipped — it may
      be a repo-relative binary the plan builds).
    - ``module-not-importable`` when ``argv`` is ``[<python>, "-m", <module>, …]``,
      the interpreter resolves, and ``module_probe`` reports the module missing.

    The check is skipped entirely when ``repo_root`` is ``None`` (mirrors the
    preflight cwd guard). Every ambiguous or failing probe fails open.
    """
    if repo_root is None:
        return None
    argv = check.argv
    if not argv:
        return None

    executable = argv[0]
    has_separator = _has_path_separator(executable)
    resolved_interpreter: str | None
    if has_separator:
        resolved_interpreter = executable
    else:
        resolved_interpreter = which(executable)
        if resolved_interpreter is None:
            return CommandDiagnostic(
                reason="executable-not-found",
                detail=f"executable not found on PATH: {executable}",
            )

    if _is_module_invocation(argv) and resolved_interpreter is not None:
        module = argv[2]
        outcome = module_probe(resolved_interpreter, module, cwd=repo_root)
        if outcome is False:
            return CommandDiagnostic(
                reason="module-not-importable",
                detail=(
                    f"module not importable by {executable}: {module}"
                ),
            )

    return None


def diagnose_command_scope(
    check: CommandCheck,
) -> CommandScopeDiagnostic | None:
    """Diagnose whether a command is overly broad for ASL session verification.

    ASL session verification must stay focused on the packages actually touched
    by the session. Repository-wide ``./operator health`` commands are too broad
    because, when no package flags are supplied, the operator CLI fans out
    across every workspace package and may start shared services.
    """
    argv = check.argv
    if not _is_operator_health_command(argv):
        return None

    subcommand = argv[2]
    rendered = " ".join(argv)
    if subcommand == "check-all":
        return CommandScopeDiagnostic(
            reason="operator-health-check-all",
            detail=(
                "overly broad verification command: "
                f"{rendered}; `./operator health check-all` is forbidden in "
                "ASL session verification"
            ),
        )

    if (
        subcommand in _OPERATOR_HEALTH_SCOPED_SUBCOMMANDS
        and not _has_operator_health_target_flag(argv[3:])
    ):
        return CommandScopeDiagnostic(
            reason="operator-health-unscoped",
            detail=(
                "overly broad verification command: "
                f"{rendered}; add one or more package target flags such as "
                "`--core-oidc` so the ASL verification stays session-scoped"
            ),
        )

    return None


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


def run_check(
    check: VerificationCheck,
    *,
    repo_root: str | Path,
    artifact_dir: str | Path | None = None,
) -> CheckResult:
    """Execute a single verification check and return a structured result.

    ``artifact_dir`` — when set — is where failed command checks retain their
    full gzip-compressed stdout/stderr; command previews and metadata are
    always bounded regardless. File and regex checks ignore it.
    """
    canonical_root = canonicalize_path(repo_root)
    resolved_artifact_dir = (
        None if artifact_dir is None else Path(artifact_dir)
    )
    if isinstance(check, CommandCheck):
        return _run_command_check(
            check,
            repo_root=canonical_root,
            artifact_dir=resolved_artifact_dir,
        )
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
    artifact_dir: str | Path | None = None,
) -> VerificationResult:
    """Execute every check in ``contract.verify`` against ``repo_root``.

    When ``observer`` is provided, its ``on_check_started`` and
    ``on_check_finished`` callbacks are invoked synchronously around each
    check in declared order. The CLI behavior and JSON payload of
    :func:`verify_from_args` are unchanged when no observer is passed.

    ``artifact_dir`` — when set — is forwarded to each command check so failed
    commands retain their full stdout/stderr as gzip artifacts there; without
    it, only bounded previews and metadata are recorded.
    """
    canonical_root = canonicalize_path(repo_root)
    resolved_artifact_dir = (
        None if artifact_dir is None else Path(artifact_dir)
    )
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
        check_result = run_check(
            check,
            repo_root=canonical_root,
            artifact_dir=resolved_artifact_dir,
        )
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
    artifact_dir_arg = getattr(args, "artifact_dir", None)

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

    result = run_verification(
        contract,
        repo_root=canonical_cwd,
        artifact_dir=artifact_dir_arg,
    )

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
