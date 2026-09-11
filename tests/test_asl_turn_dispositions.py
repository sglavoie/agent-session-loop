"""The eight terminating branches, exercised without the run loop.

``turn_dispositions`` owns what a turn that did not advance the session does
next: two pauses for a plan that could not be re-inspected, an operator abort
that is recorded rather than paused, a malformed plan that may repair itself, a
restore the provider refused, a worker pause that may be answered out of, a
failed harness exit that may retry off Spark, and a turn an operator corrected
mid-flight, which earns the same session another attempt. These tests drive each
branch through a stand-in run
context defined here rather than through ``runner``, which is the point of the
boundary, and every collaborator is replaced by a recorder so what is asserted
is the decision and the record rather than any collaborator's own behaviour.

What is covered is the shape the module docstring claims, as properties rather
than as a walk through one happy path:

* the two uninspected pauses publish the newest plan image the run holds, which
  is the after-image when there is one and the before-image otherwise;
* an abort is recorded, never paused, and is the only branch that consumes the
  control listener;
* the malformed-plan branch prefers a correction to a reopen, and only the
  correction lets the run continue;
* a runner pause short-circuits its wait when the answer already arrived,
  enters it only when the pause could still be answered, and pins the idle
  baseline exactly where the wait is entered;
* a reply and a continue-resolution each produce a continuation, and a
  pause-resolution does not;
* the Spark retry is refused unless the failed turn left no trace, and its
  run-scoped suppression is set only when the shared ledger could not be
  written;
* a nonzero exit that pauses takes back a completion claim the turn never
  settled, publishes the reconciled plan image rather than the claimed one,
  and says so in the pause when the reversion itself could not be written;
* the refused-restore branch retires the conversation the provider would not
  restore and pauses for recovery, and is argued in
  ``tests/test_asl_native_restore_recovery.py`` alongside the adapter refusals
  that are the only thing that can select it;
* the module owns no loop statement, because the loop is not its to break;
* the redirected branch is dispatched from the same table, and is argued in
  ``tests/test_asl_redirect_continuation.py`` alongside the gate whose
  acceptance is the only thing that can select it;
* the runner's own body reaches these entry points and no longer performs any
  of the branch work itself.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    handoff_correction,
    harness_failures,
    harnesses,
    mailbox,
    model_availability,
    plans,
    run_coordination,
    run_loop,
    run_termination,
    turn_dispositions,
    turn_execution,
)

from .support import assert_no_runner_import, write_plan

RUN_ID = "run-dispositions-1"
SESSION_ID = "1"
SESSION_LABEL = "Session 1"
STATE_DIR_ARG = "/tmp/asl-state-does-not-exist"

#: The entry points the run loop is allowed to reach here: one per terminating
#: branch. The loop keeps the ``if terminating_disposition == ...`` dispatch
#: itself, so a ninth name would be a branch that was never dispatched.
ENTRY_POINTS = (
    "turn_dispositions.pause_interrupted_run",
    "turn_dispositions.pause_timed_out_turn",
    "turn_dispositions.record_aborted_turn",
    "turn_dispositions.settle_malformed_plan",
    "turn_dispositions.settle_restore_refused",
    "turn_dispositions.settle_runner_pause",
    "turn_dispositions.settle_nonzero_exit",
    "turn_dispositions.settle_redirected_turn",
)

#: Branch work that belonged to the loop and now belongs here. Each of these is
#: checked as the *callee* of a call, so the runner's surviving module-level
#: aliases -- which are assignments, not calls -- are deliberately unaffected.
MOVED_CALLS = frozenset(
    {
        "run_termination._honor_abort_turn",
        "handoff_correction._correct_malformed_handoff",
        "handoff_correction._reopen_uninspectable_handoff",
        "run_coordination.MailboxWaitCoordinator",
        "model_availability.suppress_until",
        "_worker_pause_resolution_for_turn",
        "_runner_pause_can_wait_for_mailbox_reply",
        "_runner_pause_reason",
        "_record_mailbox_reply_wait_preparation_failed",
        "classify_harness_failure",
    }
)

#: The mailbox wait seams the loop still resolves and passes in as callbacks.
#: Session 37 moved the loop out of the runner, so they are now read at the
#: module that owns them; in-tree callers patch them there to assert a wait was
#: or was not entered, which only works while the loop resolves them at the
#: call site rather than binding them once at import.
INJECTED_SEAMS = (
    "run_coordination.prepare_mailbox_reply_wait",
    "run_coordination.wait_for_mailbox_reply",
    "run_coordination.finish_mailbox_reply_wait_lifecycle",
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real -- the Spark fallback commits a route change, an artifact
    and an event under one hold, and a fake that did not lock would hide a
    missing hold rather than expose it -- while events and state writes are
    kept in order so a test can assert on what was recorded without reading
    files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))


class FakeRunContext:
    """A :class:`turn_dispositions.RunContext` built by hand, not by a run.

    Only the members these branches reach are populated, which is what the
    Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.event_path = event_path
        self.payload: dict[str, Any] = {
            "run_id": RUN_ID,
            "spark": {"baseline": "sonnet"},
        }
        self.persistence = RecordingPersistence()
        self.plan_cache = plans.PlanSnapshotCache()
        self.control_payload: dict[str, Any] = {}
        self.runner_work_finished_at: Any = None
        self.idle_baseline_pinned = False
        self.final_snapshots: list[plans.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []
        # Present and None, exactly as `run_loop.RunContext` declares it: the
        # crash-recovery branches read it before anything sets it, and absent
        # here that read would raise instead of taking the untouched path.
        self.redirect_recovery_report: Any = None

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None:
        self.final_snapshots.append(snapshot)
        with self.persistence.lock:
            self.payload["final_snapshot"] = plans.snapshot_payload(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


def fake_message(**overrides: Any) -> mailbox.MailboxMessage:
    """One mailbox record, with only the fields these branches read set."""
    values: dict[str, Any] = {
        "seq": 7,
        "id": "msg-1",
        "kind": mailbox.KIND_REPLY,
        "created_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return mailbox.MailboxMessage(**values)


class DispositionTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments a branch needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(event_path=self.event_path)
        write_plan(self.plan_dir, ("pending",))
        self.before = plans.inspect_plan(self.plan_dir)
        self.after = plans.inspect_plan(self.plan_dir)
        self.session_details: dict[str, Any] = {"session_id": SESSION_ID}

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.artifact_dir,
            "result": self.artifact_dir / "result.json",
        }

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        values: dict[str, Any] = {
            "argv": ["claude"],
            "returncode": 1,
            "stdout": "",
            "stderr": "",
        }
        values.update(overrides)
        return harnesses.HarnessResult(**values)


class BoundaryTests(unittest.TestCase):
    """The dependency runs one way, and the loop stays the loop's."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, turn_dispositions)

    def test_the_module_owns_no_loop_statement(self) -> None:
        source = Path(turn_dispositions.__file__ or "").read_text(encoding="utf-8")
        loops = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.While | ast.For)
        ]
        self.assertEqual(
            [],
            loops,
            "a boundary that owns a loop statement is deciding when to stop; "
            "these branches hand a continuation back and let the loop decide",
        )

    def test_the_context_protocol_matches_the_runner_s_own_context(self) -> None:
        fields = {field.name for field in dataclasses.fields(run_loop.RunContext)}
        for name in turn_dispositions.RunContext.__annotations__:
            with self.subTest(member=name):
                self.assertIn(name, fields)
        for name in ("run_id", "event_path", "payload", "persistence"):
            with self.subTest(member=name):
                self.assertIn(name, fields)


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of the branch work."""

    def loop_calls(self) -> set[str]:
        source = Path(run_loop.__file__ or "").read_text(encoding="utf-8")
        return {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }

    def test_the_loop_reaches_exactly_the_declared_entry_points(self) -> None:
        reached = {
            name for name in self.loop_calls() if name.startswith("turn_dispositions.")
        }
        self.assertEqual(set(ENTRY_POINTS), reached)

    def test_the_loop_no_longer_performs_the_branch_work_itself(self) -> None:
        self.assertEqual(
            set(),
            MOVED_CALLS & self.loop_calls(),
            "the terminating branches belong to turn_dispositions now; a "
            "surviving call is work that escaped",
        )

    def test_the_loop_still_hands_in_the_three_mailbox_wait_seams(self) -> None:
        # The counterpart to the assertion above: these three are deliberately
        # *not* imported by the boundary. The loop resolves each one as an
        # attribute of its owning module and passes it in, which is what makes
        # a caller's patch on ``run_coordination`` take effect; binding them to
        # a module-level name here would freeze the pre-patch function instead.
        source = Path(run_loop.__file__ or "").read_text(encoding="utf-8")
        tree = ast.parse(source)
        loaded = {
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
        }
        for name in INJECTED_SEAMS:
            with self.subTest(seam=name):
                self.assertIn(name, loaded)


class UninspectedTurnTests(DispositionTestCase):
    """Turns the plan could not be re-inspected after."""

    def test_an_interrupted_turn_publishes_the_after_image_when_it_has_one(
        self,
    ) -> None:
        turn_dispositions.pause_interrupted_run(
            self.ctx,
            before=self.before,
            after=self.after,
            session_details=self.session_details,
        )
        self.assertEqual(
            plans.snapshot_payload(self.after), self.ctx.payload["final_snapshot"]
        )
        self.assertEqual(
            [
                {
                    "reason": "interrupted",
                    "message": "run interrupted by user",
                    "details": self.session_details,
                }
            ],
            self.ctx.pauses,
        )

    def test_an_interrupted_turn_falls_back_to_the_before_image(self) -> None:
        turn_dispositions.pause_interrupted_run(
            self.ctx,
            before=self.before,
            after=None,
            session_details=self.session_details,
        )
        self.assertEqual(
            plans.snapshot_payload(self.before), self.ctx.payload["final_snapshot"]
        )

    def test_a_timed_out_turn_names_the_session_in_its_message(self) -> None:
        turn_dispositions.pause_timed_out_turn(
            self.ctx,
            before=self.before,
            after=None,
            session_label=SESSION_LABEL,
            session_details=self.session_details,
        )
        self.assertEqual(
            plans.snapshot_payload(self.before), self.ctx.payload["final_snapshot"]
        )
        self.assertEqual("timeout", self.ctx.pauses[0]["reason"])
        self.assertEqual(
            f"harness turn timed out for {SESSION_LABEL}",
            self.ctx.pauses[0]["message"],
        )

    def test_an_abort_is_recorded_rather_than_paused(self) -> None:
        listener = mock.Mock()
        with mock.patch.object(run_termination, "_honor_abort_turn") as honor:
            turn_dispositions.record_aborted_turn(
                self.ctx,
                listener,
                before=self.before,
                after=self.after,
                session_id=SESSION_ID,
                turn_payload={"turn_index": 1},
            )
        self.assertEqual([], self.ctx.pauses)
        honor.assert_called_once()
        self.assertIs(listener, honor.call_args.args[0])
        self.assertIs(self.after, honor.call_args.kwargs["snapshot"])

    def test_an_abort_falls_back_to_the_before_image(self) -> None:
        with mock.patch.object(run_termination, "_honor_abort_turn") as honor:
            turn_dispositions.record_aborted_turn(
                self.ctx,
                mock.Mock(),
                before=self.before,
                after=None,
                session_id=SESSION_ID,
                turn_payload={},
            )
        self.assertIs(self.before, honor.call_args.kwargs["snapshot"])


class MalformedPlanTests(DispositionTestCase):
    """The one terminating branch that may put the run back on its feet."""

    def settle(
        self,
        *,
        corrected: tuple[Any, Any],
        reopened: tuple[Any, Any] = (None, None),
    ) -> Any:
        with (
            mock.patch.object(
                handoff_correction,
                "_correct_malformed_handoff",
                return_value=corrected,
            ),
            mock.patch.object(
                handoff_correction,
                "_reopen_uninspectable_handoff",
                return_value=reopened,
            ),
            mock.patch.object(turn_execution, "record_turn_rejected") as rejected,
        ):
            self.rejected = rejected
            return turn_dispositions.settle_malformed_plan(
                self.ctx,
                before=self.before,
                workspace=None,
                session_id=SESSION_ID,
                turn_index=1,
                plan_error="unparsable handoff",
                turn_payload={},
                paths=self.paths(),
                session_details=self.session_details,
            )

    def test_a_corrected_handoff_retries_without_pausing(self) -> None:
        outcome = self.settle(corrected=(self.after, {"attempts": 1}))
        self.assertIs(self.after, outcome)
        self.assertEqual([], self.ctx.pauses)
        self.rejected.assert_not_called()

    def test_an_uncorrectable_handoff_pauses_and_records_the_rejection(self) -> None:
        outcome = self.settle(corrected=(None, {"exhausted": True}))
        self.assertIsNone(outcome)
        self.assertEqual("malformed-plan", self.ctx.pauses[0]["reason"])
        details = self.ctx.pauses[0]["details"]
        self.assertEqual("unparsable handoff", details["error"])
        self.assertEqual({"exhausted": True}, details["handoff_correction"])
        self.assertEqual(SESSION_ID, details["session_id"])
        self.rejected.assert_called_once()
        self.assertEqual("plan-inspection", self.rejected.call_args.kwargs["guard"])

    def test_a_reopened_session_becomes_the_final_snapshot(self) -> None:
        evidence = {"reopened": True}
        self.settle(
            corrected=(None, {"exhausted": True}),
            reopened=(self.after, evidence),
        )
        self.assertEqual([self.after], self.ctx.final_snapshots)
        self.assertEqual(evidence, self.ctx.pauses[0]["details"]["handoff_reopen"])
        self.assertIs(True, self.rejected.call_args.kwargs["reopened"])

    def test_a_reopen_that_found_nothing_keeps_the_before_image(self) -> None:
        self.settle(corrected=(None, None), reopened=(None, {"reopened": False}))
        self.assertEqual([self.before], self.ctx.final_snapshots)
        self.assertIs(False, self.rejected.call_args.kwargs["reopened"])


class RunnerPauseTests(DispositionTestCase):
    """A worker's own pause, and the answer that can undo it."""

    def setUp(self) -> None:
        super().setUp()
        self.prepared: Any = mock.Mock()
        self.prepared.question = fake_message(id="question-1")
        self.prepared.context = mock.Mock()
        self.wait_calls: list[dict[str, Any]] = []

    def settle(
        self,
        *,
        resolution_for_turn: Any = None,
        can_wait: bool = True,
        wait_result: Any = None,
        prepare_error: Exception | None = None,
        mailbox_enabled: bool = True,
    ) -> Any:
        coordinator = mock.Mock()
        coordinator.return_value.run.return_value = wait_result

        def prepare_wait(**kwargs: Any) -> Any:
            self.wait_calls.append(kwargs)
            if prepare_error is not None:
                raise prepare_error
            return self.prepared

        with (
            mock.patch.object(
                run_coordination,
                "worker_pause_resolution_for_turn",
                return_value=resolution_for_turn,
            ),
            mock.patch.object(
                run_coordination,
                "runner_pause_can_wait_for_mailbox_reply",
                return_value=can_wait,
            ),
            mock.patch.object(run_coordination, "MailboxWaitCoordinator", coordinator),
            mock.patch.object(
                run_coordination, "record_mailbox_reply_wait_preparation_failed"
            ) as failed,
        ):
            self.coordinator = coordinator
            self.preparation_failed = failed
            return turn_dispositions.settle_runner_pause(
                self.ctx,
                after=self.after,
                session_id=SESSION_ID,
                turn_index=1,
                result=self.result(returncode=0),
                turn_payload={
                    "runner_pause": "blocked - needs a decision",
                    "runner_pause_source": "worker",
                },
                session_details=self.session_details,
                paused_status_repair={"repaired": True},
                mailbox_enabled=mailbox_enabled,
                mailbox_poll=mock.Mock(),
                plan_state=self.tmp_dir / "state",
                state_path=self.tmp_dir / "state" / "state.json",
                state_dir_arg=STATE_DIR_ARG,
                prepare_wait=prepare_wait,
                wait_for_reply=mock.Mock(),
                finish_lifecycle=mock.Mock(),
            )

    def test_an_answer_that_beat_the_pause_short_circuits_the_wait(self) -> None:
        resolution = fake_message(
            id="res-1",
            kind=mailbox.KIND_RESOLUTION,
            to="question-1",
            action=mailbox.RESOLUTION_ACTION_CONTINUE,
            note="carry on",
        )
        outcome = self.settle(resolution_for_turn=resolution)
        self.assertIsNone(outcome)
        self.assertEqual([], self.wait_calls)
        self.coordinator.assert_not_called()
        recorded = self.ctx.pauses[0]["details"]["mailbox_resolution"]
        self.assertEqual("question-1", recorded["question_id"])
        self.assertEqual("res-1", recorded["resolution_message_id"])
        self.assertEqual(len("carry on"), recorded["note_length"])
        self.assertEqual(mailbox.WAITER_ORIGIN_WORKER, recorded["waiter_origin"])

    def test_the_pause_carries_its_source_and_status_repair(self) -> None:
        self.settle(can_wait=False)
        details = self.ctx.pauses[0]["details"]
        self.assertEqual("worker", details["source"])
        self.assertEqual({"repaired": True}, details["handoff_status_repair"])
        self.assertEqual("blocked - needs a decision", self.ctx.pauses[0]["message"])
        self.assertEqual([self.after], self.ctx.final_snapshots)

    def test_a_pause_that_cannot_be_answered_never_enters_the_wait(self) -> None:
        outcome = self.settle(can_wait=False)
        self.assertIsNone(outcome)
        self.assertEqual([], self.wait_calls)
        self.assertFalse(self.ctx.idle_baseline_pinned)

    def test_a_reply_produces_the_continuation_and_pins_the_idle_baseline(
        self,
    ) -> None:
        reply = fake_message(id="reply-1", seq=11, text="do this instead")
        outcome = self.settle(
            wait_result=run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_REPLY,
                pending_outbox=0,
                reply=reply,
            )
        )
        assert outcome is not None
        self.assertIs(self.after, outcome.snapshot)
        self.assertIsNone(outcome.pending_resolution)
        assert outcome.pending_reply is not None
        self.assertEqual("do this instead", outcome.pending_reply.text)
        self.assertEqual("question-1", outcome.pending_reply.question_id)
        self.assertEqual("reply-1", outcome.pending_reply.reply_message_id)
        self.assertEqual(11, outcome.pending_reply.reply_seq)
        self.assertFalse(outcome.pending_reply.process_start)
        self.assertEqual([], self.ctx.pauses)
        self.assertTrue(self.ctx.idle_baseline_pinned)
        self.assertIsNotNone(self.ctx.runner_work_finished_at)

    def test_a_continue_resolution_produces_an_answerless_continuation(self) -> None:
        resolution = fake_message(
            id="res-2",
            seq=12,
            kind=mailbox.KIND_RESOLUTION,
            action=mailbox.RESOLUTION_ACTION_CONTINUE,
            note="proceed",
        )
        outcome = self.settle(
            wait_result=run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution=resolution,
            )
        )
        assert outcome is not None
        self.assertIsNone(outcome.pending_reply)
        assert outcome.pending_resolution is not None
        self.assertEqual("proceed", outcome.pending_resolution.note)
        self.assertEqual("question-1", outcome.pending_resolution.question_id)
        self.assertEqual("res-2", outcome.pending_resolution.resolution_message_id)
        self.assertEqual([], self.ctx.pauses)

    def test_a_pause_resolution_leaves_the_run_paused(self) -> None:
        resolution = fake_message(
            id="res-3",
            kind=mailbox.KIND_RESOLUTION,
            action=mailbox.RESOLUTION_ACTION_PAUSE,
        )
        outcome = self.settle(
            wait_result=run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_RESOLUTION,
                pending_outbox=0,
                resolution=resolution,
            )
        )
        self.assertIsNone(outcome)
        self.assertEqual(1, len(self.ctx.pauses))

    def test_a_timed_out_wait_leaves_the_run_paused(self) -> None:
        outcome = self.settle(
            wait_result=run_coordination.MailboxWaitResult(
                reason=run_coordination.WAIT_REASON_TIMEOUT, pending_outbox=0
            )
        )
        self.assertIsNone(outcome)
        self.assertEqual(1, len(self.ctx.pauses))

    def test_an_unpreparable_wait_is_recorded_and_the_run_pauses(self) -> None:
        outcome = self.settle(
            prepare_error=run_coordination.MailboxWaitUnavailable("no-state-path")
        )
        self.assertIsNone(outcome)
        self.coordinator.assert_not_called()
        self.preparation_failed.assert_called_once()
        self.assertEqual(
            "no-state-path", self.preparation_failed.call_args.kwargs["reason"]
        )
        self.assertFalse(self.ctx.idle_baseline_pinned)
        self.assertEqual(1, len(self.ctx.pauses))

    def test_a_disabled_mailbox_asks_for_no_prior_resolution(self) -> None:
        with mock.patch.object(
            run_coordination, "worker_pause_resolution_for_turn"
        ) as lookup:
            self.settle(mailbox_enabled=False, can_wait=False)
        lookup.assert_not_called()


class NonzeroExitTests(DispositionTestCase):
    """A failed harness exit, and the one retry it can earn."""

    def classification(self, reason: str) -> harness_failures.FailureClassification:
        return harness_failures.FailureClassification(
            reason=reason,
            retryable=True,
            fingerprint="fp",
            evidence="",
            returncode=1,
            retry_at="2026-01-01T01:00:00Z",
        )

    def settle(
        self,
        *,
        reason: str = events.PAUSE_REASON_QUOTA_EXHAUSTED,
        decision: str = "spark",
        after: plans.PlanSnapshot | None = None,
        cooldown: Any = None,
        suppress_error: Exception | None = None,
        spark_run_suppression: str | None = None,
    ) -> Any:
        self.spark_route: dict[str, Any] = {"decision": decision}
        self.turn_payload: dict[str, Any] = {}
        suppress = mock.Mock(return_value=cooldown, side_effect=suppress_error)
        with (
            mock.patch.object(
                harness_failures,
                "classify_harness_failure",
                return_value=self.classification(reason),
            ),
            mock.patch.object(model_availability, "suppress_until", suppress),
        ):
            self.suppress = suppress
            return turn_dispositions.settle_nonzero_exit(
                self.ctx,
                before=self.before,
                after=self.after if after is None else after,
                result=self.result(returncode=2),
                spark_route=self.spark_route,
                session_id=SESSION_ID,
                session_label=SESSION_LABEL,
                turn_index=1,
                turn_payload=self.turn_payload,
                session_details=self.session_details,
                paths=self.paths(),
                state_dir_arg=STATE_DIR_ARG,
                spark_run_suppression=spark_run_suppression,
            )

    def cooldown(self) -> Any:
        record = mock.Mock()
        record.to_dict.return_value = {"available": False, "retry_at": "later"}
        return record

    def test_a_retryable_spark_failure_falls_back_and_retries(self) -> None:
        outcome = self.settle(cooldown=self.cooldown())
        assert outcome is not None
        self.assertIs(self.after, outcome.snapshot)
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, outcome.fallback_reason)
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual(
            events.PAUSE_REASON_QUOTA_EXHAUSTED,
            self.spark_route["fallback_reason"],
        )
        self.assertEqual(self.spark_route, self.turn_payload["spark_route"])
        self.assertEqual(
            ["spark_fallback"], [name for name, _ in self.ctx.persistence.events]
        )
        self.assertEqual(1, len(self.ctx.persistence.states))
        self.assertTrue(self.paths()["result"].exists())

    def test_a_written_ledger_leaves_the_run_scoped_suppression_alone(self) -> None:
        outcome = self.settle(
            cooldown=self.cooldown(), spark_run_suppression="already-set"
        )
        assert outcome is not None
        self.assertEqual("already-set", outcome.run_suppression)
        self.assertIs(False, self.ctx.payload["spark"]["available"])

    def test_an_unwritable_ledger_suppresses_spark_for_this_run_only(self) -> None:
        outcome = self.settle(suppress_error=OSError("read-only"))
        assert outcome is not None
        self.assertEqual(
            f"{events.PAUSE_REASON_QUOTA_EXHAUSTED}-for-current-run",
            outcome.run_suppression,
        )
        self.assertEqual(outcome.run_suppression, self.ctx.payload["spark"]["reason"])
        self.assertIsNone(self.ctx.payload["spark"]["retry_at"])
        self.assertIn("read-only", self.ctx.payload["spark"]["warning"])

    def test_a_spark_failure_that_left_a_trace_is_not_retried(self) -> None:
        write_plan(self.plan_dir, ("complete",))
        moved = plans.inspect_plan(self.plan_dir)
        outcome = self.settle(after=moved)
        self.assertIsNone(outcome)
        self.suppress.assert_not_called()
        self.assertIs(True, self.ctx.pauses[0]["details"]["spark_fallback_unsafe"])

    def test_a_non_spark_failure_pauses_with_its_classification(self) -> None:
        outcome = self.settle(decision="baseline")
        self.assertIsNone(outcome)
        self.suppress.assert_not_called()
        pause = self.ctx.pauses[0]
        self.assertEqual(events.PAUSE_REASON_QUOTA_EXHAUSTED, pause["reason"])
        self.assertEqual(
            f"{SESSION_LABEL} failed: {events.PAUSE_REASON_QUOTA_EXHAUSTED} "
            "(harness exit 2)",
            pause["message"],
        )
        self.assertEqual(2, pause["details"]["returncode"])
        self.assertNotIn("spark_fallback_unsafe", pause["details"])
        self.assertEqual([self.after], self.ctx.final_snapshots)

    def test_a_plain_nonzero_exit_reports_the_status_rather_than_a_reason(
        self,
    ) -> None:
        self.settle(reason=events.PAUSE_REASON_NONZERO_EXIT, decision="baseline")
        self.assertEqual(
            f"harness exited with status 2 for {SESSION_LABEL}",
            self.ctx.pauses[0]["message"],
        )

    def claimed_complete(self) -> plans.PlanSnapshot:
        """The after-image of a turn that claimed the session and then died."""
        write_plan(self.plan_dir, ("complete",))
        return plans.inspect_plan(self.plan_dir)

    def test_an_unsettled_completion_is_taken_back_before_the_run_pauses(
        self,
    ) -> None:
        self.settle(after=self.claimed_complete(), decision="baseline")
        reverted = plans.inspect_plan(self.plan_dir)
        self.assertEqual("pending", reverted.sessions[0].status)
        record = self.turn_payload["completion_reconciliation"]
        self.assertIs(True, record["reverted"])
        self.assertEqual("complete", record["claimed_status"])
        self.assertEqual("pending", record["restored_status"])
        self.assertEqual(2, record["returncode"])
        self.assertEqual(
            record, self.ctx.pauses[0]["details"]["completion_reconciliation"]
        )

    def test_the_published_snapshot_is_the_reconciled_one(self) -> None:
        self.settle(after=self.claimed_complete(), decision="baseline")
        published = self.ctx.final_snapshots[0]
        self.assertEqual("pending", published.sessions[0].status)
        self.assertEqual(
            plans.snapshot_payload(published), self.ctx.payload["final_snapshot"]
        )

    def test_the_reversion_is_recorded_as_a_reopen(self) -> None:
        self.settle(after=self.claimed_complete(), decision="baseline")
        reopens = [
            payload
            for name, payload in self.ctx.persistence.events
            if name == events.SESSION_REOPENED_EVENT
        ]
        self.assertEqual(1, len(reopens))
        self.assertEqual("complete", reopens[0]["from_status"])
        self.assertEqual("pending", reopens[0]["to_status"])
        self.assertEqual("turn-completion-reconciliation", reopens[0]["guard"])
        self.assertEqual(
            plans.inspect_plan(self.plan_dir).content_sha256,
            reopens[0]["content_sha256"],
        )

    def test_a_session_that_claimed_nothing_is_left_alone(self) -> None:
        self.settle(decision="baseline")
        self.assertNotIn("completion_reconciliation", self.turn_payload)
        self.assertNotIn("completion_reconciliation", self.ctx.pauses[0]["details"])
        self.assertEqual([], [name for name, _ in self.ctx.persistence.events])
        self.assertEqual([self.after], self.ctx.final_snapshots)

    def test_an_unwritable_session_file_still_reports_the_claim_it_kept(
        self,
    ) -> None:
        after = self.claimed_complete()
        with mock.patch.object(
            turn_dispositions.handoff_guards,
            "_revert_session_status_to_pending",
            return_value=None,
        ):
            self.settle(after=after, decision="baseline")
        record = self.turn_payload["completion_reconciliation"]
        self.assertIs(False, record["reverted"])
        self.assertEqual("complete", record["restored_status"])
        self.assertIsNone(record["session_path"])
        self.assertEqual([], [name for name, _ in self.ctx.persistence.events])
        self.assertEqual([after], self.ctx.final_snapshots)

    def test_a_retryable_spark_failure_reconciles_nothing(self) -> None:
        outcome = self.settle(cooldown=self.cooldown())
        self.assertIsNotNone(outcome)
        self.assertNotIn("completion_reconciliation", self.turn_payload)


if __name__ == "__main__":
    unittest.main()
