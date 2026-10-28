# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F403, F405, I001
"""ACP adapter transport tests."""

# ruff: noqa: F403, F405
from tests.asl_copilot_acp_support import *

import signal


ASL_TEST_LANE = "integration"


class AdapterTransportTests(unittest.TestCase):
    """The adapter against a real copilot process on a real pipe."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.last_message = self.tmp / "last-message.txt"
        self.record = self.tmp / "record.json"
        # The real sweep shells out to ``ps``; these tests are about the
        # transport, not the process table.
        sweep = mock.patch.object(harness_process, "sweep_enabled", return_value=False)
        sweep.start()
        self.addCleanup(sweep.stop)
        self.env = mock.patch.dict("os.environ", {"FAKE_ACP_RECORD": str(self.record)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _adapter(self, mode: str = "success") -> harness_copilot.CopilotAdapter:
        os.environ["FAKE_ACP_MODE"] = mode
        executable = self.tmp / "copilot"
        shutil.copyfile(FAKE_ACP_FIXTURE, executable)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return harness_copilot.CopilotAdapter(executable=str(executable))

    def _run(self, adapter: harness_copilot.CopilotAdapter, **kwargs: Any) -> Any:
        options: dict[str, Any] = {
            "cwd": self.tmp,
            "plan_dir": self.plan_dir,
            "prompt": PROMPT,
            "last_message_path": self.last_message,
            "timeout": WAIT_SECONDS,
            "unsafe_bypass": True,
        }
        options.update(kwargs)
        return adapter.run(**options)

    def _recorded(self) -> dict[str, Any]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_an_ordinary_worker_turn_runs_through_fake_acp(self) -> None:
        chunks: list[tuple[str, str]] = []

        result = self._run(
            self._adapter(),
            stream_callback=lambda name, text: chunks.append((name, text)),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(harnesses.TRANSPORT_ACP, result.transport)
        self.assertEqual("working\n", result.last_message)
        self.assertEqual("fake-model-1", result.observed_model)
        self.assertIn("$ just test-fast", result.stdout)
        self.assertIn("working\n", result.stdout)
        # Streamed as it arrived, one protocol message at a time, rather than
        # withheld until the turn was over.
        self.assertEqual(
            ["$ just test-fast\n", "work", "ing\n"],
            [text for name, text in chunks if name == "stdout"],
        )

    def test_an_agent_ignoring_shutdown_is_killed_without_a_harness_timeout(
        self,
    ) -> None:
        session_type = copilot_acp.CopilotAcpSession
        sessions = []

        def open_session(**kwargs: Any):
            session = session_type(**kwargs, shutdown_grace=0.01)
            sessions.append(session)
            return session

        for mode in ("success", "future-protocol"):
            with (
                self.subTest(mode=mode),
                mock.patch.dict(os.environ, {"FAKE_ACP_IGNORE_SHUTDOWN": "1"}),
                mock.patch.object(
                    copilot_acp, "CopilotAcpSession", side_effect=open_session
                ),
            ):
                # The timeout is a safety bound for a broken implementation;
                # neither it nor the harness's forced kill should end this run.
                result = self._run(self._adapter(mode))

                self.assertFalse(result.timed_out)
                self.assertEqual(-signal.SIGKILL, sessions[-1]._process.poll())
                if mode == "success":
                    self.assertEqual(0, result.returncode)
                    self.assertEqual("working\n", result.last_message)
                else:
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("protocol version", result.stderr)
                    self.assertIsNone(self._recorded()["prompt"])

    def test_the_agents_last_unterminated_line_still_reaches_the_log(self) -> None:
        result = self._run(self._adapter("trailing-prose"))

        # ``readline`` returns an unterminated string only at EOF, so prose the
        # binary printed on its way out arrives with no newline to frame it and
        # would otherwise be left in the decoder's buffer.
        self.assertEqual(0, result.returncode)
        self.assertIn("copilot: goodbye\n", result.stdout)

    def test_the_prompt_travels_the_protocol_and_never_the_log(self) -> None:
        result = self._run(self._adapter())

        self.assertEqual(PROMPT, self._recorded()["prompt"])
        self.assertNotIn(PROMPT, result.stdout)
        self.assertNotIn(PROMPT, " ".join(result.argv))
        self.assertNotIn("-p", result.argv)

    def test_the_worker_argv_keeps_its_access_restrictions(self) -> None:
        self._run(self._adapter())

        argv = self._recorded()["argv"]
        self.assertEqual(["-C", str(self.tmp)], argv[:2])
        self.assertIn("--add-dir", argv)
        self.assertEqual(str(self.plan_dir), argv[argv.index("--add-dir") + 1])
        self.assertIn("--allow-all-tools", argv)
        self.assertIn("--allow-all-paths", argv)
        self.assertIn("--no-ask-user", argv)
        self.assertIn("--silent", argv)
        self.assertEqual("--acp", argv[-1])

    def test_a_guard_deny_rule_still_reaches_the_launch_argv(self) -> None:
        policy = self.tmp / "policy.json"
        policy.write_text(
            json.dumps({"guards": [{"command": ["git", "push"]}]}), encoding="utf-8"
        )
        env = dict(os.environ)
        env[harness_adapter.guards.GUARD_POLICY_ENV_VAR] = str(policy)

        self._run(self._adapter(), env=env)

        self.assertIn("--deny-tool=shell(git push)", self._recorded()["argv"])

    def test_the_negotiated_capabilities_cross_the_wire(self) -> None:
        self._run(self._adapter())

        recorded = self._recorded()
        self.assertEqual(1, recorded["protocol_version"])
        self.assertEqual(
            {"readTextFile": False, "writeTextFile": False},
            recorded["client_capabilities"]["fs"],
        )
        self.assertFalse(recorded["client_capabilities"]["terminal"])
        self.assertEqual(str(self.tmp), recorded["session_params"]["cwd"])

    def test_an_audit_turn_keeps_its_established_one_shot_path(self) -> None:
        adapter = self._adapter()

        result = adapter.run(
            cwd=self.tmp,
            plan_dir=None,
            prompt=PROMPT,
            last_message_path=self.last_message,
            timeout=WAIT_SECONDS,
            access=harnesses.audit_read_only_access((self.tmp,)),
        )

        self.assertEqual(harnesses.TRANSPORT_EXEC, result.transport)
        self.assertNotIn("--acp", result.argv)
        self.assertIn("-p", result.argv)
        self.assertEqual(PROMPT, result.argv[-1])
        self.assertIn(
            f"--available-tools={harness_copilot.COPILOT_AUDIT_TOOLS}", result.argv
        )
        self.assertIn("--deny-tool=shell", result.argv)
        # Nothing spoke ACP, so the process recorded no handshake at all.
        self.assertIsNone(self._recorded()["client_capabilities"])
        self.assertIsNone(self._recorded()["prompt"])

    def test_a_permission_request_is_answered_once_under_bypass(self) -> None:
        result = self._run(self._adapter("permission"))

        answer = self._recorded()["permissions"][0]
        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "ao"}}, answer["result"]
        )
        self.assertEqual(0, result.returncode)

    def test_a_permission_request_without_bypass_is_rejected(self) -> None:
        result = self._run(self._adapter("permission"), unsafe_bypass=False)

        answer = self._recorded()["permissions"][0]
        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "ro"}}, answer["result"]
        )
        self.assertIn("rejected a permission request", result.stderr)

    def test_an_unadvertised_request_is_refused_over_a_real_pipe(self) -> None:
        result = self._run(self._adapter("unadvertised"))

        answer = self._recorded()["unadvertised"][0]
        self.assertEqual(-32601, answer["error"]["code"])
        self.assertIn("refused unadvertised request", result.stdout)

    def test_a_refusal_stop_reason_is_not_reported_as_success(self) -> None:
        result = self._run(self._adapter("refusal"))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("copilot acp stopped early: refusal", result.stderr)

    def test_an_agent_that_dies_after_the_prompt_is_not_reported_as_success(
        self,
    ) -> None:
        result = self._run(self._adapter("die-after-submit"))

        # The turn was spent: the prompt crossed the wire and the agent left
        # without a verdict, which must never read as a clean run.
        self.assertEqual(PROMPT, self._recorded()["prompt"])
        self.assertEqual(9, result.returncode)
        self.assertIn("copilot acp", result.stderr)

    def test_an_agent_with_no_acp_mode_fails_rather_than_falls_back(self) -> None:
        result = self._run(self._adapter("decline"))

        self.assertEqual(harnesses.TRANSPORT_ACP, result.transport)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("this build has no acp mode", result.stderr)

    def test_a_drifted_launch_control_is_reported_on_the_turn(self) -> None:
        result = self._run(self._adapter("model-drift"), model="asked-for-this")

        self.assertIn("launch control mismatch", result.stderr)
        self.assertEqual(0, result.returncode)

    def test_a_live_steer_is_declined_as_unsupported_rather_than_ignored(self) -> None:
        adapter = self._adapter()
        outcomes: list[guidance_delivery.GuidanceOutcome] = []

        def steer(transport: harnesses.HarnessTransport | None) -> None:
            assert transport is not None
            deliver = transport.guidance_transport()
            assert deliver is not None
            outcomes.append(deliver(_guidance_request()))

        self._run(adapter, on_transport=steer)

        self.assertEqual(
            [guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER],
            [outcome.reason for outcome in outcomes],
        )
