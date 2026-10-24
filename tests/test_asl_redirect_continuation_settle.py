"""Settling a redirected turn into the next attempt of the same session.

Split from tests/test_asl_redirect_continuation.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    harnesses,
    plans_models,
    plans_snapshot,
    redirect_continuation,
    redirect_control,
    run_loop_control_redirects,
    run_termination,
    turn_dispositions,
)

from .asl_redirect_continuation_support import (
    FEEDBACK,
    SESSION_ID,
    SESSION_LABEL,
    UNCHANGED,
    WORKER_PID,
    FakeController,
    JournalTestCase,
    target,
    termination_report,
)
from .support import (
    write_plan,
)
from .support_asl_turn_dispositions import FakeRunContext

ASL_TEST_PORTABLE = True


class SettleRedirectedTurnTests(JournalTestCase):
    """The branch that turns one correction into the next attempt."""

    def setUp(self) -> None:
        super().setUp()
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(event_path=self.event_path)
        write_plan(self.plan_dir, ("pending",))
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        self.after = plans_snapshot.inspect_plan(self.plan_dir)
        self.turn_payload: dict[str, Any] = {"turn_index": 1}
        self.session_details: dict[str, Any] = {"session_id": SESSION_ID}
        self.listener = mock.Mock()
        self.listener.state.abort_turn_requested = False
        self.listener.state.stop_after_current = False

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.artifact_dir,
            "result": self.artifact_dir / "result.json",
        }

    def redirected_gate(
        self, **controller_kwargs: Any
    ) -> redirect_continuation.TurnRedirect:
        """A gate that has already accepted one correction."""
        request = self.submit()
        controller_kwargs.setdefault("report", termination_report())
        turn_redirect, _ = self.gate(controller=FakeController(**controller_kwargs))
        turn_redirect.bind_worker(WORKER_PID)
        turn_redirect.accept(request)
        return turn_redirect

    def settle(
        self,
        *,
        after: plans_models.PlanSnapshot | None | object = UNCHANGED,
        redirect: redirect_continuation.TurnRedirect | None = None,
        pending: redirect_continuation.PendingRedirect | None = None,
        returncode: int = 137,
    ) -> redirect_continuation.PendingRedirect | Any:
        return turn_dispositions.settle_redirected_turn(
            self.ctx,
            self.listener,
            before=self.before,
            after=self.after if after is UNCHANGED else after,
            redirect=redirect if redirect is not None else self.redirected_gate(),
            harness="codex",
            conversation_id="conv-1",
            session_id=SESSION_ID,
            session_label=SESSION_LABEL,
            turn_index=1,
            result=harnesses.HarnessResult(
                argv=["codex"],
                returncode=returncode,
                stdout="",
                stderr="",
                aborted=returncode == 137,
            ),
            turn_payload=self.turn_payload,
            paths=self.paths(),
            session_details=self.session_details,
            pending=pending,
        )

    def continuations(self) -> list[dict[str, Any]]:
        return [
            payload
            for name, payload in self.ctx.persistence.events
            if name == events.REDIRECT_CONTINUATION_EVENT
        ]

    def claimed_complete(self) -> plans_models.PlanSnapshot:
        write_plan(self.plan_dir, ("complete",))
        return plans_snapshot.inspect_plan(self.plan_dir)

    def plan_status(self) -> str:
        """What the session file says now, rather than what a snapshot said."""
        return plans_snapshot.inspect_plan(self.plan_dir).sessions[0].status

    def test_the_correction_becomes_the_next_attempt_of_the_same_session(self) -> None:
        outcome = self.settle()

        assert outcome is not None
        self.assertEqual(SESSION_ID, outcome.pending.session_id)
        self.assertEqual("codex", outcome.pending.harness)
        self.assertEqual("conv-1", outcome.pending.conversation_id)
        self.assertEqual(("op-1",), outcome.pending.operation_ids)
        self.assertEqual(1, outcome.pending.attempt)
        self.assertIs(self.after, outcome.snapshot)
        self.assertEqual([], self.ctx.pauses)

    def test_the_continuation_is_published_before_anything_acts_on_it(self) -> None:
        self.settle()

        records = self.continuations()
        self.assertEqual(
            [turn_dispositions.REDIRECT_OUTCOME_CONTINUED],
            [record["outcome"] for record in records],
        )
        self.assertEqual(("op-1",), tuple(records[0]["redirect"]["operation_ids"]))
        self.assertNotIn(FEEDBACK, json.dumps(records[0]))
        # The turn's own record carries the same claim, and the run state was
        # written under the same hold.
        self.assertEqual(
            turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
            self.turn_payload["redirect_continuation"]["outcome"],
        )
        written = json.loads(self.paths()["result"].read_text(encoding="utf-8"))
        self.assertEqual(
            turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
            written["redirect_continuation"]["outcome"],
        )
        self.assertEqual(1, len(self.ctx.persistence.states))

    def test_a_completion_the_turn_never_settled_is_taken_back(self) -> None:
        outcome = self.settle(after=self.claimed_complete(), returncode=0)

        assert outcome is not None
        self.assertEqual("pending", self.plan_status())
        record = self.turn_payload["completion_reconciliation"]
        self.assertEqual(turn_dispositions.REDIRECT_COMPLETION_REASON, record["reason"])
        self.assertIs(True, record["reverted"])
        # The plan image handed back is the reconciled one, so the next attempt
        # runs against the session as it now is rather than as it was claimed.
        assert outcome.snapshot is not None
        self.assertEqual("pending", outcome.snapshot.sessions[0].status)
        self.assertEqual(record, self.continuations()[0]["completion_reconciliation"])

    def test_a_session_that_claimed_nothing_is_left_alone(self) -> None:
        self.settle()

        self.assertNotIn("completion_reconciliation", self.turn_payload)
        self.assertEqual(
            [],
            [
                name
                for name, _ in self.ctx.persistence.events
                if name == events.SESSION_REOPENED_EVENT
            ],
        )

    def test_an_uninspectable_plan_still_carries_the_correction(self) -> None:
        outcome = self.settle(after=None)

        assert outcome is not None
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(("op-1",), outcome.pending.operation_ids)
        self.assertNotIn("completion_reconciliation", self.turn_payload)

    def test_a_predecessor_that_could_not_be_shown_gone_pauses_the_run(self) -> None:
        blocked = redirect_control.SuccessorBlockedError(
            "predecessor-uncollected", termination_report(collected=False)
        )

        outcome = self.settle(redirect=self.redirected_gate(blocked=blocked))

        self.assertIsNone(outcome)
        pause = self.ctx.pauses[0]
        self.assertEqual(
            events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED, pause["reason"]
        )
        self.assertIn("predecessor-uncollected", pause["message"])
        self.assertEqual(
            "predecessor-uncollected",
            pause["details"]["redirect_continuation"]["reason"],
        )
        self.assertEqual([self.after], self.ctx.final_snapshots)
        self.assertEqual(
            [turn_dispositions.REDIRECT_OUTCOME_BLOCKED],
            [record["outcome"] for record in self.continuations()],
        )

    def test_a_block_is_decided_before_the_completion_is_touched(self) -> None:
        blocked = redirect_control.SuccessorBlockedError(
            "termination-unconfirmed", termination_report(complete=False)
        )

        self.settle(
            after=self.claimed_complete(),
            redirect=self.redirected_gate(blocked=blocked),
            returncode=0,
        )

        # Nothing may run beside a live predecessor, including the reverter.
        self.assertNotIn("completion_reconciliation", self.turn_payload)
        self.assertEqual("complete", self.plan_status())

    def test_an_abort_queued_after_the_correction_outranks_it(self) -> None:
        self.listener.state.abort_turn_requested = True

        with mock.patch.object(run_termination, "_honor_abort_turn") as honor:
            outcome = self.settle()

        self.assertIsNone(outcome)
        honor.assert_called_once()
        self.assertIs(self.listener, honor.call_args.args[0])
        self.assertIs(self.after, honor.call_args.kwargs["snapshot"])
        self.assertEqual([], self.ctx.pauses)
        record = self.continuations()[0]
        self.assertEqual(turn_dispositions.REDIRECT_OUTCOME_STOPPED, record["outcome"])
        self.assertEqual("abort-turn", record["stopped_by"])
        self.assertTrue(record["termination"]["permits_continuation"])

    def test_a_stop_queued_after_the_correction_outranks_it(self) -> None:
        self.listener.state.stop_after_current = True

        with mock.patch.object(run_termination, "_honor_stop_after_current") as honor:
            outcome = self.settle()

        self.assertIsNone(outcome)
        honor.assert_called_once()
        record = self.continuations()[0]
        self.assertEqual(turn_dispositions.REDIRECT_OUTCOME_STOPPED, record["outcome"])
        self.assertEqual("stop-after-current", record["stopped_by"])

    def test_a_stopped_run_still_takes_back_the_completion_claim(self) -> None:
        self.listener.state.stop_after_current = True

        with mock.patch.object(run_termination, "_honor_stop_after_current") as honor:
            self.settle(after=self.claimed_complete(), returncode=0)

        self.assertEqual("pending", self.plan_status())
        self.assertEqual(
            "pending", honor.call_args.kwargs["snapshot"].sessions[0].status
        )

    def test_a_second_correction_rides_the_lineage_of_the_first(self) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            (
                redirect_continuation.AcceptedRedirect(
                    operation_id="op-0",
                    target=target(),
                    accepted_at=clock.utc_iso_now(),
                    feedback_digest="digest-0",
                    feedback_bytes=4,
                ),
            ),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        outcome = self.settle(pending=first)

        assert outcome is not None
        self.assertEqual(("op-0", "op-1"), outcome.pending.operation_ids)
        self.assertEqual(2, outcome.pending.attempt)


class RedirectedHarnessTests(unittest.TestCase):
    """A queued harness switch is about the next session, not this attempt."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = FakeRunContext(event_path=Path(self._tmp.name) / "events.jsonl")

    def pending(
        self, *, harness: str | None = "codex", session_id: str = SESSION_ID
    ) -> redirect_continuation.PendingRedirect:
        return redirect_continuation.PendingRedirect(
            session_id=session_id,
            harness=harness,
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=("op-1",),
            feedback_digests=("digest",),
            feedback_bytes=4,
        )

    def choose(
        self,
        pending: redirect_continuation.PendingRedirect | None,
        *,
        selected: str = "claude",
        session_id: str = SESSION_ID,
    ) -> str:
        return run_loop_control_redirects._redirected_turn_harness(
            self.ctx,
            pending=pending,
            selected=selected,
            session_id=session_id,
            turn_index=2,
        )

    def deferrals(self) -> list[dict[str, Any]]:
        return [
            payload
            for name, payload in self.ctx.persistence.events
            if name == events.REDIRECT_CONTINUATION_EVENT
        ]

    def test_the_corrected_conversation_keeps_its_harness(self) -> None:
        self.assertEqual("codex", self.choose(self.pending()))

        record = self.deferrals()[0]
        self.assertEqual("harness-deferred", record["outcome"])
        self.assertEqual("claude", record["deferred_harness"])
        self.assertEqual("codex", record["redirect"]["harness"])

    def test_a_turn_carrying_no_correction_takes_the_selection(self) -> None:
        self.assertEqual("claude", self.choose(None))
        self.assertEqual([], self.deferrals())

    def test_a_correction_from_another_session_does_not_pin_this_one(self) -> None:
        self.assertEqual("claude", self.choose(self.pending(session_id="2")))
        self.assertEqual([], self.deferrals())

    def test_a_selection_that_changes_nothing_is_not_a_deferral(self) -> None:
        self.assertEqual("codex", self.choose(self.pending(), selected="codex"))
        self.assertEqual("claude", self.choose(self.pending(harness=None)))
        self.assertEqual([], self.deferrals())
