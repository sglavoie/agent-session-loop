# mypy: ignore-errors
"""Workbench conversation recovery reads and launch idempotency."""

# ruff: noqa: F403, F405
from ._test_asl_workbench_redirect_support import *


class ConversationRecoveryTests(WorkbenchRedirectTestCase):
    """The two pauses that hand a decision to a person, reported and not taken."""

    def paused_at(self, reason: str) -> None:
        self.write_state(paused=True, pause_reason=reason)

    def test_an_unresumable_conversation_reports_its_one_use_authorization(
        self,
    ) -> None:
        self.paused_at("conversation-recovery-required")

        status, body, _ = self.recovery()

        self.assertEqual(200, status)
        self.assertTrue(body["recovery_required"])
        self.assertEqual("conversation-recovery-required", body["pause_reason"])
        self.assertEqual(
            intervention.RECOVERY_DECISION_FRESH_CONVERSATION, body["decision"]
        )
        self.assertFalse(body["auto_resume"])
        self.assertEqual(intervention.RECOVERY_AUTHORIZED_BY, body["authorized_by"])
        self.assertTrue(body["one_use"])
        self.assertEqual(redirect_cli.FRESH_CONVERSATION_NOTE, body["note"])
        expected = redirect_cli.recovery_commands(
            self.canonical_plan_dir,
            str(self.state_dir),
            decision=intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
        )
        self.assertEqual(expected["inspect_command"], body["inspect_command"])
        self.assertEqual(
            expected["authorization_command"], body["authorization_command"]
        )
        self.assertIn("--fresh-conversation", body["authorization_command"])

    def test_the_read_performs_no_recovery(self) -> None:
        """Performing it here would persist a one-use decision in a replayable
        request, so the endpoint reports the relaunch and never runs it."""
        self.paused_at("conversation-recovery-required")
        before = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)

        self.recovery()
        self.recovery()

        after = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        self.assertTrue(after.paused)
        self.assertEqual(before.fingerprint(), after.fingerprint())
        self.assertEqual((), self.journal().requests)

    def test_a_blocked_successor_has_no_authorization_to_offer(self) -> None:
        """No text an operator sends makes a second worker safe to start."""
        self.paused_at("redirect-successor-blocked")

        status, body, _ = self.recovery()

        self.assertEqual(200, status)
        self.assertTrue(body["recovery_required"])
        self.assertEqual(
            intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE, body["decision"]
        )
        self.assertIsNone(body["authorization_command"])
        # Both describe the authorization command, so both stay empty where
        # there is none: telling an operator an absent authorization is one-use
        # would read as an invitation to go find it.
        self.assertFalse(body["one_use"])
        self.assertIsNone(body["note"])
        self.assertIn("redirect-status", body["inspect_command"])

    def test_a_fresh_start_reports_the_corrections_it_would_inherit(self) -> None:
        request = redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
            operation_id=OPERATION_ID,
        )
        self.paused_at("conversation-recovery-required")

        _, body, _ = self.recovery()

        self.assertEqual(
            [request.operation_id],
            [row["operation_id"] for row in body["retained_operations"]],
        )
        self.assertEqual(
            [redirect_cli.STAGE_UNACCEPTED],
            [row["stage"] for row in body["retained_operations"]],
        )

    def test_an_unrelated_pause_needs_no_conversation_decision(self) -> None:
        """Paused, and not on this: that is an answer rather than a null."""
        self.paused_at("verify-failed")

        _, body, _ = self.recovery()

        self.assertFalse(body["recovery_required"])
        self.assertEqual("verify-failed", body["pause_reason"])
        self.assertIsNone(body["decision"])
        self.assertIsNone(body["inspect_command"])
        self.assertIsNone(body["authorization_command"])

    def test_a_running_plan_needs_no_conversation_decision(self) -> None:
        self.write_state()

        _, body, _ = self.recovery()

        self.assertFalse(body["recovery_required"])
        self.assertIsNone(body["pause_reason"])
        self.assertIsNone(body["decision"])

    def test_the_read_and_the_cli_name_the_same_recovery(self) -> None:
        """One mapping behind both surfaces, so neither can invent a decision."""
        for reason in intervention.RECOVERY_DECISION_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.paused_at(reason)
                _, body, _ = self.recovery()
                classified = intervention.recovery_decision(reason, paused=True)
                self.assertEqual(classified.decision, body["decision"])
                self.assertEqual(
                    redirect_cli.recovery_commands(
                        self.canonical_plan_dir,
                        str(self.state_dir),
                        decision=classified.decision,
                    ),
                    {
                        "inspect_command": body["inspect_command"],
                        "authorization_command": body["authorization_command"],
                    },
                )


class LaunchIdempotencyTests(WorkbenchRedirectTestCase):
    """One run per plan, however many times a reconnecting browser asks."""

    def launch_result(
        self, *, pid: int, reply: bool = False
    ) -> workbench_launch.LaunchResult:
        return workbench_launch.LaunchResult(
            pid=pid,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=len("go-ok") if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def test_a_resent_launch_with_the_same_id_spawns_one_run(self) -> None:
        # A pid that is genuinely alive, because the guard's second check asks
        # the operating system whether the run it started is still going.
        result = self.launch_result(pid=os.getpid())
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=result
        ) as patched:
            first_status, first, _ = self.post(
                "/api/launch",
                {"plan_dir": str(self.plan_dir), "launch_id": "lid-1"},
            )
            second_status, second, _ = self.post(
                "/api/launch",
                {"plan_dir": str(self.plan_dir), "launch_id": "lid-1"},
            )

        self.assertEqual(200, first_status)
        self.assertFalse(first["replayed"])
        self.assertEqual(200, second_status)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["launch"], second["launch"])
        patched.assert_called_once()

    def test_a_second_launch_under_a_new_id_is_refused_while_one_is_in_flight(
        self,
    ) -> None:
        """The window a reconnecting browser lands in: spawned, not yet locked."""
        result = self.launch_result(pid=os.getpid())
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=result
        ) as patched:
            self.post("/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "a"})
            status, body, _ = self.post(
                "/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "b"}
            )

        self.assertEqual(409, status)
        self.assertEqual("launch-already-in-flight", body["reason"])
        patched.assert_called_once()

    def test_a_launch_is_refused_while_a_run_holds_the_plan(self) -> None:
        self.write_state()
        self.write_lock()
        with mock.patch.object(workbench_launch, "launch_plan") as patched:
            status, body, _ = self.post("/api/launch", {"plan_dir": str(self.plan_dir)})

        self.assertEqual(409, status)
        self.assertEqual("run-already-active", body["reason"])
        self.assertIn(RUN_ID, body["error"])
        patched.assert_not_called()

    def test_a_resent_reply_with_the_same_id_resumes_once(self) -> None:
        result = self.launch_result(pid=os.getpid(), reply=True)
        with mock.patch.object(
            workbench_launch, "resume_plan_with_reply", return_value=result
        ) as patched:
            first_status, first, _ = self.post(
                "/api/reply",
                {
                    "plan_dir": str(self.plan_dir),
                    "reply_text": "go-ok",
                    "launch_id": "lid-1",
                },
            )
            second_status, second, _ = self.post(
                "/api/reply",
                {
                    "plan_dir": str(self.plan_dir),
                    "reply_text": "go-ok",
                    "launch_id": "lid-1",
                },
            )

        self.assertEqual(200, first_status)
        self.assertFalse(first["replayed"])
        self.assertEqual(200, second_status)
        self.assertTrue(second["replayed"])
        patched.assert_called_once()

    def test_a_reply_with_no_text_is_refused_before_any_launch_is_counted(
        self,
    ) -> None:
        """A malformed request must not consume the idempotency key it carried."""
        result = self.launch_result(pid=os.getpid(), reply=True)
        with mock.patch.object(
            workbench_launch, "resume_plan_with_reply", return_value=result
        ) as patched:
            status, body, _ = self.post(
                "/api/reply",
                {"plan_dir": str(self.plan_dir), "launch_id": "lid-1"},
            )
            patched.assert_not_called()
            replay_status, replay, _ = self.post(
                "/api/reply",
                {
                    "plan_dir": str(self.plan_dir),
                    "reply_text": "go-ok",
                    "launch_id": "lid-1",
                },
            )

        self.assertEqual(400, status)
        self.assertIn("reply_text", body["error"])
        self.assertEqual(200, replay_status)
        self.assertFalse(replay["replayed"])

    def test_a_spawned_process_that_exited_stops_blocking_launches(self) -> None:
        """The in-flight check must not outlive the process it is about.

        A pid is a number the operating system hands out again. Held forever, a
        long-dead run would eventually refuse a launch on the strength of some
        unrelated process wearing its number, so an exited pid is forgotten the
        first time the guard looks at it.
        """
        result = self.launch_result(pid=os.getpid())
        server = self.server()
        with mock.patch.object(workbench_launch, "launch_plan", return_value=result):
            self.post("/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "a"})
        self.assertEqual(os.getpid(), server.launched_pid(self.canonical_plan_dir))

        with (
            mock.patch.object(
                workbench_launch, "launch_plan", return_value=result
            ) as patched,
            mock.patch.object(state, "is_pid_running", return_value=False),
        ):
            status, body, _ = self.post(
                "/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "b"}
            )

        self.assertEqual(200, status)
        self.assertFalse(body["replayed"])
        patched.assert_called_once()

    def test_a_blank_launch_id_is_refused(self) -> None:
        status, body, _ = self.post(
            "/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "   "}
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_an_overlong_launch_id_is_refused(self) -> None:
        status, body, _ = self.post(
            "/api/launch",
            {
                "plan_dir": str(self.plan_dir),
                "launch_id": "x" * (workbench_server.MAX_LAUNCH_ID_CHARS + 1),
            },
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("launch_id", body["error"])

    def test_a_launch_guard_reaches_across_two_servers(self) -> None:
        """Two servers share the plan's operation journal, so they share a guard.

        This test previously pinned the opposite -- that the receipt table was
        one server's own memory, and a resend to a second server spawned a
        second run. Session 5 made admission durable and cross-process, so the
        resend now replays from the journal instead.
        """
        other = self.start_server()
        result = self.launch_result(pid=os.getpid())
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=result
        ) as patched:
            self.post("/api/launch", {"plan_dir": str(self.plan_dir), "launch_id": "a"})
            status, body, _ = self.post(
                "/api/launch",
                {"plan_dir": str(self.plan_dir), "launch_id": "a"},
                server=other,
            )

        self.assertEqual(200, status)
        self.assertTrue(body["replayed"])
        # The second server never spawned. It has no receipt of its own for
        # this id, so it answers with the operation the journal holds and no
        # launch payload rather than inventing one.
        self.assertIsNone(body["launch"])
        self.assertEqual("a", body["operation"]["operation_id"])
        patched.assert_called_once()
