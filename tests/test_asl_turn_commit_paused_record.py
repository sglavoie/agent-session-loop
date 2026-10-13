"""The record, message and eligibility rules of a retained-work settlement."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    commit_settlement,
    review_guard,
    run_commit,
    run_selection,
)
from tests.asl_turn_commit_support import PausedRetainedWorkCase


class PausedRetainedWorkRecordTests(PausedRetainedWorkCase):
    """What the settlement record carries and which pauses owe a commit."""

    def test_a_guard_that_did_not_pass_fails_with_no_group_to_retry(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",), passed=False),
        )

        assert record is not None
        self.assertEqual("failed", record["result"])
        self.assertIn("workspace write guard did not pass", record["error"])
        self.assertEqual([], self.launched)
        # Session 1's gap 1: the resolver raises before `create_group`, so the
        # failure leaves no durable row and nothing for the loop to re-arm.
        self.assertIsNone(record["commit_group"])
        self.assertNotIn("commit_groups", self.payload)
        self.assertEqual(1, len(self.soft_failures))
        self.assertIsNone(self.soft_failures[0]["group"])
        self.assertIsNone(self.soft_failures[0]["commit"])
        # The run still says it lost a commit, from the failure record alone.
        self.assertEqual(["worked.txt"], self._dirty(self.repositories["SVC"]))

    # -- what the record and the message carry ----------------------------

    def test_the_message_file_is_the_deterministic_marking(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        self._settle(self.registered, guard=self._guard(added_paths=("worked.txt",)))

        expected = run_commit._paused_work_commit_message(
            session_label="session 1",
            pause_reason="verify-failed",
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            repair_attempt=1,
            repair_max_attempts=2,
            run_id="run-paused",
        )
        self.assertEqual(
            self._message_file(), self.launched[0]["generation"].message_file
        )
        self.assertEqual(expected, self._message_file().read_text(encoding="utf-8"))
        # The message the settlement wrote is the message git kept.
        committed = review_guard._run_git(
            self.repositories["SVC"], ["log", "-1", "--pretty=%B"]
        ).stdout
        self.assertEqual(expected.strip(), committed.strip())

    def test_the_event_carries_the_record_the_turn_payload_keeps(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, turn_payload = self._settle(
            self.registered, guard=self._guard(added_paths=("worked.txt",))
        )

        assert record is not None
        published = self._events()
        self.assertEqual(1, len(published))
        self.assertEqual(
            {"run_id": "run-paused", "session_id": "1", **record}, published[0]
        )
        self.assertEqual(
            commit_settlement.PAUSED_WORK_COMMIT_GUARD,
            turn_payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]["guard"],
        )

    def test_the_repair_numbers_are_read_from_the_pause_details(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            pause_details={
                "verification_repair_attempt": 2,
                "verification_repair_max_attempts": 2,
                "verification_repair_exhausted": True,
            },
        )

        assert record is not None
        self.assertEqual(2, record["verification_repair_attempt"])
        self.assertEqual(2, record["verification_repair_max_attempts"])
        self.assertIs(True, record["verification_repair_exhausted"])
        # Settling an exhausted repair is deliberate: the work stays
        # inspectable through `git show` and the flag says the budget is spent.
        self.assertEqual("committed", record["result"])

    def test_details_that_carry_no_numbers_invent_none(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            pause_reason="timeout",
            pause_details={},
        )

        assert record is not None
        self.assertIsNone(record["verification_repair_attempt"])
        self.assertIsNone(record["verification_repair_max_attempts"])
        self.assertIsNone(record["verification_repair_exhausted"])
        self.assertEqual("timeout", record["pause_reason"])
        # And the message says nothing about an attempt it was never told.
        self.assertNotIn("Repair attempt", self._message_file().read_text("utf-8"))

    # -- which pauses owe a commit at all ---------------------------------

    def test_a_pause_that_owes_no_commit_settles_nothing(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        guard = self._guard(added_paths=("worked.txt",))
        for label, kwargs in (
            ("review session", {"is_review_session": True}),
            ("no commit mode", {"commit_mode": None}),
            ("non-recoverable pause", {"pause_reason": "commit-preflight"}),
        ):
            with self.subTest(case=label):
                record, turn_payload = self._settle(
                    self.registered, guard=guard, **kwargs
                )
                self.assertIsNone(record)
                self.assertNotIn(commit_settlement.PAUSED_WORK_COMMIT_KEY, turn_payload)
        self.assertEqual([], self.launched)
        self.assertEqual([], self._events())

    def test_a_repair_child_owns_its_own_retained_work_commit(self) -> None:
        self._write(self.repositories["SVC"], "worked.txt")
        record, _turn_payload = self._settle(
            self.registered,
            guard=self._guard(added_paths=("worked.txt",)),
            commit_mode=None,
            is_repair_session=True,
        )

        assert record is not None
        self.assertEqual("committed", record["result"])
        self.assertIsNone(record["commit_mode"])


if __name__ == "__main__":
    unittest.main()
