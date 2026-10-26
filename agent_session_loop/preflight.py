"""Preflight checks for split session plan directories.

The preflight engine inspects a plan directory and surfaces structured
findings that downstream tooling (``asl preflight`` and the guided repair
skill) can route to humans or machines. Each finding carries a severity
(``red``, ``yellow``, ``green``), a stable ``category`` string, and the
file/line/session context needed to localize the issue.

Session 5 covers the core checks that run against the durable plan files
alone: plan structure validation, canonical session-id filenames, manifest
readiness metadata (state + stable SHA-256), per-session completion
contracts, ambiguity sentinels in session bodies, and lightweight path
hygiene on contract ``path`` fields plus a scope lint for overly broad
verification commands. Session 6 adds environment-aware
checks that depend on the runtime working directory, the requested
harness, the active commit mode, and the operational lock state: repo
root, git worktree status plus a clean-tree check when a commit mode is
requested, harness executable availability, stale or active locks,
optional Agent Mux ``amx`` hook discovery, and a coarse turn-budget
estimate based on pending session count. Commit modes need no external
executable check: auto-commit re-enters this checkout as ``asl commit``,
so cleanliness of the tree is the only environment requirement left.
``run_core_preflight`` remains
environment-free; ``run_environment_preflight`` exposes only the
environment findings; ``run_preflight`` aggregates both into a single
report. Registered plans additionally resolve one workspace context, validate
and capture fail-closed evidence for every declared checkout beneath the
non-Git-capable common parent, and reuse those roots for command, baseline,
and commit-mode checks. Undeclared checkouts beneath the workspace root are
never inspected, so an unrelated broken sibling cannot block an otherwise
valid plan.
Session 13 attaches machine-readable repair hints at the report
level — every finding resolves to a stable :class:`RepairHint` via
:func:`repair_hint_for_finding`, and runtime pause reasons emitted by
``asl run``/``asl verify`` (``no-contract``, ``malformed-contract``,
``verify-failed`` plus its per-check ``failure_reason``) resolve via
:func:`runtime_repair_hint`. ``ok`` is reserved as the gate signal: it
is ``True`` only when no red **or** yellow finding was emitted, matching
the discipline ``asl verify`` already uses for its JSON ``ok`` field.

This module keeps the preflight entry points (``run_preflight`` and friends);
models, checks, hints, auto-repair and CLI output live in the sibling
``preflight_*`` modules, and callers import each symbol from its owner.
"""

from __future__ import annotations

from pathlib import Path

from . import (
    workspaces,
)
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_models import PlanSnapshot, ReadinessState
from .plans_registry import read_repository_registry
from .plans_snapshot import inspect_plan
from .preflight_baseline_checks import _check_baselines, _check_progress_journal
from .preflight_context import (
    _check_repository_registry,
    _repository_summaries,
    _resolve_preflight_context,
)
from .preflight_environment import _run_environment_checks
from .preflight_footprint import (
    _check_bound_repository_footprint,
    _check_bound_repository_intent,
    _check_path_references,
    _check_repository_footprint,
    _check_repository_intent,
)
from .preflight_models import CommitMode, PreflightFinding, PreflightReport, _finding
from .preflight_session_checks import (
    _check_ambiguity_sentinels,
    _check_canonical_session_ids,
    _check_manifest_readiness_block,
    _check_plan_structure,
    _check_session_contracts,
)
from .preflight_verification_checks import (
    _check_command_runnability,
    _check_duplicate_verification_commands,
    _check_verification_scope,
)
from .verification_models import VerificationContext


def run_core_preflight(plan_dir: str | Path) -> PreflightReport:
    """Run the core preflight checks against a split plan directory.

    Structural failures short-circuit deeper checks: if the plan directory is
    missing or unreadable, only a ``plan-structure`` finding is returned. When
    structure validates, the report also includes canonical session-id checks,
    manifest readiness metadata, per-session completion contracts, ambiguity
    sentinels, lightweight contract path-reference checks, and verification
    scope linting. Severity is
    surfaced via ``PreflightFinding.severity``; ``PreflightReport.ok`` is
    ``True`` only when every finding is green.
    """
    canonical_dir = canonicalize_path(plan_dir)
    findings: list[PreflightFinding] = []

    registry_findings, repository_registry = _check_repository_registry(canonical_dir)
    findings.extend(registry_findings)
    structure_findings, snapshot = _check_plan_structure(
        canonical_dir,
        repository_registry,
    )
    findings.extend(structure_findings)

    if not canonical_dir.is_dir():
        return PreflightReport(
            plan_dir=canonical_dir,
            findings=tuple(findings),
            repository_registry=repository_registry,
            repositories=_repository_summaries(repository_registry),
        )

    canonical_findings = _check_canonical_session_ids(canonical_dir)
    findings.extend(canonical_findings)

    if snapshot is None:
        return PreflightReport(
            plan_dir=canonical_dir,
            findings=tuple(findings),
            repository_registry=repository_registry,
            repositories=_repository_summaries(repository_registry),
        )

    readiness_findings, readiness, expected_hash = _check_manifest_readiness_block(
        snapshot
    )
    findings.extend(readiness_findings)

    contract_findings, contracts_by_session = _check_session_contracts(
        snapshot,
        repository_registry,
    )
    findings.extend(contract_findings)

    findings.extend(_check_ambiguity_sentinels(snapshot))
    findings.extend(_check_path_references(contracts_by_session))
    footprint_findings, footprint = _check_bound_repository_footprint(
        canonical_dir,
        contracts_by_session,
        snapshot,
        repository_registry,
    )
    findings.extend(footprint_findings)
    intent_findings, intent = _check_bound_repository_intent(
        canonical_dir,
        contracts_by_session,
        snapshot,
        repository_registry,
    )
    findings.extend(intent_findings)
    findings.extend(_check_verification_scope(contracts_by_session))
    findings.extend(_check_duplicate_verification_commands(contracts_by_session))
    findings.extend(_check_progress_journal(snapshot))

    return PreflightReport(
        plan_dir=canonical_dir,
        findings=tuple(findings),
        snapshot=snapshot,
        readiness=readiness,
        expected_readiness_sha256=expected_hash,
        contracts_by_session=contracts_by_session,
        repository_registry=repository_registry,
        repositories=_repository_summaries(repository_registry),
        repository_footprint=footprint,
        repository_intent=intent,
    )


def _session_paths(snapshot: PlanSnapshot | None) -> dict[str, str]:
    if snapshot is None:
        return {}
    return {status.id: str(status.session.path) for status in snapshot.sessions}


def run_environment_preflight(
    plan_dir: str | Path,
    *,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
    max_pending_sessions: int | None = None,
) -> PreflightReport:
    """Run only the environment preflight checks against a plan directory.

    Environment checks are compositional: ``cwd``, ``harness``, and
    ``commit_mode`` are all optional, and each one only triggers the
    checks that depend on it. Lock, ``amx``, and turn-budget checks run
    whenever the plan directory exists. Core plan checks (structure,
    contracts, readiness) are intentionally **not** invoked here; use
    ``run_preflight`` to aggregate both.
    """
    canonical_dir = canonicalize_path(plan_dir)
    registry_findings, repository_registry = _check_repository_registry(canonical_dir)
    snapshot: PlanSnapshot | None = None
    if canonical_dir.is_dir():
        try:
            snapshot = inspect_plan(
                canonical_dir,
                repository_registry=repository_registry,
            )
        except PlanError:
            snapshot = None

    verification_context, workspace_findings, repositories = _resolve_preflight_context(
        plan_dir=canonical_dir,
        cwd=cwd,
        registry=repository_registry,
    )

    env_findings = _run_environment_checks(
        plan_dir=canonical_dir,
        snapshot=snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
        max_pending_sessions=max_pending_sessions,
        repository_registry=repository_registry,
        verification_context=verification_context,
    )
    return PreflightReport(
        plan_dir=canonical_dir,
        findings=registry_findings + workspace_findings + env_findings,
        snapshot=snapshot,
        repository_registry=repository_registry,
        verification_context=verification_context,
        repositories=repositories,
    )


RESUME_READINESS_STATES: tuple[ReadinessState, ...] = ("loop-ready", "running")


_PREFLIGHT_REGISTRY_UNSET = object()


def run_resume_preflight(
    plan_dir: str | Path,
    *,
    snapshot: PlanSnapshot | None,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
    max_pending_sessions: int | None = None,
) -> PreflightReport | None:
    """Run an environment-only preflight when plan checks are redundant.

    Eligible only when a plan snapshot is supplied, the manifest carries
    parseable readiness metadata whose state is in
    :data:`RESUME_READINESS_STATES`, and the recorded readiness SHA-256 still
    matches the locally recomputed stable hash. That equality proves the plan
    files have not changed since preflight last cleared them, so every check
    whose only inputs are those files repeats a known-good verdict: contract
    parsing, ambiguity sentinels, path references, verification-scope linting,
    and the per-command runnability probes (~95% of a full preflight).

    The checks whose inputs live *outside* the readiness hash — the progress
    journal, recorded baseline evidence, and the repository footprint, which
    depends on the run's ``cwd`` rather than on plan content alone — are not
    covered by that proof and are not skipped by this optimization; they move
    to :func:`run_unhashed_state_checks`, which a caller runs to decide whether
    this reduced report may be trusted.

    Returns ``None`` when any precondition fails so callers fall back to the
    full :func:`run_preflight`. ``contracts_by_session`` is always empty: the
    runner re-reads the contract of each session as it starts it.
    """
    if snapshot is None:
        return None

    try:
        _, readiness, expected_hash = _check_manifest_readiness_block(snapshot)
    except PlanError:
        return None
    if readiness is None:
        return None
    if readiness.state not in RESUME_READINESS_STATES:
        return None
    if readiness.readiness_sha256 != expected_hash:
        return None

    canonical_dir = canonicalize_path(plan_dir)
    try:
        repository_registry = read_repository_registry(canonical_dir)
    except PlanError:
        return None
    verification_context, workspace_findings, repositories = _resolve_preflight_context(
        plan_dir=canonical_dir,
        cwd=cwd,
        registry=repository_registry,
    )
    findings = _run_environment_checks(
        plan_dir=canonical_dir,
        snapshot=snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
        max_pending_sessions=max_pending_sessions,
        repository_registry=repository_registry,
        verification_context=verification_context,
    )
    return PreflightReport(
        plan_dir=canonical_dir,
        findings=workspace_findings + findings,
        snapshot=snapshot,
        readiness=readiness,
        expected_readiness_sha256=expected_hash,
        contracts_by_session={},
        repository_registry=repository_registry,
        verification_context=verification_context,
        repositories=repositories,
    )


def run_unhashed_state_checks(
    plan_dir: str | Path,
    *,
    snapshot: PlanSnapshot,
    cwd: str | Path | None = None,
    repository_registry: workspaces.RepositoryRegistry
    | None
    | object = _PREFLIGHT_REGISTRY_UNSET,
    context: VerificationContext | None = None,
) -> tuple[PreflightFinding, ...]:
    """Check the plan state that ``stable_readiness_sha256`` does not cover.

    ``progress.md`` and ``baseline-results.json`` are deliberately outside the
    readiness hash, so a matching SHA proves nothing about them. A caller that
    accepts a :func:`run_resume_preflight` report has to confirm them here or
    it silently drops the runner's progress auto-repair and its
    stale-baseline gate. Completion contracts are re-parsed because the
    baseline check needs them and a malformed contract must stay fail-closed.
    The repository footprint is rechecked for the same reason: the readiness
    hash covers the plan, not the ``cwd`` the run binds it to.

    Any returned finding means the reduced report cannot stand in for a full
    one, so the caller falls back to :func:`run_preflight`.
    """
    canonical_dir = canonicalize_path(plan_dir)
    if repository_registry is _PREFLIGHT_REGISTRY_UNSET:
        try:
            resolved_registry = read_repository_registry(canonical_dir)
        except PlanError as error:
            return (
                _finding(
                    "red",
                    "repository-registry",
                    str(error),
                    path=str(canonical_dir / "manifest.md"),
                ),
            )
    elif repository_registry is None or isinstance(
        repository_registry,
        workspaces.RepositoryRegistry,
    ):
        resolved_registry = repository_registry
    else:
        raise TypeError("repository_registry must be a RepositoryRegistry or None")
    workspace_findings: tuple[PreflightFinding, ...] = ()
    verification_context = context
    if verification_context is None:
        verification_context, workspace_findings, _repositories = (
            _resolve_preflight_context(
                plan_dir=canonical_dir,
                cwd=cwd,
                registry=resolved_registry,
            )
        )
    contract_findings, contracts_by_session = _check_session_contracts(
        snapshot,
        resolved_registry,
    )
    footprint_findings: tuple[PreflightFinding, ...] = ()
    intent_findings: tuple[PreflightFinding, ...] = ()
    if cwd is not None:
        footprint_findings, _footprint = _check_repository_footprint(
            contracts_by_session,
            snapshot,
            cwd,
            resolved_registry,
            canonical_dir,
        )
        intent_findings, _intent = _check_repository_intent(
            contracts_by_session,
            snapshot,
            cwd,
            resolved_registry,
            canonical_dir,
        )
    baseline_findings: tuple[PreflightFinding, ...] = ()
    if resolved_registry is None or verification_context is not None:
        baseline_findings = _check_baselines(
            contracts_by_session,
            cwd,
            snapshot,
            plan_dir=canonical_dir,
            context=verification_context,
        )
    return (
        workspace_findings
        + contract_findings
        + footprint_findings
        + intent_findings
        + _check_progress_journal(snapshot)
        + baseline_findings
    )


def run_preflight(
    plan_dir: str | Path,
    *,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
    max_pending_sessions: int | None = None,
) -> PreflightReport:
    """Aggregate core and environment preflight findings into one report.

    Core findings are produced first via ``run_core_preflight`` and reuse
    the same plan snapshot when feeding the environment checks, so
    ``inspect_plan`` runs at most once per call.
    """
    core_report = run_core_preflight(plan_dir)
    verification_context, workspace_findings, repositories = _resolve_preflight_context(
        plan_dir=core_report.plan_dir,
        cwd=cwd,
        registry=core_report.repository_registry,
    )
    env_findings = _run_environment_checks(
        plan_dir=core_report.plan_dir,
        snapshot=core_report.snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
        max_pending_sessions=max_pending_sessions,
        repository_registry=core_report.repository_registry,
        verification_context=verification_context,
    )
    footprint_findings: tuple[PreflightFinding, ...] = ()
    footprint = core_report.repository_footprint
    if footprint is None and cwd is not None:
        footprint_findings, footprint = _check_repository_footprint(
            core_report.contracts_by_session,
            core_report.snapshot,
            cwd,
            core_report.repository_registry,
            core_report.plan_dir,
        )
    intent_findings: tuple[PreflightFinding, ...] = ()
    intent = core_report.repository_intent
    if intent is None and cwd is not None:
        intent_findings, intent = _check_repository_intent(
            core_report.contracts_by_session,
            core_report.snapshot,
            cwd,
            core_report.repository_registry,
            core_report.plan_dir,
        )
    command_findings: tuple[PreflightFinding, ...] = ()
    baseline_findings: tuple[PreflightFinding, ...] = ()
    if core_report.repository_registry is None or verification_context is not None:
        command_findings = _check_command_runnability(
            core_report.contracts_by_session,
            cwd,
            context=verification_context,
            session_paths=_session_paths(core_report.snapshot),
            footprint_escapes=frozenset(
                target.value
                for target in (footprint.out_of_root if footprint else ())
                if target.kind == "contract-cwd"
            ),
        )
        baseline_findings = _check_baselines(
            core_report.contracts_by_session,
            cwd,
            core_report.snapshot,
            plan_dir=core_report.plan_dir,
            context=verification_context,
        )
    return PreflightReport(
        plan_dir=core_report.plan_dir,
        findings=(
            core_report.findings
            + footprint_findings
            + intent_findings
            + workspace_findings
            + env_findings
            + command_findings
            + baseline_findings
        ),
        snapshot=core_report.snapshot,
        readiness=core_report.readiness,
        expected_readiness_sha256=core_report.expected_readiness_sha256,
        contracts_by_session=core_report.contracts_by_session,
        repository_registry=core_report.repository_registry,
        verification_context=verification_context,
        repositories=repositories,
        repository_footprint=footprint,
        repository_intent=intent,
    )
