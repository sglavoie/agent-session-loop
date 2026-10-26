"""Session status and handoff parsing, including generated-review artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .plans_discovery import (
    discover_session_files,
)
from .plans_files import (
    _atomic_write_text,
    _raise_read_error,
    canonicalize_path,
)
from .plans_models import (
    SessionFile,
    SessionHandoff,
    SessionStatus,
    StatusValue,
)
from .plans_registry import (
    read_repository_registry,
    validate_completed_handoff_paths,
)
from .plans_session_scope import (
    parse_session_scope_lines,
)
from .plans_transaction import (
    recover_pending_plan_transaction,
)
from .workspaces import (
    RepositoryRegistry,
)

STATUS_PREFIX = "- **Status**:"


HANDOFF_FILES_PREFIX = "- **Files modified**:"


HANDOFF_DECISIONS_PREFIX = "- **Key decisions**:"


HANDOFF_BLOCKERS_PREFIX = "- **Blockers for next session**:"


HANDOFF_NOTES_PREFIX = "- **Notes**:"


HANDOFF_HEADING = "## Handoff"


DISCOVERED_FACTS_HEADING = "### Discovered Facts"


OPEN_QUESTIONS_HEADING = "### Open Questions"


# The generated final-review session owns these artifacts in the plan directory,
# not in a repository under review.  Keep the marker recognition local to the
# plan parser so a completed review can be inspected before the runner imports
# its higher-level review orchestration.
_GENERATED_REVIEW_SESSION_MARKER = "<!-- asl:generated-review-session -->"


_GENERATED_REVIEW_SESSION_VERSION_MARKER_RE = re.compile(
    r"<!--\s*asl:generated-review-session\s+version=[0-9]+\s*-->"
)


_GENERATED_REVIEW_PLAN_ARTIFACTS = frozenset({"final-review.md", "final-review.json"})


def parse_status_value(raw_value: str) -> StatusValue:
    normalized = raw_value.strip().lower()
    if normalized == "":
        return "empty"
    if normalized == "pending":
        return "pending"
    if normalized == "complete":
        return "complete"
    return "unknown"


def _final_handoff_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return index
    return None


def _is_generated_review_session_text(text: str) -> bool:
    """Return whether ``text`` belongs to a generated final-review session."""
    return (
        _GENERATED_REVIEW_SESSION_MARKER in text
        or _GENERATED_REVIEW_SESSION_VERSION_MARKER_RE.search(text) is not None
    )


@dataclass(frozen=True)
class _ReviewArtifactProjection:
    """A handoff text with its generated review artifacts projected away.

    ``exclusive`` says every ``Files modified`` entry named a review artifact,
    which is the only shape the persisted normalization rewrites on disk.
    """

    text: str
    exclusive: bool


def _is_generated_review_artifact_entry(entry: str) -> bool:
    """Return whether one ``Files modified`` entry names a review artifact."""
    candidate = entry.strip()
    if candidate.startswith("./"):
        candidate = candidate[2:]
    return candidate in _GENERATED_REVIEW_PLAN_ARTIFACTS


def _project_generated_review_handoff_artifacts(
    text: str,
) -> _ReviewArtifactProjection:
    """Project generated review artifacts out of a completed handoff.

    ``final-review.md`` and ``final-review.json`` live beside session files;
    they are evidence produced by a generated review, never files modified in
    one of the repositories under review.  A review harness can nevertheless
    record them in its own handoff.  Drop those entries during inspection so
    registered-plan path validation remains about repository changes, and do it
    per entry: a handoff that also records a real repository change keeps that
    entry, and its ordinary fail-closed validation, without the review having
    to rewrite an otherwise valid handoff.  Every other entry stays untouched.
    """
    if not _is_generated_review_session_text(text):
        return _ReviewArtifactProjection(text, False)

    lines = text.splitlines(keepends=True)
    plain_lines = [line.rstrip("\r\n") for line in lines]
    handoff_start = _final_handoff_start(plain_lines)
    if handoff_start is None:
        return _ReviewArtifactProjection(text, False)

    section_end = len(lines)
    for index in range(handoff_start + 1, len(lines)):
        if plain_lines[index].startswith("## "):
            section_end = index
            break

    status = "empty"
    files_index: int | None = None
    for index in range(handoff_start + 1, section_end):
        line = plain_lines[index]
        if line.startswith(STATUS_PREFIX):
            status = parse_status_value(line[len(STATUS_PREFIX) :])
        elif line.startswith(HANDOFF_FILES_PREFIX):
            files_index = index

    if status != "complete" or files_index is None:
        return _ReviewArtifactProjection(text, False)

    inline = plain_lines[files_index][len(HANDOFF_FILES_PREFIX) :].strip()
    entries = tuple(part.strip() for part in inline.split(",") if part.strip())
    retained = tuple(
        entry for entry in entries if not _is_generated_review_artifact_entry(entry)
    )
    if not entries or len(retained) == len(entries):
        return _ReviewArtifactProjection(text, False)

    line_ending = "\r\n" if lines[files_index].endswith("\r\n") else "\n"
    if not lines[files_index].endswith(("\n", "\r")):
        line_ending = ""
    rendered = ", ".join(retained) if retained else "(none)"
    lines[files_index] = f"{HANDOFF_FILES_PREFIX} {rendered}{line_ending}"
    return _ReviewArtifactProjection("".join(lines), not retained)


def normalize_generated_review_handoff_artifacts(
    plan_dir: str | Path,
) -> tuple[Path, ...]:
    """Persist the bounded generated-review handoff normalization.

    This is an idempotent recovery for a completed generated review that wrote
    only review-owned plan artifacts into ``Files modified``.  It deliberately
    leaves every other handoff entry alone, including unqualified repository
    paths, so multi-repository validation continues to fail closed.  A handoff
    that mixes review artifacts with other entries is projected during
    inspection but never rewritten here, so no handoff is edited on disk to buy
    a validation that reading it already grants.
    """
    recover_pending_plan_transaction(plan_dir)
    canonical_dir = canonicalize_path(plan_dir)
    changed_paths: list[Path] = []
    for session in discover_session_files(canonical_dir):
        try:
            text = session.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            _raise_read_error(session.path, error)
        projection = _project_generated_review_handoff_artifacts(text)
        if not projection.exclusive or projection.text == text:
            continue
        _atomic_write_text(session.path, projection.text)
        changed_paths.append(session.path)
    return tuple(changed_paths)


_REPOSITORY_REGISTRY_UNSET = object()


def read_session_handoff(
    session: SessionFile,
    *,
    repository_registry: RepositoryRegistry
    | None
    | object = _REPOSITORY_REGISTRY_UNSET,
) -> SessionHandoff:
    """Read the final `## Handoff` block in a session file as a structured record.

    Body bullets (`- **Status**:`, `- **Files modified**:`, etc.) are captured
    until the first `### ` or `## ` heading. Recognized subsections
    (`### Discovered Facts`, `### Open Questions`) then accumulate top-level
    `- ` bullets — with no leading whitespace, matching the body parser — into
    their respective tuples. Subsection bullet capture stops at the next
    `### ` or `## ` heading. Subsections may appear in either order; missing
    subsections default to empty tuples; unknown `### ` subsections are
    ignored.

    Missing handoff blocks and blocks without a `- **Status**:` line both
    yield `status="empty"` and `line_number=None`, matching the prior
    behavior of `read_session_status`.
    """
    recover_pending_plan_transaction(session.path.parent)
    try:
        text = session.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        _raise_read_error(session.path, error)
    lines = _project_generated_review_handoff_artifacts(text).text.splitlines()

    registry = (
        read_repository_registry(session.path.parent)
        if repository_registry is _REPOSITORY_REGISTRY_UNSET
        else repository_registry
    )
    if registry is not None and not isinstance(registry, RepositoryRegistry):
        raise TypeError("repository_registry must be a RepositoryRegistry or None")
    scope = (
        parse_session_scope_lines(lines, registry.aliases)
        if registry is not None
        else None
    )

    handoff_start = _final_handoff_start(lines)
    if handoff_start is None:
        return SessionHandoff(
            session=session,
            status="empty",
            raw_value="",
            line_number=None,
        )

    status: StatusValue = "empty"
    raw_value = ""
    status_line: int | None = None
    files_modified: tuple[str, ...] = ()
    key_decisions: str | None = None
    blockers: str | None = None
    notes: str | None = None
    discovered_facts: list[str] = []
    open_questions: list[str] = []
    current_subsection: str | None = None

    for index in range(handoff_start + 1, len(lines)):
        line = lines[index]
        if line.startswith("## "):
            break
        if line.startswith("### "):
            heading = line.rstrip()
            if heading == DISCOVERED_FACTS_HEADING:
                current_subsection = "facts"
            elif heading == OPEN_QUESTIONS_HEADING:
                current_subsection = "questions"
            else:
                current_subsection = "other"
            continue

        if current_subsection is None:
            if line.startswith(STATUS_PREFIX):
                raw_value = line[len(STATUS_PREFIX) :].strip()
                status = parse_status_value(raw_value)
                status_line = index + 1
            elif line.startswith(HANDOFF_FILES_PREFIX):
                inline = line[len(HANDOFF_FILES_PREFIX) :].strip()
                files_modified = (inline,) if inline else ()
            elif line.startswith(HANDOFF_DECISIONS_PREFIX):
                inline = line[len(HANDOFF_DECISIONS_PREFIX) :].strip()
                key_decisions = inline or None
            elif line.startswith(HANDOFF_BLOCKERS_PREFIX):
                inline = line[len(HANDOFF_BLOCKERS_PREFIX) :].strip()
                blockers = inline or None
            elif line.startswith(HANDOFF_NOTES_PREFIX):
                inline = line[len(HANDOFF_NOTES_PREFIX) :].strip()
                notes = inline or None
        elif current_subsection == "facts" and line.startswith("- "):
            content = line[2:].strip()
            if content:
                discovered_facts.append(content)
        elif current_subsection == "questions" and line.startswith("- "):
            content = line[2:].strip()
            if content:
                open_questions.append(content)

    handoff = SessionHandoff(
        session=session,
        status=status,
        raw_value=raw_value,
        line_number=status_line,
        files_modified=files_modified,
        key_decisions=key_decisions,
        blockers=blockers,
        notes=notes,
        discovered_facts=tuple(discovered_facts),
        open_questions=tuple(open_questions),
    )
    if registry is not None and scope is not None and handoff.status == "complete":
        validate_completed_handoff_paths(handoff, scope, registry.aliases)
    return handoff


def read_session_status(
    session: SessionFile,
    *,
    repository_registry: RepositoryRegistry
    | None
    | object = _REPOSITORY_REGISTRY_UNSET,
) -> SessionStatus:
    """Read the status marker from the final handoff block in a session file."""
    handoff = read_session_handoff(
        session,
        repository_registry=repository_registry,
    )
    return SessionStatus(
        session=handoff.session,
        status=handoff.status,
        raw_value=handoff.raw_value,
        line_number=handoff.line_number,
    )
