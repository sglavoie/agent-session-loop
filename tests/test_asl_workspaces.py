"""Workspace domain model, resolution, and metadata compatibility tests."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from agent_session_loop import plans, workspaces
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_git_repo,
    init_multi_repo_workspace,
    write_plan,
)


def _registry(
    specs: tuple[tuple[str, str, str], ...] = DEFAULT_MULTI_REPO_SPECS,
) -> workspaces.RepositoryRegistry:
    return workspaces.RepositoryRegistry(
        tuple(
            workspaces.Repository(alias=alias, path=path, origin=origin)
            for alias, path, origin in specs
        )
    )


class RepositoryValueTests(unittest.TestCase):
    def test_alias_validation_is_exact_and_models_are_immutable(self) -> None:
        repository = workspaces.Repository(
            alias="SVC_2",
            path="./services//svc/",
            origin="git@GitHub.com:tundra/SVC.git",
        )
        self.assertEqual("SVC_2", repository.alias)
        self.assertEqual("services/svc", repository.path)
        self.assertEqual("github.com/tundra/SVC", repository.origin)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            repository.alias = "changed"  # type: ignore[misc]

        for invalid in ("", "2SDR", "SD R", "svc.", " svc"):
            with self.subTest(alias=invalid):
                with self.assertRaises(workspaces.WorkspaceError):
                    workspaces.validate_repository_alias(invalid)

    def test_relative_path_normalization_rejects_unsafe_forms(self) -> None:
        self.assertEqual(
            "group/repo", workspaces.normalize_repository_path("./group//repo/")
        )
        for invalid in (
            "",
            ".",
            "../repo",
            "repo/../other",
            "/repo",
            "C:\\repo",
            "repo\\nested",
        ):
            with self.subTest(path=invalid):
                with self.assertRaises(workspaces.WorkspaceError):
                    workspaces.normalize_repository_path(invalid)

    def test_origin_identity_ignores_transport_credentials_and_dot_git(self) -> None:
        origins = (
            "git@github.com:tundra/SVC.git",
            "ssh://git@github.com/tundra/SVC.git",
            "https://token:secret@GITHUB.COM/tundra/SVC.git",
            "git://github.com/tundra/SVC",
        )
        identities = {workspaces.canonicalize_origin(origin) for origin in origins}
        self.assertEqual({"github.com/tundra/SVC"}, identities)
        self.assertTrue(workspaces.origins_equal(origins[0], origins[2]))
        identity = workspaces.canonicalize_origin(origins[2])
        self.assertNotIn("token", identity)
        self.assertNotIn("secret", identity)

    def test_origin_rejects_local_traversing_and_unsupported_values(self) -> None:
        for invalid in (
            "../repo",
            "file:///tmp/repo",
            "https://github.com/org/../repo.git",
            "https://github.com",
        ):
            with self.subTest(origin=invalid):
                with self.assertRaises(workspaces.WorkspaceError):
                    workspaces.canonicalize_origin(invalid)


class RepositoryRegistryTests(unittest.TestCase):
    def test_registry_preserves_order_exact_lookup_and_stable_digest(self) -> None:
        registry = _registry()
        self.assertEqual(("SVC", "OAK", "OWL", "Kiln"), registry.aliases)
        self.assertEqual("tundra_OAK", registry.get("OAK").path)
        with self.assertRaisesRegex(workspaces.WorkspaceError, "unknown"):
            registry.get("oak")
        self.assertRegex(registry.digest, r"^[0-9a-f]{64}$")
        self.assertEqual(registry.digest, _registry().digest)

        reordered = _registry(tuple(reversed(DEFAULT_MULTI_REPO_SPECS)))
        self.assertNotEqual(registry.digest, reordered.digest)

    def test_registry_digest_uses_canonical_origin_identity(self) -> None:
        ssh = _registry((DEFAULT_MULTI_REPO_SPECS[0],))
        https = _registry(
            (("SVC", "tundra_svc", "https://github.com/tundra/SVC"),)
        )
        self.assertEqual(ssh.digest, https.digest)

    def test_registry_rejects_case_collisions_duplicates_and_overlap(self) -> None:
        cases = (
            (
                ("SVC", "one", "git@github.com:org/one.git"),
                ("svc", "two", "git@github.com:org/two.git"),
            ),
            (
                ("One", "shared", "git@github.com:org/one.git"),
                ("Two", "shared", "git@github.com:org/two.git"),
            ),
            (
                ("One", "group", "git@github.com:org/one.git"),
                ("Two", "group/two", "git@github.com:org/two.git"),
            ),
        )
        for specs in cases:
            with self.subTest(specs=specs):
                with self.assertRaises(workspaces.WorkspaceError):
                    _registry(specs)


class WorkspaceResolutionTests(unittest.TestCase):
    def test_resolves_sibling_repositories_and_builds_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            repositories = init_multi_repo_workspace(workspace_root)
            completed = subprocess.run(
                ["git", "-C", str(workspace_root), "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(0, completed.returncode)

            resolved = workspaces.resolve_workspace(workspace_root, _registry())

            self.assertEqual(workspace_root.resolve(), resolved.root)
            self.assertEqual(
                ("SVC", "OAK", "OWL", "Kiln"), resolved.registry.aliases
            )
            self.assertEqual(repositories["OAK"].resolve(), resolved.get("OAK").root)
            self.assertEqual(_registry().digest, resolved.binding.registry_digest)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                resolved.binding.workspace_root = "changed"  # type: ignore[misc]

    def test_resolution_does_not_retain_origin_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            repositories = init_multi_repo_workspace(
                workspace_root, (DEFAULT_MULTI_REPO_SPECS[0],)
            )
            subprocess.run(
                [
                    "git",
                    "remote",
                    "set-url",
                    "origin",
                    "https://token:secret@github.com/tundra/SVC.git",
                ],
                cwd=repositories["SVC"],
                text=True,
                capture_output=True,
                check=True,
            )

            resolved = workspaces.resolve_workspace(
                workspace_root, _registry((DEFAULT_MULTI_REPO_SPECS[0],))
            )

            self.assertEqual(
                "github.com/tundra/SVC", resolved.get("SVC").origin
            )

    def test_resolution_rejects_nested_non_top_level_and_origin_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            repositories = init_multi_repo_workspace(
                workspace_root, (DEFAULT_MULTI_REPO_SPECS[0],)
            )
            nested = repositories["SVC"] / "nested"
            nested.mkdir()
            nested_registry = _registry(
                (("SVC", "tundra_svc/nested", DEFAULT_MULTI_REPO_SPECS[0][2]),)
            )
            with self.assertRaisesRegex(workspaces.WorkspaceError, "top-level"):
                workspaces.resolve_workspace(workspace_root, nested_registry)

            mismatch = _registry(
                (("SVC", "tundra_svc", "git@github.com:other/project.git"),)
            )
            with self.assertRaisesRegex(workspaces.WorkspaceError, "does not match"):
                workspaces.resolve_workspace(workspace_root, mismatch)

    def test_resolution_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            outside = root / "outside"
            init_git_repo(outside)
            subprocess.run(
                ["git", "remote", "add", "origin", "git@github.com:org/outside.git"],
                cwd=outside,
                text=True,
                capture_output=True,
                check=True,
            )
            (workspace_root / "linked").symlink_to(outside, target_is_directory=True)
            registry = _registry(
                (("Outside", "linked", "git@github.com:org/outside.git"),)
            )

            with self.assertRaisesRegex(workspaces.WorkspaceError, "outside"):
                workspaces.resolve_workspace(workspace_root, registry)


class WorkspaceMetadataTests(unittest.TestCase):
    def test_v2_round_trip_uses_workspace_root_and_registry_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            workspace_root = root / "workspace"
            plan_dir.mkdir()
            workspace_root.mkdir()
            digest = _registry().digest

            path = plans.save_workspace_plan_metadata(
                plan_dir, workspace_root / ".." / "workspace", digest
            )
            metadata = plans.load_plan_metadata(plan_dir)

            self.assertEqual(
                plans.WorkspacePlanMetadata(
                    workspace_root=str(workspace_root.resolve()),
                    registry_digest=digest,
                ),
                metadata,
            )
            self.assertEqual(
                {
                    "registry_digest": digest,
                    "version": 2,
                    "workspace_root": str(workspace_root.resolve()),
                },
                json.loads(path.read_text(encoding="utf-8")),
            )

    def test_v2_loader_is_strict_and_does_not_change_plan_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            workspace_root = root / "workspace"
            write_plan(plan_dir, ("pending",))
            workspace_root.mkdir()
            content_before = plans.plan_content_sha256(plan_dir)
            readiness_before = plans.stable_readiness_sha256(plan_dir)

            plans.save_workspace_plan_metadata(
                plan_dir, workspace_root, _registry().digest
            )

            self.assertEqual(content_before, plans.plan_content_sha256(plan_dir))
            self.assertEqual(
                readiness_before, plans.stable_readiness_sha256(plan_dir)
            )
            path = plan_dir / plans.PLAN_METADATA_FILE_NAME
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "workspace_root": str(plan_dir.resolve()),
                        "registry_digest": "not-a-digest",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(plans.PlanError, "SHA-256"):
                plans.load_plan_metadata(plan_dir)

    def test_v1_serializer_and_parser_remain_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            cwd = root / "repo"
            plan_dir.mkdir()
            cwd.mkdir()

            path = plans.save_plan_metadata(plan_dir, cwd)
            metadata = plans.load_plan_metadata(plan_dir)

            self.assertEqual(plans.PlanMetadata(cwd=str(cwd.resolve())), metadata)
            self.assertEqual(
                json.dumps(
                    {"cwd": str(cwd.resolve()), "version": 1},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
