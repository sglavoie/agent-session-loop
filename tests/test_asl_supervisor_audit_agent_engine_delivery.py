"""Agent audit engine verdicts, retries, and delivery budgets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    assurance_models,
    harness_failures,
    model_availability,
    state,
    supervisor_audit,
    supervisor_audit_checks,
    supervisor_audit_evidence,
)
from agent_session_loop.assurance_models import (
    MAX_AUDIT_ATTEMPTS,
    MAX_AUDIT_DELIVERY_ATTEMPTS,
    RUBRIC_LANES,
    AuditRole,
)
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import FIXED_CLOCK, RUN_ID, ScriptedAuditor


class AgentAuditEngineTests(checks_support.AgentAuditEngineFixture):
    """The semantic auditor is validated strictly and never trusted as output."""

    def _refused_for_size(self) -> dict[str, object]:
        """The provider's refusal of a request larger than its context window."""
        return {
            "message": "",
            "returncode": 1,
            "stderr": (
                "API Error: 400 "
                '{"type":"invalid_request_error","message":'
                '"prompt is too long: 517143 tokens > 200000 maximum"}'
            ),
        }

    def test_a_clean_run_with_every_lane_proven_is_all_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertIsNone(outcome.audit.reason)
            self.assertEqual((), outcome.audit.findings)
            self.assertEqual(["ok"], [item.outcome for item in outcome.attempts])
            self.assertEqual(
                list(RUBRIC_LANES), [lane.lane for lane in outcome.audit.lanes]
            )

    def test_an_agent_finding_is_reported_with_an_asl_assigned_fingerprint(
        self,
    ) -> None:
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

            self.assertEqual("findings", outcome.audit.state)
            self.assertIsNone(outcome.audit.reason)
            finding = outcome.audit.findings[0]
            self.assertEqual("identity.agent-observed-drift", finding.invariant_key)
            self.assertEqual(64, len(finding.fingerprint))
            self.assertEqual((locator,), finding.evidence)
            self.assertEqual("fail", outcome.audit.lanes[0].status)

    def test_an_agent_that_assigns_its_own_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = ScriptedAuditor(
                self._reply(
                    self._lanes(locator),
                    [self._finding(locator, issue_id="ISSUE-0001")],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("malformed-output", outcome.attempts[0].outcome)
            self.assertIn("never", outcome.attempts[0].detail)
            self.assertIn("issue IDs", outcome.attempts[0].detail)

    def test_a_malformed_answer_is_retried_in_a_fresh_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                "I could not be bothered to answer in JSON.",
                self._reply(self._lanes(bundle.evidence[0].locator)),
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertEqual(
                ["malformed-output", "ok"], [item.outcome for item in outcome.attempts]
            )
            retry = auditor.prompts[1]
            self.assertIn("Your previous answer was rejected", retry)
            self.assertIn("not parseable JSON", retry)
            self.assertIn("Operational evidence for run", retry)
            self.assertNotIn("could not be bothered", retry)

    def test_two_malformed_answers_keep_the_deterministic_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            auditor = ScriptedAuditor("{not json")

            outcome = self._run(history, auditor)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual("malformed-output", outcome.audit.reason)
            self.assertEqual(MAX_AUDIT_ATTEMPTS, len(outcome.attempts))
            self.assertEqual(
                len(supervisor_audit_checks.DETERMINISTIC_CHECK_SPECS),
                len(outcome.audit.deterministic),
            )
            self.assertTrue(outcome.audit.findings)
            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertEqual("fail", statuses["verification-review-independence"])

    def test_a_timeout_spends_the_delivery_budget_before_going_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor({"message": "", "timed_out": True})
            sleeps: list[float] = []

            outcome = self._run(history, auditor, sleeps=sleeps)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(supervisor_audit.REASON_TIMEOUT, outcome.audit.reason)
            self.assertEqual(
                ["timeout"] * MAX_AUDIT_DELIVERY_ATTEMPTS,
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual(MAX_AUDIT_DELIVERY_ATTEMPTS, len(auditor.prompts))
            self.assertEqual(
                list(supervisor_audit.AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS), sleeps
            )

    def test_a_transport_failure_backs_off_and_reuses_the_same_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor(
                {"message": "", "returncode": 1, "stderr": "connection reset by peer"}
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, sleeps=sleeps)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                supervisor_audit.REASON_HARNESS_FAILED, outcome.audit.reason
            )
            self.assertEqual(
                ["transport-error"] * MAX_AUDIT_DELIVERY_ATTEMPTS,
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual([15.0, 60.0, 120.0], sleeps)
            # The agent never saw the prompt, so nothing about it is corrected:
            # every relaunch repeats it verbatim.
            self.assertEqual({auditor.prompts[0]}, set(auditor.prompts))
            self.assertNotIn("Your previous answer was rejected", auditor.prompts[-1])

    def test_an_exhausted_quota_is_not_filed_as_a_harness_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor(
                {
                    "message": "",
                    "returncode": 1,
                    "stderr": "You've hit your weekly limit · resets Sep 6 at 1am",
                }
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, sleeps=sleeps)

            self.assertEqual("unavailable", outcome.audit.state)
            # Its own class, not `harness-failed`: nothing about the audit broke,
            # and the class an operator reads decides whether they go looking for
            # a defect that is not there (ASI-127).
            self.assertEqual(
                supervisor_audit.REASON_QUOTA_EXHAUSTED, outcome.audit.reason
            )
            # A refusal the provider will repeat is not waited out: the launch is
            # classified non-retryable, so no delivery budget is spent on it.
            self.assertEqual(1, len(auditor.prompts))
            self.assertEqual([], sleeps)

    def test_a_capacity_outage_that_clears_still_earns_a_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            capacity = {
                "message": "",
                "returncode": 1,
                "stderr": (
                    '{"error": {"message": "Selected model is at capacity. '
                    'Please try a different model.", '
                    '"codex_error_info": "server_overloaded"}}'
                ),
            }
            auditor = ScriptedAuditor(
                capacity,
                capacity,
                self._reply(self._lanes(bundle.evidence[0].locator)),
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, bundle=bundle, sleeps=sleeps)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertEqual(
                ["transport-error", "transport-error", "ok"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual([15.0, 60.0], sleeps)

            attempt_dir = history[3] / assurance_models.attempt_dir_name(1)
            recorded = json.loads(
                (attempt_dir / supervisor_audit.ATTEMPT_OUTCOME_FILE).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                harness_failures.MODEL_CAPACITY_MARKER,
                recorded["classification"]["marker"],
            )
            self.assertIn("at capacity", recorded["classification"]["evidence"])
            self.assertIn(
                "server_overloaded",
                (attempt_dir / supervisor_audit.ATTEMPT_STDERR_FILE).read_text(
                    encoding="utf-8"
                ),
            )
            # An attempt that produced no stdout leaves no empty file behind.
            self.assertFalse(
                (attempt_dir / supervisor_audit.ATTEMPT_STDOUT_FILE).exists()
            )

    def test_a_capacity_outage_is_recorded_without_swapping_the_auditor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            role = AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=30.0,
                model="claude-opus-4",
            )
            auditor = ScriptedAuditor(
                {
                    "message": "",
                    "returncode": 1,
                    "stderr": '{"codex_error_info": "server_overloaded"}',
                }
            )

            supervisor_audit.run_agent_audit(
                role,
                bundle=self._bundle((workspace, plan_dir, state_dir, run_dir)),
                run_id=RUN_ID,
                run_dir=run_dir,
                workspace_root=workspace,
                plan_dir=plan_dir,
                repositories=("app",),
                state_dir=state_dir,
                durable=False,
                now=lambda: FIXED_CLOCK,
                _run_harness_fn=auditor,
                _sleep_fn=lambda _seconds: None,
            )

            recorded = model_availability.availability(
                state.state_root(state_dir), "claude-opus-4"
            )
            self.assertFalse(recorded.available)
            self.assertEqual(harness_failures.MODEL_CAPACITY_MARKER, recorded.reason)
            # The outage is recorded, never routed around: every relaunch used
            # the auditor the role named.
            self.assertEqual({"claude"}, set(auditor.names))

    def test_a_failure_that_can_never_succeed_is_not_waited_out(self) -> None:
        """The shared classifier, not the outcome slug, decides what is transient."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor(
                {"message": "", "returncode": 7, "stderr": "fake harness failure"}
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, sleeps=sleeps)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                ["transport-error"], [item.outcome for item in outcome.attempts]
            )
            self.assertEqual([], sleeps)

    def test_a_request_refused_for_size_is_retried_with_less_of_the_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                self._refused_for_size(),
                self._reply(self._lanes(bundle.evidence[0].locator)),
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, bundle=bundle, sleeps=sleeps)

            # The refusal is the caller's own doing, so the retry changes what
            # was sent instead of waiting for a provider to recover, and the
            # audit still earns its verdict.
            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertEqual(
                ["transport-error", "ok"], [item.outcome for item in outcome.attempts]
            )
            self.assertEqual(2, len(auditor.prompts))
            self.assertLess(len(auditor.prompts[1]), len(auditor.prompts[0]))
            self.assertEqual([], sleeps)

            attempt_dir = history[3] / assurance_models.attempt_dir_name(1)
            recorded = json.loads(
                (attempt_dir / supervisor_audit.ATTEMPT_OUTCOME_FILE).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                harness_failures.CONTEXT_OVERFLOW_MARKER,
                recorded["classification"]["marker"],
            )

    def test_a_shrink_does_not_spend_the_delivery_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor(
                self._refused_for_size(),
                {"message": "", "returncode": 1, "stderr": "socket hang up"},
            )

            outcome = self._run(history, auditor, sleeps=[])

            # One oversized bundle must not cost the retries a real outage is
            # owed: the transport failures that follow still get all of theirs.
            self.assertEqual(
                ["transport-error"] * (MAX_AUDIT_DELIVERY_ATTEMPTS + 1),
                [item.outcome for item in outcome.attempts],
            )

    def test_a_shrink_does_not_spend_the_answer_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor(self._refused_for_size(), "{not json")

            outcome = self._run(history, auditor)

            self.assertEqual(
                ["transport-error"] + ["malformed-output"] * MAX_AUDIT_ATTEMPTS,
                [item.outcome for item in outcome.attempts],
            )

    def test_an_unshrinkable_bundle_does_not_buy_a_second_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # Already shed as far as it goes, so a tighter ceiling changes
            # nothing and a second launch would be refused identically.
            bundle = supervisor_audit_evidence.tighten_audit_input(
                self._bundle(history), max_bytes=1
            )
            auditor = ScriptedAuditor(self._refused_for_size())

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                ["transport-error"], [item.outcome for item in outcome.attempts]
            )
            self.assertEqual(1, len(auditor.prompts))

    def test_delivery_failures_do_not_spend_the_answer_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = ScriptedAuditor(
                {"message": "", "returncode": 1, "stderr": "socket hang up"},
                "{not json",
                self._reply(self._lanes(bundle.evidence[0].locator)),
            )

            outcome = self._run(history, auditor, bundle=bundle, sleeps=[])

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertEqual(
                ["transport-error", "malformed-output", "ok"],
                [item.outcome for item in outcome.attempts],
            )

    def test_an_interrupted_attempt_is_never_retried(self) -> None:
        """The reply a harness returns after Ctrl+C, not a raised exception."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = ScriptedAuditor(
                {"message": "", "returncode": 130, "interrupted": True}
            )

            outcome = self._run(history, auditor)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, outcome.audit.reason)
            self.assertEqual(
                ["interrupted"], [item.outcome for item in outcome.attempts]
            )
            self.assertEqual(1, len(auditor.prompts))

    def test_an_interrupt_outranks_the_nonzero_exit_it_arrives_with(self) -> None:
        """130 is the harness reporting the interrupt, not a transport failure."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            auditor = ScriptedAuditor(
                {"message": "", "returncode": 130, "interrupted": True}
            )

            attempt = supervisor_audit.run_restricted_audit(
                self._role(),
                run_dir=run_dir,
                run_id=RUN_ID,
                workspace_root=workspace,
                plan_dir=plan_dir,
                prompt="grade this run",
                repositories=("app",),
                state_dir=state_dir,
                _run_harness_fn=auditor,
            )

            self.assertEqual("interrupted", attempt.outcome)
            self.assertEqual("unavailable", attempt.state)
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, attempt.reason)
            self.assertNotIn("interrupted", supervisor_audit.RETRYABLE_ATTEMPT_OUTCOMES)


if __name__ == "__main__":
    unittest.main()
