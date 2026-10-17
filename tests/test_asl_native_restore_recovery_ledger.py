"""Tests for test asl native restore recovery ledger."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    conversation_resume,
    conversations,
    events,
    handoff_guards,
    harnesses,
    turn_execution_artifacts,
)
from tests.support_asl_native_restore_recovery import THREAD_ID


class RefusedRestoreDispositionTests(unittest.TestCase):
    """Where a refused restore lands among the turn's terminating branches."""

    def _result(self, **overrides: Any) -> harnesses.HarnessResult:
        fields: dict[str, Any] = {
            "name": "codex",
            "argv": ["codex"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "conversation_restore_refused": f"could not restore {THREAD_ID}",
        }
        fields.update(overrides)
        return harnesses.HarnessResult(**fields)

    def test_a_refused_restore_is_named_rather_than_filed_as_a_failed_harness(
        self,
    ) -> None:
        disposition = handoff_guards._terminating_result_disposition(
            self._result(), object(), {}
        )

        self.assertEqual("restore-refused", disposition)

    def test_it_outranks_the_uninspectable_plan_that_would_hide_it(self) -> None:
        # An unreadable plan is a condition that predates this turn, and the
        # ledger still has to be told the conversation is gone.
        disposition = handoff_guards._terminating_result_disposition(
            self._result(), None, {}
        )

        self.assertEqual("restore-refused", disposition)

    def test_a_stopped_turn_still_belongs_to_whatever_stopped_it(self) -> None:
        # None of these say the history is gone: the turn never got far enough
        # to find out, so the conversation keeps its standing.
        for field, expected in (
            ("interrupted", "interrupted"),
            ("timed_out", "timeout"),
            ("aborted", "aborted"),
        ):
            with self.subTest(field=field):
                disposition = handoff_guards._terminating_result_disposition(
                    self._result(**{field: True}), object(), {}
                )

                self.assertEqual(expected, disposition)

    def test_the_turn_record_names_the_refusal_the_run_pauses_under(self) -> None:
        status = turn_execution_artifacts.turn_status_result(
            result=self._result(),
            status_changes=(),
            pending_completed=(),
            session_id="1",
            runner_signals={},
            plan_error=None,
        )

        self.assertEqual(events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED, status)


class RefusedRestoreLedgerTests(unittest.TestCase):
    """What the refusal writes, and what every later launch makes of it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.ledger = self.tmp / "ledger"
        self.ledger.mkdir()
        self.binding = conversation_resume.turn_binding(
            plan_dir=self.plan_dir, session_id="1", harness="codex", cwd=self.tmp
        )
        reference = conversations.ConversationReference(
            binding=self.binding,
            native_id=THREAD_ID,
        )
        conversations.open_conversation(self.ledger, reference)
        conversations.confirm_conversation(self.ledger, reference)

    def _refuse(self) -> conversation_resume.RestoreRefusal:
        return conversation_resume.refuse_unrestorable_conversation(
            self.ledger,
            binding=self.binding,
            conversation=THREAD_ID,
            detail=f"could not restore conversation {THREAD_ID}: no such thread",
        )

    def _continuation(self, *, granted: bool) -> tuple[Any, Any]:
        authorization = conversation_resume.FreshConversationAuthorization(granted)
        continuation = conversation_resume.plan_turn_continuation(
            self.ledger, binding=self.binding, authorization=authorization
        )
        return continuation, authorization

    def test_a_confirmed_conversation_resumes_until_it_is_refused(self) -> None:
        continuation, authorization = self._continuation(granted=False)

        self.assertEqual(
            conversation_resume.CONTINUATION_RESUMED, continuation.disposition
        )
        self.assertEqual(
            THREAD_ID,
            continuation.resume_conversation_id,
        )
        self.assertFalse(authorization.consumed)

    def test_the_refusal_retires_the_entry_as_unusable(self) -> None:
        refusal = self._refuse()

        self.assertTrue(refusal.retired)
        self.assertIsNone(refusal.error)
        # Named by locator, the way every other continuation record names a
        # conversation, so one reader can follow both.
        self.assertEqual(
            f"codex:{THREAD_ID}",
            refusal.conversation,
        )
        selection = conversations.select_conversation(self.ledger, self.binding)
        self.assertEqual(conversations.SELECTION_RETIRED, selection.state)
        self.assertIn(
            conversation_resume.RESTORE_REFUSED_RETIREMENT_REASON, selection.detail
        )

    def test_an_ordinary_launch_afterwards_stops_for_recovery(self) -> None:
        self._refuse()

        continuation, authorization = self._continuation(granted=False)

        self.assertTrue(continuation.paused)
        self.assertEqual(
            conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
            continuation.disposition,
        )
        # No latest, no fresh: an unauthorized launch starts nothing at all.
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertFalse(authorization.consumed)

    def test_the_block_survives_every_later_ordinary_launch(self) -> None:
        self._refuse()

        for attempt in range(3):
            with self.subTest(attempt=attempt):
                continuation, _ = self._continuation(granted=False)

                self.assertTrue(continuation.paused)

    def test_one_authorized_launch_supersedes_it_and_opens_fresh(self) -> None:
        self._refuse()

        continuation, authorization = self._continuation(granted=True)

        self.assertEqual(
            conversation_resume.CONTINUATION_FRESH, continuation.disposition
        )
        # Fresh means fresh: the dead conversation is not offered to the turn
        # under another name, and nothing picks "the latest" in its place.
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertTrue(continuation.captures)
        self.assertTrue(authorization.consumed)

    def test_the_authorization_is_still_one_use(self) -> None:
        self._refuse()
        authorization = conversation_resume.FreshConversationAuthorization(True)

        first = conversation_resume.plan_turn_continuation(
            self.ledger, binding=self.binding, authorization=authorization
        )
        second = conversation_resume.plan_turn_continuation(
            self.ledger, binding=self.binding, authorization=authorization
        )

        self.assertEqual(conversation_resume.CONTINUATION_FRESH, first.disposition)
        self.assertTrue(second.paused)

    def test_a_refusal_that_names_no_conversation_writes_nothing(self) -> None:
        refusal = conversation_resume.refuse_unrestorable_conversation(
            self.ledger,
            binding=self.binding,
            conversation=None,
            detail="the conversation was not restored",
        )

        # Reported rather than guessed at: retiring "whatever this binding
        # points at" would discard a record on no evidence.
        self.assertFalse(refusal.retired)
        self.assertIsNotNone(refusal.error)
        selection = conversations.select_conversation(self.ledger, self.binding)
        self.assertEqual(conversations.SELECTION_RESUMABLE, selection.state)

    def test_the_names_it_uses_are_in_the_vocabularies_readers_share(self) -> None:
        self.assertIn(
            conversation_resume.CONTINUATION_RESTORE_REFUSED,
            conversation_resume.CONTINUATION_DISPOSITIONS,
        )
        self.assertIn(
            conversation_resume.RESTORE_REFUSED_RETIREMENT_REASON,
            conversations.RETIREMENT_REASONS,
        )


if __name__ == "__main__":
    unittest.main()
