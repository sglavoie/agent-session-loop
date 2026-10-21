# mypy: ignore-errors
from .asl_workbench_launch_support import *
from .asl_workbench_launch_support import _BrokenStdinFakePopen, _FakePopen


class WorkbenchLaunchArgvTests(LaunchPlanSeedMixin, unittest.TestCase):
    """Cover build_launch_argv and launch profile/plan validation."""

    def test_build_launch_argv_maps_profile_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench_registry_models.LaunchProfile(
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

            argv = workbench_launch.build_launch_argv(
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
            profile = workbench_registry_models.LaunchProfile(
                commit_mode="end",
                unsafe_bypass=False,
            )

            argv = workbench_launch.build_launch_argv(
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

            argv = workbench_launch.build_launch_argv(plan_dir)

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

            argv = workbench_launch.build_launch_argv(plan_dir)

            self.assertEqual(
                str(workspace_root.resolve()), argv[argv.index("--cwd") + 1]
            )
            captured: list[_FakePopen] = []

            def factory(child_argv, **kwargs):
                process = _FakePopen(child_argv, **kwargs)
                captured.append(process)
                return process

            launched = workbench_launch.launch_plan(plan_dir, popen_factory=factory)
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

            with self.assertRaises(workbench_launch.LaunchError) as caught:
                workbench_launch.build_launch_argv(
                    plan_dir,
                    profile=workbench_registry_models.LaunchProfile(
                        cwd=str(repositories["SVC"])
                    ),
                )

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PROFILE, caught.exception.reason
            )
            self.assertIn("--rebind-workspace", caught.exception.message)

    def test_invalid_plan_dir_missing_raises_invalid_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(missing)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PLAN, ctx.exception.reason
            )

    def test_invalid_plan_dir_missing_manifest_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            broken = tmp_path / "broken"
            broken.mkdir()
            (broken / "context.md").write_text("# Context\n", encoding="utf-8")

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(broken)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PLAN, ctx.exception.reason
            )
            self.assertIn("manifest.md", ctx.exception.message)

    def test_invalid_plan_dir_missing_context_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            broken = tmp_path / "broken"
            broken.mkdir()
            (broken / "manifest.md").write_text("# Manifest\n", encoding="utf-8")

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(broken)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PLAN, ctx.exception.reason
            )
            self.assertIn("context.md", ctx.exception.message)

    def test_invalid_profile_harness_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench_registry_models.LaunchProfile(harness="bogus")

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_review_harness_matches_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench_registry_models.LaunchProfile(
                harness="codex", review_harness="codex"
            )

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_review_model_requires_review_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench_registry_models.LaunchProfile(
                review_harness_model="claude-sonnet"
            )

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )
            self.assertEqual(
                "review_harness_model requires review_harness", ctx.exception.message
            )

    def test_invalid_profile_commit_mode_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench_registry_models.LaunchProfile(commit_mode="weekly")  # type: ignore[arg-type]

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_non_positive_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench_registry_models.LaunchProfile(timeout=0)

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_invalid_profile_negative_review_fix_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            profile = workbench_registry_models.LaunchProfile(max_review_fix_cycles=-1)

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.build_launch_argv(plan_dir, profile=profile)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_INVALID_PROFILE, ctx.exception.reason
            )

    def test_registry_loader_drops_negative_review_fix_cap_with_warning(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": (
                            workbench_registry_models.REGISTRY_SCHEMA_VERSION
                        ),
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

            registry = workbench_registry.load_registry(registry_path)

            self.assertIsNone(registry.pins[0].profile.max_review_fix_cycles)
            self.assertEqual(
                ("pin[0]: ignoring negative max_review_fix_cycles -1",),
                registry.warnings,
            )
