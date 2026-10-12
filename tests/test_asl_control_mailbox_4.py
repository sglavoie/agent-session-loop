"""MailboxTestsPart4.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from agent_session_loop import (
    mailbox,
)
from tests.asl_control_mailbox_support import MailboxTestsBase


class MailboxTestsPart4(MailboxTestsBase):
    def test_v3_malformed_fields_and_cross_record_references_are_rejected(
        self,
    ) -> None:
        question_id = self.message_id()
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": question_id,
                "kind": mailbox.KIND_QUESTION,
                "delivery_mode": mailbox.DELIVERY_MODE_ASYNC,
                "run_id": "run-A",
                "text": "valid question",
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
            }
        )
        malformed = (
            (
                mailbox.REJECT_INVALID_RUN_ID,
                {
                    "kind": mailbox.KIND_QUESTION,
                    "run_id": "",
                    "text": "bad run",
                },
            ),
            (
                mailbox.REJECT_INVALID_SESSION_ID,
                {
                    "kind": mailbox.KIND_QUESTION,
                    "session_id": "01",
                    "text": "bad session",
                },
            ),
            (
                mailbox.REJECT_INVALID_TURN_INDEX,
                {
                    "kind": mailbox.KIND_QUESTION,
                    "text": "bad turn",
                    "turn_index": 0,
                },
            ),
            (
                mailbox.REJECT_INVALID_WAITER_ORIGIN,
                {
                    "kind": mailbox.KIND_QUESTION,
                    "text": "bad waiter",
                    "waiter_origin": "supervisor",
                },
            ),
            (
                mailbox.REJECT_INVALID_RUN_ID,
                {
                    "kind": mailbox.KIND_NOTICE,
                    "session_id": "1",
                    "text": "missing run",
                    "turn_index": 1,
                },
            ),
            (
                mailbox.REJECT_INVALID_ACTION,
                {
                    "action": "answer",
                    "kind": mailbox.KIND_RESOLUTION,
                    "run_id": "run-A",
                    "to": question_id,
                },
            ),
            (
                mailbox.REJECT_QUESTION_NOT_OPEN,
                {
                    "action": mailbox.RESOLUTION_ACTION_PAUSE,
                    "kind": mailbox.KIND_RESOLUTION,
                    "run_id": "run-B",
                    "to": question_id,
                },
            ),
            (
                mailbox.REJECT_INVALID_NOTE,
                {
                    "action": mailbox.RESOLUTION_ACTION_PAUSE,
                    "kind": mailbox.KIND_RESOLUTION,
                    "note": 1,
                    "run_id": "run-A",
                    "to": question_id,
                },
            ),
        )
        for _reason, fields in malformed:
            if fields.get("kind") == mailbox.KIND_QUESTION:
                fields = {
                    "delivery_mode": mailbox.DELIVERY_MODE_ASYNC,
                    **fields,
                }
            self.append_raw(
                {
                    "created_at": self.timestamp(),
                    "id": self.message_id(),
                    "version": mailbox.MAILBOX_SCHEMA_VERSION,
                    **fields,
                }
            )
        self.append_raw(
            {
                "action": mailbox.RESOLUTION_ACTION_PAUSE,
                "created_at": self.timestamp(),
                "id": self.message_id(),
                "kind": mailbox.KIND_RESOLUTION,
                "run_id": "run-A",
                "to": question_id,
                "version": mailbox.MAILBOX_LEGACY_SCHEMA_VERSION,
            }
        )

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(
            [reason for reason, _fields in malformed] + [mailbox.REJECT_UNKNOWN_KIND],
            [record.reason for record in view.rejected],
        )
        self.assertEqual((view.messages[0],), view.unanswered_questions)

    def test_reply_and_resolution_share_atomic_first_close_wins(self) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Reply or resolve?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        successes: list[mailbox.MailboxMessage] = []
        failures: list[str] = []
        result_lock = threading.Lock()

        def close(index: int) -> None:
            try:
                if index % 2:
                    result = mailbox.append_reply(
                        self.plan_dir,
                        question.id,
                        f"reply-{index}",
                        state_dir=self.state_dir,
                        created_at=self.timestamp(),
                    )
                else:
                    result = mailbox.append_resolution(
                        self.plan_dir,
                        question.id,
                        mailbox.RESOLUTION_ACTION_CONTINUE,
                        expected_run_id="run-A",
                        state_dir=self.state_dir,
                        created_at=self.timestamp(),
                    )
            except mailbox.MailboxError as error:
                with result_lock:
                    failures.append(error.reason)
            else:
                with result_lock:
                    successes.append(result)

        threads = [threading.Thread(target=close, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.assertEqual(1, len(successes))
        self.assertEqual(19, len(failures))
        expected_failure = (
            mailbox.REJECT_QUESTION_ALREADY_ANSWERED
            if successes[0].kind == mailbox.KIND_REPLY
            else mailbox.REJECT_QUESTION_ALREADY_RESOLVED
        )
        self.assertEqual({expected_failure}, set(failures))
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(successes[0], view.closure_for(question.id))
        self.assertEqual(2, len(view.messages))
        self.assertEqual((), view.unanswered_questions)

    def test_concurrent_resolutions_commit_exactly_one_action(self) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Continue or pause?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        successes: list[mailbox.MailboxMessage] = []
        failures: list[str] = []
        result_lock = threading.Lock()
        race_barrier = threading.Barrier(20)

        def resolve(index: int) -> None:
            race_barrier.wait(timeout=3)
            action = (
                mailbox.RESOLUTION_ACTION_CONTINUE
                if index % 2
                else mailbox.RESOLUTION_ACTION_PAUSE
            )
            try:
                result = mailbox.append_resolution(
                    self.plan_dir,
                    question.id,
                    action,
                    expected_run_id="run-A",
                    state_dir=self.state_dir,
                    created_at=self.timestamp(),
                )
            except mailbox.MailboxError as error:
                with result_lock:
                    failures.append(error.reason)
            else:
                with result_lock:
                    successes.append(result)

        threads = [
            threading.Thread(target=resolve, args=(index,)) for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(1, len(successes))
        self.assertEqual(
            [mailbox.REJECT_QUESTION_ALREADY_RESOLVED] * 19,
            sorted(failures),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(successes[0], view.closure_for(question.id))
        self.assertIn(successes[0].action, mailbox.RESOLUTION_ACTIONS)
        self.assertEqual(2, len(view.messages))
        self.assertEqual((), view.unanswered_questions)

    def test_resolution_uses_reserved_capacity_after_nonclosure_saturation(
        self,
    ) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Can the reserved closure still land?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        path = self.mailbox_path()
        nonclosure_limit = (
            mailbox.MAILBOX_MAX_BYTES - mailbox.MAILBOX_CLOSURE_RESERVE_BYTES
        )
        current_size = path.stat().st_size
        filler_size = nonclosure_limit - current_size
        self.assertGreater(filler_size, 1)
        with path.open("ab") as mailbox_file:
            mailbox_file.write(b"x" * (filler_size - 1) + b"\n")

        saturated = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(nonclosure_limit, saturated.size_bytes)
        self.assertFalse(saturated.accepts_questions)
        self.assertTrue(saturated.accepts_replies)
        self.assertTrue(saturated.accepts_resolutions)
        self.assertFalse(saturated.full)
        with self.assertRaises(mailbox.MailboxError) as refused:
            mailbox.append_notice(
                self.plan_dir,
                "This non-closure must not consume the reserve.",
                run_id="run-A",
                session_id="1",
                turn_index=1,
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(mailbox.ERROR_MAILBOX_FULL, refused.exception.reason)

        resolution = mailbox.append_resolution(
            self.plan_dir,
            question.id,
            mailbox.RESOLUTION_ACTION_CONTINUE,
            expected_run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        closed = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(resolution, closed.closure_for(question.id))
        self.assertEqual((), closed.unanswered_questions)
        self.assertGreater(closed.size_bytes, nonclosure_limit)
        self.assertLessEqual(closed.size_bytes, mailbox.MAILBOX_MAX_BYTES)

    def test_resolution_requires_expected_owner_and_defeats_later_closers(
        self,
    ) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Continue?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        with self.assertRaises(mailbox.MailboxError) as mismatched:
            mailbox.append_resolution(
                self.plan_dir,
                question.id,
                mailbox.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="run-B",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(mailbox.REJECT_QUESTION_NOT_OPEN, mismatched.exception.reason)
        self.assertEqual(
            (question,),
            mailbox.read_mailbox(
                self.plan_dir, state_dir=self.state_dir
            ).unanswered_questions,
        )

        resolution = mailbox.append_resolution(
            self.plan_dir,
            question.id,
            mailbox.RESOLUTION_ACTION_PAUSE,
            expected_run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        for closer in (
            lambda: mailbox.append_reply(
                self.plan_dir,
                question.id,
                "too late",
                state_dir=self.state_dir,
            ),
            lambda: mailbox.append_resolution(
                self.plan_dir,
                question.id,
                mailbox.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="run-A",
                state_dir=self.state_dir,
            ),
        ):
            with self.assertRaises(mailbox.MailboxError) as closed:
                closer()
            self.assertEqual(
                mailbox.REJECT_QUESTION_ALREADY_RESOLVED,
                closed.exception.reason,
            )
        self.assertEqual(
            resolution,
            mailbox.find_closure(
                self.plan_dir,
                question.id,
                state_dir=self.state_dir,
            ),
        )

    def test_concurrent_readers_and_writers_observe_only_complete_lines(
        self,
    ) -> None:
        errors: list[str] = []
        error_lock = threading.Lock()

        def append_many(worker: int) -> None:
            for index in range(20):
                mailbox.append_guidance(
                    self.plan_dir,
                    f"{worker}:{index}",
                    state_dir=self.state_dir,
                    created_at=self.timestamp(),
                )

        def read_many() -> None:
            for _ in range(25):
                view = mailbox.read_mailbox(
                    self.plan_dir,
                    state_dir=self.state_dir,
                )
                if view.rejected:
                    with error_lock:
                        errors.extend(record.reason for record in view.rejected)

        writers = [
            threading.Thread(target=append_many, args=(worker,)) for worker in range(4)
        ]
        readers = [threading.Thread(target=read_many) for _ in range(3)]
        for thread in writers + readers:
            thread.start()
        for thread in writers + readers:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual([], errors)
        self.assertEqual(80, len(view.messages))
        self.assertEqual(list(range(1, 81)), [m.seq for m in view.messages])
        self.assertEqual(80, len(self.mailbox_path().read_bytes().splitlines()))

    def test_mailbox_transactions_are_synced_but_lease_heartbeats_are_not(
        self,
    ) -> None:
        with mock.patch.object(mailbox.durability, "fullsync") as fullsync:
            mailbox.append_guidance(
                self.plan_dir,
                "durable guidance",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
            fullsync.assert_called_once()

            fullsync.reset_mock()
            mailbox.append_wait_question(
                self.plan_dir,
                "durable wait",
                state_dir=self.state_dir,
                run_id="run-A",
                session_id="1",
                turn_index=1,
                waiter_origin=mailbox.WAITER_ORIGIN_WORKER,
                created_at=self.timestamp(),
            )
            fullsync.assert_called_once()

            fullsync.reset_mock()
            mailbox.write_lease(
                self.plan_dir,
                self.message_id(),
                state_dir=self.state_dir,
                updated_at=self.timestamp(),
            )
            fullsync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
