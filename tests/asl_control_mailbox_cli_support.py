"""Shared fixtures for the MailboxCliTests modules split from test_asl_control."""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    mailbox,
    mailbox_appenders,
    mailbox_cli_constants,
    mailbox_cli_outbox,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    mailbox_vocabulary,
    process_tree,
    supervision_liveness,
)
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    write_plan,
)


class MailboxCliTestsBase(unittest.TestCase):
    """Public mailbox commands, cursor waits, and lazy parser wiring."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        write_plan(self.plan_dir, ("pending",))

    def args(self, **overrides) -> argparse.Namespace:
        values = {
            "plan_dir": str(self.plan_dir),
            "state_dir": str(self.state_dir),
            "json": True,
            "text": None,
            "text_stdin": False,
            "note": None,
            "note_stdin": False,
            "action": None,
            "to": None,
            "peek": False,
            "question_id": None,
            "wait": False,
            "wait_consumed": False,
            "wait_delivered": False,
            "wait_timeout": None,
            "since": 0,
            "timeout": mailbox_cli_constants.DEFAULT_OUTBOX_WAIT_TIMEOUT_SECONDS,
            "include": mailbox_cli_constants.OUTBOX_INCLUDE_QUESTIONS,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def payload_from(self, handler, args) -> tuple[int, dict]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = handler(args)
        return exit_code, json.loads(stdout.getvalue())

    def cli_payload_from(self, argv: list[str]) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(argv)
        return exit_code, json.loads(stdout.getvalue()), stderr.getvalue()

    def write_active_turn(
        self,
        *,
        run_id: str = "run-live",
        session_id: str = "5",
        turn_index: int = 2,
        harness: str | None = None,
    ) -> None:
        active_turn: dict[str, Any] = {
            "run_id": run_id,
            "session_id": session_id,
            "turn_index": turn_index,
        }
        if harness is not None:
            active_turn["harness"] = harness
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": run_id,
                "ok": True,
                "paused": False,
                "active_turn": active_turn,
            },
        )
        _write_lock_file(
            self.plan_dir,
            self.state_dir,
            pid=os.getpid(),
        )

    def _deliver_on_first_poll(self, **overrides):
        """Append a delivery record the first time the wait polls the mailbox.

        Hooking the change token keeps the test deterministic: the delivery
        lands before the wait's first read, so no iteration ever sleeps.
        """
        real_token = mailbox.mailbox_change_token
        appended: list[mailbox_models.MailboxMessage] = []

        def hook(path):
            if not appended:
                view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
                guidance = next(
                    message
                    for message in reversed(view.messages)
                    if message.kind == mailbox_vocabulary.KIND_GUIDANCE
                )
                values = {
                    "delivery_state": mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
                    "delivery_reason": mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
                    "channel": mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
                }
                values.update(overrides)
                appended.append(
                    mailbox_appenders.append_guidance_delivery(
                        self.plan_dir,
                        guidance.id,
                        state_dir=self.state_dir,
                        **values,
                    )
                )
            return real_token(path)

        return mock.patch.object(mailbox, "mailbox_change_token", side_effect=hook)

    def _write_projected_lease(
        self,
        supervisor_id: str,
        question: mailbox_models.MailboxMessage,
        *,
        run_id: str = "run-live",
        age_seconds: float = 30.0,
    ) -> mailbox_models.MailboxLease:
        anchored = datetime.now(UTC) - timedelta(seconds=age_seconds)
        return mailbox_leases.write_lease(
            self.plan_dir,
            supervisor_id,
            state_dir=self.state_dir,
            updated_at=anchored.isoformat(),
            run_id=run_id,
            projection_seq=question.seq,
            unanswered_questions=1,
            latest_unanswered_question_id=question.id,
            latest_unanswered_question_created_at=question.created_at,
        )

    def _lease_on_disk(self) -> mailbox_models.MailboxLease:
        view = mailbox_leases.read_lease(self.plan_dir, state_dir=self.state_dir)
        self.assertTrue(view.fresh)
        assert view.lease is not None
        return view.lease

    def assert_heartbeat(
        self,
        original: mailbox_models.MailboxLease,
        after: mailbox_models.MailboxLease,
    ) -> None:
        """Everything but ``updated_at`` survived byte-identically."""
        self.assertEqual(
            original,
            dataclasses.replace(after, updated_at=original.updated_at),
        )
        self.assertNotEqual(original.updated_at, after.updated_at)

    def supervise_lines(
        self,
        *,
        timeout: float = 0,
        **overrides,
    ) -> tuple[int, list[str]]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = mailbox_cli_outbox.outbox_supervise_from_args(
                self.args(json=False, timeout=timeout, **overrides)
            )
        return code, stdout.getvalue().splitlines()

    def cursor_on_disk(self, supervision_id: str) -> int:
        return mailbox_cursors.read_supervision_cursor(
            self.plan_dir,
            supervision_id,
            state_dir=self.state_dir,
        )

    def write_liveness_record(self, supervision_id: str) -> None:
        supervision_liveness.write_record(
            self.plan_dir,
            supervision_id,
            pid=4242,
            state_dir=self.state_dir,
            run_command=lambda *args, **kwargs: subprocess.CompletedProcess(
                list(process_tree.PS_BIRTH_COMMAND),
                0,
                stdout="Sun Jul 26 17:57:01 2026\n",
                stderr="",
            ),
        )

    def age_grace_anchor(self, supervision_id: str, seconds: float) -> None:
        """Backdate this launch's grace anchor as an earlier poll would have."""
        mailbox_cursors.write_supervision_cursor(
            self.plan_dir,
            supervision_id,
            0,
            state_dir=self.state_dir,
            first_seen_at=datetime.now(UTC) - timedelta(seconds=seconds),
        )
