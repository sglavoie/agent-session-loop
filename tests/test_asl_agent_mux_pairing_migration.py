"""``asl agent-mux pairing`` refusal and ``--migrate`` behaviour."""

from __future__ import annotations

import json
import os
import unittest

from agent_session_loop import (
    agent_mux_pairing,
    agent_mux_projection,
    state,
)
from tests.asl_agent_mux_pairing_support import CURRENT, PairingTestBase


class PairingRefusalTests(PairingTestBase):
    """A live run whose state predates the projection refuses the whole pairing."""

    def test_live_legacy_run_is_active_incompatible(self) -> None:
        plan_dir = self._seed("live", version=2, locked=True)
        report = self._survey()
        self.assertFalse(report["ok"])
        self.assertEqual(
            report["reason"], agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS
        )
        self.assertEqual(len(report["active_incompatible_runs"]), 1)
        run = report["active_incompatible_runs"][0]
        self.assertEqual(run["plan_dir"], str(plan_dir))
        self.assertEqual(run["pid"], os.getpid())
        self.assertEqual(run["schema_version"], 2)

    def test_refusal_names_the_run_so_an_operator_can_act(self) -> None:
        self._seed("live", version=2, locked=True)
        run = self._survey()["active_incompatible_runs"][0]
        for field in ("plan_dir", "state_dir", "run_id", "pid", "detail"):
            self.assertIsNotNone(run[field], f"{field} must identify the run")

    def test_live_run_at_current_schema_does_not_refuse(self) -> None:
        """Liveness alone is not incompatibility."""
        self._seed("live", version=CURRENT, locked=True)
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(report["plans"]["compatible"], 1)
        self.assertEqual(report["active_incompatible_runs"], [])

    def test_live_run_at_an_uninterpretable_version_refuses(self) -> None:
        self._seed("live", version=99, locked=True)
        report = self._survey()
        self.assertFalse(report["ok"])
        self.assertEqual(report["plans"]["active-incompatible"], 1)

    def test_stale_lock_does_not_refuse(self) -> None:
        """A dead owner is not a run; the plan migrates normally."""
        plan_dir = self._seed("stale", version=2)
        lock_path = state.lock_path_for_plan(plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "pid": 999_999_999,
                    "plan_dir": str(plan_dir),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "hostname": os.uname().nodename,
                    "owner": "agent-session-loop",
                }
            ),
            encoding="utf-8",
        )
        report = self._survey()
        self.assertTrue(report["ok"])
        self.assertEqual(report["plans"]["migratable"], 1)

    def test_survey_never_reclaims_the_lock_it_reads(self) -> None:
        """A read-only survey must be a function of the corpus, not of its order."""
        plan_dir = self._seed("stale", version=2)
        lock_path = state.lock_path_for_plan(plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "pid": 999_999_999,
                    "plan_dir": str(plan_dir),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "hostname": os.uname().nodename,
                    "owner": "agent-session-loop",
                }
            ),
            encoding="utf-8",
        )
        before = lock_path.read_bytes()
        self._survey()
        self.assertTrue(lock_path.exists(), "the survey removed a lock it only read")
        self.assertEqual(lock_path.read_bytes(), before)


class PairingMigrationTests(PairingTestBase):
    """``--migrate`` upgrades the inactive remainder, or nothing at all."""

    def test_migrate_upgrades_an_inactive_legacy_plan(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        report = self._survey(migrate=True)
        self.assertTrue(report["ok"], report.get("error"))
        self.assertTrue(report["migrated"])
        self.assertEqual(len(report["migrations"]), 1)
        record = report["migrations"][0]
        self.assertTrue(record["ok"])
        self.assertTrue(record["applied"])
        self.assertEqual(record["schema_version_before"], 2)
        self.assertEqual(record["schema_version_after"], CURRENT)
        upgraded = self._state_object(plan_dir)
        self.assertEqual(upgraded["version"], CURRENT)
        self.assertIsInstance(upgraded.get(agent_mux_projection.PROJECTION_KEY), dict)

    def test_migrate_is_idempotent(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        self._survey(migrate=True)
        after_first = self._state_bytes(plan_dir)
        second = self._survey(migrate=True)
        self.assertTrue(second["ok"])
        self.assertEqual(second["migrations"], [], "a current plan is not migratable")
        self.assertEqual(self._state_bytes(plan_dir), after_first)

    def test_migrate_leaves_pause_evidence_intact(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        before = self._state_object(plan_dir)
        self._survey(migrate=True)
        after = self._state_object(plan_dir)
        for key, value in before.items():
            if key == "version":
                continue
            self.assertEqual(after[key], value, f"{key} was rewritten")

    def test_migrate_writes_nothing_while_a_live_legacy_run_exists(self) -> None:
        """The refusal is whole-root: the innocent bystander is not upgraded."""
        bystander = self._seed("inactive", version=2)
        self._seed("live", version=2, locked=True)
        before = self._state_bytes(bystander)
        report = self._survey(migrate=True)
        self.assertFalse(report["ok"])
        self.assertEqual(
            report["reason"], agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS
        )
        self.assertFalse(report["migrated"])
        self.assertEqual(report["migrations"], [])
        self.assertEqual(self._state_bytes(bystander), before)

    def test_live_legacy_run_is_never_migrated_underneath(self) -> None:
        plan_dir = self._seed("live", version=2, locked=True)
        before = self._state_bytes(plan_dir)
        self._survey(migrate=True)
        self.assertEqual(self._state_bytes(plan_dir), before)

    def test_survey_without_migrate_writes_nothing(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        before = self._state_bytes(plan_dir)
        report = self._survey()
        self.assertFalse(report["migrated"])
        self.assertEqual(self._state_bytes(plan_dir), before)

    def test_unprojectable_and_unreadable_plans_are_not_migrated(self) -> None:
        thin = self._seed("thin", identity=False)
        broken = self._seed("broken", raw="nope\n")
        before = (self._state_bytes(thin), self._state_bytes(broken))
        report = self._survey(migrate=True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["migrations"], [])
        self.assertEqual((self._state_bytes(thin), self._state_bytes(broken)), before)


if __name__ == "__main__":
    unittest.main()
