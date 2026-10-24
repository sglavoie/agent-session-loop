"""Bridge projection cadence behavior.

Split from tests/test_asl_bridge_server.py."""

from __future__ import annotations

import io
import json
import os
import threading
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

from agent_session_loop import (
    bridge_projection,
    bridge_protocol,
    bridge_server,
    bridge_session,
    bridge_workers,
)

from .asl_bridge_server_support import (
    WAIT,
    _request,
)

ASL_TEST_LANE = "integration"


class _HeldBuilder:
    """A corpus walk the test releases one at a time.

    Every call blocks until the test hands it a permit, so a walk can be held
    open while the test asserts what the rest of the bridge does meanwhile.
    """

    def __init__(self) -> None:
        self._entered = threading.Semaphore(0)
        self._permits = threading.Semaphore(0)
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        digest = f"d{self.calls}"
        self._entered.release()
        if not self._permits.acquire(timeout=WAIT):
            raise AssertionError("the test never released the held projection")
        return {
            "digest": digest,
            "row_count": 0,
            "rows": [],
            "warnings": [],
            "truncated": False,
        }

    def await_entry(self) -> None:
        if not self._entered.acquire(timeout=WAIT):
            raise AssertionError("the projection never started")

    def let_through(self, count: int = 1) -> None:
        for _ in range(count):
            self._permits.release()


class _ColdWalkScript:
    """Drives serve_stdio's select loop in fake time while a walk is held.

    The first scan waits until the walk is actually in flight, so what follows
    is a loop running beside a held walk rather than one that got lucky on
    timing. The first two scans deliver a frame, advancing the clock by half
    the timeout the way a frame arriving early cuts a wait short; the rest wait
    the timeout out. ``feed`` runs on each delivering scan so the frame really
    arrives then, instead of every frame being framed by the first read.
    """

    def __init__(
        self,
        clock: list[float],
        builder: _HeldBuilder,
        *,
        scans: int,
        feed: Callable[[], None] = lambda: None,
    ) -> None:
        self.clock = clock
        self.builder = builder
        self.scans = scans
        self.feed = feed
        self.timeouts: list[float] = []

    def select(
        self, rlist: list[Any], _wlist: list[Any], _xlist: list[Any], timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        if len(self.timeouts) >= self.scans:
            # serve_stdio treats a select error as a closed stream and returns.
            raise OSError("the script has run out of scans")
        if not self.timeouts:
            self.builder.await_entry()
        self.timeouts.append(timeout)
        if len(self.timeouts) <= 2:
            self.feed()
            self.clock[0] += timeout / 2
            return list(rlist), [], []
        self.clock[0] += timeout
        return [], [], []


class BridgeProjectionCadenceTests(unittest.TestCase):
    """A projection that costs more than the poll interval must not run back to back.

    Found against the installed bridge on a 444-plan corpus, where one
    projection took ten seconds: polling it every second left the server
    permanently mid-build, so it never returned to the select loop and no
    client ever received a snapshot within its read timeout.

    The cached-projection half below is what the protocol loop still does with
    whatever it is handed. The worker half is what it is handed: the walk runs
    on its own thread, so a request is answered from the last projection while
    the next one runs, and the next one is owed one interval after the last one
    started rather than one interval after it finished.
    """

    def _session(
        self, cost: float
    ) -> tuple[bridge_session.BridgeSession, list[float], list[int]]:
        clock = [0.0]
        builds: list[int] = []

        def build() -> dict[str, Any]:
            builds.append(1)
            clock[0] += cost
            return {
                "digest": "d",
                "row_count": len(builds),
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        session = bridge_session.BridgeSession(
            snapshot_builder=build, clock=lambda: clock[0], poll_interval=1.0
        )
        return session, clock, builds

    def test_interval_grows_to_the_cost_of_the_last_projection(self) -> None:
        session, _, _ = self._session(cost=10.0)

        self.assertEqual(1.0, session.projection_interval())
        session.poll()
        self.assertEqual(10.0, session.projection_interval())

    def test_request_inside_the_interval_reuses_the_polled_projection(self) -> None:
        session, clock, builds = self._session(cost=10.0)
        session.poll()

        clock[0] += 5.0
        session.runs_snapshot()
        self.assertEqual(1, len(builds))

        clock[0] += 6.0
        session.runs_snapshot()
        self.assertEqual(2, len(builds))

    def test_a_request_defers_the_next_poll_by_a_whole_interval(self) -> None:
        session, clock, builds = self._session(cost=10.0)

        session.runs_snapshot()

        self.assertEqual(clock[0] + 10.0, session.next_projection_at())
        self.assertEqual(1, len(builds))

    def test_a_cheap_corpus_still_reprojects_every_poll_interval(self) -> None:
        session, clock, builds = self._session(cost=0.0)
        session.poll()

        clock[0] += 1.5
        session.runs_snapshot()

        self.assertEqual(2, len(builds))

    # ----------------------------------------------------------------
    # The background worker: cadence, freshness, and what never waits.

    def _worker(
        self, cost: float, interval: float = 1.0
    ) -> tuple[bridge_workers.ProjectionWorker, list[float], list[int]]:
        clock = [0.0]
        builds: list[int] = []

        def build() -> dict[str, Any]:
            builds.append(1)
            clock[0] += cost
            return {
                "digest": f"d{len(builds)}",
                "row_count": len(builds),
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        worker = bridge_workers.ProjectionWorker(
            build, interval=interval, clock=lambda: clock[0]
        )
        return worker, clock, builds

    def test_an_overrunning_walk_owes_no_wait_before_the_next_one(self) -> None:
        """The regression: a ten-second walk used to observe change every twenty."""
        worker, clock, _ = self._worker(cost=10.0)

        self.assertEqual(0.0, worker.cycle())
        self.assertEqual(10.0, clock[0])
        self.assertEqual(0.0, worker.cycle())
        self.assertEqual(20.0, clock[0])
        self.assertEqual(10.0, worker.cadence()["observed_interval_seconds"])

    def test_a_walk_inside_the_interval_waits_out_the_declared_cadence(self) -> None:
        worker, clock, _ = self._worker(cost=0.1)

        owed = worker.cycle()

        self.assertAlmostEqual(0.9, owed)
        clock[0] += owed
        worker.cycle()
        self.assertEqual(1.0, worker.cadence()["observed_interval_seconds"])

    def test_the_cadence_reports_the_measured_bound_not_the_declared_floor(
        self,
    ) -> None:
        worker, clock, _ = self._worker(cost=10.0)
        worker.cycle()
        worker.cycle()

        clock[0] += 2.0
        cadence = worker.cadence()

        self.assertEqual(1.0, cadence["interval_seconds"])
        self.assertEqual(10.0, cadence["build_seconds"])
        self.assertEqual(2.0, cadence["age_seconds"])
        # Read at the start of a ten-second walk, ten seconds old when it
        # landed, two seconds old since, and not replaced for another ten.
        self.assertEqual(22.0, cadence["staleness_bound_seconds"])
        self.assertEqual(0, cadence["failed_projections"])

    def test_a_failed_walk_is_counted_and_does_not_replace_the_last_projection(
        self,
    ) -> None:
        clock = [0.0]
        outcomes: list[str] = ["ok", "boom"]

        def build() -> dict[str, Any]:
            clock[0] += 1.0
            if outcomes.pop(0) == "boom":
                raise OSError("the corpus went away")
            return {
                "digest": "d1",
                "row_count": 0,
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        worker = bridge_workers.ProjectionWorker(
            build, interval=1.0, clock=lambda: clock[0]
        )
        worker.cycle()
        worker.cycle()

        self.assertEqual("d1", worker.latest()["digest"])
        self.assertEqual(1, worker.cadence()["failed_projections"])

    def test_a_failed_walk_does_not_retime_the_snapshot_that_stays_published(
        self,
    ) -> None:
        """The regression: a cheap failure made an expensive walk look cheap."""
        clock = [0.0]
        costs: list[float] = [10.0, 0.1]

        def build() -> dict[str, Any]:
            cost = costs.pop(0)
            clock[0] += cost
            if cost == 0.1:
                raise OSError("the corpus went away")
            return {
                "digest": "d1",
                "row_count": 0,
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        worker = bridge_workers.ProjectionWorker(
            build, interval=1.0, clock=lambda: clock[0]
        )
        worker.cycle()
        after_success = worker.cadence()
        worker.cycle()
        cadence = worker.cadence()

        self.assertEqual("d1", worker.latest()["digest"])
        # The published walk cost ten seconds and is now 0.1 seconds old; the
        # failure that aged it must not claim to have built it in 0.1.
        self.assertEqual(10.0, cadence["build_seconds"])
        self.assertEqual(0.1, cadence["age_seconds"])
        self.assertEqual(20.1, cadence["staleness_bound_seconds"])
        self.assertEqual(
            after_success["observed_interval_seconds"],
            cadence["observed_interval_seconds"],
        )
        self.assertEqual(1, cadence["failed_projections"])

    def test_a_request_is_served_from_the_last_walk_while_the_next_one_runs(
        self,
    ) -> None:
        builder = _HeldBuilder()
        worker = bridge_workers.ProjectionWorker(builder, interval=0.0)
        self.addCleanup(builder.let_through, 4)
        self.addCleanup(worker.stop)

        worker.start()
        builder.await_entry()
        builder.let_through()
        builder.await_entry()

        # A walk is in flight and holding. The published one still answers, and
        # answers now: a blocking read would sit here until the cleanup released
        # the builder and the test would time out instead of asserting.
        session = bridge_session.BridgeSession(snapshot_builder=worker.latest)

        self.assertEqual("d1", session.runs_snapshot()["digest"])
        self.assertEqual(2, builder.calls)

    def test_a_cold_request_refuses_retryably_while_the_first_walk_is_held(
        self,
    ) -> None:
        """The regression: a cold snapshot used to block on the whole first walk."""
        builder = _HeldBuilder()
        worker = bridge_workers.ProjectionWorker(builder, interval=60.0)
        self.addCleanup(builder.let_through, 4)
        self.addCleanup(worker.stop)
        session = bridge_session.BridgeSession(snapshot_builder=worker.latest)

        worker.start()
        builder.await_entry()

        # The walk is in flight and holding, so this call returns only because
        # it refuses instead of waiting; a blocking read would sit here until
        # the cleanup released the builder and the test would time out.
        with self.assertRaises(bridge_protocol.BridgeError) as caught:
            session.runs_snapshot()

        self.assertEqual(bridge_protocol.ERROR_INTERNAL, caught.exception.code)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(1, builder.calls)

    def test_the_loop_keeps_framing_while_the_first_walk_is_held(self) -> None:
        """The cold refusal is worth nothing if the loop is stuck behind the walk."""
        clock = [0.0]
        builder = _HeldBuilder()
        # Released only after the loop has returned, so every frame below was
        # framed while the first walk was still held.
        self.addCleanup(builder.let_through, 4)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        arrivals = [
            _request("r1", "runs.snapshot") + b"\n",
            _request("r2", "runs.snapshot") + b"\n",
        ]
        script = _ColdWalkScript(
            clock,
            builder,
            scans=3,
            feed=lambda: os.write(write_fd, arrivals.pop(0)) and None,
        )
        writer = io.StringIO()

        with (
            mock.patch.object(
                bridge_server, "build_runs_snapshot", lambda **_k: builder()
            ),
            mock.patch.object(bridge_server, "select", script),
            os.fdopen(read_fd, "rb") as reader,
        ):
            code = bridge_server.serve_stdio(
                reader=reader,
                writer=writer,
                diagnostics=io.StringIO(),
                include_default_dirs=False,
                poll_interval=1.0,
                clock=lambda: clock[0],
            )

        self.assertEqual(0, code)
        # One walk for the whole loop: neither request, nor the poll after them,
        # started a second one or waited on the first.
        self.assertEqual(1, builder.calls)
        # Each refusal owes a second, so the loop waits rather than spinning on
        # a projection that has not landed.
        self.assertEqual([1.0, 1.0, 1.0], script.timeouts)
        frames = [
            json.loads(line) for line in writer.getvalue().splitlines() if line.strip()
        ]
        self.assertEqual("hello", frames[0]["type"])
        self.assertEqual(["r1", "r2"], [frame["id"] for frame in frames[1:]])
        for frame in frames[1:]:
            self.assertEqual(bridge_protocol.ERROR_INTERNAL, frame["error"]["code"])
            self.assertTrue(frame["error"]["retryable"])

    def test_a_served_snapshot_carries_the_measured_cadence_not_the_floor(
        self,
    ) -> None:
        worker, clock, _ = self._worker(cost=10.0)
        session = bridge_session.BridgeSession(
            snapshot_builder=worker.latest,
            clock=lambda: clock[0],
            projection_cadence=worker.cadence,
        )
        worker.cycle()
        worker.cycle()

        cadence = session.runs_snapshot()["projection"]

        self.assertEqual(1.0, cadence["interval_seconds"])
        self.assertEqual(10.0, cadence["observed_interval_seconds"])
        self.assertEqual(20.0, cadence["staleness_bound_seconds"])

    def test_the_cadence_block_never_moves_the_digest(self) -> None:
        """Measured durations jitter; a corpus that did not change did not change."""
        worker, clock, _ = self._worker(cost=10.0)
        session = bridge_session.BridgeSession(
            snapshot_builder=worker.latest,
            clock=lambda: clock[0],
            projection_cadence=worker.cadence,
        )
        worker.cycle()
        served = session.runs_snapshot()

        clock[0] += 5.0
        again = session.runs_snapshot()

        self.assertNotEqual(served["projection"], again["projection"])
        self.assertEqual(served["digest"], again["digest"])
        self.assertEqual(
            bridge_projection.snapshot_digest(served),
            bridge_projection.snapshot_digest(again),
        )

    def test_a_session_without_a_projector_still_reports_its_own_cadence(self) -> None:
        session, clock, _ = self._session(cost=10.0)
        session.runs_snapshot()

        clock[0] += 3.0
        cadence = session.runs_snapshot()["projection"]

        self.assertEqual(10.0, cadence["build_seconds"])
        self.assertEqual(3.0, cadence["age_seconds"])
        self.assertEqual(10.0, cadence["observed_interval_seconds"])

    def test_a_failed_first_walk_refuses_retryably_rather_than_waiting(self) -> None:
        def build() -> dict[str, Any]:
            raise OSError("the state root vanished")

        worker = bridge_workers.ProjectionWorker(build, interval=60.0)
        session = bridge_session.BridgeSession(snapshot_builder=worker.latest)
        worker.cycle()

        with self.assertRaises(bridge_protocol.BridgeError) as caught:
            session.runs_snapshot()

        self.assertEqual(bridge_protocol.ERROR_INTERNAL, caught.exception.code)
        self.assertTrue(caught.exception.retryable)
        self.assertIn("the state root vanished", caught.exception.message)

    def test_a_stopped_worker_refuses_instead_of_blocking_the_loop(self) -> None:
        worker, _, builds = self._worker(cost=0.0, interval=60.0)
        worker.stop()

        with self.assertRaises(bridge_workers.ProjectionPending):
            worker.latest()

        self.assertEqual([], builds)


if __name__ == "__main__":
    unittest.main()
