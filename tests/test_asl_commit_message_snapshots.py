"""Tests for test asl commit message snapshots."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from agent_session_loop import commit_message
from tests import support_asl_commit_message as fixtures
from tests.support import (
    git_stdout,
)


class CommitHandlerTests(fixtures.CommitHandlerCase):
    def test_two_step_pathspec_does_not_commit_post_capture_edits(self) -> None:
        # The gitsummary skill prepares, writes the message from its own
        # context, and then commits through a second invocation; the drift
        # window spans the two calls.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--pathspec", "src/alpha.py"
        )
        self.assertEqual(0, code, err)
        snapshot = json.loads(out.partition("\n\n")[0])["snapshot"]
        self.assertTrue(snapshot)

        self.write("src/alpha.py", "print('smuggled')\n")
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--pathspec",
            "src/alpha.py",
            "--expect-snapshot",
            snapshot,
            "--message-file",
            "-",
            stdin="feat: add the alpha module\n\nIt prints a greeting.\n",
        )

        self.assertEqual(1, code)
        self.assertIn("changed after the diff was captured", err)
        self.assertEqual(before, self.commit_count())
        # Refusing came before staging, so the index still holds the prepared
        # snapshot rather than the drifted content.
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", ":src/alpha.py")
        )
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_two_step_pathspec_commits_the_captured_snapshot(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--pathspec", "src/alpha.py"
        )
        self.assertEqual(0, code, err)
        snapshot = json.loads(out.partition("\n\n")[0])["snapshot"]

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--pathspec",
            "src/alpha.py",
            "--expect-snapshot",
            snapshot,
            "--message-file",
            "-",
            stdin="feat: add the alpha module\n\nIt prints a greeting.\n",
        )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", "HEAD:src/alpha.py")
        )
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_pathspec_commit_outruns_an_edit_landing_after_validation(self) -> None:
        # Every other drift test mutates the path early enough for a later
        # check to see it.  This one lands the edit in the only window no
        # check can cover: after the final validation returns and before git
        # is asked to commit.  Nothing can re-check it, so the commit has to
        # be built from content fixed before that window opens.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/beta.py"])
        validated = commit_message.assert_pathspec_snapshot

        def smuggle(*args: object, **kwargs: object) -> None:
            validated(*args, **kwargs)  # type: ignore[arg-type]
            self.write("src/alpha.py", "print('smuggled')\n")

        with mock.patch.object(
            commit_message, "assert_pathspec_snapshot", side_effect=smuggle
        ):
            code, _out, err = self.run_commit(
                str(self.repo), "--yes", "--pathspec", "src/alpha.py"
            )

        self.assertEqual(0, code, err)
        committed = git_stdout(self.repo, "show", "--name-only", "--format=", "HEAD")
        self.assertEqual(["src/alpha.py"], committed.splitlines())
        # The commit carries the captured snapshot, not what the worktree held
        # a moment later, and the message was never written from the latter.
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", "HEAD:src/alpha.py")
        )
        self.assertNotIn("smuggled", self.recorded_turn()["prompt"])
        # The late edit is not swallowed either: it stays on disk as an
        # uncommitted modification for whoever made it.
        self.assertEqual(
            "print('smuggled')",
            (self.repo / "src/alpha.py").read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            "print('alpha')", git_stdout(self.repo, "show", ":src/alpha.py")
        )
        # The unrelated pre-existing index entry survives the scoped commit.
        self.assertIn("A  src/beta.py", self.porcelain())

    def test_scoped_commit_carries_a_staged_deletion_as_a_deletion(self) -> None:
        # Seeding the scoped index from HEAD reinstates every path HEAD
        # carries, including one staged for deletion — no `--index-info` entry
        # exists to overwrite it — so it needs removing again or it would
        # quietly survive the commit meant to delete it.  Driven below the
        # CLI. Re-staging the deleted path must also succeed: it is still in
        # the commit scope even though it has already left the index.
        self.write("src/alpha.py", "print('alpha')\n")
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_paths(self.repo, ["src/alpha.py", "src/beta.py"])
        git_stdout(self.repo, "commit", "-m", "seed", "--no-verify")

        (self.repo / "src/alpha.py").unlink()
        self.write("src/beta.py", "print('beta edited')\n")
        commit_message.stage_paths(self.repo, ["src/alpha.py", "src/beta.py"])

        staged = commit_message.stage_paths(self.repo, ["src/alpha.py"])
        self.assertEqual(0, staged.returncode, staged.stderr)
        committed = commit_message.commit_staged(
            self.repo, "chore: drop alpha\n", pathspec=["src/alpha.py"]
        )

        self.assertEqual(0, committed.returncode, committed.stderr)
        self.assertEqual(
            ["src/alpha.py"],
            git_stdout(
                self.repo, "show", "--name-only", "--format=", "HEAD"
            ).splitlines(),
        )
        self.assertNotIn(
            "src/alpha.py",
            git_stdout(self.repo, "ls-tree", "-r", "--name-only", "HEAD"),
        )
        # The unrelated staged edit is neither committed nor unstaged.
        self.assertIn("M  src/beta.py", self.porcelain())

    def test_expect_snapshot_without_pathspec_is_refused(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--expect-snapshot", "deadbeefdeadbeef"
        )

        self.assertEqual(1, code)
        self.assertIn("--expect-snapshot", err)
        self.assertIn("?? src/alpha.py", self.porcelain())


if __name__ == "__main__":
    unittest.main()
