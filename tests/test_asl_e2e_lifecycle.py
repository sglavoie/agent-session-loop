"""End-to-end lifecycle, preflight, and JSON contract tests.

Split from tests/test_asl_e2e.py."""

from __future__ import annotations

import json
import sys

from agent_session_loop import (
    plans_content_hash,
    plans_files,
    plans_models,
    plans_readiness,
    state,
)
from tests.asl_e2e_support import (
    EndToEndQABase,
    run_to_completion,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    _write_loop_ready_plan,
    write_plan,
)

ASL_TEST_LANE = "integration"


class EndToEndQATests(EndToEndQABase):
    """Legacy repair lifecycle, preflight signals, and JSON contract stability."""

    def _write_legacy_plan(self) -> None:
        """A pre-loop-readiness plan: no manifest readiness, no contract."""
        write_plan(self.plan_dir, ("pending",))

    def _repair_legacy_plan_to_draft(self) -> None:
        """Repair legacy content while leaving readiness for preflight to mark."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="draft",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_full_lifecycle_repairs_legacy_plan_and_runs_to_completion(
        self,
    ) -> None:
        """Legacy plan → preflight red → repair → mark → run → verify pass."""
        self._write_legacy_plan()
        path_value = self._path_value("codex", "spec", "amx")

        # Step 1: preflight surfaces red findings on the legacy plan.
        before, before_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, before.returncode)
        self.assertFalse(before_payload["ok"])
        before_categories = {f["category"] for f in before_payload["findings"]}
        self.assertIn("manifest-readiness", before_categories)
        self.assertIn("missing-contract", before_categories)
        # Repair hints stay aligned with findings 1:1 for the guided skill.
        self.assertEqual(
            len(before_payload["findings"]),
            len(before_payload["repair_hints"]),
        )

        # Step 2: repair (add contract, write draft readiness).
        self._repair_legacy_plan_to_draft()
        readiness_before_mark = plans_readiness.read_readiness_metadata(self.plan_dir)
        self.assertEqual("draft", readiness_before_mark.state)

        # Step 3: --mark-loop-ready promotes a clean draft plan.
        marked, marked_payload = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(0, marked.returncode, marked.stderr)
        self.assertTrue(marked_payload["ok"])
        self.assertEqual("loop-ready-marked", marked_payload["reason"])
        self.assertTrue(marked_payload["marked_loop_ready"])
        readiness_after_mark = plans_readiness.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness_after_mark)
        self.assertEqual("loop-ready", readiness_after_mark.state)
        self.assertEqual(
            plans_content_hash.stable_readiness_sha256(self.plan_dir),
            readiness_after_mark.readiness_sha256,
        )
        self.assertNotEqual(readiness_before_mark.state, readiness_after_mark.state)

        # Step 4: asl run completes the plan with verification passing.
        run_completed, run_payload = self._run_run()
        self.assertEqual(0, run_completed.returncode, run_completed.stderr)
        self.assertTrue(run_payload["ok"])
        self.assertFalse(run_payload["paused"])
        self.assertEqual("all-complete", run_payload["reason"])
        self.assertEqual("complete", run_payload["readiness_state_final"])
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", session_text)
        verify_payload = run_payload["turns"][0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])

    def test_verification_failure_pauses_then_resumes_after_repair(self) -> None:
        """`verify-failed` reverts status; re-running after repair completes."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "later.txt", "name": "later"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        # First run: verify fails, status reverts to pending.
        first_completed, first_payload = self._run_run()
        self.assertEqual(1, first_completed.returncode)
        self.assertTrue(first_payload["paused"])
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        pause_details = first_payload["pause_details"]
        self.assertEqual(["later"], pause_details["recipe"]["names"])
        self.assertEqual("file-missing", pause_details["failures"][0]["failure_reason"])
        session_path = self.plan_dir / "session-1.md"
        session_text = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        self.assertIn("verification failed", session_text)

        # A verify-failed pause flips readiness to `reviewed`; after the
        # underlying issue is repaired, preflight can re-mark the plan.
        reviewed_metadata = plans_readiness.read_readiness_metadata(self.plan_dir)
        self.assertEqual("reviewed", reviewed_metadata.state)

        # Repair: create the missing file so the contract will now pass.
        (self.repo_root / "later.txt").write_text("done\n", encoding="utf-8")

        remark_completed, remark_payload = self._run_preflight(mark_loop_ready=True)
        self.assertEqual(0, remark_completed.returncode, remark_completed.stderr)
        self.assertTrue(remark_payload["marked_loop_ready"])
        self.assertEqual("loop-ready", remark_payload["readiness"]["state"])

        # Second run: the still-pending session is picked up and completes.
        second_completed, second_payload = self._run_run()
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertFalse(second_payload["paused"])
        self.assertEqual("all-complete", second_payload["reason"])
        final_session = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", final_session)
        # Readiness lifecycle resolves to complete after the resumed run.
        final_metadata = plans_readiness.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", final_metadata.state)

    def test_stale_lock_surfaces_in_preflight_cli_with_repair_hint(
        self,
    ) -> None:
        """A stale lock JSON-fingerprints to env-lock.stale via the CLI."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = state.lock_path_for_plan(self.plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 0,
                    "plan_dir": str(plans_files.canonicalize_path(self.plan_dir)),
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        lock_findings = [f for f in payload["findings"] if f["category"] == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("yellow", lock_findings[0]["severity"])
        self.assertIn("stale lock", lock_findings[0]["message"])
        # Each finding pairs with a stable repair hint check id.
        lock_hints = [
            hint
            for hint, finding in zip(
                payload["repair_hints"], payload["findings"], strict=False
            )
            if finding["category"] == "env-lock"
        ]
        self.assertEqual(1, len(lock_hints))
        self.assertEqual("env-lock.stale", lock_hints[0]["check_id"])

    def test_commit_mode_dirty_worktree_emits_yellow_via_preflight_cli(
        self,
    ) -> None:
        """`--commit-per-session` plus a dirty worktree surfaces env-git yellow."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        (self.repo_root / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        git_findings = [f for f in payload["findings"] if f["category"] == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("yellow", git_findings[0]["severity"])
        self.assertIn("dirty", git_findings[0]["message"])
        # Marking loop-ready must be refused while yellow findings remain.
        completed_mark, payload_mark = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(1, completed_mark.returncode)
        self.assertFalse(payload_mark["marked_loop_ready"])

    def test_json_contracts_share_stable_keys_across_preflight_and_verify(
        self,
    ) -> None:
        """The JSON shape published in the docs must hold at the CLI surface."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        path_value = self._path_value("codex", "spec", "amx")

        # Preflight JSON: documented top-level keys.
        preflight_completed, preflight_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(0, preflight_completed.returncode)
        for key in (
            "ok",
            "command",
            "reason",
            "findings",
            "findings_by_severity",
            "repair_hints",
            "readiness",
            "expected_readiness_sha256",
            "mark_loop_ready_requested",
            "marked_loop_ready",
        ):
            self.assertIn(key, preflight_payload)
        self.assertEqual("preflight", preflight_payload["command"])

        # Verify JSON: documented top-level keys including stable reason enum.
        verify_argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "verify",
            str(self.plan_dir),
            "--session",
            "1",
            "--cwd",
            str(self.repo_root),
            "--json",
        ]
        verify_completed = run_to_completion(
            verify_argv,
            cwd=REPO_ROOT,
            env=self._base_env(),
            budget_seconds=20,
            label="verify",
        )
        self.assertEqual(0, verify_completed.returncode, verify_completed.stderr)
        verify_payload = json.loads(verify_completed.stdout)
        for key in (
            "ok",
            "command",
            "reason",
            "passed",
            "recipe",
            "checks",
            "session",
            "cwd",
            "plan_dir",
        ):
            self.assertIn(key, verify_payload)
        self.assertEqual("verify", verify_payload["command"])
        self.assertEqual("ok", verify_payload["reason"])
