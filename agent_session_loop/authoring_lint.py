"""Plan linting and plan footprint reporting."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import (
    repository_intent,
    repository_scope,
)
from .authoring_parse import (
    _iter_structural_lines,
    _parse_repository_registry,
    _parse_source_session_heading,
    _source_heading_error,
    parse_plan_document,
)
from .authoring_render import _contract_file_targets
from .authoring_source_models import (
    _ATX_CLOSING_MARKER_RE,
    _NON_SESSION_PLAN_HEADING_RE,
    _SESSION_HEADING_PREFIX_RE,
    LINT_DUPLICATE_SESSION_ID,
    LINT_MISSING_REPOSITORY_REGISTRY,
    LINT_NON_NUMERIC_SESSION_ID,
    LINT_SUGGESTABLE_CATEGORIES,
    LINT_UNSUPPORTED_TITLE_SEPARATOR,
    PlanLintFinding,
    PlanLintResult,
    SourceHeadingError,
    SourceRegistryError,
    SourceScopeError,
)
from .plans_errors import PlanError


def _suggest_heading_repair(error: SourceHeadingError) -> str | None:
    """Rebuild a canonical heading when exactly one rewrite is correct."""
    line = error.source_line
    without_closing_marker = _ATX_CLOSING_MARKER_RE.sub("", line)
    closing_marker = line[len(without_closing_marker) :]
    prefix = _SESSION_HEADING_PREFIX_RE.match(without_closing_marker)
    if prefix is None:
        return None

    head = without_closing_marker[: prefix.start("rest")]
    after_token = prefix.group("rest")
    payload = after_token.lstrip(" \t")
    gap = after_token[: len(after_token) - len(payload)]

    # ``## Session plan`` is ordinary prose whose first word happens to be the
    # reserved session marker. It cannot describe a valid session (IDs are
    # numeric), and rewriting it to a stable overview heading leaves its body
    # untouched. Keep this deliberately narrow: other non-numeric values may
    # be an intended session whose ID was omitted.
    if (
        error.category == LINT_NON_NUMERIC_SESSION_ID
        and _NON_SESSION_PLAN_HEADING_RE.fullmatch(payload) is not None
    ):
        return f"{head[: -len('Session')]}Plan overview{closing_marker}"

    if error.category not in LINT_SUGGESTABLE_CATEGORIES:
        return None

    numeric_prefix = re.match(r"[0-9.]+", payload)
    if numeric_prefix is None:
        return None

    raw_id = numeric_prefix.group(0)
    title = payload[numeric_prefix.end() :].lstrip(" \t")
    if error.category == LINT_UNSUPPORTED_TITLE_SEPARATOR:
        title = title[1:]
    title = title.strip(" \t")
    if not title:
        return None

    suggestion = f"{head}{gap}{raw_id}: {title}{closing_marker}"
    try:
        _parse_source_session_heading(suggestion, error.line_number)
    except PlanError:
        return None
    return suggestion


def lint_plan_document(
    text: str,
    *,
    cwd: str | Path | None = None,
    source_path: str | Path | None = None,
) -> PlanLintResult:
    """Collect every malformed source heading instead of stopping at the first.

    :func:`parse_plan_document` fails fast, so repairing a plan through it costs
    one round trip per broken heading. This walks the same fence-aware grammar
    and the same per-heading parser, but accumulates diagnostics so a caller can
    repair a whole plan in one pass.

    With ``cwd``, a registry-free plan is additionally judged against that root:
    a plan that reaches into a second repository is reported as
    :data:`LINT_MISSING_REPOSITORY_REGISTRY` rather than being split into a
    legacy plan nothing can guard. ``source_path`` names the document itself,
    which that judgement must not count as somewhere the plan reaches.
    """
    findings: list[PlanLintFinding] = []
    session_ids: list[str] = []
    first_id_lines: dict[str, int] = {}

    lines = text.splitlines()
    structural_lines = tuple(_iter_structural_lines(lines))
    try:
        registry, _ = _parse_repository_registry(lines, structural_lines)
    except SourceRegistryError as error:
        findings.append(
            PlanLintFinding(
                line=error.line_number,
                category=error.category,
                message=str(error),
                source_line=error.source_line,
                suggested_line=None,
            )
        )
        registry = None

    for line_number, line, is_candidate, _ in structural_lines:
        if not is_candidate:
            continue

        try:
            heading = _parse_source_session_heading(line, line_number)
        except SourceHeadingError as error:
            findings.append(
                PlanLintFinding(
                    line=line_number,
                    category=error.category,
                    message=str(error),
                    source_line=line,
                    suggested_line=_suggest_heading_repair(error),
                )
            )
            continue
        if heading is None:
            continue

        session_id = heading[0]
        first_line = first_id_lines.get(session_id.value)
        if first_line is not None:
            duplicate = _source_heading_error(
                line_number,
                f"duplicate canonical session id {session_id.value!r}; "
                f"first defined on line {first_line}",
                line,
                category=LINT_DUPLICATE_SESSION_ID,
            )
            findings.append(
                PlanLintFinding(
                    line=line_number,
                    category=duplicate.category,
                    message=str(duplicate),
                    source_line=line,
                    suggested_line=None,
                )
            )
            continue

        first_id_lines[session_id.value] = line_number
        session_ids.append(session_id.value)

    if not findings and registry is not None:
        try:
            parse_plan_document(text)
        except SourceScopeError as error:
            findings.append(
                PlanLintFinding(
                    line=error.line_number,
                    category=error.category,
                    message=str(error),
                    source_line=error.source_line,
                    suggested_line=None,
                )
            )

    footprint_payload: dict[str, Any] | None = None
    if not findings and registry is None and cwd is not None:
        footprint_payload = plan_footprint_payload(text, cwd, source_path=source_path)
        if footprint_payload is not None:
            heading_line = min(first_id_lines.values(), default=1)
            findings.append(
                PlanLintFinding(
                    line=heading_line,
                    category=LINT_MISSING_REPOSITORY_REGISTRY,
                    message=(
                        "plan reaches outside a single repository but declares "
                        "no `## Repositories` table; paste "
                        "`repository_footprint.suggested_registry_table` above "
                        "the first session heading and add "
                        "`**Reads repositories**:` / `**Writes repositories**:` "
                        "lines from `repository_footprint.suggested_scopes`"
                    ),
                    source_line=lines[heading_line - 1] if lines else "",
                    suggested_line=None,
                )
            )

    return PlanLintResult(
        findings=tuple(sorted(findings, key=lambda finding: finding.line)),
        session_ids=tuple(session_ids),
        registry=registry,
        repository_footprint=footprint_payload,
    )


def plan_footprint_payload(
    text: str,
    cwd: str | Path,
    *,
    contracts_by_id: Any = None,
    source_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Judge a source plan against ``cwd``, returning ``None`` when it fits.

    ``contracts_by_id`` is the raw ``--contracts-file`` object: its check
    ``path``/``cwd`` values are the strongest evidence of what a session will
    touch, and they never appear in the plan prose ``--check`` sees.

    ``source_path`` is the document being split. A plan that cites its own
    location is not a plan that reaches a second repository, and at this point
    no plan directory exists yet to exempt it as an artifact.
    """
    try:
        plan = parse_plan_document(text)
    except PlanError:
        return None
    targets_by_session: dict[str, tuple[repository_scope.PathTarget, ...]] = {}
    for session in plan.sessions:
        session_id = session.session_id.value
        targets = list(repository_scope.extract_path_targets("\n".join(session.lines)))
        targets.extend(_contract_file_targets(contracts_by_id, session_id))
        targets_by_session[session_id] = tuple(targets)
    footprint = repository_scope.resolve_footprint(
        [target for targets in targets_by_session.values() for target in targets],
        cwd,
        provenance_roots=repository_intent.provenance_exempt_roots(source_path),
    )
    return repository_scope.footprint_payload(
        targets_by_session, footprint, registry_present=plan.registry is not None
    )


def _plan_declares_registry(text: str) -> bool:
    """Whether a source plan carries a parseable ``## Repositories`` table."""
    try:
        return parse_plan_document(text).registry is not None
    except PlanError:
        return False
