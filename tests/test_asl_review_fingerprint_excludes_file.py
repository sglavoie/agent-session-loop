"""Fingerprint evidence from ``core.excludesFile`` and ignore symlinks."""

from __future__ import annotations

import hashlib
import os
from unittest import mock

from agent_session_loop import (
    review_guard,
)
from tests.review_fingerprint_base import ReviewFingerprintBase
from tests.support import (
    git_stdout,
)

ASL_TEST_LANE = "integration"


class ReviewFingerprintExcludesFileTests(ReviewFingerprintBase):
    """Fingerprint evidence from ``core.excludesFile`` and ignore symlinks."""

    def test_core_excludes_file_cannot_hide_a_review_mutation(self) -> None:
        # `--exclude-standard` honours `core.excludesFile` too, so a turn can
        # repoint it at a file it writes under `.git` and every worktree scan
        # goes quiet about the path the new rule hides.
        before = self._fingerprint()

        selected = self.repo_root / ".git" / "review-excludes"
        selected.write_text("secret.txt\n", encoding="utf-8")
        self._set_excludes_file(".git/review-excludes")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertIn("config:core.excludesFile", changed["ignore_config"])
        self.assertIn(".git/review-excludes", changed["ignore_config"])
        self.assertIn(
            "config:core.excludesFile",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_core_excludes_file_trailing_newline_cannot_hide_a_review_mutation(
        self,
    ) -> None:
        # A newline terminates git's own output record but is also a legal
        # pathname byte, so trimming it collapses two different settings — and
        # the two different files they select — onto one fingerprint.
        selected = self.repo_root / ".git" / "review-excludes"
        selected.write_text("*.tmp\n", encoding="utf-8")
        self._set_excludes_file(".git/review-excludes")
        before = self._fingerprint()

        (self.repo_root / ".git" / "review-excludes\n").write_text(
            "secret.txt\n", encoding="utf-8"
        )
        self._set_excludes_file(".git/review-excludes\n")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        # Git honours the newline-terminated pathname, so every worktree scan
        # goes quiet about the file its rule hides.
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertIn("config:core.excludesFile", changed["ignore_config"])
        self.assertIn(".git/review-excludes\n", changed["ignore_config"])
        self.assertIn(
            "config:core.excludesFile",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_effective_excludes_file_preserves_every_pathname_byte(self) -> None:
        # Reading git's answer as text would rewrite `\r` and `\r\n` to `\n`,
        # mapping three distinct settings — and three distinct files — onto one
        # marker, which is the newline evasion one byte over.
        markers = set()
        for value in (".git/x\rex", ".git/x\r\nex", ".git/x\nex"):
            self._set_excludes_file(value)
            marker, selected = review_guard._effective_excludes_file(
                self.repo_root, self.repo_root
            )
            markers.add(marker)
            self.assertEqual(self.repo_root / value, selected)
        self.assertEqual(3, len(markers))

    def test_selected_excludes_file_edit_trips_the_guard(self) -> None:
        # Leaving the setting alone and editing the file it already selects is
        # the same evasion one step later.
        selected = self.repo_root / ".git" / "review-excludes"
        selected.write_text("*.tmp\n", encoding="utf-8")
        self._set_excludes_file(".git/review-excludes")
        before = self._fingerprint()

        selected.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        self.assertEqual(before["untracked"], after["untracked"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/review-excludes"], changed["ignore_config"])

    def test_selected_excludes_file_symlink_target_edit_trips_the_guard(
        self,
    ) -> None:
        # The same evasion through the other source: git reads the target of
        # the link `core.excludesFile` selects, so the link text alone is not
        # evidence of the rules in force.
        target = self._symlink_selected_excludes_file("review-rules")
        target.write_text("*.tmp\n", encoding="utf-8")
        before = self._fingerprint()
        self.assertEqual(
            "symlink", self._ignore_config(before)[".git/review-excludes"][0]
        )

        target.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual("", git_stdout(self.repo_root, "status", "--short").strip())
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["target:.git/review-rules"], changed["ignore_config"])
        self.assertIn(
            "target:.git/review-rules",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_stable_symlinked_ignore_sources_leave_the_fingerprint_still(
        self,
    ) -> None:
        # Following the link must not make an untouched configuration look like
        # movement, which would pause every review turn on a linked source.
        info_target = self._symlink_info_exclude("rules")
        info_target.write_text("*.tmp\n", encoding="utf-8")
        selected_target = self._symlink_selected_excludes_file("review-rules")
        selected_target.write_text("*.log\n", encoding="utf-8")

        before = self._fingerprint()

        self.assertEqual(before, self._fingerprint())
        entries = self._ignore_config(before)
        self.assertEqual("file", entries["target:.git/info/rules"][0])
        self.assertEqual("file", entries["target:.git/review-rules"][0])

    def test_ignore_target_label_cannot_collide_with_source_path(self) -> None:
        # `target:` is a legal repo-relative pathname prefix, so an excludes
        # file at `target:.git/info/rules` spells exactly the label a symlinked
        # `.git/info/exclude` resolving to `.git/info/rules` produces. Keyed by
        # name alone the source entry displaced the target entry, and a
        # target-only mutation moved the fingerprint while naming nothing.
        collide = "target:.git/info/rules"
        self._write(collide, "*.log\n")
        self._set_excludes_file(collide)
        target = self._symlink_info_exclude("rules")
        target.write_text("*.tmp\n", encoding="utf-8")

        before = self._fingerprint()
        colliding = [
            list(entry) for entry in before["ignore_config"] if entry[1] == collide
        ]
        self.assertEqual(
            ["source", "target"], sorted(str(entry[0]) for entry in colliding)
        )
        # Two different files under one label: whichever the name-keyed map kept
        # made the other's content unobservable.
        self.assertNotEqual(colliding[0][2:], colliding[1][2:])

        target.write_text("*.tmp\nsecret.txt\n", encoding="utf-8")
        self._write("secret.txt", "leak\n")
        after = self._fingerprint()

        # The link text, the setting, and the file the setting selects are all
        # untouched, and git hides the file the new rule names.
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([collide], changed["ignore_config"])
        self.assertIn(collide, review_guard._review_guard_changed_summary(changed))

    def test_dev_null_excludes_file_leaves_the_fingerprint_still(self) -> None:
        self._set_excludes_file("/dev/null")

        before = self._fingerprint()

        self.assertEqual(
            ["unsupported", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)["/dev/null"],
        )
        self.assertEqual(before, self._fingerprint())

    def test_symlinked_dev_null_excludes_file_fails_the_guard_closed(
        self,
    ) -> None:
        # A link is never how `core.excludesFile=/dev/null` switches the global
        # excludes off, so an unsupported target is a state the evidence cannot
        # describe rather than a configuration to tolerate.
        self._symlink_selected_excludes_file("/dev/null")

        fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        assert detail is not None
        self.assertEqual("excludes-file-target-node-unsupported", detail["reason"])
        self.assertEqual("target:/dev/null", detail["path"])

    def test_absent_excludes_file_is_fingerprinted_by_its_own_kind(
        self,
    ) -> None:
        self._set_excludes_file(".git/review-excludes")
        before = self._fingerprint()

        self.assertEqual(
            ["absent", hashlib.sha256(b"").hexdigest()],
            self._ignore_config(before)[".git/review-excludes"],
        )

        (self.repo_root / ".git" / "review-excludes").write_text(
            "secret.txt\n", encoding="utf-8"
        )
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual([".git/review-excludes"], changed["ignore_config"])

    def test_unset_excludes_file_falls_back_to_the_xdg_default(self) -> None:
        xdg = self.tmp_path / "xdg"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False):
            self.assertEqual(
                xdg / "git" / "ignore", review_guard._default_excludes_file()
            )
            os.environ["XDG_CONFIG_HOME"] = ""
            os.environ["HOME"] = str(self.tmp_path / "home")
            self.assertEqual(
                self.tmp_path / "home" / ".config" / "git" / "ignore",
                review_guard._default_excludes_file(),
            )

    def test_non_excluded_change_still_trips_the_guard(self) -> None:
        before = self._fingerprint("claude")

        self._write("README.md", "mutated\n")
        after = self._fingerprint("claude")

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])
