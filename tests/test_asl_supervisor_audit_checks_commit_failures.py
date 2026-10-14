"""Deterministic checks for shed retries, skips, pauses, and commit failures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    supervisor_audit_checks,
    supervisor_audit_evidence_constants,
    supervisor_audit_evidence_input,
    verification_models,
)
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import RUN_ID


class DeterministicCheckTests(checks_support.DeterministicCheckFixture):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def _events_recording_only_a_skip(self) -> list[tuple[str, dict[str, object]]]:
        """A log whose sole verification record for turn 1 is a skip."""
        return [
            ("run_started", {"run_id": RUN_ID}),
            (
                events.VERIFICATION_CHECK_SKIPPED_EVENT,
                {
                    "run_id": RUN_ID,
                    "turn_index": 1,
                    "name": "diagnose",
                    "kind": "command",
                    "on_failure_of": ["tests"],
                    "reason": verification_models.SKIP_REASON_CONDITION_NOT_MET,
                },
            ),
            (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
        ]

    def _recorded_commit(
        self, history: tuple[Path, Path, Path, Path], **payload: Any
    ) -> None:
        """Record one commit before the run's terminal record.

        Shaped like ``run_commit._run_asl_commit``'s payload, which writes
        ``success`` and ``status_result`` and no ``ok`` field. A helper that adds
        one grades a record no run emits, and hides that every commit failure but
        ``nonzero-exit`` is reached at exit zero.
        """
        state_dir = history[2]
        run_dir = history[3]
        commit_dir = run_dir / "commit-0001"
        commit_dir.mkdir(exist_ok=True)
        self._write_events(
            state_dir,
            [
                ("run_started", {"run_id": RUN_ID}),
                (
                    events.COMMIT_FINISHED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "commit_index": 1,
                        "artifact_dir": str(commit_dir),
                        **payload,
                    },
                ),
                (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
            ],
        )

    def test_a_shed_retry_does_not_excuse_a_write_scope_breach(self) -> None:
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

            bundle, _ = self._audit(history)
            results = self._checks_without_the_retry(bundle)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "exceeded its declared write scope",
                results["misleading-success"].detail,
            )

    def test_an_expected_conditional_skip_does_not_contradict_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": True,
                "recipe": {"check_count": 2, "gate_count": 1, "diagnostic_count": 1},
                "checks": [{"name": "tests", "status": "pass", "returncode": 0}],
                "skipped_checks": [
                    {
                        "name": "diagnose",
                        "kind": "command",
                        "on_failure_of": ["tests"],
                        "reason": verification_models.SKIP_REASON_CONDITION_NOT_MET,
                    }
                ],
            }
            self._write_result(history[3] / "turn-0001-01", payload)
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_SKIPPED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "on_failure_of": ["tests"],
                            "reason": verification_models.SKIP_REASON_CONDITION_NOT_MET,
                        },
                    ),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "status": "pass",
                            "returncode": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["misleading-success"].status)
            self.assertEqual(
                (), supervisor_audit_checks.findings_from_results(results.values())
            )

    def test_a_skipped_required_check_contradicts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": True,
                "recipe": {"check_count": 2, "gate_count": 2, "diagnostic_count": 0},
                "checks": [{"name": "tests", "status": "pass", "returncode": 0}],
                "skipped_checks": [
                    {
                        "name": "lint",
                        "kind": "command",
                        "on_failure_of": [],
                        "reason": "executable-not-found",
                    }
                ],
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "without a condition that explains the skip",
                results["misleading-success"].detail,
            )

    def test_a_verification_that_gated_nothing_contradicts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": True,
                "recipe": {"check_count": 1, "gate_count": 0, "diagnostic_count": 1},
                "checks": [],
                "skipped_checks": [
                    {
                        "name": "diagnose",
                        "kind": "command",
                        "on_failure_of": ["tests"],
                        "reason": verification_models.SKIP_REASON_CONDITION_NOT_MET,
                    }
                ],
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "nothing gated the changes", results["misleading-success"].detail
            )

    def test_a_log_recording_only_skips_contradicts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(history[2], self._events_recording_only_a_skip())

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "every verification check recorded for turn(s) 1 was skipped",
                results["misleading-success"].detail,
            )

    def test_a_shed_log_cannot_prove_a_verification_gap(self) -> None:
        # Shedding is not neutral for this claim: a skipped record outranks a
        # finished one, so the cap keeps exactly the half of the pair that reads
        # as a coverage gap and drops the half that would refute it.
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(history[2], self._events_recording_only_a_skip())
            bundle, _ = self._audit(history)

            shed = supervisor_audit_evidence_input.tighten_audit_input(
                bundle, max_bytes=1
            )
            results = {
                result.check: result
                for result in supervisor_audit_checks.run_deterministic_checks(
                    shed.payload
                )
            }

            self.assertTrue(
                any(
                    entry["section"] == "events"
                    and entry["reason"]
                    == supervisor_audit_evidence_constants.REASON_SIZE_CAP
                    for entry in shed.omissions
                )
            )
            self.assertNotIn(
                "was skipped, so nothing gated the changes they made",
                results["misleading-success"].detail,
            )

    def test_a_retryable_pause_accounts_for_the_turn_it_failed_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history)

            _, results = self._audit(history)

            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertIn(
                "transport-failed:model-capacity",
                results["retryable-transport-failure"].detail,
            )
            # The turn the run paused on is explained by the pause, so it must
            # not fall through to the catch-all that reports unknown failures.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_another_sessions_failure_is_not_excused_by_the_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history, also_failed=(9,))

            _, results = self._audit(history)

            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.TURN_EXECUTION_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def test_both_records_for_the_paused_turn_are_accounted_for(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history,
                also_recorded=(
                    (
                        events.TURN_FINISHED_EVENT,
                        {"run_id": RUN_ID, "session": 12, "returncode": 1},
                    ),
                ),
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_non_turn_failure_is_not_excused_by_the_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history,
                also_recorded=(
                    (
                        events.COMMIT_GROUP_FINISHED_EVENT,
                        {"run_id": RUN_ID, "session": 12, "ok": False},
                    ),
                ),
            )

            _, results = self._audit(history)

            # The pause explains one failed turn, not everything the same
            # session touched on its way there.
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.COMMIT_GROUP_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def test_a_failed_commit_is_read_from_the_account_it_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            commit_dir = history[3] / "commit-0001"
            self._recorded_commit(
                history,
                success=False,
                status_result="nonzero-exit",
                returncode=1,
                stderr_path=str(commit_dir / "stderr.log"),
                stderr_chars=64,
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["commit-failure-accounting"].status)
            self.assertIn(
                "1 of 1 recorded commit(s) failed",
                results["commit-failure-accounting"].detail,
            )
            # The exemption in the catch-all covers exactly what the check above
            # graded, so a commit failure the run explained is not also reported
            # as one nothing classified.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_zero_exit_commit_failure_is_graded_like_any_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # Every commit outcome but nonzero-exit is reached on a clean exit,
            # so a check that selects on the exit code alone reports the run's
            # only failed commit as no failure at all.
            self._recorded_commit(
                history,
                success=False,
                status_result="unchanged-head",
                returncode=0,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "failed without recording why",
                results["commit-failure-accounting"].detail,
            )

    def test_a_zero_exit_commit_failure_that_accounted_for_itself_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            commit_dir = history[3] / "commit-0001"
            self._recorded_commit(
                history,
                success=False,
                status_result="dirty-after-commit",
                returncode=0,
                stderr_path=str(commit_dir / "stderr.log"),
                stderr_chars=32,
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["commit-failure-accounting"].status)
            self.assertIn(
                "1 of 1 recorded commit(s) failed",
                results["commit-failure-accounting"].detail,
            )
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_successful_commit_is_not_read_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(
                history,
                success=True,
                status_result="success",
                returncode=0,
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["commit-failure-accounting"].status)
            self.assertIn(
                "0 of 1 recorded commit(s) failed",
                results["commit-failure-accounting"].detail,
            )

    def test_a_failed_commit_that_classified_nothing_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(history, success=False, returncode=1)

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "failed without classifying the failure",
                results["commit-failure-accounting"].detail,
            )

    def test_a_commit_outcome_contradicting_its_exit_code_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(
                history,
                success=False,
                status_result="unchanged-head",
                returncode=1,
                status_error="HEAD did not move",
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "recorded unchanged-head against exit 1",
                results["commit-failure-accounting"].detail,
            )

    def test_a_commit_outcome_asl_never_writes_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(
                history,
                success=False,
                status_result="exploded",
                returncode=1,
                status_error="something else entirely",
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "which is no commit outcome ASL writes",
                results["commit-failure-accounting"].detail,
            )

    def test_a_failed_commit_that_recorded_no_reason_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            commit_dir = history[3] / "commit-0001"
            self._recorded_commit(
                history,
                success=False,
                status_result="nonzero-exit",
                returncode=1,
                # An empty stream is a stream nobody can read a reason out of.
                stderr_path=str(commit_dir / "stderr.log"),
                stderr_chars=0,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "failed without recording why",
                results["commit-failure-accounting"].detail,
            )

    def test_a_run_that_ended_owing_commits_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # A commit failure no longer pauses the run, so the audit is what
            # makes the loss visible to a supervisor at all.
            self._state_with(
                history,
                unresolved_commit_groups=[
                    {
                        "group_id": "g1",
                        "scope": "session",
                        "session_id": "2",
                        "status": "failed",
                        "failed_alias": "app",
                        "remaining_aliases": ["app"],
                        "committed_aliases": [],
                    }
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-group-resolution"].status)
            self.assertIn("session 2 in app", results["commit-group-resolution"].detail)

    def test_a_run_that_completed_every_group_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._state_with(
                history,
                unresolved_commit_groups=[],
                commit_groups=[{"group_id": "g1", "status": "complete"}],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["commit-group-resolution"].status)
            self.assertIn(
                "reached completion", results["commit-group-resolution"].detail
            )

    def test_a_failed_group_is_read_from_an_older_run_that_names_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # A run recorded before the field existed still says what it owes,
            # in the group rows themselves.
            self._state_with(
                history,
                commit_groups=[
                    {
                        "group_id": "g1",
                        "status": "failed",
                        "session_id": None,
                        "failed_alias": "app",
                    }
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-group-resolution"].status)
            self.assertIn(
                "the end-of-run commit in app",
                results["commit-group-resolution"].detail,
            )

    def test_a_refused_restore_accounts_for_its_own_failed_exit(self) -> None:
        results = self._restore_checks(self._refused_restore_bundle())
        self.assertEqual("pass", results["conversation-restore-refusal"].status)
        self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_refused_restore_does_not_excuse_another_turn(self) -> None:
        bundle = self._refused_restore_bundle()
        bundle["events"]["records"].append(
            {
                "name": events.TURN_EXECUTION_FINISHED_EVENT,
                "payload": {"session": 4, "turn_index": 2, "returncode": 1},
            }
        )
        results = self._restore_checks(bundle)
        self.assertEqual("pass", results["conversation-restore-refusal"].status)
        self.assertEqual("fail", results["unclassified-anomalies"].status)


if __name__ == "__main__":
    unittest.main()
