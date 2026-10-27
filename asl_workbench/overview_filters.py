"""What the queue is narrowed by and counted over -- said once.

The Companion pushed project, title and harness into SQL and paged what
survived; the Workbench fetched everything and filtered it in the browser. The
second is only correct while the catalog is small: filtering after a read limit
describes the first page rather than the corpus, and a count taken from that
page is a count of what happened to be fetched.

This module is deliberately *not* an executor. It derives the two things that
must not be re-derived per mode:

* :func:`allowed_states` turns the chip and the show-completed toggle into the
  set of primary states a page may contain. The Mac filters a list with it and
  the Pi puts it in a ``WHERE`` clause; neither invents membership.
* :func:`counts_from_states` turns a histogram of primary states into the chip
  counts. The histogram is taken over everything that matched project, title
  and harness -- the complete result set -- and never over the page, so the
  number on a chip is the number of plans that chip would show.

Portable: standard library only, no import from :mod:`agent_session_loop`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .http import Call, invalid_request
from .overview_vocabulary import (
    COMPLETED_GROUP,
    COMPLETED_STATES,
    COUNT_NAMES,
    DEFAULT_PAGE_LIMIT,
    DEFAULT_SORT_MODE,
    FILTER_ALL,
    FILTER_GROUPS,
    MAX_PAGE_LIMIT,
    SORT_MODES,
    OverviewError,
    harness_of,
    plan_search_text,
    primary_state,
    project_names,
    urgency_group,
)

__all__ = [
    "QueueFilters",
    "allowed_states",
    "counts_from_states",
    "histogram",
    "matches",
    "parse_filters",
]


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
        if self.limit < 1 or self.limit > MAX_PAGE_LIMIT:
            raise OverviewError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")

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
    limit = call.bounded_int("limit", default=default_limit, maximum=MAX_PAGE_LIMIT)
    # An empty page is not a page size: the Mac answered it with nothing and the
    # Pi's store with one row, so the two modes disagreed about a request
    # neither front end sends. Refused, like the detail reads refuse it.
    if limit < 1:
        raise invalid_request("query parameter 'limit' must be at least 1", limit=limit)
    return QueueFilters(
        project=call.optional_query("project"),
        search=call.optional_query("q"),
        harness=call.optional_query("harness"),
        group=group,
        include_completed=completed != "0",
        sort=sort,
        offset=call.non_negative_int("offset", default=0),
        limit=limit,
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
