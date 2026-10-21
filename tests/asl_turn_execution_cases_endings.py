from .asl_turn_execution_support import *


class TurnExecutorEndingTests(unittest.TestCase):
    """Every way a turn can end, read back off the records it produced."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.harness = TurnExecutorHarness(Path(self._tmp.name))

    def finalize(
        self, execution: turn_execution_executor.TurnExecution
    ) -> turn_execution_commit.TurnFinalization:
        with silent_delivery():
            return self.harness.executor.finalize(execution, before=self.harness.before)

    def test_launch_starts_the_turn_before_the_process(self) -> None:
        execution = self.harness.run(harness_result())

        self.assertEqual("turn_started", self.harness.names()[0])
        self.assertIn("turn_started", self.harness.progress.calls)
        # The live record is finished, so nothing can still be writing to it.
        self.assertIsNotNone(execution.live_turn["finished_at"])
        self.assertIsNone(self.harness.persistence.states[-1]["active_turn"])
        self.assertIsInstance(execution.finished_at, datetime)

    def test_launch_materializes_the_logs_the_started_event_names(self) -> None:
        self.harness.run(harness_result())

        started = self.harness.persistence.payloads("turn_started")[0]
        for key in ("stdout_path", "stderr_path"):
            with self.subTest(key=key):
                self.assertTrue(Path(started[key]).exists(), key)

    def test_successful_turn_records_a_clean_exit(self) -> None:
        finalization = self.finalize(self.harness.run(harness_result()))

        payload = finalization.turn_payload
        self.assertEqual(0, payload["returncode"])
        self.assertFalse(payload["timed_out"])
        self.assertFalse(payload["interrupted"])
        self.assertIsNotNone(finalization.after)
        self.assertIsNone(finalization.plan_error)
        self.assertEqual("working\n", self.harness.paths["stdout"].read_text("utf-8"))
        self.assertEqual(
            "done\n", self.harness.paths["last_message"].read_text("utf-8")
        )

    def test_failed_turn_carries_the_classified_failure(self) -> None:
        finalization = self.finalize(
            self.harness.run(harness_result(returncode=2, stderr="boom\n"))
        )

        payload = finalization.turn_payload
        self.assertEqual(2, payload["returncode"])
        self.assertFalse(payload["timed_out"])
        self.assertNotEqual("complete", payload["status_result"])
        self.assertEqual("boom\n", self.harness.paths["stderr"].read_text("utf-8"))

    def test_shutdown_recovery_records_the_exit_without_earning_completion(
        self,
    ) -> None:
        recovery = {
            "policy": process_tree.POST_RESULT_ROOT_RECOVERY_POLICY,
            "root_pid": 123,
            "signal": "SIGKILL",
            "signaled_pids": [123],
        }
        retracted = {
            "reason": turn_execution_constants.HANG_REASON_RESULT_RECEIVED,
            "harness_pid": 123,
            "recovery": recovery,
        }
        with mock.patch.object(
            turn_execution_hang_sentinel.HangSentinel,
            "note_turn_exit",
            return_value=retracted,
        ):
            execution = self.harness.run(harness_result(returncode=137))
        finalization = self.finalize(execution)
        payload = finalization.turn_payload
        self.assertEqual(
            "post-result-shutdown", payload["failure_classification"]["marker"]
        )
        self.assertFalse(payload["failure_classification"]["retryable"])
        self.assertEqual("nonzero-exit", payload["status_result"])
        self.assertEqual(137, payload["returncode"])
        self.assertEqual(recovery, payload["harness_shutdown"]["recovery"])
        self.assertEqual(
            payload["harness_shutdown"],
            self.harness.persistence.payloads(events.TURN_EXECUTION_FINISHED_EVENT)[0][
                "harness_shutdown"
            ],
        )

    def test_resumed_output_preserves_but_cancels_shutdown_attribution(self) -> None:
        active = self.harness.active_turn()
        active["harness_shutdown"] = {
            "result": "recovery-attempted",
            "recovery": {
                "policy": process_tree.POST_RESULT_ROOT_RECOVERY_POLICY,
                "root_pid": 123,
                "signal": "SIGTERM",
                "signaled_pids": [123],
            },
        }
        live = self.harness.live_turn(active)
        with silent_delivery():
            live.record_activity("stdout", "claude status: requesting\n")
            finished = live.finish(clock.utc_now())
        self.assertEqual("activity", finished["harness_shutdown"]["canceled_by"])
        self.assertEqual(
            [123], finished["harness_shutdown"]["recovery"]["signaled_pids"]
        )

    def test_timed_out_turn_is_recorded_as_a_timeout(self) -> None:
        finalization = self.finalize(
            self.harness.run(harness_result(returncode=124, timed_out=True))
        )

        self.assertTrue(finalization.turn_payload["timed_out"])
        self.assertEqual("timeout", finalization.turn_payload["status_result"])
        execution_finished = self.harness.persistence.payloads(
            events.TURN_EXECUTION_FINISHED_EVENT
        )
        self.assertEqual([True], [event["timed_out"] for event in execution_finished])

    def test_interrupted_turn_is_a_result_rather_than_a_raise(self) -> None:
        # The run has to record what it spent before it stops, so a
        # KeyboardInterrupt out of the harness becomes a turn record.
        execution = self.harness.run(KeyboardInterrupt())

        self.assertTrue(execution.result.interrupted)
        self.assertEqual(
            turn_execution_constants.INTERRUPTED_EXIT, execution.result.returncode
        )
        self.assertEqual(["codex", "exec", "do the work"], execution.result.argv)

        finalization = self.finalize(execution)
        self.assertEqual("interrupted", finalization.turn_payload["status_result"])
        self.assertIsNone(self.harness.persistence.states[-1]["active_turn"])

    def test_unknown_failure_tears_the_live_turn_down_and_re_raises(self) -> None:
        active_turn = self.harness.active_turn()
        live_turn = self.harness.live_turn(active_turn)
        abandoned: list[str] = []

        with (
            silent_delivery(),
            mock.patch.object(harnesses, "run_harness", side_effect=RuntimeError("x")),
            self.assertRaises(RuntimeError),
        ):
            self.harness.executor.launch(
                self.harness.launch_spec(),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=self.harness.payload,
                on_failure=lambda: abandoned.append("stopped"),
            )

        self.assertEqual(["stopped"], abandoned)
        self.assertIsNone(self.harness.persistence.states[-1]["active_turn"])

    def test_after_stop_runs_before_the_live_turn_is_cleared(self) -> None:
        active_turn = self.harness.active_turn()
        live_turn = self.harness.live_turn(active_turn)
        observed: list[Any] = []

        with (
            silent_delivery(),
            mock.patch.object(harnesses, "run_harness", return_value=harness_result()),
        ):
            self.harness.executor.launch(
                self.harness.launch_spec(),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=self.harness.payload,
                after_stop=lambda: observed.append(self.harness.payload["active_turn"]),
            )

        self.assertEqual(1, len(observed))
        self.assertIsNotNone(observed[0])
        self.assertIsNone(self.harness.payload["active_turn"])

    def test_execution_finished_precedes_any_turn_finished(self) -> None:
        finalization = self.finalize(self.harness.run(harness_result()))

        names = self.harness.names()
        self.assertIn(events.TURN_EXECUTION_FINISHED_EVENT, names)
        self.assertNotIn(events.TURN_FINISHED_EVENT, names)
        self.assertIsNotNone(finalization.after)

    def test_unreadable_plan_is_carried_rather_than_raised(self) -> None:
        execution = self.harness.run(harness_result())
        # A harness that removed the manifest leaves a plan the runner cannot
        # re-read; the boundary reports that, it does not decide what it means.
        (self.harness.plan_dir / "manifest.md").unlink()

        finalization = self.finalize(execution)

        self.assertIsNone(finalization.after)
        self.assertIsNotNone(finalization.plan_error)
        self.assertEqual("malformed-plan", finalization.turn_payload["status_result"])

    def test_commit_publishes_an_immutable_record_once(self) -> None:
        finalization = self.finalize(self.harness.run(harness_result()))
        assert finalization.after is not None
        self.harness.payload["turns"] = [finalization.turn_payload]

        with silent_delivery():
            commit = self.harness.executor.commit(
                payload=self.harness.payload,
                turn_payload=finalization.turn_payload,
                live_turn=dict(finalization.turn_payload),
                accepted=finalization.after,
            )

        self.assertIsInstance(commit.record, MappingProxyType)
        self.assertTrue(commit.record["committed"])
        self.assertEqual(
            plans_payloads.snapshot_payload(commit.published), commit.record["after"]
        )
        self.assertEqual([events.TURN_FINISHED_EVENT], self.harness.names()[-1:])
        self.assertEqual(1, self.harness.names().count(events.TURN_FINISHED_EVENT))
        self.assertTrue(self.harness.paths["result"].exists())
        # The result file and the event agree, because the file is written
        # inside the same lock, before the event.
        persisted = json.loads(self.harness.paths["result"].read_text("utf-8"))
        self.assertEqual(
            persisted, self.harness.persistence.payloads(events.TURN_FINISHED_EVENT)[0]
        )
