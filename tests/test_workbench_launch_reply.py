"""One path for a launch, a reply, and a paused resume, whoever asked.

Session 6's claim is that the local Workbench and the connector now *do the
same thing*: "local and connector-facing operations have matching behavior,
refuse stale targets, and cannot spawn or answer twice after a lost response."
Four properties add up to that, and each has its own class here.

**The interpretation is portable and it is one interpretation.**
:class:`PortableInterpretationTests` holds :mod:`asl_workbench.actions`, which
is where the three claims a mutation can make are finally spelled apart: a
process was created, the plan left the paused state, a worker consumed the
reply. Only the third is about the operator's answer. This module reads bytes
and returns what they establish, so the Pi's reading of an imported log and the
Mac's reading of a live one cannot diverge.

**A reply is settled by evidence, not by a side effect.**
:class:`ReconciliationTests` holds the consequence: a launch is ``consumed``
when a process exists, because a process is the whole of what a launch
promised, and a reply-bearing mutation stops at ``accepted`` until a run
records having read it. The old rule -- resume, then watch for the pause to
lift -- is refused by name there, because the plan leaving the paused state is
a fact about the plan.

**Both surfaces are the same code.** :class:`SharedActionTests` drives
:mod:`agent_session_loop.workbench_actions` directly, and
:class:`SurfaceParityTests` proves the HTTP route reaches the same journal: a
retry that arrives at the other surface replays rather than spawning again.

**A run outlives its launcher.** :class:`LaunchIsolationTests` holds the one
line that keeps a Ctrl-C in the operator's terminal, or a connector going away,
from taking down the run it started.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    intervention,
    mailbox,
    plans,
    state,
    workbench,
    workbench_actions,
    workbench_operations,
)
from asl_workbench import actions, operations
from tests.support import (
    _FakePopen,
    _http_request,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)
from tests.workbench_support import WorkbenchAPIFixture

RUN_ID = "20260619T140000Z-1"
RESUMED_RUN_ID = "20260619T150000Z-2"
QUESTION_BODY = "Which column should v1 carry?"
REPLY_TEXT = "carry the v1 column"

#: A record older than the dispatch it is offered against answers something
#: else, so anything written for a live journal has to be stamped *now*: the
#: durable reconciler attributes by wall clock, not by a byte mark.
STALE_MOMENT = "2026-06-19T12:00:00+00:00"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _reply_line(
    *,
    run_id: str = RESUMED_RUN_ID,
    origin: str = actions.REPLY_ORIGIN_STDIN,
    question_id: str | None = None,
    created_at: str | None = None,
) -> bytes:
    """One ``reply_provided`` record, shaped the way ``run_loop`` writes it."""
    if created_at is None:
        created_at = _now_iso()
    source: dict[str, Any] = {
        "run_id": run_id,
        "session_id": "1",
        "turn_index": 2,
        "reply_length": len(REPLY_TEXT),
        "origin": origin,
    }
    if question_id is not None:
        source["question_id"] = question_id
    record = {
        "created_at": created_at,
        "name": actions.REPLY_PROVIDED_EVENT,
        "payload": {
            "run_id": run_id,
            "turn_index": 2,
            "session_id": "1",
            "reply_length": len(REPLY_TEXT),
            "source": source,
        },
        "version": 1,
    }
    return json.dumps(record).encode("utf-8") + b"\n"


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
            sorted(workbench.LaunchProfile().to_payload()),
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


class _PlanFixture(unittest.TestCase):
    """A plan, a state root, and the two shapes a reply can be aimed at."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state_dir = root / "state"
        self.plan_dir = root / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans.canonicalize_path(self.plan_dir)

    def rows(self) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            self.canonical, state_dir=self.state_dir
        )

    def row(self, operation_id: str) -> dict[str, Any]:
        record = workbench_operations.read_operation(
            self.canonical, operation_id, state_dir=self.state_dir
        )
        assert record is not None
        return record

    def launch_result(self, *, reply: bool = False) -> workbench.LaunchResult:
        return workbench.LaunchResult(
            pid=4242,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical,
            profile_payload=workbench.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=len(REPLY_TEXT) if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def make_paused(self) -> None:
        runs = state.plan_state_dir(self.canonical, self.state_dir) / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        last_message = runs / "last-message.txt"
        last_message.write_text(QUESTION_BODY, encoding="utf-8")
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "run_id": RUN_ID,
                "harness": "claude",
                "turns": [
                    {
                        "session_id": "1",
                        "turn_index": 1,
                        "last_message_path": str(last_message),
                        "harness": "claude",
                    }
                ],
            },
        )

    def make_waiting(self) -> str:
        """A live run holding one open mailbox question."""
        _write_lock_file(
            self.canonical,
            self.state_dir,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        question = mailbox.append_question(
            self.canonical,
            QUESTION_BODY,
            state_dir=self.state_dir,
            run_id=RUN_ID,
            session_id="2",
            turn_index=3,
        )
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "ok": None,
                "paused": False,
                "run_id": RUN_ID,
                "harness": "claude",
                "active_turn": {"session_id": "2", "turn_index": 3},
                "waiting_for_reply": {"question_id": question.id},
            },
        )
        return question.id

    def append_reply_provided(self, **kwargs: Any) -> None:
        path = workbench_operations.events_path_for_plan(self.canonical, self.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(_reply_line(**kwargs))


class SharedActionTests(_PlanFixture):
    """The one path every surface takes, driven directly."""

    def test_a_launch_is_finished_by_the_process_it_promised(self) -> None:
        with mock.patch.object(
            workbench, "launch_plan", return_value=self.launch_result()
        ) as patched:
            outcome = workbench_actions.launch_run(
                self.canonical, operation_id="op-launch", state_dir=self.state_dir
            )
        patched.assert_called_once()
        self.assertEqual(operations.OPERATION_CONSUMED, outcome.status)
        self.assertEqual(operations.METHOD_LAUNCH, outcome.method)

    def test_a_resume_waits_for_a_run_to_say_it_read_the_reply(self) -> None:
        """Accepted, not consumed: a ``Popen`` is not a reader."""
        self.make_paused()
        with mock.patch.object(
            workbench,
            "resume_plan_with_reply",
            return_value=self.launch_result(reply=True),
        ):
            outcome = workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
        self.assertEqual(operations.OPERATION_ACCEPTED, outcome.status)
        self.assertEqual(operations.METHOD_REPLY, outcome.method)

    def test_a_mailbox_reply_is_appended_and_left_accepted(self) -> None:
        question_id = self.make_waiting()
        outcome = workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        self.assertEqual(operations.OPERATION_ACCEPTED, outcome.status)
        self.assertEqual(question_id, outcome.question_id)
        self.assertIsNotNone(outcome.closure_id)
        view = mailbox.read_mailbox(self.canonical, state_dir=self.state_dir)
        replies = [
            message for message in view.messages if message.kind == mailbox.KIND_REPLY
        ]
        self.assertEqual(1, len(replies))

    def test_a_reply_witnessed_later_reconciles_to_consumed(self) -> None:
        """The operation's own end, read out of the run's record of reading it."""
        question_id = self.make_waiting()
        workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        self.append_reply_provided(
            origin=actions.REPLY_ORIGIN_MAILBOX, question_id=question_id
        )
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        row = self.row("op-reply")
        self.assertEqual(operations.OPERATION_CONSUMED, row["status"])
        self.assertEqual(operations.DETAIL_REPLY_WITNESSED, row["detail_code"])

    def test_a_mailbox_receipt_reconciles_an_in_turn_reply_to_consumed(self) -> None:
        """An in-turn ask writes a receipt without starting another turn."""
        question_id = self.make_waiting()
        outcome = workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        assert outcome.closure_id is not None
        mailbox.append_receipt(
            self.canonical,
            question_id,
            outcome.closure_id,
            state_dir=self.state_dir,
        )

        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)

        row = self.row("op-reply")
        self.assertEqual(operations.OPERATION_CONSUMED, row["status"])
        self.assertEqual(operations.DETAIL_REPLY_WITNESSED, row["detail_code"])

    def test_a_reply_to_another_question_leaves_this_one_open(self) -> None:
        question_id = self.make_waiting()
        workbench_actions.reply_to_question(
            self.canonical,
            text=REPLY_TEXT,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        self.append_reply_provided(
            origin=actions.REPLY_ORIGIN_MAILBOX, question_id="q-somebody-else"
        )
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_ACCEPTED, self.row("op-reply")["status"])

    def test_a_resent_id_replays_instead_of_spawning_again(self) -> None:
        self.make_paused()
        with mock.patch.object(
            workbench,
            "resume_plan_with_reply",
            return_value=self.launch_result(reply=True),
        ) as patched:
            first = workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
            second = workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
        patched.assert_called_once()
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(1, len(self.rows()))

    def test_a_named_run_that_moved_is_refused_without_any_token(self) -> None:
        """A surface that carries no snapshot still cannot act on a stale target.

        Forcing the browser to acquire an action token would be a new
        requirement on a surface that never had one. Naming a run *is* a claim
        about that run, and the claim is checked either way.
        """
        self.make_paused()
        with (
            mock.patch.object(workbench, "resume_plan_with_reply") as patched,
            self.assertRaises(intervention.InterventionError) as caught,
        ):
            workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
                run_id="20990101T000000Z-9",
            )
        self.assertEqual(intervention.REASON_RUN_MISMATCH, caught.exception.reason)
        patched.assert_not_called()
        # Refused before admission, so the id it carried is still spendable.
        self.assertEqual([], self.rows())

    def test_a_question_that_is_not_open_is_refused_without_any_token(self) -> None:
        self.make_waiting()
        with self.assertRaises(intervention.InterventionError) as caught:
            workbench_actions.reply_to_question(
                self.canonical,
                text=REPLY_TEXT,
                operation_id="op-reply",
                state_dir=self.state_dir,
                question_id="q-not-open",
            )
        self.assertEqual(intervention.REASON_QUESTION_MISMATCH, caught.exception.reason)
        self.assertEqual([], self.rows())

    def test_a_reply_to_a_plan_with_no_run_is_refused(self) -> None:
        with self.assertRaises(intervention.InterventionError) as caught:
            workbench_actions.reply_to_question(
                self.canonical,
                text=REPLY_TEXT,
                operation_id="op-reply",
                state_dir=self.state_dir,
            )
        self.assertEqual(intervention.REASON_NO_ACTIVE_RUN, caught.exception.reason)

    def test_a_guard_refusal_closes_the_row_rather_than_holding_the_plan(self) -> None:
        """An open row holds the plan's launch scope; a withdrawn one must not.

        The row is ``cancelled`` rather than ``rejected`` because nothing was
        attempted -- and because ``pending -> rejected`` is not a transition the
        vocabulary has, so writing it would fail silently and leave the row
        open forever.
        """
        with (
            mock.patch.object(workbench, "launch_plan") as patched,
            self.assertRaises(workbench_actions.ActionRefused) as caught,
        ):
            workbench_actions.launch_run(
                self.canonical,
                operation_id="op-launch",
                state_dir=self.state_dir,
                guard=lambda facts: ("run-already-active", "somebody has it"),
            )
        self.assertEqual("run-already-active", caught.exception.reason)
        patched.assert_not_called()
        self.assertEqual(
            operations.OPERATION_CANCELLED, self.row("op-launch")["status"]
        )
        self.assertEqual(
            [],
            workbench_operations.open_operations(
                self.canonical, state_dir=self.state_dir
            ),
        )
        # And the plan is free: a new submission is admitted rather than
        # refused by the withdrawn one.
        with mock.patch.object(
            workbench, "launch_plan", return_value=self.launch_result()
        ):
            again = workbench_actions.launch_run(
                self.canonical, operation_id="op-launch-2", state_dir=self.state_dir
            )
        self.assertEqual(operations.OPERATION_CONSUMED, again.status)

    def test_a_launch_refused_before_the_spawn_is_closed_as_rejected(self) -> None:
        error = workbench.LaunchError(
            workbench.LAUNCH_REASON_INVALID_PROFILE, "no such harness"
        )
        with (
            mock.patch.object(workbench, "launch_plan", side_effect=error),
            self.assertRaises(workbench.LaunchError),
        ):
            workbench_actions.launch_run(
                self.canonical,
                operation_id="op-launch",
                state_dir=self.state_dir,
            )
        row = self.row("op-launch")
        self.assertEqual(operations.OPERATION_REJECTED, row["status"])
        self.assertEqual(operations.DETAIL_LAUNCH_REFUSED, row["detail_code"])

    def test_a_child_killed_after_the_spawn_is_left_uncertain(self) -> None:
        """The one failure that must not be recorded as "did not run"."""
        error = workbench.LaunchError(
            workbench.LAUNCH_REASON_PROCESS_FAILED,
            "failed to stream reply text to child stdin",
            spawned=True,
        )
        self.make_paused()
        with (
            mock.patch.object(workbench, "resume_plan_with_reply", side_effect=error),
            self.assertRaises(workbench.LaunchError),
        ):
            workbench_actions.resume_with_reply(
                self.canonical,
                reply_text=REPLY_TEXT,
                operation_id="op-resume",
                state_dir=self.state_dir,
            )
        self.assertEqual(
            operations.OPERATION_UNCONFIRMED, self.row("op-resume")["status"]
        )

    def test_the_reply_text_never_reaches_the_journal(self) -> None:
        question_id = self.make_waiting()
        secret = "the-operator-typed-this"
        workbench_actions.reply_to_question(
            self.canonical,
            text=secret,
            operation_id="op-reply",
            state_dir=self.state_dir,
            question_id=question_id,
        )
        raw = workbench_operations.journal_path_for_plan(
            self.canonical, self.state_dir
        ).read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertIn(b"op-reply", raw)


class LaunchIsolationTests(_PlanFixture):
    """A run outlives the tooling that started it."""

    def test_a_launched_run_gets_its_own_session(self) -> None:
        """Ctrl-C in the launcher's terminal must not reach the worker.

        A Workbench server stopped at the keyboard, a bridge whose connector
        went away, a closing terminal -- each signals a process *group*. A child
        in the launcher's group would be killed mid-turn by an event that has
        nothing to do with the plan.
        """
        spawned: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            spawned.append(process)
            return process

        workbench.launch_plan(
            self.canonical, state_dir=self.state_dir, popen_factory=factory
        )
        self.assertEqual(1, len(spawned))
        self.assertTrue(spawned[0].start_new_session)

    def test_a_resume_streaming_a_reply_is_isolated_too(self) -> None:
        self.make_paused()
        spawned: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            spawned.append(process)
            return process

        workbench.resume_plan_with_reply(
            self.canonical,
            reply_text=REPLY_TEXT,
            state_dir=self.state_dir,
            popen_factory=factory,
        )
        self.assertEqual(1, len(spawned))
        self.assertTrue(spawned[0].start_new_session)
        self.assertEqual(REPLY_TEXT, spawned[0].stdin_text)


class SurfaceParityTests(WorkbenchAPIFixture, unittest.TestCase):
    """The local route and the shared action are one gate, not two."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))
        self.plan_dir = self.plans_root / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans.canonicalize_path(self.plan_dir)
        self.server = self.start_workbench_server()

    def _post(self, route: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, payload, _ = _http_request(
            f"{self.base_url(self.server)}{route}",
            method="POST",
            body=body,
            headers=self.auth_headers(self.server),
        )
        return status, payload

    def _result(self) -> workbench.LaunchResult:
        return workbench.LaunchResult(
            pid=os.getpid(),
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical,
            profile_payload=workbench.LaunchProfile().to_payload(),
            reply_provided=False,
            reply_length=None,
            reply_origin=None,
            state_dir=self.state_dir,
        )

    def test_a_retry_that_arrives_at_the_other_surface_does_not_spawn_again(
        self,
    ) -> None:
        """The failure this session is about: a lost response, then a retry.

        The connector submits, the answer never arrives, and the operator
        clicks the button in the browser instead. Two surfaces, one plan, one
        idempotency key -- and one process, because both surfaces admit through
        the same file.
        """
        with mock.patch.object(
            workbench, "launch_plan", return_value=self._result()
        ) as patched:
            first = workbench_actions.launch_run(
                self.canonical, operation_id="shared-1", state_dir=self.state_dir
            )
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "shared-1"},
            )
        patched.assert_called_once()
        self.assertFalse(first.replayed)
        self.assertEqual(200, status)
        self.assertTrue(body["replayed"])
        # The HTTP surface answered from the journal rather than inventing a
        # launch payload for a process it never started.
        self.assertIsNone(body["launch"])
        self.assertEqual("shared-1", body["operation"]["operation_id"])

    def test_a_route_launch_records_the_same_row_the_action_would(self) -> None:
        with mock.patch.object(workbench, "launch_plan", return_value=self._result()):
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "route-1"},
            )
        self.assertEqual(200, status)
        self.assertEqual(operations.OPERATION_CONSUMED, body["operation"]["status"])
        self.assertEqual(
            operations.METHOD_LAUNCH,
            workbench_operations.read_operation(
                self.canonical, "route-1", state_dir=self.state_dir
            )["method"],
        )

    def test_the_route_still_refuses_a_plan_a_run_already_holds(self) -> None:
        """Admission is a gate in front of the ownership check, not instead."""
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "paused": False,
                "harness": "claude",
                "run_id": RUN_ID,
            },
        )
        _write_lock_file(
            self.canonical,
            self.state_dir,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        with mock.patch.object(workbench, "launch_plan") as patched:
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "route-1"},
            )
        self.assertEqual(409, status)
        self.assertEqual("run-already-active", body["reason"])
        patched.assert_not_called()
        record = workbench_operations.read_operation(
            self.canonical, "route-1", state_dir=self.state_dir
        )
        assert record is not None
        self.assertEqual(operations.OPERATION_CANCELLED, record["status"])


class EventRecordShapeTests(_PlanFixture):
    """The witness reads the record ASL actually writes, not a copy of it."""

    def test_the_event_name_is_asls_own(self) -> None:
        self.assertEqual(events.REPLY_PROVIDED_EVENT, actions.REPLY_PROVIDED_EVENT)

    def test_the_log_the_witness_reads_is_the_one_the_runner_appends_to(
        self,
    ) -> None:
        self.assertEqual(
            state.plan_state_dir(self.canonical, self.state_dir)
            / events.EVENTS_FILE_NAME,
            workbench_operations.events_path_for_plan(self.canonical, self.state_dir),
        )


if __name__ == "__main__":
    unittest.main()
