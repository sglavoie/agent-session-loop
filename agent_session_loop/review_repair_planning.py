"""Build and validate a repair-session proposal without changing the plan."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    authoring_render,
    plans_discovery,
    plans_models,
    plans_registry,
    plans_session_scope,
    review,
    workspaces,
)
from . import (
    review_repair_models as repair_models,
)
from . import (
    review_repair_render as repair_render,
)
from . import (
    review_repair_scope as repair_scope,
)
from . import (
    review_repair_validation as repair_validation,
)
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_session_ids import SessionId, parse_session_id


def _build_repair_group_plans(
    plan_dir: Path,
    review_id: SessionId,
    grouped: Sequence[tuple[str, tuple[dict[str, Any], ...]]],
    *,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> list[repair_models.RepairGroupPlan]:
    """Validate and fully render every proposed child without writing files."""
    group_plans: list[repair_models.RepairGroupPlan] = []
    previous_id = review_id
    for index, (group, group_findings) in enumerate(grouped, start=1):
        child_id = parse_session_id(f"{review_id.value}.{index}")
        foreign_artifacts = repair_scope.foreign_plan_artifact_references(
            plan_dir, child_id, group_findings
        )
        if foreign_artifacts:
            raise repair_models.ReviewRepairError(
                f"repair group {group!r} would send repair child "
                f"{child_id.value} at plan session artifacts it is not "
                f"authorized to change: {', '.join(foreign_artifacts)}; a "
                "repair child may only rewrite its own session file, so apply "
                "a plan-contract repair outside the repair cycle rather than "
                "materializing a child that cannot perform it"
            )
        title = f"Review repair: {group}"
        finding_ids = tuple(finding["id"] for finding in group_findings)
        done_when = tuple(
            repair_scope._dedupe_preserving_order(
                [
                    bullet
                    for finding in group_findings
                    for bullet in finding["done_criteria"]
                ]
            )
        )
        read_repositories: tuple[str, ...] = ()
        write_repositories: tuple[str, ...] = ()
        write_repository_sources: dict[str, str] = {}
        scope: plans_session_scope.SessionScope | None = None
        marker_version = repair_models.GENERATED_REVIEW_REPAIR_SESSION_VERSION
        if repository_registry is not None:
            write_repository_sources = (
                repair_scope.repair_group_write_repository_sources(
                    group,
                    group_findings,
                    repository_registry=repository_registry,
                )
            )
            read_repositories = repository_registry.aliases
            write_repositories = tuple(write_repository_sources)
            scope = plans_session_scope.SessionScope(
                reads=read_repositories,
                writes=write_repositories,
            )
            marker_version = (
                repair_models.REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION
            )
        checks = tuple(
            repair_render._merge_verification_checks(
                group,
                group_findings,
                repository_registry=repository_registry,
            )
        )
        body_lines = repair_render._render_child_body(
            child_id,
            review_id,
            repair_group=group,
            finding_ids=finding_ids,
            findings=group_findings,
            marker_version=marker_version,
            write_repository_sources=write_repository_sources,
            plan_dir=plan_dir,
        )
        try:
            contract = authoring_render.contract_from_payload(
                child_id,
                done_when,
                {"session": child_id.value, "checks": list(checks)},
                repository_registry=repository_registry,
            )
            rendered_text = authoring_render.render_session_file(
                session_id=child_id,
                title=title,
                instruction_lines=body_lines,
                heading_level=2,
                is_first=False,
                previous_session_id=previous_id,
                contract=contract,
                require_contract=True,
                scope=scope,
                repository_registry=repository_registry,
            )
            if repository_registry is not None:
                rendered_text = rendered_text.replace(
                    "- **Files modified**: (none yet)",
                    "- **Files modified**: (none)",
                    1,
                )
        except PlanError as error:
            raise repair_models.ReviewRepairError(
                f"could not author repair child {child_id.value}: {error}"
            ) from error
        group_plans.append(
            repair_models.RepairGroupPlan(
                repair_group=group,
                proposed_child_id=child_id,
                title=title,
                finding_ids=finding_ids,
                done_when=done_when,
                checks=checks,
                findings=tuple(group_findings),
                contract=contract,
                rendered_text=rendered_text,
                read_repositories=read_repositories,
                write_repositories=write_repositories,
                write_repository_sources=write_repository_sources,
            )
        )
        previous_id = child_id
    return group_plans


# ---------------------------------------------------------------------------
# Plan-structure helpers
# ---------------------------------------------------------------------------


def find_generated_review_session(
    plan_dir: str | Path,
) -> plans_models.SessionFile | None:
    """Return the latest generated review session in ``plan_dir``, if any."""
    canonical_dir = canonicalize_path(plan_dir)
    candidates: list[plans_models.SessionFile] = []
    for session in plans_discovery.discover_session_files(canonical_dir):
        try:
            body = session.path.read_text(encoding="utf-8")
        except OSError:
            continue
        if review.contains_review_session_marker(body):
            candidates.append(session)
    if not candidates:
        return None
    return max(candidates, key=lambda session: session.session_id.sort_key)


def _existing_children(
    sessions: Sequence[plans_models.SessionFile], review_id: SessionId
) -> list[plans_models.SessionFile]:
    top = review_id.components[0]
    return [
        session
        for session in sessions
        if len(session.session_id.components) > 1
        and session.session_id.components[0] == top
    ]


def _immediate_successor(
    sessions: Sequence[plans_models.SessionFile], review_id: SessionId
) -> plans_models.SessionFile | None:
    top = review_id.components[0]
    later = [session for session in sessions if session.session_id.components[0] > top]
    if not later:
        return None
    return min(later, key=lambda session: session.session_id.sort_key)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _artifact_sha256_or_none(digest_fn: Any, canonical_dir: Path) -> str | None:
    """Return artifact provenance when readable without masking validation."""
    try:
        return str(digest_fn(canonical_dir))
    except PlanError:
        return None


def _invalid_plan(
    review_id: SessionId | None,
    error: str,
    *,
    detail: str | None = None,
    validated_findings: Sequence[dict[str, Any]] = (),
    finding_fingerprints: Sequence[str] = (),
    proposed_child_ids: Sequence[SessionId] = (),
    existing_child_ids: Sequence[SessionId] = (),
    review_output_sha256: str | None = None,
    review_artifact_sha256: str | None = None,
    repository_bases: Sequence[dict[str, Any]] = (),
) -> repair_models.ReviewRepairPlan:
    return repair_models.ReviewRepairPlan(
        ok=False,
        reason=repair_models.REVIEW_REPAIR_REASON_MALFORMED,
        review_session_id=review_id,
        validated_findings=tuple(validated_findings),
        finding_fingerprints=tuple(finding_fingerprints),
        proposed_child_ids=tuple(proposed_child_ids),
        existing_child_ids=tuple(existing_child_ids),
        review_output_sha256=review_output_sha256,
        review_artifact_sha256=review_artifact_sha256,
        repository_bases=tuple(repository_bases),
        error=error,
        detail=detail,
    )


def plan_review_repair_sessions(
    plan_dir: str | Path,
    *,
    review_session_id: int | str | SessionId | None = None,
) -> repair_models.ReviewRepairPlan:
    """Validate and specify review repair children without mutating state.

    This is the single validation/specification path used by materialization
    and read-only continuation consumers. It validates the existing v2 review
    contract, resolves the source review, detects child conflicts, enforces the
    group cap, merges group contracts, and renders every proposed child wholly
    in memory. It never acquires a lock or writes plan/state files.
    """
    canonical_dir = canonicalize_path(plan_dir)
    repository_registry = plans_registry.read_repository_registry(canonical_dir)
    output_sha256 = _artifact_sha256_or_none(
        review.final_review_output_sha256, canonical_dir
    )
    artifact_sha256 = _artifact_sha256_or_none(
        review.final_review_artifact_sha256, canonical_dir
    )
    verdict = review.load_review_verdict(canonical_dir, require_artifact=True)
    if not verdict["ok"]:
        return _invalid_plan(
            None,
            f"structured review verdict is not usable: {verdict.get('error')}",
            detail=str(verdict.get("reason")),
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
        )

    # Resolve the review session the children hang beneath.
    if review_session_id is not None:
        try:
            review_id = parse_session_id(review_session_id)
        except ValueError as error:
            return _invalid_plan(
                None,
                f"invalid review session id: {error}",
                review_output_sha256=output_sha256,
                review_artifact_sha256=artifact_sha256,
            )
        if not (canonical_dir / review_id.filename).is_file():
            return _invalid_plan(
                review_id,
                f"review session file not found: {review_id.filename}",
                review_output_sha256=output_sha256,
                review_artifact_sha256=artifact_sha256,
            )
    else:
        found = find_generated_review_session(canonical_dir)
        if found is None:
            return _invalid_plan(
                None,
                "no generated review session found in the plan",
                review_output_sha256=output_sha256,
                review_artifact_sha256=artifact_sha256,
            )
        review_id = found.session_id

    if not review_id.is_integer:
        return _invalid_plan(
            review_id,
            f"review session id {review_id.value!r} must be a top-level id",
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
        )

    try:
        repository_bases = repair_validation._read_repository_review_bases(
            canonical_dir / review_id.filename,
            repository_registry,
        )
    except repair_models.ReviewRepairError as error:
        return _invalid_plan(
            review_id,
            str(error),
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
        )

    findings = tuple(verdict.get("findings", ()))
    fingerprints = review.finding_fingerprints(list(findings)) or ()
    if not findings:
        return repair_models.ReviewRepairPlan(
            ok=True,
            reason=repair_models.REVIEW_REPAIR_REASON_NO_FINDINGS,
            review_session_id=review_id,
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
            repository_bases=repository_bases,
        )

    sessions = plans_discovery.discover_session_files(canonical_dir)
    grouped = repair_scope.group_findings_by_repair_group(findings)
    proposed_child_ids = tuple(
        parse_session_id(f"{review_id.value}.{index}")
        for index in range(1, len(grouped) + 1)
    )
    existing = _existing_children(sessions, review_id)
    existing_child_ids = tuple(session.session_id for session in existing)
    if existing:
        ids = ", ".join(session_id.value for session_id in existing_child_ids)
        return _invalid_plan(
            review_id,
            "repair children already exist for review session "
            f"{review_id.value}: {ids}",
            validated_findings=findings,
            finding_fingerprints=fingerprints,
            proposed_child_ids=proposed_child_ids,
            existing_child_ids=existing_child_ids,
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
            repository_bases=repository_bases,
        )

    if len(grouped) > repair_models.MAX_REPAIR_GROUPS:
        return _invalid_plan(
            review_id,
            f"{len(grouped)} repair groups exceed the maximum of "
            f"{repair_models.MAX_REPAIR_GROUPS}; group findings by subsystem before "
            "materializing",
            validated_findings=findings,
            finding_fingerprints=fingerprints,
            proposed_child_ids=proposed_child_ids,
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
            repository_bases=repository_bases,
        )

    try:
        repair_groups = tuple(
            _build_repair_group_plans(
                canonical_dir,
                review_id,
                grouped,
                repository_registry=repository_registry,
            )
        )
    except repair_models.ReviewRepairError as error:
        return _invalid_plan(
            review_id,
            str(error),
            validated_findings=findings,
            finding_fingerprints=fingerprints,
            proposed_child_ids=proposed_child_ids,
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
            repository_bases=repository_bases,
        )

    return repair_models.ReviewRepairPlan(
        ok=True,
        reason=repair_models.REVIEW_REPAIR_REASON_OK,
        review_session_id=review_id,
        validated_findings=findings,
        finding_fingerprints=tuple(fingerprints),
        repair_groups=repair_groups,
        proposed_child_ids=proposed_child_ids,
        review_output_sha256=output_sha256,
        review_artifact_sha256=artifact_sha256,
        repository_bases=repository_bases,
    )
