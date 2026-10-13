"""Deadline rebasing and exhausted-mailbox reply tests."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import tests.reply_flow_support as rfs
from agent_session_loop import (
    events,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    qna,
    run_coordination,
    run_persistence,
)
from tests.support import (
    read_jsonl,
    write_plan,
)


class ExhaustedMailboxFlowTests(rfs.ReplyResumeFlowCase):
    """Rebase thresholds, change tokens, and exhausted mailbox handling."""

    def test_subthreshold_jitter_triggers_no_rebase_and_no_state_write(
        self,
    ) -> None:
        question_id = "00000000-0000-0000-0000-000000000043"
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
        # Base equals hard so the loop never probes the lease: the only
        # state write a rebase could cause is the one under test.
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=4.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )
        epoch = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
        waiting = run_coordination.waiting_for_reply_payload(
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
            if wall_jump[0] == 0.0:
                wall_jump[0] = 1.0

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
                run_persistence,
                "_write_state_payload",
            ) as write_state,
        ):
            result = run_coordination.wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload=payload,
                question_id=question_id,
                run_id="RUN-TEST",
                settings=settings,
                persistence=run_persistence._RunPersistence(append_events=False),
                started_monotonic=0.0,
                monotonic=monotonic,
                sleep=sleep,
                wall_clock=wall_clock,
            )

        self.assertEqual("timeout", result.reason)
        self.assertIsNone(result.suspend_skew_seconds)
        self.assertEqual(waiting, payload["waiting_for_reply"])
        write_state.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            run_coordination.clear_waiting_for_reply(
                event_path,
                payload,
                reason=result.reason,
                pending_outbox=result.pending_outbox,
                suspend_skew_seconds=result.suspend_skew_seconds,
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            [cleared_event] = read_jsonl(event_path)
        self.assertNotIn(
            "suspend_skew_seconds",
            cleared_event["payload"],
        )

    def test_rebase_waiting_deadlines_skips_when_question_id_mismatches(
        self,
    ) -> None:
        epoch = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
        waiting = {
            "question_id": "question-owned-by-another-wait",
            "base_deadline": "2026-07-24T12:04:00+00:00",
            "unsupervised_deadline_at": "2026-07-24T12:20:00+00:00",
        }
        payload = {"waiting_for_reply": dict(waiting)}

        with mock.patch.object(
            run_persistence,
            "_write_state_payload",
        ) as write_state:
            run_coordination.rebase_waiting_deadlines(
                payload,
                question_id="question-this-wait-expected",
                base_deadline=epoch + timedelta(hours=8, minutes=4),
                unsupervised_deadline_at=epoch + timedelta(hours=8, minutes=20),
                persistence=run_persistence._RunPersistence(),
            )

        self.assertEqual(waiting, payload["waiting_for_reply"])
        write_state.assert_not_called()

    def test_mailbox_reply_wait_reparses_only_after_change_token(self) -> None:
        question_id = "00000000-0000-0000-0000-000000000016"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        reply = mailbox_models.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000017",
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
        lease = mailbox_models.MailboxLeaseView(
            lease_path=Path("/tmp/mailbox-lease.json"),
            lease=mailbox_models.MailboxLease(
                supervisor_id="00000000-0000-0000-0000-000000000018",
                updated_at="2026-07-24T12:00:00+00:00",
            ),
            fresh=True,
        )
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=4.0,
            lease_extension_seconds=2.0,
            unsupervised_ceiling_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=1.0,
        )
        now = [0.0]
        sleeps: list[float] = []
        unchanged_token = (1, 2, 1, 3)
        changed_token = (1, 2, 2, 4)

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                side_effect=[
                    unchanged_token,
                    unchanged_token,
                    changed_token,
                ],
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=[waiting_view, replied_view],
            ) as read,
            mock.patch.object(
                mailbox_leases,
                "read_lease",
                return_value=lease,
            ),
        ):
            result = run_coordination.wait_for_mailbox_reply(
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
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertEqual(reply, result.reply)
        self.assertEqual("reply", result.reason)
        self.assertEqual(0, result.pending_outbox)
        self.assertEqual(2, read.call_count)
        self.assertEqual([1.0, 1.0], sleeps)

    def test_exhausted_mailbox_ends_reply_wait_before_polling(self) -> None:
        question_id = "00000000-0000-4000-8000-000000000021"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
            run_id="RUN-TEST",
        )
        exhausted_view = mailbox_snapshot.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(question,),
            last_seq=1,
            size_bytes=(
                mailbox_constants.MAILBOX_MAX_BYTES
                - mailbox_constants.MAILBOX_MIN_APPEND_BYTES
                + 1
            ),
        )
        settings = run_coordination.MailboxWaitSettings(
            base_seconds=300.0,
            lease_extension_seconds=60.0,
            unsupervised_ceiling_seconds=900.0,
            poll_seconds=2.0,
            io_grace_seconds=10.0,
        )

        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                return_value=(1, 2, exhausted_view.size_bytes, 3),
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                return_value=exhausted_view,
            ) as read,
            mock.patch.object(
                mailbox_leases,
                "read_lease",
            ) as read_lease,
        ):
            result = run_coordination.wait_for_mailbox_reply(
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
                monotonic=lambda: 0.0,
                sleep=lambda _seconds: self.fail("exhausted mailbox must not poll"),
            )

        self.assertIsNone(result.reply)
        self.assertEqual(mailbox_constants.ERROR_MAILBOX_FULL, result.reason)
        self.assertEqual(1, result.pending_outbox)
        read.assert_called_once()
        read_lease.assert_not_called()

    def test_exhausted_mailbox_returns_durably_appended_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            question_id = "00000000-0000-4000-8000-000000000022"
            reply_id = "00000000-0000-4000-8000-000000000023"
            mailbox_appenders.append_question(
                plan_dir,
                "Which option?",
                state_dir=state_dir,
                message_id=question_id,
                created_at=created_at,
            )
            reply_record = {
                "created_at": created_at,
                "id": reply_id,
                "kind": mailbox_vocabulary.KIND_REPLY,
                "text": "",
                "to": question_id,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
            }
            reply_overhead = len(
                json.dumps(
                    reply_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            current_size = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            ).size_bytes
            final_headroom = mailbox_constants.MAILBOX_MIN_APPEND_BYTES - 1
            reply_text = "x" * (
                mailbox_constants.MAILBOX_MAX_BYTES
                - final_headroom
                - current_size
                - reply_overhead
            )
            appended_reply = mailbox_appenders.append_reply(
                plan_dir,
                question_id,
                reply_text,
                state_dir=state_dir,
                message_id=reply_id,
                created_at=created_at,
            )
            exhausted_view = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            )
            self.assertTrue(exhausted_view.full)
            self.assertEqual(
                appended_reply,
                exhausted_view.reply_for(question_id),
            )
            settings = run_coordination.MailboxWaitSettings(
                base_seconds=300.0,
                lease_extension_seconds=60.0,
                unsupervised_ceiling_seconds=900.0,
                poll_seconds=2.0,
                io_grace_seconds=10.0,
            )

            with mock.patch.object(
                mailbox_leases,
                "read_lease",
            ) as read_lease:
                result = run_coordination.wait_for_mailbox_reply(
                    plan_dir=plan_dir,
                    state_dir_arg=str(state_dir),
                    payload={},
                    question_id=question_id,
                    run_id="RUN-TEST",
                    settings=settings,
                    persistence=run_persistence._RunPersistence(
                        write_state=False,
                        append_events=False,
                    ),
                    started_monotonic=0.0,
                    monotonic=lambda: 0.0,
                    sleep=lambda _seconds: self.fail("available reply must not poll"),
                )

            self.assertEqual(appended_reply, result.reply)
            self.assertEqual("reply", result.reason)
            self.assertEqual(0, result.pending_outbox)
            read_lease.assert_not_called()

    def test_exhausted_mailbox_reuses_deterministic_answered_question(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            question_text = "Which option?"
            question_id = run_coordination.mailbox_question_id(
                plan_dir=plan_dir,
                run_id="run-retry",
                session_id="1",
                turn_index=2,
            )
            reply_id = "00000000-0000-4000-8000-000000000025"
            published_question = mailbox_appenders.append_question(
                plan_dir,
                question_text,
                state_dir=state_dir,
                message_id=question_id,
                created_at=created_at,
                run_id="run-retry",
                session_id="1",
                turn_index=2,
                waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
            )
            reply_record = {
                "created_at": created_at,
                "id": reply_id,
                "kind": mailbox_vocabulary.KIND_REPLY,
                "text": "",
                "to": question_id,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
            }
            reply_overhead = len(
                json.dumps(
                    reply_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            current_size = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            ).size_bytes
            reply_text = "x" * (
                mailbox_constants.MAILBOX_MAX_BYTES
                - (mailbox_constants.MAILBOX_MIN_APPEND_BYTES - 1)
                - current_size
                - reply_overhead
            )
            mailbox_appenders.append_reply(
                plan_dir,
                question_id,
                reply_text,
                state_dir=state_dir,
                message_id=reply_id,
                created_at=created_at,
            )
            exhausted_view = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            )
            self.assertTrue(exhausted_view.full)
            self.assertIsNotNone(exhausted_view.reply_for(question_id))

            with mock.patch.object(
                mailbox_appenders,
                "append_question",
                side_effect=AssertionError(
                    "reusing a published question must not append"
                ),
            ) as append_question:
                reused_question = run_coordination.ensure_mailbox_question(
                    plan_dir=plan_dir,
                    state_dir_arg=str(state_dir),
                    question_id=question_id,
                    run_id="run-retry",
                    session_id="1",
                    turn_index=2,
                    text=question_text,
                )

            self.assertEqual(published_question, reused_question)
            append_question.assert_not_called()

    def test_exhausted_mailbox_preserves_question_preparation_errors(
        self,
    ) -> None:
        question_id = "00000000-0000-4000-8000-000000000027"
        mismatched_kind = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_GUIDANCE,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
            run_id="RUN-TEST",
            session_id="1",
            turn_index=1,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
        )
        mismatched_text = dataclasses.replace(
            mismatched_kind,
            kind=mailbox_vocabulary.KIND_QUESTION,
            text="A different question?",
        )
        cases = (
            ("append-required", (), mailbox_constants.ERROR_MAILBOX_FULL),
            (
                "mismatched-kind",
                (mismatched_kind,),
                qna.REASON_MISSING_QUESTION_CONTEXT,
            ),
            (
                "mismatched-text",
                (mismatched_text,),
                qna.REASON_MISSING_QUESTION_CONTEXT,
            ),
        )

        for name, messages, expected_reason in cases:
            with self.subTest(name=name):
                exhausted_view = mailbox_snapshot.MailboxView(
                    mailbox_path=Path("/tmp/mailbox.jsonl"),
                    messages=messages,
                    last_seq=len(messages),
                    size_bytes=mailbox_constants.MAILBOX_MAX_BYTES,
                )
                with (
                    mock.patch.object(
                        mailbox,
                        "read_mailbox",
                        return_value=exhausted_view,
                    ),
                    mock.patch.object(
                        mailbox_appenders,
                        "append_question",
                    ) as append_question,
                    self.assertRaises(
                        run_coordination.MailboxWaitUnavailable
                    ) as raised,
                ):
                    run_coordination.ensure_mailbox_question(
                        plan_dir=Path("/tmp/plan"),
                        state_dir_arg=None,
                        question_id=question_id,
                        run_id="RUN-TEST",
                        session_id="1",
                        turn_index=1,
                        text="Which option?",
                    )

                self.assertEqual(expected_reason, raised.exception.reason)
                append_question.assert_not_called()


if __name__ == "__main__":
    unittest.main()
