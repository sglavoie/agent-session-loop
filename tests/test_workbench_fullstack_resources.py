"""Bounded scale, inert launches and the capability-matrix consolidation audit."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    workbench_launch,
)
from asl_workbench import (
    capabilities,
    catalog,
    remote_reads,
    transport,
)
from asl_workbench import connector_payloads as portable_payloads
from asl_workbench import connector_support as portable_support
from asl_workbench import http as workbench_http
from tests.support_workbench_fullstack import (
    CONNECTOR_ID,
    NOW,
    TARGET_ID,
)
from tests.support_workbench_fullstack_case import FullStackCase
from tests.workbench_support import (
    seed_shell_plans,
    start_local_shell_server,
    workbench_api_routes,
    workbench_cli_entry_points,
)

# ---------------------------------------------------------------------------
# Scale is bounded
# ---------------------------------------------------------------------------


class ResourceTests(FullStackCase):
    """A thousand plans, concurrent readers, and queues with a ceiling."""

    reads = True

    #: Past every page limit the transport declares, so paging rather than the
    #: fixture size is what the assertions are about.
    PLANS = 1000

    def large_catalog(self) -> catalog.PlanCatalog:
        records = tuple(
            catalog.record_from_summary(
                {
                    **{name: None for name in catalog.PRIVATE_FIELDS},
                    **{name: None for name in catalog.CACHE_SAFE_FIELDS},
                    "plan_dir": f"/tmp/agent-plans/plan-{index:05d}",
                    "exists": True,
                    "primary_state": "ready",
                    "recommended_action": "run",
                    "urgency_rank": 80,
                    "urgency_group": "ready",
                    "activity_label": "Ready to run",
                    "counts": {"pending": 1, "complete": 0, "empty": 0, "unknown": 0},
                    "sources": ["state"],
                    "repositories": [],
                    "repository_count": 0,
                    "warnings": [],
                }
            )
            for index in range(self.PLANS)
        )
        return catalog.build_catalog(records)

    def publish_large_catalog(self) -> catalog.PlanCatalog:
        """Page a thousand plans into the store through the real builders."""
        catalogue = self.large_catalog()
        pages = catalogue.pages(limit=transport.MAX_CATALOG_ENTRIES_PER_PAGE)
        self.assertGreater(len(pages), 1, "a single page proves nothing about paging")
        for page in pages:
            self.link.deliver(
                transport.observation_envelope(
                    payload=portable_payloads.catalog_page_payload(
                        page, observed_at=self.clock.now, source_generation=2
                    ),
                    connector_id=CONNECTOR_ID,
                    target_id=TARGET_ID,
                    sent_at=self.clock.now,
                )
            )
        return catalogue

    def test_a_thousand_plan_catalog_arrives_whole_and_is_served_in_pages(
        self,
    ) -> None:
        self.publish()
        self.publish_large_catalog()
        body = self.queue("?limit=100")
        self.assertEqual(self.PLANS, body["catalog_total"])
        self.assertEqual(self.PLANS, body["catalog_matched"])
        self.assertLessEqual(len(body["plans"]), 100)

    def test_paging_the_whole_catalog_covers_it_without_overlap(self) -> None:
        self.publish()
        self.publish_large_catalog()
        seen: list[str] = []
        offset = 0
        while offset < self.PLANS:
            rows = self.queue_rows(f"?limit=200&offset={offset}")
            if not rows:
                break
            seen.extend(str(row["plan_key"]) for row in rows)
            offset += len(rows)
        self.assertEqual(self.PLANS, len(seen))
        self.assertEqual(self.PLANS, len(set(seen)))

    def test_a_catalog_that_size_leaves_the_heartbeat_fresh(self) -> None:
        self.publish()
        self.publish_large_catalog()
        self.link.deliver(
            transport.observation_envelope(
                payload=portable_payloads.heartbeat_payload(
                    observed_at=self.clock.now,
                    bridge_alive=True,
                    source_generation=2,
                ),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=self.clock.now,
            )
        )
        statuses = self.store.connector_statuses(now=self.clock.tick(5.0))
        self.assertTrue(statuses)
        self.assertFalse(any(status.degraded for status in statuses))

    def test_concurrent_summaries_over_a_large_catalog_all_answer(self) -> None:
        self.publish()
        self.publish_large_catalog()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request("/api/summary"), range(16)))
        for status, body in results:
            self.assertEqual(200, status, body)
            self.assertEqual(self.PLANS, body["counts"]["plans"])

    def test_a_dead_link_fills_the_queues_to_their_ceiling_and_no_further(
        self,
    ) -> None:
        self.link.down()
        for _ in range(50):
            self.publish(steps=4, tick=150.0)
        # Bounded by construction, not by how long the link stays down.
        self.assertLessEqual(
            self.connector.pending_reads(), portable_support.MAX_PENDING_REQUESTS
        )
        self.assertLessEqual(
            self.connector.pending(),
            portable_support.MAX_PENDING_REQUESTS
            + portable_support.MAX_QUEUED_ANSWERS
            + 4,
        )

    def test_reads_past_the_pending_ceiling_are_refused_rather_than_queued(
        self,
    ) -> None:
        assert self.client is not None
        admitted = 0
        for index in range(remote_reads.MAX_PENDING_READS + 8):
            ticket = self.client.read(
                request_id=f"probe-{index}",
                detail="progress",
                plan_key=self.corpus.plan_keys["alpha"],
                now=NOW,
            )
            if ticket.outstanding:
                admitted += 1
        self.assertEqual(remote_reads.MAX_PENDING_READS, admitted)


# ---------------------------------------------------------------------------
# A launch spawns a real child
# ---------------------------------------------------------------------------

#: An interpreter that starts, drains whatever was written to it, and stops.
#: The launcher's argv is passed to it and ignored, so the spawn, the new
#: session, the environment and the stdin write are all the real ones while the
#: child does nothing to the plan.
INERT_INTERPRETER = "#!/bin/sh\nexec cat >/dev/null 2>&1\n"


class InertLaunchTests(unittest.TestCase):
    """``POST /api/launch`` against the real launcher and a real child."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-launch-")).resolve()
        self.addCleanup(FullStackCase.remove_tree, self.root)
        self.corpus = seed_shell_plans(self.root / "mac")
        self.interpreter = self.root / "inert-python"
        self.interpreter.write_text(INERT_INTERPRETER, encoding="utf-8")
        self.interpreter.chmod(0o700)
        self.token = "fullstack-launch-token"
        self.server, self.thread = start_local_shell_server(
            self.corpus, token=self.token
        )
        self.addCleanup(self.stop)

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, path: str, body: Any) -> tuple[int, Any]:
        raw = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://{self.server.bound_host}:{self.server.bound_port}{path}",
            data=raw,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def inert(self) -> Any:
        """Patch only the interpreter, leaving every other launcher decision."""
        real = workbench_launch.launch_plan

        def launch(plan_dir: Any, **kwargs: Any) -> Any:
            kwargs["python_executable"] = str(self.interpreter)
            return real(plan_dir, **kwargs)

        return mock.patch.object(workbench_launch, "launch_plan", launch)

    def reap(self, pid: int) -> None:
        """End the child and collect it, so no test leaves a process behind.

        Signalled rather than waited for: the inert interpreter drains its stdin
        and the launcher leaves that pipe open for the reply path, so a child
        that is behaving correctly is a child that is still running. Killing it
        is the test taking back what it started.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(250):
            try:
                found, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if found == pid:
                return
            threading.Event().wait(0.02)

    def test_a_launch_spawns_a_child_in_its_own_session(self) -> None:
        plan_dir = self.corpus.plan_dir("bravo")
        with self.inert():
            status, body = self.request("/api/launch", {"plan_dir": str(plan_dir)})
        self.assertEqual(200, status, body)
        pid = int(body["launch"]["pid"])
        self.addCleanup(self.reap, pid)
        self.assertNotEqual(os.getpid(), pid)
        # The argv the real builder produced, pointed at the inert interpreter.
        self.assertEqual(str(self.interpreter), body["launch"]["argv"][0])
        self.assertIn("agent_session_loop", body["launch"]["argv"])

    def test_the_child_is_not_in_the_servers_process_group(self) -> None:
        plan_dir = self.corpus.plan_dir("bravo")
        with self.inert():
            status, body = self.request("/api/launch", {"plan_dir": str(plan_dir)})
        self.assertEqual(200, status, body)
        pid = int(body["launch"]["pid"])
        self.addCleanup(self.reap, pid)
        # A Ctrl-C in the launcher's terminal reaches a process group. The run
        # outliving its launcher is the whole reason for `start_new_session`.
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:  # pragma: no cover - the child already exited
            self.skipTest("the inert child exited before it could be inspected")
        self.assertNotEqual(os.getpgrp(), group)

    def test_a_plan_that_does_not_exist_is_refused_before_any_spawn(self) -> None:
        with self.inert():
            status, body = self.request(
                "/api/launch", {"plan_dir": str(self.root / "nowhere")}
            )
        self.assertEqual(400, status)
        self.assertEqual(workbench_launch.LAUNCH_REASON_INVALID_PLAN, body["reason"])

    def test_a_repeated_launch_id_replays_rather_than_spawning_again(self) -> None:
        plan_dir = self.corpus.plan_dir("bravo")
        with self.inert():
            first_status, first = self.request(
                "/api/launch", {"plan_dir": str(plan_dir), "launch_id": "once"}
            )
            second_status, second = self.request(
                "/api/launch", {"plan_dir": str(plan_dir), "launch_id": "once"}
            )
        self.assertEqual(200, first_status, first)
        self.assertEqual(200, second_status, second)
        self.addCleanup(self.reap, int(first["launch"]["pid"]))
        self.assertEqual(first["launch"]["pid"], second["launch"]["pid"])


# ---------------------------------------------------------------------------
# One behaviour, one owner
# ---------------------------------------------------------------------------


class ConsolidationTests(unittest.TestCase):
    """The audit for the duplication this consolidation is meant to end.

    Deliberately *not* re-checking what
    ``tests/test_workbench_foundations.py::CapabilityMatrixTests`` already owns
    -- that every route has a row, that every owner comes from the closed
    vocabulary, that coverage is either named or declared missing. Those are one
    behaviour with one test, and adding a second would be the thing this class
    exists to find. What is here is the other direction and the pairs: a row
    with no behaviour behind it, two rows behind one behaviour, and two
    deployments that have quietly stopped serving the same application.
    """

    rows: tuple[capabilities.Capability, ...]

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = tuple(capabilities.CAPABILITIES)

    def normalized(self, summary: str) -> str:
        return " ".join(summary.split()).casefold()

    def test_no_two_rows_describe_the_same_behaviour(self) -> None:
        # One summary on two rows is the signature of the duplication this
        # consolidation removes: the same behaviour reached two ways, each
        # maintained by somebody who believes theirs is the only one.
        seen: dict[str, str] = {}
        for row in self.rows:
            summary = self.normalized(row.summary)
            first = seen.get(summary)
            self.assertIsNone(first, f"{row.key} repeats the summary of {first}")
            seen[summary] = row.key

    def test_every_row_is_addressed_exactly_once(self) -> None:
        keys = [row.key for row in self.rows]
        self.assertEqual(sorted(set(keys)), sorted(keys))

    def test_no_row_claims_a_route_the_application_does_not_serve(self) -> None:
        # The coverage direction is checked in the foundations module; this is
        # the orphan direction. A row that outlived its route is a behaviour
        # somebody will look for, find described, and reimplement.
        served = {
            f"{capabilities.SURFACE_WORKBENCH_HTTP}:{route.method} {route.path}"
            for route in workbench_http.ROUTES
        }
        claimed = {
            row.key
            for row in self.rows
            if row.surface == capabilities.SURFACE_WORKBENCH_HTTP
        }
        self.assertEqual(set(), claimed - served)

    def test_both_deployments_serve_the_same_route_set(self) -> None:
        # The laptop and the Pi are one application in two places. The moment
        # one grows a route the other lacks, the matrix's single row for that
        # behaviour is describing two different behaviours.
        remote = {f"{route.method} {route.path}" for route in workbench_http.ROUTES}
        self.assertEqual(set(workbench_api_routes()), remote)

    def test_every_cli_entry_point_has_exactly_one_row(self) -> None:
        rows = [
            row
            for row in self.rows
            if row.surface == capabilities.SURFACE_WORKBENCH_CLI
        ]
        named = [row.key.split(":", 1)[1] for row in rows]
        self.assertEqual(sorted(set(named)), sorted(named))
        self.assertEqual(set(workbench_cli_entry_points()), set(named))

    def test_a_declared_gap_is_one_of_the_two_the_matrix_knows(self) -> None:
        # A gap is an excuse, and an excuse spelled freehand is one nobody can
        # search for or ever close. There are two, they are named constants, and
        # a row wanting a third has to add it here first.
        known = {capabilities.GAP_DEPLOYED_HOST, capabilities.GAP_WORKBENCH_BROWSER}
        for row in self.rows:
            if row.coverage_gap is None:
                continue
            with self.subTest(row.key):
                self.assertIn(row.coverage_gap, known)

    def test_a_browser_gap_still_names_coverage_somewhere(self) -> None:
        # `GAP_WORKBENCH_BROWSER` means "no spec drives this in a real browser",
        # which is not the same as "nothing tests this at all". A row that
        # declared the gap and named nothing would be behaviour with no coverage
        # of any kind, reading as merely unspecced.
        for row in self.rows:
            if row.coverage_gap != capabilities.GAP_WORKBENCH_BROWSER:
                continue
            with self.subTest(row.key):
                self.assertTrue(
                    row.regression_coverage,
                    "a browser gap excuses the spec, not the coverage",
                )


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
