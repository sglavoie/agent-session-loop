"""Deterministic checks for restore refusals, retryable failures, and aborts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    state,
    supervisor_audit_checks,
    supervisor_audit_evidence_constants,
)
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import RUN_ID


class DeterministicCheckTests(checks_support.DeterministicCheckFixture):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def _aborted_a_turn(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        session: Any = 9,
        turn_index: int = 4,
        reason: str | None = "Operator requested immediate stop",
        requested: bool = True,
        requested_turn_index: int | None = None,
        also_failed: tuple[tuple[Any, int], ...] = (),
    ) -> None:
        """Rewrite the run as one whose turn an operator killed mid-session.

        ``requested`` false drops the request record the honored abort should
        trace back to; ``requested_turn_index`` points the request at another
        turn. ``also_failed`` names further session/turn pairs that failed and
        which no abort accounts for.
        """

        state_dir = history[2]
        payload = self._read_state(state_dir)
        payload["ok"] = False
        payload["reason"] = "abandoned"
        self._write_state(state_dir, payload)
        control = {
            "run_id": RUN_ID,
            "session": session,
            "abort_reason": reason,
            "abort_requested": True,
        }
        records: list[tuple[str, dict[str, Any]]] = [
            ("run_started", {"run_id": RUN_ID})
        ]
        if requested:
            records.append(
                (
                    events.RUN_CONTROL_EVENT,
                    {
                        **control,
                        "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                        "turn_index": (
                            turn_index
                            if requested_turn_index is None
                            else requested_turn_index
                        ),
                    },
                )
            )
        records.append(
            (
                events.RUN_CONTROL_EVENT,
                {
                    **control,
                    "action": events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
                    "turn_index": turn_index,
                },
            )
        )
        records.extend(
            (
                events.TURN_FINISHED_EVENT,
                {
                    "run_id": RUN_ID,
                    "session": failed_session,
                    "turn_index": failed_turn,
                    "returncode": 137,
                },
            )
            for failed_session, failed_turn in ((session, turn_index), *also_failed)
        )
        records.append(
            ("run_finished", {"run_id": RUN_ID, "ok": False, "reason": "abandoned"})
        )
        self._write_events(state_dir, records)

    def test_an_unaccountable_restore_refusal_stays_visible(self) -> None:
        for defect in (
            "not-retired",
            "not-paused",
            "wrong-session",
            "different-exit",
            "timeout",
            "malformed-turn",
            "no-event",
        ):
            with self.subTest(defect=defect):
                bundle = self._refused_restore_bundle()
                status = bundle["status"]
                records = bundle["events"]["records"]
                if defect == "not-retired":
                    records[1]["payload"]["retired"] = False
                elif defect == "not-paused":
                    status["paused"] = False
                    status["ok"] = True
                elif defect == "wrong-session":
                    status["pause_details"]["session"] = 5
                elif defect == "different-exit":
                    records[0]["payload"]["returncode"] = 143
                elif defect == "timeout":
                    records[0]["payload"]["timed_out"] = True
                elif defect == "malformed-turn":
                    records[1]["payload"]["turn_index"] = []
                else:
                    records.pop()
                results = self._restore_checks(bundle)
                self.assertEqual("fail", results["conversation-restore-refusal"].status)
                self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_a_shed_restore_event_is_a_gap_not_a_clean_refusal(self) -> None:
        bundle = self._refused_restore_bundle()
        bundle["events"]["records"].pop()
        bundle["omissions"] = [{"section": "events", "reason": "size-cap", "count": 1}]
        results = self._restore_checks(bundle)
        self.assertEqual("unavailable", results["conversation-restore-refusal"].status)
        self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_a_shed_exit_cannot_hide_a_later_contradictory_refusal(self) -> None:
        bundle = self._refused_restore_bundle()
        records = bundle["events"]["records"]
        records.pop(0)
        records.append(
            {
                "name": events.CONVERSATION_CONTINUATION_EVENT,
                "payload": {**records[0]["payload"], "turn_index": 2, "retired": False},
            }
        )
        bundle["omissions"] = [{"section": "events", "reason": "size-cap", "count": 1}]
        results = self._restore_checks(bundle)
        self.assertEqual("fail", results["conversation-restore-refusal"].status)

    def test_a_final_failure_is_accounted_for_by_its_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history, reason="harness-missing")

            _, results = self._audit(history)

            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertIn(
                "non-retryable", results["retryable-transport-failure"].detail
            )
            # Nothing will relaunch this session, but the run still named why
            # the turn failed, so it is not a failure nothing classified.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_classification_the_run_is_no_longer_paused_on_explains_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history, paused=False)

            _, results = self._audit(history)

            # A run that resumed past its pause carries the old classification
            # in state; reading it would excuse a turn nothing is holding.
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertIn(
                "did not pause on a harness failure",
                results["retryable-transport-failure"].detail,
            )

    def test_a_pause_that_named_no_reason_explains_no_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history, classification={"reason": None, "retryable": None}
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.TURN_EXECUTION_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def test_a_retryable_failure_nothing_will_relaunch_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history,
                policy={
                    "recoverable": False,
                    "requires_user": True,
                    "max_retries": 0,
                },
            )

            _, results = self._audit(history)
            findings = supervisor_audit_checks.findings_from_results(results.values())

            verdict = results["retryable-transport-failure"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("nothing will relaunch it", verdict.detail)
            self.assertIn("waits on a person no reply can help", verdict.detail)
            self.assertIn("grants max_retries=0", verdict.detail)
            self.assertIn(
                "lifecycle.retryable-transport-failure",
                [item.invariant_key for item in findings],
            )
            self.assertEqual(
                "fail",
                supervisor_audit_checks.lane_rollup(results.values())[
                    "lifecycle-retry-resume"
                ],
            )

    def test_a_retryable_flag_the_reason_denies_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history, classification={"reason": "harness-missing"}
            )

            _, results = self._audit(history)

            verdict = results["retryable-transport-failure"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("retryable=True", verdict.detail)
            self.assertIn("not one the classifier marks retryable", verdict.detail)

    def test_a_classification_with_no_policy_cannot_be_judged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history)
            payload = self._read_state(history[2])
            payload["pause_policy"] = {}
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            verdict = results["retryable-transport-failure"]
            self.assertEqual("unavailable", verdict.status)
            self.assertIn("recorded no pause policy", verdict.detail)

    def test_a_killed_turn_is_accounted_for_by_the_abort_that_asked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history)

            _, results = self._audit(history)

            self.assertEqual("pass", results["operator-abort"].status)
            self.assertIn("1 turn(s) were killed", results["operator-abort"].detail)
            # The killed turn is explained by the abort, so it must not fall
            # through to the catch-all that reports unknown failures.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_successful_stop_only_excuses_its_accountable_aborted_turn(self) -> None:
        for case in (
            "stopped",
            "complete",
            "unrequested",
            "other-turn",
            "scope-breach",
            "guard-unavailable",
            "malformed-plan",
            "verification-failed",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                history = self._history(Path(tmp))
                self._aborted_a_turn(
                    history, session=1, turn_index=1, requested=case != "unrequested"
                )
                self._state_with(
                    history,
                    ok=True,
                    reason="complete" if case == "complete" else "stopped",
                )
                result = self._read_result(history[3])
                result.update(
                    session=1,
                    session_id="1",
                    turn_index=2 if case == "other-turn" else 1,
                    returncode=137,
                    status_result="nonzero-exit",
                )
                if case == "scope-breach":
                    result["workspace_write_guard"] = {
                        "passed": False,
                        "violations": [{"alias": "other"}],
                    }
                elif case == "guard-unavailable":
                    result["workspace_write_guard"] = {"passed": False}
                elif case == "malformed-plan":
                    result["status_result"] = "malformed-plan"
                elif case == "verification-failed":
                    result["verification"] = {"passed": False}
                self._write_result(history[3] / "turn-0001-01", result)

                _, results = self._audit(history)

                self.assertEqual(
                    "pass" if case == "stopped" else "fail",
                    results["misleading-success"].status,
                )
                if case == "unrequested":
                    self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_a_run_that_killed_nothing_owes_no_abort_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            _, results = self._audit(history)

            verdict = results["operator-abort"]
            self.assertEqual("pass", verdict.status)
            self.assertIn("killed no turn", verdict.detail)

    def test_another_turns_failure_is_not_excused_by_the_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, also_failed=((9, 5),))

            _, results = self._audit(history)

            self.assertEqual("pass", results["operator-abort"].status)
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.TURN_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def test_a_kill_no_request_asked_for_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, requested=False)

            _, results = self._audit(history)
            findings = supervisor_audit_checks.findings_from_results(results.values())

            verdict = results["operator-abort"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("no recorded request asked for", verdict.detail)
            self.assertIn(
                "lifecycle.operator-abort",
                [item.invariant_key for item in findings],
            )

    def test_a_request_naming_another_turn_does_not_cover_this_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, requested_turn_index=3)

            _, results = self._audit(history)

            verdict = results["operator-abort"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("no recorded request asked for", verdict.detail)

    def test_a_kill_that_names_no_reason_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, reason=None)

            _, results = self._audit(history)

            verdict = results["operator-abort"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("names no reason", verdict.detail)
            # An abort with no reason on it explains nothing either.
            self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_a_damaged_event_log_lands_in_the_unknown_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            with (history[2] / events.EVENTS_FILE_NAME).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("{not json\n")

            _, results = self._audit(history)

            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn("would not parse", results["unclassified-anomalies"].detail)
            self.assertEqual(
                "fail",
                supervisor_audit_checks.lane_rollup(results.values())[
                    "unknown-anomalies"
                ],
            )

    def test_a_bundle_written_before_the_rename_still_reports_its_damage(self) -> None:
        # Every replay proof is drawn from bundles written under schema
        # version 1, which spell the count `rejected_count`. Reading only the
        # new key would silently re-judge a damaged log as an undamaged one.
        legacy_key = (
            supervisor_audit_evidence_constants.LEGACY_UNPARSABLE_LINE_COUNT_KEY
        )
        results = supervisor_audit_checks.run_deterministic_checks(
            {
                "events": {
                    "summary": {
                        "available": True,
                        legacy_key: 2,
                    }
                }
            }
        )
        verdict = {result.check: result for result in results}["unclassified-anomalies"]

        self.assertEqual("fail", verdict.status)
        self.assertIn("2 event log line(s) would not parse", verdict.detail)

    def test_checks_report_unavailable_when_the_workspace_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, results = self._audit(self._history(Path(tmp)), workspace_root=None)

            self.assertEqual("unavailable", results["root-registry-agreement"].status)
            self.assertEqual("unavailable", results["repository-intent"].status)
            self.assertEqual(
                "unavailable",
                supervisor_audit_checks.lane_rollup(results.values())[
                    "identity-authority"
                ],
            )

    def test_findings_keep_one_fingerprint_across_relocated_runs(self) -> None:
        fingerprints = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                history = self._history(Path(tmp))
                payload = self._read_state(history[2])
                payload["review_harness"] = payload["harness"]
                self._write_state(history[2], payload)

                _, results = self._audit(history)
                fingerprints.append(
                    [
                        item.fingerprint
                        for item in supervisor_audit_checks.findings_from_results(
                            results.values()
                        )
                    ]
                )

        self.assertEqual(1, len(fingerprints[0]))
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_every_cited_locator_is_declared_in_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            (history[3] / "turn-0001-01" / state.RESULT_FILE_NAME).unlink()

            bundle, results = self._audit(history)
            declared = {reference.locator for reference in bundle.evidence}

            self.assertEqual(
                len(supervisor_audit_checks.DETERMINISTIC_CHECK_SPECS), len(results)
            )
            for result in results.values():
                self.assertTrue(result.evidence, result.check)
                self.assertLessEqual(set(result.evidence), declared, result.check)


if __name__ == "__main__":
    unittest.main()
