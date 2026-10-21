"""Deterministic rendering of context, session files, and completion contracts."""

from __future__ import annotations

import json
import os
from typing import Any

from . import contracts as _contracts
from . import (
    plans_handoff,
    plans_progress,
    plans_session_scope,
    repository_scope_targets,
)
from .authoring_source_models import _fence_delimiter, _trim_body
from .plans_errors import PlanError
from .plans_session_ids import SessionId, parse_session_id
from .workspaces import (
    RepositoryRegistry,
    WorkspaceError,
)

CONTEXT_REFERENCE_LINE = (
    "> **Before starting**: Read `context.md` in this directory for shared "
    "project context."
)
PREVIOUS_SESSION_REFERENCE_TEMPLATE = (
    "> **Previous session**: Read the **Handoff** section at the bottom of "
    "`{filename}`."
)
HANDOFF_TEMPLATE = (
    "## Handoff\n"
    "\n"
    "<!-- Fill this section when the session is complete -->\n"
    "\n"
    "- **Status**: pending\n"
    "- **Files modified**: (none yet)\n"
    "- **Key decisions**: (none yet)\n"
    "- **Blockers for next session**: (none)\n"
    "- **Notes**:\n"
    "\n"
    "<!--\n"
    "Optional: record verified facts and open uncertainties as flat, top-level\n"
    f"bullets under `{plans_handoff.DISCOVERED_FACTS_HEADING}` and "
    f"`{plans_handoff.OPEN_QUESTIONS_HEADING}` headings added below this comment. The\n"
    "handoff parser reads only top-level `- ` bullets directly under those exact\n"
    "headings; indented or nested bullets are ignored. Keep each bullet on one\n"
    "line and never put a comma inside a file path (the Files modified list is\n"
    "comma-separated).\n"
    "-->\n"
)
EMPTY_CONTEXT_TEMPLATE = (
    "# Context\n"
    "\n"
    "_No shared context section found in the original plan._\n"
    "\n"
    "**Original plan**: `{source_path}`\n"
)


#: Contract check keys that hold a path, paired with the target kind each one
#: becomes. Spelled out so the kind stays the ``TargetKind`` literal
#: :class:`repository_scope_targets.PathTarget` declares rather than a bare ``str``.
_CONTRACT_PATH_FIELDS: tuple[tuple[str, repository_scope_targets.TargetKind], ...] = (
    ("path", "contract-path"),
    ("cwd", "contract-cwd"),
)


def _contract_file_targets(
    contracts_by_id: Any, session_id: str
) -> list[repository_scope_targets.PathTarget]:
    if not isinstance(contracts_by_id, dict):
        return []
    contract = contracts_by_id.get(session_id)
    if not isinstance(contract, dict):
        return []
    verify = contract.get("verify")
    checks = (
        verify.get("checks") if isinstance(verify, dict) else contract.get("checks")
    )
    if not isinstance(checks, list):
        return []
    targets: list[repository_scope_targets.PathTarget] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        for field_name, kind in _CONTRACT_PATH_FIELDS:
            value = check.get(field_name)
            if isinstance(value, str) and value:
                targets.append(
                    repository_scope_targets.PathTarget(
                        value=value, kind=kind, line=0, confidence="hard"
                    )
                )
    return targets


def normalize_heading_levels(lines: list[str], heading_level: int) -> list[str]:
    """Promote body headings so a standalone session file reads naturally.

    When the original session heading was ``###`` (or deeper), every heading in
    the body is shifted so the session's own level maps to ``#`` (the rendered
    title). A ``###`` session therefore turns a ``####`` sub-heading into ``##``.
    Level-2 (``## Session``) bodies are preserved verbatim. Headings inside
    fenced code blocks are never rewritten.
    """
    if heading_level <= 2:
        return list(lines)

    shift = 1 - heading_level  # e.g. level 3 -> -2
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        delimiter = _fence_delimiter(line)
        if delimiter is not None:
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            out.append(line)
            continue
        if fence is None:
            level = _heading_level(line)
            if level > 0:
                new_level = max(1, level + shift)
                stripped = line.lstrip()
                leading = line[: len(line) - len(stripped)]
                out.append(f"{leading}{'#' * new_level}{stripped[level:]}")
                continue
        out.append(line)
    return out


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level == 0 or level == len(stripped) or stripped[level] != " ":
        return 0
    return level


# ---------------------------------------------------------------------------
# Completion-contract rendering
# ---------------------------------------------------------------------------


def _check_to_json(
    check: _contracts.VerificationCheck,
    *,
    repository_registry: RepositoryRegistry | None,
    session_id: SessionId,
    index: int,
) -> dict[str, object]:
    payload: dict[str, object] = {"name": check.name, "type": check.kind}
    repository_alias = check.repo
    if repository_registry is None:
        if repository_alias is not None:
            raise PlanError(
                f"session {session_id.value} contract check #{index + 1} "
                "cannot render 'repo' without a repository registry"
            )
    else:
        if repository_alias is None:
            if len(repository_registry) == 1:
                repository_alias = repository_registry.aliases[0]
            else:
                raise PlanError(
                    f"session {session_id.value} contract check #{index + 1} "
                    "cannot render without 'repo' when the repository registry "
                    "has multiple entries"
                )
        try:
            repository_registry.get(repository_alias)
        except WorkspaceError as error:
            raise PlanError(
                f"session {session_id.value} contract check #{index + 1} "
                f"cannot render invalid 'repo': {error}"
            ) from error
        payload["repo"] = repository_alias
    if isinstance(check, _contracts.CommandCheck):
        payload["argv"] = list(check.argv)
        payload["timeout_seconds"] = _render_number(check.timeout_seconds)
        if check.cwd is not None:
            payload["cwd"] = check.cwd
    elif isinstance(check, _contracts.FileExistsCheck):
        payload["path"] = check.path
    elif isinstance(check, _contracts.RegexInFileCheck):
        payload["path"] = check.path
        payload["pattern"] = check.pattern
    if check.baseline_expectation != _contracts.DEFAULT_BASELINE_EXPECTATION:
        payload["baseline_expectation"] = check.baseline_expectation
    if check.on_failure_of:
        payload["on_failure_of"] = list(check.on_failure_of)
    if check.review_replay:
        payload["review_replay"] = True
    return payload


def _render_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def contract_from_payload(
    session_id: int | str | SessionId,
    done_when: tuple[str, ...] | list[str],
    verify_payload: dict[str, Any],
    *,
    repository_registry: RepositoryRegistry | None = None,
) -> _contracts.CompletionContract:
    """Build a validated :class:`CompletionContract` from JSON-shaped inputs.

    ``verify_payload`` uses the same shape as a contract's fenced ``Verify``
    JSON (``{"session": ..., "checks": [...]}``; the ``session`` key is
    optional and must match when present). Raises :class:`PlanError` on any
    malformed input, mirroring the markdown contract parser.
    """
    canonical_id = parse_session_id(session_id)
    if not isinstance(done_when, (list, tuple)):
        raise PlanError(
            f"session {canonical_id.value} contract 'done_when' must be a JSON "
            f"array, got {type(done_when).__name__}"
        )
    bullets = tuple(str(item).strip() for item in done_when if str(item).strip())
    if not bullets:
        raise PlanError(
            f"session {canonical_id.value} contract requires at least one "
            "'Done when' bullet"
        )
    recipe = _contracts._parse_verify_payload(
        verify_payload,
        session_id=canonical_id,
        repository_registry=repository_registry,
    )
    return _contracts.CompletionContract(
        session_id=canonical_id, done_when=bullets, verify=recipe
    )


def render_completion_contract(
    contract: _contracts.CompletionContract,
    *,
    repository_registry: RepositoryRegistry | None = None,
) -> str:
    """Render a ``## Completion Contract`` block that round-trips through the parser."""
    lines: list[str] = [
        _contracts.COMPLETION_CONTRACT_HEADING,
        "",
        _contracts.DONE_WHEN_HEADING,
        "",
    ]
    if contract.done_when:
        lines.extend(f"- {item}" for item in contract.done_when)
    else:
        raise PlanError("completion contract requires at least one 'Done when' bullet")
    lines.extend(["", _contracts.VERIFY_HEADING, "", "```json"])

    payload: dict[str, Any] = {
        "session": contract.session_id.value,
        "checks": [
            _check_to_json(
                check,
                repository_registry=repository_registry,
                session_id=contract.session_id,
                index=index,
            )
            for index, check in enumerate(contract.verify.checks)
        ],
    }
    if contract.verify.parallel:
        payload["parallel"] = True
    lines.extend(json.dumps(payload, indent=2).splitlines())
    lines.append("```")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# File rendering
# ---------------------------------------------------------------------------


def previous_session_reference_line(previous_session_id: int | str | SessionId) -> str:
    """Render the ``> **Previous session**`` handoff-reference line."""
    filename = parse_session_id(previous_session_id).filename
    return PREVIOUS_SESSION_REFERENCE_TEMPLATE.format(filename=filename)


def render_context(
    context_lines: tuple[str, ...] | list[str],
    source_path: str | os.PathLike[str],
) -> str:
    """Render ``context.md`` from the plan's shared-context lines."""
    trimmed = _trim_leading_trailing_blanks(list(context_lines))
    if not trimmed:
        return EMPTY_CONTEXT_TEMPLATE.format(source_path=os.fspath(source_path))
    return "\n".join(trimmed) + "\n"


def _trim_leading_trailing_blanks(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and lines[start].strip() == "":
        start += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    return lines[start:end]


def render_session_file(
    *,
    session_id: int | str | SessionId,
    title: str | None,
    instruction_lines: tuple[str, ...] | list[str],
    heading_level: int = 2,
    is_first: bool = False,
    previous_session_id: int | str | SessionId | None = None,
    contract: _contracts.CompletionContract | None = None,
    require_contract: bool = True,
    scope: plans_session_scope.SessionScope | None = None,
    repository_registry: RepositoryRegistry | None = None,
) -> str:
    """Render a full ``session-<id>.md`` file.

    Original instruction content is preserved verbatim except for standalone
    heading-level normalization (see :func:`normalize_heading_levels`) and,
    when ``contract`` is given, removal of any inline ``## Completion Contract``
    section it supersedes, so the rendered file carries exactly one contract.
    When ``require_contract`` is set (deterministic preparation mode), a
    parseable ``contract`` is mandatory and its absence fails closed.
    """
    canonical_id = parse_session_id(session_id)
    if require_contract and contract is None:
        raise PlanError(
            f"session {canonical_id.value} requires an explicit completion contract"
        )
    if contract is not None and contract.session_id != canonical_id:
        raise PlanError(
            "contract session id "
            f"{contract.session_id.value!r} does not match session "
            f"{canonical_id.value!r}"
        )

    title_text = (title or "").strip()
    heading = (
        f"# {canonical_id.display_label}: {title_text}"
        if title_text
        else f"# {canonical_id.display_label}"
    )

    parts: list[str] = [heading, "", CONTEXT_REFERENCE_LINE]
    if not is_first and previous_session_id is not None:
        parts.append(previous_session_reference_line(previous_session_id))
    if scope is not None:
        parts.append("")
        parts.extend(
            plans_session_scope.render_session_scope(scope).rstrip("\n").splitlines()
        )
    parts.append("")
    parts.append("---")
    parts.append("")

    body = normalize_heading_levels(_trim_body(list(instruction_lines)), heading_level)
    if contract is not None:
        body, replaced = _contracts.strip_completion_contract_sections(body)
        if replaced:
            body = _trim_body(body)
    parts.extend(body)
    parts.append("")
    parts.append("---")
    parts.append("")

    if contract is not None:
        parts.append(
            render_completion_contract(
                contract, repository_registry=repository_registry
            ).rstrip("\n")
        )
        parts.append("")
        parts.append("---")
        parts.append("")

    parts.append(HANDOFF_TEMPLATE.rstrip("\n"))
    return "\n".join(parts) + "\n"


def render_progress_skeleton() -> str:
    """Return the header-only ``progress.md`` skeleton for a fresh plan."""
    return plans_progress.PROGRESS_HEADER
