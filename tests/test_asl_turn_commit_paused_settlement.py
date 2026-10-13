"""Which repositories a recoverable pause stages and commits, and what survives."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    review_guard,
    run_selection,
    runner,
)
from tests.asl_turn_commit_support import PausedRetainedWorkCase


class PausedRetainedWorkTests(PausedRetainedWorkCase):
    """Settlement of the work a recoverable pause retained."""

    def test_a_registered_settlement_stages_the_guarded_paths_only(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(["SVC"], [call["repository_key"] for call in self.launched])
        # The staging is the write guard's evidence and nothing else: the
        # settlement reuses the accepted path's resolver rather than sweeping
        # the repository the way an end-of-run commit would.
        self.assertEqual(["SVC:worked.txt"], self.launched[0]["staging_paths"])
        self.assertEqual(
            self.repositories["SVC"].resolve(), Path(self.launched[0]["cwd"])
        )
        self.assertEqual([], self._dirty(self.repositories["SVC"]))
        self.assertEqual(record, turn_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY])
        self.assertEqual(
            commit_groups.GROUP_SCOPE_SESSION_PAUSED, record["commit_group"]["scope"]
        )
        self.assertEqual({"repositories": []}, record["residue"])
        self.assertIsNone(record["error"])

    def test_the_legacy_settlement_covers_its_one_repository(self) -> None:
        self._write(self.legacy_root, "worked.txt")
        self._write(self.legacy_root, "also.txt")
        record, _turn_payload = self._settle(self.legacy, guard=None)

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertEqual(
            [commit_groups.LEGACY_REPOSITORY_KEY],
            [call["repository_key"] for call in self.launched],
        )
        # No pathspec means `asl commit` stays on its `git add -A` default, so
        # the file the guard never named is committed too.
        self.assertEqual([], self.launched[0]["staging_paths"])
        self.assertEqual([], self._dirty(self.legacy_root))

    def test_the_at_end_settlement_covers_the_whole_workspace(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=run_selection.COMMIT_MODE_END,
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        # The guard authorized SVC alone; the at-end sweep commits every dirty
        # repository regardless, because that is what the mode already claims.
        self.assertEqual(
            ["SVC", "OAK"], [call["repository_key"] for call in self.launched]
        )
        self.assertEqual([[], []], [call["staging_paths"] for call in self.launched])
        self.assertEqual([], self._dirty(self.repositories["OAK"]))

    def test_nothing_to_commit_records_no_changes_without_a_commit(self) -> None:
        record, _turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        self.assertEqual("no-changes", record["result"])
        self.assertEqual([], self.launched)
        self.assertIsNone(record["commit_group"])
        self.assertIsNone(record["commit"])
        self.assertIsNone(record["error"])
        # No group was created and no message was rendered for a commit that
        # never had a reason to run.
        self.assertNotIn("commit_groups", self.payload)
        self.assertFalse(self._message_file().exists())
        self.assertEqual([], self.soft_failures)

    def test_dirt_that_survives_the_commit_is_named_in_the_record(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["SVC"], "stray.txt")
        record, _turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        self.assertEqual("committed-with-residue", record["result"])
        self.assertEqual(
            {
                "repositories": [
                    {
                        "alias": "SVC",
                        "clean": False,
                        "dirty_paths": ["SVC:stray.txt"],
                        "error": None,
                    }
                ]
            },
            record["residue"],
        )
        # The commit is real; what it left behind is what the relaunch's
        # startup gate will still refuse, which is why it is reported.
        self.assertIsNone(record["error"])
        self.assertEqual(["stray.txt"], self._dirty(self.repositories["SVC"]))

    def test_a_failed_settlement_soft_fails_and_leaves_the_group_owed(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit=self._refused_commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual(1, record["commit"]["returncode"])
        self.assertEqual(
            commit_groups.GROUP_STATUS_FAILED, record["commit_group"]["status"]
        )
        # The durable row is what a later run recovers, and the run's own
        # end-of-run report names it as a loss.
        self.assertEqual(
            [commit_groups.GROUP_STATUS_FAILED],
            [group["status"] for group in self.payload["commit_groups"]],
        )
        self.assertEqual(
            [commit_groups.GROUP_SCOPE_SESSION_PAUSED],
            [
                summary["scope"]
                for summary in commit_settlement._unresolved_commit_groups(self.payload)
            ],
        )
        self.assertEqual(1, len(self.soft_failures))
        failure = self.soft_failures[0]
        self.assertEqual(commit_groups.GROUP_SCOPE_SESSION_PAUSED, failure["scope"])
        self.assertEqual("1", failure["session_id"])
        self.assertEqual(record["commit_group"], failure["group"])
        self.assertEqual(record["commit"], failure["commit"])
        # A commit that exited nonzero moved no HEAD, so nothing reached the
        # history and the sentence says exactly that. The landed-commit wording
        # is the other branch's, and it must not leak into this one.
        self.assertEqual(
            "the work retained by the verify-failed pause of session 1 "
            "could not be committed",
            failure["message"],
        )

    def test_a_refused_commit_names_the_retained_residue(self) -> None:
        # A commit that exits nonzero took nothing, so everything the turn
        # retained is still in the tree -- and those are exactly the paths the
        # relaunch's startup gate refuses over. Until the probe became
        # unconditional the record reported an empty residue here, which reads
        # as a clean tree the run never confirmed.
        self._write(self.repositories["SVC"], "worked.txt")
        head_before = self._head(self.repositories["SVC"])
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit=self._refused_commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual("nonzero-exit", record["commit"]["status_result"])
        # The ordinary refusal branch: HEAD never moved, so this is not the
        # commit-landed-on-a-dirty-tree case that was already probed.
        self.assertEqual(head_before, self._head(self.repositories["SVC"]))
        self.assertEqual(
            [
                {
                    "alias": "SVC",
                    "clean": False,
                    "dirty_paths": ["SVC:worked.txt"],
                    "error": None,
                }
            ],
            record["residue"]["repositories"],
        )
        self.assertEqual(["worked.txt"], self._dirty(self.repositories["SVC"]))

    def test_a_partial_failure_names_only_the_repository_left_dirty(self) -> None:
        # The group's verdict is one word for the whole set, and on a partial
        # failure that word is wrong about the repositories that committed.
        # The residue is per target, so it separates them.
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")

        def commit(**kwargs: Any) -> dict[str, Any]:
            if kwargs["repository_key"] == "OAK":
                return self._refused_commit(**kwargs)
            return self._fake_commit(**kwargs)

        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=run_selection.COMMIT_MODE_END,
            commit=commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual("OAK", record["commit_group"]["failed_alias"])
        self.assertEqual(
            [
                {
                    "alias": "OAK",
                    "clean": False,
                    "dirty_paths": ["OAK:elsewhere.txt"],
                    "error": None,
                }
            ],
            record["residue"]["repositories"],
        )
        # SVC committed before OAK refused, and the record does not accuse it
        # of residue it does not have.
        self.assertEqual([], self._dirty(self.repositories["SVC"]))
        self.assertEqual(["elsewhere.txt"], self._dirty(self.repositories["OAK"]))

    def test_a_partial_failure_message_reports_both_committed_and_retained_work(
        self,
    ) -> None:
        # The failing commit landed nothing, but the group had already
        # committed SVC before OAK refused. The all-or-nothing sentence sent
        # the operator looking for work that is already in `git log`, so the
        # message now names what committed and what did not.
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")

        def commit(**kwargs: Any) -> dict[str, Any]:
            if kwargs["repository_key"] == "OAK":
                return self._refused_commit(**kwargs)
            return self._fake_commit(**kwargs)

        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=run_selection.COMMIT_MODE_END,
            commit=commit,
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        # The commit the message reads landed nothing; the correction comes
        # from the group, not from a second reading of the commit.
        self.assertEqual("nonzero-exit", record["commit"]["status_result"])
        committed = record["commit_group"]["successful_repositories"]
        self.assertEqual(["SVC"], [row["alias"] for row in committed])
        self.assertEqual(1, len(self.soft_failures))
        self.assertEqual(
            "the verify-failed pause of session 1 committed the work it "
            "retained in SVC, but the work it retained in OAK could not be "
            "committed; the worktree still holds OAK:elsewhere.txt",
            self.soft_failures[0]["message"],
        )

    def test_a_partial_failure_message_survives_an_unreadable_residue_probe(
        self,
    ) -> None:
        # With no residue path to name, the sentence still must not deny SVC's
        # commit -- the correction lives on the group, and the probe's silence
        # only drops the trailing clause.
        self._write(self.repositories["SVC"], "worked.txt")
        self._write(self.repositories["OAK"], "elsewhere.txt")
        real_run_git = review_guard._run_git

        def commit(**kwargs: Any) -> dict[str, Any]:
            if kwargs["repository_key"] == "OAK":
                return self._refused_commit(**kwargs)
            return self._fake_commit(**kwargs)

        def blinded(cwd: Path, args: list[str]) -> Any:
            if len(self.launched) == 2 and args[:1] == ["status"]:
                raise runner.RunError("could not inspect git status: fatal: unreadable")
            return real_run_git(cwd, args)

        with mock.patch.object(review_guard, "_run_git", side_effect=blinded):
            record, _turn_payload = self._settle(
                self.registered,
                guard=self._guard(added_paths=("worked.txt",)),
                commit_mode=run_selection.COMMIT_MODE_END,
                commit=commit,
            )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual(
            [None, None],
            [row["clean"] for row in record["residue"]["repositories"]],
        )
        self.assertEqual(
            "the verify-failed pause of session 1 committed the work it "
            "retained in SVC, but the work it retained in OAK could not be "
            "committed",
            self.soft_failures[0]["message"],
        )

    def test_a_target_whose_status_is_unreadable_records_undetermined(self) -> None:
        # The commit may have failed *because* the repository became
        # unreadable, and the settlement is on its way to a pause it must not
        # replace with a crash. So the probe answers `clean: None` with the
        # failure text rather than raising or reading the tree as clean.
        self._write(self.repositories["SVC"], "worked.txt")
        real_run_git = review_guard._run_git

        def blinded(cwd: Path, args: list[str]) -> Any:
            # Only after the commit ran: the target resolution and the group's
            # own skip-clean check need real answers to reach the commit at all.
            if self.launched and args[:1] == ["status"]:
                raise runner.RunError("could not inspect git status: fatal: unreadable")
            return real_run_git(cwd, args)

        with mock.patch.object(review_guard, "_run_git", side_effect=blinded):
            record, _turn_payload = self._settle(
                self.registered,
                guard=self._guard(added_paths=("worked.txt",)),
                commit=self._refused_commit,
            )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertEqual(
            [
                {
                    "alias": "SVC",
                    "clean": None,
                    "dirty_paths": [],
                    "error": "could not inspect git status: fatal: unreadable",
                }
            ],
            record["residue"]["repositories"],
        )
        # And the settlement still recorded, published and soft-failed.
        self.assertEqual(1, len(self._events()))
        self.assertEqual(1, len(self.soft_failures))


if __name__ == "__main__":
    unittest.main()
