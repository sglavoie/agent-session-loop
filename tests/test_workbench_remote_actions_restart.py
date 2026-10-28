"""Restart settlement of open rows, and proof that no frame names this disk."""

from __future__ import annotations

import json
import os
import socket
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    mailbox_appenders,
    workbench_operations,
)
from asl_workbench import remote_actions
from asl_workbench.connector_lanes import _Answer
from asl_workbench.connector_support import MAX_QUEUED_ANSWERS
from asl_workbench.operations import (
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_NO_RESULT,
    DETAIL_REPLY_WITNESSED,
    METHOD_ABORT,
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
)
from asl_workbench.transport import (
    CAPABILITY_MUTATE,
    KIND_DETAIL,
    KIND_OPERATION,
    KIND_RECEIPT,
    request_envelope,
)
from tests.support import (
    _WORKBENCH_DEAD_PID,
)
from tests.support_workbench_remote_actions import (
    CONNECTOR_ID,
    NOW,
    PLAN_KEY,
    REPLY_TEXT,
    RUN_ID,
    SESSION_ID,
    TARGET_ID,
    LinkChannel,
    PerformerTestCase,
    StubPerformer,
    drive,
    make_connector,
    submit_body,
)

ASL_TEST_PORTABLE = True


class RestartSettlementTests(unittest.TestCase):
    """Two words, and the distinction between them is the whole point."""

    def test_a_row_that_never_reached_dispatch_certainly_did_not_run(self) -> None:
        settlement = remote_actions.restart_settlement(
            {
                "operation_id": "op-1",
                "status": OPERATION_PENDING,
                "method": "plan.abort",
            },
            now=NOW,
        )

        assert settlement is not None
        self.assertEqual(OPERATION_EXPIRED, settlement.status)
        self.assertEqual(DETAIL_CONNECTOR_RESTARTED, settlement.detail_code)
        self.assertEqual("plan.abort", settlement.method)

    def test_a_row_that_reached_dispatch_may_have_run(self) -> None:
        for status in (OPERATION_DISPATCHED, OPERATION_ACCEPTED):
            with self.subTest(status=status):
                settlement = remote_actions.restart_settlement(
                    {"operation_id": "op-1", "status": status}, now=NOW
                )
                assert settlement is not None
                self.assertEqual(OPERATION_UNCONFIRMED, settlement.status)

    def test_a_row_that_already_says_everything_true_is_left_alone(self) -> None:
        for status in (OPERATION_UNCONFIRMED, OPERATION_CONSUMED, OPERATION_REJECTED):
            with self.subTest(status=status):
                self.assertIsNone(
                    remote_actions.restart_settlement(
                        {"operation_id": "op-1", "status": status}, now=NOW
                    )
                )

    def test_a_settlement_frame_names_the_plan_by_key(self) -> None:
        payload = remote_actions.settlement_payload(
            remote_actions.Settlement(
                operation_id="op-1",
                status=OPERATION_UNCONFIRMED,
                detail_code=DETAIL_CONNECTOR_RESTARTED,
                method=METHOD_ABORT,
            ),
            observed_at=NOW,
            target={"plan_key": PLAN_KEY},
        )

        self.assertEqual(PLAN_KEY, payload["target"]["plan_key"])
        self.assertEqual(OPERATION_UNCONFIRMED, payload["status"])

    def test_recovery_runs_once_and_reports_what_it_closed(self) -> None:
        channel = LinkChannel()
        performer = StubPerformer(
            settlements=[
                {
                    "operation_id": "op-old",
                    "status": OPERATION_UNCONFIRMED,
                    "observed_at": NOW,
                    "detail_code": DETAIL_CONNECTOR_RESTARTED,
                }
            ]
        )
        connector = make_connector(channel=channel, performer=performer)

        self.assertEqual(1, connector.recover(NOW))
        self.assertEqual(0, connector.recover(NOW))
        drive(connector, 4)

        (report,) = channel.operations()
        self.assertEqual("op-old", report["payload"]["operation_id"])
        self.assertEqual(OPERATION_UNCONFIRMED, report["payload"]["status"])
        self.assertEqual(1, performer.recovered)

    def test_recovery_delivers_every_report_past_the_read_queue_limit(self) -> None:
        channel = LinkChannel()
        count = MAX_QUEUED_ANSWERS + 1
        performer = StubPerformer(
            settlements=[
                {
                    "operation_id": f"op-old-{index}",
                    "status": OPERATION_UNCONFIRMED,
                    "observed_at": NOW,
                    "detail_code": DETAIL_CONNECTOR_RESTARTED,
                }
                for index in range(count)
            ]
        )
        connector = make_connector(channel=channel, performer=performer)

        self.assertEqual(count, connector.recover(NOW))
        self.assertEqual(count, len(connector._queue.answers))
        drive(connector, count + 4)
        self.assertEqual(
            {f"op-old-{index}" for index in range(count)},
            {frame["payload"]["operation_id"] for frame in channel.operations()},
        )

    def test_completed_mutation_report_survives_a_full_read_queue(self) -> None:
        connector = make_connector(performer=StubPerformer())
        connector._queue.answers = [
            _Answer(request_id=f"read-{index}", kind=KIND_DETAIL, payload={})
            for index in range(MAX_QUEUED_ANSWERS)
        ]
        connector._mutation_result = _Answer(
            request_id=None,
            kind=KIND_OPERATION,
            payload={"operation_id": "op-finished", "status": OPERATION_UNCONFIRMED},
            capability=CAPABILITY_MUTATE,
        )

        connector._collect_mutation()

        self.assertIsNone(connector._mutation_result)
        self.assertEqual(MAX_QUEUED_ANSWERS + 1, len(connector._queue.answers))
        self.assertEqual(
            "op-finished", connector._queue.answers[-1].payload["operation_id"]
        )

    def test_mutation_refusal_survives_queue_pressure_and_link_failure(self) -> None:
        connector = make_connector(performer=StubPerformer())
        connector._queue.answers = [
            _Answer(request_id=f"read-{index}", kind=KIND_DETAIL, payload={})
            for index in range(MAX_QUEUED_ANSWERS)
        ]
        refusal = _Answer(
            request_id="submit-op-refused",
            kind=KIND_RECEIPT,
            payload={"outcome": "refused"},
            capability=CAPABILITY_MUTATE,
        )

        connector._enqueue(refusal)
        self.assertEqual(MAX_QUEUED_ANSWERS + 1, len(connector._queue.answers))
        connector._fail(NOW)

        self.assertEqual(connector._queue.answers, [refusal])
        self.assertEqual(refusal.payload["outcome"], "refused")

    def test_a_connector_that_cannot_act_recovers_nothing(self) -> None:
        connector = make_connector(performer=None, body_source=None)

        self.assertEqual(0, connector.recover(NOW))

    def test_a_performer_that_raises_during_recovery_is_recorded_not_fatal(
        self,
    ) -> None:
        class Broken(StubPerformer):
            def recover(self, *, now: float) -> list[dict[str, Any]]:
                raise OSError("the journal is unreadable")

        connector = make_connector(performer=Broken())

        self.assertEqual(0, connector.recover(NOW))
        assert connector.mutation_fault is not None
        self.assertIn("OSError", connector.mutation_fault)


# --------------------------------------------------------------------------
# A real corpus
# --------------------------------------------------------------------------


class RestartTests(PerformerTestCase):
    """What a new connector owes the rows its predecessor left open."""

    _seq = 1

    def journal_path(self) -> Path:
        return workbench_operations.journal_path_for_plan(self.plan_dir, self.state_dir)

    def orphan(self, operation_id: str, status: str, **overrides: Any) -> None:
        """Write one open row the way a connector that then died would have.

        Written straight into the journal rather than through
        :func:`~agent_session_loop.workbench_operations.admit_operation`,
        because admitting records *this* process as the owner and the whole
        point of the row is that its owner is gone.
        """
        record: dict[str, Any] = {
            "operation_id": operation_id,
            "command_id": operation_id,
            "method": METHOD_STOP_AFTER_CURRENT,
            "status": status,
            "plan_key": self.plan_key,
            "run_id": RUN_ID,
            "requested_at": NOW - 10.0,
            "deliver_by": NOW + 30.0,
            "pid": _WORKBENCH_DEAD_PID,
            "hostname": socket.gethostname(),
        }
        record.update(overrides)
        line = json.dumps(
            {
                "version": workbench_operations.OPERATIONS_JOURNAL_VERSION,
                "seq": self._seq,
                "operation": {
                    key: value for key, value in record.items() if value is not None
                },
            },
            sort_keys=True,
        )
        self._seq += 1
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def test_a_row_journalled_and_never_dispatched_expires(self) -> None:
        self.write_state()
        self.orphan("op-1", OPERATION_PENDING)

        settled = self.performer.recover(now=NOW)

        (payload,) = settled
        self.assertEqual(OPERATION_EXPIRED, payload["status"])
        self.assertEqual(DETAIL_CONNECTOR_RESTARTED, payload["detail_code"])
        self.assertEqual(self.plan_key, payload["target"]["plan_key"])
        self.assertEqual(OPERATION_EXPIRED, self.row("op-1")["status"])

    def test_a_dispatched_row_becomes_unconfirmed_and_is_never_retried(self) -> None:
        self.write_state()
        self.orphan("op-2", OPERATION_DISPATCHED)

        settled = self.performer.recover(now=NOW)

        (payload,) = settled
        self.assertEqual(OPERATION_UNCONFIRMED, payload["status"])
        self.assertEqual(DETAIL_CONNECTOR_RESTARTED, payload["detail_code"])
        # Nothing was queued: recovery settles, it does not act.
        self.assertEqual([], self.queued())

    def test_recovery_reports_a_consumed_in_turn_mailbox_reply(self) -> None:
        """A receipt is stronger evidence than the dead connector process."""
        self.write_state()
        question = mailbox_appenders.append_question(
            self.plan_dir,
            "May the fixture continue?",
            state_dir=self.state_dir,
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=1,
        )
        reply = mailbox_appenders.append_reply(
            self.plan_dir,
            question.id,
            REPLY_TEXT,
            state_dir=self.state_dir,
        )
        mailbox_appenders.append_receipt(
            self.plan_dir,
            question.id,
            reply.id,
            state_dir=self.state_dir,
        )
        self.orphan(
            "op-reply",
            OPERATION_ACCEPTED,
            method=METHOD_QUESTION_REPLY,
            question_id=question.id,
            dispatched_at=NOW - 5.0,
        )

        settled = self.performer.recover(now=NOW)

        (payload,) = settled
        self.assertEqual(OPERATION_CONSUMED, payload["status"])
        self.assertEqual(DETAIL_REPLY_WITNESSED, payload["detail_code"])
        self.assertEqual(OPERATION_CONSUMED, self.row("op-reply")["status"])

    def test_a_row_the_local_surface_submitted_is_not_the_connectors_to_settle(
        self,
    ) -> None:
        """No ``command_id`` means a browser asked for it, not the far end."""
        self.write_state()
        self.orphan("op-3", OPERATION_PENDING, command_id=None)

        self.assertEqual([], self.performer.recover(now=NOW))
        self.assertEqual(OPERATION_PENDING, self.row("op-3")["status"])

    def test_a_row_whose_owner_is_alive_is_happening_not_lost(self) -> None:
        self.write_state()
        self.orphan("op-4", OPERATION_DISPATCHED, pid=os.getpid())

        self.assertEqual([], self.performer.recover(now=NOW))
        self.assertEqual(OPERATION_DISPATCHED, self.row("op-4")["status"])

    def test_a_row_on_another_host_is_left_to_that_host(self) -> None:
        self.write_state()
        self.orphan("op-5", OPERATION_PENDING, hostname="some-other-mac")

        self.assertEqual([], self.performer.recover(now=NOW))

    def test_recovery_reports_a_settlement_the_connector_can_send(self) -> None:
        self.write_state()
        self.orphan("op-6", OPERATION_DISPATCHED)
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=self.performer)

        self.assertEqual(1, connector.recover(NOW))
        drive(connector, 4)

        (report,) = channel.operations()
        self.assertEqual("op-6", report["payload"]["operation_id"])
        self.assertEqual(OPERATION_UNCONFIRMED, report["payload"]["status"])
        self.assertNotIn(str(self.root), json.dumps(report))


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


class PrivacyTests(PerformerTestCase):
    """The far end may act on a plan and must not learn where it lives."""

    def test_no_frame_the_mutation_lane_builds_names_this_disk(self) -> None:
        self.live_run()
        channel = LinkChannel()
        connector = make_connector(channel=channel, performer=self.performer)
        channel.carry(
            request_envelope(
                payload=submit_body(
                    target={"plan_key": self.plan_key, "run_id": RUN_ID},
                    bridge_token=self.token(),
                ),
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=NOW,
                request_id="m1",
                deadline=NOW + 30.0,
            )
        )

        drive(connector, 8)

        rendered = json.dumps(channel.frames)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("plan-alpha", rendered)
        self.assertTrue(channel.operations())

    def test_a_delivered_operation_frame_carries_only_the_closed_vocabulary(
        self,
    ) -> None:
        request = self.request(plan_key=self.plan_key)
        payload = remote_actions.operation_payload(
            request,
            remote_actions.MutationResult(
                status=OPERATION_REJECTED,
                detail_code=DETAIL_NO_RESULT,
                reason="asl-private-word",
            ),
            observed_at=NOW,
            dispatched_at=NOW,
            settled_at=NOW + 1.0,
        )

        self.assertEqual(OPERATION_REJECTED, payload["status"])
        self.assertEqual(DETAIL_NO_RESULT, payload["detail_code"])
        self.assertNotIn("asl-private-word", json.dumps(payload))
        self.assertEqual(self.plan_key, payload["target"]["plan_key"])


if __name__ == "__main__":
    unittest.main()
