"""MailboxTestsPart4.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
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
                "kind": mailbox_vocabulary.KIND_QUESTION,
                "delivery_mode": mailbox_vocabulary.DELIVERY_MODE_ASYNC,
                "run_id": "run-A",
                "text": "valid question",
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
            }
        )
        malformed = (
            (
                mailbox_constants.REJECT_INVALID_RUN_ID,
                {
                    "kind": mailbox_vocabulary.KIND_QUESTION,
                    "run_id": "",
                    "text": "bad run",
                },
            ),
            (
                mailbox_constants.REJECT_INVALID_SESSION_ID,
                {
                    "kind": mailbox_vocabulary.KIND_QUESTION,
                    "session_id": "01",
                    "text": "bad session",
                },
            ),
            (
                mailbox_constants.REJECT_INVALID_TURN_INDEX,
                {
                    "kind": mailbox_vocabulary.KIND_QUESTION,
                    "text": "bad turn",
                    "turn_index": 0,
                },
            ),
            (
                mailbox_constants.REJECT_INVALID_WAITER_ORIGIN,
                {
                    "kind": mailbox_vocabulary.KIND_QUESTION,
                    "text": "bad waiter",
                    "waiter_origin": "supervisor",
                },
            ),
            (
                mailbox_constants.REJECT_INVALID_RUN_ID,
                {
                    "kind": mailbox_vocabulary.KIND_NOTICE,
                    "session_id": "1",
                    "text": "missing run",
                    "turn_index": 1,
                },
            ),
            (
                mailbox_constants.REJECT_INVALID_ACTION,
                {
                    "action": "answer",
                    "kind": mailbox_vocabulary.KIND_RESOLUTION,
                    "run_id": "run-A",
                    "to": question_id,
                },
            ),
            (
                mailbox_constants.REJECT_QUESTION_NOT_OPEN,
                {
                    "action": mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                    "kind": mailbox_vocabulary.KIND_RESOLUTION,
                    "run_id": "run-B",
                    "to": question_id,
                },
            ),
            (
                mailbox_constants.REJECT_INVALID_NOTE,
                {
                    "action": mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                    "kind": mailbox_vocabulary.KIND_RESOLUTION,
                    "note": 1,
                    "run_id": "run-A",
                    "to": question_id,
                },
            ),
        )
        for _reason, fields in malformed:
            if fields.get("kind") == mailbox_vocabulary.KIND_QUESTION:
                fields = {
                    "delivery_mode": mailbox_vocabulary.DELIVERY_MODE_ASYNC,
                    **fields,
                }
            self.append_raw(
                {
                    "created_at": self.timestamp(),
                    "id": self.message_id(),
                    "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                    **fields,
                }
            )
        self.append_raw(
            {
                "action": mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                "created_at": self.timestamp(),
                "id": self.message_id(),
                "kind": mailbox_vocabulary.KIND_RESOLUTION,
                "run_id": "run-A",
                "to": question_id,
                "version": mailbox_constants.MAILBOX_LEGACY_SCHEMA_VERSION,
            }
        )

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(
            [reason for reason, _fields in malformed]
            + [mailbox_constants.REJECT_UNKNOWN_KIND],
            [record.reason for record in view.rejected],
        )
        self.assertEqual((view.messages[0],), view.unanswered_questions)

    def test_reply_and_resolution_share_atomic_first_close_wins(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Reply or resolve?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        successes: list[mailbox_models.MailboxMessage] = []
        failures: list[str] = []
        result_lock = threading.Lock()

        def close(index: int) -> None:
            try:
                if index % 2:
                    result = mailbox_appenders.append_reply(
                        self.plan_dir,
                        question.id,
                        f"reply-{index}",
                        state_dir=self.state_dir,
                        created_at=self.timestamp(),
                    )
                else:
                    result = mailbox_appenders.append_resolution(
                        self.plan_dir,
                        question.id,
                        mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
                        expected_run_id="run-A",
                        state_dir=self.state_dir,
                        created_at=self.timestamp(),
                    )
            except mailbox_models.MailboxError as error:
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
            mailbox_constants.REJECT_QUESTION_ALREADY_ANSWERED
            if successes[0].kind == mailbox_vocabulary.KIND_REPLY
            else mailbox_constants.REJECT_QUESTION_ALREADY_RESOLVED
        )
        self.assertEqual({expected_failure}, set(failures))
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(successes[0], view.closure_for(question.id))
        self.assertEqual(2, len(view.messages))
        self.assertEqual((), view.unanswered_questions)

    def test_concurrent_resolutions_commit_exactly_one_action(self) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Continue or pause?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        successes: list[mailbox_models.MailboxMessage] = []
        failures: list[str] = []
        result_lock = threading.Lock()
        race_barrier = threading.Barrier(20)

        def resolve(index: int) -> None:
            race_barrier.wait(timeout=3)
            action = (
                mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE
                if index % 2
                else mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
            )
            try:
                result = mailbox_appenders.append_resolution(
                    self.plan_dir,
                    question.id,
                    action,
                    expected_run_id="run-A",
                    state_dir=self.state_dir,
                    created_at=self.timestamp(),
                )
            except mailbox_models.MailboxError as error:
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
            [mailbox_constants.REJECT_QUESTION_ALREADY_RESOLVED] * 19,
            sorted(failures),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(successes[0], view.closure_for(question.id))
        self.assertIn(successes[0].action, mailbox_vocabulary.RESOLUTION_ACTIONS)
        self.assertEqual(2, len(view.messages))
        self.assertEqual((), view.unanswered_questions)

    def test_resolution_uses_reserved_capacity_after_nonclosure_saturation(
        self,
    ) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Can the reserved closure still land?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        path = self.mailbox_path()
        nonclosure_limit = (
            mailbox_constants.MAILBOX_MAX_BYTES
            - mailbox_constants.MAILBOX_CLOSURE_RESERVE_BYTES
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
        with self.assertRaises(mailbox_models.MailboxError) as refused:
            mailbox_appenders.append_notice(
                self.plan_dir,
                "This non-closure must not consume the reserve.",
                run_id="run-A",
                session_id="1",
                turn_index=1,
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(mailbox_constants.ERROR_MAILBOX_FULL, refused.exception.reason)

        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
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
        self.assertLessEqual(closed.size_bytes, mailbox_constants.MAILBOX_MAX_BYTES)

    def test_resolution_requires_expected_owner_and_defeats_later_closers(
        self,
    ) -> None:
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "Continue?",
            run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        with self.assertRaises(mailbox_models.MailboxError) as mismatched:
            mailbox_appenders.append_resolution(
                self.plan_dir,
                question.id,
                mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="run-B",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(
            mailbox_constants.REJECT_QUESTION_NOT_OPEN, mismatched.exception.reason
        )
        self.assertEqual(
            (question,),
            mailbox.read_mailbox(
                self.plan_dir, state_dir=self.state_dir
            ).unanswered_questions,
        )

        resolution = mailbox_appenders.append_resolution(
            self.plan_dir,
            question.id,
            mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
            expected_run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        for closer in (
            lambda: mailbox_appenders.append_reply(
                self.plan_dir,
                question.id,
                "too late",
                state_dir=self.state_dir,
            ),
            lambda: mailbox_appenders.append_resolution(
                self.plan_dir,
                question.id,
                mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="run-A",
                state_dir=self.state_dir,
            ),
        ):
            with self.assertRaises(mailbox_models.MailboxError) as closed:
                closer()
            self.assertEqual(
                mailbox_constants.REJECT_QUESTION_ALREADY_RESOLVED,
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
                mailbox_appenders.append_guidance(
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
            mailbox_appenders.append_guidance(
                self.plan_dir,
                "durable guidance",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
            fullsync.assert_called_once()

            fullsync.reset_mock()
            mailbox_waits.append_wait_question(
                self.plan_dir,
                "durable wait",
                state_dir=self.state_dir,
                run_id="run-A",
                session_id="1",
                turn_index=1,
                waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
                created_at=self.timestamp(),
            )
            fullsync.assert_called_once()

            fullsync.reset_mock()
            mailbox_leases.write_lease(
                self.plan_dir,
                self.message_id(),
                state_dir=self.state_dir,
                updated_at=self.timestamp(),
            )
            fullsync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
