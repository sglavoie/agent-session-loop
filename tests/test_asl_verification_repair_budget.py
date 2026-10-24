"""Asl verification repair budget regressions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    verification_cli,
    verification_transitions,
)

ASL_TEST_PORTABLE = True


class VerificationFingerprintTests(unittest.TestCase):
    def _details(self, **overrides: Any) -> dict[str, Any]:
        failure = {
            "name": "tests",
            "kind": "command",
            "failure_reason": "exit-status",
            "returncode": 1,
            "stdout_sha256": "aa",
            "stderr_sha256": "bb",
        }
        failure.update(overrides)
        return {"failures": [failure]}

    def test_an_identical_failure_folds_to_the_same_slug(self) -> None:
        first = verification_transitions.verification_fingerprint(self._details())
        second = verification_transitions.verification_fingerprint(self._details())
        assert first is not None
        self.assertEqual(first, second)
        self.assertTrue(
            first.startswith(verification_transitions.VERIFICATION_FINGERPRINT_PREFIX)
        )

    def test_changed_output_yields_a_different_slug(self) -> None:
        self.assertNotEqual(
            verification_transitions.verification_fingerprint(self._details()),
            verification_transitions.verification_fingerprint(
                self._details(stdout_sha256="cc")
            ),
        )

    def test_failure_order_does_not_change_the_slug(self) -> None:
        one = {"failures": [self._details()["failures"][0], {"name": "lint"}]}
        other = {"failures": [{"name": "lint"}, self._details()["failures"][0]]}
        self.assertEqual(
            verification_transitions.verification_fingerprint(one),
            verification_transitions.verification_fingerprint(other),
        )

    def test_evidence_free_input_has_no_slug(self) -> None:
        for details in (None, {}, {"failures": "nope"}, {"failures": []}):
            with self.subTest(details=details):
                self.assertIsNone(
                    verification_transitions.verification_fingerprint(details)
                )


class VerificationRepairBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def _pause(self, session: str, *, stdout: str = "aa") -> None:
        self._append(
            events.RUN_PAUSED_EVENT,
            {
                "reason": verification_cli.VERIFY_EXIT_REASON_FAILED,
                "details": {
                    "session": session,
                    "failures": [
                        {
                            "name": "tests",
                            "kind": "command",
                            "failure_reason": "exit-status",
                            "returncode": 1,
                            "stdout_sha256": stdout,
                            "stderr_sha256": "bb",
                            "stdout_artifact_path": "/tmp/out.txt",
                        }
                    ],
                },
            },
        )

    def test_a_missing_log_leaves_the_first_attempt_unbudgeted(self) -> None:
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        self.assertEqual(budget.attempt, 1)
        self.assertEqual(budget.references, ())
        self.assertEqual(budget.prior_fingerprints, ())

    def test_only_this_session_s_verify_failures_are_counted(self) -> None:
        self._pause("1")
        self._pause("2")
        self._append(
            events.RUN_PAUSED_EVENT,
            {"reason": "no-contract", "details": {"session": "1"}},
        )
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        self.assertEqual(budget.attempt, 2)
        self.assertEqual(len(budget.prior_fingerprints), 1)

    def test_references_name_the_failure_the_next_turn_must_fix(self) -> None:
        self._pause("1")
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        joined = "\n".join(budget.references)
        self.assertIn("Prior verification attempt 1 failed", joined)
        self.assertIn("Failed check `tests` (command): exit-status.", joined)
        self.assertIn("Full stdout for `tests` retained at /tmp/out.txt.", joined)
        self.assertIn("do not re-submit the same failing result unchanged", joined)

    def test_an_identical_failure_exhausts_the_loop_under_the_cap(self) -> None:
        self._pause("1")
        budget = verification_transitions.verification_repair_budget(
            self.event_path, "1"
        )
        repair = verification_transitions.adjudicate_verification_repair(
            details={
                "failures": [
                    {
                        "name": "tests",
                        "kind": "command",
                        "failure_reason": "exit-status",
                        "returncode": 1,
                        "stdout_sha256": "aa",
                        "stderr_sha256": "bb",
                    }
                ]
            },
            budget=budget,
        )
        self.assertEqual(repair.attempt, 2)
        self.assertTrue(repair.exhausted)

    def test_a_changed_failure_under_the_cap_stays_eligible(self) -> None:
        self._pause("1", stdout="aa")
        repair = verification_transitions.adjudicate_verification_repair(
            details={
                "failures": [
                    {
                        "name": "tests",
                        "kind": "command",
                        "failure_reason": "exit-status",
                        "returncode": 1,
                        "stdout_sha256": "cc",
                        "stderr_sha256": "bb",
                    }
                ]
            },
            budget=verification_transitions.verification_repair_budget(
                self.event_path, "1"
            ),
        )
        self.assertFalse(repair.exhausted)
        self.assertEqual(
            repair.to_payload(),
            {
                "verification_fingerprint": repair.fingerprint,
                "verification_repair_attempt": 2,
                "verification_repair_exhausted": False,
                "verification_repair_max_attempts": (
                    verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS
                ),
            },
        )

    def test_the_attempt_cap_stops_a_reviewer_producing_new_failures(self) -> None:
        self._pause("1", stdout="aa")
        self._pause("1", stdout="cc")
        repair = verification_transitions.adjudicate_verification_repair(
            details={
                "failures": [
                    {
                        "name": "tests",
                        "kind": "command",
                        "failure_reason": "exit-status",
                        "returncode": 1,
                        "stdout_sha256": "dd",
                        "stderr_sha256": "bb",
                    }
                ]
            },
            budget=verification_transitions.verification_repair_budget(
                self.event_path, "1"
            ),
        )
        self.assertEqual(repair.attempt, 3)
        self.assertTrue(repair.exhausted)

    def test_a_failure_without_evidence_is_bounded_by_the_cap_alone(self) -> None:
        self.assertFalse(
            verification_transitions.is_verification_repair_exhausted(None, ("x",), 1)
        )
        self.assertTrue(
            verification_transitions.is_verification_repair_exhausted(
                None,
                (),
                verification_transitions.MAX_VERIFICATION_REPAIR_ATTEMPTS + 1,
            )
        )


class EventLogReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def test_an_absent_log_reads_as_empty(self) -> None:
        self.assertEqual(
            verification_transitions.read_event_records(self.event_path), []
        )

    def test_malformed_lines_are_skipped_not_fatal(self) -> None:
        self.event_path.write_text(
            "\n".join(
                [
                    "",
                    "{not json",
                    "[1, 2]",
                    json.dumps(
                        {
                            "name": events.VERIFICATION_FINISHED_EVENT,
                            "payload": {"a": 1},
                        }
                    ),
                    json.dumps(
                        {"name": events.VERIFICATION_FINISHED_EVENT, "payload": "no"}
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            verification_transitions.read_verification_finished_events(self.event_path),
            [{"a": 1}],
        )


if __name__ == "__main__":
    unittest.main()
