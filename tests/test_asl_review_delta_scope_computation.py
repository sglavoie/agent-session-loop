"""Delta scope computation against recorded repair cycles."""

from __future__ import annotations

import subprocess
from unittest import mock

from agent_session_loop import (
    events,
    review,
    review_guard_git,
    review_session_body,
)
from tests.review_delta_scope_base import ReviewDeltaScopeBase

ASL_TEST_LANE = "integration"


class ReviewDeltaScopeComputationTests(ReviewDeltaScopeBase):
    """Delta scope computation against recorded repair cycles."""

    def test_first_review_without_repair_history_is_full_scope(self) -> None:
        self.assertEqual((None, "no-prior-repair-cycle"), self._compute())

    def test_unloadable_prior_findings_fall_back_to_full_scope(self) -> None:
        self._record_repair_cycle(repo_head=self._git("rev-parse", "HEAD"))
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

        self.assertEqual((None, "prior-findings-unavailable"), self._compute())

    def test_committed_repairs_scope_review_to_the_recorded_base(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base, worktree_dirty=False)
        self._commit("src/repair.py", "repaired\n")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(base, delta["base_sha"])
        self.assertEqual(["src/repair.py"], delta["changed_files"])
        self.assertEqual([self.finding], delta["prior_findings"])

        scope = self._scope_section(delta)
        flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
        self.assertEqual(
            [
                f"git diff {flags} {base}",
                f"git diff --cached {flags} {base}",
            ],
            self._scope_diff_commands(scope),
        )
        self.assertIn("src/repair.py", scope)

    def test_a_reopened_review_scopes_against_the_tree_it_reviewed(self) -> None:
        """A reopen supersedes its cycle, so it has to carry the same base.

        The reopen record is the newest repair evidence, and one written
        without the reviewed tree left the rereview with no base to narrow
        against at all — a full re-read after every reopen, which is the one
        case delta scoping exists for.
        """
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base, worktree_dirty=False)
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "materialized": True,
                    "source_cycle": 1,
                    "source_review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                },
            ),
        )
        self._commit("src/repair.py", "repaired\n")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(base, delta["base_sha"])
        self.assertEqual(["src/repair.py"], delta["changed_files"])
        self.assertIn("src/repair.py", self._scope_section(delta))

    def test_scope_covers_repairs_the_handoff_never_declared(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        # Uncommitted repair work: one edit to a tracked file and one brand new
        # file, of which the handoff declared only the first.
        (self.repo_root / "src" / "base.py").write_text("repaired\n", encoding="utf-8")
        (self.repo_root / "src" / "collateral.py").write_text(
            "collateral\n", encoding="utf-8"
        )
        self._record_repair_progress("src/base.py")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/base.py", "src/collateral.py"], delta["changed_files"])
        self.assertIn("src/collateral.py", self._scope_section(delta))

    def test_unusual_path_names_reach_the_scope_unquoted(self) -> None:
        # Without -z git C-quotes these, and the reviewer would be told to
        # reread files that do not exist.
        base = self._commit("src/café.md", "base\n")
        self._commit('src/say "hi".txt', "base\n")
        self._commit("src/flagged é.py", "base\n")
        base = self._git("rev-parse", "HEAD")
        self._git("update-index", "--assume-unchanged", "src/flagged é.py")
        self._record_repair_cycle(repo_head=base)
        (self.repo_root / "src" / "café.md").write_text("tracked\n", encoding="utf-8")
        (self.repo_root / "src" / 'say "hi".txt').write_text(
            "staged\n", encoding="utf-8"
        )
        self._git("add", 'src/say "hi".txt')
        (self.repo_root / "src" / "naïve.py").write_text("new\n", encoding="utf-8")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(
            [
                "src/café.md",
                "src/flagged é.py",
                "src/naïve.py",
                'src/say "hi".txt',
            ],
            delta["changed_files"],
        )

    def test_ignored_repair_file_stays_out_of_scope(self) -> None:
        base = self._commit(".gitignore", "generated/\n")
        self._record_repair_cycle(repo_head=base)
        generated = self.repo_root / "generated"
        generated.mkdir()
        (generated / "artifact.txt").write_text("repaired\n", encoding="utf-8")
        src = self.repo_root / "src"
        src.mkdir()
        (src / "repair.py").write_text("repaired\n", encoding="utf-8")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/repair.py"], delta["changed_files"])
        self.assertNotIn("generated/artifact.txt", self._scope_section(delta))

    def test_renamed_files_keep_both_paths_in_scope(self) -> None:
        base = self._commit("src/before.py", "moved\n")
        self._record_repair_cycle(repo_head=base)
        self._git("mv", "src/before.py", "src/after.py")
        self._git("commit", "-m", "rename")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(["src/after.py", "src/before.py"], delta["changed_files"])

    def test_dirty_baseline_keeps_a_restored_tracked_file_in_scope(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        # The reviewer read an uncommitted edit to src/base.py.
        (self.repo_root / "src" / "base.py").write_text(
            "reviewed wip\n", encoding="utf-8"
        )
        self._record_repair_cycle(
            repo_head=base,
            worktree_dirty=True,
            dirty_paths=["src/base.py"],
        )
        # The repair restored it to the committed content, so the current
        # base-to-worktree diff no longer mentions it at all.
        (self.repo_root / "src" / "base.py").write_text("base\n", encoding="utf-8")
        self._commit("src/repair.py", "repaired\n")
        self.assertEqual(
            "src/repair.py",
            self._git("diff", "--name-only", "--no-renames", base),
        )

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/base.py", "src/repair.py"], delta["changed_files"])
        self.assertIs(True, delta["baseline_dirty"])
        scope = self._scope_section(delta)
        self.assertIn("src/base.py", scope)
        self.assertIn("carried uncommitted changes", scope)

    def test_dirty_baseline_keeps_a_deleted_untracked_file_in_scope(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        scratch = self.repo_root / "src" / "scratch.py"
        scratch.write_text("reviewed scratch\n", encoding="utf-8")
        self._record_repair_cycle(
            repo_head=base,
            worktree_dirty=True,
            dirty_paths=["src/scratch.py"],
        )
        # The repair deleted the untracked file the reviewer saw, so neither
        # the diff nor the untracked scan reports it any more.
        scratch.unlink()

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/scratch.py"], delta["changed_files"])
        self.assertIn("src/scratch.py", self._scope_section(delta))

    def test_staged_repair_masked_by_a_restored_file_stays_in_scope(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        # The repair staged an edit and then restored the working file to its
        # committed bytes, so only the index still carries the change.
        target = self.repo_root / "src" / "base.py"
        target.write_text("repaired\n", encoding="utf-8")
        self._git("add", "src/base.py")
        target.write_text("base\n", encoding="utf-8")
        self.assertEqual("", self._git("diff", "--name-only", "--no-renames", base))

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(["src/base.py"], delta["changed_files"])
        self.assertIn("src/base.py", self._scope_section(delta))

    def test_rendered_scope_exposes_the_staged_patch_not_just_its_path(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        target = self.repo_root / "src" / "base.py"
        target.write_text("repaired\n", encoding="utf-8")
        self._git("add", "src/base.py")
        target.write_text("base\n", encoding="utf-8")

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        commands = self._scope_diff_commands(self._scope_section(delta))
        self.assertEqual(2, len(commands))
        patches = [self._git(*command.split()[1:]) for command in commands]
        # The repair restored the working file, so the base-to-worktree patch
        # is empty: only the second command carries the staged bytes.
        self.assertEqual("", patches[0])
        self.assertIn("src/base.py", patches[1])
        self.assertIn("+repaired", patches[1])

    def test_a_constant_textconv_cannot_empty_the_rendered_patches(
        self,
    ) -> None:
        (self.repo_root / ".gitattributes").write_text(
            "*.txt diff=const\n", encoding="utf-8"
        )
        (self.repo_root / "masked.txt").write_text("one\n", encoding="utf-8")
        self._git("add", ".gitattributes", "masked.txt")
        base = self._commit("staged.txt", "one\n")
        self._git("config", "diff.const.textconv", "printf CONST")
        self._record_repair_cycle(repo_head=base)
        (self.repo_root / "masked.txt").write_text(
            "worktree-repair\n", encoding="utf-8"
        )
        staged = self.repo_root / "staged.txt"
        staged.write_text("cached-repair\n", encoding="utf-8")
        self._git("add", "staged.txt")
        staged.write_text("one\n", encoding="utf-8")
        # The converter renders every content as the same constant text, so
        # git prints no patch at all without the presentation flags and this
        # regression cannot pass vacuously.
        self.assertEqual("", self._git("diff", "--no-renames", "--no-relative", base))
        self.assertEqual(
            "",
            self._git("diff", "--cached", "--no-renames", "--no-relative", base),
        )
        # Both name selectors still see past the converter, so the repairs are
        # in the file list either way.
        flags = list(review_session_body._DELTA_DIFF_FLAGS)
        self.assertEqual("masked.txt", self._git("diff", "--name-only", *flags, base))
        self.assertEqual(
            "staged.txt",
            self._git("diff", "--cached", "--name-only", *flags, base),
        )

        delta, fallback_reason = self._compute()

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(["masked.txt", "staged.txt"], delta["changed_files"])

        commands = self._scope_diff_commands(self._scope_section(delta))
        self.assertEqual(2, len(commands))
        patches = [self._git(*command.split()[1:]) for command in commands]
        self.assertIn("+worktree-repair", patches[0])
        self.assertIn("+cached-repair", patches[1])

    def test_nested_cwd_keeps_repository_root_repairs_in_scope(self) -> None:
        self._commit("nested/base.py", "base\n")
        self._commit("root_staged.py", "base\n")
        base = self._commit("root.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        self._git("config", "diff.relative", "true")
        (self.repo_root / "nested" / "base.py").write_text(
            "repaired\n", encoding="utf-8"
        )
        (self.repo_root / "root.py").write_text("repaired\n", encoding="utf-8")
        staged = self.repo_root / "root_staged.py"
        staged.write_text("repaired\n", encoding="utf-8")
        self._git("add", "root_staged.py")
        staged.write_text("base\n", encoding="utf-8")
        # Unpinned, both probes report only the nested subtree from that
        # directory, so this regression cannot pass vacuously.
        self.assertEqual(
            "base.py",
            self._git("-C", "nested", "diff", "--name-only", "--no-renames", base),
        )
        self.assertEqual(
            "",
            self._git(
                "-C",
                "nested",
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                base,
            ),
        )

        delta, fallback_reason = self._compute(cwd=self.repo_root / "nested")

        self.assertIsNone(fallback_reason)
        assert delta is not None
        self.assertEqual(
            ["nested/base.py", "root.py", "root_staged.py"],
            delta["changed_files"],
        )
        # The rendered commands must reach as far as that list does, run the
        # way a reviewer in the same nested directory would run them.
        commands = self._scope_diff_commands(self._scope_section(delta))
        self.assertEqual(2, len(commands))
        patches = [
            self._git("-C", "nested", *command.split()[1:]) for command in commands
        ]
        self.assertIn("root.py", patches[0])
        self.assertIn("root_staged.py", patches[1])

    def test_repair_to_an_index_flagged_file_stays_in_scope(self) -> None:
        for flag, rel in (
            ("--assume-unchanged", "src/assumed.py"),
            ("--skip-worktree", "src/skipped.py"),
        ):
            with self.subTest(flag=flag):
                base = self._commit(rel, "base\n")
                self._git("update-index", flag, rel)
                self._record_repair_cycle(repo_head=base)
                # The repair edits the flagged file, which git reports in no
                # diff at all.
                (self.repo_root / rel).write_text("repaired\n", encoding="utf-8")
                self.assertEqual(
                    "", self._git("diff", "--name-only", "--no-renames", base)
                )
                self.assertEqual("", self._git("diff", "--cached", "--name-only", base))

                delta, fallback_reason = self._compute()

                self.assertIsNone(fallback_reason)
                assert delta is not None
                self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
                self.assertIn(rel, delta["changed_files"])
                self.assertIn(rel, self._scope_section(delta))

    def test_repo_state_carries_an_index_flagged_path_into_the_next_scope(
        self,
    ) -> None:
        head = self._git("rev-parse", "HEAD")
        self._git("update-index", "--skip-worktree", "README.md")

        state = self._repo_state()

        # No diff can prove the flagged entry matches HEAD, so the reviewed
        # tree is recorded as dirty at that path and the next rereview keeps it.
        self.assertEqual(
            {
                "head": head,
                "worktree_dirty": True,
                "dirty_paths": ["README.md"],
            },
            state,
        )

    def test_dirty_baseline_without_path_evidence_is_full_scope(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base, worktree_dirty=True, dirty_paths=None)
        self._commit("src/repair.py", "repaired\n")

        delta, fallback_reason = self._compute()

        self.assertEqual("dirty-base-evidence-missing", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertNotIn("src/", self._scope_section(delta))

    def test_cycle_with_unknown_dirtiness_is_full_scope(self) -> None:
        base = self._commit("src/base.py", "base\n")
        # A cycle recorded before dirty-path evidence existed.
        self._record_repair_cycle(repo_head=base, worktree_dirty=None, dirty_paths=None)
        self._commit("src/repair.py", "repaired\n")

        delta, fallback_reason = self._compute()

        self.assertEqual("dirty-base-evidence-missing", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])

    def test_cycle_without_recorded_base_falls_back_to_full_scope(
        self,
    ) -> None:
        self._record_repair_cycle()
        self._record_repair_progress("src/repair.py")

        delta, fallback_reason = self._compute()

        self.assertEqual("no-recorded-base", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertNotIn("base_sha", delta)
        self.assertEqual([self.finding], delta["prior_findings"])
        self.assertNotIn("src/", self._scope_section(delta))

    def test_unknown_base_commit_falls_back_to_full_scope(self) -> None:
        self._record_repair_cycle(repo_head="0" * 40)
        self._record_repair_progress("src/repair.py")

        delta, fallback_reason = self._compute()

        self.assertEqual("base-unresolvable", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertNotIn("src/", self._scope_section(delta))

    def test_rewritten_history_falls_back_to_full_scope(self) -> None:
        keep = self._git("rev-parse", "HEAD")
        dropped = self._commit("src/dropped.py", "dropped\n")
        self._record_repair_cycle(repo_head=dropped)
        # A rebase/reset moves the recorded base off the current history while
        # the commit object itself still resolves.
        self._git("reset", "--hard", keep)
        self._commit("src/repair.py", "repaired\n")
        self._record_repair_progress("src/dropped.py")

        delta, fallback_reason = self._compute()

        self.assertEqual("base-not-ancestor", fallback_reason)
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        # The reviewer is given no file list at all, so neither the handoff's
        # stale path nor the file the repair really changed can be excluded.
        scope = self._scope_section(delta)
        self.assertIn("this scope is not narrowed", scope)
        self.assertNotIn("src/", scope)

    def test_failed_git_probes_fall_back_to_full_scope(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        self._commit("src/repair.py", "repaired\n")
        real_run_git = review_guard_git._run_git

        for prefix, reason in (
            (("diff", "--name-only"), "base-diff-failed"),
            (("diff", "--cached"), "index-diff-failed"),
            (("ls-files", "--others"), "untracked-scan-failed"),
            (("ls-files", "-v"), "index-flags-scan-failed"),
        ):
            with self.subTest(command=prefix):

                def fail_one(cwd, args, _prefix=prefix):
                    if tuple(args[: len(_prefix)]) == _prefix:
                        return subprocess.CompletedProcess(args, 128, "", "boom")
                    return real_run_git(cwd, args)

                with mock.patch.object(review_guard_git, "_run_git", fail_one):
                    delta, fallback_reason = self._compute()

                self.assertEqual(reason, fallback_reason)
                assert delta is not None
                self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
                self.assertNotIn("src/", self._scope_section(delta))
