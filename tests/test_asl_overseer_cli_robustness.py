"""Overseer leaves meeting hostile records, stores and inputs fail closed."""

from __future__ import annotations

import shutil
import unittest

from agent_session_loop import (
    overseer_cli_claims,
    overseer_cli_common,
    overseer_discovery,
    state,
)
from tests.asl_overseer_cli_fleet import _OverseerCase

ASL_TEST_PORTABLE = True


class PlanResolutionTests(_OverseerCase):
    """A plan resolves to the record that proves it is that plan's."""

    def _copy_state_aside(self, name: str) -> None:
        own = state.plan_state_dir(self.plan, self.fleet.state_root)
        shutil.copytree(own, own.parent / name)

    def test_a_stray_copy_sorting_first_does_not_shadow_the_real_record(
        self,
    ) -> None:
        self._copy_state_aside("0" * 64)

        self.watch()
        payload = self.inspect()

        self.assertEqual(f"{self.plan.name}-run-1", payload["run_id"])

    def test_two_records_proving_one_plan_are_refused_as_ambiguous(self) -> None:
        own = state.plan_state_dir(self.plan, self.fleet.state_root)
        inventory = overseer_discovery.build_inventory(state_dir=self.fleet.state_root)
        (candidate,) = inventory.candidates
        twin = overseer_discovery.Inventory(
            state_root=inventory.state_root,
            candidates=(candidate, candidate),
            warnings=(),
            complete=True,
        )

        with self.assertRaises(overseer_cli_common.OverseerCliError) as caught:
            overseer_cli_claims._resolve_candidate(twin, str(self.plan))

        self.assertEqual("plan_identity_ambiguous", caught.exception.reason)
        self.assertIn(str(own), caught.exception.message)


if __name__ == "__main__":
    unittest.main()
