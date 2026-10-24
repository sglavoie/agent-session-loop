"""The whole path against a real corpus: delivery, repeats, and reconnects.

Split from tests/test_workbench_connector.py."""

from __future__ import annotations

from asl_workbench import catalog, history
from asl_workbench.transport import (
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
)
from tests.workbench_connector_support import (
    CONNECTOR_ID,
    NOW,
    CorpusTestCase,
    FakeChannel,
    drain,
    ingest,
    make_connector,
)

ASL_TEST_PORTABLE = True


class CorpusDeliveryTests(CorpusTestCase):
    """A real corpus reaches a real store, accurately."""

    def test_one_pass_delivers_all_four_views(self) -> None:
        db = self.open_store()
        _, channel, _ = self.publish(db)
        self.assertEqual(
            {VIEW_HEARTBEAT, VIEW_SNAPSHOT, VIEW_CATALOG_PAGE, VIEW_HISTORY_BATCH},
            set(channel.views()),
        )

    def test_the_store_holds_the_plans_the_corpus_holds(self) -> None:
        db = self.open_store()
        self.publish(db)
        stored = {
            row["plan_key"]: row for row in db.catalog(now=NOW, include_absent=True)
        }
        self.assertEqual(
            {
                catalog.plan_key(str(self.paused)),
                catalog.plan_key(str(self.finished)),
            },
            set(stored),
        )
        paused = stored[catalog.plan_key(str(self.paused))]
        self.assertEqual("Paused plan", paused["title"])
        self.assertEqual("paused-for-reply", paused["primary_state"])
        self.assertEqual(
            "Finished plan", stored[catalog.plan_key(str(self.finished))]["title"]
        )

    def test_the_store_holds_the_active_run_and_not_the_settled_one(self) -> None:
        # A catalog carries every plan; a snapshot carries what an operator can
        # act on. A complete plan is not actionable work.
        db = self.open_store()
        self.publish(db)
        rows = db.runs(now=NOW)
        self.assertEqual(["run-paused"], [row["run_id"] for row in rows])
        self.assertEqual("paused-for-reply", rows[0]["primary_state"])
        # The run and the catalog entry agree on the run key, which is how the
        # cockpit joins the two views it was sent separately.
        paused = next(
            row
            for row in db.catalog(now=NOW)
            if row["plan_key"] == catalog.plan_key(str(self.paused))
        )
        self.assertEqual(paused["run_key"], rows[0]["run_key"])
        self.assertEqual("Paused plan", rows[0]["plan_title"])

    def test_the_store_holds_the_ending_the_corpus_recorded(self) -> None:
        db = self.open_store()
        self.publish(db)
        entries = db.history(now=NOW)
        self.assertEqual(["run-finished"], [entry["run_id"] for entry in entries])
        self.assertEqual(history.HISTORY_OUTCOME_COMPLETED, entries[0]["outcome"])

    def test_the_connector_is_read_back_as_a_live_observation_only_source(
        self,
    ) -> None:
        db = self.open_store()
        self.publish(db)
        statuses = {
            status.connector_id: status for status in db.connector_statuses(now=NOW)
        }
        status = statuses[CONNECTOR_ID]
        self.assertTrue(status.bridge_alive)
        self.assertTrue(status.compatible)
        self.assertIsNone(status.degraded_reason)
        self.assertEqual(1, status.row_count)
        self.assertFalse(status.truncated)

    def test_the_catalog_generation_the_snapshot_names_is_the_one_delivered(
        self,
    ) -> None:
        db = self.open_store()
        _, channel, _ = self.publish(db)
        snapshot = channel.payloads(VIEW_SNAPSHOT)[0]
        page = channel.payloads(VIEW_CATALOG_PAGE)[0]
        self.assertEqual(page["generation"], snapshot["source_digest"])

    def test_the_final_page_closes_a_complete_generation(self) -> None:
        db = self.open_store()
        _, channel, _ = self.publish(db)
        pages = channel.payloads(VIEW_CATALOG_PAGE)
        self.assertTrue(pages[-1]["final"])
        self.assertTrue(pages[-1]["complete"])
        self.assertEqual(2, pages[-1]["total"])

    def test_a_pass_over_an_unchanged_corpus_repeats_rather_than_duplicates(
        self,
    ) -> None:
        db = self.open_store()
        source = self.make_source()
        self.publish(db, source=source)
        before = (
            [row["run_id"] for row in db.runs(now=NOW)],
            [row["plan_key"] for row in db.catalog(now=NOW)],
            [entry["entry_id"] for entry in db.history(now=NOW)],
        )
        self.publish(db, source=source, start=NOW + 120.0)
        after = (
            [row["run_id"] for row in db.runs(now=NOW + 120.0)],
            [row["plan_key"] for row in db.catalog(now=NOW + 120.0)],
            [entry["entry_id"] for entry in db.history(now=NOW + 120.0)],
        )
        self.assertEqual(before, after)

    def test_a_reconnect_across_a_dropped_link_duplicates_nothing(self) -> None:
        db = self.open_store()
        source = self.make_recording_source()
        channel = FakeChannel()
        connector = make_connector(source, channel)

        # One full pass lands, then the link drops mid-way through the next.
        drain(connector, start=NOW, steps=16)
        self.assertEqual(1, len(source.committed))
        channel.failing = True
        connector.step(NOW + 120.0)
        channel.failing = False
        drain(connector, start=NOW + 240.0, steps=16)

        for frame in channel.frames:
            ingest(db, frame, at=NOW + 240.0)
        self.assertEqual(
            1, len(db.runs(now=NOW + 240.0)), "a reconnect duplicated a run"
        )
        self.assertEqual(
            2, len(db.catalog(now=NOW + 240.0)), "a reconnect duplicated a plan"
        )
        self.assertEqual(
            1, len(db.history(now=NOW + 240.0)), "a reconnect duplicated an ending"
        )
        self.assertGreater(connector.generation, 1)

    def test_a_redelivered_corpus_frame_is_recognised_as_the_same_frame(self) -> None:
        # The heartbeat is excluded on purpose: it is the one view whose
        # re-arrival is refused outright rather than absorbed, because a
        # replayed liveness signal would revive a connector that has gone away.
        db = self.open_store()
        _, channel, _ = self.publish(db)
        corpus = [
            frame
            for frame in channel.frames
            if frame["payload"]["view"] != VIEW_HEARTBEAT
        ]
        self.assertEqual(3, len(corpus))
        again = [ingest(db, frame, at=NOW) for frame in corpus]
        self.assertTrue(all(result.duplicate for result in again))

    def test_an_ending_survives_a_shutdown_that_interrupted_its_delivery(self) -> None:
        # Nothing is committed unless the whole cycle landed, so the window is
        # re-read rather than lost.
        db = self.open_store()
        source = self.make_recording_source()
        channel = FakeChannel()
        connector = make_connector(source, channel)
        connector.step(NOW)
        connector.step(NOW + 1.0)
        connector.stop()
        self.assertEqual([], source.committed)

        recovered = make_connector(
            self.make_source(checkpoints=source.inner.checkpoints), channel
        )
        drain(recovered, start=NOW + 120.0, steps=16)
        for frame in channel.frames:
            ingest(db, frame, at=NOW + 120.0)
        self.assertEqual(
            ["run-finished"],
            [entry["run_id"] for entry in db.history(now=NOW + 120.0)],
        )

    def test_a_committed_window_is_not_re_sent(self) -> None:
        source = self.make_source()
        channel = FakeChannel()
        connector = make_connector(source, channel)
        drain(connector, start=NOW, steps=16)
        first = channel.payloads(VIEW_HISTORY_BATCH)
        self.assertEqual(1, len(first))
        self.assertEqual(1, first[0]["entry_count"])

        drain(connector, start=NOW + 120.0, steps=16)
        later = channel.payloads(VIEW_HISTORY_BATCH)[1:]
        self.assertTrue(later)
        self.assertEqual(
            [0] * len(later),
            [batch["entry_count"] for batch in later],
            "a committed window was imported again",
        )
