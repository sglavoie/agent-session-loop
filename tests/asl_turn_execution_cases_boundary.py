from .asl_turn_execution_support import *


class TurnExecutorBoundaryTests(unittest.TestCase):
    """The shape of the boundary itself, independent of any single ending."""

    def test_the_run_loop_collaborators_satisfy_the_protocols(self) -> None:
        # The Protocols exist so turn_execution_* never import runner; that is
        # only true if the runner's own objects still fit through them.
        persistence: turn_execution_protocols.TurnPersistence = (
            run_persistence._RunPersistence(write_state=False)
        )
        progress: turn_execution_protocols.TurnProgress = (
            run_reporting._ProgressReporter(enabled=False)
        )
        self.assertIsNotNone(persistence.lock)
        self.assertIsNotNone(progress)

    def test_turn_execution_does_not_import_the_runner(self) -> None:
        for module in (
            turn_execution_argv,
            turn_execution_artifacts,
            turn_execution_commit,
            turn_execution_constants,
            turn_execution_executor,
            turn_execution_hang_sentinel,
            turn_execution_hang_settings,
            turn_execution_live,
            turn_execution_protocols,
            turn_execution_signals,
        ):
            with self.subTest(module=module.__name__):
                assert_no_runner_import(self, module)

    def test_transition_records_are_frozen(self) -> None:
        for record in (
            turn_execution_executor.TurnIdentity,
            turn_execution_executor.TurnLaunch,
            turn_execution_executor.TurnExecution,
            turn_execution_commit.TurnFinalization,
            turn_execution_commit.TurnCommit,
        ):
            with self.subTest(record=record.__name__):
                self.assertTrue(record.__dataclass_params__.frozen)


class ResumedTurnTests(unittest.TestCase):
    """A second turn of the same run against the same session."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_resumed_turn_keeps_its_own_artifacts_and_totals(self) -> None:
        first = TurnExecutorHarness(self.root, turn_index=1)
        first_execution = first.run(harness_result())
        with silent_delivery():
            first_final = first.executor.finalize(first_execution, before=first.before)

        second = TurnExecutorHarness(self.root, turn_index=2)
        second.plan_dir = first.plan_dir
        second.before = first.before
        second.payload["run_started_at"] = first.payload["run_started_at"]
        second_execution = second.run(harness_result(stdout="resumed\n"))
        with silent_delivery():
            second_final = second.executor.finalize(
                second_execution, before=second.before
            )

        self.assertNotEqual(first.paths["artifact_dir"], second.paths["artifact_dir"])
        self.assertEqual("working\n", first.paths["stdout"].read_text("utf-8"))
        self.assertEqual("resumed\n", second.paths["stdout"].read_text("utf-8"))
        self.assertEqual(1, first_final.turn_payload["turn_index"])
        self.assertEqual(2, second_final.turn_payload["turn_index"])
        # Each turn's own duration is bounded by the run total it reports.
        for execution in (first_execution, second_execution):
            with self.subTest(turn=execution.live_turn["turn_index"]):
                self.assertLessEqual(
                    execution.live_turn["duration_seconds"],
                    execution.live_turn["total_duration_seconds"],
                )

    def test_each_turn_appends_its_own_started_event(self) -> None:
        first = TurnExecutorHarness(self.root, turn_index=1)
        first.run(harness_result())
        second = TurnExecutorHarness(self.root, turn_index=2)
        second.plan_dir = first.plan_dir
        second.run(harness_result())

        self.assertEqual(1, first.names().count("turn_started"))
        self.assertEqual(1, second.names().count("turn_started"))
        self.assertEqual(
            1, second.persistence.payloads("turn_started")[0]["turn_index"] - 1
        )
