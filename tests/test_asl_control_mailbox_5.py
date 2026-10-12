"""MailboxTestsPart5.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import dataclasses
import json
import stat
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import (
    mailbox,
)
from tests.asl_control_mailbox_support import MailboxTestsBase


class MailboxTestsPart5(MailboxTestsBase):
    def test_reply_timeout_race_is_consistent_across_processes(self) -> None:
        question, wait_start = mailbox.append_wait_question(
            self.plan_dir,
            "Reply before timeout?",
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=1,
            waiter_origin=mailbox.WAITER_ORIGIN_WORKER,
            created_at=self.timestamp(),
        )
        start_path = self.tmp / "start-race"
        script = """
import json
from pathlib import Path
import sys
import time

from agent_session_loop import mailbox

mode, plan_dir, state_dir, question_id, wait_id, start_path = sys.argv[1:]
deadline = time.monotonic() + 10
while not Path(start_path).exists():
    if time.monotonic() >= deadline:
        raise SystemExit("race start timed out")
    time.sleep(0.005)
try:
    if mode == "reply":
        result = mailbox.append_reply(
            plan_dir,
            question_id,
            "process reply",
            state_dir=state_dir,
        )
        payload = {"ok": True, "kind": result.kind}
    else:
        result = mailbox.finish_wait(
            plan_dir,
            question_id,
            wait_id,
            reason=mailbox.WAIT_END_REASON_TIMEOUT,
            state_dir=state_dir,
        )
        payload = {
            "ok": True,
            "kind": "finish",
            "consumed": result.consumed,
        }
except mailbox.MailboxError as error:
    payload = {"ok": False, "reason": error.reason}
print(json.dumps(payload))
"""

        common = (
            str(self.plan_dir),
            str(self.state_dir),
            question.id,
            wait_start.id,
            str(start_path),
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, mode, *common],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for mode in ("reply", "timeout")
        ]
        start_path.write_text("go\n", encoding="utf-8")
        outputs: list[dict[str, object]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(0, process.returncode, msg=stderr)
            outputs.append(json.loads(stdout))

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual((), view.rejected)
        self.assertIsNone(view.active_wait_for(question.id))
        wait_ends = [
            message
            for message in view.messages
            if message.kind == mailbox.KIND_WAIT_END and message.to == question.id
        ]
        self.assertEqual(1, len(wait_ends))

        closure = view.closure_for(question.id)
        if closure is None:
            self.assertEqual(
                mailbox.WAIT_END_REASON_TIMEOUT,
                wait_ends[0].wait_end_reason,
            )
            self.assertIn(
                {
                    "ok": False,
                    "reason": mailbox.REJECT_QUESTION_NOT_WAITING,
                },
                outputs,
            )
        else:
            self.assertEqual(mailbox.KIND_REPLY, closure.kind)
            self.assertEqual(
                mailbox.WAIT_END_REASON_CLOSURE,
                wait_ends[0].wait_end_reason,
            )
            self.assertIsNotNone(view.receipt_for_closure(closure.id))

    def test_size_cap_rejects_without_partial_write(self) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b"x" * mailbox.MAILBOX_MAX_BYTES
        path.write_bytes(original)

        with self.assertRaises(mailbox.MailboxError) as raised:
            mailbox.append_guidance(
                self.plan_dir,
                "does not fit",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)
        self.assertEqual(mailbox.MAILBOX_MAX_BYTES, path.stat().st_size)
        self.assertEqual(original, path.read_bytes())

    def test_live_input_validation_uses_stable_reasons(self) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "question",
            run_id="run-A",
            state_dir=self.state_dir,
        )
        cases = (
            (
                lambda: mailbox.append_guidance(
                    self.plan_dir,
                    "   ",
                    state_dir=self.state_dir,
                ),
                mailbox.REJECT_MISSING_TEXT,
            ),
            (
                lambda: mailbox.append_question(
                    self.plan_dir,
                    "question",
                    state_dir=self.state_dir,
                    message_id="not-a-uuid",
                ),
                mailbox.REJECT_INVALID_ID,
            ),
            (
                lambda: mailbox.append_question(
                    self.plan_dir,
                    "question",
                    state_dir=self.state_dir,
                    created_at="yesterday",
                ),
                mailbox.REJECT_INVALID_CREATED_AT,
            ),
            (
                lambda: mailbox.append_question(
                    self.plan_dir,
                    "question",
                    state_dir=self.state_dir,
                    session_id="01",
                ),
                mailbox.REJECT_INVALID_SESSION_ID,
            ),
            (
                lambda: mailbox.append_question(
                    self.plan_dir,
                    "question",
                    state_dir=self.state_dir,
                    waiter_origin="supervisor",  # type: ignore[arg-type]
                ),
                mailbox.REJECT_INVALID_WAITER_ORIGIN,
            ),
            (
                lambda: mailbox.append_resolution(
                    self.plan_dir,
                    question.id,
                    "answer",
                    expected_run_id="run-A",
                    state_dir=self.state_dir,
                ),
                mailbox.REJECT_INVALID_ACTION,
            ),
            (
                lambda: mailbox.append_resolution(
                    self.plan_dir,
                    question.id,
                    mailbox.RESOLUTION_ACTION_PAUSE,
                    expected_run_id="run-A",
                    note=1,  # type: ignore[arg-type]
                    state_dir=self.state_dir,
                ),
                mailbox.REJECT_INVALID_NOTE,
            ),
            (
                lambda: mailbox.append_notice(
                    self.plan_dir,
                    "notice",
                    run_id="run-A",
                    session_id="01",
                    turn_index=1,
                    state_dir=self.state_dir,
                ),
                mailbox.REJECT_INVALID_SESSION_ID,
            ),
            (
                lambda: mailbox.append_acknowledgement(
                    self.plan_dir,
                    -1,
                    state_dir=self.state_dir,
                ),
                mailbox.REJECT_INVALID_ACKNOWLEDGEMENT,
            ),
        )
        for operation, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(mailbox.MailboxError) as raised:
                    operation()
                self.assertEqual(reason, raised.exception.reason)

    def test_message_and_view_dataclasses_are_frozen(self) -> None:
        message = mailbox.append_guidance(
            self.plan_dir,
            "immutable",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            message.text = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.last_seq = 99  # type: ignore[misc]

    def test_lease_round_trip_is_locked_fixed_size_and_age_bounded(self) -> None:
        supervisor_id = self.message_id()
        updated = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        lease = mailbox.write_lease(
            self.plan_dir,
            supervisor_id,
            state_dir=self.state_dir,
            updated_at=updated.isoformat(),
        )

        path = self.lease_path()
        self.assertEqual(mailbox.MAILBOX_LEASE_BYTES, path.stat().st_size)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        fresh = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=updated + timedelta(seconds=30),
        )
        self.assertTrue(fresh.fresh)
        self.assertEqual(lease, fresh.lease)
        self.assertIsNone(fresh.reason)

        expired = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=updated + timedelta(seconds=mailbox.MAILBOX_LEASE_FRESH_SECONDS + 1),
        )
        self.assertFalse(expired.fresh)
        self.assertEqual(mailbox.LEASE_EXPIRED, expired.reason)
        self.assertEqual(lease, expired.lease)

    def test_v2_lease_projects_question_metadata_without_text(self) -> None:
        supervisor_id = self.message_id()
        question_id = self.message_id()
        updated = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        lease = mailbox.write_lease(
            self.plan_dir,
            supervisor_id,
            state_dir=self.state_dir,
            updated_at=updated.isoformat(),
            run_id="run-current",
            projection_seq=7,
            unanswered_questions=1,
            latest_unanswered_question_id=question_id,
            latest_unanswered_question_created_at=updated.isoformat(),
        )

        self.assertEqual(mailbox.MAILBOX_LEASE_BYTES, self.lease_path().stat().st_size)
        raw = self.lease_path().read_text(encoding="utf-8")
        self.assertNotIn("private question", raw)
        view = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=updated + timedelta(seconds=1),
        )
        self.assertTrue(view.fresh)
        self.assertEqual(lease, view.lease)
        assert view.lease is not None
        self.assertEqual("run-current", view.lease.run_id)
        self.assertEqual(7, view.lease.projection_seq)
        self.assertEqual(1, view.lease.unanswered_questions)
        self.assertEqual(question_id, view.lease.latest_unanswered_question_id)

    def test_v1_lease_remains_readable_without_projection(self) -> None:
        updated = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        payload = {
            "supervisor_id": self.message_id(),
            "updated_at": updated.isoformat(),
            "version": 1,
        }
        path = self.lease_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        path.write_bytes(encoded.ljust(mailbox.MAILBOX_LEASE_BYTES, b" "))

        view = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=updated + timedelta(seconds=1),
        )
        self.assertTrue(view.fresh)
        assert view.lease is not None
        self.assertEqual(1, view.lease.version)
        self.assertIsNone(view.lease.run_id)
        self.assertIsNone(view.lease.unanswered_questions)

    def test_missing_malformed_and_future_leases_are_not_fresh(self) -> None:
        now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        missing = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(missing.fresh)
        self.assertEqual(mailbox.LEASE_MISSING, missing.reason)

        mailbox.write_lease(
            self.plan_dir,
            self.message_id(),
            state_dir=self.state_dir,
            updated_at=(now + timedelta(seconds=1)).isoformat(),
        )
        future = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(future.fresh)
        self.assertEqual(mailbox.LEASE_FUTURE_UPDATED_AT, future.reason)

        self.lease_path().write_bytes(
            b"{not-json".ljust(mailbox.MAILBOX_LEASE_BYTES, b" ")
        )
        malformed = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(malformed.fresh)
        self.assertEqual(mailbox.LEASE_INVALID_JSON, malformed.reason)

        self.lease_path().write_bytes(b"{}\n")
        wrong_size = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(wrong_size.fresh)
        self.assertEqual(mailbox.LEASE_INVALID_SIZE, wrong_size.reason)

    def test_lease_requires_uuid_and_utc_timestamp(self) -> None:
        with self.assertRaises(mailbox.MailboxError) as invalid_id:
            mailbox.write_lease(
                self.plan_dir,
                "supervisor",
                state_dir=self.state_dir,
            )
        self.assertEqual(
            mailbox.LEASE_INVALID_SUPERVISOR_ID,
            invalid_id.exception.reason,
        )
        with self.assertRaises(mailbox.MailboxError) as invalid_time:
            mailbox.write_lease(
                self.plan_dir,
                self.message_id(),
                state_dir=self.state_dir,
                updated_at="2026-07-24T13:00:00+01:00",
            )
        self.assertEqual(
            mailbox.LEASE_INVALID_UPDATED_AT,
            invalid_time.exception.reason,
        )


if __name__ == "__main__":
    unittest.main()
