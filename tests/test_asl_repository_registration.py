"""Derivation and application of the multi-repository registry migration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from typing import get_args
import unittest

from agent_session_loop import authoring, plans, repository_registration
from agent_session_loop.contracts import parse_completion_contract
from agent_session_loop.workspaces import (
    Repository,
    RepositoryRegistry,
    canonicalize_filesystem_path,
)
from tests.support import (
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)


FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


TWO_REPO_SPECS: tuple[tuple[str, str, str], ...] = (
    ("SVC", "tundra_svc", "git@github.com:tundra/SVC.git"),
    ("OAK", "tundra_OAK", "git@github.com:tundra/OAK.git"),
)


def _contract_block(session: str, checks: list[dict]) -> str:
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "",
            "- the session lands",
            "",
            "### Verify",
            "",
            "```json",
            json.dumps({"session": session, "checks": checks}),
            "```",
        ]
    )


def _verify_payload(contract) -> dict:
    """Round-trip a rewritten contract back through the registry validators."""
    checks = []
    for check in contract.verify.checks:
        payload = {"name": check.name, "type": check.kind, "repo": check.repo}
        if check.kind == "command":
            payload["argv"] = list(check.argv)
            if check.cwd is not None:
                payload["cwd"] = check.cwd
        else:
            payload["path"] = check.path
        if check.kind == "regex_in_file":
            payload["pattern"] = check.pattern
        checks.append(payload)
    return {"session": contract.session_id.value, "checks": checks}


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    """Every file beneath ``root`` with its size and modification time."""
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=svc
        )

        self.assertIsInstance(derived, repository_registration.RegistrationPlan)
        self.assertEqual(self.workspace, derived.workspace_root)
        self.assertEqual(
            ("tundra_OAK", "tundra_svc"), derived.registry.aliases
        )
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

        second = {
            check.name: check for check in derived.contracts["2"].verify.checks
        }
        self.assertEqual("tundra_OAK", second["oak-regex"].repo)
        self.assertEqual("README.md", second["oak-regex"].path)
        # No cwd, no absolute argv: the session's sole write repository owns it.
        self.assertEqual("tundra_OAK", second["inherited"].repo)
        self.assertIsNone(second["inherited"].cwd)

        self.assertEqual(
            ("tundra_OAK", "tundra_svc"), derived.scopes["1"].writes
        )
        self.assertEqual(("tundra_OAK",), derived.scopes["2"].writes)
        for session_id, scope in derived.scopes.items():
            self.assertEqual(
                set(scope.writes) & set(scope.reads),
                set(scope.writes),
                msg=session_id,
            )

        for session_id, contract in derived.contracts.items():
            parse_completion_contract(
                _contract_block(
                    session_id, _verify_payload(contract)["checks"]
                ),
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

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=svc
        )

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

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=svc
        )

        checks = {
            check.name: check for check in derived.contracts["1"].verify.checks
        }
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

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=svc
        )

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

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=outer
        )

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

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=svc
        )

        self.assertEqual("refused", derived.status)
        self.assertEqual(
            "contract-path-outside-registry", derived.missing_fact
        )
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

        derived = repository_registration.derive_registration(
            self.plan_dir, cwd=svc
        )

        self.assertEqual("refused", derived.status)
        self.assertEqual("command-check-repo", derived.missing_fact)
        self.assertIn("'floating'", derived.detail)

    def test_registered_plan_is_already_registered(self) -> None:
        repositories = self._init_two_repositories()
        write_multi_repo_plan(
            self.plan_dir, ("pending",), specs=TWO_REPO_SPECS
        )

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
        return authoring.contract_from_payload(
            "2",
            ["the session lands"],
            {"session": "2", "checks": [check]},
            repository_registry=self.registry if repo is not None else None,
        )

    def test_migrated_session_file_matches_render_session_file(self) -> None:
        scope = plans.SessionScope(reads=("A", "B"), writes=("A",))
        legacy = authoring.render_session_file(
            session_id="2",
            title="Thing",
            instruction_lines=["Do it."],
            previous_session_id="1",
            contract=self._contract(repo=None),
        )
        expected = authoring.render_session_file(
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
        scope = plans.SessionScope(reads=("A", "B"), writes=("A",))
        contract = self._contract(repo="A")
        once = repository_registration._migrate_session(
            authoring.render_session_file(
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
        spec = authoring.ManifestSpec(
            plan_title="Plan",
            source_path="/tmp/plan.md",
            output_dir="/tmp/plan",
            plan_id="abc",
            source_fingerprint="fp",
            content_sha256="sha",
            sessions=(
                authoring.ManifestSession(
                    session_id=plans.parse_session_id("1"), title="One"
                ),
            ),
            split_date="2026-01-01",
            readiness=plans.ReadinessMetadata(
                state="draft",
                readiness_sha256="sha",
                checked_at="2026-01-01T00:00:00+00:00",
            ),
        )

        migrated = repository_registration._insert_registry_table(
            authoring.render_manifest(spec), self.registry
        )

        self.assertEqual(
            authoring.render_manifest(replace(spec, registry=self.registry)),
            migrated,
        )


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
        return (self.plan_dir / f"session-{session_id}.md").read_text(
            encoding="utf-8"
        )

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

        registry = plans.read_repository_registry(self.plan_dir)
        self.assertEqual(("tundra_OAK", "tundra_svc"), registry.aliases)
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertLess(
            manifest.index("## Repositories"), manifest.index("## Sessions")
        )
        self.assertIn("**Content SHA-256**: `deadbeef`", manifest)

        snapshot = plans.inspect_plan(self.plan_dir)
        self.assertEqual(
            ("1", "2"), tuple(status.session.id for status in snapshot.sessions)
        )

        for session_id, expected_scope in derived.scopes.items():
            text = self._session_text(session_id)
            self.assertEqual(
                expected_scope,
                plans.parse_session_scope(text, registry.aliases),
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
            plans.stable_readiness_sha256(self.plan_dir), result.readiness
        )
        readiness = plans.read_readiness_metadata(self.plan_dir)
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
        self.assertIsNone(plans.read_repository_registry(self.plan_dir))
        self.assertEqual(
            ("complete", "pending"),
            tuple(
                status.status
                for status in plans.inspect_plan(self.plan_dir).sessions
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
                for status in plans.inspect_plan(self.plan_dir).sessions
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
        self.assertIsNone(plans.read_repository_registry(self.plan_dir))


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

        result = repository_registration.register_source_plan(
            text, cwd=self.svc
        )

        self.assertEqual("applied", result.status)
        self.assertEqual(self.workspace, result.workspace_root)
        self.assertEqual(("1", "2"), result.sessions_changed)
        migrated = result.markdown
        self.assertLess(
            migrated.index("## Context"), migrated.index("## Repositories")
        )
        self.assertLess(
            migrated.index("## Repositories"),
            migrated.index("## Session 1: First"),
        )

        parsed = authoring.parse_plan_document(migrated)
        self.assertEqual(
            ("tundra_OAK", "tundra_svc"), parsed.registry.aliases
        )
        scopes = {
            session.session_id.value: session.scope
            for session in parsed.sessions
        }
        self.assertEqual(
            plans.SessionScope(
                reads=("tundra_OAK", "tundra_svc"),
                writes=("tundra_OAK", "tundra_svc"),
            ),
            scopes["1"],
        )
        self.assertEqual(
            plans.SessionScope(
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

        parsed = authoring.parse_plan_document(result.markdown)
        contracts = {
            session.session_id.value: parse_completion_contract(
                "\n".join(session.lines),
                session_id=session.session_id,
                repository_registry=parsed.registry,
            )
            for session in parsed.sessions
        }
        first = {
            check.name: check for check in contracts["1"].verify.checks
        }
        self.assertEqual("tundra_svc", first["svc-file"].repo)
        self.assertEqual("src/new.py", first["svc-file"].path)
        self.assertEqual("tundra_OAK", first["oak-suite"].repo)
        self.assertEqual(".", first["oak-suite"].cwd)
        second = {
            check.name: check for check in contracts["2"].verify.checks
        }
        self.assertEqual("tundra_OAK", second["oak-regex"].repo)
        self.assertEqual("README.md", second["oak-regex"].path)

    def test_migrated_source_plan_lints_clean_the_way_split_check_does(
        self,
    ) -> None:
        result = repository_registration.register_source_plan(
            self._source_plan(), cwd=self.svc
        )

        lint = authoring.lint_plan_document(result.markdown, cwd=self.workspace)

        self.assertEqual((), lint.findings)
        self.assertEqual(("1", "2"), lint.session_ids)
        self.assertEqual(
            ("tundra_OAK", "tundra_svc"), lint.registry.aliases
        )

    def test_migrating_a_migrated_plan_is_already_registered_and_verbatim(
        self,
    ) -> None:
        migrated = repository_registration.register_source_plan(
            self._source_plan(), cwd=self.svc
        ).markdown

        again = repository_registration.register_source_plan(
            migrated, cwd=self.svc
        )

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

        result = repository_registration.register_source_plan(
            text, cwd=self.svc
        )

        self.assertEqual("not-required", result.status)
        self.assertEqual(text, result.markdown)

    def test_refusal_emits_no_markdown(self) -> None:
        result = repository_registration.register_source_plan(
            self._source_plan(stray=True), cwd=self.svc
        )

        self.assertEqual("refused", result.status)
        self.assertEqual(
            "contract-path-outside-registry", result.missing_fact
        )
        self.assertIn("'stray'", result.detail)
        self.assertIsNone(result.markdown)

    def test_deriving_from_text_writes_nothing_to_the_workspace(self) -> None:
        before = _tree_state(self.workspace)

        repository_registration.register_source_plan(
            self._source_plan(), cwd=self.svc
        )

        self.assertEqual(before, _tree_state(self.workspace))


if __name__ == "__main__":
    unittest.main()
