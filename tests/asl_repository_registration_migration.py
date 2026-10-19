"""Focused portion of tests/test_asl_repository_registration.py."""

from .asl_repository_registration_derivation import *


class MigratedShapeTests(unittest.TestCase):
    """A migrated file must be byte-identical to a freshly rendered one."""

    registry = RepositoryRegistry(
        (
            Repository("A", "a", "github.com/x/a"),
            Repository("B", "b", "github.com/x/b"),
        )
    )

    def _contract(self, *, repo: str | None) -> object:
        check: dict[str, object] = {
            "name": "suite",
            "type": "command",
            "argv": ["true"],
        }
        if repo is not None:
            check["repo"] = repo
        return authoring_render.contract_from_payload(
            "2",
            ["the session lands"],
            {"session": "2", "checks": [check]},
            repository_registry=self.registry if repo is not None else None,
        )

    def test_migrated_session_file_matches_render_session_file(self) -> None:
        scope = plans_session_scope.SessionScope(reads=("A", "B"), writes=("A",))
        legacy = authoring_render.render_session_file(
            session_id="2",
            title="Thing",
            instruction_lines=["Do it."],
            previous_session_id="1",
            contract=self._contract(repo=None),
        )
        expected = authoring_render.render_session_file(
            session_id="2",
            title="Thing",
            instruction_lines=["Do it."],
            previous_session_id="1",
            contract=self._contract(repo="A"),
            scope=scope,
            repository_registry=self.registry,
        )

        migrated = repository_registration._migrate_session(
            legacy,
            scope=scope,
            contract=self._contract(repo="A"),
            registry=self.registry,
        )

        self.assertEqual(expected, migrated)

    def test_migrating_twice_is_a_fixed_point(self) -> None:
        scope = plans_session_scope.SessionScope(reads=("A", "B"), writes=("A",))
        contract = self._contract(repo="A")
        once = repository_registration._migrate_session(
            authoring_render.render_session_file(
                session_id="2",
                title="Thing",
                instruction_lines=["Do it."],
                previous_session_id="1",
                contract=self._contract(repo=None),
            ),
            scope=scope,
            contract=contract,
            registry=self.registry,
        )

        twice = repository_registration._migrate_session(
            once, scope=scope, contract=contract, registry=self.registry
        )

        self.assertEqual(once, twice)

    def test_migrated_manifest_matches_render_manifest(self) -> None:
        spec = authoring_manifest.ManifestSpec(
            plan_title="Plan",
            source_path="/tmp/plan.md",
            output_dir="/tmp/plan",
            plan_id="abc",
            source_fingerprint="fp",
            content_sha256="sha",
            sessions=(
                authoring_manifest.ManifestSession(
                    session_id=plans_session_ids.parse_session_id("1"), title="One"
                ),
            ),
            split_date="2026-01-01",
            readiness=plans_models.ReadinessMetadata(
                state="draft",
                readiness_sha256="sha",
                checked_at="2026-01-01T00:00:00+00:00",
            ),
        )

        migrated = repository_registration._insert_registry_table(
            authoring_manifest.render_manifest(spec), self.registry
        )

        self.assertEqual(
            authoring_manifest.render_manifest(replace(spec, registry=self.registry)),
            migrated,
        )


__all__ = ["json", "subprocess", "tempfile", "unittest", "replace", "UTC", "datetime", "Path", "get_args", "authoring_lint", "authoring_manifest", "authoring_parse", "authoring_render", "plans_content_hash", "plans_models", "plans_readiness", "plans_registry", "plans_session_ids", "plans_session_scope", "plans_snapshot", "repository_registration", "parse_completion_contract", "Repository", "RepositoryRegistry", "canonicalize_filesystem_path", "init_git_repo", "init_multi_repo_workspace", "write_multi_repo_plan", "write_plan", "FIXED_NOW", "TWO_REPO_SPECS", "_contract_block", "_verify_payload", "_tree_state", "VocabularyTests", "RegistrationDerivationTests", "MigratedShapeTests"]  # fmt: skip
