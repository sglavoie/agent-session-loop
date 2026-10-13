"""Workbench launch tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli_trace,
    config,
    events,
    plans_files,
    plans_metadata,
    plans_registry,
    state,
    workbench,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _BrokenStdinFakePopen,
    _FakePopen,
    init_multi_repo_workspace,
    install_fake_codex,
    write_multi_repo_plan,
    write_plan,
)


class WorkbenchLaunchTests(unittest.TestCase):
    """Cover build_launch_argv, validation, and Popen-based launch behavior."""

    def _seed_launchable_plan(self, tmp_path: Path) -> Path:
        plan_dir = tmp_path / "plan"
        write_plan(plan_dir, ("pending", "pending"))
        return plans_files.canonicalize_path(plan_dir)

    def test_build_launch_argv_maps_profile_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(
                cwd="/tmp/repo",
                harness="codex",
                harness_model="o4-mini",
                effort="high",
                timeout=900,
                commit_mode="per-session",
                review_harness="claude",
                review_harness_model="claude-sonnet",
                max_review_fix_cycles=3,
                unsafe_bypass=True,
                allow_not_loop_ready=True,
                note="launch note",
            )

            argv = workbench.build_launch_argv(
                plan_dir,
                profile=profile,
                state_dir=tmp_path / "state",
                python_executable="/usr/bin/python3-pinned",
            )

            self.assertEqual(argv[0], "/usr/bin/python3-pinned")
            self.assertEqual(argv[1:4], ["-m", "agent_session_loop", "run"])
            self.assertEqual(argv[4], str(plan_dir))
            self.assertIn("--cwd", argv)
            self.assertEqual(argv[argv.index("--cwd") + 1], "/tmp/repo")
            self.assertIn("--harness", argv)
            self.assertEqual(argv[argv.index("--harness") + 1], "codex")
            self.assertIn("--review-harness", argv)
            self.assertEqual(argv[argv.index("--review-harness") + 1], "claude")
            self.assertIn("--harness-model", argv)
            self.assertEqual(argv[argv.index("--harness-model") + 1], "o4-mini")
            self.assertIn("--review-harness-model", argv)
            self.assertEqual(
                argv[argv.index("--review-harness-model") + 1], "claude-sonnet"
            )
            self.assertNotIn("--model", argv)
            self.assertIn("--effort", argv)
            self.assertEqual(argv[argv.index("--effort") + 1], "high")
            self.assertIn("--timeout", argv)
            self.assertEqual(argv[argv.index("--timeout") + 1], "900")
            self.assertIn("--max-review-fix-cycles", argv)
            self.assertEqual(argv[argv.index("--max-review-fix-cycles") + 1], "3")
            self.assertIn("--commit-per-session", argv)
            self.assertNotIn("--commit-at-end", argv)
            self.assertIn("--unsafe-bypass", argv)
            self.assertNotIn("--no-unsafe-bypass", argv)
            self.assertIn("--allow-not-loop-ready", argv)
            self.assertIn("--note", argv)
            self.assertEqual(argv[argv.index("--note") + 1], "launch note")
            self.assertIn("--state-dir", argv)
            self.assertEqual(
                argv[argv.index("--state-dir") + 1], str(tmp_path / "state")
            )
            self.assertIn("--json", argv)
            self.assertNotIn("--reply", argv)
            self.assertNotIn("--reply-stdin", argv)

    def test_build_launch_argv_handles_commit_at_end_and_no_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(
                commit_mode="end",
                unsafe_bypass=False,
            )

            argv = workbench.build_launch_argv(
                plan_dir,
                profile=profile,
                reply_via_stdin=True,
                json_output=False,
            )

            self.assertIn("--commit-at-end", argv)
            self.assertNotIn("--commit-per-session", argv)
            self.assertIn("--no-unsafe-bypass", argv)
            self.assertNotIn("--unsafe-bypass", argv)
            self.assertNotIn("--json", argv)
            self.assertIn("--reply-stdin", argv)
            self.assertNotIn("--reply", argv)

    def test_build_launch_argv_empty_profile_uses_minimal_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)

            argv = workbench.build_launch_argv(plan_dir)

            self.assertEqual(argv[1:4], ["-m", "agent_session_loop", "run"])
            self.assertEqual(argv[4], str(plan_dir))
            for unexpected in (
                "--cwd",
                "--harness",
                "--model",
                "--harness-model",
                "--review-harness-model",
                "--max-review-fix-cycles",
                "--effort",
                "--timeout",
                "--commit-per-session",
                "--commit-at-end",
                "--allow-not-loop-ready",
                "--note",
                "--state-dir",
                "--reply",
                "--reply-stdin",
            ):
                self.assertNotIn(unexpected, argv)
            self.assertIn("--json", argv)

    def test_registered_launch_uses_prepared_workspace_and_projects_repositories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            workspace_root = tmp_path / "workspace"
            init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_path / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir, workspace_root, registry.digest
            )

            argv = workbench.build_launch_argv(plan_dir)

            self.assertEqual(
                str(workspace_root.resolve()), argv[argv.index("--cwd") + 1]
            )
            captured: list[_FakePopen] = []

            def factory(child_argv, **kwargs):
                process = _FakePopen(child_argv, **kwargs)
                captured.append(process)
                return process

            launched = workbench.launch_plan(plan_dir, popen_factory=factory)
            payload = launched.to_payload()
            self.assertEqual(str(workspace_root.resolve()), payload["cwd"])
            self.assertEqual(str(workspace_root.resolve()), payload["workspace_root"])
            self.assertEqual(
                ["SVC", "OAK"], [row["alias"] for row in payload["repositories"]]
            )

    def test_registered_launch_rejects_child_repository_as_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            workspace_root = tmp_path / "workspace"
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_path / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir, workspace_root, registry.digest
            )

            with self.assertRaises(workbench.LaunchError) as caught:
                workbench.build_launch_argv(
                    plan_dir,
                    profile=workbench.LaunchProfile(cwd=str(repositories["SVC"])),
                )

            self.assertEqual(
                workbench.LAUNCH_REASON_INVALID_PROFILE, caught.exception.reason
            )
            self.assertIn("--rebind-workspace", caught.exception.message)

    def test_invalid_plan_dir_missing_raises_invalid_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(missing)

            self.assertEqual(workbench.LAUNCH_REASON_INVALID_PLAN, ctx.exception.reason)

    def test_invalid_plan_dir_missing_manifest_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            broken = tmp_path / "broken"
            broken.mkdir()
            (broken / "context.md").write_text("# Context\n", encoding="utf-8")

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(broken)

            self.assertEqual(workbench.LAUNCH_REASON_INVALID_PLAN, ctx.exception.reason)
            self.assertIn("manifest.md", ctx.exception.message)

    def test_invalid_plan_dir_missing_context_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            broken = tmp_path / "broken"
            broken.mkdir()
            (broken / "manifest.md").write_text("# Manifest\n", encoding="utf-8")

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(broken)

            self.assertEqual(workbench.LAUNCH_REASON_INVALID_PLAN, ctx.exception.reason)
            self.assertIn("context.md", ctx.exception.message)

    def test_invalid_profile_harness_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(harness="bogus")

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_review_harness_matches_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(harness="codex", review_harness="codex")

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_review_model_requires_review_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(review_harness_model="claude-sonnet")

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )
            self.assertEqual(
                "review_harness_model requires review_harness", ctx.exception.message
            )

    def test_invalid_profile_commit_mode_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(commit_mode="weekly")  # type: ignore[arg-type]

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_non_positive_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(timeout=0)

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_negative_review_fix_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench.LaunchProfile(max_review_fix_cycles=-1)

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_registry_loader_drops_negative_review_fix_cap_with_warning(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            registry_path = tmp_path / workbench.REGISTRY_FILE_NAME
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": workbench.REGISTRY_SCHEMA_VERSION,
                        "pins": [
                            {
                                "plan_dir": str(plan_dir),
                                "profile": {"max_review_fix_cycles": -1},
                            }
                        ],
                        "plan_roots": [],
                    }
                ),
                encoding="utf-8",
            )

            registry = workbench.load_registry(registry_path)

            self.assertIsNone(registry.pins[0].profile.max_review_fix_cycles)
            self.assertEqual(
                ("pin[0]: ignoring negative max_review_fix_cycles -1",),
                registry.warnings,
            )

    def test_launch_plan_records_metadata_without_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            result = workbench.launch_plan(
                plan_dir,
                profile=workbench.LaunchProfile(harness="codex", timeout=42),
                state_dir=tmp_path / "state",
                popen_factory=factory,
            )

            self.assertEqual(1, len(captured))
            spawned = captured[0]
            self.assertFalse(result.reply_provided)
            self.assertIsNone(result.reply_length)
            self.assertIsNone(result.reply_origin)
            self.assertEqual(spawned.pid, result.pid)
            self.assertEqual(tuple(spawned.argv), result.argv)
            self.assertIn("--harness", result.argv)
            self.assertNotIn("--reply-stdin", result.argv)
            self.assertNotIn("--reply", result.argv)
            self.assertEqual(plan_dir, result.plan_dir)
            self.assertEqual((tmp_path / "state").resolve(), result.state_dir)
            payload = result.to_payload()
            self.assertEqual("codex", payload["profile"]["harness"])
            self.assertEqual(42, payload["profile"]["timeout"])
            self.assertFalse(payload["reply_provided"])
            self.assertIsNone(payload["reply_origin"])

    def test_launch_plan_streams_reply_through_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            reply = "answer is 1a"
            result = workbench.launch_plan(
                plan_dir,
                reply_text=reply,
                popen_factory=factory,
            )

            self.assertEqual(1, len(captured))
            spawned = captured[0]
            self.assertTrue(result.reply_provided)
            self.assertEqual(len(reply), result.reply_length)
            self.assertEqual("stdin", result.reply_origin)
            self.assertIn("--reply-stdin", spawned.argv)
            self.assertNotIn("--reply", spawned.argv)
            for token in spawned.argv:
                self.assertNotIn(reply, token)
            self.assertEqual(reply, spawned.stdin_text)
            self.assertTrue(spawned.stdin.closed)

    def test_launch_plan_resume_helper_streams_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            result = workbench.resume_plan_with_reply(
                plan_dir,
                reply_text="resume with option B",
                popen_factory=factory,
            )

            self.assertTrue(result.reply_provided)
            self.assertEqual("stdin", result.reply_origin)
            self.assertEqual(1, len(captured))
            self.assertEqual("resume with option B", captured[0].stdin_text)

    def test_launch_plan_empty_reply_raises_empty_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.launch_plan(
                    plan_dir,
                    reply_text="   \n  ",
                    popen_factory=factory,
                )

            self.assertEqual(workbench.LAUNCH_REASON_EMPTY_REPLY, ctx.exception.reason)
            self.assertEqual(0, len(captured), "process must not be spawned")

    def _spawn_and_capture_env(self, **launch_kwargs) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            workbench.launch_plan(plan_dir, popen_factory=factory, **launch_kwargs)

            self.assertEqual(1, len(captured))
            env = captured[0].env
            assert env is not None
            return dict(env)

    def test_launch_plan_marks_the_child_as_workbench_origin(self) -> None:
        env = self._spawn_and_capture_env()

        self.assertEqual(cli_trace.ORIGIN_WORKBENCH, env[cli_trace.ORIGIN_ENV_VAR])

    def test_launch_plan_preserves_caller_supplied_environment_entries(self) -> None:
        env = self._spawn_and_capture_env(
            env={"PATH": "/usr/bin", "ASL_WORKER_MODE": "packet"}
        )

        self.assertEqual("/usr/bin", env["PATH"])
        self.assertEqual("packet", env["ASL_WORKER_MODE"])
        self.assertEqual(cli_trace.ORIGIN_WORKBENCH, env[cli_trace.ORIGIN_ENV_VAR])

    def test_launch_plan_overrides_a_caller_supplied_origin(self) -> None:
        env = self._spawn_and_capture_env(
            env={cli_trace.ORIGIN_ENV_VAR: cli_trace.ORIGIN_DIRECT}
        )

        self.assertEqual(cli_trace.ORIGIN_WORKBENCH, env[cli_trace.ORIGIN_ENV_VAR])

    def test_launch_plan_without_env_still_inherits_and_marks(self) -> None:
        with mock.patch.dict(os.environ, {"ASL_LAUNCH_PROBE": "inherited"}):
            env = self._spawn_and_capture_env()

        self.assertEqual("inherited", env["ASL_LAUNCH_PROBE"])
        self.assertEqual(cli_trace.ORIGIN_WORKBENCH, env[cli_trace.ORIGIN_ENV_VAR])

    def test_launch_plan_propagates_popen_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)

            def factory(argv, **kwargs):
                raise OSError("permission denied")

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.launch_plan(plan_dir, popen_factory=factory)

            self.assertEqual(
                workbench.LAUNCH_REASON_PROCESS_FAILED, ctx.exception.reason
            )

    def test_launch_plan_stdin_failure_kills_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_BrokenStdinFakePopen] = []

            def factory(argv, **kwargs):
                proc = _BrokenStdinFakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            with self.assertRaises(workbench.LaunchError) as ctx:
                workbench.launch_plan(
                    plan_dir,
                    reply_text="oops",
                    popen_factory=factory,
                )

            self.assertEqual(
                workbench.LAUNCH_REASON_PROCESS_FAILED, ctx.exception.reason
            )
            self.assertEqual(1, len(captured))
            self.assertEqual(-9, captured[0].returncode)

    def test_launch_plan_fake_harness_creates_normal_asl_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            state_dir = tmp_path / "state"
            config_home = tmp_path / "config"
            harness_config_path = (
                config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            )
            harness_config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_path)
            harness_config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["FAKE_CODEX_MODE"] = "success"
            env["FAKE_CODEX_SLEEP"] = "0"
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

            profile = workbench.LaunchProfile(
                cwd=str(REPO_ROOT),
                harness="codex",
                allow_not_loop_ready=True,
            )

            captured: list[subprocess.Popen] = []

            def factory(argv, **kwargs):
                proc = subprocess.Popen(argv, **kwargs)
                captured.append(proc)
                return proc

            result = workbench.launch_plan(
                plan_dir,
                profile=profile,
                state_dir=state_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                popen_factory=factory,
            )

            self.assertEqual(1, len(captured))
            child = captured[0]
            try:
                returncode = child.wait(timeout=120)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                self.fail("launched subprocess did not exit within timeout")

            self.assertEqual(0, returncode)
            self.assertEqual(child.pid, result.pid)
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            self.assertTrue((plan_state / events.STATE_FILE_NAME).exists())
            self.assertTrue((plan_state / events.EVENTS_FILE_NAME).exists())
            self.assertEqual(plan_dir, result.plan_dir)
            self.assertFalse(result.reply_provided)


if __name__ == "__main__":
    unittest.main()
