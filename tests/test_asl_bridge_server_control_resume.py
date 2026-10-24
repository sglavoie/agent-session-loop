"""Bridge control, resume, and operation-window behavior.

Split from tests/test_asl_bridge_server.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    bridge_protocol,
    bridge_session,
    bridge_session_state,
    control_queue,
    intervention,
    state,
    workbench_launch,
)

from .asl_bridge_server_support import (
    REPLY_TEXT,
    _Corpus,
)

ASL_TEST_LANE = "integration"


class BridgeControlTests(unittest.TestCase):
    def test_stop_after_current_queues_the_operation_id_as_the_command_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)

            result = corpus.session().run_stop_after_current(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-stop",
                    "action_token": row["action_token"],
                }
            )

            self.assertEqual("op-stop", result["command_id"])
            drained = control_queue.drain_queue(plan_dir, state_dir=corpus.state_root)
            self.assertEqual(1, len(drained.accepted))
            self.assertEqual(
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                drained.accepted[0].command,
            )
            self.assertEqual("op-stop", drained.accepted[0].id)

    def test_abort_uses_the_default_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)

            corpus.session().run_abort(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-abort",
                    "action_token": row["action_token"],
                }
            )

            drained = control_queue.drain_queue(plan_dir, state_dir=corpus.state_root)
            command = drained.accepted[0]
            self.assertEqual(control_queue.COMMAND_REQUEST_ABORT_TURN, command.command)
            self.assertEqual(
                control_queue.DEFAULT_ABORT_GRACE_SECONDS, command.grace_seconds
            )

    def test_observed_command_settles_as_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            session.run_stop_after_current(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-stop",
                    "action_token": row["action_token"],
                }
            )

            self.assertEqual([], session.poll_operations())
            corpus.record_command_observed(plan_dir, "op-stop")
            updates = session.poll_operations()

            self.assertEqual(
                bridge_protocol.OPERATION_CONSUMED, updates[0]["data"]["status"]
            )

    def test_command_drained_after_the_run_ends_settles_as_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.running_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            session.run_abort(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-abort",
                    "action_token": row["action_token"],
                }
            )

            control_queue.drain_queue(plan_dir, state_dir=corpus.state_root)
            # Draining alone is not conclusive; the runner may still log it.
            self.assertEqual([], session.poll_operations())

            (
                state.plan_state_dir(plan_dir, corpus.state_root) / state.LOCK_FILE_NAME
            ).unlink()
            updates = session.poll_operations()

            self.assertEqual(
                bridge_protocol.OPERATION_REJECTED, updates[0]["data"]["status"]
            )


class BridgeResumeTests(unittest.TestCase):
    def test_resume_streams_the_reply_and_asks_for_persisted_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            row = corpus.row_for(plan_dir)
            calls: list[dict[str, Any]] = []

            def _capture(target: Any, **kwargs: Any) -> Any:
                calls.append({"plan_dir": target, **kwargs})
                return workbench_launch.LaunchResult(
                    pid=4242,
                    argv=("python3", "-m", "agent_session_loop", "run"),
                    plan_dir=Path(target),
                    profile_payload={},
                    reply_provided=True,
                    reply_length=len(kwargs.get("reply_text") or ""),
                    reply_origin="stdin",
                    state_dir=None,
                )

            original = workbench_launch.launch_plan
            workbench_launch.launch_plan = _capture  # type: ignore[assignment]
            try:
                result = corpus.session().run_resume(
                    {
                        "plan_dir": str(plan_dir),
                        "operation_id": "op-resume",
                        "action_token": row["action_token"],
                        "text": REPLY_TEXT,
                    }
                )
            finally:
                workbench_launch.launch_plan = original  # type: ignore[assignment]

            self.assertEqual(bridge_protocol.OPERATION_ACCEPTED, result["status"])
            self.assertEqual(1, len(calls))
            self.assertTrue(calls[0]["resume"])
            self.assertEqual(REPLY_TEXT, calls[0]["reply_text"])
            self.assertNotIn(REPLY_TEXT, json.dumps(result))

    def test_one_request_spawns_at_most_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            row = corpus.row_for(plan_dir)
            session = corpus.session()
            spawns: list[Any] = []

            def _capture(target: Any, **kwargs: Any) -> Any:
                spawns.append(target)
                return workbench_launch.LaunchResult(
                    pid=4242,
                    argv=(),
                    plan_dir=Path(target),
                    profile_payload={},
                    reply_provided=True,
                    reply_length=1,
                    reply_origin="stdin",
                    state_dir=None,
                )

            params = {
                "plan_dir": str(plan_dir),
                "operation_id": "op-resume",
                "action_token": row["action_token"],
                "text": REPLY_TEXT,
            }
            original = workbench_launch.launch_plan
            workbench_launch.launch_plan = _capture  # type: ignore[assignment]
            try:
                first = session.run_resume(params)
                second = session.run_resume(params)
            finally:
                workbench_launch.launch_plan = original  # type: ignore[assignment]

            self.assertEqual(1, len(spawns))
            self.assertEqual(first, second)

    def _resume(
        self, corpus: _Corpus, session: bridge_session.BridgeSession, plan_dir: Path
    ) -> dict[str, Any]:
        """Resume a paused plan without spawning anything."""
        row = corpus.row_for(plan_dir)
        original = workbench_launch.launch_plan
        workbench_launch.launch_plan = lambda target, **kwargs: (
            workbench_launch.LaunchResult(  # type: ignore[assignment]
                pid=1,
                argv=(),
                plan_dir=Path(target),
                profile_payload={},
                reply_provided=True,
                reply_length=1,
                reply_origin="stdin",
                state_dir=None,
            )
        )
        try:
            return session.run_resume(
                {
                    "plan_dir": str(plan_dir),
                    "operation_id": "op-resume",
                    "action_token": row["action_token"],
                    "text": REPLY_TEXT,
                }
            )
        finally:
            workbench_launch.launch_plan = original  # type: ignore[assignment]

    def test_a_lifted_pause_alone_does_not_settle_the_resume(self) -> None:
        """The flipped pin.

        This test previously asserted the opposite: that a plan no longer being
        paused settled the resume as ``consumed``. That is a fact about the
        plan, not about the operator's reply -- *any* run starting on the plan
        lifts the pause, including one an operator launched from the Workbench a
        second earlier -- so the old rule credited this reply with whatever
        happened next. An unwitnessed resume now stays ``accepted`` and reaches
        a terminal status through the witness below or through the TTL, which is
        "nobody can say" rather than a claim.
        """
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            session = corpus.session()
            self._resume(corpus, session, plan_dir)

            self.assertEqual([], session.poll_operations())
            corpus.write_state(
                plan_dir,
                {"ok": None, "paused": False, "run_id": "20260619T140000Z-1"},
            )

            self.assertEqual([], session.poll_operations())
            self.assertEqual(
                bridge_protocol.OPERATION_ACCEPTED,
                session.operations["op-resume"].status,
            )

    def test_resume_settles_as_consumed_when_a_run_records_reading_the_reply(
        self,
    ) -> None:
        """The only evidence that is about the reply rather than about the plan.

        The consuming run is deliberately a *different* run id than the paused
        one the operation named: ``asl run --resume`` mints a new run id, so
        requiring a match would refuse every successful resume there is.
        """
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            session = corpus.session()
            self._resume(corpus, session, plan_dir)

            self.assertEqual([], session.poll_operations())
            corpus.record_reply_provided(plan_dir, run_id="20260619T150000Z-2")
            updates = session.poll_operations()

            self.assertEqual(
                bridge_protocol.OPERATION_CONSUMED, updates[0]["data"]["status"]
            )
            self.assertEqual("20260619T140000Z-1", updates[0]["data"]["run_id"])

    def test_a_reply_recorded_before_the_resume_settles_nothing(self) -> None:
        """A log is not evidence of everything that is in it.

        The previous run read a reply too, and its record sits in the same log.
        The mark taken before this resume is what keeps that record from
        settling this operation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            corpus.record_reply_provided(plan_dir, run_id="20260619T130000Z-0")
            session = corpus.session()
            self._resume(corpus, session, plan_dir)

            self.assertEqual([], session.poll_operations())
            self.assertEqual(
                bridge_protocol.OPERATION_ACCEPTED,
                session.operations["op-resume"].status,
            )

    def test_a_mailbox_reply_does_not_settle_a_resume(self) -> None:
        """Two deliveries, two operations, and neither settles the other."""
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            session = corpus.session()
            self._resume(corpus, session, plan_dir)

            corpus.record_reply_provided(
                plan_dir, run_id="20260619T150000Z-2", origin="mailbox"
            )

            self.assertEqual([], session.poll_operations())
            self.assertEqual(
                bridge_protocol.OPERATION_ACCEPTED,
                session.operations["op-resume"].status,
            )

    def test_a_resume_still_superseded_by_another_run_holding_the_plan(self) -> None:
        """Still paused under someone else's run id is somebody else's plan now."""
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()
            session = corpus.session()
            self._resume(corpus, session, plan_dir)

            corpus.write_state(
                plan_dir,
                {"ok": False, "paused": True, "run_id": "20260619T160000Z-3"},
            )
            updates = session.poll_operations()

            self.assertEqual(
                bridge_protocol.OPERATION_SUPERSEDED, updates[0]["data"]["status"]
            )


class BridgeOperationWindowTests(unittest.TestCase):
    def test_window_evicts_settled_operations_but_never_live_ones(self) -> None:
        clock = [0.0]
        session = bridge_session.BridgeSession(
            snapshot_builder=lambda: {"digest": "d", "row_count": 0, "rows": []},
            clock=lambda: clock[0],
        )
        for index in range(bridge_protocol.MAX_OPERATIONS + 10):
            operation = bridge_session_state.Operation(
                operation_id=f"op-{index}",
                method=intervention.ACTION_REPLY,
                plan_dir="/plans/alpha",
                row_id="row",
                accepted_at=clock[0],
            )
            if index % 2 == 0:
                operation.settle(bridge_protocol.OPERATION_CONSUMED)
            session.operations[operation.operation_id] = operation
        session._evict()

        live = [op for op in session.operations.values() if not op.terminal]
        self.assertEqual(bridge_protocol.MAX_OPERATIONS // 2 + 5, len(live))
        self.assertLessEqual(len(session.operations), bridge_protocol.MAX_OPERATIONS)

    def test_unsettled_operation_expires_after_the_ttl(self) -> None:
        clock = [0.0]
        session = bridge_session.BridgeSession(
            snapshot_builder=lambda: {"digest": "d", "row_count": 0, "rows": []},
            clock=lambda: clock[0],
        )
        session.operations["op-1"] = bridge_session_state.Operation(
            operation_id="op-1",
            method=intervention.ACTION_REPLY,
            plan_dir="/plans/alpha",
            row_id="row",
            accepted_at=0.0,
        )

        clock[0] = bridge_protocol.OPERATION_TTL_SECONDS + 1.0
        updates = session.poll_operations()

        self.assertEqual(
            bridge_protocol.OPERATION_EXPIRED, updates[0]["data"]["status"]
        )


if __name__ == "__main__":
    unittest.main()
