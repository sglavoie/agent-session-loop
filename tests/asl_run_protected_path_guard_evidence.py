"""Evidence cases for ProtectedPathGuardRunTests."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    repository_evidence,
)

from .asl_run_protected_path_guard_support import ProtectedPathGuardRunTestsBase


class ProtectedPathGuardRunTests(ProtectedPathGuardRunTestsBase):
    def test_a_changed_protected_path_rejects_the_turn_and_keeps_the_change(
        self,
    ) -> None:
        run_id = "guard-protected-changed"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_protected(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_protected)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual([".:notes.md"], sorted(guard["matched_paths"].values())[0])
        self.assertEqual(guard["guard_ids"], list(guard["matched_paths"]))
        self.assertTrue(guard["evidence"])
        self.assertIn("command-gate", guard["coverage"][guard["guard_ids"][0]])
        # The completion the turn claimed is withdrawn...
        self.assertTrue(guard["reverted_to_pending"])
        # ...but the worker's edit is left exactly where the worker put it. A
        # guard decides what gets published, not what gets destroyed.
        self.assertTrue(guard["retained"])
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_a_committed_change_to_a_protected_path_rejects_the_turn(self) -> None:
        """Committing the edit does not hide it from the guard.

        A commit leaves the status lanes clean and moves only HEAD, so a guard
        that read the status lanes alone cleared a frozen path the worker had
        rewritten and committed.
        """
        run_id = "guard-protected-committed"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_and_commit(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-qam", "worker"], cwd=self.repo_root, check=True
            )
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_and_commit)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual([".:notes.md"], sorted(guard["matched_paths"].values())[0])

    def test_a_committed_change_without_head_paths_is_unavailable(self) -> None:
        """A moved tip whose paths could not be listed clears nothing."""
        run_id = "guard-protected-committed-blind"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_and_commit(*_args: object, **_kwargs: object) -> Any:
            (self.repo_root / "README.md").write_text("worker\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-qam", "worker"], cwd=self.repo_root, check=True
            )
            return self._mark_complete()

        with mock.patch.object(
            repository_evidence,
            "head_transition_paths",
            side_effect=repository_evidence.RepositoryEvidenceError("boom"),
        ):
            exit_code, payload = self._run_with(run_id, edit_and_commit)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE,
            payload["pause_reason"],
        )

    def test_a_protected_path_restored_before_the_end_passes(self) -> None:
        run_id = "guard-protected-restored"
        self._enqueue_guard(run_id, ".:notes.md")
        original = self.protected.read_text(encoding="utf-8")

        def edit_then_restore(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("scratch\n", encoding="utf-8")
            self.protected.write_text(original, encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_then_restore)

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload.get("pause_reason"))
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertTrue(guard["passed"])
        self.assertNotIn(".:notes.md", guard["observed_paths"])

    def test_a_dirty_protected_path_at_turn_start_is_the_accepted_baseline(
        self,
    ) -> None:
        run_id = "guard-protected-dirty-baseline"
        self._enqueue_guard(run_id, ".:notes.md")
        # The operator's own uncommitted edit, made before the run. The guard
        # freezes the file as the turn found it, not as HEAD has it.
        self.protected.write_text("operator edit\n", encoding="utf-8")

        def touch_something_else(*_args: object, **_kwargs: object) -> Any:
            (self.repo_root / "README.md").write_text("worker\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, touch_something_else)

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["turns"][0]["protected_path_guard"]["passed"])
        self.assertEqual("operator edit\n", self.protected.read_text(encoding="utf-8"))

    def test_a_write_outside_the_pattern_does_not_trip_the_guard(self) -> None:
        run_id = "guard-protected-unmatched"
        self._enqueue_guard(run_id, ".:docs/**/*.md")

        def edit_protected(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("not under docs/\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_protected)

        self.assertEqual(0, exit_code)
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertTrue(guard["passed"])
        self.assertIn(".:notes.md", guard["observed_paths"])

    def test_scope_violation_still_adjudicates_protected_paths(self) -> None:
        """Two breaches in one turn are both reported, not just the first.

        The write guard pauses first, so its reason is the one the operator
        acts on. But a turn that also moved a frozen path has to say so: an
        operator who repairs only the scope and reruns would otherwise never
        learn the guard was breached at all.
        """
        self._use_workspace_fixture()
        run_id = "guard-protected-with-scope-violation"
        self._enqueue_guard(run_id, "SVC:notes.md")
        out_of_scope = self.repositories["OAK"] / "README.md"

        def breach_both(*_args: object, **_kwargs: object) -> Any:
            out_of_scope.write_text("outside the write scope\n", encoding="utf-8")
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, breach_both)

        self.assertEqual(1, exit_code)
        # The primary pause is untouched: same reason, same remediation.
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            payload["pause_reason"],
        )
        self.assertEqual(
            ["OAK:README.md"],
            payload["turns"][0]["workspace_write_guard"]["violations"][0][
                "qualified_paths"
            ],
        )

        guard = payload["turns"][0]["protected_path_guard"]
        self.assertFalse(guard["passed"])
        self.assertTrue(guard["adjudicated_only"])
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, guard["reason"]
        )
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            guard["deferred_to_pause_reason"],
        )
        self.assertEqual(guard["guard_ids"], list(guard["matched_paths"]))
        self.assertEqual(["SVC:notes.md"], sorted(guard["matched_paths"].values())[0])
        # The matched repository's before/after vectors survive the
        # composition, which is what "restore or accept" needs.
        self.assertTrue(guard["evidence"])
        self.assertTrue(guard["retained"])
        # The write guard already withdrew the session; this guard does not
        # withdraw it a second time.
        self.assertFalse(guard["reverted_to_pending"])
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

        # The pause an operator reads carries both halves.
        self.assertEqual(guard, payload["pause_details"]["protected_path_guard"])
        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        self.assertFalse(rejections[0]["reopened"])
        self.assertEqual(guard["guard_ids"], rejections[0]["detail"]["guard_ids"])
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )
