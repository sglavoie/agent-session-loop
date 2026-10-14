"""Observation is read-only, privacy gate, and page caps against a real corpus.

Split from tests/test_workbench_connector.py."""

from __future__ import annotations

import json
import re
import unittest
from unittest import mock

from agent_session_loop import (
    bridge_projection,
    intervention,
    state,
    workbench_history,
)
from asl_workbench import catalog
from asl_workbench import connector_support as portable_support
from asl_workbench.transport import (
    MAX_CATALOG_ENTRIES_PER_PAGE,
    MAX_RUNS_PER_SNAPSHOT,
    RUN_SHAPE,
    VIEW_CATALOG_PAGE,
    VIEW_SNAPSHOT,
)
from tests.workbench_connector_support import (
    NOW,
    CorpusTestCase,
    FakeChannel,
    drain,
    ingest,
    iso,
    make_connector,
    summary_payload,
    tree_digest,
)


class ObservationIsReadOnlyTests(CorpusTestCase):
    """Describing this corpus does not change it."""

    def test_repeated_passes_leave_every_byte_of_the_state_tree_alone(self) -> None:
        source = self.make_source()
        before = tree_digest(self.state_root)
        plans_before = tree_digest(self.plans_root)
        for index in range(3):
            source.observe(NOW + index)
        after = tree_digest(self.state_root)
        self.assertEqual(before, after)
        self.assertEqual(plans_before, tree_digest(self.plans_root))

    def test_a_stale_lock_is_inspected_and_never_reclaimed(self) -> None:
        lock_path = (
            state.plan_state_dir(self.paused, self.state_root) / state.LOCK_FILE_NAME
        )
        before = lock_path.read_bytes()
        self.make_source().observe(NOW)
        self.assertTrue(lock_path.exists(), "observation reclaimed a stale lock")
        self.assertEqual(before, lock_path.read_bytes())

    def test_a_whole_publication_pass_writes_only_the_checkpoint(self) -> None:
        source = self.make_source()
        before = tree_digest(self.state_root)
        connector = make_connector(source, FakeChannel())
        drain(connector, steps=16)
        after = tree_digest(self.state_root)
        changed = {
            name
            for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        }
        self.assertEqual(
            {workbench_history.CHECKPOINT_FILE_NAME},
            changed,
            "a publication pass wrote something other than its own checkpoint",
        )

    def test_the_source_never_commits_on_its_own(self) -> None:
        source = self.make_source()
        source.observe(NOW)
        self.assertFalse(
            (self.state_root / workbench_history.CHECKPOINT_FILE_NAME).exists()
        )


class PrivacyTests(CorpusTestCase):
    """A remote service may hold the corpus and not the disk it lives on."""

    def rendered_frames(self) -> str:
        channel = FakeChannel()
        connector = make_connector(self.make_source(), channel)
        drain(connector, steps=16)
        self.assertTrue(channel.frames)
        return json.dumps(channel.frames, sort_keys=True)

    def test_no_frame_carries_a_path_from_this_machine(self) -> None:
        rendered = self.rendered_frames()
        for leaked in (
            str(self.root),
            str(self.plans_root),
            str(self.paused),
            str(self.state_root),
        ):
            self.assertNotIn(leaked, rendered, f"a frame leaked {leaked}")

    def test_no_catalog_entry_carries_a_field_the_catalog_calls_private(self) -> None:
        # `catalog.PRIVATE_FIELDS` is the partition the catalog already made
        # between what a cache may hold and what it may not, so the assertion
        # is against that list rather than against a second list kept here.
        # The snapshot is a different projection with its own declared shape
        # and is checked against `RUN_SHAPE` instead.
        for frame in json.loads(self.rendered_frames()):
            payload = frame["payload"]
            if payload["view"] != VIEW_CATALOG_PAGE:
                continue
            for entry in payload["entries"]:
                leaked = set(entry) & set(catalog.PRIVATE_FIELDS)
                self.assertEqual(
                    set(), leaked, f"a catalog entry carried {sorted(leaked)}"
                )

    def test_no_snapshot_row_carries_a_name_the_run_shape_does_not_declare(
        self,
    ) -> None:
        declared = RUN_SHAPE.required | RUN_SHAPE.optional
        for frame in json.loads(self.rendered_frames()):
            payload = frame["payload"]
            if payload["view"] != VIEW_SNAPSHOT:
                continue
            for row in payload["runs"]:
                self.assertEqual(
                    set(), set(row) - declared, f"a run row carried {sorted(row)}"
                )

    def test_the_frames_carry_no_absolute_path_at_all(self) -> None:
        # Stronger than naming this test's own directories: nothing that looks
        # like a filesystem path may appear anywhere in a frame.
        rendered = self.rendered_frames()
        self.assertIsNone(
            re.search(r'"/[^"]*/', rendered),
            "a frame carried something shaped like an absolute path",
        )


class PageAndCapTests(CorpusTestCase):
    """Bounds are the transport's, and a bound that bites says so."""

    def test_a_catalog_larger_than_one_page_is_delivered_as_several(self) -> None:
        source = self.make_source(page_limit=1)
        cycle = source.observe(NOW)
        self.assertEqual(2, len(cycle.catalog_pages))
        self.assertFalse(cycle.catalog_pages[0].final)
        self.assertTrue(cycle.catalog_pages[-1].final)

        db = self.open_store()
        channel = FakeChannel()
        connector = make_connector(source, channel)
        drain(connector, steps=16)
        for frame in channel.frames:
            ingest(db, frame, at=NOW)
        self.assertEqual(2, len(db.catalog(now=NOW)))

    def test_the_page_and_row_limits_are_the_transports_own(self) -> None:
        self.assertEqual(
            MAX_CATALOG_ENTRIES_PER_PAGE, portable_support.CATALOG_PAGE_LIMIT
        )
        self.assertEqual(MAX_RUNS_PER_SNAPSHOT, portable_support.MAX_PUBLISHED_RUNS)

    def test_an_unanswered_question_survives_a_full_snapshot_of_live_runs(
        self,
    ) -> None:
        live_count = portable_support.MAX_PUBLISHED_RUNS
        summaries = [mock.Mock(primary_state="running") for _ in range(live_count)]
        summaries.append(mock.Mock(primary_state="paused-for-reply"))
        records = [
            mock.Mock(
                entry=catalog.record_from_summary(
                    summary_payload(f"/tmp/agent-plans/plan-{index}")
                ).entry
            )
            for index in range(live_count + 1)
        ]
        rows = [
            {
                "run_id": f"run-{index}",
                "primary_state": "running",
                "phase": "running",
                "urgency_group": "in-progress",
                "last_activity_at": NOW - index,
                "urgency_rank": 60,
                "actions": [],
            }
            for index in range(live_count)
        ]
        rows.append(
            {
                "run_id": "phone-question",
                "primary_state": "paused-for-reply",
                "phase": "awaiting-reply",
                "urgency_group": "needs-action",
                "last_activity_at": NOW - 600.0,
                "urgency_rank": 10,
                "actions": [intervention.ACTION_REPLY],
                "question": {
                    "question_id": "question-1",
                    "asked_at": iso(-600.0),
                    "unanswered_count": 1,
                },
            }
        )
        scan = mock.Mock(
            summaries=summaries,
            catalog=mock.Mock(records=records),
        )

        with mock.patch.object(bridge_projection, "project_row", side_effect=rows):
            kept, failed, truncated = self.make_source()._runs(  # noqa: SLF001
                scan
            )

        self.assertEqual(0, failed)
        self.assertTrue(truncated)
        self.assertEqual(live_count, len(kept))
        self.assertIn("question-1", {row.get("question_id") for row in kept})

    def test_a_malformed_plan_is_catalogued_rather_than_lost(self) -> None:
        # A plan nobody can run is classified, not dropped: an operator looking
        # for the plan that stopped working has to be able to see it. So the
        # generation stays complete and the entry rides it like any other.
        broken = self.plans_root / "broken-plan"
        broken.mkdir()
        (broken / "context.md").write_text("# Context\n", encoding="utf-8")
        (broken / "manifest.md").write_text("# Broken plan\n", encoding="utf-8")
        cycle = self.make_source().observe(NOW)
        self.assertTrue(all(page.complete for page in cycle.catalog_pages))
        self.assertEqual(3, cycle.catalog_pages[-1].total)
        states = {
            entry.primary_state
            for page in cycle.catalog_pages
            for entry in page.entries
        }
        self.assertIn("invalid", states)
        self.assertIsNone(cycle.degraded_reason)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
