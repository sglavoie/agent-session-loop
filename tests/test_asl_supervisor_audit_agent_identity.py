"""Agent audit identity decisions and fail-closed identity handling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    assurance_ledger,
    assurance_projection,
    supervisor_audit,
    supervisor_audit_agent,
    supervisor_audit_agent_resolution,
)
from agent_session_loop.assurance_models_findings import Finding
from agent_session_loop.assurance_models_inbox import IssueObservation
from agent_session_loop.assurance_models_summary import AuditAttempt
from agent_session_loop.assurance_models_vocab import MAX_AUDIT_ATTEMPTS
from agent_session_loop.supervisor_audit_agent_contract import (
    AgentAudit,
    IdentityDecision,
)
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import RUN_ID, RunHistoryFixture, ScriptedAuditor


class AgentIdentityDecisionTests(RunHistoryFixture, unittest.TestCase):
    """Every finding states one identity, and only from the supplied catalog."""

    _lanes = checks_support.AgentAuditEngineFixture._lanes
    _finding = checks_support.AgentAuditEngineFixture._finding
    _reply = checks_support.AgentAuditEngineFixture._reply
    _role = checks_support.AgentAuditEngineFixture._role
    _run = checks_support.AgentAuditEngineFixture._run
    _self_reviewed = checks_support.AgentAuditEngineFixture._self_reviewed

    def _open_issue(self, state_dir: Path, *, invariant: str) -> str:
        """Open one canonical issue in the ledger the audit will read."""
        record = assurance_ledger.append_issue_observation(
            IssueObservation(
                run_id=RUN_ID,
                finding=Finding(
                    category="identity",
                    component="workspace-registry",
                    invariant_key=invariant,
                    lane="identity-authority",
                    severity="medium",
                    summary="The registry disagreed with the manifest.",
                    recommendation="Re-derive the registry from the manifest.",
                    evidence=("supervisor-audit-input.json",),
                ),
                observed_at="2026-08-01T00:00:00+00:00",
            ),
            state_dir=state_dir,
        )
        assert record.issue_id is not None
        return record.issue_id

    def _rejected(
        self, history: tuple[Path, Path, Path, Path], **identity: object
    ) -> AuditAttempt:
        bundle = self._bundle(history)
        locator = bundle.evidence[0].locator
        auditor = ScriptedAuditor(
            self._reply(
                self._lanes(locator, {"identity-authority": "fail"}),
                [self._finding(locator, **identity)],
            )
        )
        outcome = self._run(history, auditor, bundle=bundle)
        self.assertFalse(outcome.agent_covered)
        return outcome.attempts[0]

    def test_a_finding_without_an_identity_decision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            finding = self._finding(locator)
            del finding["identity"]
            auditor = ScriptedAuditor(self._reply(self._lanes(locator), [finding]))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("malformed-output", outcome.attempts[0].outcome)
            self.assertIn("exactly one 'identity' object", outcome.attempts[0].detail)

    def test_an_identity_decision_outside_the_two_choices_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)), identity={"decision": "merge"}
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("existing, new", attempt.detail)

    def test_an_identity_object_with_an_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)),
                identity={"decision": "new", "confidence": 0.9},
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("unknown key(s) confidence", attempt.detail)

    def test_a_new_allocation_request_may_not_name_an_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)),
                identity={"decision": "new", "issue_id": "ASI-1"},
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("must name no 'issue_id'", attempt.detail)

    def test_a_malformed_issue_id_never_reaches_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)),
                identity={"decision": "existing", "issue_id": "the registry one"},
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("must name the 'issue_id'", attempt.detail)

    def test_an_invented_issue_id_is_refused_as_an_invalid_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._open_issue(history[2], invariant="identity.first")

            attempt = self._rejected(
                history, identity={"decision": "existing", "issue_id": "ASI-4242"}
            )

            self.assertEqual("invalid-reference", attempt.outcome)
            self.assertIn("ASI-4242", attempt.detail)
            self.assertIn("does not list", attempt.detail)

    def test_a_merged_away_issue_id_is_not_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            source = self._open_issue(history[2], invariant="identity.source")
            target = self._open_issue(history[2], invariant="identity.target")
            assurance_projection.record_merge(source, target, state_dir=history[2])

            attempt = self._rejected(
                history, identity={"decision": "existing", "issue_id": source}
            )

            self.assertEqual("invalid-reference", attempt.outcome)
            self.assertIn("merged away", attempt.detail)

    def test_a_catalog_selection_is_accepted_and_carried_with_its_finding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._open_issue(history[2], invariant="identity.known")
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": issue_id},
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertIsNotNone(outcome.agent)
            assert outcome.agent is not None
            finding, decision = outcome.agent.decisions[0]
            self.assertEqual("identity.agent-observed-drift", finding.invariant_key)
            self.assertEqual("existing", decision.decision)
            self.assertEqual(issue_id, decision.issue_id)
            self.assertFalse(decision.is_new)

    def test_a_new_allocation_request_is_carried_without_an_issue_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [self._finding(locator)],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertTrue(decision.is_new)
            self.assertIsNone(decision.issue_id)

    def test_a_v1_answer_carries_no_identity_and_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            body = json.dumps(
                {
                    "schema_version": 1,
                    "lanes": self._lanes(locator),
                    "findings": [],
                }
            )
            auditor = ScriptedAuditor(f"```json\n{body}\n```\n")

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("malformed-output", outcome.attempts[0].outcome)
            self.assertIn(
                f"schema_version {supervisor_audit_agent.AGENT_OUTPUT_SCHEMA_VERSION}",
                outcome.attempts[0].detail,
            )

    def test_an_identity_rejection_keeps_the_deterministic_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": "ASI-77"},
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual("invalid-reference", outcome.audit.reason)
            self.assertTrue(outcome.audit.findings)
            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertEqual("fail", statuses["verification-review-independence"])

    def test_the_prompt_supplies_the_catalog_with_its_digest_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._open_issue(history[2], invariant="identity.listed")
            catalog = supervisor_audit_agent.read_agent_catalog(history[2])

            prompt = supervisor_audit_agent.build_audit_prompt(
                self._bundle(history), catalog=catalog
            )

            self.assertIn(catalog.digest, prompt)
            self.assertIn(f'"issue_id":"{issue_id}"', prompt)
            self.assertIn('"omitted":0', prompt)
            self.assertLess(
                prompt.index("Every finding carries exactly one `identity` object"),
                prompt.index(catalog.digest),
            )

    def test_the_retry_repeats_the_catalog_and_the_identity_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._open_issue(history[2], invariant="identity.listed")
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": "ASI-9001"},
                        )
                    ],
                ),
                self._reply(self._lanes(locator)),
            )

            outcome = self._run(history, auditor, bundle=bundle)
            retry = auditor.prompts[1]

            self.assertTrue(outcome.agent_covered)
            self.assertIn("Your previous answer was rejected", retry)
            self.assertIn("ASI-9001", retry)
            self.assertIn(
                "An ID it does not list cannot be selected no matter where you saw it",
                retry,
            )
            self.assertIn(f'"issue_id":"{issue_id}"', retry)
            self.assertNotIn("identity.agent-observed-drift", retry)

    def test_an_unreadable_ledger_supplies_the_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            (state_dir / assurance_ledger.LEDGER_FILE_NAME).mkdir(parents=True)

            catalog = supervisor_audit_agent.read_agent_catalog(state_dir)

            self.assertEqual(supervisor_audit_agent.EMPTY_AGENT_CATALOG, catalog)
            self.assertEqual((), catalog.entries)

    def test_a_missing_ledger_is_an_empty_catalog_rather_than_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = supervisor_audit_agent.read_agent_catalog(Path(tmp) / "absent")

            self.assertEqual((), catalog.entries)
            self.assertEqual((), catalog.omitted)


class FailClosedIdentityTests(RunHistoryFixture, unittest.TestCase):
    """A renamed recurrence is asked about once, then refused rather than split."""

    _lanes = checks_support.AgentAuditEngineFixture._lanes
    _finding = checks_support.AgentAuditEngineFixture._finding
    _reply = checks_support.AgentAuditEngineFixture._reply
    _role = checks_support.AgentAuditEngineFixture._role
    _run = checks_support.AgentAuditEngineFixture._run
    _self_reviewed = checks_support.AgentAuditEngineFixture._self_reviewed

    #: The summary the agent finding carries, so an issue seeded with it is a
    #: perfect normalized-summary match and nothing else.
    SUMMARY = "The registry named a repository the manifest never declared."

    def _seed(self, state_dir: Path, **overrides: object) -> str:
        payload: dict[str, Any] = {
            "category": "identity",
            "component": "workspace-registry",
            "invariant_key": "identity.registry-drift",
            "lane": "identity-authority",
            "severity": "medium",
            "summary": self.SUMMARY,
            "recommendation": "Re-derive the registry from the manifest.",
            "evidence": ("supervisor-audit-input.json",),
        }
        payload.update(overrides)
        record = assurance_ledger.append_issue_observation(
            IssueObservation(
                run_id=RUN_ID,
                finding=Finding(**payload),
                observed_at="2026-08-01T00:00:00+00:00",
            ),
            state_dir=state_dir,
        )
        assert record.issue_id is not None
        return record.issue_id

    def _new_answer(self, locator: str) -> str:
        return self._reply(
            self._lanes(locator, {"identity-authority": "fail"}),
            [self._finding(locator)],
        )

    def test_an_exact_fingerprint_is_reused_without_spending_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._seed(
                history[2], invariant_key="identity.agent-observed-drift"
            )
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            self.assertEqual(["ok"], [item.outcome for item in outcome.attempts])
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertEqual("existing", decision.decision)
            self.assertEqual(issue_id, decision.issue_id)
            self.assertEqual("recurrence", decision.source)

    def test_a_renamed_recurrence_is_retried_with_the_candidate_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._seed(history[2])
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._new_answer(locator),
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": issue_id},
                        )
                    ],
                ),
            )

            outcome = self._run(history, auditor, bundle=bundle)
            retry = auditor.prompts[1]

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(2, len(auditor.prompts))
            self.assertEqual(
                ["invalid-reference", "ok"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertIn("Your previous answer was well-formed", retry)
            self.assertNotIn("Your previous answer was rejected", retry)
            self.assertIn(f'{{"issue_id":"{issue_id}","reasons":["summary"]', retry)
            self.assertIn('"similarity":1.0', retry)
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertEqual(issue_id, decision.issue_id)
            self.assertEqual("agent", decision.source)

    def test_a_second_new_answer_fails_closed_without_allocating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            issue_id = self._seed(history[2])
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertIsNone(outcome.agent)
            self.assertEqual(2, len(auditor.prompts))
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                supervisor_audit.REASON_IDENTITY_AMBIGUOUS, outcome.audit.reason
            )
            self.assertEqual(
                ["invalid-reference", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertIn(issue_id, outcome.attempts[0].detail)
            # The reproducible half of the audit is not lost with the agent half.
            self.assertTrue(outcome.audit.findings)
            self.assertNotIn(
                "identity.agent-observed-drift",
                [finding.invariant_key for finding in outcome.audit.findings],
            )

    def test_the_candidate_list_is_offered_once_and_never_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(history[2])
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            self._run(history, auditor, bundle=bundle)

            self.assertEqual(MAX_AUDIT_ATTEMPTS, len(auditor.prompts))
            self.assertNotIn("Your previous answer was well-formed", auditor.prompts[0])
            self.assertIn("Your previous answer was well-formed", auditor.prompts[1])

    def test_an_undelivered_turn_does_not_consume_the_candidate_offer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(history[2])
            bundle = self._bundle(history)
            answer = self._new_answer(bundle.evidence[0].locator)
            auditor = ScriptedAuditor(
                answer,
                {"message": "", "returncode": 1, "stderr": "socket hang up"},
                answer,
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, bundle=bundle, sleeps=sleeps)

            self.assertEqual(
                ["invalid-reference", "transport-error", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual([15.0], sleeps)
            # The relaunch after the outage carries the candidate list the
            # undelivered turn was meant to show.
            self.assertIn("Your previous answer was well-formed", auditor.prompts[1])
            self.assertEqual(auditor.prompts[1], auditor.prompts[2])

    def test_a_wasted_first_attempt_leaves_no_turn_for_the_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(history[2])
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                "the audit went fine", self._new_answer(bundle.evidence[0].locator)
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual(
                ["malformed-output", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual(
                supervisor_audit.REASON_IDENTITY_AMBIGUOUS, outcome.audit.reason
            )
            self.assertNotIn("Your previous answer was well-formed", auditor.prompts[1])
            self.assertIn("Your previous answer was rejected", auditor.prompts[1])

    def test_a_distinct_near_neighbour_is_allocated_rather_than_questioned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(
                history[2],
                invariant_key="scope.no-escape",
                summary="A commit landed outside the declared write scope.",
            )
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertTrue(decision.is_new)
            self.assertEqual("agent", decision.source)

    def test_a_selection_the_validator_accepted_is_never_second_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            chosen = self._seed(history[2], invariant_key="identity.chosen")
            self._seed(history[2])
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": chosen},
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertEqual(chosen, decision.issue_id)

    def test_an_empty_inbox_leaves_a_new_allocation_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            assert outcome.agent is not None
            self.assertTrue(outcome.agent.decisions[0][1].is_new)

    def test_an_identity_source_outside_the_two_is_refused(self) -> None:
        with self.assertRaises(supervisor_audit_agent.AgentOutputError) as raised:
            supervisor_audit_agent.IdentityDecision(decision="new", source="guess")

        self.assertEqual("malformed-output", raised.exception.outcome)

    def test_resolution_names_every_candidate_it_could_not_settle(self) -> None:
        resolution = supervisor_audit_agent.IdentityResolution(
            audit=supervisor_audit_agent.AgentAudit(
                lanes=(), findings=(), identities=()
            ),
            candidates=(
                assurance_projection.IdentityCandidate(
                    issue_id="ASI-3", reasons=("summary",), similarity=0.9
                ),
                assurance_projection.IdentityCandidate(
                    issue_id="ASI-7", reasons=("invariant",)
                ),
            ),
            ambiguous=2,
        )

        self.assertFalse(resolution.settled)
        self.assertIn("2 finding(s)", resolution.detail)
        self.assertIn("ASI-3", resolution.detail)
        self.assertIn("ASI-7", resolution.detail)


class IdentitySelectionTests(unittest.TestCase):
    """A fingerprint's selection is the decision of the finding that was kept."""

    def _finding(self, summary: str) -> Finding:
        return Finding(
            category="lifecycle",
            component="runner",
            invariant_key="retry.budget",
            lane="lifecycle-retry-resume",
            severity="high",
            summary=summary,
            recommendation="Respect the retry budget.",
            evidence=("events.jsonl",),
        )

    def test_a_kept_new_finding_is_not_given_a_duplicates_issue(self) -> None:
        kept, duplicate = self._finding("first wording"), self._finding("second")
        self.assertEqual(kept.fingerprint, duplicate.fingerprint)
        agent = AgentAudit(
            lanes=(),
            findings=(kept, duplicate),
            identities=(IdentityDecision("new"), IdentityDecision("existing", "ASI-7")),
        )
        merged = supervisor_audit_agent_resolution._merged_findings((), agent.findings)

        self.assertEqual([kept], list(merged))
        self.assertEqual(
            {}, supervisor_audit_agent_resolution._identity_selections(agent, merged)
        )


if __name__ == "__main__":
    unittest.main()
