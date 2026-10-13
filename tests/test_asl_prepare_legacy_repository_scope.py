"""Prepare behaviour for legacy single-repository footprint and intent."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import (
    authoring,
    cli,
    plans,
    preflight_footprint,
    prepare_expand_ingest,
    prepare_models,
    prepare_packet,
    prepare_parsing,
    prepare_validation_store,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _validator_payload,
    _write_prepare_plan,
    init_multi_repo_workspace,
)


class LegacyRepositoryFootprintPrepareTests(unittest.TestCase):
    """A legacy plan reaching two repositories cannot be expanded as v1."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace = self.tmp_path / "workspace"
        repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = repositories["SVC"]
        self.second = repositories["OAK"]
        self.plan_dir = self.tmp_path / "plan"
        self._write_plan()

    def _write_plan(self) -> None:
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        (self.plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (self.plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        checks = [
            {
                "name": "first",
                "type": "file_exists",
                "path": str(self.first / "README.md"),
            },
            {
                "name": "second",
                "type": "file_exists",
                "path": str(self.second / "README.md"),
            },
        ]
        (self.plan_dir / "session-1.md").write_text(
            "\n".join(
                [
                    "# Session 1: Test",
                    "",
                    authoring.CONTEXT_REFERENCE_LINE,
                    "",
                    "---",
                    "",
                    "Update the retry helper in `foo/bar.py`.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    "- The helper passes tests.",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    json.dumps({"session": "1", "checks": checks}),
                    "```",
                    "",
                    "## Handoff",
                    "",
                    "- **Status**: pending",
                    "- **Files modified**: (none yet)",
                    "- **Key decisions**: (none yet)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def test_build_validation_packet_refuses_a_multi_repo_legacy_plan(self) -> None:
        with self.assertRaises(prepare_models.RepositoryFootprintError) as caught:
            prepare_packet.build_validation_packet(
                self.plan_dir, ("1",), codebase_root=self.first
            )

        payload = caught.exception.payload
        self.assertTrue(payload["registry_required"])
        self.assertFalse(payload["registry_present"])
        self.assertTrue(payload["suggested_registry_table"])

    def test_expand_ingest_refuses_a_v1_payload_with_its_own_reason(self) -> None:
        payload = _validator_payload()
        payload["codebase_root"] = str(plans.canonicalize_path(self.first))
        batch = self.tmp_path / "batch.json"
        batch.write_text(json.dumps(payload), encoding="utf-8")
        args = cli.build_parser().parse_args(
            [
                "expand-ingest",
                str(self.plan_dir),
                "--batch-file",
                str(batch),
                "--json",
            ]
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = prepare_expand_ingest.expand_ingest_from_args(args)

        self.assertEqual(1, code)
        emitted = json.loads(stdout.getvalue())
        self.assertEqual(
            prepare_expand_ingest.EXPAND_INGEST_REASON_REPOSITORY_FOOTPRINT,
            emitted["reason"],
        )
        self.assertTrue(emitted["repository_footprint"]["registry_required"])
        self.assertIsNone(
            prepare_validation_store.load_prepare_validation(self.plan_dir)
        )

    def test_expand_ingest_refuses_a_v1_payload_reporting_repository_ambiguity(
        self,
    ) -> None:
        # A plan whose own footprint is clean, so only the validator's report
        # can refuse it.
        plan_dir = self.tmp_path / "single-repo-plan"
        _write_prepare_plan(plan_dir, ("1",))
        payload = _validator_payload(
            issues=[
                {
                    "issue_type": prepare_models.REPOSITORY_AMBIGUITY_ISSUE,
                    "summary": "`foo/bar.py` names more than one repository",
                    "session_ref": "`foo/bar.py`",
                    "actual": "the path exists in two sibling repositories",
                    "fix": "declare a `## Repositories` registry",
                }
            ]
        )
        payload["codebase_root"] = str(plans.canonicalize_path(self.first))
        with self.assertRaisesRegex(plans.PlanError, "repository ambiguity"):
            prepare_validation_store.ingest_validator_output(plan_dir, payload)


class LegacyRepositoryIntentPrepareTests(unittest.TestCase):
    """A legacy plan that *intends* two repositories cannot be prepared as v1.

    The targets here do not exist yet, so the footprint check cannot see them;
    only intent can.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace = self.tmp_path / "workspace"
        repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = repositories["SVC"]
        self.second = repositories["OAK"]
        self.plan_dir = self.tmp_path / "plan"
        self._write_plan()

    def _write_plan(self) -> None:
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        (self.plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (self.plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        checks = [
            {
                "name": "first",
                "type": "file_exists",
                "path": str(self.first / "src" / "helper.py"),
            },
            {
                "name": "second",
                "type": "file_exists",
                "path": str(self.second / "src" / "helper.py"),
            },
        ]
        (self.plan_dir / "session-1.md").write_text(
            "\n".join(
                [
                    "# Session 1: Test",
                    "",
                    authoring.CONTEXT_REFERENCE_LINE,
                    "",
                    "---",
                    "",
                    "Add the retry helper to both checkouts.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    "- The helper exists in both checkouts.",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    json.dumps({"session": "1", "checks": checks}),
                    "```",
                    "",
                    "## Handoff",
                    "",
                    "- **Status**: pending",
                    "- **Files modified**: (none yet)",
                    "- **Key decisions**: (none yet)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def test_footprint_alone_would_have_let_this_plan_through(self) -> None:
        _, payload = preflight_footprint.plan_repository_footprint(
            self.plan_dir, self.first
        )
        self.assertIsNone(payload)

    def test_build_validation_packet_refuses_a_multi_repo_intent(self) -> None:
        with self.assertRaises(
            prepare_models.RepositoryRegistryRequiredError
        ) as caught:
            prepare_packet.build_validation_packet(
                self.plan_dir, ("1",), codebase_root=self.first
            )

        payload = caught.exception.payload
        self.assertTrue(payload["registry_required"])
        self.assertEqual("repository-registry-required", payload["reason"])
        self.assertEqual(2, payload["git_root_count"])
        message = str(caught.exception)
        self.assertIn("`## Repositories`", message)
        self.assertIn("read and write repository scopes", message)
        self.assertIn("attribute every contract check", message)

    def test_expand_ingest_refuses_with_its_own_reason_and_writes_nothing(
        self,
    ) -> None:
        payload = _validator_payload()
        payload["codebase_root"] = str(plans.canonicalize_path(self.first))
        batch = self.tmp_path / "batch.json"
        batch.write_text(json.dumps(payload), encoding="utf-8")
        args = cli.build_parser().parse_args(
            [
                "expand-ingest",
                str(self.plan_dir),
                "--batch-file",
                str(batch),
                "--json",
            ]
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = prepare_expand_ingest.expand_ingest_from_args(args)

        self.assertEqual(1, code)
        emitted = json.loads(stdout.getvalue())
        self.assertEqual(
            prepare_expand_ingest.EXPAND_INGEST_REASON_REGISTRY_REQUIRED,
            emitted["reason"],
        )
        self.assertTrue(emitted["repository_intent"]["registry_required"])
        self.assertIsNone(
            prepare_validation_store.load_prepare_validation(self.plan_dir)
        )

    def test_a_stored_aggregate_is_refused_on_resume(self) -> None:
        aggregate = prepare_parsing.parse_validator_output(
            {
                **_validator_payload(),
                "codebase_root": str(plans.canonicalize_path(self.first)),
            }
        )
        with self.assertRaises(prepare_models.RepositoryRegistryRequiredError):
            prepare_validation_store._validate_validation_authority(
                self.plan_dir, aggregate
            )

    def test_a_single_repository_legacy_plan_still_prepares(self) -> None:
        plan_dir = self.tmp_path / "single-repo-plan"
        _write_prepare_plan(plan_dir, ("1",))

        packet = prepare_packet.build_validation_packet(
            plan_dir, ("1",), codebase_root=self.first
        )

        self.assertEqual(
            prepare_models.LEGACY_PREPARE_VALIDATION_VERSION, packet.version
        )
