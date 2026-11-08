"""Exceptions cases for ProtectedPathGuardRunTests."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    repository_evidence,
    review_guard,
)

from .asl_run_protected_path_guard_support import ProtectedPathGuardRunTestsBase


class ProtectedPathGuardRunTests(ProtectedPathGuardRunTestsBase):
    def test_harness_exception_still_adjudicates_protected_paths(self) -> None:
        """A crashing turn still answers the operator's standing instruction.

        The exception cleanup is the last place the run's guards are still
        alive, so the verdict is reached there or never. The exception stays
        the outcome -- it is re-raised unchanged -- and the verdict rides
        beside it in the event stream.
        """
        run_id = "guard-protected-exception"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic harness crash")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            self.assertRaisesRegex(RuntimeError, "synthetic harness crash"),
        ):
            self._invoke(run_id)

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        rejection = rejections[0]
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, rejection["reason"]
        )
        self.assertFalse(rejection["reopened"])
        self.assertTrue(rejection["detail"]["adjudicated_only"])
        self.assertEqual(
            [".:notes.md"],
            sorted(rejection["detail"]["matched_paths"].values())[0],
        )
        self.assertEqual(
            rejection["detail"]["guard_ids"],
            list(rejection["detail"]["matched_paths"]),
        )
        self.assertEqual(
            "worker rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_legacy_review_exception_still_adjudicates_protected_paths(self) -> None:
        """A crashing legacy review is adjudicated like any other crashing turn.

        It captures no write-guard snapshot, so before this it entered the
        harness with no exception context at all and the crash path skipped the
        protected-path guard entirely -- the guard then expired with the
        operator's standing instruction never answered. Its turn-start review
        fingerprint is the comparison that answers it.
        """
        self._use_legacy_review_fixture()
        run_id = "guard-protected-legacy-review-exception"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("review rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic review crash")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            self.assertRaisesRegex(RuntimeError, "synthetic review crash"),
        ):
            self._invoke(run_id)

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        rejection = rejections[0]
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED, rejection["reason"]
        )
        self.assertTrue(rejection["detail"]["adjudicated_only"])
        self.assertFalse(rejection["reopened"])
        self.assertEqual(
            [".:notes.md"],
            sorted(rejection["detail"]["matched_paths"].values())[0],
        )
        self.assertEqual(
            rejection["detail"]["guard_ids"],
            list(rejection["detail"]["matched_paths"]),
        )
        # The review's edit is retained, exactly as it is for an implementation
        # turn: the guard decides what gets published, not what survives.
        self.assertEqual(
            "review rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_legacy_review_exception_without_evidence_records_unavailable(
        self,
    ) -> None:
        """Unreadable post-crash evidence is a verdict, not a silent clearance.

        The recapture is what names the changed paths here, so a recapture that
        cannot answer must leave the guard explicitly unavailable rather than
        clear -- the failure mode the finding calls out is a frozen path with
        neither a violation nor an unavailable verdict.
        """
        self._use_legacy_review_fixture()
        run_id = "guard-protected-legacy-review-blind"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("review rewrote this\n", encoding="utf-8")
            raise RuntimeError("synthetic review crash")

        real_capture = review_guard._capture_review_repo_fingerprint
        captures: list[object] = []

        def capture_then_fail(*args: object, **kwargs: object) -> Any:
            # Only the post-crash recapture fails; the turn-start capture has
            # to succeed or the turn never reaches the harness at all.
            captures.append(args)
            if len(captures) == 1:
                return real_capture(*args, **kwargs)
            raise repository_evidence.RepositoryEvidenceError("git is gone")

        with (
            mock.patch.object(harnesses, "run_harness", side_effect=edit_then_raise),
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                side_effect=capture_then_fail,
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic review crash"),
        ):
            self._invoke(run_id)

        self.assertEqual(2, len(captures))

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE,
            rejections[0]["reason"],
        )
        self.assertEqual(
            "review rewrote this\n", self.protected.read_text(encoding="utf-8")
        )

    def test_legacy_review_exception_convicts_a_committed_protected_path(
        self,
    ) -> None:
        """The crash-path recapture names what a review committed, too."""
        self._use_legacy_review_fixture()
        run_id = "guard-protected-legacy-review-committed"
        self._enqueue_guard(run_id, ".:notes.md")

        def edit_commit_then_raise(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("review rewrote this\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-qam", "review"], cwd=self.repo_root, check=True
            )
            raise RuntimeError("synthetic review crash")

        with (
            mock.patch.object(
                harnesses, "run_harness", side_effect=edit_commit_then_raise
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic review crash"),
        ):
            self._invoke(run_id)

        rejections = self._turn_rejections("protected-path-guard")
        self.assertEqual(1, len(rejections))
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            rejections[0]["reason"],
        )
        self.assertEqual(
            [".:notes.md"],
            sorted(rejections[0]["detail"]["matched_paths"].values())[0],
        )

    def test_a_workspace_commit_to_a_protected_path_rejects_the_turn(self) -> None:
        """A registered repository's commit is named under its alias."""
        self._use_workspace_fixture()
        run_id = "guard-protected-workspace-committed"
        self._enqueue_guard(run_id, "SVC:notes.md")

        def edit_and_commit(*_args: object, **_kwargs: object) -> Any:
            self.protected.write_text("worker rewrote this\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-qam", "worker"],
                cwd=self.repositories["SVC"],
                check=True,
            )
            return self._mark_complete()

        exit_code, payload = self._run_with(run_id, edit_and_commit)

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["protected_path_guard"]
        self.assertEqual(["SVC:notes.md"], sorted(guard["matched_paths"].values())[0])
