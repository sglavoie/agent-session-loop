"""SelectionOrderTests, FilterParsingTests, FixtureIntentTests coverage."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from typing import Any

from asl_workbench import http as workbench_http
from asl_workbench import overview_filters, overview_selection, overview_vocabulary
from tests import workbench_support
from tests.workbench_overview_support import (
    LARGE_CATALOG_SIZE,
    _call,
    _row,
)
from tests.workbench_support import (
    SHELL_FROZEN_NOW,
    SHELL_QUIET_SECONDS,
)

ASL_TEST_LANE = "integration"


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
        page = overview_selection.select(self.rows, overview_filters.QueueFilters())
        self.assertEqual(overview_vocabulary.DEFAULT_PAGE_LIMIT, len(page.rows))
        self.assertEqual(LARGE_CATALOG_SIZE, page.matched)
        self.assertEqual(LARGE_CATALOG_SIZE, page.total)
        self.assertTrue(page.truncated)

    def test_the_filter_runs_before_the_page_rather_than_over_it(self) -> None:
        # `notes` holds every even index, so half the corpus. If the page were
        # cut first the filter would then be applied to 200 rows and answer
        # with about a hundred; applied first it answers with all 120.
        filters = overview_filters.QueueFilters(project="notes")
        page = overview_selection.select(self.rows, filters)
        self.assertEqual(LARGE_CATALOG_SIZE // 2, page.matched)
        self.assertEqual(LARGE_CATALOG_SIZE, page.total)
        for row in page.rows:
            self.assertEqual(["notes"], row["projects"])

    def test_counts_are_taken_over_the_matched_set_not_over_the_page(
        self,
    ) -> None:
        page = overview_selection.select(self.rows, overview_filters.QueueFilters())
        complete = sum(1 for row in self.rows if row["primary_state"] == "complete")
        self.assertEqual(complete, page.counts["complete"])
        self.assertEqual(LARGE_CATALOG_SIZE, page.counts["all"])
        # Every chip exceeds the page it was counted beside, which is the whole
        # reason the count cannot be taken from `page.rows`.
        self.assertGreater(page.counts["all"], len(page.rows))

    def test_a_pressed_chip_does_not_narrow_the_counts_it_sits_beside(
        self,
    ) -> None:
        pressed = overview_selection.select(
            self.rows, overview_filters.QueueFilters(group="complete")
        )
        unpressed = overview_selection.select(
            self.rows, overview_filters.QueueFilters()
        )
        # A count narrowed by the chip currently pressed would report the page
        # back to itself and every other chip would read zero.
        self.assertEqual(dict(unpressed.counts), dict(pressed.counts))
        self.assertLess(pressed.matched, unpressed.matched)

    def test_a_narrowing_filter_still_counts_the_states_it_left_behind(
        self,
    ) -> None:
        page = overview_selection.select(
            self.rows, overview_filters.QueueFilters(project="notes")
        )
        self.assertEqual(LARGE_CATALOG_SIZE // 2, page.counts["all"])
        self.assertLess(page.counts["all"], LARGE_CATALOG_SIZE)

    def test_paging_walks_the_matched_set_without_repeating_or_skipping(
        self,
    ) -> None:
        seen: list[str] = []
        for offset in range(0, LARGE_CATALOG_SIZE, 100):
            page = overview_selection.select(
                self.rows, overview_filters.QueueFilters(offset=offset, limit=100)
            )
            seen.extend(str(row["plan_key"]) for row in page.rows)
        self.assertEqual(LARGE_CATALOG_SIZE, len(seen))
        self.assertEqual(LARGE_CATALOG_SIZE, len(set(seen)))

    def test_the_page_is_the_first_page_of_the_requested_order(self) -> None:
        page = overview_selection.select(
            self.rows, overview_filters.QueueFilters(sort="label")
        )
        labels = [str(row["title"]) for row in page.rows]
        self.assertEqual(sorted(labels), labels)
        self.assertEqual("Plan 000", labels[0])

    def test_facets_are_taken_from_the_catalog_and_not_from_the_filter(
        self,
    ) -> None:
        # A project list narrowed by the project already chosen is a list an
        # operator cannot leave.
        page = overview_selection.select(
            self.rows, overview_filters.QueueFilters(project="notes")
        )
        self.assertEqual(("notes", "scripts"), page.projects)
        self.assertEqual(("claude", "codex"), page.harnesses)

    def test_hiding_completed_work_narrows_the_page_but_not_the_catalog(
        self,
    ) -> None:
        page = overview_selection.select(
            self.rows, overview_filters.QueueFilters(include_completed=False)
        )
        self.assertNotIn("complete", {str(row["primary_state"]) for row in page.rows})
        self.assertEqual(LARGE_CATALOG_SIZE, page.total)
        self.assertGreater(page.counts["complete"], 0)

    def test_the_complete_chip_is_itself_the_request_to_see_completed_work(
        self,
    ) -> None:
        states = overview_filters.allowed_states("complete", include_completed=False)
        self.assertEqual(("complete",), states)


class FilterParsingTests(unittest.TestCase):
    """What a URL may say, refused identically for both providers."""

    def test_an_absent_parameter_narrows_nothing(self) -> None:
        filters = overview_filters.parse_filters(_call("/api/queue"))
        self.assertIsNone(filters.project)
        self.assertIsNone(filters.search)
        self.assertIsNone(filters.harness)
        self.assertEqual(overview_vocabulary.FILTER_ALL, filters.group)
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
            overview_filters.parse_filters(
                _call("/api/queue?include_completed=0")
            ).include_completed
        )
        self.assertTrue(
            overview_filters.parse_filters(
                _call("/api/queue?include_completed=1")
            ).include_completed
        )
        with self.assertRaises(workbench_http.HttpRefusal) as refused:
            overview_filters.parse_filters(_call("/api/queue?include_completed=yes"))
        self.assertEqual(400, refused.exception.status)

    def test_an_unknown_chip_or_sort_mode_is_named_rather_than_ignored(
        self,
    ) -> None:
        for target in ("/api/queue?filter=urgent", "/api/queue?sort=newest"):
            with self.assertRaises(workbench_http.HttpRefusal) as refused:
                overview_filters.parse_filters(_call(target))
            self.assertEqual(400, refused.exception.status)

    def test_a_page_larger_than_the_ceiling_is_refused(self) -> None:
        with self.assertRaises(workbench_http.HttpRefusal):
            overview_filters.parse_filters(
                _call(f"/api/queue?limit={overview_vocabulary.MAX_PAGE_LIMIT + 1}")
            )

    def test_a_non_finite_activity_is_no_activity(self) -> None:
        # NaN sorts nowhere and an infinity outranks every real moment, so
        # either would scramble the order the page cursor walks.
        for value in ("nan", "NaN", "inf", "-Infinity", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(overview_selection._activity_epoch(value))
        self.assertIsNone(
            overview_selection.activity_at(
                {"last_activity_at": "nan", "run_started_at": None}
            )
        )

    def test_an_offset_less_moment_is_read_as_utc(self) -> None:
        # The browser reads it as UTC too; local time would move the row
        # across the eight-hour window by the host's offset.
        self.assertEqual(
            overview_selection._activity_epoch("2026-09-15T08:00:00"),
            overview_selection._activity_epoch("2026-09-15T08:00:00Z"),
        )

    def test_the_filter_set_round_trips_through_the_answer(self) -> None:
        target = (
            "/api/queue?project=scripts&q=alpha&harness=claude&filter=paused"
            "&sort=recent&offset=10&limit=25&include_completed=1"
        )
        document = overview_filters.parse_filters(_call(target)).as_document()
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


class FixtureIntentTests(unittest.TestCase):
    """The corpus really is what the sections above assume it is.

    These assert the fixture rather than the code. Without them a later edit
    that quietly made ``charlie`` current would turn every window assertion
    above into a test that passes by not exercising anything.
    """

    def test_the_quiet_plan_is_quiet_by_more_than_the_window(self) -> None:
        self.assertGreater(
            SHELL_QUIET_SECONDS, overview_vocabulary.ACTIVE_WINDOW_SECONDS * 2
        )

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


if __name__ == "__main__":
    unittest.main()
