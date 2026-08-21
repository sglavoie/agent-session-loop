from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import contracts, events, plans, review, runner, verification

from .support import (
    DEFAULT_MULTI_REPO_SPECS,
    write_completed_review_plan,
    write_multi_repo_plan,
    write_plan_with_ids,
)

ALIASES = tuple(alias for alias, _path, _origin in DEFAULT_MULTI_REPO_SPECS)


def set_handoff(session_path: Path, *, status: str, files_modified: str | None) -> None:
    """Rewrite one session's handoff status and Files modified line."""
    text = session_path.read_text(encoding="utf-8")
    text = re.sub(r"- \*\*Status\*\*: \w+", f"- **Status**: {status}", text, count=1)
    if files_modified is None:
        text = re.sub(r"- \*\*Files modified\*\*: .*\n", "", text, count=1)
    else:
        text = re.sub(
            r"- \*\*Files modified\*\*: .*",
            f"- **Files modified**: {files_modified}",
            text,
            count=1,
        )
    session_path.write_text(text, encoding="utf-8")


class TurnFinalizationShapeTests(unittest.TestCase):
    def test_finalization_carries_execution_state_only(self) -> None:
        fields = tuple(runner.TurnFinalization.__dataclass_fields__)
        self.assertEqual(("result", "after", "plan_error", "turn_payload"), fields)

    def test_finalization_exposes_no_accepted_completion(self) -> None:
        public = [
            name
            for name in vars(runner.TurnFinalization)
            if not name.startswith("_")
            and callable(getattr(runner.TurnFinalization, name, None))
        ]
        self.assertEqual([], public)

    def test_execution_phase_never_announces_turn_finished(self) -> None:
        source = inspect.getsource(runner._finalize_turn_execution)
        self.assertIn("TURN_EXECUTION_FINISHED_EVENT", source)
        self.assertNotIn("turn_finished", source)

    def test_execution_event_precedes_turn_finished_in_run_loop(self) -> None:
        source = inspect.getsource(runner._run_locked)
        finalize = source.index("_finalize_turn_execution(")
        finished = source.index("_commit_turn(")
        self.assertLess(finalize, finished)

    def test_turn_result_is_written_durably(self) -> None:
        source = inspect.getsource(runner._write_turn_artifacts)
        self.assertIn(
            '_write_json_file(paths["result"], payload, durable=True)', source
        )


class MechanicalHandoffDefectTests(unittest.TestCase):
    def classify(
        self, *, status: str, files_modified: str | None
    ) -> runner._HandoffDefect | None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("pending",))
            snapshot = plans.inspect_plan(plan_dir)
            set_handoff(
                plan_dir / "session-1.md",
                status=status,
                files_modified=files_modified,
            )
            return runner._classify_mechanical_handoff_defect(
                snapshot, "1", registry_aliases=ALIASES
            )

    def test_missing_files_modified_line_is_correctable(self) -> None:
        defect = self.classify(status="complete", files_modified=None)
        assert defect is not None
        self.assertEqual(runner.HANDOFF_DEFECT_MISSING_FILES, defect.code)
        self.assertIn("SVC", defect.instruction)

    def test_pending_placeholder_is_correctable(self) -> None:
        defect = self.classify(status="complete", files_modified="(none yet)")
        assert defect is not None
        self.assertEqual(runner.HANDOFF_DEFECT_PENDING_PLACEHOLDER, defect.code)

    def test_unqualified_entry_is_correctable(self) -> None:
        defect = self.classify(status="complete", files_modified="README.md")
        assert defect is not None
        self.assertEqual(runner.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, defect.code)
        self.assertIn("README.md", defect.detail)

    def test_mixed_entries_report_the_first_invalid_one(self) -> None:
        defect = self.classify(
            status="complete", files_modified="SVC:README.md, README.md"
        )
        assert defect is not None
        self.assertEqual(runner.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, defect.code)
        self.assertIn("'README.md'", defect.detail)

    def test_qualified_entries_are_not_a_defect(self) -> None:
        self.assertIsNone(
            self.classify(status="complete", files_modified="SVC:README.md")
        )

    def test_no_files_marker_is_not_a_defect(self) -> None:
        self.assertIsNone(self.classify(status="complete", files_modified="(none)"))

    def test_unknown_alias_is_not_a_mechanical_defect(self) -> None:
        # ``Nope`` parses as a qualified path but names no registered
        # repository, which is a scope question rather than a typo.
        defect = self.classify(status="complete", files_modified="Nope:README.md")
        assert defect is not None
        self.assertEqual(runner.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, defect.code)

    def test_incomplete_session_is_not_corrected(self) -> None:
        self.assertIsNone(self.classify(status="pending", files_modified="README.md"))

    def test_unknown_session_is_not_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("pending",))
            snapshot = plans.inspect_plan(plan_dir)
            self.assertIsNone(
                runner._classify_mechanical_handoff_defect(
                    snapshot, "9", registry_aliases=ALIASES
                )
            )


class PreservedReviewArtifactTests(unittest.TestCase):
    def test_valid_artifacts_are_digested_with_their_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            evidence = runner._preserved_review_artifact_evidence(plan_dir)
            assert evidence is not None
            self.assertEqual(
                str(review.final_review_output_path(plan_dir)),
                evidence["review_output_path"],
            )
            self.assertEqual(
                review.final_review_output_sha256(plan_dir),
                evidence["review_output_sha256"],
            )
            self.assertEqual(
                review.final_review_artifact_sha256(plan_dir),
                evidence["review_artifact_sha256"],
            )
            self.assertTrue(evidence["review_verdict_ok"])
            self.assertEqual("all-clear", evidence["review_outcome"])
            self.assertEqual(0, evidence["review_total_findings"])

    def test_unparsable_verdict_still_preserves_the_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\nNo verdict here.\n", encoding="utf-8"
            )
            evidence = runner._preserved_review_artifact_evidence(plan_dir)
            assert evidence is not None
            self.assertFalse(evidence["review_verdict_ok"])
            self.assertTrue(evidence["review_verdict_reason"])
            self.assertIn("review_output_sha256", evidence)

    def test_plan_without_review_artifacts_yields_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("pending",))
            self.assertIsNone(runner._preserved_review_artifact_evidence(plan_dir))


class HandoffCorrectionAttemptTests(unittest.TestCase):
    def write_events(self, path: Path, records: list[dict[str, object]]) -> None:
        path.write_text(
            "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
        )

    def correction(self, session_id: str) -> dict[str, object]:
        return {
            "name": events.TURN_HANDOFF_CORRECTION_EVENT,
            "payload": {"session_id": session_id},
        }

    def test_missing_event_log_starts_at_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                1, runner._handoff_correction_attempt(Path(tmp) / "none.jsonl", "1")
            )

    def test_prior_corrections_for_the_session_advance_the_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write_events(
                path,
                [
                    self.correction("1"),
                    {"name": "turn_finished", "payload": {"session_id": "1"}},
                    self.correction("1"),
                ],
            )
            self.assertEqual(3, runner._handoff_correction_attempt(path, "1"))

    def test_other_sessions_do_not_consume_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write_events(path, [self.correction("2"), self.correction("3")])
            self.assertEqual(1, runner._handoff_correction_attempt(path, "1"))

    def test_legacy_session_key_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write_events(
                path,
                [
                    {
                        "name": events.TURN_HANDOFF_CORRECTION_EVENT,
                        "payload": {"session": "1"},
                    }
                ],
            )
            self.assertEqual(2, runner._handoff_correction_attempt(path, "1"))

    def test_corrupt_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                f"not json\n\n{json.dumps(self.correction('1'))}\n[]\n",
                encoding="utf-8",
            )
            self.assertEqual(2, runner._handoff_correction_attempt(path, "1"))

    def test_correction_note_names_the_defect_and_the_repair(self) -> None:
        defect = runner._HandoffDefect(
            code=runner.HANDOFF_DEFECT_MISSING_FILES,
            detail="the completed handoff carries no Files modified entry",
            instruction="list every file you changed",
        )
        plain = runner._handoff_correction_note(defect, preserved_review=None)
        self.assertIn(defect.detail, plain)
        self.assertIn(defect.instruction, plain)
        self.assertNotIn("review artifacts", plain)
        with_review = runner._handoff_correction_note(
            defect, preserved_review={"review_verdict_ok": True}
        )
        self.assertIn("review artifacts", with_review)


class CommandFailureDetailTests(unittest.TestCase):
    def check(self) -> verification.CommandCheck:
        return verification.CommandCheck(
            name="final-review-artifact-consistent",
            argv=("python3", "-c", "pass"),
            timeout_seconds=60,
        )

    def test_the_authenticated_diagnostic_leads_the_detail(self) -> None:
        """A `python3 -c` gate renders kilobytes of argv; the reason goes first.

        Every consumer that bounds an inline string cuts the tail, so an
        argv-first detail is exactly the one that loses the reason.
        """
        detail = verification._command_failure_detail(
            self.check(),
            1,
            {
                "failure_diagnostic": {
                    "check": "final-review-artifact-consistent",
                    "reason": "artifact-missing",
                }
            },
        )
        self.assertEqual(
            "final-review-artifact-consistent: artifact-missing; "
            "command exited 1: python3",
            detail,
        )

    def test_an_unauthenticated_stream_line_stays_out_of_the_detail(self) -> None:
        """The streams are application output wherever the diagnostic is absent.

        `detail` is inlined into the audit bundle, so a failed user command's
        last line must reach it through no route at all -- not on stderr, and
        not on stdout when stderr is silent.
        """
        detail = verification._command_failure_detail(
            self.check(),
            2,
            {
                "stderr": "   \n",
                "stdout": "only stdout said this\n",
                "failure_diagnostic": None,
            },
        )
        self.assertEqual("command exited 2: python3", detail)

    def test_the_authored_argv_stays_out_of_the_detail(self) -> None:
        check = verification.CommandCheck(
            name="final-review-output-evidence-based",
            argv=("python3", "-c", "x = 1  # " + "y" * 4000),
            timeout_seconds=60,
        )

        detail = verification._command_failure_detail(check, 1, {})

        self.assertEqual("command exited 1: python3", detail)

    def test_diagnostic_lead_is_truncated(self) -> None:
        """A total on the lead, independent of the parser's own vocabulary."""
        detail = verification._command_failure_detail(
            self.check(),
            1,
            {
                "failure_diagnostic": {
                    "check": "final-review-artifact-consistent",
                    "reason": "x" * 5000,
                }
            },
        )
        self.assertLessEqual(
            len(detail) - len("; command exited 1: python3"),
            verification._COMMAND_DIAGNOSTIC_LIMIT,
        )


class GeneratedReviewValidatorDiagnosticTests(unittest.TestCase):
    def run_checks(self, plan_dir: Path) -> dict[str, subprocess.CompletedProcess[str]]:
        payload = runner._generated_review_contract_payload(
            plans.parse_session_id("2"), plan_dir=plan_dir
        )
        results: dict[str, subprocess.CompletedProcess[str]] = {}
        for check in payload["checks"]:
            argv = [
                sys.executable if part == "python3" else part for part in check["argv"]
            ]
            results[check["name"]] = subprocess.run(
                argv, cwd=plan_dir, text=True, capture_output=True, check=False
            )
        return results

    def diagnostic(
        self, completed: subprocess.CompletedProcess[str]
    ) -> dict[str, object]:
        self.assertEqual(1, completed.returncode)
        lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertTrue(lines, "validator failed without a diagnostic line")
        return json.loads(lines[-1])

    def test_valid_review_passes_both_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\n## Findings\n\n"
                "Read agent_session_loop/runner.py, agent_session_loop/plans.py "
                "and agent_session_loop/review.py.\n\n"
                f"{review.REVIEW_STATUS_ALL_CLEAR}\n",
                encoding="utf-8",
            )
            for name, completed in self.run_checks(plan_dir).items():
                self.assertEqual(0, completed.returncode, f"{name}: {completed.stderr}")

    def test_output_gate_reports_its_rejecting_predicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\nNo heading of interest.\n", encoding="utf-8"
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-output-evidence-based"]
            )
            self.assertEqual("final-review-output-evidence-based", payload["check"])
            self.assertEqual("missing-required-heading", payload["reason"])

    def test_output_gate_reports_thin_path_evidence_with_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_output_path(plan_dir).write_text(
                "# Final review\n\n## Findings\n\nSaw agent_session_loop/runner.py.\n\n"
                f"{review.REVIEW_STATUS_ALL_CLEAR}\n",
                encoding="utf-8",
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-output-evidence-based"]
            )
            self.assertEqual("insufficient-path-evidence", payload["reason"])
            self.assertEqual(1, payload["path_reference_count"])
            self.assertEqual(3, payload["required"])

    def test_artifact_gate_reports_an_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_artifact_path(plan_dir).write_text(
                json.dumps({"version": 1, "outcome": "all-clear", "findings": []}),
                encoding="utf-8",
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )
            self.assertEqual("final-review-artifact-consistent", payload["check"])
            self.assertEqual("artifact-version-unsupported", payload["reason"])
            self.assertEqual(1, payload["version"])
            self.assertEqual(review.REVIEW_ARTIFACT_VERSION, payload["expected"])

    def test_artifact_gate_reports_the_offending_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_artifact_path(plan_dir).write_text(
                json.dumps(
                    {
                        "version": review.REVIEW_ARTIFACT_VERSION,
                        "outcome": "findings",
                        "findings": [{"id": "F1", "severity": "high"}],
                    }
                ),
                encoding="utf-8",
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )
            self.assertEqual("finding-missing-keys", payload["reason"])
            self.assertEqual(0, payload["index"])
            self.assertEqual("F1", payload["finding_id"])
            self.assertIn("title", payload["missing"])

    def test_artifact_gate_reports_unparsable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            review.final_review_artifact_path(plan_dir).write_text(
                "{not json", encoding="utf-8"
            )
            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )
            self.assertEqual("artifact-unparsable", payload["reason"])
            self.assertTrue(payload["error"])

    def finding_artifact(self, plan_dir: Path, checks: list[dict[str, object]]) -> None:
        review.final_review_output_path(plan_dir).write_text(
            "# Final review\n\n## Findings\n\nSee agent_session_loop/runner.py.\n\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        review.final_review_artifact_path(plan_dir).write_text(
            json.dumps(
                {
                    "version": review.REVIEW_ARTIFACT_VERSION,
                    "outcome": "findings",
                    "findings": [
                        {
                            "id": "F1",
                            "severity": "high",
                            "title": "Absence gate",
                            "evidence": [{"path": "agent_session_loop/runner.py"}],
                            "explanation": "why",
                            "repair_group": "g1",
                            "done_criteria": ["fixed"],
                            "verification_checks": checks,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_artifact_gate_rejects_an_absence_gate_the_review_authored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            self.finding_artifact(
                plan_dir,
                [
                    {
                        "type": "regex_in_file",
                        "name": "drops-the-claim",
                        "path": "docs/evidence.md",
                        "pattern": "75-pixel",
                        "expect_absent": True,
                    }
                ],
            )

            payload = self.diagnostic(
                self.run_checks(plan_dir)["final-review-artifact-consistent"]
            )

            self.assertEqual("verification-check-unknown-keys", payload["reason"])
            self.assertEqual("F1", payload["finding_id"])
            self.assertEqual("drops-the-claim", payload["check_name"])
            self.assertEqual(["expect_absent"], payload["unknown"])
            self.assertEqual(
                contracts.CHECK_PRESENCE_SEMANTICS["regex_in_file"],
                payload["semantics"],
            )

    def test_artifact_gate_leaves_an_unknown_check_type_to_deep_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            self.finding_artifact(
                plan_dir, [{"type": "bogus", "name": "not-a-check", "nonsense": 1}]
            )

            completed = self.run_checks(plan_dir)["final-review-artifact-consistent"]

            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_repository_aware_gate_keeps_the_severity_predicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            payload = runner._generated_review_contract_payload(
                plans.parse_session_id("2"),
                plan_dir=plan_dir,
                repository_aliases=("SVC", "OAK"),
            )
            source = payload["checks"][1]["argv"][2]
            self.assertIn(
                f"EXPECTED_VERSION = {review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION}",
                source,
            )
            self.assertIn("finding-severity-invalid", source)
            self.assertIn("finding-repo-unknown", source)
            # The reported expectation reads the same anchor the widening
            # rewrote, so it cannot drift back to the unaware version.
            self.assertIn("expected=EXPECTED_VERSION", source)
            self.assertNotIn(
                f"EXPECTED_VERSION = {review.REVIEW_ARTIFACT_VERSION}", source
            )


class BroadReviewGateTests(unittest.TestCase):
    def contract(self, session: str, *checks: dict[str, object]) -> str:
        return "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- the work is done",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps({"session": session, "checks": list(checks)}),
                "```",
            ]
        )

    def write_plan(
        self,
        plan_dir: Path,
        contracts_by_id: dict[str, str],
        *,
        prior_review: bool,
    ) -> plans.SessionId:
        write_plan_with_ids(
            plan_dir,
            tuple((session, "complete") for session in sorted(contracts_by_id)),
            contracts_by_id=contracts_by_id,
        )
        if prior_review:
            prior_id = plans.next_top_level_session_id(plan_dir)
            (plan_dir / prior_id.filename).write_text(
                runner.build_generated_review_session_body(
                    prior_id, review_harness="codex", plan_dir=plan_dir
                ),
                encoding="utf-8",
            )
        return plans.next_top_level_session_id(plan_dir)

    def review_checks(
        self,
        contracts_by_id: dict[str, str],
        *,
        prior_review: bool = False,
    ) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            review_id = self.write_plan(
                plan_dir, contracts_by_id, prior_review=prior_review
            )
            payload = runner._generated_review_contract_payload(
                review_id, plan_dir=plan_dir
            )
        return list(payload["checks"])

    def replayed(self, checks: list[dict[str, Any]]) -> list[str]:
        return [
            str(check["name"])
            for check in checks
            if str(check["name"]).startswith(
                f"{runner.BROAD_REVIEW_CHECK_PREFIX}:",
            )
        ]

    def test_the_plans_own_gates_are_replayed_after_the_validators(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "lint",
                        "argv": ["true"],
                        "review_replay": True,
                    },
                    {
                        "type": "file_exists",
                        "name": "readme",
                        "path": "README.md",
                        "review_replay": True,
                    },
                )
            }
        )

        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
                "broad:1:lint",
                "broad:1:readme",
            ],
            [check["name"] for check in checks],
        )
        self.assertEqual(["true"], checks[2]["argv"])
        self.assertEqual("README.md", checks[3]["path"])

    def test_one_command_declared_twice_is_replayed_once(self) -> None:
        recipe: dict[str, object] = {
            "type": "command",
            "argv": ["python3", "-c", "pass"],
            "review_replay": True,
        }
        checks = self.review_checks(
            {
                "1": self.contract("1", {**recipe, "name": "suite"}),
                "2": self.contract("2", {**recipe, "name": "suite-again"}),
            }
        )

        self.assertEqual(["broad:1:suite"], self.replayed(checks))

    def test_a_diagnostic_is_not_replayed_as_a_gate(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "suite",
                        "argv": ["true"],
                        "review_replay": True,
                    },
                    {
                        "type": "command",
                        "name": "suite-detail",
                        "argv": ["false"],
                        "on_failure_of": ["suite"],
                    },
                )
            }
        )

        # A diagnostic never decides whether verification passed, so replaying
        # it would buy cost without buying a gate.
        self.assertEqual(["broad:1:suite"], self.replayed(checks))

    def test_a_prior_reviews_own_validators_are_not_replayed(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "suite",
                        "argv": ["true"],
                        "review_replay": True,
                    },
                )
            },
            prior_review=True,
        )

        self.assertEqual(["broad:1:suite"], self.replayed(checks))

    def test_a_baseline_expectation_does_not_travel_with_the_replay(self) -> None:
        checks = self.review_checks(
            {
                "1": self.contract(
                    "1",
                    {
                        "type": "command",
                        "name": "suite",
                        "argv": ["true"],
                        "baseline_expectation": "fail",
                        "review_replay": True,
                    },
                )
            }
        )

        # The expectation describes the workspace the plan started from, which
        # says nothing about the one the repairs produced.
        self.assertNotIn("baseline_expectation", checks[-1])
        self.assertNotIn("review_replay", checks[-1])

    def test_the_replayed_recipe_is_a_readable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            review_id = self.write_plan(
                plan_dir,
                {
                    "1": self.contract(
                        "1",
                        {
                            "type": "command",
                            "name": "suite",
                            "argv": ["true"],
                            "review_replay": True,
                        },
                    ),
                    "2": self.contract(
                        "2",
                        {
                            "type": "command",
                            "name": "suite",
                            "argv": ["false"],
                            "review_replay": True,
                        },
                    ),
                },
                prior_review=False,
            )
            (plan_dir / review_id.filename).write_text(
                runner.build_generated_review_session_body(
                    review_id, review_harness="codex", plan_dir=plan_dir
                ),
                encoding="utf-8",
            )
            contract = contracts.read_completion_contract(plan_dir, review_id)

        assert contract is not None
        # Two sessions declaring the same check name still parse: the source
        # session qualifies each replayed name, so the recipe has no duplicate.
        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
                "broad:1:suite",
                "broad:2:suite",
            ],
            [check.name for check in contract.verify.checks],
        )


class ReviewArtifactProjectionTests(unittest.TestCase):
    def review_session_text(self, plan_dir: Path, files_modified: str) -> str:
        body = runner.build_generated_review_session_body(
            plans.parse_session_id("2"), review_harness="codex", plan_dir=plan_dir
        ).replace("- **Status**: pending", "- **Status**: complete")
        return re.sub(
            r"- \*\*Files modified\*\*: .*",
            f"- **Files modified**: {files_modified}",
            body,
            count=1,
        )

    def project(self, files_modified: str) -> plans._ReviewArtifactProjection:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir(parents=True)
            text = self.review_session_text(plan_dir, files_modified)
            return plans._project_generated_review_handoff_artifacts(text)

    def files_line(self, text: str) -> str:
        for line in text.splitlines():
            if line.startswith(plans.HANDOFF_FILES_PREFIX):
                return line[len(plans.HANDOFF_FILES_PREFIX) :].strip()
        raise AssertionError("no Files modified line")

    def test_review_only_entries_project_to_none_and_are_exclusive(self) -> None:
        projection = self.project(
            f"{review.FINAL_REVIEW_FILENAME}, {review.FINAL_REVIEW_ARTIFACT_FILENAME}"
        )
        self.assertTrue(projection.exclusive)
        self.assertEqual("(none)", self.files_line(projection.text))

    def test_mixed_entries_keep_the_repository_path_and_are_not_exclusive(self) -> None:
        projection = self.project(f"{review.FINAL_REVIEW_FILENAME}, README.md")
        self.assertFalse(projection.exclusive)
        self.assertEqual("README.md", self.files_line(projection.text))

    def test_repository_only_entries_are_left_untouched(self) -> None:
        projection = self.project("README.md")
        self.assertFalse(projection.exclusive)
        self.assertEqual("README.md", self.files_line(projection.text))

    def test_dot_prefixed_review_entries_are_recognized(self) -> None:
        projection = self.project(f"./{review.FINAL_REVIEW_FILENAME}, README.md")
        self.assertEqual("README.md", self.files_line(projection.text))

    def test_mixed_handoff_is_never_rewritten_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("complete", "complete"))
            session_path = plan_dir / "session-2.md"
            session_path.write_text(
                self.review_session_text(
                    plan_dir, f"{review.FINAL_REVIEW_FILENAME}, README.md"
                ),
                encoding="utf-8",
            )
            before = session_path.read_text(encoding="utf-8")
            changed = plans.normalize_generated_review_handoff_artifacts(plan_dir)
            self.assertEqual((), changed)
            self.assertEqual(before, session_path.read_text(encoding="utf-8"))

    def test_review_only_handoff_is_normalized_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_multi_repo_plan(plan_dir, ("complete", "complete"))
            session_path = plan_dir / "session-2.md"
            session_path.write_text(
                self.review_session_text(plan_dir, review.FINAL_REVIEW_FILENAME),
                encoding="utf-8",
            )
            changed = plans.normalize_generated_review_handoff_artifacts(plan_dir)
            self.assertEqual(["session-2.md"], [path.name for path in changed])
            self.assertEqual(
                "(none)", self.files_line(session_path.read_text(encoding="utf-8"))
            )


if __name__ == "__main__":
    unittest.main()
