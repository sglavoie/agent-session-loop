"""Run readiness auto-repair and baseline evidence tests."""

from __future__ import annotations

import unittest

from agent_session_loop import baseline, events, plans, state
from tests.support import (
    read_jsonl,
)
from tests.support_asl_run_readiness import RunReadinessGateFixture


class RunReadinessAutoRepairTests(RunReadinessGateFixture):
    """Auto-repair of progress and readiness findings, and baseline evidence gating."""

    def test_run_auto_repairs_missing_progress_journal_and_proceeds(self) -> None:
        self._write_plan(("complete", "pending"), readiness="loop-ready")
        progress_file = plans.progress_path(self.plan_dir)
        self.assertFalse(progress_file.exists())

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

        # The readiness SHA matches, so only the unhashed-state check keeps the
        # loop-ready start from skipping the core preflight past this finding.
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["progress-journal-missing"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("rewrite", repair["action"])
        self.assertEqual("progress-journal-missing", repair["category"])
        self.assertEqual(str(progress_file), repair["path"])
        self.assertEqual(1, repair["entries_written"])
        self.assertEqual(["1"], repair["completed_session_ids"])
        self.assertFalse(repair["journal_replaced"])

        self.assertTrue(payload["readiness_gate"]["ok"])
        self.assertTrue(progress_file.exists())
        entries = plans.read_progress_entries(self.plan_dir)
        self.assertEqual(["1", "2"], [entry.session_id.value for entry in entries])

        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        repair_events = [
            record
            for record in read_jsonl(event_path)
            if record["name"] == events.PREFLIGHT_AUTO_REPAIRED_EVENT
        ]
        self.assertEqual(1, len(repair_events))
        self.assertEqual(
            "progress-journal-missing", repair_events[0]["payload"]["category"]
        )

    def test_run_auto_repairs_progress_and_readiness_together(self) -> None:
        self._write_plan(("complete", "pending"), readiness=None)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(
            ["progress-journal-missing", "manifest-readiness"],
            repair_payload["attempted"],
        )
        self.assertEqual([], repair_payload["errors"])
        self.assertTrue(repair_payload["ok"])
        categories = [repair["category"] for repair in repair_payload["auto_repairs"]]
        self.assertEqual(["progress-journal-missing", "manifest-readiness"], categories)
        self.assertTrue(plans.progress_path(self.plan_dir).exists())
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)

    def test_allow_not_loop_ready_skips_auto_repair(self) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        # The bypass path never runs auto-repair, so the field is absent.
        self.assertNotIn("readiness_gate_repair", payload)
        # Readiness metadata was not synthesised on behalf of the bypass.
        self.assertIsNone(plans.read_readiness_metadata(self.plan_dir))

    def test_run_pauses_when_non_repairable_finding_remains(self) -> None:
        self._write_plan(("complete", "pending"), readiness="loop-ready")
        # Inject a yellow ambiguity sentinel so progress-journal-missing is not
        # the only non-readiness blocker. Refresh readiness SHA so the
        # manifest-readiness drift does not also fire.
        session_path = self.plan_dir / "session-2.md"
        text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            text.replace("Instructions.", "Instructions. TODO: revisit later"),
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        progress_file = plans.progress_path(self.plan_dir)
        self.assertFalse(progress_file.exists())

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])

        repair_payload = payload["readiness_gate_repair"]
        # Progress repair was attempted (it is always attempted when the
        # category is present), but manifest-readiness was not (other blocker
        # remained after the progress repair).
        self.assertEqual(["progress-journal-missing"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        self.assertFalse(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))

        # The journal really was rebuilt even though the run paused.
        self.assertTrue(progress_file.exists())
        # The final gate payload still surfaces the unresolved sentinel.
        gate_findings = payload["readiness_gate"]["findings"]
        categories = {finding["category"] for finding in gate_findings}
        self.assertIn("ambiguity-sentinel", categories)
        self.assertNotIn("progress-journal-missing", categories)

    def test_run_blocks_when_baseline_evidence_missing(self) -> None:
        self._write_baseline_gated_plan()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        categories = {
            finding["category"] for finding in payload["readiness_gate"]["findings"]
        }
        self.assertIn("baseline-missing", categories)
        # Baseline evidence is outside the readiness SHA, so the loop-ready
        # start must not skip the core preflight past this finding.
        self.assertEqual([], self._preflight_skipped_events())
        self.assertNotIn("core_skipped", payload["readiness_gate"])

    def test_run_proceeds_when_baseline_evidence_present(self) -> None:
        self._write_baseline_gated_plan()
        # Record current baseline evidence before the run so the gate is
        # satisfied (the check passes and matches its expected-pass declaration).
        baseline.run_baseline(self.plan_dir, self.repo_root)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        categories = {
            finding["category"] for finding in payload["readiness_gate"]["findings"]
        }
        self.assertNotIn("baseline-missing", categories)


if __name__ == "__main__":
    unittest.main()
