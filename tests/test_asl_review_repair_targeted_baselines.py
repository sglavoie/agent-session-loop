"""Repair-child baseline scope tests (ASI-6)."""

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import contracts, review_repair_validation
from tests.support import _command_contract, write_plan


class TargetedBaselineScopeTests(unittest.TestCase):
    """Run only the repair children's own baseline checks."""

    def test_a_completed_sessions_satisfied_fail_baseline_is_not_rejudged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            # Session 1 expected its check to fail before its work; the work is
            # done, so the check now passes. Session 2 stands in for a repair
            # child whose own baseline is satisfied.
            write_plan(
                plan_dir,
                ("complete", "pending"),
                contracts_by_id={
                    "1": _command_contract(
                        '["true"]', session="1", name="done", expectation="fail"
                    ),
                    "2": _command_contract(
                        '["true"]', session="2", name="child", expectation="pass"
                    ),
                },
            )
            contract = contracts.read_completion_contract(plan_dir, "2")

            summary = review_repair_validation._run_targeted_baselines(
                plan_dir, repo_root, {"2": contract}
            )

            self.assertEqual([], summary["unsatisfied"])
            self.assertEqual(["child"], [c["name"] for c in summary["checks"]])


if __name__ == "__main__":
    unittest.main()
