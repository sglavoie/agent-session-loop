"""Source-plan heading grammar, lint categories, errors, and parsed-plan models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import (
    plans_session_scope,
)
from .plans_errors import PlanError
from .plans_session_ids import SessionId
from .workspaces import (
    RepositoryRegistry,
)

#: Matches the bounded source-plan session-heading grammar after any optional
#: ATX closing marker has been removed. Source IDs are intentionally broader
#: than :data:`plans_session_ids.SESSION_ID_PATTERN`; they are canonicalized privately
#: before a strict :class:`SessionId` is constructed.
SESSION_HEADING_RE = re.compile(
    r"^ {0,3}(?P<hashes>#{2,6})[ \t]+Session[ \t]+"
    r"(?P<session_id>[0-9]+(?:\.[0-9]+)*)"
    r"(?:[ \t]*(?P<separator>[:–—-])[ \t]*(?P<title>.*?))?"
    r"[ \t]*$",
    re.IGNORECASE,
)
#: Detects H2--H6 ATX lines that start a session id after the ``Session``
#: token: a digit after optional horizontal whitespace and at most one ``#``,
#: ``:``, ``.``, or ``-`` (``Session 1``, ``Session1``, ``Session #1``). A
#: detected line that does not match ``SESSION_HEADING_RE`` is malformed rather
#: than ordinary plan content. A heading whose next word is not an id
#: (``### Session cookies``, ``## Session plan``) is prose and stays content.
_SESSION_HEADING_CANDIDATE_RE = re.compile(
    r"^ {0,3}#{2,6}[ \t]+Session[ \t]*[#:.-]?[0-9]",
    re.IGNORECASE,
)
_SESSION_HEADING_PREFIX_RE = re.compile(
    r"^ {0,3}#{2,6}[ \t]+Session(?P<rest>.*)$",
    re.IGNORECASE,
)
_SOURCE_SESSION_ID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_ATX_CLOSING_MARKER_RE = re.compile(r"[ \t]+#+[ \t]*$")
_TITLE_SEPARATORS = frozenset((":", "-", "–", "—"))

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
LINT_OUT_OF_ORDER_SESSION = "out-of-order-session"
LINT_REPOSITORIES_PLACEMENT = "repositories-placement"
LINT_DUPLICATE_REPOSITORIES = "duplicate-repositories"
LINT_MALFORMED_REPOSITORIES = "malformed-repositories"
LINT_INVALID_REPOSITORY = "invalid-repository"
LINT_INVALID_SESSION_SCOPE = "invalid-session-scope"
LINT_MISSING_REPOSITORY_REGISTRY = "missing-repository-registry"

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
        super().__init__(f"line {line_number}: {reason}; source line: {source_line!r}")


class SourceRegistryError(SourceHeadingError):
    """A line-specific malformed source repository-registry diagnostic."""


class SourceScopeError(SourceHeadingError):
    """A line-specific malformed multi-repository session-scope diagnostic."""


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
    scope: plans_session_scope.SessionScope | None = None

    @property
    def instruction_lines(self) -> tuple[str, ...]:
        """Body lines with leading/trailing blank lines and ``---`` rules trimmed."""
        lines = (
            plans_session_scope.strip_session_scope_lines(self.lines)
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
    repository_footprint: dict[str, Any] | None = None


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
