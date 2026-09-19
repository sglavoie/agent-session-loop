"""The overview: one queue, one history, and the same answers in both modes.

Session 16 folds the Companion's cockpit into the Workbench's queue. The claim
being tested is not "the overview renders" -- it is that *two deployments show
one body of work*, and that everything which decides membership happens before
anything that discards rows.

Four kinds of check, in the order they build on each other:

* **One vocabulary.** ``asl_workbench.overview`` and
  ``static/modules/constants.js`` hold the same words twice, because the server
  filters with them and the browser sections with them and neither imports the
  other. Every mirrored name is pinned against its twin here, so a word added
  on one side fails rather than quietly meaning nothing on the other.

* **Order of operations.** Filter, then count, then sort, then cut. A count
  taken after the page reports the page back to itself, and a page cut before
  the filter is the first N rows of the corpus with the narrowing still to
  come. Both are exercised over a catalog larger than one page, because that
  is the only size at which either mistake is visible.

* **Parity.** The same queue and the same history, read from the local
  provider over real plan directories and from the remote provider over an
  observation of those same directories. The fixture seeds the remote store
  from the *local* import (see ``workbench_support.seed_shell_history``), so a
  disagreement here is a disagreement between the providers rather than
  between two hand-written corpora.

* **Difference that is supposed to exist.** Retained observations. Only the Pi
  can hold a row the newest observation did not confirm, and it has to say so
  rather than present it as live -- so those cases are asserted against the
  remote deployment alone, which is the honest shape of the claim.

The browser half of session 16 lives in ``tests/workbench_browser/overview.spec.js``.
This module is what runs where no browser is installed.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from asl_workbench import catalog as workbench_catalog
from asl_workbench import history as portable_history
from asl_workbench import http as workbench_http
from asl_workbench import observation, overview
from tests import workbench_support
from tests.workbench_support import (
    SHELL_FROZEN_NOW,
    SHELL_QUIET_SECONDS,
    ShellDeployment,
)

CONSTANTS_JS = Path(workbench_http.STATIC_ROOT) / "modules" / "constants.js"
OVERVIEW_JS = Path(workbench_http.STATIC_ROOT) / "modules" / "overview.js"

#: A catalog comfortably past one page, so ``offset``/``limit`` and the counts
#: are exercised on a set that a single read cannot return.
LARGE_CATALOG_SIZE = 240


def _js_source() -> str:
    return CONSTANTS_JS.read_text(encoding="utf-8")


def _js_array(name: str, source: str) -> list[str]:
    """The string members of one ``export const NAME = [...]`` array.

    Read out of the source rather than out of a JavaScript runtime, because
    this suite has to pass on a machine with no Node installed -- the browser
    harness is the dev-only half, and a parity check that only ran there would
    not run in CI at all.

    Members named by identifier rather than spelled out are resolved through
    the constant they name, so ``OVERVIEW_SECTIONS`` reads as the six words it
    renders rather than as the six symbols it is written with.
    """
    match = re.search(
        r"export const " + re.escape(name) + r"\s*=\s*\[(.*?)\];", source, re.S
    )
    if match is None:
        raise AssertionError(f"constants.js has no exported array named {name}")
    members: list[str] = []
    for line in match.group(1).split(","):
        text = line.strip()
        if not text:
            continue
        literal = re.fullmatch(r'"([^"]*)"', text)
        if literal is not None:
            members.append(literal.group(1))
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*", text):
            members.append(_js_string(text, source))
    return members


def _js_object(name: str, source: str) -> dict[str, list[str]]:
    """One ``export const NAME = {...}`` object of string arrays."""
    match = re.search(
        r"export const " + re.escape(name) + r"\s*=\s*\{(.*?)\n\};", source, re.S
    )
    if match is None:
        raise AssertionError(f"constants.js has no exported object named {name}")
    found: dict[str, list[str]] = {}
    for line in match.group(1).splitlines():
        entry = re.match(r'\s*"?([A-Za-z_][\w-]*)"?:\s*(.*?),?\s*$', line)
        if entry is None:
            continue
        key, value = entry.group(1), entry.group(2)
        if value.startswith("["):
            found[key] = re.findall(r'"([^"]*)"', value)
        elif value.startswith("PRIMARY_STATES"):
            found[key] = _js_array("PRIMARY_STATES", source)
    return found


def _js_string(name: str, source: str) -> str:
    match = re.search(r"export const " + re.escape(name) + r'\s*=\s*"([^"]*)";', source)
    if match is None:
        raise AssertionError(f"constants.js has no exported string named {name}")
    return match.group(1)


def _js_number(name: str, source: str) -> float:
    match = re.search(
        r"export const " + re.escape(name) + r"\s*=\s*([0-9*\s]+);", source
    )
    if match is None:
        raise AssertionError(f"constants.js has no exported number named {name}")
    total = 1.0
    for factor in match.group(1).split("*"):
        total *= float(factor.strip())
    return total


def _call(target: str) -> workbench_http.Call:
    """One parsed queue request, without a socket under it.

    ``parse_filters`` is the only place either provider reads the URL, so the
    refusals below are the refusals both modes give -- which is why they are
    asserted once here rather than twice through two servers.
    """
    return workbench_http.Call(
        route=workbench_http.ROUTES_BY_KEY[("GET", "/api/queue")],
        request=workbench_http.Request.build("GET", target),
        principal=workbench_http.Principal(role=workbench_http.ROLE_OWNER),
        body={},
    )


def _row(**fields: Any) -> dict[str, Any]:
    """One catalog row with the fields every reader of one needs."""
    base: dict[str, Any] = {
        "plan_key": fields.get("plan_key", "0" * 16),
        "title": "Plan",
        "primary_state": "ready",
        "urgency_group": "ready",
        "urgency_rank": 70,
        "projects": ["alpha"],
    }
    base.update(fields)
    return base


def _request(deployment: ShellDeployment, path: str) -> tuple[int, dict[str, Any]]:
    """One GET against one deployment, with the credentials that mode wants."""
    request = urllib.request.Request(
        deployment.url(path), headers=dict(deployment.headers), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as error:
        # `HTTPError` is the response object and holds the socket until closed;
        # left to the collector it surfaces as a ResourceWarning blamed on
        # whatever ran next.
        with error:
            raw = error.read()
            status = int(error.code)
    parsed = json.loads(raw) if raw else {}
    return status, parsed if isinstance(parsed, dict) else {}


class SharedVocabularyTests(unittest.TestCase):
    """The words the server filters by and the browser sections by, pinned.

    Two copies exist on purpose: ``asl_workbench`` may not import a browser and
    a browser may not import Python. What must not exist is two copies that
    have drifted, because every one of these names is a join between a query
    the server ran and a heading the browser drew.
    """

    source: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _js_source()

    def test_primary_states_are_the_same_list_in_the_same_order(self) -> None:
        self.assertEqual(
            list(overview.PRIMARY_STATES), _js_array("PRIMARY_STATES", self.source)
        )

    def test_every_filter_chip_covers_the_same_states_in_both_copies(self) -> None:
        published = _js_object("FILTER_GROUPS", self.source)
        self.assertEqual(sorted(overview.FILTER_GROUPS), sorted(published))
        for name, states in overview.FILTER_GROUPS.items():
            self.assertEqual(
                list(states),
                published[name],
                f"chip {name!r} covers different states on the two sides",
            )

    def test_sort_modes_match_and_every_mode_has_a_label(self) -> None:
        modes = _js_array("SORT_MODES", self.source)
        self.assertEqual(list(overview.SORT_MODES), modes)
        labels = re.search(
            r"export const SORT_MODE_LABELS\s*=\s*\{(.*?)\n\};", self.source, re.S
        )
        self.assertIsNotNone(labels)
        assert labels is not None
        named = re.findall(r"^\s*([A-Za-z]+):", labels.group(1), re.M)
        self.assertEqual(sorted(modes), sorted(named))

    def test_sections_match_and_each_one_has_a_heading_and_an_empty_line(
        self,
    ) -> None:
        sections = _js_array("OVERVIEW_SECTIONS", self.source)
        self.assertEqual(list(overview.OVERVIEW_SECTIONS), sections)
        for name in ("SECTION_LABELS", "SECTION_EMPTY_TEXT"):
            block = re.search(
                r"export const " + name + r"\s*=\s*\{(.*?)\n\};", self.source, re.S
            )
            self.assertIsNotNone(block, f"constants.js has no {name}")
            assert block is not None
            keys = re.findall(r'^\s*"?([\w-]+)"?:', block.group(1), re.M)
            self.assertEqual(sorted(sections), sorted(keys), name)

    def test_the_ungrouped_project_heading_is_one_string(self) -> None:
        self.assertEqual(
            overview.UNGROUPED_PROJECT, _js_string("UNGROUPED_PROJECT", self.source)
        )

    def test_the_observation_vocabulary_matches_and_every_word_reads(self) -> None:
        for name, value in (
            ("OBSERVATION_CURRENT", overview.OBSERVATION_CURRENT),
            ("OBSERVATION_UNCONFIRMED", overview.OBSERVATION_UNCONFIRMED),
            ("OBSERVATION_ABSENT", overview.OBSERVATION_ABSENT),
        ):
            self.assertEqual(value, _js_string(name, self.source))
        labels = re.search(
            r"export const OBSERVATION_LABELS\s*=\s*\{(.*?)\n\};", self.source, re.S
        )
        self.assertIsNotNone(labels)
        assert labels is not None
        named = re.findall(r"^\s*([a-z]+):", labels.group(1), re.M)
        self.assertEqual(
            sorted(
                (
                    overview.OBSERVATION_CURRENT,
                    overview.OBSERVATION_UNCONFIRMED,
                    overview.OBSERVATION_ABSENT,
                )
            ),
            sorted(named),
        )

    def test_the_inactive_work_window_is_eight_hours_on_both_sides(self) -> None:
        self.assertEqual(8 * 60 * 60, overview.ACTIVE_WINDOW_SECONDS)
        self.assertEqual(
            float(overview.ACTIVE_WINDOW_SECONDS),
            _js_number("ACTIVE_WINDOW_SECONDS", self.source),
        )

    def test_the_page_limit_the_browser_asks_for_is_one_the_server_allows(
        self,
    ) -> None:
        for name in ("QUEUE_PAGE_LIMIT", "HISTORY_PAGE_LIMIT"):
            asked = _js_number(name, self.source)
            self.assertEqual(float(overview.DEFAULT_PAGE_LIMIT), asked, name)
            self.assertLessEqual(asked, float(overview.MAX_PAGE_LIMIT), name)

    def test_history_outcomes_match_the_importer_that_produces_them(self) -> None:
        self.assertEqual(
            list(portable_history.HISTORY_OUTCOMES),
            _js_array("HISTORY_OUTCOMES", self.source),
        )

    def test_the_states_the_window_never_ages_out_are_the_same_two(self) -> None:
        source = OVERVIEW_JS.read_text(encoding="utf-8")
        match = re.search(r"const ACTIVE_STATES\s*=\s*\[(.*?)\];", source, re.S)
        self.assertIsNotNone(match, "overview.js has no ACTIVE_STATES")
        assert match is not None
        self.assertEqual(
            sorted(overview.ACTIVE_STATES),
            sorted(re.findall(r'"([^"]*)"', match.group(1))),
        )

    def test_the_window_keeps_both_of_its_escapes_in_the_mirror(self) -> None:
        # Not a string comparison -- a behaviour one. A row that is running,
        # and a row that has never moved, are the two cases where the window
        # must not consult the clock at all, and the mirror has to hold both or
        # the two deployments draw different sections from one payload.
        source = OVERVIEW_JS.read_text(encoding="utf-8")
        body = source.split("export function recentlyActive", 1)[1].split("\n}", 1)[0]
        self.assertIn("ACTIVE_STATES", body)
        self.assertIn("=== null) return true", body)

    def test_the_sections_are_the_same_six_in_the_same_render_order(self) -> None:
        source = OVERVIEW_JS.read_text(encoding="utf-8")
        for section in overview.OVERVIEW_SECTIONS:
            self.assertIn(
                section,
                _js_array("OVERVIEW_SECTIONS", self.source),
                f"section {section!r} is not rendered",
            )
        # The windowed section is the one the toggle moves rows out of, and
        # `sectionFor` has to reach it only through `recentlyActive`.
        self.assertEqual({overview.SECTION_RECENT}, set(overview.WINDOWED_SECTIONS))
        self.assertIn("recentlyActive(plan, nowSeconds)) return SECTION_RECENT", source)


class InactiveWorkWindowTests(unittest.TestCase):
    """Eight hours, measured against the observation rather than the browser."""

    def setUp(self) -> None:
        self.now = 1_800_000_000.0

    def test_a_running_row_is_current_however_old_its_last_movement(self) -> None:
        for state in sorted(overview.ACTIVE_STATES):
            row = _row(primary_state=state, last_activity_at=self.now - 400_000)
            self.assertTrue(
                overview.recently_active(row, now=self.now),
                f"{state} was aged out by the clock",
            )

    def test_a_row_that_has_never_moved_is_queued_work_not_old_work(self) -> None:
        # The window's question is "has this gone quiet", and a plan that has
        # never been run has not gone quiet. Filing it with the inactive runs
        # would hide the newest plan on the machine behind a toggle.
        self.assertTrue(overview.recently_active(_row(), now=self.now))

    def test_the_boundary_is_inclusive_and_the_far_side_is_not(self) -> None:
        window = overview.ACTIVE_WINDOW_SECONDS
        edge = _row(last_activity_at=self.now - window)
        past = _row(last_activity_at=self.now - window - 1)
        self.assertTrue(overview.recently_active(edge, now=self.now))
        self.assertFalse(overview.recently_active(past, now=self.now))

    def test_both_publications_of_a_moment_are_read_the_same_way(self) -> None:
        moment = self.now - 2 * overview.ACTIVE_WINDOW_SECONDS
        as_epoch = _row(last_activity_at=moment)
        as_iso = _row(last_activity_at=workbench_support.shell_iso(moment))
        self.assertEqual(overview.activity_at(as_epoch), overview.activity_at(as_iso))
        self.assertFalse(overview.recently_active(as_iso, now=self.now))

    def test_a_number_of_seconds_spelled_as_text_is_still_a_moment(self) -> None:
        # Not a courtesy to sloppy callers: the Pi's catalog_entry column for
        # this field is TEXT, so an epoch published as a float comes back out
        # of SQLite as "1789466400.0". A reader that only knew ISO-8601 and
        # numbers would call the row never-moved and hand a plan that has been
        # quiet for thirty hours back to the top of the queue.
        moment = self.now - 2 * overview.ACTIVE_WINDOW_SECONDS
        as_text = _row(last_activity_at=str(moment))
        self.assertEqual(moment, overview.activity_at(as_text))
        self.assertFalse(overview.recently_active(as_text, now=self.now))

    def test_a_quiet_row_falls_back_to_when_it_was_last_observed(self) -> None:
        # The Pi's rows carry no activity of their own until one is published,
        # and "when I last saw this" is the only honest stand-in. It has to be
        # read, or every retained row would count as never-moved and current.
        row = _row(last_observed_at=self.now - 400_000)
        self.assertEqual(self.now - 400_000, overview.activity_at(row))
        self.assertFalse(overview.recently_active(row, now=self.now))


class PlanNameTests(unittest.TestCase):
    """What a row is called, read the one way both modes can answer."""

    def test_each_mode_is_read_under_the_name_it_publishes(self) -> None:
        self.assertEqual("Pi", overview.plan_label({"display_label": "Pi"}))
        self.assertEqual("Mac", overview.plan_label({"label": "Mac"}))

    def test_a_row_with_no_explicit_label_is_named_by_its_title(self) -> None:
        # The Mac publishes `label: null` for a plan nobody renamed, so this is
        # the common case and not the edge one. Stopping before the title would
        # make `sort=label` order the entire local queue under one empty
        # string, which reads as "the sort did nothing".
        row = {"label": None, "display_label": None, "title": "Charlie"}
        self.assertEqual("Charlie", overview.plan_label(row))

    def test_label_sort_orders_local_style_rows_by_their_titles(self) -> None:
        rows = [
            {"label": None, "title": title} for title in ("Charlie", "Alpha", "Bravo")
        ]
        self.assertEqual(
            ["Alpha", "Bravo", "Charlie"],
            [row["title"] for row in overview.sorted_rows(rows, "label")],
        )

    def test_a_row_naming_nothing_is_empty_rather_than_invented(self) -> None:
        self.assertEqual("", overview.plan_label({"plan_key": "abc"}))


class ProjectMembershipTests(unittest.TestCase):
    """A plan in two projects is found by either and drawn once."""

    def test_a_multi_project_plan_is_headed_by_every_project_it_is_in(
        self,
    ) -> None:
        row = _row(projects=["scripts", "notes"])
        # Publication order, not sorted: the registry's order is the order the
        # operator wrote, and re-sorting it here would put a plan's incidental
        # second project ahead of the one it was registered under.
        self.assertEqual(("scripts", "notes"), overview.project_names(row))
        # One heading, not two rows: the plan is one plan with one archive
        # button, and splitting it would offer the operator two of them.
        self.assertEqual("scripts + notes", overview.project_group(row))

    def test_membership_matches_on_any_one_project(self) -> None:
        row = _row(projects=["scripts", "notes"])
        for name in ("scripts", "notes"):
            filters = overview.QueueFilters(project=name)
            self.assertTrue(
                overview.matches(row, filters, states=True),
                f"a plan in {name} was not found by it",
            )
        self.assertFalse(
            overview.matches(
                row, overview.QueueFilters(project="elsewhere"), states=True
            )
        )

    def test_a_plan_with_no_project_is_headed_rather_than_dropped(self) -> None:
        row = _row(projects=[])
        self.assertEqual(overview.UNGROUPED_PROJECT, overview.project_group(row))

    def test_the_project_facet_names_every_project_once(self) -> None:
        rows = [
            _row(plan_key="a", projects=["scripts", "notes"]),
            _row(plan_key="b", projects=["scripts"]),
            _row(plan_key="c", projects=[]),
        ]
        self.assertEqual(("notes", "scripts"), overview.facet_projects(rows))


class SelectionOrderTests(unittest.TestCase):
    """Filter, count, sort, cut -- and each one provably before the next."""

    def setUp(self) -> None:
        self.rows: list[dict[str, Any]] = []
        for index in range(LARGE_CATALOG_SIZE):
            state = "complete" if index % 6 == 0 else "ready"
            self.rows.append(
                _row(
                    plan_key=f"{index:016d}",
                    title=f"Plan {index:03d}",
                    primary_state=state,
                    urgency_group="complete" if state == "complete" else "ready",
                    urgency_rank=index,
                    projects=["scripts"] if index % 2 else ["notes"],
                    harness="claude" if index % 3 else "codex",
                )
            )

    def test_a_catalog_larger_than_one_page_reports_both_sizes(self) -> None:
        page = overview.select(self.rows, overview.QueueFilters())
        self.assertEqual(overview.DEFAULT_PAGE_LIMIT, len(page.rows))
        self.assertEqual(LARGE_CATALOG_SIZE, page.matched)
        self.assertEqual(LARGE_CATALOG_SIZE, page.total)
        self.assertTrue(page.truncated)

    def test_the_filter_runs_before_the_page_rather_than_over_it(self) -> None:
        # `notes` holds every even index, so half the corpus. If the page were
        # cut first the filter would then be applied to 200 rows and answer
        # with about a hundred; applied first it answers with all 120.
        filters = overview.QueueFilters(project="notes")
        page = overview.select(self.rows, filters)
        self.assertEqual(LARGE_CATALOG_SIZE // 2, page.matched)
        self.assertEqual(LARGE_CATALOG_SIZE, page.total)
        for row in page.rows:
            self.assertEqual(["notes"], row["projects"])

    def test_counts_are_taken_over_the_matched_set_not_over_the_page(
        self,
    ) -> None:
        page = overview.select(self.rows, overview.QueueFilters())
        complete = sum(1 for row in self.rows if row["primary_state"] == "complete")
        self.assertEqual(complete, page.counts["complete"])
        self.assertEqual(LARGE_CATALOG_SIZE, page.counts["all"])
        # Every chip exceeds the page it was counted beside, which is the whole
        # reason the count cannot be taken from `page.rows`.
        self.assertGreater(page.counts["all"], len(page.rows))

    def test_a_pressed_chip_does_not_narrow_the_counts_it_sits_beside(
        self,
    ) -> None:
        pressed = overview.select(self.rows, overview.QueueFilters(group="complete"))
        unpressed = overview.select(self.rows, overview.QueueFilters())
        # A count narrowed by the chip currently pressed would report the page
        # back to itself and every other chip would read zero.
        self.assertEqual(dict(unpressed.counts), dict(pressed.counts))
        self.assertLess(pressed.matched, unpressed.matched)

    def test_a_narrowing_filter_still_counts_the_states_it_left_behind(
        self,
    ) -> None:
        page = overview.select(self.rows, overview.QueueFilters(project="notes"))
        self.assertEqual(LARGE_CATALOG_SIZE // 2, page.counts["all"])
        self.assertLess(page.counts["all"], LARGE_CATALOG_SIZE)

    def test_paging_walks_the_matched_set_without_repeating_or_skipping(
        self,
    ) -> None:
        seen: list[str] = []
        for offset in range(0, LARGE_CATALOG_SIZE, 100):
            page = overview.select(
                self.rows, overview.QueueFilters(offset=offset, limit=100)
            )
            seen.extend(str(row["plan_key"]) for row in page.rows)
        self.assertEqual(LARGE_CATALOG_SIZE, len(seen))
        self.assertEqual(LARGE_CATALOG_SIZE, len(set(seen)))

    def test_the_page_is_the_first_page_of_the_requested_order(self) -> None:
        page = overview.select(self.rows, overview.QueueFilters(sort="label"))
        labels = [str(row["title"]) for row in page.rows]
        self.assertEqual(sorted(labels), labels)
        self.assertEqual("Plan 000", labels[0])

    def test_facets_are_taken_from_the_catalog_and_not_from_the_filter(
        self,
    ) -> None:
        # A project list narrowed by the project already chosen is a list an
        # operator cannot leave.
        page = overview.select(self.rows, overview.QueueFilters(project="notes"))
        self.assertEqual(("notes", "scripts"), page.projects)
        self.assertEqual(("claude", "codex"), page.harnesses)

    def test_hiding_completed_work_narrows_the_page_but_not_the_catalog(
        self,
    ) -> None:
        page = overview.select(
            self.rows, overview.QueueFilters(include_completed=False)
        )
        self.assertNotIn("complete", {str(row["primary_state"]) for row in page.rows})
        self.assertEqual(LARGE_CATALOG_SIZE, page.total)
        self.assertGreater(page.counts["complete"], 0)

    def test_the_complete_chip_is_itself_the_request_to_see_completed_work(
        self,
    ) -> None:
        states = overview.allowed_states("complete", include_completed=False)
        self.assertEqual(("complete",), states)


class FilterParsingTests(unittest.TestCase):
    """What a URL may say, refused identically for both providers."""

    def test_an_absent_parameter_narrows_nothing(self) -> None:
        filters = overview.parse_filters(_call("/api/queue"))
        self.assertIsNone(filters.project)
        self.assertIsNone(filters.search)
        self.assertIsNone(filters.harness)
        self.assertEqual(overview.FILTER_ALL, filters.group)
        # Including completed work. Hiding it is the browser's opinion about a
        # default view, and a bare `GET /api/queue` that answered a narrowed
        # question would be answering something other than what it looks like.
        self.assertTrue(filters.include_completed)

    def test_the_browser_asks_for_the_default_view_explicitly(self) -> None:
        queue_js = (
            Path(workbench_http.STATIC_ROOT) / "modules" / "queue.js"
        ).read_text(encoding="utf-8")
        self.assertIn('"include_completed",', queue_js)
        self.assertIn('state.showCompleted || state.cleanupMode ? "1" : "0"', queue_js)

    def test_include_completed_takes_a_flag_and_refuses_anything_else(
        self,
    ) -> None:
        self.assertFalse(
            overview.parse_filters(
                _call("/api/queue?include_completed=0")
            ).include_completed
        )
        self.assertTrue(
            overview.parse_filters(
                _call("/api/queue?include_completed=1")
            ).include_completed
        )
        with self.assertRaises(workbench_http.HttpRefusal) as refused:
            overview.parse_filters(_call("/api/queue?include_completed=yes"))
        self.assertEqual(400, refused.exception.status)

    def test_an_unknown_chip_or_sort_mode_is_named_rather_than_ignored(
        self,
    ) -> None:
        for target in ("/api/queue?filter=urgent", "/api/queue?sort=newest"):
            with self.assertRaises(workbench_http.HttpRefusal) as refused:
                overview.parse_filters(_call(target))
            self.assertEqual(400, refused.exception.status)

    def test_a_page_larger_than_the_ceiling_is_refused(self) -> None:
        with self.assertRaises(workbench_http.HttpRefusal):
            overview.parse_filters(
                _call(f"/api/queue?limit={overview.MAX_PAGE_LIMIT + 1}")
            )

    def test_the_filter_set_round_trips_through_the_answer(self) -> None:
        target = (
            "/api/queue?project=scripts&q=alpha&harness=claude&filter=paused"
            "&sort=recent&offset=10&limit=25&include_completed=1"
        )
        document = overview.parse_filters(_call(target)).as_document()
        self.assertEqual(
            {
                "project": "scripts",
                "q": "alpha",
                "harness": "claude",
                "filter": "paused",
                "include_completed": True,
                "sort": "recent",
                "offset": 10,
                "limit": 25,
            },
            document,
        )


class OverviewParityTests(unittest.TestCase):
    """One body of work, read through both providers at once."""

    deployments: workbench_support.ShellDeployments
    _tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="workbench-overview-")
        cls.deployments = workbench_support.start_shell_deployments(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployments.shutdown()
        cls._tmp.cleanup()

    def queue(self, deployment: ShellDeployment, query: str = "") -> dict[str, Any]:
        status, body = _request(deployment, "/api/queue" + query)
        self.assertEqual(200, status, f"{deployment.mode}: {body}")
        self.assertTrue(body.get("ok"), body)
        return body

    def history(self, deployment: ShellDeployment, query: str = "") -> dict[str, Any]:
        status, body = _request(deployment, "/api/history" + query)
        self.assertEqual(200, status, f"{deployment.mode}: {body}")
        self.assertTrue(body.get("ok"), body)
        return body

    def rows_by_key(self, body: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(row["plan_key"]): row for row in body["plans"] if isinstance(row, dict)
        }

    # -- The queue ----------------------------------------------------------

    def test_both_modes_publish_the_same_plans_under_the_same_keys(self) -> None:
        keys = [
            sorted(self.rows_by_key(self.queue(deployment)))
            for deployment in self.deployments.both
        ]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(sorted(self.deployments.corpus.plan_keys.values()), keys[0])

    def test_both_modes_agree_on_state_urgency_and_project(self) -> None:
        views = [
            self.rows_by_key(self.queue(deployment))
            for deployment in self.deployments.both
        ]
        for key, local in views[0].items():
            remote = views[1][key]
            for field in ("primary_state", "urgency_group", "project_group"):
                self.assertEqual(
                    local.get(field),
                    remote.get(field),
                    f"{field} differs for {key}",
                )

    def test_both_modes_publish_the_same_chip_counts(self) -> None:
        counts = [
            self.queue(deployment)["counts"] for deployment in self.deployments.both
        ]
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(len(self.deployments.corpus.plan_dirs), counts[0]["all"])

    def test_every_answer_says_when_it_was_observed_and_how_fresh_it_is(
        self,
    ) -> None:
        # The eight-hour window is measured against this moment rather than the
        # browser's clock: a phone measuring its own idea of now against a
        # laptop's idea of when a run last moved would draw a different queue.
        for deployment in self.deployments.both:
            body = self.queue(deployment)
            self.assertIsInstance(body.get("observed_at"), float)
            self.assertIn(body.get("freshness"), ("live", "stale", "lost"))
            self.assertEqual("complete", body.get("catalog_completeness"))
            for row in body["plans"]:
                # Read through the shared accessor, because the two modes say
                # it in the two honest places: the Pi marks each row, and the
                # Mac -- standing on the plans it is describing -- says it once
                # for the whole answer. One reader absorbs the difference.
                self.assertEqual(
                    overview.OBSERVATION_CURRENT, overview.observation_status(row)
                )

    def test_the_local_mode_says_current_once_for_the_whole_answer(self) -> None:
        body = self.queue(self.deployments.local)
        self.assertEqual(overview.OBSERVATION_CURRENT, body.get("observation_status"))

    def test_the_remote_mode_marks_each_row_it_still_confirms(self) -> None:
        for row in self.queue(self.deployments.remote)["plans"]:
            self.assertEqual(
                overview.OBSERVATION_CURRENT, row.get("observation_status")
            )

    def test_the_quiet_plan_falls_outside_the_window_in_both_modes(self) -> None:
        key = self.deployments.corpus.plan_keys["charlie"]
        for deployment in self.deployments.both:
            body = self.queue(deployment)
            row = self.rows_by_key(body)[key]
            now = float(body["observed_at"])
            self.assertFalse(
                overview.recently_active(row, now=now),
                f"{deployment.mode} still calls charlie current work",
            )
            # And it is still in the page: the window moves a row, it never
            # drops one. An operator can always reach it behind the toggle.
            self.assertEqual("recent", row["primary_state"])

    def test_the_two_modes_spell_that_one_moment_differently(self) -> None:
        # The reason the window has to read three shapes rather than two, kept
        # as a test so the requirement is visible rather than folded into a
        # parser. The Mac writes ISO-8601 and the Pi hands back whatever its
        # TEXT column stored, which for an epoch float is its repr.
        key = self.deployments.corpus.plan_keys["charlie"]
        spellings = {
            deployment.mode: self.rows_by_key(self.queue(deployment))[key][
                "last_activity_at"
            ]
            for deployment in self.deployments.both
        }
        self.assertNotEqual(spellings["local"], spellings["remote"])
        for mode, published in spellings.items():
            self.assertIsInstance(published, str, mode)
            self.assertIsNotNone(overview._activity_epoch(published), mode)
        # Named rather than implied: this one is a number wearing a string.
        self.assertEqual(
            float(spellings["remote"]),
            overview._activity_epoch(spellings["remote"]),
        )

    def test_the_other_plans_stay_current_work_in_both_modes(self) -> None:
        for deployment in self.deployments.both:
            body = self.queue(deployment)
            rows = self.rows_by_key(body)
            now = float(body["observed_at"])
            for name in ("alpha", "bravo"):
                key = self.deployments.corpus.plan_keys[name]
                self.assertTrue(
                    overview.recently_active(rows[key], now=now),
                    f"{deployment.mode} aged out {name}",
                )

    def test_a_title_filter_narrows_both_modes_to_the_same_row(self) -> None:
        for deployment in self.deployments.both:
            body = self.queue(deployment, "?q=charlie")
            self.assertEqual(1, body["catalog_matched"])
            self.assertEqual(
                len(self.deployments.corpus.plan_dirs), body["catalog_total"]
            )
            rows = self.rows_by_key(body)
            self.assertEqual([self.deployments.corpus.plan_keys["charlie"]], list(rows))

    def test_a_project_filter_narrows_both_modes_identically(self) -> None:
        for deployment in self.deployments.both:
            seeded = urllib.parse.quote(workbench_catalog.UNKNOWN_PROJECT)
            body = self.queue(deployment, f"?project={seeded}")
            self.assertEqual(
                len(self.deployments.corpus.plan_dirs), body["catalog_matched"]
            )
            empty = self.queue(deployment, "?project=nothing-is-here")
            self.assertEqual(0, empty["catalog_matched"])
            self.assertEqual([], empty["plans"])

    def test_both_modes_offer_the_same_project_facet(self) -> None:
        facets = [
            self.queue(deployment)["projects"] for deployment in self.deployments.both
        ]
        self.assertEqual(facets[0], facets[1])
        self.assertEqual([workbench_catalog.UNKNOWN_PROJECT], facets[0])

    def test_hiding_completed_work_is_a_request_both_modes_honour(self) -> None:
        for deployment in self.deployments.both:
            body = self.queue(deployment, "?include_completed=0")
            self.assertFalse(body["filters"]["include_completed"])
            self.assertNotIn(
                "complete", {row["primary_state"] for row in body["plans"]}
            )

    def test_both_modes_refuse_the_same_malformed_request(self) -> None:
        for deployment in self.deployments.both:
            for query in (
                "?filter=urgent",
                "?sort=newest",
                "?include_completed=maybe",
            ):
                status, body = _request(deployment, "/api/queue" + query)
                self.assertEqual(400, status, f"{deployment.mode}{query}: {body}")
                self.assertEqual("invalid-request", body.get("reason"))

    def test_a_page_smaller_than_the_catalog_says_it_was_cut(self) -> None:
        for deployment in self.deployments.both:
            body = self.queue(deployment, "?limit=1")
            self.assertEqual(1, len(body["plans"]))
            self.assertEqual(
                len(self.deployments.corpus.plan_dirs), body["catalog_matched"]
            )
            self.assertTrue(body["truncated"])

    # -- The history --------------------------------------------------------

    def test_both_modes_report_the_same_endings_in_the_same_order(self) -> None:
        seen = []
        for deployment in self.deployments.both:
            body = self.history(deployment)
            self.assertTrue(body["complete"])
            seen.append(
                [
                    (entry["outcome"], entry["run_id"], entry["entry_id"])
                    for entry in body["entries"]
                ]
            )
        self.assertEqual(seen[0], seen[1])
        # Newest first, and both endings present: a run that finished while the
        # operator was away is the case a notification link most often names.
        self.assertEqual(["stopped", "failed"], [row[0] for row in seen[0]])

    def test_an_ending_carries_the_facts_a_row_renders_and_no_prose(self) -> None:
        for deployment in self.deployments.both:
            for entry in self.history(deployment)["entries"]:
                for field in (
                    "duration_seconds",
                    "harness",
                    "plan_label",
                    "run_finished_at",
                ):
                    self.assertIsNotNone(entry.get(field), f"{field} missing")
                self.assertLessEqual(set(entry), observation.HISTORY_EXPORT_FIELDS)

    def test_the_outcome_filter_narrows_both_modes_to_the_same_ending(
        self,
    ) -> None:
        for deployment in self.deployments.both:
            body = self.history(deployment, "?outcome=failed")
            self.assertEqual(1, len(body["entries"]))
            self.assertEqual("failed", body["entries"][0]["outcome"])
            self.assertEqual(
                [], self.history(deployment, "?outcome=completed")["entries"]
            )

    def test_history_is_narrowed_by_the_same_words_the_queue_is(self) -> None:
        # A project filter that applied to one pane and not the other would put
        # two different projects on one screen under one heading.
        for deployment in self.deployments.both:
            self.assertEqual(2, len(self.history(deployment, "?q=charlie")["entries"]))
            self.assertEqual([], self.history(deployment, "?q=alpha")["entries"])

    def test_the_endings_belong_to_the_plan_the_queue_shows(self) -> None:
        for deployment in self.deployments.both:
            rows = self.rows_by_key(self.queue(deployment))
            charlie = rows[self.deployments.corpus.plan_keys["charlie"]]
            labels = {
                entry["plan_label"] for entry in self.history(deployment)["entries"]
            }
            # Read through the shared accessor: the Mac publishes `label` and
            # the Pi publishes `display_label`, and the history pane has to be
            # able to say which plan an ending belongs to in either mode.
            self.assertEqual({overview.plan_label(charlie)}, labels)

    def test_the_finished_run_the_queue_names_is_the_newest_ending(self) -> None:
        # The queue's `run_key` is a link target; history's is the same string
        # for the same run, which is what lets one `#run=` link resolve from a
        # notification into either pane.
        for deployment in self.deployments.both:
            rows = self.rows_by_key(self.queue(deployment))
            charlie = rows[self.deployments.corpus.plan_keys["charlie"]]
            newest = self.history(deployment)["entries"][0]
            self.assertEqual(charlie["run_key"], newest["run_key"])


class LargeCatalogTests(unittest.TestCase):
    """Above one page, in the mode that has to answer from a database."""

    deployments: workbench_support.ShellDeployments
    _tmp: tempfile.TemporaryDirectory[str]
    extra: int
    total: int

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="workbench-overview-big-")
        cls.deployments = workbench_support.start_shell_deployments(Path(cls._tmp.name))
        cls.extra = LARGE_CATALOG_SIZE
        entries = workbench_support.shell_catalog_entries(cls.deployments.corpus)
        for index in range(cls.extra):
            entries.append(
                {
                    "plan_key": f"bulk{index:012d}",
                    "run_key": f"bulkrun{index:09d}",
                    "title": f"Bulk {index:03d}",
                    "display_label": f"Bulk {index:03d}",
                    "primary_state": "complete" if index % 5 == 0 else "ready",
                    "recommended_action": "view" if index % 5 == 0 else "run",
                    "urgency_rank": 80 if index % 5 == 0 else 70,
                    "urgency_group": "complete" if index % 5 == 0 else "ready",
                    "activity_label": "Done" if index % 5 == 0 else "Ready to run",
                    "projects": ["bulk"] if index % 2 else ["bulk", "shared"],
                    "project_group": "bulk" if index % 2 else "bulk + shared",
                    "counts": {"sessions": 1},
                    "sources": ["manifest"],
                    "exists": True,
                    "pinned": False,
                    "repository_count": 1,
                    "warning_count": 0,
                    "search_text": f"bulk {index:03d}",
                    "registry_digest": "9d2e59249fe21a3c",
                    "last_activity_at": SHELL_FROZEN_NOW - 60.0,
                }
            )
        cls.total = len(entries)
        # A second generation carrying the whole enlarged catalog, ingested
        # directly rather than through `seed_shell_store`: that helper pairs
        # each entry with a seeded run, and these rows have no runs. Complete
        # and final, so nothing is retired and every row stays confirmed --
        # the retained-observation case is `RetainedObservationTests`, and
        # mixing it in here would make a paging failure look like a staleness
        # one.
        cls.deployments.store.ingest_catalog(
            {
                "protocol": 1,
                "kind": "catalog_page",
                "connector_id": workbench_support.SHELL_CONNECTOR_ID,
                "sent_at": SHELL_FROZEN_NOW + 60.0,
                "payload": {
                    "observed_at": SHELL_FROZEN_NOW + 60.0,
                    "generation": "shell-fixture-bulk",
                    "source_generation": 2,
                    "complete": True,
                    "final": True,
                    "offset": 0,
                    "total": cls.total,
                    "entries": entries,
                },
            },
            received_at=SHELL_FROZEN_NOW + 60.0,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployments.shutdown()
        cls._tmp.cleanup()

    def queue(self, query: str = "") -> dict[str, Any]:
        status, body = _request(self.deployments.remote, "/api/queue" + query)
        self.assertEqual(200, status, body)
        self.assertTrue(body.get("ok"), body)
        return body

    def test_a_catalog_above_one_page_reports_the_whole_of_itself(self) -> None:
        body = self.queue()
        self.assertEqual(self.total, body["catalog_total"])
        self.assertEqual(self.total, body["catalog_matched"])
        self.assertEqual(overview.DEFAULT_PAGE_LIMIT, len(body["plans"]))
        self.assertTrue(body["truncated"])

    def test_counts_cover_the_catalog_rather_than_the_page(self) -> None:
        body = self.queue()
        self.assertEqual(self.total, body["counts"]["all"])
        self.assertEqual(self.extra // 5, body["counts"]["complete"])
        self.assertGreater(body["counts"]["all"], len(body["plans"]))

    def test_a_filter_below_the_page_limit_is_answered_whole(self) -> None:
        # The row it is looking for is beyond the first page of the catalog, so
        # a client-side filter over a server-side page could never have found
        # it. This is the case the consolidation exists for.
        body = self.queue(f"?q=bulk%20{self.extra - 1:03d}")
        self.assertEqual(1, body["catalog_matched"])
        self.assertFalse(body["truncated"])
        self.assertEqual(
            [f"Bulk {self.extra - 1:03d}"], [row["title"] for row in body["plans"]]
        )

    def test_a_project_filter_is_applied_before_the_read_limit(self) -> None:
        body = self.queue("?project=shared")
        self.assertEqual(self.extra // 2, body["catalog_matched"])
        self.assertEqual(self.total, body["catalog_total"])
        for row in body["plans"]:
            self.assertIn("shared", row["projects"])

    def test_paging_walks_the_whole_catalog_without_repeating_a_row(
        self,
    ) -> None:
        seen: list[str] = []
        offset = 0
        while offset < self.total:
            body = self.queue(f"?offset={offset}&limit=200")
            seen.extend(str(row["plan_key"]) for row in body["plans"])
            offset += 200
        self.assertEqual(self.total, len(seen))
        self.assertEqual(self.total, len(set(seen)))

    def test_hiding_completed_work_narrows_the_page_not_the_catalog(
        self,
    ) -> None:
        body = self.queue("?include_completed=0")
        self.assertEqual(self.total - self.extra // 5, body["catalog_matched"])
        self.assertEqual(self.total, body["catalog_total"])
        self.assertEqual(self.extra // 5, body["counts"]["complete"])


class RetainedObservationTests(unittest.TestCase):
    """What only the Pi can hold: a row it has not re-observed.

    The distinction is the whole reason the remote mode is a separate provider.
    A cockpit that showed a retained row as live would be telling an operator
    that a plan is in a state the Mac last reported an hour ago and may have
    left since.
    """

    deployments: workbench_support.ShellDeployments
    corpus: workbench_support.ShellCorpus
    _tmp: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="workbench-overview-part-")
        cls.deployments = workbench_support.start_shell_deployments(Path(cls._tmp.name))
        cls.corpus = cls.deployments.corpus
        entries = workbench_support.shell_catalog_entries(cls.corpus)
        # A second, *partial* generation that mentions only alpha. A partial
        # page never proves a plan is gone, so bravo and charlie are retained
        # rather than dropped -- and retained is exactly what has to be said.
        cls.deployments.store.ingest_catalog(
            {
                "protocol": 1,
                "kind": "catalog_page",
                "connector_id": workbench_support.SHELL_CONNECTOR_ID,
                "sent_at": SHELL_FROZEN_NOW + 60.0,
                "payload": {
                    "observed_at": SHELL_FROZEN_NOW + 60.0,
                    "generation": "shell-fixture-2",
                    "source_generation": 2,
                    "complete": False,
                    "final": True,
                    "offset": 0,
                    "total": len(entries),
                    "entries": entries[:1],
                },
            },
            received_at=SHELL_FROZEN_NOW + 60.0,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployments.shutdown()
        cls._tmp.cleanup()

    def queue(self, query: str = "") -> dict[str, Any]:
        status, body = _request(self.deployments.remote, "/api/queue" + query)
        self.assertEqual(200, status, body)
        self.assertTrue(body.get("ok"), body)
        return body

    def test_a_row_the_newest_observation_missed_is_marked_unconfirmed(
        self,
    ) -> None:
        rows = {str(row["plan_key"]): row for row in self.queue()["plans"]}
        self.assertEqual(
            overview.OBSERVATION_CURRENT,
            rows[self.corpus.plan_keys["alpha"]]["observation_status"],
        )
        for name in ("bravo", "charlie"):
            self.assertEqual(
                overview.OBSERVATION_UNCONFIRMED,
                rows[self.corpus.plan_keys[name]]["observation_status"],
                f"{name} was presented as live",
            )

    def test_an_incomplete_observation_says_so_about_itself(self) -> None:
        body = self.queue()
        self.assertEqual("partial", body["catalog_completeness"])
        self.assertFalse(body["catalog_complete"])

    def test_a_retained_row_is_still_answered_rather_than_withheld(self) -> None:
        # Retained is not absent. Dropping the row would lose the operator's
        # only record of work the Mac stopped reporting on.
        body = self.queue()
        self.assertEqual(
            len(self.corpus.plan_dirs), len({row["plan_key"] for row in body["plans"]})
        )

    def test_the_local_mode_has_nothing_to_retain_and_says_current(self) -> None:
        status, body = _request(self.deployments.local, "/api/queue")
        self.assertEqual(200, status, body)
        # Said once for the whole answer rather than per row: the Mac is
        # standing on the plans it describes, so there is no row it could say
        # anything else about. It says it anyway, so a front end never has to
        # ask which provider answered before deciding whether to warn.
        self.assertEqual(overview.OBSERVATION_CURRENT, body.get("observation_status"))
        for row in body["plans"]:
            self.assertEqual(
                overview.OBSERVATION_CURRENT, overview.observation_status(row)
            )
        self.assertEqual("complete", body.get("catalog_completeness"))


class FixtureIntentTests(unittest.TestCase):
    """The corpus really is what the sections above assume it is.

    These assert the fixture rather than the code. Without them a later edit
    that quietly made ``charlie`` current would turn every window assertion
    above into a test that passes by not exercising anything.
    """

    def test_the_quiet_plan_is_quiet_by_more_than_the_window(self) -> None:
        self.assertGreater(SHELL_QUIET_SECONDS, overview.ACTIVE_WINDOW_SECONDS * 2)

    def test_the_seeded_endings_are_two_different_outcomes(self) -> None:
        # One success and one failure, so the outcome filter has something to
        # exclude as well as something to keep. A corpus of two identical
        # endings would let `?outcome=` return everything and still pass.
        self.assertEqual(2, len(workbench_support.SHELL_HISTORY_RUNS))
        self.assertEqual(
            {True, False},
            {bool(run["ok"]) for run in workbench_support.SHELL_HISTORY_RUNS},
        )

    def test_the_endings_sit_inside_the_retention_window(self) -> None:
        oldest = max(
            float(run["started_ago"]) for run in workbench_support.SHELL_HISTORY_RUNS
        )
        self.assertLess(oldest, 7 * 24 * 60 * 60)

    def test_the_fixture_clock_is_not_the_wall_clock(self) -> None:
        # The remote side is read against a frozen moment so a slow test run
        # cannot change what "stale" means; the local side is written against
        # the real one so a seven-day retention does not age the corpus out.
        self.assertNotEqual(SHELL_FROZEN_NOW, time.time())


if __name__ == "__main__":  # pragma: no cover - module entry point
    unittest.main()
