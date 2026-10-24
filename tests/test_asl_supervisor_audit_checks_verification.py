"""Deterministic checks for verification outcomes and retried turns."""

from __future__ import annotations

import ast
import dataclasses
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    supervisor_audit_checks,
    supervisor_audit_models,
    supervisor_audit_write_scope,
    verification_models,
)
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import DEFERRED_REASON, RUN_ID


class DeterministicCheckTests(checks_support.DeterministicCheckFixture):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    REPAIR_CYCLE = {
        "cycle": 1,
        "review_session_id": "2",
        "outcome": "findings",
        "disposition": "repairs-materialized",
    }

    ALL_CLEAR_CYCLE = {
        "cycle": 2,
        "review_session_id": "3",
        "outcome": "all-clear",
        "disposition": "all-clear",
    }

    def _defer_broad_failure(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        review_cycles: list[dict[str, Any]],
        deferred_failure_names: list[str] | None = None,
        paused: bool = False,
    ) -> None:
        """Record a turn whose broad-check failure was deferred to review."""
        result = self._read_result(history[3])
        result.update(session_id="2", turn_index=1, status_result="complete")
        failure = {
            "name": "broad:1:tests",
            "kind": "command",
            "status": "fail",
            "returncode": 1,
            "failure_reason": "command-failed",
        }
        names = (
            ["broad:1:tests"]
            if deferred_failure_names is None
            else deferred_failure_names
        )
        result["verification"] = {
            "session": "2",
            "passed": False,
            "checks": [failure],
            "skipped_checks": [],
            "diagnostic_failures": [],
            "acceptance": {
                "accepted": True,
                "reason": DEFERRED_REASON,
                "review_outcome": "findings",
                "deferred_failure_count": len(names),
                "deferred_failure_names": names,
            },
            "deferred_failures": [failure],
        }
        self._write_result(history[3] / "turn-0001-01", result)
        state_payload = self._read_state(history[2])
        state_payload["review_cycles"] = review_cycles
        if paused:
            state_payload["ok"] = False
            state_payload["paused"] = True
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.RUN_PAUSED_EVENT,
                        {"run_id": RUN_ID, "reason": "verify-failed"},
                    ),
                ],
            )
        self._write_state(history[2], state_payload)

    def test_write_scope_severity_lives_on_the_spec_not_on_the_verdict(self) -> None:
        specs = {
            spec.invariant_key: spec
            for spec in supervisor_audit_checks.DETERMINISTIC_CHECK_SPECS
        }
        containment = specs["packet.write-scope-containment"]
        catch = specs["packet.write-scope-guard-catch"]

        self.assertEqual("critical", containment.severity)
        self.assertEqual("medium", catch.severity)
        self.assertEqual("task-packet-coherence", containment.lane)
        self.assertEqual(containment.lane, catch.lane)
        self.assertNotEqual(containment.component, catch.component)
        # A severity on the verdict would let one issue key report critical on
        # one occurrence and medium on the next, which is what corrupts the
        # inbox's dedup history for that key.
        self.assertNotIn(
            "severity",
            {
                field.name
                for field in dataclasses.fields(supervisor_audit_models._Verdict)
            },
        )

    def test_both_write_scope_checks_share_one_analysis_helper(self) -> None:
        module = ast.parse(
            Path(supervisor_audit_write_scope.__file__).read_text(encoding="utf-8")
        )
        called = {
            node.name: {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            for node in module.body
            if isinstance(node, ast.FunctionDef)
        }

        self.assertIn("_analyze_write_scope", called["_check_scope_escape"])
        self.assertIn("_analyze_write_scope", called["_check_scope_guard_catch"])

    def test_success_claimed_over_failed_verification_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": False,
                "checks": [{"name": "tests", "passed": False, "returncode": 1}],
                "skipped_checks": ["lint"],
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)
            findings = supervisor_audit_checks.findings_from_results(results.values())

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn("verification failed", results["misleading-success"].detail)
            self.assertIn("skipped", results["misleading-success"].detail)
            self.assertEqual(
                ["observability.success-is-earned"],
                [item.invariant_key for item in findings],
            )

    def test_every_recorded_verification_failure_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "role": verification_models.CHECK_ROLE_DIAGNOSTIC,
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 2,
                            "passed_count": 1,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["unclassified-anomalies"].status)
            self.assertEqual("pass", results["verification-outcomes"].status)
            self.assertIn("accounted for", results["verification-outcomes"].detail)

    def test_deferred_broad_failure_is_accounted_only_after_later_all_clear(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history, review_cycles=[self.REPAIR_CYCLE, self.ALL_CLEAR_CYCLE]
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["verification-outcomes"].status)
            self.assertEqual("pass", results["misleading-success"].status)

    def test_deferred_broad_failure_without_later_all_clear_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(history, review_cycles=[self.REPAIR_CYCLE])

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn("later all-clear", results["verification-outcomes"].detail)
            self.assertEqual("fail", results["misleading-success"].status)

    def test_a_deferral_the_run_paused_before_clearing_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history, review_cycles=[self.REPAIR_CYCLE], paused=True
            )

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["verification-outcomes"].status)
            self.assertIn(
                "reached no all-clear review before the run stopped",
                results["verification-outcomes"].detail,
            )

    def test_a_paused_deferral_is_unavailable_without_a_repair_cycle(self) -> None:
        """The pending branch cannot key off a repairs-materialized source."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history,
                review_cycles=[
                    {**self.REPAIR_CYCLE, "disposition": "malformed-review-repair"}
                ],
                paused=True,
            )

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["verification-outcomes"].status)

    def test_a_paused_run_still_fails_a_deferral_naming_checks_that_passed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history,
                review_cycles=[self.REPAIR_CYCLE],
                deferred_failure_names=["broad:1:docs"],
                paused=True,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)

    def test_review_findings_a_later_all_clear_closed_do_not_contradict_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_verdict"] = {"outcome": "findings", "total_findings": 2}
            payload["review_cycles"] = [self.REPAIR_CYCLE, self.ALL_CLEAR_CYCLE]
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("pass", results["misleading-success"].status)

    def test_review_findings_no_all_clear_closed_contradict_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_verdict"] = {"outcome": "findings", "total_findings": 2}
            payload["review_cycles"] = [self.REPAIR_CYCLE]
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "reports 2 finding(s) and no later all-clear review closed them",
                results["misleading-success"].detail,
            )

    def test_a_skipped_diagnostic_cannot_absorb_a_failed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "role": verification_models.CHECK_ROLE_GATE,
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_CHECK_SKIPPED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "on_failure_of": ["lint"],
                            "reason": verification_models.SKIP_REASON_CONDITION_NOT_MET,
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 1,
                            "passed_count": 0,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 1,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn(
                "reported a failure against a gate",
                results["verification-outcomes"].detail,
            )

    def test_a_passing_diagnostic_cannot_absorb_a_failed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "role": verification_models.CHECK_ROLE_GATE,
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "role": verification_models.CHECK_ROLE_DIAGNOSTIC,
                            "status": "pass",
                            "returncode": 0,
                            "failure_reason": None,
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 2,
                            "passed_count": 1,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn(
                "reported a failure against a gate",
                results["verification-outcomes"].detail,
            )

    def test_a_skipped_diagnostic_absorbs_nothing_without_recorded_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 1,
                            "passed_count": 0,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 1,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn("unidentified role", results["verification-outcomes"].detail)

    def test_a_verification_that_passed_over_a_failed_gate_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 1,
                            "passed_count": 0,
                            "gate_count": 1,
                            "diagnostic_count": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)
            findings = supervisor_audit_checks.findings_from_results(results.values())

            self.assertEqual("pass", results["unclassified-anomalies"].status)
            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn(
                "verification.failure-accounting",
                [item.invariant_key for item in findings],
            )

    def test_a_retried_turn_does_not_contradict_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_retry_pair(history)

            bundle, results = self._audit(history)
            findings = supervisor_audit_checks.findings_from_results(results.values())

            self.assertTrue(bundle.payload["turns"][0]["anomalous"])
            self.assertEqual("pass", results["misleading-success"].status)
            self.assertNotIn(
                "observability.success-is-earned",
                [item.invariant_key for item in findings],
            )

    def test_an_unretried_anomalous_turn_still_contradicts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload.update(
                {"session_id": "01", "turn_index": 1, "status_result": "blocked"}
            )
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "recorded an anomalous result", results["misleading-success"].detail
            )

    def test_a_retry_does_not_excuse_a_write_scope_breach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_retry_pair(
                history,
                first={
                    "workspace_write_guard": {
                        "passed": False,
                        "write_aliases": ["app", "other"],
                        "authorized_aliases": ["app"],
                        "violations": [{"alias": "other"}],
                        "unavailable": {},
                    }
                },
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "exceeded its declared write scope",
                results["misleading-success"].detail,
            )

    def test_supersession_survives_the_retry_being_shed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_retry_pair(history)

            bundle, _ = self._audit(history)
            first = bundle.payload["turns"][0]

            self.assertEqual("turn-0002-01", first["superseded_by"])
            self.assertNotIn("superseded_by", bundle.payload["turns"][1])
            results = self._checks_without_the_retry(bundle)
            self.assertEqual("pass", results["misleading-success"].status)

    def test_an_unretried_anomalous_turn_is_never_stamped_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload.update(
                {"session_id": "01", "turn_index": 1, "status_result": "blocked"}
            )
            self._write_result(history[3] / "turn-0001-01", payload)

            bundle, _ = self._audit(history)

            self.assertNotIn("superseded_by", bundle.payload["turns"][0])
            results = self._checks_without_the_retry(bundle)
            self.assertEqual("fail", results["misleading-success"].status)


if __name__ == "__main__":
    unittest.main()
