"""Agent audit engine answer validation, prompts, and archived attempts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    repository_evidence,
    supervisor_audit,
    supervisor_audit_agent,
    supervisor_audit_checks,
)
from agent_session_loop.assurance_models import (
    RUBRIC_LANES,
)
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import FIXED_CLOCK, RUN_ID, ScriptedAuditor


class AgentAuditEngineTests(checks_support.AgentAuditEngineFixture):
    """The semantic auditor is validated strictly and never trusted as output."""

    def _reports(self, run_dir: Path, audit: object) -> tuple[dict[str, Any], str]:
        report, markdown = supervisor_audit_agent.write_audit_reports(
            run_dir, audit, durable=False
        )
        return (
            json.loads(report.read_text(encoding="utf-8")),
            markdown.read_text(encoding="utf-8"),
        )

    def test_a_localized_finding_names_its_source_in_both_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            localization=[
                                "package/runner.py:8129",
                                "skills/asl/SKILL.md",
                            ],
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)
            report, markdown = self._reports(history[3], outcome.audit)

            self.assertEqual(
                ("package/runner.py:8129", "skills/asl/SKILL.md"),
                outcome.audit.findings[0].localization,
            )
            self.assertEqual(
                ["package/runner.py:8129", "skills/asl/SKILL.md"],
                report["findings"][0]["localization"],
            )
            self.assertIn("`package/runner.py:8129`", markdown)

    def test_a_localization_outside_the_source_roots_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [self._finding(locator, localization=["workspace/app/main.py:12"])],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("invalid-reference", outcome.audit.reason)
            self.assertIn("outside the read-only source roots", auditor.prompts[1])

    def test_evidence_the_bundle_never_declared_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor(
                self._reply(self._lanes("run/invented-by-the-agent.json"))
            )

            outcome = self._run(history, auditor)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("invalid-reference", outcome.audit.reason)
            self.assertEqual(
                ["invalid-reference", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )

    def test_an_answer_missing_a_rubric_lane_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator)[:3])
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("missing-lanes", outcome.audit.reason)
            self.assertEqual("missing-lanes", outcome.attempts[0].outcome)

    def test_a_lane_the_agent_could_not_judge_is_never_all_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(
                        bundle.evidence[0].locator, {"resources-stalls": "unavailable"}
                    )
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertTrue(outcome.agent_covered)
            self.assertEqual("partial", outcome.audit.state)
            self.assertEqual(
                supervisor_audit_agent.REASON_LANE_EVIDENCE_ABSENT, outcome.audit.reason
            )
            self.assertEqual("unavailable", statuses["resources-stalls"])
            self.assertEqual((), outcome.audit.findings)

    def test_a_lane_the_agent_failed_is_reported_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(
                        bundle.evidence[0].locator, {"resources-stalls": "fail"}
                    )
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            # A failing lane with no finding to explain it is not a verdict the
            # audit can stand behind, so it degrades rather than reading partial.
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                supervisor_audit_agent.REASON_LANE_COVERAGE, outcome.audit.reason
            )

    def test_a_legacy_plan_without_a_registry_binding_can_be_all_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            repository = self._make_legacy_plan(history)
            bundle = self._bundle(history, workspace_root=repository)
            auditor = ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(
                history,
                auditor,
                bundle=bundle,
                workspace_root=repository,
                repositories=(),
            )
            checks = {
                result.check: result.status for result in outcome.audit.deterministic
            }
            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}

            # The incident this pins: a check that could not read its evidence
            # used to drag a lane the agent had judged down to `unavailable`,
            # so no legacy plan could ever earn a clean audit.
            self.assertEqual("unavailable", checks["root-registry-agreement"])
            self.assertEqual("pass", statuses["identity-authority"])
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertIsNone(outcome.audit.reason)

    def test_an_agent_pass_never_overrules_a_deterministic_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(history, auditor, bundle=bundle)

            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertEqual("fail", statuses["verification-review-independence"])
            self.assertEqual("findings", outcome.audit.state)

    def test_no_agent_markdown_reaches_the_operator_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            lanes = self._lanes(bundle.evidence[0].locator)
            lanes[0]["summary"] = "table | breaker\nwith a newline"
            auditor = ScriptedAuditor(
                self._reply(
                    lanes, prose="## Trust me instead\n\nI rewrote the run.\n\n"
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)
            _, markdown = self._reports(history[3], outcome.audit)

            self.assertNotIn("Trust me instead", markdown)
            self.assertNotIn("I rewrote the run.", markdown)
            self.assertIn("table \\| breaker with a newline", markdown)

    def test_the_prompt_orders_evidence_before_any_source_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(self._history(Path(tmp)))

            prompt = supervisor_audit_agent.build_audit_prompt(
                bundle, catalog=supervisor_audit_agent.EMPTY_AGENT_CATALOG
            )

            self.assertLess(
                prompt.index("Analyze the operational evidence below first"),
                prompt.index("read ASL source to localize it"),
            )
            self.assertIn("You are not reviewing the application", prompt)
            self.assertIn(
                "Reviewing, grading, or commenting on the application", prompt
            )
            self.assertIn("Assigning issue IDs, fingerprints", prompt)
            self.assertIn("Editing, creating, or deleting any file", prompt)
            self.assertIn(
                "refused or paused before launching an unauthorized turn", prompt
            )
            self.assertIn("registry-free legacy plan confined", prompt)
            self.assertIn("`model_projection` is authoritative", prompt)
            self.assertIn("An unresolved", prompt)
            self.assertIn("requested default beside a resolved", prompt)
            self.assertIn("`findings-defer-broad-failures`", prompt)
            self.assertIn("later review reaches all-clear", prompt)
            self.assertIn("paused or still in-flight run is provisional", prompt)
            self.assertIn("`required-checks-incomplete` preserves", prompt)
            self.assertIn("A pause never excuses malformed acceptance", prompt)
            for lane in RUBRIC_LANES:
                self.assertIn(f"- {lane}", prompt)
            for root in supervisor_audit_agent.LOCALIZATION_ROOTS:
                self.assertIn(f"`{root}`", prompt)

    def test_identical_inputs_produce_identical_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            declared = [entry.locator for entry in bundle.evidence]
            results = supervisor_audit_checks.run_deterministic_checks(bundle)
            reply = self._reply(
                self._lanes(declared[0], {"identity-authority": "fail"}),
                [self._finding(declared[0])],
            )
            rendered = []
            for index in range(2):
                directory = Path(tmp) / f"render-{index}"
                directory.mkdir()
                audit = supervisor_audit_agent.render_supervisor_audit(
                    run_id=RUN_ID,
                    bundle=bundle,
                    results=results,
                    generated_at=FIXED_CLOCK.isoformat(),
                    agent=supervisor_audit_agent.parse_agent_audit(
                        reply,
                        declared=declared,
                        catalog=supervisor_audit_agent.EMPTY_AGENT_CATALOG,
                    ),
                )
                report, markdown = supervisor_audit_agent.write_audit_reports(
                    directory, audit, durable=False
                )
                rendered.append((report.read_bytes(), markdown.read_bytes()))

            self.assertEqual(rendered[0], rendered[1])

    def test_the_audit_leaves_the_workspace_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, plan_dir, _, run_dir = history
            before = repository_evidence.capture_workspace_snapshot(workspace, plan_dir)
            auditor = ScriptedAuditor("not an audit")

            self._run(history, auditor)
            comparison = repository_evidence.compare_workspace_snapshots(
                before,
                repository_evidence.capture_workspace_snapshot(workspace, plan_dir),
            )

            self.assertEqual({}, dict(comparison["changed"]))
            self.assertEqual([], comparison["added"])
            self.assertEqual([], comparison["removed"])
            self.assertFalse(
                (run_dir / supervisor_audit_checks.MUTATION_ARTIFACT_NAME).exists()
            )

    def test_every_attempt_is_archived_without_inlining_the_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            run_dir = history[3]
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                "not json", self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(history, auditor, bundle=bundle)

            for attempt in outcome.attempts:
                directory = run_dir / str(attempt.artifact_dir)
                self.assertTrue(
                    (directory / supervisor_audit.ATTEMPT_PROMPT_FILE).is_file()
                )
                self.assertTrue(
                    (directory / supervisor_audit.ATTEMPT_MESSAGE_FILE).is_file()
                )
                recorded = json.loads(
                    (directory / supervisor_audit.ATTEMPT_OUTCOME_FILE).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(attempt.outcome, recorded["outcome"])
                self.assertFalse(
                    any("Operational evidence" in item for item in recorded["argv"])
                )

    def test_every_rubric_lane_owns_at_least_one_check(self) -> None:
        covered = {
            spec.lane for spec in supervisor_audit_checks.DETERMINISTIC_CHECK_SPECS
        }

        self.assertEqual(set(RUBRIC_LANES), covered)


if __name__ == "__main__":
    unittest.main()
