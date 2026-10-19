"""Focused test_asl_repository_evidence test group 2."""

from __future__ import annotations

from .support_asl_repository_evidence_base import (
    OrderedDict,
    Path,
    clone_git_repo,
    init_git_repo,
    repository_evidence,
    shutil,
    subprocess,
    tempfile,
    unittest,
    workspace_evidence,
)


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
