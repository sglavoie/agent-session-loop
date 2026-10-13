"""Wait-cleared evidence and suspend re-anchoring tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    events,
    mailbox,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    qna,
    run_coordination_constants,
    run_coordination_mailbox_wait,
    run_coordination_wait_models,
    run_coordination_wait_record,
    run_coordination_wait_state,
    run_persistence,
    state,
    state_payloads,
)
from tests.support import (
    read_jsonl,
)


class MailboxWaitEvidenceTests(rfs.ReplyResumeFlowCase):
    """Lease evidence on wait exits and suspend re-anchoring."""

    def test_wait_cleared_names_the_lease_behind_a_failed_exit(self) -> None:
        question_id = "00000000-0000-0000-0000-000000000031"
        supervisor_id = "00000000-0000-0000-0000-000000000032"
        lease_updated_at = "2026-07-24T12:00:00+00:00"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        reply = mailbox_models.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000033",
            kind=mailbox_vocabulary.KIND_REPLY,
            created_at="2026-07-24T12:00:01+00:00",
            text="Option A",
            to=question_id,
        )
        mailbox_path = Path("/tmp/mailbox.jsonl")
        waiting_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question,),
            last_seq=1,
        )
        replied_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question, reply),
            last_seq=2,
        )
        lease_record = mailbox_models.MailboxLease(
            supervisor_id=supervisor_id,
            updated_at=lease_updated_at,
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
        # One extension outruns the ceiling, so a proved supervisor whose next
        # liveness check falls beyond the ceiling still ends at it — the only
        # way a timeout can name a lease that was fresh when last read.
        settings = run_coordination_wait_models.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=20.0,
            unsupervised_ceiling_seconds=8.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        def run_wait(
            view: mailbox_snapshot.MailboxView,
            lease: mailbox_models.MailboxLeaseView,
        ) -> run_coordination_wait_models.MailboxWaitResult:
            now = [0.0]

            def sleep(seconds: float) -> None:
                now[0] += seconds

            with (
                mock.patch.object(
                    mailbox,
                    "mailbox_change_token",
                    return_value=(1, 2, 3, 4),
                ),
                mock.patch.object(
                    mailbox,
                    "read_mailbox",
                    return_value=view,
                ),
                mock.patch.object(
                    mailbox_leases,
                    "read_lease",
                    return_value=lease,
                ),
            ):
                return run_coordination_mailbox_wait.wait_for_mailbox_reply(
                    plan_dir=Path("/tmp/plan"),
                    state_dir_arg=None,
                    payload={},
                    question_id=question_id,
                    run_id="RUN-TEST",
                    settings=settings,
                    persistence=run_persistence._RunPersistence(
                        write_state=False,
                        append_events=False,
                    ),
                    started_monotonic=0.0,
                    monotonic=lambda: now[0],
                    sleep=sleep,
                )

        def clear(result: run_coordination_wait_models.MailboxWaitResult) -> dict:
            with tempfile.TemporaryDirectory() as tmp:
                event_path = Path(tmp) / events.EVENTS_FILE_NAME
                run_coordination_wait_state.clear_waiting_for_reply(
                    event_path,
                    {
                        "run_id": "run-1",
                        "waiting_for_reply": {
                            "question_id": question_id,
                            "session_id": "9",
                            "turn_index": 3,
                        },
                    },
                    reason=result.reason,
                    pending_outbox=result.pending_outbox,
                    lease_updated_at=result.lease_updated_at,
                    lease_reason=result.lease_reason,
                    lease_supervisor_id=result.lease_supervisor_id,
                    persistence=run_persistence._RunPersistence(write_state=False),
                )
                [event] = read_jsonl(event_path)
            return event["payload"]

        lost = run_wait(waiting_view, stale_lease)
        self.assertEqual(
            run_coordination_constants.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            lost.reason,
        )
        self.assertEqual(lease_updated_at, lost.lease_updated_at)
        self.assertEqual(mailbox_constants.LEASE_EXPIRED, lost.lease_reason)
        self.assertEqual(supervisor_id, lost.lease_supervisor_id)
        lost_payload = clear(lost)
        self.assertEqual(lease_updated_at, lost_payload["lease_updated_at"])
        self.assertEqual(
            mailbox_constants.LEASE_EXPIRED,
            lost_payload["lease_reason"],
        )
        self.assertEqual(
            supervisor_id,
            lost_payload["lease_supervisor_id"],
        )

        timed_out = run_wait(waiting_view, fresh_lease)
        self.assertEqual("timeout", timed_out.reason)
        self.assertEqual(lease_updated_at, timed_out.lease_updated_at)
        self.assertIsNone(timed_out.lease_reason)
        self.assertEqual(supervisor_id, timed_out.lease_supervisor_id)
        timed_out_payload = clear(timed_out)
        self.assertEqual(
            lease_updated_at,
            timed_out_payload["lease_updated_at"],
        )
        self.assertEqual(
            supervisor_id,
            timed_out_payload["lease_supervisor_id"],
        )
        self.assertNotIn("lease_reason", timed_out_payload)

        replied = run_wait(replied_view, fresh_lease)
        self.assertEqual("reply", replied.reason)
        self.assertIsNone(replied.lease_updated_at)
        self.assertIsNone(replied.lease_reason)
        self.assertIsNone(replied.lease_supervisor_id)
        replied_payload = clear(replied)
        for key in (
            "lease_updated_at",
            "lease_reason",
            "lease_supervisor_id",
        ):
            self.assertNotIn(key, replied_payload)

    def test_capacity_and_context_exits_carry_lease_evidence(self) -> None:
        question_id = "00000000-0000-0000-0000-000000000034"
        supervisor_id = "00000000-0000-0000-0000-000000000035"
        lease_updated_at = "2026-07-24T12:00:00+00:00"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        mailbox_path = Path("/tmp/mailbox.jsonl")
        waiting_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question,),
            last_seq=1,
        )
        full_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question,),
            last_seq=1,
            size_bytes=mailbox_constants.MAILBOX_MAX_BYTES,
        )
        vanished_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(),
            last_seq=1,
        )
        fresh_lease = mailbox_models.MailboxLeaseView(
            lease_path=Path("/tmp/mailbox-lease.json"),
            lease=mailbox_models.MailboxLease(
                supervisor_id=supervisor_id,
                updated_at=lease_updated_at,
            ),
            fresh=True,
        )
        settings = run_coordination_wait_models.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=8.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )

        def run_wait(
            failing_view: mailbox_snapshot.MailboxView,
        ) -> run_coordination_wait_models.MailboxWaitResult:
            now = [0.0]
            token = [0]

            def next_token(_path: object) -> tuple[int, int, int, int]:
                token[0] += 1
                return (1, 2, 3, token[0])

            def sleep(seconds: float) -> None:
                now[0] += seconds

            with (
                mock.patch.object(
                    mailbox,
                    "mailbox_change_token",
                    side_effect=next_token,
                ),
                mock.patch.object(
                    mailbox,
                    "read_mailbox",
                    side_effect=lambda *_args, **_kwargs: (
                        waiting_view if now[0] < 5.0 else failing_view
                    ),
                ),
                mock.patch.object(
                    mailbox_leases,
                    "read_lease",
                    return_value=fresh_lease,
                ),
            ):
                return run_coordination_mailbox_wait.wait_for_mailbox_reply(
                    plan_dir=Path("/tmp/plan"),
                    state_dir_arg=None,
                    payload={},
                    question_id=question_id,
                    run_id="RUN-TEST",
                    settings=settings,
                    persistence=run_persistence._RunPersistence(
                        write_state=False,
                        append_events=False,
                    ),
                    started_monotonic=0.0,
                    monotonic=lambda: now[0],
                    sleep=sleep,
                )

        for failing_view, expected_reason in (
            (full_view, mailbox_constants.ERROR_MAILBOX_FULL),
            (vanished_view, qna.REASON_MISSING_QUESTION_CONTEXT),
        ):
            with self.subTest(reason=expected_reason):
                result = run_wait(failing_view)
                self.assertEqual(expected_reason, result.reason)
                self.assertEqual(lease_updated_at, result.lease_updated_at)
                self.assertEqual(supervisor_id, result.lease_supervisor_id)
                self.assertIsNone(result.lease_reason)
                self.assertIsNone(result.suspend_skew_seconds)
                self.assertIsNone(result.suspend_lease_grace_seconds)

    def test_suspend_mid_wait_reanchors_deadlines_and_keeps_wait_live(
        self,
    ) -> None:
        question_id = "00000000-0000-0000-0000-000000000041"
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
            supervisor_id="00000000-0000-0000-0000-000000000042",
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
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )
        epoch = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
        suspend_seconds = 8 * 3600.0
        # Well past the awake grace, so the lapse is judged on freshness alone.
        supervision_ends = 100.0
        waiting = run_coordination_wait_record.waiting_for_reply_payload(
            question_id=question_id,
            session_id="9",
            turn_index=3,
            started_at=epoch,
            base_deadline=epoch + timedelta(seconds=settings.base_seconds),
            unsupervised_deadline_at=epoch
            + timedelta(seconds=settings.unsupervised_ceiling_seconds),
            unsupervised_ceiling_seconds=(settings.unsupervised_ceiling_seconds),
            last_lease_activity_at=None,
        )
        payload = {"run_id": "run-1", "waiting_for_reply": dict(waiting)}
        now = [0.0]
        wall_jump = [0.0]

        def monotonic() -> float:
            return now[0]

        def wall_clock() -> datetime:
            return epoch + timedelta(seconds=now[0] + wall_jump[0])

        def sleep(seconds: float) -> None:
            now[0] += seconds
            # The lid closes during the first poll sleep: the wall clock
            # jumps eight hours while the monotonic clock stays put.
            if wall_jump[0] == 0.0:
                wall_jump[0] = suspend_seconds

        def read_lease(*_args: object, **_kwargs: object) -> object:
            if now[0] >= supervision_ends:
                return stale_lease
            return fresh_lease

        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                return_value=(1, 2, 3, 4),
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                return_value=view,
            ),
            mock.patch.object(
                mailbox_leases,
                "read_lease",
                side_effect=read_lease,
            ),
        ):
            result = run_coordination_mailbox_wait.wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload=payload,
                question_id=question_id,
                run_id="RUN-TEST",
                settings=settings,
                persistence=run_persistence._RunPersistence(
                    write_state=False,
                    append_events=False,
                ),
                started_monotonic=0.0,
                monotonic=monotonic,
                sleep=sleep,
                wall_clock=wall_clock,
            )

        self.assertIsNone(result.reply)
        self.assertEqual(
            run_coordination_constants.MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
            result.reason,
        )
        self.assertEqual(suspend_seconds, result.suspend_skew_seconds)
        # Eight sleeping hours neither refunded nor consumed the ceiling: the
        # supervisor kept proving liveness across the wake and the wait ran
        # five times the ceiling before the lease finally lapsed.
        self.assertEqual(supervision_ends, now[0])
        self.assertGreater(now[0], settings.unsupervised_ceiling_seconds)

        # Detection fired on the iteration after the jump, at monotonic 1.0.
        detection_wall = epoch + timedelta(seconds=1.0 + suspend_seconds)
        rebased = payload["waiting_for_reply"]
        base_after = datetime.fromisoformat(rebased["base_deadline"])
        hard_after = datetime.fromisoformat(rebased["unsupervised_deadline_at"])
        self.assertEqual(
            detection_wall + timedelta(seconds=settings.base_seconds - 1.0),
            base_after,
        )
        # The base keeps its surviving budget, but the ceiling re-anchors at
        # the wake in full: nobody could prove liveness while the lid was shut.
        self.assertEqual(
            detection_wall + timedelta(seconds=settings.unsupervised_ceiling_seconds),
            hard_after,
        )
        self.assertGreater(base_after, detection_wall)
        self.assertGreater(hard_after, detection_wall)
        self.assertEqual(waiting["started_at"], rebased["started_at"])

        lock_info = state.LockInfo(
            path=Path("/tmp/plan-lock.json"),
            pid=os.getpid(),
            plan_dir="/tmp/plan",
            created_at="2026-07-24T12:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )
        post_wake_now = detection_wall + timedelta(seconds=1.0)
        self.assertEqual(
            state_payloads.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state_payloads.waiting_for_reply_status(
                waiting,
                lock_info=lock_info,
                now=post_wake_now,
            ),
        )
        self.assertEqual(
            state_payloads.WAITING_FOR_REPLY_STATUS_LIVE,
            state_payloads.waiting_for_reply_status(
                rebased,
                lock_info=lock_info,
                now=post_wake_now,
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            run_coordination_wait_state.clear_waiting_for_reply(
                event_path,
                payload,
                reason=result.reason,
                pending_outbox=result.pending_outbox,
                lease_updated_at=result.lease_updated_at,
                lease_reason=result.lease_reason,
                lease_supervisor_id=result.lease_supervisor_id,
                suspend_skew_seconds=result.suspend_skew_seconds,
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            [cleared_event] = read_jsonl(event_path)
        self.assertEqual(
            suspend_seconds,
            cleared_event["payload"]["suspend_skew_seconds"],
        )


if __name__ == "__main__":
    unittest.main()
