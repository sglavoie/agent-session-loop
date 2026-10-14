"""Threshold, protection and over-budget tests for storage maintenance.

Split from tests/test_asl_maintenance.py."""

from __future__ import annotations

import os
import unittest
import unittest.mock

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    maintenance_models,
    maintenance_policy,
)
from tests.asl_maintenance_support import (
    ALWAYS_LOW,
    HUGE_BUDGET,
    NEVER_LOW,
    NOW,
    RUN_ANCIENT,
    RUN_NEWEST,
    RUN_OLD,
    MaintenanceTestBase,
    _stamp,
)


class ThresholdTests(MaintenanceTestBase):
    """Nothing happens until the corpus is over budget or the volume is low."""

    def test_under_both_thresholds_nothing_is_planned_or_touched(self) -> None:
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"x" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, force=False)

        self.assertEqual(result.outcome, maintenance_policy.OUTCOME_UNDER_THRESHOLD)
        self.assertEqual(result.trigger, maintenance_policy.TRIGGER_NONE)
        self.assertIsNone(result.plan)
        self.assertFalse(result.applied)
        self.assertTrue(result.ok)
        self.assertTrue(run_dir.is_dir())

    def test_corpus_over_budget_is_the_reported_trigger(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"x" * 8192}
        )

        usage = maintenance_models.measure_corpus(
            self.state_root, now=NOW, budget_bytes=4096, min_free_fraction=NEVER_LOW
        )

        self.assertTrue(usage.triggered)
        self.assertTrue(usage.over_budget)
        self.assertFalse(usage.low_free_space)
        self.assertEqual(usage.trigger, maintenance_policy.TRIGGER_CORPUS_OVER_BUDGET)

    def test_low_free_space_triggers_even_when_the_corpus_is_small(self) -> None:
        usage = maintenance_models.measure_corpus(
            self.state_root,
            now=NOW,
            budget_bytes=HUGE_BUDGET,
            min_free_fraction=ALWAYS_LOW,
        )

        self.assertTrue(usage.triggered)
        self.assertFalse(usage.over_budget)
        self.assertTrue(usage.low_free_space)
        self.assertEqual(usage.trigger, maintenance_policy.TRIGGER_LOW_FREE_SPACE)

    def test_shipped_thresholds_are_the_governing_decision(self) -> None:
        self.assertEqual(maintenance_policy.CORPUS_BUDGET_BYTES, 8 * 1024**3)
        self.assertEqual(maintenance_policy.MIN_FREE_FRACTION, 0.15)
        self.assertEqual(maintenance_policy.COMPACTION_AGE_DAYS, 90.0)
        self.assertEqual(maintenance_policy.LOG_BODY_BUDGET_BYTES, 64 * 1024**2)

    def test_a_missing_state_root_measures_zero_rather_than_failing(self) -> None:
        usage = maintenance_models.measure_corpus(
            self.tmp_path / "absent", now=NOW, min_free_fraction=NEVER_LOW
        )

        self.assertEqual(usage.corpus_bytes, 0)
        self.assertFalse(usage.over_budget)


class ProtectedEvidenceTests(MaintenanceTestBase):
    """Every retention fence still holds when maintenance is the one deleting."""

    def test_locked_live_pinned_and_recent_runs_all_survive_an_applied_pass(
        self,
    ) -> None:
        locked_plan = self._make_plan("locked")
        locked_run = self._write_run(
            RUN_OLD, finished_at=_stamp(200), plan_dir=locked_plan
        )
        self._write_lock(plan_dir=locked_plan, pid=os.getpid())

        recent = self._write_run(RUN_OLD, finished_at=_stamp(2))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        unfinished_plan = self._make_plan("unfinished")
        unfinished = self._write_run(
            RUN_ANCIENT, finished_at=None, plan_dir=unfinished_plan
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1), plan_dir=unfinished_plan)

        result = self._run(apply=True)

        self.assertEqual(result.outcome, maintenance_policy.OUTCOME_APPLIED)
        self.assertEqual(result.removed_run_ids, ())
        self.assertEqual(result.reclaimed_bytes, 0)
        for survivor in (locked_run, recent, unfinished):
            self.assertTrue(survivor.is_dir(), f"{survivor} was removed")

    def test_an_open_assurance_issue_pins_its_run_against_maintenance(self) -> None:
        pinned = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"y" * 512}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._pin_run(RUN_OLD)

        result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(pinned.is_dir())

    def test_an_unreadable_assurance_ledger_stops_every_removal(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        # A missing ledger is a readable empty inbox; only one that exists and
        # cannot be parsed leaves the pin question unanswered.
        ledger.ledger_path(self.state_root).mkdir(parents=True)

        result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())

    def test_an_expired_unprotected_run_is_the_one_thing_removed(self) -> None:
        expired = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"z" * 2048}
        )
        newest = self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, (RUN_OLD,))
        self.assertGreaterEqual(result.reclaimed_bytes, 2048)
        self.assertFalse(expired.exists())
        self.assertTrue(newest.is_dir())
        self.assertTrue((self._state_dir() / "state.json").is_file())

    def test_dry_run_projects_the_same_removal_without_performing_it(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=False)

        self.assertEqual(result.outcome, maintenance_policy.OUTCOME_PLANNED)
        self.assertFalse(result.applied)
        assert result.plan is not None
        self.assertEqual(result.plan.reclaim.reclaimable_run_count, 1)
        self.assertTrue(expired.is_dir())


class ProtectedOverBudgetTests(MaintenanceTestBase):
    """When only protected data is left, nothing goes and a human is told."""

    def test_protected_data_over_budget_raises_a_strict_finding(self) -> None:
        locked = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"L" * 16384}
        )
        self._write_lock(pid=os.getpid())

        result = self._run(apply=True, budget_bytes=1024)

        self.assertFalse(result.ok)
        findings = {finding.code: finding for finding in result.health}
        self.assertIn(maintenance_policy.HEALTH_PROTECTED_OVER_BUDGET, findings)
        self.assertTrue(
            findings[maintenance_policy.HEALTH_PROTECTED_OVER_BUDGET].strict
        )
        self.assertEqual(result.removed_run_ids, ())
        self.assertEqual(result.removed_body_count, 0)
        self.assertTrue((locked / "turn-0001-session-1" / "stderr.log").is_file())

    def test_a_corpus_policy_can_bring_under_budget_raises_nothing(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"R" * 16384}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, budget_bytes=8192)

        self.assertEqual(result.removed_run_ids, (RUN_OLD,))
        self.assertTrue(result.ok)
        self.assertEqual(result.health, ())

    def test_a_dry_run_projects_the_over_budget_verdict_too(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"P" * 16384}
        )
        self._write_lock(pid=os.getpid())

        result = self._run(apply=False, budget_bytes=1024)

        self.assertFalse(result.ok)
        self.assertIn(
            maintenance_policy.HEALTH_PROTECTED_OVER_BUDGET,
            [finding.code for finding in result.health],
        )


if __name__ == "__main__":
    unittest.main()
