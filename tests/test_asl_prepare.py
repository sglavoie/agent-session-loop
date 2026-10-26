"""Prepare validator schema, expansion renderer, and prepare CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    authoring_render,
    plans_errors,
    plans_metadata,
    prepare_expand_ingest,
    prepare_models,
    prepare_packet,
    prepare_parsing,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _prepare_pipeline_payload,
    _validator_payload,
    init_multi_repo_workspace,
)
from tests.support_asl_prepare import PrepareValidatorFixtures


class PrepareValidatorPayloadTests(PrepareValidatorFixtures):
    def test_parse_valid_payload_round_trips_through_to_dict(self) -> None:
        payload = _validator_payload(
            issues=[
                {
                    "issue_type": "path-missing",
                    "summary": "wrong path",
                    "session_ref": "`foo/old.py`",
                    "actual": "file lives at foo/bar.py",
                    "fix": "use foo/bar.py",
                }
            ],
            details=[{"path": "foo/bar.py", "notes": ["Lines 1-3: retry helper"]}],
            patterns=[{"name": "retry", "location": "foo/bar.py:1-3", "reason": "r"}],
            dependencies=[
                {"direction": "provides", "session_id": "2", "description": "helper"}
            ],
            corrections=[
                {
                    "expected_text": "`foo/old.py`",
                    "replacement_text": "`foo/bar.py`",
                    "reason": "moved",
                }
            ],
        )

        validation = prepare_parsing.parse_validator_output(payload)

        self.assertEqual(
            prepare_models.LEGACY_PREPARE_VALIDATION_VERSION, validation.version
        )
        self.assertEqual("/repo", validation.codebase_root)
        self.assertEqual(("1",), validation.session_ids)
        session = validation.sessions[0]
        # 1 claim + 1 issue + parsed contract.
        self.assertEqual(3, session.claims_checked)
        reparsed = prepare_parsing.parse_validator_output(validation.to_dict())
        self.assertEqual(validation, reparsed)

    def test_rejects_wrong_version(self) -> None:
        payload = _validator_payload()
        payload["version"] = 99
        with self.assertRaisesRegex(plans_errors.PlanError, "unsupported version"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_a_version_that_only_equals_an_integer(self) -> None:
        # True == 1 and 1.0 == 1, but neither is version 1, and accepting one
        # would store it back into prepare-validation.json as written.
        for version in (True, 1.0, 2.0):
            with self.subTest(version=version):
                payload = _validator_payload()
                payload["version"] = version
                with self.assertRaisesRegex(
                    plans_errors.PlanError, f"unsupported version {version!r}"
                ):
                    prepare_parsing.parse_validator_output(payload)

    def test_v2_requires_repo_attribution_on_every_finding_kind(self) -> None:
        payload = {
            "version": prepare_models.PREPARE_VALIDATION_VERSION,
            "workspace_root": "/workspace",
            "repositories": [
                {
                    "alias": "SVC",
                    "path": "svc",
                    "origin": "github.com/org/svc",
                    "root": "/workspace/svc",
                }
            ],
            "sessions": _validator_payload()["sessions"],
        }
        with self.assertRaisesRegex(plans_errors.PlanError, "claim #1 'repo'"):
            prepare_parsing.parse_validator_output(payload)

    def test_registered_packet_contains_exact_roots_and_declared_reads(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            init_multi_repo_workspace(workspace, specs)
            plan_dir = root / "plan"
            self._write_registered_plan(plan_dir, specs)

            packet = prepare_packet.build_validation_packet(
                plan_dir, ("1", "2"), codebase_root=workspace
            )
            rendered = packet.render()

            self.assertEqual(prepare_models.PREPARE_VALIDATION_VERSION, packet.version)
            self.assertEqual(
                ("SVC", "OAK"), tuple(r.alias for r in packet.repositories)
            )
            self.assertIn(str((workspace / specs[0][1]).resolve()), rendered)
            self.assertIn("declared reads", rendered)
            self.assertIn("not filesystem isolation", rendered)
            self.assertIn("repository-ambiguous", rendered)

    def test_registered_ingest_binds_and_renders_qualified_evidence(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            init_multi_repo_workspace(workspace, specs)
            plan_dir = root / "plan"
            self._write_registered_plan(plan_dir, specs)
            packet = prepare_packet.build_validation_packet(
                plan_dir, ("1",), codebase_root=workspace
            )
            payload = _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
                corrections_by_id={
                    "1": [
                        {
                            "expected_text": "Instructions.",
                            "replacement_text": "Updated instructions.",
                            "reason": "renamed helper",
                        }
                    ]
                },
            )

            result = prepare_expand_ingest.ingest_expand_batch(
                plan_dir, payload, expanded="2026-08-08"
            )

            metadata = plans_metadata.load_plan_metadata(plan_dir)
            self.assertIsInstance(metadata, plans_metadata.WorkspacePlanMetadata)
            self.assertEqual(("1",), result.batch_session_ids)
            expansion = (plan_dir / "session-1.expand.md").read_text(encoding="utf-8")
            self.assertIn("**Workspace root**", expansion)
            self.assertIn("`SVC:demo/module.py`", expansion)
            self.assertIn(
                "Updated instructions.",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_registered_ingest_rejects_authority_mismatches_without_mutation(
        self,
    ) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        mutations = (
            (
                "workspace",
                lambda payload: payload.__setitem__("workspace_root", "/wrong"),
            ),
            (
                "registry",
                lambda payload: payload["repositories"][0].__setitem__(
                    "root", "/wrong/svc"
                ),
            ),
            (
                "session",
                lambda payload: payload["sessions"][0].__setitem__("session_id", "9"),
            ),
            (
                "repo",
                lambda payload: payload["sessions"][0]["claims"][0].__setitem__(
                    "repo", "UNKNOWN"
                ),
            ),
            (
                "dependency-session",
                lambda payload: payload["sessions"][0].__setitem__(
                    "dependencies",
                    [
                        {
                            "direction": "provides",
                            "session_id": "9",
                            "description": "unknown target",
                            "repo": "SVC",
                        }
                    ],
                ),
            ),
            (
                "correction-repository",
                lambda payload: payload["sessions"][0].__setitem__(
                    "corrections",
                    [
                        {
                            "expected_text": "`OAK:old.py`",
                            "replacement_text": "`OAK:new.py`",
                            "reason": "crossed repository",
                            "repo": "SVC",
                        }
                    ],
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workspace = root / "workspace"
                init_multi_repo_workspace(workspace, specs)
                plan_dir = root / "plan"
                self._write_registered_plan(plan_dir, specs)
                packet = prepare_packet.build_validation_packet(
                    plan_dir, ("1",), codebase_root=workspace
                )
                payload = _prepare_pipeline_payload(
                    packet.codebase_root,
                    packet.session_ids,
                    version=packet.version,
                    repositories=packet.repositories,
                    session_repositories=packet.session_read_repositories,
                )
                mutate(payload)
                before = {
                    path.name: path.read_bytes()
                    for path in plan_dir.iterdir()
                    if path.is_file()
                }

                with self.assertRaises(plans_errors.PlanError):
                    prepare_expand_ingest.ingest_expand_batch(plan_dir, payload)

                after = {
                    path.name: path.read_bytes()
                    for path in plan_dir.iterdir()
                    if path.is_file()
                }
                self.assertEqual(before, after)

    def test_registered_plan_rejects_legacy_validator_output(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            init_multi_repo_workspace(workspace, specs)
            plan_dir = root / "plan"
            self._write_registered_plan(plan_dir, specs)
            before = {
                path.name: path.read_bytes()
                for path in plan_dir.iterdir()
                if path.is_file()
            }

            with self.assertRaisesRegex(
                plans_errors.PlanError, "require validator output"
            ):
                prepare_expand_ingest.ingest_expand_batch(
                    plan_dir, _validator_payload()
                )

            after = {
                path.name: path.read_bytes()
                for path in plan_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_registered_first_binding_is_atomic_with_late_render_failure(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            init_multi_repo_workspace(workspace, specs)
            plan_dir = root / "plan"
            self._write_registered_plan(plan_dir, specs)
            packet = prepare_packet.build_validation_packet(
                plan_dir, ("1",), codebase_root=workspace
            )
            payload = _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    authoring_render.CONTEXT_REFERENCE_LINE + "\n", ""
                ),
                encoding="utf-8",
            )
            before = {
                path.name: path.read_bytes()
                for path in plan_dir.iterdir()
                if path.is_file()
            }

            with self.assertRaisesRegex(plans_errors.PlanError, "Before starting"):
                prepare_expand_ingest.ingest_expand_batch(plan_dir, payload)

            after = {
                path.name: path.read_bytes()
                for path in plan_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse(
                (plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME).exists()
            )

    def test_rejects_unknown_top_level_keys(self) -> None:
        payload = _validator_payload()
        payload["extra"] = True
        with self.assertRaisesRegex(plans_errors.PlanError, "unknown keys: extra"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_empty_or_missing_sessions(self) -> None:
        payload = _validator_payload()
        payload["sessions"] = []
        with self.assertRaisesRegex(plans_errors.PlanError, "'sessions'"):
            prepare_parsing.parse_validator_output(payload)
        del payload["sessions"]
        with self.assertRaisesRegex(plans_errors.PlanError, "'sessions'"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_duplicate_session_ids(self) -> None:
        payload = _validator_payload()
        payload["sessions"] = [payload["sessions"][0], payload["sessions"][0]]
        with self.assertRaisesRegex(plans_errors.PlanError, "duplicate session id"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_invalid_session_id(self) -> None:
        payload = _validator_payload(session_id="01")
        with self.assertRaisesRegex(plans_errors.PlanError, "session_id"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_unknown_claim_type(self) -> None:
        payload = _validator_payload(
            claims=[
                {
                    "claim": "x",
                    "claim_type": "Z",
                    "location": "a.py",
                    "line": 1,
                    "to_be_created": False,
                }
            ]
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "claim_type"):
            prepare_parsing.parse_validator_output(payload)

    def test_rejects_claim_missing_location(self) -> None:
        payload = _validator_payload(
            claims=[{"claim": "x", "claim_type": "P", "line": 1}]
        )
        with self.assertRaisesRegex(plans_errors.PlanError, "location"):
            prepare_parsing.parse_validator_output(payload)

    def test_to_be_created_claim_drops_a_supplied_line(self) -> None:
        payload = _validator_payload(
            claims=[
                {
                    "claim": "NewTests",
                    "claim_type": "S",
                    "location": prepare_models.TO_BE_CREATED_LOCATION,
                    "line": 5,
                    "to_be_created": True,
                }
            ]
        )

        claim = prepare_parsing.parse_validator_output(payload).sessions[0].claims[0]

        self.assertTrue(claim.to_be_created)
        self.assertIsNone(claim.line)

    def test_claim_line_degrades_to_null_or_leading_integer(self) -> None:
        cases = (
            (7, 7),
            ("42", 42),
            ("42-50", 42),
            (0, None),
            (-3, None),
            ("n/a", None),
            (True, None),
            (1.5, None),
            (None, None),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                payload = _validator_payload(
                    claims=[
                        {
                            "claim": "x",
                            "claim_type": "P",
                            "location": "a.py",
                            "line": raw,
                            "to_be_created": False,
                        }
                    ]
                )

                claim = (
                    prepare_parsing.parse_validator_output(payload)
                    .sessions[0]
                    .claims[0]
                )

                self.assertEqual(expected, claim.line)

    def test_registered_contract_problem_accepts_repo(self) -> None:
        session = _validator_payload(
            claims=[],
            contract_checked=True,
            contract_problems=[{"message": "m", "suggested_fix": "f", "repo": "SVC"}],
        )["sessions"][0]

        parsed = prepare_parsing.parse_validator_output(
            self._registered_envelope(session)
        )

        self.assertEqual("m", parsed.sessions[0].contract_problems[0].message)


if __name__ == "__main__":
    unittest.main()
