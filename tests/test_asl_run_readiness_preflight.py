"""Run readiness gate: auto-repair and core-preflight skipping."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    plans_content_hash,
    plans_files,
    plans_metadata,
    plans_readiness,
)
from tests.support_asl_run_readiness import RunReadinessGateFixture


class RunReadinessPreflightTests(RunReadinessGateFixture):
    """Runs repair readiness metadata and skip or rerun core preflight by state."""

    def test_run_auto_repairs_when_readiness_metadata_missing(self) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual([], repair_payload["errors"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("rewrite", repair["action"])
        self.assertEqual("manifest-readiness", repair["category"])
        self.assertEqual(str((self.plan_dir / "manifest.md").resolve()), repair["path"])
        self.assertEqual(0, repair["entries_written"])
        self.assertEqual([], repair["completed_session_ids"])
        self.assertFalse(repair["journal_replaced"])
        # Readiness lifecycle picks up from the repaired metadata.
        self.assertEqual("complete", payload["readiness_state_final"])
        metadata = plans_readiness.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)
        self.assertEqual(
            plans_content_hash.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )
        self.assertEqual(
            str(plans_files.canonicalize_path(self.repo_root)),
            plans_metadata.load_plan_metadata(self.plan_dir).cwd,
        )

    def test_run_auto_repairs_when_state_is_reviewed(self) -> None:
        self._write_plan(("pending",), readiness="reviewed")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("reviewed", payload["readiness_state_initial"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        # Metadata was rewritten to ``loop-ready`` and then lifecycled to
        # ``complete`` once the only pending session finished.
        self.assertEqual(
            "complete", plans_readiness.read_readiness_metadata(self.plan_dir).state
        )

    def test_run_auto_repairs_when_state_is_complete_on_pending_plan(self) -> None:
        self._write_plan(("pending",), readiness="complete")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("complete", payload["readiness_state_initial"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual([], repair_payload["errors"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("manifest-readiness", repair["category"])
        self.assertEqual("loop-ready", repair["target_state"])
        self.assertEqual(
            "complete", plans_readiness.read_readiness_metadata(self.plan_dir).state
        )

    def test_run_auto_repairs_when_state_is_running_on_pending_plan(self) -> None:
        self._write_plan(("pending",), readiness="running")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("running", payload["readiness_state_initial"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual([], repair_payload["errors"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("manifest-readiness", repair["category"])
        self.assertEqual("loop-ready", repair["target_state"])
        self.assertEqual(
            "complete", plans_readiness.read_readiness_metadata(self.plan_dir).state
        )

    def test_run_auto_repairs_when_recorded_sha_does_not_match(self) -> None:
        self._write_plan(
            ("pending",),
            readiness="loop-ready",
            sha_override="0" * 64,
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        metadata = plans_readiness.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)
        self.assertNotEqual("0" * 64, metadata.readiness_sha256)
        self.assertEqual(
            plans_content_hash.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_loop_ready_start_skips_core_preflight(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        expected_sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["readiness_gate"]["core_skipped"])
        self.assertNotIn("readiness_gate_repair", payload)
        skipped = self._preflight_skipped_events()
        self.assertEqual(1, len(skipped))
        self.assertEqual(
            {"readiness_state": "loop-ready", "readiness_sha256": expected_sha},
            skipped[0]["payload"],
        )
        # Command runnability probes only run on the full preflight path.
        self.assertEqual(
            [],
            [
                finding
                for finding in payload["readiness_gate"]["findings"]
                if finding["category"] == "command-not-runnable"
            ],
        )

    def test_running_readiness_start_skips_core_preflight(self) -> None:
        self._write_plan_with_unrunnable_probe(readiness="running")
        expected_sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("running", payload["readiness_state_initial"])
        skipped = self._preflight_skipped_events()
        self.assertEqual(1, len(skipped))
        self.assertEqual(
            {"readiness_state": "running", "readiness_sha256": expected_sha},
            skipped[0]["payload"],
        )
        # `running` still fails the strict gate, so the readiness metadata is
        # normalized -- but the repair rerun stays on the reduced path.
        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertTrue(repair_payload["ok"])
        self.assertTrue(repair_payload["core_skipped"])
        self.assertTrue(payload["readiness_gate"]["core_skipped"])
        self.assertEqual([], self._command_runnability_findings(payload))

    def test_running_readiness_with_stale_sha_probes_commands(self) -> None:
        self._write_plan_with_unrunnable_probe(
            readiness="running", sha_override="0" * 64
        )

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])
        self.assertEqual("not-loop-ready", payload["reason"])
        findings = self._command_runnability_findings(payload)
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0]["severity"])
        self.assertEqual("2", findings[0]["session_id"])

    def test_session_edited_after_loop_ready_runs_full_preflight(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.", "Instructions. Edited while paused."
            ),
            encoding="utf-8",
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])
        self.assertEqual(
            ["manifest-readiness"],
            payload["readiness_gate_repair"]["attempted"],
        )


if __name__ == "__main__":
    unittest.main()
