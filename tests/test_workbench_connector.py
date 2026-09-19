"""``asl workbench connect``: what leaves this Mac, when, and exactly once.

Session 11 replaces the Companion's connector -- which launched ``asl bridge``
as a child process and re-derived observations from its frames -- with a command
that calls the shared catalog, run, and history services in process. The tests
are grouped by the claim they hold rather than by the function they call,
because every claim here is a property of the whole path rather than of one
mapper:

* **a real ASL corpus reaches the store accurately.** :class:`CorpusDeliveryTests`
  builds a plan tree, publishes it through the connector, and ingests every
  frame into a real :class:`asl_workbench.store.WorkbenchStore`. The rows, the
  catalog entries, and the endings are then read back out of the store and
  compared against the corpus -- not against the payloads the connector built,
  which would only prove the connector agrees with itself;
* **a reconnect repeats rather than duplicates.** The same corpus published
  twice, and published again across a dropped link, leaves one row per run, one
  entry per plan, and one history entry per ending;
* **observation cannot modify what it observes.** The whole state tree is
  hashed before and after repeated passes, including a live lock and a stale
  one, and a byte that changed is a failure;
* **a checkpoint moves only after delivery.** The commit is asserted against
  the source, not against the store: a cycle whose delivery failed part way
  through, and a cycle interrupted by shutdown, must leave the window
  uncommitted so the next pass re-reads it;
* **retry is bounded and deterministic**, and a recovered link announces itself
  as a new generation, because the store's ordering rule needs to tell a
  restarted source from a replay;
* **one connector per state directory**, enforced by a pid file that is stolen
  from a dead owner and never from a live one; and
* **heartbeats do not wait for observation.** The observation pass runs on an
  injected executor, so a pass that never finishes is held open while the clock
  advances and the heartbeats are counted.

The last group is the privacy gate: every frame the connector builds is
searched for this machine's paths, for operator-typed labels, and for question
prose. A remote service may hold the whole catalog and must not learn the
layout of the disk it describes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import signal
import threading
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from agent_session_loop import (
    bridge,
    events,
    intervention,
    plans,
    state,
    workbench_connector,
    workbench_history,
)
from asl_workbench import catalog, history, observation, operations, store
from asl_workbench import connector as portable
from asl_workbench.transport import (
    CATALOG_ENTRY_SHAPE,
    MAX_CATALOG_ENTRIES_PER_PAGE,
    MAX_RUNS_PER_SNAPSHOT,
    MAX_WARNING_CHARS,
    RUN_SHAPE,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
    TransportError,
    observation_envelope,
)
from tests.support import _write_lock_file, _write_state_payload, write_plan

#: A fixed moment, so every digest and every window in this module is stable.
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
            raise portable.ChannelError("the link is down")
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
        self.committed: list[portable.ObservationCycle] = []
        self.observations = 0

    def observe(self, now: float) -> portable.ObservationCycle:
        self.observations += 1
        if not self.cycles:
            return portable.ObservationCycle(observed_at=now)
        nxt = self.cycles.pop(0) if len(self.cycles) > 1 else self.cycles[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def commit(self, cycle: portable.ObservationCycle) -> None:
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
        self.committed: list[portable.ObservationCycle] = []

    def observe(self, now: float) -> portable.ObservationCycle:
        return self.inner.observe(now)

    def commit(self, cycle: portable.ObservationCycle) -> None:
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
) -> portable.ObservationCycle:
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
    return portable.ObservationCycle(**fields)


def make_connector(
    source: Any,
    channel: Any,
    **overrides: Any,
) -> portable.ObservationConnector:
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
    return portable.ObservationConnector(**fields)


def drain(
    connector: portable.ObservationConnector,
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


def ingest(db: store.WorkbenchStore, frame: dict[str, Any], *, at: float) -> Any:
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


# -- Bounded retry ----------------------------------------------------------


class BackoffTests(unittest.TestCase):
    """Retry is bounded, deterministic, and assertable."""

    def test_the_delay_doubles_to_the_ceiling_and_stays_there(self) -> None:
        backoff = portable.Backoff(base=1.0, cap=8.0)
        self.assertEqual(
            [1.0, 2.0, 4.0, 8.0, 8.0, 8.0],
            [backoff.delay() for _ in range(6)],
        )

    def test_a_success_forgets_the_failures(self) -> None:
        backoff = portable.Backoff(base=1.0, cap=60.0)
        backoff.delay()
        backoff.delay()
        backoff.reset()
        self.assertEqual(1.0, backoff.delay())

    def test_a_very_long_outage_does_not_overflow_the_doubling(self) -> None:
        backoff = portable.Backoff(base=1.0, cap=60.0)
        for _ in range(2_000):
            self.assertLessEqual(backoff.delay(), 60.0)

    def test_a_backoff_needs_a_positive_base_and_ceiling(self) -> None:
        for kwargs in ({"base": 0.0}, {"cap": 0.0}, {"base": -1.0}):
            with self.assertRaises(portable.ConnectorError):
                portable.Backoff(**kwargs)

    def test_the_defaults_are_the_documented_schedule(self) -> None:
        # The docs and the README both quote 1s to a 60s cap.
        self.assertEqual(1.0, portable.BACKOFF_BASE_SECONDS)
        self.assertEqual(60.0, portable.BACKOFF_CAP_SECONDS)


class DeliveryFailureTests(unittest.TestCase):
    """What a failed delivery does to the schedule, the state, and the cycle."""

    def setUp(self) -> None:
        self.channel = FakeChannel()
        self.source = StubSource([cycle_with()])
        self.connector = make_connector(
            self.source, self.channel, backoff=portable.Backoff(base=2.0, cap=8.0)
        )

    def test_a_failure_enters_backoff_and_sends_nothing_until_it_elapses(self) -> None:
        self.channel.failing = True
        self.assertIsNone(self.connector.step(NOW))
        self.assertEqual(portable.STATE_BACKOFF, self.connector.state)
        self.assertEqual(NOW + 2.0, self.connector.retry_at)

        self.channel.failing = False
        attempts = self.channel.attempts
        self.assertIsNone(self.connector.step(NOW + 1.0))
        self.assertEqual(
            attempts,
            self.channel.attempts,
            "a connector in backoff attempted a delivery before its delay elapsed",
        )
        self.assertEqual(VIEW_HEARTBEAT, self.connector.step(NOW + 2.0))

    def test_the_delays_grow_across_consecutive_failures(self) -> None:
        self.channel.failing = True
        self.connector.step(NOW)
        self.assertEqual(NOW + 2.0, self.connector.retry_at)
        self.connector.step(NOW + 2.0)
        self.assertEqual(NOW + 2.0 + 4.0, self.connector.retry_at)
        self.connector.step(NOW + 6.0)
        self.assertEqual(NOW + 6.0 + 8.0, self.connector.retry_at)

    def test_a_recovered_link_is_announced_as_a_new_generation(self) -> None:
        # The store's ordering rule is "time decides, and the generation only
        # refuses what time cannot". A reconnect has to move the generation or
        # the receiver cannot tell a restarted source from a replay.
        before = self.connector.generation
        self.channel.failing = True
        self.connector.step(NOW)
        self.channel.failing = False
        self.connector.step(NOW + 2.0)
        self.assertEqual(before + 1, self.connector.generation)
        self.assertEqual(portable.STATE_READY, self.connector.state)

    def test_an_uninterrupted_link_never_moves_the_generation(self) -> None:
        before = self.connector.generation
        drain(self.connector)
        self.assertEqual(before, self.connector.generation)

    def test_a_link_failure_is_not_reported_as_a_degraded_projection(self) -> None:
        # ``degraded_reason`` travels on a heartbeat, and a heartbeat about a
        # dead link can only be sent once the link is alive again.
        self.channel.failing = True
        self.connector.step(NOW)
        self.assertIsNone(self.connector.degraded_reason)


# -- Publication order and checkpoint timing --------------------------------


class PublicationOrderTests(unittest.TestCase):
    """One frame per step, liveness first, then the corpus."""

    def setUp(self) -> None:
        self.channel = FakeChannel()
        self.source = StubSource([cycle_with(pages=3, batches=2)])
        self.connector = make_connector(self.source, self.channel)

    def test_a_pass_publishes_liveness_then_runs_then_catalog_then_history(
        self,
    ) -> None:
        self.assertEqual(
            [
                VIEW_HEARTBEAT,
                VIEW_SNAPSHOT,
                VIEW_CATALOG_PAGE,
                VIEW_CATALOG_PAGE,
                VIEW_CATALOG_PAGE,
                VIEW_HISTORY_BATCH,
                VIEW_HISTORY_BATCH,
            ],
            drain(self.connector, steps=8),
        )

    def test_one_step_sends_at_most_one_frame(self) -> None:
        # A recovered link must not turn a built-up queue into a burst.
        self.connector.step(NOW)
        self.assertEqual(1, len(self.channel.frames))

    def test_an_idle_connector_sends_nothing_between_heartbeats(self) -> None:
        drain(self.connector, steps=8)
        self.assertEqual(0, self.connector.pending())
        self.assertIsNone(self.connector.step(NOW + 8.0))

    def test_the_heartbeat_returns_on_its_own_interval(self) -> None:
        drain(self.connector, steps=8)
        self.assertEqual(VIEW_HEARTBEAT, self.connector.step(NOW + 15.0))

    def test_a_newer_heartbeat_replaces_an_undelivered_one(self) -> None:
        # Single slot, latest wins: the older heartbeat described a moment
        # nobody needs any more.
        self.channel.failing = True
        self.connector.step(NOW)
        self.connector.step(NOW + 30.0)
        self.channel.failing = False
        self.connector.step(NOW + 60.0)
        beats = self.channel.payloads(VIEW_HEARTBEAT)
        self.assertEqual(1, len(beats))
        self.assertEqual(NOW + 60.0, beats[0]["observed_at"])


class CheckpointTests(unittest.TestCase):
    """A checkpoint moves after delivery, and only after a whole cycle."""

    def setUp(self) -> None:
        self.channel = FakeChannel()
        self.source = StubSource([cycle_with(pages=2, batches=2)])
        self.connector = make_connector(self.source, self.channel)

    def test_nothing_is_committed_until_every_frame_of_the_cycle_has_landed(
        self,
    ) -> None:
        views: list[str] = []
        moment = NOW
        while len(views) < 6:
            view = self.connector.step(moment)
            if view is not None:
                views.append(view)
                if len(views) < 6:
                    self.assertEqual(
                        [],
                        self.source.committed,
                        f"committed after {views[-1]} with frames still queued",
                    )
            moment += 1.0
        self.assertEqual(1, len(self.source.committed))

    def test_a_failure_part_way_through_leaves_the_window_uncommitted(self) -> None:
        self.connector.step(NOW)  # heartbeat
        self.connector.step(NOW + 1.0)  # snapshot
        self.connector.step(NOW + 2.0)  # first catalog page
        self.channel.failing = True
        self.connector.step(NOW + 3.0)
        self.assertEqual([], self.source.committed)
        self.assertEqual(0, len(self.connector._queue.pages))
        self.assertEqual(0, len(self.connector._queue.batches))

    def test_a_shutdown_mid_cycle_commits_nothing(self) -> None:
        self.connector.step(NOW)
        self.connector.step(NOW + 1.0)
        self.connector.stop()
        self.assertEqual([], self.source.committed)
        self.assertEqual(portable.STATE_STOPPED, self.connector.state)

    def test_the_next_pass_re_reads_a_window_that_was_never_committed(self) -> None:
        self.channel.failing = True
        self.connector.step(NOW)
        self.channel.failing = False
        drain(self.connector, start=NOW + 40.0, steps=10)
        self.assertEqual(2, self.source.observations)
        self.assertEqual(1, len(self.source.committed))

    def test_a_cycle_with_no_pages_or_batches_commits_nothing_and_waits_for_nothing(
        self,
    ) -> None:
        source = StubSource([portable.ObservationCycle(observed_at=NOW)])
        connector = make_connector(source, FakeChannel())
        drain(connector, steps=4)
        self.assertEqual([], source.committed)
        self.assertEqual(0, connector.pending())

    def test_a_stopped_connector_publishes_nothing_further(self) -> None:
        self.connector.stop()
        self.assertIsNone(self.connector.step(NOW))
        self.assertEqual(0, len(self.channel.frames))

    def test_stopping_twice_is_the_same_as_stopping_once(self) -> None:
        self.connector.stop()
        self.connector.stop()
        self.assertEqual(portable.STATE_STOPPED, self.connector.state)


# -- Heartbeats versus slow observation -------------------------------------


class HeartbeatIsolationTests(unittest.TestCase):
    """A slow corpus delays the corpus, never the liveness signal."""

    def test_heartbeats_continue_while_an_observation_pass_is_in_flight(self) -> None:
        worker = HeldWorker()
        source = StubSource([cycle_with()])
        connector = make_connector(source, FakeChannel(), run_observation=worker)

        views = drain(connector, start=NOW, steps=4, tick=15.0)

        self.assertTrue(connector.observing)
        self.assertEqual([VIEW_HEARTBEAT] * 4, views)
        self.assertEqual(
            1,
            len(worker.pending),
            "a pass still in flight was re-entered on the next interval",
        )
        self.assertEqual(1, worker.release())
        # A moment before the next heartbeat falls due, so the frame that comes
        # out is the one the released pass produced.
        self.assertEqual(VIEW_SNAPSHOT, connector.step(NOW + 50.0))

    def test_the_first_heartbeat_does_not_wait_for_the_first_pass(self) -> None:
        worker = HeldWorker()
        connector = make_connector(
            StubSource([cycle_with()]), FakeChannel(), run_observation=worker
        )
        self.assertEqual(VIEW_HEARTBEAT, connector.step(NOW))

    def test_a_connector_with_no_projection_yet_claims_no_projection_age(self) -> None:
        channel = FakeChannel()
        connector = make_connector(
            StubSource([cycle_with()]), channel, run_observation=HeldWorker()
        )
        connector.step(NOW)
        beat = channel.payloads(VIEW_HEARTBEAT)[0]
        self.assertFalse(beat["bridge_alive"])
        self.assertNotIn("projection_age_seconds", beat)

    def test_a_delivered_pass_makes_the_heartbeat_report_a_live_projection(
        self,
    ) -> None:
        channel = FakeChannel()
        connector = make_connector(StubSource([cycle_with()]), channel)
        drain(connector, steps=6)
        connector.step(NOW + 15.0)
        beat = channel.payloads(VIEW_HEARTBEAT)[-1]
        self.assertTrue(beat["bridge_alive"])
        self.assertEqual(15.0, beat["projection_age_seconds"])

    def test_a_failed_pass_is_reported_as_alive_and_degraded(self) -> None:
        channel = FakeChannel()
        source = StubSource([OSError("the disk went away")])
        connector = make_connector(source, channel)
        connector.step(NOW)
        connector.step(NOW + 15.0)
        beat = channel.payloads(VIEW_HEARTBEAT)[-1]
        self.assertFalse(beat["bridge_alive"])
        self.assertIn("the disk went away", beat["degraded_reason"])

    def test_a_degraded_pass_publishes_its_reason_and_still_publishes_the_corpus(
        self,
    ) -> None:
        channel = FakeChannel()
        source = StubSource(
            [cycle_with(degraded_reason="catalog incomplete (2 plans)")]
        )
        connector = make_connector(source, channel)
        drain(connector, steps=6)
        connector.step(NOW + 15.0)
        beat = channel.payloads(VIEW_HEARTBEAT)[-1]
        self.assertEqual("catalog incomplete (2 plans)", beat["degraded_reason"])
        self.assertFalse(beat["bridge_alive"])
        self.assertEqual(1, len(channel.payloads(VIEW_CATALOG_PAGE)))

    def test_a_degraded_reason_is_bounded_to_what_the_transport_accepts(self) -> None:
        channel = FakeChannel()
        connector = make_connector(
            StubSource([cycle_with(degraded_reason="x" * 4_000)]), channel
        )
        drain(connector, steps=6)
        connector.step(NOW + 15.0)
        beat = channel.payloads(VIEW_HEARTBEAT)[-1]
        self.assertEqual(observation.MAX_LABEL_CHARS, len(beat["degraded_reason"]))

    def test_a_new_pass_is_dropped_while_the_previous_one_is_still_going_out(
        self,
    ) -> None:
        # One generation's pages stay together. The next pass reads a corpus at
        # least as current as the one that was dropped.
        channel = FakeChannel()
        first = cycle_with(pages=4, observed_at=NOW)
        second = cycle_with(pages=4, observed_at=NOW + 5.0)
        source = StubSource([first, second])
        connector = make_connector(
            source, channel, observation_interval=1.0, heartbeat_interval=1_000.0
        )
        drain(connector, steps=3, tick=1.0)
        self.assertGreaterEqual(source.observations, 2)
        pages = channel.payloads(VIEW_CATALOG_PAGE)
        self.assertTrue(pages)
        self.assertEqual({NOW}, {page["observed_at"] for page in pages})


# -- Single-instance protection ---------------------------------------------


class SingleInstanceTests(unittest.TestCase):
    """One connector per state directory, arbitrated by the filesystem."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / portable.LOCK_FILE_NAME

    def test_the_lock_records_this_process_and_is_only_readable_by_its_owner(
        self,
    ) -> None:
        lock = portable.SingleInstanceLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(str(os.getpid()), self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_a_second_connector_is_refused_while_the_first_holds_the_lock(
        self,
    ) -> None:
        first = portable.SingleInstanceLock(self.path)
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(portable.ConnectorError):
            portable.SingleInstanceLock(self.path).acquire()

    def test_a_lock_left_by_a_dead_owner_is_stolen(self) -> None:
        # Otherwise a crash would need a human before the Mac could be observed
        # again.
        self.path.write_text(str(DEAD_PID), encoding="utf-8")
        lock = portable.SingleInstanceLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(str(os.getpid()), self.path.read_text(encoding="utf-8"))

    def test_an_unparsable_lock_is_stolen_rather_than_honoured(self) -> None:
        self.path.write_text("not a pid", encoding="utf-8")
        lock = portable.SingleInstanceLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(str(os.getpid()), self.path.read_text(encoding="utf-8"))

    def test_a_release_by_a_process_that_does_not_own_the_lock_leaves_it_alone(
        self,
    ) -> None:
        self.path.write_text(str(os.getpid() + 1), encoding="utf-8")
        portable.SingleInstanceLock(self.path).release()
        self.assertTrue(self.path.exists())

    def test_the_lock_is_a_context_manager_that_releases_on_the_way_out(self) -> None:
        with portable.SingleInstanceLock(self.path):
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_a_permission_error_on_the_owner_probe_means_alive(self) -> None:
        # A signal this user may not send is proof the process exists. Refusing
        # to start is recoverable; stealing from a live connector is not.
        self.path.write_text(str(DEAD_PID), encoding="utf-8")
        with (
            mock.patch.object(os, "kill", side_effect=PermissionError),
            self.assertRaises(portable.ConnectorError),
        ):
            portable.SingleInstanceLock(self.path).acquire()

    def test_the_lock_lives_beside_the_state_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "state"
            self.assertEqual(
                root / portable.LOCK_FILE_NAME,
                workbench_connector.connector_lock_path(root),
            )


# -- Payload mapping --------------------------------------------------------


class PayloadTests(unittest.TestCase):
    """Every frame the connector builds is one its own receiver accepts."""

    def assert_publishable(self, payload: dict[str, Any]) -> None:
        observation_envelope(
            payload=payload,
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            sent_at=NOW,
        )

    def test_the_local_catalog_entry_shape_is_not_the_wire_shape(self) -> None:
        # The reason `catalog_entry_payload` exists. A title-less plan is legal
        # locally and refused on the wire, so the raw payload cannot be sent.
        entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry
        self.assertIsNone(entry.title)
        with self.assertRaises(TransportError):
            self.assert_publishable(
                {
                    "view": VIEW_CATALOG_PAGE,
                    "observed_at": NOW,
                    "generation": "g",
                    "complete": False,
                    "final": False,
                    "offset": 0,
                    "entry_count": 1,
                    "entries": [dict(entry.to_payload())],
                }
            )
        self.assertEqual("", portable.catalog_entry_payload(entry)["title"])

    def test_every_field_the_wire_requires_is_supplied_by_the_mapper(self) -> None:
        entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry
        published = portable.catalog_entry_payload(entry)
        self.assertEqual(
            set(),
            CATALOG_ENTRY_SHAPE.required - set(published),
            "the catalog mapper dropped a field the transport requires",
        )
        declared = CATALOG_ENTRY_SHAPE.required | CATALOG_ENTRY_SHAPE.optional
        self.assertEqual(
            set(),
            set(published) - declared,
            "the catalog mapper carried a field the transport does not declare",
        )

    def test_an_unknown_pause_reason_folds_onto_other_rather_than_dropping_the_plan(
        self,
    ) -> None:
        self.assertEqual("blocked", portable.pause_reason_word("blocked"))
        self.assertEqual(
            history.PAUSE_REASON_OTHER, portable.pause_reason_word("brand-new-reason")
        )
        self.assertIsNone(portable.pause_reason_word(None))
        self.assertIsNone(portable.pause_reason_word(""))
        self.assertIsNone(portable.pause_reason_word(7))

    def test_a_session_identifier_that_is_not_a_dotted_number_is_omitted(self) -> None:
        self.assertEqual("1", portable.session_id_word("1"))
        self.assertEqual("2.3.1", portable.session_id_word(" 2.3.1 "))
        self.assertIsNone(portable.session_id_word("review-fix"))
        self.assertIsNone(portable.session_id_word("0.1"))
        self.assertIsNone(portable.session_id_word(None))

    def test_a_long_project_list_is_truncated_rather_than_costing_the_plan(
        self,
    ) -> None:
        names = [f"repo-{index}" for index in range(catalog.MAX_PROJECTS_PER_PLAN)]
        entry = catalog.record_from_summary(
            summary_payload(
                "/tmp/plan",
                repositories=[
                    {"alias": name, "path": f"/tmp/{name}"} for name in names
                ],
                repository_count=len(names),
            )
        ).entry
        kept = observation.MAX_WARNINGS
        self.assertGreater(len(entry.projects), kept)
        published = portable.catalog_entry_payload(entry)
        self.assertEqual(kept, len(published["projects"]))
        self.assertEqual(list(entry.projects[:kept]), published["projects"])

    def test_a_catalog_page_is_published_in_the_shape_the_store_ingests(self) -> None:
        page = build_page(2)
        published = portable.catalog_page_payload(page, observed_at=NOW)
        self.assert_publishable(published)
        self.assertEqual(page.generation, published["generation"])
        self.assertEqual(2, published["entry_count"])
        self.assertEqual(2, published["total"])
        self.assertTrue(published["complete"])
        self.assertTrue(published["final"])
        # Not `CatalogPage.to_payload`: those two names are local only.
        self.assertNotIn("count", published)
        self.assertNotIn("limit", published)

    def test_an_entry_the_wire_refuses_clears_both_complete_and_final(self) -> None:
        # Clearing `complete` alone would be refused on arrival, and a page
        # that lost a plan must not retire the plan it lost.
        page = build_page(2)
        broken = dict(portable.catalog_entry_payload(page.entries[0]))
        broken["urgency_rank"] = "not a number"
        with mock.patch.object(
            portable,
            "catalog_entry_payload",
            side_effect=[broken, portable.catalog_entry_payload(page.entries[1])],
        ):
            published = portable.catalog_page_payload(page, observed_at=NOW)
        self.assert_publishable(published)
        self.assertEqual(1, published["entry_count"])
        self.assertFalse(published["complete"])
        self.assertFalse(published["final"])
        self.assertTrue(
            any("could not be published" in text for text in published["warnings"])
        )

    def test_a_history_entry_with_no_run_id_is_dropped_and_the_batch_says_so(
        self,
    ) -> None:
        batch = history_batch((history_entry("a"), history_entry("b", run_id=None)))
        published = portable.history_batch_payload(batch)
        self.assert_publishable(published)
        self.assertEqual(1, published["entry_count"])
        self.assertFalse(published["complete"])
        self.assertTrue(
            any("could not be published" in text for text in published["warnings"])
        )

    def test_a_history_batch_never_carries_the_questions_it_was_built_beside(
        self,
    ) -> None:
        # A question is prose. It reaches the Pi through a correlated detail
        # read or not at all.
        question = history.QuestionEntry(
            entry_id="q-1",
            source_key="source-a",
            plan_key="0123456789abcdef",
            run_key="fedcba9876543210",
            question_key="qk-1",
            outcome=history.QUESTION_OUTCOME_ANSWERED,
            recorded_at=NOW - 30.0,
        )
        batch = history_batch((history_entry(),), questions=(question,))
        self.assertIn("questions", batch.to_payload())
        published = portable.history_batch_payload(batch)
        self.assert_publishable(published)
        self.assertNotIn("questions", published)
        self.assertNotIn("question_count", published)

    def test_a_complete_batch_with_nothing_dropped_stays_complete(self) -> None:
        published = portable.history_batch_payload(history_batch((history_entry(),)))
        self.assertTrue(published["complete"])
        self.assertEqual(1, published["entry_count"])

    def test_a_snapshot_digest_ignores_the_moment_it_was_taken(self) -> None:
        # Otherwise every poll of an idle corpus would look like a change.
        rows = (wire_row(),)
        first = portable.snapshot_payload(rows, observed_at=NOW, source_digest="gen")
        second = portable.snapshot_payload(
            rows, observed_at=NOW + 600.0, source_digest="gen"
        )
        self.assertEqual(1, first["row_count"])
        self.assertEqual(first["digest"], second["digest"])
        self.assertNotEqual(first["observed_at"], second["observed_at"])

    def test_a_changed_row_changes_the_digest(self) -> None:
        first = portable.snapshot_payload(
            (wire_row(primary_state="running"),), observed_at=NOW, source_digest="gen"
        )
        second = portable.snapshot_payload(
            (wire_row(primary_state="verifying"),), observed_at=NOW, source_digest="gen"
        )
        self.assertNotEqual(first["digest"], second["digest"])

    def test_rows_beyond_the_frame_limit_are_cut_and_the_frame_admits_it(self) -> None:
        rows = tuple(
            wire_row(run_id=f"run-{index}", run_key=f"{index:016x}")
            for index in range(MAX_RUNS_PER_SNAPSHOT + 3)
        )
        published = portable.snapshot_payload(rows, observed_at=NOW)
        self.assert_publishable(published)
        self.assertEqual(MAX_RUNS_PER_SNAPSHOT, published["row_count"])
        self.assertTrue(published["truncated"])
        self.assertTrue(any("row limit" in text for text in published["warnings"]))

    def test_a_row_the_wire_refuses_is_dropped_and_counted(self) -> None:
        published = portable.snapshot_payload(
            (wire_row(), wire_row(run_key=None)), observed_at=NOW
        )
        self.assert_publishable(published)
        self.assertEqual(1, published["row_count"])
        self.assertEqual(1, published["failed_projections"])

    def test_warnings_are_bounded_in_count_and_in_length(self) -> None:
        published = portable.snapshot_payload(
            (),
            observed_at=NOW,
            warnings=["w" * (MAX_WARNING_CHARS + 50)] * (observation.MAX_WARNINGS + 5),
        )
        self.assert_publishable(published)
        self.assertEqual(observation.MAX_WARNINGS, len(published["warnings"]))
        self.assertEqual(MAX_WARNING_CHARS, len(published["warnings"][0]))

    def test_a_heartbeat_always_names_the_contract_set_it_speaks(self) -> None:
        published = portable.heartbeat_payload(observed_at=NOW, bridge_alive=True)
        self.assert_publishable(published)
        self.assertEqual(
            observation.SUPPORTED_CONTRACT_SET_IDS[0], published["contract_set_id"]
        )

    def test_the_hello_declares_observation_only_and_reads_but_never_mutation(
        self,
    ) -> None:
        hello = portable.connector_hello(
            connector_id=CONNECTOR_ID, target_id=TARGET_ID, asl_version="1.2.3"
        )
        self.assertTrue(hello["observation_only"])
        self.assertEqual(list(portable.CONNECTOR_CAPABILITIES), hello["capabilities"])
        self.assertIn("detail", hello["capabilities"])
        # Reading on request is not mutating on request. The connector answers a
        # correlated read and still performs nothing, so ``observation_only``
        # stays true and ``mutate`` stays absent.
        self.assertNotIn("mutate", hello["capabilities"])

    def test_a_connector_without_a_responder_does_not_offer_detail(self) -> None:
        connector = portable.ObservationConnector(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            source=StubSource(),
            channel=FakeChannel(),
        )
        self.assertEqual(portable.OBSERVATION_CAPABILITIES, connector.capabilities)
        self.assertNotIn("detail", connector.hello()["capabilities"])

    def test_a_hello_the_service_would_refuse_is_refused_before_anything_is_sent(
        self,
    ) -> None:
        with self.assertRaises(TransportError):
            portable.connector_hello(connector_id="not a valid id", target_id=TARGET_ID)

    def test_the_scan_warnings_ride_only_the_page_that_closes_the_generation(
        self,
    ) -> None:
        channel = FakeChannel()
        connector = make_connector(
            StubSource([cycle_with(pages=3, warnings=("one plan is unreadable",))]),
            channel,
        )
        drain(connector, steps=8)
        pages = channel.payloads(VIEW_CATALOG_PAGE)
        self.assertEqual(3, len(pages))
        carrying = [page for page in pages if page.get("warnings")]
        self.assertEqual(1, len(carrying))
        self.assertTrue(carrying[0]["final"])

    def test_a_frame_this_connector_cannot_publish_does_not_wedge_the_loop(
        self,
    ) -> None:
        channel = FakeChannel()
        connector = make_connector(StubSource([cycle_with()]), channel)
        with mock.patch.object(
            portable, "heartbeat_payload", return_value={"view": VIEW_HEARTBEAT}
        ):
            self.assertIsNone(connector.step(NOW))
            self.assertIn("unpublishable", str(connector.degraded_reason))
            # The refused frame comes off the queue along with the cycle. A
            # refusal is a property of the payload and not of the moment, so a
            # frame left queued would be re-selected and re-refused on every
            # step, and nothing behind it would ever be delivered.
            self.assertEqual(VIEW_SNAPSHOT, connector.step(NOW + 1.0))
        self.assertEqual(VIEW_HEARTBEAT, connector.step(NOW + 15.0))


# -- The ASL provider half --------------------------------------------------


class RemoteActionTests(unittest.TestCase):
    """The published action vocabulary is the operations vocabulary."""

    def test_every_published_action_is_a_real_operation_method(self) -> None:
        for action, method in workbench_connector.REMOTE_ACTIONS.items():
            self.assertIn(action, intervention.ACTIONS, f"{action} is not an action")
            self.assertIn(method, operations.OPERATION_METHODS, f"{method} is not real")

    def test_the_two_actions_with_no_remote_form_are_not_published(self) -> None:
        # `question.resolve` has no operation method, and `workbench.ensure`
        # opens a browser on this machine.
        self.assertNotIn(
            intervention.ACTION_RESOLVE, workbench_connector.REMOTE_ACTIONS
        )
        self.assertNotIn(
            intervention.ACTION_WORKBENCH, workbench_connector.REMOTE_ACTIONS
        )

    def test_every_other_action_is_published(self) -> None:
        unpublished = set(intervention.ACTIONS) - set(
            workbench_connector.REMOTE_ACTIONS
        )
        self.assertEqual(
            {intervention.ACTION_RESOLVE, intervention.ACTION_WORKBENCH},
            unpublished,
            "an action gained or lost a remote form without this test moving",
        )

    def test_an_unknown_action_is_dropped_rather_than_published_verbatim(self) -> None:
        row = {
            "actions": ["run.abort", "something.new", 7, intervention.ACTION_RESOLVE]
        }
        entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry
        published = workbench_connector.project_run(
            dict(row, run_id="run-1", last_activity_at=NOW), entry=entry
        )
        assert published is not None
        self.assertEqual([operations.METHOD_ABORT], published["actions"])

    def test_the_active_state_list_is_the_bridges_own(self) -> None:
        self.assertEqual(
            bridge.ACTIVE_PRIMARY_STATES, workbench_connector.ACTIVE_PRIMARY_STATES
        )


class RunProjectionTests(unittest.TestCase):
    """One bridge row becomes one wire row, or none at all."""

    def setUp(self) -> None:
        self.entry = catalog.record_from_summary(summary_payload("/tmp/plan")).entry

    def project(self, **row: Any) -> dict[str, Any] | None:
        return workbench_connector.project_run(row, entry=self.entry)

    def test_the_identity_fields_come_from_the_catalog_record(self) -> None:
        published = self.project(run_id="run-1", last_activity_at=NOW)
        assert published is not None
        self.assertEqual(self.entry.plan_key, published["plan_key"])
        self.assertEqual(self.entry.run_key, published["run_key"])
        self.assertEqual(self.entry.project_group, published["project"])

    def test_a_row_with_no_run_id_is_refused(self) -> None:
        self.assertIsNone(self.project(last_activity_at=NOW))
        self.assertIsNone(self.project(run_id="", last_activity_at=NOW))
        self.assertIsNone(self.project(run_id=7, last_activity_at=NOW))

    def test_a_row_with_no_usable_time_is_refused_rather_than_stamped(self) -> None:
        self.assertIsNone(self.project(run_id="run-1"))
        published = self.project(run_id="run-1", run_started_at="2026-09-01T10:00:00Z")
        assert published is not None
        self.assertIsNotNone(published["last_activity_at"])

    def test_the_question_keeps_its_identity_and_loses_its_words(self) -> None:
        published = self.project(
            run_id="run-1",
            last_activity_at=NOW,
            question={
                "question_id": "q-1",
                "asked_at": "2026-09-01T10:00:00+00:00",
                "unanswered_count": 2,
                "text": "Should we drop the v1 column?",
                "session_id": "3",
                "context_kind": "waiting",
            },
        )
        assert published is not None
        self.assertEqual("q-1", published["question_id"])
        self.assertEqual(2, published["unanswered_count"])
        self.assertNotIn("question", published)
        self.assertNotIn(
            "Should we drop the v1 column?", json.dumps(published, sort_keys=True)
        )

    def test_the_rows_paths_and_operator_label_never_travel(self) -> None:
        published = self.project(
            run_id="run-1",
            last_activity_at=NOW,
            plan_dir="/Users/someone/plans/alpha",
            workspace_root="/Users/someone/code",
            label="Nightly refactor",
        )
        assert published is not None
        rendered = json.dumps(published, sort_keys=True)
        for leaked in ("/Users/someone", "Nightly refactor"):
            self.assertNotIn(leaked, rendered)

    def test_a_projected_row_is_one_the_wire_accepts(self) -> None:
        published = self.project(
            run_id="run-1",
            last_activity_at=NOW,
            primary_state="running",
            phase="running",
            urgency_group="in-progress",
            actions=["run.abort"],
            harness="codex",
            session_id="2",
            pause_reason="blocked",
        )
        assert published is not None
        observation_envelope(
            payload=portable.snapshot_payload((published,), observed_at=NOW),
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            sent_at=NOW,
        )


class ConnectorIdentityTests(unittest.TestCase):
    """The receiver's primary key for this Mac is stable and spellable."""

    def test_a_hostname_becomes_a_transport_identifier(self) -> None:
        self.assertEqual(
            "macbook-pro", workbench_connector.default_connector_id("macbook pro")
        )
        self.assertEqual(
            "studio.local", workbench_connector.default_connector_id("studio.local")
        )

    def test_a_name_that_cannot_be_spelled_at_all_becomes_mac(self) -> None:
        for raw in ("", "   ", "!!!", "---"):
            self.assertEqual("mac", workbench_connector.default_connector_id(raw))

    def test_the_derived_identifier_is_one_the_hello_accepts(self) -> None:
        for raw in ("séb's mac", "a" * 400, "studio.local", "!!!"):
            derived = workbench_connector.default_connector_id(raw)
            portable.connector_hello(connector_id=derived, target_id=TARGET_ID)

    def test_the_identifier_is_stable_across_calls(self) -> None:
        self.assertEqual(
            workbench_connector.default_connector_id(),
            workbench_connector.default_connector_id(),
        )


class TokenFileTests(unittest.TestCase):
    """A token file only this user may read, or no connector at all."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.path = self.root / "connector.json"

    def write(self, document: object, *, mode: int = 0o600) -> Path:
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.path.chmod(mode)
        return self.path

    def test_a_private_file_yields_its_token(self) -> None:
        self.write({"connector_token": "abc123"})
        self.assertEqual("abc123", workbench_connector.read_connector_token(self.path))

    def test_a_file_readable_beyond_its_owner_is_refused(self) -> None:
        self.write({"connector_token": "abc123"}, mode=0o644)
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.path)

    def test_a_symlink_is_refused_rather_than_followed(self) -> None:
        real = self.write({"connector_token": "abc123"})
        link = self.root / "link.json"
        link.symlink_to(real)
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(link)

    def test_a_missing_malformed_or_tokenless_file_is_refused(self) -> None:
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.root / "absent.json")
        self.path.write_text("{not json", encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.path)
        for document in ([], {}, {"connector_token": ""}, {"connector_token": 7}):
            self.write(document)
            with self.assertRaises(workbench_connector.ConnectorSetupError):
                workbench_connector.read_connector_token(self.path)

    def test_a_token_that_is_not_printable_ascii_is_refused(self) -> None:
        self.write({"connector_token": "abcé"})
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.read_connector_token(self.path)


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]


class _FakeConnection:
    def __init__(self, *, status: int = 202, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.requests: list[dict[str, Any]] = []
        self.closed = 0

    def request(self, method: str, path: str, *, body: bytes, headers: Any) -> None:
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": dict(headers)}
        )

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.status, self.body)

    def close(self) -> None:
        self.closed += 1


class HttpsChannelTests(unittest.TestCase):
    """One bounded POST per frame, and nothing clever."""

    def channel(self, connection: Any) -> workbench_connector.HttpsChannel:
        return workbench_connector.HttpsChannel(
            origin="https://workbench.example",
            token="secret-token",
            connect=lambda: connection,
        )

    def test_a_frame_is_posted_to_the_ingest_route_with_a_bearer_token(self) -> None:
        connection = _FakeConnection()
        self.channel(connection).send(b'{"kind":"observation"}')
        sent = connection.requests[0]
        self.assertEqual("POST", sent["method"])
        self.assertEqual(workbench_connector.INGEST_PATH, sent["path"])
        self.assertEqual("Bearer secret-token", sent["headers"]["Authorization"])
        self.assertEqual("application/json", sent["headers"]["Content-Type"])
        self.assertEqual("22", sent["headers"]["Content-Length"])
        self.assertEqual(1, connection.closed)

    def test_both_accepted_statuses_are_accepted(self) -> None:
        for status in workbench_connector.ACCEPTED_STATUSES:
            self.channel(_FakeConnection(status=status)).send(b"{}")

    def test_a_refused_status_is_a_channel_error(self) -> None:
        for status in (400, 401, 409, 500):
            with self.assertRaises(portable.ChannelError):
                self.channel(_FakeConnection(status=status)).send(b"{}")

    def test_a_connection_that_cannot_be_made_is_a_channel_error(self) -> None:
        channel = workbench_connector.HttpsChannel(
            origin="https://workbench.example",
            token="t",
            connect=lambda: (_ for _ in ()).throw(OSError("no route to host")),
        )
        with self.assertRaises(portable.ChannelError):
            channel.send(b"{}")

    def test_a_request_that_fails_mid_flight_closes_the_connection(self) -> None:
        connection = _FakeConnection()
        connection.request = mock.Mock(  # type: ignore[method-assign]
            side_effect=OSError("broken pipe")
        )
        with self.assertRaises(portable.ChannelError):
            self.channel(connection).send(b"{}")
        self.assertEqual(1, connection.closed)

    def test_an_oversized_response_is_a_channel_error(self) -> None:
        from asl_workbench.transport import MAX_FRAME_BYTES

        connection = _FakeConnection(body=b"x" * (MAX_FRAME_BYTES + 10))
        with self.assertRaises(portable.ChannelError):
            self.channel(connection).send(b"{}")

    def test_an_origin_that_is_not_https_is_refused_at_construction(self) -> None:
        for origin in ("http://workbench.example", "https://", "not a url", ""):
            with self.assertRaises(workbench_connector.ConnectorSetupError):
                workbench_connector.HttpsChannel(origin=origin, token="t")

    def test_an_explicit_port_is_honoured(self) -> None:
        channel = workbench_connector.HttpsChannel(
            origin="https://workbench.example:8443", token="t"
        )
        self.assertEqual(8443, channel.port)
        self.assertEqual(
            443,
            workbench_connector.HttpsChannel(
                origin="https://workbench.example", token="t"
            ).port,
        )


# -- The command ------------------------------------------------------------


class _Args:
    def __init__(self, **fields: Any) -> None:
        defaults: dict[str, Any] = {
            "target_id": TARGET_ID,
            "origin": "https://workbench.example",
            "token_file": None,
            "connector_id": CONNECTOR_ID,
            "state_dir": None,
            "plan_root": None,
            "heartbeat_interval": None,
            "observation_interval": None,
            "json": True,
        }
        defaults.update(fields)
        for name, value in defaults.items():
            setattr(self, name, value)


class CommandTests(unittest.TestCase):
    """``asl workbench connect`` from parsed arguments, with no socket."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.state_dir.mkdir(parents=True)
        self.channel = FakeChannel()
        self.source = StubSource([cycle_with()])

    def run_command(self, **overrides: Any) -> tuple[int, dict[str, Any]]:
        args = _Args(state_dir=str(self.state_dir), **overrides)
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=lambda connector, stop: None,
                install_signal_handlers=lambda stop: lambda: None,
            )
        text = stream.getvalue().strip()
        return code, json.loads(text) if text else {}

    def test_a_start_reports_its_identity_capabilities_and_lock(self) -> None:
        code, payload = self.run_command()
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("connecting", payload["reason"])
        self.assertEqual(CONNECTOR_ID, payload["connector_id"])
        self.assertEqual(TARGET_ID, payload["target_id"])
        self.assertEqual(list(portable.CONNECTOR_CAPABILITIES), payload["capabilities"])
        self.assertEqual(
            str(workbench_connector.connector_lock_path(self.state_dir)),
            payload["lock"],
        )

    def test_the_lock_is_released_when_the_command_returns(self) -> None:
        self.run_command()
        self.assertFalse(
            workbench_connector.connector_lock_path(self.state_dir).exists()
        )

    def test_a_second_connector_is_refused_rather_than_publishing_a_rival_view(
        self,
    ) -> None:
        lock = portable.SingleInstanceLock(
            workbench_connector.connector_lock_path(self.state_dir)
        )
        lock.acquire()
        self.addCleanup(lock.release)
        code, payload = self.run_command()
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("connector-locked", payload["reason"])

    def test_a_missing_target_is_a_setup_refusal_that_leaves_no_lock(self) -> None:
        code, payload = self.run_command(target_id=None)
        self.assertEqual(1, code)
        self.assertEqual("setup-failed", payload["reason"])
        self.assertFalse(
            workbench_connector.connector_lock_path(self.state_dir).exists()
        )

    def test_the_publication_loop_receives_the_connector_and_a_stop_event(self) -> None:
        seen: dict[str, Any] = {}

        def publish(connector: Any, stop: Any) -> None:
            seen["connector"] = connector
            seen["stop"] = stop

        args = _Args(state_dir=str(self.state_dir))
        with redirect_stdout(io.StringIO()):
            workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=publish,
                install_signal_handlers=lambda stop: lambda: None,
            )
        self.assertEqual(CONNECTOR_ID, seen["connector"].connector_id)
        self.assertIsInstance(seen["stop"], threading.Event)
        self.assertEqual(portable.STATE_STOPPED, seen["connector"].state)

    def test_an_interrupt_during_the_loop_is_a_clean_exit(self) -> None:
        args = _Args(state_dir=str(self.state_dir))

        def publish(connector: Any, stop: Any) -> None:
            raise KeyboardInterrupt

        with redirect_stdout(io.StringIO()):
            code = workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=publish,
                install_signal_handlers=lambda stop: lambda: None,
            )
        self.assertEqual(0, code)
        self.assertFalse(
            workbench_connector.connector_lock_path(self.state_dir).exists()
        )

    def test_the_signal_handlers_are_restored_on_the_way_out(self) -> None:
        restored: list[bool] = []
        args = _Args(state_dir=str(self.state_dir))
        with redirect_stdout(io.StringIO()):
            workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=lambda connector, stop: None,
                install_signal_handlers=lambda stop: lambda: restored.append(True),
            )
        self.assertEqual([True], restored)

    def test_the_human_form_prints_a_line_instead_of_a_payload(self) -> None:
        args = _Args(state_dir=str(self.state_dir), json=False)
        stream = io.StringIO()
        with redirect_stdout(stream):
            workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=lambda connector, stop: None,
                install_signal_handlers=lambda stop: lambda: None,
            )
        self.assertIn(CONNECTOR_ID, stream.getvalue())
        self.assertIn(TARGET_ID, stream.getvalue())

    def test_the_intervals_the_operator_asked_for_are_the_ones_used(self) -> None:
        connector = workbench_connector.build_connector(
            _Args(heartbeat_interval=5.0, observation_interval=90.0),
            channel=self.channel,
            source=self.source,
        )
        self.assertEqual(5.0, connector._heartbeat_interval)
        self.assertEqual(90.0, connector._observation_interval)

    def test_a_non_positive_interval_is_a_setup_refusal(self) -> None:
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.build_connector(
                _Args(heartbeat_interval=-1.0),
                channel=self.channel,
                source=self.source,
            )

    def test_a_channel_needs_an_origin_and_a_token_file(self) -> None:
        for overrides in ({"origin": None}, {"token_file": None}):
            with self.assertRaises(workbench_connector.ConnectorSetupError):
                workbench_connector.build_connector(_Args(**overrides))


class ShutdownTests(unittest.TestCase):
    """A signal stops the loop between frames, and the loop stops the runtime."""

    def test_the_loop_runs_until_the_event_is_set_and_then_stops_the_connector(
        self,
    ) -> None:
        channel = FakeChannel()
        connector = make_connector(StubSource([cycle_with()]), channel)
        stop = threading.Event()
        moments = iter([NOW + index for index in range(4)])

        def clock() -> float:
            try:
                return next(moments)
            except StopIteration:
                stop.set()
                return NOW + 10.0

        workbench_connector.publish_forever(
            connector, stop=stop, clock=clock, interval=0.0
        )
        self.assertEqual(portable.STATE_STOPPED, connector.state)
        self.assertTrue(channel.frames)

    def test_a_loop_that_raises_still_stops_the_connector(self) -> None:
        connector = make_connector(StubSource([cycle_with()]), FakeChannel())
        stop = threading.Event()

        def clock() -> float:
            raise RuntimeError("the clock broke")

        with self.assertRaises(RuntimeError):
            workbench_connector.publish_forever(connector, stop=stop, clock=clock)
        self.assertEqual(portable.STATE_STOPPED, connector.state)

    def test_sigterm_and_sigint_set_the_event_and_are_restored(self) -> None:
        stop = threading.Event()
        before = {
            number: signal.getsignal(number)
            for number in (signal.SIGTERM, signal.SIGINT)
        }
        restore = workbench_connector.install_shutdown_handlers(stop)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
        finally:
            restore()
        for number, handler in before.items():
            self.assertIs(handler, signal.getsignal(number))


# -- The whole path, against a real corpus ----------------------------------


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
        canonical = plans.canonicalize_path(plan_dir)
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

    def open_store(self, name: str = "workbench.sqlite3") -> store.WorkbenchStore:
        db = store.WorkbenchStore.open(self.root / name, policy=POLICY, created_at=NOW)
        self.addCleanup(db.close)
        return db

    def publish(
        self,
        db: store.WorkbenchStore,
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


class CorpusDeliveryTests(CorpusTestCase):
    """A real corpus reaches a real store, accurately."""

    def test_one_pass_delivers_all_four_views(self) -> None:
        db = self.open_store()
        _, channel, _ = self.publish(db)
        self.assertEqual(
            {VIEW_HEARTBEAT, VIEW_SNAPSHOT, VIEW_CATALOG_PAGE, VIEW_HISTORY_BATCH},
            set(channel.views()),
        )

    def test_the_store_holds_the_plans_the_corpus_holds(self) -> None:
        db = self.open_store()
        self.publish(db)
        stored = {
            row["plan_key"]: row for row in db.catalog(now=NOW, include_absent=True)
        }
        self.assertEqual(
            {
                catalog.plan_key(str(self.paused)),
                catalog.plan_key(str(self.finished)),
            },
            set(stored),
        )
        paused = stored[catalog.plan_key(str(self.paused))]
        self.assertEqual("Paused plan", paused["title"])
        self.assertEqual("paused-for-reply", paused["primary_state"])
        self.assertEqual(
            "Finished plan", stored[catalog.plan_key(str(self.finished))]["title"]
        )

    def test_the_store_holds_the_active_run_and_not_the_settled_one(self) -> None:
        # A catalog carries every plan; a snapshot carries what an operator can
        # act on. A complete plan is not actionable work.
        db = self.open_store()
        self.publish(db)
        rows = db.runs(now=NOW)
        self.assertEqual(["run-paused"], [row["run_id"] for row in rows])
        self.assertEqual("paused-for-reply", rows[0]["primary_state"])
        # The run and the catalog entry agree on the run key, which is how the
        # cockpit joins the two views it was sent separately.
        paused = next(
            row
            for row in db.catalog(now=NOW)
            if row["plan_key"] == catalog.plan_key(str(self.paused))
        )
        self.assertEqual(paused["run_key"], rows[0]["run_key"])
        self.assertEqual("Paused plan", rows[0]["plan_title"])

    def test_the_store_holds_the_ending_the_corpus_recorded(self) -> None:
        db = self.open_store()
        self.publish(db)
        entries = db.history(now=NOW)
        self.assertEqual(["run-finished"], [entry["run_id"] for entry in entries])
        self.assertEqual(history.HISTORY_OUTCOME_COMPLETED, entries[0]["outcome"])

    def test_the_connector_is_read_back_as_a_live_observation_only_source(
        self,
    ) -> None:
        db = self.open_store()
        self.publish(db)
        statuses = {
            status.connector_id: status for status in db.connector_statuses(now=NOW)
        }
        status = statuses[CONNECTOR_ID]
        self.assertTrue(status.bridge_alive)
        self.assertTrue(status.compatible)
        self.assertIsNone(status.degraded_reason)
        self.assertEqual(1, status.row_count)
        self.assertFalse(status.truncated)

    def test_the_catalog_generation_the_snapshot_names_is_the_one_delivered(
        self,
    ) -> None:
        db = self.open_store()
        _, channel, _ = self.publish(db)
        snapshot = channel.payloads(VIEW_SNAPSHOT)[0]
        page = channel.payloads(VIEW_CATALOG_PAGE)[0]
        self.assertEqual(page["generation"], snapshot["source_digest"])

    def test_the_final_page_closes_a_complete_generation(self) -> None:
        db = self.open_store()
        _, channel, _ = self.publish(db)
        pages = channel.payloads(VIEW_CATALOG_PAGE)
        self.assertTrue(pages[-1]["final"])
        self.assertTrue(pages[-1]["complete"])
        self.assertEqual(2, pages[-1]["total"])

    def test_a_pass_over_an_unchanged_corpus_repeats_rather_than_duplicates(
        self,
    ) -> None:
        db = self.open_store()
        source = self.make_source()
        self.publish(db, source=source)
        before = (
            [row["run_id"] for row in db.runs(now=NOW)],
            [row["plan_key"] for row in db.catalog(now=NOW)],
            [entry["entry_id"] for entry in db.history(now=NOW)],
        )
        self.publish(db, source=source, start=NOW + 120.0)
        after = (
            [row["run_id"] for row in db.runs(now=NOW + 120.0)],
            [row["plan_key"] for row in db.catalog(now=NOW + 120.0)],
            [entry["entry_id"] for entry in db.history(now=NOW + 120.0)],
        )
        self.assertEqual(before, after)

    def test_a_reconnect_across_a_dropped_link_duplicates_nothing(self) -> None:
        db = self.open_store()
        source = self.make_recording_source()
        channel = FakeChannel()
        connector = make_connector(source, channel)

        # One full pass lands, then the link drops mid-way through the next.
        drain(connector, start=NOW, steps=16)
        self.assertEqual(1, len(source.committed))
        channel.failing = True
        connector.step(NOW + 120.0)
        channel.failing = False
        drain(connector, start=NOW + 240.0, steps=16)

        for frame in channel.frames:
            ingest(db, frame, at=NOW + 240.0)
        self.assertEqual(
            1, len(db.runs(now=NOW + 240.0)), "a reconnect duplicated a run"
        )
        self.assertEqual(
            2, len(db.catalog(now=NOW + 240.0)), "a reconnect duplicated a plan"
        )
        self.assertEqual(
            1, len(db.history(now=NOW + 240.0)), "a reconnect duplicated an ending"
        )
        self.assertGreater(connector.generation, 1)

    def test_a_redelivered_corpus_frame_is_recognised_as_the_same_frame(self) -> None:
        # The heartbeat is excluded on purpose: it is the one view whose
        # re-arrival is refused outright rather than absorbed, because a
        # replayed liveness signal would revive a connector that has gone away.
        db = self.open_store()
        _, channel, _ = self.publish(db)
        corpus = [
            frame
            for frame in channel.frames
            if frame["payload"]["view"] != VIEW_HEARTBEAT
        ]
        self.assertEqual(3, len(corpus))
        again = [ingest(db, frame, at=NOW) for frame in corpus]
        self.assertTrue(all(result.duplicate for result in again))

    def test_an_ending_survives_a_shutdown_that_interrupted_its_delivery(self) -> None:
        # Nothing is committed unless the whole cycle landed, so the window is
        # re-read rather than lost.
        db = self.open_store()
        source = self.make_recording_source()
        channel = FakeChannel()
        connector = make_connector(source, channel)
        connector.step(NOW)
        connector.step(NOW + 1.0)
        connector.stop()
        self.assertEqual([], source.committed)

        recovered = make_connector(
            self.make_source(checkpoints=source.inner.checkpoints), channel
        )
        drain(recovered, start=NOW + 120.0, steps=16)
        for frame in channel.frames:
            ingest(db, frame, at=NOW + 120.0)
        self.assertEqual(
            ["run-finished"],
            [entry["run_id"] for entry in db.history(now=NOW + 120.0)],
        )

    def test_a_committed_window_is_not_re_sent(self) -> None:
        source = self.make_source()
        channel = FakeChannel()
        connector = make_connector(source, channel)
        drain(connector, start=NOW, steps=16)
        first = channel.payloads(VIEW_HISTORY_BATCH)
        self.assertEqual(1, len(first))
        self.assertEqual(1, first[0]["entry_count"])

        drain(connector, start=NOW + 120.0, steps=16)
        later = channel.payloads(VIEW_HISTORY_BATCH)[1:]
        self.assertTrue(later)
        self.assertEqual(
            [0] * len(later),
            [batch["entry_count"] for batch in later],
            "a committed window was imported again",
        )


class ObservationIsReadOnlyTests(CorpusTestCase):
    """Describing this corpus does not change it."""

    def test_repeated_passes_leave_every_byte_of_the_state_tree_alone(self) -> None:
        source = self.make_source()
        before = tree_digest(self.state_root)
        plans_before = tree_digest(self.plans_root)
        for index in range(3):
            source.observe(NOW + index)
        after = tree_digest(self.state_root)
        self.assertEqual(before, after)
        self.assertEqual(plans_before, tree_digest(self.plans_root))

    def test_a_stale_lock_is_inspected_and_never_reclaimed(self) -> None:
        lock_path = (
            state.plan_state_dir(self.paused, self.state_root) / state.LOCK_FILE_NAME
        )
        before = lock_path.read_bytes()
        self.make_source().observe(NOW)
        self.assertTrue(lock_path.exists(), "observation reclaimed a stale lock")
        self.assertEqual(before, lock_path.read_bytes())

    def test_a_whole_publication_pass_writes_only_the_checkpoint(self) -> None:
        source = self.make_source()
        before = tree_digest(self.state_root)
        connector = make_connector(source, FakeChannel())
        drain(connector, steps=16)
        after = tree_digest(self.state_root)
        changed = {
            name
            for name in set(before) | set(after)
            if before.get(name) != after.get(name)
        }
        self.assertEqual(
            {workbench_history.CHECKPOINT_FILE_NAME},
            changed,
            "a publication pass wrote something other than its own checkpoint",
        )

    def test_the_source_never_commits_on_its_own(self) -> None:
        source = self.make_source()
        source.observe(NOW)
        self.assertFalse(
            (self.state_root / workbench_history.CHECKPOINT_FILE_NAME).exists()
        )


class PrivacyTests(CorpusTestCase):
    """A remote service may hold the corpus and not the disk it lives on."""

    def rendered_frames(self) -> str:
        channel = FakeChannel()
        connector = make_connector(self.make_source(), channel)
        drain(connector, steps=16)
        self.assertTrue(channel.frames)
        return json.dumps(channel.frames, sort_keys=True)

    def test_no_frame_carries_a_path_from_this_machine(self) -> None:
        rendered = self.rendered_frames()
        for leaked in (
            str(self.root),
            str(self.plans_root),
            str(self.paused),
            str(self.state_root),
        ):
            self.assertNotIn(leaked, rendered, f"a frame leaked {leaked}")

    def test_no_catalog_entry_carries_a_field_the_catalog_calls_private(self) -> None:
        # `catalog.PRIVATE_FIELDS` is the partition the catalog already made
        # between what a cache may hold and what it may not, so the assertion
        # is against that list rather than against a second list kept here.
        # The snapshot is a different projection with its own declared shape
        # and is checked against `RUN_SHAPE` instead.
        for frame in json.loads(self.rendered_frames()):
            payload = frame["payload"]
            if payload["view"] != VIEW_CATALOG_PAGE:
                continue
            for entry in payload["entries"]:
                leaked = set(entry) & set(catalog.PRIVATE_FIELDS)
                self.assertEqual(
                    set(), leaked, f"a catalog entry carried {sorted(leaked)}"
                )

    def test_no_snapshot_row_carries_a_name_the_run_shape_does_not_declare(
        self,
    ) -> None:
        declared = RUN_SHAPE.required | RUN_SHAPE.optional
        for frame in json.loads(self.rendered_frames()):
            payload = frame["payload"]
            if payload["view"] != VIEW_SNAPSHOT:
                continue
            for row in payload["runs"]:
                self.assertEqual(
                    set(), set(row) - declared, f"a run row carried {sorted(row)}"
                )

    def test_the_frames_carry_no_absolute_path_at_all(self) -> None:
        # Stronger than naming this test's own directories: nothing that looks
        # like a filesystem path may appear anywhere in a frame.
        rendered = self.rendered_frames()
        self.assertIsNone(
            re.search(r'"/[^"]*/', rendered),
            "a frame carried something shaped like an absolute path",
        )


class PageAndCapTests(CorpusTestCase):
    """Bounds are the transport's, and a bound that bites says so."""

    def test_a_catalog_larger_than_one_page_is_delivered_as_several(self) -> None:
        source = self.make_source(page_limit=1)
        cycle = source.observe(NOW)
        self.assertEqual(2, len(cycle.catalog_pages))
        self.assertFalse(cycle.catalog_pages[0].final)
        self.assertTrue(cycle.catalog_pages[-1].final)

        db = self.open_store()
        channel = FakeChannel()
        connector = make_connector(source, channel)
        drain(connector, steps=16)
        for frame in channel.frames:
            ingest(db, frame, at=NOW)
        self.assertEqual(2, len(db.catalog(now=NOW)))

    def test_the_page_and_row_limits_are_the_transports_own(self) -> None:
        self.assertEqual(MAX_CATALOG_ENTRIES_PER_PAGE, portable.CATALOG_PAGE_LIMIT)
        self.assertEqual(MAX_RUNS_PER_SNAPSHOT, portable.MAX_PUBLISHED_RUNS)

    def test_an_unanswered_question_survives_a_full_snapshot_of_live_runs(
        self,
    ) -> None:
        live_count = portable.MAX_PUBLISHED_RUNS
        summaries = [mock.Mock(primary_state="running") for _ in range(live_count)]
        summaries.append(mock.Mock(primary_state="paused-for-reply"))
        records = [
            mock.Mock(
                entry=catalog.record_from_summary(
                    summary_payload(f"/tmp/agent-plans/plan-{index}")
                ).entry
            )
            for index in range(live_count + 1)
        ]
        rows = [
            {
                "run_id": f"run-{index}",
                "primary_state": "running",
                "phase": "running",
                "urgency_group": "in-progress",
                "last_activity_at": NOW - index,
                "urgency_rank": 60,
                "actions": [],
            }
            for index in range(live_count)
        ]
        rows.append(
            {
                "run_id": "phone-question",
                "primary_state": "paused-for-reply",
                "phase": "awaiting-reply",
                "urgency_group": "needs-action",
                "last_activity_at": NOW - 600.0,
                "urgency_rank": 10,
                "actions": [intervention.ACTION_REPLY],
                "question": {
                    "question_id": "question-1",
                    "asked_at": iso(-600.0),
                    "unanswered_count": 1,
                },
            }
        )
        scan = mock.Mock(
            summaries=summaries,
            catalog=mock.Mock(records=records),
        )

        with mock.patch.object(bridge, "project_row", side_effect=rows):
            kept, failed, truncated = self.make_source()._runs(  # noqa: SLF001
                scan
            )

        self.assertEqual(0, failed)
        self.assertTrue(truncated)
        self.assertEqual(live_count, len(kept))
        self.assertIn("question-1", {row.get("question_id") for row in kept})

    def test_a_malformed_plan_is_catalogued_rather_than_lost(self) -> None:
        # A plan nobody can run is classified, not dropped: an operator looking
        # for the plan that stopped working has to be able to see it. So the
        # generation stays complete and the entry rides it like any other.
        broken = self.plans_root / "broken-plan"
        broken.mkdir()
        (broken / "context.md").write_text("# Context\n", encoding="utf-8")
        (broken / "manifest.md").write_text("# Broken plan\n", encoding="utf-8")
        cycle = self.make_source().observe(NOW)
        self.assertTrue(all(page.complete for page in cycle.catalog_pages))
        self.assertEqual(3, cycle.catalog_pages[-1].total)
        states = {
            entry.primary_state
            for page in cycle.catalog_pages
            for entry in page.entries
        }
        self.assertIn("invalid", states)
        self.assertIsNone(cycle.degraded_reason)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
