"""Multi-repository read-only session, guard and resume binding tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    plans_metadata,
    plans_registry,
    plans_snapshot,
    state_status,
)
from tests.support import (
    REPO_ROOT,
)
from tests.support_asl_cli_multi_repo import MultiRepoRunnerHelpers


class MultiRepoReadOnlyAndGuardTests(MultiRepoRunnerHelpers, unittest.TestCase):
    """Multi-repository read-only session, guard and resume binding tests."""

    def test_multi_repo_read_only_session_has_empty_write_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: (none)",
                ),
                encoding="utf-8",
            )
            target = repositories["SVC"] / "README.md"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0]["workspace_write_guard"]
            self.assertEqual([], guard["write_aliases"])
            self.assertEqual(
                "read-only-repository",
                guard["violations"][0]["kind"],
            )

    def test_multi_repo_read_only_session_passes_without_repository_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: (none)",
                ),
                encoding="utf-8",
            )
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(tmp_dir),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0]["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual([], guard["write_aliases"])
            self.assertEqual({}, guard["changed"])

    def test_multi_repo_real_turn_ignores_new_unregistered_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            added = workspace_root / "surprise-repository"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(added),
                    FAKE_CODEX_MUTATION_ACTION="git-init",
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual([], guard["added"])
            self.assertTrue((added / ".git").is_dir())

    def test_multi_repo_real_turn_rejects_removed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            removed = repositories["OAK"]
            retained_destination = tmp_dir / "retained-oak"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(removed),
                    FAKE_CODEX_MUTATION_ACTION="rename",
                    FAKE_CODEX_MUTATION_DESTINATION=str(retained_destination),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertEqual(["tundra_OAK"], guard["removed"])
            self.assertEqual("repository-removed", guard["violations"][0]["kind"])
            self.assertTrue((retained_destination / ".git").is_dir())

    def test_multi_repo_scope_guard_precedes_timeout_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--timeout",
                # The deadline runs from the harness spawn, so it has to cover
                # the fake harness reaching its mutation: a freshly copied
                # executable can stall for seconds in Gatekeeper assessment on
                # top of full-suite parallel load, and a deadline that lands
                # first leaves nothing for the guard to see, which reports the
                # bare timeout instead of the scope violation.  The sleep stays
                # far beyond the deadline so this still exercises the timeout.
                "30",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MODE="timeout",
                    FAKE_CODEX_SLEEP="120",
                    FAKE_CODEX_MUTATION_PATH=str(repositories["OAK"] / "README.md"),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                payload["pause_reason"],
            )
            self.assertTrue(payload["turns"][0]["timed_out"])

    def test_multi_repo_guard_compares_against_dirty_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            (repositories["OAK"] / "README.md").write_text(
                "pre-existing dirty state\n", encoding="utf-8"
            )
            target = repositories["SVC"] / "authorized.txt"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0]["workspace_write_guard"]
            self.assertEqual(["tundra_svc"], list(guard["changed"]))

    def test_multi_repo_guard_ignores_git_ignored_build_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            ignore_file = repositories["OAK"] / ".gitignore"
            ignore_file.write_text("cache/\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "ignore cache"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            target = repositories["OAK"] / "cache" / "build.bin"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0]["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual({}, guard["changed"])
            self.assertTrue(target.is_file())

    def test_multi_repo_guard_can_include_git_ignored_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            ignore_file = repositories["OAK"] / ".gitignore"
            ignore_file.write_text("cache/\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "ignore cache"],
                cwd=repositories["OAK"],
                capture_output=True,
                check=True,
            )
            target = repositories["OAK"] / "cache" / "build.bin"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--review-guard-include-ignored",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            guard = json.loads(completed.stdout)["turns"][0]["workspace_write_guard"]
            self.assertTrue(guard["include_ignored"])
            self.assertEqual(
                ["OAK:cache/build.bin"],
                guard["violations"][0]["qualified_paths"],
            )

    def test_multi_repo_run_rejects_binding_mismatch_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            metadata_path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
            before = metadata_path.read_bytes()
            moved_root = tmp_dir / "moved-workspace"
            shutil.copytree(workspace_root, moved_root)
            completed = self._run(plan_dir, moved_root, tmp_dir / "state", "--dry-run")

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("workspace-error", payload["reason"])
            self.assertEqual(before, metadata_path.read_bytes())

    def test_status_projects_persisted_workspace_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
            )
            metadata_path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
            legacy = json.loads(metadata_path.read_text(encoding="utf-8"))
            legacy["workspace_inventory_excludes"] = ["Library/Protected.dv"]
            metadata_path.write_text(json.dumps(legacy), encoding="utf-8")
            before_run = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir),
                state_dir,
            )
            self.assertEqual(
                str(workspace_root.resolve()), before_run["workspace_root"]
            )
            self.assertEqual(before_run["workspace_root"], before_run["cwd"])
            self.assertNotIn("workspace_inventory_excludes", before_run)
            run_completed = self._run(plan_dir, workspace_root, state_dir, "--dry-run")
            self.assertEqual(0, run_completed.returncode, run_completed.stderr)

            status_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            payload = json.loads(status_completed.stdout)
            self.assertEqual(str(workspace_root.resolve()), payload["workspace_root"])
            self.assertEqual(
                payload["workspace_root"],
                payload["runner_state"]["workspace_root"],
            )
            self.assertNotIn("workspace_inventory_excludes", payload)
            self.assertEqual(
                ["SVC", "OAK"],
                [repository["alias"] for repository in payload["repositories"]],
            )

    def test_resume_rejects_workspace_changed_since_persisted_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_dir = tmp_dir / "state"
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            first = self._run(plan_dir, workspace_root, state_dir, "--dry-run")
            self.assertEqual(0, first.returncode, first.stderr)

            moved_root = tmp_dir / "moved-workspace"
            shutil.copytree(workspace_root, moved_root)
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir, moved_root, registry.digest
            )
            resumed = self._run(
                plan_dir,
                moved_root,
                state_dir,
                "--resume",
                "--dry-run",
            )

            self.assertEqual(1, resumed.returncode)
            payload = json.loads(resumed.stdout)
            self.assertEqual("workspace-error", payload["reason"])
            self.assertIn("persisted run", payload["error"])


if __name__ == "__main__":
    unittest.main()
