"""Expansion-evidence and scope-repair freshness/reconciliation tests.

Payload-only, portable tests; see `test_asl_workspace_evidence_boundary.py`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans_session_ids,
    plans_session_scope,
    workspace_evidence,
)

ASL_TEST_PORTABLE = True


class SessionExpansionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name)

    def test_an_absent_expansion_is_absent_rather_than_unavailable(self) -> None:
        record = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertFalse(record["present"])
        self.assertNotIn("unavailable", record)
        self.assertTrue(record["path"].endswith("session-7.expand.md"))

    def test_a_present_expansion_carries_its_mtime_and_content_digest(self) -> None:
        path = self.plan_dir / plans_session_ids.session_id_to_expansion_filename("7")
        path.write_text("# expansion\n", encoding="utf-8")

        record = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertTrue(record["present"])
        self.assertIsInstance(record["mtime_ns"], int)
        self.assertEqual(64, len(record["sha256"]))

    def test_rewriting_the_same_bytes_leaves_the_digest_alone(self) -> None:
        # The freshness rule below compares mtimes, not digests; the digest is
        # what tells an operator whether the re-expansion changed anything.
        path = self.plan_dir / plans_session_ids.session_id_to_expansion_filename("7")
        path.write_text("# expansion\n", encoding="utf-8")
        before = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")
        path.write_text("# expansion\n", encoding="utf-8")

        after = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertEqual(before["sha256"], after["sha256"])

    def test_a_directory_in_the_expansion_slot_reports_unavailable(self) -> None:
        (
            self.plan_dir / plans_session_ids.session_id_to_expansion_filename("7")
        ).mkdir()

        record = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertTrue(record["present"])
        self.assertTrue(record["unavailable"])
        self.assertNotIn("sha256", record)


class WorkspaceScopeRepairFreshnessTests(unittest.TestCase):
    def test_an_expansion_written_after_the_violation_is_fresh(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 100},
                {"present": True, "mtime_ns": 101},
            )
        )

    def test_an_expansion_that_did_not_move_is_not_fresh(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 100},
                {"present": True, "mtime_ns": 100},
            )
        )

    def test_an_expansion_that_did_not_exist_before_is_fresh(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": False}, {"present": True, "mtime_ns": 1}
            )
        )

    def test_an_expansion_that_vanished_is_not_fresh(self) -> None:
        # Fail closed: the repair is only credited when there is a readable
        # expansion to credit it to.
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 1}, {"present": False}
            )
        )

    def test_an_unreadable_expansion_is_not_fresh(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 1},
                {"present": True, "unavailable": "Is a directory"},
            )
        )

    def test_a_missing_mtime_on_either_side_is_not_fresh(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True}, {"present": True}
            )
        )


class WorkspaceScopeRepairReconciliationTests(unittest.TestCase):
    """Whether a retained violation still names something out of scope."""

    def _record(self, *aliases: str | None, **extra: object) -> dict[str, object]:
        return {
            "violations": [
                {
                    "kind": "outside-read-scope",
                    "alias": alias,
                    "repository": alias or "unregistered",
                    "qualified_paths": [],
                }
                for alias in aliases
            ],
            **extra,
        }

    def _scope(self, *aliases: str) -> plans_session_scope.SessionScope:
        return plans_session_scope.SessionScope(reads=aliases, writes=aliases)

    def test_a_violation_the_widened_scope_now_writes_is_reconciled(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"), self._scope("brainnotes", "scripts")
            )
        )

    def test_a_violation_the_scope_still_excludes_is_not_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"), self._scope("brainnotes")
            )
        )

    def test_one_unreconciled_violation_holds_the_whole_record(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts", "vendor"),
                self._scope("brainnotes", "scripts"),
            )
        )

    def test_a_read_only_alias_is_not_reconciled(self) -> None:
        # The violation is about writing, so widening reads alone answers
        # nothing.
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"),
                plans_session_scope.SessionScope(
                    reads=("brainnotes", "scripts"), writes=()
                ),
            )
        )

    def test_an_unregistered_repository_can_never_be_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record(None), self._scope("brainnotes", "scripts")
            )
        )

    def test_an_incomplete_observation_is_not_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts", unavailable={".": {"reason": "unreadable"}}),
                self._scope("scripts"),
            )
        )

    def test_a_record_with_nothing_to_judge_is_not_reconciled(self) -> None:
        for record in ({}, {"violations": []}, {"violations": "scripts"}):
            with self.subTest(record=record):
                self.assertTrue(
                    workspace_evidence._workspace_scope_repair_still_unauthorized(
                        record, self._scope("scripts")
                    )
                )

    def test_an_unreadable_scope_is_not_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"), None
            )
        )


if __name__ == "__main__":
    unittest.main()
