"""RemoteProviderTests: catalog."""

from __future__ import annotations

from .workbench_http_remote_support import (
    CAPABILITY_OPERATION,
    MAX_READ_LIMIT,
    MODE_REMOTE,
    NOW,
    RemoteProviderTestsBase,
    build,
    catalog_entry,
    catalog_payload,
    document,
    envelope,
    workbench_service,
)


class RemoteProviderTests(RemoteProviderTestsBase):
    def test_the_mode_is_declared_not_inferred(self) -> None:
        self.assertEqual(self.provider().mode, MODE_REMOTE)

    def test_health_reports_the_service_and_nothing_observed(self) -> None:
        response = self.remote_app().handle(build("GET", "/api/health"))
        self.assertEqual(response.status, 200)
        payload = document(response)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            set(payload), {"ok", "command", "reason", "status"}, "health says no more"
        )

    def test_the_summary_answers_in_the_shape_the_local_mode_answers_in(self) -> None:
        payload = document(self.remote_app().handle(build("GET", "/api/summary")))
        self.assertEqual(sorted(payload["counts"]), ["by_state", "plans"])
        self.assertEqual(
            sorted(payload["freshness"]),
            ["complete", "generation", "observed_at", "stale", "total"],
        )
        # Nothing has ever been observed, so the view is stale rather than
        # empty-and-current: "no connector has reported" is not "no plans".
        self.assertTrue(payload["freshness"]["stale"])

    def test_the_queue_is_the_retained_catalog(self) -> None:
        payload = document(self.remote_app().handle(build("GET", "/api/queue")))
        self.assertEqual(payload["plans"], [])
        self.assertEqual(payload["connectors"], [])
        self.assertIsNone(payload["state_root"], "the Pi has no state root")

    def test_a_plan_nobody_published_is_not_found(self) -> None:
        response = self.remote_app().handle(build("GET", "/api/plan?plan_key=absent"))
        self.assertEqual(response.status, 404)
        self.assertEqual(document(response)["reason"], "not-found")

    def test_a_plan_past_the_read_limit_is_still_found(self) -> None:
        self.seed_catalog_past_the_read_limit()
        response = self.remote_app().handle(
            build("GET", "/api/plan?plan_key=zzz-quiet")
        )
        self.assertEqual(response.status, 200, document(response))
        self.assertEqual(document(response)["plan"]["plan_key"], "zzz-quiet")

    def test_the_summary_histogram_counts_every_plan(self) -> None:
        total = self.seed_catalog_past_the_read_limit()
        payload = document(self.remote_app().handle(build("GET", "/api/summary")))
        self.assertEqual(payload["counts"]["plans"], total)
        self.assertEqual(
            payload["counts"]["by_state"],
            {"running": MAX_READ_LIMIT, "complete": 1},
        )
        self.assertEqual(sum(payload["counts"]["by_state"].values()), total)

    def test_the_sort_is_applied_before_the_page_is_cut(self) -> None:
        # Urgency and activity disagree on purpose: the most urgent row is the
        # oldest and sorts last by label, so a page cut in urgency order and
        # then labelled with the requested sort would put it first every time.
        self.store.ingest_catalog(
            envelope(
                "catalog_page",
                catalog_payload(
                    observed_at=NOW - 10,
                    generation="gen-sort",
                    entries=[
                        catalog_entry(
                            "urgent-old",
                            urgency_rank=10,
                            display_label="Zulu",
                            last_activity_at="2020-01-01T00:00:00+00:00",
                        ),
                        catalog_entry(
                            "quiet-new",
                            urgency_rank=50,
                            display_label="alpha",
                            last_activity_at="2030-01-01T00:00:00Z",
                        ),
                    ],
                ),
            ),
            received_at=NOW - 10,
        )
        expected = {
            "urgency": "urgent-old",
            "recent": "quiet-new",
            "label": "quiet-new",
            "oldest": "urgent-old",
        }
        for sort, first in expected.items():
            with self.subTest(sort=sort):
                payload = document(
                    self.remote_app().handle(
                        build("GET", f"/api/queue?sort={sort}&limit=1")
                    )
                )
                self.assertEqual(payload["filters"]["sort"], sort)
                self.assertEqual([row["plan_key"] for row in payload["plans"]], [first])
                self.assertTrue(payload["truncated"])

    def test_an_empty_page_size_is_refused_rather_than_rounded_up(self) -> None:
        for path in ("/api/queue?limit=0", "/api/history?limit=0"):
            with self.subTest(path=path):
                response = self.remote_app().handle(build("GET", path))
                self.assertEqual(response.status, 400)
                payload = document(response)
                self.assertEqual(payload["reason"], "invalid-request")
                self.assertIn("limit", payload["error"])

    def test_a_path_is_refused_rather_than_guessed_at(self) -> None:
        response = self.remote_app().handle(
            build("GET", "/api/plan?plan_dir=/Users/x/plan")
        )
        self.assertEqual(response.status, 400)
        self.assertIn("plan_key", document(response)["error"])

    def test_what_stays_on_the_mac_says_so_in_advance(self) -> None:
        provider = self.provider()
        for operation, reason in workbench_service.UNAVAILABLE_REASONS.items():
            with self.subTest(operation=operation):
                self.assertEqual(provider.unavailable(operation), reason)
        payload = document(self.remote_app().handle(build("GET", "/api/capabilities")))
        advertised = set(payload["unavailable_operations"])
        self.assertTrue(set(workbench_service.UNAVAILABLE_REASONS) <= advertised)
        self.assertNotIn("state", advertised, "a paired link can read details")
        self.assertNotIn(CAPABILITY_OPERATION, advertised)

    def test_an_unpaired_service_says_so_rather_than_failing_later(self) -> None:
        provider = self.provider(reads=None, mutations=None)
        self.assertIn("no connector is paired", provider.unavailable("state") or "")
        self.assertIn("no connector is paired", provider.unavailable("reply") or "")
        self.assertIsNone(provider.unavailable("summary"))

    def test_a_reason_of_its_own_outranks_the_unpaired_one(self) -> None:
        """An unpaired link is true of every operation and explains none of them."""
        provider = self.provider(reads=None, mutations=None)
        self.assertEqual(
            provider.unavailable("registry"),
            workbench_service.UNAVAILABLE_REASONS["registry"],
        )
