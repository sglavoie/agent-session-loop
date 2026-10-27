"""Fingerprint diff evidence: changed paths, symlinks, drivers, and typechanges."""

from __future__ import annotations

import hashlib
import subprocess
from unittest import mock

from agent_session_loop import (
    review_guard,
    review_guard_git,
)
from tests.review_fingerprint_base import ReviewFingerprintBase
from tests.support import init_git_repo

ASL_TEST_LANE = "integration"


class ReviewFingerprintDiffEvidenceTests(ReviewFingerprintBase):
    """Fingerprint diff evidence: changed paths, symlinks, drivers, and typechanges."""

    def test_changed_paths_reports_tracked_and_untracked_movement(self) -> None:
        self._write("kept.txt", "kept\n")
        self._write("removed.txt", "removed\n")
        before = self._fingerprint()

        self._write("README.md", "mutated\n")
        self._write("kept.txt", "changed\n")
        (self.repo_root / "removed.txt").unlink()
        self._write("added.txt", "added\n")
        after = self._fingerprint()

        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(
            {
                "tracked": ["README.md"],
                "untracked_added": ["added.txt"],
                "untracked_removed": ["removed.txt"],
                "untracked_changed": ["kept.txt"],
                "worktree_ignore": [],
                "ignore_config": [],
                "head_changed": False,
                "head_before": before["head"],
                "head_after": after["head"],
                "refs_added": [],
                "refs_removed": [],
                "refs_changed": [],
            },
            changed,
        )
        self.assertEqual(
            ": README.md, added.txt, kept.txt (+1 more)",
            review_guard._review_guard_changed_summary(changed),
        )

    def test_untracked_symlink_is_fingerprinted_as_a_node(self) -> None:
        self._write("payload.txt", "hi\n")
        (self.repo_root / "link").symlink_to("payload.txt")
        before = self._fingerprint()

        self.assertIn(
            ["link", "symlink", hashlib.sha256(b"payload.txt").hexdigest()],
            before["untracked"],
        )

        # Following the link would have hashed the target's bytes, so replacing
        # the link with a regular file holding those bytes compared equal.
        (self.repo_root / "link").unlink()
        self._write("link", "hi\n")
        after = self._fingerprint()

        self.assertNotEqual(before, after)
        self.assertEqual(
            ["link"],
            review_guard._review_guard_changed_paths(before, after)[
                "untracked_changed"
            ],
        )

    def test_retargeted_dangling_symlink_trips_the_guard(self) -> None:
        (self.repo_root / "link").symlink_to("missing-a")
        before = self._fingerprint()

        (self.repo_root / "link").unlink()
        (self.repo_root / "link").symlink_to("missing-b")
        after = self._fingerprint()

        # Reading through the link fails for both targets, so omitting the path
        # left the mutation invisible in both fingerprints.
        self.assertNotEqual(before, after)
        self.assertEqual(
            ["link"],
            review_guard._review_guard_changed_paths(before, after)[
                "untracked_changed"
            ],
        )

    def test_renamed_tracked_file_names_both_sides(self) -> None:
        subprocess.run(
            ["git", "mv", "README.md", "DOCS.md"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

        fingerprint = self._fingerprint()

        # Rename detection would report the destination only, hiding the source
        # from the dirty-path evidence a later rereview scopes against.
        self.assertEqual(["DOCS.md", "README.md"], fingerprint["tracked_changed"])
        self.assertEqual(
            ["DOCS.md", "README.md"],
            sorted(entry[0] for entry in fingerprint["tracked_patches"]),
        )

    def test_unborn_head_fingerprints_staged_content_against_empty_tree(
        self,
    ) -> None:
        unborn = self.tmp_path / "unborn"
        unborn.mkdir()
        subprocess.run(["git", "init"], cwd=unborn, capture_output=True, check=True)
        (unborn / "tracked.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=unborn,
            capture_output=True,
            check=True,
        )

        before = review_guard._capture_review_repo_fingerprint(unborn, self.plan_dir)
        self.assertIsNone(review_guard._review_guard_unavailable_detail(before))
        self.assertEqual("", before["head"])
        self.assertEqual(["tracked.txt"], before["tracked_changed"])

        (unborn / "tracked.txt").write_text("two\n", encoding="utf-8")
        after = review_guard._capture_review_repo_fingerprint(unborn, self.plan_dir)

        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["tracked.txt"], changed["tracked"])
        # No commit exists, so no base ref can be recorded for delta scoping.
        self.assertIsNone(review_guard._review_repo_state(after))

    def test_staged_change_masked_by_a_restored_file_trips_the_guard(
        self,
    ) -> None:
        before = self._fingerprint()

        self._write("README.md", "staged\n")
        self._stage("README.md")
        self._write("README.md", "test repo\n")
        after = self._fingerprint()

        # The working file is byte-identical to HEAD again, so every piece of
        # evidence but the index diff compares equal across the turn.
        self.assertEqual([], after["tracked_changed"])
        self.assertEqual(before["diff_sha256"], after["diff_sha256"])
        self.assertEqual(before["untracked"], after["untracked"])
        self.assertEqual(["README.md"], after["index_changed"])
        self.assertNotEqual(before, after)
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])
        self.assertEqual(
            ": README.md", review_guard._review_guard_changed_summary(changed)
        )

    def test_an_excluded_path_is_dropped_from_the_index_evidence_too(
        self,
    ) -> None:
        before = self._fingerprint("README.md")

        self._write("README.md", "staged\n")
        self._stage("README.md")
        self._write("README.md", "test repo\n")

        self.assertEqual(before, self._fingerprint("README.md"))

    def test_a_textconv_driver_cannot_mask_a_worktree_mutation(self) -> None:
        self._commit_constant_textconv_driver()

        self._write("masked.txt", "two\n")
        before = self._fingerprint()
        self._write("masked.txt", "three\n")
        after = self._fingerprint()

        # Git renders no patch at all for a path whose converted text did not
        # move, and the path is already dirty in both captures, so the name
        # lists cannot tell the two contents apart either.
        self.assertEqual("", self._git("diff", "HEAD", "--", "masked.txt"))
        self.assertEqual(["masked.txt"], before["tracked_changed"])
        self.assertEqual(before["tracked_changed"], after["tracked_changed"])

        self.assertNotEqual(before["diff_sha256"], after["diff_sha256"])
        self.assertNotEqual(before["tracked_patches"], after["tracked_patches"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["masked.txt"], changed["tracked"])

    def test_a_textconv_driver_cannot_mask_a_staged_mutation(self) -> None:
        self._commit_constant_textconv_driver()

        self._write("masked.txt", "two\n")
        self._stage("masked.txt")
        before = self._fingerprint()
        self._write("masked.txt", "three\n")
        self._stage("masked.txt")
        after = self._fingerprint()

        self.assertEqual("", self._git("diff", "--cached", "HEAD", "--", "masked.txt"))
        self.assertEqual(["masked.txt"], before["index_changed"])
        self.assertEqual(before["index_changed"], after["index_changed"])

        self.assertNotEqual(before["index_diff_sha256"], after["index_diff_sha256"])
        self.assertNotEqual(before["index_patches"], after["index_patches"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["masked.txt"], changed["tracked"])

    def test_an_external_diff_driver_cannot_mask_a_mutation(self) -> None:
        # Lives outside the repository so the driver itself is not evidence.
        driver = self.tmp_path / "constant-diff.sh"
        driver.write_text("#!/bin/sh\necho CONSTANT\n", encoding="utf-8")
        driver.chmod(0o755)
        self._git_config("diff.external", str(driver))

        self._write("README.md", "two\n")
        before = self._fingerprint()
        self._write("README.md", "three\n")
        after = self._fingerprint()

        self.assertEqual("CONSTANT\n", self._git("diff", "HEAD", "--", "README.md"))
        self.assertNotEqual(before["diff_sha256"], after["diff_sha256"])
        self.assertNotEqual(before["tracked_patches"], after["tracked_patches"])
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(["README.md"], changed["tracked"])

    def test_non_utf8_content_is_fingerprinted_rather_than_unavailable(
        self,
    ) -> None:
        before = self._fingerprint()
        (self.repo_root / "README.md").write_bytes(b"caf\xe9\n")
        self._git("add", "README.md")
        after = self._fingerprint()

        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        self.assertEqual(["README.md"], after["index_changed"])
        (self.repo_root / "README.md").write_bytes(b"caf\xe8\n")
        self._git("add", "README.md")
        later = self._fingerprint()
        # The undecodable byte is part of the evidence, not replaced away.
        self.assertNotEqual(after["index_diff_sha256"], later["index_diff_sha256"])
        changed = review_guard._review_guard_changed_paths(before, later)
        self.assertEqual(["README.md"], changed["tracked"])

    def test_unusual_untracked_names_are_fingerprinted_not_unavailable(
        self,
    ) -> None:
        before = self._fingerprint()
        self._write("café.md", "one\n")
        self._write('say "hi".txt', "two\n")
        self._write("tab\there.txt", "three\n")
        after = self._fingerprint()

        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        self.assertEqual(
            ["café.md", 'say "hi".txt', "tab\there.txt"],
            [entry[0] for entry in after["untracked"]],
        )
        changed = review_guard._review_guard_changed_paths(before, after)
        self.assertEqual(
            ["café.md", 'say "hi".txt', "tab\there.txt"],
            changed["untracked_added"],
        )

    def test_unusual_tracked_names_are_recorded_unquoted(self) -> None:
        names = ["café.md", 'say "hi".txt', "tab\there.txt", "new\nline.txt"]
        for name in names:
            self._write(name, "one\n")
        self._commit_all("add unusual names")
        before = self._fingerprint()
        for name in names:
            self._write(name, "two\n")
        self._stage("café.md")
        after = self._fingerprint()

        # Without `-z` git C-quotes each of these, so `café.md` would be
        # recorded as `"caf\303\251.md"`, which names no file on disk.
        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        self.assertEqual(sorted(names), after["tracked_changed"])
        self.assertEqual(
            sorted(names), sorted(entry[0] for entry in after["tracked_patches"])
        )
        self.assertEqual(["café.md"], after["index_changed"])
        for name in names:
            self.assertTrue((self.repo_root / name).is_file(), name)
        self.assertEqual(
            sorted(names),
            review_guard._review_guard_changed_paths(before, after)["tracked"],
        )

    def test_an_untracked_nested_repository_is_fingerprinted_as_a_node(
        self,
    ) -> None:
        nested = self.repo_root / "nested"
        init_git_repo(nested)

        # `ls-files --others` lists the embedded repository as `nested/`, a
        # directory no file fingerprint can describe.
        before = self._fingerprint()
        self.assertIsNone(review_guard._review_guard_unavailable_detail(before))
        self.assertEqual(
            [["nested", "nested-repo"]],
            [entry[:2] for entry in before["untracked"]],
        )
        self.assertEqual(before, self._fingerprint())

        (nested / "README.md").write_text("edited\n", encoding="utf-8")
        edited = self._fingerprint()
        self.assertEqual(
            ["nested"],
            review_guard._review_guard_changed_paths(before, edited)[
                "untracked_changed"
            ],
        )
        (nested / "new.txt").write_text("new\n", encoding="utf-8")
        added = self._fingerprint()
        self.assertNotEqual(edited["untracked"], added["untracked"])
        # An allowlisted path inside the nested repository stays out of it.
        self.assertEqual(
            edited["untracked"], self._fingerprint("nested/new.txt")["untracked"]
        )

    def test_an_unresolved_merge_conflict_is_fingerprinted(self) -> None:
        self._write("a.txt", "base\n")
        self._write("z.txt", "base\n")
        self._commit_all("add conflict files")
        self._git("checkout", "-q", "-b", "theirs")
        self._write("z.txt", "theirs\n")
        self._commit_all("theirs")
        self._git("checkout", "-q", "-")
        self._write("z.txt", "ours\n")
        self._commit_all("ours")
        merge = subprocess.run(
            ["git", "merge", "theirs"], cwd=self.repo_root, capture_output=True
        )
        self.assertNotEqual(0, merge.returncode)
        self._write("a.txt", "staged\n")
        self._stage("a.txt")

        # The index diff lists `z.txt` as `U` and renders no patch for it.
        before = self._fingerprint()
        self.assertIsNone(review_guard._review_guard_unavailable_detail(before))
        self.assertEqual(["a.txt", "z.txt"], before["index_changed"])
        self.assertEqual(
            ["a.txt", "z.txt"], [entry[0] for entry in before["index_patches"]]
        )

        # The unmerged notice is not attributed to its neighbour, so a stable
        # conflict compares equal and editing the conflicted file names it.
        self.assertEqual(before, self._fingerprint())
        self._write("z.txt", "edited\n")
        edited = self._fingerprint()
        self.assertEqual(
            ["z.txt"],
            review_guard._review_guard_changed_paths(before, edited)["tracked"],
        )
        self._stage("z.txt")
        resolved = self._fingerprint()
        self.assertIsNone(review_guard._review_guard_unavailable_detail(resolved))
        self.assertEqual(
            ["z.txt"],
            review_guard._review_guard_changed_paths(edited, resolved)["tracked"],
        )

    def test_a_named_path_git_renders_no_patch_for_fails_closed(self) -> None:
        self._write("README.md", "mutated\n")
        real_run_git_bytes = review_guard_git._run_git_bytes

        def fake_run_git_bytes(cwd, args):
            result = real_run_git_bytes(cwd, args)
            if tuple(args[:3]) == ("diff", "HEAD", "--patch-with-raw"):
                # Keep the raw entry that names the path; drop its patch.
                raw, _, _patch = result.stdout.partition(b"\0\0")
                return subprocess.CompletedProcess(
                    args=result.args, returncode=0, stdout=raw + b"\0\0", stderr=b""
                )
            return result

        with mock.patch.object(review_guard_git, "_run_git_bytes", fake_run_git_bytes):
            fingerprint = self._fingerprint()

        # A named path with no patch carries no content, so pairing short would
        # make any two of its contents compare equal.
        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("tracked-patch-missing", detail["reason"])
        self.assertNotIn("tracked_patches", fingerprint)

    def test_a_raw_entry_without_its_fields_fails_closed(self) -> None:
        self._write("README.md", "mutated\n")
        real_run_git_bytes = review_guard_git._run_git_bytes

        def fake_run_git_bytes(cwd, args):
            result = real_run_git_bytes(cwd, args)
            if tuple(args[:3]) == ("diff", "HEAD", "--patch-with-raw"):
                return subprocess.CompletedProcess(
                    args=result.args,
                    returncode=0,
                    stdout=b":100644 M\0README.md\0" + result.stdout,
                    stderr=b"",
                )
            return result

        with mock.patch.object(review_guard_git, "_run_git_bytes", fake_run_git_bytes):
            fingerprint = self._fingerprint()

        detail = review_guard._review_guard_unavailable_detail(fingerprint)
        self.assertIsNotNone(detail)
        self.assertEqual("diff-raw-malformed", detail["reason"])
        self.assertNotIn("tracked_patches", fingerprint)

    def test_a_typechanged_path_pairs_both_of_its_patches(self) -> None:
        before = self._fingerprint()

        (self.repo_root / "README.md").unlink()
        (self.repo_root / "README.md").symlink_to("target-a")
        after = self._fingerprint()

        # Git cannot express a change of type as one patch, so it renders the
        # old type's deletion and the new type's creation as two sections for
        # the one name entry every other status claims exactly one of.
        self.assertEqual(
            2, self._git("diff", "HEAD", "--", "README.md").count("diff --git ")
        )
        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        self.assertEqual(["README.md"], after["tracked_changed"])
        self.assertEqual(
            ["README.md"], [entry[0] for entry in after["tracked_patches"]]
        )
        self.assertEqual(
            ["README.md"],
            review_guard._review_guard_changed_paths(before, after)["tracked"],
        )

    def test_a_staged_typechange_pairs_both_of_its_patches(self) -> None:
        before = self._fingerprint()

        (self.repo_root / "README.md").unlink()
        (self.repo_root / "README.md").symlink_to("target-a")
        self._stage("README.md")
        after = self._fingerprint()

        self.assertEqual(
            2,
            self._git("diff", "--cached", "HEAD", "--", "README.md").count(
                "diff --git "
            ),
        )
        self.assertIsNone(review_guard._review_guard_unavailable_detail(after))
        self.assertEqual(["README.md"], after["index_changed"])
        self.assertEqual(["README.md"], [entry[0] for entry in after["index_patches"]])
        self.assertEqual(
            ["README.md"],
            review_guard._review_guard_changed_paths(before, after)["tracked"],
        )

    def test_a_typechange_does_not_misattribute_later_patches(self) -> None:
        self._write("a.txt", "one\n")
        self._write("b.txt", "one\n")
        self._commit_all("add two files")

        (self.repo_root / "a.txt").unlink()
        (self.repo_root / "a.txt").symlink_to("target")
        before = self._fingerprint()

        self._write("b.txt", "two\n")
        after = self._fingerprint()

        # Consuming one section for the typechange would have paired `b.txt`
        # with the second half of `a.txt`'s patch, so its own mutation would
        # not have moved its hash.
        self.assertEqual(["a.txt", "b.txt"], after["tracked_changed"])
        self.assertEqual(
            ["a.txt", "b.txt"],
            [entry[0] for entry in after["tracked_patches"]],
        )
        self.assertEqual(
            ["b.txt"],
            review_guard._review_guard_changed_paths(before, after)["tracked"],
        )
