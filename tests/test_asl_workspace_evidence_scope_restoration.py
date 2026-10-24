"""Scope-repair restoration, requirement lookup and comparison tests.

Payload-only, portable tests; see `test_asl_workspace_evidence_boundary.py`.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    plans_session_scope,
    workspace_evidence,
)
from tests.asl_workspace_evidence_support import _workspace

ASL_TEST_PORTABLE = True


class WorkspaceScopeRestorationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = _workspace("Writer", "Reader", "Other")
        self.scope = plans_session_scope.SessionScope(
            reads=("Writer", "Reader", "Other"), writes=("Writer",)
        )
        self.current = {
            repo.repository.path: {
                "git": True,
                "head": "a" * 40,
                "refs": [["refs/heads/main", "a" * 40]],
                "diff_sha256": "empty",
                "tracked_changed": [],
                "tracked_patches": {},
                "index_diff_sha256": "empty",
                "index_changed": [],
                "index_patches": {},
                "untracked": {},
                "index_flagged": {},
                "worktree_ignore": {},
                "ignore_config": {},
            }
            for repo in self.workspace.repositories
        }
        self.record = {
            "read_aliases": list(self.scope.reads),
            "write_aliases": list(self.scope.writes),
            "excluded": [],
            "include_ignored": False,
            "registered_repositories": [
                {"alias": repo.alias, "path": repo.repository.path}
                for repo in self.workspace.repositories
            ],
            "snapshot_before": copy.deepcopy(self.current),
            "violations": [{"alias": "Reader", "repository": "reader"}],
        }

    def restored(self) -> bool:
        return workspace_evidence._workspace_scope_repair_restored(
            self.record, self.current, workspace=self.workspace, scope=self.scope
        )

    def test_complete_restoration_preserves_read_only_authority(self) -> None:
        self.assertTrue(self.restored())
        self.assertEqual(("Writer",), self.scope.writes)

    def test_authorized_work_can_remain_after_restoring_the_violation(self) -> None:
        self.current["writer"]["refs"] = [["refs/heads/main", "b" * 40]]
        self.assertTrue(self.restored())

    def test_unchanged_head_and_clean_files_do_not_excuse_a_retained_branch(
        self,
    ) -> None:
        self.current["reader"]["refs"] = [["refs/heads/unauthorized", "a" * 40]]
        self.assertFalse(self.restored())

    def test_a_new_violation_in_another_read_only_repository_blocks_recovery(
        self,
    ) -> None:
        self.current["other"]["refs"] = [["refs/heads/unauthorized", "a" * 40]]
        self.assertFalse(self.restored())

    def test_a_missing_repository_blocks_recovery(self) -> None:
        del self.current["reader"]
        self.assertFalse(self.restored())

    def test_unavailable_current_evidence_blocks_recovery(self) -> None:
        self.current["reader"]["unavailable"] = {"reason": "unreadable"}
        self.assertFalse(self.restored())

    def test_a_changed_registry_blocks_recovery(self) -> None:
        self.record["registered_repositories"] = []
        self.assertFalse(self.restored())

    def test_an_incomplete_or_malformed_retained_record_blocks_recovery(self) -> None:
        original = copy.deepcopy(self.record)
        for key, value in (
            ("snapshot_before", {}),
            ("snapshot_before", {"reader": None}),
            ("violations", []),
            ("violations", [{"alias": "Reader", "repository": []}]),
            ("violations", [{"alias": "Unknown", "repository": "reader"}]),
            ("unavailable", {".": {"reason": "unreadable"}}),
            ("read_aliases", ["Writer", "Reader"]),
            ("write_aliases", ["Writer", "Reader"]),
            ("excluded", ["hidden.py"]),
            ("include_ignored", True),
        ):
            with self.subTest(key=key, value=value):
                self.record = {**original, key: value}
                self.assertFalse(self.restored())


class LatestWorkspaceScopeRepairRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def _write(self, *records: dict[str, object]) -> None:
        self.event_path.write_text(
            "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
        )

    @staticmethod
    def _violation(session_id: str) -> dict[str, object]:
        return {
            "name": events.RUN_PAUSED_EVENT,
            "payload": {
                "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                "details": {
                    "workspace_write_guard": {
                        "session_id": session_id,
                        "violations": [{"alias": "OAK"}],
                    }
                },
            },
        }

    def test_a_missing_event_log_requires_nothing(self) -> None:
        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "7"
            )
        )

    def test_the_latest_uncleared_violation_is_returned(self) -> None:
        self._write(self._violation("7"))

        record = workspace_evidence._latest_workspace_scope_repair_requirement(
            self.event_path, "7"
        )

        self.assertEqual("7", (record or {})["session_id"])

    def test_a_repair_event_after_the_violation_clears_it(self) -> None:
        self._write(
            self._violation("7"),
            {
                "name": events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                "payload": {"details": {"session_id": "7"}},
            },
        )

        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "7"
            )
        )

    def test_another_sessions_repair_does_not_clear_this_one(self) -> None:
        # The scan walks backwards and stops at the first record about *this*
        # session, so a repair recorded for a sibling must not be read as one.
        self._write(
            self._violation("7"),
            {
                "name": events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                "payload": {"details": {"session_id": "8"}},
            },
        )

        record = workspace_evidence._latest_workspace_scope_repair_requirement(
            self.event_path, "7"
        )

        self.assertEqual("7", (record or {})["session_id"])

    def test_a_violation_paused_for_another_session_is_ignored(self) -> None:
        self._write(self._violation("8"))

        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "7"
            )
        )

    def test_a_truncated_line_is_skipped_rather_than_raised(self) -> None:
        self.event_path.write_text(
            f"{json.dumps(self._violation('7'))}\n{{not json\n", encoding="utf-8"
        )

        record = workspace_evidence._latest_workspace_scope_repair_requirement(
            self.event_path, "7"
        )

        self.assertEqual("7", (record or {})["session_id"])


class WorkspaceScopeRepairComparisonTests(unittest.TestCase):
    def test_registering_the_written_repository_is_not_an_addition(self) -> None:
        repaired, suppressed = workspace_evidence._workspace_scope_repair_comparison(
            {"added": ["oak"], "removed": []}, frozenset({"oak"})
        )

        self.assertEqual([], repaired["added"])
        self.assertEqual(["oak"], suppressed)

    def test_deregistering_a_repository_is_not_a_removal(self) -> None:
        repaired, suppressed = workspace_evidence._workspace_scope_repair_comparison(
            {"added": [], "removed": ["oak"]}, frozenset({"svc"})
        )

        self.assertEqual([], repaired["removed"])
        self.assertEqual(["oak"], suppressed)

    def test_topology_change_to_a_repository_still_registered_survives(self) -> None:
        # The suppression must never hide real topology change to a repository
        # inside the guard's domain.
        repaired, suppressed = workspace_evidence._workspace_scope_repair_comparison(
            {"added": ["vendor/other"], "removed": ["svc"]}, frozenset({"svc"})
        )

        self.assertEqual(["vendor/other"], repaired["added"])
        self.assertEqual(["svc"], repaired["removed"])
        self.assertEqual([], suppressed)

    def test_every_other_key_is_carried_through_unchanged(self) -> None:
        repaired, _ = workspace_evidence._workspace_scope_repair_comparison(
            {"changed": {"svc": {"qualified_paths": ["svc:a.py"]}}}, frozenset()
        )

        self.assertEqual(
            {"svc": {"qualified_paths": ["svc:a.py"]}}, repaired["changed"]
        )


if __name__ == "__main__":
    unittest.main()
