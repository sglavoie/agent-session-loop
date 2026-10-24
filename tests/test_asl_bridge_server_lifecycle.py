"""Bridge projection failure backoff, Workbench lifecycle, dispatch, and diagnostics.

Split from tests/test_asl_bridge_server.py."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    bridge_protocol,
    bridge_server,
    bridge_session,
    bridge_workers,
    intervention,
    mailbox_vocabulary,
)

from .asl_bridge_server_support import (
    REPLY_TEXT,
    WAIT,
    _Corpus,
    _handshake,
    _handshake_request,
    _LineWriter,
    _RecordingServer,
    _request,
)

ASL_TEST_LANE = "integration"


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
    real frame arriving early cuts a wait short.
    """

    def __init__(
        self, clock: list[float], *, ready_scans: set[int], scans: int
    ) -> None:
        self.clock = clock
        self.ready_scans = ready_scans
        self.scans = scans
        self.timeouts: list[float] = []

    def select(
        self, rlist: list[Any], _wlist: list[Any], _xlist: list[Any], timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        if len(self.timeouts) >= self.scans:
            # serve_stdio treats a select error as a closed stream and returns.
            raise OSError("the script has run out of scans")
        self.timeouts.append(timeout)
        if len(self.timeouts) in self.ready_scans:
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
        script = _LoopScript(clock, ready_scans={1, 3}, scans=5)
        read_fd, write_fd = os.pipe()
        os.write(
            write_fd,
            _request("r1", "runs.snapshot")
            + b"\n"
            + _request("r2", "runs.snapshot")
            + b"\n",
        )
        os.close(write_fd)
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


class BridgeWorkbenchLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingServer.instances = []

    def test_link_carries_the_token_in_the_query_and_the_plan_in_the_fragment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            lifecycle = bridge_workers.WorkbenchLifecycle(
                state_dir=corpus.state_root, server_factory=_RecordingServer
            )
            session = corpus.session(workbench_lifecycle=lifecycle)

            result = session.workbench_ensure({"plan_dir": str(plan_dir)})

            self.assertEqual(bridge_protocol.WORKBENCH_FOCUS_REPLY, result["focus"])
            self.assertTrue(result["url"].startswith("http://127.0.0.1:54321/?token="))
            query, _, fragment = result["url"].partition("#")
            self.assertIn("token=test-token", query)
            self.assertNotIn("token", fragment)
            self.assertTrue(fragment.startswith("plan?dir="))
            self.assertIn("focus=reply", fragment)
            lifecycle.stop()

    def test_one_server_is_started_lazily_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            lifecycle = bridge_workers.WorkbenchLifecycle(
                state_dir=corpus.state_root, server_factory=_RecordingServer
            )
            session = corpus.session(workbench_lifecycle=lifecycle)

            self.assertEqual([], _RecordingServer.instances)
            first = session.workbench_ensure({"plan_dir": str(plan_dir)})
            second = session.workbench_ensure({"plan_dir": str(plan_dir)})

            self.assertEqual(1, len(_RecordingServer.instances))
            self.assertEqual(first["url"], second["url"])
            self.assertEqual(0, _RecordingServer.instances[0].kwargs["port"])
            self.assertEqual("127.0.0.1", _RecordingServer.instances[0].kwargs["host"])
            lifecycle.stop()

    def test_stop_shuts_the_server_down(self) -> None:
        lifecycle = bridge_workers.WorkbenchLifecycle(server_factory=_RecordingServer)
        lifecycle.ensure()

        lifecycle.stop()
        lifecycle.stop()

        self.assertEqual(1, _RecordingServer.instances[0].shutdowns)
        self.assertEqual(1, _RecordingServer.instances[0].closes)

    def test_the_listener_notices_a_stop_well_inside_the_client_grace(self) -> None:
        """A slow poll turns every clean bridge exit into a kill.

        The client closes stdin and terminates a bridge that has not exited
        within a fraction of a second, and the stdlib default of half a second
        is longer than that whole budget.
        """
        lifecycle = bridge_workers.WorkbenchLifecycle(server_factory=_RecordingServer)
        lifecycle.ensure()

        lifecycle.stop()

        self.assertEqual(
            [bridge_protocol.WORKBENCH_POLL_INTERVAL_SECONDS],
            _RecordingServer.instances[0].poll_intervals,
        )
        self.assertLess(bridge_protocol.WORKBENCH_POLL_INTERVAL_SECONDS, 0.1)

    def test_unsupported_focus_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            lifecycle = bridge_workers.WorkbenchLifecycle(
                server_factory=_RecordingServer
            )
            session = corpus.session(workbench_lifecycle=lifecycle)

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                session.workbench_ensure(
                    {"plan_dir": str(plan_dir), "focus": "not-a-panel"}
                )

            self.assertEqual(
                bridge_protocol.ERROR_INVALID_REQUEST, caught.exception.code
            )
            self.assertEqual([], _RecordingServer.instances)

    def test_session_without_a_lifecycle_reports_the_workbench_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()

            with self.assertRaises(bridge_protocol.BridgeError) as caught:
                corpus.session().workbench_ensure({"plan_dir": str(plan_dir)})

            self.assertEqual(
                bridge_protocol.ERROR_WORKBENCH_UNAVAILABLE, caught.exception.code
            )


class BridgeDispatchTests(unittest.TestCase):
    def test_mutating_methods_answer_over_the_frame_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            _handshake(session)

            response = session.handle_frame(
                _request(
                    "r1",
                    intervention.ACTION_REPLY,
                    plan_dir=str(plan_dir),
                    operation_id="op-1",
                    action_token=row["action_token"],
                    text=REPLY_TEXT,
                )
            )

            self.assertIsNotNone(response)
            assert response is not None
            self.assertTrue(response["ok"])
            self.assertEqual("r1", response["id"])
            self.assertEqual(
                bridge_protocol.OPERATION_ACCEPTED, response["result"]["status"]
            )
            self.assertNotIn(REPLY_TEXT, json.dumps(response))

    def test_missing_operation_id_is_an_invalid_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            session = corpus.session()
            _handshake(session)

            response = session.handle_frame(
                _request("r1", intervention.ACTION_REPLY, plan_dir=str(plan_dir))
            )

            assert response is not None
            self.assertFalse(response["ok"])
            self.assertEqual(
                bridge_protocol.ERROR_INVALID_REQUEST, response["error"]["code"]
            )

    def test_the_mutation_gate_runs_before_the_request_is_parsed(self) -> None:
        """An unauthorized client is refused, not told how to fix its params."""
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()

            response = corpus.session().handle_frame(
                _request("r1", intervention.ACTION_REPLY, plan_dir=str(plan_dir))
            )

            assert response is not None
            self.assertFalse(response["ok"])
            self.assertEqual(
                bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, response["error"]["code"]
            )
            self.assertFalse(response["error"]["retryable"])

    def test_unknown_method_still_reports_unknown_method(self) -> None:
        session = bridge_session.BridgeSession(
            snapshot_builder=lambda: {"digest": "d", "row_count": 0, "rows": []}
        )

        response = session.handle_frame(_request("r1", "run.explode"))

        assert response is not None
        self.assertEqual(
            bridge_protocol.ERROR_UNKNOWN_METHOD, response["error"]["code"]
        )


class BridgeDiagnosticsTests(unittest.TestCase):
    def test_serve_stdio_keeps_reply_text_out_of_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            read_fd, write_fd = os.pipe()
            writer = _LineWriter()
            diagnostics = io.StringIO()

            def serve() -> None:
                with os.fdopen(read_fd, "rb") as reader:
                    bridge_server.serve_stdio(
                        reader=reader,
                        writer=writer,
                        diagnostics=diagnostics,
                        state_dir=corpus.state_root,
                        plan_roots=(corpus.plans_root,),
                        include_default_dirs=False,
                        poll_interval=3600.0,
                    )

            server = threading.Thread(target=serve, name="serve-stdio", daemon=True)
            server.start()
            try:
                writer.wait_for('"type":"hello"', WAIT)
                # The first projection is built off the protocol loop, so a
                # handshake can legitimately arrive before there is a digest to
                # name. That refusal is retryable, and retrying is what a real
                # client does: the digest is a function of the corpus alone, so
                # every attempt names the snapshot it is about to be served.
                for attempt in range(200):
                    request_id = f"hs-{attempt}"
                    os.write(
                        write_fd,
                        _handshake_request(corpus.snapshot()["digest"], request_id)
                        + b"\n",
                    )
                    settled = writer.wait_for(f'"id":"{request_id}"', WAIT)
                    if '"ok":true' in settled:
                        break
                    time.sleep(0.05)
                else:  # pragma: no cover - the worker never published
                    self.fail("the bridge never produced a first projection")

                os.write(
                    write_fd,
                    _request(
                        "r1",
                        intervention.ACTION_REPLY,
                        plan_dir=str(plan_dir),
                        operation_id="op-1",
                        action_token=row["action_token"],
                        text=REPLY_TEXT,
                    )
                    + b"\n",
                )
                writer.wait_for('"id":"r1"', WAIT)
            finally:
                os.close(write_fd)
                server.join(timeout=WAIT)

            self.assertFalse(server.is_alive())
            self.assertNotIn(REPLY_TEXT, diagnostics.getvalue())
            self.assertNotIn(REPLY_TEXT, writer.getvalue())
            self.assertIn('"ok":true', writer.getvalue())
            self.assertIn(
                mailbox_vocabulary.KIND_REPLY,
                [line["kind"] for line in corpus.mailbox_lines(plan_dir)],
            )


if __name__ == "__main__":
    unittest.main()
