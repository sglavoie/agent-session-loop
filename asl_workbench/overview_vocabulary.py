"""The overview's shared vocabulary, and the readers that absorb a row's mode.

Both cockpits used to answer "which plans, in what order, and how many" in
their own way. This module declares the vocabulary -- which chip covers which
primary state, which sort modes exist, how wide the inactive-work window is --
and the readers that take one row, whichever mode published it. The filter set
lives in :mod:`asl_workbench.overview_filters` and the ordering, paging and
window in :mod:`asl_workbench.overview_selection`.

The window and the section vocabulary live here because the front end decides
where a row is drawn, and it may not decide what "still active" means: a row
the Pi calls in-progress and the Mac calls older would be two applications
wearing one name.

Portable: standard library only, no import from :mod:`agent_session_loop`, and
no clock of its own -- the moment is always an argument.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
    "OverviewError",
    "harness_of",
    "plan_projects",
    "plan_search_text",
    "plan_title",
    "project_group",
    "project_names",
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
