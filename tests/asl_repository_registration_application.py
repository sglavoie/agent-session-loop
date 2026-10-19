"""Focused portion of tests/test_asl_repository_registration.py."""

from .asl_repository_registration_migration import *


class RegistrationApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = canonicalize_filesystem_path(self._temp.name)
        self.workspace = self.tmp / "workspace"
        self.plan_dir = self.tmp / "plan"
        self.repositories = init_multi_repo_workspace(
            self.workspace, specs=TWO_REPO_SPECS
        )
        self.svc = self.repositories["SVC"]
        self.oak = self.repositories["OAK"]

    def _write_manifest(self) -> None:
        (self.plan_dir / "manifest.md").write_text(
            "\n".join(
                [
                    "# Test Plan",
                    "",
                    "**Split from**: `/tmp/plan.md`",
                    "**Content SHA-256**: `deadbeef`",
                    "**Sessions**: 2",
                    "**Readiness**: reviewed",
                    "**Readiness SHA-256**: `stale`",
                    "**Readiness checked at**: 2020-01-01T00:00:00+00:00",
                    "",
                    "## Sessions",
                    "",
                    "| # | Title | Status | File |",
                    "| --- | --- | --- | --- |",
                    "| 1 | Test | pending | session-1.md |",
                    "| 2 | Test | pending | session-2.md |",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _write_two_session_plan(self) -> None:
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
                            "cwd": str(self.oak),
                        },
                    ],
                ),
                "2": _contract_block(
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
            },
        )
        self._write_manifest()
        (self.plan_dir / "plan-metadata.json").write_text(
            '{"schema_version": 1}\n', encoding="utf-8"
        )

    def _session_text(self, session_id: str) -> str:
        return (self.plan_dir / f"session-{session_id}.md").read_text(encoding="utf-8")

    def _set_handoff(
        self, session_id: str, *, status: str, files_modified: str
    ) -> None:
        path = self.plan_dir / f"session-{session_id}.md"
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("- **Status**: "):
                line = f"- **Status**: {status}"
            elif line.startswith("- **Files modified**: "):
                line = f"- **Files modified**: {files_modified}"
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_applied_migration_round_trips_through_every_parser(self) -> None:
        self._write_two_session_plan()
        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=self.svc
        )
        handoffs_before = {
            session_id: self._session_text(session_id).split("## Handoff", 1)[1]
            for session_id in ("1", "2")
        }
        metadata_before = _tree_state(self.plan_dir)["plan-metadata.json"]

        result = repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )

        self.assertEqual("applied", result.status)
        self.assertIsNone(result.missing_fact)
        self.assertEqual(self.workspace, result.workspace_root)
        self.assertEqual(("1", "2"), result.sessions_changed)
        self.assertEqual(
            ("tundra_OAK", "tundra_svc"),
            tuple(repository.alias for repository in result.repositories),
        )
        self.assertEqual(
            {
                self.plan_dir / "manifest.md",
                self.plan_dir / "session-1.md",
                self.plan_dir / "session-2.md",
            },
            set(result.files),
        )

        registry = plans_registry.read_repository_registry(self.plan_dir)
        self.assertEqual(("tundra_OAK", "tundra_svc"), registry.aliases)
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertLess(
            manifest.index("## Repositories"), manifest.index("## Sessions")
        )
        self.assertIn("**Content SHA-256**: `deadbeef`", manifest)

        snapshot = plans_snapshot.inspect_plan(self.plan_dir)
        self.assertEqual(
            ("1", "2"), tuple(status.session.id for status in snapshot.sessions)
        )

        for session_id, expected_scope in derived.scopes.items():
            text = self._session_text(session_id)
            self.assertEqual(
                expected_scope,
                plans_session_scope.parse_session_scope(text, registry.aliases),
                msg=session_id,
            )
            parsed = parse_completion_contract(
                text,
                session_id=session_id,
                repository_registry=registry,
            )
            self.assertEqual(
                derived.contracts[session_id].verify.checks,
                parsed.verify.checks,
                msg=session_id,
            )
            self.assertEqual(
                derived.contracts[session_id].done_when,
                parsed.done_when,
                msg=session_id,
            )
            self.assertIn("Instructions.", text)
            self.assertEqual(
                handoffs_before[session_id],
                text.split("## Handoff", 1)[1],
                msg=session_id,
            )

        self.assertEqual(
            metadata_before, _tree_state(self.plan_dir)["plan-metadata.json"]
        )
        self.assertEqual(
            plans_content_hash.stable_readiness_sha256(self.plan_dir), result.readiness
        )
        readiness = plans_readiness.read_readiness_metadata(self.plan_dir)
        self.assertEqual(result.readiness, readiness.readiness_sha256)
        self.assertEqual("reviewed", readiness.state)
        self.assertEqual(FIXED_NOW.isoformat(), readiness.checked_at)

    def test_scope_lands_below_the_leading_reference_lines(self) -> None:
        self._write_two_session_plan()
        path = self.plan_dir / "session-2.md"
        heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
        path.write_text(
            f"{heading}\n\n> **Before starting**: Read `context.md`.\n"
            f"> **Previous session**: Read `session-1.md`.\n{remainder}",
            encoding="utf-8",
        )

        repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )

        lines = self._session_text("2").splitlines()
        self.assertEqual("> **Previous session**: Read `session-1.md`.", lines[3])
        self.assertEqual("", lines[4])
        self.assertTrue(lines[5].startswith("**Reads repositories**: "))
        self.assertTrue(lines[6].startswith("**Writes repositories**: "))
        self.assertEqual("", lines[7])

    def test_next_commands_name_the_rebind_then_the_rerun(self) -> None:
        self._write_two_session_plan()

        result = repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )

        self.assertEqual(
            (
                f"asl prepare {self.plan_dir} --resume --rebind-workspace "
                f"--cwd {self.workspace}",
                f"asl run {self.plan_dir} --cwd {self.workspace}",
            ),
            result.next_commands,
        )

    def test_rerunning_is_already_registered_and_writes_nothing(self) -> None:
        self._write_two_session_plan()
        repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )
        before = _tree_state(self.plan_dir)

        again = repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )

        self.assertEqual("already-registered", again.status)
        self.assertEqual((), again.files)
        self.assertEqual(before, _tree_state(self.plan_dir))

    def test_completed_handoff_migration_preserves_plan_parseability(
        self,
    ) -> None:
        """A registry may never turn a part-way plan into one nothing parses."""
        self._write_two_session_plan()
        self._set_handoff("1", status="complete", files_modified="src/new.py")
        before = _tree_state(self.plan_dir)

        refused = repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )

        self.assertEqual("refused", refused.status)
        self.assertEqual("completed-handoff-paths", refused.missing_fact)
        self.assertIn("session-1.md", refused.detail)
        self.assertIn("src/new.py", refused.detail)
        self.assertEqual((), refused.files)
        self.assertEqual(before, _tree_state(self.plan_dir))
        self.assertIsNone(plans_registry.read_repository_registry(self.plan_dir))
        self.assertEqual(
            ("complete", "pending"),
            tuple(
                status.status
                for status in plans_snapshot.inspect_plan(self.plan_dir).sessions
            ),
        )

        # A completed handoff that modified nothing survives any registry, so
        # the migration still lands and the plan still parses afterwards.
        self._set_handoff("1", status="complete", files_modified="(none)")
        applied = repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )

        self.assertEqual("applied", applied.status)
        self.assertEqual(
            ("complete", "pending"),
            tuple(
                status.status
                for status in plans_snapshot.inspect_plan(self.plan_dir).sessions
            ),
        )

    def test_refusal_leaves_the_plan_directory_byte_identical(self) -> None:
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
                            "path": f"{self.oak}/README.md",
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
        before = _tree_state(self.plan_dir)

        result = repository_registration.register_repositories(
            self.plan_dir, cwd=self.svc, now=FIXED_NOW
        )

        self.assertEqual("refused", result.status)
        self.assertEqual("contract-path-outside-registry", result.missing_fact)
        self.assertEqual((), result.files)
        self.assertEqual(before, _tree_state(self.plan_dir))
        self.assertIsNone(plans_registry.read_repository_registry(self.plan_dir))


__all__ = ["json", "subprocess", "tempfile", "unittest", "replace", "UTC", "datetime", "Path", "get_args", "authoring_lint", "authoring_manifest", "authoring_parse", "authoring_render", "plans_content_hash", "plans_models", "plans_readiness", "plans_registry", "plans_session_ids", "plans_session_scope", "plans_snapshot", "repository_registration", "parse_completion_contract", "Repository", "RepositoryRegistry", "canonicalize_filesystem_path", "init_git_repo", "init_multi_repo_workspace", "write_multi_repo_plan", "write_plan", "FIXED_NOW", "TWO_REPO_SPECS", "_contract_block", "_verify_payload", "_tree_state", "VocabularyTests", "RegistrationDerivationTests", "MigratedShapeTests", "RegistrationApplicationTests"]  # fmt: skip
