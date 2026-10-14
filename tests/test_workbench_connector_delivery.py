"""Bounded retry, publication order, checkpoints, heartbeats, single-instance, shutdown.

Split from tests/test_workbench_connector.py."""

from __future__ import annotations

import os
import signal
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agent_session_loop import (
    workbench_connector,
)
from asl_workbench import connector_protocols as portable_protocols
from asl_workbench import connector_support as portable_support
from asl_workbench import observation
from asl_workbench.transport import (
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
)
from tests.workbench_connector_support import (
    DEAD_PID,
    NOW,
    FakeChannel,
    HeldWorker,
    StubSource,
    cycle_with,
    drain,
    make_connector,
)

# -- Bounded retry ----------------------------------------------------------


class BackoffTests(unittest.TestCase):
    """Retry is bounded, deterministic, and assertable."""

    def test_the_delay_doubles_to_the_ceiling_and_stays_there(self) -> None:
        backoff = portable_support.Backoff(base=1.0, cap=8.0)
        self.assertEqual(
            [1.0, 2.0, 4.0, 8.0, 8.0, 8.0],
            [backoff.delay() for _ in range(6)],
        )

    def test_a_success_forgets_the_failures(self) -> None:
        backoff = portable_support.Backoff(base=1.0, cap=60.0)
        backoff.delay()
        backoff.delay()
        backoff.reset()
        self.assertEqual(1.0, backoff.delay())

    def test_a_very_long_outage_does_not_overflow_the_doubling(self) -> None:
        backoff = portable_support.Backoff(base=1.0, cap=60.0)
        for _ in range(2_000):
            self.assertLessEqual(backoff.delay(), 60.0)

    def test_a_backoff_needs_a_positive_base_and_ceiling(self) -> None:
        for kwargs in ({"base": 0.0}, {"cap": 0.0}, {"base": -1.0}):
            with self.assertRaises(portable_support.ConnectorError):
                portable_support.Backoff(**kwargs)

    def test_the_defaults_are_the_documented_schedule(self) -> None:
        # The docs and the README both quote 1s to a 60s cap.
        self.assertEqual(1.0, portable_support.BACKOFF_BASE_SECONDS)
        self.assertEqual(60.0, portable_support.BACKOFF_CAP_SECONDS)


class DeliveryFailureTests(unittest.TestCase):
    """What a failed delivery does to the schedule, the state, and the cycle."""

    def setUp(self) -> None:
        self.channel = FakeChannel()
        self.source = StubSource([cycle_with()])
        self.connector = make_connector(
            self.source,
            self.channel,
            backoff=portable_support.Backoff(base=2.0, cap=8.0),
        )

    def test_a_failure_enters_backoff_and_sends_nothing_until_it_elapses(self) -> None:
        self.channel.failing = True
        self.assertIsNone(self.connector.step(NOW))
        self.assertEqual(portable_support.STATE_BACKOFF, self.connector.state)
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
        self.assertEqual(portable_support.STATE_READY, self.connector.state)

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
        self.assertEqual(portable_support.STATE_STOPPED, self.connector.state)

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
        source = StubSource([portable_protocols.ObservationCycle(observed_at=NOW)])
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
        self.assertEqual(portable_support.STATE_STOPPED, self.connector.state)


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
        self.path = Path(self._temporary.name) / portable_support.LOCK_FILE_NAME

    def test_the_lock_records_this_process_and_is_only_readable_by_its_owner(
        self,
    ) -> None:
        lock = portable_support.SingleInstanceLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(str(os.getpid()), self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_a_second_connector_is_refused_while_the_first_holds_the_lock(
        self,
    ) -> None:
        first = portable_support.SingleInstanceLock(self.path)
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(portable_support.ConnectorError):
            portable_support.SingleInstanceLock(self.path).acquire()

    def test_a_lock_left_by_a_dead_owner_is_stolen(self) -> None:
        # Otherwise a crash would need a human before the Mac could be observed
        # again.
        self.path.write_text(str(DEAD_PID), encoding="utf-8")
        lock = portable_support.SingleInstanceLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(str(os.getpid()), self.path.read_text(encoding="utf-8"))

    def test_an_unparsable_lock_is_stolen_rather_than_honoured(self) -> None:
        self.path.write_text("not a pid", encoding="utf-8")
        lock = portable_support.SingleInstanceLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(str(os.getpid()), self.path.read_text(encoding="utf-8"))

    def test_a_release_by_a_process_that_does_not_own_the_lock_leaves_it_alone(
        self,
    ) -> None:
        self.path.write_text(str(os.getpid() + 1), encoding="utf-8")
        portable_support.SingleInstanceLock(self.path).release()
        self.assertTrue(self.path.exists())

    def test_the_lock_is_a_context_manager_that_releases_on_the_way_out(self) -> None:
        with portable_support.SingleInstanceLock(self.path):
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_a_permission_error_on_the_owner_probe_means_alive(self) -> None:
        # A signal this user may not send is proof the process exists. Refusing
        # to start is recoverable; stealing from a live connector is not.
        self.path.write_text(str(DEAD_PID), encoding="utf-8")
        with (
            mock.patch.object(os, "kill", side_effect=PermissionError),
            self.assertRaises(portable_support.ConnectorError),
        ):
            portable_support.SingleInstanceLock(self.path).acquire()

    def test_the_lock_lives_beside_the_state_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "state"
            self.assertEqual(
                root / portable_support.LOCK_FILE_NAME,
                workbench_connector.connector_lock_path(root),
            )


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
        self.assertEqual(portable_support.STATE_STOPPED, connector.state)
        self.assertTrue(channel.frames)

    def test_a_loop_that_raises_still_stops_the_connector(self) -> None:
        connector = make_connector(StubSource([cycle_with()]), FakeChannel())
        stop = threading.Event()

        def clock() -> float:
            raise RuntimeError("the clock broke")

        with self.assertRaises(RuntimeError):
            workbench_connector.publish_forever(connector, stop=stop, clock=clock)
        self.assertEqual(portable_support.STATE_STOPPED, connector.state)

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
