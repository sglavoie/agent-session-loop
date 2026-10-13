"""ParserAndStateTests (part 5).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_session_loop import (
    contracts,
    plans_content_hash,
    plans_errors,
    plans_models,
    plans_readiness,
    plans_session_ids,
)
from agent_session_loop.workspaces import Repository, RepositoryRegistry
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_stable_readiness_sha256_ignores_readiness_metadata_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

            plans_readiness.write_readiness_metadata(
                plan_dir,
                plans_models.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=baseline,
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

            plans_readiness.write_readiness_metadata(
                plan_dir,
                plans_models.ReadinessMetadata(
                    state="reviewed",
                    readiness_sha256="differenthash",
                    checked_at="2027-01-01T00:00:00Z",
                ),
            )
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_stable_readiness_sha256_changes_when_instructions_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            session_path.write_text(
                text.replace("Instructions.", "Instructions changed."),
                encoding="utf-8",
            )

            self.assertNotEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_stable_readiness_sha256_tracks_expansion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

            expansion = plan_dir / "session-1.expand.md"
            expansion.write_text("notes\n", encoding="utf-8")
            with_expansion = plans_content_hash.stable_readiness_sha256(plan_dir)
            self.assertNotEqual(baseline, with_expansion)

            expansion.write_text("notes changed\n", encoding="utf-8")
            self.assertNotEqual(
                with_expansion, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_stable_readiness_sha256_ignores_parallel_validation_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

            validation = plan_dir / "parallel-validation.md"
            validation.write_text(
                "# Parallel Validation\n\nany content at all\n",
                encoding="utf-8",
            )
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

            validation.write_text(
                "# Parallel Validation\n\nentirely different content\n",
                encoding="utf-8",
            )
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_stable_readiness_sha256_ignores_legacy_parallel_sections(
        self,
    ) -> None:
        baseline_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with_parallel_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: serial-required",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            session_path.write_text(baseline_session, encoding="utf-8")
            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n## Parallel Execution\n\nLegacy scheduling text.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

            session_path.write_text(with_parallel_session, encoding="utf-8")
            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_plan_content_sha256_ignores_parallel_validation_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans_content_hash.plan_content_sha256(plan_dir)

            validation = plan_dir / "parallel-validation.md"
            validation.write_text("anything\n", encoding="utf-8")
            self.assertEqual(baseline, plans_content_hash.plan_content_sha256(plan_dir))

            validation.write_text("anything else\n", encoding="utf-8")
            self.assertEqual(baseline, plans_content_hash.plan_content_sha256(plan_dir))

    def test_plan_content_sha256_ignores_legacy_parallel_sections(self) -> None:
        baseline_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with_parallel_session = "\n".join(
            [
                "# Session 1: Test",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: serial-required",
                "",
                "## Instructions",
                "",
                "Do the thing.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            session_path.write_text(baseline_session, encoding="utf-8")
            baseline = plans_content_hash.plan_content_sha256(plan_dir)

            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8")
                + "\n## Parallel Execution\n\nLegacy scheduling text.\n",
                encoding="utf-8",
            )
            self.assertEqual(baseline, plans_content_hash.plan_content_sha256(plan_dir))

            session_path.write_text(with_parallel_session, encoding="utf-8")
            self.assertEqual(baseline, plans_content_hash.plan_content_sha256(plan_dir))

    def test_plan_content_sha256_ignores_readiness_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            baseline = plans_content_hash.plan_content_sha256(plan_dir)

            for state, checked_at in (
                ("loop-ready", "2026-06-19T00:00:00Z"),
                ("running", "2026-06-19T00:00:01Z"),
                ("complete", "2026-06-19T00:00:02Z"),
            ):
                plans_readiness.write_readiness_metadata(
                    plan_dir,
                    plans_models.ReadinessMetadata(
                        state=state,
                        readiness_sha256=plans_content_hash.stable_readiness_sha256(
                            plan_dir
                        ),
                        checked_at=checked_at,
                    ),
                )
                self.assertEqual(
                    baseline, plans_content_hash.plan_content_sha256(plan_dir)
                )

    def test_plan_content_sha256_still_moves_on_a_manifest_status_change(self) -> None:
        manifest = "\n".join(
            [
                "# Manifest",
                "",
                "| Session | Status |",
                "| --- | --- |",
                "| 1 | {status} |",
                "",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                manifest.format(status="pending"), encoding="utf-8"
            )
            baseline = plans_content_hash.plan_content_sha256(plan_dir)
            readiness_baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

            manifest_path.write_text(
                manifest.format(status="complete"), encoding="utf-8"
            )
            self.assertNotEqual(
                baseline, plans_content_hash.plan_content_sha256(plan_dir)
            )
            self.assertEqual(
                readiness_baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_parse_completion_contract_returns_none_when_section_absent(self) -> None:
        text = "\n".join(
            [
                "# Session 1: No contract",
                "",
                "Instructions.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )

        self.assertIsNone(contracts.parse_completion_contract(text, session_id="1"))

    def test_parse_completion_contract_parses_done_when_bullets_and_recipe(
        self,
    ) -> None:
        text = "\n".join(
            [
                "# Session 2: Contract Parser",
                "",
                "Instructions.",
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "- contracts.py exists",
                "- parser returns CompletionContract",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "session": "2",',
                '  "checks": [',
                "    {",
                '      "name": "module-exists",',
                '      "type": "file_exists",',
                '      "path": "agent_session_loop/contracts.py"',
                "    },",
                "    {",
                '      "type": "command",',
                '      "argv": ["python3", "-m", "unittest", "tests.t"],',
                '      "timeout_seconds": 30',
                "    },",
                "    {",
                '      "type": "regex_in_file",',
                '      "path": "agent_session_loop/contracts.py",',
                r'      "pattern": "^def parse_completion_contract"',
                "    }",
                "  ]",
                "}",
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id=2)

        self.assertIsNotNone(contract)
        self.assertEqual("2", contract.session_id.value)
        self.assertEqual(
            ("contracts.py exists", "parser returns CompletionContract"),
            contract.done_when,
        )
        kinds = [check.kind for check in contract.verify.checks]
        self.assertEqual(["file_exists", "command", "regex_in_file"], kinds)
        file_check, command_check, regex_check = contract.verify.checks
        self.assertEqual("module-exists", file_check.name)
        self.assertEqual("agent_session_loop/contracts.py", file_check.path)
        self.assertEqual("check-2", command_check.name)
        self.assertEqual(("python3", "-m", "unittest", "tests.t"), command_check.argv)
        self.assertEqual(30.0, command_check.timeout_seconds)
        self.assertIsNone(command_check.cwd)
        self.assertEqual("check-3", regex_check.name)
        self.assertEqual("^def parse_completion_contract", regex_check.pattern)

    def test_repository_aware_contract_parses_every_check_kind(self) -> None:
        registry = RepositoryRegistry(
            (
                Repository("SVC", "svc", "https://example.com/svc.git"),
                Repository("OAK", "oak", "git@example.com:org/oak.git"),
            )
        )
        payload = {
            "checks": [
                {
                    "name": "command",
                    "type": "command",
                    "repo": "SVC",
                    "argv": ["python3", "-V"],
                    "cwd": "tools",
                },
                {
                    "name": "file",
                    "type": "file_exists",
                    "repo": "OAK",
                    "path": "README.md",
                },
                {
                    "name": "regex",
                    "type": "regex_in_file",
                    "repo": "SVC",
                    "path": "src/main.py",
                    "pattern": "main",
                },
            ]
        }
        text = (
            "## Completion Contract\n\n### Done when\n\n- done\n\n"
            "### Verify\n\n```json\n" + json.dumps(payload) + "\n```\n"
        )

        contract = contracts.parse_completion_contract(
            text, session_id="1", repository_registry=registry
        )

        self.assertEqual(
            ["SVC", "OAK", "SVC"],
            [check.repo for check in contract.verify.checks],
        )

    def test_repository_aware_contract_requires_known_repo_in_multi_registry(
        self,
    ) -> None:
        registry = RepositoryRegistry(
            (
                Repository("SVC", "svc", "https://example.com/svc.git"),
                Repository("OAK", "oak", "https://example.com/oak.git"),
            )
        )
        base = {
            "type": "file_exists",
            "path": "README.md",
        }
        for label, check, pattern in (
            ("missing", base, "repo.*required"),
            ("unknown", {**base, "repo": "OWL"}, "unknown repository alias"),
            ("malformed", {**base, "repo": "bad alias"}, "repo.*invalid"),
        ):
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(plans_errors.PlanError, pattern),
            ):
                contracts._parse_verify_payload(
                    {"checks": [check]},
                    session_id=plans_session_ids.parse_session_id("1"),
                    repository_registry=registry,
                )

    def test_single_repository_contract_resolves_omitted_repo(self) -> None:
        registry = RepositoryRegistry(
            (Repository("SVC", "svc", "https://example.com/svc.git"),)
        )
        recipe = contracts._parse_verify_payload(
            {
                "checks": [
                    {"type": "file_exists", "path": "README.md"},
                    {"type": "command", "argv": ["true"]},
                ]
            },
            session_id=plans_session_ids.parse_session_id("1"),
            repository_registry=registry,
        )

        self.assertEqual(["SVC", "SVC"], [check.repo for check in recipe.checks])

    def test_repository_aware_contract_rejects_escaping_paths_and_cwds(self) -> None:
        registry = RepositoryRegistry(
            (Repository("SVC", "svc", "https://example.com/svc.git"),)
        )
        cases = (
            ({"type": "file_exists", "path": "/tmp/x"}, "path"),
            ({"type": "regex_in_file", "path": "../x", "pattern": "x"}, "path"),
            ({"type": "command", "argv": ["true"], "cwd": "a/../../b"}, "cwd"),
            ({"type": "command", "argv": ["true"], "cwd": "C:\\tmp"}, "cwd"),
        )
        for check, field in cases:
            with (
                self.subTest(check=check),
                self.assertRaisesRegex(plans_errors.PlanError, field + ".*beneath"),
            ):
                contracts._parse_verify_payload(
                    {"checks": [check]},
                    session_id=plans_session_ids.parse_session_id("1"),
                    repository_registry=registry,
                )

    def test_legacy_contract_keeps_absolute_paths_and_omitted_repo(self) -> None:
        recipe = contracts._parse_verify_payload(
            {
                "checks": [
                    {"type": "file_exists", "path": "/tmp/legacy"},
                    {"type": "command", "argv": ["true"], "cwd": "/tmp"},
                ]
            },
            session_id=plans_session_ids.parse_session_id("1"),
        )

        self.assertEqual([None, None], [check.repo for check in recipe.checks])

    def test_parse_completion_contract_requires_done_when_bullets(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "file_exists", "path": "a"}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans_errors.PlanError, "Done when"):
            contracts.parse_completion_contract(text, session_id="1")
