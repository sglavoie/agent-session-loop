# mypy: ignore-errors
# ruff: noqa
"""Conversation reference, continuation intent, and durable ledger tests.

The behaviour under test is continuity across an interruption: a native harness
conversation has to survive the replacement of ``state.json`` by the next run,
and an invalid or unproven reference has to refuse rather than reach for some
other conversation. The cases below are organised around the ways that can go
wrong -- a crash before work is submitted, a plan with no conversation state at
all, a truncated append, a session that finished, and a plan directory that
moved.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import conversations, harnesses, plans_files, workspaces

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class ConversationTestBase(unittest.TestCase):
    """Shared fixture: one plan directory and its plan-owned state directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans_files.canonicalize_path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.plan_dir.mkdir()
        self.plan_state = self.tmp_path / "state" / "plan-hash"
        self.ledger_path = conversations.conversation_ledger_path(self.plan_state)

    def workspace(self, digest: str = DIGEST_A) -> workspaces.WorkspaceBinding:
        return workspaces.WorkspaceBinding(
            workspace_root=str(self.tmp_path), registry_digest=digest
        )

    def binding(
        self,
        *,
        session_id: str = "1",
        harness: str = "claude",
        plan_dir: Path | None = None,
        digest: str = DIGEST_A,
        access_profile: str = "worker",
        scope_aliases: tuple[str, ...] = (),
    ) -> conversations.ConversationBinding:
        return conversations.ConversationBinding(
            plan_dir=str(self.plan_dir if plan_dir is None else plan_dir),
            session_id=session_id,
            harness=harness,
            workspace=self.workspace(digest),
            access_profile=access_profile,
            scope_aliases=scope_aliases,
        )

    def reference(
        self, *, native_id: str = "conv-1", **kwargs: Any
    ) -> conversations.ConversationReference:
        return conversations.ConversationReference(
            binding=self.binding(**kwargs), native_id=native_id
        )

    def live_conversation(
        self, *, native_id: str = "conv-1", **kwargs: Any
    ) -> conversations.ConversationReference:
        """Open *and* confirm a conversation, the only state that resumes."""
        reference = self.reference(native_id=native_id, **kwargs)
        conversations.open_conversation(self.plan_state, reference)
        conversations.confirm_conversation(self.plan_state, reference)
        return reference

    def ledger_lines(self) -> list[str]:
        return self.ledger_path.read_text(encoding="utf-8").splitlines()
