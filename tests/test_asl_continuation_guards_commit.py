"""Commit settlement owns the retained work of every session attempt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_message,
    commit_settlement,
    continuation_guards,
    review_guard,
    run_commit,
    run_persistence,
    run_selection,
    verification_models,
)

from .support import init_git_repo, init_multi_repo_workspace, write_multi_repo_plan
from .support_asl_continuation_guards import (
    RUN_ID,
    SESSION_ID,
    SESSION_LABEL,
    SPECS,
    legacy_guard,
    registered_guard,
)

# ---------------------------------------------------------------------------
# Settlement over real repositories
# ---------------------------------------------------------------------------


class ContinuationCommitScopeTests(unittest.TestCase):
    """What a recoverable pause commits after the loop re-attempted a session.

    Driven straight at the settlement with a fake ``asl commit`` that performs
    the real ``git add``/``git commit`` the staging asks for, so the claim that
    the earlier attempt's file was committed is answered by the tree.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, SPECS)
        self.plan_dir = self.tmp_path / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.registered = verification_models.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        self.legacy_root = self.tmp_path / "legacy"
        init_git_repo(self.legacy_root)
        self.legacy = verification_models.VerificationContext.legacy(self.legacy_root)
        self.run_dir = self.tmp_path / "run"
        self.event_path = self.tmp_path / "events.jsonl"
        self.artifact_dir = self.tmp_path / "turn-0002"
        self.payload: dict[str, Any] = {"commits": []}
        self.launched: list[dict[str, Any]] = []
        self.soft_failures: list[dict[str, Any]] = []

    # -- fixtures ---------------------------------------------------------

    def write(self, root: Path, relative: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n", encoding="utf-8")

    def accumulated(
        self, first: dict[str, tuple[str, ...]], second: dict[str, tuple[str, ...]]
    ) -> dict[str, Any]:
        """The record the guard sequence leaves after two cleared attempts."""
        ledger = continuation_guards.SessionAttempts()
        ledger.record(
            session_id=SESSION_ID,
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes=first),
        )
        current = registered_guard(changes=second)
        merged = continuation_guards.merge_session_write_evidence(
            ledger.latest(SESSION_ID), current
        )
        return current if merged is None else merged

    def fake_commit(self, **kwargs: Any) -> dict[str, Any]:
        self.launched.append(dict(kwargs))
        root = Path(str(kwargs["cwd"]))
        staging = list(kwargs["staging_paths"])
        if staging:
            review_guard._run_git(
                root,
                ["add", "--", *commit_message.dequalify_paths(staging)],
            )
        else:
            review_guard._run_git(root, ["add", "-A"])
        message_file = kwargs["generation"].message_file
        assert message_file is not None
        review_guard._run_git(root, ["commit", "--no-verify", "-F", str(message_file)])
        return {
            "success": True,
            "commit_sha": review_guard._run_git(
                root, ["rev-parse", "HEAD"]
            ).stdout.strip(),
            "commit_index": kwargs["commit_index"],
            "repository_key": kwargs["repository_key"],
        }

    def settle(
        self,
        context: verification_models.VerificationContext,
        *,
        guard: dict[str, Any] | None,
        commit_mode: str | None = run_selection.COMMIT_MODE_PER_SESSION,
        pause_reason: str = "verify-failed",
        is_review_session: bool = False,
        is_repair_session: bool = False,
    ) -> dict[str, Any] | None:
        turn_payload: dict[str, Any] = {"turn_index": 2, "session": SESSION_ID}
        if guard is not None:
            turn_payload["workspace_write_guard"] = guard
        retained = commit_settlement.RetainedWorkContext(
            session_id=SESSION_ID,
            session_label=SESSION_LABEL,
            turn_payload=turn_payload,
            result_path=self.artifact_dir / "result.json",
            artifact_dir=self.artifact_dir,
            is_review_session=is_review_session,
            is_repair_session=is_repair_session,
        )
        with mock.patch.object(
            run_commit, "_run_asl_commit", side_effect=self.fake_commit
        ):
            return commit_settlement._settle_paused_retained_work(
                event_path=self.event_path,
                payload=self.payload,
                progress=mock.Mock(),
                verification_context=context,
                run_dir=self.run_dir,
                run_id=RUN_ID,
                retained=retained,
                pause_reason=pause_reason,
                pause_details={
                    "verification_repair_attempt": 1,
                    "verification_repair_max_attempts": 2,
                    "verification_repair_exhausted": False,
                },
                commit_mode=commit_mode,
                generation=run_commit._CommitGeneration(),
                soft_fail=lambda **kwargs: self.soft_failures.append(dict(kwargs)),
                persistence=run_persistence._RunPersistence(),
            )

    def dirty(self, root: Path) -> list[str]:
        return review_guard._porcelain_dirty_paths(
            review_guard._run_git(
                root, ["status", "--porcelain", "-z", "--untracked-files=all"]
            ).stdout
        )

    def staged_by_repository(self) -> dict[str, list[str]]:
        return {
            str(call["repository_key"]): list(call["staging_paths"])
            for call in self.launched
        }

    # -- the four fixtures ------------------------------------------------

    def test_an_implementation_session_commits_both_of_its_attempts(self) -> None:
        self.write(self.repositories["SVC"], "first.txt")
        self.write(self.repositories["SVC"], "second.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"SVC": ("second.txt",)}),
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            {"SVC": ["SVC:first.txt", "SVC:second.txt"]}, self.staged_by_repository()
        )
        # The whole point: nothing the first attempt wrote is left behind.
        self.assertEqual([], self.dirty(self.repositories["SVC"]))

    def test_a_multi_repository_session_commits_every_repository_it_touched(
        self,
    ) -> None:
        self.write(self.repositories["SVC"], "first.txt")
        self.write(self.repositories["OAK"], "second.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"OAK": ("second.txt",)}),
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            {"SVC": ["SVC:first.txt"], "OAK": ["OAK:second.txt"]},
            self.staged_by_repository(),
        )
        self.assertEqual([], self.dirty(self.repositories["SVC"]))
        self.assertEqual([], self.dirty(self.repositories["OAK"]))
        self.assertEqual({"repositories": []}, record["residue"])

    def test_a_repair_child_owns_its_commit_without_a_mode_of_its_own(self) -> None:
        self.write(self.repositories["SVC"], "first.txt")
        self.write(self.repositories["SVC"], "second.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"SVC": ("second.txt",)}),
            commit_mode=None,
            is_repair_session=True,
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            {"SVC": ["SVC:first.txt", "SVC:second.txt"]}, self.staged_by_repository()
        )
        self.assertEqual([], self.dirty(self.repositories["SVC"]))

    def test_a_review_session_still_commits_nothing_however_many_attempts(
        self,
    ) -> None:
        self.write(self.repositories["SVC"], "first.txt")

        record = self.settle(
            self.registered,
            guard=self.accumulated({"SVC": ("first.txt",)}, {"OAK": ("second.txt",)}),
            is_review_session=True,
        )

        # Accumulating evidence changes what a commit would stage, never
        # whether one is owed. A review session is never auto-committed.
        self.assertIsNone(record)
        self.assertEqual([], self.launched)
        self.assertEqual(["first.txt"], self.dirty(self.repositories["SVC"]))

    def test_the_legacy_lane_keeps_sweeping_its_one_repository(self) -> None:
        self.write(self.legacy_root, "first.txt")
        self.write(self.legacy_root, "second.txt")

        record = self.settle(self.legacy, guard=legacy_guard("first.txt"))

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual([], self.dirty(self.legacy_root))


if __name__ == "__main__":
    unittest.main()
