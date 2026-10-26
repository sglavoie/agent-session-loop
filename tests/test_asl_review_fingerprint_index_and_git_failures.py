"""Fingerprint index flags and fail-closed git probe behaviour."""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    git_subprocess,
    repository_scope_registry,
    review_guard,
    review_guard_git,
    runner,
    workspaces,
)
from tests.review_fingerprint_base import ReviewFingerprintBase
from tests.support import (
    patched_env,
)

ASL_TEST_LANE = "integration"


class ReviewFingerprintIndexAndGitFailureTests(ReviewFingerprintBase):
    """Fingerprint index flags and fail-closed git probe behaviour."""

    def test_index_flagged_file_mutation_trips_the_guard(self) -> None:
        for flag, rel in (
            ("--assume-unchanged", "assumed.txt"),
            ("--skip-worktree", "skipped.txt"),
        ):
            with self.subTest(flag=flag):
                self._write(rel, "one\n")
                self._commit_all(f"add {rel}")
                self._flag_index_entry(rel, flag)
                before = self._fingerprint()

                self._write(rel, "two\n")
                after = self._fingerprint()

                # Git honours the flag by never comparing the entry against the
                # working tree, so every diff-derived key compares equal across
                # a mutation git itself refuses to look at.
                self.assertEqual([], after["tracked_changed"])
                self.assertEqual([], after["index_changed"])
                self.assertEqual(before["diff_sha256"], after["diff_sha256"])
                self.assertEqual(
                    before["index_diff_sha256"], after["index_diff_sha256"]
                )
                self.assertEqual(before["untracked"], after["untracked"])

                self.assertNotEqual(before, after)
                changed = review_guard._review_guard_changed_paths(before, after)
                self.assertEqual([rel], changed["tracked"])
                self.assertEqual(
                    f": {rel}", review_guard._review_guard_changed_summary(changed)
                )

    def test_deleted_skip_worktree_file_trips_the_guard(self) -> None:
        self._write("sparse.txt", "one\n")
        self._commit_all("add sparse.txt")
        self._flag_index_entry("sparse.txt", "--skip-worktree")
        before = self._fingerprint()

        # Removing the file is the ordinary skip-worktree state, so it is
        # recorded rather than failing the capture closed — but it is still a
        # repository mutation the reviewer's tree did not have.
        (self.repo_root / "sparse.txt").unlink()
        after = self._fingerprint()

        self.assertEqual("", self._git_status_porcelain())
        self.assertEqual(["sparse.txt", "absent"], after["index_flagged"][0][:2])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["sparse.txt"], changed["tracked"])

    def test_an_excluded_index_flagged_path_is_not_fingerprinted(self) -> None:
        self._write("vendor/pinned.txt", "one\n")
        self._commit_all("add vendor tree")
        self._flag_index_entry("vendor/pinned.txt", "--skip-worktree")
        before = self._fingerprint("vendor")

        self._write("vendor/pinned.txt", "two\n")

        self.assertEqual([], before["index_flagged"])
        self.assertEqual(before, self._fingerprint("vendor"))
        self.assertEqual(
            ["vendor/pinned.txt"],
            [entry[0] for entry in self._fingerprint()["index_flagged"]],
        )

    def test_failed_git_probe_yields_an_unavailable_fingerprint(self) -> None:
        failing = {
            "diff": ("diff", "HEAD"),
            "index-diff": ("diff", "--cached", "HEAD"),
            "untracked": ("ls-files", "--others"),
        }
        real_run_git = review_guard._run_git
        real_run_git_bytes = review_guard_git._run_git_bytes
        for label, prefix in failing.items():
            with self.subTest(probe=label):

                def fake_run_git(cwd, args, _prefix=prefix):
                    if tuple(args[: len(_prefix)]) == _prefix:
                        return subprocess.CompletedProcess(
                            args=["git", *args],
                            returncode=128,
                            stdout="",
                            stderr="fatal: broken index\n",
                        )
                    return real_run_git(cwd, args)

                # The diff probes read bytes so a non-UTF-8 patch stays
                # evidence; fail them on that runner.
                def fake_run_git_bytes(cwd, args, _prefix=prefix):
                    if tuple(args[: len(_prefix)]) == _prefix:
                        return subprocess.CompletedProcess(
                            args=["git", *args],
                            returncode=128,
                            stdout=b"",
                            stderr=b"fatal: broken index\n",
                        )
                    return real_run_git_bytes(cwd, args)

                with (
                    mock.patch.object(review_guard_git, "_run_git", fake_run_git),
                    mock.patch.object(
                        review_guard_git, "_run_git_bytes", fake_run_git_bytes
                    ),
                ):
                    fingerprint = self._fingerprint()

                detail = review_guard._review_guard_unavailable_detail(fingerprint)
                self.assertIsNotNone(detail)
                self.assertEqual("git-command-failed", detail["reason"])
                self.assertEqual(128, detail["returncode"])
                self.assertEqual("fatal: broken index", detail["stderr"])
                self.assertEqual(list(prefix), detail["argv"][: len(prefix)])
                # The failure must not masquerade as clean evidence.
                self.assertNotIn("diff_sha256", fingerprint)
                self.assertNotIn("tracked_changed", fingerprint)
                self.assertNotIn("untracked", fingerprint)

    def test_untracked_read_error_yields_an_unavailable_fingerprint(self) -> None:
        self._write("scratch.txt", "readable\n")
        real_read_bytes = Path.read_bytes

        def fake_read_bytes(self_path: Path) -> bytes:
            if self_path.name == "scratch.txt":
                raise OSError(errno.EIO, "input/output error")
            return real_read_bytes(self_path)

        with mock.patch.object(Path, "read_bytes", fake_read_bytes):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("untracked-path-unreadable", detail["reason"])
        self.assertEqual("scratch.txt", detail["path"])
        self.assertIn("input/output error", detail["error"])
        # A dropped path must not leave the rest of the capture usable.
        self.assertNotIn("untracked", fingerprint)
        self.assertNotIn("diff_sha256", fingerprint)

    def test_untracked_path_git_reports_but_that_vanished_fails_closed(
        self,
    ) -> None:
        real_run_git = review_guard._run_git

        def fake_run_git(cwd, args):
            if tuple(args[:2]) == ("ls-files", "--others"):
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=0,
                    stdout="vanished.txt\0",
                    stderr="",
                )
            return real_run_git(cwd, args)

        with mock.patch.object(review_guard_git, "_run_git", fake_run_git):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("untracked-path-unreadable", detail["reason"])
        self.assertEqual("vanished.txt", detail["path"])

    def test_unrunnable_git_yields_an_unavailable_fingerprint(self) -> None:
        def fake_run_git(cwd, args):
            raise runner.RunError("git executable not found on PATH")

        with mock.patch.object(review_guard_git, "_run_git", fake_run_git):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("git-unrunnable", detail["reason"])
        self.assertIn("git executable not found", detail["error"])

    def test_non_repository_is_not_treated_as_unavailable(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir()

        # The ceiling keeps the answer independent of whatever encloses the
        # temporary directory on the machine running the suite.
        with patched_env(GIT_CEILING_DIRECTORIES=str(self.tmp_path)):
            fingerprint = review_guard._capture_review_repo_fingerprint(
                outside, self.plan_dir
            )

        self.assertEqual({"git": False}, fingerprint)
        self.assertIsNone(review_guard._review_guard_unavailable_detail(fingerprint))

    def test_changed_paths_reports_a_moved_head(self) -> None:
        before = self._fingerprint()
        self._write("README.md", "committed\n")
        subprocess.run(
            ["git", "commit", "-am", "second"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertTrue(changed["head_changed"])
        self.assertEqual("", review_guard._review_guard_changed_summary(changed))


class GitTimeoutTests(ReviewFingerprintBase):
    """A git that never returns is killed and reported, never waited on."""

    def _hung(self, argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    def test_every_git_call_carries_the_default_ceiling(self) -> None:
        with mock.patch.object(
            git_subprocess.subprocess, "run", wraps=subprocess.run
        ) as spawned:
            git_subprocess.run_git(self.repo_root, ["rev-parse", "--git-dir"])
            git_subprocess.run_git_reporting(self.repo_root, ["rev-parse", "HEAD"])
            git_subprocess.run_git(self.repo_root, ["status"], timeout=None)

        ceiling = git_subprocess.GIT_TIMEOUT_SECONDS
        self.assertEqual(
            [ceiling, ceiling, None],
            [call.kwargs["timeout"] for call in spawned.call_args_list],
        )

    def test_a_timeout_takes_the_unrunnable_path_of_each_shape(self) -> None:
        with mock.patch.object(git_subprocess.subprocess, "run", self._hung):
            with self.assertRaises(git_subprocess.GitTimeoutError) as raised:
                git_subprocess.run_git(self.repo_root, ["status"])
            self.assertIsInstance(raised.exception, OSError)
            self.assertEqual(errno.ETIMEDOUT, raised.exception.errno)

            with self.assertRaises(runner.RunError) as converted:
                git_subprocess.run_git_or_raise(
                    self.repo_root, ["status"], error=runner.RunError
                )
            self.assertIn("did not finish within 300s", str(converted.exception))

            reported = git_subprocess.run_git_reporting(self.repo_root, ["status"])
            self.assertEqual(1, reported.returncode)
            self.assertIn("did not finish", reported.stderr)

            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("git-unrunnable", detail["reason"])

    def test_workspace_and_origin_probes_fold_a_timeout_into_their_contract(
        self,
    ) -> None:
        with mock.patch.object(git_subprocess.subprocess, "run", self._hung):
            with self.assertRaises(workspaces.WorkspaceError):
                workspaces._git_stdout(self.repo_root, "rev-parse", "--show-toplevel")
            self.assertIsNone(repository_scope_registry._origin_of(self.repo_root))
