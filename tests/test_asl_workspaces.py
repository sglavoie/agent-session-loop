"""Workspace domain model, resolution, and metadata compatibility tests."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    authoring_manifest,
    plans_content_hash,
    plans_errors,
    plans_metadata,
    plans_registry,
    run_inputs,
    run_prompts,
    verification_models,
    workspaces,
)
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
            with (
                self.subTest(alias=invalid),
                self.assertRaises(workspaces.WorkspaceError),
            ):
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
            "repo\x00nested",
            "group/repo\nother",
            "group/a|b",
        ):
            with (
                self.subTest(path=invalid),
                self.assertRaises(workspaces.WorkspaceError),
            ):
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

    def test_origin_identity_is_idempotent_with_non_default_ports(self) -> None:
        expected = {
            "ssh://git@code.internal:2222/tundra/SVC.git": (
                "code.internal:2222/tundra/SVC"
            ),
            "https://host:8443/a/b.git": "host:8443/a/b",
            "git@github.com:t/SVC.git": "github.com/t/SVC",
            "ssh://git@host/a/b.git": "host/a/b",
        }
        for origin, identity in expected.items():
            with self.subTest(origin=origin):
                self.assertEqual(identity, workspaces.canonicalize_origin(origin))
                self.assertEqual(identity, workspaces.canonicalize_origin(identity))

    def test_origin_identity_quotes_markdown_metacharacters_idempotently(self) -> None:
        identity = workspaces.canonicalize_origin(
            "ssh://git@github.com/tundra/a@b/pipe|name.git"
        )

        self.assertEqual(
            "github.com/tundra/a%40b/pipe%7Cname",
            identity,
        )
        self.assertEqual(identity, workspaces.canonicalize_origin(identity))
        self.assertNotIn("|", identity)
        with self.assertRaisesRegex(
            workspaces.WorkspaceError, "Markdown table delimiters"
        ):
            workspaces.canonicalize_declared_origin(
                "https://github.com/tundra/pipe|name.git"
            )

    def test_userless_scp_numeric_path_is_not_treated_as_a_port(self) -> None:
        user_bearing = "git@github.com:22/tundra/SVC.git"
        userless = "github.com:22/tundra/SVC.git"

        self.assertEqual(
            "github.com/22/tundra/SVC",
            workspaces.canonicalize_origin(userless),
        )
        self.assertTrue(workspaces.origins_equal(user_bearing, userless))
        self.assertEqual(
            "github.com/22/tundra/SVC",
            workspaces.canonicalize_declared_origin("github.com:22/tundra/SVC"),
        )
        self.assertTrue(
            workspaces.origins_equal(
                "git@github.com:22/tundra/SVC",
                "github.com:22/tundra/SVC",
            )
        )
        self.assertEqual(
            "code.internal:2222/tundra/SVC",
            workspaces.canonicalize_origin("code.internal:2222/tundra/SVC"),
        )

    def test_origin_rejects_local_traversing_and_unsupported_values(self) -> None:
        for invalid in (
            "../repo",
            "file:///tmp/repo",
            "https://github.com/org/../repo.git",
            "https://github.com",
            "C:\\repo",
            "C:/repo",
        ):
            with (
                self.subTest(origin=invalid),
                self.assertRaises(workspaces.WorkspaceError),
            ):
                workspaces.canonicalize_origin(invalid)

        with self.assertRaisesRegex(workspaces.WorkspaceError, "absolute Git URL"):
            workspaces.canonicalize_declared_origin("github.com/org/repo")


class RepositoryRegistryTests(unittest.TestCase):
    def test_port_bearing_registry_round_trips_through_manifest(self) -> None:
        registry = _registry(
            (("SVC", "svc", "ssh://git@code.internal:2222/org/svc.git"),)
        )
        manifest = (
            "# Manifest\n\n## Repositories\n\n"
            + authoring_manifest.render_repository_registry_table(registry)
        )

        parsed = plans_registry.parse_repository_registry_manifest(manifest)

        self.assertIsNotNone(parsed)
        self.assertEqual(registry.digest, parsed.digest)
        self.assertEqual("code.internal:2222/org/svc", parsed.get("SVC").origin)

    def test_repositories_heading_quoted_inside_a_longer_fence_is_not_a_section(
        self,
    ) -> None:
        # A shorter inner fence used to close the quoting ```` block, so the
        # quoted heading read as a second, duplicate registry section.
        registry = _registry(
            (("SVC", "svc", "ssh://git@code.internal:2222/org/svc.git"),)
        )
        manifest = (
            "# Manifest\n\n## Repositories\n\n"
            + authoring_manifest.render_repository_registry_table(registry)
            + "\n## Notes\n\n````markdown\n```\n## Repositories\n````\n"
        )

        parsed = plans_registry.parse_repository_registry_manifest(manifest)

        self.assertIsNotNone(parsed)
        self.assertEqual(registry.digest, parsed.digest)

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
        https = _registry((("SVC", "tundra_svc", "https://github.com/tundra/SVC"),))
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
            with (
                self.subTest(specs=specs),
                self.assertRaises(workspaces.WorkspaceError),
            ):
                _registry(specs)


class WorkspaceResolutionTests(unittest.TestCase):
    def test_resolution_accepts_matching_non_default_port_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            repository_root = workspace_root / "svc"
            init_git_repo(repository_root)
            origin = "ssh://git@code.internal:2222/org/svc.git"
            subprocess.run(
                ["git", "remote", "add", "origin", origin],
                cwd=repository_root,
                text=True,
                capture_output=True,
                check=True,
            )

            resolved = workspaces.resolve_workspace(
                workspace_root, _registry((("SVC", "svc", origin),))
            )

            self.assertEqual("code.internal:2222/org/svc", resolved.get("SVC").origin)

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
            self.assertEqual(("SVC", "OAK", "OWL", "Kiln"), resolved.registry.aliases)
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

            self.assertEqual("github.com/tundra/SVC", resolved.get("SVC").origin)

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

    def test_resolution_rejects_symlinked_repository_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            repository_root = workspace_root / "real" / "repository"
            init_git_repo(repository_root)
            origin = "git@github.com:org/repository.git"
            subprocess.run(
                ["git", "remote", "add", "origin", origin],
                cwd=repository_root,
                text=True,
                capture_output=True,
                check=True,
            )
            (workspace_root / "linked").symlink_to(
                workspace_root / "real", target_is_directory=True
            )
            registry = _registry((("Repo", "linked/repository", origin),))

            with self.assertRaisesRegex(
                workspaces.WorkspaceError,
                "repository Repo path must not traverse a symlink",
            ):
                workspaces.resolve_workspace(workspace_root, registry)


class WorkspaceMetadataTests(unittest.TestCase):
    def test_v3_round_trip_persists_only_the_workspace_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            workspace_root = root / "workspace"
            plan_dir.mkdir()
            workspace_root.mkdir()
            digest = _registry().digest

            path = plans_metadata.save_workspace_plan_metadata(
                plan_dir,
                workspace_root / ".." / "workspace",
                digest,
            )
            metadata = plans_metadata.load_plan_metadata(plan_dir)

            self.assertEqual(
                plans_metadata.WorkspacePlanMetadata(
                    workspace_root=str(workspace_root.resolve()),
                    registry_digest=digest,
                ),
                metadata,
            )
            self.assertEqual(
                {
                    "registry_digest": digest,
                    "version": 3,
                    "workspace_root": str(workspace_root.resolve()),
                },
                json.loads(path.read_text(encoding="utf-8")),
            )

    def test_v2_loader_reads_the_binding_and_hashes_stay_stable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            workspace_root = root / "workspace"
            write_plan(plan_dir, ("pending",))
            workspace_root.mkdir()
            content_before = plans_content_hash.plan_content_sha256(plan_dir)
            readiness_before = plans_content_hash.stable_readiness_sha256(plan_dir)

            metadata_path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
            metadata_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "workspace_root": str(workspace_root.resolve()),
                        "registry_digest": _registry().digest,
                    }
                ),
                encoding="utf-8",
            )

            metadata = plans_metadata.load_plan_metadata(plan_dir)
            self.assertIsInstance(metadata, plans_metadata.WorkspacePlanMetadata)
            assert isinstance(metadata, plans_metadata.WorkspacePlanMetadata)
            self.assertEqual(2, metadata.version)

            self.assertEqual(
                content_before, plans_content_hash.plan_content_sha256(plan_dir)
            )
            self.assertEqual(
                readiness_before, plans_content_hash.stable_readiness_sha256(plan_dir)
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "workspace_root": str(plan_dir.resolve()),
                        "registry_digest": "not-a-digest",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(plans_errors.PlanError, "SHA-256"):
                plans_metadata.load_plan_metadata(plan_dir)

    def test_loader_refuses_a_non_integer_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            metadata_path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
            for version in ([2], {}, True):
                with self.subTest(version=version):
                    metadata_path.write_text(
                        json.dumps({"version": version, "cwd": "/tmp"}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        plans_errors.PlanError, "unsupported version"
                    ):
                        plans_metadata.load_plan_metadata(plan_dir)

    def test_v3_loader_accepts_and_discards_a_legacy_inventory_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            workspace_root = root / "workspace"
            plan_dir.mkdir()
            workspace_root.mkdir()
            metadata_path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME

            def write(excludes: object) -> None:
                metadata_path.write_text(
                    json.dumps(
                        {
                            "version": plans_metadata.PLAN_METADATA_V3_SCHEMA_VERSION,
                            "workspace_root": str(workspace_root.resolve()),
                            "registry_digest": "0" * 64,
                            "workspace_inventory_excludes": excludes,
                        }
                    ),
                    encoding="utf-8",
                )

            write(["Library/Protected.dv"])
            self.assertEqual(
                plans_metadata.WorkspacePlanMetadata(
                    workspace_root=str(workspace_root.resolve()),
                    registry_digest="0" * 64,
                ),
                plans_metadata.load_plan_metadata(plan_dir),
            )

            write("Library/Protected.dv")
            with self.assertRaisesRegex(
                plans_errors.PlanError,
                "'workspace_inventory_excludes' must be a string array",
            ):
                plans_metadata.load_plan_metadata(plan_dir)

    def test_resume_binding_ignores_a_persisted_inventory_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp) / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            init_multi_repo_workspace(workspace_root, specs)
            resolved = workspaces.resolve_workspace(
                workspace_root,
                _registry(specs),
            )
            context = verification_models.VerificationContext.resolved_workspace(
                resolved
            )
            current_binding = run_prompts._workspace_payload_fields(context)
            self.assertNotIn("workspace_inventory_excludes", current_binding)

            run_inputs._validate_resume_workspace_binding(
                argparse.Namespace(
                    _resume_workspace_config={
                        **current_binding,
                        "workspace_inventory_excludes": ["Library/Protected.dv"],
                    }
                ),
                context,
            )

            with self.assertRaisesRegex(plans_errors.PlanError, "persisted run"):
                run_inputs._validate_resume_workspace_binding(
                    argparse.Namespace(
                        _resume_workspace_config={
                            **current_binding,
                            "registry_digest": "stale",
                        }
                    ),
                    context,
                )

    def test_v1_serializer_and_parser_remain_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            cwd = root / "repo"
            plan_dir.mkdir()
            cwd.mkdir()

            path = plans_metadata.save_plan_metadata(plan_dir, cwd)
            metadata = plans_metadata.load_plan_metadata(plan_dir)

            self.assertEqual(
                plans_metadata.PlanMetadata(cwd=str(cwd.resolve())), metadata
            )
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
