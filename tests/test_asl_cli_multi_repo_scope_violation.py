"""Multi-repository scope violation and scope expansion tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    plans,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_multi_repo_workspace,
)
from tests.support_asl_cli_multi_repo import MultiRepoRunnerHelpers


class MultiRepoScopeViolationTests(MultiRepoRunnerHelpers, unittest.TestCase):
    """Multi-repository scope violation and scope expansion tests."""

    def test_multi_repo_real_turn_retains_and_reports_read_only_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            target = repositories["OAK"] / "README.md"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                    FAKE_CODEX_MUTATION_TEXT="unauthorized but retained\n",
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["paused"])
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                payload["pause_reason"],
            )
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertFalse(guard["passed"])
            self.assertEqual("outside-read-scope", guard["violations"][0]["kind"])
            self.assertEqual(
                ["OAK:README.md"],
                guard["violations"][0]["qualified_paths"],
            )
            self.assertTrue(guard["reverted_to_pending"])
            self.assertNotIn("verification", payload["turns"][0])
            self.assertIn(
                "unauthorized but retained",
                target.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_multi_repo_scope_violation_requires_scope_expansion_and_preflight(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            first = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(repositories["OAK"] / "README.md"),
                ),
            )
            self.assertEqual(1, first.returncode, first.stderr)

            unrepaired = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(tmp_dir),
            )
            self.assertEqual(1, unrepaired.returncode, unrepaired.stderr)
            unrepaired_payload = json.loads(unrepaired.stdout)
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                unrepaired_payload["pause_reason"],
            )
            self.assertEqual([], unrepaired_payload["turns"])

            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            (plan_dir / "session-1.expand.md").write_text(
                "# Refreshed expansion\n", encoding="utf-8"
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-08-08T00:00:00Z",
                ),
            )
            repaired = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(tmp_dir),
            )

            self.assertEqual(0, repaired.returncode, repaired.stderr)
            repaired_payload = json.loads(repaired.stdout)
            self.assertTrue(
                repaired_payload["turns"][0]["workspace_write_guard"]["passed"]
            )
            records = [
                json.loads(line)
                for line in Path(repaired_payload["events_path"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(
                any(
                    record["name"] == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT
                    for record in records
                )
            )

    def test_multi_repo_scope_violation_repair_may_register_a_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            newly_registered = DEFAULT_MULTI_REPO_SPECS[2]
            init_multi_repo_workspace(workspace_root, (newly_registered,))
            first = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(repositories["OAK"] / "README.md"),
                ),
            )
            self.assertEqual(1, first.returncode, first.stderr)
            first_guard = json.loads(first.stdout)["turns"][0]["workspace_write_guard"]
            self.assertEqual(
                "outside-read-scope",
                first_guard["violations"][0]["kind"],
            )

            manifest_path = plan_dir / "manifest.md"
            alias, relative_path, origin = newly_registered
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + f"| {alias} | {relative_path} | {origin} |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            (plan_dir / "session-1.expand.md").write_text(
                "# Refreshed expansion after registering a repository\n",
                encoding="utf-8",
            )
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            plans.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-08-08T00:00:00Z",
                ),
            )

            repaired = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides=self._fake_codex_env(tmp_dir),
            )

            self.assertEqual(0, repaired.returncode, repaired.stderr)
            repaired_payload = json.loads(repaired.stdout)
            self.assertTrue(
                repaired_payload["turns"][0]["workspace_write_guard"]["passed"]
            )
            records = [
                json.loads(line)
                for line in Path(repaired_payload["events_path"])
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            repair_records = [
                record
                for record in records
                if record["name"] == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT
            ]
            self.assertEqual(1, len(repair_records))
            repair_guard = repair_records[0]["payload"]["details"][
                "workspace_write_guard"
            ]
            self.assertEqual(
                [relative_path],
                repair_guard["repair_registry_delta"],
            )
            self.assertEqual([], repair_guard["added"])


if __name__ == "__main__":
    unittest.main()
