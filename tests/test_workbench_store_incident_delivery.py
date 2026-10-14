"""Incident delivery and summaries."""

from __future__ import annotations

import unittest
from typing import Any

from asl_workbench import (
    freshness,
    incidents,
    observation,
    store_types,
)
from asl_workbench.operations import (
    FORBIDDEN_PERSISTED_KEYS,
)
from tests.support_workbench_store import (
    CONNECTOR_ID,
    NOW,
    IncidentCase,
    StoreCase,
    catalog_entry,
    history_entry,
    run_row,
)


class IncidentDeliveryTests(IncidentCase):
    """Persisted before it is attempted, and uncertainty recorded as uncertainty."""

    def ready(self) -> None:
        self.observe()
        self.at(121)
        self.observe()

    def test_a_refusal_to_send_retries_after_a_backoff(self) -> None:
        self.publisher.results = [incidents.PublishResult(incidents.DELIVERY_RETRY)]
        self.ready()
        self.engine.tick()
        row = self.diagnostics()[0]
        self.assertEqual(row["delivery_state"], incidents.DELIVERY_RETRY)
        self.assertEqual(row["detail_code"], incidents.DETAIL_NOT_SENT)
        self.assertEqual(row["attempts"], 1)
        # Still inside the backoff: the second tick must not send.
        self.engine.tick()
        self.assertEqual(len(self.publisher.calls), 1)
        self.at(121 + incidents.backoff_seconds(1) + 1)
        self.observe()
        self.engine.tick()
        self.assertEqual(len(self.publisher.calls), 2)
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_PUBLISHED
        )

    def test_backoff_doubles_and_then_stops_at_the_ceiling(self) -> None:
        self.assertEqual(incidents.backoff_seconds(0), 5.0)
        self.assertEqual(incidents.backoff_seconds(1), 10.0)
        self.assertEqual(incidents.backoff_seconds(5), 160.0)
        # A transport that has been down for an hour is not helped by being
        # asked every five seconds, so the interval stops growing rather than
        # running away.
        self.assertEqual(incidents.backoff_seconds(6), 300.0)
        self.assertEqual(incidents.backoff_seconds(50), 300.0)

    def test_an_ambiguous_delivery_is_recorded_and_never_replayed(self) -> None:
        self.publisher.results = [
            incidents.PublishResult(incidents.DELIVERY_UNCONFIRMED)
        ]
        self.ready()
        self.engine.tick()
        row = self.diagnostics()[0]
        self.assertEqual(row["delivery_state"], incidents.DELIVERY_UNCONFIRMED)
        self.assertEqual(row["detail_code"], incidents.DETAIL_DELIVERY_UNKNOWN)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(len(self.publisher.calls), 1)

    def test_a_crash_during_publishing_is_unconfirmed_on_restart(self) -> None:
        """A row left mid-send is the one thing a restart may not guess about.

        The process died between claiming the incident and learning what the
        transport did with it. Treating that as pending would double-send and
        treating it as published would hide a notification that never arrived,
        so it is recorded as the unknown it is and never attempted again.
        """
        self.ready()
        self.engine.tick()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE incident SET delivery_state = ?, detail_code = NULL, "
                "receipt_id = NULL, published_at = NULL",
                (incidents.DELIVERY_PUBLISHING,),
            )
        sent_before_the_crash = len(self.publisher.calls)
        self.restart()
        row = self.diagnostics()[0]
        self.assertEqual(row["delivery_state"], incidents.DELIVERY_UNCONFIRMED)
        self.assertEqual(row["detail_code"], incidents.DETAIL_RESTART_DURING_PUBLISH)
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(len(self.publisher.calls), sent_before_the_crash)

    def test_a_publisher_that_raises_establishes_nothing(self) -> None:
        class Exploding:
            def publish(self, **_: Any):
                raise RuntimeError("remote detail nobody should see")

        self.engine.publisher = Exploding()
        self.ready()
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_UNCONFIRMED
        )

    def test_resolution_before_the_send_stops_it_without_any_bytes(self) -> None:
        """The gate is consulted at the transport, not only when claiming.

        An answer that lands while the publisher is connecting should stop the
        notification, because the moment that matters is the one just before
        the bytes leave.
        """
        self.ready()

        def answer_arrives() -> None:
            self.at(122)
            self.observe([run_row("run-1")])

        self.publisher.before_gate = answer_arrives
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_CANCELLED
        )

    def test_a_deployment_that_does_not_publish_never_sends(self) -> None:
        quiet = observation.ObservationPolicy(
            observation_stale_seconds=60.0,
            lost_observation_alert_seconds=120.0,
            unanswered_question_alert_seconds=120.0,
            publish_incidents=False,
        )
        self.store.close()
        self.policy = quiet
        self.store = self.open_store(created_at=NOW)
        self.engine = incidents.IncidentEngine(
            store=self.store, clock=lambda: self.wall, publisher=self.publisher
        )
        self.ready()
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_PENDING
        )

    def test_many_stale_questions_cannot_starve_a_terminal_failure(self) -> None:
        """A candidate nobody may send steps aside rather than blocking the queue."""
        stale = [
            self.question_row(f"q-{index}", run_key=f"stale-{index}")
            for index in range(3)
        ]
        self.observe(stale)
        self.at(121)
        self.observe(stale)
        # The projection goes quiet, so every question becomes ineligible.
        self.at(400)
        self.send_history(
            generated_at=self.wall,
            entries=[
                history_entry(
                    "entry-failed",
                    recorded_at=self.wall,
                    run_finished_at=self.wall,
                    run_key="stale-2",
                    plan_key="plan-stale-2",
                    outcome="failed",
                )
            ],
            received_at=self.wall,
        )
        self.engine.tick()
        self.assertEqual(
            [call["kind"] for call in self.publisher.calls],
            [incidents.INCIDENT_TERMINAL_FAILURE],
        )

    def test_diagnostics_expose_state_and_never_prose(self) -> None:
        self.ready()
        self.engine.tick()
        row = self.diagnostics()[0]
        self.assertEqual(set(row), set(incidents.DIAGNOSTIC_FIELDS))
        self.assertEqual(set(row) & FORBIDDEN_PERSISTED_KEYS, set())
        self.assertEqual(self.engine.diagnostics(limit=0), self.diagnostics()[:1])

    def test_a_refused_snapshot_cannot_change_incident_state(self) -> None:
        self.ready()
        self.engine.tick()
        published = self.diagnostics()[0]
        with self.assertRaises(store_types.IngestRefused):
            self.send_snapshot(
                observed_at=NOW - 100, runs=[run_row("run-1")], received_at=self.wall
            )
        self.assertEqual(self.diagnostics()[0], published)


# --------------------------------------------------------------------------
# Summary-only answers
# --------------------------------------------------------------------------


class SummaryTests(StoreCase):
    """What a summary credential is entitled to: numbers, and no identity."""

    def test_counts_carry_no_identifier(self) -> None:
        self.send_heartbeat(observed_at=NOW - 10)
        self.send_snapshot(
            observed_at=NOW - 10,
            runs=[
                run_row("alpha"),
                run_row(
                    "beta",
                    urgency_group="needs-action",
                    primary_state="paused-for-reply",
                    question_id="q-1",
                    unanswered_count=1,
                ),
            ],
        )
        self.send_history(
            generated_at=NOW - 20, entries=[history_entry("e1", recorded_at=NOW - 20)]
        )
        self.send_catalog(
            observed_at=NOW - 10, generation="gen-1", entries=[catalog_entry("plan-a")]
        )
        document = self.store.counts(now=NOW).as_document()
        self.assertEqual(
            document,
            {
                "runs": 2,
                "needs_attention": 1,
                "running": 1,
                "absent_runs": 0,
                "unconfirmed_runs": 0,
                "history_entries": 1,
                "connectors": 1,
                "plans": 1,
                "absent_plans": 0,
                "unconfirmed_plans": 0,
            },
        )
        for value in document.values():
            self.assertIsInstance(value, int)

    def test_a_status_document_names_its_source_and_nothing_inside_it(self) -> None:
        self.send_heartbeat(observed_at=NOW - 5)
        self.send_snapshot(observed_at=NOW - 5, runs=[run_row("alpha")])
        document = self.store.connector_statuses(now=NOW)[0].as_document()
        self.assertEqual(document["connector_id"], CONNECTOR_ID)
        self.assertEqual(document["freshness"], freshness.FRESHNESS_LIVE)
        self.assertTrue(document["absence_proves_removal"])
        self.assertEqual(set(document) & FORBIDDEN_PERSISTED_KEYS, set())
        self.assertNotIn("run_key", document)

    def test_a_deployment_with_no_source_knows_that_it_knows_nothing(self) -> None:
        self.assertEqual(self.store.connector_statuses(now=NOW), [])
        self.assertEqual(freshness.aggregate_freshness([]), freshness.FRESHNESS_LOST)
        self.assertEqual(self.store.counts(now=NOW).connectors, 0)

    def test_an_unsupported_contract_is_reported_rather_than_ingested_blindly(
        self,
    ) -> None:
        self.send_heartbeat(observed_at=NOW - 5, contract_set_id="asl-amx-99")
        self.send_snapshot(
            observed_at=NOW - 5,
            runs=[run_row("alpha")],
            contract_set_id="asl-amx-99",
        )
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertFalse(status.compatible)
        self.assertEqual(status.freshness.label, freshness.FRESHNESS_INCOMPATIBLE)
        self.assertFalse(status.freshness.replies_enabled)
        # Stored, and never used as incident evidence.
        self.assertEqual(len(self.store.runs(now=NOW)), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
