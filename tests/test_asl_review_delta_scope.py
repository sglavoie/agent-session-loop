"""Fail-closed, registered all-or-nothing and narrowed delta scope."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    review,
    review_continuation,
    review_delta,
    review_session_body,
    verification_models,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
from tests.asl_review_delta_support import DeltaHarness
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _structured_review_finding,
    git_stdout,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)


class FailClosedScopeTests(DeltaHarness):
    """Every probe that can fail degrades to full scope under its own name."""

    def _degrades_with(self, reason: str) -> None:
        delta, fallback = self.compute()
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertEqual([self.finding], delta["prior_findings"])
        self.assertEqual(reason, fallback)

    def test_a_missing_recorded_base_degrades(self) -> None:
        self.record_repair_cycle(repo_head=None)
        self._degrades_with("no-recorded-base")

    def test_a_dirty_base_with_no_path_evidence_degrades(self) -> None:
        self.record_repair_cycle(worktree_dirty=True, dirty_paths=None)
        self._degrades_with("dirty-base-evidence-missing")

    def test_an_unresolvable_base_degrades(self) -> None:
        self.record_repair_cycle(repo_head="0" * 40)
        self._degrades_with("base-unresolvable")

    def test_a_base_that_is_no_longer_an_ancestor_degrades(self) -> None:
        # A rebase, reset or amend moves the recorded base off the history the
        # repair actually landed on; diffing against it would misreport scope.
        base = self.head()
        (self.repo_root / "a.py").write_text("one\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("commit", "-m", "a")
        orphan = self.head()
        self.git("reset", "--hard", base)
        self.record_repair_cycle(repo_head=orphan)
        self._degrades_with("base-not-ancestor")


class RegisteredScopeIsAllOrNothingTests(unittest.TestCase):
    """One unprovable repository degrades the whole workspace, not just itself.

    ``tests/test_asl_cli.py`` owns the combined delta's happy path. What is
    asserted here is the part that has no happy path to observe: a narrowed
    scope covering three of four repositories would read like a complete file
    list while saying nothing at all about the fourth.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.workspace_root = root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root, specs=self.SPECS
        )
        self.plan_dir = root / "plan"
        write_multi_repo_plan(self.plan_dir, ("complete",), specs=self.SPECS)
        self.finding = _structured_review_finding()
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "# Final review\n\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": [self.finding]}
            ),
            encoding="utf-8",
        )
        context = verification_models.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        assert context.workspace is not None
        self.workspace = context.workspace
        self.event_path = root / "events.jsonl"

    def _record_bases(self, bases: list[dict[str, Any]]) -> None:
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "disposition": (
                        review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                    ),
                    "repositories": bases,
                },
            ),
        )

    def _head(self, alias: str) -> str:
        return git_stdout(self.repositories[alias], "rev-parse", "HEAD")

    def _compute(self) -> tuple[dict[str, Any] | None, str | None]:
        return review_delta._compute_registered_review_delta_context(
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            workspace=self.workspace,
        )

    def _degrades_with(self, reason: str) -> None:
        delta, fallback = self._compute()
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertEqual([self.finding], delta["prior_findings"])
        self.assertNotIn("repositories", delta)
        self.assertEqual(reason, fallback)

    def test_a_cycle_that_recorded_no_repository_bases_degrades(self) -> None:
        # A single-repository cycle carries `repo_head` and no `repositories`
        # list at all, which is exactly the record a workspace cannot narrow
        # against.
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "disposition": (
                        review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                    ),
                    "repo_head": self._head("SVC"),
                },
            ),
        )
        self._degrades_with("no-recorded-repository-bases")

    def test_an_empty_recorded_base_list_still_names_the_first_repository(
        self,
    ) -> None:
        # An empty list is a list: the loop runs and the first registry entry
        # is the one that cannot be proved, so the reason names it rather than
        # reporting the vaguer "no bases recorded".
        self._record_bases([])
        self._degrades_with("base-missing:SVC")

    def test_one_repository_missing_its_base_degrades_the_whole_workspace(
        self,
    ) -> None:
        # SVC is fully provable; OAK is not. The scope narrows for neither.
        self._record_bases(
            [
                {
                    "alias": "SVC",
                    "head": self._head("SVC"),
                    "worktree_dirty": False,
                    "dirty_paths": [],
                }
            ]
        )
        self._degrades_with("base-missing:OAK")

    def test_one_unresolvable_base_degrades_the_whole_workspace(self) -> None:
        self._record_bases(
            [
                {
                    "alias": alias,
                    "head": self._head(alias) if alias == "SVC" else "0" * 40,
                    "worktree_dirty": False,
                    "dirty_paths": [],
                }
                for alias in ("SVC", "OAK")
            ]
        )
        self._degrades_with("base-unresolvable:OAK")

    def test_one_unprovable_dirty_base_degrades_the_whole_workspace(self) -> None:
        self._record_bases(
            [
                {
                    "alias": "SVC",
                    "head": self._head("SVC"),
                    "worktree_dirty": False,
                    "dirty_paths": [],
                },
                {"alias": "OAK", "head": self._head("OAK"), "worktree_dirty": True},
            ]
        )
        self._degrades_with("dirty-base-evidence-missing:OAK")


class NarrowedScopeTests(DeltaHarness):
    """What a narrowed scope is allowed to leave out — which is very little."""

    def test_the_changed_set_unions_worktree_index_untracked_and_baseline(
        self,
    ) -> None:
        base = self.head()
        (self.repo_root / "committed.py").write_text("one\n", encoding="utf-8")
        self.git("add", "committed.py")
        self.git("commit", "-m", "committed repair")
        (self.repo_root / "staged.py").write_text("two\n", encoding="utf-8")
        self.git("add", "staged.py")
        (self.repo_root / "untracked.py").write_text("three\n", encoding="utf-8")
        (self.repo_root / "README.md").write_text("dirty\n", encoding="utf-8")
        self.record_repair_cycle(
            repo_head=base,
            worktree_dirty=True,
            dirty_paths=["reviewed-dirty.py"],
        )

        delta, fallback = self.compute()
        assert delta is not None
        self.assertIsNone(fallback)
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(base, delta["base_sha"])
        self.assertTrue(delta["baseline_dirty"])
        self.assertEqual(
            [
                "README.md",
                "committed.py",
                "reviewed-dirty.py",
                "staged.py",
                "untracked.py",
            ],
            delta["changed_files"],
        )

    def test_an_assume_unchanged_entry_joins_the_scope_wholesale(self) -> None:
        # Git compares such an entry against nothing, so the same blindness
        # that hides the repair makes it impossible to tell whether one
        # happened. The path joins the scope rather than being trusted.
        base = self.head()
        (self.repo_root / "hidden.py").write_text("one\n", encoding="utf-8")
        self.git("add", "hidden.py")
        self.git("commit", "-m", "hidden")
        self.git("update-index", "--assume-unchanged", "hidden.py")
        self.record_repair_cycle(repo_head=base)

        delta, fallback = self.compute()
        assert delta is not None
        self.assertIsNone(fallback)
        self.assertIn("hidden.py", delta["changed_files"])


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
