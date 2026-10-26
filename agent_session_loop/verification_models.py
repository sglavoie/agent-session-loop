"""Data models for executing a completion contract's ``Verify`` recipe.

The executor consumes a parsed :class:`contracts.CompletionContract` and runs
its authoritative gates against either one legacy repository root or a resolved
workspace registry. This module owns the context that resolves each check's
location, the structured results, and the observer events the executor emits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from . import plans_errors, plans_registry, workspaces
from .contracts import (
    CommandCheck,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
)
from .plans_files import canonicalize_path
from .plans_session_ids import SessionId

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
            raise plans_errors.PlanError(
                "verification workspace root does not match the resolved workspace"
            )
        object.__setattr__(self, "root", canonical_root)
        if self.plan_dir is not None:
            object.__setattr__(self, "plan_dir", canonicalize_path(self.plan_dir))

    @classmethod
    def legacy(
        cls, repo_root: str | Path, *, plan_dir: str | Path | None = None
    ) -> VerificationContext:
        return cls(
            root=canonicalize_path(repo_root),
            # `__post_init__` canonicalizes this again; converting here is what
            # keeps the field a `Path` rather than whatever the caller passed.
            plan_dir=None if plan_dir is None else canonicalize_path(plan_dir),
        )

    @classmethod
    def resolved_workspace(
        cls, workspace: workspaces.ResolvedWorkspace
    ) -> VerificationContext:
        return cls(root=workspace.root, workspace=workspace)

    @classmethod
    def from_plan(cls, plan_dir: str | Path, cwd: str | Path) -> VerificationContext:
        """Interpret ``cwd`` as a repository root or workspace root by plan mode."""
        registry = plans_registry.read_repository_registry(plan_dir)
        if registry is None:
            return cls.legacy(cwd, plan_dir=plan_dir)
        try:
            workspace = workspaces.resolve_workspace(cwd, registry)
        except workspaces.WorkspaceError as error:
            raise plans_errors.PlanError(
                f"invalid verification workspace: {error}"
            ) from error
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
            raise plans_errors.PlanError(
                "registered verification checks require a repository alias"
            )
        try:
            return self.workspace.get(repo).root
        except workspaces.WorkspaceError as error:
            raise plans_errors.PlanError(str(error)) from error

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
                raise plans_errors.PlanError(
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
                raise plans_errors.PlanError(
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


def require_verification_root(cwd: str | Path) -> None:
    """Refuse a ``--cwd`` root that is not an existing directory.

    Worded as preflight's ``env-repo-root`` finding words it, so verify and
    contract-check refuse the root instead of failing every check against it.
    """
    root = Path(cwd)
    if not root.exists():
        raise plans_errors.PlanError(f"cwd does not exist: {cwd}")
    if not root.is_dir():
        raise plans_errors.PlanError(f"cwd is not a directory: {cwd}")


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


def _resolve_under_root(repo_root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        return candidate
    return repo_root / candidate


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
    the last line of its stderr (see
    :func:`agent_session_loop.verification_diagnostics._parse_failure_diagnostic`).
    It is ``None`` for every passing check and for any failure whose stderr
    carries no such line.
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
    """Optional callbacks invoked around each check in ``run_verification``.

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
