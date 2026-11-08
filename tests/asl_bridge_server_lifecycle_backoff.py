"""Backoff tests extracted from test_asl_bridge_server_lifecycle."""

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
    bridge_protocol,
    bridge_server,
    bridge_session,
    bridge_workers,
)

from .asl_bridge_server_support import (
    WAIT,
    _LineWriter,
    _request,
)


class _FailingProjector:
    """Publishes one projection and then fails every walk after it.

    That is the exact shape the regression needs: the success stamps a
    deadline, and every failure after it leaves that deadline in the past.
    """

    def __init__(self) -> None:
        self.calls = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def latest(self) -> dict[str, Any]:
        self.calls += 1
        if self.calls > 1:
            raise OSError("the corpus went away")
        return {
            "digest": "d1",
            "row_count": 0,
            "rows": [],
            "warnings": [],
            "truncated": False,
        }

    def cadence(self) -> dict[str, Any]:
        return bridge_workers.projection_cadence(
            interval=1.0,
            observed=1.0,
            build_seconds=0.0,
            age=0.0,
            failures=max(0, self.calls - 1),
        )


class _LoopScript:
    """Drives serve_stdio's select loop in fake time, recording every timeout.

    Standing in for ``select`` is what makes the timeout the loop actually
    passes observable; nothing else in the process reports it. A scan that
    finds the reader ready advances the clock by half the timeout, the way a
    real frame arriving early cuts a wait short. ``feed`` runs on each ready
    scan, so a test can make that frame actually arrive then rather than having
    written every frame up front, where one read would frame them all.
    """

    def __init__(
        self,
        clock: list[float],
        *,
        ready_scans: set[int],
        scans: int,
        feed: Callable[[], None] = lambda: None,
    ) -> None:
        self.clock = clock
        self.ready_scans = ready_scans
        self.scans = scans
        self.feed = feed
        self.timeouts: list[float] = []

    def select(
        self, rlist: list[Any], _wlist: list[Any], _xlist: list[Any], timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        if len(self.timeouts) >= self.scans:
            # serve_stdio treats a select error as a closed stream and returns.
            raise OSError("the script has run out of scans")
        self.timeouts.append(timeout)
        if len(self.timeouts) in self.ready_scans:
            self.feed()
            self.clock[0] += timeout / 2
            return list(rlist), [], []
        self.clock[0] += timeout
        return [], [], []


class BridgeProjectionFailureBackoffTests(unittest.TestCase):
    """A projection that fails must not be retried with no delay at all.

    A failure publishes nothing, so the deadline of the last *successful*
    projection stays where it was — in the past. Without a deadline of its own
    the loop selected with timeout zero, immediately retried the same failing
    walk, and spent a core on it while the corpus stayed broken.
    """

    def _session(
        self, outcomes: list[str]
    ) -> tuple[bridge_session.BridgeSession, list[float], list[str]]:
        clock = [0.0]
        calls: list[str] = []

        def build() -> dict[str, Any]:
            outcome = outcomes.pop(0) if outcomes else "boom"
            calls.append(outcome)
            if outcome == "boom":
                raise OSError("the corpus went away")
            return {
                "digest": "d",
                "row_count": 0,
                "rows": [],
                "warnings": [],
                "truncated": False,
            }

        session = bridge_session.BridgeSession(
            snapshot_builder=build, clock=lambda: clock[0], poll_interval=1.0
        )
        return session, clock, calls

    def test_the_first_projection_comes_due_without_a_snapshot_request(
        self,
    ) -> None:
        # A client that only listens never polls the session itself, so the
        # first deadline must hold still rather than recede with the clock.
        session, clock, _ = self._session(["ok"])
        self.assertEqual(1.0, session.select_timeout())

        clock[0] += 0.75
        self.assertEqual(0.25, session.select_timeout())
        clock[0] += 0.25
        self.assertEqual(0.0, session.select_timeout())
        self.assertIsNotNone(session.poll())

    def test_a_failed_projection_owes_a_bounded_nonzero_delay(self) -> None:
        session, clock, _ = self._session(["ok"])
        session.poll()

        clock[0] += 1.0
        self.assertEqual(0.0, session.select_timeout())
        session.poll()

        self.assertEqual(
            bridge_protocol.PROJECTION_RETRY_INTERVAL_SECONDS, session.select_timeout()
        )

    def test_consecutive_failures_never_scan_with_a_zero_timeout(self) -> None:
        session, clock, calls = self._session(["ok"])
        session.poll()
        timeouts: list[float] = []

        for _ in range(5):
            timeouts.append(session.select_timeout())
            clock[0] += timeouts[-1]
            session.poll()

        self.assertEqual([1.0] * 5, timeouts)
        self.assertEqual(["ok"] + ["boom"] * 5, calls)

    def test_the_retry_deadline_only_ever_moves_forward(self) -> None:
        session, clock, _ = self._session(["ok"])
        session.poll()
        session.poll()
        deferred = session.next_projection_at()

        clock[0] += 0.25
        session.poll()

        self.assertGreater(session.next_projection_at(), deferred)

    def test_a_recovered_projection_returns_to_the_declared_cadence(self) -> None:
        session, clock, _ = self._session(["ok", "boom", "ok"])
        session.poll()
        clock[0] += 1.0
        session.poll()

        clock[0] += session.select_timeout()
        session.poll()

        self.assertEqual(2.0, clock[0])
        self.assertEqual(3.0, session.next_projection_at())

    def test_a_failed_walk_owes_the_worker_a_wait_even_when_it_overruns(self) -> None:
        clock = [0.0]

        def build() -> dict[str, Any]:
            clock[0] += 10.0
            raise OSError("the corpus went away")

        worker = bridge_workers.ProjectionWorker(
            build, interval=1.0, clock=lambda: clock[0]
        )

        owed = worker.cycle()

        self.assertEqual(bridge_protocol.PROJECTION_RETRY_INTERVAL_SECONDS, owed)
        self.assertEqual(1, worker.cadence()["failed_projections"])

    def test_the_event_loop_keeps_answering_between_failed_projections(self) -> None:
        clock = [0.0]
        projector = _FailingProjector()
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        arrivals = [
            _request("r1", "runs.snapshot") + b"\n",
            _request("r2", "runs.snapshot") + b"\n",
        ]
        script = _LoopScript(
            clock,
            ready_scans={1, 3},
            scans=5,
            feed=lambda: os.write(write_fd, arrivals.pop(0)) and None,
        )
        writer = io.StringIO()

        with (
            mock.patch.object(
                bridge_server, "ProjectionWorker", lambda *_a, **_k: projector
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
        # Every scan but the first two polls a failing projection, and the scan
        # after each failure still waits, so the loop never spins.
        self.assertEqual([1.0, 1.0, 1.0, 0.5, 1.0], script.timeouts)
        # Four walks for five scans: the second request was answered from the
        # last good projection without attempting another one.
        self.assertEqual(4, projector.calls)
        frames = [
            json.loads(line) for line in writer.getvalue().splitlines() if line.strip()
        ]
        self.assertEqual("hello", frames[0]["type"])
        self.assertEqual(["r1", "r2"], [frame["id"] for frame in frames[1:]])
        self.assertEqual(
            ["d1", "d1"], [frame["result"]["digest"] for frame in frames[1:]]
        )


class BridgePipelinedFrameTests(unittest.TestCase):
    """Frames written back to back are each answered without more input.

    One read can frame several requests at once, and ``select`` reports only
    what the kernel still holds, so the loop must answer what it has already
    framed before it waits. The client here keeps its end open and writes
    nothing further: a server that waited on ``select`` for the second frame
    would never answer it.
    """

    def test_a_second_buffered_frame_is_answered_while_the_client_stays_open(
        self,
    ) -> None:
        read_fd, write_fd = os.pipe()
        writer = _LineWriter()

        def serve() -> None:
            with os.fdopen(read_fd, "rb") as reader:
                bridge_server.serve_stdio(
                    reader=reader,
                    writer=writer,
                    diagnostics=io.StringIO(),
                    include_default_dirs=False,
                    poll_interval=3600.0,
                )

        server = threading.Thread(target=serve, name="serve-stdio", daemon=True)
        server.start()
        try:
            writer.wait_for('"type":"hello"', WAIT)
            os.write(
                write_fd,
                _request("p1", "runs.snapshot")
                + b"\n"
                + _request("p2", "runs.snapshot")
                + b"\n",
            )

            writer.wait_for('"id":"p1"', WAIT)
            writer.wait_for('"id":"p2"', WAIT)
        finally:
            os.close(write_fd)
            server.join(WAIT)
        self.assertFalse(server.is_alive())
