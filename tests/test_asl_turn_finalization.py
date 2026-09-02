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

from agent_session_loop import (
    assurance_models,
    contracts,
    events,
    pause_policy,
    plans,
    review,
    review_continuation,
    review_session_contract,
    runner,
    supervisor_audit,
    verification,
)

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
        finalize = source.index("turn_executor.finalize(")
        finished = source.index("turn_executor.commit(")
        self.assertLess(finalize, finished)

    def test_turn_result_is_written_durably(self) -> None:
        source = inspect.getsource(runner._write_turn_artifacts)
        self.assertIn(
            'durability.write_json_file(paths["result"], payload, durable=True)', source
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
        payload = review_session_contract._generated_review_contract_payload(
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
            payload = review_session_contract._generated_review_contract_payload(
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
            payload = review_session_contract._generated_review_contract_payload(
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


class TerminalAdjudicationTests(unittest.TestCase):
    """The one gate every terminal path is closed through (ASI-133).

    Each test states an ending a run could reach and asks the adjudicator
    whether the success that ending claimed was earned. The evidence lives in
    the append-only event log, so the same questions are answered identically by
    the run that opened a blocker and by any later run resumed over it.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = Path(tmp.name) / "events.jsonl"
        self.persistence = runner._RunPersistence(
            write_state=False, append_events=False
        )

    def append(self, name: str, payload: dict[str, Any]) -> None:
        events.append_event(self.event_path, events.Event(name=name, payload=payload))

    def record_deferral(
        self,
        *,
        session_id: str = "2",
        run_id: str = "run-1",
        checks: tuple[str, ...] = ("broad:1:suite",),
        acceptance: dict[str, Any] | None = None,
    ) -> None:
        """Record a review turn that failed its broad gates and deferred them."""
        self.append(
            events.VERIFICATION_FINISHED_EVENT,
            {
                "run_id": run_id,
                "turn_index": 1,
                "session": session_id,
                "session_id": session_id,
                "passed": False,
                "reason": "checks-failed",
                "acceptance": (
                    {
                        "accepted": True,
                        "reason": runner.REVIEW_BROAD_FAILURES_DEFERRED_REASON,
                        "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                        "deferred_failure_count": len(checks),
                        "deferred_failure_names": list(checks),
                    }
                    if acceptance is None
                    else acceptance
                ),
            },
        )

    def record_cycle(
        self,
        *,
        cycle: int,
        outcome: str,
        disposition: str,
        session_id: str = "2",
    ) -> None:
        self.append(
            events.REVIEW_CYCLE_EVENT,
            {
                "cycle": cycle,
                "outcome": outcome,
                "disposition": disposition,
                "review_session_id": session_id,
                "repair_session_ids": [],
                "fingerprints": [],
            },
        )

    def clear_review(self) -> dict[str, Any]:
        return {
            "review_requested": True,
            "review_completed": True,
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            ),
            "review_verdict": {
                "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                "total_findings": 0,
            },
        }

    def state(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "paused": False,
            "reason": "all-complete",
            "turns": [],
            "review_requested": False,
        }
        payload.update(overrides)
        return payload

    def adjudicate(self, payload: dict[str, Any]) -> runner._TerminalAdjudication:
        return runner._adjudicate_terminal_run(
            event_path=self.event_path,
            payload=payload,
            persistence=self.persistence,
        )

    def test_an_unredeemed_deferral_blocks_the_success_it_promised(self) -> None:
        # ASI-133: a findings review may finish with the plan's own gates
        # failing, but only against a later all-clear. Without one, the run
        # holds a recorded required-check failure and may not report success —
        # not even behind a clear review, which is the narrower claim.
        self.record_deferral(checks=("broad:1:suite", "broad:1:readme"))
        payload = self.state(**self.clear_review())

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON, adjudication.blocker.reason
        )
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON, payload["pause_reason"]
        )
        unresolved = payload["pause_details"]["deferred_unresolved"]
        self.assertEqual(["broad:1:suite", "broad:1:readme"], unresolved[0]["checks"])
        self.assertEqual("2", unresolved[0]["session_id"])
        self.assertEqual("run-1", unresolved[0]["run_id"])
        # The claim the run arrived with is preserved beside the refusal.
        self.assertEqual(
            {"claimed_ok": True, "claimed_reason": "all-complete", "blocked": True},
            {
                key: payload["terminal_adjudication"][key]
                for key in ("claimed_ok", "claimed_reason", "blocked")
            },
        )

    def test_a_later_all_clear_cycle_redeems_the_deferral(self) -> None:
        self.record_deferral()
        self.record_cycle(
            cycle=1,
            outcome=review.REVIEW_OUTCOME_FINDINGS,
            disposition=runner.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        )
        self.record_cycle(
            cycle=2,
            outcome=review.REVIEW_OUTCOME_ALL_CLEAR,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            session_id="3",
        )
        payload = self.state(**self.clear_review())

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_a_deferral_that_never_bought_repairs_is_never_redeemed(self) -> None:
        # A cycle the cap exhausted materialized no repairs, so its broad
        # failures were never owed to anything; a later all-clear over a
        # different review cannot retroactively discharge them.
        self.record_deferral()
        self.record_cycle(
            cycle=1,
            outcome=review.REVIEW_OUTCOME_FINDINGS,
            disposition=runner.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
        )
        self.record_cycle(
            cycle=2,
            outcome=review.REVIEW_OUTCOME_ALL_CLEAR,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            session_id="3",
        )

        payload = self.state(**self.clear_review())
        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON, adjudication.blocker.reason
        )

    def test_an_operator_requested_stop_cannot_strand_a_deferral_green(self) -> None:
        # The exact ASI-133 residual: the loop stops for a reason nobody
        # disputes, and the stop is what keeps the redeeming review from ever
        # running. The ending is legitimate; the success is not.
        self.record_deferral()

        payload = self.state(reason="stopped")
        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual("stopped", adjudication.claimed_reason)
        self.assertEqual(
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON, payload["pause_reason"]
        )

    def test_an_unverifiable_acceptance_is_not_evidence_of_a_deferral(self) -> None:
        # A run resumed over an older event log reads acceptances a previous
        # build wrote. One whose shape cannot be checked proves nothing, so the
        # failure under it is an ordinary unpassed check rather than a deferral.
        payload = self.state(
            turns=[
                {
                    "session_id": "2",
                    "turn_index": 1,
                    "verification": {
                        "session": "2",
                        "passed": False,
                        "reason": "checks-failed",
                        "acceptance": {
                            "accepted": True,
                            "reason": runner.REVIEW_BROAD_FAILURES_DEFERRED_REASON,
                            "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                            "deferred_failure_count": 2,
                            "deferred_failure_names": ["broad:1:suite"],
                        },
                    },
                }
            ],
        )

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON, adjudication.blocker.reason
        )
        self.assertEqual(
            [{"session_id": "2", "turn_index": 1, "reason": "checks-failed"}],
            payload["pause_details"]["unpassed_verifications"],
        )

    def test_a_plan_called_complete_needs_a_review_that_came_back_clear(self) -> None:
        payload = self.state(
            review_requested=True,
            review_completed=False,
            review_skipped_reason="harness-unavailable",
        )

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertEqual(runner.REVIEW_NOT_CLEAR_REASON, adjudication.blocker.reason)
        self.assertEqual(runner.REVIEW_NOT_CLEAR_REASON, payload["pause_reason"])
        self.assertEqual(
            "harness-unavailable", payload["pause_details"]["review_skipped_reason"]
        )

    def test_open_findings_are_not_a_clear_review(self) -> None:
        payload = self.state(
            review_requested=True,
            review_completed=True,
            review_fix_disposition=runner.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            review_verdict={
                "outcome": review.REVIEW_OUTCOME_FINDINGS,
                "total_findings": 3,
                "findings_by_severity": {"high": 1, "med": 2, "low": 0},
            },
        )

        adjudication = self.adjudicate(payload)

        self.assertEqual(runner.REVIEW_NOT_CLEAR_REASON, adjudication.blocker.reason)
        self.assertEqual(3, payload["pause_details"]["unresolved_findings"])
        self.assertEqual(
            review.REVIEW_OUTCOME_FINDINGS, payload["pause_details"]["review_outcome"]
        )

    def test_an_early_exit_is_never_asked_for_a_final_review(self) -> None:
        # ``stopped`` and ``max-sessions`` report the exit an operator asked
        # for. Neither claims the plan is finished, so neither owes a review.
        for reason in ("stopped", "max-sessions"):
            with self.subTest(reason=reason):
                payload = self.state(
                    reason=reason, review_requested=True, review_completed=False
                )

                adjudication = self.adjudicate(payload)

                self.assertFalse(adjudication.blocked)
                self.assertTrue(payload["ok"])
                self.assertEqual(reason, payload["reason"])

    def test_a_plan_that_asked_for_no_review_completes_without_one(self) -> None:
        payload = self.state(review_requested=False)

        self.assertFalse(self.adjudicate(payload).blocked)
        self.assertTrue(payload["ok"])

    def test_a_run_that_already_paused_keeps_its_own_blocker(self) -> None:
        # The specific pause is what an operator needs; restating it as a
        # generic adjudication verdict would lose the evidence attached to it.
        self.record_deferral()
        payload = self.state(
            ok=False,
            paused=True,
            reason="verify-failed",
            pause_reason="verify-failed",
        )

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertFalse(adjudication.claimed_ok)
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertEqual("verify-failed", payload["reason"])

    def test_a_failed_run_still_cannot_strand_an_unredeemed_deferral(self) -> None:
        # Failing is not the same claim as pausing. A run that reports
        # unresolved findings has a repair loop that never converged, and a
        # resume can converge it — so the deferral it still owes belongs in a
        # durable blocker rather than behind a plain failure nobody resumes.
        self.record_deferral()
        payload = self.state(
            ok=False,
            paused=False,
            reason=runner.REVIEW_FINDINGS_UNRESOLVED_REASON,
            action_required={"reason": runner.REVIEW_FINDINGS_UNRESOLVED_REASON},
        )

        adjudication = self.adjudicate(payload)

        self.assertTrue(adjudication.blocked)
        self.assertFalse(adjudication.claimed_ok)
        self.assertEqual(
            runner.REVIEW_FINDINGS_UNRESOLVED_REASON, adjudication.claimed_reason
        )
        self.assertEqual(
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON, payload["pause_reason"]
        )
        # The narrower reason the review path recorded is not lost to the pause.
        self.assertEqual(
            runner.REVIEW_FINDINGS_UNRESOLVED_REASON,
            payload["action_required"]["reason"],
        )

    def test_a_failed_run_owing_nothing_keeps_the_ending_it_reached(self) -> None:
        # The review clause is scoped to endings that assert the plan finished,
        # and this one asserts the opposite, so nothing here is adjudicable.
        payload = self.state(
            ok=False,
            paused=False,
            reason=runner.REVIEW_FINDINGS_UNRESOLVED_REASON,
            review_requested=True,
            review_completed=True,
        )

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertFalse(payload["paused"])
        self.assertEqual(runner.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"])

    def test_a_dry_run_claimed_nothing_and_is_not_adjudicated(self) -> None:
        self.record_deferral()
        payload = self.state(reason="dry-run", review_requested=True)

        adjudication = self.adjudicate(payload)

        self.assertFalse(adjudication.blocked)
        self.assertTrue(payload["ok"])
        self.assertEqual("dry-run", payload["reason"])

    def test_an_earned_success_records_the_verdict_that_tested_it(self) -> None:
        payload = self.state(**self.clear_review())

        self.adjudicate(payload)

        self.assertEqual(
            {
                "claimed_ok": True,
                "claimed_reason": "all-complete",
                "blocked": False,
                "reason": None,
                "details": {},
            },
            payload["terminal_adjudication"],
        )

    def test_an_adjudicated_blocker_routes_the_operator_to_an_inspection(self) -> None:
        # No mailbox answer makes a failing gate pass, so both reasons have to
        # classify as inspections rather than burning a reply lease first.
        for reason in (
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON,
            runner.REVIEW_NOT_CLEAR_REASON,
        ):
            with self.subTest(reason=reason):
                policy = pause_policy.classify_pause(reason)

                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT, policy["user_action"]
                )
                self.assertEqual(0, policy["max_retries"])

    def test_the_blocker_reaches_the_payload_with_an_action_and_a_policy(self) -> None:
        self.record_deferral()
        payload = self.state()

        self.adjudicate(payload)

        self.assertEqual(
            runner.REQUIRED_CHECKS_INCOMPLETE_REASON, payload["pause_policy"]["reason"]
        )
        action = payload["action_required"]
        self.assertEqual(runner.REQUIRED_CHECKS_INCOMPLETE_REASON, action["reason"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, action["user_action"])


class DegradedAssuranceTests(unittest.TestCase):
    """Optional assurance may fail to speak, but it may not fail silently."""

    def setUp(self) -> None:
        self.persistence = runner._RunPersistence(
            write_state=False, append_events=False
        )

    def assurance(
        self,
        state: str,
        reason: str | None,
        *,
        issue_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return assurance_models.AssuranceSummary(
            run_id="20260619T150000000000Z-4242",
            state=state,
            eligible=True,
            reason=reason,
            issue_ids=issue_ids,
            new_issue_count=len(issue_ids),
            artifacts=(
                assurance_models.EvidenceRef(
                    kind="state", locator="state/state.json", detail="run state"
                ),
            ),
        ).to_dict()

    def record(self, payload: dict[str, Any]) -> None:
        runner._record_degraded_assurance(payload, persistence=self.persistence)

    def test_an_audit_that_could_not_speak_degrades_a_success(self) -> None:
        payload = {
            "ok": True,
            "paused": False,
            "reason": "all-complete",
            "assurance": self.assurance(
                "unavailable", supervisor_audit.REASON_NO_INDEPENDENT_ROLE
            ),
        }

        self.record(payload)

        degraded = payload["assurance_degraded"]
        self.assertEqual("unavailable", degraded["assurance_state"])
        self.assertEqual(
            supervisor_audit.REASON_NO_INDEPENDENT_ROLE, degraded["reason"]
        )
        self.assertFalse(degraded["blocking"])
        self.assertTrue(degraded["run_ok"])
        self.assertEqual(["state/state.json"], degraded["evidence"])
        # Advisory means advisory: the run keeps the success it earned.
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_a_partial_audit_is_degraded_too(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance": self.assurance("partial", "attempts-exhausted"),
        }

        self.record(payload)

        self.assertEqual("partial", payload["assurance_degraded"]["assurance_state"])

    def test_a_settled_skip_is_not_a_degradation(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance": self.assurance("skipped", supervisor_audit.REASON_OPTED_OUT),
        }

        self.record(payload)

        self.assertNotIn("assurance_degraded", payload)

    def test_a_graded_run_carries_no_degradation_note(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance": self.assurance("all-clear", None),
        }

        self.record(payload)

        self.assertNotIn("assurance_degraded", payload)

    def test_a_stale_note_is_dropped_once_the_audit_speaks(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance_degraded": {"assurance_state": "unavailable"},
            "assurance": self.assurance("findings", None, issue_ids=("ASI-999",)),
        }

        self.record(payload)

        self.assertNotIn("assurance_degraded", payload)


if __name__ == "__main__":
    unittest.main()
