"""Preflight auto-repair plan tests."""

from __future__ import annotations

import json
import shlex
import unittest

from agent_session_loop import (
    preflight_auto_repair,
)
from tests.support_asl_preflight import _finding, _report


class AutoRepairPlanTests(unittest.TestCase):
    def test_ok_report_has_empty_plan_and_no_pause(self) -> None:
        report = _report(_finding("green", "env-amx", "optional"))
        self.assertEqual(
            (),
            preflight_auto_repair.build_auto_repair_plan(
                report, plan_dir="/plan", cwd=None
            ),
        )
        self.assertIsNone(
            preflight_auto_repair.build_suggested_pause(report, plan_dir="/plan")
        )

    def test_stale_lock_then_progress_then_readiness_order(self) -> None:
        report = _report(
            _finding(
                "red", "manifest-readiness", "manifest is missing readiness metadata"
            ),
            _finding(
                "yellow", "progress-journal-missing", "progress journal not initialized"
            ),
            _finding("yellow", "env-lock", "stale lock present at /p/lock"),
        )
        plan = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir="/plans/x", cwd=None
        )
        commands = [step["command"] for step in plan]
        self.assertEqual(
            [
                "asl unlock /plans/x --json",
                "asl backfill-progress --force /plans/x",
                "asl preflight /plans/x --json --mark-loop-ready",
            ],
            commands,
        )
        for step in plan:
            self.assertEqual(1, step["max_attempts"])
        # readiness-only residue is fully repairable -> no fallback pause.
        self.assertIsNone(
            preflight_auto_repair.build_suggested_pause(report, plan_dir="/plans/x")
        )

    def test_active_lock_blocks_and_never_unlocks(self) -> None:
        report = _report(
            _finding("red", "env-lock", "active lock held by pid 9 at /p/lock"),
            _finding("yellow", "progress-journal-missing", "journal missing"),
        )
        plan = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir="/plans/x", cwd=None
        )
        self.assertEqual(1, len(plan))
        step = plan[0]
        self.assertTrue(step["blocked"])
        self.assertEqual("lock-contention", step["pause_reason"])
        # The active-lock plan never emits an unlock command or `--force`.
        self.assertNotIn("command", step)
        for entry in plan:
            self.assertNotIn("--force", json.dumps(entry))
        pause = preflight_auto_repair.build_suggested_pause(report, plan_dir="/plans/x")
        self.assertEqual("lock-contention", pause["reason"])

    def test_hard_blocker_suppresses_readiness_step_and_suggests_unsafe(
        self,
    ) -> None:
        report = _report(
            _finding(
                "red", "manifest-readiness", "manifest is missing readiness metadata"
            ),
            _finding(
                "red",
                "missing-contract",
                "session 1 is missing a ## Completion Contract section",
            ),
        )
        plan = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir="/plans/x", cwd=None
        )
        # A non-readiness blocker (missing-contract) means mark-loop-ready would
        # be refused, so no readiness repair step is offered.
        self.assertEqual([], [s for s in plan if "command" in s])
        pause = preflight_auto_repair.build_suggested_pause(report, plan_dir="/plans/x")
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("missing-contract", pause["detail"])
        self.assertIn("no automatic repair clears", pause["detail"])

    def test_progress_only_repairable_without_pause(self) -> None:
        report = _report(
            _finding("yellow", "progress-journal-missing", "journal missing"),
        )
        plan = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir="/plans/x", cwd=None
        )
        self.assertEqual(
            ["asl backfill-progress --force /plans/x"],
            [s["command"] for s in plan],
        )
        self.assertIsNone(
            preflight_auto_repair.build_suggested_pause(report, plan_dir="/plans/x")
        )

    def test_repair_commands_survive_whitespace_and_metacharacters(self) -> None:
        # A plan path carrying whitespace and shell metacharacters must stay a
        # single argument in every auto-repair step: stale-lock, progress-
        # backfill, and readiness. Each step exposes an authoritative `argv`
        # array, and its `command` is that argv rendered shell-safe so it
        # round-trips through shlex.split.
        weird = "/plans/de mo; rm -rf $(evil) 'q'"
        report = _report(
            _finding(
                "red",
                "manifest-readiness",
                "manifest is missing readiness metadata",
            ),
            _finding(
                "yellow",
                "progress-journal-missing",
                "progress journal not initialized",
            ),
            _finding("yellow", "env-lock", "stale lock present at /p/lock"),
        )
        plan = preflight_auto_repair.build_auto_repair_plan(
            report, plan_dir=weird, cwd=None
        )
        self.assertEqual(
            [
                ["asl", "unlock", weird, "--json"],
                ["asl", "backfill-progress", "--force", weird],
                ["asl", "preflight", weird, "--json", "--mark-loop-ready"],
            ],
            [step["argv"] for step in plan],
        )
        for step in plan:
            self.assertEqual(step["argv"], shlex.split(step["command"]))
            self.assertIn(weird, shlex.split(step["command"]))
