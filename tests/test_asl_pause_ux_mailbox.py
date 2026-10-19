"""Test asl pause ux mailbox."""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    harnesses,
    mailbox,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    run_coordination_mailbox_question,
    run_coordination_mailbox_wait,
    run_coordination_wait_models,
    run_persistence,
)
from agent_session_loop import run_coordination_constants as wait_budgets
from tests import support_asl_pause_ux as fixtures


class ArtifactSafetyAndPauseUxTests(fixtures.ArtifactSafetyAndPauseUxCase):
    def test_mailbox_question_identity_is_stable_uuid5_per_turn(self) -> None:
        first = run_coordination_mailbox_question.mailbox_question_id(
            plan_dir=Path("/tmp/plan"),
            run_id="run-1",
            session_id="10",
            turn_index=3,
        )
        repeated = run_coordination_mailbox_question.mailbox_question_id(
            plan_dir=Path("/tmp/plan"),
            run_id="run-1",
            session_id="10",
            turn_index=3,
        )
        next_turn = run_coordination_mailbox_question.mailbox_question_id(
            plan_dir=Path("/tmp/plan"),
            run_id="run-1",
            session_id="10",
            turn_index=4,
        )
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, next_turn)
        self.assertEqual(5, uuid.UUID(first).version)

    def test_mailbox_reply_wait_lease_extensions_outlive_the_ceiling(
        self,
    ) -> None:
        question_id = "00000000-0000-0000-0000-000000000010"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        view = mailbox_snapshot.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(question,),
            last_seq=1,
        )
        lease_record = mailbox_models.MailboxLease(
            supervisor_id="00000000-0000-0000-0000-000000000011",
            updated_at="2026-07-24T12:00:00+00:00",
        )
        lease_path = Path("/tmp/mailbox-lease.json")
        fresh_lease = mailbox_models.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=True,
        )
        stale_lease = mailbox_models.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=False,
            reason=mailbox_constants.LEASE_EXPIRED,
        )
        settings = run_coordination_wait_models.MailboxWaitSettings(
            base_seconds=2.0,
            lease_extension_seconds=1.0,
            unsupervised_ceiling_seconds=5.0,
            poll_seconds=0.5,
            io_grace_seconds=1.0,
        )
        supervision_ends = 8.0
        now = [0.0]

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        def read_lease(*_args: object, **_kwargs: object) -> object:
            if now[0] >= supervision_ends:
                return stale_lease
            return fresh_lease

        payload = {
            "waiting_for_reply": {
                "question_id": question_id,
                "last_lease_activity_at": None,
            }
        }
        persistence = run_persistence._RunPersistence(
            write_state=False,
            append_events=False,
        )
        with (
            mock.patch.object(mailbox, "read_mailbox", return_value=view),
            mock.patch.object(mailbox_leases, "read_lease", side_effect=read_lease),
        ):
            result = run_coordination_mailbox_wait.wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload=payload,
                question_id=question_id,
                run_id="RUN-TEST",
                settings=settings,
                persistence=persistence,
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertIsNone(result.reply)
        # Each fresh lease moves the ceiling with the active deadline, so the
        # ratchet carried the wait past a bound that used to end it at 5.0.
        self.assertEqual(
            wait_budgets.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            result.reason,
        )
        self.assertEqual(supervision_ends, now[0])
        self.assertGreater(now[0], settings.unsupervised_ceiling_seconds)
        self.assertEqual(
            lease_record.updated_at,
            payload["waiting_for_reply"]["last_lease_activity_at"],
        )

    def test_mailbox_reply_wait_degrades_after_sustained_io_failure(
        self,
    ) -> None:
        settings = run_coordination_wait_models.MailboxWaitSettings(
            base_seconds=10.0,
            lease_extension_seconds=1.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=3.0,
        )
        now = [0.0]

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with mock.patch.object(
            mailbox,
            "read_mailbox",
            side_effect=mailbox_models.MailboxError(
                mailbox_constants.ERROR_MAILBOX_READ
            ),
        ):
            result = run_coordination_mailbox_wait.wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload={},
                question_id="00000000-0000-0000-0000-000000000012",
                run_id="RUN-TEST",
                settings=settings,
                persistence=run_persistence._RunPersistence(
                    write_state=False,
                    append_events=False,
                ),
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertIsNone(result.reply)
        self.assertEqual(mailbox_constants.ERROR_MAILBOX_READ, result.reason)
        self.assertEqual(3.0, now[0])

    def test_mailbox_reply_wait_gate_excludes_recoverable_and_failed_turns(
        self,
    ) -> None:
        success = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=0,
            stdout="",
            stderr="",
        )
        failure = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=1,
            stdout="",
            stderr="quota exhausted",
        )
        self.assertTrue(
            run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=True,
                pause_reason="blocked",
                result=success,
            )
        )
        self.assertFalse(
            run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=True,
                pause_reason="verify-failed",
                result=success,
            )
        )
        self.assertFalse(
            run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=True,
                pause_reason="blocked",
                result=failure,
            )
        )
        self.assertFalse(
            run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=False,
                pause_reason="blocked",
                result=success,
            )
        )


if __name__ == "__main__":
    unittest.main()
