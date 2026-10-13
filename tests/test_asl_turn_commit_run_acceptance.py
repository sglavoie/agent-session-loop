"""Full runs that accept a turn: ordering, record agreement and commit scoping."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    plans_payloads,
    plans_snapshot,
    review_guard,
    run_commit,
)
from tests.asl_turn_commit_support import TurnCommitRunCase
from tests.support import (
    fake_commit_harness_env,
    read_jsonl,
)


class TurnCommitRunTests(TurnCommitRunCase):
    """Full runs, so the assertions are about the durable record, not calls."""

    def test_acceptance_follows_execution_and_every_guard(self) -> None:
        self._run_accepted()
        names = self._names()
        self.assertEqual(1, names.count(events.TURN_FINISHED_EVENT))
        accepted = names.index(events.TURN_FINISHED_EVENT)
        for earlier in (
            events.TURN_EXECUTION_FINISHED_EVENT,
            events.VERIFICATION_STARTED_EVENT,
            events.VERIFICATION_FINISHED_EVENT,
        ):
            self.assertLess(names.index(earlier), accepted, earlier)
        self.assertLess(accepted, names.index("run_finished"))
        self.assertEqual([], self._payloads(events.TURN_REJECTED_EVENT))

    def test_event_result_and_state_describe_the_same_committed_turn(self) -> None:
        payload = self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertTrue(finished["committed"])
        result_path = Path(str(finished["result_path"]))
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), finished)
        self.assertEqual([finished], payload["turns"])
        state_payload = json.loads(
            Path(str(payload["state_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(
            finished["turn_index"], state_payload["turns"][0]["turn_index"]
        )
        self.assertTrue(state_payload["turns"][0]["committed"])

    def test_committed_turn_reports_the_guarded_snapshot(self) -> None:
        self._run_accepted()
        finished = self._payloads(events.TURN_FINISHED_EVENT)[0]
        self.assertEqual(
            plans_payloads.snapshot_payload(plans_snapshot.inspect_plan(self.plan_dir)),
            finished["after"],
        )

    def test_authored_session_remains_uncommitted_under_null_mode(self) -> None:
        payload = self._run_accepted()

        self.assertIsNone(payload["commit_mode"])
        self.assertEqual([], payload["commits"])
        self.assertIn(
            "README.md",
            review_guard._run_git(
                self.repositories["SVC"], ["status", "--porcelain"]
            ).stdout,
        )

    def test_registered_repair_commits_only_changed_writable_aliases(self) -> None:
        self._mark_repair_session()
        (self.repositories["OAK"] / "preexisting.txt").write_text(
            "read-only dirty state\n", encoding="utf-8"
        )
        oak_head = review_guard._read_git_head(self.repositories["OAK"])["head"]
        commit_log = self.tmp_path / "commit-turns.jsonl"
        env = fake_commit_harness_env(self.tmp_path, log_path=commit_log)

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(
                harnesses,
                "run_harness",
                side_effect=self._harness(
                    writes="SVC:repair.txt", declares="SVC:repair.txt"
                ),
            ),
        ):
            exit_code, payload = self._invoke("registered-repair-commit")

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload["commit_mode"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertEqual("SVC", payload["commits"][0]["repository_alias"])
        # Only the writable alias ran a message-generation turn, and it ran
        # with that repository as its cwd.
        self.assertEqual(
            [str(self.repositories["SVC"].resolve())],
            [record["cwd"] for record in read_jsonl(commit_log)],
        )
        self.assertEqual(
            "",
            review_guard._run_git(
                self.repositories["SVC"], ["status", "--porcelain"]
            ).stdout.strip(),
        )
        self.assertEqual(
            oak_head,
            review_guard._read_git_head(self.repositories["OAK"])["head"],
        )
        self.assertTrue(
            review_guard._run_git(
                self.repositories["OAK"], ["status", "--porcelain"]
            ).stdout.strip()
        )

    def test_pathspec_commit_carries_only_the_paths_it_names(self) -> None:
        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("somebody else's edit\n", encoding="utf-8")
        env = fake_commit_harness_env(self.tmp_path)

        with mock.patch.dict(os.environ, env):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "pathspec-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=run_commit._CommitGeneration(harness="codex"),
            )

        self.assertEqual(0, commit["returncode"])
        self.assertEqual(["--pathspec", "declared.txt"], commit["argv"][-2:])
        self.assertEqual(
            ["declared.txt"],
            review_guard._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        # A pathspec commit only promises the paths it names, so the file the
        # turn never touched survives the commit as it was — which is also why
        # the probe reports `dirty-after-commit` rather than success.
        self.assertEqual(
            "?? unrelated.txt",
            review_guard._run_git(repo, ["status", "--porcelain"]).stdout.strip(),
        )
        self.assertEqual("dirty-after-commit", commit["status_result"])
        self.assertFalse(commit["success"])

    def test_pathspec_commit_leaves_a_preexisting_index_entry_staged(self) -> None:
        repo = self.repositories["SVC"]
        (repo / "declared.txt").write_text("session work\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("somebody else's edit\n", encoding="utf-8")
        # Somebody staged their own work before the run's commit boundary.
        review_guard._run_git(repo, ["add", "--", "unrelated.txt"])
        env = fake_commit_harness_env(self.tmp_path)

        with mock.patch.dict(os.environ, env):
            commit = run_commit._run_asl_commit(
                cwd=repo,
                run_dir=self.tmp_path / "prestaged-run",
                commit_index=1,
                session_id="1",
                repository_alias="SVC",
                qualified_paths=["SVC:declared.txt"],
                staging_paths=["SVC:declared.txt"],
                generation=run_commit._CommitGeneration(harness="codex"),
            )

        self.assertEqual(0, commit["returncode"])
        self.assertEqual(
            ["declared.txt"],
            review_guard._run_git(
                repo, ["show", "--name-only", "--format=", "HEAD"]
            ).stdout.split(),
        )
        # The pre-staged path was neither committed nor unstaged by the scoped
        # commit; it is still staged, now against the new HEAD.
        self.assertEqual(
            "A  unrelated.txt",
            review_guard._run_git(repo, ["status", "--porcelain"]).stdout.strip(),
        )


if __name__ == "__main__":
    unittest.main()
