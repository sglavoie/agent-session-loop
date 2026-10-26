"""The shared history service: interpretation, incremental reads, and honesty.

``asl_workbench.history`` holds what a run's ending means;
``agent_session_loop.workbench_history`` holds the reading of the files it means
it about. The tests here are grouped by the claim they hold rather than by the
module they touch, because every claim worth making spans both halves:

* the vocabularies match ASL's live constants in both directions, so a reason
  added there fails here rather than arriving on the Pi as an unknown string;
* an outcome comes from a terminal record and never from an absence;
* attribution is by run id and never by recency;
* a file that was replaced, truncated, or rewritten in place is re-read, and a
  record still being appended is never half-consumed;
* nothing is consumed until it has been delivered; and
* the local view and the publication batches are the same entries, so the two
  cockpits cannot disagree about what a run did.
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_session_loop import (
    events,
    review,
    run_coordination_constants,
    workbench_history,
)
from asl_workbench import catalog, history

_T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
_NOW = (_T0 + timedelta(hours=1)).timestamp()
_PLAN = "/Users/someone/plans/alpha"
_OTHER_PLAN = "/Users/someone/plans/beta"


def _at(seconds: float) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat()


def _record(name: str, payload: dict[str, object], *, at: float, version: int = 2):
    return {
        "created_at": _at(at),
        "name": name,
        "payload": payload,
        "version": version,
    }


def _started(
    run_id: str,
    *,
    at: float,
    plan_dir: str | None = _PLAN,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "run_started_at": _at(at),
        "harness": "claude",
        "model": "opus",
        **extra,
    }
    if plan_dir is not None:
        payload["plan_dir"] = plan_dir
    return _record(events.RUN_STARTED_EVENT, payload, at=at)


def _finished(
    run_id: str | None,
    *,
    at: float,
    ok: bool = True,
    paused: bool = False,
    reason: str = "all-complete",
    finished: bool = True,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": ok,
        "paused": paused,
        "reason": reason,
        "turn_count": 3,
        "counts": {"complete": 2, "pending": 0},
        **extra,
    }
    if run_id is not None:
        payload["run_id"] = run_id
    if finished:
        payload.setdefault("run_finished_at", _at(at))
    return _record(events.RUN_FINISHED_EVENT, payload, at=at)


def _wait_started(
    question_id: str,
    *,
    run_id: str | None,
    at: float,
    session_id: str = "1",
) -> dict[str, object]:
    return _record(
        events.MAILBOX_EVENT,
        {
            "run_id": run_id,
            "question_id": question_id,
            "session_id": session_id,
            "turn_index": 1,
            "started_at": _at(at),
            "pending_outbox": 0,
            "action": events.MAILBOX_ACTION_WAIT_STARTED,
        },
        at=at,
    )


def _wait_cleared(
    question_id: str,
    *,
    run_id: str | None,
    at: float,
    reason: str = run_coordination_constants.WAIT_REASON_REPLY,
    started_at: float | None = None,
    session_id: str = "1",
) -> dict[str, object]:
    return _record(
        events.MAILBOX_EVENT,
        {
            "run_id": run_id,
            "question_id": question_id,
            "session_id": session_id,
            "turn_index": 1,
            "started_at": _at(started_at if started_at is not None else at),
            "reason": reason,
            "action": events.MAILBOX_ACTION_WAIT_CLEARED,
        },
        at=at,
    )


class HistoryTestCase(unittest.TestCase):
    """A temporary ASL state root with one or more plan state directories."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "state"
        self.root.mkdir(parents=True)
        self.store = workbench_history.CheckpointStore(
            Path(self._temporary.name) / "history.json"
        )

    def source_dir(self, name: str = "plan-a") -> Path:
        # A real plan state directory is named for its plan's path digest;
        # the state-root scan skips anything else, so the label is hashed.
        directory = self.root / hashlib.sha256(name.encode()).hexdigest()
        directory.mkdir(exist_ok=True)
        return directory

    def write_events(self, records, *, name: str = "plan-a") -> Path:
        directory = self.source_dir(name)
        body = "\n".join(json.dumps(record) for record in records)
        if body:
            body += "\n"
        path = directory / history.EVENTS_FILE_NAME
        path.write_text(body, encoding="utf-8")
        return path

    def append_events(self, records, *, name: str = "plan-a") -> None:
        path = self.source_dir(name) / history.EVENTS_FILE_NAME
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def write_state(self, document: object, *, name: str = "plan-a") -> None:
        path = self.source_dir(name) / history.STATE_FILE_NAME
        path.write_text(json.dumps(document), encoding="utf-8")

    def run_import(self, *, now: float = _NOW, **kwargs) -> history.ImportResult:
        return workbench_history.import_window(
            now=now, store=self.store, state_dir=self.root, **kwargs
        )


# -- Vocabularies -----------------------------------------------------------


__all__ = [
    "json",
    "os",
    "unittest",
    "UTC",
    "datetime",
    "timedelta",
    "Path",
    "TemporaryDirectory",
    "events",
    "review",
    "run_coordination_constants",
    "workbench_history",
    "catalog",
    "history",
    "_T0",
    "_NOW",
    "_PLAN",
    "_OTHER_PLAN",
    "_at",
    "_record",
    "_started",
    "_finished",
    "_wait_started",
    "_wait_cleared",
    "HistoryTestCase",
]
