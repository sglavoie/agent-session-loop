"""MailboxTestsPart3.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import json
import threading
import unittest

from agent_session_loop import (
    mailbox,
)
from tests.asl_control_mailbox_support import MailboxTestsBase


class MailboxTestsPart3(MailboxTestsBase):
    def test_acknowledgements_cannot_consume_their_own_or_future_sequence(
        self,
    ) -> None:
        first = mailbox.append_guidance(
            self.plan_dir,
            "first",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        second = mailbox.append_guidance(
            self.plan_dir,
            "second",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        acknowledged = mailbox.append_acknowledgement(
            self.plan_dir,
            first.seq,
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": self.message_id(),
                "kind": "ack",
                "upto": 4,
                "version": 1,
            }
        )
        third = mailbox.append_guidance(
            self.plan_dir,
            "third",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )

        with self.assertRaises(mailbox.MailboxError) as raised:
            mailbox.append_acknowledgement(
                self.plan_dir,
                6,
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(
            mailbox.REJECT_INVALID_ACKNOWLEDGEMENT,
            raised.exception.reason,
        )

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(acknowledged.seq, 3)
        self.assertEqual(1, view.max_acknowledged_seq)
        self.assertEqual(
            [second.id, third.id],
            [message.id for message in view.pending_guidance],
        )
        self.assertEqual(
            mailbox.REJECT_INVALID_ACKNOWLEDGEMENT,
            view.rejected[0].reason,
        )

        final_ack = mailbox.append_acknowledgement(
            self.plan_dir,
            third.seq,
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        self.assertEqual(6, final_ack.seq)
        self.assertEqual(
            (),
            mailbox.pending_guidance(
                self.plan_dir,
                state_dir=self.state_dir,
            ),
        )

    def test_reply_must_target_a_question_and_first_reply_wins_atomically(
        self,
    ) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Which implementation?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        successes: list[mailbox.MailboxMessage] = []
        failures: list[str] = []
        result_lock = threading.Lock()

        def reply(index: int) -> None:
            try:
                result = mailbox.append_reply(
                    self.plan_dir,
                    question.id,
                    f"reply-{index}",
                    state_dir=self.state_dir,
                    created_at=self.timestamp(),
                )
            except mailbox.MailboxError as error:
                with result_lock:
                    failures.append(error.reason)
            else:
                with result_lock:
                    successes.append(result)

        threads = [threading.Thread(target=reply, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.assertEqual(1, len(successes))
        self.assertEqual(
            [mailbox.REJECT_QUESTION_ALREADY_ANSWERED] * 19,
            sorted(failures),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(successes[0], view.reply_for(question.id))
        self.assertEqual(2, len(view.messages))
        self.assertEqual((), view.unanswered_questions)

        with self.assertRaises(mailbox.MailboxError) as closed:
            mailbox.append_resolution(
                self.plan_dir,
                question.id,
                mailbox.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="run-A",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(
            mailbox.REJECT_QUESTION_ALREADY_ANSWERED,
            closed.exception.reason,
        )

        with self.assertRaises(mailbox.MailboxError) as raised:
            mailbox.append_reply(
                self.plan_dir,
                self.message_id(),
                "orphaned",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(
            mailbox.REJECT_QUESTION_NOT_FOUND,
            raised.exception.reason,
        )

    def test_unanswered_questions_and_stateless_since_reads(self) -> None:
        first = mailbox.append_question(
            self.plan_dir,
            "first?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        second = mailbox.append_question(
            self.plan_dir,
            "second?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        mailbox.append_reply(
            self.plan_dir,
            first.id,
            "answer",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        guidance = mailbox.append_guidance(
            self.plan_dir,
            "new guidance",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )

        self.assertEqual(
            (second,),
            mailbox.unanswered_questions(
                self.plan_dir,
                state_dir=self.state_dir,
            ),
        )
        self.assertEqual(
            (guidance,),
            mailbox.read_messages_since(
                self.plan_dir,
                3,
                state_dir=self.state_dir,
            ),
        )
        self.assertEqual(
            (first, second),
            mailbox.read_messages_since(
                self.plan_dir,
                0,
                state_dir=self.state_dir,
                kinds=(mailbox.KIND_QUESTION,),
            ),
        )

    def test_open_questions_are_scoped_to_the_owning_run(self) -> None:
        legacy = mailbox.append_question(
            self.plan_dir,
            "predates run attribution",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        other = mailbox.append_question(
            self.plan_dir,
            "belongs to an earlier run",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
            run_id="run-A",
        )
        mine = mailbox.append_question(
            self.plan_dir,
            "belongs to the live run",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
            run_id="run-B",
        )

        self.assertIsNone(legacy.run_id)
        self.assertEqual("run-A", other.run_id)
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((legacy, other, mine), view.unanswered_questions)
        self.assertEqual((mine,), view.open_questions("run-B"))
        self.assertEqual((other,), view.open_questions("run-A"))
        self.assertEqual((), view.open_questions("run-C"))
        self.assertEqual((), view.open_questions(None))
        self.assertEqual((), view.open_questions(""))

    def test_question_run_id_round_trips_and_stays_off_other_kinds(
        self,
    ) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "which shape?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
            run_id="run-A",
        )
        legacy = mailbox.append_question(
            self.plan_dir,
            "unattributed",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        guidance = mailbox.append_guidance(
            self.plan_dir,
            "keep it small",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        reply = mailbox.append_reply(
            self.plan_dir,
            question.id,
            "one object",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )

        self.assertEqual("run-A", question.to_record()["run_id"])
        # Records written before run attribution stay byte-identical.
        self.assertNotIn("run_id", legacy.to_record())
        self.assertNotIn("run_id", guidance.to_record())
        self.assertNotIn("run_id", reply.to_record())
        path = mailbox.mailbox_path_for_plan(self.plan_dir, self.state_dir)
        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(["run-A"], [r["run_id"] for r in records if "run_id" in r])
        self.assertEqual(
            [mailbox.MAILBOX_SCHEMA_VERSION] * 4,
            [r["version"] for r in records],
        )

    def test_malformed_legacy_question_run_id_parses_as_unattributed(
        self,
    ) -> None:
        cases = (
            ("non-string", 123),
            ("blank", ""),
            ("whitespace", "   "),
            ("null", None),
            ("oversized", "x" * (mailbox.MAILBOX_MAX_RUN_ID_CHARS + 1)),
        )
        for label, raw in cases:
            with self.subTest(label):
                mailbox.append_question(
                    self.plan_dir,
                    f"question {label}",
                    state_dir=self.state_dir,
                    created_at=self.timestamp(),
                )
                path = mailbox.mailbox_path_for_plan(self.plan_dir, self.state_dir)
                lines = path.read_text(encoding="utf-8").splitlines()
                record = json.loads(lines[-1])
                record["run_id"] = raw
                record["version"] = mailbox.MAILBOX_LEGACY_SCHEMA_VERSION
                lines[-1] = json.dumps(record, sort_keys=True)
                path.write_text(
                    "".join(f"{line}\n" for line in lines), encoding="utf-8"
                )

                view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

                # Degrades to unattributed rather than rejecting the line.
                self.assertEqual((), view.rejected)
                self.assertIsNone(view.messages[-1].run_id)
                self.assertIn(view.messages[-1], view.unanswered_questions)
                self.assertEqual((), view.open_questions("run-A"))

    def test_v2_resolution_notice_and_ownership_fields_round_trip(self) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Should the worker continue?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
            run_id="run-A",
            session_id="2.1",
            turn_index=3,
            waiter_origin=mailbox.WAITER_ORIGIN_WORKER,
        )
        resolution = mailbox.append_resolution(
            self.plan_dir,
            question.id,
            mailbox.RESOLUTION_ACTION_CONTINUE,
            expected_run_id="run-A",
            note="Proceed with the verified parser.",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        notice = mailbox.append_notice(
            self.plan_dir,
            "The parser migration is complete.",
            run_id="run-A",
            session_id="2.1",
            turn_index=3,
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(mailbox.MAILBOX_SCHEMA_VERSION, question.version)
        self.assertEqual("run-A", question.run_id)
        self.assertEqual("2.1", question.session_id)
        self.assertEqual(3, question.turn_index)
        self.assertEqual(mailbox.WAITER_ORIGIN_WORKER, question.waiter_origin)
        self.assertEqual(question.id, resolution.to)
        self.assertEqual("run-A", resolution.run_id)
        self.assertEqual(
            mailbox.RESOLUTION_ACTION_CONTINUE,
            resolution.action,
        )
        self.assertEqual("Proceed with the verified parser.", resolution.note)
        self.assertEqual("run-A", notice.run_id)
        self.assertEqual("2.1", notice.session_id)
        self.assertEqual(3, notice.turn_index)
        self.assertEqual(resolution, view.closure_for(question.id))
        self.assertEqual(resolution, view.resolution_for(question.id))
        self.assertIsNone(view.reply_for(question.id))
        self.assertEqual((), view.unanswered_questions)
        self.assertEqual((), view.open_questions("run-A"))
        self.assertEqual(
            (notice,),
            view.messages_since(0, kinds=(mailbox.KIND_NOTICE,)),
        )
        self.assertEqual(1, view.counts.resolutions)
        self.assertEqual(1, view.counts.notices)
        self.assertIn("accepts_resolutions", view.to_payload())

    def test_versionless_v1_and_v3_records_coexist_in_one_log(self) -> None:
        legacy_guidance_id = self.message_id()
        legacy_question_id = self.message_id()
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": legacy_guidance_id,
                "kind": mailbox.KIND_GUIDANCE,
                "text": "legacy guidance",
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": legacy_question_id,
                "kind": mailbox.KIND_QUESTION,
                "run_id": "run-A",
                "text": "legacy question",
                "version": mailbox.MAILBOX_LEGACY_SCHEMA_VERSION,
            }
        )
        notice = mailbox.append_notice(
            self.plan_dir,
            "v3 notice",
            run_id="run-A",
            session_id="1",
            turn_index=1,
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        resolution = mailbox.append_resolution(
            self.plan_dir,
            legacy_question_id,
            mailbox.RESOLUTION_ACTION_PAUSE,
            expected_run_id="run-A",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )

        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual((), view.rejected)
        self.assertEqual(
            [
                mailbox.MAILBOX_LEGACY_SCHEMA_VERSION,
                mailbox.MAILBOX_LEGACY_SCHEMA_VERSION,
                mailbox.MAILBOX_SCHEMA_VERSION,
                mailbox.MAILBOX_SCHEMA_VERSION,
            ],
            [message.version for message in view.messages],
        )
        self.assertEqual(notice, view.messages[2])
        self.assertEqual(resolution, view.closure_for(legacy_question_id))
        self.assertEqual((), view.unanswered_questions)


if __name__ == "__main__":
    unittest.main()
