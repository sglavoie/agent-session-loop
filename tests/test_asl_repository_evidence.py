"""Reusable repository evidence and recursive workspace inventory tests."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_session_loop import repository_evidence, runner
from tests.support import init_git_repo


class RepositoryEvidenceParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repository = self.root / "repository"
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        init_git_repo(self.repository)

    def test_runner_wrapper_and_extracted_engine_are_byte_for_byte_equal(
        self,
    ) -> None:
        (self.repository / "README.md").write_text("working tree\n", encoding="utf-8")
        staged = self.repository / "staged.txt"
        staged.write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "staged.txt"],
            cwd=self.repository,
            capture_output=True,
            check=True,
        )
        (self.repository / "untracked.txt").write_text(
            "untracked\n", encoding="utf-8"
        )

        extracted = repository_evidence.capture_repository_fingerprint(
            self.repository, self.plan_dir
        )
        compatibility = runner._capture_review_repo_fingerprint(
            self.repository, self.plan_dir
        )

        self.assertEqual(extracted, compatibility)
        self.assertEqual(
            repository_evidence.compare_repository_fingerprints(
                extracted, compatibility
            ),
            runner._review_guard_changed_paths(extracted, compatibility),
        )


class WorkspaceInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()

    def test_inventory_finds_ordinary_and_linked_worktrees_only(self) -> None:
        ordinary = self.workspace / "nested" / "ordinary"
        source = self.workspace / "source"
        linked = self.workspace / "linked"
        init_git_repo(ordinary)
        init_git_repo(source)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(linked)],
            cwd=source,
            capture_output=True,
            check=True,
        )

        # Git metadata may contain repository-looking directories but inventory
        # must prune it entirely.
        (source / ".git" / "phantom" / ".git").mkdir(parents=True)
        outside = self.root / "outside"
        init_git_repo(outside)
        (self.workspace / "outside-link").symlink_to(
            outside, target_is_directory=True
        )

        discovered = repository_evidence.discover_git_worktrees(self.workspace)

        self.assertEqual(
            tuple(path.resolve() for path in (linked, ordinary, source)),
            discovered,
        )
        self.assertTrue((linked / ".git").is_file())
        self.assertNotIn(outside, discovered)

    def test_snapshot_and_vector_comparison_are_ordered_and_qualified(
        self,
    ) -> None:
        alpha = self.workspace / "alpha"
        beta = self.workspace / "group" / "beta"
        init_git_repo(beta)
        init_git_repo(alpha)
        before = repository_evidence.capture_workspace_snapshot(
            self.workspace, self.plan_dir
        )

        (alpha / "README.md").write_text("changed\n", encoding="utf-8")
        (beta / "new.txt").write_text("new\n", encoding="utf-8")
        after = repository_evidence.capture_workspace_snapshot(
            self.workspace, self.plan_dir
        )
        comparison = repository_evidence.compare_workspace_snapshots(before, after)

        self.assertIsInstance(before, OrderedDict)
        self.assertEqual(["alpha", "group/beta"], list(before))
        self.assertRegex(before["alpha"]["head"], r"^[0-9a-f]{40,64}$")
        self.assertEqual(["alpha", "group/beta"], list(comparison["changed"]))
        self.assertEqual(
            ["alpha:README.md"],
            comparison["changed"]["alpha"]["qualified_paths"],
        )
        self.assertEqual(
            ["group/beta:new.txt"],
            comparison["changed"]["group/beta"]["qualified_paths"],
        )
        self.assertEqual([], comparison["added"])
        self.assertEqual([], comparison["removed"])
        self.assertEqual({}, comparison["unavailable"])

    def test_vector_reports_added_removed_and_unavailable_repositories(
        self,
    ) -> None:
        clean = {"git": True, "head": "a"}
        unavailable = {
            "git": True,
            "unavailable": {"reason": "git-command-failed", "argv": ["diff"]},
        }
        before = OrderedDict(
            (
                ("gone", clean),
                ("steady", clean),
                ("unavailable", clean),
                ("vanished", clean),
            )
        )
        after = OrderedDict(
            (
                ("added", clean),
                ("steady", clean),
                ("unavailable", unavailable),
                ("vanished", {"git": False}),
            )
        )

        comparison = repository_evidence.compare_workspace_snapshots(before, after)

        self.assertEqual(["added"], comparison["added"])
        self.assertEqual(["gone"], comparison["removed"])
        self.assertEqual(
            ["unavailable", "vanished"], list(comparison["unavailable"])
        )
        self.assertEqual(
            "git-command-failed",
            comparison["unavailable"]["unavailable"]["after"]["reason"],
        )
        self.assertEqual(
            "repository-unavailable",
            comparison["unavailable"]["vanished"]["after"]["reason"],
        )

    def test_inventory_has_an_explicit_bound_over_dozen_scale_workspaces(
        self,
    ) -> None:
        repositories = []
        for index in range(24):
            repository = self.workspace / f"repo-{index:02d}"
            init_git_repo(repository)
            repositories.append(repository)

        self.assertEqual(
            tuple(repository.resolve() for repository in repositories),
            repository_evidence.discover_git_worktrees(
                self.workspace, max_repositories=24
            ),
        )
        with self.assertRaisesRegex(
            repository_evidence.RepositoryEvidenceError,
            "23-repository bound",
        ):
            repository_evidence.discover_git_worktrees(
                self.workspace, max_repositories=23
            )


if __name__ == "__main__":
    unittest.main()
