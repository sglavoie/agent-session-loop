"""OverviewParityTests coverage."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from asl_workbench import catalog as workbench_catalog
from asl_workbench import observation, overview_selection, overview_vocabulary
from tests import workbench_support
from tests.workbench_overview_support import (
    _request,
)
from tests.workbench_support import (
    ShellDeployment,
)

ASL_TEST_LANE = "integration"


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
                    overview_vocabulary.OBSERVATION_CURRENT,
                    overview_vocabulary.observation_status(row),
                )

    def test_the_local_mode_says_current_once_for_the_whole_answer(self) -> None:
        body = self.queue(self.deployments.local)
        self.assertEqual(
            overview_vocabulary.OBSERVATION_CURRENT, body.get("observation_status")
        )

    def test_the_remote_mode_marks_each_row_it_still_confirms(self) -> None:
        for row in self.queue(self.deployments.remote)["plans"]:
            self.assertEqual(
                overview_vocabulary.OBSERVATION_CURRENT, row.get("observation_status")
            )

    def test_the_quiet_plan_falls_outside_the_window_in_both_modes(self) -> None:
        key = self.deployments.corpus.plan_keys["charlie"]
        for deployment in self.deployments.both:
            body = self.queue(deployment)
            row = self.rows_by_key(body)[key]
            now = float(body["observed_at"])
            self.assertFalse(
                overview_selection.recently_active(row, now=now),
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
            self.assertIsNotNone(overview_selection._activity_epoch(published), mode)
        # Named rather than implied: this one is a number wearing a string.
        self.assertEqual(
            float(spellings["remote"]),
            overview_selection._activity_epoch(spellings["remote"]),
        )

    def test_the_other_plans_stay_current_work_in_both_modes(self) -> None:
        for deployment in self.deployments.both:
            body = self.queue(deployment)
            rows = self.rows_by_key(body)
            now = float(body["observed_at"])
            for name in ("alpha", "bravo"):
                key = self.deployments.corpus.plan_keys[name]
                self.assertTrue(
                    overview_selection.recently_active(rows[key], now=now),
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
            self.assertEqual({overview_vocabulary.plan_label(charlie)}, labels)

    def test_the_finished_run_the_queue_names_is_the_newest_ending(self) -> None:
        # The queue's `run_key` is a link target; history's is the same string
        # for the same run, which is what lets one `#run=` link resolve from a
        # notification into either pane.
        for deployment in self.deployments.both:
            rows = self.rows_by_key(self.queue(deployment))
            charlie = rows[self.deployments.corpus.plan_keys["charlie"]]
            newest = self.history(deployment)["entries"][0]
            self.assertEqual(charlie["run_key"], newest["run_key"])


if __name__ == "__main__":
    unittest.main()
