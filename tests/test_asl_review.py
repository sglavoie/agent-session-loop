"""Structured review artifact, repair materialization, and cycle tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    authoring,
    config,
    contracts,
    events,
    pause_policy,
    plans,
    progress_repair,
    readiness_gate,
    review,
    review_continuation,
    review_delta,
    review_guard,
    review_repair,
    review_session_body,
    review_session_contract,
    run_inputs,
    run_persistence,
    runner,
    state,
    workbench,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _contract_block,
    _discovered,
    _load_script_module,
    _structured_review_finding,
    init_git_repo,
    init_multi_repo_workspace,
    install_fake_codex,
    install_fake_harness,
    read_jsonl,
    write_completed_review_plan,
    write_multi_repo_plan,
    write_plan,
    write_plan_with_ids,
)


class StructuredReviewArtifactTests(unittest.TestCase):
    """Shared review module: verdict grammar, final-review.json, consistency."""

    def _artifact(self, **overrides: object) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": review.REVIEW_ARTIFACT_VERSION,
            "outcome": "findings",
            "findings": [_structured_review_finding()],
        }
        payload.update(overrides)
        return payload

    def test_verification_checks_preserve_repository_alias(self) -> None:
        checks = [
            {
                "type": "command",
                "name": "verify",
                "repo": "SVC",
                "argv": ["python3", "-V"],
            }
        ]

        registry = workspaces.RepositoryRegistry(
            (
                workspaces.Repository(
                    "SVC", "tundra_svc", "git@github.com:tundra/SVC.git"
                ),
            )
        )

        self.assertEqual(
            checks,
            review._validate_verification_checks(
                "F1", checks, repository_registry=registry
            ),
        )

    def test_verification_checks_preserve_review_replay(self) -> None:
        checks = [
            {
                "type": "command",
                "name": "durable-current-state",
                "argv": ["python3", "-V"],
                "review_replay": True,
            }
        ]

        self.assertEqual(
            checks,
            review._validate_verification_checks("F1", checks),
        )

    def test_parse_review_artifact_accepts_complete_findings(self) -> None:
        artifact = review.parse_review_artifact(json.dumps(self._artifact()))

        self.assertEqual(review.REVIEW_ARTIFACT_VERSION, artifact["version"])
        self.assertEqual("findings", artifact["outcome"])
        self.assertEqual(1, artifact["total_findings"])
        self.assertEqual(
            {"high": 1, "med": 0, "low": 0}, artifact["findings_by_severity"]
        )
        self.assertEqual("F1", artifact["findings"][0]["id"])
        self.assertEqual("run_loop", artifact["findings"][0]["evidence"][0]["symbol"])

    def test_parse_review_artifact_accepts_all_clear(self) -> None:
        artifact = review.parse_review_artifact(
            json.dumps(self._artifact(outcome="all-clear", findings=[]))
        )

        self.assertEqual("all-clear", artifact["outcome"])
        self.assertEqual(0, artifact["total_findings"])
        self.assertEqual([], artifact["findings"])

    def test_parse_review_artifact_rejects_unsupported_version(self) -> None:
        for version in (None, 1, 4, "2"):
            with self.assertRaisesRegex(
                review.ReviewArtifactError, "'version' must be"
            ):
                review.parse_review_artifact(
                    json.dumps(self._artifact(version=version))
                )

    def test_repository_aware_artifact_requires_qualified_attribution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_multi_repo_workspace(
                tmp_dir / "workspace", DEFAULT_MULTI_REPO_SPECS[:2]
            )
            plan_dir = tmp_dir / "plan"
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                specs=DEFAULT_MULTI_REPO_SPECS[:2],
            )
            registry = plans.read_repository_registry(plan_dir)
            assert registry is not None
            finding = _structured_review_finding(
                repo="SVC",
                evidence=[
                    {
                        "path": "OAK:README.md",
                        "symbol": "cross_repository_contract",
                    }
                ],
                verification_checks=[
                    {
                        "type": "command",
                        "name": "verify-oak",
                        "repo": "OAK",
                        "argv": ["python3", "-c", "pass"],
                    }
                ],
            )
            payload = self._artifact(
                version=review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                findings=[finding],
            )

            parsed = review.parse_review_artifact(
                json.dumps(payload), repository_registry=registry
            )

            self.assertEqual("SVC", parsed["findings"][0]["repo"])
            self.assertEqual(
                "OAK:README.md",
                parsed["findings"][0]["evidence"][0]["path"],
            )

            for label, mutation in (
                ("primary", lambda row: row.pop("repo")),
                (
                    "evidence",
                    lambda row: row["evidence"][0].__setitem__("path", "README.md"),
                ),
                (
                    "check",
                    lambda row: row["verification_checks"][0].pop("repo"),
                ),
            ):
                invalid = json.loads(json.dumps(payload))
                mutation(invalid["findings"][0])
                with (
                    self.subTest(label=label),
                    self.assertRaises(review.ReviewArtifactError),
                ):
                    review.parse_review_artifact(
                        json.dumps(invalid), repository_registry=registry
                    )

    def test_parse_review_artifact_rejects_invalid_json_and_shape(self) -> None:
        with self.assertRaisesRegex(review.ReviewArtifactError, "is empty"):
            review.parse_review_artifact("   \n")
        with self.assertRaisesRegex(review.ReviewArtifactError, "not valid JSON"):
            review.parse_review_artifact("{not json")
        with self.assertRaisesRegex(
            review.ReviewArtifactError, "top-level JSON object"
        ):
            review.parse_review_artifact("[]")
        with self.assertRaisesRegex(review.ReviewArtifactError, "unknown keys"):
            review.parse_review_artifact(json.dumps(self._artifact(extra="nope")))
        with self.assertRaisesRegex(
            review.ReviewArtifactError, "'outcome' must be one of"
        ):
            review.parse_review_artifact(json.dumps(self._artifact(outcome="mixed")))

    def test_parse_review_artifact_rejects_incomplete_findings(self) -> None:
        for missing_key in review.FINDING_REQUIRED_KEYS:
            finding = _structured_review_finding()
            del finding[missing_key]
            with self.assertRaisesRegex(
                review.ReviewArtifactError, "missing required keys"
            ):
                review.parse_review_artifact(
                    json.dumps(self._artifact(findings=[finding]))
                )

    def test_parse_review_artifact_rejects_invalid_finding_values(self) -> None:
        cases = (
            ({"severity": "critical"}, "'severity' must be one of"),
            ({"title": "  "}, "'title' must be a non-empty string"),
            ({"evidence": []}, "'evidence' must be a non-empty list"),
            (
                {"evidence": [{"symbol": "f"}]},
                "'path' must be a non-empty string",
            ),
            (
                {"evidence": [{"path": "a.py", "line": 3}]},
                "unknown keys",
            ),
            ({"done_criteria": []}, "'done_criteria' must be a non-empty list"),
            (
                {"verification_checks": [{"type": "bogus"}]},
                "verification check #1 is invalid",
            ),
            (
                {
                    "verification_checks": [
                        {
                            "type": "command",
                            "name": "dup",
                            "argv": ["python3", "-c", "pass"],
                        },
                        {
                            "type": "command",
                            "name": "dup",
                            "argv": ["python3", "-c", "pass"],
                        },
                    ]
                },
                "is duplicated",
            ),
        )
        for overrides, expected in cases:
            with self.assertRaisesRegex(review.ReviewArtifactError, expected):
                review.parse_review_artifact(
                    json.dumps(
                        self._artifact(
                            findings=[_structured_review_finding(**overrides)]
                        )
                    )
                )

    def test_parse_review_artifact_rejects_an_absence_gate(self) -> None:
        # A review that free-authors an absence flag onto a presence check is
        # emitting a gate that passes exactly when the forbidden text is there.
        with self.assertRaises(review.ReviewArtifactError) as caught:
            review.parse_review_artifact(
                json.dumps(
                    self._artifact(
                        findings=[
                            _structured_review_finding(
                                verification_checks=[
                                    {
                                        "type": "regex_in_file",
                                        "name": "drops-the-claim",
                                        "path": "docs/evidence.md",
                                        "pattern": "75-pixel",
                                        "expect_absent": True,
                                    }
                                ]
                            )
                        ]
                    )
                )
            )

        message = str(caught.exception)
        self.assertIn("verification check #1 is invalid", message)
        self.assertIn("'drops-the-claim'", message)
        self.assertIn("'expect_absent'", message)
        self.assertIn(
            "regex_in_file asserts the pattern is PRESENT and has no absence semantics",
            message,
        )

    def test_parse_review_artifact_rejects_duplicate_finding_ids(self) -> None:
        artifact = self._artifact(
            findings=[
                _structured_review_finding(finding_id="F1"),
                _structured_review_finding(finding_id="F1", severity="low"),
            ]
        )
        with self.assertRaisesRegex(
            review.ReviewArtifactError, "finding id 'F1' is duplicated"
        ):
            review.parse_review_artifact(json.dumps(artifact))

    def test_parse_review_artifact_rejects_outcome_findings_mismatch(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            review.ReviewArtifactError, "contradicts a non-empty findings list"
        ):
            review.parse_review_artifact(
                json.dumps(self._artifact(outcome="all-clear"))
            )
        with self.assertRaisesRegex(
            review.ReviewArtifactError, "requires a non-empty findings list"
        ):
            review.parse_review_artifact(json.dumps(self._artifact(findings=[])))

    def test_ensure_review_consistency_detects_contradictions(self) -> None:
        artifact = review.parse_review_artifact(json.dumps(self._artifact()))
        matching = review.parse_review_verdict(
            "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n"
        )
        review.ensure_review_consistency(matching, artifact)

        all_clear = review.parse_review_verdict(
            f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n"
        )
        with self.assertRaisesRegex(
            review.ReviewConsistencyError, "contradicts the final-review.md"
        ):
            review.ensure_review_consistency(all_clear, artifact)

        wrong_severity = review.parse_review_verdict(
            "body\nStatus: 1 Findings (0 high, 1 med, 0 low)\n"
        )
        with self.assertRaisesRegex(review.ReviewConsistencyError, "severity totals"):
            review.ensure_review_consistency(wrong_severity, artifact)

    def test_review_session_marker_versions(self) -> None:
        self.assertIsNone(review.review_session_marker_version("# Session 1"))
        self.assertEqual(
            1,
            review.review_session_marker_version(
                f"# Review\n{review.REVIEW_SESSION_MARKER}\n"
            ),
        )
        self.assertEqual(
            review.GENERATED_REVIEW_SESSION_VERSION,
            review.review_session_marker_version(
                f"# Review\n{review.review_session_version_marker()}\n"
            ),
        )
        self.assertEqual(
            2,
            review.review_session_marker_version(
                f"# Review\n{review.review_session_version_marker(2)}\n"
            ),
        )
        self.assertTrue(
            review.contains_review_session_marker(
                review.review_session_version_marker()
            )
        )

    def test_review_artifact_sha256_helpers_hash_full_files_in_chunks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            markdown = b"review\n" + (b"m" * (1024 * 1024 + 17))
            artifact = b'{"findings":"' + (b"j" * (1024 * 1024 + 31)) + b'"}'
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_bytes(markdown)
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_bytes(artifact)

            self.assertEqual(
                hashlib.sha256(markdown).hexdigest(),
                review.final_review_output_sha256(plan_dir),
            )
            self.assertEqual(
                hashlib.sha256(artifact).hexdigest(),
                review.final_review_artifact_sha256(plan_dir),
            )

    def test_load_review_verdict_markdown_only_stays_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
            )

            verdict = review.load_review_verdict(plan_dir)

            self.assertTrue(verdict["ok"], verdict)
            self.assertEqual("all-clear", verdict["outcome"])
            self.assertNotIn("findings", verdict)
            self.assertNotIn("artifact_version", verdict)

    def test_load_review_verdict_requires_artifact_for_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
            )

            verdict = review.load_review_verdict(plan_dir, require_artifact=True)

            self.assertFalse(verdict["ok"])
            self.assertEqual("missing-review-artifact", verdict["reason"])
            self.assertIn("review_artifact_path", verdict)

    def test_load_review_verdict_fails_closed_on_bad_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )
            artifact_path = plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME

            artifact_path.write_text("{not json", encoding="utf-8")
            malformed = review.load_review_verdict(plan_dir)
            self.assertFalse(malformed["ok"])
            self.assertEqual("malformed-review-artifact", malformed["reason"])

            artifact_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "outcome": "findings",
                        "findings": [_structured_review_finding(severity="low")],
                    }
                ),
                encoding="utf-8",
            )
            contradictory = review.load_review_verdict(plan_dir)
            self.assertFalse(contradictory["ok"])
            self.assertEqual("contradictory-review-artifact", contradictory["reason"])

    def test_load_review_verdict_merges_valid_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(self._artifact()), encoding="utf-8"
            )

            verdict = review.load_review_verdict(plan_dir, require_artifact=True)

            self.assertTrue(verdict["ok"], verdict)
            self.assertEqual(
                review.REVIEW_ARTIFACT_VERSION, verdict["artifact_version"]
            )
            self.assertEqual("F1", verdict["findings"][0]["id"])
            self.assertEqual(
                str(plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME),
                verdict["review_artifact_path"],
            )

    def test_load_review_verdict_degrades_when_registry_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n| Alias | Path |\n"
                "| --- | --- |\n| Core | core |\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(self._artifact()), encoding="utf-8"
            )

            verdict = review.load_review_verdict(plan_dir)

            self.assertFalse(verdict["ok"])
            self.assertEqual(review.REVIEW_REASON_MALFORMED_ARTIFACT, verdict["reason"])
            self.assertIn("repository registry", verdict["error"])
            self.assertEqual([], review_delta._prior_review_findings(plan_dir))

    def test_runner_and_autopilot_report_same_verdict_payload(self) -> None:
        autopilot = _load_script_module(
            "asl_autopilot_review_parity",
            "skills",
            "asl",
            "scripts",
            "asl_autopilot.py",
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(self._artifact()), encoding="utf-8"
            )

            runner_verdict = runner._read_review_verdict(
                plan_dir, require_artifact=True
            )
            autopilot_verdict = autopilot._read_review_verdict(
                {"plan_dir": str(plan_dir)}
            )

            summary = autopilot_verdict.pop("summary")
            self.assertIn("Status: 1 Findings", summary)
            # The helper additionally flags truncation and points at the full
            # review; drop those before comparing the shared verdict payload.
            self.assertFalse(autopilot_verdict.pop("summary_truncated"))
            self.assertIn("asl review-verdict", autopilot_verdict.pop("full_review"))
            self.assertEqual(runner_verdict, autopilot_verdict)
            self.assertIs(autopilot.parse_review_verdict, review.parse_review_verdict)

    def test_eventless_disposition_enforces_artifact_for_v2_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            session_two = plan_dir / "session-2.md"
            session_two.write_text(
                session_two.read_text(encoding="utf-8").replace(
                    review.review_session_version_marker(),
                    review.review_session_version_marker(
                        review.STRUCTURED_REVIEW_MIN_VERSION
                    ),
                ),
                encoding="utf-8",
            )
            snapshot = plans.inspect_plan(plan_dir)
            source = next(session for session in snapshot.sessions if session.id == "2")

            self.assertEqual(
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
                review_continuation._eventless_review_disposition(
                    snapshot.plan_dir, source
                ),
            )

            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            self.assertIsNone(
                review_continuation._eventless_review_disposition(
                    snapshot.plan_dir, source
                )
            )

    def test_generated_review_session_body_renders_delta_scope(self) -> None:
        finding = {
            "id": "F1",
            "severity": "high",
            "title": "Guard is bypassed",
            "evidence": [
                {"path": "agent_session_loop/runner.py", "symbol": "guard"},
                {"path": "docs/agent-session-loop.md"},
            ],
            "done_criteria": ["The guard rejects the bypass."],
        }
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans.SessionId((7,))

            full = review_session_body.build_generated_review_session_body(
                session_id, review_harness="claude", plan_dir=plan_dir
            )
            delta = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
                delta={
                    "mode": review_session_body.REVIEW_SCOPE_DELTA,
                    "base_sha": "f" * 40,
                    "changed_files": ["agent_session_loop/runner.py"],
                    "prior_findings": [finding],
                },
            )
            degraded = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
                delta={
                    "mode": review_session_body.REVIEW_SCOPE_FULL,
                    "prior_findings": [finding],
                },
            )

            # The first review of a run renders no scope section at all.
            self.assertNotIn("## Delta Review Scope", full)

            self.assertIn("## Delta Review Scope", delta)
            flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
            self.assertIn(f"git diff {flags} {'f' * 40}", delta)
            self.assertIn(f"git diff --cached {flags} {'f' * 40}", delta)
            self.assertIn("`agent_session_loop/runner.py`", delta)
            self.assertIn("`F1` (high): Guard is bypassed", delta)
            self.assertIn("Done when: The guard rejects the bypass.", delta)

            self.assertIn("## Delta Review Scope", degraded)
            self.assertNotIn("git diff", degraded)
            self.assertIn("this review stays full scope", degraded)
            self.assertIn("this scope is not narrowed", degraded)
            self.assertIn("`F1` (high): Guard is bypassed", degraded)

            # Scope narrows reading only: grammar, artifacts, and the
            # completion contract stay identical across all three bodies.
            for body in (full, delta, degraded):
                contract = contracts.parse_completion_contract(
                    body, session_id=session_id
                )
                self.assertEqual(
                    [
                        "final-review-output-evidence-based",
                        "final-review-artifact-consistent",
                    ],
                    [check.name for check in contract.verify.checks],
                )
                self.assertEqual(
                    review.GENERATED_REVIEW_SESSION_VERSION,
                    review.review_session_marker_version(body),
                )
                self.assertIn(runner.FINAL_REVIEW_ARTIFACT_FILENAME, body)
            self.assertEqual(
                contracts.parse_completion_contract(
                    full, session_id=session_id
                ).done_when,
                contracts.parse_completion_contract(
                    delta, session_id=session_id
                ).done_when,
            )

    def test_generated_review_session_body_is_versioned_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans.SessionId((7,))

            body = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
            )

            self.assertIn(runner.REVIEW_SESSION_MARKER, body)
            self.assertIn(review.review_session_version_marker(), body)
            self.assertIn(runner.FINAL_REVIEW_ARTIFACT_FILENAME, body)
            self.assertEqual(
                review.GENERATED_REVIEW_SESSION_VERSION,
                review.review_session_marker_version(body),
            )
            contract = contracts.parse_completion_contract(body, session_id=session_id)
            self.assertEqual(
                [
                    "final-review-output-evidence-based",
                    "final-review-artifact-consistent",
                ],
                [check.name for check in contract.verify.checks],
            )

    def test_generated_review_session_body_states_presence_only_semantics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = review_session_body.build_generated_review_session_body(
                plans.SessionId((7,)),
                review_harness="claude",
                plan_dir=Path(tmp),
            )

            self.assertIn(
                "A verification check accepts only the keys its own type "
                "documents; any other key is rejected.",
                body,
            )
            self.assertIn(
                "`regex_in_file` asserts the pattern is PRESENT, and neither "
                "has absence semantics",
                body,
            )
            # The gate the review runs against itself says the same thing.
            contract = contracts.parse_completion_contract(
                body, session_id=plans.SessionId((7,))
            )
            self.assertIn(
                contracts.CHECK_PRESENCE_SEMANTICS["regex_in_file"],
                contract.verify.checks[1].argv[2],
            )

    def test_generated_artifact_contract_check_gates_consistency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans.SessionId((7,))
            body = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
            )
            contract = contracts.parse_completion_contract(body, session_id=session_id)
            artifact_check = contract.verify.checks[1]
            md_path = plan_dir / review.FINAL_REVIEW_FILENAME
            artifact_path = plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
            md_path.write_text(
                "## Findings\n"
                "- One issue in agent_session_loop/runner.py.\n"
                "Status: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )

            def run_check() -> int:
                return subprocess.run(
                    list(artifact_check.argv),
                    cwd=plan_dir,
                    capture_output=True,
                    check=False,
                ).returncode

            # Missing artifact fails.
            self.assertEqual(1, run_check())
            # Consistent artifact passes.
            artifact_path.write_text(json.dumps(self._artifact()), encoding="utf-8")
            self.assertEqual(0, run_check())
            # Contradictory severity totals fail.
            artifact_path.write_text(
                json.dumps(
                    self._artifact(
                        findings=[_structured_review_finding(severity="low")]
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(1, run_check())
            # Incomplete findings fail.
            incomplete = _structured_review_finding()
            del incomplete["repair_group"]
            artifact_path.write_text(
                json.dumps(self._artifact(findings=[incomplete])),
                encoding="utf-8",
            )
            self.assertEqual(1, run_check())


class GeneratedReviewReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"

    def _write_replay_plan(self) -> None:
        durable = json.dumps(
            {
                "type": "file_exists",
                "name": "durable-readme",
                "path": "README.md",
                "baseline_expectation": "fail",
                "review_replay": True,
            }
        )
        historical = json.dumps(
            {
                "type": "command",
                "name": "historical-head",
                "argv": ["git", "rev-parse", "HEAD"],
            }
        )
        dirty_tree = json.dumps(
            {
                "type": "command",
                "name": "session-local-clean-tree",
                "argv": ["git", "diff", "--quiet"],
            }
        )
        write_plan(
            self.plan_dir,
            ("pending", "pending"),
            contracts_by_id={
                "1": _contract_block(
                    ",".join((durable, historical, dirty_tree)), session="1"
                ),
                "2": _contract_block(durable, session="2"),
            },
        )

    def test_broad_review_replays_only_opted_in_durable_gates(self) -> None:
        self._write_replay_plan()

        first = review_session_contract._broad_review_verification_checks(self.plan_dir)
        review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        second = review_session_contract._broad_review_verification_checks(
            self.plan_dir
        )

        self.assertEqual(first, second)
        self.assertEqual(["broad:1:durable-readme"], [row["name"] for row in first])
        self.assertNotIn("baseline_expectation", first[0])
        self.assertNotIn("review_replay", first[0])
        serialized = json.dumps(first)
        self.assertNotIn("historical-head", serialized)
        self.assertNotIn("session-local-clean-tree", serialized)

    def test_pending_generated_review_refresh_preserves_lifetime_sections(
        self,
    ) -> None:
        self._write_replay_plan()
        session_id = plans.parse_session_id("3")
        current = review_session_body.build_generated_review_session_body(
            session_id,
            review_harness="claude",
            plan_dir=self.plan_dir,
            delta={"mode": review_session_body.REVIEW_SCOPE_FULL, "prior_findings": []},
        )
        old = current.replace(
            review.review_session_version_marker(),
            review.review_session_version_marker(2),
            1,
        ).replace(
            "## Completion Contract",
            "## Ordered repository review base\n\n- preserved-base\n\n"
            "## Completion Contract",
            1,
        )
        old = old.replace(
            "- **Key decisions**: (none yet)",
            "- **Key decisions**: preserve this handoff",
            1,
        )
        old_scope = old.split("## Delta Review Scope", 1)[1].split(
            "## Completion Contract", 1
        )[0]
        old_handoff = old.split("## Handoff", 1)[1]

        refreshed = review_session_contract.refreshed_generated_review_contract(
            old,
            session_id,
            plan_dir=self.plan_dir,
        )

        self.assertIn(review.review_session_version_marker(), refreshed)
        self.assertEqual(
            old_scope,
            refreshed.split("## Delta Review Scope", 1)[1].split(
                "## Completion Contract", 1
            )[0],
        )
        self.assertIn("- preserved-base", refreshed)
        self.assertEqual(old_handoff, refreshed.split("## Handoff", 1)[1])
        contract = contracts.parse_completion_contract(refreshed, session_id="3")
        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
                "broad:1:durable-readme",
            ],
            [check.name for check in contract.verify.checks],
        )


class ReviewVerdictCommandTests(unittest.TestCase):
    """`asl review-verdict` authoritative, untruncated verdict surface."""

    def _artifact(self, **overrides: object) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": review.REVIEW_ARTIFACT_VERSION,
            "outcome": "findings",
            "findings": [_structured_review_finding()],
        }
        payload.update(overrides)
        return payload

    def _write_review(
        self, plan_dir: Path, body: str, artifact: dict | None = None
    ) -> None:
        (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(body, encoding="utf-8")
        if artifact is not None:
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(artifact), encoding="utf-8"
            )

    def test_all_clear_payload_has_no_findings_and_no_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(plan_dir, f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n")

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual("review-verdict", payload["command"])
            self.assertEqual("all-clear", payload["outcome"])
            self.assertIsNone(payload["findings"])
            self.assertFalse(payload["summary_truncated"])
            self.assertEqual([], payload["next_steps"])
            self.assertIn("review_resume", payload)

    def test_findings_payload_carries_full_untruncated_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            big_body = "detail " * 1000
            self._write_review(
                plan_dir,
                f"{big_body}\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                artifact=self._artifact(),
            )

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual("findings", payload["outcome"])
            self.assertEqual("F1", payload["findings"][0]["id"])
            # The summary is never truncated by this surface, even well past
            # the helper's bounded excerpt limit.
            self.assertFalse(payload["summary_truncated"])
            self.assertEqual(payload["summary_total_chars"], len(payload["summary"]))
            self.assertGreater(payload["summary_total_chars"], 2000)
            self.assertTrue(payload["next_steps"])

    def test_missing_review_reports_stable_reason_and_generate_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MISSING, payload["reason"])
            self.assertEqual("", payload["summary"])
            self.assertEqual(1, len(payload["next_steps"]))
            self.assertIn("--review-harness", payload["next_steps"][0])

    def test_malformed_review_reports_stable_reason_and_correct_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(plan_dir, "no status line here\n")

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MALFORMED, payload["reason"])
            self.assertEqual(1, len(payload["next_steps"]))
            self.assertIn(review.FINAL_REVIEW_FILENAME, payload["next_steps"][0])

    def test_malformed_artifact_reports_stable_reason_and_correct_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(
                plan_dir, "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n"
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                "{not json", encoding="utf-8"
            )

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MALFORMED_ARTIFACT, payload["reason"])
            self.assertIn(
                review.FINAL_REVIEW_ARTIFACT_FILENAME,
                payload["next_steps"][0],
            )

    def test_missing_review_step_keeps_metacharacter_plan_path_one_arg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "weird plan dir; echo $HOME"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertEqual(review.REVIEW_REASON_MISSING, payload["reason"])
            command = payload["next_steps"][0]
            # The whitespace/metacharacter plan path round-trips as exactly one
            # argument under shlex.split instead of splitting apart.
            self.assertIn(payload["plan_dir"], shlex.split(command))

    def test_findings_fallback_step_keeps_metacharacter_plan_path_one_arg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "weird plan dir; echo $HOME"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            # A plain findings review (no generated-review marker, no events)
            # yields no continuation guidance, so the findings-without-guidance
            # fallback command is emitted.
            self._write_review(
                plan_dir,
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                artifact=self._artifact(),
            )

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertEqual("findings", payload["outcome"])
            command = payload["next_steps"][0]
            self.assertIn("--review-harness", command)
            self.assertIn(payload["plan_dir"], shlex.split(command))

    def test_from_args_json_round_trips_and_rejects_malformed_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(plan_dir, f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n")

            args = argparse.Namespace(
                plan_dir=str(plan_dir), state_dir=str(state_dir), json=True
            )
            out = io.StringIO()
            with redirect_stdout(out):
                code = review.review_verdict_from_args(args)
            self.assertEqual(0, code)
            payload = json.loads(out.getvalue())
            self.assertEqual("review-verdict", payload["command"])
            self.assertTrue(payload["ok"])

            missing = argparse.Namespace(
                plan_dir=str(Path(tmp) / "nope"),
                state_dir=str(state_dir),
                json=True,
            )
            err_out = io.StringIO()
            with redirect_stdout(err_out):
                code = review.review_verdict_from_args(missing)
            self.assertEqual(1, code)
            err_payload = json.loads(err_out.getvalue())
            self.assertFalse(err_payload["ok"])
            self.assertEqual("malformed-plan", err_payload["reason"])

    def _run_from_args(self, plan_dir: Path, state_dir: Path) -> tuple[int, dict]:
        args = argparse.Namespace(
            plan_dir=str(plan_dir), state_dir=str(state_dir), json=True
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = review.review_verdict_from_args(args)
        return code, json.loads(out.getvalue())

    def test_from_args_exit_status_tracks_verdict_validity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            # Valid all-clear → ok, exit 0.
            clear_dir = Path(tmp) / "clear"
            write_plan(clear_dir, ("complete",))
            self._write_review(clear_dir, f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n")
            code, payload = self._run_from_args(clear_dir, state_dir)
            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-clear", payload["outcome"])

            # Valid findings → ok, exit 0.
            findings_dir = Path(tmp) / "findings"
            write_plan(findings_dir, ("complete",))
            self._write_review(
                findings_dir,
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                artifact=self._artifact(),
            )
            code, payload = self._run_from_args(findings_dir, state_dir)
            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertEqual("findings", payload["outcome"])

            # Missing review → non-ok, exit 1.
            missing_dir = Path(tmp) / "missing"
            write_plan(missing_dir, ("complete",))
            code, payload = self._run_from_args(missing_dir, state_dir)
            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MISSING, payload["reason"])

            # Malformed review → non-ok, exit 1.
            malformed_dir = Path(tmp) / "malformed"
            write_plan(malformed_dir, ("complete",))
            self._write_review(malformed_dir, "no status line here\n")
            code, payload = self._run_from_args(malformed_dir, state_dir)
            self.assertEqual(1, code)
            self.assertEqual(review.REVIEW_REASON_MALFORMED, payload["reason"])

            # Contradictory artifact → non-ok, exit 1.
            contra_dir = Path(tmp) / "contra"
            write_plan(contra_dir, ("complete",))
            self._write_review(
                contra_dir,
                f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n",
                artifact=self._artifact(),
            )
            code, payload = self._run_from_args(contra_dir, state_dir)
            self.assertEqual(1, code)
            self.assertEqual(
                review.REVIEW_REASON_CONTRADICTORY_ARTIFACT, payload["reason"]
            )

    def test_generated_v2_review_without_artifact_reports_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            # A completed generated v2 review with an all-clear Markdown
            # verdict but the structured artifact removed.
            write_completed_review_plan(plan_dir, [])
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            code, payload = self._run_from_args(plan_dir, state_dir)

            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, payload["reason"])
            self.assertIn(
                review.FINAL_REVIEW_ARTIFACT_FILENAME,
                payload["next_steps"][0],
            )

    def test_legacy_v1_markdown_only_review_stays_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_completed_review_plan(plan_dir, [])
            # Downgrade the generated review session to legacy v1 (base marker
            # only) and drop the structured artifact: v1 reviews are
            # Markdown-only, so the all-clear verdict must remain valid.
            session_two = plan_dir / "session-2.md"
            body = session_two.read_text(encoding="utf-8")
            body = body.replace(review.review_session_version_marker() + "\n", "")
            session_two.write_text(body, encoding="utf-8")
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            code, payload = self._run_from_args(plan_dir, state_dir)

            self.assertEqual(0, code)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual("all-clear", payload["outcome"])
            self.assertEqual([], payload["next_steps"])

    def test_pending_v2_review_still_requires_artifact_after_bump(
        self,
    ) -> None:
        # Enforcement is pinned to STRUCTURED_REVIEW_MIN_VERSION, so a review
        # session written before the generated-version bump keeps requiring
        # final-review.json instead of silently degrading to Markdown-only.
        self.assertLess(
            review.STRUCTURED_REVIEW_MIN_VERSION,
            review.GENERATED_REVIEW_SESSION_VERSION,
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_completed_review_plan(plan_dir, [])
            session_two = plan_dir / "session-2.md"
            body = session_two.read_text(encoding="utf-8").replace(
                review.review_session_version_marker(),
                review.review_session_version_marker(
                    review.STRUCTURED_REVIEW_MIN_VERSION
                ),
            )
            session_two.write_text(body, encoding="utf-8")
            self.assertEqual(
                review.STRUCTURED_REVIEW_MIN_VERSION,
                review.review_session_marker_version(body),
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            code, payload = self._run_from_args(plan_dir, state_dir)

            self.assertEqual(1, code)
            self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, payload["reason"])


class ReviewRepairMaterializationTests(unittest.TestCase):
    """Materialize validated review findings into serial repair children."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"

    def _finding(
        self,
        finding_id: str,
        repair_group: str,
        *,
        severity: str = "high",
        **overrides: object,
    ) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "repair_group": repair_group,
            "title": f"Fix {finding_id}",
            "evidence": [{"path": f"pkg/{finding_id}.py", "symbol": "target"}],
            "explanation": f"{finding_id} is broken.",
            "done_criteria": [f"{finding_id} is fixed and tested."],
            "verification_checks": [
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        }
        defaults.update(overrides)
        return _structured_review_finding(
            finding_id=finding_id, severity=severity, **defaults
        )

    def _status_line(self, findings: list[dict[str, Any]]) -> str:
        if not findings:
            return review.REVIEW_STATUS_ALL_CLEAR
        high = sum(1 for f in findings if f["severity"] == "high")
        med = sum(1 for f in findings if f["severity"] == "med")
        low = sum(1 for f in findings if f["severity"] == "low")
        return f"Status: {len(findings)} Findings ({high} high, {med} med, {low} low)"

    def _write_plan(
        self,
        findings: list[dict[str, Any]],
        *,
        with_manifest_table: bool = True,
        review_status: str = "complete",
        session_one_contract: str | None = None,
        outcome: str = "findings",
    ) -> None:
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        (self.plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")

        if with_manifest_table:
            sessions = (
                authoring.ManifestSession(
                    plans.parse_session_id("1"), "One", "complete"
                ),
                authoring.ManifestSession(
                    plans.parse_session_id("2"), "Review", "complete"
                ),
            )
            manifest = authoring.render_manifest(
                authoring.ManifestSpec(
                    plan_title="Test Plan",
                    source_path="/src/plan.md",
                    output_dir=str(self.plan_dir),
                    plan_id="pid",
                    source_fingerprint="fp",
                    content_sha256="csha",
                    sessions=sessions,
                    split_date="2026-01-01",
                    readiness=plans.ReadinessMetadata(
                        "draft", "sha", "2026-01-01T00:00:00Z"
                    ),
                )
            )
        else:
            manifest = "# Manifest\n\n**Sessions**: 2\n"
        (self.plan_dir / "manifest.md").write_text(manifest, encoding="utf-8")

        session_one = [
            "# Session 1: One",
            "",
            "## Instructions",
            "",
            "Do the first thing.",
            "",
        ]
        if session_one_contract:
            session_one.extend([session_one_contract, ""])
        session_one.extend(
            [
                "## Handoff",
                "",
                "- **Status**: complete",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        )
        (self.plan_dir / "session-1.md").write_text(
            "\n".join(session_one), encoding="utf-8"
        )

        review_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("2"),
            review_harness="codex",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", f"- **Status**: {review_status}")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")

        (self.plan_dir / "final-review.md").write_text(
            "# Review\n\n## Findings\n\n"
            "See pkg/a.py and pkg/b.py and README.md.\n\n"
            f"{self._status_line(findings)}\n",
            encoding="utf-8",
        )
        (self.plan_dir / "final-review.json").write_text(
            json.dumps({"version": 2, "outcome": outcome, "findings": findings}),
            encoding="utf-8",
        )

    def _make_registered_finding(
        self,
        finding_id: str,
        repair_group: str,
        repo: str,
    ) -> dict[str, Any]:
        finding = self._finding(
            finding_id,
            repair_group,
            evidence=[{"path": f"{repo}:pkg/{finding_id}.py", "symbol": "target"}],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "repo": repo,
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )
        finding["repo"] = repo
        return finding

    def _register_plan(
        self,
        findings: list[dict[str, Any]],
        *,
        artifact_version: int = review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
    ) -> None:
        self._write_plan(findings)
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n",
            encoding="utf-8",
        )
        for session_id, writes in (("1", "SVC, OAK"), ("2", "(none)")):
            path = self.plan_dir / f"session-{session_id}.md"
            heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
            body = (
                f"{heading}\n\n**Reads repositories**: SVC, OAK\n"
                f"**Writes repositories**: {writes}\n{remainder}"
            )
            if session_id == "2":
                body = body.replace(
                    "- **Files modified**: (none yet)",
                    "- **Files modified**: (none)",
                )
            path.write_text(body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": artifact_version,
                    "outcome": "findings",
                    "findings": findings,
                }
            ),
            encoding="utf-8",
        )

    def _materialize(self, **kwargs: object) -> review_repair.ReviewRepairResult:
        return review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, **kwargs
        )

    def _plan_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in self.plan_dir.rglob("*")
            if path.is_file()
        }

    def _write_review_events(self, *payloads: dict[str, Any]) -> Path:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(
            "".join(
                json.dumps(
                    {
                        "name": events.REVIEW_CYCLE_EVENT,
                        "payload": payload,
                    }
                )
                + "\n"
                for payload in payloads
            ),
            encoding="utf-8",
        )
        return event_path

    def test_dry_planning_is_complete_and_performs_no_writes(self) -> None:
        findings = [
            self._finding("F1", "runner"),
            self._finding("F2", "runner", severity="med"),
            self._finding("F3", "workbench", severity="low"),
        ]
        self._write_plan(findings)
        before = self._plan_bytes()

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, repair_plan.reason)
        self.assertEqual("2", repair_plan.review_session_id.value)
        self.assertEqual(
            tuple(review.finding_fingerprint(finding) for finding in findings),
            repair_plan.finding_fingerprints,
        )
        self.assertEqual(
            ["runner", "workbench"],
            [group.repair_group for group in repair_plan.repair_groups],
        )
        self.assertEqual(
            ["2.1", "2.2"],
            [session_id.value for session_id in repair_plan.proposed_child_ids],
        )
        self.assertEqual(("F1", "F2"), repair_plan.repair_groups[0].finding_ids)
        self.assertIn(
            "## Completion Contract",
            repair_plan.repair_groups[0].rendered_text,
        )
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self.state_dir.exists())
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_registered_groups_read_all_repositories_and_write_finding_aliases(
        self,
    ) -> None:
        findings = [
            self._make_registered_finding("F1", "shared", "OAK"),
            self._make_registered_finding("F2", "shared", "SVC"),
            self._make_registered_finding("F3", "svc-only", "SVC"),
        ]
        self._register_plan(findings)

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        shared, svc_only = repair_plan.repair_groups
        self.assertEqual(("SVC", "OAK"), shared.read_repositories)
        self.assertEqual(("SVC", "OAK"), shared.write_repositories)
        self.assertEqual(("SVC",), svc_only.write_repositories)
        self.assertIn("**Reads repositories**: SVC, OAK", shared.rendered_text)
        self.assertIn("**Writes repositories**: SVC, OAK", shared.rendered_text)
        self.assertIn("**Writes repositories**: SVC", svc_only.rendered_text)
        self.assertIn(
            review_repair.review_repair_session_version_marker(
                review_repair.REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION
            ),
            shared.rendered_text,
        )
        self.assertEqual(
            ["OAK", "SVC"],
            [check["repo"] for check in shared.checks],
        )

        result = self._materialize()

        self.assertTrue(result.ok, result.error)
        self.assertEqual(("SVC", "OAK"), result.children[0].read_repositories)
        self.assertEqual(("SVC", "OAK"), result.children[0].write_repositories)
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        child_scope = plans.parse_session_scope(
            result.children[1].path.read_text(encoding="utf-8"), registry.aliases
        )
        self.assertEqual(plans.SessionScope(("SVC", "OAK"), ("SVC",)), child_scope)

    def _cross_repo_finding(
        self,
        finding_id: str,
        repair_group: str,
        repo: str,
        *,
        evidence_repos: tuple[str, ...],
        check_repos: tuple[str, ...],
    ) -> dict[str, Any]:
        finding = self._finding(
            finding_id,
            repair_group,
            evidence=[
                {"path": f"{alias}:pkg/{finding_id}.py", "symbol": "target"}
                for alias in evidence_repos
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}-{alias}",
                    "repo": alias,
                    "argv": ["python3", "-c", "pass"],
                }
                for alias in check_repos
            ],
        )
        finding["repo"] = repo
        return finding

    def test_registered_group_writes_union_evidence_and_check_repositories(
        self,
    ) -> None:
        # Shape of F002 in run 20260820T182939015261Z-25011: attributed to a
        # single alias while its evidence and checks span two.
        widened_finding = self._cross_repo_finding(
            "F1",
            "manifest-contract",
            "OAK",
            evidence_repos=("OAK", "SVC"),
            check_repos=("OAK", "SVC"),
        )
        narrow_finding = self._make_registered_finding("F2", "svc-only", "SVC")
        self._register_plan([widened_finding, narrow_finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        widened, narrow = repair_plan.repair_groups
        self.assertEqual(("SVC", "OAK"), widened.write_repositories)
        self.assertEqual(
            {"SVC": "evidence", "OAK": "repo"},
            widened.write_repository_sources,
        )
        self.assertIn("**Writes repositories**: SVC, OAK", widened.rendered_text)
        self.assertIn(
            "- Write scope: `SVC` (from finding evidence paths), "
            "`OAK` (from finding `repo`).",
            widened.rendered_text,
        )
        self.assertEqual(("SVC",), narrow.write_repositories)
        self.assertEqual({"SVC": "repo"}, narrow.write_repository_sources)
        self.assertIn(
            "- Write scope: `SVC` (from finding `repo`).",
            narrow.rendered_text,
        )

        result = self._materialize()

        self.assertTrue(result.ok, result.error)
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        self.assertEqual(
            plans.SessionScope(("SVC", "OAK"), ("SVC", "OAK")),
            plans.parse_session_scope(
                result.children[0].path.read_text(encoding="utf-8"), registry.aliases
            ),
        )

    def test_registered_group_write_scope_records_check_only_attribution(
        self,
    ) -> None:
        finding = self._cross_repo_finding(
            "F1",
            "shared",
            "SVC",
            evidence_repos=("SVC",),
            check_repos=("SVC", "OAK"),
        )
        self._register_plan([finding])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        (group,) = repair_plan.repair_groups
        self.assertEqual(("SVC", "OAK"), group.write_repositories)
        self.assertEqual(
            {"SVC": "repo", "OAK": "check"},
            group.write_repository_sources,
        )
        self.assertIn(
            "- Write scope: `SVC` (from finding `repo`), "
            "`OAK` (from finding verification checks).",
            group.rendered_text,
        )

    def test_write_repository_sources_helper_is_bounded_by_group_findings(
        self,
    ) -> None:
        self._register_plan([self._make_registered_finding("F1", "svc-only", "SVC")])
        registry = plans.read_repository_registry(self.plan_dir)
        assert registry is not None
        findings = [
            self._cross_repo_finding(
                "F1",
                "shared",
                "OAK",
                evidence_repos=("OAK",),
                check_repos=("OAK",),
            ),
            self._cross_repo_finding(
                "F2",
                "shared",
                "SVC",
                evidence_repos=("SVC",),
                check_repos=("SVC",),
            ),
        ]

        self.assertEqual(
            {"OAK": "repo"},
            review_repair.repair_group_write_repository_sources(
                "shared", findings[:1], repository_registry=registry
            ),
        )
        self.assertEqual(
            ["SVC", "OAK"],
            list(
                review_repair.repair_group_write_repository_sources(
                    "shared", findings, repository_registry=registry
                )
            ),
        )

    def test_registered_repair_fails_closed_for_unattributed_legacy_finding(
        self,
    ) -> None:
        finding = self._finding("F1", "runner")
        self._register_plan([finding], artifact_version=review.REVIEW_ARTIFACT_VERSION)
        before = self._plan_bytes()

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("without an explicit repository alias", repair_plan.error)
        self.assertEqual(before, self._plan_bytes())

    def test_registered_repair_qualifies_same_check_name_across_aliases(self) -> None:
        first = self._make_registered_finding("F1", "shared", "SVC")
        second = self._make_registered_finding("F2", "shared", "OAK")
        first["verification_checks"][0]["name"] = "shared-check"
        second["verification_checks"][0]["name"] = "shared-check"
        self._register_plan([first, second])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertEqual(
            ["SVC:shared-check", "OAK:shared-check"],
            [check["name"] for check in repair_plan.repair_groups[0].checks],
        )
        result = self._materialize()
        self.assertTrue(result.ok, result.error)
        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        assert contract is not None
        self.assertEqual(
            ["SVC:shared-check", "OAK:shared-check"],
            [check.name for check in contract.verify.checks],
        )

    def test_registered_repair_avoids_literal_qualified_name_collision(self) -> None:
        first = self._make_registered_finding("F1", "shared", "SVC")
        second = self._make_registered_finding("F2", "shared", "OAK")
        literal = self._make_registered_finding("F3", "shared", "SVC")
        first["verification_checks"][0].update(
            name="shared-check", argv=["python3", "-c", "pass # generated"]
        )
        second["verification_checks"][0]["name"] = "shared-check"
        literal["verification_checks"][0].update(
            name="SVC:shared-check",
            argv=["python3", "-c", "pass # literal"],
            on_failure_of=["shared-check"],
        )
        self._register_plan([first, second, literal])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        expected_names = [
            "SVC:shared-check#2",
            "OAK:shared-check",
            "SVC:shared-check",
        ]
        self.assertEqual(
            expected_names,
            [check["name"] for check in repair_plan.repair_groups[0].checks],
        )
        result = self._materialize()
        self.assertTrue(result.ok, result.error)
        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        assert contract is not None
        self.assertEqual(
            expected_names,
            [check.name for check in contract.verify.checks],
        )
        self.assertEqual(
            ("SVC:shared-check#2",),
            contract.verify.checks[2].on_failure_of,
        )

    def test_registered_repair_rejects_same_repo_check_name_conflict(self) -> None:
        first = self._make_registered_finding("F1", "shared", "SVC")
        second = self._make_registered_finding("F2", "shared", "SVC")
        first["verification_checks"][0]["name"] = "shared-check"
        second["verification_checks"][0]["name"] = "shared-check"
        second["verification_checks"][0]["argv"] = [
            "python3",
            "-c",
            "raise SystemExit(1)",
        ]
        self._register_plan([first, second])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("conflicting definitions", repair_plan.error)

    def test_registered_eventless_recovery_reads_bases_and_scopes_from_sessions(
        self,
    ) -> None:
        finding = self._make_registered_finding("F1", "runner", "SVC")
        self._register_plan([finding])
        review_path = self.plan_dir / "session-2.md"
        review_text = review_path.read_text(encoding="utf-8").replace(
            review.review_session_version_marker(),
            review.review_session_version_marker(
                review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
            ),
        )
        base_section = (
            "## Ordered repository review base\n\n"
            "- `SVC`: path `tundra_svc`; origin `github.com/tundra/SVC`; "
            "HEAD `1111111111111111111111111111111111111111`.\n"
            "  - Pre-existing dirty paths: (none)\n"
            "- `OAK`: path `tundra_OAK`; origin `github.com/tundra/OAK`; "
            "HEAD `2222222222222222222222222222222222222222`.\n"
            "  - Pre-existing dirty paths:\n"
            "    - `OAK:staged.txt`\n\n"
        )
        review_path.write_text(
            review_text.replace(
                "## Instructions\n", base_section + "## Instructions\n"
            ),
            encoding="utf-8",
        )

        planned = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(planned.ok, planned.error)
        self.assertEqual(
            ["SVC", "OAK"],
            [row["alias"] for row in planned.repository_bases],
        )
        self.assertEqual(["OAK:staged.txt"], planned.repository_bases[1]["dirty_paths"])
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            self.tmp_path / "missing-events.jsonl",
            max_review_fix_cycles=1,
        )

        self.assertEqual("run-repairs", resume.action)
        self.assertEqual(
            ["SVC", "OAK"],
            [row["alias"] for row in resume.current_repositories],
        )
        assert resume.repair_groups is not None
        self.assertEqual(["SVC"], resume.repair_groups[0]["write_repositories"])
        payload = resume.to_payload()
        self.assertEqual("SVC", payload["current_findings"][0]["repo"])
        self.assertEqual(
            ["OAK:staged.txt"],
            payload["current_repositories"][1]["dirty_paths"],
        )
        event_path = self.tmp_path / "events.jsonl"
        reopen = runner._review_reopen_record(
            event_path=event_path,
            payload={"run_id": "eventless"},
            resume=resume,
            max_review_fix_cycles=1,
            materialized=True,
            repair_session_ids=("2.1",),
            result=review_repair.REVIEW_REPAIR_REASON_OK,
            recovered=True,
        )
        events.append_event(
            event_path,
            events.Event(events.REVIEW_REPAIR_REOPENED_EVENT, reopen),
        )
        recovered = review_delta._last_repairs_materialized_cycle(event_path)
        assert recovered is not None
        self.assertEqual("SVC", recovered["findings"][0]["repo"])
        self.assertEqual(
            ["OAK:staged.txt"], recovered["repositories"][1]["dirty_paths"]
        )

    def test_narrowed_pending_repair_child_blocks_until_scope_is_regenerated(
        self,
    ) -> None:
        self._register_plan(
            [
                self._cross_repo_finding(
                    "F1",
                    "manifest-contract",
                    "OAK",
                    evidence_repos=("OAK", "SVC"),
                    check_repos=("OAK",),
                )
            ]
        )
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)
        child_path = materialized.children[0].path
        regenerated = child_path.read_text(encoding="utf-8")

        def resume() -> review_continuation.ReviewResume:
            return review_continuation.evaluate_review_continuation(
                plans.inspect_plan(self.plan_dir),
                self.tmp_path / "missing-events.jsonl",
                max_review_fix_cycles=1,
            )

        ready = resume()

        self.assertEqual("run-repairs", ready.action)
        self.assertTrue(ready.eligible)
        self.assertIsNone(ready.blocker)
        self.assertIsNone(ready.repair_scope_gaps)

        child_path.write_text(
            regenerated.replace(
                "**Writes repositories**: SVC, OAK",
                "**Writes repositories**: OAK",
            ),
            encoding="utf-8",
        )
        narrowed = resume()

        self.assertEqual("run-repairs", narrowed.action)
        self.assertFalse(narrowed.eligible)
        self.assertEqual("repair-scope-stale", narrowed.blocker)
        assert narrowed.repair_scope_gaps is not None
        (gap,) = narrowed.repair_scope_gaps
        self.assertEqual(
            {
                "repair_session_id": "2.1",
                "repair_group": "manifest-contract",
                "durable_write_repositories": ["OAK"],
                "required_write_repositories": ["SVC", "OAK"],
                "missing_write_repositories": ["SVC"],
                "write_repository_sources": {"SVC": "evidence", "OAK": "repo"},
            },
            dict(gap),
        )
        self.assertEqual(
            "widen **Writes repositories** in the pending repair children, "
            "then resume the same plan: 2.1 needs SVC",
            review_continuation.review_resume_guidance(
                narrowed.to_payload(), review_harness_active=True
            ),
        )

        child_path.write_text(regenerated, encoding="utf-8")
        cleared = resume()

        self.assertEqual("run-repairs", cleared.action)
        self.assertTrue(cleared.eligible)
        self.assertIsNone(cleared.blocker)
        self.assertIsNone(cleared.repair_scope_gaps)

    def test_dry_planning_accepts_exactly_eight_groups(self) -> None:
        self._write_plan([self._finding(f"F{i}", f"g{i}") for i in range(8)])

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(repair_plan.ok, repair_plan.error)
        self.assertEqual(8, len(repair_plan.repair_groups))
        self.assertEqual(
            [f"2.{index}" for index in range(1, 9)],
            [session_id.value for session_id in repair_plan.proposed_child_ids],
        )

    def test_dry_planning_rejects_nine_groups_without_writes(self) -> None:
        self._write_plan([self._finding(f"F{i}", f"g{i}") for i in range(9)])
        before = self._plan_bytes()

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_MALFORMED,
            repair_plan.reason,
        )
        self.assertIn("exceed the maximum of 8", repair_plan.error)
        self.assertEqual(9, len(repair_plan.proposed_child_ids))
        self.assertEqual(before, self._plan_bytes())

    def test_dry_planning_reports_conflicting_checks(self) -> None:
        self._write_plan(
            [
                self._finding(
                    "F1",
                    "runner",
                    verification_checks=[
                        {
                            "type": "command",
                            "name": "shared",
                            "argv": ["python3", "-c", "pass"],
                        }
                    ],
                ),
                self._finding(
                    "F2",
                    "runner",
                    verification_checks=[
                        {
                            "type": "command",
                            "name": "shared",
                            "argv": ["python3", "-c", "print(1)"],
                        }
                    ],
                ),
            ]
        )

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("conflicting", repair_plan.error)
        self.assertEqual(2, len(repair_plan.validated_findings))
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_dry_planning_reports_missing_and_inconsistent_artifacts(
        self,
    ) -> None:
        self._write_plan([self._finding("F1", "runner")])
        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_body = artifact_path.read_text(encoding="utf-8")
        artifact_path.unlink()

        missing = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(missing.ok)
        self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, missing.detail)
        self.assertIsNone(missing.review_artifact_sha256)
        artifact_path.write_text(artifact_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n",
            encoding="utf-8",
        )

        inconsistent = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(inconsistent.ok)
        self.assertEqual(
            review.REVIEW_REASON_CONTRADICTORY_ARTIFACT,
            inconsistent.detail,
        )
        self.assertIsNotNone(inconsistent.review_output_sha256)
        self.assertIsNotNone(inconsistent.review_artifact_sha256)

    def test_dry_planning_reports_existing_child_conflicts(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        materialized = self._materialize()
        self.assertTrue(materialized.ok)

        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertFalse(repair_plan.ok)
        self.assertIn("already exist", repair_plan.error)
        self.assertEqual(
            ("2.1",),
            tuple(session_id.value for session_id in repair_plan.existing_child_ids),
        )
        self.assertEqual(
            ("2.1",),
            tuple(session_id.value for session_id in repair_plan.proposed_child_ids),
        )

    def test_revised_grouping_changes_json_digest_not_fingerprints(self) -> None:
        original = self._finding("F1", "runner")
        self._write_plan([original])
        first = review_repair.plan_review_repair_sessions(self.plan_dir)
        revised = self._finding(
            "F1",
            "workbench",
            done_criteria=["F1 is fixed through a revised criterion."],
            verification_checks=[
                {
                    "type": "command",
                    "name": "verify-revised-F1",
                    "argv": ["python3", "-c", "print('revised')"],
                }
            ],
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [revised]}),
            encoding="utf-8",
        )

        second = review_repair.plan_review_repair_sessions(self.plan_dir)

        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(first.finding_fingerprints, second.finding_fingerprints)
        self.assertEqual(first.review_output_sha256, second.review_output_sha256)
        self.assertNotEqual(first.review_artifact_sha256, second.review_artifact_sha256)
        self.assertEqual("runner", first.repair_groups[0].repair_group)
        self.assertEqual("workbench", second.repair_groups[0].repair_group)

    def test_materializer_stages_prevalidated_planner_output(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        repair_plan = review_repair.plan_review_repair_sessions(self.plan_dir)
        self.assertTrue(repair_plan.ok, repair_plan.error)

        with (
            mock.patch.object(
                review_repair,
                "plan_review_repair_sessions",
                return_value=repair_plan,
            ),
            mock.patch.object(
                review_repair.authoring,
                "render_session_file",
                side_effect=AssertionError("writer re-rendered a planned child"),
            ),
        ):
            result = self._materialize()

        self.assertTrue(result.ok, result.error)
        child_text = result.children[0].path.read_text(encoding="utf-8")
        self.assertEqual(repair_plan.repair_groups[0].rendered_text, child_text)

    def test_materializes_one_child_per_repair_group(self) -> None:
        self._write_plan(
            [
                self._finding("F1", "runner"),
                self._finding("F2", "runner", severity="med"),
                self._finding("F3", "workbench", severity="low"),
            ]
        )
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, result.reason)
        self.assertEqual("2", result.review_session_id.value)
        self.assertEqual(
            ["2.1", "2.2"],
            [child.session_id.value for child in result.children],
        )
        self.assertEqual(
            ["runner", "workbench"],
            [child.repair_group for child in result.children],
        )
        self.assertEqual(("F1", "F2"), result.children[0].finding_ids)
        self.assertEqual(("F3",), result.children[1].finding_ids)
        for child in result.children:
            self.assertTrue(child.path.is_file())
            status = plans.read_session_status(
                plans.SessionFile(child.session_id, child.path)
            )
            self.assertEqual("pending", status.status)

    def test_child_contract_merges_checks_and_done_criteria(self) -> None:
        self._write_plan(
            [
                self._finding("F1", "runner"),
                self._finding("F2", "runner", severity="med"),
            ]
        )
        self._materialize()

        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        self.assertIsNotNone(contract)
        self.assertEqual(
            ["verify-F1"],
            [check.name for check in contract.verify.checks],
        )
        self.assertEqual(
            ("F1 is fixed and tested.", "F2 is fixed and tested."),
            contract.done_when,
        )

    def test_merged_diagnostic_retargets_deduplicated_gate_name(self) -> None:
        gate_one = {
            "type": "command",
            "name": "gate-one",
            "argv": ["python3", "-c", "pass"],
        }
        gate_two = {
            "type": "command",
            "name": "gate-two",
            "argv": ["python3", "-c", "pass"],
        }
        diagnostic = {
            "type": "file_exists",
            "name": "focused-evidence",
            "path": "focused.txt",
            "on_failure_of": ["gate-two"],
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[gate_one]),
                self._finding(
                    "F2",
                    "runner",
                    verification_checks=[gate_two, diagnostic],
                ),
            ]
        )

        result = self._materialize()

        self.assertTrue(result.ok, result.detail)
        contract = contracts.read_completion_contract(self.plan_dir, "2.1")
        self.assertIsNotNone(contract)
        self.assertEqual(
            ["gate-one", "focused-evidence"],
            [check.name for check in contract.verify.checks],
        )
        self.assertEqual(("gate-one",), contract.verify.checks[1].on_failure_of)

    def test_duplicate_commands_with_conflicting_settings_pause_malformed(
        self,
    ) -> None:
        first = {
            "type": "command",
            "name": "first",
            "argv": ["python3", "-c", "pass"],
            "timeout_seconds": 30,
        }
        second = {
            "type": "command",
            "name": "second",
            "argv": ["python3", "-c", "pass"],
            "timeout_seconds": 60,
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[first]),
                self._finding("F2", "runner", verification_checks=[second]),
            ]
        )

        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("conflicting settings", result.error)

    def test_duplicate_commands_with_conflicting_review_replay_fail_closed(
        self,
    ) -> None:
        first = {
            "type": "command",
            "name": "first",
            "argv": ["python3", "-c", "pass"],
            "review_replay": True,
        }
        second = {
            "type": "command",
            "name": "second",
            "argv": ["python3", "-c", "pass"],
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[first]),
                self._finding("F2", "runner", verification_checks=[second]),
            ]
        )

        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("conflicting settings", result.error)

    def test_merge_keeps_identical_commands_for_distinct_repositories(self) -> None:
        findings = [
            {
                "id": "F1",
                "verification_checks": [
                    {
                        "type": "command",
                        "name": "svc-check",
                        "repo": "SVC",
                        "argv": ["python3", "-m", "unittest"],
                    }
                ],
            },
            {
                "id": "F2",
                "verification_checks": [
                    {
                        "type": "command",
                        "name": "oak-check",
                        "repo": "OAK",
                        "argv": ["python3", "-m", "unittest"],
                    }
                ],
            },
        ]

        registry = workspaces.RepositoryRegistry(
            tuple(
                workspaces.Repository(alias, path, origin)
                for alias, path, origin in DEFAULT_MULTI_REPO_SPECS[:2]
            )
        )
        merged = review_repair._merge_verification_checks(
            "runner", findings, repository_registry=registry
        )

        self.assertEqual(["SVC", "OAK"], [check["repo"] for check in merged])
        self.assertEqual(2, len(merged))

    def test_merge_refuses_to_carry_an_absence_gate_into_a_repair_child(self) -> None:
        findings = [
            {
                "id": "F1",
                "verification_checks": [
                    {
                        "type": "regex_in_file",
                        "name": "drops-the-claim",
                        "path": "docs/evidence.md",
                        "pattern": "75-pixel",
                        "expect_absent": True,
                    }
                ],
            }
        ]

        with self.assertRaisesRegex(plans.PlanError, "no absence semantics"):
            review_repair._merge_verification_checks("runner", findings)

    def test_no_finding_is_omitted_or_assigned_twice(self) -> None:
        findings = [
            self._finding("F1", "a"),
            self._finding("F2", "b"),
            self._finding("F3", "a"),
            self._finding("F4", "c"),
        ]
        self._write_plan(findings)
        result = self._materialize()

        assigned = [fid for child in result.children for fid in child.finding_ids]
        self.assertEqual(sorted(assigned), ["F1", "F2", "F3", "F4"])
        self.assertEqual(len(assigned), len(set(assigned)))
        # First-appearance group ordering: a, b, c.
        self.assertEqual(
            ["a", "b", "c"],
            [child.repair_group for child in result.children],
        )
        self.assertEqual(("F1", "F3"), result.children[0].finding_ids)

    def test_children_carry_review_repair_marker(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        result = self._materialize()

        body = result.children[0].path.read_text(encoding="utf-8")
        self.assertTrue(review_repair.contains_review_repair_session_marker(body))
        self.assertEqual(1, review_repair.review_repair_session_marker_version(body))
        self.assertTrue(
            review_repair.is_generated_review_repair_session_file(
                result.children[0].path
            )
        )
        # A repair child must not masquerade as a generated review session.
        self.assertFalse(review.contains_review_session_marker(body))

    def test_children_chain_previous_session_references(self) -> None:
        self._write_plan([self._finding("F1", "a"), self._finding("F2", "b")])
        result = self._materialize()

        first = result.children[0].path.read_text(encoding="utf-8")
        second = result.children[1].path.read_text(encoding="utf-8")
        self.assertIn("`session-2.md`", first)
        self.assertIn("`session-2.1.md`", second)

    def test_manifest_table_and_count_refreshed(self) -> None:
        self._write_plan([self._finding("F1", "a"), self._finding("F2", "b")])
        self._materialize()

        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("| 2.1 | Review repair: a | pending | session-2.1.md |", manifest)
        self.assertIn("| 2.2 | Review repair: b | pending | session-2.2.md |", manifest)
        self.assertIn("**Sessions**: 4", manifest)

    def test_missing_manifest_table_still_materializes(self) -> None:
        self._write_plan([self._finding("F1", "a")], with_manifest_table=False)
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertTrue(result.children[0].path.is_file())
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Sessions**: 3", manifest)

    def test_auto_detects_generated_review_session(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        result = self._materialize()
        self.assertEqual("2", result.review_session_id.value)

    def test_explicit_review_session_id(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        result = self._materialize(review_session_id="2")
        self.assertTrue(result.ok)
        self.assertEqual("2", result.review_session_id.value)

    def test_over_eight_groups_pauses_malformed(self) -> None:
        findings = [self._finding(f"F{i}", f"g{i}") for i in range(9)]
        self._write_plan(findings)
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("exceed the maximum of 8", result.error)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_existing_children_pause_malformed(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        first = self._materialize()
        self.assertTrue(first.ok)
        original = first.children[0].path.read_text(encoding="utf-8")

        second = self._materialize()
        self.assertFalse(second.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, second.reason)
        self.assertIn("already exist", second.error)
        # Existing child left untouched.
        self.assertEqual(original, first.children[0].path.read_text(encoding="utf-8"))

    def test_missing_artifact_pauses_malformed(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        (self.plan_dir / "final-review.json").unlink()
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, result.detail)

    def test_conflicting_merged_check_names_pause_malformed(self) -> None:
        clash = {
            "type": "command",
            "name": "shared",
            "argv": ["python3", "-c", "pass"],
        }
        clash_other = {
            "type": "command",
            "name": "shared",
            "argv": ["python3", "-c", "print(1)"],
        }
        self._write_plan(
            [
                self._finding("F1", "runner", verification_checks=[clash]),
                self._finding("F2", "runner", verification_checks=[clash_other]),
            ]
        )
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("conflicting", result.error)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_all_clear_is_noop(self) -> None:
        self._write_plan([], outcome="all-clear")
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS, result.reason)
        self.assertEqual((), result.children)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))

    def test_commit_failure_rolls_back_atomically(self) -> None:
        self._write_plan([self._finding("F1", "a"), self._finding("F2", "b")])
        manifest_before = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        with mock.patch.object(
            review_repair.PlanTransaction,
            "commit",
            side_effect=plans.PlanError("boom"),
        ):
            result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))
        self.assertEqual(
            manifest_before,
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_without_cwd_leaves_children_draft(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        result = self._materialize()

        self.assertTrue(result.ok)
        self.assertFalse(result.loop_ready)
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: draft", manifest)

    def test_active_lock_pauses_malformed(self) -> None:
        self._write_plan([self._finding("F1", "a")])
        state.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state.release_lock, self.plan_dir, self.state_dir)
        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("lock", result.error)
        # Holding the lock externally, hold_lock=False still works.
        result_held = self._materialize(hold_lock=False)
        self.assertTrue(result_held.ok)

    def test_cwd_gate_marks_plan_loop_ready_with_absolute_repair_path(self) -> None:
        repo = self.tmp_path / "repo"
        init_git_repo(repo)
        (repo / "README.md").write_text("readme\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=a@b.c",
                "-c",
                "user.name=x",
                "commit",
                "-q",
                "-m",
                "readme",
            ],
            check=True,
        )
        contract_one = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
            session="1",
        )
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            evidence=[{"path": "README.md"}],
            done_criteria=["README stays present."],
            verification_checks=[
                {
                    "type": "file_exists",
                    "name": "readme-present",
                    "path": str(repo / "README.md"),
                }
            ],
        )
        self._write_plan([finding], session_one_contract=contract_one)
        progress_repair.repair_progress_journal(self.plan_dir)

        result = self._materialize(cwd=repo)

        self.assertTrue(result.ok, msg=str(result.blockers))
        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, result.reason)
        manifest = (self.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)

    def _committed_git_repo(self, name: str) -> Path:
        repo = self.tmp_path / name
        init_git_repo(repo)
        (repo / "README.md").write_text("readme\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=a@b.c",
                "-c",
                "user.name=x",
                "commit",
                "-q",
                "-m",
                "readme",
            ],
            check=True,
        )
        return repo

    def test_child_escaping_bound_root_is_refused_before_the_plan_is_written(
        self,
    ) -> None:
        repo = self._committed_git_repo("repo")
        outside = self.tmp_path / "outside"
        init_git_repo(outside)
        (outside / "pkg").mkdir(parents=True, exist_ok=True)
        (outside / "pkg" / "target.py").write_text("x = 1\n", encoding="utf-8")
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="outside",
            evidence=[{"path": "README.md"}],
            done_criteria=["The outside module is fixed."],
            verification_checks=[
                {
                    "type": "file_exists",
                    "name": "outside-present",
                    "path": str(outside / "pkg" / "target.py"),
                }
            ],
        )
        self._write_plan([finding])
        before = self._plan_bytes()

        result = self._materialize(cwd=repo)

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual((), result.children)
        self.assertFalse(result.loop_ready)
        # Both pre-write rules refuse this child: its target escapes the bound
        # root and its check cannot resolve there either.
        self.assertEqual(
            ("repository-footprint", "repository-contract"),
            result.non_bypassable_blockers,
        )
        self.assertTrue(
            any("escapes the bound root" in blocker for blocker in result.blockers),
            msg=str(result.blockers),
        )
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

    def test_stale_progress_journal_is_repaired_rather_than_blocking_the_gate(
        self,
    ) -> None:
        repo = self._committed_git_repo("repo")
        contract_one = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
            session="1",
        )
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            evidence=[{"path": "README.md"}],
            done_criteria=["README stays present."],
            verification_checks=[
                {
                    "type": "file_exists",
                    "name": "readme-present",
                    "path": str(repo / "README.md"),
                }
            ],
        )
        self._write_plan([finding], session_one_contract=contract_one)
        journal = plans.progress_path(self.plan_dir)
        self.assertFalse(journal.exists())

        result = self._materialize(cwd=repo)

        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_OK, result.reason)
        self.assertTrue(journal.exists())
        self.assertIn(
            "**Readiness**: loop-ready",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_reopened_plan_is_regated_after_repairs_grow_the_session_inventory(
        self,
    ) -> None:
        repo = self._committed_git_repo("repo")
        contract_one = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
            session="1",
        )
        findings = [
            _structured_review_finding(
                finding_id=finding_id,
                repair_group=repair_group,
                evidence=[{"path": "README.md"}],
                done_criteria=["README stays present."],
                verification_checks=[
                    {
                        "type": "file_exists",
                        "name": f"readme-present-{finding_id}",
                        "path": str(repo / "README.md"),
                    }
                ],
            )
            for finding_id, repair_group in (("F1", "docs"), ("F2", "runner"))
        ]
        self._write_plan(findings, session_one_contract=contract_one)

        authorized = readiness_gate._reopened_plan_gate_payload(
            plans.inspect_plan(self.plan_dir),
            max_pending_sessions=1,
            repair_session_ids=[],
        )
        self.assertTrue(authorized["ok"])
        self.assertEqual(0, authorized["pending_sessions"])
        self.assertEqual(
            "all sessions complete; no turns required",
            authorized["findings"][0]["message"],
        )

        result = self._materialize(cwd=repo)
        self.assertTrue(result.ok, msg=str(result.blockers))
        repair_session_ids = [child.session_id.value for child in result.children]
        self.assertEqual(["2.1", "2.2"], repair_session_ids)

        regated = readiness_gate._reopened_plan_gate_payload(
            plans.inspect_plan(self.plan_dir),
            max_pending_sessions=1,
            repair_session_ids=repair_session_ids,
        )
        self.assertFalse(regated["ok"])
        self.assertEqual(2, regated["pending_sessions"])
        self.assertEqual(4, regated["total_sessions"])
        self.assertEqual(repair_session_ids, regated["repair_session_ids"])
        self.assertEqual("env-turn-budget", regated["findings"][0]["category"])
        # Unstamped, the gate is the repairs one. The review-session append is
        # the other growth path and has to be distinguishable from this.
        self.assertEqual(events.REVIEW_REPAIR_REOPENED_EVENT, regated["trigger"])

    def test_the_two_growth_paths_stamp_distinguishable_triggers(self) -> None:
        # Same verdict shape from both routes, so the trigger is the only thing
        # that tells an audit which growth a gate authorized.
        self._write_plan([self._finding("F1", "runner")])
        snapshot = plans.inspect_plan(self.plan_dir)

        repairs = readiness_gate._reopened_plan_gate_payload(
            snapshot, max_pending_sessions=1, repair_session_ids=["2.1"]
        )
        review_append = readiness_gate._reopened_plan_gate_payload(
            snapshot,
            max_pending_sessions=1,
            repair_session_ids=[],
            trigger=events.REVIEW_SESSION_CREATED_EVENT,
        )

        self.assertEqual(events.REVIEW_REPAIR_REOPENED_EVENT, repairs["trigger"])
        self.assertEqual(events.REVIEW_SESSION_CREATED_EVENT, review_append["trigger"])
        self.assertNotEqual(repairs["trigger"], review_append["trigger"])
        self.assertEqual(repairs["total_sessions"], review_append["total_sessions"])

    def test_continuation_projects_disabled_and_exhausted_cap_boundaries(
        self,
    ) -> None:
        finding = self._finding("F1", "runner")
        self._write_plan([finding])
        fingerprint = review.finding_fingerprint(finding)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "cycles-disabled",
                "fingerprints": [fingerprint],
                "repair_session_ids": [],
                "max_review_fix_cycles": 0,
            }
        )

        disabled = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("materialize-repairs", disabled.action)
        self.assertFalse(disabled.eligible)
        self.assertEqual("higher-cap-required", disabled.blocker)
        self.assertEqual(1, disabled.minimum_required_cap)
        self.assertEqual((fingerprint,), disabled.source_fingerprints)
        self.assertIsNone(disabled.source_review_output_sha256)
        self.assertIsNotNone(disabled.current_review_output_sha256)
        self.assertIsNotNone(disabled.current_review_artifact_sha256)

        resumed = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )
        self.assertTrue(resumed.eligible)
        self.assertIsNone(resumed.blocker)

        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "1",
                "disposition": "repairs-materialized",
                "fingerprints": ["finding:old"],
                "repair_session_ids": ["1.1"],
                "max_review_fix_cycles": 1,
            },
            {
                "cycle": 2,
                "review_session_id": "2",
                "disposition": "cycle-cap-exhausted",
                "fingerprints": [fingerprint],
                "repair_session_ids": [],
                "max_review_fix_cycles": 1,
            },
        )
        exhausted = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual(1, exhausted.historical_materializations)
        self.assertEqual(2, exhausted.minimum_required_cap)
        self.assertEqual("higher-cap-required", exhausted.blocker)

    def test_continuation_rechecks_malformed_artifact_after_correction(
        self,
    ) -> None:
        findings = [self._finding(f"F{i}", f"g{i}") for i in range(9)]
        self._write_plan(findings)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "malformed-review-repair",
                "fingerprints": [
                    review.finding_fingerprint(finding) for finding in findings
                ],
                "repair_session_ids": [],
                "max_review_fix_cycles": 1,
            }
        )

        malformed = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertFalse(malformed.eligible)
        self.assertEqual("artifact-invalid", malformed.blocker)
        self.assertFalse(malformed.artifact_valid)
        self.assertIn("exceed the maximum of 8", malformed.artifact_error or "")

        findings[-1]["repair_group"] = "g7"
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
            encoding="utf-8",
        )
        corrected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertTrue(corrected.artifact_valid)
        self.assertTrue(corrected.eligible)
        self.assertEqual("materialize-repairs", corrected.action)
        self.assertIsNone(corrected.blocker)

    def test_continuation_marks_manually_changed_all_clear_invalid(
        self,
    ) -> None:
        self._write_plan([], outcome="all-clear")
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "cycles-disabled",
                "fingerprints": ["finding:original"],
                "repair_session_ids": [],
                "max_review_fix_cycles": 0,
            }
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )

        self.assertEqual("materialize-repairs", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("artifact-invalid", projected.blocker)
        self.assertIs(projected.artifact_valid, False)
        self.assertEqual(
            "the completed findings review no longer has repair findings; "
            "a manually changed all-clear artifact cannot finalize it",
            projected.artifact_error,
        )
        self.assertIsNotNone(projected.current_review_output_sha256)
        self.assertIsNotNone(projected.current_review_artifact_sha256)

    def test_continuation_reconciles_not_loop_ready_and_crash_lineage(
        self,
    ) -> None:
        self._write_plan([self._finding("F1", "runner")])
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)
        child_path = materialized.children[0].path
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "fingerprints": ["finding:legacy"],
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )

        pending = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual("run-repairs", pending.action)
        # The children exist and look runnable, but the gate that refused them
        # has not been cleared, so they are not eligible yet.
        self.assertFalse(pending.eligible)
        self.assertEqual("not-loop-ready", pending.blocker)
        self.assertEqual(1, pending.historical_materializations)
        self.assertEqual(("2.1",), pending.pending_repair_session_ids)
        self.assertIsNone(pending.artifact_valid)
        self.assertIsNone(pending.artifact_error)
        self.assertEqual(
            1, review_delta._review_cycle_state(event_path)["repairs_materialized"]
        )

        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_cycle": 1,
                    "source_review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                    "materialized": True,
                },
            ),
        )
        deduped = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual(1, deduped.historical_materializations)
        self.assertEqual(
            1, review_delta._review_cycle_state(event_path)["repairs_materialized"]
        )

        event_path.unlink()
        crashed = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual("run-repairs", crashed.action)
        self.assertEqual(1, crashed.historical_materializations)

        child_path.write_text(
            child_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        rereview = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertEqual("rereview", rereview.action)
        self.assertTrue(rereview.eligible)
        self.assertIsNone(rereview.artifact_valid)
        self.assertIsNone(rereview.artifact_error)

    def test_clearing_the_refusal_makes_the_repairs_eligible_again(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-08-17T00:00:00Z",
            ),
        )

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("run-repairs", resume.action)
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def _pending_repairs_ready_to_run(self) -> Path:
        """A plan whose pending repair children the continuation calls eligible."""
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-08-17T00:00:00Z",
            ),
        )
        baseline = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )
        self.assertTrue(baseline.eligible, baseline.blocker)
        return event_path

    def _append_run(self, event_path: Path, run_id: str, *, paused: str | None) -> None:
        """Record a run that opened and, optionally, paused the way runs do."""
        events.append_event(
            event_path, events.Event(events.RUN_STARTED_EVENT, {"run_id": run_id})
        )
        if paused is not None:
            events.append_event(
                event_path,
                events.Event(
                    events.RUN_PAUSED_EVENT, {"run_id": run_id, "reason": paused}
                ),
            )

    def test_a_standing_workspace_pause_blocks_the_pending_repairs(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        eligible_log = event_path.read_text(encoding="utf-8")
        for reason in pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS:
            with self.subTest(reason=reason):
                event_path.write_text(eligible_log, encoding="utf-8")
                self._append_run(event_path, "run-1", paused=reason)

                resume = review_continuation.evaluate_review_continuation(
                    plans.inspect_plan(self.plan_dir), event_path
                )

                # The children are on disk and runnable, but the precondition the
                # run stopped on is still true, so the same state.json cannot
                # honestly call them eligible while it reports the pause.
                self.assertEqual("run-repairs", resume.action)
                self.assertFalse(resume.eligible)
                self.assertEqual(reason, resume.blocker)
                self.assertEqual(("2.1",), resume.pending_repair_session_ids)

    def test_a_later_run_start_clears_the_standing_pause_blocker(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        self._append_run(event_path, "run-1", paused="commit-preflight")
        self._append_run(event_path, "run-2", paused=None)

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        # Rerunning the plan after fixing the workspace is the whole remedy, and
        # it is also what keeps the blocker inert for a run still in flight.
        self.assertEqual("run-repairs", resume.action)
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def test_finishing_the_paused_run_leaves_the_blocker_standing(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        self._append_run(event_path, "run-1", paused="commit-preflight")
        events.append_event(
            event_path,
            events.Event(events.RUN_FINISHED_EVENT, {"run_id": "run-1", "ok": False}),
        )

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        # The runner closes paused runs with run_finished too, so treating that
        # record as the all-clear would make the blocker unreachable in practice.
        self.assertFalse(resume.eligible)
        self.assertEqual("commit-preflight", resume.blocker)

    def test_a_pause_an_answer_can_clear_does_not_block_the_repairs(self) -> None:
        event_path = self._pending_repairs_ready_to_run()
        self._append_run(event_path, "run-1", paused="needs-decision")
        self.assertEqual(
            pause_policy.USER_ACTION_REPLY,
            pause_policy.classify_pause("needs-decision")["user_action"],
        )

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        # A pause waiting on a mailbox answer says nothing about whether the
        # workspace can host the next run, so it is not this blocker's business.
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def test_a_stale_loop_ready_stamp_does_not_clear_the_refusal(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="0" * 64,
                checked_at="2026-08-17T00:00:00Z",
            ),
        )

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        self.assertFalse(resume.eligible)
        self.assertEqual("not-loop-ready", resume.blocker)

    def test_materialized_repairs_stay_eligible_without_a_refusal(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "repairs-materialized",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )

        resume = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("run-repairs", resume.action)
        self.assertTrue(resume.eligible)
        self.assertIsNone(resume.blocker)

    def test_continuation_projects_group_exhausted_from_run_config(
        self,
    ) -> None:
        first = self._finding("F1", "runner")
        second = self._finding(
            "F2",
            "runner",
            title="Fix the rerouted runner failure",
            evidence=[{"path": "pkg/F2.py", "symbol": "retry_loop"}],
            explanation="F2 is still broken in a different way.",
            done_criteria=["F2 is fixed and tested."],
        )
        self._write_plan([first])
        materialized = self._materialize()
        self.assertTrue(materialized.ok, materialized.error)
        child_path = materialized.children[0].path
        child_path.write_text(
            child_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        review_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-3.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "# Review\n\n## Findings\n\n"
            "See pkg/F2.py and README.md.\n\n"
            f"{self._status_line([second])}\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [second]}),
            encoding="utf-8",
        )
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                "fingerprints": [review.finding_fingerprint(first)],
                "repair_session_ids": ["2.1"],
                "repair_groups": [
                    {
                        "repair_group": "runner",
                        "repair_session_id": "2.1",
                        "finding_ids": ["F1"],
                    }
                ],
                "max_review_fix_cycles": 4,
            }
        )
        event_path.with_name(events.STATE_FILE_NAME).write_text(
            json.dumps({"run_config": {"max_repair_group_attempts": 1}}),
            encoding="utf-8",
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
            projected.blocker,
        )
        self.assertEqual(1, projected.max_repair_group_attempts)
        self.assertEqual({"runner": 1}, projected.repair_group_attempts)
        self.assertEqual(
            (review.finding_fingerprint(second),),
            projected.current_fingerprints,
        )
        self.assertEqual((), projected.repair_session_ids)
        payload = projected.to_payload()
        self.assertEqual(1, payload["max_repair_group_attempts"])
        self.assertEqual({"runner": 1}, payload["repair_group_attempts"])
        self.assertEqual(
            (
                'the recurring repair group "runner" already hit 1 attempt; '
                "fix it manually or re-split instead of raising "
                "--max-review-fix-cycles"
            ),
            review_continuation.review_resume_guidance(
                payload, review_harness_active=False
            ),
        )

    def test_review_resume_guidance_avoids_higher_cap_for_exhausted_group(
        self,
    ) -> None:
        resume = review_continuation.ReviewResume(
            source_cycle=2,
            review_session_id="2",
            source_disposition=runner.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            action="materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            historical_materializations=2,
            minimum_required_cap=3,
            selected_cap=2,
            max_repair_group_attempts=2,
            repair_group_attempts={"runner": 2},
            repair_groups=(
                {
                    "repair_group": "runner",
                    "repair_session_id": "2.1",
                    "finding_ids": ["F1"],
                },
            ),
        )

        guidance = review_continuation.review_resume_guidance(
            resume.to_payload(), review_harness_active=False
        )

        self.assertIn('repair group "runner"', guidance or "")
        self.assertIn("2 attempts", guidance or "")
        self.assertIn("fix it manually or re-split", guidance or "")
        self.assertNotIn("or higher", guidance or "")

    def test_continuation_skips_repair_planning_for_terminal_states(self) -> None:
        self._write_plan([self._finding("F1", "runner")])
        for disposition, repair_ids, blocker, materializations in (
            ("all-clear", [], "all-clear", 0),
            ("findings-repeated", [], "findings-repeated", 0),
            ("unstructured-findings", [], "unstructured-findings", 0),
            ("repairs-materialized", [], "repair-sessions-missing", 1),
            (
                "repairs-materialized",
                ["2.1"],
                "repair-sessions-missing",
                1,
            ),
            ("future-disposition", [], "unknown-disposition", 0),
        ):
            with self.subTest(disposition=disposition, repair_ids=repair_ids):
                event_path = self._write_review_events(
                    {
                        "cycle": 1,
                        "review_session_id": "2",
                        "disposition": disposition,
                        "fingerprints": [],
                        "repair_session_ids": repair_ids,
                        "max_review_fix_cycles": 8,
                    }
                )
                with mock.patch.object(
                    review_repair,
                    "plan_review_repair_sessions",
                    side_effect=AssertionError(
                        "terminal continuation must not plan repair children"
                    ),
                ):
                    terminal = review_continuation.evaluate_review_continuation(
                        plans.inspect_plan(self.plan_dir), event_path
                    )
                self.assertEqual("terminal", terminal.action)
                self.assertFalse(terminal.eligible)
                self.assertEqual(blocker, terminal.blocker)
                self.assertEqual(disposition, terminal.source_disposition)
                self.assertEqual(materializations, terminal.historical_materializations)
                self.assertEqual(materializations + 1, terminal.minimum_required_cap)
                self.assertIsNone(terminal.current_review_output_sha256)
                self.assertIsNone(terminal.current_review_artifact_sha256)


class _SimulatedCrash(BaseException):
    """Stands in for a process death: nothing in-process gets to clean up."""


def _committed_repo(path: Path) -> Path:
    """Initialize a Git repository holding one committed file."""
    init_git_repo(path)
    (path / "README.md").write_text("readme\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=x",
            "commit",
            "-q",
            "-m",
            "readme",
        ],
        check=True,
    )
    return path


def _write_repair_ready_plan(
    plan_dir: Path,
    repo: Path,
    *,
    findings: list[dict[str, Any]] | None = None,
    specs: tuple[tuple[str, str, str], ...] | None = None,
) -> None:
    """Write a completed two-session plan whose review carries repair findings.

    The plan is loop-ready material: session 1 is complete with a contract that
    passes against ``repo``, and session 2 is a completed generated review whose
    artifact holds ``findings``. Passing ``specs`` registers those repositories,
    which is what makes the plan take the workspace-resolved verification path.
    """
    registered = specs is not None
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")

    manifest = authoring.render_manifest(
        authoring.ManifestSpec(
            plan_title="Test Plan",
            source_path="/src/plan.md",
            output_dir=str(plan_dir),
            plan_id="pid",
            source_fingerprint="fp",
            content_sha256="csha",
            sessions=(
                authoring.ManifestSession(
                    plans.parse_session_id("1"), "One", "complete"
                ),
                authoring.ManifestSession(
                    plans.parse_session_id("2"), "Review", "complete"
                ),
            ),
            split_date="2026-01-01",
            readiness=plans.ReadinessMetadata("draft", "sha", "2026-01-01T00:00:00Z"),
        )
    )
    if registered:
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
        )
        manifest += (
            "\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n"
        )
    (plan_dir / "manifest.md").write_text(manifest, encoding="utf-8")

    aliases = (
        ", ".join(alias for alias, _path, _origin in specs) if registered else None
    )
    check_payload = (
        '{"type": "file_exists", "path": "README.md", "name": "readme-1"'
        + (f', "repo": "{specs[0][0]}"' if registered else "")
        + "}"
    )
    session_one = ["# Session 1: One", ""]
    if registered:
        session_one.extend(
            [
                f"**Reads repositories**: {aliases}",
                f"**Writes repositories**: {aliases}",
                "",
            ]
        )
    session_one.extend(
        [
            "## Instructions",
            "",
            "Do the first thing.",
            "",
            _contract_block(check_payload, session="1"),
            "",
            "## Handoff",
            "",
            "- **Status**: complete",
            "- **Files modified**: (none)",
            "- **Key decisions**: (none)",
            "- **Blockers for next session**: (none)",
            "- **Notes**:",
            "",
        ]
    )
    (plan_dir / "session-1.md").write_text("\n".join(session_one), encoding="utf-8")

    review_body = review_session_body.build_generated_review_session_body(
        plans.parse_session_id("2"),
        review_harness="codex",
        plan_dir=plan_dir,
    ).replace("- **Status**: pending", "- **Status**: complete")
    if registered:
        heading, remainder = review_body.split("\n", 1)
        review_body = (
            f"{heading}\n\n**Reads repositories**: {aliases}\n"
            f"**Writes repositories**: (none)\n{remainder}"
        ).replace("- **Files modified**: (none yet)", "- **Files modified**: (none)")
    (plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")

    if findings is None:
        findings = [
            _structured_review_finding(
                finding_id="F1",
                repair_group="docs",
                evidence=[{"path": "README.md"}],
                done_criteria=["README stays present."],
                verification_checks=[
                    {
                        "type": "file_exists",
                        "name": "readme-present",
                        "path": str(repo / "README.md"),
                    }
                ],
            )
        ]
    (plan_dir / "final-review.md").write_text(
        "# Review\n\n## Findings\n\nSee README.md.\n\n"
        f"Status: {len(findings)} Findings ({len(findings)} high, 0 med, 0 low)\n",
        encoding="utf-8",
    )
    (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
        json.dumps(
            {
                "version": (
                    review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION if registered else 2
                ),
                "outcome": "findings",
                "findings": findings,
            }
        ),
        encoding="utf-8",
    )


class ReviewRepairPublicationWindowTests(unittest.TestCase):
    """ASI-136: repair-child publication is atomic and readiness-gated.

    Materialization writes children, refreshes readiness, and only then runs the
    baseline + preflight gate that makes them executable. Every one of those
    boundaries is a place a refusal or a crash can strand a pending child no run
    can select and no run can clear, so each is exercised here. The outcome is
    always the plan the publication started from or a complete, internally
    consistent plan with children in it; the latter is loop-ready unless the
    operator explicitly accepts only bypassable readiness findings.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo = _committed_repo(self.tmp_path / "repo")
        _write_repair_ready_plan(self.plan_dir, self.repo)
        progress_repair.repair_progress_journal(self.plan_dir)

    def _materialize(self, **kwargs: Any) -> review_repair.ReviewRepairResult:
        return review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, cwd=self.repo, **kwargs
        )

    def _plan_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in self.plan_dir.rglob("*")
            if path.is_file()
        }

    def _window_dir(self) -> Path:
        return self.plan_dir / review_repair.REPAIR_PUBLICATION_DIR_NAME

    def _blocked_gate(self, *, non_bypassable: list[str]) -> dict[str, Any]:
        return {
            "loop_ready": False,
            "blockers": ["repository-footprint: injected refusal"],
            "non_bypassable_blockers": non_bypassable,
            "baseline_summary": {"ok": False},
        }

    def test_the_happy_path_publishes_children_and_retires_the_window(self) -> None:
        result = self._materialize()

        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertFalse(result.rolled_back)
        self.assertIsNone(result.rollback)
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())
        self.assertIn(
            "**Readiness**: loop-ready",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_a_blocked_loop_ready_gate_withdraws_the_children_it_authored(
        self,
    ) -> None:
        before = self._plan_bytes()

        with mock.patch.object(
            review_repair,
            "_make_children_executable",
            return_value=self._blocked_gate(non_bypassable=[]),
        ):
            result = self._materialize()

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual((), result.children)
        self.assertTrue(result.rolled_back)
        assert result.rollback is not None
        self.assertEqual([], result.rollback["errors"])
        self.assertIn("session-2.1.md", result.rollback["removed"])
        self.assertIn("withdrawn after authoring", result.error or "")
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())

    def test_allow_not_loop_ready_still_retains_bypassable_children(self) -> None:
        with mock.patch.object(
            review_repair,
            "_make_children_executable",
            return_value=self._blocked_gate(non_bypassable=[]),
        ):
            result = self._materialize(allow_not_loop_ready=True)

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertFalse(result.loop_ready)
        self.assertFalse(result.rolled_back)
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())

    def test_a_non_bypassable_blocker_withdraws_children_even_when_allowed(
        self,
    ) -> None:
        before = self._plan_bytes()

        with mock.patch.object(
            review_repair,
            "_make_children_executable",
            return_value=self._blocked_gate(non_bypassable=["repository-footprint"]),
        ):
            result = self._materialize(allow_not_loop_ready=True)

        self.assertEqual((), result.children)
        self.assertTrue(result.rolled_back)
        self.assertEqual(before, self._plan_bytes())

    def test_a_failed_commit_leaves_the_plan_and_retires_the_window(self) -> None:
        before = self._plan_bytes()

        with mock.patch.object(
            review_repair.PlanTransaction,
            "commit",
            side_effect=plans.PlanError("injected commit failure"),
        ):
            result = self._materialize()

        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertFalse(result.ok)
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_an_interrupt_between_authoring_and_the_gate_rolls_back(self) -> None:
        before = self._plan_bytes()

        with (
            mock.patch.object(
                review_repair.authoring,
                "refresh_readiness_metadata",
                side_effect=KeyboardInterrupt(),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._materialize()

        self.assertEqual(before, self._plan_bytes())
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())

    def test_an_interrupt_inside_the_gate_rolls_back(self) -> None:
        before = self._plan_bytes()

        with (
            mock.patch.object(
                review_repair,
                "_make_children_executable",
                side_effect=KeyboardInterrupt(),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._materialize()

        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def _crash_mid_publication(self, phase: str) -> dict[str, bytes]:
        """Die mid-publication: no in-process rollback, window left on disk."""
        before = self._plan_bytes()
        target = {
            "authoring": (review_repair.authoring, "refresh_readiness_metadata"),
            "validating": (review_repair, "_make_children_executable"),
        }[phase]
        with (
            mock.patch.object(
                review_repair, "_rollback_publication_window", return_value={}
            ),
            mock.patch.object(*target, side_effect=_SimulatedCrash()),
            self.assertRaises(_SimulatedCrash),
        ):
            self._materialize()
        self.assertTrue(self._window_dir().is_dir())
        return before

    def test_a_crash_after_authoring_is_rolled_back_by_the_next_locked_caller(
        self,
    ) -> None:
        before = self._crash_mid_publication("authoring")
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("rolled-back", record["reason"])
        self.assertEqual([], record["errors"])
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_a_crash_inside_the_gate_is_rolled_back_by_the_next_locked_caller(
        self,
    ) -> None:
        before = self._crash_mid_publication("validating")

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("validating", record["phase"])
        self.assertEqual(before, self._plan_bytes())

    def test_a_crash_in_the_publishing_phase_is_rolled_back(
        self,
    ) -> None:
        before = self._plan_bytes()
        advance_phase = review_repair._advance_publication_phase

        def crash_after_publishing_marker(
            window: review_repair._PublicationWindow, phase: str
        ) -> None:
            advance_phase(window, phase)
            if phase == review_repair.PUBLICATION_PHASE_PUBLISHING:
                raise _SimulatedCrash()

        with (
            mock.patch.object(
                review_repair,
                "_advance_publication_phase",
                side_effect=crash_after_publishing_marker,
            ),
            self.assertRaises(_SimulatedCrash),
        ):
            self._materialize()

        self.assertTrue(self._window_dir().is_dir())
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertIn(
            "**Readiness**: loop-ready",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("publishing", record["phase"])
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_a_crashed_publication_is_recovered_by_the_next_materialization(
        self,
    ) -> None:
        before = self._crash_mid_publication("validating")

        result = self._materialize()

        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertNotEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_recovery_is_idempotent(self) -> None:
        before = self._crash_mid_publication("validating")

        first = review_repair.recover_review_repair_publication(self.plan_dir)
        second = review_repair.recover_review_repair_publication(self.plan_dir)

        assert first is not None
        self.assertTrue(first["recovered"])
        self.assertIsNone(second)
        self.assertEqual(before, self._plan_bytes())

    def test_nothing_to_recover_reports_nothing(self) -> None:
        self.assertIsNone(
            review_repair.recover_review_repair_publication(self.plan_dir)
        )

    def test_a_window_without_a_journal_never_opened_and_is_discarded(self) -> None:
        before = self._plan_bytes()
        self._window_dir().mkdir(parents=True)

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("unopened-publication-window", record["reason"])
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_an_unsupported_publication_schema_fails_closed(self) -> None:
        window = self._window_dir()
        window.mkdir(parents=True)
        (window / review_repair.REPAIR_PUBLICATION_JOURNAL_NAME).write_text(
            json.dumps({"version": 999, "entries": []}), encoding="utf-8"
        )

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertFalse(record["recovered"])
        self.assertEqual("unsupported-publication-schema", record["reason"])
        self.assertTrue(window.is_dir())

        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertIn("could not be rolled back", result.error or "")
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertTrue(window.is_dir())


class ReviewRepairContractGateTests(unittest.TestCase):
    """ASI-136: a check the contract cannot resolve is refused before authoring.

    The reported shape is a repair check whose working directory is invalid
    under the plan's repository contract. Preflight rejects it with a red
    ``repository-contract`` finding — but that ran only after the child was on
    disk, so the plan kept a pending repair child no run could select and no
    run could clear. The same resolution now runs before anything is written.

    A registered plan is already covered upstream: the review-artifact grammar
    validates every check against the registry, so an unresolvable ``repo``,
    ``cwd``, or ``path`` is refused as a malformed verdict. The gap is the
    legacy (unregistered) plan, whose artifact grammar has no registry to
    check against and whose footprint gate judges targets rather than checks.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo = _committed_repo(self.tmp_path / "repo")
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"

    def _write_plan_with_check(self, check: dict[str, Any]) -> None:
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            evidence=[{"path": "README.md"}],
            done_criteria=["README stays present."],
            verification_checks=[check],
        )
        _write_repair_ready_plan(self.plan_dir, self.repo, findings=[finding])
        progress_repair.repair_progress_journal(self.plan_dir)

    def _plan_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in self.plan_dir.rglob("*")
            if path.is_file()
        }

    def _materialize(self) -> review_repair.ReviewRepairResult:
        return review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, cwd=self.repo
        )

    def test_an_unresolvable_check_cwd_is_refused_before_authoring(self) -> None:
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "escaping",
                "cwd": "..",
                "argv": ["python3", "-c", "pass"],
            }
        )
        before = self._plan_bytes()

        result = self._materialize()

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual((), result.children)
        self.assertEqual(("repository-contract",), result.non_bypassable_blockers)
        self.assertTrue(
            any(
                "verification check 'escaping' that cannot resolve" in blocker
                for blocker in result.blockers
            ),
            msg=str(result.blockers),
        )
        self.assertFalse(result.rolled_back)
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(
            (self.plan_dir / review_repair.REPAIR_PUBLICATION_DIR_NAME).exists()
        )

    def test_the_refusal_is_non_bypassable(self) -> None:
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "escaping",
                "cwd": "nested/../../elsewhere",
                "argv": ["python3", "-c", "pass"],
            }
        )

        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            state_dir=self.state_dir,
            cwd=self.repo,
            allow_not_loop_ready=True,
        )

        self.assertEqual((), result.children)
        self.assertEqual(("repository-contract",), result.non_bypassable_blockers)
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

    def test_without_the_gate_the_same_plan_strands_a_pending_child(self) -> None:
        """The pre-fix path, pinned so the regression cannot come back quietly."""
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "escaping",
                "cwd": "..",
                "argv": ["python3", "-c", "pass"],
            }
        )

        with (
            mock.patch.object(
                review_repair, "_validate_children_contract", return_value=[]
            ),
            mock.patch.object(
                review_repair,
                "_rollback_publication_window",
                return_value={"errors": []},
            ),
        ):
            result = self._materialize()

        # Preflight is the one that catches it, and only after the write.
        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertTrue(
            any("repository-contract" in blocker for blocker in result.blockers),
            msg=str(result.blockers),
        )
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertIn(
            "**Readiness**: draft",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_a_resolvable_check_is_authored(self) -> None:
        self._write_plan_with_check(
            {
                "type": "command",
                "name": "resolvable",
                "argv": ["python3", "-c", "pass"],
            }
        )

        result = self._materialize()

        self.assertEqual((), result.non_bypassable_blockers)
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertTrue(result.loop_ready, msg=str(result.blockers))

    def test_a_registered_plan_rejects_the_same_shape_as_a_malformed_verdict(
        self,
    ) -> None:
        workspace = self.tmp_path / "workspace"
        repositories = init_multi_repo_workspace(
            workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="svc",
            repo="SVC",
            evidence=[{"path": "SVC:README.md", "symbol": "target"}],
            done_criteria=["SVC is fixed."],
            verification_checks=[
                {
                    "type": "command",
                    "name": "escaping",
                    "repo": "SVC",
                    "cwd": "../tundra_OAK",
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )
        _write_repair_ready_plan(
            self.plan_dir,
            repositories["SVC"],
            findings=[finding],
            specs=DEFAULT_MULTI_REPO_SPECS[:2],
        )
        progress_repair.repair_progress_journal(self.plan_dir)

        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, cwd=workspace
        )

        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertIn("must stay beneath the selected repository", result.error or "")
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())


class ReviewRepairCycleTests(unittest.TestCase):
    """Automatic repair/rereview cycles: fingerprints, caps, dispositions."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        self.fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(self.fake_codex)},
                    "claude": {"executable": str(self.fake_claude)},
                }
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Finding fingerprints
    # ------------------------------------------------------------------

    def test_finding_fingerprint_ignores_line_number_drift(self) -> None:
        original = _structured_review_finding(
            title="Unbounded retry loop at runner.py:100",
            evidence=[
                {
                    "path": "agent_session_loop/runner.py:100",
                    "symbol": "supervise_run",
                }
            ],
        )
        drifted = _structured_review_finding(
            finding_id="F1-redetected",
            title="Unbounded retry loop at runner.py:250",
            evidence=[
                {
                    "path": "agent_session_loop/runner.py:250:8",
                    "symbol": "supervise_run",
                }
            ],
            explanation="Re-detected on the second review.",
        )

        self.assertEqual(
            review.finding_fingerprint(original),
            review.finding_fingerprint(drifted),
        )
        self.assertTrue(review.finding_fingerprint(original).startswith("finding:"))

    def test_finding_fingerprint_pins_legacy_identity_and_scopes_repository(
        self,
    ) -> None:
        legacy = _structured_review_finding(
            finding_id="f1",
            severity="high",
            title="Broken thing at line 12",
            evidence=[{"path": "a/b.py:12", "symbol": "do_thing"}],
        )

        self.assertEqual("finding:a8b33340696c4f61", review.finding_fingerprint(legacy))
        svc = dict(legacy, repo="SVC")
        oak = dict(legacy, repo="OAK")
        self.assertNotEqual(
            review.finding_fingerprint(svc), review.finding_fingerprint(oak)
        )

    def test_finding_fingerprint_normalizes_title_and_evidence_order(
        self,
    ) -> None:
        one = _structured_review_finding(
            title="  Retry Loop   is unbounded (see lines 12-34)",
            evidence=[
                {"path": "pkg/a.py", "symbol": "alpha"},
                {"path": "pkg/b.py:9"},
            ],
        )
        two = _structured_review_finding(
            title="retry loop is unbounded (see line 99)",
            evidence=[
                {"path": "pkg/b.py"},
                {"path": "pkg/a.py:44", "symbol": "alpha"},
            ],
        )

        self.assertEqual(
            review.finding_fingerprint(one), review.finding_fingerprint(two)
        )

    def test_finding_fingerprint_changes_with_identity_fields(self) -> None:
        base = _structured_review_finding()
        for overrides in (
            {"severity": "low"},
            {"title": "A different defect"},
            {"evidence": [{"path": "pkg/other.py", "symbol": "run_loop"}]},
            {
                "evidence": [
                    {
                        "path": "agent_session_loop/runner.py",
                        "symbol": "other_symbol",
                    }
                ]
            },
        ):
            changed = _structured_review_finding(**overrides)
            self.assertNotEqual(
                review.finding_fingerprint(base),
                review.finding_fingerprint(changed),
                overrides,
            )

    def test_finding_fingerprints_handles_missing_findings(self) -> None:
        self.assertIsNone(review.finding_fingerprints(None))
        self.assertEqual((), review.finding_fingerprints([]))
        fingerprints = review.finding_fingerprints([_structured_review_finding()])
        self.assertEqual(1, len(fingerprints))

    # ------------------------------------------------------------------
    # Flag resolution
    # ------------------------------------------------------------------

    def _run_args(self, **overrides: object) -> argparse.Namespace:
        defaults: dict[str, Any] = {
            "cwd": str(self.repo_root),
            "timeout": None,
            "max_sessions": None,
            "max_pending_sessions": None,
            "commit_mode": None,
            "harness": "codex",
            "review_harness": None,
            "max_review_fix_cycles": None,
            "max_review_fix_plans": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_max_review_fix_cycles_resolution_and_alias(self) -> None:
        self.assertEqual(
            run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            run_inputs._max_review_fix_cycles(self._run_args()),
        )
        self.assertEqual(
            0,
            run_inputs._max_review_fix_cycles(self._run_args(max_review_fix_cycles=0)),
        )
        self.assertEqual(
            5,
            run_inputs._max_review_fix_cycles(self._run_args(max_review_fix_plans=5)),
        )
        self.assertEqual(
            1,
            run_inputs._max_review_fix_cycles(
                self._run_args(max_review_fix_cycles=1, max_review_fix_plans=9)
            ),
        )

    def test_run_input_validation_for_review_fix_flags(self) -> None:
        run_inputs._validate_run_inputs(
            self._run_args(max_review_fix_cycles=0),
            resolve_review_guard_excludes=review_guard._review_guard_excludes,
        )
        run_inputs._validate_run_inputs(
            self._run_args(max_review_fix_plans=0),
            resolve_review_guard_excludes=review_guard._review_guard_excludes,
        )
        with self.assertRaisesRegex(
            runner.RunError, "--max-review-fix-cycles must be zero"
        ):
            run_inputs._validate_run_inputs(
                self._run_args(max_review_fix_cycles=-1),
                resolve_review_guard_excludes=review_guard._review_guard_excludes,
            )
        with self.assertRaisesRegex(
            runner.RunError, "--max-review-fix-plans must be zero"
        ):
            run_inputs._validate_run_inputs(
                self._run_args(max_review_fix_plans=-2),
                resolve_review_guard_excludes=review_guard._review_guard_excludes,
            )
        with self.assertRaisesRegex(runner.RunError, "deprecated alias"):
            run_inputs._validate_run_inputs(
                self._run_args(max_review_fix_cycles=2, max_review_fix_plans=2),
                resolve_review_guard_excludes=review_guard._review_guard_excludes,
            )

    # ------------------------------------------------------------------
    # Cross-process cycle state
    # ------------------------------------------------------------------

    def test_review_cycle_state_reads_prior_events(self) -> None:
        event_path = self.tmp_path / "events.jsonl"
        self.assertEqual(
            {
                "cycle": 1,
                "repairs_materialized": 0,
                "previous_fingerprints": (),
                "previous_repeat_keys": (),
            },
            review_delta._review_cycle_state(event_path),
        )
        records = [
            {
                "name": events.REVIEW_CYCLE_EVENT,
                "payload": {
                    "cycle": 1,
                    "disposition": runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                    "fingerprints": ["finding:aaaa", "finding:bbbb"],
                },
            },
            {"name": "turn_finished", "payload": {}},
            {
                "name": events.REVIEW_CYCLE_EVENT,
                "payload": {
                    "cycle": 2,
                    "disposition": (runner.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR),
                    "fingerprints": [],
                },
            },
        ]
        event_path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

        state_payload = review_delta._review_cycle_state(event_path)
        self.assertEqual(3, state_payload["cycle"])
        self.assertEqual(1, state_payload["repairs_materialized"])
        self.assertEqual((), state_payload["previous_fingerprints"])

    def test_repeat_survives_a_moved_evidence_path_within_the_same_group(self) -> None:
        original = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="Review repeat detection ignores drifted evidence",
            evidence=[{"path": "agent_session_loop/runner.py:10625", "symbol": "run"}],
        )
        moved = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="Review repeat detection ignores drifted evidence",
            evidence=[{"path": "agent_session_loop/review.py:42", "symbol": "run"}],
        )
        unrelated = _structured_review_finding(
            finding_id="F2",
            repair_group="preflight",
            title="A different defect entirely",
            evidence=[{"path": "agent_session_loop/preflight.py:1", "symbol": "run"}],
        )

        self.assertNotEqual(
            review.finding_fingerprint(original), review.finding_fingerprint(moved)
        )
        self.assertEqual(
            review.finding_repeat_key(original), review.finding_repeat_key(moved)
        )
        self.assertNotEqual(
            review.finding_repeat_key(original), review.finding_repeat_key(unrelated)
        )

        repeated = review_continuation.repeated_finding_positions(
            fingerprints=review.finding_fingerprints([moved, unrelated]),
            repeat_keys=review.finding_repeat_keys([moved, unrelated]),
            previous_fingerprints=review.finding_fingerprints([original]),
            previous_repeat_keys=review.finding_repeat_keys([original]),
        )

        self.assertEqual((0,), repeated)

    def test_repeat_key_separates_the_same_defect_in_a_different_group(self) -> None:
        docs = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            title="Same title, different group",
            evidence=[{"path": "README.md", "symbol": "x"}],
        )
        runner_group = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="Same title, different group",
            evidence=[{"path": "README.md", "symbol": "x"}],
        )

        self.assertNotEqual(
            review.finding_repeat_key(docs), review.finding_repeat_key(runner_group)
        )
        self.assertEqual(
            (),
            review_continuation.repeated_finding_positions(
                fingerprints=review.finding_fingerprints([runner_group]),
                repeat_keys=review.finding_repeat_keys([runner_group]),
                previous_fingerprints=(),
                previous_repeat_keys=review.finding_repeat_keys([docs]),
            ),
        )

    def test_reopen_baseline_recovers_repeat_keys_from_stored_findings(self) -> None:
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="A defect reopened before repeat keys were recorded",
            evidence=[{"path": "agent_session_loop/runner.py:1", "symbol": "run"}],
        )
        event_path = self.tmp_path / "legacy-reopen-keys.jsonl"
        event_path.write_text(
            json.dumps(
                {
                    "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                    "payload": {
                        "source_cycle": 1,
                        "materialized": True,
                        "current_fingerprints": [review.finding_fingerprint(finding)],
                        "current_findings": [finding],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        state_payload = review_delta._review_cycle_state(event_path)

        self.assertEqual(
            review.finding_repeat_keys([finding]),
            state_payload["previous_repeat_keys"],
        )

    def test_post_reopen_baseline_uses_ordered_source_lineage(self) -> None:
        cases = (
            ("same-source", "3", True, ("finding:same",)),
            ("newer-source", "4", True, ("finding:newer",)),
            ("older-source", "2", True, ("finding:cycle",)),
            ("failed-newer", "4", False, ("finding:cycle",)),
            ("malformed-source", "not-a-session", True, ("finding:cycle",)),
        )
        for name, reopen_source, materialized, expected in cases:
            with self.subTest(name=name):
                event_path = self.tmp_path / f"{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(record) + "\n"
                        for record in (
                            {
                                "name": events.REVIEW_CYCLE_EVENT,
                                "payload": {
                                    "cycle": 2,
                                    "review_session_id": "3",
                                    "fingerprints": ["finding:cycle"],
                                },
                            },
                            {
                                "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                                "payload": {
                                    "source_cycle": 99,
                                    "source_review_session_id": reopen_source,
                                    "materialized": materialized,
                                    "current_fingerprints": [
                                        f"finding:{name.split('-')[0]}"
                                    ],
                                },
                            },
                        )
                    ),
                    encoding="utf-8",
                )

                state_payload = review_delta._review_cycle_state(event_path)

                self.assertEqual(expected, state_payload["previous_fingerprints"])

        legacy_path = self.tmp_path / "legacy-cycle-fallback.jsonl"
        legacy_path.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in (
                    {
                        "name": events.REVIEW_CYCLE_EVENT,
                        "payload": {
                            "cycle": 2,
                            "fingerprints": ["finding:legacy"],
                        },
                    },
                    {
                        "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                        "payload": {
                            "source_cycle": 2,
                            "materialized": True,
                            "current_fingerprints": ["finding:corrected"],
                        },
                    },
                )
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            ("finding:corrected",),
            review_delta._review_cycle_state(legacy_path)["previous_fingerprints"],
        )

    def test_post_reopen_baseline_resolves_lineage_less_cycle_source(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2", "3", "4"),
        )
        snapshot = plans.inspect_plan(self.plan_dir)

        def write_history(
            name: str,
            *,
            reopen_source: str | None,
            reopen_cycle: int | None,
        ) -> Path:
            event_path = self.tmp_path / f"lineage-less-{name}.jsonl"
            reopen_payload: dict[str, Any] = {
                "materialized": True,
                "current_fingerprints": [f"finding:{name}"],
            }
            if reopen_source is not None:
                reopen_payload["source_review_session_id"] = reopen_source
            if reopen_cycle is not None:
                reopen_payload["source_cycle"] = reopen_cycle
            event_path.write_text(
                "".join(
                    json.dumps(record) + "\n"
                    for record in (
                        {
                            "name": events.REVIEW_CYCLE_EVENT,
                            "payload": {
                                "cycle": 2,
                                "fingerprints": ["finding:legacy"],
                            },
                        },
                        {
                            "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                            "payload": reopen_payload,
                        },
                    )
                ),
                encoding="utf-8",
            )
            return event_path

        newer_path = write_history("newer", reopen_source="4", reopen_cycle=None)
        self.assertEqual(
            ("finding:newer",),
            review_delta._review_cycle_state(newer_path, snapshot)[
                "previous_fingerprints"
            ],
        )
        self.assertEqual(
            ("finding:newer",),
            review_delta._review_cycle_state(newer_path)["previous_fingerprints"],
        )

        same_path = write_history("same", reopen_source="3", reopen_cycle=None)
        self.assertEqual(
            ("finding:same",),
            review_delta._review_cycle_state(same_path, snapshot)[
                "previous_fingerprints"
            ],
        )

        older_path = write_history("older", reopen_source="2", reopen_cycle=None)
        self.assertEqual(
            ("finding:legacy",),
            review_delta._review_cycle_state(older_path, snapshot)[
                "previous_fingerprints"
            ],
        )

        mismatch_path = write_history(
            "numeric-mismatch", reopen_source=None, reopen_cycle=99
        )
        self.assertEqual(
            ("finding:legacy",),
            review_delta._review_cycle_state(mismatch_path, snapshot)[
                "previous_fingerprints"
            ],
        )

    def test_post_reopen_baseline_without_readable_reviews_orders_sources(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2", "4"),
        )
        snapshot = plans.inspect_plan(self.plan_dir)
        for review_id in ("2", "4"):
            (self.plan_dir / f"session-{review_id}.md").unlink()
        self.assertEqual({}, review_continuation._generated_review_statuses(snapshot))

        for name, sources in (
            ("older-then-newer", ("2", "4")),
            ("newer-then-older", ("4", "2")),
        ):
            with self.subTest(name=name):
                event_path = self.tmp_path / f"unreadable-reviews-{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(record) + "\n"
                        for record in (
                            {
                                "name": events.REVIEW_CYCLE_EVENT,
                                "payload": {
                                    "cycle": 2,
                                    "fingerprints": ["finding:legacy"],
                                },
                            },
                            *(
                                {
                                    "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                                    "payload": {
                                        "source_review_session_id": source,
                                        "materialized": True,
                                        "current_fingerprints": [
                                            f"finding:source-{source}"
                                        ],
                                    },
                                }
                                for source in sources
                            ),
                        )
                    ),
                    encoding="utf-8",
                )

                self.assertEqual(
                    ("finding:source-4",),
                    review_delta._review_cycle_state(event_path, snapshot)[
                        "previous_fingerprints"
                    ],
                )

    def test_zero_cycle_reopen_baseline_orders_sources(self) -> None:
        for name, sources in (
            ("older-then-newer", ("2", "4")),
            ("newer-then-older", ("4", "2")),
        ):
            with self.subTest(name=name):
                event_path = self.tmp_path / f"zero-cycle-{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(
                            {
                                "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                                "payload": {
                                    "source_review_session_id": source,
                                    "materialized": True,
                                    "current_fingerprints": [
                                        f"finding:source-{source}"
                                    ],
                                },
                            }
                        )
                        + "\n"
                        for source in sources
                    ),
                    encoding="utf-8",
                )

                state_payload = review_delta._review_cycle_state(event_path)

                self.assertEqual(1, state_payload["cycle"])
                self.assertEqual(
                    ("finding:source-4",),
                    state_payload["previous_fingerprints"],
                )

    def test_legacy_event_without_lineage_counts_once_with_children(
        self,
    ) -> None:
        write_plan_with_ids(
            self.plan_dir,
            (("1", "complete"), ("2", "complete"), ("2.1", "pending")),
        )
        review_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("2"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
        child_path = self.plan_dir / "session-2.1.md"
        child_path.write_text(
            review_repair.REVIEW_REPAIR_SESSION_MARKER
            + "\n"
            + review_repair.REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(
                version=review_repair.GENERATED_REVIEW_REPAIR_SESSION_VERSION
            )
            + "\n"
            + child_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        event_path = self.tmp_path / "events.jsonl"
        event_path.write_text(
            json.dumps(
                {
                    "name": events.REVIEW_CYCLE_EVENT,
                    "payload": {
                        "cycle": 1,
                        "disposition": (runner.REVIEW_CYCLE_DISPOSITION_REPAIRS),
                        "fingerprints": ["finding:legacy"],
                        "max_review_fix_cycles": 1,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        snapshot = plans.inspect_plan(self.plan_dir)

        event_only = review_continuation.review_cycle_state(event_path)
        reconciled = review_continuation.review_cycle_state(event_path, snapshot)
        projected = review_continuation.evaluate_review_continuation(
            snapshot, event_path
        )

        self.assertEqual(1, event_only["repairs_materialized"])
        self.assertEqual(
            event_only["repairs_materialized"],
            reconciled["repairs_materialized"],
        )
        self.assertEqual(1, projected.historical_materializations)
        self.assertEqual(2, projected.minimum_required_cap)

    def test_cap_enforcement_counts_children_after_cycle_event_crash(
        self,
    ) -> None:
        finding = _structured_review_finding()
        write_plan_with_ids(
            self.plan_dir,
            (
                ("1", "complete"),
                ("2", "complete"),
                ("2.1", "complete"),
                ("3", "complete"),
            ),
        )
        for review_id in ("2", "3"):
            review_body = review_session_body.build_generated_review_session_body(
                plans.parse_session_id(review_id),
                review_harness="claude",
                plan_dir=self.plan_dir,
            ).replace("- **Status**: pending", "- **Status**: complete")
            (self.plan_dir / f"session-{review_id}.md").write_text(
                review_body, encoding="utf-8"
            )
        child_path = self.plan_dir / "session-2.1.md"
        child_path.write_text(
            review_repair.REVIEW_REPAIR_SESSION_MARKER
            + "\n"
            + review_repair.REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(
                version=review_repair.GENERATED_REVIEW_REPAIR_SESSION_VERSION
            )
            + "\n"
            + child_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_asl_review.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.state_dir / "events.jsonl"
        snapshot = plans.inspect_plan(self.plan_dir)
        projected_before = review_continuation.evaluate_review_continuation(
            snapshot, event_path, max_review_fix_cycles=1
        )
        self.assertEqual(1, projected_before.historical_materializations)
        self.assertEqual(2, projected_before.minimum_required_cap)

        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }
        action, after = runner._advance_review_cycle(
            event_path=event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.state_dir), allow_not_loop_ready=False
            ),
            cwd=self.repo_root,
            session_id="3",
            session_label="session 3",
            session_details={"session": "3"},
            snapshot=snapshot,
            turn_payload={
                "review_verdict": {
                    "passed": True,
                    "outcome": "findings",
                    "total_findings": 1,
                    "findings": [finding],
                }
            },
            max_review_fix_cycles=1,
            plan_cache=plans.PlanSnapshotCache(),
        )

        self.assertEqual("complete", action)
        self.assertIs(snapshot, after)
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            payload["review_cycles"][0]["disposition"],
        )
        enforced = review_delta._review_cycle_state(event_path, snapshot)
        projected_after = review_continuation.evaluate_review_continuation(
            snapshot, event_path, max_review_fix_cycles=1
        )
        self.assertEqual(
            projected_after.historical_materializations,
            enforced["repairs_materialized"],
        )
        self.assertEqual(1, enforced["repairs_materialized"])

    def test_advance_review_cycle_pauses_on_malformed_repair(self) -> None:
        # Pre-existing children trip the materializer's idempotency guard,
        # which is one of the fail-closed malformed-review-repair paths.
        finding = _structured_review_finding()
        write_plan_with_ids(
            self.plan_dir,
            (("1", "complete"), ("2.1", "pending")),
        )
        review_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("2"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / "final-review.md").write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_agent_session_loop.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / "final-review.json").write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.state_dir / "events.jsonl"
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }
        turn_payload = {
            "review_verdict": {
                "passed": True,
                "outcome": "findings",
                "total_findings": 1,
                "findings": [finding],
            }
        }
        snapshot = plans.inspect_plan(self.plan_dir)

        action, after = runner._advance_review_cycle(
            event_path=event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.state_dir), allow_not_loop_ready=False
            ),
            cwd=self.repo_root,
            session_id="2",
            session_label="session 2",
            session_details={"session": "2"},
            snapshot=snapshot,
            turn_payload=turn_payload,
            max_review_fix_cycles=2,
            plan_cache=plans.PlanSnapshotCache(),
        )

        self.assertEqual("paused", action)
        # The pause reports a re-read of the plan rather than the snapshot the
        # cycle started from, because materialization already wrote to it.
        self.assertIsNot(snapshot, after)
        self.assertEqual(
            plans.inspect_plan(self.plan_dir).fingerprint, after.fingerprint
        )
        self.assertEqual("malformed-review-repair", payload["pause_reason"])
        cycles = payload["review_cycles"]
        self.assertEqual(1, len(cycles))
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_MALFORMED,
            cycles[0]["disposition"],
        )
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_MALFORMED,
            payload["review_fix_disposition"],
        )
        event_names = [record["name"] for record in read_jsonl(event_path)]
        self.assertIn(events.REVIEW_CYCLE_EVENT, event_names)
        self.assertIn(events.RUN_PAUSED_EVENT, event_names)

    def _advance_blocked_review_cycle(
        self, *, allow_not_loop_ready: bool
    ) -> tuple[str, Any, dict[str, Any], Path]:
        """Run one review cycle whose loop-ready gate refuses the children."""
        finding = _structured_review_finding()
        write_plan_with_ids(self.plan_dir, (("1", "complete"),))
        review_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("2"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_asl_review.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.state_dir / "events.jsonl"
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }
        snapshot = plans.inspect_plan(self.plan_dir)

        with mock.patch.object(
            review_repair,
            "_make_children_executable",
            return_value={
                "loop_ready": False,
                "blockers": ["worktree-dirty: uncommitted changes"],
                "non_bypassable_blockers": [],
                "baseline_summary": None,
            },
        ):
            action, after = runner._advance_review_cycle(
                event_path=event_path,
                payload=payload,
                persistence=run_persistence._RunPersistence(),
                args=argparse.Namespace(
                    state_dir=str(self.state_dir),
                    allow_not_loop_ready=allow_not_loop_ready,
                ),
                cwd=self.repo_root,
                session_id="2",
                session_label="session 2",
                session_details={"session": "2"},
                snapshot=snapshot,
                turn_payload={
                    "review_verdict": {
                        "passed": True,
                        "outcome": "findings",
                        "total_findings": 1,
                        "findings": [finding],
                    }
                },
                max_review_fix_cycles=2,
                plan_cache=plans.PlanSnapshotCache(),
            )
        return action, after, payload, event_path

    def test_a_blocked_gate_withdraws_the_children_and_projects_the_old_plan(
        self,
    ) -> None:
        """ASI-136: the pause must not leave a repair child no run can select."""
        action, after, payload, event_path = self._advance_blocked_review_cycle(
            allow_not_loop_ready=False
        )

        self.assertEqual("paused", action)
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

        # The children were withdrawn, so every projection has to name the plan
        # that is actually on disk: the one the cycle started from.
        self.assertEqual(
            plans.inspect_plan(self.plan_dir).fingerprint, after.fingerprint
        )
        self.assertNotIn("2.1", [item.id for item in after.sessions])
        projected = payload["final_snapshot"]
        self.assertEqual(
            [item.id for item in after.sessions],
            [item["id"] for item in projected["sessions"]],
        )
        self.assertNotEqual("2.1", projected["next_pending_id"])
        paused = [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ][0]
        self.assertEqual([], paused["details"]["repair_session_ids"])
        self.assertTrue(paused["details"]["repair_children_rolled_back"])
        self.assertIn("session-2.1.md", paused["details"]["repair_rollback"]["removed"])
        self.assertIn("withdrawn after authoring", paused["message"])

    def test_allow_not_loop_ready_keeps_projecting_the_materialized_children(
        self,
    ) -> None:
        action, after, payload, _event_path = self._advance_blocked_review_cycle(
            allow_not_loop_ready=True
        )

        self.assertEqual("continue", action)
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertEqual(
            plans.inspect_plan(self.plan_dir).fingerprint, after.fingerprint
        )
        self.assertIn("2.1", [item.id for item in after.sessions])
        projected = payload["final_snapshot"]
        self.assertEqual(
            [item.id for item in after.sessions],
            [item["id"] for item in projected["sessions"]],
        )
        self.assertEqual("2.1", projected["next_pending_id"])

    # ------------------------------------------------------------------
    # End-to-end cycles through the fake harness
    # ------------------------------------------------------------------

    def _write_plan(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                session="1",
            )
        }
        write_plan(self.plan_dir, ("pending",), contracts_by_id=contracts_by_id)
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run_cycles(
        self,
        *,
        review_outcomes: str,
        extra_args: list[str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--review-harness",
            "claude",
            "--state-dir",
            str(self.state_dir),
            "--json",
            *(extra_args or []),
        ]
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success-with-review",
                "FAKE_CODEX_REVIEW_OUTCOMES": review_outcomes,
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def _events_path(self) -> Path:
        plan_state = self.state_dir / plans.path_sha256(
            plans.canonicalize_path(self.plan_dir)
        )
        return plan_state / events.EVENTS_FILE_NAME

    def _review_cycle_events(self) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == events.REVIEW_CYCLE_EVENT
        ]

    def _review_reopen_events(self) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == events.REVIEW_REPAIR_REOPENED_EVENT
        ]

    def _write_completed_review_history(
        self,
        plan_dir: Path,
        *,
        review_ids: tuple[str, ...],
        repair_ids: tuple[str, ...] = (),
    ) -> None:
        session_ids = ("1", *review_ids, *repair_ids)
        write_plan_with_ids(
            plan_dir,
            tuple(
                (session_id, "complete")
                for session_id in sorted(
                    session_ids,
                    key=lambda item: plans.parse_session_id(item).sort_key,
                )
            ),
        )
        for review_id in review_ids:
            review_body = review_session_body.build_generated_review_session_body(
                plans.parse_session_id(review_id),
                review_harness="claude",
                plan_dir=plan_dir,
            ).replace("- **Status**: pending", "- **Status**: complete")
            (plan_dir / f"session-{review_id}.md").write_text(
                review_body, encoding="utf-8"
            )
        for repair_id in repair_ids:
            repair_path = plan_dir / f"session-{repair_id}.md"
            repair_path.write_text(
                review_repair.REVIEW_REPAIR_SESSION_MARKER
                + "\n"
                + review_repair.REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(
                    version=review_repair.GENERATED_REVIEW_REPAIR_SESSION_VERSION
                )
                + "\n"
                + repair_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Final review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n",
            encoding="utf-8",
        )
        (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )

    def test_legacy_cycle_does_not_claim_newer_eventless_review(self) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2", "3"),
            repair_ids=("2.1",),
        )
        event_path = self._events_path()
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "disposition": runner.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
                    "fingerprints": ["finding:legacy"],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 0,
                },
            ),
        )
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_cycle": 1,
                    "source_review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                    "materialized": True,
                    "before_max_review_fix_cycles": 0,
                    "after_max_review_fix_cycles": 1,
                },
            ),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=2,
        )

        self.assertEqual("3", projected.review_session_id)
        self.assertIsNone(projected.source_cycle)
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("all-clear", projected.source_disposition)
        self.assertEqual("all-clear", projected.blocker)

        reopen_events_before = self._review_reopen_events()
        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=[
                "--allow-not-loop-ready",
                "--max-review-fix-cycles",
                "2",
            ],
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("3", payload["review_resume"]["review_session_id"])
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        self.assertEqual(reopen_events_before, self._review_reopen_events())
        self.assertTrue(
            all(
                record.get("materialized") is True
                for record in self._review_reopen_events()
            )
        )

    def test_legacy_cycle_source_resolution_preserves_controls(self) -> None:
        cases = (
            (
                "one-review-legacy",
                ("2",),
                ({"cycle": 1, "disposition": "all-clear"},),
                "2",
                1,
            ),
            (
                "explicit-lineage",
                ("2", "3"),
                (
                    {
                        "cycle": 1,
                        "review_session_id": "3",
                        "disposition": "all-clear",
                    },
                ),
                "3",
                1,
            ),
            (
                "multiple-legacy-positions",
                ("2", "3"),
                (
                    {"cycle": 1, "disposition": "all-clear"},
                    {"cycle": 2, "disposition": "all-clear"},
                ),
                "3",
                2,
            ),
        )
        for name, review_ids, cycle_payloads, source_id, source_cycle in cases:
            with self.subTest(name=name):
                plan_dir = self.tmp_path / name
                self._write_completed_review_history(plan_dir, review_ids=review_ids)
                event_path = self.tmp_path / f"{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(
                            {
                                "name": events.REVIEW_CYCLE_EVENT,
                                "payload": payload,
                            }
                        )
                        + "\n"
                        for payload in cycle_payloads
                    ),
                    encoding="utf-8",
                )

                projected = review_continuation.evaluate_review_continuation(
                    plans.inspect_plan(plan_dir), event_path
                )

                self.assertEqual(source_id, projected.review_session_id)
                self.assertEqual(source_cycle, projected.source_cycle)
                self.assertEqual("all-clear", projected.source_disposition)
                self.assertEqual("terminal", projected.action)
                self.assertEqual("all-clear", projected.blocker)

    def test_eventless_all_clear_review_projects_terminal(self) -> None:
        write_completed_review_plan(self.plan_dir, [])
        event_path = self._events_path()
        self.assertFalse(event_path.exists())

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("all-clear", projected.source_disposition)
        self.assertEqual("all-clear", projected.blocker)
        recovery = workbench.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("none", recovery.kind)

        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=["--allow-not-loop-ready"],
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("all-clear", payload["review_resume"]["blocker"])
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        self.assertEqual([], self._review_reopen_events())

    def test_eventless_all_clear_defers_to_durable_repair_children(
        self,
    ) -> None:
        cases = (
            (
                "pending",
                (("2.1", "pending"),),
                "run-repairs",
                True,
                None,
            ),
            (
                "complete",
                (("2.1", "complete"),),
                "rereview",
                True,
                None,
            ),
            (
                "mixed",
                (("2.1", "complete"), ("2.2", "blocked")),
                "terminal",
                False,
                "repair-sessions-not-runnable",
            ),
        )
        for name, child_statuses, action, eligible, blocker in cases:
            with self.subTest(name=name):
                plan_dir = self.tmp_path / f"eventless-{name}"
                child_ids = tuple(session_id for session_id, _status in child_statuses)
                self._write_completed_review_history(
                    plan_dir,
                    review_ids=("2",),
                    repair_ids=child_ids,
                )
                for child_id, status in child_statuses:
                    child_path = plan_dir / f"session-{child_id}.md"
                    child_path.write_text(
                        child_path.read_text(encoding="utf-8").replace(
                            "- **Status**: complete",
                            f"- **Status**: {status}",
                        ),
                        encoding="utf-8",
                    )

                projected = review_continuation.evaluate_review_continuation(
                    plans.inspect_plan(plan_dir),
                    self.tmp_path / f"eventless-{name}.jsonl",
                )

                self.assertEqual("2", projected.review_session_id)
                self.assertIsNone(projected.source_cycle)
                self.assertEqual("all-clear", projected.source_disposition)
                self.assertEqual(action, projected.action)
                self.assertEqual(eligible, projected.eligible)
                self.assertEqual(blocker, projected.blocker)
                self.assertNotEqual("all-clear", projected.blocker)

    def test_eventless_all_clear_with_missing_reopen_children_pauses(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1",),
        )
        child_path = self.plan_dir / "session-2.1.md"
        self.assertTrue(child_path.is_file())
        child_path.unlink()
        event_path = self._events_path()
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_cycle": None,
                    "source_review_session_id": "2",
                    "source_disposition": None,
                    "repair_session_ids": ["2.1"],
                    "materialized": True,
                    "result": "ok",
                    "after_max_review_fix_cycles": 1,
                },
            ),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )

        self.assertEqual("2", projected.review_session_id)
        self.assertIsNone(projected.source_cycle)
        self.assertEqual("all-clear", projected.source_disposition)
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("repair-sessions-missing", projected.blocker)
        self.assertEqual(("2.1",), projected.repair_session_ids)

        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=[
                "--allow-not-loop-ready",
                "--max-review-fix-cycles",
                "1",
            ],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            payload["pause_reason"],
        )
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("repair-sessions-missing", payload["review_resume"]["blocker"])
        self.assertIsNone(payload["review_fix_disposition"])

        recovery = workbench.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("review-continuation", recovery.kind)
        self.assertEqual("Review continuation is inconsistent", recovery.title)
        self.assertEqual("error", recovery.severity)
        self.assertEqual(payload["pause_message"], recovery.summary)
        self.assertEqual(payload["review_resume"], recovery.review_resume)

    def test_findings_repairs_all_clear_completes_in_one_invocation(
        self,
    ) -> None:
        self._write_plan()

        completed, payload = self._run_cycles(review_outcomes="findings,all-clear")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        # Turn order: impl 1, review 2 (findings), repair 2.1, review 3.
        self.assertEqual(
            ["1", "2", "2.1", "3"],
            [turn["session_id"] for turn in payload["turns"]],
        )
        self.assertEqual(
            [
                runner.SESSION_KIND_IMPLEMENTATION,
                runner.SESSION_KIND_REVIEW,
                runner.SESSION_KIND_IMPLEMENTATION,
                runner.SESSION_KIND_REVIEW,
            ],
            [turn["session_kind"] for turn in payload["turns"]],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(2, len(cycles))
        self.assertEqual(1, cycles[0]["cycle"])
        self.assertEqual("2", cycles[0]["review_session_id"])
        self.assertEqual("findings", cycles[0]["outcome"])
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            cycles[0]["disposition"],
        )
        self.assertEqual(["2.1"], cycles[0]["repair_session_ids"])
        self.assertEqual(cycles[0]["fingerprints"], cycles[0]["introduced_findings"])
        self.assertEqual(2, cycles[1]["cycle"])
        self.assertEqual("3", cycles[1]["review_session_id"])
        self.assertEqual("all-clear", cycles[1]["outcome"])
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            cycles[1]["disposition"],
        )
        self.assertEqual(cycles[0]["fingerprints"], cycles[1]["closed_findings"])
        child_path = self.plan_dir / "session-2.1.md"
        self.assertTrue(child_path.is_file())
        self.assertTrue(
            review_repair.is_generated_review_repair_session_file(child_path)
        )
        self.assertEqual(2, len(self._review_cycle_events()))
        self.assertEqual(["1", "2", "2.1", "3"], payload["completed_session_ids"])

    def test_repeated_findings_stop_without_new_cycle(self) -> None:
        self._write_plan()

        # The second review re-detects the same finding with drifted line
        # numbers; the normalized fingerprint must match and stop the loop.
        completed, payload = self._run_cycles(review_outcomes="findings,findings-drift")

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(runner.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"])
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPEATED,
            payload["review_fix_disposition"],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(2, len(cycles))
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            cycles[0]["disposition"],
        )
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPEATED,
            cycles[1]["disposition"],
        )
        self.assertEqual(cycles[0]["fingerprints"], cycles[1]["repeated_findings"])
        self.assertEqual([], cycles[1]["repair_session_ids"])
        # One repair generation only: 2.1 exists, review 3 has no children.
        self.assertTrue((self.plan_dir / "session-2.1.md").is_file())
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

        # A larger unused cap cannot reopen the repeated-finding circuit
        # breaker or append another review on a later direct invocation.
        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,findings-drift,all-clear",
            extra_args=["--max-review-fix-cycles", "8"],
        )
        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual([], resumed_payload["turns"])
        self.assertEqual("terminal", resumed_payload["review_resume"]["action"])
        self.assertEqual(
            "findings-repeated", resumed_payload["review_resume"]["blocker"]
        )
        self.assertFalse((self.plan_dir / "session-4.md").exists())

    def test_eventless_repeated_rereview_stops_before_rematerialization(
        self,
    ) -> None:
        self._write_plan()

        # Stop after the first findings review materializes its child. Then
        # model the crash window after the child completes and the rereview
        # completes, but before _advance_review_cycle appends that rereview's
        # findings-repeated cycle record.
        first, first_payload = self._run_cycles(
            review_outcomes="findings",
            extra_args=["--max-sessions", "1"],
        )
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("max-sessions", first_payload["reason"])
        first_cycle = first_payload["review_cycles"][0]
        fingerprint = first_cycle["fingerprints"][0]
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            first_cycle["disposition"],
        )

        repair_path = self.plan_dir / "session-2.1.md"
        repair_path.write_text(
            repair_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        rereview, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        self.assertTrue(created)
        self.assertEqual("3", rereview.id)
        rereview.path.write_text(
            rereview.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            self._events_path(),
            max_review_fix_cycles=2,
        )
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPEATED,
            projected.blocker,
        )
        self.assertEqual((fingerprint,), projected.current_fingerprints)

        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,findings-drift",
            extra_args=["--max-review-fix-cycles", "2"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual([], resumed_payload["turns"])
        self.assertEqual("terminal", resumed_payload["review_resume"]["action"])
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPEATED,
            resumed_payload["review_resume"]["blocker"],
        )
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())
        self.assertEqual(1, len(self._review_cycle_events()))

    def test_zero_cycles_reviews_but_creates_no_repairs(self) -> None:
        self._write_plan()

        completed, payload = self._run_cycles(
            review_outcomes="findings",
            extra_args=["--max-review-fix-cycles", "0"],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(runner.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"])
        self.assertEqual(0, payload["max_review_fix_cycles"])
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
            payload["review_fix_disposition"],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(1, len(cycles))
        self.assertEqual([], cycles[0]["repair_session_ids"])
        self.assertEqual(["1", "2"], payload["completed_session_ids"])
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        # The findings verdict is still recorded for the operator.
        self.assertEqual("findings", payload["review_verdict"]["outcome"])

    def test_cycle_cap_stops_after_configured_cycles(self) -> None:
        self._write_plan()

        # Cycle 1 repairs; the rereview finds a *different* defect, but the
        # cap of 1 materialization is already spent.
        completed, payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(runner.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"])
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            payload["review_fix_disposition"],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(2, len(cycles))
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            cycles[0]["disposition"],
        )
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            cycles[1]["disposition"],
        )
        # The new finding is introduced, not repeated.
        self.assertEqual([], cycles[1]["repeated_findings"])
        self.assertEqual(cycles[1]["fingerprints"], cycles[1]["introduced_findings"])
        self.assertTrue((self.plan_dir / "session-2.1.md").is_file())
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

    def test_deprecated_alias_flag_maps_to_cycles(self) -> None:
        self._write_plan()

        completed, payload = self._run_cycles(
            review_outcomes="findings",
            extra_args=["--max-review-fix-plans", "0"],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(0, payload["max_review_fix_cycles"])
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
            payload["review_fix_disposition"],
        )
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

    def test_completed_review_reopens_only_when_absolute_cap_increases(
        self,
    ) -> None:
        self._write_plan()
        scripted = "findings,findings-new,all-clear"

        first, first_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "0"],
        )

        self.assertEqual(1, first.returncode, first.stderr)
        self.assertEqual("cycles-disabled", first_payload["review_fix_disposition"])
        self.assertEqual(
            "higher-cap-required", first_payload["review_resume"]["blocker"]
        )
        first_cycle = self._review_cycle_events()[0]
        self.assertIsNotNone(first_cycle["review_output_sha256"])
        self.assertIsNotNone(first_cycle["review_artifact_sha256"])
        artifact_before_noop = (
            self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        ).read_bytes()

        same, same_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "0"],
        )

        self.assertEqual(1, same.returncode, same.stderr)
        self.assertEqual([], same_payload["turns"])
        self.assertEqual(
            "higher-cap-required", same_payload["review_resume"]["blocker"]
        )
        self.assertEqual(
            artifact_before_noop,
            (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_bytes(),
        )
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertEqual([], self._review_reopen_events())

        second, second_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, second.returncode, second.stderr)
        self.assertEqual(
            ["2.1", "3"],
            [turn["session_id"] for turn in second_payload["turns"]],
        )
        self.assertEqual(
            "cycle-cap-exhausted", second_payload["review_fix_disposition"]
        )
        self.assertEqual(
            "higher-cap-required", second_payload["review_resume"]["blocker"]
        )
        first_reopen = self._review_reopen_events()[0]
        self.assertTrue(first_reopen["materialized"])
        self.assertEqual("2", first_reopen["source_review_session_id"])
        self.assertEqual(["2.1"], first_reopen["repair_session_ids"])
        self.assertEqual(0, first_reopen["historical_materializations_before"])
        self.assertEqual(1, first_reopen["historical_materializations_after"])
        self.assertEqual(0, first_reopen["before_max_review_fix_cycles"])
        self.assertEqual(1, first_reopen["after_max_review_fix_cycles"])
        self.assertIsNotNone(first_reopen["current_review_output_sha256"])
        self.assertIsNotNone(first_reopen["current_review_artifact_sha256"])

        artifact_before_second_noop = (
            self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        ).read_bytes()
        lower, lower_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "1"],
        )
        self.assertEqual(1, lower.returncode, lower.stderr)
        self.assertEqual([], lower_payload["turns"])
        self.assertEqual(
            artifact_before_second_noop,
            (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_bytes(),
        )
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

        third, third_payload = self._run_cycles(
            review_outcomes=scripted,
            extra_args=["--max-review-fix-cycles", "2"],
        )

        self.assertEqual(0, third.returncode, third.stderr)
        self.assertEqual(
            ["3.1", "4"],
            [turn["session_id"] for turn in third_payload["turns"]],
        )
        self.assertEqual("all-clear", third_payload["review_fix_disposition"])
        self.assertEqual("terminal", third_payload["review_resume"]["action"])
        reopen_events = self._review_reopen_events()
        self.assertEqual(2, len(reopen_events))
        self.assertEqual("3", reopen_events[1]["source_review_session_id"])
        self.assertEqual(["3.1"], reopen_events[1]["repair_session_ids"])
        self.assertEqual(1, reopen_events[1]["historical_materializations_before"])
        self.assertEqual(2, reopen_events[1]["historical_materializations_after"])
        self.assertEqual(1, reopen_events[1]["before_max_review_fix_cycles"])
        self.assertEqual(2, reopen_events[1]["after_max_review_fix_cycles"])

    def test_repair_sessions_missing_blocker_is_not_reported_all_complete(
        self,
    ) -> None:
        self._write_plan()
        first, first_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=[
                "--max-review-fix-cycles",
                "1",
                "--max-sessions",
                "1",
            ],
        )
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("max-sessions", first_payload["reason"])
        child_path = self.plan_dir / "session-2.1.md"
        self.assertTrue(child_path.is_file())

        child_path.unlink()
        resumed, payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertNotEqual("all-complete", payload["reason"])
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            payload["pause_reason"],
        )
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("repair-sessions-missing", payload["review_resume"]["blocker"])
        self.assertIsNone(payload["review_fix_disposition"])

        finished_events = [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == "run_finished"
            and record["payload"]["run_id"] == payload["run_id"]
        ]
        self.assertEqual(1, len(finished_events))
        self.assertIsNone(finished_events[0]["review_fix_disposition"])

        recovery = workbench.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("review-continuation", recovery.kind)
        self.assertEqual("Review continuation is inconsistent", recovery.title)
        self.assertEqual("error", recovery.severity)
        self.assertEqual(payload["pause_message"], recovery.summary)
        self.assertEqual(payload["review_resume"], recovery.review_resume)

    def test_partial_repair_child_deletion_pauses_as_missing_lineage(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1", "2.2"),
        )
        missing_child = self.plan_dir / "session-2.2.md"
        self.assertTrue(missing_child.is_file())
        missing_child.unlink()
        event_path = self._events_path()
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "repairs-materialized",
                    "repair_session_ids": ["2.1", "2.2"],
                    "max_review_fix_cycles": 1,
                },
            ),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )

        self.assertEqual("2", projected.review_session_id)
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("repair-sessions-missing", projected.blocker)
        self.assertEqual(("2.1", "2.2"), projected.repair_session_ids)
        self.assertEqual(("2.1",), projected.completed_repair_session_ids)

        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=[
                "--allow-not-loop-ready",
                "--max-review-fix-cycles",
                "1",
            ],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            payload["pause_reason"],
        )
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual(
            "repair-sessions-missing",
            payload["review_resume"]["blocker"],
        )
        self.assertIsNone(payload["review_fix_disposition"])

        recovery = workbench.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("review-continuation", recovery.kind)
        self.assertEqual("Review continuation is inconsistent", recovery.title)
        self.assertEqual("error", recovery.severity)
        self.assertEqual(payload["pause_message"], recovery.summary)
        self.assertEqual(payload["review_resume"], recovery.review_resume)

    def test_reopen_rechecks_corrected_artifact_and_preserves_legacy_lineage(
        self,
    ) -> None:
        self._write_plan()
        first, _ = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)

        # Model a legacy source cycle whose persisted event predates digests.
        event_path = self._events_path()
        records = read_jsonl(event_path)
        for record in records:
            if record["name"] == events.REVIEW_CYCLE_EVENT:
                record["payload"].pop("review_output_sha256", None)
                record["payload"].pop("review_artifact_sha256", None)
        event_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        findings = [
            _structured_review_finding(
                finding_id=f"revised-{index}",
                title=f"Revised finding {index}",
                evidence=[
                    {
                        "path": f"agent_session_loop/revised_{index}.py:{index}",
                        "symbol": f"target_{index}",
                    }
                ],
                repair_group=f"group-{index}",
                verification_checks=[
                    {
                        "type": "command",
                        "name": f"verify-revised-{index}",
                        "argv": ["python3", "-c", "pass"],
                    }
                ],
            )
            for index in range(1, 10)
        ]
        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_path.write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
            encoding="utf-8",
        )
        review_path = self.plan_dir / review.FINAL_REVIEW_FILENAME
        review_path.write_text(
            review_path.read_text(encoding="utf-8").replace(
                "Status: 1 Findings (1 high, 0 med, 0 low)",
                "Status: 9 Findings (9 high, 0 med, 0 low)",
            ),
            encoding="utf-8",
        )
        invalid_bytes = artifact_path.read_bytes()

        invalid, invalid_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, invalid.returncode)
        self.assertEqual("malformed-review-repair", invalid_payload["pause_reason"])
        self.assertEqual(
            "artifact-invalid", invalid_payload["review_resume"]["blocker"]
        )
        self.assertIn("exceed the maximum of 8", invalid_payload["pause_message"])
        self.assertEqual(invalid_bytes, artifact_path.read_bytes())
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))
        self.assertFalse(self._review_reopen_events()[-1]["materialized"])

        # A revised, internally consistent v2 artifact may regroup findings.
        findings[-1]["repair_group"] = "group-8"
        artifact_path.write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
            encoding="utf-8",
        )
        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(
            [*(f"2.{index}" for index in range(1, 9)), "3"],
            [turn["session_id"] for turn in resumed_payload["turns"]],
        )
        success = [
            record for record in self._review_reopen_events() if record["materialized"]
        ][0]
        self.assertIsNone(success["source_review_output_sha256"])
        self.assertIsNone(success["source_review_artifact_sha256"])
        self.assertNotEqual(
            success["previous_fingerprints"], success["current_fingerprints"]
        )
        self.assertEqual(
            [f"2.{index}" for index in range(1, 9)],
            success["repair_session_ids"],
        )

    def test_post_reopen_review_repeats_against_corrected_fingerprints(
        self,
    ) -> None:
        self._write_plan()
        first, first_payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
            first_payload["review_fix_disposition"],
        )

        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_path.write_text("{not valid json", encoding="utf-8")
        invalid, invalid_payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )
        self.assertEqual(1, invalid.returncode)
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            invalid_payload["pause_reason"],
        )
        self.assertFalse(self._review_reopen_events()[-1]["materialized"])

        # Model a completed findings rereview whose review_cycle event was
        # lost. Its corrected artifact is then materialized successfully,
        # leaving the latest durable cycle attached to the older review 2.
        eventless_review, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        self.assertTrue(created)
        self.assertEqual("3", eventless_review.id)
        eventless_review.path.write_text(
            eventless_review.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        corrected = _structured_review_finding(
            finding_id="corrected-cycle-finding",
            title="Missing error handling in verification executor",
            evidence=[
                {
                    "path": "agent_session_loop/verification.py:88",
                    "symbol": "run_check",
                }
            ],
            repair_group="verification-errors",
        )
        corrected_fingerprint = review.finding_fingerprint(corrected)
        self.assertNotEqual(
            first_payload["review_cycles"][0]["fingerprints"],
            [corrected_fingerprint],
        )
        artifact_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "outcome": "findings",
                    "findings": [corrected],
                }
            ),
            encoding="utf-8",
        )

        materialized = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            review_session_id="3",
            cwd=self.repo_root,
            state_dir=self.state_dir,
        )
        self.assertTrue(materialized.ok, materialized.error)
        self.assertEqual(
            ["3.1"], [child.session_id.value for child in materialized.children]
        )
        events.append_event(
            self._events_path(),
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "run_id": "eventless-review-reopen",
                    "source_cycle": 2,
                    "source_review_session_id": "3",
                    "source_disposition": (
                        runner.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED
                    ),
                    "previous_fingerprints": None,
                    "current_fingerprints": [corrected_fingerprint],
                    "repair_session_ids": ["3.1"],
                    "materialized": True,
                    "recovered": False,
                    "result": "materialized",
                    "historical_materializations_before": 0,
                    "historical_materializations_after": 1,
                    "minimum_required_cap": 1,
                    "before_max_review_fix_cycles": 0,
                    "after_max_review_fix_cycles": 1,
                },
            ),
        )
        before_rereview = review_delta._review_cycle_state(
            self._events_path(), plans.inspect_plan(self.plan_dir)
        )
        self.assertEqual(
            (corrected_fingerprint,), before_rereview["previous_fingerprints"]
        )

        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual(
            ["3.1", "4"],
            [turn["session_id"] for turn in resumed_payload["turns"]],
        )
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPEATED,
            resumed_payload["review_fix_disposition"],
        )
        post_reopen = resumed_payload["review_cycles"][-1]
        self.assertEqual("4", post_reopen["review_session_id"])
        self.assertEqual([corrected_fingerprint], post_reopen["fingerprints"])
        self.assertEqual([corrected_fingerprint], post_reopen["repeated_findings"])
        self.assertEqual([], post_reopen["introduced_findings"])
        successful_reopen = [
            record for record in self._review_reopen_events() if record["materialized"]
        ][0]
        self.assertEqual("3", successful_reopen["source_review_session_id"])
        self.assertEqual(
            [corrected_fingerprint],
            successful_reopen["current_fingerprints"],
        )
        self.assertFalse((self.plan_dir / "session-4.1.md").exists())

    def test_reopened_children_rereview_across_max_sessions_boundary(
        self,
    ) -> None:
        self._write_plan()
        first, _ = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)

        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        original = json.loads(artifact_path.read_text(encoding="utf-8"))
        extra_finding = dict(original["findings"][0])
        extra_finding.update(
            {
                "id": "cycle-finding-extra",
                "title": "A second repair group",
                "evidence": [
                    {
                        "path": "agent_session_loop/review.py:200",
                        "symbol": "load_review_verdict",
                    }
                ],
                "repair_group": "review-loading",
                "verification_checks": [
                    {
                        "type": "command",
                        "name": "review-loading-check",
                        "argv": ["python3", "-c", "pass"],
                    }
                ],
            }
        )
        original["findings"].append(extra_finding)
        artifact_path.write_text(json.dumps(original), encoding="utf-8")
        review_path = self.plan_dir / review.FINAL_REVIEW_FILENAME
        review_path.write_text(
            review_path.read_text(encoding="utf-8").replace(
                "Status: 1 Findings (1 high, 0 med, 0 low)",
                "Status: 2 Findings (2 high, 0 med, 0 low)",
            ),
            encoding="utf-8",
        )

        partial, partial_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=[
                "--max-review-fix-cycles",
                "1",
                "--max-sessions",
                "1",
            ],
        )

        self.assertEqual(0, partial.returncode, partial.stderr)
        self.assertEqual("max-sessions", partial_payload["reason"])
        self.assertEqual(
            ["2.1"], [turn["session_id"] for turn in partial_payload["turns"]]
        )
        self.assertTrue((self.plan_dir / "session-2.2.md").is_file())
        self.assertFalse((self.plan_dir / "session-3.md").exists())
        partial_resume = partial_payload["review_resume"]
        self.assertEqual(["2.2"], partial_resume["pending_repair_session_ids"])
        self.assertEqual(["2.1"], partial_resume["completed_repair_session_ids"])
        partial_finished = [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == "run_finished"
            and record["payload"]["run_id"] == partial_payload["run_id"]
        ]
        self.assertEqual(1, len(partial_finished))
        self.assertEqual(partial_resume, partial_finished[0]["review_resume"])

        final, final_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=[
                "--max-review-fix-cycles",
                "1",
                "--max-sessions",
                "1",
            ],
        )

        self.assertEqual(0, final.returncode, final.stderr)
        self.assertEqual(
            ["2.2", "3"],
            [turn["session_id"] for turn in final_payload["turns"]],
        )
        self.assertEqual("all-clear", final_payload["review_fix_disposition"])
        self.assertEqual(1, len(self._review_reopen_events()))
        self.assertFalse((self.plan_dir / "session-2.3.md").exists())
        progress = (self.plan_dir / "progress.md").read_text(encoding="utf-8")
        self.assertEqual(1, progress.count("## Session 2.1 "))
        self.assertEqual(1, progress.count("## Session 2.2 "))
        self.assertEqual(1, progress.count("## Session 3 "))

    def test_eventless_materialization_crash_backfills_rereview_baseline(
        self,
    ) -> None:
        eventless_finding = _structured_review_finding(
            finding_id="cycle-finding-2",
            title="Missing error handling in verification executor",
            evidence=[
                {
                    "path": "agent_session_loop/verification.py:88",
                    "symbol": "run_check",
                }
            ],
            repair_group="verification-errors",
        )
        eventless_fingerprint = review.finding_fingerprint(eventless_finding)
        cases = (
            (
                "repeats-eventless",
                "findings-new",
                runner.REVIEW_CYCLE_DISPOSITION_REPEATED,
                [eventless_fingerprint],
                [],
            ),
            (
                "matches-only-older",
                "findings",
                runner.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
                [],
                None,
            ),
        )
        for (
            name,
            rereview_outcome,
            expected_disposition,
            expected_repeated,
            expected_introduced,
        ) in cases:
            with self.subTest(name=name):
                self.plan_dir = self.tmp_path / f"plan-{name}"
                self.state_dir = self.tmp_path / f"state-{name}"
                self._write_plan()
                first, first_payload = self._run_cycles(
                    review_outcomes="findings",
                    extra_args=["--max-review-fix-cycles", "0"],
                )
                self.assertEqual(1, first.returncode, first.stderr)
                older_fingerprint = first_payload["review_cycles"][0]["fingerprints"][0]
                self.assertNotEqual(older_fingerprint, eventless_fingerprint)

                eventless_review, created = (
                    review_session_body.ensure_generated_review_session(
                        self.plan_dir, review_harness="claude"
                    )
                )
                self.assertTrue(created)
                self.assertEqual("3", eventless_review.id)
                eventless_review.path.write_text(
                    eventless_review.path.read_text(encoding="utf-8").replace(
                        "- **Status**: pending", "- **Status**: complete"
                    ),
                    encoding="utf-8",
                )
                (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "outcome": "findings",
                            "findings": [eventless_finding],
                        }
                    ),
                    encoding="utf-8",
                )
                materialized = review_repair.materialize_review_repair_sessions(
                    self.plan_dir,
                    review_session_id="3",
                    cwd=self.repo_root,
                    state_dir=self.state_dir,
                )
                self.assertTrue(materialized.ok, materialized.error)
                self.assertEqual([], self._review_reopen_events())

                projected = review_continuation.evaluate_review_continuation(
                    plans.inspect_plan(self.plan_dir),
                    self._events_path(),
                    max_review_fix_cycles=1,
                )
                self.assertIsNone(projected.source_disposition)
                self.assertEqual("run-repairs", projected.action)
                self.assertEqual(
                    (eventless_fingerprint,), projected.current_fingerprints
                )

                resumed, payload = self._run_cycles(
                    review_outcomes=rereview_outcome,
                    extra_args=["--max-review-fix-cycles", "1"],
                )

                self.assertEqual(1, resumed.returncode, resumed.stderr)
                self.assertEqual(
                    ["3.1", "4"],
                    [turn["session_id"] for turn in payload["turns"]],
                )
                reopen_events = self._review_reopen_events()
                self.assertEqual(1, len(reopen_events))
                self.assertTrue(reopen_events[0]["recovered"])
                self.assertEqual(
                    [eventless_fingerprint],
                    reopen_events[0]["current_fingerprints"],
                )
                rereview_cycle = payload["review_cycles"][-1]
                self.assertEqual(expected_disposition, rereview_cycle["disposition"])
                self.assertEqual(expected_repeated, rereview_cycle["repeated_findings"])
                if expected_introduced is None:
                    expected_introduced = [older_fingerprint]
                self.assertEqual(
                    expected_introduced,
                    rereview_cycle["introduced_findings"],
                )

    def test_first_review_crash_backfill_repeats_against_reopen_baseline(
        self,
    ) -> None:
        self._write_plan()

        # Model a crash after the first implementation completed, followed by
        # a first review that materialized its repair child before the initial
        # review_cycle append. The event log therefore has no cycle record.
        implementation_path = self.plan_dir / "session-1.md"
        implementation_path.write_text(
            implementation_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        first_review, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        self.assertTrue(created)
        self.assertEqual("2", first_review.id)
        first_review.path.write_text(
            first_review.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        finding = _structured_review_finding(
            finding_id="cycle-finding-2",
            title="Missing error handling in verification executor",
            evidence=[
                {
                    "path": "agent_session_loop/verification.py:88",
                    "symbol": "run_check",
                }
            ],
            repair_group="verification-errors",
        )
        fingerprint = review.finding_fingerprint(finding)
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- agent_session_loop/verification.py:88 — Missing error handling "
            "in verification executor.\n"
            "- Checked tests/test_agent_session_loop.py for regression coverage.\n"
            "- Compared docs/agent-session-loop.md for documented behavior.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        materialized = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            review_session_id="2",
            cwd=self.repo_root,
            state_dir=self.state_dir,
        )
        self.assertTrue(materialized.ok, materialized.error)
        self.assertFalse(self._events_path().exists())

        resumed, payload = self._run_cycles(
            review_outcomes="findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual(
            ["2.1", "3"], [turn["session_id"] for turn in payload["turns"]]
        )
        reopen_events = self._review_reopen_events()
        self.assertEqual(1, len(reopen_events))
        self.assertTrue(reopen_events[0]["recovered"])
        self.assertEqual([fingerprint], reopen_events[0]["current_fingerprints"])
        self.assertEqual(1, len(payload["review_cycles"]))
        rereview_cycle = payload["review_cycles"][0]
        self.assertEqual(
            runner.REVIEW_CYCLE_DISPOSITION_REPEATED,
            rereview_cycle["disposition"],
        )
        self.assertEqual([fingerprint], rereview_cycle["repeated_findings"])
        self.assertEqual([], rereview_cycle["introduced_findings"])
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

    def test_existing_reopened_children_backfill_missing_success_event(
        self,
    ) -> None:
        self._write_plan()
        first, _ = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)
        materialized = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            review_session_id="2",
            cwd=self.repo_root,
            state_dir=self.state_dir,
        )
        self.assertTrue(materialized.ok, materialized.error)
        self.assertEqual([], self._review_reopen_events())

        resumed, payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(
            ["2.1", "3"], [turn["session_id"] for turn in payload["turns"]]
        )
        reopen_events = self._review_reopen_events()
        self.assertEqual(1, len(reopen_events))
        self.assertTrue(reopen_events[0]["materialized"])
        self.assertTrue(reopen_events[0]["recovered"])
        self.assertEqual("recovered-existing-children", reopen_events[0]["result"])
        self.assertEqual(["2.1"], reopen_events[0]["repair_session_ids"])
        self.assertFalse((self.plan_dir / "session-2.2.md").exists())


if __name__ == "__main__":
    unittest.main()
