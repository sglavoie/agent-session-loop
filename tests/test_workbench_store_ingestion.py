"""Ordered ingestion, omission semantics and history uploads."""

from __future__ import annotations

from asl_workbench import (
    freshness,
    store_types,
)
from tests.support_workbench_store import (
    NOW,
    StoreCase,
    envelope,
    heartbeat_payload,
    history_entry,
    run_row,
    snapshot_payload,
)

ASL_TEST_PORTABLE = True


class OrderedIngestionTests(StoreCase):
    """Time orders; the generation counter only breaks ties."""

    def test_a_snapshot_stores_every_run_it_carries(self) -> None:
        result = self.send_snapshot(
            observed_at=NOW - 1,
            runs=[run_row("alpha"), run_row("beta", project="scripts")],
        )
        self.assertEqual(result.stored, 2)
        self.assertFalse(result.duplicate)
        self.assertEqual(self.run_keys(), {"alpha", "beta"})

    def test_an_identical_redelivery_is_a_duplicate_and_changes_nothing(self) -> None:
        payload = snapshot_payload(observed_at=NOW - 1, runs=[run_row("alpha")])
        first = self.store.ingest_snapshot(
            envelope("snapshot", payload), received_at=NOW - 1
        )
        second = self.store.ingest_snapshot(
            envelope("snapshot", payload), received_at=NOW - 1
        )
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(self.store.runs(now=NOW)), 1)

    def test_a_repeated_heartbeat_instant_is_a_duplicate(self) -> None:
        self.send_heartbeat(observed_at=NOW - 10)
        self.assertTrue(self.send_heartbeat(observed_at=NOW - 10).duplicate)

    def test_an_older_observation_is_refused_as_stale(self) -> None:
        self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        with self.assertRaises(store_types.IngestRefused) as refusal:
            self.send_snapshot(observed_at=NOW - 20, runs=[run_row("beta")])
        self.assertEqual(refusal.exception.code, store_types.REFUSED_STALE_OBSERVATION)
        self.assertEqual(self.run_keys(), {"alpha"})

    def test_a_superseded_bridge_session_is_refused_as_a_stale_generation(
        self,
    ) -> None:
        """Same instant, older bridge: the generation is what breaks the tie."""
        self.send_snapshot(observed_at=NOW - 10, generation=5, runs=[run_row("alpha")])
        with self.assertRaises(store_types.IngestRefused) as refusal:
            self.send_snapshot(
                observed_at=NOW - 10, generation=3, runs=[run_row("beta")]
            )
        self.assertEqual(refusal.exception.code, store_types.REFUSED_STALE_GENERATION)
        self.assertEqual(self.run_keys(), {"alpha"})

    def test_a_restarted_connector_is_not_locked_out_by_its_reset_counter(
        self,
    ) -> None:
        """Ordering on the counter would refuse a restarted Mac forever."""
        self.send_snapshot(observed_at=NOW - 10, generation=7, runs=[run_row("alpha")])
        self.send_snapshot(observed_at=NOW - 5, generation=1, runs=[run_row("beta")])
        self.assertEqual(self.run_keys(), {"beta"})

    def test_an_old_heartbeat_cannot_overwrite_a_newer_one(self) -> None:
        self.send_heartbeat(observed_at=NOW - 10, generation=4)
        with self.assertRaises(store_types.IngestRefused) as refusal:
            self.send_heartbeat(observed_at=NOW - 20, generation=4)
        self.assertEqual(refusal.exception.code, store_types.REFUSED_STALE_OBSERVATION)
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.last_heartbeat_at, NOW - 10)

    def test_a_superseded_heartbeat_at_the_same_instant_changes_nothing(self) -> None:
        """Two bridges at one instant: the second is a retry, not a rewrite.

        Unlike a snapshot, a heartbeat carries no rows, so the older generation
        has nothing to overwrite and is absorbed as the duplicate it is.
        """
        self.send_heartbeat(observed_at=NOW - 10, generation=4)
        again = self.send_heartbeat(observed_at=NOW - 10, generation=2)
        self.assertTrue(again.duplicate)
        with self.store.reading() as connection:
            row = connection.execute("SELECT * FROM connector").fetchone()
        self.assertEqual(row["heartbeat_generation"], 4)

    def test_a_snapshot_that_repeats_a_run_is_refused_without_partial_writes(
        self,
    ) -> None:
        """Atomicity proven on the rows: the refused snapshot leaves no trace.

        The repeat is only detectable part-way through the row loop, so rows
        have already been written when it is found. What the reader sees
        afterwards is the previous snapshot, entire.
        """
        self.send_snapshot(
            observed_at=NOW - 20, runs=[run_row("alpha"), run_row("beta")]
        )
        before = self.store.connector_statuses(now=NOW)[0]
        with self.assertRaises(store_types.IngestInvalid):
            self.send_snapshot(
                observed_at=NOW - 10,
                runs=[run_row("gamma"), run_row("delta"), run_row("gamma")],
            )
        after = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(self.run_keys(include_absent=True), {"alpha", "beta"})
        self.assertEqual(after.last_snapshot_at, before.last_snapshot_at)
        self.assertEqual(after.row_count, 2)

    def test_a_future_dated_observation_is_refused_and_sets_no_high_water(
        self,
    ) -> None:
        """A fast clock must not lock out every correctly stamped frame after it."""
        self.send_snapshot(observed_at=NOW, runs=[run_row("alpha")])
        for send in (
            lambda: self.store.ingest_heartbeat(
                envelope("heartbeat", heartbeat_payload(observed_at=NOW + 3600)),
                received_at=NOW + 5,
            ),
            lambda: self.send_snapshot(
                observed_at=NOW + 3600, runs=[run_row("alpha")], received_at=NOW + 5
            ),
            lambda: self.send_catalog(
                observed_at=NOW + 3600, generation="g1", received_at=NOW + 5
            ),
        ):
            with self.assertRaises(store_types.IngestRefused) as refusal:
                send()
            self.assertEqual(
                refusal.exception.code, store_types.REFUSED_FUTURE_OBSERVATION
            )
        self.send_snapshot(observed_at=NOW + 10, runs=[run_row("beta")])
        self.assertEqual(self.run_keys(), {"beta"})

    def test_a_clock_ahead_within_the_staleness_bound_is_ordinary_skew(
        self,
    ) -> None:
        self.store.ingest_heartbeat(
            envelope("heartbeat", heartbeat_payload(observed_at=NOW + 30)),
            received_at=NOW,
        )
        self.send_snapshot(
            observed_at=NOW + 30, runs=[run_row("alpha")], received_at=NOW
        )
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.freshness.label, freshness.FRESHNESS_LIVE)

    def test_an_observation_far_ahead_of_now_is_not_fresh(self) -> None:
        evaluated = freshness.evaluate(
            now=NOW,
            policy=self.store.policy,
            last_heartbeat_at=NOW + 3600,
            last_snapshot_at=NOW + 3600,
            truncated=False,
        )
        self.assertFalse(evaluated.transport_fresh)
        self.assertFalse(evaluated.projection_fresh)
        self.assertFalse(evaluated.replies_enabled)
        self.assertNotEqual(evaluated.label, freshness.FRESHNESS_LIVE)
        self.assertIn(freshness.REASON_FUTURE_PROJECTION, evaluated.reasons)

    def test_an_unknown_refusal_code_cannot_be_invented(self) -> None:
        with self.assertRaises(AssertionError):
            raise store_types.IngestRefused("made-up", "detail")


# --------------------------------------------------------------------------
# Omission
# --------------------------------------------------------------------------


class OmissionTests(StoreCase):
    """Omission is evidence only from a view that claimed to be whole."""

    def test_a_complete_snapshot_marks_the_runs_it_omits_absent(self) -> None:
        self.send_snapshot(
            observed_at=NOW - 20, runs=[run_row("alpha"), run_row("beta")]
        )
        result = self.send_snapshot(observed_at=NOW - 10, runs=[run_row("alpha")])
        self.assertEqual(result.absent, 1)
        self.assertEqual(self.run_keys(), {"alpha"})
        self.assertEqual(self.run_keys(include_absent=True), {"alpha", "beta"})

    def test_a_truncated_snapshot_never_proves_a_run_disappeared(self) -> None:
        """The omitted rows are unknown, not gone. Nothing is marked absent."""
        self.send_heartbeat(observed_at=NOW - 20)
        self.send_snapshot(
            observed_at=NOW - 20, runs=[run_row("alpha"), run_row("beta")]
        )
        result = self.send_snapshot(
            observed_at=NOW - 10, runs=[run_row("alpha")], truncated=True
        )
        self.assertEqual(result.absent, 0)
        rows = self.rows_by_key()
        self.assertEqual(set(rows), {"alpha", "beta"})
        self.assertEqual(rows["alpha"]["observation_status"], "current")
        self.assertEqual(rows["beta"]["observation_status"], "unconfirmed")
        self.assertEqual(rows["beta"]["last_observed_at"], NOW - 20)
        # An unconfirmed row is not a command target: nobody can see it now.
        self.assertIsNone(self.store.command_target("beta"))
        counts = self.store.counts(now=NOW)
        self.assertEqual(counts.unconfirmed_runs, 1)
        self.assertEqual(counts.running, 1)
        self.send_heartbeat(observed_at=NOW - 5)
        self.assertEqual(self.store.counts(now=NOW).unconfirmed_runs, 1)
        self.send_snapshot(
            observed_at=NOW,
            runs=[run_row("alpha"), run_row("beta")],
            truncated=True,
        )
        self.assertEqual(self.store.counts(now=NOW).unconfirmed_runs, 0)
        self.assertIsNotNone(self.store.command_target("beta"))

    def test_partial_replacement_retires_only_positively_superseded_runs(
        self,
    ) -> None:
        old = run_row("old", plan_key="shared-plan", run_started_at=NOW - 1000)
        unrelated = run_row(
            "unrelated",
            urgency_group="needs-action",
            primary_state="paused-for-reply",
            question_id="q-unrelated",
            unanswered_count=1,
        )
        self.send_snapshot(observed_at=NOW - 20, runs=[old, unrelated])
        replacement = run_row("new", plan_key="shared-plan", run_started_at=NOW - 10)
        result = self.send_snapshot(
            observed_at=NOW - 5, runs=[replacement], truncated=True
        )
        self.assertEqual(result.absent, 1)
        rows = self.rows_by_key(include_absent=True)
        self.assertEqual(rows["old"]["observation_status"], "absent")
        self.assertEqual(rows["new"]["observation_status"], "current")
        self.assertEqual(rows["unrelated"]["observation_status"], "unconfirmed")
        self.assertEqual(rows["unrelated"]["question_id"], "q-unrelated")
        self.assertIsNone(self.store.command_target("old"))
        self.assertEqual(self.store.counts(now=NOW).needs_attention, 0)
        self.assertEqual(self.store.counts(now=NOW).unconfirmed_runs, 1)

    def test_older_or_unknown_run_start_is_not_positive_replacement_evidence(
        self,
    ) -> None:
        self.send_snapshot(
            observed_at=NOW - 20,
            runs=[run_row("new", plan_key="shared-plan", run_started_at=NOW - 100)],
        )
        self.send_snapshot(
            observed_at=NOW - 10,
            runs=[run_row("older", plan_key="shared-plan", run_started_at=NOW - 200)],
            truncated=True,
        )
        self.assertEqual(self.rows_by_key()["new"]["observation_status"], "unconfirmed")
        self.send_snapshot(
            observed_at=NOW,
            runs=[run_row("unknown", plan_key="shared-plan")],
            truncated=True,
        )
        self.assertEqual(len(self.store.runs(now=NOW)), 3)

    def test_replacement_preserves_terminal_history_and_separates_connectors(
        self,
    ) -> None:
        old = run_row("old", plan_key="shared-plan", run_started_at=NOW - 1000)
        self.send_snapshot(observed_at=NOW - 20, runs=[old])
        self.send_history(
            generated_at=NOW - 15,
            entries=[
                history_entry(
                    "old-history",
                    run_key="old",
                    plan_key="shared-plan",
                    recorded_at=NOW - 15,
                )
            ],
        )
        self.send_heartbeat(observed_at=NOW - 20, connector_id="another-mac")
        self.send_snapshot(observed_at=NOW - 20, runs=[old], connector_id="another-mac")
        self.send_snapshot(
            observed_at=NOW - 5,
            runs=[run_row("new", plan_key="shared-plan", run_started_at=NOW - 10)],
            truncated=True,
        )
        self.assertEqual(self.store.history(now=NOW)[0]["entry_id"], "old-history")
        self.assertEqual(
            self.store.command_target("old")["connector_id"], "another-mac"
        )

    def test_completeness_is_reported_per_source_rather_than_as_one_flag(
        self,
    ) -> None:
        self.send_heartbeat(observed_at=NOW - 5)
        self.send_snapshot(observed_at=NOW - 5, runs=[run_row("alpha")], truncated=True)
        self.send_history(generated_at=NOW - 5, entries=[], complete=False)
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.runs.completeness, freshness.COMPLETENESS_PARTIAL)
        self.assertEqual(status.history.completeness, freshness.COMPLETENESS_PARTIAL)
        self.assertEqual(status.catalog.completeness, freshness.COMPLETENESS_UNKNOWN)
        self.assertEqual(status.completeness, freshness.COMPLETENESS_PARTIAL)
        self.assertTrue(status.degraded)
        self.assertFalse(status.freshness.absence_proves_removal)

    def test_a_slow_walk_is_not_counted_twice_against_connector_freshness(
        self,
    ) -> None:
        """``observed_at`` is the walk's start; its duration is not extra age."""
        self.send_heartbeat(observed_at=NOW)
        self.send_snapshot(
            observed_at=NOW - 40,
            runs=[run_row("alpha")],
            received_at=NOW,
            projection_age_seconds=40.0,
        )
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.freshness.projection_age_seconds, 40.0)
        self.assertEqual(status.freshness.label, freshness.FRESHNESS_LIVE)
        self.assertTrue(status.freshness.replies_enabled)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


class HistoryTests(StoreCase):
    """An ending does not change, so a batch upserts rather than duplicating."""

    def test_history_upserts_rather_than_duplicating(self) -> None:
        entries = [
            history_entry("e1", recorded_at=NOW - 100),
            history_entry("e2", recorded_at=NOW - 200, outcome="failed"),
        ]
        first = self.send_history(generated_at=NOW - 50, entries=entries)
        self.assertEqual(first.stored, 2)
        second = self.send_history(
            generated_at=NOW - 40, entries=entries, batch_id="second"
        )
        self.assertEqual((second.stored, second.updated), (0, 2))
        self.assertTrue(second.duplicate)
        self.assertEqual(len(self.store.history(now=NOW)), 2)

    def test_a_late_batch_cannot_overwrite_a_newer_completeness_claim(self) -> None:
        self.send_history(generated_at=NOW - 10, entries=[], complete=True)
        self.send_history(
            generated_at=NOW - 60, entries=[], complete=False, batch_id="late"
        )
        status = self.store.connector_statuses(now=NOW)[0]
        self.assertEqual(status.history.completeness, freshness.COMPLETENESS_COMPLETE)

    def test_a_stored_ending_keeps_its_lists_and_flags(self) -> None:
        self.send_history(
            generated_at=NOW - 10,
            entries=[
                history_entry(
                    "e1",
                    recorded_at=NOW - 10,
                    completed_session_ids=["s1", "s2"],
                    review_completed=True,
                    reconstructed=False,
                )
            ],
        )
        row = self.store.history(now=NOW)[0]
        self.assertEqual(row["completed_session_ids"], ["s1", "s2"])
        self.assertIs(row["review_completed"], True)
        self.assertIs(row["reconstructed"], False)

    def test_history_filters_by_outcome_and_harness(self) -> None:
        self.send_history(
            generated_at=NOW - 10,
            entries=[
                history_entry("good", recorded_at=NOW - 10, harness="claude"),
                history_entry(
                    "bad", recorded_at=NOW - 20, outcome="failed", harness="codex"
                ),
            ],
        )
        self.assertEqual(
            [row["entry_id"] for row in self.store.history(now=NOW, outcome="failed")],
            ["bad"],
        )
        self.assertEqual(
            [row["entry_id"] for row in self.store.history(now=NOW, harness="claude")],
            ["good"],
        )
        self.assertEqual(self.store.harnesses(now=NOW), ["claude", "codex"])


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------
