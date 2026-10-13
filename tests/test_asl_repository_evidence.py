"""Reusable repository evidence and recursive workspace inventory tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    external_write_evidence,
    handoff_guards,
    plans_registry,
    plans_session_scope,
    repository_evidence,
    review_guard,
    workspace_evidence,
    workspaces,
)
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

    def test_worktree_cleanup_removes_only_its_own_stale_registration(self) -> None:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repository, text=True
        ).strip()
        owned = repository_evidence.add_detached_worktree(
            self.repository, self.root / "owned", head
        )
        other = repository_evidence.add_detached_worktree(
            self.repository, self.root / "other", head
        )
        shutil.rmtree(owned)
        shutil.rmtree(other)

        self.assertTrue(repository_evidence.remove_worktree(self.repository, owned))
        self.assertEqual(
            (other.resolve(),), repository_evidence.linked_worktrees(self.repository)
        )

    def test_worktree_cleanup_cannot_claim_success_when_inventory_fails(self) -> None:
        commands: list[list[str]] = []

        def git_unavailable(
            root: Path, argv: list[str]
        ) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "Git unavailable")

        self.assertFalse(
            repository_evidence.remove_worktree(
                self.repository, self.root / "owned", _run_git_fn=git_unavailable
            )
        )
        self.assertEqual(["worktree", "list", "--porcelain"], commands[-1])

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
        compatibility = review_guard._capture_review_repo_fingerprint(
            self.repository, self.plan_dir
        )

        self.assertEqual(extracted, compatibility)
        self.assertEqual(
            repository_evidence.compare_repository_fingerprints(
                extracted, compatibility
            ),
            review_guard._review_guard_changed_paths(extracted, compatibility),
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

    def _git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def test_snapshot_covers_a_linked_worktree_of_a_registered_repository(
        self,
    ) -> None:
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        linked = self.root / "alpha-hardening"
        self._git(alpha, "worktree", "add", "-b", "hardening", str(linked))

        snapshot = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        key = repository_evidence.linked_worktree_key("alpha", linked.resolve())
        self.assertEqual(["alpha", key], list(snapshot))
        self.assertEqual(
            ("alpha", linked.resolve().as_posix()),
            (repository_evidence.split_linked_worktree_key(key)),
        )
        self.assertRegex(snapshot[key]["head"], r"^[0-9a-f]{40,64}$")

    def test_a_moved_linked_worktree_head_is_evidence(self) -> None:
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        linked = self.root / "alpha-hardening"
        self._git(alpha, "worktree", "add", "-b", "hardening", str(linked))
        before = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        (linked / "README.md").write_text("hardened\n", encoding="utf-8")
        self._git(linked, "commit", "-am", "harden")
        after = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        comparison = repository_evidence.compare_workspace_snapshots(before, after)
        key = repository_evidence.linked_worktree_key("alpha", linked.resolve())
        moved = comparison["changed"][key]
        self.assertNotEqual(moved["head_before"], moved["head_after"])
        # The registered root shares the object store, so it sees the ref move
        # but not the checkout the commit actually landed on.
        self.assertEqual(
            comparison["changed"]["alpha"]["head_before"],
            comparison["changed"]["alpha"]["head_after"],
        )

    def test_history_annotation_resolves_a_linked_worktree_through_its_owner(
        self,
    ) -> None:
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        linked = self.root / "alpha-hardening"
        self._git(alpha, "worktree", "add", "-b", "hardening", str(linked))
        before = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        (linked / "README.md").write_text("hardened\n", encoding="utf-8")
        self._git(linked, "commit", "-am", "harden")
        after = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        comparison = repository_evidence.compare_workspace_snapshots(before, after)
        workspace_evidence._annotate_history_transitions(
            comparison, repository_roots={"alpha": alpha}
        )

        key = repository_evidence.linked_worktree_key("alpha", linked.resolve())
        self.assertNotIn(key, comparison["unavailable"])
        self.assertEqual(
            "fast-forward", comparison["changed"][key]["head_transition"]["relation"]
        )

    def test_unchanged_linked_worktrees_are_classified_without_a_root(self) -> None:
        """ASI-19: an unmoved tip is conclusive evidence, not missing evidence.

        A shared ignore configuration moves every checkout of the repository at
        once, so each linked worktree lands in ``changed`` with its HEAD exactly
        where it was. Nothing here needs an ancestry probe, and the annotator
        used to report all of them ``repository-scope-unresolved`` anyway
        because it demanded a checkout root before it would say so.
        """
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        first = self.root / "alpha-one"
        second = self.root / "alpha-two"
        self._git(alpha, "worktree", "add", "-b", "one", str(first))
        self._git(alpha, "worktree", "add", "-b", "two", str(second))
        before = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        (alpha / ".git" / "info" / "exclude").write_text("*.log\n", encoding="utf-8")
        after = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        comparison = repository_evidence.compare_workspace_snapshots(before, after)
        keys = [
            repository_evidence.linked_worktree_key("alpha", path.resolve())
            for path in (first, second)
        ]
        for key in keys:
            self.assertIn(key, comparison["changed"])
            changes = comparison["changed"][key]
            self.assertEqual(changes["head_before"], changes["head_after"])

        # The owning alias is deliberately absent from the map, which is the
        # only condition the old fail-closed branch needed.
        workspace_evidence._annotate_history_transitions(
            comparison, repository_roots={}
        )

        for key in keys:
            self.assertNotIn(key, comparison["unavailable"])
            self.assertEqual(
                "unchanged",
                comparison["changed"][key]["head_transition"]["relation"],
            )

    def test_a_moved_tip_with_no_resolvable_root_stays_unavailable(self) -> None:
        """The fail-closed branch still covers the case that needs a probe."""
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        linked = self.root / "alpha-hardening"
        self._git(alpha, "worktree", "add", "-b", "hardening", str(linked))
        before = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        (linked / "README.md").write_text("hardened\n", encoding="utf-8")
        self._git(linked, "commit", "-am", "harden")
        after = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha"]
        )

        comparison = repository_evidence.compare_workspace_snapshots(before, after)
        workspace_evidence._annotate_history_transitions(
            comparison, repository_roots={}
        )

        key = repository_evidence.linked_worktree_key("alpha", linked.resolve())
        self.assertEqual(
            "repository-scope-unresolved",
            comparison["unavailable"][key]["after"]["reason"],
        )

    def test_a_registered_sibling_worktree_keeps_its_own_key(self) -> None:
        alpha = self.workspace / "alpha"
        init_git_repo(alpha)
        sibling = self.workspace / "alpha-hardening"
        self._git(alpha, "worktree", "add", "-b", "hardening", str(sibling))

        snapshot = repository_evidence.capture_registered_snapshot(
            self.workspace, self.plan_dir, ["alpha", "alpha-hardening"]
        )

        self.assertEqual(["alpha", "alpha-hardening"], list(snapshot))

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
        registry = plans_registry.read_repository_registry(self.plan_dir)
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
        evidence = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=plans_session_scope.SessionScope(
                reads=("SVC", "OAK"), writes=("SVC",)
            ),
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

    def test_guard_judges_a_linked_worktree_by_its_owners_alias(self) -> None:
        writable = repository_evidence.linked_worktree_key(
            "tundra_svc", "/tmp/svc-hardening"
        )
        read_only = repository_evidence.linked_worktree_key(
            "tundra_OAK", "/tmp/oak-hardening"
        )
        evidence = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=plans_session_scope.SessionScope(
                reads=("SVC", "OAK"), writes=("SVC",)
            ),
            comparison={
                "changed": OrderedDict(
                    (
                        (writable, {"qualified_paths": ["allowed.py"]}),
                        (read_only, {"qualified_paths": ["readonly.py"]}),
                    )
                ),
                "added": [writable],
                "removed": [read_only],
                "unavailable": {},
            },
            exclude=(),
            include_ignored=False,
        )

        self.assertEqual(["SVC"], evidence["authorized_aliases"])
        self.assertEqual("tundra_svc", evidence["changed"][writable]["worktree_of"])
        self.assertEqual(
            ["SVC:allowed.py"], evidence["changed"][writable]["qualified_paths"]
        )
        # The owning alias decides: a checkout of a read-only repository is a
        # breach however it was reached, and a checkout of a writable one is
        # authorized work even when it is created or removed outright.
        self.assertEqual(
            [
                ("read-only-repository", read_only),
                ("repository-removed", read_only),
            ],
            [
                (violation["kind"], violation["repository"])
                for violation in evidence["violations"]
            ],
        )

    def test_guard_fails_closed_for_unavailable_evidence(self) -> None:
        evidence = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",)),
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
        missing = workspace_evidence._workspace_snapshot_unavailable(
            OrderedDict(), required_repository_paths=("tundra_svc",)
        )
        self.assertEqual(
            "registered-repository-missing",
            missing["tundra_svc"]["reason"],
        )

    def test_guard_fails_closed_for_an_unclassified_external_write(self) -> None:
        # ASI-19 in registered mode: every declared repository is clean, so the
        # only thing that can speak for a write outside them is the path-level
        # verdict.
        clean = {"changed": {}, "added": [], "removed": [], "unavailable": {}}
        scope = plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",))
        self.assertTrue(
            workspace_evidence._workspace_write_guard_evidence(
                workspace=self.workspace,
                scope=scope,
                comparison=clean,
                exclude=(),
                include_ignored=False,
            )["passed"]
        )

        unclassified_reason = external_write_evidence.EXTERNAL_WRITE_UNCLASSIFIED_REASON
        evidence = workspace_evidence._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=scope,
            comparison=clean,
            exclude=(),
            include_ignored=False,
            external_writes={
                "classified": {"unclassified": 1},
                "control_plane": {},
                "control_plane_mutated": [],
                "unclassified": {
                    "/elsewhere/inbox.jsonl": {"reason": unclassified_reason}
                },
                "paths": {"/elsewhere/inbox.jsonl": "unclassified"},
            },
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual([], evidence["violations"])
        self.assertEqual(
            external_write_evidence.EXTERNAL_WRITE_UNCLASSIFIED_REASON,
            evidence["unavailable"]["/elsewhere/inbox.jsonl"]["reason"],
        )
        self.assertEqual(
            "unclassified",
            evidence["external_writes"]["paths"]["/elsewhere/inbox.jsonl"],
        )

    def test_guard_snapshot_covers_registered_repositories_only(self) -> None:
        scratch = self.workspace_root / "scratch"
        init_git_repo(scratch)
        init_git_repo(scratch / "inner")

        snapshot = handoff_guards._capture_workspace_write_guard_snapshot(
            workspace=self.workspace,
            plan_dir=self.plan_dir,
            exclude=(),
            include_ignored=False,
        )

        self.assertEqual(["tundra_OAK", "tundra_svc"], list(snapshot))
        self.assertEqual(
            {}, workspace_evidence._workspace_snapshot_unavailable(snapshot)
        )

    def test_repair_comparison_suppresses_registry_delta_only(self) -> None:
        comparison = {
            "changed": {},
            "added": ["surprise-repository", "tundra_OAK"],
            "removed": ["deregistered-repository", "tundra_svc"],
            "unavailable": {},
        }

        repaired, suppressed = workspace_evidence._workspace_scope_repair_comparison(
            comparison,
            frozenset({"tundra_svc", "tundra_OAK"}),
        )

        self.assertEqual(["tundra_OAK", "deregistered-repository"], suppressed)
        self.assertEqual(["surprise-repository"], repaired["added"])
        self.assertEqual(["tundra_svc"], repaired["removed"])

    # ASI-150: a background sync pushing a local branch moves only
    # refs/remotes/*; that is drift with an unknown writer, not a write.

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.workspace_root / "tundra_OAK",
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def _read_only_guard(self, mutate) -> dict[str, Any]:
        aliases = ("tundra_OAK", "tundra_svc")
        before = repository_evidence.capture_registered_snapshot(
            self.workspace_root, self.plan_dir, aliases
        )
        mutate()
        after = repository_evidence.capture_registered_snapshot(
            self.workspace_root, self.plan_dir, aliases
        )
        return workspace_evidence._workspace_write_guard_evidence(
            workspace=self.workspace,
            scope=plans_session_scope.SessionScope(
                reads=("SVC", "OAK"), writes=("SVC",)
            ),
            comparison=repository_evidence.compare_workspace_snapshots(before, after),
            exclude=(),
            include_ignored=False,
        )

    def test_remote_ref_drift_in_a_read_only_repository_is_not_a_violation(
        self,
    ) -> None:
        head = self._git("rev-parse", "HEAD")
        self._git("update-ref", "refs/remotes/sync/main", head)
        self._git("commit", "--allow-empty", "-m", "pushed later")
        tip = self._git("rev-parse", "HEAD")

        evidence = self._read_only_guard(
            lambda: self._git("update-ref", "refs/remotes/sync/main", tip)
        )

        self.assertTrue(evidence["passed"])
        self.assertEqual([], evidence["violations"])
        self.assertEqual([], evidence["authorized_aliases"])
        self.assertEqual(
            [
                {
                    "ref": "refs/remotes/sync/main",
                    "change": "changed",
                    "before": head,
                    "after": tip,
                    "writer": "unknown",
                    "new_tip_is_local": True,
                }
            ],
            evidence["remote_ref_drift"]["tundra_OAK"],
        )
        repair = workspace_evidence._workspace_repair_required(
            evidence, events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
        )
        self.assertFalse(any(repair.values()))

    def test_local_ref_or_content_beside_remote_drift_keeps_full_repair(
        self,
    ) -> None:
        def mixed() -> None:
            self._git("update-ref", "refs/remotes/sync/main", "HEAD")
            self._git("branch", "side")

        for mutate in (
            mixed,
            lambda: (
                self._git("update-ref", "refs/remotes/sync/main", "HEAD"),
                (self.workspace_root / "tundra_OAK" / "README.md").write_text(
                    "changed\n", encoding="utf-8"
                ),
            ),
        ):
            with self.subTest(mutate=mutate):
                self.setUp()
                evidence = self._read_only_guard(mutate)

                self.assertFalse(evidence["passed"])
                self.assertEqual(
                    ["read-only-repository"],
                    [violation["kind"] for violation in evidence["violations"]],
                )
                self.assertNotIn(
                    "refs/remotes/sync/main",
                    workspace_evidence._workspace_guard_violation_summary(evidence),
                )
                self.assertEqual(
                    {
                        "durable_scope": True,
                        "fresh_expansion": True,
                        "fresh_preflight": True,
                    },
                    workspace_evidence._workspace_repair_required(
                        evidence, events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
                    ),
                )


class LegacyUnobservedRepositoryTests(unittest.TestCase):
    """ASI-19: the legacy guard used to report clean about places it never saw.

    The legacy snapshot fingerprints exactly one repository, so a write landing
    anywhere else produced no change, no added or removed repository and
    nothing unavailable -- and ``passed`` was computed from precisely those
    three. Silence about an unvisited repository now reaches ``unavailable``,
    the lane that already fails closed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo_root = root / "repo"
        self.other_root = root / "other"
        init_git_repo(self.repo_root)
        init_git_repo(self.other_root)
        self.plan_dir = root / "plan"
        self.plan_dir.mkdir()
        self.artifact = root / "last-message.txt"

    def _canonical(self, path: Path) -> str:
        return str(workspaces.canonicalize_filesystem_path(path))

    def _unobserved(self, text: str, **overrides: Any) -> dict[str, Any]:
        self.artifact.write_text(text, encoding="utf-8")
        options: dict[str, Any] = {
            "cwd": self.repo_root,
            "artifact_paths": (self.artifact,),
            "exempt_roots": (),
            "provenance_roots": (),
        }
        options.update(overrides)
        return handoff_guards._legacy_unobserved_repositories(**options)

    def test_a_second_repository_named_after_the_turn_is_not_observed(self) -> None:
        unobserved = self._unobserved(
            f"Edited {self.other_root / 'README.md'} to fix the helper.\n"
        )

        entry = unobserved[self._canonical(self.other_root)]
        self.assertEqual("repository-not-observed", entry["reason"])
        self.assertEqual(self._canonical(self.repo_root), entry["observed_repository"])

    def test_the_fingerprinted_repository_is_never_reported_unobserved(self) -> None:
        # The one repository the snapshot does look at is exactly the one this
        # scan must stay quiet about, or every legacy turn would fail closed.
        self.assertEqual(
            {}, self._unobserved(f"Edited {self.repo_root / 'README.md'}.\n")
        )

    def test_a_path_in_no_repository_is_not_a_repository(self) -> None:
        plain = Path(self._tmp.name) / "notes"
        plain.mkdir()
        (plain / "scratch.md").write_text("scratch\n", encoding="utf-8")

        self.assertEqual({}, self._unobserved(f"Wrote {plain / 'scratch.md'}.\n"))

    def test_runner_owned_and_provenance_trees_stay_exempt(self) -> None:
        # Whatever the pre-turn packet gate excuses, this scan excuses too:
        # judging the same evidence by two different rules is how a guard and
        # an audit end up contradicting each other.
        source = self.other_root / "source-plan.md"
        source.write_text("# Plan\n", encoding="utf-8")
        artifact = self.other_root / "notes.md"
        artifact.write_text("notes\n", encoding="utf-8")

        self.assertEqual(
            {},
            self._unobserved(
                f"Split from {source}; wrote {artifact}.\n",
                exempt_roots=(artifact,),
                provenance_roots=(source,),
            ),
        )

    def test_an_unreadable_artifact_names_no_repository(self) -> None:
        missing = Path(self._tmp.name) / "absent.txt"

        self.assertEqual(
            {},
            handoff_guards._legacy_unobserved_repositories(
                cwd=self.repo_root,
                artifact_paths=(missing,),
                exempt_roots=(),
                provenance_roots=(),
            ),
        )

    def test_the_report_is_bounded_and_says_how_many_it_dropped(self) -> None:
        extras = []
        for index in range(3):
            extra = Path(self._tmp.name) / f"extra-{index}"
            init_git_repo(extra)
            extras.append(extra)

        unobserved = self._unobserved(
            "".join(f"Edited {extra / 'README.md'}.\n" for extra in extras),
            limit=2,
        )

        self.assertEqual(3, len(unobserved))
        self.assertEqual(1, unobserved["<omitted>"]["omitted_repositories"])

    def test_an_unobserved_repository_alone_fails_the_guard_closed(self) -> None:
        # The fail-open reproduced: a comparison with nothing changed, nothing
        # added or removed, and nothing unavailable used to pass outright.
        clean = {"changed": {}, "added": [], "removed": [], "unavailable": {}}

        self.assertTrue(
            workspace_evidence._legacy_implementation_write_guard_evidence(
                cwd=self.repo_root,
                comparison=clean,
                exclude=(),
                include_ignored=False,
            )["passed"]
        )

        evidence = workspace_evidence._legacy_implementation_write_guard_evidence(
            cwd=self.repo_root,
            comparison=clean,
            exclude=(),
            include_ignored=False,
            unobserved={
                str(self.other_root): {"reason": "repository-not-observed"},
            },
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual(
            "repository-not-observed",
            evidence["unavailable"][str(self.other_root)]["reason"],
        )
        self.assertEqual({}, evidence["changed"])
        self.assertEqual([], evidence["violations"])

    def test_a_failed_fingerprint_outranks_a_repository_never_looked_at(self) -> None:
        evidence = workspace_evidence._legacy_implementation_write_guard_evidence(
            cwd=self.repo_root,
            comparison={
                "changed": {},
                "added": [],
                "removed": [],
                "unavailable": {".": {"reason": "repository-unavailable"}},
            },
            exclude=(),
            include_ignored=False,
            unobserved={".": {"reason": "repository-not-observed"}},
        )

        self.assertFalse(evidence["passed"])
        self.assertEqual(
            "repository-unavailable", evidence["unavailable"]["."]["reason"]
        )


class ExternalWriteEvidenceTests(unittest.TestCase):
    """ASI-19: a write outside the bound checkout used to leave no evidence.

    The repository snapshot answers one question -- did a registered checkout
    change -- so a turn that edited an external audit store produced no changed
    path, no topology row and nothing unavailable, and passed as
    workspace-clean. Paths are now judged rather than repositories: every
    location a turn names is classified, control-plane locations carry before
    and after digests so a permitted mutation is evidenced rather than assumed,
    and a write answering to no authority the run can name fails closed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.repo_root = self.workspace_root / "repo"
        init_git_repo(self.repo_root)
        self.unregistered_root = self.root / "elsewhere"
        init_git_repo(self.unregistered_root)
        self.scratch = self.workspace_root / "notes.md"
        self.scratch.write_text("notes\n", encoding="utf-8")
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        # This run's own control plane: the plan directory and the state root
        # holding the assurance inbox.
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.ledger = self.state_root / "assurance-inbox.jsonl"
        self.ledger.write_text("{}\n", encoding="utf-8")
        # A store belonging to no authority this run can name. ASI-19's own
        # store was ASL's state root, which is control-plane and permitted;
        # this is the shape that has nothing vouching for it.
        self.foreign_store = self.root / "audit-store"
        self.foreign_store.mkdir()
        self.foreign_record = self.foreign_store / "inbox.jsonl"
        self.foreign_record.write_text("{}\n", encoding="utf-8")
        self.artifact = self.root / "last-message.txt"

    def _canonical(self, path: Path) -> str:
        return str(workspaces.canonicalize_filesystem_path(path))

    def _digests_before(self) -> dict[str, dict[str, Any]]:
        return external_write_evidence._capture_external_write_digests(
            (self.plan_dir,), shallow_roots=(self.state_root,)
        )

    def _evidence(self, text: str, **overrides: Any) -> dict[str, Any]:
        self.artifact.write_text(text, encoding="utf-8")
        options: dict[str, Any] = {
            "artifact_paths": (self.artifact,),
            "root": self.workspace_root,
            "workspace_root": self.workspace_root,
            "repository_roots": (self.repo_root,),
            "control_plane_roots": (self.plan_dir, self.state_root),
            "provenance_roots": (),
            "digests_before": {},
            "turn_started_ns": None,
            # Under test the whole taxonomy lives inside TMPDIR, so the
            # ephemeral bucket is opted into by the one test about it.
            "ephemeral_roots": (),
        }
        options.update(overrides)
        return external_write_evidence._external_write_evidence(**options)

    def _legacy_guard(self, **overrides: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "cwd": self.repo_root,
            "comparison": {
                "changed": {},
                "added": [],
                "removed": [],
                "unavailable": {},
            },
            "exclude": (),
            "include_ignored": False,
        }
        options.update(overrides)
        return workspace_evidence._legacy_implementation_write_guard_evidence(**options)

    def test_every_named_path_lands_in_exactly_one_bucket(self) -> None:
        evidence = self._evidence(
            f"Edited {self.repo_root / 'README.md'} and "
            f"{self.unregistered_root / 'README.md'}, refreshed {self.scratch}, "
            f"appended {self.ledger}, wrote {self.plan_dir / 'session-1.md'}, "
            f"and recorded {self.foreign_record}.\n"
        )

        self.assertEqual(
            {
                self._canonical(self.repo_root / "README.md"): "workspace-repository",
                self._canonical(
                    self.unregistered_root / "README.md"
                ): "unregistered-repository",
                self._canonical(self.scratch): "workspace-unregistered-path",
                self._canonical(self.ledger): "control-plane",
                self._canonical(self.plan_dir / "session-1.md"): "control-plane",
                self._canonical(self.foreign_record): "unclassified",
            },
            evidence["paths"],
        )
        # Every path is counted once and the counts cover the whole vocabulary.
        self.assertEqual(len(evidence["paths"]), sum(evidence["classified"].values()))
        self.assertEqual(
            sorted(external_write_evidence.EXTERNAL_WRITE_CLASSES),
            sorted(evidence["classified"]),
        )

    def test_a_write_no_authority_claims_fails_the_guard_closed(self) -> None:
        # The fail-open reproduced: the bound checkout is clean, so without a
        # path-level verdict the turn passes while the store it edited is
        # invisible.
        self.assertTrue(self._legacy_guard()["passed"])

        started = time.time_ns()
        os.utime(self.foreign_record, ns=(started + 10**9, started + 10**9))
        evidence = self._evidence(
            f"Recorded three dispositions in {self.foreign_record}.\n",
            turn_started_ns=started,
        )
        guard = self._legacy_guard(external_writes=evidence)

        self.assertFalse(guard["passed"])
        row = guard["unavailable"][self._canonical(self.foreign_record)]
        self.assertEqual(
            external_write_evidence.EXTERNAL_WRITE_UNCLASSIFIED_REASON, row["reason"]
        )
        # Unavailable rather than a violation, and no repository was implicated.
        self.assertEqual([], guard["violations"])
        self.assertEqual({}, guard["changed"])

    def test_explicit_external_data_authority_clears_only_that_write(self) -> None:
        evidence = self._evidence(
            f"Wrote `{self.foreign_record}`.",
            authorized_roots=(self.foreign_store.resolve(),),
        )
        self.assertTrue(self._legacy_guard(external_writes=evidence)["passed"])
        unapproved = self.root / "another-store.json"
        unapproved.write_text("{}")
        evidence = self._evidence(
            f"Wrote `{self.foreign_record}` and `{unapproved}`.",
            authorized_roots=(self.foreign_store.resolve(),),
        )
        guard = self._legacy_guard(external_writes=evidence)
        self.assertFalse(guard["passed"])
        self.assertEqual({str(unapproved.resolve())}, set(guard["unavailable"]))

    def test_a_path_named_but_not_written_this_turn_is_not_charged(self) -> None:
        started = time.time_ns()
        os.utime(self.foreign_record, ns=(started - 10**9, started - 10**9))

        evidence = self._evidence(
            f"Read {self.foreign_record} for context.\n", turn_started_ns=started
        )

        self.assertEqual(
            "unclassified", evidence["paths"][self._canonical(self.foreign_record)]
        )
        self.assertEqual(1, evidence["classified"]["unclassified"])
        self.assertEqual({}, evidence["unclassified"])
        self.assertTrue(self._legacy_guard(external_writes=evidence)["passed"])

    def test_a_permitted_control_plane_write_is_evidenced_not_fatal(self) -> None:
        digests_before = self._digests_before()
        started = time.time_ns()
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write("{}\n")

        evidence = self._evidence(
            f"Resolved an issue in {self.ledger}.\n",
            digests_before=digests_before,
            turn_started_ns=started,
        )

        self.assertEqual(
            "control-plane", evidence["paths"][self._canonical(self.ledger)]
        )
        # The store itself was never digested before the turn, so the proof the
        # control plane moved is the state root's own shape digest.
        self.assertEqual(
            [self._canonical(self.state_root)], evidence["control_plane_mutated"]
        )
        root_record = evidence["control_plane"][self._canonical(self.state_root)]
        self.assertNotEqual(
            root_record["before"]["sha256"], root_record["after"]["sha256"]
        )
        self.assertTrue(self._legacy_guard(external_writes=evidence)["passed"])

    def test_a_control_plane_root_the_turn_never_named_is_still_compared(self) -> None:
        digests_before = self._digests_before()
        (self.plan_dir / "session-1.1.md").write_text("# child\n", encoding="utf-8")

        evidence = self._evidence(
            f"Edited {self.repo_root / 'README.md'}.\n", digests_before=digests_before
        )

        self.assertIn(self._canonical(self.plan_dir), evidence["control_plane_mutated"])
        self.assertEqual(
            "control-plane-root",
            evidence["control_plane"][self._canonical(self.plan_dir)]["surface"],
        )

    def test_the_control_plane_outranks_a_repository_containing_it(self) -> None:
        nested_plan = self.repo_root / "plan"
        nested_plan.mkdir()

        evidence = self._evidence(
            f"Wrote {nested_plan / 'manifest.md'}.\n",
            control_plane_roots=(nested_plan, self.state_root),
        )

        self.assertEqual(
            "control-plane",
            evidence["paths"][self._canonical(nested_plan / "manifest.md")],
        )

    def test_a_temporary_directory_is_matched_after_every_other_authority(
        self,
    ) -> None:
        # Everything in this test lives under TMPDIR, which is exactly the
        # operator who keeps a checkout there: an early ephemeral match would
        # collapse the whole taxonomy into one bucket.
        evidence = self._evidence(
            f"Edited {self.repo_root / 'README.md'} and {self.foreign_record}.\n",
            ephemeral_roots=(self.root,),
        )

        self.assertEqual(
            {
                self._canonical(self.repo_root / "README.md"): "workspace-repository",
                self._canonical(self.foreign_record): "ephemeral-path",
            },
            evidence["paths"],
        )
        self.assertEqual({}, evidence["unclassified"])

    def test_an_unreadable_artifact_names_no_path(self) -> None:
        evidence = self._evidence(
            "ignored\n", artifact_paths=(self.root / "absent.txt",)
        )

        self.assertEqual({}, evidence["paths"])
        self.assertEqual({}, evidence["unclassified"])

    def test_the_state_root_is_digested_by_shape_and_never_expanded(self) -> None:
        sibling = self.state_root / "another-plans-state"
        sibling.mkdir()
        (sibling / "state.json").write_text("{}\n", encoding="utf-8")
        (self.plan_dir / "manifest.md").write_text("# manifest\n", encoding="utf-8")

        digests = self._digests_before()

        self.assertIn(self._canonical(self.state_root), digests)
        self.assertNotIn(self._canonical(sibling), digests)
        # An expanded root contributes its direct entries, which is what
        # catches an append the parent's own mtime would not move.
        self.assertIn(self._canonical(self.plan_dir / "manifest.md"), digests)

    def test_the_shallow_digest_root_is_the_state_root(self) -> None:
        self.assertEqual(
            [self._canonical(self.state_root)],
            [
                self._canonical(root)
                for root in external_write_evidence._control_plane_shallow_digest_roots(
                    str(self.state_root)
                )
            ],
        )

    def test_a_large_file_is_digested_by_size_and_mtime(self) -> None:
        transcript = self.root / "transcript.log"
        transcript.write_bytes(
            b"x" * (external_write_evidence.EXTERNAL_WRITE_DIGEST_MAX_BYTES + 1)
        )

        record = external_write_evidence._external_path_digest(transcript)

        self.assertTrue(record["truncated"])
        self.assertNotIn("sha256", record)
        self.assertEqual("file", record["kind"])

    def test_an_absent_path_digests_as_absent(self) -> None:
        self.assertEqual(
            {"present": False},
            external_write_evidence._external_path_digest(self.root / "absent"),
        )


if __name__ == "__main__":
    unittest.main()
