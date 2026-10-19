"""Attempt ledger lifetime, interruption policy, and continuation boundaries."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    commit_settlement,
    continuation_guards,
    events,
    pause_policy,
    review_transitions,
    run_selection,
    verification_cli,
    verification_transitions,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import assert_no_runner_import
from .support_asl_continuation_guards import SESSION_ID, SESSION_LABEL, registered_guard

# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class SessionAttemptLedgerTests(unittest.TestCase):
    """What the run-scoped ledger keeps, publishes and forgets."""

    def setUp(self) -> None:
        self.ledger = continuation_guards.SessionAttempts()

    def test_the_newest_cleared_attempt_is_what_the_next_one_merges_against(
        self,
    ) -> None:
        first = registered_guard(changes={"SVC": ("first.txt",)})
        self.ledger.record(
            session_id=SESSION_ID, turn_index=1, disposition="timeout", guard=first
        )
        self.assertEqual(first, self.ledger.latest(SESSION_ID))

        second = registered_guard(changes={"SVC": ("first.txt", "second.txt")})
        self.ledger.record(
            session_id=SESSION_ID, turn_index=2, disposition=None, guard=second
        )
        self.assertEqual(second, self.ledger.latest(SESSION_ID))

    def test_a_record_that_cannot_be_merged_never_becomes_the_latest(self) -> None:
        cleared = registered_guard(changes={"SVC": ("first.txt",)})
        self.ledger.record(
            session_id=SESSION_ID, turn_index=1, disposition="timeout", guard=cleared
        )
        self.ledger.record(
            session_id=SESSION_ID,
            turn_index=2,
            disposition="timeout",
            guard=registered_guard(changes={"OAK": ("blind.txt",)}, passed=False),
        )
        # The cleared attempt is still what a third attempt accumulates onto:
        # an unmergeable record neither replaces it nor joins it.
        self.assertEqual(cleared, self.ledger.latest(SESSION_ID))

    def test_sessions_do_not_borrow_each_others_evidence(self) -> None:
        self.ledger.record(
            session_id="1",
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes={"SVC": ("first.txt",)}),
        )
        self.assertIsNone(self.ledger.latest("2"))

    def test_every_attempt_is_summarized_even_when_it_is_not_merged(self) -> None:
        self.ledger.record(
            session_id=SESSION_ID,
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes={"SVC": ("first.txt",)}),
        )
        self.ledger.record(
            session_id=SESSION_ID, turn_index=2, disposition=None, guard=None
        )

        summaries = self.ledger.summaries(SESSION_ID)
        self.assertEqual((1, 2), tuple(item.turn_index for item in summaries))
        self.assertEqual(
            ("timeout", continuation_guards.DISPOSITION_SETTLED),
            tuple(item.disposition for item in summaries),
        )
        self.assertEqual(
            {
                SESSION_ID: [
                    {
                        "turn_index": 1,
                        "disposition": "timeout",
                        "repositories": ["SVC"],
                        "qualified_paths": 1,
                    },
                    {
                        "turn_index": 2,
                        "disposition": continuation_guards.DISPOSITION_SETTLED,
                        "repositories": [],
                        "qualified_paths": 0,
                    },
                ]
            },
            self.ledger.to_payload(),
        )

    def test_a_completed_session_stops_being_accumulated(self) -> None:
        self.ledger.record(
            session_id=SESSION_ID,
            turn_index=1,
            disposition="timeout",
            guard=registered_guard(changes={"SVC": ("first.txt",)}),
        )
        retired = self.ledger.retire(SESSION_ID)

        self.assertEqual((1,), tuple(item.turn_index for item in retired))
        self.assertIsNone(self.ledger.latest(SESSION_ID))
        self.assertEqual((), self.ledger.summaries(SESSION_ID))
        self.assertEqual({}, self.ledger.to_payload())


# ---------------------------------------------------------------------------
# What an interruption cannot earn
# ---------------------------------------------------------------------------


class InterruptionEarnsNothingTests(unittest.TestCase):
    """An urgent redirection retains bytes and gains nothing else.

    Accumulating evidence across attempts widens what a *recoverable* pause
    settles, so the boundary that keeps an interruption from settling at all has
    to be re-stated against the same machinery rather than assumed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.event_path = self.tmp_path / "events.jsonl"

    def retained(self, **overrides: Any) -> commit_settlement.RetainedWorkContext:
        values: dict[str, Any] = {
            "session_id": SESSION_ID,
            "session_label": SESSION_LABEL,
            "turn_payload": {
                "workspace_write_guard": registered_guard(
                    changes={"SVC": ("first.txt", "second.txt")}
                )
            },
            "result_path": self.tmp_path / "result.json",
            "artifact_dir": self.tmp_path / "turn-0002",
            "is_review_session": False,
            "is_repair_session": False,
        }
        values.update(overrides)
        return commit_settlement.RetainedWorkContext(**values)

    def test_an_interruption_is_not_a_recoverable_pause(self) -> None:
        self.assertNotIn("interrupted", pause_policy.RECOVERABLE_PAUSE_REASONS)
        self.assertIn("timeout", pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_no_checkpoint_commit_is_owed_for_an_interruption(self) -> None:
        for commit_mode in (run_selection.COMMIT_MODE_PER_SESSION, None):
            for is_repair_session in (False, True):
                with self.subTest(mode=commit_mode, repair=is_repair_session):
                    self.assertFalse(
                        commit_settlement._paused_work_commit_is_owed(
                            self.retained(is_repair_session=is_repair_session),
                            pause_reason="interrupted",
                            commit_mode=commit_mode,
                        )
                    )

    def test_an_ordinary_timeout_keeps_the_settlement_it_already_had(self) -> None:
        self.assertTrue(
            commit_settlement._paused_work_commit_is_owed(
                self.retained(),
                pause_reason="timeout",
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            )
        )

    def write_events(self, *reasons: str) -> None:
        """A run's pause history, as the repair counters read it back."""
        self.event_path.write_text(
            "".join(
                json.dumps(
                    {
                        "name": events.RUN_PAUSED_EVENT,
                        "payload": {
                            "reason": reason,
                            "details": {"session": SESSION_ID},
                        },
                    }
                )
                + "\n"
                for reason in reasons
            ),
            encoding="utf-8",
        )

    def test_an_interrupted_attempt_consumes_no_repair_budget(self) -> None:
        # The budget is derived from the session's own `verify-failed` history,
        # and an interrupted attempt never verified, so it appends nothing the
        # counter can read. No counter is decremented to achieve that.
        self.write_events("interrupted", "interrupted")

        budget = verification_transitions.verification_repair_budget(
            self.event_path, SESSION_ID
        )

        self.assertEqual(1, budget.attempt)
        self.assertEqual((), budget.prior_fingerprints)
        self.assertEqual((), budget.references)

    def test_a_verification_failure_beside_it_still_spends_one(self) -> None:
        # The companion claim: the budget is not simply inert here.
        self.write_events(
            "interrupted", verification_cli.VERIFY_EXIT_REASON_FAILED, "interrupted"
        )

        self.assertEqual(
            2,
            verification_transitions.verification_repair_budget(
                self.event_path, SESSION_ID
            ).attempt,
        )

    def test_an_interrupted_review_attempt_consumes_no_verdict_retry(self) -> None:
        self.write_events("interrupted", "interrupted")

        self.assertEqual(
            [],
            review_transitions.read_prior_review_verdict_retries(
                self.event_path, SESSION_ID
            ),
        )

    def test_an_interruption_earns_no_completion(self) -> None:
        # `interrupted` is a terminating disposition that breaks the loop before
        # `settle_turn`, so there is no path on which it publishes a turn. The
        # runner body is the proof: the branch pauses and breaks.
        source = run_loop_view.assembled_source()
        self.assertRegex(
            source,
            r"if terminating_disposition == ['\"]interrupted['\"]:\s+"
            r"turn_dispositions\.pause_interrupted_run\(",
        )


# ---------------------------------------------------------------------------
# The boundary's own shape
# ---------------------------------------------------------------------------


class ContinuationBoundaryTests(unittest.TestCase):
    """The module's one-way dependency and its single wiring sites."""

    def test_continuation_guards_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, continuation_guards)

    def test_the_module_opens_no_file_of_its_own(self) -> None:
        # Every durable write of the accumulated record goes through the
        # persistence seam its caller holds; a file opened here would be a
        # second writer of the same evidence.
        source = ast.parse(
            Path(continuation_guards.__file__).read_text(encoding="utf-8")
        )
        stray = [
            f"line {node.lineno}: {ast.unparse(node.func)}"
            for node in ast.walk(source)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"open", "write_text", "write_bytes", "mkdir"}
        ]
        self.assertEqual([], stray)

    def test_one_arming_site_and_one_recording_site(self) -> None:
        # The ledger's meaning depends on where it is written: an attempt joins
        # it exactly where retained work is armed, which is the one place every
        # post-turn write guard has already cleared.
        source = run_loop_view.assembled_source()
        self.assertEqual(1, source.count("ctx.retained_work = commit_settlement."))
        self.assertEqual(1, source.count("ctx.session_attempts.record("))

    def test_the_ledger_retires_where_the_conversation_does(self) -> None:
        from agent_session_loop import turn_settlement

        source = Path(turn_settlement.__file__).read_text(encoding="utf-8")
        self.assertEqual(1, source.count("ctx.session_attempts.retire("))
        self.assertLess(
            source.index("conversation_resume.retire_session_conversations("),
            source.index("ctx.session_attempts.retire("),
        )

    def test_the_events_the_settlement_writes_are_unchanged_in_shape(self) -> None:
        # Guard against the accumulation quietly acquiring an event stream of
        # its own: the record it produces is published by the turn's own
        # artifact and the run state, not by a new event name.
        self.assertFalse(
            any(
                name.startswith("continuation")
                for name in dir(events)
                if name.endswith("_EVENT")
            )
        )


if __name__ == "__main__":
    unittest.main()
