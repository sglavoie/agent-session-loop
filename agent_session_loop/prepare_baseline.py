"""Baseline stage: run or reuse baseline checks for prepared plans."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import baseline
from .baseline_cache import BASELINE_RESULTS_FILENAME, BaselineCache
from .prepare_results import _REUSABLE_BASELINE_STATUSES
from .verification import VerificationContext


def _baseline_check_entry(
    session_id: str,
    name: str,
    expectation: str,
    status: str,
    satisfied: bool,
    detail: str,
    *,
    repo: str | None = None,
    resolved_root: str | None = None,
    qualified_cwd: str | None = None,
    qualified_path: str | None = None,
) -> dict[str, Any]:
    payload = {
        "session_id": session_id,
        "name": name,
        "expectation": expectation,
        "status": status,
        "satisfied": satisfied,
        "detail": detail,
    }
    if repo is not None:
        payload.update(
            {
                "repo": repo,
                "resolved_root": resolved_root,
                "qualified_cwd": qualified_cwd,
                "qualified_path": qualified_path,
            }
        )
    return payload


def _cached_baseline_entries(
    plan_dir: Path,
    context: VerificationContext,
    unique_checks: list[tuple[str, str, Any]],
) -> list[dict[str, Any]] | None:
    """Return check entries from recorded evidence, or ``None`` when any
    check lacks valid (current-fingerprint, pass/fail) evidence."""
    cache = BaselineCache.load(plan_dir / BASELINE_RESULTS_FILENAME)
    entries: list[dict[str, Any]] = []
    current_evidence = baseline.capture_baseline_repository_evidence(context, plan_dir)
    for key, session_id, check in unique_checks:
        record = cache.get(key)
        if record is None:
            return None
        result = record.result
        if not baseline.baseline_evidence_is_current(
            result,
            context,
            plan_dir,
            current_evidence=current_evidence,
        ):
            return None
        if str(result.get("status", "")) not in _REUSABLE_BASELINE_STATUSES:
            return None
        entries.append(
            _baseline_check_entry(
                session_id,
                check.name,
                getattr(check, "baseline_expectation", "skip"),
                str(result.get("status", "")),
                bool(result.get("satisfied", False)),
                str(result.get("detail", "")),
                repo=(str(result["repo"]) if result.get("repo") is not None else None),
                resolved_root=(
                    str(result["resolved_root"])
                    if result.get("resolved_root") is not None
                    else None
                ),
                qualified_cwd=(
                    str(result["qualified_cwd"])
                    if result.get("qualified_cwd") is not None
                    else None
                ),
                qualified_path=(
                    str(result["qualified_path"])
                    if result.get("qualified_path") is not None
                    else None
                ),
            )
        )
    return entries


def _run_baseline_stage(
    plan_dir: Path,
    context: VerificationContext,
    contracts_by_session: dict[str, Any],
) -> dict[str, Any]:
    """Execute (or reuse) baseline evidence and summarize the outcomes."""
    collected = baseline.collect_baseline_checks(contracts_by_session)
    if not collected:
        return {"check_count": 0, "reused": False, "checks": [], "unsatisfied": []}

    unique_checks: list[tuple[str, str, Any]] = []
    seen_keys: set[str] = set()
    for session_id, check in collected:
        key = baseline.baseline_identity_key(check, context.root, context=context)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_checks.append((key, session_id, check))

    cached = _cached_baseline_entries(plan_dir, context, unique_checks)
    if cached is not None:
        checks = cached
        reused = True
    else:
        run = baseline.run_baseline(plan_dir, context.root, context=context)
        checks = [
            _baseline_check_entry(
                outcome.session_id,
                outcome.name,
                outcome.expectation,
                outcome.status,
                outcome.satisfied,
                outcome.detail,
                repo=outcome.repo,
                resolved_root=outcome.resolved_root,
                qualified_cwd=outcome.qualified_cwd,
                qualified_path=outcome.qualified_path,
            )
            for outcome in run.outcomes
        ]
        reused = False

    return {
        "check_count": len(checks),
        "reused": reused,
        "checks": checks,
        "unsatisfied": [entry for entry in checks if not entry["satisfied"]],
    }
