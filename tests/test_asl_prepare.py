"""Prepare validator schema, expansion renderer, and prepare CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shlex
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    authoring,
    baseline,
    baseline_cache,
    cli,
    contracts,
    harnesses,
    plans,
    preflight,
    prepare,
    state,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _prepare_pipeline_payload,
    _recording_validator,
    _validator_payload,
    _write_prepare_plan,
    _write_prepare_source_plan,
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)


class PrepareValidatorSchemaTests(unittest.TestCase):
    """Versioned validator result schema (`agent_session_loop.prepare`)."""

    @staticmethod
    def _write_registered_plan(
        plan_dir: Path,
        specs: tuple[tuple[str, str, str], ...],
    ) -> None:
        write_multi_repo_plan(plan_dir, ("pending", "pending"), specs=specs)
        for session_path in sorted(plan_dir.glob("session-*.md")):
            text = session_path.read_text(encoding="utf-8")
            heading, remainder = text.split("\n", 1)
            session_path.write_text(
                f"{heading}\n{authoring.CONTEXT_REFERENCE_LINE}\n{remainder}",
                encoding="utf-8",
            )

    @staticmethod
    def _registered_envelope(session: dict) -> dict:
        return {
            "version": prepare.PREPARE_VALIDATION_VERSION,
            "workspace_root": "/workspace",
            "repositories": [
                {
                    "alias": "SVC",
                    "path": "svc",
                    "origin": "github.com/org/svc",
                    "root": "/workspace/svc",
                }
            ],
            "sessions": [session],
        }

    @staticmethod
    def _removal_overrides(*, repo: str | None = None) -> dict:
        claim: dict[str, Any] = {
            "claim": "retry backoff branch",
            "claim_type": "R",
            "location": "foo/bar.py — added in a1b2c3d, covered by test_retry.py:40",
            "line": 12,
            "to_be_created": False,
        }
        issue: dict[str, Any] = {
            "issue_type": "removal-unjustified",
            "summary": "no rationale for dropping the backoff",
            "session_ref": "delete the backoff branch",
            "actual": "no commit or covering test explains the branch",
            "fix": "record the origin rationale before removing it",
        }
        if repo is not None:
            claim["repo"] = repo
            issue["repo"] = repo
        return {"claims": [claim], "issues": [issue]}

    # -- fail-closed parsing -------------------------------------------------

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

        validation = prepare.parse_validator_output(payload)

        self.assertEqual(prepare.LEGACY_PREPARE_VALIDATION_VERSION, validation.version)
        self.assertEqual("/repo", validation.codebase_root)
        self.assertEqual(("1",), validation.session_ids)
        session = validation.sessions[0]
        # 1 claim + 1 issue + parsed contract.
        self.assertEqual(3, session.claims_checked)
        reparsed = prepare.parse_validator_output(validation.to_dict())
        self.assertEqual(validation, reparsed)

    def test_rejects_wrong_version(self) -> None:
        payload = _validator_payload()
        payload["version"] = 99
        with self.assertRaisesRegex(plans.PlanError, "unsupported version"):
            prepare.parse_validator_output(payload)

    def test_v2_requires_repo_attribution_on_every_finding_kind(self) -> None:
        payload = {
            "version": prepare.PREPARE_VALIDATION_VERSION,
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
        with self.assertRaisesRegex(plans.PlanError, "claim #1 'repo'"):
            prepare.parse_validator_output(payload)

    def test_registered_packet_contains_exact_roots_and_declared_reads(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            init_multi_repo_workspace(workspace, specs)
            plan_dir = root / "plan"
            self._write_registered_plan(plan_dir, specs)

            packet = prepare.build_validation_packet(
                plan_dir, ("1", "2"), codebase_root=workspace
            )
            rendered = packet.render()

            self.assertEqual(prepare.PREPARE_VALIDATION_VERSION, packet.version)
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
            packet = prepare.build_validation_packet(
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

            result = prepare.ingest_expand_batch(
                plan_dir, payload, expanded="2026-08-08"
            )

            metadata = plans.load_plan_metadata(plan_dir)
            self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
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
                packet = prepare.build_validation_packet(
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

                with self.assertRaises(plans.PlanError):
                    prepare.ingest_expand_batch(plan_dir, payload)

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

            with self.assertRaisesRegex(plans.PlanError, "require validator output"):
                prepare.ingest_expand_batch(plan_dir, _validator_payload())

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
            packet = prepare.build_validation_packet(
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
                    authoring.CONTEXT_REFERENCE_LINE + "\n", ""
                ),
                encoding="utf-8",
            )
            before = {
                path.name: path.read_bytes()
                for path in plan_dir.iterdir()
                if path.is_file()
            }

            with self.assertRaisesRegex(plans.PlanError, "Before starting"):
                prepare.ingest_expand_batch(plan_dir, payload)

            after = {
                path.name: path.read_bytes()
                for path in plan_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse((plan_dir / plans.PLAN_METADATA_FILE_NAME).exists())

    def test_rejects_unknown_top_level_keys(self) -> None:
        payload = _validator_payload()
        payload["extra"] = True
        with self.assertRaisesRegex(plans.PlanError, "unknown keys: extra"):
            prepare.parse_validator_output(payload)

    def test_rejects_empty_or_missing_sessions(self) -> None:
        payload = _validator_payload()
        payload["sessions"] = []
        with self.assertRaisesRegex(plans.PlanError, "'sessions'"):
            prepare.parse_validator_output(payload)
        del payload["sessions"]
        with self.assertRaisesRegex(plans.PlanError, "'sessions'"):
            prepare.parse_validator_output(payload)

    def test_rejects_duplicate_session_ids(self) -> None:
        payload = _validator_payload()
        payload["sessions"] = [payload["sessions"][0], payload["sessions"][0]]
        with self.assertRaisesRegex(plans.PlanError, "duplicate session id"):
            prepare.parse_validator_output(payload)

    def test_rejects_invalid_session_id(self) -> None:
        payload = _validator_payload(session_id="01")
        with self.assertRaisesRegex(plans.PlanError, "session_id"):
            prepare.parse_validator_output(payload)

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
        with self.assertRaisesRegex(plans.PlanError, "claim_type"):
            prepare.parse_validator_output(payload)

    def test_rejects_claim_missing_location(self) -> None:
        payload = _validator_payload(
            claims=[{"claim": "x", "claim_type": "P", "line": 1}]
        )
        with self.assertRaisesRegex(plans.PlanError, "location"):
            prepare.parse_validator_output(payload)

    def test_to_be_created_claim_drops_a_supplied_line(self) -> None:
        payload = _validator_payload(
            claims=[
                {
                    "claim": "NewTests",
                    "claim_type": "S",
                    "location": prepare.TO_BE_CREATED_LOCATION,
                    "line": 5,
                    "to_be_created": True,
                }
            ]
        )

        claim = prepare.parse_validator_output(payload).sessions[0].claims[0]

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

                claim = prepare.parse_validator_output(payload).sessions[0].claims[0]

                self.assertEqual(expected, claim.line)

    def test_registered_contract_problem_accepts_repo(self) -> None:
        session = _validator_payload(
            claims=[],
            contract_checked=True,
            contract_problems=[{"message": "m", "suggested_fix": "f", "repo": "SVC"}],
        )["sessions"][0]

        parsed = prepare.parse_validator_output(self._registered_envelope(session))

        self.assertEqual("m", parsed.sessions[0].contract_problems[0].message)

    def _scripted_validator(
        self, *messages: str
    ) -> tuple[tuple[Any, mock.Mock], list[str]]:
        prompts: list[str] = []
        replies = iter(messages)

        def run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            prompts.append(str(kwargs["prompt"]))
            return harnesses.HarnessResult(
                argv=[name],
                returncode=0,
                stdout="",
                stderr="",
                last_message=next(replies),
                name=name,
            )

        packet = mock.Mock(codebase_root="/repo", plan_dir="/plan")
        packet.render.return_value = "PACKET"
        launch = mock.patch.object(
            prepare.harnesses, "run_harness", side_effect=run_harness
        )
        return (launch, packet), prompts

    def test_launch_validator_repairs_a_rejected_shape(self) -> None:
        bad = _validator_payload(issues=[{"issue_type": "bogus"}])
        good = _validator_payload()
        (launch, packet), prompts = self._scripted_validator(
            json.dumps(bad), json.dumps(good)
        )

        with launch, redirect_stderr(io.StringIO()) as stderr:
            payload = prepare._launch_validator("claude", packet)

        self.assertEqual(good, payload)
        self.assertEqual(2, len(prompts))
        self.assertIn("issue_type", prompts[1])
        self.assertIn(json.dumps(bad), prompts[1])
        self.assertIn("repair it (1/2)", stderr.getvalue())

    def test_launch_validator_gives_up_after_bounded_repairs(self) -> None:
        (launch, packet), prompts = self._scripted_validator("nope", "nope", "nope")

        with (
            launch,
            redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(plans.PlanError, "invalid JSON"),
        ):
            prepare._launch_validator("claude", packet)

        self.assertEqual(1 + prepare.VALIDATOR_REPAIR_ATTEMPTS, len(prompts))

    def test_legacy_contract_problem_rejects_repo(self) -> None:
        payload = _validator_payload(
            contract_checked=True,
            contract_problems=[{"message": "m", "suggested_fix": "f", "repo": "SVC"}],
        )
        with self.assertRaisesRegex(plans.PlanError, "unknown keys: repo"):
            prepare.parse_validator_output(payload)

    def test_rejects_unknown_issue_type(self) -> None:
        payload = _validator_payload(
            issues=[
                {
                    "issue_type": "design-change",
                    "summary": "s",
                    "session_ref": "r",
                    "actual": "a",
                    "fix": "f",
                }
            ]
        )
        with self.assertRaisesRegex(plans.PlanError, "issue_type"):
            prepare.parse_validator_output(payload)

    def test_accepts_removal_taxonomy_in_legacy_payload(self) -> None:
        payload = _validator_payload(**self._removal_overrides())

        session = prepare.parse_validator_output(payload).sessions[0]

        self.assertEqual("R", session.claims[0].claim_type)
        self.assertIsNone(session.claims[0].repo)
        self.assertEqual("removal-unjustified", session.issues[0].issue_type)

    def test_accepts_removal_taxonomy_in_registered_payload(self) -> None:
        payload = self._registered_envelope(
            _validator_payload(**self._removal_overrides(repo="SVC"))["sessions"][0]
        )

        session = prepare.parse_validator_output(payload).sessions[0]

        self.assertEqual(
            ("R", "SVC"), (session.claims[0].claim_type, session.claims[0].repo)
        )
        self.assertEqual(
            ("removal-unjustified", "SVC"),
            (session.issues[0].issue_type, session.issues[0].repo),
        )

    def test_rejects_unknown_taxonomy_in_both_versions_without_writing(self) -> None:
        mutations = (
            (
                "claim_type",
                lambda session: session["claims"][0].__setitem__("claim_type", "X"),
            ),
            (
                "issue_type",
                lambda session: session["issues"][0].__setitem__(
                    "issue_type", "removal-justified"
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                plan_dir = Path(tmp) / "plan"
                _write_prepare_plan(plan_dir)
                payload = _validator_payload(**self._removal_overrides())
                mutate(payload["sessions"][0])
                before = {
                    path.name: path.read_bytes()
                    for path in plan_dir.iterdir()
                    if path.is_file()
                }

                with self.assertRaisesRegex(plans.PlanError, label):
                    prepare.ingest_validator_output(plan_dir, payload)

                after = {
                    path.name: path.read_bytes()
                    for path in plan_dir.iterdir()
                    if path.is_file()
                }
                self.assertEqual(before, after)

                registered_session = _validator_payload(
                    **self._removal_overrides(repo="SVC")
                )["sessions"][0]
                mutate(registered_session)
                with self.assertRaisesRegex(plans.PlanError, label):
                    prepare.parse_validator_output(
                        self._registered_envelope(registered_session)
                    )

    def test_rejects_more_than_three_patterns(self) -> None:
        pattern = {"name": "p", "location": "a.py:1", "reason": "r"}
        payload = _validator_payload(
            patterns=[pattern, dict(pattern), dict(pattern), dict(pattern)]
        )
        with self.assertRaisesRegex(plans.PlanError, "at most 3"):
            prepare.parse_validator_output(payload)

    def test_rejects_correction_with_identical_texts(self) -> None:
        payload = _validator_payload(
            corrections=[
                {"expected_text": "same", "replacement_text": "same", "reason": "r"}
            ]
        )
        with self.assertRaisesRegex(plans.PlanError, "must differ"):
            prepare.parse_validator_output(payload)

    def test_rejects_contract_problems_without_contract_checked(self) -> None:
        payload = _validator_payload(
            contract_checked=False,
            contract_problems=[{"message": "m", "suggested_fix": "f"}],
        )
        with self.assertRaisesRegex(plans.PlanError, "contract_checked"):
            prepare.parse_validator_output(payload)

    def test_rejects_invalid_dependency_direction(self) -> None:
        payload = _validator_payload(
            dependencies=[
                {"direction": "blocks", "session_id": "2", "description": "d"}
            ]
        )
        with self.assertRaisesRegex(plans.PlanError, "direction"):
            prepare.parse_validator_output(payload)

    # -- bounded batches and packets ------------------------------------------

    def test_batch_sessions_bounds_batch_size(self) -> None:
        batches = prepare.batch_sessions(["1", "2", "2.1", "3", "4"], 2)
        self.assertEqual((("1", "2"), ("2.1", "3"), ("4",)), batches)

    def test_batch_sessions_rejects_nonpositive_batch_size(self) -> None:
        with self.assertRaisesRegex(plans.PlanError, "batch size"):
            prepare.batch_sessions(["1"], 0)

    def test_build_validation_packet_references_paths_not_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))

            packet = prepare.build_validation_packet(
                plan_dir, ["1", "2"], codebase_root="/repo"
            )
            rendered = packet.render()

            self.assertTrue(
                rendered.startswith(
                    f"{prepare.VALIDATION_PACKET_HEADING_PREFIX} — sessions 1, 2"
                )
            )
            self.assertIn(str(plan_dir.resolve() / "session-1.md"), rendered)
            self.assertIn(str(plan_dir.resolve() / "context.md"), rendered)
            self.assertIn(
                f'"version": {prepare.LEGACY_PREPARE_VALIDATION_VERSION}',
                rendered,
            )
            self.assertIn("environment and integrations", rendered)
            self.assertIn(
                "Keep factual mismatches distinct from planning advice", rendered
            )
            # Plan file contents are referenced, never inlined.
            self.assertNotIn("Update the retry helper", rendered)
            for issue_type in prepare.ISSUE_TYPES:
                self.assertIn(issue_type, rendered)

    def test_validation_prompt_digest_tracks_the_rendered_packet(self) -> None:
        legacy = prepare._validation_prompt_digest(
            prepare.LEGACY_PREPARE_VALIDATION_VERSION
        )
        registered = prepare._validation_prompt_digest(
            prepare.PREPARE_VALIDATION_VERSION
        )

        self.assertNotEqual(legacy, registered)
        self.assertEqual(
            legacy,
            prepare._validation_prompt_digest(
                prepare.LEGACY_PREPARE_VALIDATION_VERSION
            ),
        )

        original_render = prepare.ValidationTaskPacket.render
        with mock.patch.object(
            prepare.ValidationTaskPacket,
            "render",
            lambda packet: original_render(packet) + "\n- Research removal rationale.",
        ):
            changed = prepare._validation_prompt_digest(
                prepare.LEGACY_PREPARE_VALIDATION_VERSION
            )

        self.assertNotEqual(legacy, changed)

    def test_build_validation_packet_rejects_missing_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            with self.assertRaisesRegex(plans.PlanError, "missing session file"):
                prepare.build_validation_packet(
                    plan_dir, ["1", "9"], codebase_root="/repo"
                )

    # -- derived aggregate store ----------------------------------------------

    def test_ingest_saves_aggregate_and_merges_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))

            first = prepare.ingest_validator_output(plan_dir, _validator_payload())
            self.assertEqual(("1",), first.session_ids)

            second_payload = _validator_payload(session_id="2")
            second = prepare.ingest_validator_output(plan_dir, second_payload)
            self.assertEqual(("1", "2"), second.session_ids)

            stored = prepare.load_prepare_validation(plan_dir)
            assert stored is not None
            self.assertEqual(("1", "2"), stored.session_ids)
            self.assertTrue((plan_dir / prepare.PREPARE_VALIDATION_FILENAME).is_file())

    def test_merge_replaces_existing_session_entry(self) -> None:
        base = prepare.parse_validator_output(_validator_payload())
        update = prepare.parse_validator_output(
            _validator_payload(title="Updated title")
        )
        merged = prepare.merge_validations(base, update)
        self.assertEqual(("1",), merged.session_ids)
        self.assertEqual("Updated title", merged.sessions[0].title)

    def test_merge_rejects_codebase_root_mismatch(self) -> None:
        base = prepare.parse_validator_output(_validator_payload())
        other = _validator_payload()
        other["codebase_root"] = "/elsewhere"
        update = prepare.parse_validator_output(other)
        with self.assertRaisesRegex(plans.PlanError, "codebase root"):
            prepare.merge_validations(base, update)

    def test_load_returns_none_when_absent_and_fails_closed_on_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            self.assertIsNone(prepare.load_prepare_validation(plan_dir))

            (plan_dir / prepare.PREPARE_VALIDATION_FILENAME).write_text(
                "{not json", encoding="utf-8"
            )
            with self.assertRaisesRegex(plans.PlanError, "not valid JSON"):
                prepare.load_prepare_validation(plan_dir)

    def test_unknown_validation_cache_version_is_a_safe_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            (plan_dir / prepare.PREPARE_VALIDATION_CACHE_FILENAME).write_text(
                json.dumps({"version": 0, "obsolete_entries": []}),
                encoding="utf-8",
            )

            cache = prepare.load_prepare_validation_cache(plan_dir)

        self.assertEqual((), cache.entries)

    def test_obsolete_cached_validator_schema_is_a_safe_cache_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = prepare.parse_validator_output(_validator_payload())
            identity = {"validator_schema_version": validation.version}
            entry = prepare.PrepareValidationCacheEntry(
                session_id=validation.sessions[0].session_id,
                key=prepare._canonical_json_digest(identity),
                identity=identity,
                validation=validation,
            )
            prepare.save_prepare_validation_cache(
                plan_dir,
                prepare.PrepareValidationCache(entries=(entry,)),
            )
            cache_path = plan_dir / prepare.PREPARE_VALIDATION_CACHE_FILENAME
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["entries"][0]["validation"]["version"] = 999
            cache_path.write_text(json.dumps(payload), encoding="utf-8")

            cache = prepare.load_prepare_validation_cache(plan_dir)

        self.assertEqual((), cache.entries)

    def test_malformed_output_cannot_mutate_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = {path.name: path.read_bytes() for path in plan_dir.iterdir()}
            payload = _validator_payload()
            payload["sessions"][0]["claims"] = [{"claim": ""}]

            with self.assertRaises(plans.PlanError):
                prepare.ingest_validator_output(plan_dir, payload)

            after = {path.name: path.read_bytes() for path in plan_dir.iterdir()}
            self.assertEqual(before, after)
            self.assertFalse((plan_dir / prepare.PREPARE_VALIDATION_FILENAME).exists())

    def test_prepare_validation_excluded_from_readiness_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = plans.stable_readiness_sha256(plan_dir)
            prepare.ingest_validator_output(plan_dir, _validator_payload())
            prepare.save_prepare_validation_cache(
                plan_dir, prepare.PrepareValidationCache()
            )
            self.assertEqual(before, plans.stable_readiness_sha256(plan_dir))


class ExpansionRendererTests(unittest.TestCase):
    """Expansion rendering and exact factual corrections."""

    def _validation(self, **session_overrides: object) -> prepare.PrepareValidation:
        return prepare.parse_validator_output(_validator_payload(**session_overrides))

    # -- rendering -------------------------------------------------------------

    def test_render_matches_expansion_schema(self) -> None:
        validation = self._validation(
            issues=[
                {
                    "issue_type": "signature-mismatch",
                    "summary": "load_config takes two args",
                    "session_ref": "`load_config(path)`",
                    "actual": "load_config(path, strict)",
                    "fix": "pass strict=True",
                }
            ],
            details=[{"path": "foo/bar.py", "notes": ["Lines 1-3: retry helper"]}],
            patterns=[
                {"name": "retry", "location": "`foo/bar.py:1-3`", "reason": "mirror it"}
            ],
            dependencies=[
                {"direction": "requires", "session_id": "2", "description": "helper"},
                {"direction": "provides", "session_id": "3", "description": "API"},
            ],
            contract_problems=[
                {"message": "stale path", "suggested_fix": "point at foo/bar.py"}
            ],
        )

        rendered = prepare.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertIn("# Session 1 Expansion: Test", rendered)
        self.assertIn("**Expanded**: 2026-07-10", rendered)
        self.assertIn("**Codebase root**: `/repo`", rendered)
        self.assertIn("**Claims checked**: 3 | **Issues found**: 1", rendered)
        self.assertIn("### ISSUE-1: load_config takes two args", rendered)
        self.assertIn("- **Type**: signature-mismatch", rendered)
        self.assertIn("| Claim | Type | Location | Line |", rendered)
        self.assertIn("| `load_config` | S | `foo/config.py` | 12 |", rendered)
        self.assertIn("### File: `foo/bar.py`", rendered)
        self.assertIn("- **retry**: `foo/bar.py:1-3` — mirror it", rendered)
        self.assertIn("- **Requires from session 2**: helper", rendered)
        self.assertIn("- **Provides for session 3**: API", rendered)
        self.assertIn("## Contract Issues", rendered)
        self.assertIn("- `1: stale path: point at foo/bar.py`", rendered)
        self.assertNotIn(prepare.NO_CONTRACT_ISSUES_LINE, rendered)

    def test_render_omits_empty_sections_and_marks_clean_contract(self) -> None:
        validation = self._validation(claims=[], issues=[])
        rendered = prepare.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertNotIn("## Issues", rendered)
        self.assertNotIn("## Verified Claims", rendered)
        self.assertNotIn("## Concrete Details", rendered)
        self.assertNotIn("## Patterns to Reuse", rendered)
        self.assertNotIn("## Session Dependencies", rendered)
        self.assertIn("## Contract Issues", rendered)
        self.assertIn(prepare.NO_CONTRACT_ISSUES_LINE, rendered)

    def test_render_to_be_created_claim_row(self) -> None:
        validation = self._validation(
            claims=[
                {
                    "claim": "NewTests",
                    "claim_type": "S",
                    "to_be_created": True,
                }
            ]
        )
        rendered = prepare.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )
        self.assertIn(
            f"| `NewTests` | S | {prepare.TO_BE_CREATED_LOCATION} | "
            f"{prepare.TO_BE_CREATED_LINE} |",
            rendered,
        )

    def test_render_places_removal_findings_in_existing_sections(self) -> None:
        validation = self._validation(
            claims=[
                {
                    "claim": "retry backoff branch",
                    "claim_type": "R",
                    "location": "foo/bar.py — added in a1b2c3d",
                    "line": 12,
                    "to_be_created": False,
                }
            ],
            issues=[
                {
                    "issue_type": "removal-unjustified",
                    "summary": "no rationale for dropping the backoff",
                    "session_ref": "delete the backoff branch",
                    "actual": "no commit or covering test explains the branch",
                    "fix": "record the origin rationale before removing it",
                }
            ],
        )

        rendered = prepare.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertIn(
            "| `retry backoff branch` | R | `foo/bar.py — added in a1b2c3d` | 12 |",
            rendered,
        )
        self.assertIn("- **Type**: removal-unjustified", rendered)
        self.assertEqual(1, rendered.count("## Issues"))
        self.assertEqual(1, rendered.count("## Verified Claims"))

    def test_render_without_removal_content_is_byte_identical(self) -> None:
        validation = self._validation(
            issues=[
                {
                    "issue_type": "signature-mismatch",
                    "summary": "load_config takes two args",
                    "session_ref": "`load_config(path)`",
                    "actual": "load_config(path, strict)",
                    "fix": "pass strict=True",
                }
            ],
            details=[{"path": "foo/bar.py", "notes": ["Lines 1-3: retry helper"]}],
            patterns=[
                {"name": "retry", "location": "`foo/bar.py:1-3`", "reason": "mirror it"}
            ],
            dependencies=[
                {"direction": "requires", "session_id": "2", "description": "helper"},
                {"direction": "provides", "session_id": "3", "description": "API"},
            ],
            contract_problems=[
                {"message": "stale path", "suggested_fix": "point at foo/bar.py"}
            ],
        )

        rendered = prepare.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertEqual(
            "\n".join(
                [
                    "# Session 1 Expansion: Test",
                    "",
                    "**Expanded**: 2026-07-10",
                    "**Codebase root**: `/repo`",
                    "**Claims checked**: 3 | **Issues found**: 1",
                    "",
                    "## Issues",
                    "",
                    "### ISSUE-1: load_config takes two args",
                    "",
                    "- **Type**: signature-mismatch",
                    '- **Session ref**: "`load_config(path)`"',
                    "- **Actual**: load_config(path, strict)",
                    "- **Fix**: pass strict=True",
                    "",
                    "## Verified Claims",
                    "",
                    "| Claim | Type | Location | Line |",
                    "| --- | --- | --- | --- |",
                    "| `load_config` | S | `foo/config.py` | 12 |",
                    "",
                    "## Concrete Details",
                    "",
                    "### File: `foo/bar.py`",
                    "",
                    "- Lines 1-3: retry helper",
                    "",
                    "## Patterns to Reuse",
                    "",
                    "- **retry**: `foo/bar.py:1-3` — mirror it",
                    "",
                    "## Session Dependencies",
                    "",
                    "- **Requires from session 2**: helper",
                    "- **Provides for session 3**: API",
                    "",
                    "## Contract Issues",
                    "",
                    "- `1: stale path: point at foo/bar.py`",
                    "",
                ]
            ),
            rendered,
        )

    def test_render_omits_contract_issues_when_not_checked(self) -> None:
        validation = self._validation(contract_checked=False)
        rendered = prepare.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )
        self.assertNotIn("## Contract Issues", rendered)

    # -- expansion writing -----------------------------------------------------

    def test_write_expansions_writes_file_and_reference_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._validation()

            prepare.write_expansions(plan_dir, validation, expanded="2026-07-10")

            expansion_path = plan_dir / "session-1.expand.md"
            self.assertTrue(expansion_path.is_file())
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            reference = prepare.expansion_reference_line("1")
            self.assertEqual(1, session_text.count(reference))
            # The reference sits directly under the Before-starting line.
            lines = session_text.splitlines()
            anchor = lines.index(authoring.CONTEXT_REFERENCE_LINE)
            self.assertEqual(reference, lines[anchor + 1])

            prepare.write_expansions(plan_dir, validation, expanded="2026-07-10")
            session_text_again = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertEqual(session_text, session_text_again)
            self.assertEqual(1, session_text_again.count(reference))

    def test_write_expansions_fails_closed_on_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            payload = _validator_payload(session_id="7")
            validation = prepare.parse_validator_output(payload)

            with self.assertRaisesRegex(plans.PlanError, "missing session file"):
                prepare.write_expansions(plan_dir, validation)
            self.assertFalse((plan_dir / "session-7.expand.md").exists())

    # -- exact factual corrections ----------------------------------------------

    def _corrections_validation(
        self, corrections: list[dict]
    ) -> prepare.PrepareValidation:
        return self._validation(corrections=corrections)

    def test_correction_applies_on_exactly_one_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "Update the retry helper in `foo/bar.py`",
                        "replacement_text": "Update the retry helper in `foo/baz.py`",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(1, len(outcomes))
            self.assertEqual(prepare.CORRECTION_APPLIED, outcomes[0].status)
            text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn("`foo/baz.py` to use `load_config`", text)
            # Contract and handoff occurrences of the old path are untouched.
            self.assertIn("The helper in `foo/bar.py` passes tests.", text)
            self.assertIn("remember `foo/bar.py` cleanup", text)

    def test_correction_is_idempotent_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "Update the retry helper in `foo/bar.py`",
                        "replacement_text": "Update the retry helper in `foo/baz.py`",
                        "reason": "file moved",
                    }
                ]
            )

            prepare.apply_corrections(plan_dir, validation)
            text_after_first = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare.CORRECTION_ALREADY_APPLIED, outcomes[0].status)
            self.assertEqual(
                text_after_first,
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_ambiguous_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "`foo/bar.py`",
                        "replacement_text": "`foo/baz.py`",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare.BLOCKER_AMBIGUOUS, outcomes[0].blocker)
            self.assertEqual(
                before, (plan_dir / "session-1.md").read_text(encoding="utf-8")
            )

    def test_unknown_text_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "text that never existed",
                        "replacement_text": "anything",
                        "reason": "r",
                    }
                ]
            )

            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare.BLOCKER_NOT_FOUND, outcomes[0].blocker)

    def test_contract_body_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "The helper in `foo/bar.py` passes tests.",
                        "replacement_text": "The helper in `foo/baz.py` passes tests.",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare.BLOCKER_CONTRACT_BODY, outcomes[0].blocker)
            self.assertEqual(
                before, (plan_dir / "session-1.md").read_text(encoding="utf-8")
            )

    def test_handoff_body_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "remember `foo/bar.py` cleanup",
                        "replacement_text": "remember `foo/baz.py` cleanup",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare.BLOCKER_HANDOFF_BODY, outcomes[0].blocker)

    def test_heading_correction_is_blocked_as_structural(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "## Completion Contract",
                        "replacement_text": "## Different Section",
                        "reason": "restructure",
                    }
                ]
            )

            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare.BLOCKER_STRUCTURAL, outcomes[0].blocker)

    def test_multiple_corrections_to_one_file_compose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "Update the retry helper in `foo/bar.py`",
                        "replacement_text": "Update the retry helper in `foo/baz.py`",
                        "reason": "file moved",
                    },
                    {
                        "expected_text": "to use `load_config`.",
                        "replacement_text": "to use `load_settings`.",
                        "reason": "renamed",
                    },
                ]
            )

            outcomes = prepare.apply_corrections(plan_dir, validation)

            self.assertEqual(
                [prepare.CORRECTION_APPLIED, prepare.CORRECTION_APPLIED],
                [outcome.status for outcome in outcomes],
            )
            text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn(
                "Update the retry helper in `foo/baz.py` to use `load_settings`.",
                text,
            )


class PrepareCliTests(unittest.TestCase):
    """`asl prepare` CLI wiring and input validation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.base_dir = self.tmp_path / "plans"
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.repo = self.tmp_path / "repo"
        init_git_repo(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_source(self, **kwargs: object) -> Path:
        return _write_prepare_source_plan(self.tmp_path / "demo-plan.md", **kwargs)

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["prepare", *argv])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.resolve_handler(args)(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_defaults_and_handler_wiring(self) -> None:
        args = cli.build_parser().parse_args(["prepare", "plan.md"])

        self.assertIs(prepare.prepare_from_args, cli.resolve_handler(args))
        self.assertEqual("plan.md", args.plan)
        self.assertFalse(args.resume)
        self.assertFalse(args.rebind_workspace)
        self.assertEqual(".", args.cwd)
        self.assertEqual("codex", args.harness)
        self.assertIsNone(args.harness_model)
        self.assertEqual("codex", args.validator_harness)
        self.assertIsNone(args.validator_model)
        self.assertIsNone(args.validator_effort)
        self.assertEqual(prepare.DEFAULT_VALIDATOR_JOBS, args.validator_jobs)
        self.assertEqual(
            prepare.DEFAULT_VALIDATOR_TIMEOUT_SECONDS, args.validator_timeout
        )
        self.assertEqual(
            prepare.DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
            args.validator_heartbeat,
        )
        self.assertIsNone(args.review_harness)
        self.assertIsNone(args.review_harness_model)
        self.assertIsNone(args.base_dir)
        self.assertIsNone(args.state_dir)
        self.assertTrue(args.unsafe_bypass)
        self.assertFalse(args.json)

    def test_review_harness_equal_to_harness_is_input_error(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--harness",
            "codex",
            "--review-harness",
            "codex",
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("prepare", payload["command"])
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("must differ", payload["error"])

    def test_rebind_workspace_requires_resume(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--rebind-workspace",
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertTrue(payload["rebind_workspace"])
        self.assertIn("requires --resume", payload["error"])

    def test_rebind_concurrency_error_has_distinct_reason_and_context(self) -> None:
        plan_dir = self.tmp_path / "plan"
        plan_dir.mkdir()
        (plan_dir / "manifest.md").write_text("# Plan\n", encoding="utf-8")
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        rebind_error = prepare._WorkspaceRebindConcurrencyError(
            "cannot lock plan for workspace rebind: busy",
            old_workspace={"workspace_root": "/old"},
            new_workspace={"workspace_root": "/new"},
            stage=prepare.PREPARE_STAGE_WORKSPACE,
        )

        with mock.patch.object(prepare, "prepare_plan", side_effect=rebind_error):
            code, stdout, _stderr = self._run_cli(
                str(plan_dir),
                "--resume",
                "--rebind-workspace",
                "--cwd",
                str(self.repo),
                "--json",
            )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("workspace-rebind-failed", payload["reason"])
        self.assertTrue(payload["rebind_workspace"])
        self.assertEqual(
            prepare.PREPARE_STAGE_WORKSPACE,
            payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual("/old", payload["workspace_rebind"]["old"]["workspace_root"])
        self.assertEqual("/new", payload["workspace_rebind"]["new"]["workspace_root"])

    def test_run_parser_rejects_rebind_workspace(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(["run", "/tmp/plan", "--rebind-workspace"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_review_harness_model_requires_review_harness(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--review-harness-model",
            "claude-opus-4",
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertEqual("claude-opus-4", payload["review_harness_model"])
        self.assertIn("requires --review-harness", payload["error"])

    def test_validator_numeric_controls_must_be_positive(self) -> None:
        source = self._write_source()
        for flag in (
            "--validator-jobs",
            "--validator-timeout",
            "--validator-heartbeat",
        ):
            with self.subTest(flag=flag):
                code, stdout, _stderr = self._run_cli(
                    str(source),
                    "--cwd",
                    str(self.repo),
                    flag,
                    "0",
                    "--json",
                )

                self.assertEqual(1, code)
                payload = json.loads(stdout)
                self.assertEqual("input-error", payload["reason"])
                self.assertIn("must be a positive integer", payload["error"])

    def test_missing_source_file_is_input_error(self) -> None:
        code, stdout, _stderr = self._run_cli(
            str(self.tmp_path / "missing.md"),
            "--cwd",
            str(self.repo),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("source plan file does not exist", payload["error"])

    def test_missing_cwd_is_cwd_error(self) -> None:
        source = self._write_source()
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.tmp_path / "no-such-repo"),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("cwd-error", payload["reason"])

    def test_resume_requires_existing_plan_directory(self) -> None:
        empty_dir = self.tmp_path / "not-a-plan"
        empty_dir.mkdir()
        code, stdout, _stderr = self._run_cli(
            str(empty_dir),
            "--resume",
            "--cwd",
            str(self.repo),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("manifest.md", payload["error"])

    def test_single_session_source_returns_needs_plan_iteration(self) -> None:
        source = self._write_source(session_count=1)
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--base-dir",
            str(self.base_dir),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("needs-plan-iteration", payload["reason"])
        self.assertIsNone(payload["plan_dir"])
        message = payload["blockers"][0]["message"]
        self.assertIn("supported H2-H6", message)
        self.assertIn("'Session <id>' headings", message)
        self.assertNotIn("'## Session <id>'", message)
        # The structure gate fails before any output directory is created.
        self.assertFalse(self.base_dir.exists())

    def test_malformed_session_heading_fails_structure_gate_atomically(self) -> None:
        malformed = "#### Session 02 / Broken"
        source = self._write_source(heading_by_id={"2": malformed})
        source_lines = source.read_text(encoding="utf-8").splitlines()
        line_number = source_lines.index(malformed) + 1

        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--base-dir",
            str(self.base_dir),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("needs-plan-iteration", payload["reason"])
        self.assertIsNone(payload["plan_dir"])
        self.assertEqual(1, len(payload["blockers"]))
        blocker = payload["blockers"][0]
        self.assertEqual("structure", blocker["stage"])
        self.assertIn(f"line {line_number}", blocker["message"])
        self.assertIn("unsupported title separator", blocker["message"])
        self.assertIn(malformed, blocker["message"])
        self.assertFalse(self.base_dir.exists())

    def test_missing_contract_returns_needs_plan_iteration(self) -> None:
        source = self._write_source(omit_contract_for=("2",))
        code, stdout, _stderr = self._run_cli(
            str(source),
            "--cwd",
            str(self.repo),
            "--base-dir",
            str(self.base_dir),
            "--json",
        )

        self.assertEqual(1, code)
        payload = json.loads(stdout)
        self.assertEqual("needs-plan-iteration", payload["reason"])
        blockers = payload["blockers"]
        self.assertEqual(1, len(blockers))
        self.assertEqual("2", blockers[0]["session_id"])
        self.assertIn("Completion Contract", blockers[0]["message"])
        self.assertFalse(self.base_dir.exists())

    def test_prepare_success_via_default_validator_harness(self) -> None:
        source = self._write_source()
        canonical_repo = plans.canonicalize_path(self.repo)
        payload = _prepare_pipeline_payload(str(canonical_repo), ("1", "2"))

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            self.assertEqual("claude", name)
            self.assertTrue(
                str(kwargs["prompt"]).startswith(
                    prepare.VALIDATION_PACKET_HEADING_PREFIX
                )
            )
            self.assertEqual("claude-opus-validator", kwargs["model"])
            self.assertEqual("high", kwargs["effort"])
            self.assertEqual(17, kwargs["timeout"])
            self.assertFalse(kwargs["unsafe_bypass"])
            self.assertIsNone(kwargs["plan_dir"])
            access = kwargs["access"]
            assert isinstance(access, harnesses.AccessProfile)
            self.assertEqual("audit-read-only", access.name)
            return harnesses.HarnessResult(
                argv=["claude"],
                returncode=0,
                stdout="",
                stderr="",
                last_message=json.dumps(payload),
                name="claude",
            )

        with mock.patch.object(
            prepare.harnesses, "run_harness", side_effect=fake_run_harness
        ) as run_mock:
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--harness",
                "codex",
                "--harness-model",
                "gpt-5",
                "--validator-harness",
                "claude",
                "--validator-model",
                "claude-opus-validator",
                "--validator-effort",
                "high",
                "--validator-jobs",
                "3",
                "--validator-timeout",
                "17",
                "--validator-heartbeat",
                "4",
                "--review-harness",
                "claude",
                "--review-harness-model",
                "claude-opus-4",
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
                "--json",
            )

        self.assertEqual(0, code, stderr)
        response = json.loads(stdout)
        self.assertTrue(response["ok"])
        self.assertEqual("ok", response["reason"])
        self.assertEqual("gpt-5", response["harness_model"])
        self.assertEqual("claude-opus-validator", response["validator_model"])
        self.assertEqual("high", response["validator_effort"])
        self.assertEqual(3, response["validator_jobs"])
        self.assertEqual(17, response["validator_timeout"])
        self.assertEqual(4, response["validator_heartbeat"])
        self.assertEqual("claude", response["validator"]["harness"])
        self.assertEqual("claude-opus-4", response["review_harness_model"])
        self.assertTrue(response["marked_loop_ready"])
        self.assertEqual(1, run_mock.call_count)
        plan_dir = Path(response["plan_dir"])
        self.assertTrue((plan_dir / "session-1.md").is_file())
        self.assertTrue((plan_dir / "session-1.expand.md").is_file())
        manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        self.assertEqual(
            shlex.join(
                [
                    "asl",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(canonical_repo),
                    "--harness",
                    "codex",
                    "--harness-model",
                    "gpt-5",
                    "--review-harness",
                    "claude",
                    "--review-harness-model",
                    "claude-opus-4",
                ]
            ),
            response["run_command"],
        )
        self.assertIn("--harness-model gpt-5", response["resume_command"])
        self.assertIn(
            "--validator-model claude-opus-validator", response["resume_command"]
        )
        self.assertIn("--validator-effort high", response["resume_command"])
        self.assertIn("--validator-jobs 3", response["resume_command"])
        self.assertIn("--validator-timeout 17", response["resume_command"])
        self.assertIn("--validator-heartbeat 4", response["resume_command"])
        self.assertIn(
            "--review-harness-model claude-opus-4",
            response["resume_command"],
        )

    def test_omitted_validator_selection_uses_harness_native_defaults(self) -> None:
        source = self._write_source()
        canonical_repo = plans.canonicalize_path(self.repo)
        payload = _prepare_pipeline_payload(str(canonical_repo), ("1", "2"))

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            self.assertEqual("codex", name)
            self.assertIsNone(kwargs["model"])
            self.assertIsNone(kwargs["effort"])
            self.assertEqual(
                prepare.DEFAULT_VALIDATOR_TIMEOUT_SECONDS, kwargs["timeout"]
            )
            return harnesses.HarnessResult(
                argv=["codex"],
                returncode=0,
                stdout="",
                stderr="",
                last_message=json.dumps(payload),
                name="codex",
            )

        with mock.patch.object(
            prepare.harnesses, "run_harness", side_effect=fake_run_harness
        ):
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
                "--json",
            )

        self.assertEqual(0, code, stderr)
        response = json.loads(stdout)
        self.assertIsNone(response["validator_model"])
        self.assertIsNone(response["validator_effort"])

    def test_validator_heartbeat_keeps_json_stdout_clean(self) -> None:
        source = self._write_source()
        canonical_repo = plans.canonicalize_path(self.repo)
        payload = _prepare_pipeline_payload(str(canonical_repo), ("1", "2"))

        def slow_launch(*_args: object, **_kwargs: object) -> object:
            time.sleep(1.1)
            return payload

        with mock.patch.object(prepare, "_launch_validator", side_effect=slow_launch):
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
                "--validator-heartbeat",
                "1",
                "--json",
            )

        self.assertEqual(0, code, stderr)
        self.assertTrue(json.loads(stdout)["ok"])
        self.assertIn("validator heartbeat", stderr)

    def test_text_output_reports_resume_command_on_validator_failure(self) -> None:
        source = self._write_source()

        def failing_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            return harnesses.HarnessResult(
                argv=["codex"],
                returncode=1,
                stdout="",
                stderr="usage limit reached",
                name="codex",
            )

        with mock.patch.object(
            prepare.harnesses, "run_harness", side_effect=failing_run_harness
        ):
            code, stdout, stderr = self._run_cli(
                str(source),
                "--cwd",
                str(self.repo),
                "--harness-model",
                "gpt-5",
                "--review-harness",
                "claude",
                "--review-harness-model",
                "claude-opus-4",
                "--base-dir",
                str(self.base_dir),
                "--state-dir",
                str(self.state_dir),
            )

        self.assertEqual(1, code)
        self.assertIn("Resume with: asl prepare --resume", stdout)
        self.assertIn("--harness-model gpt-5", stdout)
        self.assertIn("--review-harness claude", stdout)
        self.assertIn("--review-harness-model claude-opus-4", stdout)
        self.assertIn("agent-session-loop prepare:", stderr)
        # The failed preparation directory is preserved as draft.
        plan_dirs = list(self.base_dir.iterdir())
        self.assertEqual(1, len(plan_dirs))
        manifest = (plan_dirs[0] / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)


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
        with self.assertRaises(prepare.RepositoryFootprintError) as caught:
            prepare.build_validation_packet(
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
            code = prepare.expand_ingest_from_args(args)

        self.assertEqual(1, code)
        emitted = json.loads(stdout.getvalue())
        self.assertEqual(
            prepare.EXPAND_INGEST_REASON_REPOSITORY_FOOTPRINT, emitted["reason"]
        )
        self.assertTrue(emitted["repository_footprint"]["registry_required"])
        self.assertIsNone(prepare.load_prepare_validation(self.plan_dir))

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
                    "issue_type": prepare.REPOSITORY_AMBIGUITY_ISSUE,
                    "summary": "`foo/bar.py` names more than one repository",
                    "session_ref": "`foo/bar.py`",
                    "actual": "the path exists in two sibling repositories",
                    "fix": "declare a `## Repositories` registry",
                }
            ]
        )
        payload["codebase_root"] = str(plans.canonicalize_path(self.first))
        with self.assertRaisesRegex(plans.PlanError, "repository ambiguity"):
            prepare.ingest_validator_output(plan_dir, payload)


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
        _, payload = preflight.plan_repository_footprint(self.plan_dir, self.first)
        self.assertIsNone(payload)

    def test_build_validation_packet_refuses_a_multi_repo_intent(self) -> None:
        with self.assertRaises(prepare.RepositoryRegistryRequiredError) as caught:
            prepare.build_validation_packet(
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
            code = prepare.expand_ingest_from_args(args)

        self.assertEqual(1, code)
        emitted = json.loads(stdout.getvalue())
        self.assertEqual(
            prepare.EXPAND_INGEST_REASON_REGISTRY_REQUIRED, emitted["reason"]
        )
        self.assertTrue(emitted["repository_intent"]["registry_required"])
        self.assertIsNone(prepare.load_prepare_validation(self.plan_dir))

    def test_a_stored_aggregate_is_refused_on_resume(self) -> None:
        aggregate = prepare.parse_validator_output(
            {
                **_validator_payload(),
                "codebase_root": str(plans.canonicalize_path(self.first)),
            }
        )
        with self.assertRaises(prepare.RepositoryRegistryRequiredError):
            prepare._validate_validation_authority(self.plan_dir, aggregate)

    def test_a_single_repository_legacy_plan_still_prepares(self) -> None:
        plan_dir = self.tmp_path / "single-repo-plan"
        _write_prepare_plan(plan_dir, ("1",))

        packet = prepare.build_validation_packet(
            plan_dir, ("1",), codebase_root=self.first
        )

        self.assertEqual(prepare.LEGACY_PREPARE_VALIDATION_VERSION, packet.version)


class PrepareEndToEndTests(unittest.TestCase):
    """`prepare_plan` end-to-end pipeline behavior."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.base_dir = self.tmp_path / "plans"
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.repo = self.tmp_path / "repo"
        init_git_repo(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_source(self, **kwargs: object) -> Path:
        return _write_prepare_source_plan(self.tmp_path / "demo-plan.md", **kwargs)

    def _prepare(
        self,
        source: Path | None,
        validator,
        **kwargs: object,
    ) -> prepare.PrepareResult:
        options: dict[str, Any] = {
            "cwd": self.repo,
            "harness": "codex",
            "harness_model": "gpt-5",
            "validator_harness": "claude",
            "review_harness": "claude",
            "review_harness_model": "claude-opus-4",
            "base_dir": self.base_dir,
            "validator": validator,
            "state_dir": self.state_dir,
        }
        options.update(kwargs)
        return prepare.prepare_plan(source, **options)

    def _write_registered_source(
        self,
        specs: tuple[tuple[str, str, str], ...],
    ) -> Path:
        lines = [
            "# Registered Prepare Demo",
            "",
            "## Repositories",
            "",
            "| Alias | Path | Origin |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
        )
        aliases = ", ".join(alias for alias, _path, _origin in specs)
        for number, (alias, relative_path, _origin) in enumerate(specs, start=1):
            check = {
                "name": f"check-{number}",
                "type": "command",
                "repo": alias,
                "argv": [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"raise SystemExit(Path.cwd().name != {relative_path!r})"
                    ),
                ],
                "timeout_seconds": 30,
                "baseline_expectation": "pass",
            }
            lines.extend(
                [
                    "",
                    f"## Session {number}: Step {number}",
                    "",
                    f"**Reads repositories**: {aliases}",
                    f"**Writes repositories**: {alias}",
                    "",
                    f"Implement `{alias}:demo/module.py`.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    f"- Step {number} is complete.",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    json.dumps({"session": str(number), "checks": [check]}),
                    "```",
                ]
            )
        source = self.tmp_path / "registered-plan.md"
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return source

    def test_clean_source_plan_produces_loop_ready_directory(self) -> None:
        source = self._write_source()
        calls: list[prepare.ValidationTaskPacket] = []

        result = self._prepare(source, _recording_validator(calls))

        self.assertTrue(result.ok, result)
        self.assertEqual("ok", result.reason)
        self.assertTrue(result.marked_loop_ready)
        plan_dir = result.plan_dir
        assert plan_dir is not None
        for name in (
            "context.md",
            "manifest.md",
            "progress.md",
            "session-1.md",
            "session-2.md",
            "session-1.expand.md",
            "session-2.expand.md",
            prepare.PREPARE_VALIDATION_FILENAME,
            prepare.PREPARE_VALIDATION_CACHE_FILENAME,
            plans.PLAN_METADATA_FILE_NAME,
        ):
            self.assertTrue((plan_dir / name).is_file(), name)
        self.assertEqual(
            str(plans.canonicalize_path(self.repo)),
            plans.load_plan_metadata(plan_dir).cwd,
        )
        manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertEqual(1, session_text.count(contracts.COMPLETION_CONTRACT_HEADING))
        self.assertIn("> **Expansion notes**", session_text)
        # One validation batch covered both sessions.
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        canonical_repo = plans.canonicalize_path(self.repo)
        self.assertEqual(
            shlex.join(
                [
                    "asl",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(canonical_repo),
                    "--harness",
                    "codex",
                    "--harness-model",
                    "gpt-5",
                    "--review-harness",
                    "claude",
                    "--review-harness-model",
                    "claude-opus-4",
                ]
            ),
            result.run_command,
        )
        assert result.resume_command is not None
        self.assertIn("--validator-harness claude", result.resume_command)
        self.assertIn("--harness-model gpt-5", result.resume_command)
        self.assertIn("--review-harness claude", result.resume_command)
        self.assertIn("--review-harness-model claude-opus-4", result.resume_command)
        self.assertEqual(
            ["ok", "ok", "ok", "ok", "ok", "ok"],
            [step.status for step in result.steps],
        )

    def test_registered_prepare_uses_v3_and_repository_aware_baselines(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        repositories = init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)
        calls: list[prepare.ValidationTaskPacket] = []

        result = self._prepare(
            source,
            _recording_validator(calls),
            cwd=workspace,
        )

        self.assertTrue(result.ok, result)
        self.assertEqual(1, len(calls))
        self.assertEqual(prepare.PREPARE_VALIDATION_VERSION, calls[0].version)
        assert result.plan_dir is not None
        metadata = plans.load_plan_metadata(result.plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        assert isinstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(3, metadata.version)
        validation = prepare.load_prepare_validation(result.plan_dir)
        assert validation is not None
        self.assertEqual(prepare.PREPARE_VALIDATION_VERSION, validation.version)
        self.assertEqual(
            ("SVC", "OAK"), tuple(r.alias for r in validation.repositories)
        )
        assert result.baseline is not None
        self.assertEqual(2, result.baseline["check_count"])
        self.assertEqual(
            {str(path.resolve()) for path in repositories.values()},
            {entry["resolved_root"] for entry in result.baseline["checks"]},
        )

    def test_registered_cached_baselines_capture_fingerprint_once(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)
        result = self._prepare(
            source,
            _recording_validator([]),
            cwd=workspace,
        )
        self.assertTrue(result.ok, result)
        assert result.plan_dir is not None
        context = verification.verification_context_from_plan(
            result.plan_dir, workspace
        )
        collected = baseline.collect_baseline_checks(
            {
                session.id: contracts.read_completion_contract(
                    result.plan_dir, plans.parse_session_id(session.id)
                )
                for session in plans.inspect_plan(result.plan_dir).sessions
            }
        )
        unique_checks = [
            (
                baseline.baseline_identity_key(check, context.root, context=context),
                session_id,
                check,
            )
            for session_id, check in collected
        ]
        original = baseline.capture_baseline_repository_evidence

        with mock.patch.object(
            baseline,
            "capture_baseline_repository_evidence",
            wraps=original,
        ) as capture:
            entries = prepare._cached_baseline_entries(
                result.plan_dir, context, unique_checks
            )

        self.assertIsNotNone(entries)
        self.assertEqual(2, len(entries))
        capture.assert_called_once()

    @staticmethod
    def _plan_file_bytes(plan_dir: Path) -> dict[str, bytes]:
        return {
            path.relative_to(plan_dir).as_posix(): path.read_bytes()
            for path in sorted(plan_dir.rglob("*"))
            if path.is_file()
        }

    def test_registered_resume_rebinds_all_workspace_evidence_atomically(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        new_repositories = init_multi_repo_workspace(new_workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None

        calls: list[prepare.ValidationTaskPacket] = []
        rebound = self._prepare(
            None,
            _recording_validator(calls),
            cwd=new_workspace,
            resume_plan_dir=first.plan_dir,
            rebind_workspace=True,
        )

        self.assertTrue(rebound.ok, rebound)
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        metadata = plans.load_plan_metadata(first.plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        assert isinstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(str(new_workspace.resolve()), metadata.workspace_root)
        aggregate = prepare.load_prepare_validation(first.plan_dir)
        assert aggregate is not None
        self.assertEqual(str(new_workspace.resolve()), aggregate.workspace_root)
        self.assertEqual(
            {str(path.resolve()) for path in new_repositories.values()},
            {repository.root for repository in aggregate.repositories},
        )
        validator_cache = prepare.load_prepare_validation_cache(first.plan_dir)
        cache_entry = validator_cache.entry("1")
        assert cache_entry is not None
        self.assertEqual(
            str(new_workspace.resolve()), cache_entry.validation.workspace_root
        )
        expansion = (first.plan_dir / "session-1.expand.md").read_text(encoding="utf-8")
        self.assertIn(str(new_workspace.resolve()), expansion)
        self.assertNotIn(str(old_workspace.resolve()), expansion)
        cache_text = (
            first.plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
        ).read_text(encoding="utf-8")
        self.assertIn(str(new_workspace.resolve()), cache_text)
        self.assertNotIn(str(old_workspace.resolve()), cache_text)
        manifest = (first.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        assert rebound.workspace_rebind is not None
        self.assertEqual(
            str(old_workspace.resolve()),
            rebound.workspace_rebind["old"]["workspace_root"],
        )
        self.assertEqual(
            str(new_workspace.resolve()),
            rebound.workspace_rebind["new"]["workspace_root"],
        )
        self.assertIsNone(rebound.workspace_rebind["failure_stage"])
        self.assertNotIn("git@", json.dumps(rebound.workspace_rebind))
        self.assertIn("--cwd " + str(new_workspace.resolve()), rebound.run_command)
        self.assertIn("--rebind-workspace", rebound.resume_command)
        self.assertIsNone(state.inspect_lock_info(first.plan_dir, self.state_dir))

    def test_interrupted_rebind_publication_rolls_forward_on_next_read(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None

        original_replace = os.replace

        def interrupt_publication(source_path, destination):
            target = Path(destination)
            if (
                target.parent == first.plan_dir
                and target.name == plans.PLAN_METADATA_FILE_NAME
            ):
                raise KeyboardInterrupt("injected publication interruption")
            return original_replace(source_path, destination)

        with (
            mock.patch.object(
                authoring.os, "replace", side_effect=interrupt_publication
            ),
            self.assertRaisesRegex(KeyboardInterrupt, "publication interruption"),
        ):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=new_workspace,
                resume_plan_dir=first.plan_dir,
                rebind_workspace=True,
            )

        self.assertTrue((first.plan_dir / plans.PLAN_TRANSACTION_DIR_NAME).is_dir())
        metadata = plans.load_plan_metadata(first.plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        assert isinstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(str(new_workspace.resolve()), metadata.workspace_root)
        validation = prepare.load_prepare_validation(first.plan_dir)
        assert validation is not None
        self.assertEqual(str(new_workspace.resolve()), validation.workspace_root)
        self.assertFalse((first.plan_dir / plans.PLAN_TRANSACTION_DIR_NAME).exists())
        self.assertIsNone(state.inspect_lock_info(first.plan_dir, self.state_dir))

    def test_rebind_staging_is_external_preserves_symlinks_and_rebases_paths(
        self,
    ) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        external_file = self.tmp_path / "operator-note.md"
        external_file.write_text("operator note\n", encoding="utf-8")
        (first.plan_dir / "operator-link.md").symlink_to(external_file)
        staged_paths: list[Path] = []

        def failing_validator(packet: prepare.ValidationTaskPacket) -> object:
            staged_plan_dir = Path(packet.plan_dir)
            staged_paths.append(staged_plan_dir)
            self.assertFalse(staged_plan_dir.is_relative_to(self.base_dir))
            self.assertTrue((staged_plan_dir / "operator-link.md").is_symlink())
            raise plans.PlanError(
                f"injected staged failure at {staged_plan_dir}/session-1.md"
            )

        result = self._prepare(
            None,
            failing_validator,
            cwd=new_workspace,
            resume_plan_dir=first.plan_dir,
            rebind_workspace=True,
        )

        self.assertFalse(result.ok)
        self.assertTrue(staged_paths)
        self.assertFalse(staged_paths[0].exists())
        diagnostics = "\n".join(
            [step.detail for step in result.steps]
            + [blocker.message for blocker in result.blockers]
        )
        self.assertIn(str(first.plan_dir / "session-1.md"), diagnostics)
        self.assertNotIn(str(staged_paths[0]), diagnostics)

    def test_rebind_stage_failures_preserve_ready_plan_byte_for_byte(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        before = self._plan_file_bytes(first.plan_dir)

        invalid = self._prepare(
            None,
            _recording_validator([]),
            cwd=self.tmp_path / "missing-workspace",
            resume_plan_dir=first.plan_dir,
            rebind_workspace=True,
        )
        self.assertFalse(invalid.ok)
        assert invalid.workspace_rebind is not None
        self.assertEqual(
            prepare.PREPARE_STAGE_WORKSPACE,
            invalid.workspace_rebind["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(first.plan_dir))

        def run_rebind(validator=None) -> prepare.PrepareResult:
            return self._prepare(
                None,
                validator or _recording_validator([]),
                cwd=new_workspace,
                resume_plan_dir=first.plan_dir,
                rebind_workspace=True,
            )

        injections = []
        injections.append(
            (
                "metadata",
                mock.patch.object(
                    plans,
                    "save_workspace_plan_metadata",
                    side_effect=plans.PlanError("injected metadata failure"),
                ),
                _recording_validator([]),
                prepare.PREPARE_STAGE_METADATA,
            )
        )
        injections.append(
            (
                "validation",
                nullcontext(),
                lambda _packet: (_ for _ in ()).throw(
                    plans.PlanError("injected validation failure")
                ),
                prepare.PREPARE_STAGE_VALIDATION,
            )
        )
        injections.append(
            (
                "expansion",
                mock.patch.object(
                    prepare,
                    "render_expansion",
                    side_effect=plans.PlanError("injected expansion failure"),
                ),
                _recording_validator([]),
                prepare.PREPARE_STAGE_VALIDATION,
            )
        )
        injections.append(
            (
                "correction",
                mock.patch.object(
                    prepare,
                    "_apply_one_correction",
                    side_effect=plans.PlanError("injected correction failure"),
                ),
                _recording_validator(
                    [],
                    corrections_by_id={
                        "1": [
                            {
                                "expected_text": "Implement `SVC:demo/module.py`.",
                                "replacement_text": "Implement `SVC:demo/new.py`.",
                                "reason": "injected",
                            }
                        ]
                    },
                ),
                prepare.PREPARE_STAGE_VALIDATION,
            )
        )
        unsatisfied = {
            "check_count": 1,
            "reused": False,
            "checks": [
                {
                    "session_id": "1",
                    "name": "injected",
                    "expectation": "pass",
                    "status": "fail",
                    "satisfied": False,
                    "detail": "injected baseline failure",
                }
            ],
        }
        unsatisfied["unsatisfied"] = list(unsatisfied["checks"])
        injections.append(
            (
                "baseline",
                mock.patch.object(
                    prepare, "_run_baseline_stage", return_value=unsatisfied
                ),
                _recording_validator([]),
                prepare.PREPARE_STAGE_BASELINE,
            )
        )
        injections.append(
            (
                "preflight",
                mock.patch.object(
                    preflight,
                    "_mark_loop_ready",
                    return_value=(False, "injected preflight failure"),
                ),
                _recording_validator([]),
                prepare.PREPARE_STAGE_PREFLIGHT,
            )
        )

        original_replace = os.replace

        def fail_publication(source, destination):
            target = Path(destination)
            if (
                target.parent == first.plan_dir
                and target.name == plans.PLAN_METADATA_FILE_NAME
            ):
                raise OSError("injected publication failure")
            return original_replace(source, destination)

        injections.append(
            (
                "publication",
                mock.patch.object(
                    authoring.os, "replace", side_effect=fail_publication
                ),
                _recording_validator([]),
                prepare.PREPARE_STAGE_PUBLICATION,
            )
        )

        for name, injection, validator, expected_stage in injections:
            with self.subTest(stage=name), injection:
                result = run_rebind(validator)
                self.assertFalse(result.ok)
                assert result.workspace_rebind is not None
                self.assertEqual(
                    expected_stage, result.workspace_rebind["failure_stage"]
                )
                self.assertEqual(before, self._plan_file_bytes(first.plan_dir))
                self.assertIsNone(
                    state.inspect_lock_info(first.plan_dir, self.state_dir)
                )

    def test_rebind_rejects_unchanged_legacy_and_active_bindings(self) -> None:
        legacy_source = self._write_source()
        legacy = self._prepare(legacy_source, _recording_validator([]))
        assert legacy.plan_dir is not None
        with self.assertRaisesRegex(plans.PlanError, "registered plan"):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=self.repo,
                resume_plan_dir=legacy.plan_dir,
                rebind_workspace=True,
            )

        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        registered_source = self._write_registered_source(specs)
        registered = self._prepare(
            registered_source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        assert registered.plan_dir is not None
        with self.assertRaisesRegex(plans.PlanError, "changed workspace"):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=old_workspace,
                resume_plan_dir=registered.plan_dir,
                rebind_workspace=True,
            )

        before = self._plan_file_bytes(registered.plan_dir)
        state.acquire_lock(registered.plan_dir, self.state_dir, owner="test")
        try:
            with self.assertRaisesRegex(plans.PlanError, "active lock"):
                self._prepare(
                    None,
                    _recording_validator([]),
                    cwd=new_workspace,
                    resume_plan_dir=registered.plan_dir,
                    rebind_workspace=True,
                )
        finally:
            state.release_lock(registered.plan_dir, self.state_dir)
        self.assertEqual(before, self._plan_file_bytes(registered.plan_dir))

    def test_rebind_accepts_a_legacy_binding_once_a_registry_appears(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        registered = self._prepare(
            self._write_registered_source(specs),
            _recording_validator([]),
            cwd=old_workspace,
        )
        assert registered.plan_dir is not None
        # Simulate the plan that grew a registry only after it was bound in
        # legacy mode: the binding on disk is still version 1.
        plans.save_plan_metadata(registered.plan_dir, self.repo)

        rebound = self._prepare(
            None,
            _recording_validator([]),
            cwd=new_workspace,
            resume_plan_dir=registered.plan_dir,
            rebind_workspace=True,
        )

        self.assertTrue(rebound.ok, rebound)
        assert rebound.workspace_rebind is not None
        self.assertEqual(
            str(plans.canonicalize_path(self.repo)),
            rebound.workspace_rebind["old"]["workspace_root"],
        )
        self.assertEqual([], rebound.workspace_rebind["old"]["repositories"])
        self.assertEqual(
            str(plans.canonicalize_path(new_workspace)),
            rebound.workspace_rebind["new"]["workspace_root"],
        )
        metadata = plans.load_plan_metadata(registered.plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        assert isinstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(
            str(plans.canonicalize_path(new_workspace)), metadata.workspace_root
        )

    def test_repository_ambiguity_issue_prevents_loop_ready(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)

        def ambiguous_validator(packet: prepare.ValidationTaskPacket) -> object:
            payload = _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )
            payload["sessions"][0]["issues"] = [
                {
                    "issue_type": prepare.REPOSITORY_AMBIGUITY_ISSUE,
                    "summary": "`demo/module.py` is not repository-qualified",
                    "session_ref": "`demo/module.py`",
                    "actual": "the path exists in more than one read repository",
                    "fix": "qualify the path as SVC:demo/module.py",
                    "repo": "SVC",
                }
            ]
            return payload

        result = self._prepare(source, ambiguous_validator, cwd=workspace)

        self.assertFalse(result.ok)
        self.assertEqual(prepare.PREPARE_EXIT_REASON_VALIDATION_FAILED, result.reason)
        self.assertFalse(result.marked_loop_ready)
        self.assertEqual(
            prepare.REPOSITORY_AMBIGUITY_ISSUE, result.blockers[0].category
        )
        assert result.plan_dir is not None
        self.assertIn(
            "**Readiness**: draft",
            (result.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_malformed_v2_output_does_not_bind_registered_plan(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)

        result = self._prepare(
            source,
            _recording_validator([], payload_override={"bogus": True}),
            cwd=workspace,
        )

        self.assertFalse(result.ok)
        assert result.plan_dir is not None
        self.assertFalse((result.plan_dir / plans.PLAN_METADATA_FILE_NAME).exists())
        self.assertFalse(
            (result.plan_dir / prepare.PREPARE_VALIDATION_FILENAME).exists()
        )
        self.assertEqual([], list(result.plan_dir.glob("*.expand.md")))

    def test_tolerant_headings_author_canonical_plan_artifacts(self) -> None:
        source = self._write_source(
            heading_by_id={
                "1": " ### SeSsIoN 01 - Prime Setup ###   ",
                "2": "###### SESSION 002 — Finalize ####",
            }
        )
        calls: list[prepare.ValidationTaskPacket] = []

        result = self._prepare(source, _recording_validator(calls))

        self.assertTrue(result.ok, result)
        self.assertEqual(("1", "2"), result.session_ids)
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        plan_dir = result.plan_dir
        assert plan_dir is not None
        self.assertTrue((plan_dir / "session-1.md").is_file())
        self.assertTrue((plan_dir / "session-2.md").is_file())
        self.assertFalse((plan_dir / "session-01.md").exists())
        self.assertFalse((plan_dir / "session-002.md").exists())

        session_one = (plan_dir / "session-1.md").read_text(encoding="utf-8")
        session_two = (plan_dir / "session-2.md").read_text(encoding="utf-8")
        self.assertTrue(session_one.startswith("# Session 1: Prime Setup\n"))
        self.assertTrue(session_two.startswith("# Session 2: Finalize\n"))
        self.assertIn('"session": "1"', session_one)
        self.assertIn('"session": "2"', session_two)

        manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("| 1 | Prime Setup | pending | session-1.md |", manifest)
        self.assertIn("| 2 | Finalize | pending | session-2.md |", manifest)
        self.assertNotIn("| 01 |", manifest)
        self.assertNotIn("| 002 |", manifest)

    def test_validator_corrections_are_applied_to_session_files(self) -> None:
        source = self._write_source(
            body_by_id={"1": "Implement step 1 in `demo/old_module.py`."}
        )
        corrections = {
            "1": [
                {
                    "expected_text": "`demo/old_module.py`",
                    "replacement_text": "`demo/module.py`",
                    "reason": "module was renamed",
                }
            ]
        }

        result = self._prepare(
            source,
            _recording_validator([], corrections_by_id=corrections),
        )

        self.assertTrue(result.ok, result)
        self.assertEqual(1, len(result.corrections))
        self.assertEqual(prepare.CORRECTION_APPLIED, result.corrections[0].status)
        assert result.plan_dir is not None
        session_text = (result.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("`demo/module.py`", session_text)
        self.assertNotIn("`demo/old_module.py`", session_text)

    def test_malformed_validator_output_preserves_draft_with_blockers(self) -> None:
        source = self._write_source()

        result = self._prepare(
            source,
            _recording_validator([], payload_override={"bogus": True}),
        )

        self.assertFalse(result.ok)
        self.assertEqual("validation-failed", result.reason)
        self.assertFalse(result.marked_loop_ready)
        assert result.plan_dir is not None
        manifest = (result.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)
        self.assertEqual([], list(result.plan_dir.glob("*.expand.md")))
        self.assertEqual("validation", result.blockers[0].stage)
        assert result.resume_command is not None
        self.assertTrue(result.resume_command.startswith("asl prepare --resume"))
        self.assertIn(str(result.plan_dir), result.resume_command)

    def test_resume_completes_a_failed_preparation(self) -> None:
        source = self._write_source()
        failed = self._prepare(
            source,
            _recording_validator([], payload_override={"bogus": True}),
        )
        assert failed.plan_dir is not None
        calls: list[prepare.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=failed.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertTrue(resumed.marked_loop_ready)
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        assert resumed.plan_dir is not None
        manifest = (resumed.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        self.assertTrue((resumed.plan_dir / "session-1.expand.md").is_file())
        # Resume never creates another split directory.
        self.assertEqual(1, len(list(self.base_dir.iterdir())))
        self.assertEqual(
            ["skipped", "skipped", "ok", "ok", "ok", "ok"],
            [step.status for step in resumed.steps],
        )

    def test_resume_refuses_to_rebind_existing_plan_metadata(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        other_repo = self.tmp_path / "other-repo"
        other_repo.mkdir()

        with self.assertRaisesRegex(plans.PlanError, "does not match"):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=other_repo,
                resume_plan_dir=first.plan_dir,
            )
        self.assertEqual(
            str(plans.canonicalize_path(self.repo)),
            plans.load_plan_metadata(first.plan_dir).cwd,
        )

    def test_resume_is_idempotent_and_reuses_cached_baselines(self) -> None:
        counter = self.tmp_path / "baseline-count.txt"
        argv = [
            "python3",
            "-c",
            (
                "import pathlib; "
                f"p = pathlib.Path({str(counter)!r}); "
                "p.write_text(p.read_text() + 'x' if p.exists() else 'x')"
            ),
        ]
        check = {
            "name": "baseline-check",
            "type": "command",
            "argv": argv,
            "timeout_seconds": 30,
            "baseline_expectation": "pass",
        }
        source = self._write_source(check_json_by_id={"1": check, "2": check})

        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.baseline is not None
        self.assertFalse(first.baseline["reused"])
        # Equivalent checks across sessions deduplicate to one execution.
        self.assertEqual("x", counter.read_text(encoding="utf-8"))

        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        # All sessions were already validated: no validator worker launched.
        self.assertEqual([], calls)
        # The recorded baseline evidence was reused: no re-execution.
        self.assertEqual("x", counter.read_text(encoding="utf-8"))
        assert resumed.baseline is not None
        self.assertTrue(resumed.baseline["reused"])
        self.assertEqual(1, resumed.baseline["check_count"])
        self.assertEqual(1, len(list(self.base_dir.iterdir())))
        assert resumed.plan_dir is not None
        manifest = (resumed.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)

    def test_validator_cache_revalidates_only_a_changed_session(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        cache = prepare.load_prepare_validation_cache(first.plan_dir)
        self.assertEqual(prepare.PREPARE_VALIDATION_CACHE_VERSION, cache.version)
        self.assertIsNotNone(cache.entry("1"))
        self.assertIsNotNone(cache.entry("2"))

        session_path = first.plan_dir / "session-2.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Implement step 2 in `demo/module.py`.",
                "Implement revised step 2 in `demo/module.py`.",
            ),
            encoding="utf-8",
        )
        calls: list[prepare.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("2",)], [packet.session_ids for packet in calls])

    def test_legacy_aggregate_without_cache_refreshes_once(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        (first.plan_dir / prepare.PREPARE_VALIDATION_CACHE_FILENAME).unlink()
        calls: list[prepare.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])
        self.assertTrue(
            (first.plan_dir / prepare.PREPARE_VALIDATION_CACHE_FILENAME).is_file()
        )

    def test_cache_refresh_replaces_an_untrusted_historical_aggregate(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        replacement = prepare.parse_validator_output(
            _prepare_pipeline_payload(
                str(plans.canonicalize_path(self.repo)),
                ("1", "2", "99"),
            )
        )
        prepare.save_prepare_validation(first.plan_dir, replacement)
        calls: list[prepare.ValidationTaskPacket] = []

        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([], calls)
        aggregate = prepare.load_prepare_validation(first.plan_dir)
        assert aggregate is not None
        self.assertEqual(("1", "2"), aggregate.session_ids)

    def test_validator_selection_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
            validator_model="different-validator-model",
            validator_effort="high",
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_validator_harness_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
            validator_harness="codex",
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_context_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        context_path = first.plan_dir / "context.md"
        context_path.write_text(
            context_path.read_text(encoding="utf-8")
            + "\nAdditional shared validation context.\n",
            encoding="utf-8",
        )
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_validator_schema_identity_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        cache = prepare.load_prepare_validation_cache(first.plan_dir)
        stale_entries: list[prepare.PrepareValidationCacheEntry] = []
        for entry in cache.entries:
            stale_identity = dict(entry.identity)
            stale_identity["validator_schema_version"] = 999
            stale_entries.append(
                dataclasses.replace(
                    entry,
                    identity=stale_identity,
                    key=prepare._canonical_json_digest(stale_identity),
                )
            )
        prepare.save_prepare_validation_cache(
            first.plan_dir,
            prepare.PrepareValidationCache(entries=tuple(stale_entries)),
        )
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_packet_prompt_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        original_render = prepare.ValidationTaskPacket.render
        changed_prompt = mock.patch.object(
            prepare.ValidationTaskPacket,
            "render",
            lambda packet: original_render(packet) + "\n- Research removal rationale.",
        )
        calls: list[prepare.ValidationTaskPacket] = []
        with changed_prompt:
            resumed = self._prepare(
                None,
                _recording_validator(calls),
                resume_plan_dir=first.plan_dir,
            )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

        stable: list[prepare.ValidationTaskPacket] = []
        with changed_prompt:
            reused = self._prepare(
                None,
                _recording_validator(stable),
                resume_plan_dir=first.plan_dir,
            )

        self.assertTrue(reused.ok, reused)
        self.assertEqual([], stable)

    def test_cache_identity_without_a_prompt_digest_is_stale(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        cache = prepare.load_prepare_validation_cache(first.plan_dir)
        stale_entries: list[prepare.PrepareValidationCacheEntry] = []
        for entry in cache.entries:
            self.assertIn("prompt_sha256", entry.identity)
            stale_identity = dict(entry.identity)
            del stale_identity["prompt_sha256"]
            stale_entries.append(
                dataclasses.replace(
                    entry,
                    identity=stale_identity,
                    key=prepare._canonical_json_digest(stale_identity),
                )
            )
        prepare.save_prepare_validation_cache(
            first.plan_dir,
            prepare.PrepareValidationCache(entries=tuple(stale_entries)),
        )
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_repository_fingerprint_change_invalidates_cached_sessions(self) -> None:
        source = self._write_source()
        first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        (self.repo / "validation-cache-input.txt").write_text(
            "repository changed\n", encoding="utf-8"
        )
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_repository_change_during_validation_is_not_rekeyed_as_reusable(
        self,
    ) -> None:
        source = self._write_source()
        original_ingest = prepare.ingest_expand_batch
        changed = False

        def mutate_repository_then_ingest(plan_dir, payload, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                (self.repo / "changed-during-validation.txt").write_text(
                    "changed after worker research\n",
                    encoding="utf-8",
                )
            return original_ingest(plan_dir, payload, **kwargs)

        with mock.patch.object(
            prepare,
            "ingest_expand_batch",
            side_effect=mutate_repository_then_ingest,
        ):
            first = self._prepare(source, _recording_validator([]))

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1", "2")], [packet.session_ids for packet in calls])

    def test_registered_scope_change_revalidates_only_that_session(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=workspace,
        )

        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        first_alias = specs[0][0]
        aliases = ", ".join(spec[0] for spec in specs)
        session_path = first.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8")
            .replace(
                f"**Reads repositories**: {aliases}",
                f"**Reads repositories**: {first_alias}",
            )
            .replace(
                f"**Writes repositories**: {aliases}",
                f"**Writes repositories**: {first_alias}",
            ),
            encoding="utf-8",
        )
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            cwd=workspace,
            resume_plan_dir=first.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1",)], [packet.session_ids for packet in calls])

    def test_partial_validation_wave_keeps_successes_for_resume(self) -> None:
        source = self._write_source(session_count=4)
        first_calls: list[tuple[str, ...]] = []
        first_started = threading.Event()
        second_failed = threading.Event()

        def partly_failing_validator(packet: prepare.ValidationTaskPacket) -> object:
            first_calls.append(packet.session_ids)
            if packet.session_ids == ("1",):
                first_started.set()
                self.assertTrue(second_failed.wait(timeout=5))
            if packet.session_ids == ("2",):
                self.assertTrue(first_started.wait(timeout=5))
                second_failed.set()
                raise plans.PlanError("validator service interrupted")
            return _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )

        failed = self._prepare(
            source,
            partly_failing_validator,
            batch_size=1,
            validator_jobs=2,
        )

        self.assertFalse(failed.ok)
        self.assertEqual(prepare.PREPARE_EXIT_REASON_VALIDATION_FAILED, failed.reason)
        self.assertEqual([("1",), ("2",)], first_calls)
        assert failed.plan_dir is not None
        cache = prepare.load_prepare_validation_cache(failed.plan_dir)
        self.assertIsNotNone(cache.entry("1"))
        self.assertIsNone(cache.entry("2"))
        self.assertFalse(
            (failed.plan_dir / prepare.PREPARE_VALIDATION_FILENAME).exists()
        )

        resumed_calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(resumed_calls),
            resume_plan_dir=failed.plan_dir,
            batch_size=1,
            validator_jobs=1,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual(
            [("2",), ("3",), ("4",)],
            [packet.session_ids for packet in resumed_calls],
        )

    def test_validator_workers_are_bounded_and_ingested_in_batch_order(self) -> None:
        source = self._write_source(session_count=4)
        active = 0
        max_active = 0
        lock = threading.Lock()
        first_started = threading.Event()
        second_returned = threading.Event()
        worker_order: list[str] = []
        ingest_order: list[tuple[str, ...]] = []

        def out_of_order_validator(packet: prepare.ValidationTaskPacket) -> object:
            nonlocal active, max_active
            session_id = packet.session_ids[0]
            with lock:
                active += 1
                max_active = max(max_active, active)
                worker_order.append(session_id)
            try:
                if session_id == "1":
                    first_started.set()
                    self.assertTrue(second_returned.wait(timeout=5))
                elif session_id == "2":
                    self.assertTrue(first_started.wait(timeout=5))
                    second_returned.set()
                return _prepare_pipeline_payload(
                    packet.codebase_root,
                    packet.session_ids,
                    version=packet.version,
                    repositories=packet.repositories,
                    session_repositories=packet.session_read_repositories,
                )
            finally:
                with lock:
                    active -= 1

        original_ingest = prepare.ingest_expand_batch

        def recording_ingest(plan_dir, payload, **kwargs):
            ingest_order.append(
                tuple(prepare.parse_validator_output(payload).session_ids)
            )
            return original_ingest(plan_dir, payload, **kwargs)

        stderr = io.StringIO()
        with (
            mock.patch.object(
                prepare, "ingest_expand_batch", side_effect=recording_ingest
            ),
            redirect_stderr(stderr),
        ):
            result = self._prepare(
                source,
                out_of_order_validator,
                batch_size=1,
                validator_jobs=2,
                validator_heartbeat=1,
            )

        self.assertTrue(result.ok, result)
        self.assertLessEqual(max_active, 2)
        self.assertEqual({"1", "2"}, set(worker_order[:2]))
        self.assertEqual(
            [("1",), ("2",), ("3",), ("4",)],
            ingest_order,
        )
        self.assertIn("validator batch 1/4 started", stderr.getvalue())

    def test_validator_heartbeats_stay_on_stderr(self) -> None:
        source = self._write_source()

        def slow_validator(packet: prepare.ValidationTaskPacket) -> object:
            time.sleep(1.1)
            return _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = self._prepare(
                source,
                slow_validator,
                validator_heartbeat=1,
            )

        self.assertTrue(result.ok, result)
        self.assertIn("validator heartbeat", stderr.getvalue())
        self.assertIn("in-flight", stderr.getvalue())
        self.assertIn("elapsed", stderr.getvalue())

    def test_contract_problems_block_baseline_and_preflight_until_revalidated(
        self,
    ) -> None:
        source = self._write_source()

        def contract_problem_validator(packet: prepare.ValidationTaskPacket) -> object:
            payload = _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )
            payload["sessions"][0]["contract_problems"] = [
                {
                    "message": "baseline command has an invalid expected failure",
                    "suggested_fix": "repair the completion contract command",
                }
            ]
            return payload

        with (
            mock.patch.object(prepare, "_run_baseline_stage") as baseline_stage,
            mock.patch.object(prepare.preflight, "run_preflight") as run_preflight,
        ):
            failed = self._prepare(source, contract_problem_validator)

        self.assertFalse(failed.ok)
        self.assertEqual(prepare.PREPARE_EXIT_REASON_VALIDATION_FAILED, failed.reason)
        self.assertEqual(prepare.PREPARE_STAGE_VALIDATION, failed.blockers[0].stage)
        self.assertEqual("1", failed.blockers[0].session_id)
        self.assertEqual(prepare.CONTRACT_PROBLEM_CATEGORY, failed.blockers[0].category)
        baseline_stage.assert_not_called()
        run_preflight.assert_not_called()
        assert failed.plan_dir is not None

        session_path = failed.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "### Verify",
                "- Contract repair evidence is now explicit.\n\n### Verify",
                1,
            ),
            encoding="utf-8",
        )
        calls: list[prepare.ValidationTaskPacket] = []
        resumed = self._prepare(
            None,
            _recording_validator(calls),
            resume_plan_dir=failed.plan_dir,
        )

        self.assertTrue(resumed.ok, resumed)
        self.assertEqual([("1",)], [packet.session_ids for packet in calls])

    def test_unsatisfied_baseline_blocks_and_never_claims_readiness(self) -> None:
        check = {
            "name": "expected-pass",
            "type": "command",
            "argv": ["python3", "-c", "raise SystemExit(1)"],
            "timeout_seconds": 30,
            "baseline_expectation": "pass",
        }
        source = self._write_source(check_json_by_id={"1": check})

        result = self._prepare(source, _recording_validator([]))

        self.assertFalse(result.ok)
        self.assertEqual("baseline-unsatisfied", result.reason)
        self.assertFalse(result.marked_loop_ready)
        self.assertEqual("baseline", result.blockers[0].stage)
        self.assertIn("expected-pass", result.blockers[0].message)
        assert result.plan_dir is not None
        manifest = (result.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)
        assert result.resume_command is not None
        self.assertIn("--resume", result.resume_command)

    def test_preflight_blockers_prevent_loop_ready(self) -> None:
        check = {
            "name": "missing-tool",
            "type": "command",
            "argv": ["asl-prepare-missing-executable-xyz"],
            "timeout_seconds": 30,
        }
        source = self._write_source(check_json_by_id={"1": check})

        result = self._prepare(source, _recording_validator([]))

        self.assertFalse(result.ok)
        self.assertEqual("not-loop-ready", result.reason)
        self.assertFalse(result.marked_loop_ready)
        categories = {blocker.category for blocker in result.blockers}
        self.assertIn("command-not-runnable", categories)
        assert result.plan_dir is not None
        manifest = (result.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)


class ExpandIngestCliTests(unittest.TestCase):
    """`asl expand-ingest` validator-output ingestion wrapper."""

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["expand-ingest", *argv])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.resolve_handler(args)(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def _write_batch(self, tmp: Path, payload: dict) -> Path:
        batch = tmp / "batch.json"
        batch.write_text(json.dumps(payload), encoding="utf-8")
        return batch

    def test_parser_wiring(self) -> None:
        args = cli.build_parser().parse_args(
            ["expand-ingest", "/plan", "--batch-file", "/b.json", "--json"]
        )
        self.assertIs(prepare.expand_ingest_from_args, cli.resolve_handler(args))
        self.assertEqual("/plan", args.plan_dir)
        self.assertEqual("/b.json", args.batch_file)
        self.assertTrue(args.json)

    def test_ingest_renders_expansions_and_applies_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))
            payload = _validator_payload(
                corrections=[
                    {
                        "expected_text": "load_config",
                        "replacement_text": "load_settings",
                        "reason": "renamed helper",
                    }
                ]
            )
            batch = self._write_batch(tmp_path, payload)

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(0, code)
            body = json.loads(stdout)
            self.assertTrue(body["ok"])
            self.assertEqual("ok", body["reason"])
            self.assertEqual(["1"], body["sessions"])
            self.assertTrue((plan_dir / "session-1.expand.md").is_file())
            self.assertTrue(
                any(f.endswith("session-1.expand.md") for f in body["files"])
            )
            self.assertEqual(1, body["correction_summary"]["applied"])
            self.assertEqual("applied", body["corrections"][0]["status"])
            # The stored aggregate persisted the batch.
            stored = prepare.load_prepare_validation(plan_dir)
            assert stored is not None
            self.assertEqual(("1",), stored.session_ids)
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn("load_settings", session_text)

    def test_second_batch_reports_only_its_own_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))

            first = self._write_batch(tmp_path, _validator_payload())
            self._run_cli(str(plan_dir), "--batch-file", str(first), "--json")

            second = tmp_path / "batch2.json"
            second.write_text(
                json.dumps(_validator_payload(session_id="2")), encoding="utf-8"
            )
            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(second), "--json"
            )

            self.assertEqual(0, code)
            body = json.loads(stdout)
            self.assertEqual(["2"], body["sessions"])
            # The aggregate now covers both batches.
            stored = prepare.load_prepare_validation(plan_dir)
            assert stored is not None
            self.assertEqual(("1", "2"), stored.session_ids)

    def test_malformed_payload_fails_closed_with_validator_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            payload = _validator_payload()
            payload["sessions"][0]["claims"] = [{"claim": ""}]
            batch = self._write_batch(tmp_path, payload)

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertFalse(body["ok"])
            self.assertEqual("validator-schema", body["reason"])
            self.assertIn("validator output rejected", body["error"])
            # Nothing was written.
            self.assertFalse((plan_dir / prepare.PREPARE_VALIDATION_FILENAME).exists())
            self.assertFalse((plan_dir / "session-1.expand.md").exists())

    def test_invalid_json_fails_closed_with_validator_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            batch = tmp_path / "batch.json"
            batch.write_text("{not json", encoding="utf-8")

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )
            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertEqual("validator-schema", body["reason"])

    def test_missing_batch_file_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir, ("1",))
            code, stdout, _stderr = self._run_cli(
                str(plan_dir),
                "--batch-file",
                str(Path(tmp) / "nope.json"),
                "--json",
            )
            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertEqual("input-error", body["reason"])

    def test_late_render_failure_leaves_no_partial_mutation(self) -> None:
        """A valid-schema batch that fails during expansion rendering (a session
        with no ``> **Before starting**:`` anchor) commits nothing: the
        aggregate is never persisted, no expansion file is created, and every
        pre-command file is left byte-identical."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            _write_prepare_plan(plan_dir, ("1", "2"))
            # Strip the expansion anchor from session 1 so a valid payload fails
            # closed during reference insertion, after the aggregate would have
            # been prepared.
            session_1 = plan_dir / "session-1.md"
            anchored = session_1.read_text(encoding="utf-8")
            self.assertIn(authoring.CONTEXT_REFERENCE_LINE, anchored)
            session_1.write_text(
                anchored.replace(authoring.CONTEXT_REFERENCE_LINE + "\n", ""),
                encoding="utf-8",
            )

            before = {
                path.name: path.read_bytes()
                for path in sorted(plan_dir.iterdir())
                if path.is_file()
            }
            batch = self._write_batch(tmp_path, _validator_payload())

            code, stdout, _stderr = self._run_cli(
                str(plan_dir), "--batch-file", str(batch), "--json"
            )

            self.assertEqual(1, code)
            body = json.loads(stdout)
            self.assertFalse(body["ok"])
            self.assertEqual("validator-schema", body["reason"])
            # The aggregate was never persisted ahead of the late failure.
            self.assertFalse((plan_dir / prepare.PREPARE_VALIDATION_FILENAME).exists())
            self.assertFalse((plan_dir / "session-1.expand.md").exists())
            # No new artifact and every pre-command file is byte-identical.
            after = {
                path.name: path.read_bytes()
                for path in sorted(plan_dir.iterdir())
                if path.is_file()
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
