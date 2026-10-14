"""Environment-aware preflight checks: repo root, git, harness, lock, budget."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import (
    harness_compatibility,
    harnesses,
    plans_session_scope,
    repository_evidence,
    state_locks,
    workspaces,
)
from .git_subprocess import run_git
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_models import PlanSnapshot
from .preflight_models import COMMIT_MODES, CommitMode, PreflightFinding, _finding
from .verification_models import VerificationContext

TURN_BUDGET_YELLOW_THRESHOLD = 25


TURN_BUDGET_DEFAULT_MAX_PENDING_SESSIONS = TURN_BUDGET_YELLOW_THRESHOLD - 1


TURNS_PER_SESSION_ESTIMATE = 3


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return run_git(cwd, args)
    except OSError:
        return None


def _check_env_repo_root(cwd: Path) -> tuple[PreflightFinding, ...]:
    if not cwd.exists():
        return (
            _finding(
                "red",
                "env-repo-root",
                f"cwd does not exist: {cwd}",
                path=str(cwd),
            ),
        )
    if not cwd.is_dir():
        return (
            _finding(
                "red",
                "env-repo-root",
                f"cwd is not a directory: {cwd}",
                path=str(cwd),
            ),
        )
    return ()


def _check_env_git(
    cwd: Path,
    commit_mode: CommitMode | None,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    inside = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if inside is None:
        return (
            _finding(
                "red",
                "env-git",
                "git executable not available on PATH",
                path=str(cwd),
            ),
        )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        detail = (inside.stderr.strip() or inside.stdout.strip())[:200]
        return (
            _finding(
                "red",
                "env-git",
                f"cwd is not inside a git repository: {cwd}",
                path=str(cwd),
                detail=detail,
            ),
        )

    if commit_mode in COMMIT_MODES:
        status = _run_git(cwd, ["status", "--porcelain"])
        if status is None or status.returncode != 0:
            detail = ""
            if status is not None:
                detail = (status.stderr.strip() or status.stdout.strip())[:200]
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    "could not inspect git status",
                    path=str(cwd),
                    detail=detail,
                )
            )
        elif status.stdout:
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    "git worktree is dirty; commit-mode runs require a clean tree",
                    path=str(cwd),
                    detail=f"commit-mode: {commit_mode}",
                )
            )

    return tuple(findings)


def _check_registered_env_git(
    context: VerificationContext,
    commit_mode: CommitMode | None,
    snapshot: PlanSnapshot | None = None,
) -> tuple[PreflightFinding, ...]:
    if context.workspace is None or commit_mode not in COMMIT_MODES:
        return ()
    findings: list[PreflightFinding] = []
    needed = None
    committed = None
    if commit_mode == "per-session":
        try:
            if snapshot is None:
                raise PlanError("current session scope is unavailable")
            needed = plans_session_scope.needed_repository_aliases(
                snapshot, context.workspace.registry
            )
            committed = plans_session_scope.commit_repository_aliases(
                snapshot, context.workspace.registry
            )
        except (PlanError, ValueError, OSError) as error:
            return (_finding("red", "env-git", str(error)),)
    for repository in context.workspace.repositories:
        if needed is not None and repository.alias not in needed:
            continue
        # A tree the next session only reads is inspected but may stay dirty:
        # its commit stages write aliases only (see commit_repository_aliases).
        clean_required = committed is None or repository.alias in committed
        if needed is not None and clean_required:
            try:
                linked_roots = repository_evidence.linked_worktrees(repository.root)
                registered_roots = {r.root for r in context.workspace.repositories}
                for linked in linked_roots:
                    if linked in registered_roots:
                        continue
                    linked_status = _run_git(linked, ["status", "--porcelain"])
                    if (
                        linked_status is None
                        or linked_status.returncode != 0
                        or linked_status.stdout
                    ):
                        findings.append(
                            _finding(
                                "yellow",
                                "env-git",
                                f"needed repository {repository.alias} linked tree "
                                "is dirty or unavailable",
                                path=str(linked),
                                detail=f"commit-mode: {commit_mode}",
                            )
                        )
            except repository_evidence.RepositoryEvidenceError as error:
                findings.append(_finding("red", "env-git", str(error)))
        status = _run_git(repository.root, ["status", "--porcelain"])
        if status is None or status.returncode != 0:
            detail = ""
            if status is not None:
                detail = (status.stderr.strip() or status.stdout.strip())[:200]
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    f"could not inspect git status for {repository.alias}",
                    path=str(repository.root),
                    detail=detail,
                )
            )
        elif status.stdout and not clean_required:
            findings.append(
                _finding(
                    "green",
                    "env-git",
                    (
                        f"repository {repository.alias} is dirty but read-only "
                        "for the next session; its changes are not committed"
                    ),
                    path=str(repository.root),
                    detail=f"commit-mode: {commit_mode}",
                )
            )
        elif status.stdout:
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    (
                        f"repository {repository.alias} is dirty; commit-mode "
                        "runs require written registered trees to be clean"
                    ),
                    path=str(repository.root),
                    detail=f"commit-mode: {commit_mode}",
                )
            )
    return tuple(findings)


def _check_env_harness(harness: str) -> tuple[PreflightFinding, ...]:
    # Matched out of the vocabulary rather than tested against it: the match is
    # the permitted value, so the rest of the check carries a `HarnessName`
    # instead of the `str` it was handed. Unlike `as_literal` this reports
    # rather than raises — preflight's job is to hand back a red finding.
    harness_name = next(
        (name for name in harnesses.HARNESS_NAMES if name == harness), None
    )
    if harness_name is None:
        return (
            _finding(
                "red",
                "env-harness",
                f"unsupported harness: {harness!r}",
                detail=f"choices: {', '.join(harnesses.HARNESS_NAMES)}",
            ),
        )
    try:
        adapter = harnesses.get_adapter(harness_name)
    except harnesses.HarnessError as error:
        return (
            _finding(
                "red",
                "env-harness",
                f"cannot construct harness adapter for {harness!r}: {error}",
            ),
        )

    resolved = adapter.resolve_executable()
    if resolved is None:
        return (
            _finding(
                "red",
                "env-harness",
                f"{harness} executable not found on PATH",
                detail=f"executable: {adapter.executable}",
            ),
        )
    return _check_env_harness_version(harness_name, resolved)


def _check_env_harness_version(
    harness: harnesses.HarnessName, executable: str
) -> tuple[PreflightFinding, ...]:
    """Classify the resolved harness CLI against the pinned support window.

    The severities are asymmetric on purpose, and the asymmetry is the whole
    policy. A known-incompatible release is ``red``: it blocks, because a run
    whose harness is known broken produces evidence nobody can trust. An
    unrecognised release is ``green``: it is reported on every preflight and in
    the preflight JSON, but it does not block. ``yellow`` is not available for
    this - :func:`_blocking_findings` treats yellow as a blocker, so warning in
    yellow would refuse to run on every harness release that lands after this
    build, which is the outage the "unknown warns" rule exists to avoid.
    """
    compatibility = harness_compatibility.check_executable(harness, executable)
    if compatibility.status == "incompatible":
        return (
            _finding(
                "red",
                "env-harness",
                f"{harness} {compatibility.version} is known-incompatible",
                detail=compatibility.detail,
            ),
        )
    if compatibility.status == "unknown":
        return (
            _finding(
                "green",
                "env-harness",
                f"unverified {harness} version (warning, not a blocker)",
                detail=compatibility.detail,
            ),
        )
    return ()


def _check_env_lock(
    plan_dir: Path,
    state_dir: str | Path | None,
) -> tuple[PreflightFinding, ...]:
    try:
        lock_info = state_locks.read_lock_info(plan_dir, state_dir)
    except OSError as error:
        return (
            _finding(
                "yellow",
                "env-lock",
                f"could not read lock info: {error}",
                path=str(plan_dir),
            ),
        )
    if lock_info is None:
        return ()
    if lock_info.stale:
        return (
            _finding(
                "yellow",
                "env-lock",
                f"stale lock present at {lock_info.path}",
                path=str(lock_info.path),
                detail=lock_info.reason,
            ),
        )
    return (
        _finding(
            "red",
            "env-lock",
            f"active lock held by pid {lock_info.pid} at {lock_info.path}",
            path=str(lock_info.path),
            detail=lock_info.reason,
        ),
    )


def _check_env_amx() -> tuple[PreflightFinding, ...]:
    amx_path = shutil.which("amx")
    if amx_path is None:
        return (
            _finding(
                "green",
                "env-amx",
                "amx hook indicator not on PATH (Agent Mux hooks disabled)",
                detail="optional",
            ),
        )
    return (
        _finding(
            "green",
            "env-amx",
            f"amx hook indicator available at {amx_path}",
            detail="Agent Mux hooks enabled",
        ),
    )


def _turn_budget_max_pending_sessions(max_pending_sessions: int | None) -> int:
    if max_pending_sessions is None:
        return TURN_BUDGET_DEFAULT_MAX_PENDING_SESSIONS
    if max_pending_sessions <= 0:
        raise ValueError("max_pending_sessions must be a positive integer")
    return max_pending_sessions


def check_env_turn_budget(
    snapshot: PlanSnapshot,
    *,
    max_pending_sessions: int | None = None,
) -> tuple[PreflightFinding, ...]:
    """Return the turn-budget and session-inventory findings for a snapshot.

    Public because the runner re-evaluates exactly these two after review
    repairs reopen sessions: the startup gate saw the plan as authored, and
    plan growth is what invalidates them.
    """
    total = len(snapshot.sessions)
    pending = sum(1 for status in snapshot.sessions if status.status == "pending")
    pending_limit = _turn_budget_max_pending_sessions(max_pending_sessions)
    if pending == 0:
        return (
            _finding(
                "green",
                "env-turn-budget",
                "all sessions complete; no turns required",
                detail=f"total sessions: {total}",
            ),
        )
    estimated_turns = pending * TURNS_PER_SESSION_ESTIMATE
    if pending > pending_limit:
        return (
            _finding(
                "yellow",
                "env-turn-budget",
                (
                    f"{pending} pending sessions exceeds max pending sessions "
                    f"{pending_limit}; pass --max-pending-sessions to override "
                    "or chunk the plan"
                ),
                detail=(
                    f"total sessions: {total}; rough turn budget "
                    f"~{estimated_turns} ({TURNS_PER_SESSION_ESTIMATE} turns/session); "
                    f"max pending sessions {pending_limit}"
                ),
            ),
        )
    return (
        _finding(
            "green",
            "env-turn-budget",
            (
                f"{pending} session(s) pending; rough turn budget "
                f"~{estimated_turns} turns"
            ),
            detail=(
                f"total sessions: {total}; "
                f"{TURNS_PER_SESSION_ESTIMATE} turns/session estimate; "
                f"max pending sessions {pending_limit}"
            ),
        ),
    )


def _run_environment_checks(
    *,
    plan_dir: Path,
    snapshot: PlanSnapshot | None,
    cwd: str | Path | None,
    harness: str | None,
    commit_mode: CommitMode | None,
    state_dir: str | Path | None,
    max_pending_sessions: int | None = None,
    repository_registry: workspaces.RepositoryRegistry | None = None,
    verification_context: VerificationContext | None = None,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    if cwd is not None:
        canonical_cwd = canonicalize_path(cwd)
        if repository_registry is None:
            repo_findings = _check_env_repo_root(canonical_cwd)
            findings.extend(repo_findings)
            if not repo_findings:
                findings.extend(_check_env_git(canonical_cwd, commit_mode))
        elif verification_context is not None:
            findings.extend(
                _check_registered_env_git(verification_context, commit_mode, snapshot)
            )

    if harness is not None:
        findings.extend(_check_env_harness(harness))

    findings.extend(_check_env_lock(plan_dir, state_dir))
    findings.extend(_check_env_amx())

    if snapshot is not None:
        findings.extend(
            check_env_turn_budget(
                snapshot,
                max_pending_sessions=max_pending_sessions,
            )
        )

    return tuple(findings)
