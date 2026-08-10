"""Reusable repository evidence and recursive workspace inventory tests."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_session_loop import plans, repository_evidence, runner, workspaces
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)


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

    def test_inventory_prunes_approved_subtree_before_permission_error(self) -> None:
        visible = self.workspace / "visible"
        blocked = self.workspace / "blocked"
        init_git_repo(visible)
        blocked.mkdir()

        def fake_walk(root, *, topdown, onerror, followlinks):
            walk_root = Path(root)
            directory_names = ["blocked", "visible"]
            yield str(walk_root), directory_names, []
            if "blocked" in directory_names:
                onerror(
                    PermissionError(
                        1, "not permitted", str(walk_root / "blocked")
                    )
                )
            if "visible" in directory_names:
                yield str(walk_root / "visible"), [".git"], []

        with mock.patch.object(repository_evidence.os, "walk", fake_walk):
            discovered = repository_evidence.discover_git_worktrees(
                self.workspace,
                inventory_excludes=("blocked",),
            )
        self.assertEqual((visible.resolve(),), discovered)

        with mock.patch.object(repository_evidence.os, "walk", fake_walk):
            with self.assertRaisesRegex(
                repository_evidence.RepositoryEvidenceError,
                "workspace inventory unreadable",
            ):
                repository_evidence.discover_git_worktrees(self.workspace)

    def test_comparison_covers_tracked_staged_deleted_renamed_and_untracked(
        self,
    ) -> None:
        cases = ("tracked", "staged", "deleted", "renamed", "untracked")
        for case in cases:
            with self.subTest(case=case):
                repository = self.workspace / case
                init_git_repo(repository)
                before = repository_evidence.capture_workspace_snapshot(
                    self.workspace, self.plan_dir
                )
                readme = repository / "README.md"
                if case in ("tracked", "staged"):
                    readme.write_text(f"{case}\n", encoding="utf-8")
                    if case == "staged":
                        subprocess.run(
                            ["git", "add", "README.md"],
                            cwd=repository,
                            capture_output=True,
                            check=True,
                        )
                elif case == "deleted":
                    readme.unlink()
                elif case == "renamed":
                    readme.rename(repository / "renamed.md")
                else:
                    (repository / "new.txt").write_text(
                        "untracked\n", encoding="utf-8"
                    )
                after = repository_evidence.capture_workspace_snapshot(
                    self.workspace, self.plan_dir
                )
                comparison = repository_evidence.compare_workspace_snapshots(
                    before, after
                )
                self.assertIn(case, comparison["changed"])
                qualified = comparison["changed"][case]["qualified_paths"]
                self.assertTrue(qualified)
                if case == "renamed":
                    self.assertIn(f"{case}:README.md", qualified)
                    self.assertIn(f"{case}:renamed.md", qualified)


class WorkspaceWriteGuardClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace_root = root / "workspace"
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        init_multi_repo_workspace(self.workspace_root, specs)
        self.plan_dir = root / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=specs)
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        self.workspace = workspaces.resolve_workspace(
            self.workspace_root, registry
        )

    def test_guard_classifies_read_only_outside_and_unregistered_changes(
        self,
    ) -> None:
        comparison = {
            "changed": OrderedDict(
                (
                    (
                        "tundra_svc",
                        {"qualified_paths": ["tundra_svc:allowed.py"]},
                    ),
                    (
                        "tundra_OAK",
                        {"qualified_paths": ["tundra_OAK:readonly.py"]},
                    ),
                    (
                        "scratch",
                        {"qualified_paths": ["scratch:unknown.py"]},
                    ),
                )
            ),
            "added": ["new-repo"],
            "removed": ["old-repo"],
            "unavailable": {},
        }
        evidence = runner._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=plans.SessionScope(
                reads=("SVC", "OAK"), writes=("SVC",)
            ),
            comparison=comparison,
            exclude=(),
            include_ignored=False,
            inventory_excludes=("Library/Protected.dv",),
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual(["SVC"], evidence["authorized_aliases"])
        self.assertEqual(
            ["Library/Protected.dv"], evidence["inventory_excluded"]
        )
        self.assertEqual(
            [
                "unregistered-repository",
                "read-only-repository",
                "repository-added",
                "repository-removed",
            ],
            [violation["kind"] for violation in evidence["violations"]],
        )
        self.assertEqual(
            ["OAK:readonly.py"],
            evidence["violations"][1]["qualified_paths"],
        )

    def test_guard_fails_closed_for_unavailable_evidence(self) -> None:
        evidence = runner._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=plans.SessionScope(reads=("SVC",), writes=("SVC",)),
            comparison={
                "changed": {},
                "added": [],
                "removed": [],
                "unavailable": {
                    "tundra_svc": {
                        "after": {"reason": "git-command-failed"}
                    }
                },
            },
            exclude=(),
            include_ignored=False,
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual([], evidence["violations"])
        self.assertEqual(
            "git-command-failed",
            evidence["unavailable"]["tundra_svc"]["after"]["reason"],
        )
        missing = runner._workspace_snapshot_unavailable(
            OrderedDict(), required_repository_paths=("tundra_svc",)
        )
        self.assertEqual(
            "registered-repository-missing",
            missing["tundra_svc"]["reason"],
        )

    def test_exclusion_applies_inside_unregistered_repository(self) -> None:
        scratch = self.workspace_root / "scratch"
        nested = scratch / "inner"
        init_git_repo(scratch)
        init_git_repo(nested)

        unavailable_snapshot = runner._capture_workspace_write_guard_snapshot(
            workspace=self.workspace,
            plan_dir=self.plan_dir,
            exclude=(),
            include_ignored=False,
        )
        detail = runner._workspace_snapshot_unavailable(unavailable_snapshot)
        self.assertEqual(
            "untracked-node-unsupported",
            detail["scratch"]["reason"],
        )
        self.assertEqual("inner/", detail["scratch"]["path"])
        self.assertEqual(
            ": scratch:inner/ (untracked-node-unsupported)",
            runner._workspace_unavailable_summary(
                detail,
                workspace=self.workspace,
            ),
        )

        excluded_snapshot = runner._capture_workspace_write_guard_snapshot(
            workspace=self.workspace,
            plan_dir=self.plan_dir,
            exclude=("inner",),
            include_ignored=False,
        )

        self.assertNotIn(
            "scratch",
            runner._workspace_snapshot_unavailable(excluded_snapshot),
        )


if __name__ == "__main__":
    unittest.main()
