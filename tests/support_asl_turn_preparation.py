"""Shared fixtures for test_asl_turn_preparation."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans_models,
    plans_payloads,
    plans_snapshot,
    qna,
    turn_preparation,
)

from .support import write_plan

RUN_ID = "run-preparation-1"


#: The six entry points the run loop is allowed to reach here. A seventh would
#: be work the loop kept that this boundary was supposed to take.
ENTRY_POINTS = (
    "turn_preparation.classify_session",
    "turn_preparation.prepare_review_session",
    "turn_preparation.collect_packet_inputs",
    "turn_preparation.prepare_turn_continuation",
    "turn_preparation.resume_repair_session",
    "turn_preparation.prepare_turn_prompt",
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real — the boundary takes it around the preflight record and a
    fake that did not would hide a missing hold rather than expose it — while
    events and state writes are kept in order so a test can assert on what was
    recorded without reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))


class FakeVerificationContext:
    """The verification context, reduced to the one member preparation reads."""

    def __init__(self) -> None:
        self.workspace = None


class FakeRunContext:
    """A :class:`turn_preparation.RunContext` built by hand rather than by a run.

    Only the members the four preparation steps reach are populated, which is
    what the Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {}
        self.persistence = RecordingPersistence()
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.verification_context = FakeVerificationContext()
        self.final_snapshots: list[plans_models.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None:
        self.final_snapshots.append(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class FakeReviewResume:
    """The repair continuation's projection, reduced to what the gate reads."""

    def __init__(self, *, action: str, eligible: bool, blocker: str | None) -> None:
        self.action = action
        self.eligible = eligible
        self.blocker = blocker

    def to_payload(self) -> dict[str, Any]:
        return {"action": self.action, "eligible": self.eligible}


class PreparationTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments a step needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)

    def snapshot(
        self, statuses: tuple[str, ...] = ("pending",)
    ) -> plans_models.PlanSnapshot:
        write_plan(self.plan_dir, statuses)
        return plans_snapshot.inspect_plan(self.plan_dir)

    def args(self, **overrides: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "state_dir": None,
            "max_repair_group_attempts": 2,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def classification(
        self,
        *,
        review: bool = False,
        repair: bool = False,
    ) -> turn_preparation.SessionClassification:
        return turn_preparation.SessionClassification(
            is_review_session=review,
            is_repair_session=repair,
            session_kind=(
                plans_payloads.SESSION_KIND_REVIEW
                if review
                else plans_payloads.SESSION_KIND_IMPLEMENTATION
            ),
        )

    def qna_context(self) -> qna.QnAContext:
        return qna.QnAContext(
            plan_dir=self.plan_dir,
            state_dir=self.run_dir,
            state_path=self.run_dir / "state.json",
            run_id=RUN_ID,
            run_dir=self.run_dir,
            pause_reason="question",
            pause_message="ask",
            pause_details={},
            harness="codex",
            model=None,
            model_source=None,
            effort=None,
            effort_source=None,
            session_id="1",
            next_pending_id="1",
            turn_index=3,
            artifact_dir=self.run_dir,
            stdout_path=None,
            stderr_path=None,
            last_message_path=self.run_dir / "last-message.txt",
            result_path=None,
            last_message="a question",
        )
