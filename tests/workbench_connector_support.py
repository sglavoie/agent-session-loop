"""Shared fixtures for the workbench connector test modules.

Split from tests/test_workbench_connector.py."""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import (
    events,
    plans_files,
    state,
    workbench_connector,
)
from asl_workbench import catalog, history, observation, store_workbench
from asl_workbench import connector_protocols as portable_protocols
from asl_workbench import connector_runtime as portable_runtime
from asl_workbench import connector_support as portable_support
from asl_workbench.transport import (
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_SNAPSHOT,
)
from tests.support import _write_lock_file, _write_state_payload, write_plan

NOW = 1789574400.0

#: Loose enough that nothing here has to advance a clock by a week.
POLICY = observation.ObservationPolicy(
    observation_stale_seconds=60.0,
    lost_observation_alert_seconds=120.0,
    unanswered_question_alert_seconds=120.0,
    summary_retention_days=7.0,
)

CONNECTOR_ID = "mac-studio"
TARGET_ID = "pi-1"

#: A pid this machine will not have. Used for a lock nobody owns.
DEAD_PID = 4_242_424


def iso(offset: float) -> str:
    """An ASL timestamp ``offset`` seconds from :data:`NOW`.

    Every corpus fixture is dated relative to the observation moment, because
    history keeps a seven-day window: a fixture with a fixed calendar date
    silently empties every batch once it ages past the retention bound.
    """
    return datetime.fromtimestamp(NOW + offset, tz=UTC).isoformat()


def wire_row(**overrides: Any) -> dict[str, Any]:
    """A run row carrying every field the run shape requires, and no more."""
    row: dict[str, Any] = {
        "run_key": "a" * 16,
        "plan_key": "b" * 16,
        "project": "alpha",
        "run_id": "run-1",
        "primary_state": "running",
        "phase": "running",
        "urgency_group": "in-progress",
        "actions": [],
        "last_activity_at": NOW - 30.0,
    }
    row.update(overrides)
    return row


# -- Doubles ----------------------------------------------------------------


class FakeChannel:
    """Records what was sent, and fails when told to.

    ``ChannelError`` and nothing else, because that is the contract the runtime
    classifies against: a channel that leaked an ``OSError`` would make "the
    link blinked" indistinguishable from "this code has a bug".
    """

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.failing = False
        self.attempts = 0

    def send(self, frame: bytes) -> None:
        self.attempts += 1
        if self.failing:
            raise portable_support.ChannelError("the link is down")
        self.frames.append(json.loads(frame.decode("utf-8")))

    def views(self) -> list[str]:
        return [frame["payload"]["view"] for frame in self.frames]

    def payloads(self, view: str) -> list[dict[str, Any]]:
        return [
            frame["payload"]
            for frame in self.frames
            if frame["payload"]["view"] == view
        ]


class StubSource:
    """A source whose passes are handed to it, and which records its commits."""

    def __init__(self, cycles: list[Any] | None = None) -> None:
        self.cycles = list(cycles or ())
        self.committed: list[portable_protocols.ObservationCycle] = []
        self.observations = 0

    def observe(self, now: float) -> portable_protocols.ObservationCycle:
        self.observations += 1
        if not self.cycles:
            return portable_protocols.ObservationCycle(observed_at=now)
        nxt = self.cycles.pop(0) if len(self.cycles) > 1 else self.cycles[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def commit(self, cycle: portable_protocols.ObservationCycle) -> None:
        self.committed.append(cycle)


class RecordingSource:
    """A real source with its commits counted.

    Wrapping rather than subclassing, so the checkpoint assertions in
    :class:`CorpusDeliveryTests` are made against the real
    :class:`~agent_session_loop.workbench_connector.AslObservationSource`
    behaviour instead of a stand-in that only resembles it.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.committed: list[portable_protocols.ObservationCycle] = []

    def observe(self, now: float) -> portable_protocols.ObservationCycle:
        return self.inner.observe(now)

    def commit(self, cycle: portable_protocols.ObservationCycle) -> None:
        self.committed.append(cycle)
        self.inner.commit(cycle)


def inline(attempt: Any) -> None:
    """Run an observation pass on the calling thread.

    Every scheduling test uses this: the isolation property is proven once, by
    :class:`HeartbeatIsolationTests`, with an executor that never runs the pass
    at all. Everywhere else a thread would only add a join.
    """
    attempt()
    return None


class HeldWorker:
    """An executor that keeps passes pending until released."""

    def __init__(self) -> None:
        self.pending: list[Any] = []

    def __call__(self, attempt: Any) -> None:
        self.pending.append(attempt)
        return None

    def release(self) -> int:
        released = len(self.pending)
        for attempt in self.pending:
            attempt()
        self.pending.clear()
        return released


def summary_payload(plan_dir: str, **overrides: Any) -> dict[str, Any]:
    """A summary payload carrying every classified field and nothing else."""
    payload: dict[str, Any] = {name: None for name in catalog.PRIVATE_FIELDS}
    payload.update({name: None for name in catalog.CACHE_SAFE_FIELDS})
    payload.update(
        {
            "plan_dir": plan_dir,
            "exists": True,
            "primary_state": "ready",
            "recommended_action": "run",
            "urgency_rank": 80,
            "urgency_group": "ready",
            "activity_label": "Ready to run",
            "counts": {"pending": 1, "complete": 0, "empty": 0, "unknown": 0},
            "sources": ["state"],
            "repositories": [],
            "repository_count": 0,
            "warnings": [],
        }
    )
    payload.update(overrides)
    return payload


def build_page(
    count: int = 1,
    *,
    complete: bool = True,
    final: bool = True,
    warnings: tuple[str, ...] = (),
    **entry_overrides: Any,
) -> catalog.CatalogPage:
    """One page of a real catalog built from ``count`` synthetic plans."""
    records = [
        catalog.record_from_summary(
            summary_payload(f"/tmp/agent-plans/plan-{index}", **entry_overrides)
        )
        for index in range(count)
    ]
    built = catalog.build_catalog(records, warnings=warnings, complete=complete)
    page = built.page(offset=0, limit=max(1, count))
    if final:
        return page
    return catalog.CatalogPage(
        entries=page.entries,
        offset=page.offset,
        limit=page.limit,
        total=page.total + 1,
        generation=page.generation,
        complete=page.complete,
        final=False,
    )


def history_entry(entry_id: str = "entry-1", **overrides: Any) -> history.HistoryEntry:
    fields: dict[str, Any] = {
        "entry_id": entry_id,
        "source_key": "source-a",
        "plan_key": "0123456789abcdef",
        "run_key": "fedcba9876543210",
        "outcome": history.HISTORY_OUTCOME_COMPLETED,
        "recorded_at": NOW - 60.0,
        "event_schema_version": 2,
        "project": "alpha",
        "run_id": "run-alpha",
        "harness": "codex",
    }
    fields.update(overrides)
    return history.HistoryEntry(**fields)


def history_batch(
    entries: tuple[history.HistoryEntry, ...] = (),
    **overrides: Any,
) -> history.HistoryBatch:
    fields: dict[str, Any] = {
        "batch_id": "batch-1",
        "index": 0,
        "generated_at": NOW,
        "window_start": NOW - 600.0,
        "window_end": NOW,
        "complete": True,
        "entries": entries,
    }
    fields.update(overrides)
    return history.HistoryBatch(**fields)


def cycle_with(
    *,
    runs: tuple[dict[str, Any], ...] = (),
    pages: int = 1,
    batches: int = 1,
    observed_at: float = NOW,
    **overrides: Any,
) -> portable_protocols.ObservationCycle:
    """A cycle carrying real pages and batches, sized by the caller."""
    catalog_pages = tuple(
        build_page(1, final=index == pages - 1) for index in range(pages)
    )
    history_batches = tuple(
        history_batch((history_entry(f"entry-{index}"),), batch_id=f"batch-{index}")
        for index in range(batches)
    )
    fields: dict[str, Any] = {
        "observed_at": observed_at,
        "runs": runs,
        "catalog_pages": catalog_pages,
        "history_batches": history_batches,
        "history": "checkpoint-token",
        "source_digest": catalog_pages[0].generation if catalog_pages else None,
    }
    fields.update(overrides)
    return portable_protocols.ObservationCycle(**fields)


def make_connector(
    source: Any,
    channel: Any,
    **overrides: Any,
) -> portable_runtime.ObservationConnector:
    fields: dict[str, Any] = {
        "connector_id": CONNECTOR_ID,
        "target_id": TARGET_ID,
        "source": source,
        "channel": channel,
        "heartbeat_interval": 15.0,
        "observation_interval": 30.0,
        "run_observation": inline,
    }
    fields.update(overrides)
    return portable_runtime.ObservationConnector(**fields)


def drain(
    connector: portable_runtime.ObservationConnector,
    *,
    start: float = NOW,
    steps: int = 12,
    tick: float = 1.0,
) -> list[str]:
    """Step a connector ``steps`` times and report the views it sent."""
    sent: list[str] = []
    moment = start
    for _ in range(steps):
        view = connector.step(moment)
        if view is not None:
            sent.append(view)
        moment += tick
    return sent


def ingest(
    db: store_workbench.WorkbenchStore, frame: dict[str, Any], *, at: float
) -> Any:
    """Apply one published frame through the store's real ingestion path."""
    view = frame["payload"]["view"]
    if view == VIEW_HEARTBEAT:
        return db.ingest_heartbeat(frame, received_at=at)
    if view == VIEW_SNAPSHOT:
        return db.ingest_snapshot(frame, received_at=at)
    if view == VIEW_CATALOG_PAGE:
        return db.ingest_catalog(frame, received_at=at)
    return db.ingest_history(frame, received_at=at)


def tree_digest(root: Path) -> dict[str, str]:
    """Every file under ``root``, by relative path, hashed."""
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


class CorpusTestCase(unittest.TestCase):
    """A temporary ASL corpus: two plans, one live run, one finished run."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.state_root = self.root / "state"
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()

        self.paused = self.make_plan(
            "paused-plan",
            "# Paused plan\n",
            ("pending",),
            {
                "run_id": "run-paused",
                "run_started_at": iso(-7200),
                "last_activity_at": iso(-1800),
                "harness": "claude",
                "current_session": "1",
                "sessions": {"1": {"status": "pending"}},
                "paused": True,
                "pause_reason": "blocked",
            },
        )
        self.finished = self.make_plan(
            "finished-plan",
            "# Finished plan\n",
            ("complete",),
            {
                "run_id": "run-finished",
                "run_started_at": iso(-5400),
                "last_activity_at": iso(-3600),
                "run_finished_at": iso(-3600),
                "finished": True,
                "ok": True,
                "harness": "codex",
                "sessions": {"1": {"status": "complete"}},
            },
        )
        self.write_history(
            self.finished,
            run_id="run-finished",
            started=iso(-5400),
            finished=iso(-3600),
        )
        # A lock nobody owns. Observation must neither reclaim nor rewrite it.
        _write_lock_file(self.paused, self.state_root, pid=DEAD_PID)

    def make_plan(
        self,
        name: str,
        title: str,
        statuses: tuple[str, ...],
        payload: dict[str, Any],
    ) -> Path:
        plan_dir = self.plans_root / name
        write_plan(plan_dir, statuses)
        (plan_dir / "manifest.md").write_text(title, encoding="utf-8")
        canonical = plans_files.canonicalize_path(plan_dir)
        _write_state_payload(
            canonical, self.state_root, dict(payload, plan_dir=str(canonical))
        )
        return canonical

    def write_history(
        self, plan_dir: Path, *, run_id: str, started: str, finished: str
    ) -> None:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "created_at": started,
                "name": events.RUN_STARTED_EVENT,
                "version": 2,
                "payload": {
                    "run_id": run_id,
                    "run_started_at": started,
                    "plan_dir": str(plan_dir),
                    "harness": "codex",
                    "model": "opus",
                },
            },
            {
                "created_at": finished,
                "name": events.RUN_FINISHED_EVENT,
                "version": 2,
                "payload": {
                    "run_id": run_id,
                    "ok": True,
                    "paused": False,
                    "finished": True,
                    "reason": "all-complete",
                    "run_started_at": started,
                    "run_finished_at": finished,
                    "plan_dir": str(plan_dir),
                },
            },
        ]
        path = plan_state / events.EVENTS_FILE_NAME
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def make_source(self, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "state_dir": self.state_root,
            "plan_roots": (str(self.plans_root),),
            "include_default_dirs": False,
        }
        fields.update(overrides)
        return workbench_connector.AslObservationSource(**fields)

    def make_recording_source(self, **overrides: Any) -> RecordingSource:
        return RecordingSource(self.make_source(**overrides))

    def open_store(
        self, name: str = "workbench.sqlite3"
    ) -> store_workbench.WorkbenchStore:
        db = store_workbench.WorkbenchStore.open(
            self.root / name, policy=POLICY, created_at=NOW
        )
        self.addCleanup(db.close)
        return db

    def publish(
        self,
        db: store_workbench.WorkbenchStore,
        *,
        source: Any | None = None,
        connector: Any | None = None,
        start: float = NOW,
        steps: int = 16,
    ) -> tuple[Any, FakeChannel, list[Any]]:
        """Run one connector against the corpus and ingest everything it sends."""
        channel = FakeChannel()
        resolved = connector or make_connector(source or self.make_source(), channel)
        drain(resolved, start=start, steps=steps)
        results = [ingest(db, frame, at=start) for frame in channel.frames]
        return resolved, channel, results
