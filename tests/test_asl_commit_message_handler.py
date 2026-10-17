"""Tests for test asl commit message handler."""

from __future__ import annotations

import json
import unittest

from agent_session_loop import commit_message
from tests import support_asl_commit_message as fixtures
from tests.support import (
    git_stdout,
)


class CommitHandlerTests(fixtures.CommitHandlerCase):
    def test_default_mode_stages_generates_and_commits(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(0, code, err)
        self.assertEqual(before + 1, self.commit_count())
        self.assertIn("feat: add the fake codex commit branch", out)
        # Stdout is the committed text and nothing else, so `asl commit` can be
        # piped; the outcome line goes to stderr.
        self.assertEqual(self.head_message(), out.strip())
        self.assertIn("asl commit: committed", err)
        # The message was formatted, not committed as the harness wrote it.
        message = self.head_message().splitlines()
        self.assertEqual("feat: add the fake codex commit branch", message[0])
        self.assertEqual("", message[1])
        self.assertTrue(all(len(line) <= 80 for line in message[2:]), message)
        self.assertEqual("", self.porcelain())

    def test_generation_runs_under_the_audit_read_only_profile(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(0, code, err)
        argv = self.recorded_turn()["argv"]
        self.assertIn("-s", argv)
        self.assertEqual("read-only", argv[argv.index("-s") + 1])
        self.assertIn("--skip-git-repo-check", argv)
        # `--add-dir` is the worker's writable grant and must never appear.
        self.assertNotIn("--add-dir", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_the_prompt_inlines_the_staged_diff(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(0, code, err)
        prompt = self.recorded_turn()["prompt"]
        self.assertIn("diff --git a/src/alpha.py b/src/alpha.py", prompt)
        self.assertIn("+print('alpha')", prompt)
        self.assertTrue(prompt.endswith("Generate the commit message:"))

    def test_guidance_model_and_effort_reach_the_harness(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--guidance",
            "mention the alpha module",
            "--model",
            "gpt-5",
            "--effort",
            "high",
        )

        self.assertEqual(0, code, err)
        turn = self.recorded_turn()
        self.assertIn("mention the alpha module", turn["prompt"])
        argv = turn["argv"]
        self.assertEqual("gpt-5", argv[argv.index("--model") + 1])
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_dry_run_prints_the_message_without_committing(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(str(self.repo), "--yes", "--dry-run")

        self.assertEqual(0, code, err)
        self.assertEqual(before, self.commit_count())
        self.assertIn("feat: add the fake codex commit branch", out)
        # Staging still happened; only the commit was withheld.
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_staged_mode_refuses_an_empty_index(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--yes", "--staged")

        self.assertEqual(1, code)
        self.assertIn("no staged changes", err)
        # An unstaged worktree change is left exactly as it was.
        self.assertIn("?? src/alpha.py", self.porcelain())

    def test_staged_mode_commits_only_what_was_already_staged(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/alpha.py"])

        code, _out, err = self.run_commit(str(self.repo), "--yes", "--staged")

        self.assertEqual(0, code, err)
        self.assertIn("src/alpha.py", self.recorded_turn()["prompt"])
        self.assertNotIn("src/beta.py", self.recorded_turn()["prompt"])
        self.assertIn("?? src/beta.py", self.porcelain())

    def test_pathspec_stages_only_the_named_paths(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--pathspec", "src/alpha.py"
        )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        self.assertIn("?? src/beta.py", self.porcelain())

    def test_pathspec_excludes_preexisting_staged_paths(self) -> None:
        # The index is not empty when the scoped commit starts: `src/beta.py`
        # was staged by whoever ran `asl commit --pathspec`, and the scoped
        # contract says it must neither be committed nor unstaged.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--pathspec", "src/alpha.py"
        )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        # The pre-staged path stayed in the index, still staged against the new
        # HEAD, and never reached the generation prompt.
        self.assertIn("A  src/beta.py", self.porcelain())
        self.assertNotIn("src/alpha.py", self.porcelain())
        self.assertNotIn("src/beta.py", self.recorded_turn()["prompt"])

    def test_pathspec_does_not_commit_post_capture_edits(self) -> None:
        # An edit landing during the generation turn leaves the worktree
        # describing something the message was never written from, so the run
        # refuses rather than committing the captured blob behind the
        # operator's back.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--pathspec",
            "src/alpha.py",
            env={
                "FAKE_CODEX_COMMIT_EDIT_PATH": "src/alpha.py",
                "FAKE_CODEX_COMMIT_EDIT_TEXT": "print('smuggled')\n",
            },
        )

        self.assertEqual(1, code)
        self.assertIn("changed after the diff was captured", err)
        self.assertIn("src/alpha.py", err)
        self.assertEqual(before, self.commit_count())
        # The generation prompt only ever saw the captured content, and the
        # index still holds exactly that.
        self.assertNotIn("smuggled", self.recorded_turn()["prompt"])
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", ":src/alpha.py")
        )
        # The unrelated pre-existing index entry is neither committed nor
        # discarded by the refusal.
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_pathspec_reports_drift_as_its_own_reason(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            "--pathspec",
            "src/alpha.py",
            env={"FAKE_CODEX_COMMIT_EDIT_PATH": "src/alpha.py"},
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_SNAPSHOT_DRIFTED, payload["reason"])
        self.assertEqual(["src/alpha.py"], payload["details"]["paths"])


if __name__ == "__main__":
    unittest.main()
