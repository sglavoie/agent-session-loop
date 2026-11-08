"""File safety cases for MailboxTestsPart5."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_session_loop import (
    mailbox,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
)
from tests.asl_control_mailbox_support import MailboxTestsBase


class MailboxTestsPart5(MailboxTestsBase):
    def test_cursor_read_ignores_symlinked_payload(self) -> None:
        supervision_id = self.message_id()
        mailbox_cursors.write_supervision_cursor(
            self.plan_dir, supervision_id, 42, state_dir=self.state_dir
        )
        path = mailbox_cursors.supervision_cursor_path_for_plan(
            self.plan_dir, supervision_id, self.state_dir
        )
        target = self.tmp / "original-cursor"
        path.rename(target)
        path.symlink_to(target)

        self.assertEqual(
            0,
            mailbox_cursors.read_supervision_cursor(
                self.plan_dir, supervision_id, state_dir=self.state_dir
            ),
        )

    def test_fifo_cursor_read_returns_without_blocking(self) -> None:
        supervision_id = self.message_id()
        path = mailbox_cursors.supervision_cursor_path_for_plan(
            self.plan_dir, supervision_id, self.state_dir
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        script = (
            "import sys; "
            "from agent_session_loop import mailbox_cursors; "
            "print(mailbox_cursors.read_supervision_cursor("
            "sys.argv[1], sys.argv[2], state_dir=sys.argv[3]))"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.plan_dir),
                supervision_id,
                str(self.state_dir),
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        self.assertEqual("0", result.stdout.strip())

    def test_change_token_forces_read_after_link_replacement(self) -> None:
        self.guidance("notice")
        path = self.mailbox_path()
        original_token = mailbox.mailbox_change_token(path)
        target = self.tmp / "original-mailbox"
        path.rename(target)
        path.symlink_to(target)

        self.assertIs(
            mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE,
            mailbox.mailbox_change_token(path),
        )
        with self.assertRaises(mailbox_models.MailboxError) as symlink_error:
            mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            mailbox_constants.ERROR_MAILBOX_READ, symlink_error.exception.reason
        )

        path.unlink()
        os.link(target, path)
        self.assertIs(
            mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE,
            mailbox.mailbox_change_token(path),
        )
        with self.assertRaises(mailbox_models.MailboxError) as hardlink_error:
            mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            mailbox_constants.ERROR_MAILBOX_READ, hardlink_error.exception.reason
        )
        self.assertNotEqual(
            mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE, original_token
        )

    def test_mailbox_writers_reject_symlinks_and_hard_links(self) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.tmp / "other-file"
        target.write_text("sentinel", encoding="utf-8")

        path.symlink_to(target)
        with self.assertRaises(mailbox_models.MailboxError) as guidance_error:
            self.guidance("unsafe")
        self.assertEqual(
            mailbox_constants.ERROR_MAILBOX_WRITE, guidance_error.exception.reason
        )
        with self.assertRaises(mailbox_models.MailboxError) as wait_error:
            mailbox_waits.append_wait_question(
                self.plan_dir,
                "unsafe",
                state_dir=self.state_dir,
                run_id="run-A",
                session_id="1",
                turn_index=1,
                waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
            )
        self.assertEqual(
            mailbox_constants.ERROR_MAILBOX_WRITE, wait_error.exception.reason
        )
        self.assertEqual("sentinel", target.read_text(encoding="utf-8"))

        path.unlink()
        os.link(target, path)
        with self.assertRaises(mailbox_models.MailboxError) as hardlink_error:
            self.guidance("unsafe")
        self.assertEqual(
            mailbox_constants.ERROR_MAILBOX_WRITE, hardlink_error.exception.reason
        )
        self.assertEqual("sentinel", target.read_text(encoding="utf-8"))

    def test_finish_wait_rejects_a_replaced_mailbox_symlink(self) -> None:
        question, wait = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Question?",
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=1,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
        )
        self.mailbox_path().unlink()
        target = self.tmp / "other-file"
        target.write_text("sentinel", encoding="utf-8")
        self.mailbox_path().symlink_to(target)

        with self.assertRaises(mailbox_models.MailboxError) as error:
            mailbox_waits.finish_wait(
                self.plan_dir,
                question.id,
                wait.id,
                reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
                state_dir=self.state_dir,
            )
        self.assertEqual(mailbox_constants.ERROR_MAILBOX_WRITE, error.exception.reason)
        self.assertEqual("sentinel", target.read_text(encoding="utf-8"))

    def test_lease_writer_and_readers_reject_symlinks(self) -> None:
        path = self.lease_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.tmp / "other-file"
        target.write_text("sentinel", encoding="utf-8")
        path.symlink_to(target)

        with self.assertRaises(mailbox_models.MailboxError) as write_error:
            mailbox_leases.write_lease(
                self.plan_dir, self.message_id(), state_dir=self.state_dir
            )
        self.assertEqual(
            mailbox_constants.ERROR_LEASE_WRITE, write_error.exception.reason
        )
        with self.assertRaises(mailbox_models.MailboxError) as read_error:
            mailbox_leases.read_lease(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            mailbox_constants.ERROR_LEASE_READ, read_error.exception.reason
        )
        self.assertEqual("sentinel", target.read_text(encoding="utf-8"))

    def test_fifo_mailbox_and_lease_reads_fail_without_blocking(self) -> None:
        for path, reader, expected in (
            (self.mailbox_path(), "mailbox", mailbox_constants.ERROR_MAILBOX_READ),
            (self.lease_path(), "lease", mailbox_constants.ERROR_LEASE_READ),
        ):
            with self.subTest(reader=reader):
                path.parent.mkdir(parents=True, exist_ok=True)
                os.mkfifo(path)
                script = (
                    "import sys; "
                    "from agent_session_loop import mailbox, mailbox_leases, "
                    "mailbox_models; "
                    "reader = mailbox.read_mailbox if sys.argv[1] == 'mailbox' "
                    "else mailbox_leases.read_lease; "
                    "\ntry: reader(sys.argv[2], state_dir=sys.argv[3])"
                    "\nexcept mailbox_models.MailboxError as error: print(error.reason)"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        reader,
                        str(self.plan_dir),
                        str(self.state_dir),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=True,
                )
                self.assertEqual(expected, result.stdout.strip())
                path.unlink()

    def test_fifo_mailbox_does_not_trap_a_timed_outbox_wait(self) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        script = (
            "import sys, uuid; "
            "from agent_session_loop import mailbox_cli_waits, mailbox_models; "
            "\ntry: mailbox_cli_waits.wait_for_questions("
            "sys.argv[1], state_dir=sys.argv[2], since=0, timeout=0.1, "
            "supervisor_id=str(uuid.uuid4()))"
            "\nexcept mailbox_models.MailboxError as error: print(error.reason)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.plan_dir), str(self.state_dir)],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        self.assertEqual(mailbox_constants.ERROR_MAILBOX_READ, result.stdout.strip())

    def test_reply_timeout_race_is_consistent_across_processes(self) -> None:
        question, wait_start = mailbox_waits.append_wait_question(
            self.plan_dir,
            "Reply before timeout?",
            state_dir=self.state_dir,
            run_id="run-A",
            session_id="1",
            turn_index=1,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_WORKER,
            created_at=self.timestamp(),
        )
        start_path = self.tmp / "start-race"
        script = """
import json
from pathlib import Path
import sys
import time

from agent_session_loop import (
    mailbox_appenders,
    mailbox_models,
    mailbox_vocabulary,
    mailbox_waits,
)

mode, plan_dir, state_dir, question_id, wait_id, start_path = sys.argv[1:]
deadline = time.monotonic() + 10
while not Path(start_path).exists():
    if time.monotonic() >= deadline:
        raise SystemExit("race start timed out")
    time.sleep(0.005)
try:
    if mode == "reply":
        result = mailbox_appenders.append_reply(
            plan_dir,
            question_id,
            "process reply",
            state_dir=state_dir,
        )
        payload = {"ok": True, "kind": result.kind}
    else:
        result = mailbox_waits.finish_wait(
            plan_dir,
            question_id,
            wait_id,
            reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
            state_dir=state_dir,
        )
        payload = {
            "ok": True,
            "kind": "finish",
            "consumed": result.consumed,
        }
except mailbox_models.MailboxError as error:
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
            if message.kind == mailbox_vocabulary.KIND_WAIT_END
            and message.to == question.id
        ]
        self.assertEqual(1, len(wait_ends))

        closure = view.closure_for(question.id)
        if closure is None:
            self.assertEqual(
                mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
                wait_ends[0].wait_end_reason,
            )
            self.assertIn(
                {
                    "ok": False,
                    "reason": mailbox_constants.REJECT_QUESTION_NOT_WAITING,
                },
                outputs,
            )
        else:
            self.assertEqual(mailbox_vocabulary.KIND_REPLY, closure.kind)
            self.assertEqual(
                mailbox_vocabulary.WAIT_END_REASON_CLOSURE,
                wait_ends[0].wait_end_reason,
            )
            self.assertIsNotNone(view.receipt_for_closure(closure.id))
