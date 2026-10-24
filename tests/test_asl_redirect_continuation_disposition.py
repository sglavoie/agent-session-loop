"""Pending lineage, dispositions and harness pinning of a corrected turn.

Split from tests/test_asl_redirect_continuation.py."""

from __future__ import annotations

import json
import unittest
from typing import Any

from agent_session_loop import (
    clock,
    conversations,
    events,
    handoff_guards,
    harnesses,
    pause_policy,
    redirect_continuation,
    redirect_store_validation,
    workspaces,
)

from .asl_redirect_continuation_support import (
    FEEDBACK,
    SESSION_ID,
    target,
)

ASL_TEST_PORTABLE = True


class PendingRedirectTests(unittest.TestCase):
    """What survives the turn, and what deliberately does not."""

    def accepted(
        self, operation_id: str = "op-1", *, feedback: str = FEEDBACK
    ) -> redirect_continuation.AcceptedRedirect:
        return redirect_continuation.AcceptedRedirect(
            operation_id=operation_id,
            target=target(),
            accepted_at=clock.utc_iso_now(),
            feedback_digest=redirect_store_validation.feedback_digest(feedback),
            feedback_bytes=len(feedback.encode("utf-8")),
        )

    def test_a_turn_that_accepted_nothing_carries_the_previous_correction(self) -> None:
        previous = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted(),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        unchanged = redirect_continuation.PendingRedirect.after_turn(
            (),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=2,
            previous=previous,
        )

        self.assertIs(previous, unchanged)
        self.assertIsNone(
            redirect_continuation.PendingRedirect.after_turn(
                (),
                session_id=SESSION_ID,
                harness="codex",
                conversation_id=None,
                turn_index=1,
            )
        )

    def test_a_second_correction_extends_the_lineage_rather_than_restarting_it(
        self,
    ) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-1"),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        second = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-2", feedback="and rerun the check"),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=2,
            previous=first,
        )

        assert first is not None and second is not None
        self.assertEqual(1, first.attempt)
        self.assertEqual(2, second.attempt)
        self.assertEqual(("op-1", "op-2"), second.operation_ids)
        self.assertEqual(2, len(second.feedback_digests))
        self.assertEqual(
            first.feedback_bytes + len(b"and rerun the check"),
            second.feedback_bytes,
        )
        self.assertEqual(2, second.predecessor_turn_index)

    def test_a_correction_from_another_session_starts_its_own_lineage(self) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-1"),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        other = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-2"),),
            session_id="2",
            harness="claude",
            conversation_id="conv-2",
            turn_index=2,
            previous=first,
        )

        assert other is not None
        self.assertEqual(1, other.attempt)
        self.assertEqual(("op-2",), other.operation_ids)

    def test_one_correction_is_folded_once(self) -> None:
        folded = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-1"), self.accepted("op-1")),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        assert folded is not None
        self.assertEqual(("op-1",), folded.operation_ids)
        self.assertEqual(len(FEEDBACK.encode()), folded.feedback_bytes)

    def test_the_words_never_leave_the_journal(self) -> None:
        pending = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted(),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        assert pending is not None
        serialized = json.dumps(pending.to_payload())
        self.assertNotIn(FEEDBACK, serialized)
        self.assertIn(redirect_store_validation.feedback_digest(FEEDBACK), serialized)
        self.assertEqual(len(FEEDBACK.encode()), pending.to_payload()["feedback_bytes"])

    def test_an_id_the_conversation_ledger_would_refuse_is_dropped_not_raised(
        self,
    ) -> None:
        # The journal's id rule is broader than the ledger's, and a correction
        # whose identifier the ledger cannot store is still a correction: the
        # worker reads its words out of the journal either way.
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=("op-1", "not a valid id", "op-2"),
            feedback_digests=("a", "b", "c"),
            feedback_bytes=3,
        )

        self.assertEqual(("op-1", "op-2"), pending.resumable_operation_ids())

    def test_the_ledger_s_own_bound_is_honoured(self) -> None:
        ids = tuple(
            f"op-{index}"
            for index in range(conversations.MAX_INTERRUPTED_OPERATIONS + 5)
        )
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=ids,
            feedback_digests=ids,
            feedback_bytes=0,
        )

        resumable = pending.resumable_operation_ids()

        self.assertEqual(conversations.MAX_INTERRUPTED_OPERATIONS, len(resumable))
        # Accepted by the ledger, which is the only reason to narrow at all.
        conversations.resume_intent(
            conversations.ConversationReference(
                binding=conversations.ConversationBinding(
                    plan_dir="/plan",
                    session_id=SESSION_ID,
                    harness="codex",
                    workspace=workspaces.WorkspaceBinding(
                        workspace_root="/plan", registry_digest="0" * 64
                    ),
                    access_profile="worker",
                ),
                native_id="conv-1",
            ),
            interrupted_operations=resumable,
        )


class DispositionTests(unittest.TestCase):
    """The disposition is read off the acceptance, not off the exit."""

    def payload(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "target": target().to_payload(),
            "accepted": [{"operation_id": "op-1"}],
            "refused": [],
            "settled": True,
            "control": None,
        }
        record.update(overrides)
        return {redirect_continuation.TURN_PAYLOAD_KEY: record}

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        values: dict[str, Any] = {
            "argv": ["codex"],
            "returncode": 137,
            "stdout": "",
            "stderr": "",
            "aborted": True,
        }
        values.update(overrides)
        return harnesses.HarnessResult(**values)

    def test_an_acceptance_makes_the_turn_redirected(self) -> None:
        self.assertTrue(redirect_continuation.turn_was_redirected(self.payload()))

    def test_a_turn_with_no_gate_record_is_not_redirected(self) -> None:
        self.assertFalse(redirect_continuation.turn_was_redirected({}))
        self.assertFalse(
            redirect_continuation.turn_was_redirected({"redirect": "nonsense"})
        )

    def test_a_refusal_alone_is_not_a_redirect(self) -> None:
        payload = self.payload(
            accepted=[],
            refused=[{"operation_id": "op-1", "reason": "turn-already-settled"}],
        )

        self.assertFalse(redirect_continuation.turn_was_redirected(payload))

    def disposition(self, result: harnesses.HarnessResult, **overrides: Any):
        return handoff_guards._terminating_result_disposition(
            result, None, self.payload(**overrides)
        )

    def test_a_redirect_outranks_the_abort_it_shares_a_signal_with(self) -> None:
        # A redirect arms the same abort signal an operator stop does, so the
        # result cannot tell them apart and the record has to.
        self.assertEqual(
            redirect_continuation.DISPOSITION_REDIRECTED,
            self.disposition(self.result()),
        )

    def test_a_redirect_outranks_a_timeout(self) -> None:
        self.assertEqual(
            redirect_continuation.DISPOSITION_REDIRECTED,
            self.disposition(
                self.result(returncode=124, aborted=False, timed_out=True)
            ),
        )

    def test_a_worker_that_finished_inside_the_window_is_still_redirected(self) -> None:
        self.assertEqual(
            redirect_continuation.DISPOSITION_REDIRECTED,
            self.disposition(self.result(returncode=0, aborted=False)),
        )

    def test_the_operator_s_own_interrupt_outranks_a_redirect(self) -> None:
        self.assertEqual("interrupted", self.disposition(self.result(interrupted=True)))

    def test_a_refused_correction_leaves_the_turn_to_its_own_result(self) -> None:
        self.assertEqual(
            "aborted",
            self.disposition(
                self.result(),
                accepted=[],
                refused=[{"operation_id": "op-1", "reason": "turn-already-settled"}],
            ),
        )

    def test_the_successor_block_is_not_an_answerable_pause(self) -> None:
        reason = events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED
        self.assertIn(reason, events.PAUSE_REASONS)
        self.assertIn(reason, pause_policy.UNANSWERABLE_OUTCOME_PAUSE_REASONS)
        policy = pause_policy.classify_pause(reason)
        # Non-recoverable is what the automatic paused-work commit is gated on,
        # so this pause cannot produce a commit of its own, and there is no
        # answer an operator could send that would clear it.
        self.assertFalse(policy["recoverable"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, policy["user_action"])
