"""Merge verification checks and render generated repair-session bodies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    contracts,
    workspaces,
)
from . import (
    review_repair_models as repair_models,
)
from . import (
    review_repair_scope as repair_scope,
)
from .plans_session_ids import SessionId, parse_session_id


def _merge_verification_checks(
    group: str,
    group_findings: Sequence[dict[str, Any]],
    *,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> list[dict[str, Any]]:
    """Merge a group's findings' verification checks without repeated work.

    Named checks are kept verbatim unless the same name occurs in multiple
    repositories, in which case each colliding name becomes ``Alias:name``.
    Generated qualifications receive a numeric suffix when needed so they
    cannot collide with a reviewer-supplied literal name. An unnamed check is
    given a stable, finding-scoped synthetic name so cross-finding merges
    cannot collide on the per-finding default names. Two
    checks in the same repository that share a name but disagree on payload are
    a genuine conflict and fail closed. Commands with different names but the
    same effective argv/cwd are collapsed when their remaining execution
    settings also agree. Conflicting settings for the same command fail closed
    instead of materializing a contract that preflight would reject as
    duplicate.
    """
    named_checks: list[dict[str, Any]] = []
    by_name: dict[tuple[str | None, str], dict[str, Any]] = {}
    repositories_by_name: dict[str, set[str | None]] = {}
    for finding in group_findings:
        for index, check in enumerate(finding["verification_checks"]):
            payload = dict(check)
            name = payload.get("name") or f"{finding['id']}-check-{index + 1}"
            payload["name"] = name
            repository = (
                payload.get("repo") if repository_registry is not None else None
            )
            scoped_name = (repository, name)
            repositories_by_name.setdefault(name, set()).add(repository)
            if scoped_name in by_name:
                if by_name[scoped_name] != payload:
                    raise repair_models.ReviewRepairError(
                        f"repair group {group!r} has conflicting definitions "
                        f"for verification check {name!r}"
                    )
                continue
            by_name[scoped_name] = payload
            named_checks.append(payload)

    colliding_names = {
        name
        for name, repositories in repositories_by_name.items()
        if len(repositories) > 1
    }
    emitted_names: dict[tuple[str | None, str], str] = {}
    used_names = {
        payload["name"]
        for payload in named_checks
        if payload["name"] not in colliding_names
    }
    for payload in named_checks:
        original_name = payload["name"]
        repository = payload.get("repo") if repository_registry is not None else None
        emitted_name = original_name
        if original_name in colliding_names:
            base_name = f"{repository}:{original_name}"
            emitted_name = base_name
            suffix = 2
            while emitted_name in used_names:
                emitted_name = f"{base_name}#{suffix}"
                suffix += 1
            payload["name"] = emitted_name
            used_names.add(emitted_name)
        emitted_names[(repository, original_name)] = emitted_name

    for payload in named_checks:
        dependencies = payload.get("on_failure_of")
        if not isinstance(dependencies, list):
            continue
        repository = payload.get("repo") if repository_registry is not None else None
        payload["on_failure_of"] = [
            emitted_names.get((repository, dependency), dependency)
            for dependency in dependencies
        ]

    merged: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    validation_session_id = parse_session_id("1")
    commands_by_identity: dict[
        tuple[str | None, tuple[str, ...], str], contracts.CommandCheck
    ] = {}
    for index, payload in enumerate(named_checks):
        parsed = contracts._parse_check(
            payload,
            index=index,
            session_id=validation_session_id,
            repository_registry=repository_registry,
        )
        if not isinstance(parsed, contracts.CommandCheck):
            merged.append(payload)
            continue
        identity = (parsed.repo, parsed.argv, parsed.cwd or ".")
        previous = commands_by_identity.get(identity)
        if previous is None:
            commands_by_identity[identity] = parsed
            merged.append(payload)
            continue
        settings = (
            parsed.timeout_seconds,
            parsed.repo,
            parsed.cwd or ".",
            parsed.baseline_expectation,
            parsed.on_failure_of,
            parsed.review_replay,
        )
        previous_settings = (
            previous.timeout_seconds,
            previous.repo,
            previous.cwd or ".",
            previous.baseline_expectation,
            previous.on_failure_of,
            previous.review_replay,
        )
        if settings != previous_settings:
            raise repair_models.ReviewRepairError(
                f"repair group {group!r} has conflicting settings for "
                f"duplicate verification commands {previous.name!r} and "
                f"{parsed.name!r}"
            )
        aliases[parsed.name] = previous.name

    for payload in merged:
        dependencies = payload.get("on_failure_of")
        if not isinstance(dependencies, list):
            continue
        payload["on_failure_of"] = repair_scope._dedupe_preserving_order(
            [aliases.get(name, name) for name in dependencies]
        )
    return merged


def _render_child_body(
    child_id: SessionId,
    review_id: SessionId,
    *,
    repair_group: str,
    finding_ids: Sequence[str],
    findings: Sequence[dict[str, Any]],
    marker_version: int = repair_models.GENERATED_REVIEW_REPAIR_SESSION_VERSION,
    write_repository_sources: Mapping[str, str] | None = None,
    plan_dir: Path | None = None,
) -> list[str]:
    # Evidence that cites another session's packet explains why the work is
    # owed; it is never a write target, and the child is told so (ASI-162).
    artifact_pattern = (
        None if plan_dir is None else repair_scope._plan_artifact_pattern(plan_dir)
    )
    lines: list[str] = [
        repair_models.REVIEW_REPAIR_SESSION_MARKER,
        repair_models.review_repair_session_version_marker(marker_version),
        "",
        "## Review repair scope",
        "",
        (
            f"- Lineage: repair session `{child_id.value}` materialized from "
            f"generated review session `{review_id.value}` in this plan "
            "directory."
        ),
        f"- Repair group: `{repair_group}`.",
        (
            f"- Addresses {len(finding_ids)} review finding(s): "
            f"{', '.join(finding_ids)}."
        ),
    ]
    if write_repository_sources:
        lines.append(
            "- Write scope: "
            f"{repair_scope._render_write_repository_sources(write_repository_sources)}."
        )
    lines.extend(
        [
            "",
            "## Findings to repair",
            "",
        ]
    )
    for finding in findings:
        lines.append(f"### {finding['id']} ({finding['severity']}): {finding['title']}")
        lines.append("")
        lines.append(finding["explanation"])
        lines.append("")
        lines.append("Evidence:")
        for evidence in finding["evidence"]:
            symbol = evidence.get("symbol")
            if symbol:
                line = f"- `{evidence['path']}` — `{symbol}`"
            else:
                line = f"- `{evidence['path']}`"
            if artifact_pattern is not None and repair_scope._foreign_plan_artifacts_in(
                artifact_pattern, child_id, str(evidence["path"])
            ):
                line += " — read-only reference (do not edit)"
            lines.append(line)
        lines.append("")
        lines.append("Done criteria:")
        for bullet in finding["done_criteria"]:
            lines.append(f"- {bullet}")
        lines.append("")
    lines.extend(
        [
            "## Child scope",
            "",
            (
                "- Fix only the issues in the findings above; do not expand "
                "scope to other subsystems or unrelated findings."
            ),
            (
                "- Address each demonstrated failure across the workflow it "
                "affects, but do not add unrelated scope or turn optional "
                "improvements into acceptance criteria."
            ),
            (
                "- Leave every targeted check in the completion contract "
                "passing, but do not rerun the contract solely as a final "
                "gate: ASL runs it authoritatively after this session, "
                "restores the session to pending on failure, and hands the "
                "failed-check evidence to the repair turn."
            ),
            (
                "- Running a check again, or running a broader one, is "
                "justified when an edit since that result invalidated it, "
                "when a failure needs diagnosis, or when a concrete "
                "correctness concern none of your checks covered remains. "
                "Nothing here caps how many checks you run or how long they "
                "take: keep the focused checks that cover the behavior this "
                "repair changed. Record the brief reason in this session's "
                "Handoff whenever you repeat or widen a check on that basis."
            ),
            (
                "- When the evidence supplied to this session already "
                "attributes a failure to another repair group's findings, "
                "treat that attribution as the explanation and leave the "
                "failure to that group. Investigate it further only when "
                "what you observe contradicts the supplied evidence, and say "
                "so in this session's Handoff when it does."
            ),
            (
                "- Do not commit the repair yourself; ASL owns the "
                "post-verification commit for this generated repair child."
            ),
            "- Record results in this session's Handoff when complete.",
        ]
    )
    return lines
