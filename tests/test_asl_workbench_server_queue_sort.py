"""Workbench server tests: queue catalog fields and sort modes.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import dataclasses
import json

from agent_session_loop import events, plans_files, state, workbench, workbench_server
from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _discovered,
    _http_request,
    write_plan,
)


class WorkbenchServerQueueSortTests(WorkbenchServerTestCase):
    """Queue catalog fields and sort modes."""

    def test_queue_exposes_archivable_and_catalog_fields(self) -> None:
        server = self._start_server(plan_roots=(str(self.tmp_path / "plans"),))
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")
        self.assertEqual(200, status)
        self.assertIn("archive_retention_days", body)
        # `archive_maintenance` is gone on purpose: the queue answers from one
        # non-reclaiming catalog scan and runs no retention, so it has no
        # maintenance result to report. No read has one any more -- the pass
        # itself no longer exists. See `changes.KEY_OBSERVATIONAL_READS`.
        self.assertNotIn("archive_maintenance", body)
        self.assertIn("catalog_generation", body)
        self.assertTrue(body["catalog_complete"])
        self.assertEqual(len(body["plans"]), body["catalog_total"])
        self.assertEqual([], body["catalog_warnings"])
        plan_payload = body["plans"][0]
        self.assertIn("archivable", plan_payload)
        self.assertIn("archive_blocker", plan_payload)
        # A ready plan with no active lock is archivable.
        self.assertTrue(plan_payload["archivable"])
        self.assertIsNone(plan_payload["archive_blocker"])

    def test_queue_oldest_sort_orders_missing_activity_first(self) -> None:
        # Second plan carries a finished runner state so it has activity; the
        # first plan has none and must sort oldest-first.
        second_plan = self.tmp_path / "plans" / "beta"
        write_plan(second_plan, ("pending",))
        canonical_second = plans_files.canonicalize_path(second_plan)
        second_state = state.plan_state_dir(canonical_second, self.state_dir)
        second_state.mkdir(parents=True, exist_ok=True)
        (second_state / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "plan_dir": str(canonical_second),
                    "run_finished_at": "2026-07-03T12:00:00+00:00",
                    "active_turn": {"last_activity_at": "2026-07-03T12:00:00+00:00"},
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server(plan_roots=(str(self.tmp_path / "plans"),))
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/queue?sort=oldest"
        )
        self.assertEqual(200, status)
        ordered = [p["plan_dir"] for p in body["plans"]]
        self.assertEqual(str(self.canonical_plan_dir), ordered[0])

    def test_oldest_sort_uses_completed_at_for_complete_plans(self) -> None:
        # Build a real complete summary, then force completed_at and
        # last_activity_at to diverge so the comparator must be reading
        # completed_at (not last_activity_at) for complete plans. If the
        # old last_activity_at path were still in force the ordering would
        # invert and this assertion would fail.
        done_dir = self.tmp_path / "plans" / "done"
        write_plan(done_dir, ("complete",))
        canonical = plans_files.canonicalize_path(done_dir)
        base = workbench.build_workbench_summary(
            _discovered(canonical), state_dir=self.state_dir
        )
        self.assertEqual("complete", base.primary_state)

        older = dataclasses.replace(
            base,
            completed_at="2026-01-01T00:00:00Z",
            last_activity_at="2026-12-31T00:00:00Z",
        )
        newer = dataclasses.replace(
            base,
            completed_at="2026-06-01T00:00:00Z",
            last_activity_at="2026-01-01T00:00:00Z",
        )

        ordered = workbench_server._sort_summaries_oldest_first([newer, older])

        self.assertEqual(
            ["2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z"],
            [summary.completed_at for summary in ordered],
        )

    def test_oldest_sort_missing_completed_at_sorts_complete_plan_first(
        self,
    ) -> None:
        # A complete plan with no completion timestamp must sort as the
        # oldest, ahead of an active plan that does have activity.
        done_dir = self.tmp_path / "plans" / "done-bare"
        write_plan(done_dir, ("complete",))
        complete = workbench.build_workbench_summary(
            _discovered(plans_files.canonicalize_path(done_dir)),
            state_dir=self.state_dir,
        )
        self.assertEqual("complete", complete.primary_state)
        self.assertIsNone(complete.completed_at)

        active = dataclasses.replace(
            workbench.build_workbench_summary(
                _discovered(self.canonical_plan_dir), state_dir=self.state_dir
            ),
            last_activity_at="2026-06-01T00:00:00Z",
        )

        ordered = workbench_server._sort_summaries_oldest_first([active, complete])

        self.assertEqual("complete", ordered[0].primary_state)

    def test_queue_rejects_unknown_sort_mode(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/queue?sort=bogus"
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
