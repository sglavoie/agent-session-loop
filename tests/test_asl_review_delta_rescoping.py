"""Delta rescoping, repo state capture, and cycle records."""

from __future__ import annotations

from agent_session_loop import (
    plans,
    review,
    review_delta,
    review_guard,
    review_session_body,
    review_session_contract,
)
from tests.review_delta_scope_base import ReviewDeltaScopeBase


class ReviewDeltaRescopingTests(ReviewDeltaScopeBase):
    """Delta rescoping, repo state capture, and cycle records."""

    def test_rescoping_lists_an_untracked_file_added_after_the_session(
        self,
    ) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        original = self._pending_delta_body()
        self.assertNotIn("src/late.py", original)
        # Untracked files are absent from `git diff <base>`, so the static list
        # is the only thing that can tell the reviewer to read this one.
        (self.repo_root / "src" / "late.py").write_text("late\n", encoding="utf-8")

        refreshed = review_session_body.rescoped_review_session_body(
            original, self._compute()[0]
        )

        assert refreshed is not None
        self.assertIn("  - `src/late.py`", refreshed)
        # Identity, version marker, and the pending handoff survive intact.
        session_path = self.plan_dir / "session-3.md"
        session_path.write_text(refreshed, encoding="utf-8")
        self.assertTrue(
            review_session_contract.is_generated_review_session_file(session_path)
        )
        self.assertEqual(
            review_session_contract.generated_review_session_version(session_path),
            review_session_contract.generated_review_session_version(
                self.plan_dir / "session-2.md"
            ),
        )
        session = plans.SessionFile(
            session_id=plans.parse_session_id("3"), path=session_path
        )
        self.assertEqual("pending", plans.read_session_status(session).status)
        self.assertEqual(
            original.split("## Completion Contract", 1)[1],
            refreshed.split("## Completion Contract", 1)[1],
        )
        self.assertIn(f"- `{self.finding['id']}`", refreshed)

    def test_rescoping_widens_to_full_when_the_evidence_is_gone(self) -> None:
        base = self._commit("src/base.py", "base\n")
        self._record_repair_cycle(repo_head=base)
        self._commit("src/repair.py", "repaired\n")
        original = self._pending_delta_body()
        self.assertIn("  - `src/repair.py`", original)
        # Without the findings artifact the narrowed list can no longer be
        # shown to cover the repair work.
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()
        delta, fallback_reason = self._compute()
        self.assertEqual((None, "prior-findings-unavailable"), (delta, fallback_reason))

        refreshed = review_session_body.rescoped_review_session_body(original, delta)

        assert refreshed is not None
        self.assertIn("this scope is not narrowed", refreshed)
        self.assertNotIn(base, refreshed)
        self.assertNotIn("src/repair.py", refreshed)
        # The prior findings still have to be re-verified.
        self.assertIn(f"- `{self.finding['id']}`", refreshed)

    def test_a_full_scope_session_is_never_narrowed_by_a_refresh(self) -> None:
        self._record_repair_cycle(repo_head=self._commit("src/base.py", "b\n"))
        full_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
        )

        self.assertIsNone(
            review_session_body.rescoped_review_session_body(
                full_body, self._compute()[0]
            )
        )

    def test_an_unparseable_scope_section_cannot_be_re_scoped(self) -> None:
        self._record_repair_cycle(repo_head=self._commit("src/base.py", "b\n"))
        mangled = self._pending_delta_body().replace(
            f"{review_session_body.REVIEW_SCOPE_DETAIL_HEADING}\n", "", 1
        )

        with self.assertRaises(plans.PlanError):
            review_session_body.rescoped_review_session_body(
                mangled, self._compute()[0]
            )

    def test_repo_state_is_derived_from_the_guard_fingerprint(self) -> None:
        head = self._git("rev-parse", "HEAD")

        self.assertEqual(
            {"head": head, "worktree_dirty": False, "dirty_paths": []},
            self._repo_state(),
        )

        (self.repo_root / "scratch.txt").write_text("wip\n", encoding="utf-8")
        (self.repo_root / "README.md").write_text("edited\n", encoding="utf-8")
        dirty = self._repo_state()
        assert dirty is not None
        self.assertEqual(
            {
                "head": head,
                "worktree_dirty": True,
                "dirty_paths": ["README.md", "scratch.txt"],
            },
            dirty,
        )
        self.assertIsNone(review_guard._review_repo_state({"git": False}))

    def test_repo_state_records_an_index_only_dirty_path(self) -> None:
        head = self._git("rev-parse", "HEAD")
        readme = self.repo_root / "README.md"
        readme.write_text("staged\n", encoding="utf-8")
        self._git("add", "README.md")
        readme.write_text("test repo\n", encoding="utf-8")

        # Nothing the base-to-worktree diff can see, but a commit would still
        # publish the staged bytes, so the next rereview must cover the path.
        self.assertEqual("", self._git("diff", "--name-only", head))
        self.assertEqual(
            {
                "head": head,
                "worktree_dirty": True,
                "dirty_paths": ["README.md"],
            },
            self._repo_state(),
        )

    def test_cycle_record_carries_the_reviewed_repo_head(self) -> None:
        head = self._git("rev-parse", "HEAD")

        cycle, recorded = self._advance_cycle(self._repo_state())

        self.assertEqual(head, cycle["repo_head"])
        self.assertIs(False, cycle["worktree_dirty"])
        self.assertEqual([], cycle["dirty_paths"])
        self.assertEqual(head, recorded["repo_head"])

    def test_cycle_record_carries_the_reviewed_dirty_paths(self) -> None:
        (self.repo_root / "scratch.txt").write_text("wip\n", encoding="utf-8")

        cycle, recorded = self._advance_cycle(self._repo_state())

        self.assertIs(True, cycle["worktree_dirty"])
        self.assertEqual(["scratch.txt"], cycle["dirty_paths"])
        self.assertEqual(["scratch.txt"], recorded["dirty_paths"])
        # The reader of that record agrees on the key it was written under.
        self.assertEqual(
            ["scratch.txt"], review_delta._reviewed_baseline_dirty_paths(cycle)
        )
