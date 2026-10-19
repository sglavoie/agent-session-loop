"""Focused portion of tests/test_asl_repository_registration.py."""

from .asl_repository_registration_support import *


class VocabularyTests(unittest.TestCase):
    def test_exported_vocabularies_match_their_literal_types(self) -> None:
        self.assertEqual(
            get_args(repository_registration.RegistrationStatus),
            repository_registration.REGISTRATION_STATUSES,
        )
        self.assertEqual(
            get_args(repository_registration.MissingFact),
            repository_registration.MISSING_FACTS,
        )


class RegistrationDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = canonicalize_filesystem_path(self._temp.name)
        self.workspace = self.tmp / "workspace"
        self.plan_dir = self.tmp / "plan"

    def _init_two_repositories(self) -> dict[str, Path]:
        return init_multi_repo_workspace(self.workspace, specs=TWO_REPO_SPECS)

    def test_two_repository_plan_yields_rows_scopes_and_rewritten_checks(
        self,
    ) -> None:
        repositories = self._init_two_repositories()
        svc = repositories["SVC"]
        oak = repositories["OAK"]
        write_plan(
            self.plan_dir,
            ("pending", "pending"),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "svc-file",
                            "type": "file_exists",
                            "path": "src/new.py",
                        },
                        {
                            "name": "oak-suite",
                            "type": "command",
                            "argv": ["true"],
                            "cwd": str(oak),
                        },
                    ],
                ),
                "2": _contract_block(
                    "2",
                    [
                        {
                            "name": "oak-regex",
                            "type": "regex_in_file",
                            "path": f"{oak}/README.md",
                            "pattern": "test repo",
                        },
                        {
                            "name": "inherited",
                            "type": "command",
                            "argv": ["true"],
                        },
                    ],
                ),
            },
        )
        before_plan = _tree_state(self.plan_dir)
        before_workspace = _tree_state(self.workspace)

        derived = repository_registration.derive_registration(self.plan_dir, cwd=svc)

        self.assertIsInstance(derived, repository_registration.RegistrationPlan)
        self.assertEqual(self.workspace, derived.workspace_root)
        self.assertEqual(("tundra_OAK", "tundra_svc"), derived.registry.aliases)
        self.assertEqual(
            [
                ("tundra_OAK", "tundra_OAK", "github.com/tundra/OAK"),
                ("tundra_svc", "tundra_svc", "github.com/tundra/SVC"),
            ],
            [
                (repository.alias, repository.path, repository.origin)
                for repository in derived.registry
            ],
        )

        first = {check.name: check for check in derived.contracts["1"].verify.checks}
        self.assertEqual("tundra_svc", first["svc-file"].repo)
        self.assertEqual("src/new.py", first["svc-file"].path)
        self.assertEqual("tundra_OAK", first["oak-suite"].repo)
        self.assertEqual(".", first["oak-suite"].cwd)

        second = {check.name: check for check in derived.contracts["2"].verify.checks}
        self.assertEqual("tundra_OAK", second["oak-regex"].repo)
        self.assertEqual("README.md", second["oak-regex"].path)
        # No cwd, no absolute argv: the session's sole write repository owns it.
        self.assertEqual("tundra_OAK", second["inherited"].repo)
        self.assertIsNone(second["inherited"].cwd)

        self.assertEqual(("tundra_OAK", "tundra_svc"), derived.scopes["1"].writes)
        self.assertEqual(("tundra_OAK",), derived.scopes["2"].writes)
        for session_id, scope in derived.scopes.items():
            self.assertEqual(
                set(scope.writes) & set(scope.reads),
                set(scope.writes),
                msg=session_id,
            )

        for session_id, contract in derived.contracts.items():
            parse_completion_contract(
                _contract_block(session_id, _verify_payload(contract)["checks"]),
                session_id=session_id,
                repository_registry=derived.registry,
            )

        self.assertEqual(before_plan, _tree_state(self.plan_dir))
        self.assertEqual(before_workspace, _tree_state(self.workspace))

    def test_prose_repository_widens_reads_without_widening_writes(self) -> None:
        repositories = self._init_two_repositories()
        svc = repositories["SVC"]
        oak = repositories["OAK"]
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "svc-file",
                            "type": "file_exists",
                            "path": "src/new.py",
                        }
                    ],
                )
            },
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.", f"Read {oak}/README.md for the schema."
            ),
            encoding="utf-8",
        )

        derived = repository_registration.derive_registration(self.plan_dir, cwd=svc)

        scope = derived.scopes["1"]
        self.assertEqual(("tundra_OAK", "tundra_svc"), scope.reads)
        self.assertEqual(("tundra_svc",), scope.writes)

    def test_absolute_argv_attributes_a_command_without_a_cwd(self) -> None:
        repositories = self._init_two_repositories()
        svc = repositories["SVC"]
        oak = repositories["OAK"]
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "svc-file",
                            "type": "file_exists",
                            "path": "src/new.py",
                        },
                        {
                            "name": "oak-tool",
                            "type": "command",
                            "argv": ["/usr/bin/env", "python3", f"{oak}/tool.py"],
                        },
                    ],
                )
            },
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.", f"The tool lives beside {oak}/README.md."
            ),
            encoding="utf-8",
        )

        derived = repository_registration.derive_registration(self.plan_dir, cwd=svc)

        checks = {check.name: check for check in derived.contracts["1"].verify.checks}
        self.assertEqual("tundra_OAK", checks["oak-tool"].repo)

    def test_missing_origin_remote_refuses_with_repository_origin(self) -> None:
        repositories = self._init_two_repositories()
        svc = repositories["SVC"]
        oak = repositories["OAK"]
        subprocess.run(
            ["git", "remote", "remove", "origin"],
            cwd=oak,
            text=True,
            capture_output=True,
            check=True,
        )
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "oak-file",
                            "type": "file_exists",
                            "path": f"{oak}/README.md",
                        }
                    ],
                )
            },
        )

        derived = repository_registration.derive_registration(self.plan_dir, cwd=svc)

        self.assertEqual("refused", derived.status)
        self.assertEqual("repository-origin", derived.missing_fact)
        self.assertIn(str(oak), derived.detail)
        self.assertEqual((), derived.repositories)

    def test_nested_repository_refuses_with_workspace_root(self) -> None:
        outer = self.workspace / "outer"
        inner = outer / "vendor" / "inner"
        init_git_repo(outer)
        init_git_repo(inner)
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "inner-file",
                            "type": "file_exists",
                            "path": f"{inner}/README.md",
                        }
                    ],
                )
            },
        )

        derived = repository_registration.derive_registration(self.plan_dir, cwd=outer)

        self.assertEqual("refused", derived.status)
        self.assertEqual("workspace-root", derived.missing_fact)
        self.assertIn(str(inner), derived.detail)

    def test_contract_path_outside_every_repository_refuses(self) -> None:
        repositories = self._init_two_repositories()
        svc = repositories["SVC"]
        oak = repositories["OAK"]
        outside = self.tmp / "artifacts"
        outside.mkdir()
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "oak-file",
                            "type": "file_exists",
                            "path": f"{oak}/README.md",
                        },
                        {
                            "name": "stray",
                            "type": "file_exists",
                            "path": f"{outside}/report.txt",
                        },
                    ],
                )
            },
        )

        derived = repository_registration.derive_registration(self.plan_dir, cwd=svc)

        self.assertEqual("refused", derived.status)
        self.assertEqual("contract-path-outside-registry", derived.missing_fact)
        self.assertIn("'stray'", derived.detail)
        self.assertEqual(self.workspace, derived.workspace_root)

    def test_unattributable_command_check_refuses(self) -> None:
        repositories = self._init_two_repositories()
        svc = repositories["SVC"]
        oak = repositories["OAK"]
        write_plan(
            self.plan_dir,
            ("pending", "pending"),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "svc-file",
                            "type": "file_exists",
                            "path": "src/new.py",
                        },
                        {
                            "name": "oak-suite",
                            "type": "command",
                            "argv": ["true"],
                            "cwd": str(oak),
                        },
                    ],
                ),
                "2": _contract_block(
                    "2",
                    [{"name": "floating", "type": "command", "argv": ["true"]}],
                ),
            },
        )

        derived = repository_registration.derive_registration(self.plan_dir, cwd=svc)

        self.assertEqual("refused", derived.status)
        self.assertEqual("command-check-repo", derived.missing_fact)
        self.assertIn("'floating'", derived.detail)

    def test_registered_plan_is_already_registered(self) -> None:
        repositories = self._init_two_repositories()
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=TWO_REPO_SPECS)

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=repositories["SVC"]
        )

        self.assertEqual("already-registered", derived.status)
        self.assertIsNone(derived.missing_fact)

    def test_single_repository_plan_is_not_required(self) -> None:
        repository = self.workspace / "only"
        init_git_repo(repository)
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "file",
                            "type": "file_exists",
                            "path": "README.md",
                        }
                    ],
                )
            },
        )

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=repository
        )

        self.assertEqual("not-required", derived.status)
        self.assertIsNone(derived.missing_fact)

    def test_one_git_target_outside_non_git_root_derives_registry(self) -> None:
        bound_root = self.workspace / "operator-root"
        bound_root.mkdir(parents=True)
        repository = self.workspace / "only"
        init_git_repo(repository)
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                "https://github.com/example/only.git",
            ],
            cwd=repository,
            text=True,
            capture_output=True,
            check=True,
        )
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    "1",
                    [
                        {
                            "name": "file",
                            "type": "file_exists",
                            "path": str(repository / "README.md"),
                        }
                    ],
                )
            },
        )

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=bound_root
        )

        self.assertIsInstance(derived, repository_registration.RegistrationPlan)
        self.assertEqual(self.workspace, derived.workspace_root)
        self.assertEqual(("only",), derived.registry.aliases)
        self.assertEqual(("only",), derived.scopes["1"].writes)


__all__ = ["json", "subprocess", "tempfile", "unittest", "replace", "UTC", "datetime", "Path", "get_args", "authoring_lint", "authoring_manifest", "authoring_parse", "authoring_render", "plans_content_hash", "plans_models", "plans_readiness", "plans_registry", "plans_session_ids", "plans_session_scope", "plans_snapshot", "repository_registration", "parse_completion_contract", "Repository", "RepositoryRegistry", "canonicalize_filesystem_path", "init_git_repo", "init_multi_repo_workspace", "write_multi_repo_plan", "write_plan", "FIXED_NOW", "TWO_REPO_SPECS", "_contract_block", "_verify_payload", "_tree_state", "VocabularyTests", "RegistrationDerivationTests"]  # fmt: skip
