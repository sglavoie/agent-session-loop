"""Focused portion of tests/test_asl_repository_registration.py."""

from .asl_repository_registration_application import *


class SourcePlanRegistrationTests(unittest.TestCase):
    """Migrating a source plan emits markdown and never touches its file."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = canonicalize_filesystem_path(self._temp.name)
        self.workspace = self.tmp / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, specs=TWO_REPO_SPECS
        )
        self.svc = self.repositories["SVC"]
        self.oak = self.repositories["OAK"]

    def _source_plan(self, *, stray: bool = False) -> str:
        checks = [
            {"name": "svc-file", "type": "file_exists", "path": "src/new.py"},
            {
                "name": "oak-suite",
                "type": "command",
                "argv": ["true"],
                "cwd": str(self.oak),
            },
        ]
        if stray:
            outside = self.tmp / "artifacts"
            outside.mkdir(exist_ok=True)
            checks.append(
                {
                    "name": "stray",
                    "type": "file_exists",
                    "path": f"{outside}/report.txt",
                }
            )
        return "\n".join(
            [
                "# Test Plan",
                "",
                "## Context",
                "",
                "Shared context.",
                "",
                "## Session 1: First",
                "",
                "Add the module.",
                "",
                _contract_block("1", checks),
                "",
                "---",
                "",
                "## Session 2: Second",
                "",
                f"Read {self.oak}/README.md before editing.",
                "",
                _contract_block(
                    "2",
                    [
                        {
                            "name": "oak-regex",
                            "type": "regex_in_file",
                            "path": f"{self.oak}/README.md",
                            "pattern": "test repo",
                        }
                    ],
                ),
                "",
            ]
        )

    def test_migrated_source_plan_carries_the_registry_and_every_scope(
        self,
    ) -> None:
        text = self._source_plan()

        result = repository_registration.register_source_plan(text, cwd=self.svc)

        self.assertEqual("applied", result.status)
        self.assertEqual(self.workspace, result.workspace_root)
        self.assertEqual(("1", "2"), result.sessions_changed)
        migrated = result.markdown
        self.assertLess(migrated.index("## Context"), migrated.index("## Repositories"))
        self.assertLess(
            migrated.index("## Repositories"),
            migrated.index("## Session 1: First"),
        )

        parsed = authoring_parse.parse_plan_document(migrated)
        self.assertEqual(("tundra_OAK", "tundra_svc"), parsed.registry.aliases)
        scopes = {
            session.session_id.value: session.scope for session in parsed.sessions
        }
        self.assertEqual(
            plans_session_scope.SessionScope(
                reads=("tundra_OAK", "tundra_svc"),
                writes=("tundra_OAK", "tundra_svc"),
            ),
            scopes["1"],
        )
        self.assertEqual(
            plans_session_scope.SessionScope(
                reads=("tundra_OAK",), writes=("tundra_OAK",)
            ),
            scopes["2"],
        )
        self.assertIn("Add the module.", migrated)
        self.assertIn("Read ", migrated)

    def test_embedded_contracts_are_rewritten_against_the_registry(
        self,
    ) -> None:
        result = repository_registration.register_source_plan(
            self._source_plan(), cwd=self.svc
        )

        parsed = authoring_parse.parse_plan_document(result.markdown)
        contracts = {
            session.session_id.value: parse_completion_contract(
                "\n".join(session.lines),
                session_id=session.session_id,
                repository_registry=parsed.registry,
            )
            for session in parsed.sessions
        }
        first = {check.name: check for check in contracts["1"].verify.checks}
        self.assertEqual("tundra_svc", first["svc-file"].repo)
        self.assertEqual("src/new.py", first["svc-file"].path)
        self.assertEqual("tundra_OAK", first["oak-suite"].repo)
        self.assertEqual(".", first["oak-suite"].cwd)
        second = {check.name: check for check in contracts["2"].verify.checks}
        self.assertEqual("tundra_OAK", second["oak-regex"].repo)
        self.assertEqual("README.md", second["oak-regex"].path)

    def test_migrated_source_plan_lints_clean_the_way_split_check_does(
        self,
    ) -> None:
        result = repository_registration.register_source_plan(
            self._source_plan(), cwd=self.svc
        )

        lint = authoring_lint.lint_plan_document(result.markdown, cwd=self.workspace)

        self.assertEqual((), lint.findings)
        self.assertEqual(("1", "2"), lint.session_ids)
        self.assertEqual(("tundra_OAK", "tundra_svc"), lint.registry.aliases)

    def test_migrating_a_migrated_plan_is_already_registered_and_verbatim(
        self,
    ) -> None:
        migrated = repository_registration.register_source_plan(
            self._source_plan(), cwd=self.svc
        ).markdown

        again = repository_registration.register_source_plan(migrated, cwd=self.svc)

        self.assertEqual("already-registered", again.status)
        self.assertEqual(migrated, again.markdown)

    def test_single_repository_plan_is_not_required_and_verbatim(self) -> None:
        text = "\n".join(
            [
                "# Test Plan",
                "",
                "## Session 1: First",
                "",
                "Edit `src/new.py`.",
                "",
                "## Session 2: Second",
                "",
                "Edit `src/other.py`.",
                "",
            ]
        )

        result = repository_registration.register_source_plan(text, cwd=self.svc)

        self.assertEqual("not-required", result.status)
        self.assertEqual(text, result.markdown)

    def test_refusal_emits_no_markdown(self) -> None:
        result = repository_registration.register_source_plan(
            self._source_plan(stray=True), cwd=self.svc
        )

        self.assertEqual("refused", result.status)
        self.assertEqual("contract-path-outside-registry", result.missing_fact)
        self.assertIn("'stray'", result.detail)
        self.assertIsNone(result.markdown)

    def test_deriving_from_text_writes_nothing_to_the_workspace(self) -> None:
        before = _tree_state(self.workspace)

        repository_registration.register_source_plan(self._source_plan(), cwd=self.svc)

        self.assertEqual(before, _tree_state(self.workspace))


if __name__ == "__main__":
    unittest.main()

__all__ = ["json", "subprocess", "tempfile", "unittest", "replace", "UTC", "datetime", "Path", "get_args", "authoring_lint", "authoring_manifest", "authoring_parse", "authoring_render", "plans_content_hash", "plans_models", "plans_readiness", "plans_registry", "plans_session_ids", "plans_session_scope", "plans_snapshot", "repository_registration", "parse_completion_contract", "Repository", "RepositoryRegistry", "canonicalize_filesystem_path", "init_git_repo", "init_multi_repo_workspace", "write_multi_repo_plan", "write_plan", "FIXED_NOW", "TWO_REPO_SPECS", "_contract_block", "_verify_payload", "_tree_state", "VocabularyTests", "RegistrationDerivationTests", "MigratedShapeTests", "RegistrationApplicationTests", "SourcePlanRegistrationTests"]  # fmt: skip
