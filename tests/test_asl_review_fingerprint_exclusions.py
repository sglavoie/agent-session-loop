"""Fingerprint exclusions, nested working directories, and ignored files."""

from __future__ import annotations

import subprocess

from agent_session_loop import (
    review_guard,
)
from tests.review_fingerprint_base import ReviewFingerprintBase
from tests.support import (
    git_stdout,
)


class ReviewFingerprintExclusionTests(ReviewFingerprintBase):
    """Fingerprint exclusions, nested working directories, and ignored files."""

    def test_excluded_tracked_change_leaves_fingerprint_unchanged(self) -> None:
        before_excluded = self._fingerprint("README.md")
        before_plain = self._fingerprint()

        self._write("README.md", "mutated\n")

        self.assertEqual(before_excluded, self._fingerprint("README.md"))
        self.assertNotEqual(before_plain, self._fingerprint())

    def test_excluded_untracked_file_is_not_fingerprinted(self) -> None:
        before = self._fingerprint("scratch.txt")

        self._write("scratch.txt", "ignored\n")

        after = self._fingerprint("scratch.txt")
        self.assertEqual(before, after)
        self.assertEqual([], after["untracked"])
        self.assertIn(
            "scratch.txt",
            [entry[0] for entry in self._fingerprint()["untracked"]],
        )

    def test_directory_prefix_excludes_tracked_and_untracked_children(self) -> None:
        self._write("claude/settings.json", "{}\n")
        subprocess.run(
            ["git", "add", "claude/settings.json"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add settings"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        before = self._fingerprint("claude")

        self._write("claude/settings.json", '{"mutated": true}\n')
        self._write("claude/cache/session.json", "{}\n")

        self.assertEqual(before, self._fingerprint("claude"))
        # A sibling directory sharing the prefix is still guarded.
        self._write("claude-extra/other.json", "{}\n")
        self.assertNotEqual(before, self._fingerprint("claude"))

    def test_glob_metacharacters_in_an_exclusion_are_matched_literally(
        self,
    ) -> None:
        cases = (
            ("dir[12]", ("dir1", "dir2")),
            ("q?mark.txt", ("qxmark.txt",)),
            ("star*name.txt", ("starANYname.txt",)),
        )
        for literal, siblings in cases:
            for rel in (literal, *siblings):
                self._write(rel, "one\n")
        self._commit_all("add wildcard-named files")
        for literal, siblings in cases:
            for rel in (literal, *siblings):
                self._write(rel, "two\n")

        for literal, siblings in cases:
            with self.subTest(exclusion=literal):
                tracked = self._fingerprint(literal)["tracked_changed"]
                self.assertNotIn(literal, tracked)
                # As a git pattern the exclusion would swallow these too.
                for sibling in siblings:
                    self.assertIn(sibling, tracked)

    def test_star_exclusion_does_not_erase_the_tracked_diff(self) -> None:
        before = self._fingerprint("*")

        self._write("README.md", "mutated\n")
        after = self._fingerprint("*")

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])

    def test_one_exclusion_selects_the_same_tracked_and_untracked_subtree(
        self,
    ) -> None:
        self._write("logs[1]/kept.txt", "one\n")
        self._write("logs1/kept.txt", "one\n")
        self._commit_all("add bracket-named directories")
        before = self._fingerprint("logs[1]")

        self._write("logs[1]/kept.txt", "two\n")
        self._write("logs[1]/scratch.txt", "new\n")

        self.assertEqual(before, self._fingerprint("logs[1]"))

        self._write("logs1/kept.txt", "two\n")
        self._write("logs1/scratch.txt", "new\n")
        after = self._fingerprint("logs[1]")

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["logs1/kept.txt"], changed["tracked"])
        self.assertEqual(["logs1/scratch.txt"], changed["untracked_added"])

    def test_untracked_paths_stay_repo_root_relative_from_a_subdirectory(
        self,
    ) -> None:
        self._write("nested/kept.txt", "one\n")
        self._commit_all("add nested tree")
        self._write("nested/scratch.txt", "new\n")
        nested = self.repo_root / "nested"

        fingerprint = review_guard._capture_review_repo_fingerprint(
            nested, self.plan_dir
        )

        self.assertIsNone(review_guard._review_guard_unavailable_detail(fingerprint))
        self.assertEqual(
            ["nested/scratch.txt"],
            [entry[0] for entry in fingerprint["untracked"]],
        )
        excluded = review_guard._capture_review_repo_fingerprint(
            nested, self.plan_dir, exclude=("nested",)
        )
        self.assertEqual([], excluded["untracked"])
        self.assertEqual([], excluded["tracked_changed"])

    def test_nested_cwd_still_fingerprints_the_whole_repository(self) -> None:
        self._write("inside/tracked.txt", "one\n")
        self._write("outside/tracked.txt", "one\n")
        self._commit_all("add both subtrees")
        # ``diff.relative`` narrows a diff to cwd *and* makes its paths
        # cwd-relative, so it is the sharpest test of the repo-root anchoring.
        subprocess.run(
            ["git", "config", "diff.relative", "true"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        nested = self.repo_root / "inside"
        before = review_guard._capture_review_repo_fingerprint(nested, self.plan_dir)

        self._write("inside/tracked.txt", "two\n")
        self._write("outside/tracked.txt", "two\n")
        self._write("inside/scratch.txt", "new\n")
        self._write("outside/scratch.txt", "new\n")
        after = review_guard._capture_review_repo_fingerprint(nested, self.plan_dir)

        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(
            ["inside/tracked.txt", "outside/tracked.txt"], changed["tracked"]
        )
        self.assertEqual(
            ["inside/scratch.txt", "outside/scratch.txt"],
            changed["untracked_added"],
        )

    def test_ignored_file_change_is_invisible_until_include_ignored(self) -> None:
        self._write(".gitignore", "ignored/\n")
        self._write("ignored/cache.txt", "one\n")
        self._commit_all("ignore a directory")
        before = self._fingerprint()
        strict_before = self._fingerprint(include_ignored=True)

        self._write("ignored/cache.txt", "two\n")

        self.assertEqual(before, self._fingerprint())
        strict_after = self._fingerprint(include_ignored=True)
        self.assertEqual(
            ["ignored/cache.txt"],
            review_guard._review_guard_changed_paths(strict_before, strict_after)[
                "untracked_changed"
            ],
        )

    def test_ignored_symlink_retarget_is_invisible_until_include_ignored(
        self,
    ) -> None:
        self._write(".gitignore", "*.link\n")
        self._commit_all("ignore link files")
        self._write("target-a.txt", "a\n")
        self._write("target-b.txt", "b\n")
        link = self.repo_root / "cache.link"
        link.symlink_to("target-a.txt")
        before = self._fingerprint()
        strict_before = self._fingerprint(include_ignored=True)

        link.unlink()
        link.symlink_to("target-b.txt")

        self.assertEqual(before, self._fingerprint())
        strict_after = self._fingerprint(include_ignored=True)
        self.assertIn(
            ["cache.link", "symlink"],
            [entry[:2] for entry in strict_after["untracked"]],
        )
        self.assertEqual(
            ["cache.link"],
            review_guard._review_guard_changed_paths(strict_before, strict_after)[
                "untracked_changed"
            ],
        )

    def test_self_ignoring_untracked_gitignore_cannot_hide_review_mutation(
        self,
    ) -> None:
        # Git applies a new untracked `.gitignore` before answering the guard's
        # scans, and this one matches itself as well as the file written beside
        # it: deferring to git's ignore rules would let a review turn write the
        # very rule that blinds the guard.
        before = self._fingerprint()

        self._write("generated/.gitignore", "*\n")
        self._write("generated/payload.py", "LEAKED = True\n")
        after = self._fingerprint()

        self.assertEqual("", git_stdout(self.repo_root, "status", "--short"))
        self.assertEqual(
            "",
            git_stdout(self.repo_root, "ls-files", "--others", "--exclude-standard"),
        )
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["generated/.gitignore"], changed["worktree_ignore"])
        self.assertEqual(
            ": generated/.gitignore",
            review_guard._review_guard_changed_summary(changed),
        )
        # The rule the turn wrote is evidence; the payload it hides is the
        # ordinary ignored content the default scan still defers to git about.
        self.assertEqual(
            ["generated/.gitignore"],
            [entry[0] for entry in after["worktree_ignore"]],
        )
