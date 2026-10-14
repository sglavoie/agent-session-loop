"""Incident identity and resolution."""

from __future__ import annotations

from asl_workbench import (
    incidents,
)
from tests.support_workbench_store import (
    CONNECTOR_ID,
    NOW,
    IncidentCase,
    history_entry,
    run_row,
)


class IncidentIdentityTests(IncidentCase):
    """A repeat is the same incident, and a different problem is a new one."""

    def test_identity_is_deterministic_and_connector_scoped(self) -> None:
        first = incidents.identity("unanswered-question", "mac-a", "run-1", "q-1")
        self.assertEqual(
            first, incidents.identity("unanswered-question", "mac-a", "run-1", "q-1")
        )
        self.assertNotEqual(
            first, incidents.identity("unanswered-question", "mac-b", "run-1", "q-1")
        )
        self.assertNotEqual(
            first, incidents.identity("unanswered-question", "mac-a", "run-1", "q-2")
        )

    def test_a_repeated_observation_produces_one_incident(self) -> None:
        self.observe()
        self.at(121)
        self.observe()
        for _ in range(4):
            self.engine.tick()
        self.assertEqual(len(self.diagnostics()), 1)
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.publisher.calls[0]["question_id"], "q-1")

    def test_a_different_question_is_a_different_incident(self) -> None:
        self.observe()
        self.at(121)
        self.observe()
        self.engine.tick()
        self.at(122)
        self.observe([self.question_row("q-2")])
        self.at(243)
        self.observe([self.question_row("q-2")])
        self.engine.tick()
        rows = self.diagnostics()
        self.assertEqual(len({row["incident_id"] for row in rows}), 2)
        self.assertEqual(sum(row["resolved_at"] is not None for row in rows), 1)

    def test_the_timer_measures_the_wait_and_not_each_snapshot(self) -> None:
        """Restarting the clock per snapshot means a question is never late."""
        self.observe()
        self.engine.tick()
        self.assertEqual(self.diagnostics(), [])
        self.at(120)
        self.observe()
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])
        self.at(120.001)
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_PUBLISHED
        )

    def test_resuming_the_same_question_recovers_its_original_wait_age(self) -> None:
        question = self.question_row()
        question["question_asked_at"] = NOW
        self.observe([question])
        self.at(119)
        self.observe([run_row("run-1")])
        self.at(120)
        self.observe([question])
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])
        self.at(120.001)
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_PUBLISHED
        )

    def test_an_unknown_incident_kind_is_refused(self) -> None:
        with self.store.transaction() as connection, self.assertRaises(ValueError):
            incidents.record(
                connection,
                kind="made-up",
                connector_id=CONNECTOR_ID,
                run_key="run-1",
                plan_key="plan-run-1",
                project="pi-monitoring",
                now=NOW,
            )


class IncidentResolutionTests(IncidentCase):
    """Fresh evidence resolves; a partial view does not."""

    def test_partial_omission_does_not_resolve_and_complete_absence_does(self) -> None:
        self.observe()
        self.at(121)
        self.observe()
        self.publisher.results = [incidents.PublishResult(incidents.DELIVERY_RETRY)]
        self.engine.tick()
        self.at(122)
        self.observe([run_row("other")], truncated=True)
        self.assertIsNone(self.diagnostics()[0]["resolved_at"])
        self.at(123)
        self.observe([])
        self.at(200)
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_CANCELLED
        )
        self.assertEqual(len(self.publisher.calls), 1)

    def test_a_reconnect_with_an_answer_discards_the_delayed_notification(
        self,
    ) -> None:
        self.observe()
        self.at(300)
        self.engine.tick()
        self.observe([run_row("run-1")])
        self.engine.tick()
        self.assertEqual(
            self.diagnostics()[0]["delivery_state"], incidents.DELIVERY_CANCELLED
        )
        self.assertEqual(self.publisher.calls, [])

    def test_a_superseded_run_loses_its_watch_and_keeps_its_history(self) -> None:
        old = self.question_row(run_started_at=NOW - 1000)
        self.observe([old])
        self.at(301)
        self.observe([old])
        self.engine.publisher = None
        self.engine.tick()
        self.engine.publisher = self.publisher
        self.assertTrue(
            any(
                row["delivery_state"] == incidents.DELIVERY_PENDING
                for row in self.diagnostics()
            )
        )
        self.at(302)
        self.observe(
            [run_row("replacement", plan_key="plan-run-1", run_started_at=NOW + 300)],
            truncated=True,
        )
        rows = self.diagnostics()
        # Retired, not erased: what the store keeps about a superseded run is
        # the record that it was superseded.
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(row["resolved_at"] is not None for row in rows))
        self.assertTrue(
            all(row["delivery_state"] == incidents.DELIVERY_CANCELLED for row in rows)
        )
        self.engine.tick()
        self.assertEqual(self.publisher.calls, [])

    def test_a_terminal_failure_alerts_and_a_completion_does_not(self) -> None:
        self.at(10)
        self.observe([run_row("run-1")])
        self.at(20)
        self.send_history(
            generated_at=self.wall,
            entries=[
                history_entry(
                    "entry-1",
                    recorded_at=self.wall,
                    run_finished_at=self.wall,
                    run_key="run-1",
                    plan_key="plan-run-1",
                    outcome="failed",
                )
            ],
            received_at=self.wall,
        )
        self.engine.tick()
        rows = self.diagnostics()
        self.assertEqual(
            [row["kind"] for row in rows], [incidents.INCIDENT_TERMINAL_FAILURE]
        )
        self.assertEqual(len(self.publisher.calls), 1)

    def test_an_ending_older_than_the_monitoring_window_never_alerts(self) -> None:
        """A newly discovered plan's first import is a week of old endings."""
        self.at(10)
        self.send_history(
            generated_at=self.wall,
            entries=[
                history_entry(
                    "entry-old",
                    recorded_at=NOW - 10000,
                    run_finished_at=NOW - 10000,
                    outcome="failed",
                )
            ],
            received_at=self.wall,
        )
        self.engine.tick()
        self.assertEqual(self.diagnostics(), [])

    def test_losing_sight_of_active_work_alerts_and_recovery_resolves_it(
        self,
    ) -> None:
        self.observe([run_row("run-1")])
        self.at(121)
        self.engine.tick()
        rows = self.diagnostics()
        self.assertEqual(
            [row["kind"] for row in rows], [incidents.INCIDENT_LOST_OBSERVATION]
        )
        self.at(122)
        self.observe([run_row("run-1")])
        self.assertIsNotNone(self.diagnostics()[0]["resolved_at"])

    def test_a_slow_walk_is_not_counted_twice_against_freshness(self) -> None:
        """``observed_at`` is the walk's start; its duration is not extra age."""
        self.observe([run_row("run-1")])
        self.at(100)
        self.observe(
            [run_row("run-1")],
            observed_at=self.wall - 35,
            projection_age_seconds=35.0,
        )
        self.at(150)
        self.engine.tick()
        self.assertEqual(self.diagnostics(), [])

    def test_a_snapshot_older_than_the_stale_bound_is_not_evidence(self) -> None:
        self.observe([run_row("run-1")])
        self.at(100)
        self.observe([run_row("run-1")], observed_at=self.wall - 61)
        self.at(150)
        self.engine.tick()
        self.assertEqual(
            [row["kind"] for row in self.diagnostics()],
            [incidents.INCIDENT_LOST_OBSERVATION],
        )

    def test_a_paused_run_is_not_lost_active_work(self) -> None:
        self.observe([run_row("run-1", primary_state="paused-for-reply")])
        self.at(300)
        self.engine.tick()
        self.assertEqual(
            [row["kind"] for row in self.diagnostics()],
            [],
        )
