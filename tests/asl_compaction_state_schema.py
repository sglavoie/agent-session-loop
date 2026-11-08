"""Schema cases for StateCompactionTests."""

from __future__ import annotations

import json
from pathlib import Path

from agent_session_loop import (
    agent_mux_projection,
    artifacts,
    compact_state,
    run_persistence,
)

from .asl_compaction_state_support import StateCompactionTestsBase


class StateCompactionTests(StateCompactionTestsBase):
    def test_dry_run_performs_no_writes(self) -> None:
        state_path = self._write_state({"run_id": "r", "turns": [self._v1_turn()]})
        before = state_path.read_bytes()
        result_path = self._write_result(
            [{"name": "c1", "status": "fail", "stdout": self.big, "stderr": "e"}]
        )
        result_before = result_path.read_bytes()

        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=False
        )

        self.assertFalse(report.applied)
        self.assertGreater(report.files_rewritten, 0)
        self.assertGreater(report.projected_bytes_reclaimed, 0)
        self.assertEqual(0, report.actual_bytes_reclaimed)
        # No file on disk changed, and no gzip artifact was written.
        self.assertEqual(before, state_path.read_bytes())
        self.assertEqual(result_before, result_path.read_bytes())
        self.assertFalse((result_path.parent / "verification").exists())

    def test_apply_compacts_state_to_schema_v2(self) -> None:
        state_path = self._write_state({"run_id": "r", "turns": [self._v1_turn()]})

        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        # Compaction performs the v2 output bounding and nothing more. It does
        # not attach an AgentMux projection, so it must not claim the v3 that
        # promises one; ``asl migrate-state`` is what upgrades this record.
        self.assertEqual(
            run_persistence.COMPACT_STATE_SCHEMA_VERSION, payload["version"]
        )
        self.assertNotIn(agent_mux_projection.PROJECTION_KEY, payload)
        turn = payload["turns"][0]
        self.assertNotIn("sessions", turn["before"])
        self.assertNotIn("sessions", turn["after"])
        check = turn["verification"]["checks"][0]
        self.assertNotIn("stdout", check)
        self.assertNotIn("stderr", check)

    def test_apply_keeps_schema_v3_when_a_projection_is_present(self) -> None:
        # A record a state transition already projected keeps its version and
        # its projection: compaction bounds output, it does not demote schemas.
        projection = {
            "schema_version": agent_mux_projection.PROJECTION_SCHEMA_VERSION,
            "run_id": "r",
        }
        state_path = self._write_state(
            {
                "run_id": "r",
                "turns": [self._v1_turn()],
                agent_mux_projection.PROJECTION_KEY: projection,
            }
        )

        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(agent_mux_projection.STATE_SCHEMA_VERSION, payload["version"])
        self.assertEqual(projection, payload[agent_mux_projection.PROJECTION_KEY])

    def test_compaction_preserves_unknown_top_level_run_config(self) -> None:
        # ``run_config`` is an additive top-level key the runner persists for
        # ``asl run --resume``; the compact projection must copy it through
        # unchanged so a resume after compaction still rehydrates.
        run_config = {
            "harness": "claude",
            "harness_model": "claude-opus-4",
            "effort": "high",
            "unsafe_bypass": True,
            "state_dir_override": "/tmp/state",
        }
        state_path = self._write_state(
            {"run_id": "r", "run_config": run_config, "turns": [self._v1_turn()]}
        )

        # Unit-level: the in-memory projection keeps the key verbatim.
        compact = run_persistence._compact_state_payload(
            json.loads(state_path.read_text(encoding="utf-8"))
        )
        self.assertEqual(run_config, compact["run_config"])

        # On-disk: a real compaction round-trip preserves it too.
        compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(run_config, payload["run_config"])

    def test_apply_extracts_failed_output_and_strips_pass_output(self) -> None:
        result_path = self._write_result(
            [
                {"name": "c1", "status": "fail", "stdout": self.big, "stderr": "boom"},
                {"name": "c2", "status": "pass", "stdout": self.big, "stderr": ""},
            ]
        )

        report = compact_state.compact_plan_state(
            self.plan_dir, state_dir=self.state_dir, apply=True
        )

        self.assertTrue(report.applied)
        self.assertEqual(
            report.projected_bytes_reclaimed, report.actual_bytes_reclaimed
        )
        # One passing stdout stream removed (lossy); failed stdout+stderr kept.
        self.assertEqual(1, report.pass_output_removed)
        self.assertEqual(2, report.failed_output_extracted)

        payload = json.loads(result_path.read_text(encoding="utf-8"))
        failed_check = payload["verification"]["checks"][0]
        # Full failed output is byte-for-byte recoverable from the gzip artifact.
        gzip_path = Path(failed_check["stdout_artifact_path"])
        self.assertTrue(gzip_path.exists())
        self.assertEqual(self.big, artifacts.read_gzip(gzip_path).decode("utf-8"))
        self.assertLess(len(failed_check["stdout"]), len(self.big))
        self.assertEqual(len(self.big), failed_check["stdout_byte_count"])
        # Passing output is discarded with only a preview kept — no artifact.
        pass_check = payload["verification"]["checks"][1]
        self.assertIsNone(pass_check["stdout_artifact_path"])
        self.assertLess(len(pass_check["stdout"]), len(self.big))
