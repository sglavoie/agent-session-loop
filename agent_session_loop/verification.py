"""Execute the ``Verify`` recipe for a session's completion contract.

The executor consumes a parsed :class:`contracts.CompletionContract` and runs
its authoritative gates against either one legacy repository root or a resolved
workspace registry. Conditional
diagnostics run only when a named gate fails, returning structured executed and
skipped results that the CLI and runner enforcement can serialize to JSON.

Command checks always use ``shell=False`` and an argv list; recipe-time
validation (``contracts.parse_completion_contract``) already enforces argv
non-emptiness, valid regex, positive timeouts within the documented cap, and
unknown-type rejection, so this module focuses on translating execution-time
failures into structured ``CheckResult`` rows.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from . import plans, responses, workspaces
from .artifacts import ArtifactError, StreamCapture
from .contracts import (
    CommandCheck,
    CompletionContract,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
    read_completion_contract,
)
from .plans import SessionId, canonicalize_path
from .process_tree import (
    descendant_pids,
    sample_process_table,
    sweep_enabled,
    sweep_survivors,
)

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
class ResolvedCheckContext:
    """Filesystem and reporting locations selected for one check."""

    repo: str | None
    resolved_root: Path
    cwd: Path | None = None
    path: Path | None = None
    qualified_cwd: str | None = None
    qualified_path: str | None = None


@dataclass(frozen=True)
class VerificationContext:
    """Resolve checks against either one legacy root or a workspace registry."""

    root: Path
    workspace: workspaces.ResolvedWorkspace | None = None
    #: Plan-owned directory a legacy check may also reach. ASL's own generated
    #: review contracts verify plan artifacts, which live here rather than in
    #: any repository.
    plan_dir: Path | None = None

    def __post_init__(self) -> None:
        canonical_root = canonicalize_path(self.root)
        if self.workspace is not None and canonical_root != self.workspace.root:
            raise plans.PlanError(
                "verification workspace root does not match the resolved workspace"
            )
        object.__setattr__(self, "root", canonical_root)
        if self.plan_dir is not None:
            object.__setattr__(self, "plan_dir", canonicalize_path(self.plan_dir))

    @classmethod
    def legacy(
        cls, repo_root: str | Path, *, plan_dir: str | Path | None = None
    ) -> VerificationContext:
        return cls(root=canonicalize_path(repo_root), plan_dir=plan_dir)

    @classmethod
    def resolved_workspace(
        cls, workspace: workspaces.ResolvedWorkspace
    ) -> VerificationContext:
        return cls(root=workspace.root, workspace=workspace)

    @classmethod
    def from_plan(cls, plan_dir: str | Path, cwd: str | Path) -> VerificationContext:
        """Interpret ``cwd`` as a repository root or workspace root by plan mode."""
        registry = plans.read_repository_registry(plan_dir)
        if registry is None:
            return cls.legacy(cwd, plan_dir=plan_dir)
        try:
            workspace = workspaces.resolve_workspace(cwd, registry)
        except workspaces.WorkspaceError as error:
            raise plans.PlanError(f"invalid verification workspace: {error}") from error
        return cls.resolved_workspace(workspace)

    @property
    def registry_digest(self) -> str | None:
        if self.workspace is None:
            return None
        return self.workspace.registry.digest

    def repository_root(self, repo: str | None) -> Path:
        if self.workspace is None:
            return self.root
        if repo is None:
            raise plans.PlanError(
                "registered verification checks require a repository alias"
            )
        try:
            return self.workspace.get(repo).root
        except workspaces.WorkspaceError as error:
            raise plans.PlanError(str(error)) from error

    def resolve_check(self, check: VerificationCheck) -> ResolvedCheckContext:
        selected_root = self.repository_root(check.repo)
        registered = self.workspace is not None

        def resolve(value: str) -> Path:
            candidate = Path(value)
            if registered and (
                candidate.is_absolute()
                or PureWindowsPath(value).is_absolute()
                or "\\" in value
                or ".." in candidate.parts
            ):
                raise plans.PlanError(
                    f"check {check.name!r} location must stay beneath repository "
                    f"{check.repo}"
                )
            resolved = _resolve_under_root(selected_root, value)
            canonical = canonicalize_path(resolved)
            escapes = not workspaces.is_descendant(
                canonical, selected_root, allow_equal=True
            ) and not (
                self.plan_dir is not None
                and workspaces.is_descendant(canonical, self.plan_dir, allow_equal=True)
            )
            if escapes:
                target = (
                    f"repository {check.repo}"
                    if registered
                    else f"the bound root {selected_root}"
                )
                raise plans.PlanError(
                    f"check {check.name!r} location escapes {target}: {value}"
                )
            if registered:
                return canonical
            return resolved

        cwd: Path | None = None
        path: Path | None = None
        qualified_cwd: str | None = None
        qualified_path: str | None = None
        if isinstance(check, CommandCheck):
            cwd = selected_root if check.cwd is None else resolve(check.cwd)
            if registered:
                qualified_cwd = f"{check.repo}:{check.cwd or '.'}"
        elif isinstance(check, (FileExistsCheck, RegexInFileCheck)):
            path = resolve(check.path)
            if registered:
                qualified_path = f"{check.repo}:{check.path}"
        return ResolvedCheckContext(
            repo=check.repo,
            resolved_root=selected_root,
            cwd=cwd,
            path=path,
            qualified_cwd=qualified_cwd,
            qualified_path=qualified_path,
        )


def verification_context_from_plan(
    plan_dir: str | Path, cwd: str | Path
) -> VerificationContext:
    """Public helper shared by verify, contract-check, and baseline callers."""
    return VerificationContext.from_plan(plan_dir, cwd)


def _coerce_verification_context(
    *,
    repo_root: str | Path | None,
    context: VerificationContext | None,
) -> VerificationContext:
    if context is not None:
        if repo_root is not None:
            raise TypeError("pass either repo_root or context, not both")
        return context
    if repo_root is None:
        raise TypeError("repo_root or context is required")
    return VerificationContext.legacy(repo_root)


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

    ``sweep`` is the post-check process-sweep report
    (:func:`agent_session_loop.process_tree.sweep_survivors`) for a command
    check. It stays ``None`` for file and regex checks, and also whenever the
    sweep was opted out of or could not read the process table.

    ``failure_diagnostic`` is the structured rejection a failed command wrote as
    the last line of its stderr (see :func:`_parse_failure_diagnostic`). It is
    ``None`` for every passing check and for any failure whose stderr carries no
    such line.
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
    sweep: dict[str, Any] | None = None
    failure_diagnostic: dict[str, Any] | None = None
    repo: str | None = None
    resolved_root: str | None = None
    cwd: str | None = None
    qualified_cwd: str | None = None
    path: str | None = None
    qualified_path: str | None = None


#: The one skip a recipe produces on its own: a diagnostic whose gates all
#: passed, so the condition it exists to explain never arose. Consumers grading
#: a run tell this apart from a check that was skipped for any other cause.
SKIP_REASON_CONDITION_NOT_MET = "condition-not-met"

#: What a check does to the aggregate result: a gate determines it, a
#: conditional diagnostic only explains a gate that already failed. Consumers
#: reading executed checks one at a time need the role on each record, because
#: a recipe-level diagnostic count cannot say which of the checks that ran was
#: the diagnostic.
CHECK_ROLE_GATE = "gate"
CHECK_ROLE_DIAGNOSTIC = "diagnostic"


def check_role(check: VerificationCheck) -> str:
    """Whether ``check`` gates the verification or only diagnoses a failure."""

    return CHECK_ROLE_DIAGNOSTIC if check.on_failure_of else CHECK_ROLE_GATE


@dataclass(frozen=True)
class SkippedCheck:
    """A conditional diagnostic omitted because none of its gates failed."""

    name: str
    kind: str
    on_failure_of: tuple[str, ...]
    reason: str = SKIP_REASON_CONDITION_NOT_MET
    repo: str | None = None
    resolved_root: str | None = None
    cwd: str | None = None
    qualified_cwd: str | None = None
    path: str | None = None
    qualified_path: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    """Aggregated result of running gates and eligible diagnostics."""

    session_id: SessionId
    checks: tuple[CheckResult, ...]
    skipped_checks: tuple[SkippedCheck, ...] = ()
    diagnostic_names: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        diagnostics = set(self.diagnostic_names)
        return all(
            check.status == "pass"
            for check in self.checks
            if check.name not in diagnostics
        )

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        diagnostics = set(self.diagnostic_names)
        return tuple(
            check
            for check in self.checks
            if check.name not in diagnostics and check.status != "pass"
        )

    @property
    def diagnostic_failures(self) -> tuple[CheckResult, ...]:
        diagnostics = set(self.diagnostic_names)
        return tuple(
            check
            for check in self.checks
            if check.name in diagnostics and check.status != "pass"
        )

    @property
    def gate_count(self) -> int:
        diagnostics = set(self.diagnostic_names)
        return sum(check.name not in diagnostics for check in self.checks)

    @property
    def configured_check_count(self) -> int:
        return len(self.checks) + len(self.skipped_checks)


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
    repo: str | None = None
    resolved_root: str | None = None
    qualified_cwd: str | None = None
    path: str | None = None
    qualified_path: str | None = None


@dataclass(frozen=True)
class CheckFinishedEvent:
    """Observer payload emitted after a single check finishes."""

    session_id: SessionId
    check_index: int
    check_count: int
    name: str
    kind: str
    result: CheckResult
    role: str
    repo: str | None = None
    resolved_root: str | None = None
    cwd: str | None = None
    qualified_cwd: str | None = None
    path: str | None = None
    qualified_path: str | None = None


@dataclass(frozen=True)
class CheckSkippedEvent:
    """Observer payload emitted for a conditional diagnostic that does not run."""

    session_id: SessionId
    check_index: int
    check_count: int
    name: str
    kind: str
    on_failure_of: tuple[str, ...]
    reason: str
    repo: str | None = None
    resolved_root: str | None = None
    cwd: str | None = None
    qualified_cwd: str | None = None
    path: str | None = None
    qualified_path: str | None = None


CheckStartedCallback = Callable[[CheckStartedEvent], None]
CheckFinishedCallback = Callable[[CheckFinishedEvent], None]
CheckSkippedCallback = Callable[[CheckSkippedEvent], None]


@dataclass(frozen=True)
class VerificationObserver:
    """Optional callbacks invoked around each check in :func:`run_verification`.

    Each callback is fired synchronously on the thread that runs the check —
    the calling thread for a sequential recipe, a worker thread for the
    command checks of a ``parallel`` one, where callbacks may also interleave
    out of declared order. ``on_check_started`` is invoked before the check
    executes; ``on_check_finished`` is invoked after the check completes
    (including failures). Either field may be ``None`` to skip that event.
    Exceptions raised by a callback propagate to the caller.
    """

    on_check_started: CheckStartedCallback | None = None
    on_check_finished: CheckFinishedCallback | None = None
    on_check_skipped: CheckSkippedCallback | None = None


def _make_check_started_event(
    session_id: SessionId,
    check: VerificationCheck,
    *,
    index: int,
    count: int,
    resolved: ResolvedCheckContext,
) -> CheckStartedEvent:
    argv: tuple[str, ...] | None = None
    cwd: str | None = None
    if isinstance(check, CommandCheck):
        argv = tuple(check.argv)
        cwd = str(resolved.cwd)
    return CheckStartedEvent(
        session_id=session_id,
        check_index=index,
        check_count=count,
        name=check.name,
        kind=check.kind,
        argv=argv,
        cwd=cwd,
        repo=check.repo,
        resolved_root=str(resolved.resolved_root),
        qualified_cwd=resolved.qualified_cwd,
        path=str(resolved.path) if resolved.path is not None else None,
        qualified_path=resolved.qualified_path,
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
    """Feed a process pipe into ``capture`` chunk by chunk until EOF.

    ``ArtifactError`` is tolerated because a check abandoned after
    :data:`_ABANDONED_READER_JOIN_SECONDS` discards its capture while this
    thread may still be blocked on a pipe some escaped descendant holds open.
    """
    try:
        while True:
            chunk = stream.read1(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            capture.feed(chunk)
    except OSError, ValueError, ArtifactError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class _VerificationCancelled(BaseException):
    """Raised inside a worker whose check an interrupt abandoned.

    Derives from ``BaseException`` so no ``except Exception`` handler can
    mistake it for a check failure. The future carrying it is never read: the
    interrupt that triggered the cancellation is re-raised instead.
    """


# A killed process group closes the inherited pipes, so the reader threads see
# EOF at once. This bound only exists for a descendant that escaped the group
# (by calling `setsid` itself): such a process is never stopped, so this caps
# what an abandoned check pays to walk away from it.
_ABANDONED_READER_JOIN_SECONDS = 5.0

# A check that exited on its own is not killed, so nothing closes the pipes an
# escaped descendant inherited and the join for it would otherwise never
# return — the 2h20m hang class. The bound is generous because the common case
# is a same-group straggler still flushing legitimate output, and truncating
# that would lose the evidence the check was run for.
_EXITED_READER_JOIN_SECONDS = 30.0


def _sweep_process_table() -> dict[int, dict[str, Any]] | None:
    """The table to snapshot descendants from, or ``None`` when sweeping is off."""
    if not sweep_enabled():
        return None
    return sample_process_table()


def _snapshot_descendants(
    root_pid: int, table: Mapping[int, Mapping[str, Any]] | None
) -> tuple[Mapping[str, Any], ...]:
    """Record ``root_pid``'s descendants while it is still alive.

    Killing the group reparents an escapee to pid 1, after which no descendant
    walk can reach it. A snapshot taken beforehand is the sweep's only route to
    one, so this must run before the kill, not after it.
    """
    if table is None:
        return ()
    return tuple(table[pid] for pid in descendant_pids(root_pid, table))


def _sweep_check_descendants(
    process: subprocess.Popen,
    prior_snapshot: tuple[Mapping[str, Any], ...],
) -> dict[str, Any] | None:
    """Kill whatever outlived the check, or ``None`` when the sweep is off.

    ``expected_pgid`` is the child's PID because command checks spawn with
    ``start_new_session=True``. ``root_pid`` is already reaped by the caller, so
    the sweep excludes it rather than signalling a PID that may be reused.
    """
    if not sweep_enabled():
        return None
    return sweep_survivors(
        root_pid=process.pid,
        expected_pgid=process.pid,
        prior_snapshot=prior_snapshot,
    )


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill ``process`` and every descendant still in its process group.

    Command checks are spawned with ``start_new_session=True``, so the child's
    process group ID is its PID and every descendant inherits that group unless
    it starts a session of its own. Killing the group is what stops a check
    that fans out into helper processes; without it the direct child dies while
    its children keep running, hold the captured pipes open, and can still
    write to the repository.

    A descendant that calls ``setsid`` leaves the group and survives this call.
    :func:`_sweep_check_descendants` is what reaches it afterwards, by matching
    the descendants :func:`_snapshot_descendants` recorded before the kill
    against the live process table. That stays racy regardless — a descendant
    can detach or fork between the snapshot and the signal — so callers still
    bound the *cost* of an escapee: see :data:`_ABANDONED_READER_JOIN_SECONDS`
    and :data:`_EXITED_READER_JOIN_SECONDS`.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        # The group is already empty, or the child left it for one of its own.
        with contextlib.suppress(OSError):
            process.kill()


def _kill_and_reap(process: subprocess.Popen) -> None:
    """Kill ``process``'s group and wait for it, tolerating an already-dead child."""
    _kill_process_group(process)
    process.wait()


def _join_readers(
    readers: tuple[threading.Thread, ...], *, timeout: float | None
) -> None:
    """Join the pipe readers, optionally under a shared deadline."""
    if timeout is None:
        for reader in readers:
            reader.join()
        return
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


class _ParallelCancellation:
    """Kill switch shared by the command checks of one parallel dispatch.

    :meth:`cancel` runs on the calling thread once the dispatch has raised: it
    kills every command process still in flight so the workers unblock at once
    and :func:`run_verification` can join them before the exception escapes.
    Killing is what makes that join bounded — without it, waiting would stall
    behind the longest check and not waiting would leave it running.

    Because the kill happens here rather than in the owning worker, the
    pre-kill descendant snapshot each worker needs for its sweep can only be
    taken here too; :meth:`snapshot_for` hands it back.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._processes: set[subprocess.Popen] = set()
        self._snapshots: dict[int, tuple[Mapping[str, Any], ...]] = {}

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def register(self, process: subprocess.Popen) -> bool:
        """Track a spawned process; ``False`` when the run is already cancelled."""
        with self._lock:
            if self._cancelled:
                return False
            self._processes.add(process)
            return True

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(process)

    def snapshot_for(self, process: subprocess.Popen) -> tuple[Mapping[str, Any], ...]:
        """The descendants recorded for ``process`` just before it was killed."""
        with self._lock:
            return self._snapshots.get(process.pid, ())

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            in_flight = tuple(self._processes)
            self._processes.clear()
        # One process table for the whole dispatch, read before any kill lands
        # so every root is still alive and its descendants still reachable.
        table = _sweep_process_table()
        snapshots = {
            process.pid: _snapshot_descendants(process.pid, table)
            for process in in_flight
        }
        with self._lock:
            self._snapshots.update(snapshots)
        for process in in_flight:
            # The owning worker is inside `process.wait()` and reaps it there.
            _kill_process_group(process)


#: Longest stderr line still considered for a structured diagnostic. The
#: producers are ASL's own generated gates, whose rejections run to tens of
#: bytes; the cap is what keeps an arbitrary command from pushing unbounded
#: structure into the check record, the event log, and the audit bundle.
_FAILURE_DIAGNOSTIC_MAX_BYTES = 2048

#: Key naming a failed command's structured rejection wherever it travels.
FAILURE_DIAGNOSTIC_KEY = "failure_diagnostic"

#: The marker a generated ASL gate stamps on its rejection line, and the value
#: it stamps (``runner._VALIDATOR_FAIL_HELPER``). Nothing is promoted without
#: it, so a third-party command whose output happens to be a ``check``/
#: ``reason`` object stays application output rather than becoming ASL schema.
#: It is stripped once read: it authenticates the line and says nothing about
#: the failure.
FAILURE_DIAGNOSTIC_MARKER_KEY = "asl_diagnostic"
FAILURE_DIAGNOSTIC_MARKER_VALUE = "generated-gate/1"

#: The only diagnostic fields that are control-plane text. Everything a gate
#: attaches beyond them is drawn from the artifact it rejected — the generated
#: gates pass ``status=``, ``last_line=`` and ``error=`` — so it is application
#: content and is withheld like any other command output.
_DIAGNOSTIC_CONTROL_PLANE_KEYS = frozenset({"check", "reason"})

#: Both control-plane fields must be tokens. Barring whitespace is what makes
#: them structurally unable to carry prose, so a caller that forges the marker
#: still cannot smuggle application text through the two fields that travel.
_DIAGNOSTIC_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def is_control_plane_diagnostic_field(key: str | None, value: Any) -> bool:
    """Whether a ``failure_diagnostic`` member may travel as control-plane text.

    The audit boundary asks this of every field it projects, so the definition
    of what is ASL schema and what is application output lives in one place
    rather than being restated by each consumer.
    """
    return (
        key in _DIAGNOSTIC_CONTROL_PLANE_KEYS
        and isinstance(value, str)
        and _DIAGNOSTIC_TOKEN_RE.fullmatch(value) is not None
    )


def _parse_failure_diagnostic(stderr_preview: str) -> dict[str, Any] | None:
    """Read a failed command's last stderr line as a structured rejection.

    Kept only when the line is a bounded JSON object that carries the generated
    gates' marker and whose ``check`` and ``reason`` are tokens, which is the
    contract every generated review gate leaves through
    (``runner._VALIDATOR_FAIL_HELPER``). Authenticating the producer is what
    makes the value ASL-authored schema rather than application output, so it
    can travel where a raw stream may not.
    """
    lines = [line.strip() for line in stderr_preview.splitlines() if line.strip()]
    if not lines:
        return None
    candidate = lines[-1]
    if len(candidate.encode("utf-8")) > _FAILURE_DIAGNOSTIC_MAX_BYTES:
        return None
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get(FAILURE_DIAGNOSTIC_MARKER_KEY) != FAILURE_DIAGNOSTIC_MARKER_VALUE:
        return None
    if not all(
        is_control_plane_diagnostic_field(key, parsed.get(key))
        for key in _DIAGNOSTIC_CONTROL_PLANE_KEYS
    ):
        return None
    return {
        key: value
        for key, value in parsed.items()
        if key != FAILURE_DIAGNOSTIC_MARKER_KEY
    }


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
            _parse_failure_diagnostic(stderr_meta.preview) if failed else None
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
            # see :func:`_kill_process_group`.
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
        for pipe in (process.stdout, process.stderr):
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
        cancelled = cancellation is not None and cancellation.cancelled
        if cancelled and not timed_out:
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


CommandDiagnosticReason = Literal["executable-not-found", "module-not-importable"]
CommandScopeDiagnosticReason = Literal[
    "operator-health-check-all",
    "operator-health-unscoped",
]

_PYTHON_BASENAME_PATTERN = re.compile(r"python(\d+(\.\d+)?)?\Z")
_MODULE_PROBE_TIMEOUT_SECONDS = 15.0
_OPERATOR_HEALTH_SCOPED_SUBCOMMANDS = frozenset(("check-ts", "lint", "test"))
_OPERATOR_HEALTH_NON_SELECTOR_FLAGS = frozenset(("--failed-only", "--resume", "--help"))


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


def _is_bare_python_invocation(argv: tuple[str, ...]) -> bool:
    """True when ``argv[0]`` is the *unversioned* ``python`` interpreter.

    A completion-contract command that shells out to bare, unversioned
    ``python`` is not portable: ``python`` is frequently absent in environments
    that ship only a versioned ``python3``. The actionable remediation is to
    use ``python3`` — matching the ``session-expand`` skill guidance.

    A versioned ``python3``/``python3.11`` invocation is *not* flagged here,
    regardless of whether it uses ``-m``, ``-c``, or a script path — those are
    valid inline evidence checks. Module importability for the ``-m`` form is
    diagnosed separately by :func:`diagnose_command`.
    """
    if not argv:
        return False
    basename = Path(argv[0]).name
    if _PYTHON_BASENAME_PATTERN.fullmatch(basename) is None:
        return False
    # Only the unversioned ``python`` command is flagged; ``python3`` and
    # ``python3.11`` carry an explicit version and are treated as available.
    return basename == "python"


def _default_module_probe(interpreter: str, module: str, *, cwd: Path) -> bool | None:
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
    except subprocess.TimeoutExpired, FileNotFoundError, OSError:
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
        probe_cwd = (
            repo_root
            if check.cwd is None
            else _resolve_under_root(repo_root, check.cwd)
        )
        outcome = module_probe(resolved_interpreter, module, cwd=probe_cwd)
        if outcome is False:
            return CommandDiagnostic(
                reason="module-not-importable",
                detail=(f"module not importable by {executable}: {module}"),
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

    ``cancellation`` is supplied by :func:`run_verification`'s parallel
    dispatch so an interrupt can kill this check's command process; file and
    regex checks ignore it.
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
    :func:`verify_from_args` are unchanged when no observer is passed.

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
    :func:`agent_session_loop.preflight.runtime_repair_hint`; the import is
    deferred because ``preflight`` imports this module.
    """
    from . import preflight  # local import: preflight imports verification

    fields: dict[str, Any] = {}
    hint = preflight.runtime_repair_hint(reason, failure_reason=failure_reason)
    if hint is not None:
        fields["repair_hint"] = preflight.repair_hint_payload(
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
        verification_context = verification_context_from_plan(plan_dir_arg, cwd_arg)
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
        except plans.PlanError as error:
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
    except plans.PlanError as error:
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


# ---------------------------------------------------------------------------
# `asl contract-check` — static per-check contract diagnosis
# ---------------------------------------------------------------------------

CONTRACT_CHECK_EXIT_REASON_OK = "ok"
CONTRACT_CHECK_EXIT_REASON_ISSUES = "contract-issues"
CONTRACT_CHECK_EXIT_REASON_INVALID_SESSION = "invalid-session"
CONTRACT_CHECK_EXIT_REASON_CWD_ERROR = "cwd-error"
CONTRACT_CHECK_EXIT_REASON_PLAN_ERROR = "plan-error"

#: Per-check row statuses.
CONTRACT_CHECK_ROW_OK = "ok"
CONTRACT_CHECK_ROW_SCHEMA_ERROR = "schema-error"
CONTRACT_CHECK_ROW_MISSING_CONTRACT = "missing-contract"
CONTRACT_CHECK_ROW_FACTUAL_ISSUE = "factual-issue"

#: ``factual-issue`` sub-reasons.
CONTRACT_CHECK_ISSUE_COMMAND_NOT_RUNNABLE = "command-not-runnable"
CONTRACT_CHECK_ISSUE_FILE_MISSING = "file-missing"
CONTRACT_CHECK_ISSUE_REGEX_NOT_MATCHED = "regex-not-matched"
CONTRACT_CHECK_ISSUE_BARE_PYTHON = "bare-python"


def _contract_check_session_row(
    session: str, status: str, detail: str
) -> dict[str, Any]:
    return {
        "session": session,
        "name": None,
        "type": None,
        "repo": None,
        "resolved_root": None,
        "cwd": None,
        "qualified_cwd": None,
        "path": None,
        "qualified_path": None,
        "status": status,
        "reason": None,
        "detail": detail,
    }


def _diagnose_contract_check(
    check: VerificationCheck, *, context: VerificationContext
) -> tuple[str, str | None, str]:
    """Statically diagnose one check; return ``(status, reason, detail)``.

    No command is executed and no file is written: command runnability is
    diagnosed by :func:`diagnose_command`/:func:`diagnose_command_scope` (both
    mapped to ``command-not-runnable``), an unversioned ``python`` interpreter
    invocation is flagged ``bare-python``, and file/regex checks are resolved
    against the check's selected repository for existence and pattern-match.
    """
    resolved = context.resolve_check(check)
    if isinstance(check, CommandCheck):
        if _is_bare_python_invocation(check.argv):
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_BARE_PYTHON,
                (
                    "unversioned `python` is frequently unavailable where "
                    f"`python3` exists: {' '.join(check.argv)}; use `python3`"
                ),
            )
        scope = diagnose_command_scope(check)
        if scope is not None:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_COMMAND_NOT_RUNNABLE,
                scope.detail,
            )
        diagnostic = diagnose_command(
            replace(check, cwd=str(resolved.cwd)),
            repo_root=resolved.resolved_root,
        )
        if diagnostic is not None:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_COMMAND_NOT_RUNNABLE,
                diagnostic.detail,
            )
        return (
            CONTRACT_CHECK_ROW_OK,
            None,
            f"command is runnable: {' '.join(check.argv)}",
        )

    if isinstance(check, FileExistsCheck):
        assert resolved.path is not None
        target = resolved.path
        try:
            exists = target.exists()
        except OSError as error:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_FILE_MISSING,
                f"cannot stat {check.path}: {error}",
            )
        if exists:
            return (CONTRACT_CHECK_ROW_OK, None, f"file exists: {check.path}")
        return (
            CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
            CONTRACT_CHECK_ISSUE_FILE_MISSING,
            f"file does not exist: {check.path}",
        )

    if isinstance(check, RegexInFileCheck):
        assert resolved.path is not None
        target = resolved.path
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_FILE_MISSING,
                f"file does not exist: {check.path}",
            )
        except OSError as error:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_FILE_MISSING,
                f"cannot read {check.path}: {error}",
            )
        if re.compile(check.pattern, re.MULTILINE).search(text) is None:
            return (
                CONTRACT_CHECK_ROW_FACTUAL_ISSUE,
                CONTRACT_CHECK_ISSUE_REGEX_NOT_MATCHED,
                f"pattern did not match in {check.path}: {check.pattern}",
            )
        return (CONTRACT_CHECK_ROW_OK, None, f"pattern matched in {check.path}")

    return (CONTRACT_CHECK_ROW_OK, None, "")


def _contract_check_rows(
    plan_dir: str | Path,
    session_id: SessionId,
    *,
    repo_root: Path | None = None,
    context: VerificationContext | None = None,
) -> list[dict[str, Any]]:
    """Return per-check rows for one session's completion contract."""
    verification_context = _coerce_verification_context(
        repo_root=repo_root, context=context
    )
    try:
        contract = read_completion_contract(plan_dir, session_id)
    except plans.PlanError as error:
        return [
            _contract_check_session_row(
                session_id.value, CONTRACT_CHECK_ROW_SCHEMA_ERROR, str(error)
            )
        ]
    if contract is None:
        return [
            _contract_check_session_row(
                session_id.value,
                CONTRACT_CHECK_ROW_MISSING_CONTRACT,
                f"session {session_id.value} has no Completion Contract section",
            )
        ]

    rows: list[dict[str, Any]] = []
    for check in contract.verify.checks:
        try:
            resolved = verification_context.resolve_check(check)
            status, reason, detail = _diagnose_contract_check(
                check, context=verification_context
            )
        except plans.PlanError as error:
            rows.append(
                {
                    "session": session_id.value,
                    "name": check.name,
                    "type": check.kind,
                    "repo": check.repo,
                    "resolved_root": None,
                    "cwd": None,
                    "qualified_cwd": None,
                    "path": None,
                    "qualified_path": None,
                    "status": CONTRACT_CHECK_ROW_SCHEMA_ERROR,
                    "reason": "repository-contract",
                    "detail": str(error),
                }
            )
            continue
        rows.append(
            {
                "session": session_id.value,
                "name": check.name,
                "type": check.kind,
                "repo": check.repo,
                "resolved_root": str(resolved.resolved_root),
                "cwd": str(resolved.cwd) if resolved.cwd is not None else None,
                "qualified_cwd": resolved.qualified_cwd,
                "path": str(resolved.path) if resolved.path is not None else None,
                "qualified_path": resolved.qualified_path,
                "status": status,
                "reason": reason,
                "detail": detail,
            }
        )
    return rows


def contract_check_from_args(args: argparse.Namespace) -> int:
    """Run the ``contract-check`` subcommand from CLI arguments.

    Statically checks each session's completion-contract ``Verify`` recipe
    without executing any command: it wraps
    :func:`agent_session_loop.contracts.read_completion_contract`,
    :func:`diagnose_command`/:func:`diagnose_command_scope`, and file/regex
    resolution. ``--session ID`` narrows to one session (the single-file mode
    plan-wide preflight cannot express). Every check produces a row whose status
    is ``ok``, ``schema-error``, ``missing-contract``, or ``factual-issue`` (with
    a ``command-not-runnable``/``file-missing``/``regex-not-matched``/
    ``bare-python`` reason). Exit 0 only when every row is ``ok``.
    """
    plan_dir_arg = str(args.plan_dir)
    raw_session = getattr(args, "session", None)
    cwd_arg = args.cwd
    want_json = bool(args.json)
    common_fields: dict[str, Any] = {
        "plan_dir": plan_dir_arg,
        "session": str(raw_session) if raw_session is not None else None,
    }

    def _emit_error(reason: str, error: str) -> int:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "contract-check",
                    reason=reason,
                    error=error,
                    paused=False,
                    **common_fields,
                )
            )
        else:
            print(f"agent-session-loop contract-check: {error}", file=sys.stderr)
        return 1

    try:
        verification_context = verification_context_from_plan(plan_dir_arg, cwd_arg)
    except (plans.PlanError, OSError) as error:
        return _emit_error(CONTRACT_CHECK_EXIT_REASON_CWD_ERROR, str(error))

    try:
        snapshot = plans.inspect_plan(plan_dir_arg)
    except plans.PlanError as error:
        return _emit_error(CONTRACT_CHECK_EXIT_REASON_PLAN_ERROR, str(error))

    known_ids = {status.id for status in snapshot.sessions}
    if raw_session is not None:
        try:
            session_id = plans.parse_session_id(raw_session)
        except (ValueError, plans.PlanError) as error:
            return _emit_error(CONTRACT_CHECK_EXIT_REASON_INVALID_SESSION, str(error))
        if session_id.value not in known_ids:
            return _emit_error(
                CONTRACT_CHECK_EXIT_REASON_INVALID_SESSION,
                f"session {session_id.value} is not present in {plan_dir_arg}",
            )
        session_ids = [session_id]
    else:
        session_ids = [
            plans.parse_session_id(status.id) for status in snapshot.sessions
        ]

    rows: list[dict[str, Any]] = []
    for session_id in session_ids:
        rows.extend(
            _contract_check_rows(plan_dir_arg, session_id, context=verification_context)
        )

    issues = [row for row in rows if row["status"] != CONTRACT_CHECK_ROW_OK]
    result_fields = {
        "cwd": str(verification_context.root),
        **_verification_context_payload(verification_context),
        "checks": rows,
        "issue_count": len(issues),
    }

    if not issues:
        if want_json:
            responses.print_json(
                responses.success_payload(
                    "contract-check",
                    reason=CONTRACT_CHECK_EXIT_REASON_OK,
                    **{**common_fields, **result_fields},
                )
            )
        else:
            print(f"Plan: {plan_dir_arg}")
            root_label = (
                "Workspace root"
                if verification_context.workspace is not None
                else "Repo root"
            )
            print(f"{root_label}: {verification_context.root}")
            print(f"Checks: {len(rows)} (all ok)")
        return 0

    if want_json:
        responses.print_json(
            responses.error_payload(
                "contract-check",
                reason=CONTRACT_CHECK_EXIT_REASON_ISSUES,
                error=f"{len(issues)} of {len(rows)} contract check(s) have issues",
                paused=False,
                **{**common_fields, **result_fields},
            )
        )
    else:
        print(f"Plan: {plan_dir_arg}")
        root_label = (
            "Workspace root"
            if verification_context.workspace is not None
            else "Repo root"
        )
        print(f"{root_label}: {verification_context.root}")
        for row in rows:
            marker = "OK" if row["status"] == CONTRACT_CHECK_ROW_OK else "ISSUE"
            label = row["name"] or row["status"]
            reason = f" ({row['reason']})" if row["reason"] else ""
            repository = f" {row.get('repo')}:" if row.get("repo") else ""
            print(
                f"  [{marker}]{repository} session {row['session']} "
                f"{label}{reason}: "
                f"{row['detail']}"
            )
        print(
            f"contract-check: {len(issues)} of {len(rows)} check(s) have issues",
            file=sys.stderr,
        )
    return 1
