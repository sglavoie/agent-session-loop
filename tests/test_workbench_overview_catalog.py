"""LargeCatalogTests, RetainedObservationTests coverage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from asl_workbench import overview_vocabulary
from tests import workbench_support
from tests.workbench_overview_support import (
    LARGE_CATALOG_SIZE,
    _request,
)
from tests.workbench_support import (
    SHELL_FROZEN_NOW,
)


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
        self.assertEqual(overview_vocabulary.DEFAULT_PAGE_LIMIT, len(body["plans"]))
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
            overview_vocabulary.OBSERVATION_CURRENT,
            rows[self.corpus.plan_keys["alpha"]]["observation_status"],
        )
        for name in ("bravo", "charlie"):
            self.assertEqual(
                overview_vocabulary.OBSERVATION_UNCONFIRMED,
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
        self.assertEqual(
            overview_vocabulary.OBSERVATION_CURRENT, body.get("observation_status")
        )
        for row in body["plans"]:
            self.assertEqual(
                overview_vocabulary.OBSERVATION_CURRENT,
                overview_vocabulary.observation_status(row),
            )
        self.assertEqual("complete", body.get("catalog_completeness"))


if __name__ == "__main__":
    unittest.main()
