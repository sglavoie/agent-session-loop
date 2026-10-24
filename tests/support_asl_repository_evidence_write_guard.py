"""Repository evidence workspace write guard classification."""

from __future__ import annotations

from .support_asl_repository_evidence_base import (
    DEFAULT_MULTI_REPO_SPECS,
    Any,
    OrderedDict,
    Path,
    events,
    external_write_evidence,
    handoff_guards,
    init_git_repo,
    init_multi_repo_workspace,
    plans_registry,
    plans_session_scope,
    repository_evidence,
    subprocess,
    tempfile,
    unittest,
    workspace_evidence,
    workspaces,
    write_multi_repo_plan,
)


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
