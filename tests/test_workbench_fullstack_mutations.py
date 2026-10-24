"""Mutations settled once at the boundary, and archive mutations."""

from __future__ import annotations

from typing import Any
from unittest import mock

from agent_session_loop import (
    intervention,
    workbench_actions,
    workbench_archive,
    workbench_operations,
)
from asl_workbench import connector_runtime as portable_runtime
from asl_workbench import (
    operations,
)
from tests.support_workbench_fullstack import (
    CONNECTOR_ID,
    DRAIN_STEPS,
    HEARTBEAT_SECONDS,
    TARGET_ID,
    inline,
)
from tests.support_workbench_fullstack_case import FullStackCase

ASL_TEST_LANE = "integration"

# ---------------------------------------------------------------------------
# A mutation is settled once
# ---------------------------------------------------------------------------


class MutationBoundaryTests(FullStackCase):
    """Submission, dispatch, settlement -- and the restart in the middle."""

    mutations = True

    def bridge_token(self) -> str:
        """The freshness proof ASL issues for alpha, minted on the Mac.

        Minted here rather than read back off the Pi because the Pi has no copy
        to read: the retained schema deliberately has no column for a freshness
        proof, so the token the Mac published in the snapshot is dropped on
        ingest and the store's ``command_target`` carries identifiers only. The
        proof reaches the operator from the machine that issued it and travels
        back up with the submission, and that is the direction this exercises --
        ASL is the only end that can say whether the token still matches the run
        it was issued against, and it is the end that checks.
        """
        facts = intervention.inspect_run(
            self.corpus.plan_dir("alpha"), state_dir=self.corpus.state_dir
        )
        return intervention.action_token(facts)

    def submit(self, **overrides: Any) -> tuple[int, Any]:
        """One ``stop-after-current`` over HTTP, journalled and handed down."""
        body: dict[str, Any] = {
            "operation_id": "op-stop-1",
            "plan_key": self.corpus.plan_keys["alpha"],
            "run_key": self.corpus.run_keys["alpha"],
            "bridge_token": self.bridge_token(),
        }
        body.update(overrides)
        return self.request("/api/control/stop-after-current", method="POST", body=body)

    def test_a_submission_is_journalled_before_it_is_handed_down(self) -> None:
        self.publish()
        status, body = self.submit()
        self.assertEqual(202, status, body)
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertEqual(operations.OPERATION_PENDING, record["status"])
        assert self.submissions is not None
        self.assertEqual(1, len(self.submissions.handed))

    def test_a_resubmitted_id_is_one_command_rather_than_two(self) -> None:
        self.publish()
        self.submit()
        status, body = self.submit()
        self.assertEqual(202, status, body)
        assert self.submissions is not None
        # Handed down once. The second call is the first row read back.
        self.assertEqual(1, len(self.submissions.handed))

    def test_the_settlement_asl_reached_arrives_back_in_the_journal(self) -> None:
        self.publish()
        self.submit()
        self.idle(60.0)
        self.publish()
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertIn(
            record["status"],
            {
                operations.OPERATION_CONSUMED,
                operations.OPERATION_ACCEPTED,
                operations.OPERATION_REJECTED,
                operations.OPERATION_EXPIRED,
            },
        )
        self.assertNotEqual(operations.OPERATION_PENDING, record["status"])

    def test_a_stale_proof_is_admitted_by_the_pi_and_refused_by_asl(self) -> None:
        # The split the two ends are built around. The Pi cannot tell a stale
        # proof from a current one -- it holds no copy -- so it admits the
        # command and journals it. ASL redeems the proof against the run as it
        # is now, refuses, and the refusal comes back as the settlement. The
        # operator is told no by the only end that could have known.
        self.publish()
        status, body = self.submit(bridge_token="stale" * 8)
        self.assertEqual(202, status, body)
        self.idle(60.0)
        self.publish()
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertEqual(operations.OPERATION_REJECTED, record["status"])

    def test_a_report_lost_at_the_dispatch_boundary_leaves_it_unsettled(self) -> None:
        self.publish()
        self.submit()
        self.link.losing = True
        self.idle(60.0)
        self.publish()
        self.link.losing = False
        record = self.store.operation("op-stop-1")
        assert record is not None
        # The Mac carried the command out and the report did not arrive, which
        # is the one state the operator must not be told is finished.
        self.assertIn(
            record["status"],
            {operations.OPERATION_PENDING, operations.OPERATION_DISPATCHED},
        )

    def test_a_restarted_connector_closes_the_row_out_without_rerunning_it(
        self,
    ) -> None:
        self.publish()
        self.submit()
        self.link.losing = True
        self.idle(60.0)
        self.publish()
        self.link.losing = False
        assert self.performer is not None
        before = self.performed()

        # A new connector over the same corpus and the same link: the process
        # the operator restarted. `recover` reads the journal the performer
        # wrote and reports, and it re-attempts nothing.
        revived = portable_runtime.ObservationConnector(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            source=self.source,
            channel=self.link,
            heartbeat_interval=HEARTBEAT_SECONDS,
            responder=self.responder,
            performer=self.performer,
            run_observation=inline,
            run_answer=inline,
            run_mutation=inline,
        )
        self.idle(120.0)
        for _ in range(DRAIN_STEPS):
            revived.step(self.clock.tick())
        self.assertEqual(before, self.performed())
        record = self.store.operation("op-stop-1")
        assert record is not None
        self.assertNotEqual(operations.OPERATION_PENDING, record["status"])

    def performed(self) -> int:
        """How many journal rows the performer has written for this corpus."""
        found = 0
        for plan_dir in self.corpus.plan_dirs:
            rows = workbench_operations.read_operations(
                plan_dir, state_dir=self.corpus.state_dir
            )
            found += len(rows)
        return found


class ArchiveMutationTests(FullStackCase):
    """Plan and archive actions settle against the Mac's synthetic corpus."""

    reads = True
    mutations = True

    def settle(self, operation_id: str) -> dict[str, Any]:
        self.idle(60.0)
        self.publish()
        record = self.store.operation(operation_id)
        assert record is not None
        return record

    def test_plan_archive_moves_an_idle_plan_and_can_be_read_back(self) -> None:
        self.publish()
        key = self.corpus.plan_keys["bravo"]
        status, body = self.request(
            "/api/plan/archive",
            method="POST",
            body={
                "operation_id": "op-archive-bravo",
                "plan_key": key,
                "confirm_plan_key": key,
            },
        )
        self.assertEqual(202, status, body)
        self.assertNotIn("run_key", body["operation"])
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-archive-bravo")["status"]
        )
        self.assertFalse(self.corpus.plan_dir("bravo").exists())
        status, archive = self.request("/api/archive")
        self.assertEqual(200, status, archive)
        self.assertEqual(3, len(archive["entries"]))

    def test_remote_launch_uses_the_mac_default_profile(self) -> None:
        self.publish()
        key = self.corpus.plan_keys["bravo"]
        status, body = self.request(
            "/api/launch",
            method="POST",
            body={"operation_id": "op-launch-bravo", "plan_key": key},
        )
        self.assertEqual(202, status, body)
        fake = mock.Mock(
            status=operations.OPERATION_CONSUMED,
            operation={},
            facts=mock.Mock(run_id=None),
        )
        with mock.patch.object(
            workbench_actions, "launch_run", return_value=fake
        ) as launch:
            self.assertEqual(
                operations.OPERATION_CONSUMED,
                self.settle("op-launch-bravo")["status"],
            )
        self.assertEqual(self.corpus.plan_dir("bravo"), launch.call_args.args[0])
        self.assertIsNone(launch.call_args.kwargs["profile"])

        status, refusal = self.request(
            "/api/launch",
            method="POST",
            body={
                "operation_id": "op-launch-override",
                "plan_key": key,
                "profile": {"cwd": "/tmp"},
            },
        )
        self.assertEqual(400, status, refusal)

    def test_restore_purge_and_retention_are_receipted(self) -> None:
        self.publish()
        archive_id = "20260731T090000Z-delta"
        status, body = self.request(
            "/api/archive/restore",
            method="POST",
            body={"operation_id": "op-restore-delta", "archive_id": archive_id},
        )
        self.assertEqual(202, status, body)
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-restore-delta")["status"]
        )
        status, receipt = self.request("/api/archive?operation_id=op-restore-delta")
        self.assertEqual(200, status, receipt)
        self.assertEqual("consumed", receipt["operation"]["status"])

        archive_id = "20260916T090000Z-echo"
        status, body = self.request(
            "/api/archive/delete",
            method="POST",
            body={
                "operation_id": "op-purge-echo",
                "archive_ids": [archive_id],
                "confirm_count": 1,
            },
        )
        self.assertEqual(202, status, body)
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-purge-echo")["status"]
        )
        self.assertFalse(
            (
                workbench_archive.archive_root(self.corpus.state_dir) / archive_id
            ).exists()
        )

        status, body = self.request(
            "/api/archive/settings",
            method="POST",
            body={"operation_id": "op-retention", "archive_retention_days": 45},
        )
        self.assertEqual(202, status, body)
        self.assertEqual(
            operations.OPERATION_CONSUMED, self.settle("op-retention")["status"]
        )
        status, archive = self.request("/api/archive")
        self.assertEqual(200, status, archive)
        self.assertEqual(45, archive["archive_retention_days"])
