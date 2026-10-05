"""Materialize validated structured review findings into repair sessions.

Session 16 made review findings machine-readable (``final-review.json``,
schema :data:`agent_session_loop.review.REVIEW_ARTIFACT_VERSION`). This module
turns those validated findings into *serial child repair sessions* appended
beneath the generated review session ``N`` as ``N.1``, ``N.2``, ... inside the
**same** plan directory — no second plan directory is ever created.

Planning is read-only, and materialization is deterministic and atomic:

- :func:`plan_review_repair_sessions` resolves the source review, validates the
  v2 artifacts, records full artifact digests and stable finding fingerprints,
  detects existing-child conflicts, and renders every proposed child in memory.
- Findings are grouped by their ``repair_group`` (every finding belongs to
  exactly one group; grouping preserves first-appearance order). At most
  :data:`MAX_REPAIR_GROUPS` groups are allowed per cycle — larger finding sets
  must be grouped by subsystem upstream, so an over-large set fails closed with
  the stable ``malformed-review-repair`` reason.
- Each group becomes one repair child whose body renders lineage, the finding
  references (evidence + explanation), the child scope, and the merged done
  criteria, and whose ``## Completion Contract`` embeds the findings'
  ``verification_checks`` verbatim (deduped by name and by identical command
  execution).
- The child scope states the same verification ownership the task packet does:
  the contract must end up passing, but ASL runs it authoritatively after the
  turn, so the child does not rerun it as its own final gate. Repeating or
  widening a check is justified by an invalidated result, a failure that needs
  diagnosis, or an uncovered correctness concern — never by habit — and a
  failure the supplied evidence already attributes to another repair group is
  that group's to fix unless the child observes something contradicting it.
- A group whose evidence or verification checks point the child at a session
  artifact in this plan directory other than its own is refused outright: the
  plan artifact guard makes exactly one session file writable per turn, so such
  a child is launched against work it is forbidden to do (ASI-134).
- Every child carries the versioned review-repair marker so later cycles can
  recognize generated repair sessions.
- Before anything is staged, the rendered children are judged against the bound
  repository root *and* against the plan's own verification context: a child
  whose repair targets reach an unregistered repository, or whose verification
  checks name a ``repo``/``cwd`` the contract cannot resolve, is refused while
  the plan is still untouched rather than committed and then rejected by the
  post-authoring gate.
- All child files, the manifest table/count refresh, and any predecessor
  reference repoint are staged in a single
  :class:`~agent_session_loop.authoring.PlanTransaction` and committed
  atomically, so a failed materialization leaves no partially authored plan.
- Every durable write happens inside a publication window
  (:data:`REPAIR_PUBLICATION_DIR_NAME`) whose rollback image is fsynced before
  the first destination byte moves. A publication that cannot finish — because
  the executability gate refused the children, or because the process died —
  is undone rather than left behind, so the plan is always either the one the
  publication started from or a fully loop-ready one with the children in it.
- After authoring, readiness is refreshed to ``draft``. When a repository
  ``cwd`` is supplied, targeted baselines plus ``asl preflight`` run and, if
  clean, mark the plan loop-ready so the new children become executable. A
  blocked gate withdraws the children through the publication window and
  reports its blockers, because a pending child no run may select is worse
  than no child at all (ASI-136).

The canonical review-finding grammar lives in
:mod:`agent_session_loop.review`; contract construction/validation and the
atomic transaction live in :mod:`agent_session_loop.authoring`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    authoring,
    baseline,
    contracts,
    durability,
    plans,
    preflight,
    progress_repair,
    repository_scope,
    review,
    state,
    verification,
    workspaces,
)
from .authoring import ManifestSession, PlanTransaction
from .plans import PlanError, SessionId, canonicalize_path, parse_session_id

# Generated review-repair session markers. Every materialized repair child
# carries the base marker (so a version-agnostic reader recognizes it) plus the
# versioned marker on the following line. These are intentionally distinct from
# the ``asl:generated-review-session`` markers in :mod:`review` so a review
# session and its repair children never shadow one another.
REVIEW_REPAIR_SESSION_MARKER = "<!-- asl:generated-review-repair-session -->"
REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE = (
    "<!-- asl:generated-review-repair-session version={version} -->"
)
_REVIEW_REPAIR_SESSION_MARKER_RE = re.compile(
    r"<!--\s*asl:generated-review-repair-session"
    r"(?:\s+version=([0-9]+))?\s*-->"
)
GENERATED_REVIEW_REPAIR_SESSION_VERSION = 1
REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION = 2

_REPAIR_GROUP_LINE_RE = re.compile(r"(?m)^- Repair group: `([^`]+)`\.$")

#: Maximum number of repair groups (child sessions) produced per cycle.
MAX_REPAIR_GROUPS = 8

# Provenance labels for a repair group's write repositories. A repository can be
# attributed by more than one source; the strongest attribution is reported.
WRITE_REPOSITORY_SOURCE_REPO = "repo"
WRITE_REPOSITORY_SOURCE_EVIDENCE = "evidence"
WRITE_REPOSITORY_SOURCE_CHECK = "check"
_WRITE_REPOSITORY_SOURCE_RANK = {
    WRITE_REPOSITORY_SOURCE_REPO: 0,
    WRITE_REPOSITORY_SOURCE_EVIDENCE: 1,
    WRITE_REPOSITORY_SOURCE_CHECK: 2,
}
_WRITE_REPOSITORY_SOURCE_LABELS = {
    WRITE_REPOSITORY_SOURCE_REPO: "finding `repo`",
    WRITE_REPOSITORY_SOURCE_EVIDENCE: "finding evidence paths",
    WRITE_REPOSITORY_SOURCE_CHECK: "finding verification checks",
}

# Stable ``reason`` vocabulary for materialization results.
REVIEW_REPAIR_REASON_OK = "ok"
REVIEW_REPAIR_REASON_NO_FINDINGS = "no-findings"
REVIEW_REPAIR_REASON_NOT_LOOP_READY = "not-loop-ready"
REVIEW_REPAIR_REASON_MALFORMED = "malformed-review-repair"


class ReviewRepairError(ValueError):
    """Raised internally when safe repair sessions cannot be produced."""


@dataclass(frozen=True)
class RepairChild:
    """One materialized repair child session."""

    session_id: SessionId
    repair_group: str
    title: str
    finding_ids: tuple[str, ...]
    path: Path
    read_repositories: tuple[str, ...] = ()
    write_repositories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewRepairResult:
    """Outcome of a review-repair materialization attempt.

    ``ok`` is ``True`` whenever valid pending child sessions were produced (even
    when the plan could not yet be marked loop-ready). ``ok`` is ``False`` only
    when no safe sessions could be produced; in that case ``reason`` is
    :data:`REVIEW_REPAIR_REASON_MALFORMED` and ``error`` explains why.

    ``children`` is empty with ``reason``
    :data:`REVIEW_REPAIR_REASON_NOT_LOOP_READY` when the pre-authoring gate
    refused the rendered children: the refusal is a gate verdict rather than a
    malformed artifact, and the plan is untouched.

    ``rolled_back`` reports that children *were* authored and then withdrawn
    because the executability gate refused them. The plan is back in the state
    it held before the materialization, so ``children`` is empty here too: a
    child a run may not select is not a child the plan has.
    """

    ok: bool
    reason: str
    review_session_id: SessionId | None = None
    children: tuple[RepairChild, ...] = ()
    loop_ready: bool = False
    error: str | None = None
    detail: str | None = None
    blockers: tuple[str, ...] = ()
    baseline_summary: dict[str, Any] | None = None
    non_bypassable_blockers: tuple[str, ...] = ()
    rolled_back: bool = False
    rollback: dict[str, Any] | None = None


@dataclass(frozen=True)
class RepairGroupPlan:
    """One validated, fully rendered child proposed by repair planning."""

    repair_group: str
    proposed_child_id: SessionId
    title: str
    finding_ids: tuple[str, ...]
    done_when: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]
    contract: contracts.CompletionContract
    rendered_text: str
    read_repositories: tuple[str, ...] = ()
    write_repositories: tuple[str, ...] = ()
    write_repository_sources: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewRepairPlan:
    """Read-only validation and child-specification result.

    Successful findings plans contain normalized structured findings,
    fingerprints, and fully rendered repair groups ready for atomic staging.
    Failed plans retain any artifact provenance and conflict information that
    could be established safely, while using the materializer's existing
    stable reason/error vocabulary.
    """

    ok: bool
    reason: str
    review_session_id: SessionId | None = None
    validated_findings: tuple[dict[str, Any], ...] = ()
    finding_fingerprints: tuple[str, ...] = ()
    repair_groups: tuple[RepairGroupPlan, ...] = ()
    proposed_child_ids: tuple[SessionId, ...] = ()
    existing_child_ids: tuple[SessionId, ...] = ()
    review_output_sha256: str | None = None
    review_artifact_sha256: str | None = None
    repository_bases: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


def review_repair_session_version_marker(
    version: int = GENERATED_REVIEW_REPAIR_SESSION_VERSION,
) -> str:
    """Render the versioned marker line for a generated repair session."""
    return REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(version=version)


def contains_review_repair_session_marker(text: str) -> bool:
    """Return True when a session body carries any review-repair marker."""
    return _REVIEW_REPAIR_SESSION_MARKER_RE.search(text) is not None


def review_repair_session_marker_version(text: str) -> int | None:
    """Return the review-repair version of a session body, if any.

    ``None`` when the body carries no review-repair marker; ``1`` when only the
    base (unversioned) marker is present; otherwise the integer from the
    versioned marker.
    """
    match = _REVIEW_REPAIR_SESSION_MARKER_RE.search(text)
    if match is None:
        return None
    if match.group(1) is not None:
        return int(match.group(1))
    return 1


def is_generated_review_repair_session_file(path: str | Path) -> bool:
    """Return True when ``path`` is a materialized repair session file."""
    try:
        body = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return contains_review_repair_session_marker(body)


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
            or _WRITE_REPOSITORY_SOURCE_RANK[source]
            < _WRITE_REPOSITORY_SOURCE_RANK[current]
        ):
            sources[alias] = source

    for finding in group_findings:
        alias = finding.get("repo")
        if not isinstance(alias, str) or not alias:
            raise ReviewRepairError(
                f"repair group {group!r} contains a finding without an "
                "explicit repository alias"
            )
        attribute(alias, WRITE_REPOSITORY_SOURCE_REPO)
        for evidence in finding["evidence"]:
            path = evidence["path"]
            try:
                qualified = plans.parse_qualified_path(path)
            except (PlanError, ValueError) as error:
                raise ReviewRepairError(
                    f"repair group {group!r} has evidence path {path!r} that is "
                    f"not a qualified Alias:path reference: {error}"
                ) from error
            attribute(qualified.alias, WRITE_REPOSITORY_SOURCE_EVIDENCE)
        for check in finding["verification_checks"]:
            check_repository = check.get("repo")
            if not isinstance(check_repository, str) or not check_repository:
                raise ReviewRepairError(
                    f"repair group {group!r} has a verification check without an "
                    "explicit repository alias"
                )
            attribute(check_repository, WRITE_REPOSITORY_SOURCE_CHECK)

    if unknown:
        raise ReviewRepairError(
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
        + rf"/session-({plans.SESSION_ID_PATTERN})\.(?:expand\.)?md"
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
    match = _REPAIR_GROUP_LINE_RE.search(text)
    return None if match is None else match.group(1)


def _render_write_repository_sources(sources: Mapping[str, str]) -> str:
    return ", ".join(
        f"`{alias}` (from {_WRITE_REPOSITORY_SOURCE_LABELS[source]})"
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
                    raise ReviewRepairError(
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
            raise ReviewRepairError(
                f"repair group {group!r} has conflicting settings for "
                f"duplicate verification commands {previous.name!r} and "
                f"{parsed.name!r}"
            )
        aliases[parsed.name] = previous.name

    for payload in merged:
        dependencies = payload.get("on_failure_of")
        if not isinstance(dependencies, list):
            continue
        payload["on_failure_of"] = _dedupe_preserving_order(
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
    marker_version: int = GENERATED_REVIEW_REPAIR_SESSION_VERSION,
    write_repository_sources: Mapping[str, str] | None = None,
    plan_dir: Path | None = None,
) -> list[str]:
    # Evidence that cites another session's packet explains why the work is
    # owed; it is never a write target, and the child is told so (ASI-162).
    artifact_pattern = None if plan_dir is None else _plan_artifact_pattern(plan_dir)
    lines: list[str] = [
        REVIEW_REPAIR_SESSION_MARKER,
        review_repair_session_version_marker(marker_version),
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
            f"{_render_write_repository_sources(write_repository_sources)}."
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
            if artifact_pattern is not None and _foreign_plan_artifacts_in(
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


def _build_repair_group_plans(
    plan_dir: Path,
    review_id: SessionId,
    grouped: Sequence[tuple[str, tuple[dict[str, Any], ...]]],
    *,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> list[RepairGroupPlan]:
    """Validate and fully render every proposed child without writing files."""
    group_plans: list[RepairGroupPlan] = []
    previous_id = review_id
    for index, (group, group_findings) in enumerate(grouped, start=1):
        child_id = parse_session_id(f"{review_id.value}.{index}")
        foreign_artifacts = foreign_plan_artifact_references(
            plan_dir, child_id, group_findings
        )
        if foreign_artifacts:
            raise ReviewRepairError(
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
            _dedupe_preserving_order(
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
        scope: plans.SessionScope | None = None
        marker_version = GENERATED_REVIEW_REPAIR_SESSION_VERSION
        if repository_registry is not None:
            write_repository_sources = repair_group_write_repository_sources(
                group,
                group_findings,
                repository_registry=repository_registry,
            )
            read_repositories = repository_registry.aliases
            write_repositories = tuple(write_repository_sources)
            scope = plans.SessionScope(
                reads=read_repositories,
                writes=write_repositories,
            )
            marker_version = REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION
        checks = tuple(
            _merge_verification_checks(
                group,
                group_findings,
                repository_registry=repository_registry,
            )
        )
        body_lines = _render_child_body(
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
            contract = authoring.contract_from_payload(
                child_id,
                done_when,
                {"session": child_id.value, "checks": list(checks)},
                repository_registry=repository_registry,
            )
            rendered_text = authoring.render_session_file(
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
            raise ReviewRepairError(
                f"could not author repair child {child_id.value}: {error}"
            ) from error
        group_plans.append(
            RepairGroupPlan(
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
) -> plans.SessionFile | None:
    """Return the latest generated review session in ``plan_dir``, if any."""
    canonical_dir = canonicalize_path(plan_dir)
    candidates: list[plans.SessionFile] = []
    for session in plans.discover_session_files(canonical_dir):
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
    sessions: Sequence[plans.SessionFile], review_id: SessionId
) -> list[plans.SessionFile]:
    top = review_id.components[0]
    return [
        session
        for session in sessions
        if len(session.session_id.components) > 1
        and session.session_id.components[0] == top
    ]


def _immediate_successor(
    sessions: Sequence[plans.SessionFile], review_id: SessionId
) -> plans.SessionFile | None:
    top = review_id.components[0]
    later = [session for session in sessions if session.session_id.components[0] > top]
    if not later:
        return None
    return min(later, key=lambda session: session.session_id.sort_key)


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
        raise ReviewRepairError(
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
        raise ReviewRepairError(
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
                raise ReviewRepairError(
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
            raise ReviewRepairError(
                f"repository-aware review base is missing alias {repository.alias!r}"
            )
        if row["path"] != repository.path or row["origin"] != repository.origin:
            raise ReviewRepairError(
                f"repository-aware review base identity changed for alias "
                f"{repository.alias!r}"
            )
        prefix = f"{repository.alias}:"
        if any(not path.startswith(prefix) for path in row["dirty_paths"]):
            raise ReviewRepairError(
                f"repository-aware review base has unqualified dirty paths for "
                f"alias {repository.alias!r}"
            )
        ordered.append(row)
    unknown = set(rows).difference(repository_registry.aliases)
    if unknown:
        raise ReviewRepairError(
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
    report: preflight.PreflightReport,
    lock_info: state.LockInfo | None,
) -> preflight.PreflightReport:
    """Drop the env-lock finding that points at this process's own lock.

    Materialization runs the preflight gate while holding the plan lock, so the
    lock we hold would otherwise register as an ``env-lock`` blocker. Mirrors
    ``readiness_gate._strip_own_lock_findings``.
    """
    if lock_info is None:
        return report
    own_pid = os.getpid()
    own_lock_path = str(lock_info.path)

    def is_self_lock(finding: preflight.PreflightFinding) -> bool:
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
    return preflight.PreflightReport(
        plan_dir=report.plan_dir,
        findings=filtered,
        snapshot=report.snapshot,
        readiness=report.readiness,
        expected_readiness_sha256=report.expected_readiness_sha256,
        contracts_by_session=report.contracts_by_session,
    )


def _child_footprint_targets(
    group_plan: RepairGroupPlan,
) -> tuple[repository_scope.PathTarget, ...]:
    """Collect the path evidence a rendered child carries, contract included."""
    return (
        *repository_scope.targets_from_contract(group_plan.contract),
        *repository_scope.extract_path_targets(group_plan.rendered_text),
    )


def _validate_children_footprint(
    plan_dir: Path,
    cwd: str | Path,
    group_plans: Sequence[RepairGroupPlan],
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
    if plans.read_repository_registry(plan_dir) is not None:
        return clean
    provenance_roots = preflight.plan_provenance_exempt_roots(plan_dir)
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
    group_plans: Sequence[RepairGroupPlan],
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
        context = verification.VerificationContext.from_plan(plan_dir, cwd)
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
    group_plans: Sequence[RepairGroupPlan],
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
    children: Sequence[RepairChild],
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
    mark_blockers = preflight._mark_loop_ready_blockers(report)
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
            mark_blockers = preflight._mark_loop_ready_blockers(report)
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
                    if finding.category in preflight.NON_BYPASSABLE_PREFLIGHT_CATEGORIES
                }
            ),
            "baseline_summary": baseline_summary,
        }
    marked, mark_error = preflight._mark_loop_ready(report)
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


# ---------------------------------------------------------------------------
# Durable publication window (crash-safe rollback)
# ---------------------------------------------------------------------------

#: Marker directory holding the pre-publication image of every file a repair
#: materialization is about to change. Its presence means a publication was
#: interrupted; recovery restores the image and removes it.
REPAIR_PUBLICATION_DIR_NAME = ".asl-review-repair-publication"
REPAIR_PUBLICATION_JOURNAL_NAME = "journal.json"
REPAIR_PUBLICATION_SCHEMA_VERSION = 1

#: Ordered publication phases. They are evidence, not control flow: recovery
#: rolls back from every one of them, because the only state a crashed
#: publication can be trusted to reproduce is the one it started from.
PUBLICATION_PHASE_AUTHORING = "authoring"
PUBLICATION_PHASE_VALIDATING = "validating"
PUBLICATION_PHASE_PUBLISHING = "publishing"


@dataclass(frozen=True)
class _PublicationWindow:
    """The open rollback image for one in-flight repair publication."""

    directory: Path
    plan_dir: Path
    journal: dict[str, Any]

    @property
    def journal_path(self) -> Path:
        return self.directory / REPAIR_PUBLICATION_JOURNAL_NAME


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_publication_journal(directory: Path, journal: Mapping[str, Any]) -> None:
    """Publish the journal itself atomically, payloads already durable."""
    durability.replace_text(
        directory / REPAIR_PUBLICATION_JOURNAL_NAME,
        json.dumps(dict(journal), sort_keys=True, separators=(",", ":")) + "\n",
        sync_file=True,
        sync_parent=True,
    )


def _open_publication_window(
    canonical_dir: Path,
    *,
    review_id: SessionId,
    child_filenames: Sequence[str],
    restore_paths: Sequence[Path],
    now: datetime | None = None,
) -> _PublicationWindow:
    """Record the pre-publication image of every file about to change.

    The image is durable before the first destination byte moves, so a crash at
    any later boundary is recoverable to the state the plan was in when the
    publication began.
    """
    directory = canonical_dir / REPAIR_PUBLICATION_DIR_NAME
    shutil.rmtree(directory, ignore_errors=True)
    try:
        directory.mkdir(parents=True)
        entries: list[dict[str, Any]] = []
        ordered = sorted({path for path in restore_paths}, key=str)
        for index, path in enumerate(ordered):
            relative = path.relative_to(canonical_dir).as_posix()
            original = _read_bytes_or_none(path)
            if original is None:
                entries.append({"path": relative, "payload": None})
                continue
            payload_name = f"{index:06d}.bin"
            with (directory / payload_name).open("wb") as handle:
                handle.write(original)
                handle.flush()
                durability.fullsync(handle.fileno())
            entries.append(
                {
                    "path": relative,
                    "payload": payload_name,
                    "sha256": hashlib.sha256(original).hexdigest(),
                }
            )
        journal = {
            "version": REPAIR_PUBLICATION_SCHEMA_VERSION,
            "review_session_id": review_id.value,
            "children": list(child_filenames),
            "phase": PUBLICATION_PHASE_AUTHORING,
            "opened_at": (now or datetime.now(UTC)).isoformat(),
            "pid": os.getpid(),
            "entries": entries,
        }
        _write_publication_journal(directory, journal)
    except OSError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise PlanError(
            f"failed to record the repair publication rollback image: {error}"
        ) from error
    plans._fsync_directory(canonical_dir)
    return _PublicationWindow(
        directory=directory, plan_dir=canonical_dir, journal=journal
    )


def _advance_publication_phase(window: _PublicationWindow, phase: str) -> None:
    """Record how far a publication got, for post-crash forensics."""
    window.journal["phase"] = phase
    # The phase is forensic evidence; losing it never changes what recovery does.
    with contextlib.suppress(OSError):
        _write_publication_journal(window.directory, window.journal)


def _close_publication_window(window: _PublicationWindow) -> None:
    """Retire a completed publication: the new state is now the valid one."""
    shutil.rmtree(window.directory, ignore_errors=True)
    with contextlib.suppress(OSError):
        plans._fsync_directory(window.plan_dir)


def _restore_publication_image(
    canonical_dir: Path, journal: Mapping[str, Any]
) -> dict[str, Any]:
    """Put every recorded file back exactly as the publication found it."""
    directory = canonical_dir / REPAIR_PUBLICATION_DIR_NAME
    entries = journal.get("entries")
    entries = entries if isinstance(entries, Sequence) else ()
    restored: list[str] = []
    removed: list[str] = []
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        relative = entry.get("path")
        if not isinstance(relative, str):
            continue
        destination = canonical_dir / relative
        payload = entry.get("payload")
        try:
            if payload is None:
                if destination.exists():
                    destination.unlink()
                    removed.append(relative)
                continue
            content = (directory / str(payload)).read_bytes()
            destination.parent.mkdir(parents=True, exist_ok=True)
            durability.replace_bytes(destination, content, sync_file=True)
            restored.append(relative)
        except OSError as error:
            errors.append(f"{relative}: {error}")
    with contextlib.suppress(OSError):
        plans._fsync_directory(canonical_dir)
    return {
        "restored": restored,
        "removed": removed,
        "errors": errors,
        "phase": journal.get("phase"),
        "review_session_id": journal.get("review_session_id"),
        "children": list(journal.get("children") or ()),
    }


def _rollback_publication_window(window: _PublicationWindow) -> dict[str, Any]:
    """Undo an in-flight publication and retire its window."""
    record = _restore_publication_image(window.plan_dir, window.journal)
    if not record["errors"]:
        shutil.rmtree(window.directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            plans._fsync_directory(window.plan_dir)
    return record


def recover_review_repair_publication(plan_dir: str | Path) -> dict[str, Any] | None:
    """Roll an interrupted repair publication back to its starting state.

    A publication is only valid once its children are loop-ready, and nothing
    on disk can prove that a crashed one would have got there. So recovery is
    always a rollback: the plan returns to the state it held before the
    materialization, which is a state a run can execute. The findings that
    prompted the repair are still in the review artifact, so the next cycle
    re-plans and re-materializes rather than inheriting a half-published child.

    Returns ``None`` when there is nothing to recover. Callers must hold the
    plan lock: this writes plan files.
    """
    canonical_dir = canonicalize_path(plan_dir)
    directory = canonical_dir / REPAIR_PUBLICATION_DIR_NAME
    journal_path = directory / REPAIR_PUBLICATION_JOURNAL_NAME
    if not directory.is_dir():
        return None
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        journal = None
    if not isinstance(journal, Mapping):
        # The journal is published atomically *before* the first destination
        # byte moves, so a window without one is a window that never opened:
        # there is nothing to restore. It is still removed, because leaving it
        # would make every later publication believe a recovery is pending.
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            plans._fsync_directory(canonical_dir)
        return {
            "recovered": True,
            "reason": "unopened-publication-window",
            "restored": [],
            "removed": [],
            "errors": [],
        }
    if journal.get("version") != REPAIR_PUBLICATION_SCHEMA_VERSION:
        # A window written by a different schema may name files this code would
        # restore wrongly. Leave it in place and report it: an operator (or a
        # matching ASL version) resolves it, and every caller keeps failing
        # closed until then.
        return {
            "recovered": False,
            "reason": "unsupported-publication-schema",
            "version": journal.get("version"),
            "restored": [],
            "removed": [],
            "errors": [],
        }
    # A repair publication straddles the plan transaction, so the transaction's
    # own roll-forward runs first: restoring an image over a half-installed
    # generation would leave the transaction to re-install it afterwards.
    plans.recover_pending_plan_transaction(canonical_dir)
    record = _restore_publication_image(canonical_dir, journal)
    if not record["errors"]:
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            plans._fsync_directory(canonical_dir)
    return {"recovered": not record["errors"], "reason": "rolled-back", **record}


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
) -> ReviewRepairPlan:
    return ReviewRepairPlan(
        ok=False,
        reason=REVIEW_REPAIR_REASON_MALFORMED,
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
) -> ReviewRepairPlan:
    """Validate and specify review repair children without mutating state.

    This is the single validation/specification path used by materialization
    and read-only continuation consumers. It validates the existing v2 review
    contract, resolves the source review, detects child conflicts, enforces the
    group cap, merges group contracts, and renders every proposed child wholly
    in memory. It never acquires a lock or writes plan/state files.
    """
    canonical_dir = canonicalize_path(plan_dir)
    repository_registry = plans.read_repository_registry(canonical_dir)
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
        repository_bases = _read_repository_review_bases(
            canonical_dir / review_id.filename,
            repository_registry,
        )
    except ReviewRepairError as error:
        return _invalid_plan(
            review_id,
            str(error),
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
        )

    findings = tuple(verdict.get("findings", ()))
    fingerprints = review.finding_fingerprints(list(findings)) or ()
    if not findings:
        return ReviewRepairPlan(
            ok=True,
            reason=REVIEW_REPAIR_REASON_NO_FINDINGS,
            review_session_id=review_id,
            review_output_sha256=output_sha256,
            review_artifact_sha256=artifact_sha256,
            repository_bases=repository_bases,
        )

    sessions = plans.discover_session_files(canonical_dir)
    grouped = group_findings_by_repair_group(findings)
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

    if len(grouped) > MAX_REPAIR_GROUPS:
        return _invalid_plan(
            review_id,
            f"{len(grouped)} repair groups exceed the maximum of "
            f"{MAX_REPAIR_GROUPS}; group findings by subsystem before "
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
    except ReviewRepairError as error:
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

    return ReviewRepairPlan(
        ok=True,
        reason=REVIEW_REPAIR_REASON_OK,
        review_session_id=review_id,
        validated_findings=findings,
        finding_fingerprints=tuple(fingerprints),
        repair_groups=repair_groups,
        proposed_child_ids=proposed_child_ids,
        review_output_sha256=output_sha256,
        review_artifact_sha256=artifact_sha256,
        repository_bases=repository_bases,
    )


def _malformed(
    review_id: SessionId | None, error: str, *, detail: str | None = None
) -> ReviewRepairResult:
    return ReviewRepairResult(
        ok=False,
        reason=REVIEW_REPAIR_REASON_MALFORMED,
        review_session_id=review_id,
        error=error,
        detail=detail,
    )


def materialize_review_repair_sessions(
    plan_dir: str | Path,
    *,
    review_session_id: int | str | SessionId | None = None,
    cwd: str | Path | None = None,
    state_dir: str | Path | None = None,
    hold_lock: bool = True,
    allow_not_loop_ready: bool = False,
    now: datetime | None = None,
) -> ReviewRepairResult:
    """Materialize validated review findings into serial repair children.

    Reads the structured review verdict from ``plan_dir`` (fail-closed via
    :func:`agent_session_loop.review.load_review_verdict`), groups its findings
    by ``repair_group``, and appends one pending repair child session per group
    beneath the generated review session as ``N.1``, ``N.2``, ... The write is
    atomic: on any failure the plan is left exactly as it was.

    ``review_session_id`` selects the review session explicitly; when omitted
    the latest generated review session is used. ``cwd`` (a repository root)
    enables the targeted-baseline + preflight gate that marks the plan
    loop-ready. ``hold_lock`` acquires the plan lock around the mutation; pass
    ``False`` when the caller already holds it (e.g. the runner).

    Children that reach disk but fail the loop-ready gate are withdrawn: the
    publication rolls back to the plan it started from, so a selected repair
    child is always loop-ready. ``allow_not_loop_ready=True`` is the operator
    opt-out (the runner's ``--allow-not-loop-ready``): it retains children
    blocked only by bypassable findings. Non-bypassable blockers always
    withdraw, and an interrupted publication always rolls back.
    """
    canonical_dir = canonicalize_path(plan_dir)

    lock_acquired = False
    lock_info: state.LockInfo | None = None
    if hold_lock:
        try:
            lock_info = state.acquire_lock(canonical_dir, state_dir)
            lock_acquired = True
        except state.StateError as error:
            return _malformed(None, f"could not acquire plan lock: {error}")
    else:
        lock_info = state.read_lock_info(canonical_dir, state_dir)
    try:
        # An earlier publication that died mid-flight owns this plan's files
        # until it is rolled back. Recover before planning so the plan being
        # read is the one a run could execute, and refuse outright when the
        # rollback did not land: publishing over an unrecovered window would
        # destroy the only image of the state it was meant to restore.
        recovery = recover_review_repair_publication(canonical_dir)
        if recovery is not None and not recovery.get("recovered"):
            return _malformed(
                None,
                "an interrupted review repair publication could not be rolled "
                f"back: {recovery.get('reason')}",
                detail=json.dumps(recovery, sort_keys=True),
            )
        return _materialize(
            canonical_dir,
            review_session_id=review_session_id,
            cwd=cwd,
            state_dir=state_dir,
            allow_not_loop_ready=allow_not_loop_ready,
            now=now,
            lock_info=lock_info,
        )
    finally:
        if lock_acquired:
            state.release_lock(canonical_dir, state_dir)


def _materialize(
    canonical_dir: Path,
    *,
    review_session_id: int | str | SessionId | None,
    cwd: str | Path | None,
    state_dir: str | Path | None,
    allow_not_loop_ready: bool = False,
    now: datetime | None,
    lock_info: state.LockInfo | None,
) -> ReviewRepairResult:
    repair_plan = plan_review_repair_sessions(
        canonical_dir, review_session_id=review_session_id
    )
    if not repair_plan.ok:
        return _malformed(
            repair_plan.review_session_id,
            repair_plan.error or "review repair planning failed",
            detail=repair_plan.detail,
        )
    review_id = repair_plan.review_session_id
    if review_id is None:
        return _malformed(None, "review repair planning did not resolve a session")
    if repair_plan.reason == REVIEW_REPAIR_REASON_NO_FINDINGS:
        return ReviewRepairResult(
            ok=True,
            reason=REVIEW_REPAIR_REASON_NO_FINDINGS,
            review_session_id=review_id,
        )

    if cwd is not None:
        pre_authoring_gate = _validate_children_before_authoring(
            canonical_dir, cwd, repair_plan.repair_groups
        )
        if not pre_authoring_gate["loop_ready"]:
            return ReviewRepairResult(
                ok=True,
                reason=REVIEW_REPAIR_REASON_NOT_LOOP_READY,
                review_session_id=review_id,
                error=(
                    "review repair children were refused before authoring: "
                    "they carry repair targets or verification checks the "
                    "plan's repository contract cannot satisfy"
                ),
                blockers=tuple(pre_authoring_gate["blockers"]),
                non_bypassable_blockers=tuple(
                    pre_authoring_gate["non_bypassable_blockers"]
                ),
            )

    sessions = plans.discover_session_files(canonical_dir)

    # Stage the fully validated child texts produced by the read-only planner.
    transaction = PlanTransaction(canonical_dir)
    children: list[RepairChild] = []
    manifest_rows: list[ManifestSession] = []
    for group_plan in repair_plan.repair_groups:
        child_id = group_plan.proposed_child_id
        transaction.stage(child_id.filename, group_plan.rendered_text)
        children.append(
            RepairChild(
                session_id=child_id,
                repair_group=group_plan.repair_group,
                title=group_plan.title,
                finding_ids=group_plan.finding_ids,
                path=canonical_dir / child_id.filename,
                read_repositories=group_plan.read_repositories,
                write_repositories=group_plan.write_repositories,
            )
        )
        manifest_rows.append(
            ManifestSession(
                session_id=child_id,
                title=group_plan.title,
                status="pending",
            )
        )

    # Refresh the manifest table + session count (derived bookkeeping).
    manifest_path = canonical_dir / "manifest.md"
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        manifest_text = None
    if manifest_text is not None:
        new_manifest = manifest_text
        try:
            for row in manifest_rows:
                new_manifest = authoring.add_manifest_session_row(new_manifest, row)
        except PlanError:
            # No session-status table to update; leave the table untouched but
            # still refresh the derived session count when present.
            new_manifest = manifest_text
        new_manifest = authoring.replace_manifest_sessions_count(
            new_manifest, len(sessions) + len(children)
        )
        if new_manifest != manifest_text:
            transaction.stage("manifest.md", new_manifest)

    # Repoint the successor's predecessor reference at the last child, if any.
    successor = _immediate_successor(sessions, review_id)
    if successor is not None:
        try:
            successor_text = successor.path.read_text(encoding="utf-8")
        except OSError:
            successor_text = None
        if successor_text is not None:
            updated, changed = authoring.replace_previous_session_reference(
                successor_text, review_id, children[-1].session_id
            )
            if changed:
                transaction.stage(successor.session_id.filename, updated)

    # Everything from here down changes durable plan state, so the publication
    # opens its rollback image first. Between this line and the close below the
    # plan is mid-publication: a crash leaves the image on disk and the next
    # locked caller restores it, so no run can ever select a child whose
    # loop-ready state was never established.
    window = _open_publication_window(
        canonical_dir,
        review_id=review_id,
        child_filenames=[child.session_id.filename for child in children],
        restore_paths=[
            *transaction.staged_paths,
            # readiness metadata lives in the manifest, and the post-authoring
            # refresh rewrites it whether or not the manifest was staged.
            canonical_dir / "manifest.md",
            # the executability gate runs the children's targeted baselines,
            # which persist their outcomes here.
            canonical_dir / baseline.BASELINE_RESULTS_FILENAME,
        ],
        now=now,
    )

    try:
        transaction.commit()
    except PlanError as error:
        _rollback_publication_window(window)
        return _malformed(review_id, f"atomic plan write failed: {error}")

    try:
        # Authoring succeeded — refresh readiness to draft (children not yet
        # executable). Mirrors the prepare-pipeline post-authoring contract.
        authoring.refresh_readiness_metadata(canonical_dir, state="draft", now=now)

        loop_ready = False
        blockers: tuple[str, ...] = ()
        non_bypassable_blockers: tuple[str, ...] = ()
        baseline_summary: dict[str, Any] | None = None
        reason = REVIEW_REPAIR_REASON_OK
        if cwd is not None:
            _advance_publication_phase(window, PUBLICATION_PHASE_VALIDATING)
            gate = _make_children_executable(
                canonical_dir, cwd, children, state_dir, lock_info
            )
            loop_ready = gate["loop_ready"]
            blockers = tuple(gate["blockers"])
            non_bypassable_blockers = tuple(gate["non_bypassable_blockers"])
            baseline_summary = gate["baseline_summary"]
            retained = allow_not_loop_ready and not non_bypassable_blockers
            if not loop_ready:
                reason = REVIEW_REPAIR_REASON_NOT_LOOP_READY
            if not loop_ready and not retained:
                # ASI-136: children that reached disk but cannot be executed
                # used to stay there, leaving a pending non-loop-ready repair
                # child no run could clear. Withdraw them instead: the blockers
                # still describe exactly what was refused, and the plan is the
                # valid one the publication started from.
                rollback = _rollback_publication_window(window)
                return ReviewRepairResult(
                    ok=True,
                    reason=REVIEW_REPAIR_REASON_NOT_LOOP_READY,
                    review_session_id=review_id,
                    children=(),
                    loop_ready=False,
                    error=(
                        "review repair children were withdrawn after "
                        "authoring: the plan could not be marked loop-ready "
                        "with them in it"
                    ),
                    blockers=blockers,
                    baseline_summary=baseline_summary,
                    non_bypassable_blockers=non_bypassable_blockers,
                    rolled_back=not rollback["errors"],
                    rollback=rollback,
                )
    except BaseException:
        # Includes KeyboardInterrupt and SystemExit: an interrupted publication
        # is exactly the case the rollback image exists for.
        _rollback_publication_window(window)
        raise

    _advance_publication_phase(window, PUBLICATION_PHASE_PUBLISHING)
    _close_publication_window(window)
    return ReviewRepairResult(
        ok=True,
        reason=reason,
        review_session_id=review_id,
        children=tuple(children),
        loop_ready=loop_ready,
        blockers=blockers,
        baseline_summary=baseline_summary,
        non_bypassable_blockers=non_bypassable_blockers,
    )
