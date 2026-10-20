"""Baseline identity keys, repository evidence, and check collection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import contracts, plans_session_ids, repository_evidence
from .baseline_cache import (
    _recipe_executable_identity,
    cache_key,
    environment_signature,
    repository_fingerprint,
)
from .contracts import VerificationCheck, VerifyRecipe
from .plans_errors import PlanError
from .verification_models import VerificationContext


def _single_check_recipe(check: VerificationCheck) -> VerifyRecipe:
    return VerifyRecipe(checks=(check,))


def baseline_identity_key(
    check: VerificationCheck,
    cwd: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    context: VerificationContext | None = None,
) -> str:
    """Return the fingerprint-independent cache key for a single check.

    The repository fingerprint is intentionally fixed to the empty string so
    that a recorded outcome stays findable after the working tree changes;
    staleness is detected by comparing the fingerprint stored *inside* the
    result, not by the key. Equivalent checks across sessions (same argv/path,
    possibly different names) collapse to one key because
    :func:`~agent_session_loop.baseline_cache.normalize_recipe` excludes the
    check name.
    """
    recipe = _single_check_recipe(check)
    verification_context = context or VerificationContext.legacy(cwd)
    return cache_key(
        recipe=recipe,
        cwd=verification_context.root,
        repo_fingerprint="",
        executable=_recipe_executable_identity(recipe),
        environment=environment_signature(env),
        registry_digest=verification_context.registry_digest,
    )


def aggregate_repository_fingerprint(
    context: VerificationContext,
    plan_dir: str | Path,
) -> str:
    """Hash ordered, alias-qualified evidence for every registered repository."""
    digest, available = capture_baseline_repository_evidence(context, plan_dir)
    if not available:
        raise PlanError(
            "cannot aggregate repository fingerprint because evidence is unavailable"
        )
    return digest


def capture_baseline_repository_evidence(
    context: VerificationContext,
    plan_dir: str | Path,
) -> tuple[str, bool]:
    """Capture reusable current evidence for baseline freshness checks."""
    if context.workspace is None:
        return repository_fingerprint(context.root), True
    return _registered_repository_fingerprint(context, plan_dir)


def _registered_repository_fingerprint(
    context: VerificationContext,
    plan_dir: str | Path,
) -> tuple[str, bool]:
    """Return aggregate digest and whether every repository probe was usable."""
    assert context.workspace is not None
    evidence = []
    available = True
    for repository in context.workspace.repositories:
        fingerprint = repository_evidence.capture_repository_fingerprint(
            repository.root, plan_dir
        )
        if repository_evidence.repository_evidence_unavailable(
            fingerprint
        ) is not None or not fingerprint.get("git"):
            available = False
        evidence.append(
            {
                "alias": repository.alias,
                "fingerprint": fingerprint,
            }
        )
    encoded = json.dumps(
        evidence, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest(), available


def baseline_evidence_is_current(
    result: dict[str, Any],
    context: VerificationContext,
    plan_dir: str | Path,
    *,
    current_evidence: tuple[str, bool] | None = None,
) -> bool:
    """Return whether a cache result matches the current plan-mode evidence."""
    fingerprint, available = (
        current_evidence
        if current_evidence is not None
        else capture_baseline_repository_evidence(context, plan_dir)
    )
    if context.workspace is None:
        return str(result.get("repository_fingerprint", "")) == fingerprint
    return (
        available
        and result.get("repository_evidence_available") is True
        and result.get("registry_digest") == context.registry_digest
        and result.get("aggregate_repository_fingerprint") == fingerprint
    )


def collect_baseline_checks(
    contracts_by_session: dict[str, Any],
) -> list[tuple[str, VerificationCheck]]:
    """Return ``(session_id, check)`` pairs that declare a baseline expectation.

    Only checks whose ``baseline_expectation`` is ``pass`` or ``fail`` are
    returned; ``skip`` (the legacy default) is omitted so plans authored before
    the field carry no baseline obligations. Pairs are sorted by session id then
    check name for deterministic execution and reporting order.
    """
    collected: list[tuple[str, VerificationCheck]] = []
    for session_id, contract in contracts_by_session.items():
        if contract is None:
            continue
        for check in contract.verify.checks:
            expectation = getattr(
                check, "baseline_expectation", contracts.DEFAULT_BASELINE_EXPECTATION
            )
            if expectation in ("pass", "fail"):
                collected.append((session_id, check))
    collected.sort(
        key=lambda pair: (plans_session_ids.parse_session_id(pair[0]), pair[1].name)
    )
    return collected
