"""Preflight environment check tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    harness_compatibility,
    plans_files,
    plans_models,
    plans_readiness,
    preflight,
    preflight_environment,
    preflight_models,
    state,
    state_locks,
)
from tests.support import (
    _write_loop_ready_plan,
    init_git_repo,
    patched_env,
)


class PreflightEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.xdg_config = self.tmp_path / "xdg-config"
        self.xdg_state = self.tmp_path / "xdg-state"
        self.path_dir = self.tmp_path / "path"
        self.xdg_config.mkdir()
        self.xdg_state.mkdir()
        self.path_dir.mkdir()
        self._env_ctx = patched_env(
            XDG_CONFIG_HOME=str(self.xdg_config),
            XDG_STATE_HOME=str(self.xdg_state),
        )
        self._env_ctx.__enter__()
        self.plan_dir = self.tmp_path / "plan"
        _write_loop_ready_plan(self.plan_dir, ("pending",))

    def tearDown(self) -> None:
        self._env_ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def _drop_executable(self, directory: Path, name: str) -> Path:
        target = directory / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        return target

    def _path_with(self, *names: str, base: Path | None = None) -> str:
        base = base or self.path_dir
        for name in names:
            self._drop_executable(base, name)
        return str(base)

    def test_env_repo_root_missing_emits_red(self) -> None:
        missing = self.tmp_path / "absent"
        report = preflight.run_environment_preflight(self.plan_dir, cwd=missing)
        repo_findings = [f for f in report.findings if f.category == "env-repo-root"]
        self.assertEqual(1, len(repo_findings))
        self.assertEqual("red", repo_findings[0].severity)
        self.assertIn("does not exist", repo_findings[0].message)
        self.assertFalse(report.ok)

    def test_env_repo_root_not_directory_emits_red(self) -> None:
        file_cwd = self.tmp_path / "file.txt"
        file_cwd.write_text("nope", encoding="utf-8")
        report = preflight.run_environment_preflight(self.plan_dir, cwd=file_cwd)
        repo_findings = [f for f in report.findings if f.category == "env-repo-root"]
        self.assertEqual(1, len(repo_findings))
        self.assertEqual("red", repo_findings[0].severity)
        self.assertIn("not a directory", repo_findings[0].message)

    def test_env_git_outside_repo_emits_red(self) -> None:
        cwd = self.tmp_path / "non-git"
        cwd.mkdir()
        report = preflight.run_environment_preflight(self.plan_dir, cwd=cwd)
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("red", git_findings[0].severity)
        self.assertIn("not inside a git repository", git_findings[0].message)

    def test_env_git_inside_repo_is_silent_without_commit_mode(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        report = preflight.run_environment_preflight(self.plan_dir, cwd=repo_dir)
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual([], git_findings)

    def test_env_git_dirty_with_commit_mode_emits_yellow(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        (repo_dir / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        report = preflight.run_environment_preflight(
            self.plan_dir, cwd=repo_dir, commit_mode="per-session"
        )
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("yellow", git_findings[0].severity)
        self.assertIn("dirty", git_findings[0].message)
        self.assertIn("per-session", git_findings[0].detail)

    def test_env_git_clean_with_commit_mode_is_silent(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        report = preflight.run_environment_preflight(
            self.plan_dir, cwd=repo_dir, commit_mode="end"
        )
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual([], git_findings)

    def test_env_harness_unsupported_name_emits_red(self) -> None:
        report = preflight.run_environment_preflight(self.plan_dir, harness="bogus")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("unsupported harness", harness_findings[0].message)

    def test_env_harness_missing_executable_emits_red(self) -> None:
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("not found on PATH", harness_findings[0].message)

    def _drop_versioned_harness(self, directory: Path, name: str, output: str) -> Path:
        target = directory / name
        target.write_text(f"#!/bin/sh\nprintf '%s\\n' {output!r}\n", encoding="utf-8")
        target.chmod(0o755)
        return target

    def test_env_harness_supported_version_is_silent(self) -> None:
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)
        latest = harness_compatibility.POLICY["codex"].latest
        self._drop_versioned_harness(self.path_dir, "codex", f"codex-cli {latest}")
        with patched_env(PATH=str(self.path_dir)):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual([], harness_findings)

    def test_env_harness_unverified_version_warns_without_blocking(self) -> None:
        # An executable that reports nothing parseable is `unknown`. Preflight
        # must say so and still let the run start: yellow is a blocker here, so
        # the warning lane is green with the word "warning" in its message.
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)
        path_str = self._path_with("codex")
        with patched_env(PATH=path_str):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("green", harness_findings[0].severity)
        self.assertIn("unverified codex version", harness_findings[0].message)
        self.assertEqual((), report.red_findings)
        self.assertEqual((), report.yellow_findings)

    def test_env_harness_known_incompatible_version_emits_red(self) -> None:
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)
        self._drop_versioned_harness(self.path_dir, "codex", "codex-cli 0.1.0")
        with patched_env(PATH=str(self.path_dir)):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("known-incompatible", harness_findings[0].message)
        self.assertFalse(report.ok)

    def test_commit_mode_needs_no_external_commit_executable(self) -> None:
        # Auto-commit re-enters this checkout as `asl commit`, so an empty
        # PATH can no longer withhold anything a commit mode depends on and
        # preflight has nothing left to check for it.
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(
                self.plan_dir, commit_mode="per-session"
            )
        self.assertEqual([], [f for f in report.findings if f.category == "env-spec"])
        self.assertNotIn("env-spec", preflight_models.PREFLIGHT_CATEGORIES)
        self.assertNotIn(
            "spec", " ".join(finding.message for finding in report.findings)
        )

    def test_module_overview_omits_retired_spec_requirement(self) -> None:
        # The module overview is the last place the retired external commit
        # dependency could still be documented as a checked requirement.
        overview = preflight.__doc__ or ""
        self.assertNotEqual("", overview.strip())
        self.assertNotIn("env-spec", overview)
        self.assertNotIn("``spec``", overview)
        self.assertNotIn("spec availability", overview)
        self.assertIn("asl commit", overview)

    def test_env_lock_absent_emits_no_finding(self) -> None:
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual([], lock_findings)

    def test_env_lock_stale_emits_yellow(self) -> None:
        plan_state = state.plan_state_dir(self.plan_dir, self.xdg_state)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = state.lock_path_for_plan(self.plan_dir, self.xdg_state)
        canonical_plan = plans_files.canonicalize_path(self.plan_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 0,
                    "plan_dir": str(canonical_plan),
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("yellow", lock_findings[0].severity)
        self.assertIn("stale lock", lock_findings[0].message)

    def test_env_lock_active_emits_red(self) -> None:
        lock_info = state_locks.acquire_lock(self.plan_dir, self.xdg_state)
        try:
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        finally:
            state_locks.release_lock(self.plan_dir, self.xdg_state)
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("red", lock_findings[0].severity)
        self.assertIn(str(lock_info.pid), lock_findings[0].message)

    def test_env_amx_present_emits_green(self) -> None:
        path_str = self._path_with("amx")
        with patched_env(PATH=path_str):
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        amx_findings = [f for f in report.findings if f.category == "env-amx"]
        self.assertEqual(1, len(amx_findings))
        self.assertEqual("green", amx_findings[0].severity)
        self.assertIn("available", amx_findings[0].message)

    def test_env_amx_absent_emits_green(self) -> None:
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        amx_findings = [f for f in report.findings if f.category == "env-amx"]
        self.assertEqual(1, len(amx_findings))
        self.assertEqual("green", amx_findings[0].severity)
        self.assertIn("not on PATH", amx_findings[0].message)

    def test_env_turn_budget_green_with_pending_count(self) -> None:
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn("1 session(s) pending", budget_findings[0].message)

    def test_env_turn_budget_yellow_above_threshold(self) -> None:
        many_plan = self.tmp_path / "many-plan"
        _write_loop_ready_plan(
            many_plan,
            tuple(["pending"] * preflight_environment.TURN_BUDGET_YELLOW_THRESHOLD),
        )
        report = preflight.run_environment_preflight(
            many_plan, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("yellow", budget_findings[0].severity)
        self.assertIn("exceeds", budget_findings[0].message)

    def test_env_turn_budget_override_allows_large_pending_count(self) -> None:
        many_plan = self.tmp_path / "many-plan-override"
        pending_count = preflight_environment.TURN_BUDGET_YELLOW_THRESHOLD + 5
        _write_loop_ready_plan(
            many_plan,
            tuple(["pending"] * pending_count),
        )

        report = preflight.run_environment_preflight(
            many_plan,
            state_dir=self.xdg_state,
            max_pending_sessions=pending_count,
        )

        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn(
            f"{pending_count} session(s) pending",
            budget_findings[0].message,
        )
        self.assertIn(
            f"max pending sessions {pending_count}",
            budget_findings[0].detail,
        )

    def test_env_turn_budget_complete_plan_is_green(self) -> None:
        complete_plan = self.tmp_path / "complete-plan"
        _write_loop_ready_plan(complete_plan, ("complete", "complete"))
        report = preflight.run_environment_preflight(
            complete_plan, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn("all sessions complete", budget_findings[0].message)

    def test_run_environment_preflight_emits_no_core_findings(self) -> None:
        # Break readiness so core would emit a red. Environment-only must skip it.
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        categories = {f.category for f in report.findings}
        self.assertNotIn("manifest-readiness", categories)
        self.assertNotIn("plan-structure", categories)

    def test_run_preflight_composes_core_and_env(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        # Prepend the controlled dir to system PATH so git remains discoverable
        # while the fake codex/spec/amx executables take precedence.
        for name in ("codex", "spec", "amx"):
            self._drop_executable(self.path_dir, name)
        path_str = os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])
        with patched_env(PATH=path_str):
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=repo_dir,
                harness="codex",
                commit_mode="per-session",
                state_dir=self.xdg_state,
            )
        # Core fields surface.
        self.assertIsNotNone(report.snapshot)
        self.assertIsNotNone(report.readiness)
        self.assertEqual({"1"}, set(report.contracts_by_session))
        # Env findings are present (env-amx + env-turn-budget at minimum, both green).
        categories = {f.category for f in report.findings}
        self.assertIn("env-amx", categories)
        self.assertIn("env-turn-budget", categories)
        # No red/yellow → ok.
        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
