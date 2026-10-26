"""Structured review artifact parsing and consistency tests."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import plans_registry, review, workspaces
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _structured_review_finding,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)

ASL_TEST_LANE = "integration"


class ReviewArtifactSchemaTests(unittest.TestCase):
    """Parse, validate and hash structured review artifacts."""

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
            registry = plans_registry.read_repository_registry(plan_dir)
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

            # Deep enough that the decoder raises RecursionError instead.
            artifact_path.write_text("[" * 1_000_000, encoding="utf-8")
            nested = review.load_review_verdict(plan_dir)
            self.assertFalse(nested["ok"])
            self.assertEqual("malformed-review-artifact", nested["reason"])
            self.assertIn("nested too deeply", nested["error"])


if __name__ == "__main__":
    unittest.main()
