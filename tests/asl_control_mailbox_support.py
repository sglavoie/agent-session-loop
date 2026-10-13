"""Shared fixtures for the MailboxTests modules split from test_asl_control."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from agent_session_loop import (
    mailbox_appenders,
    mailbox_cursors,
    mailbox_models,
    mailbox_vocabulary,
)


class MailboxTestsBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")

    def mailbox_path(self) -> Path:
        return mailbox_cursors.mailbox_path_for_plan(self.plan_dir, self.state_dir)

    def lease_path(self) -> Path:
        return mailbox_cursors.lease_path_for_plan(self.plan_dir, self.state_dir)

    def append_raw(self, payload: object) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as mailbox_file:
            if isinstance(payload, bytes):
                mailbox_file.write(payload)
            else:
                mailbox_file.write(
                    (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
                )

    @staticmethod
    def message_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def timestamp() -> str:
        return "2026-07-24T12:00:00+00:00"

    def guidance(
        self, text: str = "Prefer the existing parser."
    ) -> mailbox_models.MailboxMessage:
        return mailbox_appenders.append_guidance(
            self.plan_dir, text, state_dir=self.state_dir
        )

    def deliver(self, guidance_id: str, **overrides) -> mailbox_models.MailboxMessage:
        values = {
            "delivery_state": mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
            "delivery_reason": mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
            "channel": mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
        }
        values.update(overrides)
        return mailbox_appenders.append_guidance_delivery(
            self.plan_dir,
            guidance_id,
            state_dir=self.state_dir,
            **values,
        )
