"""ParserAndStateTests (part 10).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_rendering,
    plans,
    state,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_supervision_end_outcome_can_be_resolved_at_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            appended = mailbox.append_supervision_end(
                plan_dir,
                "supervision ended: complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                supervision_id=mailbox.new_supervision_id(),
                state_dir=state_dir,
                resolve_at_commit=lambda: (
                    mailbox.SUPERVISION_OUTCOME_ERROR,
                    "supervision ended: error - SIGTERM",
                ),
            )

            self.assertEqual(
                mailbox.SUPERVISION_OUTCOME_ERROR, appended.supervision_outcome
            )
            self.assertEqual("supervision ended: error - SIGTERM", appended.text)
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual(appended, view.latest_supervision_end)

    def test_commit_time_resolution_keeps_the_record_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            message_id = "00000000-0000-4000-8000-00000000000a"
            created_at = "0001-01-01T00:00:00+00:00"

            appended = mailbox.append_supervision_end(
                plan_dir,
                "supervision ended: complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
                message_id=message_id,
                created_at=created_at,
                resolve_at_commit=lambda: (
                    mailbox.SUPERVISION_OUTCOME_ERROR,
                    "supervision ended: error - SIGINT",
                ),
            )

            self.assertEqual(message_id, appended.id)
            self.assertEqual(created_at, appended.created_at)

    def test_commit_time_resolution_is_validated_like_any_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_supervision_end(
                    plan_dir,
                    "supervision ended: complete",
                    outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    state_dir=state_dir,
                    resolve_at_commit=lambda: ("not-an-outcome", "late text"),
                )

            self.assertEqual(
                mailbox.REJECT_INVALID_SUPERVISION_OUTCOME,
                raised.exception.reason,
            )
            self.assertEqual(
                (),
                mailbox.read_mailbox(plan_dir, state_dir=state_dir).messages,
            )

    def test_resolution_that_moves_at_the_write_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            written: list[int] = []
            real_write = mailbox.os.write

            def writing(fd: int, data: bytes) -> int:
                # The resolution moves inside the write itself, the seam no
                # pre-write decision can observe.
                if b'"kind":"supervision-end"' in data:
                    written.append(fd)
                return real_write(fd, data)

            def resolve() -> tuple:
                if written:
                    return (
                        mailbox.SUPERVISION_OUTCOME_ERROR,
                        "supervision ended: error - SIGTERM",
                    )
                return (
                    mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    "supervision ended: complete",
                )

            with mock.patch.object(mailbox.os, "write", writing):
                appended = mailbox.append_supervision_end(
                    plan_dir,
                    "supervision ended: complete",
                    outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    supervision_id=mailbox.new_supervision_id(),
                    state_dir=state_dir,
                    resolve_at_commit=resolve,
                )

            self.assertEqual(
                mailbox.SUPERVISION_OUTCOME_ERROR, appended.supervision_outcome
            )
            self.assertEqual("supervision ended: error - SIGTERM", appended.text)
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)
            self.assertEqual(2, len(written))

    def test_resolution_that_moves_at_the_sync_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            synced: list[int] = []
            real_fullsync = mailbox.durability.fullsync

            def fullsync(fd: int) -> None:
                # The resolution moves inside the durability barrier, after the
                # bytes are written and after every decision that precedes it.
                synced.append(fd)
                real_fullsync(fd)

            def resolve() -> tuple:
                if synced:
                    return (
                        mailbox.SUPERVISION_OUTCOME_ERROR,
                        "supervision ended: error - SIGTERM",
                    )
                return (
                    mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    "supervision ended: complete",
                )

            with mock.patch.object(mailbox.durability, "fullsync", fullsync):
                appended = mailbox.append_supervision_end(
                    plan_dir,
                    "supervision ended: complete",
                    outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                    supervision_id=mailbox.new_supervision_id(),
                    state_dir=state_dir,
                    resolve_at_commit=resolve,
                )

            self.assertEqual(
                mailbox.SUPERVISION_OUTCOME_ERROR, appended.supervision_outcome
            )
            self.assertEqual("supervision ended: error - SIGTERM", appended.text)
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)
            # The corrected line clears its own barrier rather than riding out
            # the lock unsynced.
            self.assertEqual(2, len(synced))

    def test_resolution_that_never_settles_still_writes_one_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            calls: list[int] = []

            def resolve() -> tuple:
                calls.append(len(calls))
                return (
                    mailbox.SUPERVISION_OUTCOME_ERROR,
                    f"supervision ended: error - {len(calls)}",
                )

            appended = mailbox.append_supervision_end(
                plan_dir,
                "supervision ended: complete",
                outcome=mailbox.SUPERVISION_OUTCOME_COMPLETE,
                state_dir=state_dir,
                resolve_at_commit=resolve,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual((), view.rejected)
            self.assertEqual((appended,), view.messages)

    def test_message_json_renders_supervision_end_outcome_as_ascii_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            message = mailbox.append_supervision_end(
                plan_dir,
                "reason=terminé",
                outcome=mailbox.SUPERVISION_OUTCOME_PAUSED,
                state_dir=state_dir,
            )

            rendered = mailbox_rendering.message_json(message)

            self.assertTrue(rendered.isascii())
            self.assertNotIn("\n", rendered)
            self.assertEqual(
                {
                    "id": message.id,
                    "seq": message.seq,
                    "text": "reason=terminé",
                    "outcome": mailbox.SUPERVISION_OUTCOME_PAUSED,
                },
                json.loads(rendered),
            )

    def test_closure_reserve_cannot_be_consumed_by_non_closure_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            question = mailbox.append_question(
                plan_dir,
                "Which mode?",
                state_dir=state_dir,
                message_id="00000000-0000-4000-8000-000000000001",
                created_at="0001-01-01T00:00:00+00:00",
            )
            resolution_question = mailbox.append_question(
                plan_dir,
                "Continue?",
                state_dir=state_dir,
                run_id="run-A",
                message_id="00000000-0000-4000-8000-000000000003",
                created_at="0001-01-01T00:00:00+00:00",
            )
            created_at = "0001-01-01T00:00:00+00:00"
            message_id = "00000000-0000-4000-8000-000000000002"
            empty_text_record = {
                "created_at": created_at,
                "id": message_id,
                "kind": mailbox.KIND_GUIDANCE,
                "text": "",
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
            }
            record_overhead = len(
                json.dumps(
                    empty_text_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            # Fill exactly to the ordinary-write cap. Below it sit the
            # guidance-delivery reserve and then the closure/reply reserve, so
            # ordinary records stop short of both.
            ordinary_cap = (
                mailbox.MAILBOX_MAX_BYTES
                - mailbox.MAILBOX_REPLY_RESERVE_BYTES
                - mailbox.MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES
            )
            current_size = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).size_bytes
            mailbox.append_guidance(
                plan_dir,
                "x" * (ordinary_cap - current_size - record_overhead),
                state_dir=state_dir,
                message_id=message_id,
                created_at=created_at,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(ordinary_cap, view.size_bytes)
            self.assertFalse(view.full)
            self.assertFalse(view.accepts_questions)
            self.assertTrue(view.accepts_replies)
            self.assertTrue(view.accepts_resolutions)
            self.assertTrue(view.accepts_guidance_deliveries)
            view_payload = view.to_payload()
            self.assertFalse(view_payload["full"])
            self.assertFalse(view_payload["accepts_questions"])
            self.assertTrue(view_payload["accepts_replies"])
            self.assertTrue(view_payload["accepts_resolutions"])
            self.assertTrue(view_payload["accepts_guidance_deliveries"])
            self.assertEqual(
                mailbox.MAILBOX_CLOSURE_RESERVE_BYTES,
                mailbox.MAILBOX_REPLY_RESERVE_BYTES,
            )

            for append in (mailbox.append_guidance, mailbox.append_question):
                with self.assertRaises(mailbox.MailboxError) as raised:
                    append(plan_dir, "one more?", state_dir=state_dir)
                self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)
            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_acknowledgement(
                    plan_dir, view.last_seq, state_dir=state_dir
                )
            self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)
            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_notice(
                    plan_dir,
                    "one more notice",
                    run_id="run-A",
                    session_id="1",
                    turn_index=1,
                    state_dir=state_dir,
                )
            self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)

            # Guidance still owes an honest delivery outcome, so its reserve
            # survives the ordinary-write refusals above.
            delivery = mailbox.append_guidance_delivery(
                plan_dir,
                message_id,
                delivery_state=mailbox.GUIDANCE_STATE_DEFERRED,
                delivery_reason=mailbox.GUIDANCE_REASON_STALE_TURN,
                channel=mailbox.GUIDANCE_CHANNEL_LIVE,
                state_dir=state_dir,
            )
            self.assertEqual(message_id, delivery.to)

            reply = mailbox.append_reply(
                plan_dir, question.id, "x", state_dir=state_dir
            )
            self.assertEqual(question.id, reply.to)
            resolution = mailbox.append_resolution(
                plan_dir,
                resolution_question.id,
                mailbox.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="run-A",
                state_dir=state_dir,
            )
            self.assertEqual(resolution_question.id, resolution.to)

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            status = payload["mailbox"]
            self.assertFalse(status["full"])
            self.assertFalse(status["accepts_questions"])
            self.assertTrue(status["accepts_replies"])
            self.assertTrue(status["accepts_resolutions"])

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn(
                "Mailbox: active, non-closure writes refused "
                "(replies and resolutions still accepted)",
                output,
            )
            self.assertNotIn("Mailbox: active, full", output)

    def test_status_reports_when_minimal_reply_cannot_land(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            created_at = "0001-01-01T00:00:00+00:00"
            first_question = mailbox.append_question(
                plan_dir,
                "first?",
                state_dir=state_dir,
                message_id="00000000-0000-4000-8000-000000000001",
                created_at=created_at,
            )
            second_question = mailbox.append_question(
                plan_dir,
                "second?",
                state_dir=state_dir,
                message_id="00000000-0000-4000-8000-000000000002",
                created_at=created_at,
            )
            reply_id = "00000000-0000-4000-8000-000000000003"
            empty_reply_record = {
                "created_at": created_at,
                "id": reply_id,
                "kind": mailbox.KIND_REPLY,
                "text": "",
                "to": first_question.id,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
            }
            reply_overhead = len(
                json.dumps(
                    empty_reply_record,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            target_size = (
                mailbox.MAILBOX_MAX_BYTES - mailbox.MAILBOX_MIN_REPLY_BYTES + 1
            )
            current_size = mailbox.read_mailbox(
                plan_dir, state_dir=state_dir
            ).size_bytes
            mailbox.append_reply(
                plan_dir,
                first_question.id,
                "x" * (target_size - current_size - reply_overhead),
                state_dir=state_dir,
                message_id=reply_id,
                created_at=created_at,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            self.assertEqual(target_size, view.size_bytes)
            self.assertFalse(view.full)
            self.assertFalse(view.accepts_replies)
            with self.assertRaises(mailbox.MailboxError) as raised:
                mailbox.append_reply(
                    plan_dir, second_question.id, "x", state_dir=state_dir
                )
            self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            status = payload["mailbox"]
            self.assertFalse(status["full"])
            self.assertFalse(status["accepts_replies"])
            self.assertFalse(status["accepts_resolutions"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("reply and resolution capacity exhausted", output)
            self.assertNotIn("still accepted", output)

    def test_status_payload_surfaces_mailbox_read_error_and_stale_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
            mailbox_path.parent.mkdir(parents=True)
            mailbox_path.touch()
            mailbox.write_lease(
                plan_dir,
                "00000000-0000-4000-8000-000000000001",
                state_dir=state_dir,
                updated_at="2020-01-01T00:00:00+00:00",
            )

            read_error = mailbox.MailboxError(
                mailbox.ERROR_MAILBOX_READ,
                "permission denied",
                path=mailbox_path,
            )
            with mock.patch.object(mailbox, "read_mailbox", side_effect=read_error):
                payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            status = payload["mailbox"]
            self.assertTrue(status["present"])
            self.assertIsNone(status["size_bytes"])
            self.assertIsNone(status["full"])
            self.assertIsNone(status["accepts_questions"])
            self.assertIsNone(status["accepts_replies"])
            self.assertIsNone(status["accepts_resolutions"])
            self.assertIsNone(status["resolutions"])
            self.assertIsNone(status["notices"])
            self.assertIn(mailbox.ERROR_MAILBOX_READ, status["error"])
            self.assertTrue(status["lease_present"])
            self.assertFalse(status["lease_fresh"])
            self.assertEqual("2020-01-01T00:00:00+00:00", status["lease_updated_at"])
            self.assertEqual(mailbox.LEASE_EXPIRED, status["lease_reason"])
            self.assertIsNone(status["lease_error"])
