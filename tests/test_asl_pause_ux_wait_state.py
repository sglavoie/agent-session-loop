"""Test asl pause ux wait state."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    mailbox,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    run_coordination_mailbox_wait,
    run_coordination_wait_models,
    run_coordination_wait_record,
    run_coordination_wait_state,
    run_persistence,
    state,
    state_payloads,
)
from agent_session_loop import run_coordination_constants as wait_budgets
from tests import support_asl_pause_ux as fixtures
from tests.support import (
    read_jsonl,
)

ASL_TEST_LANE = "integration"


class ArtifactSafetyAndPauseUxTests(fixtures.ArtifactSafetyAndPauseUxCase):
    def test_waiting_state_helpers_have_no_pause_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_path = tmp_dir / events.STATE_FILE_NAME
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            payload = {
                "run_id": "run-1",
                "ok": True,
                "paused": False,
                "reason": "",
                "state_path": str(state_path),
            }
            started_at = datetime(2026, 7, 24, 12, tzinfo=UTC)
            waiting = run_coordination_wait_record.waiting_for_reply_payload(
                question_id="question-1",
                session_id="9",
                turn_index=3,
                started_at=started_at,
                base_deadline=started_at + timedelta(minutes=5),
                unsupervised_deadline_at=started_at + timedelta(minutes=15),
                unsupervised_ceiling_seconds=900.0,
                last_lease_activity_at=started_at + timedelta(minutes=1),
            )
            persistence = run_persistence._RunPersistence()

            with mock.patch.object(
                run_persistence, "_notify_agent_mux_loop_paused"
            ) as notify_paused:
                run_coordination_wait_state.set_waiting_for_reply(
                    event_path,
                    payload,
                    waiting,
                    pending_outbox=2,
                    persistence=persistence,
                )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            records = read_jsonl(event_path)
            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(waiting, persisted["waiting_for_reply"])
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("", payload["reason"])
            self.assertNotIn("pause_reason", payload)
            self.assertNotIn("pause_message", payload)
            self.assertEqual(1, len(records))
            self.assertEqual(events.MAILBOX_EVENT, records[0]["name"])
            self.assertEqual(
                events.MAILBOX_ACTION_WAIT_STARTED,
                records[0]["payload"]["action"],
            )
            self.assertEqual(2, records[0]["payload"]["pending_outbox"])
            self.assertNotIn(
                events.RUN_PAUSED_EVENT,
                [record["name"] for record in records],
            )
            notify_paused.assert_not_called()

            run_coordination_wait_state.clear_waiting_for_reply(
                event_path,
                payload,
                reason="reply",
                pending_outbox=1,
                persistence=persistence,
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("waiting_for_reply", payload)
            self.assertNotIn("waiting_for_reply", persisted)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            records = read_jsonl(event_path)
            self.assertEqual(2, len(records))
            self.assertEqual(
                events.MAILBOX_ACTION_WAIT_CLEARED,
                records[1]["payload"]["action"],
            )
            self.assertEqual("reply", records[1]["payload"]["reason"])
            self.assertEqual(1, records[1]["payload"]["pending_outbox"])
            self.assertEqual("question-1", records[1]["payload"]["question_id"])

    def test_resolution_clears_wait_with_body_free_identity_metadata(self) -> None:
        question_id = "00000000-0000-4000-8000-000000000101"
        resolution_id = "00000000-0000-4000-8000-000000000102"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Continue without an answer?",
            run_id="run-1",
            session_id="9",
            turn_index=3,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
        )
        resolution = mailbox_models.MailboxMessage(
            seq=2,
            id=resolution_id,
            kind=mailbox_vocabulary.KIND_RESOLUTION,
            created_at="2026-07-24T12:00:01+00:00",
            to=question_id,
            run_id="run-1",
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            note="sensitive supervisor note",
        )
        # A committed closure wins even when no further mailbox append can fit.
        view = mailbox_snapshot.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(question, resolution),
            last_seq=2,
            size_bytes=mailbox_constants.MAILBOX_MAX_BYTES,
        )
        settings = run_coordination_wait_models.MailboxWaitSettings(
            base_seconds=2.0,
            lease_extension_seconds=1.0,
            unsupervised_ceiling_seconds=5.0,
            poll_seconds=0.5,
            io_grace_seconds=1.0,
        )

        with mock.patch.object(mailbox, "read_mailbox", return_value=view):
            result = run_coordination_mailbox_wait.wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload={},
                question_id=question_id,
                run_id="run-1",
                settings=settings,
                persistence=run_persistence._RunPersistence(
                    write_state=False,
                    append_events=False,
                ),
            )

        self.assertEqual("resolution", result.reason)
        self.assertIsNone(result.reply)
        self.assertEqual(resolution, result.resolution)
        self.assertEqual(0, result.pending_outbox)

        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            payload = {
                "run_id": "run-1",
                "waiting_for_reply": {
                    "question_id": question_id,
                    "session_id": "9",
                    "turn_index": 3,
                },
            }
            run_coordination_wait_state.clear_waiting_for_reply(
                event_path,
                payload,
                reason=result.reason,
                pending_outbox=result.pending_outbox,
                resolution_action=resolution.action,
                resolution_message_id=resolution.id,
                resolution_seq=resolution.seq,
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            [record] = read_jsonl(event_path)

        event_payload = record["payload"]
        self.assertEqual("resolution", event_payload["reason"])
        self.assertEqual("continue", event_payload["resolution_action"])
        self.assertEqual(resolution_id, event_payload["resolution_message_id"])
        self.assertEqual(2, event_payload["resolution_seq"])
        self.assertNotIn("note", event_payload)
        self.assertNotIn("sensitive supervisor note", json.dumps(record))

    def test_rebase_waiting_deadlines_rewrites_state_without_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_path = tmp_dir / events.STATE_FILE_NAME
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            payload = {
                "run_id": "run-1",
                "ok": True,
                "paused": False,
                "reason": "",
                "state_path": str(state_path),
            }
            started_at = datetime(2026, 7, 24, 12, tzinfo=UTC)
            waiting = run_coordination_wait_record.waiting_for_reply_payload(
                question_id="question-1",
                session_id="9",
                turn_index=3,
                started_at=started_at,
                base_deadline=started_at + timedelta(minutes=5),
                unsupervised_deadline_at=started_at + timedelta(minutes=30),
                unsupervised_ceiling_seconds=1800.0,
                last_lease_activity_at=None,
            )
            persistence = run_persistence._RunPersistence()
            run_coordination_wait_state.set_waiting_for_reply(
                event_path,
                payload,
                waiting,
                pending_outbox=1,
                persistence=persistence,
            )

            # Eight hours of suspend later, only the deadlines move.
            wake = started_at + timedelta(hours=8, seconds=2)
            run_coordination_wait_state.rebase_waiting_deadlines(
                payload,
                question_id="question-1",
                base_deadline=wake + timedelta(minutes=3),
                unsupervised_deadline_at=wake + timedelta(minutes=28),
                persistence=persistence,
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            records = read_jsonl(event_path)
            rebased = payload["waiting_for_reply"]
            self.assertEqual(rebased, persisted["waiting_for_reply"])
            self.assertEqual(
                (wake + timedelta(minutes=3)).isoformat(),
                rebased["base_deadline"],
            )
            self.assertEqual(
                (wake + timedelta(minutes=28)).isoformat(),
                rebased["unsupervised_deadline_at"],
            )
            self.assertEqual(waiting["started_at"], rebased["started_at"])
            self.assertFalse(payload["paused"])
            self.assertEqual(1, len(records))
            self.assertEqual(
                events.MAILBOX_ACTION_WAIT_STARTED,
                records[0]["payload"]["action"],
            )

            lock_info = state.LockInfo(
                path=tmp_dir / "plan-lock.json",
                pid=os.getpid(),
                plan_dir=str(tmp_dir),
                created_at="2026-07-24T12:00:00+00:00",
                stale=False,
                reason="active",
                raw={},
            )
            self.assertEqual(
                state_payloads.WAITING_FOR_REPLY_STATUS_LIVE,
                state_payloads.waiting_for_reply_status(
                    rebased,
                    lock_info=lock_info,
                    now=wake,
                ),
            )
            self.assertEqual(
                state_payloads.WAITING_FOR_REPLY_STATUS_EXPIRED,
                state_payloads.waiting_for_reply_status(
                    waiting,
                    lock_info=lock_info,
                    now=wake,
                ),
            )

    def test_mailbox_reply_wait_settings_fail_safe_on_bad_overrides(self) -> None:
        settings = run_coordination_wait_models.mailbox_reply_wait_settings(
            {
                wait_budgets.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "nan",
                wait_budgets.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR: "-1",
                wait_budgets.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "0",
                wait_budgets.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "not-a-number",
                wait_budgets.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "inf",
            }
        )
        self.assertEqual(
            wait_budgets.MAILBOX_REPLY_WAIT_BASE_SECONDS,
            settings.base_seconds,
        )
        self.assertEqual(
            wait_budgets.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS,
            settings.lease_extension_seconds,
        )
        self.assertEqual(
            wait_budgets.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS,
            settings.unsupervised_ceiling_seconds,
        )
        self.assertEqual(
            wait_budgets.MAILBOX_REPLY_WAIT_POLL_SECONDS,
            settings.poll_seconds,
        )
        self.assertEqual(
            wait_budgets.MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS,
            settings.io_grace_seconds,
        )

        clamped = run_coordination_wait_models.mailbox_reply_wait_settings(
            {
                wait_budgets.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "20",
                wait_budgets.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "10",
            }
        )
        self.assertEqual(10.0, clamped.base_seconds)
        self.assertEqual(10.0, clamped.unsupervised_ceiling_seconds)


if __name__ == "__main__":
    unittest.main()
