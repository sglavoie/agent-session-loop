"""What the queue is narrowed by, counted over, and cut into -- said once.

Both cockpits used to answer "which plans, in what order, and how many" in
their own way. The Companion pushed project, title and harness into SQL and
paged what survived; the Workbench fetched everything and filtered it in the
browser. The second one is only correct while the catalog is small: filtering
after a read limit describes the first page rather than the corpus, and a
count taken from that page is a count of what happened to be fetched.

This module is the shared answer, and it is deliberately *not* an executor. It
declares the vocabulary -- which chip covers which primary state, which sort
modes exist, how wide the inactive-work window is -- and it derives the two
things that must not be re-derived per mode:

* :func:`allowed_states` turns the chip and the show-completed toggle into the
  set of primary states a page may contain. The Mac filters a list with it and
  the Pi puts it in a ``WHERE`` clause; neither invents membership.
* :func:`counts_from_states` turns a histogram of primary states into the chip
  counts. The histogram is taken over everything that matched project, title
  and harness -- the complete result set -- and never over the page, so the
  number on a chip is the number of plans that chip would show.

The window and the section vocabulary live here for the same reason: the front
end decides where a row is drawn, and it may not decide what "still active"
means, because a row the Pi calls in-progress and the Mac calls older would be
two applications wearing one name.

Portable: standard library only, no import from :mod:`agent_session_loop`, and
no clock of its own -- the moment is always an argument.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .http import Call, invalid_request

__all__ = [
    "ACTIVE_WINDOW_SECONDS",
    "COUNT_NAMES",
    "DEFAULT_PAGE_LIMIT",
    "DEFAULT_SORT_MODE",
    "FILTER_ALL",
    "FILTER_GROUPS",
    "MAX_PAGE_LIMIT",
    "OVERVIEW_SECTIONS",
    "PRIMARY_STATES",
    "SECTION_IN_PROGRESS",
    "SECTION_NEEDS_ATTENTION",
    "SECTION_OLDER",
    "SECTION_OLDER_LINKED",
    "SECTION_RECENT",
    "SECTION_UNCONFIRMED",
    "SORT_MODES",
    "UNGROUPED_PROJECT",
    "QueueFilters",
    "QueuePage",
    "allowed_states",
    "counts_from_states",
    "harness_of",
    "histogram",
    "matches",
    "paginate",
    "parse_filters",
    "plan_projects",
    "plan_search_text",
    "plan_title",
    "project_group",
    "project_names",
    "recently_active",
    "select",
    "sorted_rows",
]


# ---------------------------------------------------------------------------
# The vocabulary both modes render
# ---------------------------------------------------------------------------

#: How long work stays in the operator's current view after it last moved.
#:
#: Eight hours, which is the Companion's window and is not arbitrary: it is
#: about a working day, so a run that stopped moving before the operator went
#: to bed is not still competing for attention in the morning. A run that is
#: *running* or *verifying* is exempt -- it is active by observation rather
#: than by recency -- and a run that falls out is moved, never dropped, because
#: absence is not an ending.
ACTIVE_WINDOW_SECONDS = 8 * 60 * 60

#: Every primary state a plan row may carry, in the order the front end's
#: legacy ranking used. Mirrored by ``PRIMARY_STATES`` in ``constants.js``.
PRIMARY_STATES: tuple[str, ...] = (
    "invalid",
    "verifying",
    "running",
    "paused-for-reply",
    "failed",
    "stale",
    "complete",
    "recent",
    "ready",
)

FILTER_ALL = "all"

#: Which primary states each filter chip covers. Mirrored by ``FILTER_GROUPS``
#: in ``constants.js``; ``tests/test_workbench_overview.py`` pins the two
#: against each other, because a chip the server counts differently from the
#: way the browser filters is a number an operator cannot act on.
FILTER_GROUPS: Mapping[str, tuple[str, ...]] = {
    FILTER_ALL: PRIMARY_STATES,
    "running": ("running", "verifying"),
    "paused": ("paused-for-reply",),
    "failed": ("failed", "invalid"),
    "stale": ("stale",),
    "ready": ("ready",),
    "complete": ("complete",),
    "recent": ("recent",),
}

#: The chip names, in display order.
COUNT_NAMES: tuple[str, ...] = tuple(FILTER_GROUPS)

#: The states the "needs attention" default hides until the operator asks for
#: them, either with the toggle or by picking the ``complete`` chip.
COMPLETED_STATES: frozenset[str] = frozenset({"complete"})

#: The urgency group a completed plan carries, which is what the Pi publishes
#: in place of a primary state for a row it only knows as an ending.
COMPLETED_GROUP = "complete"

#: The sort modes the queue offers, unchanged from the Workbench's own: the
#: consolidation adds filtering and sectioning to them rather than replacing
#: them with the Companion's single "newest first".
SORT_MODES: tuple[str, ...] = ("urgency", "recent", "label", "path", "oldest")
DEFAULT_SORT_MODE = "urgency"

#: How groups of rows are ordered relative to each other once they have been
#: grouped by project. ``activity`` puts the project with the newest movement
#: first; ``alpha`` is the stable order an operator scans by name.
GROUP_ORDERS: tuple[str, ...] = ("activity", "alpha")
DEFAULT_GROUP_ORDER = "activity"

#: One page of the catalog. The Companion's ``READ_LIMIT``, and the local
#: mode's new ceiling: the Mac used to answer with the whole corpus, which is
#: the shape that made the browser filter after the fact.
DEFAULT_PAGE_LIMIT = 200
MAX_PAGE_LIMIT = 500

#: The sections the overview cuts current work into, in the order they are
#: rendered. Four of them are current work and two are not: ``older-linked``
#: holds a run that a notification named and that has since gone quiet, and
#: ``older`` holds everything the inactive-work window has moved out of the way.
#: Both are kept apart from current attention rather than either hidden or
#: mixed in -- a link an operator followed has to lead somewhere, and work that
#: stopped moving yesterday is still work.
SECTION_NEEDS_ATTENTION = "needs-attention"
SECTION_IN_PROGRESS = "in-progress"
SECTION_UNCONFIRMED = "unconfirmed"
SECTION_RECENT = "recent"
SECTION_OLDER_LINKED = "older-linked"
SECTION_OLDER = "older"
OVERVIEW_SECTIONS: tuple[str, ...] = (
    SECTION_NEEDS_ATTENTION,
    SECTION_IN_PROGRESS,
    SECTION_UNCONFIRMED,
    SECTION_RECENT,
    SECTION_OLDER_LINKED,
    SECTION_OLDER,
)

#: The sections the inactive-work window may move a row out of. A row in any
#: other section is there because of what it is rather than when it last moved.
WINDOWED_SECTIONS: frozenset[str] = frozenset({SECTION_RECENT})

#: The urgency groups that put a row in front of an operator, and the states
#: that mean a run is moving under its own power.
URGENCY_NEEDS_ACTION = "needs-action"
ACTIVE_STATES: frozenset[str] = frozenset({"running", "verifying"})

#: What a row says about the observation it came from. ``current`` is what the
#: newest snapshot still carries, ``unconfirmed`` is retained and not
#: re-observed, and ``absent`` is a row the newest observation dropped. The
#: local mode reads a filesystem and so is always ``current``; publishing the
#: word anyway is what lets one front end tell retained from live.
OBSERVATION_CURRENT = "current"
OBSERVATION_UNCONFIRMED = "unconfirmed"
OBSERVATION_ABSENT = "absent"


class OverviewError(ValueError):
    """A filter this module cannot honour, named rather than silently dropped."""


# ---------------------------------------------------------------------------
# Reading one row, whichever mode published it
# ---------------------------------------------------------------------------
#
# The two publications name the same facts differently and always will: the
# Mac has a path and the Pi has a digest, so `label`/`display_label` and
# `searchable_text`/`search_text` are not an oversight to be tidied away. The
# readers below are where that difference is absorbed, once, so nothing above
# them has to ask which mode answered.


def plan_title(row: Mapping[str, Any]) -> str:
    """The plan's manifest title, or an empty string when it has none."""
    title = row.get("title")
    return title.strip() if isinstance(title, str) else ""


def plan_label(row: Mapping[str, Any]) -> str:
    """The plan's display label under whichever name its mode published it.

    The manifest title is the last fallback, and it is not decorative: the Mac
    publishes ``label`` as ``null`` for a plan that has been given no explicit
    one, so a reader that stopped at the three label fields would call every
    such row unnamed -- and ``sort=label`` would then order the whole local
    queue under one empty string. ``identity.js::planLabel`` falls through the
    same way, which is the behaviour this mirrors.
    """
    for key in ("display_label", "label", "plan_label", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def plan_search_text(row: Mapping[str, Any]) -> str:
    """The pre-folded haystack a title/label search matches against.

    Both modes publish one; they disagree only about its name. A row carrying
    neither is folded here rather than refused, because a payload predating the
    field is a row an operator should still be able to find.
    """
    for key in ("search_text", "searchable_text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.casefold()
    parts = [
        plan_label(row),
        plan_title(row),
        str(row.get("plan_dir") or ""),
        str(row.get("primary_state") or ""),
        str(row.get("recommended_action") or ""),
    ]
    return " ".join(part for part in parts if part).casefold()


def project_names(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every project this plan belongs to, in publication order.

    A plan may belong to more than one, which is the whole reason this returns
    a tuple: a workspace registered under two projects appears under both
    headings rather than under whichever one sorted first.
    """
    projects = row.get("projects")
    if isinstance(projects, (list, tuple)):
        names = [str(name).strip() for name in projects if str(name).strip()]
        if names:
            return tuple(dict.fromkeys(names))
    single = row.get("project")
    if isinstance(single, str) and single.strip():
        # The store joins multi-project membership with " + " when it labels a
        # retained run, so split it back apart rather than rendering the joined
        # string as a project nobody registered.
        parts = [part.strip() for part in single.split("+") if part.strip()]
        if parts:
            return tuple(dict.fromkeys(parts))
    return ()


#: Alias kept because "the projects of a plan" reads better at call sites that
#: already have a variable called ``projects``.
plan_projects = project_names

#: What a row with no project at all is filed under. A heading rather than a
#: silent omission: a plan outside every registered project is still a plan.
UNGROUPED_PROJECT = "(no project)"


def project_group(row: Mapping[str, Any]) -> str:
    """The single heading a row is filed under.

    Membership is plural (:func:`project_names`) and grouping is singular, and
    the two answer different questions. Filtering by ``alpha`` finds a plan
    registered under both ``alpha`` and ``beta``; the heading it appears under
    is still ``alpha + beta``, because a row drawn once under each of its
    projects would be one plan an operator could archive twice.
    """
    published = row.get("project_group")
    if isinstance(published, str) and published.strip():
        return published.strip()
    names = project_names(row)
    if not names:
        return UNGROUPED_PROJECT
    return " + ".join(names)


def harness_of(row: Mapping[str, Any]) -> str:
    """The harness a row names, or an empty string when it names none."""
    value = row.get("harness")
    return value.strip() if isinstance(value, str) and value.strip() else ""


def observation_status(row: Mapping[str, Any]) -> str:
    """Whether this row is live information or a retained observation."""
    value = row.get("observation_status")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return OBSERVATION_CURRENT


def primary_state(row: Mapping[str, Any]) -> str:
    value = row.get("primary_state")
    return str(value) if isinstance(value, str) and value else "unknown"


def urgency_group(row: Mapping[str, Any]) -> str:
    value = row.get("urgency_group")
    return str(value) if isinstance(value, str) and value else ""


# ---------------------------------------------------------------------------
# What a page may contain
# ---------------------------------------------------------------------------


def allowed_states(group: str, *, include_completed: bool) -> tuple[str, ...]:
    """The primary states a page may hold, given the chip and the toggle.

    One derivation, two executions: the Mac keeps a list of summaries whose
    state is in here, and the Pi puts the same words in an ``IN`` clause. That
    is the point -- membership decided in two places is membership that can
    disagree, and it disagreed for as long as the browser was the only thing
    applying the show-completed rule.

    Picking the ``complete`` chip is itself the request to see completed plans,
    so the toggle does not also have to be on.
    """
    if group not in FILTER_GROUPS:
        raise OverviewError(f"unknown filter group {group!r}")
    states = FILTER_GROUPS[group]
    if include_completed or group == "complete":
        return tuple(states)
    return tuple(state for state in states if state not in COMPLETED_STATES)


def counts_from_states(by_state: Mapping[str, int]) -> dict[str, int]:
    """Chip counts from a histogram of primary states.

    The histogram must be taken over the *complete* set that matched project,
    title and harness, and must not have had the chip applied to it: a chip
    count is "how many plans this chip would show", so a count narrowed by the
    chip that is currently pressed would report the page back to itself.
    """
    counts = {name: 0 for name in COUNT_NAMES}
    for state, total in by_state.items():
        amount = int(total)
        for name, states in FILTER_GROUPS.items():
            if str(state) in states:
                counts[name] += amount
    return counts


def histogram(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """A primary-state histogram of every row given."""
    tally: dict[str, int] = {}
    for row in rows:
        state = primary_state(row)
        tally[state] = tally.get(state, 0) + 1
    return tally


# ---------------------------------------------------------------------------
# The filter set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueFilters:
    """Everything one queue request narrows by, parsed once.

    ``group`` and ``include_completed`` are kept apart from ``states`` on
    purpose: the chip is what the operator pressed, and the state set is what
    that press means. The first is what a URL round-trips and the second is
    what a query executes.
    """

    project: str | None = None
    search: str | None = None
    harness: str | None = None
    group: str = FILTER_ALL
    # Defaults to *not narrowing*, like every other field here: a request that
    # names no filter gets the whole queue. Hiding completed work is the
    # browser's opinion about a default view, so the browser asks for it with
    # `include_completed=0` rather than the API assuming it -- otherwise a bare
    # `GET /api/queue` would quietly answer a different question than it looks
    # like it is asking.
    include_completed: bool = True
    sort: str = DEFAULT_SORT_MODE
    offset: int = 0
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        if self.group not in FILTER_GROUPS:
            raise OverviewError(f"unknown filter group {self.group!r}")
        if self.sort not in SORT_MODES:
            raise OverviewError(f"unknown sort mode {self.sort!r}")
        if self.offset < 0:
            raise OverviewError("offset must be non-negative")
        if self.limit < 0 or self.limit > MAX_PAGE_LIMIT:
            raise OverviewError(f"limit must be between 0 and {MAX_PAGE_LIMIT}")

    @property
    def states(self) -> tuple[str, ...]:
        """The primary states this request's page may contain."""
        return allowed_states(self.group, include_completed=self.include_completed)

    @property
    def narrowed(self) -> bool:
        """Whether anything but the chip is hiding rows.

        What an empty result has to explain. "No plans match *scripts*" and
        "there are no plans" are different sentences, and only the first one
        comes with something to undo.
        """
        return bool(self.project or self.search or self.harness)

    def as_document(self) -> dict[str, Any]:
        """The filter set as answered back, so a client can confirm its request."""
        return {
            "project": self.project,
            "q": self.search,
            "harness": self.harness,
            "filter": self.group,
            "include_completed": self.include_completed,
            "sort": self.sort,
            "offset": self.offset,
            "limit": self.limit,
        }


def parse_filters(
    call: Call, *, default_limit: int = DEFAULT_PAGE_LIMIT
) -> QueueFilters:
    """Read one queue request's filters, refusing a word neither mode knows.

    Parsed here rather than in each provider so ``sort=urgent`` is refused with
    the same sentence whichever machine is answering, and so a filter added to
    the URL vocabulary reaches both modes or neither.
    """
    group = call.optional_query("filter") or FILTER_ALL
    sort = call.optional_query("sort") or DEFAULT_SORT_MODE
    if group not in FILTER_GROUPS:
        raise invalid_request(
            "unknown filter group; expected one of " + ", ".join(COUNT_NAMES),
            filter=group,
        )
    if sort not in SORT_MODES:
        raise invalid_request(
            "unknown sort mode; expected one of " + ", ".join(SORT_MODES), sort=sort
        )
    completed = call.optional_query("include_completed")
    if completed is not None and completed not in {"0", "1"}:
        raise invalid_request(
            "include_completed must be 0 or 1", include_completed=completed
        )
    return QueueFilters(
        project=call.optional_query("project"),
        search=call.optional_query("q"),
        harness=call.optional_query("harness"),
        group=group,
        include_completed=completed != "0",
        sort=sort,
        offset=call.non_negative_int("offset", default=0),
        limit=call.bounded_int("limit", default=default_limit, maximum=MAX_PAGE_LIMIT),
    )


def matches(row: Mapping[str, Any], filters: QueueFilters, *, states: bool) -> bool:
    """Whether one row survives ``filters``.

    ``states`` says whether the chip is part of the question. It is not, when
    the caller is building the histogram the chip counts come from.
    """
    if filters.project is not None and filters.project not in project_names(row):
        return False
    if filters.harness is not None and harness_of(row) != filters.harness:
        return False
    if filters.search and filters.search.casefold() not in plan_search_text(row):
        return False
    if states and primary_state(row) not in filters.states:
        return False
    # The Pi publishes an urgency group for rows whose primary state is
    # something else; honouring it here keeps "hide completed" meaning the same
    # thing in both modes.
    hidden_as_complete = (
        states
        and not filters.include_completed
        and filters.group != COMPLETED_GROUP
        and urgency_group(row) == COMPLETED_GROUP
    )
    return not hidden_as_complete


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def _activity_epoch(value: Any) -> float | None:
    """One activity timestamp as a number, accepting either publication.

    The Mac publishes ISO-8601 strings and the Pi publishes epoch seconds.
    Neither is converted at the boundary because both are the honest shape for
    the store behind them, so the comparison absorbs the difference.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def activity_at(row: Mapping[str, Any]) -> float | None:
    """When this row last moved, by whichever field its mode filled in."""
    for key in ("last_activity_at", "run_started_at", "last_observed_at"):
        moment = _activity_epoch(row.get(key))
        if moment is not None:
            return moment
    return None


def _urgency_rank(row: Mapping[str, Any]) -> int:
    rank = row.get("urgency_rank")
    if isinstance(rank, bool) or not isinstance(rank, int):
        return 990
    return rank


def _sort_key(row: Mapping[str, Any], mode: str) -> tuple[Any, ...]:
    label = plan_label(row).casefold()
    path = str(row.get("plan_dir") or "").casefold()
    rank = _urgency_rank(row)
    moment = activity_at(row)
    if mode == "label":
        return (label, rank)
    if mode == "path":
        return (path, rank, label)
    if mode == "recent":
        # Newest first, and a row with no timestamp sorts last rather than
        # pretending to be infinitely old.
        return (0 if moment is not None else 1, -(moment or 0.0), rank, label)
    if mode == "oldest":
        # Oldest first, and a row with no timestamp sorts *first*: a plan that
        # never ran is the strongest cleanup candidate there is.
        return (0 if moment is None else 1, moment or 0.0, rank, label)
    return (rank, 0 if moment is not None else 1, -(moment or 0.0), label)


def sorted_rows(
    rows: Iterable[Mapping[str, Any]], mode: str = DEFAULT_SORT_MODE
) -> list[dict[str, Any]]:
    """Order rows by one of :data:`SORT_MODES`, newest-and-most-urgent first."""
    if mode not in SORT_MODES:
        raise OverviewError(f"unknown sort mode {mode!r}")
    return [dict(row) for row in sorted(rows, key=lambda row: _sort_key(row, mode))]


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueuePage:
    """One page, and the shape of the set it was cut from.

    ``matched`` is the size of the complete filtered result set and ``total``
    the size of the catalog behind it. Both are reported because an operator
    reading "12 of 340" is being told two different things, and a page that
    only knew its own length could tell them neither.
    """

    rows: tuple[dict[str, Any], ...]
    matched: int
    total: int
    offset: int
    limit: int
    counts: Mapping[str, int]
    projects: tuple[str, ...]
    harnesses: tuple[str, ...]

    @property
    def truncated(self) -> bool:
        return self.offset + len(self.rows) < self.matched

    def as_document(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "projects": list(self.projects),
            "harnesses": list(self.harnesses),
            "catalog_matched": self.matched,
            "catalog_total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "truncated": self.truncated,
        }


def paginate(
    rows: Sequence[Mapping[str, Any]], *, offset: int, limit: int
) -> tuple[dict[str, Any], ...]:
    """The requested window of an already-filtered, already-sorted list."""
    if limit <= 0:
        return ()
    return tuple(dict(row) for row in rows[offset : offset + limit])


def select(rows: Iterable[Mapping[str, Any]], filters: QueueFilters) -> QueuePage:
    """Filter, count, sort and page one in-memory catalog, in that order.

    The order is the whole point. Counting before the page is what makes a chip
    report the corpus; paging after the sort is what makes the first page the
    first page of the requested order rather than of the enumeration's.

    The facets -- which projects and which harnesses exist -- are taken from
    the unfiltered catalog on purpose. A project list narrowed by the project
    already chosen is a list an operator cannot leave.
    """
    catalog = [dict(row) for row in rows]
    unnarrowed = [row for row in catalog if matches(row, filters, states=False)]
    counts = counts_from_states(histogram(unnarrowed))
    survivors = [row for row in unnarrowed if matches(row, filters, states=True)]
    ordered = sorted_rows(survivors, filters.sort)
    return QueuePage(
        rows=paginate(ordered, offset=filters.offset, limit=filters.limit),
        matched=len(ordered),
        total=len(catalog),
        offset=filters.offset,
        limit=filters.limit,
        counts=counts,
        projects=facet_projects(catalog),
        harnesses=facet_harnesses(catalog),
    )


def facet_projects(rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every project name in the catalog, sorted, each named once."""
    names: set[str] = set()
    for row in rows:
        names.update(project_names(row))
    return tuple(sorted(names))


def facet_harnesses(rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every harness the catalog names, sorted."""
    names = {harness_of(row) for row in rows}
    return tuple(sorted(name for name in names if name))


# ---------------------------------------------------------------------------
# The inactive-work window
# ---------------------------------------------------------------------------


def recently_active(
    row: Mapping[str, Any],
    *,
    now: float,
    window: float = ACTIVE_WINDOW_SECONDS,
) -> bool:
    """Whether this row still belongs in the operator's current view.

    Two ways to qualify, and the first does not consult the clock at all: a run
    the newest observation calls running or verifying is active because it was
    seen to be, and a clock skew between the Mac and the Pi must not be able to
    age it out. Everything else qualifies on recency, measured against the
    moment the answer was observed rather than against the browser's clock --
    a phone with a wrong clock would otherwise show a different queue.

    A row that has never moved qualifies too, because the window's question is
    "has this gone quiet", and a plan that has never been run has not gone
    quiet -- it is queued work. Filing it with the inactive runs would hide the
    newest plan on the machine behind a toggle the operator has to know about.
    """
    if primary_state(row) in ACTIVE_STATES:
        return True
    moment = activity_at(row)
    if moment is None:
        return True
    return moment >= now - max(0.0, window)
