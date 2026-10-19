from .asl_turn_finalization_support import *


class TurnFinalizationShapeTests(unittest.TestCase):
    def test_finalization_carries_execution_state_only(self) -> None:
        fields = tuple(turn_execution_commit.TurnFinalization.__dataclass_fields__)
        self.assertEqual(("result", "after", "plan_error", "turn_payload"), fields)

    def test_finalization_exposes_no_accepted_completion(self) -> None:
        public = [
            name
            for name in vars(turn_execution_commit.TurnFinalization)
            if not name.startswith("_")
            and callable(getattr(turn_execution_commit.TurnFinalization, name, None))
        ]
        self.assertEqual([], public)

    def test_execution_phase_never_announces_turn_finished(self) -> None:
        source = inspect.getsource(turn_execution_commit.finalize_turn_execution)
        self.assertIn("TURN_EXECUTION_FINISHED_EVENT", source)
        self.assertNotIn("turn_finished", source)

    def test_execution_event_precedes_turn_finished_in_run_loop(self) -> None:
        # Session 36 moved the publication into `turn_settlement.settle_turn`,
        # so the two calls no longer sit in one function. The claim is the
        # same: the loop finalizes execution before it reaches the boundary
        # that publishes, and that boundary is where the publication lives.
        loop = run_loop_view.run_locked_source()
        finalize = loop.index("turn_executor.finalize(")
        settle = loop.index("turn_settlement.settle_turn(")
        self.assertLess(finalize, settle)
        self.assertNotIn("turn_executor.commit(", loop)
        self.assertIn(
            "turn_executor.commit(",
            inspect.getsource(turn_settlement.settle_turn),
        )

    def test_turn_result_is_written_durably(self) -> None:
        source = inspect.getsource(turn_execution_artifacts.write_turn_artifacts)
        self.assertIn(
            'durability.write_json_file(paths["result"], payload, durable=True)', source
        )
