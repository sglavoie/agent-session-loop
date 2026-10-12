"""Fingerprint evidence from gitignore files and ``info/exclude``."""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    review_guard,
)
from tests.review_fingerprint_base import ReviewFingerprintBase
from tests.support import (
    git_stdout,
)


class ReviewFingerprintIgnoreSourceTests(ReviewFingerprintBase):
    """Fingerprint evidence from gitignore files and ``info/exclude``."""

    def test_untracked_worktree_ignore_edit_and_deletion_move_the_evidence(
        self,
    ) -> None:
        self._write("generated/.gitignore", "*\n")
        before = self._fingerprint()
        self.assertEqual(
            ["generated/.gitignore"],
            [entry[0] for entry in before["worktree_ignore"]],
        )

        strict_before = self._fingerprint(include_ignored=True)

        self._write("generated/.gitignore", "*\n!keep.txt\n")
        edited = self._fingerprint()

        self.assertEqual(
            ["generated/.gitignore"],
            review_guard._review_guard_changed_paths(before, edited)["worktree_ignore"],
        )
        # The strict scan lists the same file under `untracked`, so the summary
        # would otherwise name it twice.
        self.assertEqual(
            ": generated/.gitignore",
            review_guard._review_guard_changed_summary(
                review_guard._review_guard_changed_paths(
                    strict_before, self._fingerprint(include_ignored=True)
                )
            ),
        )

        (self.repo_root / "generated" / ".gitignore").unlink()
        deleted = self._fingerprint()

        self.assertEqual([], deleted["worktree_ignore"])
        self.assertEqual(
            ["generated/.gitignore"],
            review_guard._review_guard_changed_paths(edited, deleted)[
                "worktree_ignore"
            ],
        )

    def test_ignored_gitignore_symlink_stays_out_of_worktree_ignore_evidence(
        self,
    ) -> None:
        # Git opens an ignore file with `O_NOFOLLOW`, so a symlinked
        # `.gitignore` contributes no rules however it is retargeted — but the
        # negative command-line pattern re-lists it anyway, and fingerprinting
        # the link text would discard a review turn over a mutation git never
        # acted on.
        self._write(".gitignore", "generated/.gitignore\ngenerated/rules-*\n")
        self._commit_all("ignore the generated ignore source and its targets")
        self._write("generated/rules-a", "hidden-a.txt\n")
        self._write("generated/rules-b", "hidden-b.txt\n")
        link = self.repo_root / "generated" / ".gitignore"
        link.symlink_to("rules-a")
        before = self._fingerprint()

        link.unlink()
        link.symlink_to("rules-b")
        after = self._fingerprint()

        self.assertEqual("", git_stdout(self.repo_root, "status", "--short"))
        self.assertEqual(
            "",
            git_stdout(self.repo_root, "ls-files", "--others", "--exclude-standard"),
        )
        self.assertEqual([], after["untracked"])
        self.assertEqual([], after["worktree_ignore"])
        self.assertEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([], changed["worktree_ignore"])
        self.assertEqual("", review_guard._review_guard_changed_summary(changed))

    def test_gitignore_git_cannot_apply_stays_out_of_the_evidence(self) -> None:
        # Git never descends into an ignored directory, so a `.gitignore`
        # inside one is a file whose rules git never applies — and the tool
        # caches that churn every review turn write exactly that. Fingerprinting
        # it would revive the false positives the default scan exists to avoid.
        self._write(".gitignore", "cache/\n")
        self._commit_all("ignore a cache directory")
        before = self._fingerprint()

        self._write("cache/.gitignore", "*\n")
        self._write("cache/blob.bin", "one\n")

        self.assertEqual([], before["worktree_ignore"])
        self.assertEqual(before, self._fingerprint())

    def test_git_info_exclude_mutation_trips_the_guard(self) -> None:
        self._write_info_exclude("*.tmp\n")
        before = self._fingerprint()

        self._write_info_exclude("*.tmp\nsecret.txt\n")
        after = self._fingerprint()

        # No diff and no untracked scan reaches inside the git directory, so a
        # turn could rewrite this to hide the path it wrote (as it could the
        # excludes file `core.excludesFile` names).
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/info/exclude"], changed["ignore_config"])
        self.assertEqual(
            ": .git/info/exclude",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_absent_git_info_exclude_still_trips_on_creation(self) -> None:
        self._info_exclude().unlink(missing_ok=True)
        before = self._fingerprint()

        self.assertEqual(
            ["absent", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)[".git/info/exclude"],
        )

        self._write_info_exclude("secret.txt\n")
        after = self._fingerprint()

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/info/exclude"], changed["ignore_config"])

    def test_git_info_exclude_resolves_from_a_nested_cwd(self) -> None:
        # ``rev-parse --git-path`` answers relative to the directory git ran in,
        # so a nested cwd reports ``../.git/info/exclude`` and joining that
        # against the repository root would fingerprint a path that never
        # exists — always absent, never moving.
        self._write_info_exclude("*.tmp\n")
        nested = self.repo_root / "nested"
        nested.mkdir()

        fingerprint = review_guard._capture_review_repo_fingerprint(
            nested, self.plan_dir
        )

        self.assertEqual(
            "file", self._ignore_config(fingerprint)[".git/info/exclude"][0]
        )
        self.assertEqual(
            self._fingerprint()["ignore_config"], fingerprint["ignore_config"]
        )

    def test_git_info_exclude_symlink_target_edit_trips_the_guard(self) -> None:
        # Git follows a symlinked exclusion source, so evidence that stops at
        # the link text goes quiet when only the target's rules change.
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        before = self._fingerprint()
        self.assertEqual("symlink", self._ignore_config(before)[".git/info/exclude"][0])

        target.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        # The link itself never moved and git hides the file its new rule names.
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["target:.git/info/rules"], changed["ignore_config"])
        self.assertEqual(
            ": target:.git/info/rules",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_dangling_symlinked_source_moves_when_its_target_appears(
        self,
    ) -> None:
        target = self._symlink_info_exclude("rules")
        before = self._fingerprint()

        self.assertEqual(
            ["absent", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)["target:.git/info/rules"],
        )

        target.write_text("secret.txt\n", encoding="utf-8")
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["target:.git/info/rules"], changed["ignore_config"])

    def test_replacing_a_symlinked_source_with_its_target_trips_the_guard(
        self,
    ) -> None:
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        before = self._fingerprint()

        link = self._info_exclude()
        link.unlink()
        link.write_text("*.tmp\n", encoding="utf-8")
        after = self._fingerprint()

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(
            [".git/info/exclude", "target:.git/info/rules"],
            changed["ignore_config"],
        )

    def test_symlink_cycle_in_an_ignore_source_fails_the_guard_closed(
        self,
    ) -> None:
        self._symlink_info_exclude("exclude")

        fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        assert detail is not None
        self.assertEqual("ignore-config-target-unresolved", detail["reason"])
        self.assertNotIn("ignore_config", fingerprint)

    def test_unreadable_symlinked_ignore_target_fails_the_guard_closed(
        self,
    ) -> None:
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        real_read_bytes = Path.read_bytes

        def fake_read_bytes(self_path: Path) -> bytes:
            if self_path.name == "rules":
                raise OSError(errno.EIO, "input/output error")
            return real_read_bytes(self_path)

        with mock.patch.object(Path, "read_bytes", fake_read_bytes):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        assert detail is not None
        self.assertEqual("ignore-config-target-path-unreadable", detail["reason"])
        self.assertEqual("target:.git/info/rules", detail["path"])
