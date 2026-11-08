"""Workbench tests extracted from test_asl_bridge_server_lifecycle."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    bridge_protocol,
    bridge_session,
    bridge_workers,
    intervention,
)

from .asl_bridge_server_support import (
    REPLY_TEXT,
    _Corpus,
    _handshake,
    _RecordingServer,
    _request,
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

    def test_the_owner_link_needs_a_contract_matched_handshake(self) -> None:
        """The link's token passes the Workbench's mutating routes.

        Handing it to a client that never proved its contract set would let
        that client skip the mutation gate by asking for the link instead.
        """
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            lifecycle = bridge_workers.WorkbenchLifecycle(
                state_dir=corpus.state_root, server_factory=_RecordingServer
            )
            session = corpus.session(workbench_lifecycle=lifecycle)
            ensure = _request(
                "w1", intervention.ACTION_WORKBENCH, plan_dir=str(plan_dir)
            )

            before = session.handle_frame(ensure)
            mismatched = session.handle_frame(
                _request(
                    "hs-0",
                    bridge_protocol.METHOD_HANDSHAKE,
                    contract_set_id="asl-amx-1.0123456789abcdef",
                )
            )
            after_mismatch = session.handle_frame(ensure)
            started_before_handshake = list(_RecordingServer.instances)
            _handshake(session)
            after = session.handle_frame(ensure)
            lifecycle.stop()

        for refused in (before, after_mismatch):
            assert refused is not None
            self.assertFalse(refused["ok"])
            self.assertEqual(
                bridge_protocol.ERROR_CONTRACT_SET_MISMATCH, refused["error"]["code"]
            )
            self.assertNotIn("token", json.dumps(refused))
        assert mismatched is not None
        self.assertFalse(mismatched["ok"])
        self.assertEqual([], started_before_handshake)
        assert after is not None
        self.assertTrue(after["ok"], after)
        self.assertIn("token=test-token", after["result"]["url"])

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
