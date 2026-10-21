"""Validate repair children and gate their executability with targeted baselines."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    baseline,
    contracts,
    plans_registry,
    preflight,
    preflight_cli,
    preflight_footprint,
    preflight_models,
    progress_repair,
    repository_scope,
    repository_scope_targets,
    review,
    state,
    verification_models,
    workspaces,
)
from . import (
    review_repair_models as repair_models,
)
from .plans_errors import PlanError
from .plans_files import canonicalize_path

_REPOSITORY_REVIEW_BASE_HEADING = "## Ordered repository review base"


_REPOSITORY_REVIEW_BASE_RE = re.compile(
    r"^- `(?P<alias>[^`]+)`: path `(?P<path>[^`]+)`; "
    r"origin `(?P<origin>[^`]+)`; HEAD `(?P<head>[^`]+)`\.$"
)


def _read_repository_review_bases(
    review_path: Path,
    repository_registry: workspaces.RepositoryRegistry | None,
) -> tuple[dict[str, Any], ...]:
    """Recover the ordered review base embedded in a repository-aware session."""
    if repository_registry is None:
        return ()
    try:
        text = review_path.read_text(encoding="utf-8")
    except OSError as error:
        raise repair_models.ReviewRepairError(
            f"could not read repository-aware review session: {error}"
        ) from error
    version = review.review_session_marker_version(text)
    if (
        version is None
        or version < review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
    ):
        # Historical review sessions can exist after a plan is migrated to a
        # registry. They remain readable, but do not claim vector evidence.
        return ()
    lines = text.splitlines()
    try:
        start = lines.index(_REPOSITORY_REVIEW_BASE_HEADING) + 1
    except ValueError as error:
        raise repair_models.ReviewRepairError(
            "repository-aware review session is missing its ordered repository base"
        ) from error
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    rows: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in lines[start:end]:
        match = _REPOSITORY_REVIEW_BASE_RE.fullmatch(line)
        if match is not None:
            alias = match.group("alias")
            if alias in rows:
                raise repair_models.ReviewRepairError(
                    f"repository-aware review base repeats alias {alias!r}"
                )
            head_text = match.group("head")
            current = {
                "alias": alias,
                "path": match.group("path"),
                "origin": match.group("origin"),
                "head": None if head_text == "(unborn HEAD)" else head_text,
                "worktree_dirty": False,
                "dirty_paths": [],
            }
            rows[alias] = current
            continue
        dirty_match = re.fullmatch(r"    - `([^`]+)`", line)
        if dirty_match is not None and current is not None:
            current["worktree_dirty"] = True
            current["dirty_paths"].append(dirty_match.group(1))
    ordered: list[dict[str, Any]] = []
    for repository in repository_registry.repositories:
        row = rows.get(repository.alias)
        if row is None:
            raise repair_models.ReviewRepairError(
                f"repository-aware review base is missing alias {repository.alias!r}"
            )
        if row["path"] != repository.path or row["origin"] != repository.origin:
            raise repair_models.ReviewRepairError(
                f"repository-aware review base identity changed for alias "
                f"{repository.alias!r}"
            )
        prefix = f"{repository.alias}:"
        if any(not path.startswith(prefix) for path in row["dirty_paths"]):
            raise repair_models.ReviewRepairError(
                f"repository-aware review base has unqualified dirty paths for "
                f"alias {repository.alias!r}"
            )
        ordered.append(row)
    unknown = set(rows).difference(repository_registry.aliases)
    if unknown:
        raise repair_models.ReviewRepairError(
            "repository-aware review base contains unknown aliases: "
            + ", ".join(sorted(unknown))
        )
    return tuple(ordered)


# ---------------------------------------------------------------------------
# Executability gate (targeted baselines + preflight + mark loop-ready)
# ---------------------------------------------------------------------------


def _run_targeted_baselines(
    plan_dir: Path,
    cwd: Path,
    contracts_by_session: dict[str, Any],
) -> dict[str, Any]:
    """Run baselines for child checks that declare a baseline expectation."""
    collected = baseline.collect_baseline_checks(contracts_by_session)
    if not collected:
        return {"check_count": 0, "checks": [], "unsatisfied": []}
    run = baseline.run_baseline(plan_dir, cwd)
    checks = [
        {
            "session_id": outcome.session_id,
            "name": outcome.name,
            "expectation": outcome.expectation,
            "status": outcome.status,
            "satisfied": outcome.satisfied,
            "detail": outcome.detail,
        }
        for outcome in run.outcomes
    ]
    return {
        "check_count": len(checks),
        "checks": checks,
        "unsatisfied": [entry for entry in checks if not entry["satisfied"]],
    }


def _strip_self_lock_findings(
    report: preflight_models.PreflightReport,
    lock_info: state.LockInfo | None,
) -> preflight_models.PreflightReport:
    """Drop the env-lock finding that points at this process's own lock.

    Materialization runs the preflight gate while holding the plan lock, so the
    lock we hold would otherwise register as an ``env-lock`` blocker. Mirrors
    ``readiness_gate._strip_own_lock_findings``.
    """
    if lock_info is None:
        return report
    own_pid = os.getpid()
    own_lock_path = str(lock_info.path)

    def is_self_lock(finding: preflight_models.PreflightFinding) -> bool:
        if finding.category != "env-lock":
            return False
        if finding.path != own_lock_path:
            return False
        return not (lock_info.pid is not None and lock_info.pid != own_pid)

    filtered = tuple(
        finding for finding in report.findings if not is_self_lock(finding)
    )
    if len(filtered) == len(report.findings):
        return report
    return preflight_models.PreflightReport(
        plan_dir=report.plan_dir,
        findings=filtered,
        snapshot=report.snapshot,
        readiness=report.readiness,
        expected_readiness_sha256=report.expected_readiness_sha256,
        contracts_by_session=report.contracts_by_session,
    )


def _child_footprint_targets(
    group_plan: repair_models.RepairGroupPlan,
) -> tuple[repository_scope_targets.PathTarget, ...]:
    """Collect the path evidence a rendered child carries, contract included."""
    return (
        *repository_scope_targets.targets_from_contract(group_plan.contract),
        *repository_scope_targets.extract_path_targets(group_plan.rendered_text),
    )


def _validate_children_footprint(
    plan_dir: Path,
    cwd: str | Path,
    group_plans: Sequence[repair_models.RepairGroupPlan],
) -> dict[str, Any]:
    """Judge the rendered children against the bound root before anything is written.

    The post-commit gate applies the same footprint rule through preflight, but
    by then the children are on disk and no rollback exists, so a child that
    reaches an unregistered external root leaves a half-authored plan behind. A
    registered plan is skipped: its registry already governs where it may reach.
    """
    clean: dict[str, Any] = {
        "loop_ready": True,
        "blockers": [],
        "non_bypassable_blockers": [],
        "baseline_summary": None,
    }
    if plans_registry.read_repository_registry(plan_dir) is not None:
        return clean
    provenance_roots = preflight_footprint.plan_provenance_exempt_roots(plan_dir)
    blockers: list[str] = []
    for group_plan in group_plans:
        child = group_plan.proposed_child_id.value
        footprint = repository_scope.resolve_footprint(
            _child_footprint_targets(group_plan),
            cwd,
            plan_dir=plan_dir,
            provenance_roots=provenance_roots,
        )
        for target in footprint.out_of_root:
            blockers.append(
                f"repository-footprint: repair group {group_plan.repair_group!r} "
                f"would author session {child} with a {target.kind} target that "
                f"escapes the bound root: {target.value} is outside "
                f"{footprint.root}"
            )
        if len(footprint.repo_roots) > 1:
            blockers.append(
                f"repository-footprint: repair group {group_plan.repair_group!r} "
                f"would author session {child} spanning "
                f"{len(footprint.repo_roots)} git repositories but manifest.md "
                "declares no `## Repositories` registry"
            )
    if not blockers:
        return clean
    return {
        "loop_ready": False,
        "blockers": blockers,
        "non_bypassable_blockers": ["repository-footprint"],
        "baseline_summary": None,
    }


def _validate_children_contract(
    plan_dir: Path,
    cwd: str | Path,
    group_plans: Sequence[repair_models.RepairGroupPlan],
) -> list[str]:
    """Resolve every proposed check against the effective verification context.

    ASI-136: a repair check whose ``repo``/``cwd`` cannot resolve is rejected by
    preflight's ``repository-contract`` finding — but that ran only *after* the
    children were on disk, so an unmaterializable check produced a permanently
    non-loop-ready pending child. The same resolution runs here, against the
    same context preflight builds, while the plan is still untouched.

    A context that cannot be established at all is not judged here: that is the
    run gate's business, and the post-authoring gate (with its rollback) still
    covers it.
    """
    try:
        context = verification_models.VerificationContext.from_plan(plan_dir, cwd)
    except PlanError:
        return []
    blockers: list[str] = []
    for group_plan in group_plans:
        child = group_plan.proposed_child_id.value
        for check in group_plan.contract.verify.checks:
            try:
                context.resolve_check(check)
            except PlanError as error:
                blockers.append(
                    f"repository-contract: repair group "
                    f"{group_plan.repair_group!r} would author session {child} "
                    f"with verification check {check.name!r} that cannot "
                    f"resolve: {error}"
                )
    return blockers


def _validate_children_before_authoring(
    plan_dir: Path,
    cwd: str | Path,
    group_plans: Sequence[repair_models.RepairGroupPlan],
) -> dict[str, Any]:
    """Judge rendered children on every rule that can be applied before a write.

    Two independent pre-write rules share one verdict: the repository footprint
    (which repositories a child would reach) and the contract's own resolution
    (whether each check can execute where it says it will). Both fail closed as
    non-bypassable: a child neither rule can accept is one no run could ever
    execute, so authoring it would only strand the plan.
    """
    footprint = _validate_children_footprint(plan_dir, cwd, group_plans)
    contract_blockers = _validate_children_contract(plan_dir, cwd, group_plans)
    if footprint["loop_ready"] and not contract_blockers:
        return footprint
    non_bypassable = list(footprint["non_bypassable_blockers"])
    if contract_blockers and "repository-contract" not in non_bypassable:
        non_bypassable.append("repository-contract")
    return {
        "loop_ready": False,
        "blockers": [*footprint["blockers"], *contract_blockers],
        "non_bypassable_blockers": non_bypassable,
        "baseline_summary": None,
    }


def _make_children_executable(
    plan_dir: Path,
    cwd: str | Path,
    children: Sequence[repair_models.RepairChild],
    state_dir: str | Path | None,
    lock_info: state.LockInfo | None,
) -> dict[str, Any]:
    canonical_cwd = canonicalize_path(cwd)
    contracts_by_session = {
        child.session_id.value: contracts.read_completion_contract(
            plan_dir, child.session_id
        )
        for child in children
    }
    baseline_summary = _run_targeted_baselines(
        plan_dir, canonical_cwd, contracts_by_session
    )
    if baseline_summary["unsatisfied"]:
        blockers = [
            (
                f"baseline check {entry['name']!r} expected "
                f"{entry['expectation']!r}, got {entry['status']!r}: "
                f"{entry['detail']}"
            )
            for entry in baseline_summary["unsatisfied"]
        ]
        return {
            "loop_ready": False,
            "blockers": blockers,
            "non_bypassable_blockers": [],
            "baseline_summary": baseline_summary,
        }

    report = preflight.run_preflight(plan_dir, cwd=canonical_cwd, state_dir=state_dir)
    report = _strip_self_lock_findings(report, lock_info)
    mark_blockers = preflight_cli._mark_loop_ready_blockers(report)
    if mark_blockers and all(
        blocker.category == "progress-journal-missing" for blocker in mark_blockers
    ):
        # Same auto-repair `asl preflight --mark-loop-ready` performs: the
        # journal is derivable from the completed handoffs, so a stale one must
        # not be the reason a run pauses.
        try:
            progress_repair.repair_progress_journal(plan_dir)
        except PlanError, OSError:
            pass
        else:
            report = _strip_self_lock_findings(
                preflight.run_preflight(
                    plan_dir, cwd=canonical_cwd, state_dir=state_dir
                ),
                lock_info,
            )
            mark_blockers = preflight_cli._mark_loop_ready_blockers(report)
    if mark_blockers:
        blockers = [
            f"{finding.category}: {finding.message}" for finding in mark_blockers
        ]
        return {
            "loop_ready": False,
            "blockers": blockers,
            "non_bypassable_blockers": sorted(
                {
                    finding.category
                    for finding in mark_blockers
                    if finding.category
                    in preflight_models.NON_BYPASSABLE_PREFLIGHT_CATEGORIES
                }
            ),
            "baseline_summary": baseline_summary,
        }
    marked, mark_error = preflight_cli._mark_loop_ready(report)
    if not marked:
        return {
            "loop_ready": False,
            "blockers": [mark_error or "could not mark the plan loop-ready"],
            "non_bypassable_blockers": [],
            "baseline_summary": baseline_summary,
        }
    return {
        "loop_ready": True,
        "blockers": [],
        "non_bypassable_blockers": [],
        "baseline_summary": baseline_summary,
    }
