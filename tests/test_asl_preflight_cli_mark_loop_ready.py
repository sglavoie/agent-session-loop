"""`asl preflight --mark-loop-ready` tests."""

from __future__ import annotations

import json

from agent_session_loop import (
    plans,
    preflight_environment,
)
from tests.support import (
    _contract_block,
    _write_loop_ready_plan,
    write_plan,
)
from tests.support_asl_preflight import PreflightCliFixtures


class PreflightCliMarkLoopReadyTests(PreflightCliFixtures):
    def test_mark_loop_ready_accepts_max_pending_sessions_override(self) -> None:
        pending_count = preflight_environment.TURN_BUDGET_YELLOW_THRESHOLD + 5
        _write_loop_ready_plan(
            self.plan_dir,
            tuple(["pending"] * pending_count),
            state="draft",
        )

        blocked = self._run_preflight(mark_loop_ready=True)
        blocked_payload = json.loads(blocked.stdout)

        self.assertEqual(1, blocked.returncode)
        self.assertFalse(blocked_payload["marked_loop_ready"])
        self.assertIn("env-turn-budget", blocked_payload["mark_error"])

        completed = self._run_preflight(
            mark_loop_ready=True,
            max_pending_sessions=pending_count,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual(pending_count, payload["max_pending_sessions"])
        budget_findings = [
            finding
            for finding in payload["findings"]
            if finding["category"] == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0]["severity"])

    def test_preflight_mark_loop_ready_writes_manifest(self) -> None:
        # The fixture is already loop-ready; --mark-loop-ready refreshes the
        # checked_at timestamp once preflight succeeds.
        original_sha = plans.stable_readiness_sha256(self.plan_dir)
        original = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(original)
        self.assertEqual("loop-ready", original.state)
        self.assertEqual(original_sha, original.readiness_sha256)
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertTrue(payload["marked_loop_ready"])
        refreshed = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(refreshed)
        self.assertEqual("loop-ready", refreshed.state)
        self.assertEqual(original_sha, refreshed.readiness_sha256)
        self.assertNotEqual(original.checked_at, refreshed.checked_at)

    def test_preflight_mark_loop_ready_promotes_draft_or_reviewed(self) -> None:
        for readiness_state in ("draft", "reviewed"):
            with self.subTest(readiness_state=readiness_state):
                _write_loop_ready_plan(
                    self.plan_dir,
                    ("pending",),
                    contracts_by_id={
                        "1": _contract_block(
                            '{"type": "file_exists", "path": "README.md", '
                            '"name": "readme-1"}',
                            session="1",
                        ),
                    },
                    state=readiness_state,
                )

                completed = self._run_preflight(mark_loop_ready=True)

                self.assertEqual(0, completed.returncode, completed.stderr)
                payload = json.loads(completed.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual("loop-ready-marked", payload["reason"])
                self.assertTrue(payload["marked_loop_ready"])
                self.assertEqual("loop-ready", payload["readiness"]["state"])
                self.assertEqual(0, payload["findings_by_severity"]["red"])
                self.assertEqual(0, payload["findings_by_severity"]["yellow"])
                metadata = plans.read_readiness_metadata(self.plan_dir)
                self.assertIsNotNone(metadata)
                self.assertEqual("loop-ready", metadata.state)

    def test_preflight_mark_loop_ready_repairs_missing_readiness(self) -> None:
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_preflight_mark_loop_ready_repairs_stale_readiness_sha(self) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        self.assertEqual(
            payload["expected_readiness_sha256"],
            payload["readiness"]["readiness_sha256"],
        )

    def test_preflight_mark_loop_ready_refused_when_non_readiness_not_ok(
        self,
    ) -> None:
        session_path = self.plan_dir / "session-1.md"
        text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            text.replace("Instructions.", "Instructions. TODO: revisit later"),
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="draft",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        self.assertIn("non-readiness", payload["mark_error"])
        readiness = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_preflight_command_not_runnable_blocks_mark_loop_ready(self) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "command", "argv": '
                    '["definitely-not-a-real-binary-xyz", "--version"], '
                    '"timeout_seconds": 30, "name": "run-1"}',
                    session="1",
                ),
            },
            state="draft",
        )

        completed = self._run_preflight(cwd=self.repo_root, mark_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-red", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        self.assertIn("non-readiness", payload["mark_error"])
        command_findings = [
            finding
            for finding in payload["findings"]
            if finding["category"] == "command-not-runnable"
        ]
        self.assertEqual(1, len(command_findings))
        self.assertEqual("red", command_findings[0]["severity"])
        readiness = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_preflight_mark_loop_ready_auto_backfills_missing_progress_journal(
        self,
    ) -> None:
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
        progress_file = plans.progress_path(self.plan_dir)
        self.assertFalse(progress_file.exists())

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertIn("auto_repairs", payload)
        self.assertEqual(1, len(payload["auto_repairs"]))
        repair = payload["auto_repairs"][0]
        self.assertEqual("rewrite", repair["action"])
        self.assertEqual("progress-journal-missing", repair["category"])
        self.assertEqual(str(progress_file), repair["path"])
        self.assertEqual(1, repair["entries_written"])
        self.assertEqual(["1"], repair["completed_session_ids"])
        self.assertFalse(repair["journal_replaced"])
        # The progress.md was actually rebuilt and the rerun no longer surfaces
        # progress-journal-missing.
        self.assertTrue(progress_file.exists())
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertNotIn("progress-journal-missing", categories)
        entries = plans.read_progress_entries(self.plan_dir)
        self.assertEqual(("1",), tuple(entry.session_id.value for entry in entries))

    def test_preflight_mark_loop_ready_refuses_when_progress_plus_other_blocker(
        self,
    ) -> None:
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
        # Inject a yellow ambiguity sentinel so progress-journal-missing is not
        # the only non-readiness blocker. Readiness SHA excludes handoffs and
        # sentinel-only edits do not affect the stable hash because they live
        # inside the instructions body, so refresh readiness explicitly.
        session_path = self.plan_dir / "session-1.md"
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

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        self.assertIn("non-readiness", payload["mark_error"])
        self.assertEqual([], payload["auto_repairs"])
        # No repair attempted because the blockers were not progress-only.
        self.assertFalse(progress_file.exists())
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("progress-journal-missing", categories)
        self.assertIn("ambiguity-sentinel", categories)
