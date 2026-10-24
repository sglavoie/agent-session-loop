# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F403, F405, I001
"""Portable launch/reply interpretation and reconciliation tests."""

# ruff: noqa: F403, F405
from tests.workbench_launch_reply_support import *


# Integration because the claim is that two surfaces are one path: half of
# it drives the shared action directly and half drives a live workbench
# server, and the point is that both land in the same journal.
ASL_TEST_LANE = "integration"


class PortableInterpretationTests(unittest.TestCase):
    """What a launch, a reply, and a resume mean, read off bytes alone."""

    def test_every_action_maps_to_exactly_one_method(self) -> None:
        for action, method in actions.METHOD_FOR_ACTION.items():
            with self.subTest(action=action):
                self.assertEqual(method, actions.method_for_action(action))
                self.assertEqual(action, actions.ACTION_FOR_METHOD[method])

    def test_a_resume_contends_with_a_launch_rather_than_with_a_reply(self) -> None:
        """A resume starts a process, so it must not be admissible beside one."""
        self.assertEqual(
            operations.method_scope(actions.METHOD_FOR_ACTION[actions.ACTION_LAUNCH]),
            operations.method_scope(actions.METHOD_FOR_ACTION[actions.ACTION_RESUME]),
        )
        self.assertNotEqual(
            operations.method_scope(actions.METHOD_FOR_ACTION[actions.ACTION_RESUME]),
            operations.method_scope(actions.METHOD_FOR_ACTION[actions.ACTION_REPLY]),
        )

    def test_an_unknown_action_is_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(ValueError):
            actions.method_for_action("detonate")

    def test_a_refusal_before_the_spawn_proves_nothing_ran(self) -> None:
        for reason in actions.REFUSING_LAUNCH_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(
                    operations.OPERATION_REJECTED, actions.launch_closure(reason)
                )

    def test_a_killed_child_is_unconfirmed_rather_than_rejected(self) -> None:
        """The distinction the whole journal exists to record.

        ``process-failed`` is raised from both sides of the spawn. On the far
        side a child existed, ran, and may have reached the plan before it was
        killed, so "certainly did not run" is a claim nobody can make.
        """
        self.assertEqual(
            operations.OPERATION_REJECTED,
            actions.launch_closure(actions.LAUNCH_PROCESS_FAILED),
        )
        self.assertEqual(
            operations.OPERATION_UNCONFIRMED,
            actions.launch_closure(actions.LAUNCH_PROCESS_FAILED, spawned=True),
        )

    def test_each_reply_bearing_method_is_settled_by_its_own_delivery(self) -> None:
        """A stdin reply and a mailbox reply are two deliveries, not one."""
        self.assertEqual(
            frozenset(operations.REPLY_BEARING_METHODS),
            frozenset(actions.CONSUMING_ORIGIN),
        )
        self.assertEqual(
            actions.REPLY_ORIGIN_STDIN,
            actions.consuming_origin(operations.METHOD_REPLY),
        )
        self.assertEqual(
            actions.REPLY_ORIGIN_MAILBOX,
            actions.consuming_origin(operations.METHOD_QUESTION_REPLY),
        )
        self.assertIsNone(actions.consuming_origin(operations.METHOD_LAUNCH))

    def test_a_reply_record_names_the_run_that_read_it(self) -> None:
        evidence = actions.reply_record(
            _reply_line(), origin=actions.REPLY_ORIGIN_STDIN
        )
        assert evidence is not None
        self.assertTrue(evidence.consumed)
        self.assertEqual(RESUMED_RUN_ID, evidence.run_id)

    def test_a_reply_that_arrived_another_way_is_somebody_elses(self) -> None:
        self.assertIsNone(
            actions.reply_record(
                _reply_line(origin=actions.REPLY_ORIGIN_MAILBOX),
                origin=actions.REPLY_ORIGIN_STDIN,
            )
        )

    def test_a_reply_to_another_question_is_somebody_elses(self) -> None:
        self.assertIsNone(
            actions.reply_record(
                _reply_line(question_id="q-other"),
                origin=actions.REPLY_ORIGIN_STDIN,
                question_id="q-mine",
            )
        )

    def test_a_record_written_before_the_dispatch_settles_nothing(self) -> None:
        """The reconciler's weaker attribution, and its direction.

        A row written by a process that is gone has no byte mark to fall back
        on -- only the dispatch moment it persisted. A record older than that
        answers something else.
        """
        line = _reply_line(created_at=STALE_MOMENT)
        moment = datetime.fromisoformat(STALE_MOMENT).timestamp() + 3600.0
        self.assertIsNone(
            actions.reply_record(line, origin=actions.REPLY_ORIGIN_STDIN, since=moment)
        )
        self.assertIsNotNone(
            actions.reply_record(line, origin=actions.REPLY_ORIGIN_STDIN, since=0.0)
        )

    def test_a_record_nobody_can_place_in_time_settles_nothing(self) -> None:
        line = _reply_line(created_at="not-a-timestamp")
        self.assertIsNone(
            actions.reply_record(line, origin=actions.REPLY_ORIGIN_STDIN, since=0.0)
        )

    def test_unparsable_records_are_absence_of_evidence_not_an_error(self) -> None:
        for line in (b"{", b"not json at all\n", b'{"name": "turn_started"}'):
            with self.subTest(line=line):
                self.assertIsNone(
                    actions.reply_record(line, origin=actions.REPLY_ORIGIN_STDIN)
                )

    def test_a_scan_never_steps_over_a_record_still_being_written(self) -> None:
        whole = _reply_line(run_id="20260619T120000Z-0", origin="mailbox")
        partial = _reply_line()[:40]
        evidence, stride = actions.scan_replies(
            whole + partial, origin=actions.REPLY_ORIGIN_STDIN
        )
        self.assertFalse(evidence.consumed)
        # The mark stops at the end of the last whole record, so the next look
        # reads the half-written one from its beginning.
        self.assertEqual(len(whole), stride)

    def test_a_scan_reports_the_first_record_that_settles(self) -> None:
        chunk = _reply_line(origin="mailbox") + _reply_line()
        evidence, stride = actions.scan_replies(
            chunk, origin=actions.REPLY_ORIGIN_STDIN
        )
        self.assertTrue(evidence.consumed)
        self.assertEqual(RESUMED_RUN_ID, evidence.run_id)
        self.assertEqual(len(chunk), stride)

    def test_the_profile_field_list_matches_the_launcher(self) -> None:
        """The portable half names the profile ASL actually launches under."""
        self.assertEqual(
            sorted(workbench_registry_models.LaunchProfile().to_payload()),
            sorted(actions.LAUNCH_PROFILE_FIELDS),
        )
        self.assertIn(
            actions.PROFILE_FIELD_BOUND_TO_WORKSPACE, actions.LAUNCH_PROFILE_FIELDS
        )
        self.assertEqual(
            ("harness_version",),
            actions.unknown_profile_fields({"cwd": ".", "harness_version": "9"}),
        )


class ReconciliationTests(unittest.TestCase):
    """What an interrupted reply-bearing mutation is worth, after the fact."""

    def _record(self, *, method: str, status: str) -> dict[str, Any]:
        record = operations.new_record(
            operation_id="op-1",
            method=method,
            target=operations.OperationTarget(plan_key="plan-abc", selector=""),
            now=1000.0,
            run_id=RUN_ID,
        )
        record["status"] = status
        record["dispatched_at"] = 1000.0
        return record

    def test_a_witnessed_reply_advances_to_consumed(self) -> None:
        record = self._record(
            method=operations.METHOD_REPLY, status=operations.OPERATION_ACCEPTED
        )
        outcome = operations.reconcile(
            record,
            evidence=operations.Evidence(
                run_active=True,
                run_id=RESUMED_RUN_ID,
                reply_consumed=True,
                consuming_run_id=RESUMED_RUN_ID,
            ),
            now=1001.0,
        )
        assert outcome is not None
        self.assertEqual(operations.OPERATION_CONSUMED, outcome.status)
        self.assertEqual(operations.DETAIL_REPLY_WITNESSED, outcome.detail_code)

    def test_a_live_run_alone_never_settles_a_reply(self) -> None:
        """The rule this session exists to remove, stated as a test.

        A run being there -- even the very run the operation named -- says
        nothing about whether the operator's words were read. For a launch it
        is conclusive, and the contrast is the point.
        """
        evidence = operations.Evidence(run_active=True, run_id=RUN_ID)
        reply = operations.reconcile(
            self._record(
                method=operations.METHOD_REPLY, status=operations.OPERATION_ACCEPTED
            ),
            evidence=evidence,
            now=1001.0,
        )
        self.assertIsNone(reply)

        launch = operations.reconcile(
            self._record(
                method=operations.METHOD_LAUNCH, status=operations.OPERATION_ACCEPTED
            ),
            evidence=evidence,
            now=1001.0,
        )
        assert launch is not None
        self.assertEqual(operations.OPERATION_CONSUMED, launch.status)

    def test_a_witness_settles_a_reply_even_under_a_live_owner(self) -> None:
        """Otherwise a long-lived server never finishes anything it delivered.

        The owner-alive rule protects work in progress. A run's record of
        having read the reply is not a claim about the owner's progress, and
        the local Workbench -- one process that answers a question and then
        goes back to serving pages -- is alive for hours afterwards.
        """
        record = self._record(
            method=operations.METHOD_QUESTION_REPLY,
            status=operations.OPERATION_ACCEPTED,
        )
        evidence = operations.Evidence(
            owner_alive=True,
            run_active=True,
            run_id=RUN_ID,
            reply_consumed=True,
            consuming_run_id=RUN_ID,
        )
        outcome = operations.reconcile(record, evidence=evidence, now=1001.0)
        assert outcome is not None
        self.assertEqual(operations.OPERATION_CONSUMED, outcome.status)

    def test_a_live_owner_mid_attempt_is_still_left_alone(self) -> None:
        """The exception is the witness, not the reply-bearing method."""
        self.assertIsNone(
            operations.reconcile(
                self._record(
                    method=operations.METHOD_QUESTION_REPLY,
                    status=operations.OPERATION_DISPATCHED,
                ),
                evidence=operations.Evidence(owner_alive=True, run_active=True),
                now=1001.0,
            )
        )

    def test_a_pending_row_is_never_witnessed(self) -> None:
        """Nothing was dispatched, so no run can have read it.

        The guard matters because ``pending`` settles as ``expired`` -- the one
        status that says "certainly did not run" -- and a witness let through
        here would have that claim drawn from evidence about somebody else.
        """
        record = self._record(
            method=operations.METHOD_QUESTION_REPLY,
            status=operations.OPERATION_PENDING,
        )
        outcome = operations.reconcile(
            record,
            evidence=operations.Evidence(owner_alive=True, reply_consumed=True),
            now=1001.0,
        )
        self.assertIsNone(outcome)

    def test_an_unwitnessed_reply_becomes_unconfirmed_once_overdue(self) -> None:
        record = self._record(
            method=operations.METHOD_QUESTION_REPLY,
            status=operations.OPERATION_ACCEPTED,
        )
        record["deliver_by"] = 1005.0
        outcome = operations.reconcile(
            record,
            evidence=operations.Evidence(run_active=True, run_id=RUN_ID),
            now=2000.0,
        )
        assert outcome is not None
        self.assertEqual(operations.OPERATION_UNCONFIRMED, outcome.status)

    def test_a_dispatched_reply_is_accepted_by_its_witness(self) -> None:
        outcome = operations.reconcile(
            self._record(
                method=operations.METHOD_REPLY, status=operations.OPERATION_DISPATCHED
            ),
            evidence=operations.Evidence(
                run_active=True,
                run_id=RESUMED_RUN_ID,
                reply_consumed=True,
                consuming_run_id=RESUMED_RUN_ID,
            ),
            now=1001.0,
        )
        assert outcome is not None
        self.assertEqual(operations.OPERATION_ACCEPTED, outcome.status)
        self.assertEqual(operations.DETAIL_REPLY_WITNESSED, outcome.detail_code)
