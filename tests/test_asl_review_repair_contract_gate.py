"""Repair contract gate tests."""

import unittest
from unittest import mock

from agent_session_loop import (
    progress_repair,
    review_repair,
    review_repair_publication,
    review_repair_validation,
)
from tests.review_repair_support import ContractGateFixtures, _write_repair_ready_plan
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _structured_review_finding,
    init_multi_repo_workspace,
)

ASL_TEST_LANE = "integration"


class ReviewRepairContractGateTests(ContractGateFixtures, unittest.TestCase):
    """Gate repair children on their verification contracts."""

    def test_an_unresolvable_check_cwd_is_refused_before_authoring(self) -> None:
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "escaping",
                "cwd": "..",
                "argv": ["python3", "-c", "pass"],
            }
        )
        before = self._plan_bytes()

        result = self._materialize()

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual((), result.children)
        self.assertEqual(("repository-contract",), result.non_bypassable_blockers)
        self.assertTrue(
            any(
                "verification check 'escaping' that cannot resolve" in blocker
                for blocker in result.blockers
            ),
            msg=str(result.blockers),
        )
        self.assertFalse(result.rolled_back)
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(
            (self.plan_dir / review_repair.REPAIR_PUBLICATION_DIR_NAME).exists()
        )

    def test_the_refusal_is_non_bypassable(self) -> None:
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "escaping",
                "cwd": "nested/../../elsewhere",
                "argv": ["python3", "-c", "pass"],
            }
        )

        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            state_dir=self.state_dir,
            cwd=self.repo,
            allow_not_loop_ready=True,
        )

        self.assertEqual((), result.children)
        self.assertEqual(("repository-contract",), result.non_bypassable_blockers)
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

    def test_without_the_gate_the_same_plan_strands_a_pending_child(self) -> None:
        """The pre-fix path, pinned so the regression cannot come back quietly."""
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "escaping",
                "cwd": "..",
                "argv": ["python3", "-c", "pass"],
            }
        )

        with (
            mock.patch.object(
                review_repair_validation, "_validate_children_contract", return_value=[]
            ),
            mock.patch.object(
                review_repair_publication,
                "_rollback_publication_window",
                return_value={"errors": []},
            ),
        ):
            result = self._materialize()

        # Preflight is the one that catches it, and only after the write.
        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertTrue(
            any("malformed-contract" in blocker for blocker in result.blockers),
            msg=str(result.blockers),
        )
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertIn(
            "**Readiness**: draft",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_a_resolvable_check_is_authored(self) -> None:
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "resolvable",
                "argv": ["python3", "-c", "pass"],
            }
        )

        result = self._materialize()

        self.assertEqual((), result.non_bypassable_blockers)
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertTrue(result.loop_ready, msg=str(result.blockers))

    def test_a_registered_plan_rejects_the_same_shape_as_a_malformed_verdict(
        self,
    ) -> None:
        workspace = self.tmp_path / "workspace"
        repositories = init_multi_repo_workspace(
            workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="svc",
            repo="SVC",
            evidence=[{"path": "SVC:README.md", "symbol": "target"}],
            done_criteria=["SVC is fixed."],
            verification_checks=[
                {
                    "type": "command",
                    "name": "escaping",
                    "repo": "SVC",
                    "cwd": "../tundra_OAK",
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )
        _write_repair_ready_plan(
            self.plan_dir,
            repositories["SVC"],
            findings=[finding],
            specs=DEFAULT_MULTI_REPO_SPECS[:2],
        )
        progress_repair.repair_progress_journal(self.plan_dir)

        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, cwd=workspace
        )

        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("must stay beneath the selected repository", result.error or "")
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())


if __name__ == "__main__":
    unittest.main()
