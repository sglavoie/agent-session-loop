"""Test workbench catalog generations."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import bridge_protocol, workbench_catalog
from asl_workbench import catalog
from tests.support import _write_state_payload, write_plan

from .workbench_catalog_support import _minimal_summary_payload, _paused_state_payload


class LargeCatalogTests(unittest.TestCase):
    """A catalog past the bridge's cap keeps every kind of work."""

    #: Comfortably past `bridge_protocol.MAX_ROWS` so the cap, not the fixture size, is
    #: what the assertions are about.
    READY_PLANS = 120
    COMPLETE_PLANS = 120
    PAUSED_PLANS = 5

    def test_a_catalog_over_the_bridge_cap_keeps_ready_and_complete_work(self) -> None:
        total = self.READY_PLANS + self.COMPLETE_PLANS + self.PAUSED_PLANS
        self.assertGreater(
            total,
            bridge_protocol.MAX_ROWS,
            "this test is only meaningful above the projection cap",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            roots_dir = tmp_path / "roots"
            roots_dir.mkdir()
            for index in range(self.READY_PLANS):
                write_plan(roots_dir / f"ready-{index:04d}", ("pending",))
            for index in range(self.COMPLETE_PLANS):
                write_plan(roots_dir / f"done-{index:04d}", ("complete",))
            for index in range(self.PAUSED_PLANS):
                paused = roots_dir / f"paused-{index:04d}"
                write_plan(paused, ("pending",))
                _write_state_payload(
                    paused.resolve(),
                    state_root,
                    _paused_state_payload(
                        paused.resolve(),
                        run_id=f"20260619T14000{index}Z-1",
                    ),
                )

            catalogue = workbench_catalog.scan_catalog(
                state_dir=state_root,
                plan_roots=(str(roots_dir),),
                include_default_dirs=False,
            )

            self.assertEqual(total, catalogue.total)
            self.assertTrue(catalogue.complete)
            counts = catalogue.state_counts()
            self.assertEqual(self.READY_PLANS, counts["ready"])
            self.assertEqual(self.COMPLETE_PLANS, counts["complete"])
            self.assertEqual(self.PAUSED_PLANS, counts["paused-for-reply"])

            # The contrast the catalog exists to fix: the bridge's projection
            # is both capped and filtered to active/actionable states, so it
            # could not have answered any of the above.
            self.assertEqual(200, bridge_protocol.MAX_ROWS)
            self.assertLess(bridge_protocol.MAX_ROWS, total)
            self.assertNotIn("complete", bridge_protocol.ACTIVE_PRIMARY_STATES)
            self.assertNotIn("ready", bridge_protocol.ACTIVE_PRIMARY_STATES)

    def test_pages_cover_the_whole_catalog_without_overlap(self) -> None:
        records = tuple(
            catalog.record_from_summary(
                _minimal_summary_payload(f"/tmp/agent-plans/plan-{index:04d}")
            )
            for index in range(450)
        )
        catalogue = catalog.build_catalog(records)

        pages = catalogue.pages(limit=100)

        self.assertEqual(5, len(pages))
        seen: list[str] = []
        for index, page in enumerate(pages):
            self.assertEqual(catalogue.generation, page.generation)
            self.assertEqual(450, page.total)
            self.assertEqual(index == len(pages) - 1, page.final)
            seen.extend(entry.plan_key for entry in page.entries)
        self.assertEqual([record.plan_key for record in records], seen)
        self.assertEqual(len(set(seen)), len(seen))

    def test_an_offset_past_the_end_is_an_empty_final_page(self) -> None:
        catalogue = catalog.build_catalog(
            (catalog.record_from_summary(_minimal_summary_payload("/tmp/plan")),)
        )
        page = catalogue.page(offset=50, limit=10)
        self.assertEqual((), page.entries)
        self.assertTrue(page.final)
        self.assertEqual(1, page.total)

    def test_a_page_refuses_a_negative_offset_or_an_empty_limit(self) -> None:
        catalogue = catalog.build_catalog(())
        with self.assertRaises(catalog.CatalogError):
            catalogue.page(offset=-1)
        with self.assertRaises(catalog.CatalogError):
            catalogue.page(limit=0)
        with self.assertRaises(catalog.CatalogError):
            catalogue.pages(limit=0)


class IncompleteGenerationTests(unittest.TestCase):
    """An incomplete generation says so, on the catalog and on every page."""

    def test_a_projection_failure_clears_complete_on_every_page(self) -> None:
        catalogue = catalog.build_catalog(
            (catalog.record_from_summary(_minimal_summary_payload("/tmp/plan")),),
            warnings=("catalog: could not project /tmp/other",),
            complete=False,
        )

        self.assertFalse(catalogue.complete)
        for page in catalogue.pages(limit=1):
            # `final` is the page's property and `complete` is the
            # generation's: a last page of a partial scan is final and not
            # complete, and a consumer must not reconcile a disappearance
            # against it.
            self.assertTrue(page.final)
            self.assertFalse(page.complete)
            self.assertFalse(page.to_payload()["complete"])

    def test_a_plan_that_vanishes_mid_scan_is_a_warning_not_a_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            roots_dir = tmp_path / "roots"
            roots_dir.mkdir()
            write_plan(roots_dir / "plan-alpha", ("pending",))
            write_plan(roots_dir / "plan-beta", ("pending",))

            original = workbench_catalog.build_summary
            calls: list[int] = []

            def failing_summary(discovered: Any, **kwargs: Any) -> Any:
                calls.append(1)
                if len(calls) == 2:
                    raise OSError("plan directory vanished mid-scan")
                return original(discovered, **kwargs)

            workbench_catalog.build_summary = failing_summary
            self.addCleanup(setattr, workbench_catalog, "build_summary", original)

            scan = workbench_catalog.scan(
                state_dir=tmp_path / "state",
                plan_roots=(str(roots_dir),),
                include_default_dirs=False,
            )

            self.assertEqual(1, scan.catalog.total)
            self.assertFalse(scan.catalog.complete)
            self.assertFalse(scan.complete)
            self.assertEqual(1, len(scan.projection_warnings))
            self.assertIn("vanished mid-scan", scan.projection_warnings[0])
            # The surviving plan is still served; the flag is what stops its
            # partner's absence from being read as a deletion.
            self.assertEqual(1, len(scan.summaries))


class ChangeRegisterTests(unittest.TestCase):
    """The intentional-change record tracks what actually landed."""

    def test_the_observational_reads_record_names_no_remaining_path(self) -> None:
        from asl_workbench import changes

        record = changes.intentional_change(changes.KEY_OBSERVATIONAL_READS)
        # This asserted the opposite until session 8: the record had to keep
        # naming the one automatic retention path while it existed. It does not
        # exist, so the record now has to say that instead -- a record that
        # still advertised a deleting read would send the next session looking
        # for code to remove.
        self.assertIn(
            "workbench.maintain_archive no longer exists", record.current_behavior
        )
        self.assertIn("GET /api/archive", record.current_behavior)
        self.assertIn("reclaim=False", record.current_behavior)


if __name__ == "__main__":
    unittest.main()
