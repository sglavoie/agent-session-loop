"""Reusable repository evidence and recursive workspace inventory tests."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

from agent_session_loop import plans, repository_evidence, runner, workspaces
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)


def clone_git_repo(source: Path, target: Path) -> None:
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        text=True,
        capture_output=True,
        check=True,
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
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")

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

    def test_fingerprint_records_ref_mutation_and_classifies_rewritten_head(
        self,
    ) -> None:
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        readme = self.repository / "README.md"
        readme.write_text("first child\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "first child"],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        )
        old_tip = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        before = repository_evidence.capture_repository_fingerprint(
            self.repository, self.plan_dir
        )

        subprocess.run(
            ["git", "branch", "backup/pre-rewrite", old_tip],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "reset", "--hard", base],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        )
        readme.write_text("replacement child\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "replacement child"],
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=True,
        )
        after = repository_evidence.capture_repository_fingerprint(
            self.repository, self.plan_dir
        )

        changes = repository_evidence.compare_repository_fingerprints(before, after)
        transition = repository_evidence.classify_head_transition(
            self.repository, before["head"], after["head"]
        )

        self.assertTrue(changes["head_changed"])
        self.assertEqual(old_tip, changes["head_before"])
        self.assertEqual(after["head"], changes["head_after"])
        self.assertEqual(
            ["refs/heads/backup/pre-rewrite"],
            [entry["ref"] for entry in changes["refs_added"]],
        )
        self.assertEqual("rewrite", transition["relation"])
        self.assertEqual(1, transition["commits_added"])
        self.assertEqual(1, transition["commits_removed"])
        self.assertTrue(transition["history_rewrite"])


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
        (self.workspace / "outside-link").symlink_to(outside, target_is_directory=True)

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
        self.assertEqual(["unavailable", "vanished"], list(comparison["unavailable"]))
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

    def test_inventory_prunes_hidden_subtree_before_permission_error(self) -> None:
        visible = self.workspace / "visible"
        blocked = self.workspace / ".cache"
        init_git_repo(visible)
        blocked.mkdir()

        def fake_walk(root, *, topdown, onerror, followlinks):
            walk_root = Path(root)
            directory_names = [".cache", "visible"]
            yield str(walk_root), directory_names, []
            if ".cache" in directory_names:
                onerror(PermissionError(1, "not permitted", str(walk_root / ".cache")))
            if "visible" in directory_names:
                yield str(walk_root / "visible"), [".git"], []

        with mock.patch.object(repository_evidence.os, "walk", fake_walk):
            inventory = repository_evidence.discover_git_worktree_inventory(
                self.workspace
            )
        self.assertEqual((visible.resolve(),), inventory.repositories)
        self.assertEqual(1, inventory.skipped_hidden_subtrees)

        with mock.patch.object(repository_evidence.os, "walk", fake_walk):
            self.assertEqual(
                (visible.resolve(),),
                repository_evidence.discover_git_worktrees(self.workspace),
            )

    def test_inventory_skips_hidden_invalid_git_marker_and_counts_root(
        self,
    ) -> None:
        visible = self.workspace / "visible"
        init_git_repo(visible)
        malformed = self.workspace / ".cache" / "uv" / "sdists-v5" / ".git"
        malformed.parent.mkdir(parents=True)
        malformed.write_text("not Git metadata\n", encoding="utf-8")

        inventory = repository_evidence.discover_git_worktree_inventory(self.workspace)

        self.assertEqual((visible.resolve(),), inventory.repositories)
        self.assertEqual(1, inventory.skipped_hidden_subtrees)

    def test_inventory_keeps_visible_invalid_git_marker_fail_closed(self) -> None:
        malformed = self.workspace / "visible" / ".git"
        malformed.parent.mkdir()
        malformed.write_text("not Git metadata\n", encoding="utf-8")

        with self.assertRaisesRegex(
            repository_evidence.RepositoryEvidenceError,
            "repository visible could not be inspected",
        ):
            repository_evidence.discover_git_worktree_inventory(self.workspace)

    def test_inventory_inspects_hidden_workspace_root(self) -> None:
        hidden_workspace = self.root / ".workspace"
        init_git_repo(hidden_workspace)

        inventory = repository_evidence.discover_git_worktree_inventory(
            hidden_workspace
        )

        self.assertEqual((hidden_workspace.resolve(),), inventory.repositories)
        self.assertEqual(0, inventory.skipped_hidden_subtrees)

    def test_registered_snapshot_inspects_declared_hidden_repository(self) -> None:
        repository = self.workspace / ".worktrees" / "target"
        init_git_repo(repository)

        snapshot = repository_evidence.capture_registered_snapshot(
            self.workspace,
            self.plan_dir,
            (".worktrees/target",),
        )

        self.assertEqual([".worktrees/target"], list(snapshot))
        self.assertTrue(snapshot[".worktrees/target"]["git"])

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
                    (repository / "new.txt").write_text("untracked\n", encoding="utf-8")
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


class RegisteredSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()

    def test_snapshot_covers_registered_paths_only(self) -> None:
        alpha = self.workspace / "alpha"
        beta = self.workspace / "group" / "beta"
        sibling = self.workspace / "sibling"
        init_git_repo(alpha)
        init_git_repo(beta)
        init_git_repo(sibling)
        (sibling / "collateral.txt").write_text("other run\n", encoding="utf-8")

        snapshot = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["group/beta", "alpha"]
        )

        self.assertIsInstance(snapshot, OrderedDict)
        self.assertEqual(["alpha", "group/beta"], list(snapshot))
        self.assertRegex(snapshot["alpha"]["head"], r"^[0-9a-f]{40,64}$")

    def test_missing_registered_path_is_omitted_and_compares_as_removed(
        self,
    ) -> None:
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        (self.workspace / "not-a-directory").write_text("file\n", encoding="utf-8")
        registered = ["alpha", "never-cloned", "not-a-directory"]

        snapshot = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, registered
        )

        self.assertEqual(["alpha"], list(snapshot))
        comparison = repository_evidence.compare_workspace_snapshots(
            OrderedDict(
                (("alpha", snapshot["alpha"]), ("never-cloned", {"git": True}))
            ),
            snapshot,
        )
        self.assertEqual(["never-cloned"], comparison["removed"])

    def test_registered_path_that_stops_being_a_worktree_fails_closed(
        self,
    ) -> None:
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        (alpha / ".git").rename(self.root / "detached-git")

        snapshot = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        self.assertEqual(
            {
                "git": True,
                "unavailable": {
                    "reason": "repository-disappeared",
                    "argv": ["rev-parse", "--show-toplevel"],
                },
            },
            snapshot["alpha"],
        )

    def test_a_symlink_to_an_identical_sibling_cannot_compare_cleanly(
        self,
    ) -> None:
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        twin = self.root / "twin"
        clone_git_repo(alpha, twin)
        before = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )
        twin_snapshot = repository_evidence.capture_registered_snapshot(
            self.root, self.plan_dir, ["twin"]
        )
        self.assertEqual(before["alpha"]["head"], twin_snapshot["twin"]["head"])
        self.assertEqual(
            before["alpha"]["diff_sha256"], twin_snapshot["twin"]["diff_sha256"]
        )

        shutil.rmtree(alpha)
        alpha.symlink_to(twin)
        after = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        self.assertEqual([], list(after))
        comparison = repository_evidence.compare_workspace_snapshots(before, after)
        self.assertEqual(["alpha"], comparison["removed"])
        self.assertEqual({}, comparison["changed"])

    def test_a_symlinked_parent_component_is_not_the_declared_node(
        self,
    ) -> None:
        init_git_repo(self.workspace / "real-group" / "beta")
        (self.workspace / "group").symlink_to(self.workspace / "real-group")

        snapshot = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["group/beta", "real-group/beta"]
        )

        self.assertEqual(["real-group/beta"], list(snapshot))

    def test_fingerprints_are_byte_identical_to_the_walking_capture(
        self,
    ) -> None:
        alpha = self.workspace / "alpha"
        beta = self.workspace / "group" / "beta"
        init_git_repo(alpha)
        init_git_repo(beta)
        (alpha / "README.md").write_text("changed\n", encoding="utf-8")
        (alpha / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        (beta / "excluded.txt").write_text("excluded\n", encoding="utf-8")
        (beta / "new.txt").write_text("new\n", encoding="utf-8")
        exclude = {"group/beta": ("excluded.txt",)}

        walked = repository_evidence.capture_workspace_snapshot(
            self.workspace,
            self.plan_dir,
            exclude=exclude,
            default_exclude=("untracked.txt",),
        )
        registered = repository_evidence.capture_registered_snapshot(
            self.workspace,
            self.plan_dir,
            ["alpha", "group/beta"],
            exclude=exclude,
            default_exclude=("untracked.txt",),
        )

        self.assertEqual(walked, registered)
        self.assertEqual(list(walked), list(registered))


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
        self.workspace = workspaces.resolve_workspace(self.workspace_root, registry)

    def test_guard_classifies_read_only_and_topology_changes(self) -> None:
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
                )
            ),
            "added": ["new-repo"],
            "removed": ["old-repo"],
            "unavailable": {},
        }
        evidence = runner._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=plans.SessionScope(reads=("SVC", "OAK"), writes=("SVC",)),
            comparison=comparison,
            exclude=(),
            include_ignored=False,
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual(["SVC"], evidence["authorized_aliases"])
        self.assertNotIn("inventory_excluded", evidence)
        self.assertEqual(
            [
                "read-only-repository",
                "repository-added",
                "repository-removed",
            ],
            [violation["kind"] for violation in evidence["violations"]],
        )
        self.assertEqual(
            ["OAK:readonly.py"],
            evidence["violations"][0]["qualified_paths"],
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
                    "tundra_svc": {"after": {"reason": "git-command-failed"}}
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

    def test_guard_snapshot_covers_registered_repositories_only(self) -> None:
        scratch = self.workspace_root / "scratch"
        init_git_repo(scratch)
        init_git_repo(scratch / "inner")

        snapshot = runner._capture_workspace_write_guard_snapshot(
            workspace=self.workspace,
            plan_dir=self.plan_dir,
            exclude=(),
            include_ignored=False,
        )

        self.assertEqual(["tundra_OAK", "tundra_svc"], list(snapshot))
        self.assertEqual({}, runner._workspace_snapshot_unavailable(snapshot))

    def test_repair_comparison_suppresses_registry_delta_only(self) -> None:
        comparison = {
            "changed": {},
            "added": ["surprise-repository", "tundra_OAK"],
            "removed": ["deregistered-repository", "tundra_svc"],
            "unavailable": {},
        }

        repaired, suppressed = runner._workspace_scope_repair_comparison(
            comparison,
            frozenset({"tundra_svc", "tundra_OAK"}),
        )

        self.assertEqual(["tundra_OAK", "deregistered-repository"], suppressed)
        self.assertEqual(["surprise-repository"], repaired["added"])
        self.assertEqual(["tundra_svc"], repaired["removed"])


if __name__ == "__main__":
    unittest.main()
