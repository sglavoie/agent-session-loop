# mypy: ignore-errors
from .asl_workbench_launch_support import *
from .asl_workbench_launch_support import _BrokenStdinFakePopen, _FakePopen


class WorkbenchLaunchSpawnTests(LaunchPlanSeedMixin, unittest.TestCase):
    """Cover Popen-based launch behavior."""

    def test_launch_plan_records_metadata_without_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            result = workbench_launch.launch_plan(
                plan_dir,
                profile=workbench_registry_models.LaunchProfile(
                    harness="codex", timeout=42
                ),
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
            result = workbench_launch.launch_plan(
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

            result = workbench_launch.resume_plan_with_reply(
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

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.launch_plan(
                    plan_dir,
                    reply_text="   \n  ",
                    popen_factory=factory,
                )

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_EMPTY_REPLY, ctx.exception.reason
            )
            self.assertEqual(0, len(captured), "process must not be spawned")

    def test_launch_plan_unencodable_reply_is_refused_before_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.launch_plan(
                    plan_dir,
                    reply_text="answer \ud800",
                    popen_factory=factory,
                )

            self.assertFalse(ctx.exception.spawned)
            self.assertEqual(0, len(captured), "process must not be spawned")

    def test_launch_plan_any_stdin_write_failure_kills_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_BrokenStdinFakePopen] = []

            def factory(argv, **kwargs):
                proc = _BrokenStdinFakePopen(argv, **kwargs)
                assert proc.stdin is not None

                def write(_data):
                    raise UnicodeEncodeError("utf-8", "x", 0, 1, "simulated")

                proc.stdin.write = write
                captured.append(proc)
                return proc

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.launch_plan(
                    plan_dir,
                    reply_text="ok",
                    popen_factory=factory,
                )

            self.assertTrue(ctx.exception.spawned)
            self.assertEqual(1, len(captured))
            self.assertEqual(-9, captured[0].returncode)

    def _spawn_and_capture_env(self, **launch_kwargs) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = self._seed_launchable_plan(tmp_path)
            captured: list[_FakePopen] = []

            def factory(argv, **kwargs):
                proc = _FakePopen(argv, **kwargs)
                captured.append(proc)
                return proc

            workbench_launch.launch_plan(
                plan_dir, popen_factory=factory, **launch_kwargs
            )

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

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.launch_plan(plan_dir, popen_factory=factory)

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_PROCESS_FAILED, ctx.exception.reason
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

            with self.assertRaises(workbench_launch.LaunchError) as ctx:
                workbench_launch.launch_plan(
                    plan_dir,
                    reply_text="oops",
                    popen_factory=factory,
                )

            self.assertEqual(
                workbench_launch.LAUNCH_REASON_PROCESS_FAILED, ctx.exception.reason
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

            profile = workbench_registry_models.LaunchProfile(
                cwd=str(REPO_ROOT),
                harness="codex",
                allow_not_loop_ready=True,
            )

            captured: list[subprocess.Popen] = []

            def factory(argv, **kwargs):
                proc = subprocess.Popen(argv, **kwargs)
                captured.append(proc)
                return proc

            result = workbench_launch.launch_plan(
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
