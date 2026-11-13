"""Bridge dispatch tests extracted from test_asl_bridge_server_lifecycle."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    bridge_protocol,
    bridge_session,
    intervention,
)

from .asl_bridge_server_support import (
    REPLY_TEXT,
    _Corpus,
    _handshake,
    _request,
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
