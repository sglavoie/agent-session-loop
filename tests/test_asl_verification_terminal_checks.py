"""Asl verification terminal checks regressions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    review,
    review_continuation,
    verification_transitions,
)

from .support_asl_verification_transitions import DEFERRED_REASON

ASL_TEST_PORTABLE = True


class TerminalRequiredCheckTests(unittest.TestCase):
    """Session 2's success policy, asked of payloads rather than of runs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def _append(self, name: str, payload: dict[str, Any]) -> None:
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def _acceptance(self, *names: str) -> dict[str, Any]:
        return {
            "accepted": True,
            "reason": DEFERRED_REASON,
            "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
            "deferred_failure_count": len(names),
            "deferred_failure_names": list(names),
        }

    def _defer(self, session: str, *names: str) -> None:
        self._append(
            events.VERIFICATION_FINISHED_EVENT,
            {
                "run_id": "r1",
                "session_id": session,
                "turn_index": 1,
                "passed": False,
                "acceptance": self._acceptance(*names),
            },
        )

    def test_a_clean_run_is_not_blocked(self) -> None:
        self.assertIsNone(
            verification_transitions.terminal_blocker({"turns": []}, self.event_path)
        )

    def test_an_unredeemed_deferral_blocks_a_terminal_ending(self) -> None:
        self._defer("1", "broad:tests")
        blocker = verification_transitions.terminal_blocker(
            {"reason": "all-complete", "turns": []}, self.event_path
        )
        assert blocker is not None
        self.assertEqual(
            blocker.reason, verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON
        )
        self.assertIn("broad:tests", blocker.message)
        self.assertEqual(len(blocker.details["deferred_unresolved"]), 1)

    def test_a_non_utf8_or_deeply_nested_line_spoils_only_itself(self) -> None:
        self._defer("1", "broad:tests")
        with self.event_path.open("ab") as handle:
            handle.write(b"\xff\xfe torn\n" + b"[" * 100000 + b"\n")
        blocker = verification_transitions.terminal_blocker(
            {"reason": "all-complete", "turns": []}, self.event_path
        )
        assert blocker is not None
        self.assertIn("broad:tests", blocker.message)

    def test_a_later_all_clear_review_redeems_the_deferral(self) -> None:
        self._defer("1", "broad:tests")
        self._append(
            events.REVIEW_CYCLE_EVENT,
            {
                "cycle": 1,
                "review_session_id": "1",
                "outcome": review.REVIEW_OUTCOME_FINDINGS,
                "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            },
        )
        self._append(
            events.REVIEW_CYCLE_EVENT,
            {
                "cycle": 2,
                "review_session_id": "2",
                "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                "total_findings": 0,
                "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            },
        )
        self.assertEqual(
            verification_transitions.unresolved_deferred_verifications(
                {"turns": []}, self.event_path
            ),
            [],
        )

    def test_a_malformed_acceptance_is_not_evidence_of_a_deferral(self) -> None:
        for acceptance in (
            "nope",
            {},
            {"accepted": True, "deferred_failure_names": []},
            {
                "accepted": True,
                "reason": DEFERRED_REASON,
                "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                "deferred_failure_count": 2,
                "deferred_failure_names": ["broad:tests"],
            },
            {
                "accepted": True,
                "reason": DEFERRED_REASON,
                "review_outcome": review.REVIEW_OUTCOME_FINDINGS,
                "deferred_failure_count": 1,
                "deferred_failure_names": ["tests"],
            },
        ):
            with self.subTest(acceptance=acceptance):
                self.assertIsNone(
                    verification_transitions.accepted_broad_failure_deferral(
                        {"acceptance": acceptance}
                    )
                )

    def test_an_unpassed_turn_verification_blocks_even_without_a_pause(self) -> None:
        blocker = verification_transitions.terminal_blocker(
            {
                "reason": "all-complete",
                "turns": [
                    {
                        "turn_index": 1,
                        "verification": {
                            "session": "1",
                            "passed": False,
                            "reason": "verify-failed",
                        },
                    }
                ],
            },
            self.event_path,
        )
        assert blocker is not None
        self.assertEqual(
            blocker.reason, verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON
        )
        self.assertEqual(
            blocker.details["unpassed_verifications"],
            [{"session_id": "1", "turn_index": 1, "reason": "verify-failed"}],
        )

    def test_a_deferred_turn_verification_is_not_an_unpassed_one(self) -> None:
        self.assertEqual(
            verification_transitions.unpassed_run_verifications(
                {
                    "turns": [
                        {
                            "turn_index": 1,
                            "verification": {
                                "session": "1",
                                "passed": False,
                                "acceptance": self._acceptance("broad:tests"),
                            },
                        }
                    ]
                }
            ),
            [],
        )

    def test_required_checks_are_asked_about_before_the_review(self) -> None:
        # A run whose gates never passed is not made honest by a clear review,
        # so the broader claim is the one reported.
        self._defer("1", "broad:tests")
        blocker = verification_transitions.terminal_blocker(
            {
                "reason": "all-complete",
                "review_requested": True,
                "review_fix_disposition": "findings",
                "turns": [],
            },
            self.event_path,
        )
        assert blocker is not None
        self.assertEqual(
            blocker.reason, verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON
        )

    def test_the_review_clause_is_scoped_to_plan_completion_endings(self) -> None:
        payload = {
            "review_requested": True,
            "review_fix_disposition": "findings",
            "review_verdict": {
                "outcome": review.REVIEW_OUTCOME_FINDINGS,
                "total_findings": 2,
            },
            "turns": [],
        }
        for reason, blocked in (
            ("all-complete", True),
            ("stopped", False),
            ("max-sessions", False),
            ("", False),
        ):
            with self.subTest(reason=reason):
                blocker = verification_transitions.terminal_blocker(
                    {**payload, "reason": reason}, self.event_path
                )
                self.assertEqual(blocker is not None, blocked)
                if blocker is not None:
                    self.assertEqual(
                        blocker.reason, verification_transitions.REVIEW_NOT_CLEAR_REASON
                    )
                    self.assertEqual(blocker.details["unresolved_findings"], 2)

    def test_a_cleared_review_needs_all_three_facts(self) -> None:
        clear = {
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            ),
            "review_verdict": {
                "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                "total_findings": 0,
            },
        }
        self.assertTrue(verification_transitions.final_review_is_clear(clear))
        for override in (
            {"review_fix_disposition": "findings"},
            {"review_verdict": {"outcome": "findings", "total_findings": 0}},
            {
                "review_verdict": {
                    "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                    "total_findings": 1,
                }
            },
            {"review_verdict": None},
        ):
            with self.subTest(override=override):
                self.assertFalse(
                    verification_transitions.final_review_is_clear(
                        {**clear, **override}
                    )
                )

    def test_a_run_that_never_asked_for_a_review_is_not_blocked_by_one(self) -> None:
        self.assertIsNone(
            verification_transitions.terminal_blocker(
                {"reason": "all-complete", "turns": []}, self.event_path
            )
        )

    def test_recorded_cycles_fall_back_to_the_live_projection(self) -> None:
        self.assertEqual(
            verification_transitions.recorded_review_cycles(
                {"review_cycles": [{"cycle": 1}, "junk"]}, self.event_path
            ),
            [{"cycle": 1}],
        )


if __name__ == "__main__":
    unittest.main()
