# mypy: ignore-errors
# ruff: noqa
"""Turn-level conversation continuity: what a turn resumes, and what it refuses.

``tests/test_asl_conversations.py`` covers the durable ledger in isolation --
what a record is, how it is appended, what a lookup may answer. These tests
cover the layer above it: the policy that turns a pending session plus a harness
into exactly one of continue / open / stop, and the wiring that carries that
decision into the adapter and back out into the ledger.

Two properties are load-bearing enough to be tested from more than one angle.
The first is that the intended conversation is restored -- keyed by session and
harness, so distinct plan sessions and generated repair children never reach
each other's. The second is that ``--fresh-conversation`` authorizes exactly one
fresh start: not the next turn, not the next session, not a supervisor retry,
and not a ``--resume`` that rehydrates flags from disk. The leak surfaces have
one test each, because each covers a replay the others cannot.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    conversation_capabilities,
    conversation_resume,
    conversations,
    events,
    harnesses,
    pause_policy,
    plans_files,
    preflight_runtime_hints,
    run_entry_resume,
    run_flags,
    turn_execution_executor,
    workbench_recovery_models,
    workspaces,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_autopilot() -> types.ModuleType:
    """Import the supervisor helper, which ships as a script rather than a module."""
    module_path = REPO_ROOT / "skills" / "asl" / "scripts" / "asl_autopilot.py"
    spec = importlib.util.spec_from_file_location(
        "asl_autopilot_continuity_containment", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def harness_result(**overrides: object) -> harnesses.HarnessResult:
    """A finished, successful turn, unless a field is overridden."""
    fields: dict[str, object] = {
        "argv": ["codex"],
        "returncode": 0,
        "stdout": "",
        "stderr": "",
    }
    fields.update(overrides)
    return harnesses.HarnessResult(**fields)


class ContinuationTestBase(unittest.TestCase):
    """One plan directory and the plan-owned state directory beside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans_files.canonicalize_path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.plan_dir.mkdir()
        self.cwd = self.tmp_path / "repo"
        self.cwd.mkdir()
        self.plan_state = self.tmp_path / "state" / "plan-hash"
        self.ledger_path = conversations.conversation_ledger_path(self.plan_state)

    def binding(
        self,
        *,
        session_id: str = "7",
        harness: str = "codex",
        cwd: Path | None = None,
        access_profile: str = harnesses.WORKER_ACCESS.name,
        scope_aliases: tuple[str, ...] = (),
    ) -> conversations.ConversationBinding:
        return conversation_resume.turn_binding(
            plan_dir=self.plan_dir,
            session_id=session_id,
            harness=harness,
            cwd=self.cwd if cwd is None else cwd,
            access_profile=access_profile,
            scope_aliases=scope_aliases,
        )

    def reference(
        self, native_id: str, **kwargs: object
    ) -> conversations.ConversationReference:
        return conversations.ConversationReference(
            binding=self.binding(**kwargs),
            native_id=native_id,
        )

    def confirmed(
        self, native_id: str, **kwargs: object
    ) -> conversations.ConversationReference:
        """Record a conversation that a turn has already submitted work on."""
        reference = self.reference(native_id, **kwargs)
        conversations.open_conversation(self.plan_state, reference)
        conversations.confirm_conversation(self.plan_state, reference)
        return reference

    def granted(self) -> conversation_resume.FreshConversationAuthorization:
        return conversation_resume.FreshConversationAuthorization(True)

    def withheld(self) -> conversation_resume.FreshConversationAuthorization:
        return conversation_resume.FreshConversationAuthorization(False)
