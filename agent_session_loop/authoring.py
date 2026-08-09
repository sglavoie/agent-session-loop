"""Central plan authoring and transaction API.

This module centralizes the deterministic construction and mutation of split
plan directories. Previously the rendering rules lived only in the
``session-split`` / ``session-resplit`` skill markdown (executed by an LLM) and
the collision-safe path helpers lived only in standalone scripts. This module
turns all of that into importable, unit-tested Python:

- **Inline-source capture and output resolution** — the canonical deterministic
  implementation behind the ``asl split`` subcommand (formerly the standalone
  ``capture-inline-plan.py`` / ``resolve-plan-output.py`` scripts, now retired).
- **Fenced-code-aware session parsing** — split a plan document into shared
  context plus per-session blocks, honoring ```` ``` ````/``~~~`` fences so a
  ``## Session`` line inside a code block is never mistaken for structure.
- **Rendering** — deterministic ``context.md``, ``session-<id>.md``,
  ``manifest.md``, and ``progress.md`` output. Original session content is
  preserved verbatim except for standalone heading-level normalization.
- **Deterministic preparation mode** — session rendering may require an explicit
  parseable ``## Completion Contract`` and fails closed otherwise.
- **Atomic multi-file transactions** — stage many files and commit them so a
  failure part-way through leaves no partially authored plan.
- **Manifest table / previous-session / archive / readiness helpers** — the
  small structural mutations shared by split and resplit.

Canonical session identities come from :mod:`agent_session_loop.plans`
(``SessionId``); contract parsing/validation comes from
:mod:`agent_session_loop.contracts`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any

from . import contracts as _contracts
from . import durability, harnesses, plans, responses
from .plans import (
    PlanError,
    ReadinessMetadata,
    SessionId,
    canonicalize_path,
    parse_session_id,
    stable_readiness_sha256,
    write_readiness_metadata,
)
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
    canonicalize_declared_origin,
)


DEFAULT_AGENT_PLANS_DIR_NAME = "agent-plans"
HASH_PREFIX_LENGTH = 12

#: Matches the bounded source-plan session-heading grammar after any optional
#: ATX closing marker has been removed. Source IDs are intentionally broader
#: than :data:`plans.SESSION_ID_PATTERN`; they are canonicalized privately
#: before a strict :class:`SessionId` is constructed.
SESSION_HEADING_RE = re.compile(
    r"^ {0,3}(?P<hashes>#{2,6})[ \t]+Session[ \t]+"
    r"(?P<session_id>[0-9]+(?:\.[0-9]+)*)"
    r"(?:[ \t]*(?P<separator>[:–—-])[ \t]*(?P<title>.*?))?"
    r"[ \t]*$",
    re.IGNORECASE,
)
#: Detects H2--H6 ATX lines whose content begins with the ``Session`` token.
#: A detected line that does not match ``SESSION_HEADING_RE`` is malformed,
#: rather than ordinary plan content.
_SESSION_HEADING_CANDIDATE_RE = re.compile(
    r"^ {0,3}#{2,6}[ \t]+Session\b",
    re.IGNORECASE,
)
_SESSION_HEADING_PREFIX_RE = re.compile(
    r"^ {0,3}#{2,6}[ \t]+Session(?P<rest>.*)$",
    re.IGNORECASE,
)
_SOURCE_SESSION_ID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_ATX_CLOSING_MARKER_RE = re.compile(r"[ \t]+#+[ \t]*$")
_TITLE_SEPARATORS = frozenset((":", "-", "–", "—"))
#: Matches an opening/closing fenced-code delimiter (``` or ~~~, 3+ chars).
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

#: Stable machine-readable categories for malformed source-heading diagnostics.
#: They are the repair vocabulary shared by ``asl split --check`` and the
#: ``session-split`` skill, so they are part of the public JSON contract.
LINT_INVALID_SESSION_ID = "invalid-session-id"
LINT_ZERO_ID_COMPONENT = "zero-id-component"
LINT_MALFORMED_HEADING = "malformed-heading"
LINT_MISSING_ID_WHITESPACE = "missing-id-whitespace"
LINT_MISSING_SESSION_ID = "missing-session-id"
LINT_NON_NUMERIC_SESSION_ID = "non-numeric-session-id"
LINT_SESSION_ID_SUFFIX = "session-id-suffix"
LINT_DANGLING_TITLE_SEPARATOR = "dangling-title-separator"
LINT_MALFORMED_TITLE = "malformed-title"
LINT_PUNCTUATION_FREE_TITLE = "punctuation-free-title"
LINT_UNSUPPORTED_TITLE_SEPARATOR = "unsupported-title-separator"
LINT_DUPLICATE_SESSION_ID = "duplicate-session-id"
LINT_REPOSITORIES_PLACEMENT = "repositories-placement"
LINT_DUPLICATE_REPOSITORIES = "duplicate-repositories"
LINT_MALFORMED_REPOSITORIES = "malformed-repositories"
LINT_INVALID_REPOSITORY = "invalid-repository"
LINT_INVALID_SESSION_SCOPE = "invalid-session-scope"

_REPOSITORIES_HEADING_CANDIDATE_RE = re.compile(
    r"^ {0,3}#{1,6}[ \t]+Repositories(?:[ \t]+#+)?[ \t]*$",
    re.IGNORECASE,
)
_REPOSITORIES_HEADING = "## Repositories"
_REPOSITORY_HEADERS = ("Alias", "Path", "Origin")

#: Categories whose repair has exactly one correct rewrite, so the finding
#: carries a ``suggested_line`` an agent may apply without asking. Every other
#: category either invents an id or renumbers sessions, which changes what the
#: plan means, so it is deliberately left for a human decision.
LINT_SUGGESTABLE_CATEGORIES = frozenset(
    (
        LINT_PUNCTUATION_FREE_TITLE,
        LINT_UNSUPPORTED_TITLE_SEPARATOR,
    )
)


class SourceHeadingError(PlanError):
    """A line-specific malformed source-heading diagnostic.

    Subclasses :class:`PlanError` so existing handlers and message assertions
    are unaffected; the extra attributes let ``asl split --check`` report a
    stable ``category`` instead of forcing callers to match on prose.
    """

    def __init__(
        self, *, line_number: int, category: str, source_line: str, reason: str
    ) -> None:
        self.line_number = line_number
        self.category = category
        self.source_line = source_line
        self.reason = reason
        super().__init__(
            f"line {line_number}: {reason}; source line: {source_line!r}"
        )


class SourceRegistryError(SourceHeadingError):
    """A line-specific malformed source repository-registry diagnostic."""


class SourceScopeError(SourceHeadingError):
    """A line-specific malformed multi-repository session-scope diagnostic."""


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
    f"bullets under `{plans.DISCOVERED_FACTS_HEADING}` and "
    f"`{plans.OPEN_QUESTIONS_HEADING}` headings added below this comment. The\n"
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
ARCHIVE_ROOT_NAME = "resplit-archive"


# ---------------------------------------------------------------------------
# Inline-source capture and output resolution (importable APIs)
# ---------------------------------------------------------------------------


def default_base_dir() -> str:
    """Return the persistent agent-plans base directory.

    Resolution order mirrors the standalone scripts: ``AGENT_PLANS_DIR`` wins,
    then ``$XDG_DATA_HOME/agent-plans``, then ``~/.local/share/agent-plans``.
    ``$TMPDIR`` is intentionally never consulted (macOS purges it on reboot).
    """
    agent_plans_dir = os.environ.get("AGENT_PLANS_DIR")
    if agent_plans_dir:
        return os.path.realpath(os.path.expanduser(agent_plans_dir))

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = os.path.join(
            os.path.expanduser(xdg_data_home), DEFAULT_AGENT_PLANS_DIR_NAME
        )
    else:
        base = os.path.join(
            os.path.expanduser("~/.local/share"), DEFAULT_AGENT_PLANS_DIR_NAME
        )
    return os.path.realpath(base)


def _resolve_base_dir(base_dir: str | os.PathLike[str] | None) -> str:
    if base_dir is None:
        return default_base_dir()
    return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(base_dir))))


def slugify(value: str) -> str:
    """Collapse a string to a filesystem-safe ``[a-z0-9-]`` slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "plan"


def slug_from_source_path(source_path: str | os.PathLike[str]) -> str:
    """Build a slug from a plan filename stem."""
    filename = os.path.basename(os.fspath(source_path))
    stem, _ext = os.path.splitext(filename)
    return slugify(stem or filename)


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_file(source_path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(source_path, "rb") as plan_file:
        for chunk in iter(lambda: plan_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(source_path: str | os.PathLike[str], content_hash: str) -> str:
    """Fingerprint a plan by canonical source path plus content hash."""
    canonical_path = os.path.realpath(os.path.abspath(os.fspath(source_path)))
    fingerprint_input = canonical_path.encode("utf-8", "surrogateescape")
    fingerprint_input += b"\0"
    fingerprint_input += content_hash.encode("ascii")
    return hashlib.sha256(fingerprint_input).hexdigest()


def extract_inline_plan(raw_input: str) -> str:
    """Return the last ``<proposed_plan>`` block, else the raw input.

    Raises :class:`ValueError` when no non-empty plan content is present.
    """
    matches = re.findall(
        r"<proposed_plan>\s*(.*?)\s*</proposed_plan>",
        raw_input,
        flags=re.IGNORECASE | re.DOTALL,
    )
    content = (matches[-1] if matches else raw_input).strip()
    if not content:
        raise ValueError("no plan content found on stdin")
    return content + "\n"


def title_from_plan(content: str) -> str:
    """Return the first ``# H1`` title in a plan, or ``"plan"``."""
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return "plan"


def _write_source_file(
    content: str, sources_dir: str, slug: str, content_hash: str
) -> tuple[str, bool]:
    os.makedirs(sources_dir, exist_ok=True)

    hash_prefix = content_hash[:HASH_PREFIX_LENGTH]
    collision_index = 1
    while True:
        suffix = "" if collision_index == 1 else f"-{collision_index}"
        path = os.path.join(sources_dir, f"{slug}-{hash_prefix}{suffix}.md")
        try:
            with open(path, "x", encoding="utf-8") as source_file:
                source_file.write(content)
            return path, True
        except FileExistsError:
            with open(path, encoding="utf-8") as source_file:
                if source_file.read() == content:
                    return path, False
            collision_index += 1


def capture_inline_plan(
    raw_input: str, base_dir: str | os.PathLike[str] | None = None
) -> dict[str, object]:
    """Persist pasted plan markdown as a durable source file.

    Canonical implementation behind ``asl split --inline-stdin`` (formerly the
    retired ``capture-inline-plan.py`` script); returns JSON-serializable keys.
    """
    selected_base_dir = _resolve_base_dir(base_dir)
    sources_dir = os.path.join(selected_base_dir, "sources")
    content = extract_inline_plan(raw_input)
    title = title_from_plan(content)
    slug = slugify(title)
    content_hash = sha256_text(content)
    source_path, created = _write_source_file(content, sources_dir, slug, content_hash)

    return {
        "base_dir": selected_base_dir,
        "sources_dir": sources_dir,
        "source_path": os.path.realpath(source_path),
        "title": title,
        "slug": slug,
        "content_sha256": content_hash,
        "hash_prefix": content_hash[:HASH_PREFIX_LENGTH],
        "created": created,
    }


def _create_output_dir(base_dir: str, plan_id: str) -> tuple[str, int]:
    os.makedirs(base_dir, exist_ok=True)

    collision_index = 1
    while True:
        suffix = "" if collision_index == 1 else f"-{collision_index}"
        candidate = os.path.join(base_dir, f"{plan_id}{suffix}")
        try:
            os.mkdir(candidate)
        except FileExistsError:
            collision_index += 1
            continue
        return candidate, collision_index


def resolve_plan_output(
    plan_file: str | os.PathLike[str],
    base_dir: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Create a fresh collision-safe output directory for a split plan.

    Canonical implementation behind ``asl split`` output resolution (formerly
    the retired ``resolve-plan-output.py`` script); returns JSON-serializable keys.
    """
    canonical_source = os.path.realpath(
        os.path.abspath(os.path.expanduser(os.fspath(plan_file)))
    )
    selected_base_dir = _resolve_base_dir(base_dir)
    slug = slug_from_source_path(canonical_source)
    content_hash = sha256_file(canonical_source)
    fingerprint = source_fingerprint(canonical_source, content_hash)
    content_hash_prefix = content_hash[:HASH_PREFIX_LENGTH]
    fingerprint_prefix = fingerprint[:HASH_PREFIX_LENGTH]
    plan_id = f"{content_hash_prefix}-{fingerprint_prefix}"
    output_dir, collision_index = _create_output_dir(selected_base_dir, plan_id)

    return {
        "output_dir": output_dir,
        "base_dir": selected_base_dir,
        "slug": slug,
        "source_path": canonical_source,
        "content_sha256": content_hash,
        "source_fingerprint": fingerprint,
        "content_hash_prefix": content_hash_prefix,
        "hash_prefix": fingerprint_prefix,
        "fingerprint_prefix": fingerprint_prefix,
        "plan_id": plan_id,
        "collision_index": collision_index,
    }


# ---------------------------------------------------------------------------
# Fenced-code-aware session parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedSession:
    """One session block parsed from a plan document.

    ``lines`` includes every source line from the session heading (exclusive of
    the heading itself) up to the next session heading or end of file, verbatim.
    ``heading_level`` is 2 through 6 for accepted ``Session`` headings.
    """

    session_id: SessionId
    title: str | None
    heading_level: int
    lines: tuple[str, ...]
    scope: plans.SessionScope | None = None

    @property
    def instruction_lines(self) -> tuple[str, ...]:
        """Body lines with leading/trailing blank lines and ``---`` rules trimmed."""
        lines = (
            plans.strip_session_scope_lines(self.lines)
            if self.scope is not None
            else self.lines
        )
        return tuple(_trim_body(list(lines)))


@dataclass(frozen=True)
class ParsedPlan:
    """A plan document parsed into shared context plus ordered sessions."""

    context_lines: tuple[str, ...]
    sessions: tuple[ParsedSession, ...]
    registry: RepositoryRegistry | None = None

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(session.session_id.value for session in self.sessions)


@dataclass(frozen=True)
class PlanLintFinding:
    """One malformed source heading reported by :func:`lint_plan_document`.

    ``suggested_line`` is non-``None`` exactly when the damage has a single
    correct rewrite, so a caller may apply it without asking; a ``None``
    suggestion means the repair changes meaning and needs a human decision.
    """

    line: int
    category: str
    message: str
    source_line: str
    suggested_line: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "category": self.category,
            "message": self.message,
            "source_line": self.source_line,
            "suggested_line": self.suggested_line,
        }


@dataclass(frozen=True)
class PlanLintResult:
    """Every heading diagnostic in a plan plus the ids that did parse."""

    findings: tuple[PlanLintFinding, ...]
    session_ids: tuple[str, ...]
    registry: RepositoryRegistry | None = None


def _fence_delimiter(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    if match is None:
        return None
    return match.group(1)[0] * 3  # normalize to the 3-char family marker


def _source_heading_error(
    line_number: int, reason: str, line: str, *, category: str
) -> SourceHeadingError:
    """Build a line-specific malformed source-heading error."""
    return SourceHeadingError(
        line_number=line_number, category=category, source_line=line, reason=reason
    )


def _canonicalize_source_session_id(
    raw_id: str, *, line_number: int, line: str
) -> SessionId:
    """Canonicalize a source-only, optionally padded dotted session ID."""
    if _SOURCE_SESSION_ID_RE.fullmatch(raw_id) is None:
        raise _source_heading_error(
            line_number,
            f"invalid session id {raw_id!r}; expected dotted decimal components",
            line,
            category=LINT_INVALID_SESSION_ID,
        )

    components = tuple(int(component) for component in raw_id.split("."))
    if any(component == 0 for component in components):
        raise _source_heading_error(
            line_number,
            f"zero-valued session id component in {raw_id!r}; components must be positive",
            line,
            category=LINT_ZERO_ID_COMPONENT,
        )
    return SessionId(components)


def _raise_malformed_session_heading(line: str, line_number: int) -> None:
    """Raise the most specific diagnostic for a detected malformed candidate."""
    without_closing_marker = _ATX_CLOSING_MARKER_RE.sub("", line)
    prefix = _SESSION_HEADING_PREFIX_RE.match(without_closing_marker)
    if prefix is None:  # Defensive: callers first use the candidate detector.
        raise _source_heading_error(
            line_number,
            "malformed session heading",
            line,
            category=LINT_MALFORMED_HEADING,
        )

    after_token = prefix.group("rest")
    if not after_token or after_token[0] not in " \t":
        raise _source_heading_error(
            line_number,
            "invalid session id; expected whitespace and a positive dotted decimal id",
            line,
            category=LINT_MISSING_ID_WHITESPACE,
        )

    payload = after_token.lstrip(" \t")
    if not payload:
        raise _source_heading_error(
            line_number,
            "invalid session id; session id is missing",
            line,
            category=LINT_MISSING_SESSION_ID,
        )

    numeric_prefix = re.match(r"[0-9.]+", payload)
    if numeric_prefix is None:
        raw_id = re.split(r"[ \t:–—-]", payload, maxsplit=1)[0]
        raise _source_heading_error(
            line_number,
            f"invalid session id {raw_id!r}; expected dotted decimal components",
            line,
            category=LINT_NON_NUMERIC_SESSION_ID,
        )

    raw_id = numeric_prefix.group(0)
    suffix = payload[numeric_prefix.end() :]
    if suffix and suffix[0] not in " \t" and suffix[0].isalnum():
        malformed_token = re.split(r"[ \t:–—-]", payload, maxsplit=1)[0]
        raise _source_heading_error(
            line_number,
            f"invalid session id {malformed_token!r}; expected dotted decimal components",
            line,
            category=LINT_SESSION_ID_SUFFIX,
        )

    _canonicalize_source_session_id(
        raw_id, line_number=line_number, line=line
    )
    suffix = suffix.lstrip(" \t")
    if not suffix:
        # A valid bare ID would have matched. This path is only reachable for
        # defensive completeness if the valid matcher and diagnostic parser
        # diverge.
        raise _source_heading_error(
            line_number,
            "malformed session heading after session id",
            line,
            category=LINT_MALFORMED_HEADING,
        )

    separator = suffix[0]
    if separator in _TITLE_SEPARATORS:
        if not suffix[1:].strip(" \t"):
            raise _source_heading_error(
                line_number,
                f"dangling title separator {separator!r}; a non-empty title is required",
                line,
                category=LINT_DANGLING_TITLE_SEPARATOR,
            )
        raise _source_heading_error(
            line_number,
            "malformed session title",
            line,
            category=LINT_MALFORMED_TITLE,
        )

    if separator.isalnum():
        raise _source_heading_error(
            line_number,
            "punctuation-free title suffix; use ':', '-', '–', or '—' before the title",
            line,
            category=LINT_PUNCTUATION_FREE_TITLE,
        )
    raise _source_heading_error(
        line_number,
        f"unsupported title separator {separator!r}; use ':', '-', '–', or '—'",
        line,
        category=LINT_UNSUPPORTED_TITLE_SEPARATOR,
    )


def _parse_source_session_heading(
    line: str, line_number: int
) -> tuple[SessionId, str | None, int] | None:
    """Return canonical ID, title, and level for a source session heading."""
    if _SESSION_HEADING_CANDIDATE_RE.match(line) is None:
        return None

    without_closing_marker = _ATX_CLOSING_MARKER_RE.sub("", line)
    heading = SESSION_HEADING_RE.fullmatch(without_closing_marker)
    if heading is None:
        _raise_malformed_session_heading(line, line_number)
        raise AssertionError("malformed session heading did not raise")

    raw_id = heading.group("session_id")
    session_id = _canonicalize_source_session_id(
        raw_id, line_number=line_number, line=line
    )
    separator = heading.group("separator")
    raw_title = heading.group("title")
    if separator is not None and not (raw_title or "").strip(" \t"):
        raise _source_heading_error(
            line_number,
            f"dangling title separator {separator!r}; a non-empty title is required",
            line,
            category=LINT_DANGLING_TITLE_SEPARATOR,
        )
    title = (raw_title or "").strip(" \t") or None
    return session_id, title, len(heading.group("hashes"))


def _iter_structural_lines(
    lines: Sequence[str],
) -> Iterator[tuple[int, str, bool, bool]]:
    """Yield source lines with fence-aware structural-candidate flags.

    Sole owner of fenced-code bookkeeping for source plans, so the fail-fast
    parser and the accumulating linter can never disagree about which lines are
    structure and which are content.
    """
    fence: str | None = None
    for index, line in enumerate(lines):
        delimiter = _fence_delimiter(line)
        if delimiter is not None:
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            # A different fence family inside an open fence is content; ignore.

        is_candidate = (
            fence is None and _SESSION_HEADING_CANDIDATE_RE.match(line) is not None
        )
        is_repositories = (
            fence is None
            and _REPOSITORIES_HEADING_CANDIDATE_RE.fullmatch(line) is not None
        )
        yield index + 1, line, is_candidate, is_repositories


def _source_registry_error(
    line_number: int, reason: str, line: str, *, category: str
) -> SourceRegistryError:
    return SourceRegistryError(
        line_number=line_number,
        category=category,
        source_line=line,
        reason=reason,
    )


def _redact_repository_row(_line: str) -> str:
    """Return an opaque diagnostic marker for a repository table line.

    Malformed tables cannot establish which cell is the Origin column, so no
    cell is safe to echo: credential-bearing URLs may appear in the header,
    separator, alias, or path position.  Keep the parameter to make every
    call site explicitly identify the source line being redacted.
    """
    return "[repository row redacted]"


def _parse_repository_registry(
    lines: Sequence[str],
    structural_lines: Sequence[tuple[int, str, bool, bool]],
) -> tuple[RepositoryRegistry | None, frozenset[int]]:
    """Parse and locate the optional source registry.

    Returned indices identify only the authoritative registry block. Callers
    remove those lines from shared context while preserving every other source
    line verbatim.
    """
    first_session_line = next(
        (
            line_number
            for line_number, _, candidate, _ in structural_lines
            if candidate
        ),
        None,
    )
    headings = [
        (line_number, line)
        for line_number, line, _, candidate in structural_lines
        if candidate
    ]
    if not headings:
        return None, frozenset()

    first_heading_line, first_heading = headings[0]
    if first_session_line is not None and first_heading_line > first_session_line:
        raise _source_registry_error(
            first_heading_line,
            "the top-level ## Repositories table must appear before the first session",
            first_heading,
            category=LINT_REPOSITORIES_PLACEMENT,
        )
    if first_heading != _REPOSITORIES_HEADING:
        raise _source_registry_error(
            first_heading_line,
            "repository registry heading must be exactly '## Repositories'",
            first_heading,
            category=LINT_REPOSITORIES_PLACEMENT,
        )
    if len(headings) > 1:
        duplicate_line, duplicate_heading = headings[1]
        raise _source_registry_error(
            duplicate_line,
            f"duplicate repository registry; first defined on line {first_heading_line}",
            duplicate_heading,
            category=LINT_DUPLICATE_REPOSITORIES,
        )

    heading_index = first_heading_line - 1
    table_index = heading_index + 1
    while table_index < len(lines) and not lines[table_index].strip():
        table_index += 1
    if table_index >= len(lines):
        raise _source_registry_error(
            first_heading_line,
            "repository registry table is missing",
            first_heading,
            category=LINT_MALFORMED_REPOSITORIES,
        )

    header = plans._split_table_row(lines[table_index])
    if header is None or tuple(cell.strip() for cell in header[1]) != _REPOSITORY_HEADERS:
        raise _source_registry_error(
            table_index + 1,
            "repository registry columns must be exactly Alias, Path, and Origin",
            _redact_repository_row(lines[table_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )

    separator_index = table_index + 1
    if separator_index >= len(lines):
        raise _source_registry_error(
            table_index + 1,
            "repository registry separator row is missing",
            _redact_repository_row(lines[table_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )
    separator = plans._split_table_row(lines[separator_index])
    if (
        separator is None
        or len(separator[1]) != len(_REPOSITORY_HEADERS)
        or not plans._is_separator_row(separator[1])
    ):
        raise _source_registry_error(
            separator_index + 1,
            "repository registry requires a three-column Markdown separator row",
            _redact_repository_row(lines[separator_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )

    repositories: list[Repository] = []
    row_index = separator_index + 1
    while row_index < len(lines):
        parsed_row = plans._split_table_row(lines[row_index])
        if parsed_row is None:
            break
        safe_source_line = _redact_repository_row(lines[row_index])
        cells = tuple(cell.strip() for cell in parsed_row[1])
        if len(cells) != len(_REPOSITORY_HEADERS):
            raise _source_registry_error(
                row_index + 1,
                "repository registry rows must contain exactly three cells",
                safe_source_line,
                category=LINT_MALFORMED_REPOSITORIES,
            )
        if any(not cell for cell in cells):
            raise _source_registry_error(
                row_index + 1,
                "repository registry Alias, Path, and Origin values must not be blank",
                safe_source_line,
                category=LINT_INVALID_REPOSITORY,
            )
        try:
            origin = canonicalize_declared_origin(cells[2])
            repository = Repository(alias=cells[0], path=cells[1], origin=origin)
            RepositoryRegistry(tuple((*repositories, repository)))
        except WorkspaceError as error:
            raise _source_registry_error(
                row_index + 1,
                str(error),
                safe_source_line,
                category=LINT_INVALID_REPOSITORY,
            ) from error
        repositories.append(repository)
        row_index += 1

    if not repositories:
        raise _source_registry_error(
            separator_index + 1,
            "repository registry must contain at least one repository row",
            _redact_repository_row(lines[separator_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )

    registry = RepositoryRegistry(tuple(repositories))
    excluded = frozenset(range(heading_index, row_index))
    return registry, excluded


def parse_plan_document(text: str) -> ParsedPlan:
    """Split a plan into shared context and per-session blocks (fence-aware).

    Case-insensitive H2--H6 ATX ``Session`` headings found inside a fenced code
    block are treated as content, never as structure. Malformed candidates and
    duplicate canonical session IDs raise :class:`PlanError` with source-line
    diagnostics.
    """
    lines = text.splitlines()
    context: list[str] = []
    sessions: list[ParsedSession] = []
    structural_lines = tuple(_iter_structural_lines(lines))
    registry, registry_line_indices = _parse_repository_registry(
        lines, structural_lines
    )

    current: dict | None = None
    first_id_lines: dict[str, int] = {}

    def _flush(end: int) -> None:
        if current is None:
            return
        body = lines[current["body_start"] : end]
        scope: plans.SessionScope | None = None
        if registry is not None:
            try:
                scope = plans.parse_session_scope_lines(
                    body,
                    registry.aliases,
                    start_line=current["body_start"] + 1,
                )
            except plans.SessionScopeError as error:
                raise SourceScopeError(
                    line_number=error.line_number,
                    category=LINT_INVALID_SESSION_SCOPE,
                    source_line=error.source_line,
                    reason=error.reason,
                ) from error
        sessions.append(
            ParsedSession(
                session_id=current["session_id"],
                title=current["title"],
                heading_level=current["heading_level"],
                lines=tuple(body),
                scope=scope,
            )
        )

    for line_number, line, is_candidate, _ in structural_lines:
        index = line_number - 1
        heading = (
            _parse_source_session_heading(line, line_number)
            if is_candidate
            else None
        )
        if heading is None:
            if current is None and index not in registry_line_indices:
                context.append(line)
            continue

        session_id, title, heading_level = heading
        first_line = first_id_lines.get(session_id.value)
        if first_line is not None:
            raise _source_heading_error(
                line_number,
                f"duplicate canonical session id {session_id.value!r}; "
                f"first defined on line {first_line}",
                line,
                category=LINT_DUPLICATE_SESSION_ID,
            )
        first_id_lines[session_id.value] = line_number

        _flush(index)
        current = {
            "session_id": session_id,
            "title": title,
            "heading_level": heading_level,
            "body_start": line_number,
        }

    _flush(len(lines))

    return ParsedPlan(
        context_lines=tuple(context),
        sessions=tuple(sessions),
        registry=registry,
    )


def _suggest_heading_repair(error: SourceHeadingError) -> str | None:
    """Rebuild a canonical heading when exactly one rewrite is correct."""
    if error.category not in LINT_SUGGESTABLE_CATEGORIES:
        return None

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


def lint_plan_document(text: str) -> PlanLintResult:
    """Collect every malformed source heading instead of stopping at the first.

    :func:`parse_plan_document` fails fast, so repairing a plan through it costs
    one round trip per broken heading. This walks the same fence-aware grammar
    and the same per-heading parser, but accumulates diagnostics so a caller can
    repair a whole plan in one pass.
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

    return PlanLintResult(
        findings=tuple(sorted(findings, key=lambda finding: finding.line)),
        session_ids=tuple(session_ids),
        registry=registry,
    )


# ---------------------------------------------------------------------------
# Heading normalization
# ---------------------------------------------------------------------------


def _trim_body(lines: list[str]) -> list[str]:
    """Drop leading/trailing blank lines and surrounding ``---`` rules."""
    start = 0
    end = len(lines)
    while start < end and (lines[start].strip() == "" or lines[start].strip() == "---"):
        start += 1
    while end > start and (
        lines[end - 1].strip() == "" or lines[end - 1].strip() == "---"
    ):
        end -= 1
    return lines[start:end]


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
    return payload


def _render_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def contract_from_payload(
    session_id: int | str | SessionId,
    done_when: tuple[str, ...] | list[str],
    verify_payload: dict,
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


def context_reference_line() -> str:
    return CONTEXT_REFERENCE_LINE


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
    scope: plans.SessionScope | None = None,
    repository_registry: RepositoryRegistry | None = None,
) -> str:
    """Render a full ``session-<id>.md`` file.

    Original instruction content is preserved verbatim except for standalone
    heading-level normalization (see :func:`normalize_heading_levels`). When
    ``require_contract`` is set (deterministic preparation mode), a parseable
    ``contract`` is mandatory and its absence fails closed.
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
        parts.extend(plans.render_session_scope(scope).rstrip("\n").splitlines())
    parts.append("")
    parts.append("---")
    parts.append("")

    body = normalize_heading_levels(
        _trim_body(list(instruction_lines)), heading_level
    )
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
    return plans.PROGRESS_HEADER


# ---------------------------------------------------------------------------
# Manifest rendering and table updates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestSession:
    """One row in a manifest ``## Sessions`` table."""

    session_id: SessionId
    title: str
    status: plans.StatusValue = "pending"

    @property
    def filename(self) -> str:
        return self.session_id.filename


def render_manifest_session_row(session: ManifestSession) -> str:
    return (
        f"| {session.session_id.value} | {session.title} | "
        f"{session.status} | {session.filename} |"
    )


def render_manifest_sessions_table(sessions: tuple[ManifestSession, ...]) -> str:
    lines = [
        "| # | Title | Status | File |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(render_manifest_session_row(session) for session in sessions)
    return "\n".join(lines) + "\n"


def render_repository_registry_table(registry: RepositoryRegistry) -> str:
    """Render the authoritative ordered repository registry as Markdown."""
    lines = [
        "| Alias | Path | Origin |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {repository.alias} | {repository.path} | {repository.origin} |"
        for repository in registry
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ManifestSpec:
    """Inputs for deterministic ``manifest.md`` rendering."""

    plan_title: str
    source_path: str
    output_dir: str
    plan_id: str
    source_fingerprint: str
    content_sha256: str
    sessions: tuple[ManifestSession, ...]
    split_date: str
    readiness: ReadinessMetadata
    skill_prefix: str = "/"
    execution_note: str | None = None
    registry: RepositoryRegistry | None = None


def render_manifest(spec: ManifestSpec) -> str:
    """Render a serial-only ``manifest.md`` including readiness metadata lines."""
    prefix = spec.skill_prefix
    run_command = f"{prefix}session-run {spec.output_dir}/"
    lines: list[str] = [
        f"# {spec.plan_title}",
        "",
        f"**Split from**: `{spec.source_path}`",
        f"**Output directory**: `{spec.output_dir}`",
        f"**Plan ID**: `{spec.plan_id}`",
        f"**Source fingerprint**: `{spec.source_fingerprint}`",
        f"**Content SHA-256**: `{spec.content_sha256}`",
        f"**Sessions**: {len(spec.sessions)}",
        f"**Split date**: {spec.split_date}",
        f"**Readiness**: {spec.readiness.state}",
        f"**Readiness SHA-256**: `{spec.readiness.readiness_sha256}`",
        f"**Readiness checked at**: {spec.readiness.checked_at}",
    ]
    if spec.registry is not None:
        lines.extend(
            [
                "",
                "## Repositories",
                "",
                render_repository_registry_table(spec.registry).rstrip("\n"),
            ]
        )
    lines.extend(
        [
            "",
            "## Sessions",
            "",
            render_manifest_sessions_table(spec.sessions).rstrip("\n"),
            "",
            "## Execution",
            "",
            (
                f"Run `{prefix}session-run` once per session, preferably in a fresh "
                "harness conversation for each:"
            ),
            "",
            "```text",
            run_command,
            "```",
            "",
            f"Example rendered command: `{run_command}`",
            "",
            (
                f"`{prefix}session-run` auto-advances through pending sessions in "
                "dotted numeric order. Session handoff statuses are authoritative; "
                "this manifest table is derived bookkeeping."
            ),
        ]
    )
    if spec.execution_note:
        lines.extend(["", spec.execution_note.rstrip("\n")])
    return "\n".join(lines) + "\n"


def replace_manifest_sessions_count(manifest_text: str, total_sessions: int) -> str:
    """Rewrite the manifest's ``**Sessions**: N`` metadata line, if present."""
    return re.sub(
        r"(?m)^(\*\*Sessions\*\*:[ \t]*)\d+[ \t]*$",
        rf"\g<1>{total_sessions}",
        manifest_text,
    )


def _find_header(headers: list[str], names: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in names:
            return index
    return None


def _replace_cell_content(cell: str, value: str) -> str:
    match = re.fullmatch(r"(\s*)(.*?)(\s*)", cell)
    if match is None:
        return value
    return f"{match.group(1)}{value}{match.group(3)}"


def _join_table_row(leading: str, cells: list[str]) -> str:
    return f"{leading}|{'|'.join(cells)}|"


def _replace_status_table_rows(
    block: list[str],
    session_id: SessionId,
    replacements: tuple[ManifestSession, ...],
) -> tuple[list[str], bool]:
    parsed_header = plans._split_table_row(block[0])
    assert parsed_header is not None
    headers = [plans._normalize_table_header(cell) for cell in parsed_header[1]]
    session_index = _find_header(headers, plans.SESSION_STATUS_TABLE_HEADERS)
    status_index = _find_header(headers, {"status"})
    title_index = _find_header(headers, {"title"})
    file_index = _find_header(headers, {"file"})
    if session_index is None or status_index is None:
        return block, False

    changed = False
    updated = block[:2]
    for row in block[2:]:
        parsed = plans._split_table_row(row)
        if parsed is None:
            updated.append(row)
            continue
        leading, cells = parsed
        if max(session_index, status_index) >= len(cells):
            updated.append(row)
            continue
        if plans._session_id_from_cell(cells[session_index]) != session_id:
            updated.append(row)
            continue

        for replacement in replacements:
            new_cells = list(cells)
            new_cells[session_index] = _replace_cell_content(
                new_cells[session_index], replacement.session_id.value
            )
            new_cells[status_index] = _replace_cell_content(
                new_cells[status_index], replacement.status
            )
            if title_index is not None and title_index < len(new_cells):
                new_cells[title_index] = _replace_cell_content(
                    new_cells[title_index], replacement.title
                )
            if file_index is not None and file_index < len(new_cells):
                new_cells[file_index] = _replace_cell_content(
                    new_cells[file_index], replacement.filename
                )
            updated.append(_join_table_row(leading, new_cells))
        changed = True
    return updated, changed


def replace_session_status_rows(
    text: str,
    session_id: int | str | SessionId,
    replacements: tuple[ManifestSession, ...] | list[ManifestSession],
) -> tuple[str, bool]:
    """Replace one session's row in every recognized session-status table.

    Each matching row is replaced with one row per ``replacements`` entry.
    Unrelated cells keep their padding; only the session-id, status, and (when
    present) title/file columns are rewritten. Tables without both a session-id
    column and a ``Status`` column are left untouched, as are legacy
    dependency-wave and ownership tables.
    """
    canonical_id = parse_session_id(session_id)
    replacement_rows = tuple(replacements)
    lines = text.splitlines()
    updated: list[str] = []
    changed = False
    index = 0

    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            updated.append(lines[index])
            index += 1
            continue

        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        parsed_header = plans._split_table_row(block[0]) if len(block) >= 2 else None
        parsed_separator = plans._split_table_row(block[1]) if len(block) >= 2 else None
        if (
            parsed_header is None
            or parsed_separator is None
            or not plans._is_separator_row(parsed_separator[1])
        ):
            updated.extend(block)
            continue

        new_block, block_changed = _replace_status_table_rows(
            block, canonical_id, replacement_rows
        )
        updated.extend(new_block)
        changed = changed or block_changed

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(updated) + suffix, changed


def replace_previous_session_reference(
    text: str,
    old_session_id: int | str | SessionId,
    new_session_id: int | str | SessionId,
) -> tuple[str, bool]:
    """Repoint a ``> **Previous session**`` reference line at a new session file."""
    old_line = previous_session_reference_line(old_session_id)
    new_line = previous_session_reference_line(new_session_id)
    pattern = re.compile(rf"(?m)^{re.escape(old_line)}$")
    updated = pattern.sub(lambda _match: new_line, text)
    return updated, updated != text


def add_manifest_session_row(manifest_text: str, session: ManifestSession) -> str:
    """Append a session row to the manifest's ``## Sessions`` status table.

    Reuses :mod:`plans` table primitives so the inserted row matches the
    existing header/separator alignment. Raises :class:`PlanError` when no
    session-status table is present.
    """
    lines = manifest_text.splitlines(keepends=False)
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        if _is_session_status_table(block):
            new_row = render_manifest_session_row(session)
            leading = block[0][: len(block[0]) - len(block[0].lstrip())]
            updated = lines[:index] + [f"{leading}{new_row.lstrip()}"] + lines[index:]
            suffix = "\n" if manifest_text.endswith("\n") else ""
            return "\n".join(updated) + suffix

    raise PlanError("manifest has no session-status table to update")


def _is_session_status_table(block: list[str]) -> bool:
    if len(block) < 2:
        return False
    header = plans._split_table_row(block[0])
    separator = plans._split_table_row(block[1])
    if header is None or separator is None:
        return False
    if not plans._is_separator_row(separator[1]):
        return False
    headers = [plans._normalize_table_header(cell) for cell in header[1]]
    if "status" not in headers:
        return False
    return any(name in plans.SESSION_STATUS_TABLE_HEADERS for name in headers)


# ---------------------------------------------------------------------------
# Archive paths and readiness refresh
# ---------------------------------------------------------------------------


def _timestamp_slug(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def archive_dir_name(session_id: int | str | SessionId, now: datetime) -> str:
    """Deterministic archive subdirectory name for a resplit parent session."""
    canonical_id = parse_session_id(session_id)
    return f"session-{canonical_id.value}-{_timestamp_slug(now)}"


def create_archive_dir(
    plan_dir: str | os.PathLike[str],
    session_id: int | str | SessionId,
    now: datetime,
) -> Path:
    """Create a collision-safe archive directory under ``resplit-archive/``."""
    canonical_dir = canonicalize_path(plan_dir)
    archive_root = canonical_dir / ARCHIVE_ROOT_NAME
    archive_root.mkdir(exist_ok=True)

    base_name = archive_dir_name(session_id, now)
    candidate = archive_root / base_name
    collision_index = 2
    while True:
        try:
            candidate.mkdir()
        except FileExistsError:
            candidate = archive_root / f"{base_name}-{collision_index}"
            collision_index += 1
            continue
        return candidate


def refresh_readiness_metadata(
    plan_dir: str | os.PathLike[str],
    *,
    state: plans.ReadinessState = "draft",
    checked_at: str | None = None,
    now: datetime | None = None,
) -> ReadinessMetadata:
    """Recompute the stable readiness SHA and write manifest readiness lines.

    ``checked_at`` may be supplied for deterministic callers; otherwise a UTC
    ISO-8601 timestamp is derived from ``now`` (defaulting to the current time).
    """
    canonical_dir = canonicalize_path(plan_dir)
    sha = stable_readiness_sha256(canonical_dir)
    if checked_at is None:
        stamp = now if now is not None else datetime.now(timezone.utc)
        checked_at = stamp.astimezone(timezone.utc).isoformat()
    metadata = ReadinessMetadata(
        state=state, readiness_sha256=sha, checked_at=checked_at
    )
    write_readiness_metadata(canonical_dir, metadata)
    return metadata


# ---------------------------------------------------------------------------
# Atomic multi-file transaction
# ---------------------------------------------------------------------------


@dataclass
class _StagedFile:
    path: Path
    text: str


class PlanTransaction:
    """Stage many plan files and publish them as one recoverable transaction.

    Immutable payloads and a durable journal are written before any destination
    changes. Ordinary I/O failures roll back immediately; process interruption
    leaves the journal in place so the next authoritative plan read rolls the
    complete publication forward instead of observing a mixed generation.

    Callers still hold the plan lock to serialize writers. The transaction also
    holds a marker-scoped mutex shared with ordinary read-side recovery.
    """

    def __init__(self, plan_dir: str | os.PathLike[str]) -> None:
        self.plan_dir = canonicalize_path(plan_dir)
        self._staged: list[_StagedFile] = []

    def stage(self, relative_path: str | os.PathLike[str], text: str) -> None:
        """Stage ``text`` to be written at ``relative_path`` under the plan dir."""
        path = (self.plan_dir / os.fspath(relative_path)).resolve()
        self._staged.append(_StagedFile(path=path, text=text))

    @property
    def staged_paths(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self._staged)

    def commit(self) -> tuple[Path, ...]:
        """Write all staged files, with durable roll-forward crash recovery."""
        if not self._staged:
            return ()
        try:
            self.plan_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PlanError(f"failed to stage plan write: {error}") from error
        plans.recover_pending_plan_transaction(self.plan_dir)
        transaction_dir = self.plan_dir / plans.PLAN_TRANSACTION_DIR_NAME
        staging_dir = self.plan_dir / plans.PLAN_TRANSACTION_STAGING_DIR_NAME
        cleanup_dir = self.plan_dir / plans.PLAN_TRANSACTION_CLEANUP_DIR_NAME
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(cleanup_dir, ignore_errors=True)
            staging_dir.mkdir()
            entries: list[dict[str, str]] = []
            for index, item in enumerate(self._staged):
                try:
                    relative_path = item.path.relative_to(self.plan_dir).as_posix()
                except ValueError as error:
                    raise PlanError(
                        f"staged plan path escapes the plan directory: {item.path}"
                    ) from error
                content = item.text.encode("utf-8")
                payload_name = f"{index:06d}.bin"
                payload_path = staging_dir / payload_name
                with payload_path.open("wb") as handle:
                    handle.write(content)
                    handle.flush()
                    durability.fullsync(handle.fileno())
                entries.append(
                    {
                        "path": relative_path,
                        "payload": payload_name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            journal_temp = staging_dir / f".{plans.PLAN_TRANSACTION_JOURNAL_NAME}.tmp"
            with journal_temp.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": plans.PLAN_TRANSACTION_SCHEMA_VERSION,
                        "entries": entries,
                    },
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                durability.fullsync(handle.fileno())
            os.replace(
                journal_temp,
                staging_dir / plans.PLAN_TRANSACTION_JOURNAL_NAME,
            )
            plans._fsync_directory(staging_dir)
        except (OSError, PlanError) as error:
            shutil.rmtree(staging_dir, ignore_errors=True)
            if isinstance(error, PlanError):
                raise
            raise PlanError(f"failed to stage plan write: {error}") from error

        try:
            with plans._plan_transaction_mutex(staging_dir):
                return self._publish_staged_transaction(
                    transaction_dir=transaction_dir,
                    staging_dir=staging_dir,
                )
        except (OSError, PlanError) as error:
            shutil.rmtree(staging_dir, ignore_errors=True)
            if isinstance(error, PlanError):
                raise
            raise PlanError(f"failed to stage plan write: {error}") from error

    def _publish_staged_transaction(
        self, *, transaction_dir: Path, staging_dir: Path
    ) -> tuple[Path, ...]:
        """Publish prepared payloads while holding their transaction mutex."""
        try:
            os.replace(staging_dir, transaction_dir)
            plans._fsync_directory(self.plan_dir)
        except OSError as error:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise PlanError(f"failed to stage plan write: {error}") from error

        backups: list[tuple[Path, bytes | None]] = []
        replaced: list[Path] = []
        install_temps: list[Path] = []
        try:
            for index, item in enumerate(self._staged):
                original = _read_bytes_if_exists(item.path)
                backups.append((item.path, original))
                item.path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = item.path.with_name(
                    f".{item.path.name}.asl-transaction-install.tmp"
                )
                install_temps.append(temp_path)
                content = (transaction_dir / f"{index:06d}.bin").read_bytes()
                with temp_path.open("wb") as handle:
                    handle.write(content)
                    handle.flush()
                    durability.fullsync(handle.fileno())
                os.replace(temp_path, item.path)
                plans._fsync_directory(item.path.parent)
                replaced.append(item.path)
        except OSError as error:
            rolled_back = self._rollback(backups, replaced, install_temps)
            if rolled_back:
                try:
                    plans._retire_plan_transaction(self.plan_dir, transaction_dir)
                except OSError:
                    pass
            raise PlanError(f"failed to commit plan write: {error}") from error

        try:
            plans._retire_plan_transaction(self.plan_dir, transaction_dir)
        except OSError as error:
            raise PlanError(
                f"plan write committed but transaction cleanup failed: {error}"
            ) from error
        return tuple(item.path for item in self._staged)

    @staticmethod
    def _rollback(
        backups: list[tuple[Path, bytes | None]],
        replaced: list[Path],
        temp_paths: list[Path],
    ) -> bool:
        succeeded = True
        replaced_set = set(replaced)
        for path, original in backups:
            if path not in replaced_set:
                continue
            if original is None:
                _silent_unlink(path)
            else:
                try:
                    path.write_bytes(original)
                except OSError:
                    succeeded = False
        for tmp_path in temp_paths:
            _silent_unlink(tmp_path)
        return succeeded


def _read_bytes_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# `asl split` end-to-end orchestration
# ---------------------------------------------------------------------------

SPLIT_REASON_OK = "ok"
SPLIT_REASON_SINGLE_SESSION = "single-session"
SPLIT_REASON_MALFORMED_PLAN = "malformed-plan"
SPLIT_REASON_INPUT_ERROR = "input-error"

#: Message emitted when a plan has fewer than two sessions.
SINGLE_SESSION_MESSAGE = "splitting is not needed"


class SingleSessionError(PlanError):
    """Raised when a plan defines fewer than two sessions to split."""

    def __init__(self, session_count: int) -> None:
        self.session_count = session_count
        super().__init__(
            f"only {session_count} session found — {SINGLE_SESSION_MESSAGE}"
        )


class MissingContractError(PlanError):
    """Raised when no contract is supplied for a session being rendered."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"missing contract for session {session_id}")


class InvalidContractError(PlanError):
    """Raised when a supplied contract entry is not a well-formed object.

    A contract entry must be a JSON object carrying both a ``done_when`` array
    and a ``verify`` recipe. A non-object entry or one missing either field
    would otherwise raise ``TypeError``/``KeyError`` when the fields are indexed;
    this fails closed with a stable structured reason instead.
    """

    def __init__(self, session_id: str, detail: str) -> None:
        self.session_id = session_id
        self.detail = detail
        super().__init__(f"invalid contract for session {session_id}: {detail}")


@dataclass(frozen=True)
class SplitResult:
    """The authoritative outcome of one deterministic ``asl split`` run."""

    output_dir: Path
    plan_id: str
    source_path: str
    session_ids: tuple[str, ...]
    files: tuple[Path, ...]
    readiness: ReadinessMetadata
    skill_prefix: str
    registry: RepositoryRegistry | None = None


def split_plan(
    *,
    plan_file: str | os.PathLike[str],
    contracts_by_id: Mapping[str, Mapping[str, Any]],
    base_dir: str | os.PathLike[str] | None = None,
    skill_prefix: str = "/",
    analysis_note: str | None = None,
    now: datetime | None = None,
) -> SplitResult:
    """Author a split plan directory from a source plan and Step 7 contracts.

    Chains the existing authoring APIs end to end: read the source plan, parse
    it into shared context plus per-session blocks
    (:func:`parse_plan_document`), build one validated
    :class:`~agent_session_loop.contracts.CompletionContract` per session from
    ``contracts_by_id`` (:func:`contract_from_payload`), stage context.md /
    progress.md / session files / manifest.md through one atomic
    :class:`PlanTransaction`, and refresh readiness metadata to ``draft``.

    Contracts are validated *before* the collision-safe output directory is
    created, so a :class:`MissingContractError` (no contract for a session) or a
    malformed contract leaves no empty directory behind. A plan with fewer than
    two sessions raises :class:`SingleSessionError`; a malformed source heading
    raises the line-specific :class:`PlanError` from the parser verbatim.
    """
    canonical_source = os.path.realpath(
        os.path.abspath(os.path.expanduser(os.fspath(plan_file)))
    )
    try:
        text = Path(canonical_source).read_text(encoding="utf-8")
    except OSError as error:
        raise PlanError(f"cannot read source plan {canonical_source}: {error}") from error

    parsed = parse_plan_document(text)
    if len(parsed.sessions) < 2:
        raise SingleSessionError(len(parsed.sessions))

    # Build (and validate) every contract before creating any directory so a
    # missing or malformed contract never leaves a stranded empty output dir.
    contracts: dict[str, _contracts.CompletionContract] = {}
    for session in parsed.sessions:
        session_id = session.session_id.value
        spec = contracts_by_id.get(session_id)
        if spec is None:
            raise MissingContractError(session_id)
        # Validate the entry as an object with both fields before indexing so a
        # non-object entry or a missing field fails closed as a PlanError rather
        # than escaping as an uncaught TypeError/KeyError.
        if not isinstance(spec, Mapping):
            raise InvalidContractError(
                session_id,
                f"expected a JSON object, got {type(spec).__name__}",
            )
        missing = [field for field in ("done_when", "verify") if field not in spec]
        if missing:
            raise InvalidContractError(
                session_id,
                f"missing required field(s): {', '.join(missing)}",
            )
        contracts[session_id] = contract_from_payload(
            session_id,
            spec["done_when"],
            spec["verify"],
            repository_registry=parsed.registry,
        )

    resolver = resolve_plan_output(canonical_source, base_dir)
    output_dir = Path(str(resolver["output_dir"]))

    transaction = PlanTransaction(output_dir)
    transaction.stage(
        "context.md",
        render_context(parsed.context_lines, str(resolver["source_path"])),
    )
    transaction.stage("progress.md", render_progress_skeleton())

    manifest_sessions: list[ManifestSession] = []
    for index, session in enumerate(parsed.sessions):
        session_id = session.session_id.value
        transaction.stage(
            session.session_id.filename,
            render_session_file(
                session_id=session_id,
                title=session.title,
                instruction_lines=session.instruction_lines,
                heading_level=session.heading_level,
                is_first=index == 0,
                previous_session_id=(
                    parsed.sessions[index - 1].session_id if index else None
                ),
                contract=contracts[session_id],
                scope=session.scope,
                repository_registry=parsed.registry,
            ),
        )
        manifest_sessions.append(
            ManifestSession(
                session_id=session.session_id,
                title=session.title or f"Session {session_id}",
            )
        )

    split_date = (now or datetime.now(timezone.utc)).date().isoformat()
    transaction.stage(
        "manifest.md",
        render_manifest(
            ManifestSpec(
                plan_title=title_from_plan(text),
                source_path=str(resolver["source_path"]),
                output_dir=str(output_dir),
                plan_id=str(resolver["plan_id"]),
                source_fingerprint=str(resolver["source_fingerprint"]),
                content_sha256=str(resolver["content_sha256"]),
                sessions=tuple(manifest_sessions),
                split_date=split_date,
                readiness=ReadinessMetadata(
                    state="draft",
                    readiness_sha256="pending",
                    checked_at="pending",
                ),
                registry=parsed.registry,
                skill_prefix=skill_prefix,
                execution_note=analysis_note,
            )
        ),
    )
    written = transaction.commit()
    metadata = refresh_readiness_metadata(output_dir, state="draft")

    return SplitResult(
        output_dir=output_dir,
        plan_id=str(resolver["plan_id"]),
        source_path=str(resolver["source_path"]),
        session_ids=parsed.session_ids,
        files=written,
        readiness=metadata,
        skill_prefix=skill_prefix,
        registry=parsed.registry,
    )


def _readiness_payload(metadata: ReadinessMetadata) -> dict[str, Any]:
    return {
        "state": metadata.state,
        "readiness_sha256": metadata.readiness_sha256,
        "checked_at": metadata.checked_at,
    }


def _registry_payload(registry: RepositoryRegistry | None) -> dict[str, Any]:
    if registry is None:
        return {}
    return {
        "repositories": [repository.to_dict() for repository in registry],
        "registry_digest": registry.digest,
    }


def _split_next_steps(output_dir: Path) -> list[dict[str, Any]]:
    """Session-expand invocations offered as the split plan's next step."""
    step = responses.invocation_step(
        (
            "Validate the split plan against the codebase and mark it "
            "loop-ready before running it."
        ),
        harnesses.skill_invocations("session-expand", str(output_dir)),
    )
    return responses.next_steps_payload([step])["next_steps"]


def _resolve_skill_prefix(
    skill_prefix: str,
) -> tuple[str | None, str | None]:
    """Resolve a ``--skill-prefix`` choice to a prefix char.

    Returns ``(prefix, error)``. ``auto`` detects the invoking harness and
    errors when detection is ambiguous; a named harness maps directly to its
    prefix.
    """
    if skill_prefix == "auto":
        detected = harnesses.detect_invoking_harness()
        if detected is None:
            return None, (
                "could not auto-detect the invoking harness; pass "
                "--skill-prefix {codex,claude,copilot}"
            )
        return harnesses.SKILL_PREFIXES[detected], None
    prefix = harnesses.SKILL_PREFIXES.get(skill_prefix)
    if prefix is None:
        return None, f"unknown skill prefix harness: {skill_prefix}"
    return prefix, None


def _emit_split_error(
    reason: str, error: str, *, want_json: bool, **fields: Any
) -> int:
    if want_json:
        responses.print_json(
            responses.error_payload(
                "split", reason=reason, error=error, paused=False, **fields
            )
        )
    else:
        print(f"agent-session-loop split: {error}", file=sys.stderr)
    return 1


def check_from_args(args: Any) -> int:
    """Run ``asl split --check``: lint a source plan and write nothing.

    Deliberately skips ``capture_inline_plan`` so a repair loop can re-check the
    same plan repeatedly without persisting a source file per attempt.
    """
    want_json = bool(args.json)

    if args.inline_stdin:
        source_path = "<stdin>"
        try:
            text = extract_inline_plan(sys.stdin.read())
        except ValueError as error:
            return _emit_split_error(
                SPLIT_REASON_INPUT_ERROR, str(error), want_json=want_json
            )
    else:
        source_path = os.path.expanduser(str(args.plan_file))
        if not Path(source_path).is_file():
            return _emit_split_error(
                SPLIT_REASON_INPUT_ERROR,
                f"source plan file does not exist: {args.plan_file}",
                want_json=want_json,
            )
        try:
            text = Path(source_path).read_text(encoding="utf-8")
        except OSError as error:
            return _emit_split_error(
                SPLIT_REASON_INPUT_ERROR,
                f"cannot read source plan {source_path}: {error}",
                want_json=want_json,
                source_path=source_path,
            )

    result = lint_plan_document(text)

    if result.findings:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "split",
                    reason=SPLIT_REASON_MALFORMED_PLAN,
                    error=result.findings[0].message,
                    paused=False,
                    checked=True,
                    source_path=source_path,
                    session_ids=list(result.session_ids),
                    findings=[finding.as_dict() for finding in result.findings],
                    **_registry_payload(result.registry),
                )
            )
        else:
            print(
                f"agent-session-loop split --check: {len(result.findings)} "
                f"malformed session heading(s) in {source_path}",
                file=sys.stderr,
            )
            for finding in result.findings:
                print(f"  {finding.message}", file=sys.stderr)
                if finding.suggested_line is not None:
                    print(
                        f"    suggested: {finding.suggested_line}", file=sys.stderr
                    )
        return 1

    if len(result.session_ids) < 2:
        return _emit_split_error(
            SPLIT_REASON_SINGLE_SESSION,
            str(SingleSessionError(len(result.session_ids))),
            want_json=want_json,
            checked=True,
            source_path=source_path,
            session_ids=list(result.session_ids),
            **_registry_payload(result.registry),
        )

    if want_json:
        responses.print_json(
            responses.success_payload(
                "split",
                checked=True,
                source_path=source_path,
                session_ids=list(result.session_ids),
                session_count=len(result.session_ids),
                **_registry_payload(result.registry),
            )
        )
    else:
        print(f"Checked {len(result.session_ids)} sessions in {source_path}")
        if result.registry is not None:
            print(f"  Repositories: {', '.join(result.registry.aliases)}")
        for session_id in result.session_ids:
            print(f"  Session {session_id}")
    return 0


def split_from_args(args: Any) -> int:
    """Run the ``split`` subcommand from CLI arguments."""
    want_json = bool(args.json)
    base_dir = args.base_dir
    common_fields: dict[str, Any] = {}

    def _emit_error(reason: str, error: str, **fields: Any) -> int:
        return _emit_split_error(
            reason, error, want_json=want_json, **{**common_fields, **fields}
        )

    if args.check:
        return check_from_args(args)

    if args.contracts_file is None:
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            "--contracts-file is required unless --check is given",
        )

    prefix, prefix_error = _resolve_skill_prefix(args.skill_prefix)
    if prefix_error is not None:
        return _emit_error(SPLIT_REASON_INPUT_ERROR, prefix_error)

    # Resolve the source plan (inline stdin capture or an explicit file).
    if args.inline_stdin:
        raw_input = sys.stdin.read()
        try:
            captured = capture_inline_plan(raw_input, base_dir)
        except ValueError as error:
            return _emit_error(SPLIT_REASON_INPUT_ERROR, str(error))
        source_path = str(captured["source_path"])
    else:
        source_path = os.path.expanduser(str(args.plan_file))
        if not Path(source_path).is_file():
            return _emit_error(
                SPLIT_REASON_INPUT_ERROR,
                f"source plan file does not exist: {args.plan_file}",
            )
    common_fields["source_path"] = source_path

    # Load the Step 7 contracts keyed by canonical session id.
    try:
        contracts_text = Path(
            os.path.expanduser(str(args.contracts_file))
        ).read_text(encoding="utf-8")
    except OSError as error:
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            f"cannot read contracts file {args.contracts_file}: {error}",
        )
    try:
        contracts_by_id = json.loads(contracts_text)
    except json.JSONDecodeError as error:
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            f"contracts file is not valid JSON: {error.msg} at line {error.lineno}",
        )
    if not isinstance(contracts_by_id, dict):
        return _emit_error(
            SPLIT_REASON_INPUT_ERROR,
            "contracts file must be a JSON object keyed by session id",
        )

    analysis_note: str | None = None
    if args.analysis_file:
        try:
            analysis_note = Path(
                os.path.expanduser(str(args.analysis_file))
            ).read_text(encoding="utf-8")
        except OSError as error:
            return _emit_error(
                SPLIT_REASON_INPUT_ERROR,
                f"cannot read analysis file {args.analysis_file}: {error}",
            )

    try:
        result = split_plan(
            plan_file=source_path,
            contracts_by_id=contracts_by_id,
            base_dir=base_dir,
            skill_prefix=prefix,
            analysis_note=analysis_note,
        )
    except SingleSessionError as error:
        return _emit_error(SPLIT_REASON_SINGLE_SESSION, str(error))
    except MissingContractError as error:
        return _emit_error(
            f"missing-contract:{error.session_id}", str(error)
        )
    except InvalidContractError as error:
        return _emit_error(
            f"invalid-contract:{error.session_id}", str(error)
        )
    except PlanError as error:
        return _emit_error(SPLIT_REASON_MALFORMED_PLAN, str(error))

    payload = {
        "output_dir": str(result.output_dir),
        "plan_id": result.plan_id,
        "source_path": result.source_path,
        "session_ids": list(result.session_ids),
        "files": [str(path) for path in result.files],
        "readiness": _readiness_payload(result.readiness),
        "next_steps": _split_next_steps(result.output_dir),
        "skill_prefix": result.skill_prefix,
        **_registry_payload(result.registry),
    }
    if want_json:
        responses.print_json(responses.success_payload("split", **payload))
    else:
        print(f"Split {len(result.session_ids)} sessions from {result.source_path}")
        print(f"Output directory: {result.output_dir}")
        print(f"Plan ID: {result.plan_id}")
        if result.registry is not None:
            print(f"Repositories: {', '.join(result.registry.aliases)}")
        for path in result.files:
            print(f"  {path}")
        print(f"Readiness: {result.readiness.state}")
    return 0
