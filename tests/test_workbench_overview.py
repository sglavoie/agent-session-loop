"""Shared overview vocabulary, time windows, names, and membership tests."""

from __future__ import annotations

import re
import unittest

from asl_workbench import history as portable_history
from asl_workbench import overview_filters, overview_selection, overview_vocabulary
from tests import workbench_support
from tests.workbench_overview_support import (
    OVERVIEW_JS,
    _js_array,
    _js_number,
    _js_object,
    _js_source,
    _js_string,
    _row,
)

# Integration: the vocabulary halves and the selection algebra would run
# anywhere, but "both modes present one body of work" is only a claim
# about two providers answering the same questions, so both are bound to
# loopback sockets over one corpus.
ASL_TEST_LANE = "integration"


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
            list(overview_vocabulary.PRIMARY_STATES),
            _js_array("PRIMARY_STATES", self.source),
        )

    def test_every_filter_chip_covers_the_same_states_in_both_copies(self) -> None:
        published = _js_object("FILTER_GROUPS", self.source)
        self.assertEqual(sorted(overview_vocabulary.FILTER_GROUPS), sorted(published))
        for name, states in overview_vocabulary.FILTER_GROUPS.items():
            self.assertEqual(
                list(states),
                published[name],
                f"chip {name!r} covers different states on the two sides",
            )

    def test_sort_modes_match_and_every_mode_has_a_label(self) -> None:
        modes = _js_array("SORT_MODES", self.source)
        self.assertEqual(list(overview_vocabulary.SORT_MODES), modes)
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
        self.assertEqual(list(overview_vocabulary.OVERVIEW_SECTIONS), sections)
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
            overview_vocabulary.UNGROUPED_PROJECT,
            _js_string("UNGROUPED_PROJECT", self.source),
        )

    def test_the_observation_vocabulary_matches_and_every_word_reads(self) -> None:
        for name, value in (
            ("OBSERVATION_CURRENT", overview_vocabulary.OBSERVATION_CURRENT),
            ("OBSERVATION_UNCONFIRMED", overview_vocabulary.OBSERVATION_UNCONFIRMED),
            ("OBSERVATION_ABSENT", overview_vocabulary.OBSERVATION_ABSENT),
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
                    overview_vocabulary.OBSERVATION_CURRENT,
                    overview_vocabulary.OBSERVATION_UNCONFIRMED,
                    overview_vocabulary.OBSERVATION_ABSENT,
                )
            ),
            sorted(named),
        )

    def test_the_inactive_work_window_is_eight_hours_on_both_sides(self) -> None:
        self.assertEqual(8 * 60 * 60, overview_vocabulary.ACTIVE_WINDOW_SECONDS)
        self.assertEqual(
            float(overview_vocabulary.ACTIVE_WINDOW_SECONDS),
            _js_number("ACTIVE_WINDOW_SECONDS", self.source),
        )

    def test_the_page_limit_the_browser_asks_for_is_one_the_server_allows(
        self,
    ) -> None:
        for name in ("QUEUE_PAGE_LIMIT", "HISTORY_PAGE_LIMIT"):
            asked = _js_number(name, self.source)
            self.assertEqual(float(overview_vocabulary.DEFAULT_PAGE_LIMIT), asked, name)
            self.assertLessEqual(asked, float(overview_vocabulary.MAX_PAGE_LIMIT), name)

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
            sorted(overview_vocabulary.ACTIVE_STATES),
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
        for section in overview_vocabulary.OVERVIEW_SECTIONS:
            self.assertIn(
                section,
                _js_array("OVERVIEW_SECTIONS", self.source),
                f"section {section!r} is not rendered",
            )
        # The windowed section is the one the toggle moves rows out of, and
        # `sectionFor` has to reach it only through `recentlyActive`.
        self.assertEqual(
            {overview_vocabulary.SECTION_RECENT},
            set(overview_vocabulary.WINDOWED_SECTIONS),
        )
        self.assertIn("recentlyActive(plan, nowSeconds)) return SECTION_RECENT", source)


class InactiveWorkWindowTests(unittest.TestCase):
    """Eight hours, measured against the observation rather than the browser."""

    def setUp(self) -> None:
        self.now = 1_800_000_000.0

    def test_a_running_row_is_current_however_old_its_last_movement(self) -> None:
        for state in sorted(overview_vocabulary.ACTIVE_STATES):
            row = _row(primary_state=state, last_activity_at=self.now - 400_000)
            self.assertTrue(
                overview_selection.recently_active(row, now=self.now),
                f"{state} was aged out by the clock",
            )

    def test_a_row_that_has_never_moved_is_queued_work_not_old_work(self) -> None:
        # The window's question is "has this gone quiet", and a plan that has
        # never been run has not gone quiet. Filing it with the inactive runs
        # would hide the newest plan on the machine behind a toggle.
        self.assertTrue(overview_selection.recently_active(_row(), now=self.now))

    def test_the_boundary_is_inclusive_and_the_far_side_is_not(self) -> None:
        window = overview_vocabulary.ACTIVE_WINDOW_SECONDS
        edge = _row(last_activity_at=self.now - window)
        past = _row(last_activity_at=self.now - window - 1)
        self.assertTrue(overview_selection.recently_active(edge, now=self.now))
        self.assertFalse(overview_selection.recently_active(past, now=self.now))

    def test_both_publications_of_a_moment_are_read_the_same_way(self) -> None:
        moment = self.now - 2 * overview_vocabulary.ACTIVE_WINDOW_SECONDS
        as_epoch = _row(last_activity_at=moment)
        as_iso = _row(last_activity_at=workbench_support.shell_iso(moment))
        self.assertEqual(
            overview_selection.activity_at(as_epoch),
            overview_selection.activity_at(as_iso),
        )
        self.assertFalse(overview_selection.recently_active(as_iso, now=self.now))

    def test_a_number_of_seconds_spelled_as_text_is_still_a_moment(self) -> None:
        # Not a courtesy to sloppy callers: the Pi's catalog_entry column for
        # this field is TEXT, so an epoch published as a float comes back out
        # of SQLite as "1789466400.0". A reader that only knew ISO-8601 and
        # numbers would call the row never-moved and hand a plan that has been
        # quiet for thirty hours back to the top of the queue.
        moment = self.now - 2 * overview_vocabulary.ACTIVE_WINDOW_SECONDS
        as_text = _row(last_activity_at=str(moment))
        self.assertEqual(moment, overview_selection.activity_at(as_text))
        self.assertFalse(overview_selection.recently_active(as_text, now=self.now))

    def test_a_quiet_row_falls_back_to_when_it_was_last_observed(self) -> None:
        # The Pi's rows carry no activity of their own until one is published,
        # and "when I last saw this" is the only honest stand-in. It has to be
        # read, or every retained row would count as never-moved and current.
        row = _row(last_observed_at=self.now - 400_000)
        self.assertEqual(self.now - 400_000, overview_selection.activity_at(row))
        self.assertFalse(overview_selection.recently_active(row, now=self.now))


class PlanNameTests(unittest.TestCase):
    """What a row is called, read the one way both modes can answer."""

    def test_each_mode_is_read_under_the_name_it_publishes(self) -> None:
        self.assertEqual("Pi", overview_vocabulary.plan_label({"display_label": "Pi"}))
        self.assertEqual("Mac", overview_vocabulary.plan_label({"label": "Mac"}))

    def test_a_row_with_no_explicit_label_is_named_by_its_title(self) -> None:
        # The Mac publishes `label: null` for a plan nobody renamed, so this is
        # the common case and not the edge one. Stopping before the title would
        # make `sort=label` order the entire local queue under one empty
        # string, which reads as "the sort did nothing".
        row = {"label": None, "display_label": None, "title": "Charlie"}
        self.assertEqual("Charlie", overview_vocabulary.plan_label(row))

    def test_label_sort_orders_local_style_rows_by_their_titles(self) -> None:
        rows = [
            {"label": None, "title": title} for title in ("Charlie", "Alpha", "Bravo")
        ]
        self.assertEqual(
            ["Alpha", "Bravo", "Charlie"],
            [row["title"] for row in overview_selection.sorted_rows(rows, "label")],
        )

    def test_a_row_naming_nothing_is_empty_rather_than_invented(self) -> None:
        self.assertEqual("", overview_vocabulary.plan_label({"plan_key": "abc"}))


class ProjectMembershipTests(unittest.TestCase):
    """A plan in two projects is found by either and drawn once."""

    def test_a_multi_project_plan_is_headed_by_every_project_it_is_in(
        self,
    ) -> None:
        row = _row(projects=["scripts", "notes"])
        # Publication order, not sorted: the registry's order is the order the
        # operator wrote, and re-sorting it here would put a plan's incidental
        # second project ahead of the one it was registered under.
        self.assertEqual(("scripts", "notes"), overview_vocabulary.project_names(row))
        # One heading, not two rows: the plan is one plan with one archive
        # button, and splitting it would offer the operator two of them.
        self.assertEqual("scripts + notes", overview_vocabulary.project_group(row))

    def test_membership_matches_on_any_one_project(self) -> None:
        row = _row(projects=["scripts", "notes"])
        for name in ("scripts", "notes"):
            filters = overview_filters.QueueFilters(project=name)
            self.assertTrue(
                overview_filters.matches(row, filters, states=True),
                f"a plan in {name} was not found by it",
            )
        self.assertFalse(
            overview_filters.matches(
                row, overview_filters.QueueFilters(project="elsewhere"), states=True
            )
        )

    def test_a_plan_with_no_project_is_headed_rather_than_dropped(self) -> None:
        row = _row(projects=[])
        self.assertEqual(
            overview_vocabulary.UNGROUPED_PROJECT,
            overview_vocabulary.project_group(row),
        )

    def test_the_project_facet_names_every_project_once(self) -> None:
        rows = [
            _row(plan_key="a", projects=["scripts", "notes"]),
            _row(plan_key="b", projects=["scripts"]),
            _row(plan_key="c", projects=[]),
        ]
        self.assertEqual(("notes", "scripts"), overview_selection.facet_projects(rows))


if __name__ == "__main__":
    unittest.main()
