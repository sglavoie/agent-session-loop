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
- Every child carries the versioned review-repair marker so later cycles can
  recognize generated repair sessions.
- All child files, the manifest table/count refresh, and any predecessor
  reference repoint are staged in a single
  :class:`~agent_session_loop.authoring.PlanTransaction` and committed
  atomically, so a failed materialization leaves no partially authored plan.
- After authoring, readiness is refreshed to ``draft``. When a repository
  ``cwd`` is supplied, targeted baselines plus ``asl preflight`` run and, if
  clean, mark the plan loop-ready so the new children become executable; a
  blocked gate leaves valid pending children at ``draft`` with structured
  blockers rather than executing an unverified plan.

The canonical review-finding grammar lives in
:mod:`agent_session_loop.review`; contract construction/validation and the
atomic transaction live in :mod:`agent_session_loop.authoring`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import re
from pathlib import Path
from typing import Any, Sequence

from . import authoring
from . import baseline
from . import contracts
from . import plans
from . import preflight
from . import review
from . import state
from . import workspaces
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

#: Maximum number of repair groups (child sessions) produced per cycle.
MAX_REPAIR_GROUPS = 8

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
        repository = (
            payload.get("repo") if repository_registry is not None else None
        )
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
        repository = (
            payload.get("repo") if repository_registry is not None else None
        )
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
        )
        previous_settings = (
            previous.timeout_seconds,
            previous.repo,
            previous.cwd or ".",
            previous.baseline_expectation,
            previous.on_failure_of,
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
) -> list[str]:
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
        "",
        "## Findings to repair",
        "",
    ]
    for finding in findings:
        lines.append(
            f"### {finding['id']} ({finding['severity']}): {finding['title']}"
        )
        lines.append("")
        lines.append(finding["explanation"])
        lines.append("")
        lines.append("Evidence:")
        for evidence in finding["evidence"]:
            symbol = evidence.get("symbol")
            if symbol:
                lines.append(f"- `{evidence['path']}` — `{symbol}`")
            else:
                lines.append(f"- `{evidence['path']}`")
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
            "- Leave every targeted check in the completion contract passing.",
            "- Record results in this session's Handoff when complete.",
        ]
    )
    return lines


def _build_repair_group_plans(
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
        scope: plans.SessionScope | None = None
        marker_version = GENERATED_REVIEW_REPAIR_SESSION_VERSION
        if repository_registry is not None:
            attributed = {finding.get("repo") for finding in group_findings}
            if None in attributed or any(
                not isinstance(alias, str) or not alias for alias in attributed
            ):
                raise ReviewRepairError(
                    f"repair group {group!r} contains a finding without an "
                    "explicit repository alias"
                )
            unknown = attributed.difference(repository_registry.aliases)
            if unknown:
                raise ReviewRepairError(
                    f"repair group {group!r} references unknown repository "
                    f"aliases: {', '.join(sorted(str(alias) for alias in unknown))}"
                )
            read_repositories = repository_registry.aliases
            write_repositories = tuple(
                alias for alias in repository_registry.aliases if alias in attributed
            )
            scope = plans.SessionScope(
                reads=read_repositories,
                writes=write_repositories,
            )
            marker_version = (
                REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION
            )
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
    later = [
        session
        for session in sessions
        if session.session_id.components[0] > top
    ]
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
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("## ")
        ),
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
    ``runner._strip_own_lock_findings``.
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
        if lock_info.pid is not None and lock_info.pid != own_pid:
            return False
        return True

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
            "baseline_summary": baseline_summary,
        }

    report = preflight.run_preflight(
        plan_dir, cwd=canonical_cwd, state_dir=state_dir
    )
    report = _strip_self_lock_findings(report, lock_info)
    mark_blockers = preflight._mark_loop_ready_blockers(report)
    if mark_blockers:
        blockers = [
            f"{finding.category}: {finding.message}"
            for finding in mark_blockers
        ]
        return {
            "loop_ready": False,
            "blockers": blockers,
            "baseline_summary": baseline_summary,
        }
    marked, mark_error = preflight._mark_loop_ready(report)
    if not marked:
        return {
            "loop_ready": False,
            "blockers": [mark_error or "could not mark the plan loop-ready"],
            "baseline_summary": baseline_summary,
        }
    return {
        "loop_ready": True,
        "blockers": [],
        "baseline_summary": baseline_summary,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _artifact_sha256_or_none(
    digest_fn: Any, canonical_dir: Path
) -> str | None:
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
        return _materialize(
            canonical_dir,
            review_session_id=review_session_id,
            cwd=cwd,
            state_dir=state_dir,
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

    try:
        transaction.commit()
    except PlanError as error:
        return _malformed(review_id, f"atomic plan write failed: {error}")

    # Authoring succeeded — refresh readiness to draft (children not yet
    # executable). Mirrors the prepare-pipeline post-authoring contract.
    authoring.refresh_readiness_metadata(canonical_dir, state="draft", now=now)

    loop_ready = False
    blockers: tuple[str, ...] = ()
    baseline_summary: dict[str, Any] | None = None
    reason = REVIEW_REPAIR_REASON_OK
    if cwd is not None:
        gate = _make_children_executable(
            canonical_dir, cwd, children, state_dir, lock_info
        )
        loop_ready = gate["loop_ready"]
        blockers = tuple(gate["blockers"])
        baseline_summary = gate["baseline_summary"]
        if not loop_ready:
            reason = REVIEW_REPAIR_REASON_NOT_LOOP_READY

    return ReviewRepairResult(
        ok=True,
        reason=reason,
        review_session_id=review_id,
        children=tuple(children),
        loop_ready=loop_ready,
        blockers=blockers,
        baseline_summary=baseline_summary,
    )
