# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401, F403, F405, F822, I001
"""One path for a launch, a reply, and a paused resume, whoever asked.

Session 6's claim is that the local Workbench and the connector now *do the
same thing*: "local and connector-facing operations have matching behavior,
refuse stale targets, and cannot spawn or answer twice after a lost response."
Four properties add up to that, and each has its own class here.

**The interpretation is portable and it is one interpretation.**
:class:`PortableInterpretationTests` holds :mod:`asl_workbench.actions`, which
is where the three claims a mutation can make are finally spelled apart: a
process was created, the plan left the paused state, a worker consumed the
reply. Only the third is about the operator's answer. This module reads bytes
and returns what they establish, so the Pi's reading of an imported log and the
Mac's reading of a live one cannot diverge.

**A reply is settled by evidence, not by a side effect.**
:class:`ReconciliationTests` holds the consequence: a launch is ``consumed``
when a process exists, because a process is the whole of what a launch
promised, and a reply-bearing mutation stops at ``accepted`` until a run
records having read it. The old rule -- resume, then watch for the pause to
lift -- is refused by name there, because the plan leaving the paused state is
a fact about the plan.

**Both surfaces are the same code.** :class:`SharedActionTests` drives
:mod:`agent_session_loop.workbench_actions` directly, and
:class:`SurfaceParityTests` proves the HTTP route reaches the same journal: a
retry that arrives at the other surface replays rather than spawning again.

**A run outlives its launcher.** :class:`LaunchIsolationTests` holds the one
line that keeps a Ctrl-C in the operator's terminal, or a connector going away,
from taking down the run it started.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    intervention,
    mailbox,
    mailbox_appenders,
    mailbox_vocabulary,
    plans_files,
    state,
    workbench_actions,
    workbench_launch,
    workbench_operations,
    workbench_registry_models,
)
from asl_workbench import actions, operations
from tests.support import (
    _FakePopen,
    _http_request,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)
from tests.workbench_support import WorkbenchAPIFixture

RUN_ID = "20260619T140000Z-1"
RESUMED_RUN_ID = "20260619T150000Z-2"
QUESTION_BODY = "Which column should v1 carry?"
REPLY_TEXT = "carry the v1 column"

#: A record older than the dispatch it is offered against answers something
#: else, so anything written for a live journal has to be stamped *now*: the
#: durable reconciler attributes by wall clock, not by a byte mark.
STALE_MOMENT = "2026-06-19T12:00:00+00:00"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _reply_line(
    *,
    run_id: str = RESUMED_RUN_ID,
    origin: str = actions.REPLY_ORIGIN_STDIN,
    question_id: str | None = None,
    created_at: str | None = None,
) -> bytes:
    """One ``reply_provided`` record, shaped the way ``run_loop`` writes it."""
    if created_at is None:
        created_at = _now_iso()
    source: dict[str, Any] = {
        "run_id": run_id,
        "session_id": "1",
        "turn_index": 2,
        "reply_length": len(REPLY_TEXT),
        "origin": origin,
    }
    if question_id is not None:
        source["question_id"] = question_id
    record = {
        "created_at": created_at,
        "name": actions.REPLY_PROVIDED_EVENT,
        "payload": {
            "run_id": run_id,
            "turn_index": 2,
            "session_id": "1",
            "reply_length": len(REPLY_TEXT),
            "source": source,
        },
        "version": 1,
    }
    return json.dumps(record).encode("utf-8") + b"\n"


class _PlanFixture(unittest.TestCase):
    """A plan, a state root, and the two shapes a reply can be aimed at."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state_dir = root / "state"
        self.plan_dir = root / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans_files.canonicalize_path(self.plan_dir)

    def rows(self) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            self.canonical, state_dir=self.state_dir
        )

    def row(self, operation_id: str) -> dict[str, Any]:
        record = workbench_operations.read_operation(
            self.canonical, operation_id, state_dir=self.state_dir
        )
        assert record is not None
        return record

    def launch_result(self, *, reply: bool = False) -> workbench_launch.LaunchResult:
        return workbench_launch.LaunchResult(
            pid=4242,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=len(REPLY_TEXT) if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def make_paused(self) -> None:
        runs = state.plan_state_dir(self.canonical, self.state_dir) / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        last_message = runs / "last-message.txt"
        last_message.write_text(QUESTION_BODY, encoding="utf-8")
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "run_id": RUN_ID,
                "harness": "claude",
                "turns": [
                    {
                        "session_id": "1",
                        "turn_index": 1,
                        "last_message_path": str(last_message),
                        "harness": "claude",
                    }
                ],
            },
        )

    def make_waiting(self) -> str:
        """A live run holding one open mailbox question."""
        _write_lock_file(
            self.canonical,
            self.state_dir,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        question = mailbox_appenders.append_question(
            self.canonical,
            QUESTION_BODY,
            state_dir=self.state_dir,
            run_id=RUN_ID,
            session_id="2",
            turn_index=3,
        )
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "ok": None,
                "paused": False,
                "run_id": RUN_ID,
                "harness": "claude",
                "active_turn": {"session_id": "2", "turn_index": 3},
                "waiting_for_reply": {"question_id": question.id},
            },
        )
        return question.id

    def append_reply_provided(self, **kwargs: Any) -> None:
        path = workbench_operations.events_path_for_plan(self.canonical, self.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(_reply_line(**kwargs))


__all__ = [name for name in globals() if not name.startswith("__")]
