"""`asl preflight` JSON and text report tests."""

from __future__ import annotations

import json

from agent_session_loop import plans_content_hash, plans_models, plans_readiness
from tests.support import (
    _contract_block,
    _write_loop_ready_plan,
)
from tests.support_asl_preflight import PreflightCliFixtures


class PreflightCliReportTests(PreflightCliFixtures):
    def test_preflight_json_ok_when_ready_plan(self) -> None:
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("preflight", payload["command"])
        self.assertEqual("ok", payload["reason"])
        self.assertEqual(str(self.plan_dir), payload["plan_dir"])
        self.assertEqual(str(self.repo_root), payload["cwd"])
        self.assertEqual("codex", payload["harness"])
        self.assertEqual("per-session", payload["commit_mode"])
        self.assertFalse(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        # An ok plan needs no repairs and suggests no pause.
        self.assertEqual([], payload["auto_repair_plan"])
        self.assertIsNone(payload["suggested_pause"])
        # No red/yellow findings present.
        counts = payload["findings_by_severity"]
        self.assertEqual(0, counts["red"])
        self.assertEqual(0, counts["yellow"])
        # All green findings still surface (env-amx and env-turn-budget at minimum).
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("env-amx", categories)
        self.assertIn("env-turn-budget", categories)
        # Readiness and expected sha-256 surface as serializable values.
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        self.assertEqual(
            payload["readiness"]["readiness_sha256"],
            payload["expected_readiness_sha256"],
        )

    def test_preflight_json_red_when_readiness_breaks(self) -> None:
        # Break readiness sha to produce a red manifest-readiness finding.
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-red", payload["reason"])
        self.assertFalse(payload["paused"])
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("manifest-readiness", categories)
        # A readiness-only break is auto-repairable via --mark-loop-ready and
        # the emitted repair_hint embeds the plan-dir-substituted command.
        commands = [
            step["command"] for step in payload["auto_repair_plan"] if "command" in step
        ]
        self.assertIn(
            f"asl preflight {self.plan_dir} --json --mark-loop-ready",
            commands,
        )
        self.assertIsNone(payload["suggested_pause"])
        readiness_hints = [
            hint
            for hint in payload["repair_hints"]
            if hint["category"] == "manifest-readiness"
        ]
        self.assertTrue(readiness_hints)
        self.assertIn(
            f"asl preflight {self.plan_dir} --json --mark-loop-ready",
            readiness_hints[0]["message"],
        )
        # findings keep the dataclass shape exactly.
        sample = payload["findings"][0]
        for key in (
            "severity",
            "category",
            "message",
            "session_id",
            "path",
            "line",
            "detail",
        ):
            self.assertIn(key, sample)

    def test_preflight_json_yellow_when_ambiguity_only(self) -> None:
        # Inject a yellow ambiguity sentinel into session-1.md instructions.
        session_path = self.plan_dir / "session-1.md"
        text = session_path.read_text(encoding="utf-8")
        text = text.replace("Instructions.", "Instructions. TODO: revisit later")
        session_path.write_text(text, encoding="utf-8")
        # Recompute readiness so manifest-readiness stays green.
        expected = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=expected,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        categories_by_severity = {
            (finding["severity"], finding["category"])
            for finding in payload["findings"]
        }
        self.assertIn(("yellow", "ambiguity-sentinel"), categories_by_severity)

    def test_preflight_json_auto_repairs_empty_when_no_repair_runs(self) -> None:
        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("auto_repairs", payload)
        self.assertEqual([], payload["auto_repairs"])

    def test_preflight_text_output_mentions_auto_repair(self) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        completed = self._run_preflight(
            json_output=False,
            mark_loop_ready=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Auto-repair: rebuilt progress journal", completed.stdout)
        self.assertIn("1 entry written", completed.stdout)
        self.assertIn("Preflight: ok", completed.stdout)
        self.assertIn("Loop-ready: marked", completed.stdout)
        # Stable reason strings remain unaffected.
        self.assertNotIn("loop-ready-refused", completed.stdout)
        self.assertNotIn("loop-ready-write-failed", completed.stdout)

    def test_preflight_text_output_lists_findings(self) -> None:
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            json_output=False,
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(f"Plan: {self.plan_dir}", completed.stdout)
        self.assertIn("Sessions: 1", completed.stdout)
        self.assertIn("Harness: codex", completed.stdout)
        self.assertIn("Commit mode: per-session", completed.stdout)
        self.assertIn("Preflight: ok", completed.stdout)
        self.assertIn("[GREEN] env-amx", completed.stdout)
        self.assertIn("[GREEN] env-turn-budget", completed.stdout)

    def test_preflight_text_output_failure_routes_summary_to_stderr(self) -> None:
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight(json_output=False)
        self.assertEqual(1, completed.returncode)
        self.assertIn("[RED] manifest-readiness", completed.stdout)
        self.assertIn("Preflight: failed", completed.stderr)

    def test_preflight_json_includes_repair_hints_aligned_with_findings(
        self,
    ) -> None:
        # Break readiness to force a red manifest-readiness finding so we have
        # at least one finding to align a repair hint with.
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertIn("repair_hints", payload)
        self.assertIsInstance(payload["repair_hints"], list)
        self.assertEqual(len(payload["findings"]), len(payload["repair_hints"]))
        # Each hint exposes the documented stable keys.
        for hint in payload["repair_hints"]:
            for key in (
                "category",
                "check_id",
                "severity_hint",
                "message",
                "question_template",
                "fix_category",
            ):
                self.assertIn(key, hint)
        # The manifest-readiness finding resolves to the sha-mismatch override.
        manifest_hints = [
            hint
            for hint, finding in zip(
                payload["repair_hints"], payload["findings"], strict=False
            )
            if finding["category"] == "manifest-readiness"
        ]
        self.assertTrue(manifest_hints)
        self.assertEqual(
            "manifest-readiness.sha-mismatch", manifest_hints[0]["check_id"]
        )
