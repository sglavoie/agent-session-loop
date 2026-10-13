"""Rendering and writing of ``session-<id>.expand.md`` expansion files."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .authoring_transaction import PlanTransaction
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_session_ids import SessionId, parse_session_id
from .prepare_models import (
    _BEFORE_STARTING_PREFIX,
    EXPANSION_REFERENCE_TEMPLATE,
    LEGACY_PREPARE_VALIDATION_VERSION,
    NO_CONTRACT_ISSUES_LINE,
    PREPARE_VALIDATION_VERSION,
    TO_BE_CREATED_LINE,
    TO_BE_CREATED_LOCATION,
    PrepareValidation,
    SessionValidation,
    VerifiedClaim,
)


def _claim_row(claim: VerifiedClaim) -> str:
    if claim.to_be_created:
        location = claim.location or TO_BE_CREATED_LOCATION
        line = TO_BE_CREATED_LINE
        if claim.repo is not None and location != TO_BE_CREATED_LOCATION:
            location = f"{claim.repo}:{location}"
        return f"| `{claim.claim}` | {claim.claim_type} | {location} | {line} |"
    line = str(claim.line) if claim.line is not None else TO_BE_CREATED_LINE
    location = (
        f"{claim.repo}:{claim.location}" if claim.repo is not None else claim.location
    )
    return f"| `{claim.claim}` | {claim.claim_type} | `{location}` | {line} |"


def _qualified_location(repo: str | None, location: str) -> str:
    return f"{repo}:{location}" if repo is not None else location


def render_expansion(
    session: SessionValidation,
    *,
    codebase_root: str,
    expanded: str | None = None,
    validation_version: int = LEGACY_PREPARE_VALIDATION_VERSION,
) -> str:
    """Render a ``session-<id>.expand.md`` document from validated data.

    The output matches the historical ``session-expand`` schema: header
    metadata, optional ``## Issues`` / ``## Verified Claims`` /
    ``## Concrete Details`` / ``## Patterns to Reuse`` /
    ``## Session Dependencies`` sections (omitted entirely when empty), and a
    ``## Contract Issues`` section whenever the contract was evaluated, using
    the literal ``_no contract issues found_`` line when clean.
    """
    if expanded is None:
        expanded = datetime.now(UTC).strftime("%Y-%m-%d")
    display = session.session_id.display_label
    lines: list[str] = [
        f"# {display} Expansion: {session.title}",
        "",
        f"**Expanded**: {expanded}",
        (
            f"**Workspace root**: `{codebase_root}`"
            if validation_version == PREPARE_VALIDATION_VERSION
            else f"**Codebase root**: `{codebase_root}`"
        ),
        (
            f"**Claims checked**: {session.claims_checked} | "
            f"**Issues found**: {len(session.issues)}"
        ),
    ]
    if session.spark.reported:
        lines.append(
            f"**Spark eligible**: {'yes' if session.spark.eligible else 'no'} — "
            f"{session.spark.reason}"
        )

    if session.issues:
        lines.extend(["", "## Issues"])
        for index, issue in enumerate(session.issues, start=1):
            lines.extend(
                [
                    "",
                    f"### ISSUE-{index}: {issue.summary}",
                    "",
                    f"- **Type**: {issue.issue_type}",
                    *(
                        [f"- **Repository**: {issue.repo}"]
                        if issue.repo is not None
                        else []
                    ),
                    f'- **Session ref**: "{issue.session_ref}"',
                    f"- **Actual**: {issue.actual}",
                    f"- **Fix**: {issue.fix}",
                ]
            )

    if session.claims:
        lines.extend(
            [
                "",
                "## Verified Claims",
                "",
                "| Claim | Type | Location | Line |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(_claim_row(claim) for claim in session.claims)

    if session.details:
        lines.extend(["", "## Concrete Details"])
        for detail in session.details:
            lines.extend(
                [
                    "",
                    f"### File: `{_qualified_location(detail.repo, detail.path)}`",
                    "",
                ]
            )
            lines.extend(f"- {note}" for note in detail.notes)

    if session.patterns:
        lines.extend(["", "## Patterns to Reuse", ""])
        lines.extend(
            f"- **{pattern.name}**: "
            f"{_qualified_location(pattern.repo, pattern.location)} — {pattern.reason}"
            for pattern in session.patterns
        )

    if session.dependencies:
        lines.extend(["", "## Session Dependencies", ""])
        for dependency in session.dependencies:
            direction = (
                "Requires from"
                if dependency.direction == "requires"
                else "Provides for"
            )
            lines.append(
                f"- **{direction} session {dependency.session_id}**"
                f"{f' [{dependency.repo}]' if dependency.repo is not None else ''}: "
                f"{dependency.description}"
            )

    if session.contract_checked:
        lines.extend(["", "## Contract Issues", ""])
        if session.contract_problems:
            lines.extend(
                f"- `{session.session_id.value}: {problem.message}: "
                f"{problem.suggested_fix}`"
                for problem in session.contract_problems
            )
        else:
            lines.append(NO_CONTRACT_ISSUES_LINE)

    return "\n".join(lines) + "\n"


def expansion_reference_line(session_id: int | str | SessionId) -> str:
    filename = parse_session_id(session_id).expansion_filename
    return EXPANSION_REFERENCE_TEMPLATE.format(filename=filename)


def insert_expansion_reference(
    session_text: str, session_id: int | str | SessionId
) -> tuple[str, bool]:
    """Insert the ``> **Expansion notes**`` blockquote after ``> **Before starting**``.

    Idempotent: when the reference line for this session already exists the
    text is returned unchanged. Raises :class:`PlanError` when the session has
    no ``> **Before starting**:`` anchor line to insert after.
    """
    reference = expansion_reference_line(session_id)
    lines = session_text.splitlines()
    if any(line.strip() == reference for line in lines):
        return session_text, False

    for index, line in enumerate(lines):
        if line.startswith(_BEFORE_STARTING_PREFIX):
            updated = lines[: index + 1] + [reference] + lines[index + 1 :]
            suffix = "\n" if session_text.endswith("\n") else ""
            return "\n".join(updated) + suffix, True

    raise PlanError(
        f"session {parse_session_id(session_id).value} has no "
        f"'{_BEFORE_STARTING_PREFIX}' line to anchor the expansion reference"
    )


def write_expansions(
    plan_dir: str | Path,
    validation: PrepareValidation,
    *,
    expanded: str | None = None,
) -> tuple[Path, ...]:
    """Write ``.expand.md`` files and reference lines for every validated session.

    All writes (expansion documents plus updated session files) are staged in
    one :class:`PlanTransaction` and committed atomically, so a failure leaves
    no partially expanded plan. Raises :class:`PlanError` when a validated
    session has no session file on disk.
    """
    canonical_dir = canonicalize_path(plan_dir)
    transaction = PlanTransaction(canonical_dir)

    for session in validation.sessions:
        session_path = canonical_dir / session.session_id.filename
        try:
            session_text = session_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise PlanError(f"missing session file: {session_path}") from error
        except OSError as error:
            raise PlanError(f"cannot read {session_path}: {error}") from error

        transaction.stage(
            session.session_id.expansion_filename,
            render_expansion(
                session,
                codebase_root=validation.codebase_root,
                expanded=expanded,
                validation_version=validation.version,
            ),
        )
        updated_text, changed = insert_expansion_reference(
            session_text, session.session_id
        )
        if changed:
            transaction.stage(session.session_id.filename, updated_text)

    return transaction.commit()
