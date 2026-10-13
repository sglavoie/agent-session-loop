"""ParserAndStateTests (part 9).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_rendering,
    mailbox_validation,
    mailbox_vocabulary,
    plans,
    state_status,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_status_payload_surfaces_exhausted_and_malformed_mailboxes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            question_id = "00000000-0000-4000-8000-000000000001"
            reply_id = "00000000-0000-4000-8000-000000000002"
            question = mailbox_appenders.append_question(
                plan_dir,
                "x",
                state_dir=state_dir,
                message_id=question_id,
                created_at=created_at,
            )
            empty_text_record = {
                "created_at": created_at,
                "id": reply_id,
                "kind": mailbox_vocabulary.KIND_REPLY,
                "text": "",
                "to": question.id,
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
            }
            record_overhead = len(
                json.dumps(
                    empty_text_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            target_size = (
                mailbox_constants.MAILBOX_MAX_BYTES
                - mailbox_constants.MAILBOX_MIN_APPEND_BYTES
                + 1
            )
            current_size = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).size_bytes
            mailbox_appenders.append_reply(
                plan_dir,
                question.id,
                "x" * (target_size - current_size - record_overhead),
                state_dir=state_dir,
                message_id=reply_id,
                created_at=created_at,
            )

            exhausted_view = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            )
            self.assertEqual(target_size, exhausted_view.size_bytes)
            self.assertTrue(exhausted_view.full)
            self.assertFalse(exhausted_view.accepts_questions)
            self.assertFalse(exhausted_view.accepts_replies)
            for append in (
                mailbox_appenders.append_guidance,
                mailbox_appenders.append_question,
            ):
                with self.assertRaises(mailbox_models.MailboxError) as raised:
                    append(plan_dir, "x", state_dir=state_dir)
                self.assertEqual(
                    mailbox_constants.ERROR_MAILBOX_FULL,
                    raised.exception.reason,
                )

            full_payload = state_status.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            full_status = full_payload["mailbox"]
            self.assertTrue(full_status["full"])
            self.assertFalse(full_status["accepts_questions"])
            self.assertFalse(full_status["accepts_replies"])
            self.assertFalse(full_status["accepts_resolutions"])
            self.assertEqual(target_size, full_status["size_bytes"])
            self.assertEqual(
                mailbox_constants.MAILBOX_MAX_BYTES,
                full_status["max_bytes"],
            )

            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.write_text("not-json\n", encoding="utf-8")
            lease_path = mailbox_cursors.lease_path_for_plan(plan_dir, state_dir)
            lease_path.write_text("not-json\n", encoding="utf-8")

            malformed_payload = state_status.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            malformed = malformed_payload["mailbox"]
            self.assertTrue(malformed["present"])
            self.assertEqual(1, malformed["last_seq"])
            self.assertEqual(1, malformed["rejected_records"])
            self.assertIn("rejected mailbox record", malformed["error"])
            self.assertTrue(malformed["lease_present"])
            self.assertFalse(malformed["lease_fresh"])
            self.assertEqual(
                mailbox_constants.LEASE_INVALID_SIZE, malformed["lease_reason"]
            )
            self.assertEqual(
                mailbox_constants.LEASE_INVALID_SIZE, malformed["lease_error"]
            )

    def test_oversized_integer_line_is_rejected_and_append_continues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.parent.mkdir(parents=True, exist_ok=True)
            hostile_line = '{"upto": ' + "1" * 5000 + "}\n"
            mailbox_path.write_text(hostile_line, encoding="utf-8")

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)

            self.assertEqual(1, view.last_seq)
            self.assertEqual((), view.messages)
            self.assertEqual(1, len(view.rejected))
            self.assertEqual(
                mailbox_constants.REJECT_INVALID_JSON, view.rejected[0].reason
            )
            question = mailbox_appenders.append_question(
                plan_dir,
                "post-hostile append",
                state_dir=state_dir,
                run_id="run-check",
            )
            updated = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(2, updated.last_seq)
            self.assertEqual((question,), updated.messages)
            self.assertEqual(1, len(updated.rejected))

    def test_oversized_turn_index_raises_mailbox_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            with self.assertRaises(mailbox_models.MailboxError) as raised:
                mailbox_appenders.append_question(
                    plan_dir,
                    "question",
                    state_dir=state_dir,
                    run_id="run-check",
                    session_id="1",
                    turn_index=10**5000,
                    waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
                )

            self.assertEqual(
                mailbox_constants.REJECT_INVALID_TURN_INDEX,
                raised.exception.reason,
            )
            self.assertFalse(
                mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir).exists()
            )

    def test_oversized_acknowledgement_raises_mailbox_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            with self.assertRaises(mailbox_models.MailboxError) as raised:
                mailbox_appenders.append_acknowledgement(
                    plan_dir,
                    10**5000,
                    state_dir=state_dir,
                )

            self.assertEqual(
                mailbox_constants.REJECT_INVALID_ACKNOWLEDGEMENT,
                raised.exception.reason,
            )
            self.assertFalse(
                mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir).exists()
            )

    def test_supervision_end_round_trips_with_and_without_a_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            unattributed = mailbox_appenders.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
            )
            attributed = mailbox_appenders.append_supervision_end(
                plan_dir,
                "reason=worker_hung",
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_BLOCKED,
                run_id="run-A",
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((unattributed, attributed), view.messages)
            self.assertIsNone(unattributed.run_id)
            self.assertEqual("run-A", attributed.run_id)
            self.assertEqual(
                mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                unattributed.supervision_outcome,
            )
            self.assertEqual(attributed, view.latest_supervision_end)
            self.assertEqual(
                attributed,
                view.messages_since(
                    unattributed.seq,
                    kinds=(mailbox_vocabulary.KIND_SUPERVISION_END,),
                )[0],
            )
            payload = unattributed.to_payload()
            self.assertEqual(
                mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE, payload["outcome"]
            )
            self.assertEqual("reason=complete", payload["text"])
            self.assertNotIn("run_id", payload)
            self.assertNotIn("supervision_id", payload)
            self.assertEqual(
                mailbox_constants.MAILBOX_SCHEMA_VERSION, payload["version"]
            )

    def test_supervision_end_round_trips_a_launch_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            launch = mailbox_validation.new_supervision_id()

            appended = mailbox_appenders.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                supervision_id=launch,
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)
            self.assertEqual(launch, view.messages[0].supervision_id)
            self.assertEqual(launch, appended.to_payload()["supervision_id"])
            self.assertEqual(
                launch,
                json.loads(mailbox_rendering.message_json(appended))["supervision_id"],
            )

    def test_supervision_end_appends_reject_malformed_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            cases = (
                (
                    mailbox_constants.REJECT_INVALID_SUPERVISION_OUTCOME,
                    {"text": "done", "outcome": "finished"},
                ),
                (
                    mailbox_constants.REJECT_INVALID_SUPERVISION_OUTCOME,
                    {"text": "done", "outcome": None},
                ),
                (
                    mailbox_constants.REJECT_MISSING_TEXT,
                    {"text": "   ", "outcome": "complete"},
                ),
                (
                    mailbox_constants.REJECT_MISSING_TEXT,
                    {"text": None, "outcome": "complete"},
                ),
                (
                    mailbox_constants.REJECT_INVALID_RUN_ID,
                    {"text": "done", "outcome": "complete", "run_id": " "},
                ),
                (
                    mailbox_constants.REJECT_INVALID_RUN_ID,
                    {
                        "text": "done",
                        "outcome": "complete",
                        "run_id": "r"
                        * (mailbox_constants.MAILBOX_MAX_RUN_ID_CHARS + 1),
                    },
                ),
                (
                    mailbox_constants.REJECT_INVALID_SUPERVISION_ID,
                    {
                        "text": "done",
                        "outcome": "complete",
                        "supervision_id": "helper-1",
                    },
                ),
            )

            for reason, kwargs in cases:
                with self.subTest(reason=reason, kwargs=kwargs):
                    with self.assertRaises(mailbox_models.MailboxError) as raised:
                        mailbox_appenders.append_supervision_end(
                            plan_dir,
                            kwargs["text"],
                            outcome=kwargs["outcome"],
                            run_id=kwargs.get("run_id"),
                            supervision_id=kwargs.get("supervision_id"),
                            state_dir=state_dir,
                        )
                    self.assertEqual(reason, raised.exception.reason)

            self.assertFalse(
                mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir).exists()
            )

    def test_persisted_supervision_ends_reject_malformed_and_old_versions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.parent.mkdir(parents=True, exist_ok=True)
            created_at = "0001-01-01T00:00:00+00:00"
            expected: list[str] = []
            lines: list[str] = []
            cases = (
                (
                    mailbox_constants.REJECT_UNKNOWN_KIND,
                    {"version": mailbox_constants.MAILBOX_LEGACY_SCHEMA_VERSION},
                ),
                (
                    mailbox_constants.REJECT_UNKNOWN_KIND,
                    {"version": mailbox_constants.MAILBOX_PREVIOUS_SCHEMA_VERSION},
                ),
                (
                    mailbox_constants.REJECT_INVALID_SUPERVISION_OUTCOME,
                    {"outcome": "finished"},
                ),
                (
                    mailbox_constants.REJECT_INVALID_SUPERVISION_OUTCOME,
                    {"outcome": None},
                ),
                (mailbox_constants.REJECT_MISSING_TEXT, {"text": "  "}),
                (mailbox_constants.REJECT_MISSING_TEXT, {"text": None}),
                (mailbox_constants.REJECT_INVALID_RUN_ID, {"run_id": ""}),
                (mailbox_constants.REJECT_INVALID_RUN_ID, {"run_id": 7}),
                (
                    mailbox_constants.REJECT_INVALID_SUPERVISION_ID,
                    {"supervision_id": "helper-1"},
                ),
                (
                    mailbox_constants.REJECT_INVALID_SUPERVISION_ID,
                    {"supervision_id": 7},
                ),
            )
            for index, (reason, overrides) in enumerate(cases):
                record = {
                    "created_at": created_at,
                    "id": f"00000000-0000-4000-8000-{index:012d}",
                    "kind": mailbox_vocabulary.KIND_SUPERVISION_END,
                    "outcome": mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                    "text": "supervision over",
                    "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
                }
                record.update(overrides)
                lines.append(
                    json.dumps(
                        {
                            key: value
                            for key, value in record.items()
                            if value is not None
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                expected.append(reason)
            mailbox_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)

            self.assertEqual((), view.messages)
            self.assertEqual(expected, [record.reason for record in view.rejected])
            self.assertIsNone(view.latest_supervision_end)

    def test_supervision_end_appends_where_a_notice_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            message_id = "00000000-0000-4000-8000-000000000001"
            empty_text_record = {
                "created_at": created_at,
                "id": message_id,
                "kind": mailbox_vocabulary.KIND_GUIDANCE,
                "text": "",
                "version": mailbox_constants.MAILBOX_SCHEMA_VERSION,
            }
            record_overhead = len(
                json.dumps(
                    empty_text_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            ordinary_cap = (
                mailbox_constants.MAILBOX_MAX_BYTES
                - mailbox_constants.MAILBOX_CLOSURE_RESERVE_BYTES
                - mailbox_constants.MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES
            )
            mailbox_appenders.append_guidance(
                plan_dir,
                "x" * (ordinary_cap - record_overhead),
                state_dir=state_dir,
                message_id=message_id,
                created_at=created_at,
            )
            self.assertEqual(
                ordinary_cap,
                mailbox.read_mailbox(plan_dir, state_dir=state_dir).size_bytes,
            )

            with self.assertRaises(mailbox_models.MailboxError) as raised:
                mailbox_appenders.append_notice(
                    plan_dir,
                    "supervision over",
                    run_id="run-A",
                    session_id="1",
                    turn_index=1,
                    state_dir=state_dir,
                )
            self.assertEqual(
                mailbox_constants.ERROR_MAILBOX_FULL, raised.exception.reason
            )

            appended = mailbox_appenders.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(appended, view.latest_supervision_end)
            self.assertGreater(view.size_bytes, ordinary_cap)

    def test_supervision_end_counts_extend_the_legacy_counts_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_guidance(
                plan_dir, "keep going", state_dir=state_dir
            )

            legacy_payload = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).counts.to_payload()
            self.assertEqual(
                {
                    "acknowledgements",
                    "guidance",
                    "pending_guidance",
                    "questions",
                    "rejected",
                    "replies",
                    "resolutions",
                    "notices",
                    "unanswered_questions",
                },
                set(legacy_payload),
            )

            mailbox_appenders.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
            )

            counts = mailbox.read_mailbox(plan_dir, state_dir=state_dir).counts
            self.assertEqual(1, counts.supervision_ends)
            payload = counts.to_payload()
            self.assertEqual(1, payload["supervision_ends"])
            self.assertEqual(0, payload["wait_starts"])
            self.assertEqual(0, payload["wait_ends"])
            self.assertEqual(0, payload["receipts"])
            self.assertEqual(
                legacy_payload,
                {key: value for key, value in payload.items() if key in legacy_payload},
            )
