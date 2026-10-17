"""Derive repair-group write scope and reject foreign plan artifacts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    plans_session_ids,
    plans_session_scope,
    workspaces,
)
from . import (
    review_repair_models as repair_models,
)
from .plans_errors import PlanError
from .plans_session_ids import SessionId, parse_session_id

# ---------------------------------------------------------------------------
# Grouping and child specs
# ---------------------------------------------------------------------------


def group_findings_by_repair_group(
    findings: Sequence[dict[str, Any]],
) -> list[tuple[str, tuple[dict[str, Any], ...]]]:
    """Group findings by ``repair_group``, preserving first-appearance order.

    Every finding belongs to exactly one group (its own ``repair_group``); no
    finding is omitted or assigned twice.
    """
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        group = finding["repair_group"]
        if group not in buckets:
            buckets[group] = []
            order.append(group)
        buckets[group].append(finding)
    return [(group, tuple(buckets[group])) for group in order]


def repair_group_write_repository_sources(
    group: str,
    group_findings: Sequence[Mapping[str, Any]],
    *,
    repository_registry: workspaces.RepositoryRegistry,
) -> dict[str, str]:
    """Return the aliases a repair group must write, mapped to their provenance.

    A finding's work is not confined to its ``repo``. :func:`_render_child_body`
    prints every ``evidence[].path`` to the child as its work list and
    :func:`_merge_verification_checks` puts every ``verification_checks[].repo``
    into the child's completion contract, so all three attribute writes. Deriving
    the scope from ``repo`` alone hands the child a work list naming files it is
    not authorized to write.

    The union is bounded by the group's own findings and never by the registry,
    so a group whose evidence and checks all name one alias still yields a
    single-alias write scope. Aliases are keyed in registry order and each maps
    to its strongest attribution (``repo`` over ``evidence`` over ``check``).
    """
    sources: dict[str, str] = {}
    unknown: set[str] = set()

    def attribute(alias: str, source: str) -> None:
        if alias not in repository_registry.aliases:
            unknown.add(alias)
            return
        current = sources.get(alias)
        if (
            current is None
            or repair_models._WRITE_REPOSITORY_SOURCE_RANK[source]
            < repair_models._WRITE_REPOSITORY_SOURCE_RANK[current]
        ):
            sources[alias] = source

    for finding in group_findings:
        alias = finding.get("repo")
        if not isinstance(alias, str) or not alias:
            raise repair_models.ReviewRepairError(
                f"repair group {group!r} contains a finding without an "
                "explicit repository alias"
            )
        attribute(alias, repair_models.WRITE_REPOSITORY_SOURCE_REPO)
        for evidence in finding["evidence"]:
            path = evidence["path"]
            try:
                qualified = plans_session_scope.parse_qualified_path(path)
            except (PlanError, ValueError) as error:
                raise repair_models.ReviewRepairError(
                    f"repair group {group!r} has evidence path {path!r} that is "
                    f"not a qualified Alias:path reference: {error}"
                ) from error
            attribute(qualified.alias, repair_models.WRITE_REPOSITORY_SOURCE_EVIDENCE)
        for check in finding["verification_checks"]:
            check_repository = check.get("repo")
            if not isinstance(check_repository, str) or not check_repository:
                raise repair_models.ReviewRepairError(
                    f"repair group {group!r} has a verification check without an "
                    "explicit repository alias"
                )
            attribute(check_repository, repair_models.WRITE_REPOSITORY_SOURCE_CHECK)

    if unknown:
        raise repair_models.ReviewRepairError(
            f"repair group {group!r} references unknown repository aliases: "
            f"{', '.join(sorted(unknown))}"
        )
    return {
        alias: sources[alias]
        for alias in repository_registry.aliases
        if alias in sources
    }


def required_write_repositories(
    group: str,
    group_findings: Sequence[Mapping[str, Any]],
    *,
    repository_registry: workspaces.RepositoryRegistry,
) -> tuple[str, ...]:
    """Return, in registry order, the aliases a repair group must write.

    A projection of :func:`repair_group_write_repository_sources` for callers
    that need the scope without its provenance. Both come from that one union,
    so a child regenerated from this and the provenance reported beside it can
    never describe different repositories.
    """
    return tuple(
        repair_group_write_repository_sources(
            group,
            group_findings,
            repository_registry=repository_registry,
        )
    )


def _plan_artifact_pattern(plan_dir: Path) -> re.Pattern[str]:
    return re.compile(
        re.escape(str(plan_dir))
        + rf"/session-({plans_session_ids.SESSION_ID_PATTERN})\.(?:expand\.)?md"
    )


def _foreign_plan_artifacts_in(
    pattern: re.Pattern[str], child_id: SessionId, text: str
) -> dict[str, SessionId]:
    referenced: dict[str, SessionId] = {}
    for match in pattern.finditer(text):
        if match.group(1) == child_id.value:
            continue
        name = match.group(0).rsplit("/", 1)[-1]
        referenced[name] = parse_session_id(match.group(1))
    return referenced


def foreign_plan_artifact_references(
    plan_dir: Path,
    child_id: SessionId,
    group_findings: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return the plan session artifacts a repair group would make its child edit.

    A task packet makes exactly one session file writable, and the plan
    artifact guard pauses the run when a turn changes any other one. Two
    channels carry a finding's *required work* into a child and can name a
    plan artifact anyway: :func:`_merge_verification_checks` copies the
    findings' ``verification_checks`` (argv, cwd and all) verbatim into its
    completion contract, and the findings' ``done_criteria`` become its
    ``Done when`` list. A child launched against one of those cannot win either
    way -- the check either already passes and proves nothing, or it demands
    the very edit the guard exists to refuse, and the child burns its retries
    reporting that it was not allowed to do the work (ASI-134).

    ``evidence[].path`` is deliberately not a channel: a finding routinely
    cites another session's packet as the *reason* the work is owed (its goal,
    its handoff), and :func:`_render_child_body` labels such a citation as a
    read-only reference, so refusing the group over it refused ordinary repair
    work (ASI-162).

    Only references anchored to ``plan_dir`` count. A repository file that
    happens to be named ``session-<id>.md`` is an ordinary work target, and
    nothing but its location distinguishes it from a plan artifact.

    The child's own artifacts are excluded: those are the one session file the
    packet does make writable.
    """
    pattern = _plan_artifact_pattern(plan_dir)
    referenced: dict[str, SessionId] = {}
    for finding in group_findings:
        haystacks = [json.dumps(finding.get("verification_checks", ()))]
        haystacks.extend(str(bullet) for bullet in finding.get("done_criteria", ()))
        for text in haystacks:
            referenced.update(_foreign_plan_artifacts_in(pattern, child_id, text))
    return tuple(sorted(referenced, key=lambda name: (referenced[name].sort_key, name)))


def repair_group_from_session_text(text: str) -> str | None:
    """Return the repair group a materialized child declares, if any.

    Reads the line :func:`_render_child_body` writes, so the parser and the
    renderer of that bullet stay together.
    """
    match = repair_models._REPAIR_GROUP_LINE_RE.search(text)
    return None if match is None else match.group(1)


def _render_write_repository_sources(sources: Mapping[str, str]) -> str:
    return ", ".join(
        f"`{alias}` (from {repair_models._WRITE_REPOSITORY_SOURCE_LABELS[source]})"
        for alias, source in sources.items()
    )


def _dedupe_preserving_order(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique
