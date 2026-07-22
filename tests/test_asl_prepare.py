"""Prepare validator schema, expansion renderer, and prepare CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    init_git_repo,
    _write_prepare_plan,
    _validator_payload,
    _write_prepare_source_plan,
    _prepare_pipeline_payload,
    _recording_validator,
)


class PrepareValidatorSchemaTests(unittest.TestCase):
    """Versioned validator result schema (`agent_session_loop.prepare`)."""

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

        self.assertEqual(prepare.PREPARE_VALIDATION_VERSION, validation.version)
        self.assertEqual("/repo", validation.codebase_root)
        self.assertEqual(("1",), validation.session_ids)
        session = validation.sessions[0]
        # 1 claim + 1 issue + parsed contract.
        self.assertEqual(3, session.claims_checked)
        reparsed = prepare.parse_validator_output(validation.to_dict())
        self.assertEqual(validation, reparsed)

    def test_rejects_wrong_version(self) -> None:
        payload = _validator_payload()
        payload["version"] = 2
        with self.assertRaisesRegex(plans.PlanError, "unsupported version"):
            prepare.parse_validator_output(payload)

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

    def test_to_be_created_claim_must_not_carry_line(self) -> None:
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
        with self.assertRaisesRegex(plans.PlanError, "to-be-created"):
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
            self.assertIn('"version": 1', rendered)
            # Plan file contents are referenced, never inlined.
            self.assertNotIn("Update the retry helper", rendered)
            for issue_type in prepare.ISSUE_TYPES:
                self.assertIn(issue_type, rendered)

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
            self.assertTrue(
                (plan_dir / prepare.PREPARE_VALIDATION_FILENAME).is_file()
            )

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

    def test_malformed_output_cannot_mutate_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = {
                path.name: path.read_bytes() for path in plan_dir.iterdir()
            }
            payload = _validator_payload()
            payload["sessions"][0]["claims"] = [{"claim": ""}]

            with self.assertRaises(plans.PlanError):
                prepare.ingest_validator_output(plan_dir, payload)

            after = {path.name: path.read_bytes() for path in plan_dir.iterdir()}
            self.assertEqual(before, after)
            self.assertFalse(
                (plan_dir / prepare.PREPARE_VALIDATION_FILENAME).exists()
            )

    def test_prepare_validation_excluded_from_readiness_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = plans.stable_readiness_sha256(plan_dir)
            prepare.ingest_validator_output(plan_dir, _validator_payload())
            self.assertEqual(before, plans.stable_readiness_sha256(plan_dir))


class ExpansionRendererTests(unittest.TestCase):
    """Expansion rendering and exact factual corrections."""

    def _validation(self, **session_overrides: object) -> prepare.PrepareValidation:
        return prepare.parse_validator_output(
            _validator_payload(**session_overrides)
        )

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
            session_text_again = (plan_dir / "session-1.md").read_text(
                encoding="utf-8"
            )
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
        return _write_prepare_source_plan(
            self.tmp_path / "demo-plan.md", **kwargs
        )

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["prepare", *argv])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = args.handler(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_defaults_and_handler_wiring(self) -> None:
        args = cli.build_parser().parse_args(["prepare", "plan.md"])

        self.assertIs(prepare.prepare_from_args, args.handler)
        self.assertEqual("plan.md", args.plan)
        self.assertFalse(args.resume)
        self.assertEqual(".", args.cwd)
        self.assertEqual("codex", args.harness)
        self.assertIsNone(args.harness_model)
        self.assertEqual("codex", args.validator_harness)
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
            "--review-harness-model claude-opus-4",
            response["resume_command"],
        )

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
        return _write_prepare_source_plan(
            self.tmp_path / "demo-plan.md", **kwargs
        )

    def _prepare(
        self,
        source: Path | None,
        validator,
        **kwargs: object,
    ) -> prepare.PrepareResult:
        options: dict[str, object] = {
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
        self.assertEqual(
            1, session_text.count(contracts.COMPLETION_CONTRACT_HEADING)
        )
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
        self.assertIn(
            "--review-harness-model claude-opus-4", result.resume_command
        )
        self.assertEqual(
            ["ok", "ok", "ok", "ok", "ok", "ok"],
            [step.status for step in result.steps],
        )

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
        session_text = (result.plan_dir / "session-1.md").read_text(
            encoding="utf-8"
        )
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
        source = self._write_source(
            check_json_by_id={"1": check, "2": check}
        )

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


if __name__ == "__main__":
    unittest.main()
