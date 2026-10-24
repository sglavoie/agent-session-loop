"""Workbench details search regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    workbench_details,
)
from asl_workbench import catalog, details
from tests.support import (
    _make_workbench_plan,
    _write_state_payload,
)

from .support_workbench_details import _reclaimable_lock, _tree_fingerprint

ASL_TEST_LANE = "integration"


class DetailSearchTests(unittest.TestCase):
    """Search reads the private half, online, and retains nothing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()

    def _detail(self, **fields: object) -> catalog.PlanDetail:
        return catalog.PlanDetail(plan_key="pk", fields=dict(fields))

    def test_pause_prose_is_searchable(self) -> None:
        detail = self._detail(pause_message="Awaiting confirmation on the v1 column")
        self.assertTrue(details.matches_search(detail, "v1 column"))
        self.assertFalse(details.matches_search(detail, "v2 column"))

    def test_the_operator_label_is_searchable_and_stays_private(self) -> None:
        detail = self._detail(label="Alex's migration spike")
        self.assertTrue(details.matches_search(detail, "migration spike"))
        self.assertEqual("Alex's migration spike", details.private_label(detail))
        # The label is private precisely so it does not travel on a cached row.
        self.assertIn("label", catalog.PRIVATE_FIELDS)
        self.assertNotIn("label", catalog.CACHE_SAFE_FIELDS)

    def test_an_empty_query_is_not_a_filter(self) -> None:
        detail = self._detail(plan_dir="/plans/alpha")
        self.assertTrue(details.matches_search(detail, ""))
        self.assertTrue(details.matches_search(detail, "   "))
        self.assertFalse(details.matches_search(detail, None))

    def test_the_summary_haystack_is_preferred_when_present(self) -> None:
        detail = self._detail(searchable_text="alpha beta", pause_message="gamma delta")
        self.assertEqual("alpha beta", details.detail_search_text(detail))
        self.assertFalse(details.matches_search(detail, "gamma"))

    def test_a_hit_carries_only_the_key_label_and_haystack(self) -> None:
        detail = self._detail(
            plan_dir="/plans/alpha", label="Spike", pause_message="waiting"
        )
        self.assertEqual(
            {"plan_key", "label", "search_text"}, set(details.search_payload(detail))
        )

    def test_searching_reads_the_corpus_and_changes_nothing(self) -> None:
        plan_dir = _make_workbench_plan(self.plans_root)
        _write_state_payload(
            plan_dir,
            self.state_dir,
            {
                "plan_dir": str(plan_dir),
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "pause_reason": "agent-question",
                "pause_message": "Awaiting confirmation on the v1 column",
                "run_id": "run-1",
                "harness": "claude",
            },
        )
        lock_path = _reclaimable_lock(plan_dir, self.state_dir)
        before = lock_path.read_bytes()
        fingerprint = _tree_fingerprint(self.state_dir)

        # The plan's path -- private, and never on a cache-safe row -- is the
        # thing an operator actually types, and it is what makes this search a
        # question only the Mac can answer.
        hits = workbench_details.search_plans(
            plan_dir.name,
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )

        self.assertEqual(1, len(hits))
        self.assertIn(str(plan_dir).lower(), hits[0]["search_text"])
        self.assertEqual(catalog.plan_key(str(plan_dir)), hits[0]["plan_key"])
        self.assertTrue(lock_path.is_file())
        self.assertEqual(before, lock_path.read_bytes())
        self.assertEqual(fingerprint, _tree_fingerprint(self.state_dir))

    def test_a_query_that_matches_nothing_returns_nothing(self) -> None:
        _make_workbench_plan(self.plans_root)
        hits = workbench_details.search_plans(
            "no plan says this",
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()
