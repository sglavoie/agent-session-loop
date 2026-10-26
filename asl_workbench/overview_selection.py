"""How the queue is ordered, cut into pages, and windowed by recency.

The filter set lives in :mod:`asl_workbench.overview_filters` and the shared
vocabulary in :mod:`asl_workbench.overview_vocabulary`; this module orders what
survived, pages it, and decides what "still active" means.

Portable: standard library only, no import from :mod:`agent_session_loop`, and
no clock of its own -- the moment is always an argument.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .overview_filters import QueueFilters, counts_from_states, histogram, matches
from .overview_vocabulary import (
    ACTIVE_STATES,
    ACTIVE_WINDOW_SECONDS,
    DEFAULT_SORT_MODE,
    SORT_MODES,
    OverviewError,
    harness_of,
    plan_label,
    primary_state,
    project_names,
)
from .timestamps import iso_epoch

__all__ = [
    "QueuePage",
    "paginate",
    "recently_active",
    "select",
    "sorted_rows",
]


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
        # NaN compares false against everything and an infinity outranks every
        # real moment, so either would scramble the sort and the page cursor.
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        return number if math.isfinite(number) else None
    return iso_epoch(text)


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
