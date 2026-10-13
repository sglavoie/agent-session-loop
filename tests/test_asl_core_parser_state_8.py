"""ParserAndStateTests (part 8).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    mailbox,
    mailbox_append_record,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_validation,
    mailbox_vocabulary,
    plans,
    state,
    state_abandonment,
    state_mailbox_status,
    state_status,
    state_status_text,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_status_projects_the_latest_attempt_behind_each_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            deferred_guidance = mailbox_appenders.append_guidance(
                plan_dir, "steer me", state_dir=state_dir
            )
            delivered_guidance = mailbox_appenders.append_guidance(
                plan_dir, "and me", state_dir=state_dir
            )
            mailbox_appenders.append_guidance_delivery(
                plan_dir,
                deferred_guidance.id,
                delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
                delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER,
                channel=mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
                run_id="run-1",
                session_id="5",
                turn_index=2,
                harness="copilot",
                state_dir=state_dir,
            )
            delivery = mailbox_appenders.append_guidance_delivery(
                plan_dir,
                delivered_guidance.id,
                delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
                delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
                channel=mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
                run_id="run-1",
                session_id="5",
                turn_index=3,
                harness="claude",
                state_dir=state_dir,
            )

            payload = state_status.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            first, second = payload["mailbox"]["guidance"]
            self.assertEqual(mailbox_vocabulary.GUIDANCE_STATE_DEFERRED, first["state"])
            self.assertEqual(
                {
                    "delivery_state": mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
                    "reason": (
                        mailbox_vocabulary.GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER
                    ),
                    "channel": mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
                    "harness": "copilot",
                    "run_id": "run-1",
                    "session_id": "5",
                    "turn_index": 2,
                },
                {
                    key: first["latest_attempt"][key]
                    for key in (
                        "delivery_state",
                        "reason",
                        "channel",
                        "harness",
                        "run_id",
                        "session_id",
                        "turn_index",
                    )
                },
            )
            self.assertEqual(
                mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE, second["state"]
            )
            self.assertEqual(delivery.id, second["latest_attempt"]["delivery_id"])
            self.assertEqual(
                delivery.created_at, second["latest_attempt"]["recorded_at"]
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)
            self.assertIn("delivered live to claude", buffer.getvalue())

    def test_status_guidance_projection_never_carries_a_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_guidance(
                plan_dir, "a body nobody else may read", state_dir=state_dir
            )

            payload = state_status.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            self.assertNotIn(
                "a body nobody else may read", json.dumps(payload["mailbox"])
            )

    def test_status_guidance_projection_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            appended = [
                mailbox_appenders.append_guidance(
                    plan_dir, f"note {index}", state_dir=state_dir
                )
                for index in range(state_mailbox_status.MAILBOX_STATUS_GUIDANCE_CAP + 4)
            ]

            payload = state_status.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            entries = payload["mailbox"]["guidance"]
            self.assertEqual(
                state_mailbox_status.MAILBOX_STATUS_GUIDANCE_CAP, len(entries)
            )
            # The recent end is kept: "did what I just said get through?".
            self.assertEqual(appended[-1].id, entries[-1]["id"])

    def test_status_payload_counts_open_questions_only_for_the_live_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_question(
                plan_dir,
                "from a run that already ended",
                state_dir=state_dir,
                run_id="RUN-OLD",
            )
            mailbox_appenders.append_question(
                plan_dir,
                "predates run attribution",
                state_dir=state_dir,
            )
            mailbox_appenders.append_question(
                plan_dir,
                "the live run is blocked on this",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            resolved = mailbox_appenders.append_question(
                plan_dir,
                "the live run already closed this",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            mailbox_appenders.append_resolution(
                plan_dir,
                resolved.id,
                mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="RUN-LIVE",
                state_dir=state_dir,
            )
            mailbox_appenders.append_notice(
                plan_dir,
                "checkpoint",
                run_id="RUN-LIVE",
                session_id="1",
                turn_index=1,
                state_dir=state_dir,
            )
            _write_state_payload(
                plan_dir,
                state_dir,
                {"run_id": "RUN-LIVE", "ok": True, "paused": False},
            )
            _write_lock_file(plan_dir, state_dir, pid=os.getpid())

            payload = state_status.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            status = payload["mailbox"]
            self.assertEqual(3, status["unanswered_questions"])
            self.assertEqual(1, status["open_questions"])
            self.assertEqual(1, status["resolutions"])
            self.assertEqual(1, status["notices"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("1 open questions (2 unanswered historical)", output)
            self.assertIn("1 resolutions, 1 notices", output)

    def test_status_payload_projects_supervision_ends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            mailbox_appenders.append_supervision_end(
                plan_dir,
                "reason=complete",
                outcome=mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
                run_id="RUN-LIVE",
                state_dir=state_dir,
            )

            payload = state_status.status_payload(
                plans.inspect_plan(plan_dir), state_dir
            )

            status = payload["mailbox"]
            self.assertEqual(1, status["supervision_ends"])
            self.assertEqual(0, status["rejected_records"])
            self.assertIsNone(status["error"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("0 notices, 1 supervision ends", output)

    def test_status_payload_retires_open_questions_once_the_run_ends(
        self,
    ) -> None:
        cases = (
            ("paused", {"run_id": "RUN-LIVE", "paused": True}),
            (
                "finished",
                {
                    "run_id": "RUN-LIVE",
                    "paused": False,
                    "run_finished_at": "2026-07-29T01:45:51+00:00",
                },
            ),
        )
        for label, runner_payload in cases:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                plan_dir = Path(tmp) / "plan"
                state_dir = Path(tmp) / "state"
                write_plan(plan_dir, ("pending",))
                mailbox_appenders.append_question(
                    plan_dir,
                    "abandoned when the wait timed out",
                    state_dir=state_dir,
                    run_id="RUN-LIVE",
                )
                _write_state_payload(plan_dir, state_dir, runner_payload)
                _write_lock_file(plan_dir, state_dir, pid=os.getpid())

                payload = state_status.status_payload(
                    plans.inspect_plan(plan_dir), state_dir
                )

                status = payload["mailbox"]
                self.assertEqual(1, status["unanswered_questions"])
                self.assertEqual(0, status["open_questions"])

    def test_live_run_id_requires_a_readable_unfinished_lock_guarded_run(
        self,
    ) -> None:
        active = state.LockInfo(
            path=Path("/tmp/lock.json"),
            pid=os.getpid(),
            plan_dir="/tmp/plan",
            created_at="2026-07-29T00:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )
        stale = dataclasses.replace(active, stale=True, reason="stale")
        live = {"present": True, "error": None, "run_id": "RUN-LIVE"}

        self.assertEqual("RUN-LIVE", state_abandonment._live_run_id(live, active))
        self.assertIsNone(state_abandonment._live_run_id(live, stale))
        self.assertIsNone(state_abandonment._live_run_id(live, None))
        self.assertIsNone(
            state_abandonment._live_run_id({**live, "paused": True}, active)
        )
        self.assertIsNone(
            state_abandonment._live_run_id(
                {**live, "run_finished_at": "2026-07-29T01:00:00+00:00"},
                active,
            )
        )
        self.assertIsNone(
            state_abandonment._live_run_id({**live, "error": "unreadable"}, active)
        )
        self.assertIsNone(
            state_abandonment._live_run_id({**live, "present": False}, active)
        )
        self.assertIsNone(state_abandonment._live_run_id({"present": True}, active))

    def test_min_append_bytes_matches_generated_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            message_id = "00000000-0000-4000-8000-000000000001"
            now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
            created_at = "2026-07-25T12:00:00.000000+00:00"

            with (
                mock.patch.object(
                    mailbox_append_record, "_new_id", return_value=message_id
                ),
                mock.patch.object(mailbox_validation, "_utc_now", return_value=now),
            ):
                appended = mailbox_appenders.append_acknowledgement(
                    plan_dir,
                    0,
                    state_dir=state_dir,
                )

            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            self.assertEqual(message_id, appended.id)
            self.assertEqual(created_at, appended.created_at)
            self.assertEqual(
                mailbox_constants.MAILBOX_MIN_APPEND_BYTES,
                len(mailbox_path.read_bytes()),
            )

    def test_min_reply_bytes_matches_generated_minimal_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            question_id = "00000000-0000-4000-8000-000000000001"
            reply_id = "00000000-0000-4000-8000-000000000002"
            now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
            question = mailbox_appenders.append_question(
                plan_dir,
                "x",
                state_dir=state_dir,
                message_id=question_id,
                created_at="2026-07-25T12:00:00.000000+00:00",
            )
            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            size_before = mailbox_path.stat().st_size

            with (
                mock.patch.object(
                    mailbox_append_record, "_new_id", return_value=reply_id
                ),
                mock.patch.object(mailbox_validation, "_utc_now", return_value=now),
            ):
                appended = mailbox_appenders.append_reply(
                    plan_dir,
                    question.id,
                    "x",
                    state_dir=state_dir,
                )

            self.assertEqual(reply_id, appended.id)
            self.assertEqual(
                mailbox_constants.MAILBOX_MIN_REPLY_BYTES,
                mailbox_path.stat().st_size - size_before,
            )

    def test_min_resolution_bytes_matches_generated_minimal_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            question_id = "00000000-0000-4000-8000-000000000001"
            resolution_id = "00000000-0000-4000-8000-000000000002"
            now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
            question = mailbox_appenders.append_question(
                plan_dir,
                "x",
                state_dir=state_dir,
                message_id=question_id,
                created_at="2026-07-25T12:00:00.000000+00:00",
                run_id="x",
            )
            mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
            size_before = mailbox_path.stat().st_size

            with (
                mock.patch.object(
                    mailbox_append_record, "_new_id", return_value=resolution_id
                ),
                mock.patch.object(mailbox_validation, "_utc_now", return_value=now),
            ):
                appended = mailbox_appenders.append_resolution(
                    plan_dir,
                    question.id,
                    mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                    expected_run_id="x",
                    state_dir=state_dir,
                )

            self.assertEqual(resolution_id, appended.id)
            self.assertEqual(
                mailbox_constants.MAILBOX_MIN_RESOLUTION_BYTES,
                mailbox_path.stat().st_size - size_before,
            )

    def test_resolution_empty_note_and_omitted_note_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            empty_note_question = mailbox_appenders.append_question(
                plan_dir,
                "Continue with an empty note?",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            omitted_note_question = mailbox_appenders.append_question(
                plan_dir,
                "Pause without a note?",
                state_dir=state_dir,
                run_id="RUN-LIVE",
            )
            mailbox_appenders.append_resolution(
                plan_dir,
                empty_note_question.id,
                mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
                expected_run_id="RUN-LIVE",
                note="",
                state_dir=state_dir,
            )
            mailbox_appenders.append_resolution(
                plan_dir,
                omitted_note_question.id,
                mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
                expected_run_id="RUN-LIVE",
                state_dir=state_dir,
            )

            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            empty_note_closure = view.closure_for(empty_note_question.id)
            empty_note_resolution = view.resolution_for(empty_note_question.id)
            omitted_note_closure = view.closure_for(omitted_note_question.id)
            omitted_note_resolution = view.resolution_for(omitted_note_question.id)

            self.assertIsNotNone(empty_note_closure)
            self.assertIsNotNone(empty_note_resolution)
            self.assertEqual("", empty_note_closure.note)
            self.assertEqual("", empty_note_resolution.note)
            self.assertIsNotNone(omitted_note_closure)
            self.assertIsNotNone(omitted_note_resolution)
            self.assertIsNone(omitted_note_closure.note)
            self.assertIsNone(omitted_note_resolution.note)
