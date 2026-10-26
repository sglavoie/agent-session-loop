"""Concurrency, deadline and manifest tests for storage maintenance.

Split from tests/test_asl_maintenance.py."""

from __future__ import annotations

import json
import threading
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from agent_session_loop import (
    maintenance_models,
    maintenance_plan,
    maintenance_policy,
    maintenance_run,
    retention,
)
from tests.asl_maintenance_support import (
    NEVER_LOW,
    NOW,
    RUN_ANCIENT,
    RUN_NEWEST,
    RUN_OLD,
    MaintenanceTestBase,
    _stamp,
)


class ConcurrencyTests(MaintenanceTestBase):
    """Two passes never interleave; the second declines rather than racing."""

    def test_a_held_claim_makes_the_second_pass_decline_without_mutating(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        claim = maintenance_run._maintenance_claim(
            maintenance_policy.lock_path(self.state_root), wait_seconds=0.0
        )
        with claim as held:
            self.assertTrue(held)
            result = self._run(apply=True, lock_wait_seconds=0.05)

        self.assertEqual(result.outcome, maintenance_policy.OUTCOME_LOCK_UNAVAILABLE)
        self.assertFalse(result.applied)
        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())

    def test_the_claim_is_released_when_the_holder_finishes(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        first = self._run(apply=True)
        second = self._run(apply=True)

        self.assertEqual(first.outcome, maintenance_policy.OUTCOME_APPLIED)
        self.assertEqual(second.outcome, maintenance_policy.OUTCOME_APPLIED)

    def test_two_concurrent_passes_produce_exactly_one_applier(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        gate = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()
        real_revalidate = maintenance_plan._revalidate

        def slow(*args: Any, **kwargs: Any) -> Any:
            # Widen the mutation window so the loser cannot simply arrive late.
            gate.wait(timeout=10)
            return real_revalidate(*args, **kwargs)

        def worker() -> None:
            result = self._run(apply=True, lock_wait_seconds=0.05, record=False)
            with guard:
                outcomes.append(result.outcome)
            if result.outcome != maintenance_policy.OUTCOME_APPLIED:
                gate.wait(timeout=10)

        with unittest.mock.patch.object(maintenance_run, "_revalidate", slow):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes.count(maintenance_policy.OUTCOME_APPLIED), 1)
        self.assertEqual(outcomes.count(maintenance_policy.OUTCOME_LOCK_UNAVAILABLE), 1)

    def test_waiting_pass_remeasures_pressure_after_acquiring_claim(self) -> None:
        """The pass that waited its turn deletes nothing if the wait fixed it.

        Pressure is what authorizes a pass to delete, and it is read before the
        claim is even requested. A pass that then blocks behind another pass is
        holding a fact that the other pass exists to falsify, so it reads it
        again once the claim is its own.
        """
        # Over budget when the waiting pass measures; under it once the pass
        # ahead reclaims the other plan's expired run.
        half = b"x" * (1 << 20)
        expired = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": half}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        other_plan = self._make_plan("other")
        relieving = self._write_run(
            RUN_ANCIENT,
            finished_at=_stamp(300),
            plan_dir=other_plan,
            bodies={"stderr.log": half},
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1), plan_dir=other_plan)
        # Both bodies together are over; either one alone is under.
        budget = 3 * (1 << 19)

        measured = threading.Event()
        real_measure = maintenance_models.measure_corpus

        def measure_then_signal(*args: Any, **kwargs: Any) -> Any:
            usage = real_measure(*args, **kwargs)
            # The pre-claim reading is done; the waiting pass is now parked on
            # the claim this thread holds, which is the exact race to reproduce.
            measured.set()
            return usage

        results: list[maintenance_models.MaintenanceResult] = []

        def worker() -> None:
            results.append(
                maintenance_run.run_maintenance(
                    self.state_root,
                    apply=True,
                    force=False,
                    now=NOW,
                    budget_bytes=budget,
                    min_free_fraction=NEVER_LOW,
                    lock_wait_seconds=30.0,
                    record=False,
                )
            )

        claim = maintenance_run._maintenance_claim(
            maintenance_policy.lock_path(self.state_root), wait_seconds=0.0
        )
        with claim as held:
            self.assertTrue(held)
            with unittest.mock.patch.object(
                maintenance_run, "measure_corpus", measure_then_signal
            ):
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(measured.wait(timeout=10))
                # Stand in for the pass ahead: relieve the pressure, then hand
                # the claim over.
                self.assertEqual(retention.remove_run_directory(relieving), "")
        thread.join(timeout=20)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.outcome, maintenance_policy.OUTCOME_PRESSURE_RELIEVED)
        self.assertFalse(result.applied)
        self.assertTrue(result.ok)
        self.assertEqual(result.removed_run_ids, ())
        self.assertIsNone(result.plan)
        # Both readings are kept: the one that admitted the pass, and the one
        # that sent it home.
        self.assertTrue(result.usage.over_budget)
        self.assertIsNotNone(result.usage_at_claim)
        assert result.usage_at_claim is not None
        self.assertFalse(result.usage_at_claim.triggered)
        self.assertEqual(result.trigger, maintenance_policy.TRIGGER_NONE)
        self.assertTrue(expired.is_dir())

        # And the run was genuinely reclaimable: only the remeasure spared it.
        forced = self._run(apply=True, budget_bytes=budget)
        self.assertEqual(forced.removed_run_ids, (RUN_OLD,))

    def test_a_relieved_pass_still_acts_when_force_was_requested(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, force=True)

        self.assertEqual(result.outcome, maintenance_policy.OUTCOME_APPLIED)
        self.assertEqual(result.removed_run_ids, (RUN_OLD,))
        self.assertFalse(expired.exists())


class DeadlineTests(MaintenanceTestBase):
    """A bounded pass stops cleanly and says what it left behind."""

    def test_an_expired_deadline_stops_work_and_reports_it(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, max_runtime_seconds=0.0)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())
        findings = {finding.code: finding for finding in result.health}
        self.assertIn(maintenance_policy.HEALTH_DEADLINE_EXCEEDED, findings)
        # Running out of time is a schedule fact, not a corpus fault.
        self.assertFalse(findings[maintenance_policy.HEALTH_DEADLINE_EXCEEDED].strict)
        self.assertTrue(result.ok)


class ManifestTests(MaintenanceTestBase):
    """The structured result survives the process that produced it."""

    def test_the_manifest_records_the_last_run_and_a_bounded_history(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        for _ in range(maintenance_policy.MANIFEST_HISTORY_LIMIT + 3):
            self._run(apply=True)

        payload = maintenance_run.read_manifest(self.state_root)

        self.assertEqual(
            payload["schema_version"], maintenance_policy.MANIFEST_SCHEMA_VERSION
        )
        self.assertEqual(
            payload["last_run"]["outcome"], maintenance_policy.OUTCOME_APPLIED
        )
        self.assertEqual(
            len(payload["history"]), maintenance_policy.MANIFEST_HISTORY_LIMIT
        )

    def test_the_manifest_carries_reclaimed_bytes_and_failures(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"m" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        self._run(apply=True)
        last_run = maintenance_run.read_manifest(self.state_root)["last_run"]

        self.assertGreaterEqual(last_run["reclaimed_bytes"], 4096)
        self.assertEqual(last_run["removed_run_count"], 1)
        self.assertEqual(last_run["failures"], [])
        self.assertTrue(last_run["ok"])

    def test_a_manifest_that_cannot_be_written_fails_the_pass(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        def refuse(path: Path, payload: Any, **kwargs: Any) -> None:
            if path == maintenance_policy.manifest_path(self.state_root):
                raise OSError("Read-only file system")

        with unittest.mock.patch.object(
            maintenance_run.durability, "write_json_file", refuse
        ):
            result = self._run(apply=True)

        self.assertFalse(result.ok)
        self.assertEqual(
            result.failures[-1].kind, maintenance_policy.FAILURE_MANIFEST_WRITE
        )

    def test_an_absent_manifest_reads_as_empty(self) -> None:
        self.assertEqual(maintenance_run.read_manifest(self.state_root), {})

    def test_the_result_serializes_to_json(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        payload = json.loads(json.dumps(self._run(apply=True).to_dict()))

        self.assertEqual(payload["command"], "maintenance-run")
        self.assertIn("usage", payload)
        self.assertIn("plan", payload)


if __name__ == "__main__":
    unittest.main()
