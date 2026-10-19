# ruff: noqa: F403, F405
from .asl_overseer_context_support import *


class ContractTests(unittest.TestCase):
    """The published vocabularies are the ones the type says they are."""

    def test_every_source_kind_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.SourceKind)),
            set(overseer_context.SOURCE_KINDS),
        )

    def test_every_gap_reason_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.GapReason)),
            set(overseer_context.GAP_REASONS),
        )

    def test_every_staleness_reason_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.Staleness)),
            set(overseer_context.STALENESS_REASONS),
        )

    def test_every_refusal_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.Refusal)),
            set(overseer_context.REFUSALS),
        )

    def test_the_ledger_withholdings_are_reported_unparaphrased(self) -> None:
        """A refusal an operator reads must be the ledger's word for it."""
        self.assertLessEqual(
            set(overseer_store_models.WITHHOLDING_REASONS),
            set(overseer_context.REFUSALS),
        )

    def test_every_staleness_reason_is_also_a_refusal(self) -> None:
        self.assertLessEqual(
            set(overseer_context.STALENESS_REASONS),
            set(overseer_context.REFUSALS),
        )

    def test_every_family_says_what_its_absence_means(self) -> None:
        self.assertEqual(
            set(overseer_context.SOURCE_KINDS),
            set(overseer_context._ABSENT_DETAIL),
        )

    def test_context_is_asked_for_under_the_context_category(self) -> None:
        self.assertEqual(
            overseer_policy_client.ACCESS_CONTEXT, overseer_context.CONTEXT_ACCESS
        )

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_context)

    def test_assembling_context_spawns_no_process(self) -> None:
        plan = _Plan(self)
        plan.populate()

        with mock.patch.object(
            subprocess, "run", side_effect=AssertionError("spawned a process")
        ):
            packet = plan.build()
            plan.load(packet.incident_id)
            plan.expand(packet.incident_id, "status")

        self.assertIsNone(packet.refusal)

    def test_assembling_context_writes_nothing_into_the_plan(self) -> None:
        """The only durable record is the ledger's. The corpus is read-only."""
        plan = _Plan(self)
        plan.populate()
        before = {
            path: path.read_bytes()
            for path in sorted(plan.plan_state.rglob("*"))
            if path.is_file()
        }

        packet = plan.build()
        plan.load(packet.incident_id)
        plan.expand(packet.incident_id, "questions")

        after = {
            path: path.read_bytes()
            for path in sorted(plan.plan_state.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()


__all__ = tuple(name for name in globals() if not name.startswith("__"))
